"""Bounded runtime fault/restart acceptance for the deployed version-change worker.

Run each phase from the same configured application environment. `prepare`
prints a handoff marker after the durable target plan is committed; the external
Kubernetes orchestrator must restart the worker deployment at that point, then
run the matching check phase. Use separate manifests for the two scenarios:

  python scripts/version_change_fault_runtime_acceptance.py prepare --fault restart --manifest /tmp/vcf-restart.json
  # Restart the deployed version-change worker while this owned request is pending.
  python scripts/version_change_fault_runtime_acceptance.py restart-check --manifest /tmp/vcf-restart.json
  python scripts/version_change_fault_runtime_acceptance.py cleanup --manifest /tmp/vcf-restart.json

  python scripts/version_change_fault_runtime_acceptance.py prepare --fault route --manifest /tmp/vcf-route.json
  # Restart the deployed worker while pending, then let fault-check remove only
  # the owned target APISIX route and observe retries/compensation.
  python scripts/version_change_fault_runtime_acceptance.py fault-check --manifest /tmp/vcf-route.json
  python scripts/version_change_fault_runtime_acceptance.py cleanup --manifest /tmp/vcf-route.json

The deployed worker is configured with interval 2 and batch 10 in
k8s/version-change.yaml. Before each `prepare`, temporarily set its batch to 1
(leave interval 2) in the deployment and manifest, then apply/restart the worker.
Run `prepare` and wait for `WORKER_RESTART_HANDOFF`; the owned request will be
deferred under a row lock. Restore batch 10 in both the live deployment and
k8s/version-change.yaml before restarting the worker for the scenario and running
its check; the check releases only that request. Do not leave the fixture
override in the deployment after the acceptance run.

Run from the backend Python environment with the existing QA gateway and admin
environment configured. The helper writes a companion `.base` ownership file.
"""

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

import version_change_saga_runtime_acceptance as base


class AcceptanceFailure(base.AcceptanceFailure):
    pass


MARKER = 'WORKER_RESTART_HANDOFF'
RETRY_DELAY = 10
MAX_ATTEMPTS = 3


def require(value, message):
    if not value:
        raise AcceptanceFailure(message)


