"""Installed-app, real PostgreSQL/APISIX SLA proof with exact owned fixture cleanup.

Reuses the guarded transaction acceptance fixture. No real bank payment or
historical order mutation. Evidence never contains bearer tokens/API keys.
"""
import argparse
import importlib.util
import json
import math
from pathlib import Path
import time


def sparse_request_result_valid(metric, *, future_term):
    if future_term:
        return (metric.get('status') == 'not_started'
                and metric.get('reason') == 'service_not_started'
                and metric.get('value') is None)
    return metric.get('status') == 'insufficient_data' and metric.get('value') is None


def load_fixture():
    path = Path(__file__).with_name('api_auto_delivery_runtime_acceptance.py')
    spec = importlib.util.spec_from_file_location('sla_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args):
    from fastapi.testclient import TestClient
    from sqlalchemy import delete, select
    fixture = load_fixture()
    path = Path(args.manifest)
    saved = fixture.fixture_ids()
    saved.update(consumers=[], quota_windows=[])
    fixture.write_manifest(path, saved, create=True)
    m = fixture.load_main()
    m.engine.hide_parameters = True
    saved['environment'] = fixture.environment(m)
    fixture.write_manifest(path, saved)
    fixture.preflight(m, saved)
    saved['authorized'] = True
    fixture.write_manifest(path, saved)
    browser = path.with_suffix('.private-session.json')
    report = {'status': 'running', 'source': 'installed app with real PostgreSQL and APISIX',
              'payment': 'simulated', 'bank_payment_tested': False}
    probe_consumers = []

    def remove_owned_facts():
        with m.SessionLocal() as db:
            for model in (m.sla_gateway_events['Model'], m.sla_probe_collector['Slot']):
                db.execute(delete(model).where(model.product_id.in_(saved['products'])))
            db.commit()

    try:
        report['transaction'] = fixture.exercise(m, saved, path, args.gateway_url,
                                                browser_fixture=str(browser))
        token = json.loads(browser.read_text())['token']
        with m.SessionLocal() as db:
            contracts = list(db.scalars(select(m.order_sla_contracts['Contract']).join(m.Order,
                m.Order.id == m.order_sla_contracts['Contract'].order_id).where(
                m.Order.product_id.in_(saved['products']))))
            fixture.check(bool(contracts) and all(row.verified_at for row in contracts),
                          'Paid fixtures lack verified immutable SLA contracts')
            contract_ids = [row.id for row in contracts]
            ops_token = m.issue_token(db.get(m.User, saved['users']['ops']))
            routes = list(db.scalars(select(m.ApiGatewayRoute).where(
                m.ApiGatewayRoute.product_id.in_(saved['products']))))
            from app.sla_probe_runtime import probe_consumer_name
            for route in routes:
                version_id = saved['versions'][saved['products'].index(route.product_id)]
                probe_consumers.append(probe_consumer_name(route.id, route.product_id, version_id))
        fixture.write_manifest(path.with_suffix('.sla-consumers.json'), {
            'run_id': saved['run_id'], 'consumers': probe_consumers}, create=True)
        redis = m.apisix_policy_redis()
        try:
            quota = sorted({key for window in saved['quota_windows']
                for epoch in range(window['start'], window['end'] + 1)
                for key in fixture.quota_keys(window['scope'], window['period'], epoch)
                if ':minute:' not in key})
            report['quota_check'] = 'Buyer day/month counters; expiring minute keys checked by Lua isolation tests'
            warmup_baseline = {key: redis.get(key) for key in quota}
            report['probe_preflight'] = []
            for cid in contract_ids:
                with m.SessionLocal() as db:
                    target = m.sla_probe_runtime['resolve'](db, cid, instant=m.now())
                    if target is None:
                        continue
                    fixture.check(bool(target.url), 'Business-entry binding source is missing')
                    status = None
                    for attempt in range(3):
                        headers = m.sla_probe_headers(db, target)
                        response = m.validated_http_request(target.method, target.url, timeout=5,
                            follow_redirects=False, headers=headers)
                        status = response.status_code
                        response.close()
                        if status != 401 or attempt == 2:
                            break
                        # A newly created Consumer must reach data-plane watchers.
                        time.sleep(1)
                    report['probe_preflight'].append({'product_id': target.product_id, 'status_code': status})
                    fixture.check(200 <= status < 300, 'Signed business-entry preflight failed')
            fixture.check(all(redis.get(key) == value for key, value in warmup_baseline.items()),
                          'Signed probe preflight changed buyer quota')
            # A newly delivered service does not own the earlier boundary of
            # the current slot. Wait for a real slot, never insert future facts.
            first_slot = math.ceil(max(row.verified_at.timestamp() for row in contracts) / 300) * 300
            delay = max(0, first_slot + 1 - m.now().timestamp())
            fixture.check(delay <= 310, 'Real probe wait exceeds one bounded slot')
            if delay:
                print(json.dumps({'phase': 'waiting_for_real_probe_slot', 'seconds': round(delay)}), flush=True)
                time.sleep(delay)
            before = {key: redis.get(key) for key in quota}
            results = []
            for cid in contract_ids:
                with m.SessionLocal() as db:
                    results.append(m.sla_probe_runtime['collect_one'](db, cid, instant=m.now()))
                    db.commit()
            fixture.check(all(redis.get(key) == value for key, value in before.items()),
                          'Dedicated SLA probes changed buyer quota')
            report['probe_results'] = results
            with m.SessionLocal() as db:
                Slot = m.sla_probe_collector['Slot']
                healthy = list(db.scalars(select(Slot).where(Slot.product_id.in_(saved['products']),
                    Slot.status == 'healthy', Slot.scope == 'business_entry')))
                fixture.check({row.product_id for row in healthy} == set(saved['products']),
                              'Both real business-entry probes must be healthy')
                report['healthy_probe_rows'] = len(healthy)
        finally:
            redis.close()
        deadline = time.monotonic() + 30
        event_count = 0
        while time.monotonic() < deadline:
            with m.SessionLocal() as db:
                rows = list(db.scalars(select(m.sla_gateway_events['Model']).where(
                    m.sla_gateway_events['Model'].product_id.in_(saved['products']))))
                event_count = len(rows)
                business_count = sum(row.traffic == 'business' and row.scope_resolved
                    and row.enterprise_id == saved['enterprises'][0] for row in rows)
                if business_count >= 4:
                    break
            time.sleep(1)
        fixture.check(business_count >= 4, 'Real APISIX terminal business logs were not ingested')
        fixture.check(len({row.request_id for row in rows}) == event_count,
                      'Terminal request IDs were duplicated')
        report['real_gateway_events'] = event_count
        report['real_scoped_business_events'] = business_count
        report['verified_order_contracts'] = len(contract_ids)
        # No context manager: never run lifespan/seeding against the business DB.
        client = TestClient(m.app)
        try:
            denied = client.post('/internal/sla/gateway-events', json={'events': []})
            fixture.check(denied.status_code in (401, 403), 'Untrusted telemetry receiver is writable')
            response = client.post('/api/sla/evaluate', headers={'Authorization': 'Bearer ' + ops_token})
            fixture.check(response.status_code == 200, 'Replacement SLA evaluation endpoint failed')
            own = [row for row in response.json()['items'] if row.get('product_id') in saved['products']]
            fixture.check(bool(own), 'Replacement evaluator did not persist fixture order results')
            fixture.check(all(row.get('legacy') is False and row.get('metrics') for row in own),
                          'Replacement endpoint returned legacy statistics')
            fixture.check(all(row['status'] in {'insufficient_data', 'breached'} for row in own),
                          'Fresh sparse source samples falsely reported full compliance')
            with m.SessionLocal() as db:
                Term = m.subscription_terms['Term']
                future_orders = set(db.scalars(select(Term.order_id).where(
                    Term.order_id.in_([row['order_id'] for row in own]),
                    Term.starts_at > m.now())))
            report['assessment_request_states'] = [
                {'order_id': row['order_id'], 'future_term': row['order_id'] in future_orders,
                 'metric': row['metrics']['request_error_rate']} for row in own]
            fixture.check(all(sparse_request_result_valid(row['metrics']['request_error_rate'],
                future_term=row['order_id'] in future_orders) for row in own),
                'Sparse requests or future terms falsely reported compliance')
            report['order_assessments'] = len(own)
            overview = client.get('/api/sla/overview', headers={'Authorization': 'Bearer ' + ops_token})
            fixture.check(overview.status_code == 200, 'SLA overview failed after replacement')
            report['overview_verified'] = True
            buyer = client.post('/api/sla/evaluate', headers={'Authorization': 'Bearer ' + token})
            fixture.check(buyer.status_code == 403, 'Buyer can trigger platform-wide SLA evaluation')
        finally:
            client.close()
        report['status'] = 'passed'
    except BaseException:
        report['status'] = 'failed'
        Path(args.evidence).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        raise
    finally:
        # Validate the entire ownership graph before deleting even a probe/fact.
        # A real user may have purchased a publicly visible temporary product.
        with m.SessionLocal() as db:
            fixture.owned_scopes(m, db, saved)
        for name in probe_consumers:
            fixture.admin(m, 'DELETE', '/consumers/' + name, absent=True)
            fixture.check(fixture.admin(m, 'GET', '/consumers/' + name, absent=True) is None,
                          'Owned probe consumer remains')
        remove_owned_facts()
        fixture.cleanup(m, saved, path)
        # Drain the bounded asynchronous logger before checking fixture residues.
        time.sleep(8)
        remove_owned_facts()
        if browser.exists():
            browser.unlink()
        report['cleanup'] = 'exact fixture roots, facts and probe consumers removed'
        Path(args.evidence).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    Path(args.evidence).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--gateway-url', default='http://market-apisix:9080')
    run(parser.parse_args())


if __name__ == '__main__':
    main()