def save(path, manifest, *, create=False):
    path = Path(path)
    temporary = None
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    else:
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
                'Manifest must be a regular 0600 file')
        fd, temporary = tempfile.mkstemp(prefix='.vcf-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(manifest, stream, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if temporary:
            os.replace(temporary, path)
            temporary = None
    finally:
        if temporary:
            os.unlink(temporary)


def load(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
                'Manifest must be a regular 0600 file')
        value = json.load(stream)
    require(set(value) <= {'base', 'fault', 'phase', 'target_route_original',
                           'fault_route_key'} and
            {'base', 'fault', 'phase'} <= set(value), 'Unexpected manifest fields')
    checked = base.fixture_ids(value['base']['run_id'])
    require(re.fullmatch(r'qa-vcs-[0-9a-f]{12}', value['base']['run_id']) is not None,
            'Invalid fixture run identity')
    require(set(value['base']) == set(checked) and
            all(value['base'].get(k) == checked[k] for k in
                ('users', 'enterprises', 'products', 'versions', 'routes', 'request_id')),
            'Manifest ownership roots or fields changed')
    require(value['fault'] in {'restart', 'route'}, 'Invalid fault selector')
    if 'fault_route_key' in value:
        require(value['fault_route_key'] == checked['routes']['api']['target'] and
                isinstance(value.get('target_route_original'), dict),
                'Invalid route restoration journal')
    database_ids = value['base']['database_ids']
    fault_order = database_ids.get('fault_order')
    changes = database_ids.get('changes', {})
    orders = database_ids.get('orders', {})
    combinations = database_ids.get('combinations', {}).get('api', {})
    require(isinstance(fault_order, str) and orders.get('api') == fault_order and
            re.fullmatch(r'[0-9a-f]{32}', changes.get('api', '')) is not None and
            combinations == {'id': combinations.get('id'),
                'product_id': checked['products']['api'],
                'enterprise_id': checked['enterprises']['buyer']} and
            isinstance(combinations.get('id'), str),
            'Manifest request, order, or combination ownership mismatch')
    expected_routes = set(checked['routes']['api'].values())
    require(len(value['base']['consumers']) == 1 and all(
            item.get('name') == 'market-consumer-' + item.get('id', '') and
            item.get('route') == checked['routes']['api']['source'] and
            set(item.get('allowed_routes', [])) == expected_routes
            for item in value['base']['consumers']),
            'Manifest consumer ownership mismatch')
    return value


def db_request(m, change_id, order_id):
    with m.SessionLocal() as db:
        row = db.get(m.version_change_saga['VersionChangeRequest'], change_id)
        require(row is not None and row.source_order_id == order_id,
                'Owned version-change request disappeared or changed source order')
        return {key: getattr(row, key) for key in
                ('id', 'status', 'retries', 'resource_plan_json', 'next_retry_at',
                 'source_order_id', 'funding_order_id', 'resource_applied',
                 'compensation_retries', 'evidence_json')}


def wait_and_defer_plan(m, change_id, seconds=90, timeout=90):
    from sqlalchemy import select
    Request = m.version_change_saga['VersionChangeRequest']
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with m.SessionLocal() as db:
            row = db.scalar(select(Request).where(Request.id == change_id)
                            .with_for_update(skip_locked=True))
            if row is None:
                db.rollback()
                time.sleep(.05)
                continue
            if row.resource_plan_json not in ('', '{}'):
                require(row.status == 'resource_pending' and not row.resource_applied and
                        not row.lease_owner,
                        'Worker advanced past the planned, unapplied handoff')
                row.next_retry_at = m.now() + timedelta(seconds=seconds)
                db.commit()
                return
            db.rollback()
        time.sleep(.05)
    raise AcceptanceFailure('Timed out acquiring the exact request after plan commit')


def release_request(m, change_id, order_id, *, require_plan=True):
    from sqlalchemy import select
    with m.SessionLocal() as db:
        Request = m.version_change_saga['VersionChangeRequest']
        row = db.scalar(select(Request).where(Request.id == change_id).with_for_update())
        require(row is not None and row.source_order_id == order_id and
                (not require_plan or row.resource_plan_json not in ('', '{}')) and
                not row.resource_applied and not row.lease_owner and
                row.status in {'resource_pending', 'retrying'},
                'Exact owned request is no longer pending target application')
        row.next_retry_at = m.now()
        db.commit()


def wait_for(m, change_id, order_id, predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = db_request(m, change_id, order_id)
        if predicate(row):
            return row
        time.sleep(.25)
    raise AcceptanceFailure('Timed out waiting for the deployed worker state')


def journal_current_quota(m, manifest, path):
    ids = manifest['base']
    redis = m.apisix_policy_redis()
    try:
        with m.SessionLocal() as db:
            combo = db.get(m.subscription_terms['Combination'],
                           ids['database_ids']['combinations']['api']['id'])
            require(combo is not None and combo.product_id == ids['products']['api'] and
                    combo.enterprise_id == ids['enterprises']['buyer'],
                    'Quota combination ownership mismatch')
            name = next(item['name'] for item in ids['consumers']
                        if item['route'] == ids['routes']['api']['source'])
        policy = redis.hgetall('market:apisix:credential:' + name)
        now = int(redis.time()[0])
        active = [item for item in json.loads(policy['subscription_periods'])
                  if item['start_at'] <= now < item['end_at']]
        require(policy.get('quota_scope') == combo.id and len(active) == 1,
                'Active subscription quota period is ambiguous')
        return base.journal_counter(m, path, ids, redis, combo.id,
                                    active[0]['period_key'], combo.id, 'api')
    finally:
        redis.close()


def prepare(path, fault):
    m = base.load_main()
    ids = base.fixture_ids()
    manifest = {'base': ids, 'fault': fault, 'phase': 'preflight'}
    helper_path = str(path) + '.base'
    ids['environment'] = base.environment_fingerprint(m)
    base.preflight(m, ids)
    ids['authorized'] = True
    manifest['base'] = ids
    save(path, manifest, create=True)
    base.write_manifest(helper_path, ids, create=True)
    from fastapi.testclient import TestClient
    client = TestClient(m.app)
    try:
        tokens = base.prepare(m, ids)
        # Reuse the established real API, APISIX publication, delivery, and
        # funding flow. The wrapper stops before allowing target application.
        setup_fault_fixture(m, ids, helper_path, path, manifest, client, tokens)
    finally:
        client.close()


def setup_fault_fixture(m, ids, helper_path, path, manifest, client, tokens):
    """Create the real API subscription and paid upgrade, then pause its exact request."""
    from sqlalchemy import event
    from sqlalchemy import select
    from datetime import timedelta
    import httpx
    kind = 'api'
    for side in ('source', 'target'):
        route_key = ids['routes'][kind][side]
        base.save_external(helper_path, manifest['base'], kind, side, route_key=route_key)
        payload = {'upstream_url': 'http://market-mock-api:8300', 'route_key': route_key,
                   'version': side, 'upstream_auth_mode': 'oauth2', 'upstream_scope': 'resource.invoke',
                   'rate_limit_per_minute': 100, 'daily_quota': 10000,
                   'monthly_quota': 100000, 'health_path': '/health'}
        base.request(client, tokens['provider'], 'PUT',
            f"/products/{ids['products'][kind]}/versions/{ids['versions'][kind][side]}/integration",
            body=payload)
        with m.SessionLocal() as db:
            route = db.scalar(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.route_key == route_key))
            oauth = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == ids['products'][kind]))
            require(route is not None and oauth is not None, 'Fixture route or OAuth client missing')
            route.upstream_client_id, route.upstream_client_secret = oauth.client_id, oauth.client_secret
            db.commit()
        base.request(client, tokens['provider'], 'POST',
            f"/products/{ids['products'][kind]}/versions/{ids['versions'][kind][side]}/integration/verify")
    oid = base.request(client, tokens['buyer'], 'POST', '/orders', body={
        'product_id': ids['products'][kind], 'product_version_id': ids['versions'][kind]['source'],
        'buyer_enterprise_id': ids['enterprises']['buyer'], 'subscription_months': 3})['id']
    held_orders = {oid}
    def hold_owned_delivery(db, *_):
        for task in db.new.union(db.dirty):
            if (isinstance(task, m.DeliveryTask) and task.order_id in held_orders and
                    task.status in {'in_delivery', 'retrying'}):
                task.next_retry_at = m.now() + timedelta(hours=1)

    journal = base.journal_consumer(m, helper_path, manifest['base'])
    event.listen(m.SessionLocal.class_, 'after_flush', journal)
    event.listen(m.SessionLocal.class_, 'before_flush', hold_owned_delivery)
    try:
        base.request(client, tokens['finance'], 'POST', f'/orders/{oid}/transition',
                     body={'action': 'confirm_payment', 'reason': 'runtime fault acceptance'})
        with m.SessionLocal() as db:
            task = db.scalar(select(m.DeliveryTask).where(m.DeliveryTask.order_id == oid))
            require(task is not None, 'Fixture delivery task missing')
            task_id = task.id
        base.request(client, tokens['delivery'], 'POST', f'/delivery-tasks/{task_id}/process',
                     body={'success': True})
    finally:
        event.remove(m.SessionLocal.class_, 'after_flush', journal)
        event.remove(m.SessionLocal.class_, 'before_flush', hold_owned_delivery)
    credentials = base.request(client, tokens['buyer'], 'GET', f'/orders/{oid}/api-credentials')
    item = next((x for x in credentials.get('items', []) if x.get('secret_download_available')), None)
    require(item is not None, 'Fixture shared credential was not delivered')
    # Confirm the after-flush journal captured the credential before use.
    with m.SessionLocal() as db:
        credential = db.get(m.ApiCredential, item['id'])
        require(credential is not None, 'Persisted fixture credential missing')
        name = 'market-consumer-' + credential.id
        require(any(x['id'] == credential.id and x['name'] == name
                    for x in manifest['base']['consumers']),
                'Generated consumer was not journaled before use')
        manifest['base']['database_ids'].setdefault('orders', {})[kind] = oid
        db.commit()
    save(path, manifest)
    downloaded = client.get(f"/api/orders/{oid}/api-credentials/{item['id']}/download",
        headers={'Authorization': 'Bearer ' + tokens['buyer']})
    require(downloaded.status_code == 200, 'Credential download failed')
    raw_key = downloaded.json().get('api_key')
    gateway = os.getenv('QA_VCS_GATEWAY_URL', 'http://market-apisix:9080').rstrip('/')
    source_url = f"{gateway}/gateway/{ids['routes'][kind]['source']}/v1/echo"
    redis = m.apisix_policy_redis()
    try:
        with m.SessionLocal() as db:
            combo = db.scalar(select(m.subscription_terms['Combination']).where(
                m.subscription_terms['Combination'].enterprise_id == ids['enterprises']['buyer'],
                m.subscription_terms['Combination'].product_id == ids['products'][kind]))
            require(combo is not None, 'Fixture subscription combination missing')
            scope = combo.id
        consumer = redis.hgetall('market:apisix:credential:' + name)
        period = next((x['period_key'] for x in json.loads(consumer['subscription_periods'])
                       if x['start_at'] <= int(redis.time()[0]) < x['end_at']), None)
        require(period is not None and consumer.get('quota_scope') == scope,
                'Fixture subscription quota period missing')
        quota_keys = base.journal_counter(m, helper_path, manifest['base'], redis,
                                          scope, period, scope, kind, fresh=True)
    finally:
        redis.close()
    first = httpx.get(source_url, headers={'X-API-Key': raw_key}, timeout=15)
    require(first.status_code == 200, 'Source route HTTP proof failed')
    redis = m.apisix_policy_redis()
    try:
        require(redis.get(quota_keys[-1]) == '1', 'Initial source call did not record one quota unit')
    finally:
        redis.close()
    save(path, manifest)
    quote = base.request(client, tokens['buyer'], 'POST', f'/orders/{oid}/version-change-quote', body={
        'target_version_id': ids['versions'][kind]['target'], 'compatibility_confirmed': True})
    change = base.request(client, tokens['buyer'], 'POST', f'/orders/{oid}/version-changes', body={
        'quote_token': quote['quote_token'], 'idempotency_key': ids['run_id'] + '-fault'})
    # Keep the exact request ineligible until this process has observed and
    # deferred the durable plan; no worker code or SessionLocal is replaced.
    base.request(client, tokens['buyer'], 'POST', f"/version-changes/{change['id']}/start-payment", body={})
    # Hold only this request in the API process while confirmation commits;
    # the deployed worker is then released to prepare its durable resource plan.
    Request = m.version_change_saga['VersionChangeRequest']
    def hold_owned_change(db, *_):
        for candidate in db.dirty:
            if isinstance(candidate, Request) and candidate.id == change['id']:
                candidate.next_retry_at = m.now() + timedelta(seconds=90)

    event.listen(m.SessionLocal.class_, 'before_flush', hold_owned_change)
    try:
        base.request(client, tokens['buyer'], 'POST',
                     f"/version-changes/{change['id']}/confirm-payment", body={})
    finally:
        event.remove(m.SessionLocal.class_, 'before_flush', hold_owned_change)
    manifest['base']['database_ids'].setdefault('changes', {})[kind] = change['id']
    manifest['base']['database_ids']['fault_order'] = oid
    manifest['phase'] = 'plan-wait'
    base.write_manifest(helper_path, manifest['base'])
    save(path, manifest)
    release_request(m, change['id'], oid, require_plan=False)
    wait_and_defer_plan(m, change['id'])
    # Capture exact route definition before any destructive APISIX operation.
    target = base.apisix_admin(m, 'GET', '/routes/' + ids['routes'][kind]['target'])
    require(target and target.get('labels', {}).get('market_product_id') == ids['products'][kind],
            'Target route ownership labels mismatch')
    manifest['target_route_original'] = target
    manifest['fault_route_key'] = ids['routes'][kind]['target']
    manifest['phase'] = 'handoff'
    base.write_manifest(helper_path, manifest['base'])
    save(path, manifest)
    print(json.dumps({'status': 'ready', 'marker': MARKER, 'change_id': change['id'],
                      'fault': manifest['fault'], 'manifest': str(path)}, sort_keys=True), flush=True)


def fault_and_check(path):
    manifest = load(path)
    m = base.load_main()
    ids = manifest['base']
    change_id = ids['database_ids']['changes']['api']
    require(manifest['phase'] == 'handoff', 'Manifest is not at worker restart handoff')
    require(manifest['fault'] == 'route', 'This phase requires the route fault selector')
    target_key = manifest['fault_route_key']
    current = base.apisix_admin(m, 'GET', '/routes/' + target_key, absent=True)
    require(current is None or current.get('labels', {}).get('market_product_id') ==
            ids['products']['api'], 'Target route ownership labels mismatch')
    base.apisix_admin(m, 'DELETE', '/routes/' + target_key, absent=True)
    manifest['phase'] = 'fault-active'
    save(path, manifest)
    release_request(m, change_id, ids['database_ids']['fault_order'])
    row = wait_for(m, change_id, ids['database_ids']['fault_order'],
                   lambda x: x['status'] == 'compensated', 240)
    # Restore the exact saved route before verification or cleanup.
    restore_route(m, manifest)
    verify_compensation(m, manifest, row, path)
    manifest['phase'] = 'checked'
    save(path, manifest)
    return {'status': 'passed', 'change_id': change_id, 'tries': row['retries'],
            'compensation': row['status'], 'route_restored': True}


def restart_and_check(path):
    manifest = load(path)
    m = base.load_main()
    ids = manifest['base']
    require(manifest['phase'] == 'handoff' and manifest['fault'] == 'restart',
            'Manifest is not a worker restart handoff')
    change_id = ids['database_ids']['changes']['api']
    with m.SessionLocal() as db:
        change = db.get(m.version_change_saga['VersionChangeRequest'], change_id)
        require(change is not None and change.resource_plan_json not in ('', '{}'),
                'Durable resource plan is missing after worker restart')
        funding_id = change.funding_order_id
    release_request(m, change_id, ids['database_ids']['fault_order'])
    row = wait_for(m, change_id, ids['database_ids']['fault_order'],
                   lambda x: x['status'] == 'completed', 120)
    with m.SessionLocal() as db:
        from sqlalchemy import select
        change = db.get(m.version_change_saga['VersionChangeRequest'], change_id)
        funds = db.scalars(select(m.VersionChangeFund).where(
            m.VersionChangeFund.change_id == change_id, m.VersionChangeFund.kind == 'payment')).all()
        require(change.funding_order_id == funding_id and len(funds) == 1 and
                funds[0].payment_id is not None,
                'Restart changed funding identity or duplicated the payment reservation')
        order = db.get(m.Order, ids['database_ids']['fault_order'])
        combo = db.get(m.subscription_terms['Combination'], change.combination_id)
        require(order is not None and combo is not None and
                combo.version_id == ids['versions']['api']['target'],
                'Same request did not resume to the target subscription')
    gateway = os.getenv('QA_VCS_GATEWAY_URL', 'http://market-apisix:9080').rstrip('/')
    import httpx
    quota_keys = journal_current_quota(m, manifest, str(path) + '.base')
    save(path, manifest)
    source_consumer_name = next(x['name'] for x in ids['consumers']
                                if x['route'] == ids['routes']['api']['source'])
    consumer = base.apisix_admin(m, 'GET', '/consumers/' + source_consumer_name)
    key = consumer.get('plugins', {}).get('key-auth', {}).get('key')
    require(isinstance(key, str) and key.startswith('mk_'), 'Exact consumer key is unavailable for HTTP proof')
    target = httpx.get(f"{gateway}/gateway/{ids['routes']['api']['target']}/v1/echo",
                       headers={'X-API-Key': key}, timeout=15)
    source = httpx.get(f"{gateway}/gateway/{ids['routes']['api']['source']}/v1/echo",
                       headers={'X-API-Key': key}, timeout=15)
    require(target.status_code == 200 and source.status_code in {401, 403},
            'Post-restart version route authorization is incorrect')
    redis = m.apisix_policy_redis()
    try:
        month_key = quota_keys[-1]
        require(redis.get(month_key) == '2', 'Post-restart quota counter did not continue at two calls')
    finally:
        redis.close()
    manifest['phase'] = 'checked'
    save(path, manifest)
    return {'status': 'passed', 'change_id': change_id,
            'funding_order_id': funding_id, 'worker_status': row['status'],
            'target_status': target.status_code, 'source_status': source.status_code}


def restore_route(m, manifest):
    key = manifest['fault_route_key']
    original = manifest['target_route_original']
    route = base.apisix_admin(m, 'GET', '/routes/' + key, absent=True)
    if route is not None:
        require(route.get('labels', {}).get('market_product_id') == manifest['base']['products']['api'],
                'Refusing to replace unowned live target route')
    payload = {k: v for k, v in original.items() if k not in {'id', 'create_time', 'update_time'}}
    base.apisix_admin(m, 'PUT', '/routes/' + key, payload)
    restored = base.apisix_admin(m, 'GET', '/routes/' + key)
    require(restored.get('labels', {}).get('market_product_id') ==
            manifest['base']['products']['api'], 'Original target route restoration failed')


def verify_compensation(m, manifest, row, path):
    from sqlalchemy import select
    ids = manifest['base']
    oid = ids['database_ids']['fault_order']
    change_id = row['id']
    with m.SessionLocal() as db:
        change = db.get(m.version_change_saga['VersionChangeRequest'], change_id)
        require(change.retries == MAX_ATTEMPTS and change.compensation_started and
                change.resource_applied is False, 'Worker did not exhaust exactly three apply attempts')
        funding = db.get(m.Order, change.funding_order_id)
        require(funding is not None and funding.payment_status == 'refunded' and
                funding.refunded_amount == funding.paid_amount,
                'Upgrade funding was not fully refunded')
        funds = db.scalars(select(m.VersionChangeFund).where(
            m.VersionChangeFund.change_id == change_id)).all()
        payment_links = [item for item in funds if item.kind == 'payment']
        refund_links = [item for item in funds if item.kind == 'refund']
        require(len(payment_links) == 1 and bool(refund_links) and all(
                item.refund_id and
                (refund := db.get(m.Refund, item.refund_id)) is not None and
                refund.status == 'completed' and refund.payment_id == item.payment_id and
                refund.amount == item.amount for item in refund_links),
                'Funding refund is missing, incomplete, or linked to another payment')
        retry_rows = db.scalars(select(m.AuditLog).where(
            m.AuditLog.action == 'retry_version_change_phase',
            m.AuditLog.target_type == 'version_change',
            m.AuditLog.target_id == change_id).order_by(m.AuditLog.created_at)).all()
        require(len(retry_rows) == MAX_ATTEMPTS and all(
                (later.created_at - earlier.created_at).total_seconds() >= RETRY_DELAY - 1
                for earlier, later in zip(retry_rows, retry_rows[1:])),
                'Worker did not record three retry attempts at the ten-second delay')
        adjustment = m.version_change_ledger['Model']
        entries = db.scalars(select(adjustment).where(
            adjustment.change_id == change_id)).all()
        require(bool(entries) and all(x.status == 'void' for x in entries),
                'Version-change ledger entries were not voided')
        order = db.get(m.Order, oid)
        require(order is not None and order.id != 'ORD-261012005144E1B3' and
                order.order_no != 'ORD-261012005144E1B3', 'Protected order identity collision')
    gateway = os.getenv('QA_VCS_GATEWAY_URL', 'http://market-apisix:9080').rstrip('/')
    import httpx
    quota_keys = journal_current_quota(m, manifest, str(path) + '.base')
    save(path, manifest)
    source_key = next(x['name'] for x in ids['consumers'] if x['route'].endswith('-source'))
    consumer = base.apisix_admin(m, 'GET', '/consumers/' + source_key)
    require(consumer.get('username') == source_key and
            consumer.get('plugins', {}).get('market-gateway-quota', {}).get('route_key') ==
            ids['routes']['api']['source'], 'Exact source consumer route binding was not preserved')
    key = consumer.get('plugins', {}).get('key-auth', {}).get('key')
    require(isinstance(key, str) and key.startswith('mk_'),
            'Owned APISIX consumer does not expose its fixture key for HTTP proof')
    source = httpx.get(f"{gateway}/gateway/{ids['routes']['api']['source']}/v1/echo",
                       headers={'X-API-Key': key}, timeout=15)
    higher = httpx.get(f"{gateway}/gateway/{ids['routes']['api']['target']}/v1/echo",
                       headers={'X-API-Key': key}, timeout=15)
    require(source.status_code == 200, 'Original source route stopped accepting the shared key')
    redis = m.apisix_policy_redis()
    try:
        month_key = next(key for key in ids['external']['redis_keys']
                         if ':month:' in key and ids['database_ids']['combinations']['api']['id'] in key)
        require(redis.get(month_key) == '2', 'Source quota counter reset or failed to continue')
    finally:
        redis.close()
    require(higher.status_code in {401, 403}, 'Higher-version route remained accessible')


def cleanup(path):
    manifest = load(path)
    m = base.load_main()
    if manifest.get('target_route_original'):
        restore_route(m, manifest)
    base.cleanup(m, manifest['base'], str(path) + '.base')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'restart-check', 'fault-check', 'cleanup'))
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--fault', choices=('restart', 'route'), default='restart')
    args = parser.parse_args()
    if args.phase == 'prepare':
        prepare(args.manifest, args.fault)
        return
    if args.phase == 'fault-check':
        print(json.dumps(fault_and_check(args.manifest), sort_keys=True))
        return
    if args.phase == 'restart-check':
        print(json.dumps(restart_and_check(args.manifest), sort_keys=True))
        return
    cleanup(args.manifest)
    print(json.dumps({'status': 'passed', 'cleanup': 'exact owned fixture cleanup'}))


if __name__ == '__main__':
    try:
        main()
    except AcceptanceFailure as exc:
        print(json.dumps({'status': 'failed', 'check': str(exc)}, sort_keys=True))
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'status': 'failed'} | base.safe_exception_metadata(exc), sort_keys=True))
        raise SystemExit(1)
