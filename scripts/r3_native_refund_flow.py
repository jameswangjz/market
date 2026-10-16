"""Owned native API refund flow. No entry point or cleanup implementation."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import csv
import importlib
import io
import json
import os
import time
from unittest.mock import patch

import version_change_saga_runtime_acceptance as base
import r3_native_refund_runtime_acceptance as guards

check = base.check
FIXTURE_MODE = 'baseline'
FIXTURE_MODES = frozenset({'baseline', 'coverage_fault'})

# Compare the entire owned hash before changing its one fault field. Coverage
# must be persistent for the native resource adapter, so no TTL is rewritten.
COVERAGE_SWAP = """
local expected = cjson.decode(ARGV[1])
local size = 0
for field, value in pairs(expected) do
    size = size + 1
    if redis.call('HGET', KEYS[1], field) ~= value then
        return redis.error_reply('Owned coverage changed')
    end
end
if redis.call('HLEN', KEYS[1]) ~= size or redis.call('PTTL', KEYS[1]) ~= -1 then
    return redis.error_reply('Owned coverage shape or lifetime changed')
end
redis.call('HSET', KEYS[1], 'baseline', ARGV[2])
return 1
"""


def bootstrap(m, ids):
    """Prepare owned fixtures and disable their email; global monitors/SMTP stay unchanged."""
    from sqlalchemy import select
    check(ids['users'] == base.fixture_ids(ids['run_id'])['users'],
          'Email preferences require the four exact fixture users')
    with m.SessionLocal() as db:
        check(not db.scalar(select(m.Order.id).where(
            m.Order.product_id.in_(ids['products'].values()))),
            'Fixture already has orders')
        Preference = m.message_center['Preference']
        for uid in ids['users'].values():
            check(db.get(m.User, uid) is not None, 'Fixture email preference user missing')
            preference = db.get(Preference, uid)
            if preference is None:
                preference = Preference(user_id=uid)
                db.add(preference)
            preference.email_enabled = False
        actor = db.get(m.User, ids['users']['delivery'])
        check(actor is not None, 'Delivery actor missing')
        check(not db.scalar(select(m.Membership.user_id).where(
            m.Membership.user_id == actor.id)), 'Delivery actor has enterprise membership')
        actor.platform_role = 'super_admin'
        for kind in ('api', 'model'):
            product = db.get(m.Product, ids['products'][kind])
            check(product is not None and product.enterprise_id == ids['enterprises']['provider'],
                  'Product ownership mismatch')
            product.product_type = product.delivery_method = 'api'
            product.status = 'published' if kind == 'api' else 'draft'
            product.settlement_rule_mode = 'custom'
            product.settlement_rule_json = json.dumps(dict(zip(
                ('platform_rate', 'provider_rate', 'service_rate', 'expert_rate', 'channel_rate'),
                (20, 50, 10, 10, 10))))
            for side in ('source', 'target'):
                version = db.get(m.ProductReleaseVersion, ids['versions'][kind][side])
                check(version is not None and version.product_id == product.id,
                      'Version ownership mismatch')
                version.status = 'active' if kind == 'api' and side == 'source' else 'inactive'
                version.price, version.cost = 20, 5
                version.monthly_quota, version.daily_quota = 10, 0
                version.rate_limit_per_minute = 100
        db.flush()
        check(all(db.get(Preference, uid).email_enabled is False for uid in ids['users'].values()),
              'Fixture email preferences were not disabled')
        db.commit()


def scoped_sessions(m, refund_id):
    """All worker Execution SELECTs, including the claim, require this refund."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session, sessionmaker, with_loader_criteria
    Execution = m.refund_execution['Execution']

    class OwnedSession(Session):
        pass

    @event.listens_for(OwnedSession, 'do_orm_execute')
    def restrict(state):
        check(state.is_select, 'Worker attempted a bulk mutation')
        state.statement = state.statement.options(with_loader_criteria(
            Execution, Execution.refund_id == refund_id, include_aliases=True))

    @event.listens_for(OwnedSession, 'before_flush')
    def hold_owned_execution(db, *_):
        for row in db.new.union(db.dirty):
            if isinstance(row, Execution) and row.refund_id == refund_id:
                row.lease_expires_at = m.now() + timedelta(hours=1)

    return sessionmaker(bind=m.engine, class_=OwnedSession, expire_on_commit=False)


@contextmanager
def owned_hooks(m, ids, path, manifest):
    from sqlalchemy import event
    state = manifest['database_ids']['native_refund']
    Execution = m.refund_execution['Execution']
    journal = base.journal_consumer(m, path, manifest)

    def hold_delivery(db, *_):
        for task in db.new.union(db.dirty):
            if (isinstance(task, m.DeliveryTask) and task.order_id in state['order_ids']
                    and task.status in {'in_delivery', 'retrying'}):
                task.next_retry_at = m.now() + timedelta(hours=1)
            if isinstance(task, Execution) and task.refund_id in state['refund_ids']:
                task.lease_expires_at = m.now() + timedelta(hours=1)

    def journal_roots(db, *_):
        for row in db.new:
            if isinstance(row, m.Order) and row.product_id == ids['products']['api']:
                check(row.buyer_enterprise_id == ids['enterprises']['buyer'], 'Foreign order')
                if row.id not in state['order_ids']:
                    state['order_ids'].append(row.id)
                    manifest['database_ids'].setdefault('orders', {})['api'] = row.id
            if isinstance(row, m.Refund) and row.order_id in state['order_ids']:
                if row.id not in state['refund_ids']:
                    state['refund_ids'].append(row.id)
                    state['refund_id'] = row.id
                    state['extra_keys'].append(guards.INTENT + row.id)
            if isinstance(row, m.subscription_terms['Term']) and row.order_id in state['order_ids']:
                order = db.get(m.Order, row.order_id)
                combo = db.get(m.subscription_terms['Combination'], row.combination_id)
                check(order.product_id == ids['products']['api'] and combo is not None
                      and combo.product_id == order.product_id
                      and combo.enterprise_id == ids['enterprises']['buyer']
                      and row.generation == 1 and row.start_month == 0 and row.months == 1,
                      'New term is not the exact owned baseline')
                state.update(term_id=row.id, combination_id=row.combination_id,
                             order_id=row.order_id, generation=1, period_index=0)
                coverage = guards.COVERAGE + guards.quota_token(row.combination_id) + ':' + guards.quota_token(row.combination_id + ':1:0')
                if coverage not in state['extra_keys']:
                    state['extra_keys'].append(coverage)
                manifest['database_ids'].setdefault('combinations', {})['api'] = dict(
                    id=combo.id, product_id=combo.product_id, enterprise_id=combo.enterprise_id)
        base.write_manifest(path, manifest)

    def notify(db, title, content, target_type, target_id, **kwargs):
        allowed = False
        if target_type == 'order':
            row = db.get(m.Order, target_id)
            allowed = (row is not None and row.id in state['order_ids'])
        elif target_type == 'delivery_task':
            row = db.get(m.DeliveryTask, target_id)
            allowed = row is not None and row.order_id in state['order_ids']
        elif target_type == 'refund':
            row = db.get(m.Refund, target_id)
            allowed = row is not None and row.order_id in state['order_ids']
        elif target_type == 'product' and target_id in ids['products'].values():
            row = db.get(m.Product, target_id)
            allowed = row is not None and row.enterprise_id == ids['enterprises']['provider']
        check(allowed, 'Notification suppression rejected an unrelated root')

    event.listen(m.SessionLocal.class_, 'after_flush', journal)
    event.listen(m.SessionLocal.class_, 'after_flush', journal_roots)
    event.listen(m.SessionLocal.class_, 'before_flush', hold_delivery)
    try:
        with patch.object(m, 'notify_business_event', notify):
            yield
    finally:
        event.remove(m.SessionLocal.class_, 'before_flush', hold_delivery)
        event.remove(m.SessionLocal.class_, 'after_flush', journal_roots)
        event.remove(m.SessionLocal.class_, 'after_flush', journal)


def publish(m, ids, path, manifest, client, tokens):
    from sqlalchemy import select
    product, version, route_key = (ids['products']['api'], ids['versions']['api']['source'],
                                   ids['routes']['api']['source'])
    base.save_external(path, manifest, 'api', 'source', route_key=route_key)
    base.request(client, tokens['provider'], 'PUT',
        f'/products/{product}/versions/{version}/integration', body=dict(
            upstream_url='http://market-mock-api:8300', route_key=route_key, version='source',
            upstream_auth_mode='oauth2', upstream_scope='resource.invoke',
            rate_limit_per_minute=100, daily_quota=0, monthly_quota=10, health_path='/health'))
    with m.SessionLocal() as db:
        route = db.scalar(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.route_key == route_key))
        oauth = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == product))
        check(route is not None and oauth is not None, 'Route or OAuth missing')
        route.upstream_client_id, route.upstream_client_secret = oauth.client_id, oauth.client_secret
        route_id = route.id
        manifest['database_ids']['native_refund']['route_id'] = route_id
        manifest['database_ids'].setdefault('source_routes', {})['api'] = route_id
        base.write_manifest(path, manifest)
        db.commit()
    result = base.request(client, tokens['provider'], 'POST',
                         f'/products/{product}/versions/{version}/integration/verify')
    check(result.get('status') == 'verified', 'Real integration verification failed')
    remote = base.apisix_admin(m, 'GET', '/routes/' + route_key)
    check(remote.get('labels', {}).get('market_product_id') == product, 'Remote route mismatch')
    return route_id


def verify_delivery_audit(m, order_id, task_id):
    """New-run DB evidence only; never a historical cleanup requirement."""
    from sqlalchemy import select
    with m.SessionLocal() as db:
        rows = db.scalars(select(m.AuditLog).where(
            m.AuditLog.action == 'create_delivery_task', m.AuditLog.order_id == order_id)).all()
        check(len(rows) == 1 and rows[0].target_type == 'delivery_task'
              and rows[0].target_id == task_id and bool(task_id),
              'Delivery creation audit does not uniquely identify the actual task')
    return 1


def purchase(m, ids, path, manifest, client, tokens, route_id):
    from sqlalchemy import select
    oid = base.request(client, tokens['buyer'], 'POST', '/orders', body=dict(
        product_id=ids['products']['api'], product_version_id=ids['versions']['api']['source'],
        buyer_enterprise_id=ids['enterprises']['buyer'], subscription_months=1))['id']
    check(oid in manifest['database_ids']['native_refund']['order_ids'], 'Order was not journaled')
    with m.SessionLocal() as db:
        order = db.get(m.Order, oid)
        economic = m.order_economics['economic_snapshot'](order)
        delivery = m.order_economics['delivery_snapshot'](order)
        rates = economic['settlement_rule']['rates']
        check(order.product_id == ids['products']['api'] and order.subscription_months == 1
              and order.delivery_method_snapshot == 'api'
              and float(economic['unit_price']) == 20 and float(economic['unit_cost']) == 5
              and all(float(rates[key]) == value for key, value in zip(
                  ('platform_rate', 'provider_rate', 'service_rate', 'expert_rate', 'channel_rate'),
                  (20, 50, 10, 10, 10)))
              and all(delivery['quota'][key] == value for key, value in (
                  ('monthly_quota', 10), ('daily_quota', 0), ('rate_limit_per_minute', 100))),
              'Order did not freeze the exact native source economics and quota')
    base.request(client, tokens['finance'], 'POST', f'/orders/{oid}/transition',
                 body=dict(action='confirm_payment', reason='Owned native refund simulated payment'))
    with m.SessionLocal() as db:
        task = db.scalar(select(m.DeliveryTask).where(m.DeliveryTask.order_id == oid))
        check(task is not None, 'Delivery task missing')
        task_id = task.id
    manifest['database_ids']['native_refund']['delivery_audit_identity_verified'] = verify_delivery_audit(
        m, oid, task_id)
    base.write_manifest(path, manifest)
    base.request(client, tokens['delivery'], 'POST', f'/delivery-tasks/{task_id}/process',
                 body={'success': True})
    with m.SessionLocal() as db:
        rows = db.scalars(select(m.ApiCredential).where(
            m.ApiCredential.enterprise_id == ids['enterprises']['buyer'],
            m.ApiCredential.route_id == route_id, m.ApiCredential.status == 'active')).all()
        check(len(rows) == 1 and rows[0].order_id == '', 'Expected exactly one shared credential')
        credential_id = rows[0].id
        Term = m.subscription_terms['Term']
        terms = db.scalars(select(Term).where(Term.order_id == oid)).all()
        check(len(terms) == 1 and terms[0].generation == 1, 'Expected original one-month term')
        term_id, combo_id = terms[0].id, terms[0].combination_id
    state = manifest['database_ids']['native_refund']
    state.update(order_id=oid, term_id=term_id, combination_id=combo_id,
                 credential_id=credential_id)
    manifest['database_ids'].setdefault('credentials', {})['api'] = credential_id
    base.write_manifest(path, manifest)
    listed = base.request(client, tokens['buyer'], 'GET', f'/orders/{oid}/api-credentials')
    check(any(item.get('id') == credential_id and item.get('secret_download_available')
              for item in listed.get('items', [])), 'Shared credential unavailable')
    response = client.get(f'/api/orders/{oid}/api-credentials/{credential_id}/download',
                          headers={'Authorization': 'Bearer ' + tokens['buyer']})
    check(response.status_code == 200, 'Credential download failed')
    key = response.json().get('api_key')
    check(isinstance(key, str) and key.startswith('mk_'), 'Credential missing')
    return key


def disable_fixture_sla_logging(m, ids, path, manifest):
    """Remove only the route-local logger; global SLA facts remain immutable."""
    route_key = ids['routes']['api']['source']
    state = manifest['database_ids']['native_refund']
    check(state['route_key'] == route_key and route_key in manifest['external']['route_keys'],
          'Local SLA logger edit requires the exact newly journaled route')
    endpoint = '/routes/' + route_key
    original = base.apisix_admin(m, 'GET', endpoint)
    check(original.get('labels', {}).get('market_product_id') == ids['products']['api'],
          'Local SLA logger edit rejected foreign route')
    native = ('key-auth', 'market-gateway-quota', 'market-gateway-oauth')
    plugins = original.get('plugins', {})
    check(all(name in plugins for name in native), 'Native route plugins missing')
    document = deepcopy(original)
    for name in ('create_time', 'update_time'):
        document.pop(name, None)
    document['plugins'].pop('market-sla-logger', None)
    check(all(document['plugins'][name] == plugins[name] for name in native),
          'Local SLA logger edit changed a native plugin')
    state.update(route_local_sla_logger_removal_requested=True,
                 global_sla_logging_unmodified=True, sla_coverage_claim=False)
    base.write_manifest(path, manifest)
    base.apisix_admin(m, 'PUT', endpoint, document)
    actual = base.apisix_admin(m, 'GET', endpoint)
    for name in ('create_time', 'update_time'):
        actual.pop(name, None)
    check('market-sla-logger' not in actual.get('plugins', {})
          and all(actual.get('plugins', {}).get(name) == plugins[name] for name in native)
          and actual == document, 'Owned route changed while removing local SLA logger')
    state['route_local_sla_logger_removed'] = True
    base.write_manifest(path, manifest)


def charge(m, ids, path, manifest, redis, invoke):
    from sqlalchemy import select
    state = manifest['database_ids']['native_refund']
    combo, term, oid = (state[k] for k in ('combination_id', 'term_id', 'order_id'))
    consumer = next(item['name'] for item in manifest['consumers']
                    if item['id'] == state['credential_id'])
    state['consumer'] = consumer
    metadata_key = 'market:apisix:credential:' + consumer
    metadata = redis.hgetall(metadata_key)
    periods = json.loads(metadata['subscription_periods'])
    check(metadata.get('quota_scope') == combo and len(periods) == 1
          and periods[0]['period_key'] == combo + ':1:0'
          and periods[0]['monthly_quota'] == 10, 'Unexpected original quota metadata')
    period = periods[0]['period_key']
    keys = base.journal_counter(m, path, manifest, redis, combo, period, combo, 'api', fresh=True)
    coverage = guards.COVERAGE + guards.quota_token(combo) + ':' + guards.quota_token(period)
    check(coverage in state['extra_keys'], 'Coverage was not journaled before delivery')
    state['physical_run_id'] = redis.info('server')['run_id']
    with m.SessionLocal() as db:
        stream, cursor = guards.source_context(m, db, redis)
    state['source_run_id'] = cursor.redis_run_id if cursor is not None else state['physical_run_id']
    state['source_stream'] = stream
    stream = guards.source_stream(state)
    latest = redis.xrevrange(stream, count=1)
    state['stream_start'] = latest[0][0] if latest else '0-0'
    base.write_manifest(path, manifest)
    try:
        for _ in range(2):
            response = invoke()
            check(response.status_code == 200 and response.json().get('code') == 0
                  and response.json().get('data', {}).get('provider') == 'mock-api',
                  'Real gateway charge failed')
    finally:
        latest = redis.xrevrange(guards.source_stream(state), count=1)
        state['stream_end'] = latest[0][0] if latest else state['stream_start']
        bounded_events = redis.xrange(guards.source_stream(state), min='(' + state['stream_start'],
                                     max=state['stream_end'], count=200)
        state['event_ids'] = [event_id for event_id, values in bounded_events
            if values.get('order_id') == oid]
        base.write_manifest(path, manifest)
        check(len(bounded_events) < 200 or bounded_events[-1][0] == state['stream_end'],
              'Charge stream window exceeds the bounded read; preserve journal for review')
    events = []
    for event_id, values in bounded_events:
        if values.get('order_id') == oid:
            guards.validate_charge(event_id, values, start=state['stream_start'],
                end=state['stream_end'], order_id=oid, term_id=term, combination_id=combo,
                route_key=ids['routes']['api']['source'], source_run_id=state['source_run_id'])
            events.append(event_id)
    state['event_ids'] = events
    base.write_manifest(path, manifest)
    check(len(events) == 2 and redis.get(keys[-1]) == '2', 'Expected two actual charges')
    Charge = m.api_charge_ledger['Charge']
    charge_ids = {guards.charge_id(state, event_id): event_id for event_id in events}
    deadline = time.monotonic() + 90
    while True:
        with m.SessionLocal() as db:
            rows = db.scalars(select(Charge).where(Charge.id.in_(charge_ids))).all()
            if len(rows) == 2:
                raw = dict(bounded_events)
                for row in rows:
                    event_id = charge_ids[row.id]
                    guards.validate_charge_row(row, state, event_id, raw[event_id])
                check(all(row.order_id == oid and row.segment_id == term and row.scope == combo
                          and row.quantity == 1 and row.period_key == period
                          and row.route_key == ids['routes']['api']['source']
                          and row.source_run_id == state['source_run_id']
                          and row.policy == 'gateway_actual_charge_v1' for row in rows),
                      'Ledger identity mismatch')
                break
        check(time.monotonic() < deadline, 'Deployed ledger worker did not ingest owned events')
        time.sleep(1)
    return metadata_key, keys[-1]


@contextmanager
def coverage_mismatch(m, ids, path, manifest, redis):
    """Inject and restore one exact owned persistent coverage hash."""
    from sqlalchemy import select
    state = manifest['database_ids']['native_refund']
    combo, rid = state['combination_id'], state['refund_id']
    allowed = guards.extra_keys(combination_id=combo, generation=state['generation'],
                               period_index=state['period_index'], refund_id=rid)
    coverage = next(key for key in allowed if key.startswith(guards.COVERAGE))
    check(coverage in state['extra_keys'], 'Coverage fault key was not journaled')
    with m.SessionLocal() as db:
        order = db.get(m.Order, state['order_id'])
        term = db.get(m.subscription_terms['Term'], state['term_id'])
        combination = db.get(m.subscription_terms['Combination'], combo)
        execution = db.scalar(select(m.refund_execution['Execution']).where(
            m.refund_execution['Execution'].refund_id == rid))
        check(order is not None and order.product_id == ids['products']['api']
              and order.buyer_enterprise_id == ids['enterprises']['buyer']
              and term is not None and term.order_id == order.id and term.combination_id == combo
              and term.generation == 1 and term.start_month == 0 and term.months == 1
              and combination is not None and combination.product_id == order.product_id
              and combination.enterprise_id == order.buyer_enterprise_id
              and db.get(m.Refund, rid).order_id == order.id
              and execution is not None and execution.status == 'resources_pending'
              and not execution.proof_json, 'Coverage fault ownership or phase mismatch')
    original = redis.hgetall(coverage)
    check(original.get('baseline') == '0' and redis.pttl(coverage) == -1,
          'Coverage fault requires a known persistent zero baseline')
    corrupted = dict(original, baseline='1')
    fault = dict(key=coverage, original=original, original_pttl=-1,
                 injected_baseline='1', status='injection_requested', mutated_field='baseline',
                 scope='exact_owned_coverage_hash_only', shared_state_modified=False)
    state['coverage_fault'] = fault
    base.write_manifest(path, manifest)
    # Restore even if the injection succeeded but its acknowledgement or the
    # following manifest write was interrupted.
    try:
        check(redis.eval(COVERAGE_SWAP, 1, coverage, json.dumps(original), '1') == 1,
              'Coverage injection failed')
        fault['status'] = 'injected'
        base.write_manifest(path, manifest)
        yield
    finally:
        current = redis.hgetall(coverage)
        if current == corrupted:
            check(redis.eval(COVERAGE_SWAP, 1, coverage, json.dumps(corrupted), '0') == 1,
                  'Coverage restoration failed')
        else:
            check(current == original, 'Coverage changed unexpectedly; preserve backup for review')
        check(redis.hgetall(coverage) == original and redis.pttl(coverage) == -1,
              'Coverage restoration readback failed')
        fault['status'] = 'restored'
        base.write_manifest(path, manifest)


def verify_resource_failure(m, manifest, redis):
    from sqlalchemy import select
    state = manifest['database_ids']['native_refund']
    rid, oid = state['refund_id'], state['order_id']
    hooks = m.refund_execution
    with m.SessionLocal() as db:
        execution = db.scalar(select(hooks['Execution']).where(hooks['Execution'].refund_id == rid))
        legs = db.scalars(select(hooks['PaymentLeg']).where(hooks['PaymentLeg'].refund_id == rid)).all()
        Recovery = m.refund_execution_resources['Recovery']
        check(execution is not None and execution.status == 'failed'
              and execution.resume_status == 'resources_pending'
              and execution.error_code in {'execution_revalidation_failed', 'execution_phase_failed'}
              and not execution.proof_json and db.get(m.Refund, rid).status == 'failed'
              and legs and all(leg.status == 'prepared' and not leg.receipt
                               and leg.completed_at is None for leg in legs)
              and db.get(hooks['Completion'], rid) is None
              and db.scalar(select(Recovery.id).where(Recovery.refund_id == rid)) is None
              and float(db.get(m.Order, oid).refunded_amount) == 0,
              'Coverage fault did not fail before resource and fund completion')
        version = db.get(m.refund_workflow['Case'], rid).version
    consumer = state['consumer']
    metadata = redis.hgetall('market:apisix:credential:' + consumer)
    periods = json.loads(metadata['subscription_periods'])
    month = 'market:apisix:quota:subscription:month:' + guards.quota_token(state['combination_id'])
    month += ':' + guards.quota_token(state['combination_id'] + ':1:0')
    check(metadata.get('quota_scope') == state['combination_id'] and len(periods) == 1
          and periods[0]['monthly_quota'] == 10 and redis.get(month) == '2'
          and not redis.exists(guards.INTENT + rid), 'Coverage failure changed native resources')
    return version


def fault_and_retry(m, ids, path, manifest, client, tokens, redis, worker, sessions):
    state = manifest['database_ids']['native_refund']
    rid = state['refund_id']
    check(worker.process_once(SessionLocal=sessions, ns=vars(m),
          now=lambda: m.now() + timedelta(hours=2)), 'Owned prepared phase missing')
    with coverage_mismatch(m, ids, path, manifest, redis):
        check(worker.process_once(SessionLocal=sessions, ns=vars(m),
              now=lambda: m.now() + timedelta(hours=2)), 'Owned resource phase missing')
        version = verify_resource_failure(m, manifest, redis)
        state['coverage_fault']['failure_before_funds'] = True
        base.write_manifest(path, manifest)
    body = dict(expected_version=version, reason='Owned coverage restored retry',
                idempotency_key=ids['run_id'] + '-native-coverage-retry')
    state['coverage_fault']['retry_body'] = body
    base.write_manifest(path, manifest)
    for _ in range(2):
        row = base.request(client, tokens['delivery'], 'POST', f'/refunds/{rid}/retry', body=body)
        check(row['id'] == rid, 'Retry changed refund identity')
    return body


def refund(m, ids, path, manifest, client, tokens, *, redis=None):
    from sqlalchemy import select
    state = manifest['database_ids']['native_refund']
    oid = state['order_id']
    scope = '?enterprise_id=' + ids['enterprises']['buyer'] + '&business_view=buyer'
    quote = base.request(client, tokens['buyer'], 'POST', f'/orders/{oid}/refund-quote' + scope,
                         body={'amount': '16.00'})
    check(quote.get('status') == 'available', 'Native automatic refund quote unavailable')
    row = base.request(client, tokens['buyer'], 'POST', f'/orders/{oid}/refund-requests' + scope,
        body=dict(amount='16.00', reason='Owned native refund',
                  idempotency_key=ids['run_id'] + '-native-request'))
    rid = row['id']
    check(rid in state['refund_ids'], 'Refund was not journaled')
    state['extra_keys'] = sorted(set(state['extra_keys']) | guards.extra_keys(
        combination_id=state['combination_id'], generation=1, period_index=0, refund_id=rid))
    base.write_manifest(path, manifest)
    row = base.request(client, tokens['delivery'], 'POST', f'/refunds/{rid}/review', body=dict(
        decision='approve', reason='Owned native refund approval', expected_version=row['version'],
        idempotency_key=ids['run_id'] + '-native-approve'))
    base.request(client, tokens['delivery'], 'POST', f'/refunds/{rid}/execute', body=dict(
        expected_version=row['version'], reason='Owned simulated funds execution',
        idempotency_key=ids['run_id'] + '-native-execute'))
    worker = importlib.import_module(m.__package__ + '.refund_execution_worker')
    sessions = scoped_sessions(m, rid)
    Execution = m.refund_execution['Execution']
    retry_body = None
    if state.get('fixture_mode', 'baseline') == 'coverage_fault':
        check(redis is not None, 'Coverage fault requires the real owned Redis client')
        retry_body = fault_and_retry(m, ids, path, manifest, client, tokens, redis, worker, sessions)
    replayed = False
    for _ in range(16):
        with m.SessionLocal() as db:
            execution = db.scalar(select(Execution).where(Execution.refund_id == rid))
            check(execution is not None and execution.status != 'failed', 'Owned refund execution failed')
            if execution.status == 'completed':
                legs = db.scalars(select(m.refund_execution['PaymentLeg']).where(
                    m.refund_execution['PaymentLeg'].refund_id == rid)).all()
                check(legs and all(leg.status == 'completed' and leg.receipt ==
                      'simulated:' + rid + ':' + leg.payment_id for leg in legs), 'Simulated legs missing')
                check(db.get(m.Refund, rid).status == 'completed'
                      and float(db.get(m.Order, oid).refunded_amount) == 16,
                      'Refund financial completion missing')
                completion = db.get(m.refund_execution['Completion'], rid)
                Recovery = m.refund_execution_resources['Recovery']
                recoveries = db.scalars(select(Recovery).where(Recovery.refund_id == rid)).all()
                check(len(recoveries) == 1, 'Expected exactly one native resource reduction')
                recovery = recoveries[0]
                check(completion is not None and completion.execution_id == execution.id
                      and float(completion.amount) == 16 and recovery is not None
                      and recovery.execution_id == execution.id and recovery.order_id == oid
                      and recovery.term_id == state['term_id']
                      and recovery.combination_id == state['combination_id']
                      and recovery.stage == 'resources_applied',
                      'Native resource recovery or local completion receipt missing')
                if retry_body is None or replayed:
                    if retry_body is not None:
                        state['coverage_fault']['retry_completed_idempotently'] = True
                        base.write_manifest(path, manifest)
                    return len(legs)
        if retry_body is not None and execution.status == 'completed':
            row = base.request(client, tokens['delivery'], 'POST', f'/refunds/{rid}/retry', body=retry_body)
            check(row['id'] == rid, 'Completed retry replay changed refund identity')
            check(not worker.process_once(SessionLocal=sessions, ns=vars(m),
                  now=lambda: m.now() + timedelta(hours=2)), 'Completed refund remained runnable')
            replayed = True
            continue
        check(worker.process_once(SessionLocal=sessions, ns=vars(m),
              now=lambda: m.now() + timedelta(hours=2)), 'Owned worker found no execution')
    raise base.AcceptanceFailure('Owned refund exceeded bounded worker phases')


NEGATIVE_AMOUNTS = dict(gross_amount='-16', cost_amount='-4', profit_amount='-12',
    platform_fee='-2.4', provider_share='-6', service_share='-1.2', expert_fee='-1.2', channel_fee='-1.2')
PARTICIPANT_FIELDS = dict(platform='platform_fee', provider='provider_share',
                         service='service_share', expert='expert_fee', channel='channel_fee')


def check_negative_amounts(values):
    check(all(Decimal(str(values.get(key))) == Decimal(amount)
              for key, amount in NEGATIVE_AMOUNTS.items()), 'Owned negative settlement amounts mismatch')


def negative_settlement(m, path, manifest, client, tokens):
    """QA006 exact refund adjustment and read-only reports; no monthly batch."""
    from sqlalchemy import select
    state = manifest['database_ids']['native_refund']
    rid, oid = state['refund_id'], state['order_id']
    with m.SessionLocal() as db:
        refund = db.get(m.Refund, rid)
        check(refund is not None and refund.order_id == oid and refund.status == 'completed',
              'Negative settlement requires the exact completed owned refund')
        existing = db.scalars(select(m.Settlement.id).where(m.Settlement.refund_id == rid)).all()
        check(len(existing) <= 1, 'Owned negative settlement already duplicated')
        # Invoke the production adapter for just this refund, never the global
        # monthly generator. The adapter validates durable execution evidence.
        settlement = m.refund_settlement['negative_settlement'](db, refund, 'system')
        settlement_id = settlement.id
        state['negative_settlement_id'] = settlement_id
        state['negative_settlement_adapter_mode'] = (
            'existing/idempotent_adapter' if existing else 'created_by_production_adapter')
        state['negative_settlement_source'] = (
            'preexisting_producer_unasserted' if existing else 'flow_invoked_production_adapter')
        state['negative_settlement_adapter_actor'] = 'system'
        base.write_manifest(path, manifest)
        db.commit()
    with m.SessionLocal() as db:
        rows = db.scalars(select(m.Settlement).where(m.Settlement.refund_id == rid)).all()
        link = db.get(m.refund_settlement['Model'], rid)
        check(len(rows) == 1 and rows[0].id == settlement_id and rows[0].order_id == oid
              and rows[0].is_refund and link is not None and link.settlement_id == settlement_id,
              'Owned negative settlement or immutable link not unique')
        check_negative_amounts({key: getattr(rows[0], key) for key in NEGATIVE_AMOUNTS})
        order_no, refund_no = db.get(m.Order, oid).order_no, db.get(m.Refund, rid).refund_no
    report = base.request(client, tokens['delivery'], 'GET', '/settlement-reports')
    owned = [item for item in report['items'] if item.get('refund_id') == rid]
    check(len(owned) == 1, 'Owned refund missing or duplicated in settlement report')
    item = owned[0]
    check(item.get('id') == settlement_id and item.get('order_id') == oid
          and item.get('order_no') == order_no and item.get('refund_no') == refund_no
          and item.get('source_kind') == 'ordinary_refund' and item.get('is_refund'),
          'Report lost original order or refund identity')
    check_negative_amounts(item)
    participants = item.get('participants', [])
    check(len(participants) == 5 and {p['participant_type'] for p in participants} == set(PARTICIPANT_FIELDS)
          and all(Decimal(str(p['amount'])) == Decimal(NEGATIVE_AMOUNTS[PARTICIPANT_FIELDS[p['participant_type']]])
                  for p in participants), 'Owned report participant amounts mismatch')
    response = client.get('/api/settlement-reports/export?kind=details',
                          headers={'Authorization': 'Bearer ' + tokens['delivery']})
    check(response.status_code == 200, 'Settlement detail export failed')
    exported = [row for row in csv.DictReader(io.StringIO(response.text.lstrip('\ufeff')))
                if row.get('refund_id') == rid]
    check(len(exported) == 1, 'Owned refund missing or duplicated in detail export')
    row = exported[0]
    check(row.get('order_id') == oid and row.get('order_no') == order_no
          and row.get('refund_no') == refund_no and row.get('source_kind') == 'ordinary_refund',
          'Export lost original order or refund identity')
    fields = dict(gross_amount='order_amount', cost_amount='cost_amount', profit_amount='profit_amount',
        platform_fee='\u5e73\u53f0\u8fd0\u8425\u65b9', provider_share='\u6570\u636e/\u670d\u52a1\u63d0\u4f9b\u65b9',
        service_share='\u6570\u636e\u670d\u52a1\u65b9', expert_fee='\u4e13\u5bb6', channel_fee='\u6e20\u9053')
    check_negative_amounts({key: row.get(column) for key, column in fields.items()})
    proof = {key: float(value) for key, value in NEGATIVE_AMOUNTS.items()}
    state['negative_settlement_proof'] = proof
    state['cross_month_coverage_claim'] = False
    state['report_csv_verified'] = 1
    base.write_manifest(path, manifest)
    return proof


def exercise(m, ids, path, manifest, client, tokens, *, fixture_mode=None):
    import httpx
    existing = manifest['database_ids'].get('native_refund', {})
    check(not (set(existing) - {'fixture_mode'}), 'Native refund flow cannot be replayed')
    fixture_mode = fixture_mode if fixture_mode is not None else existing.get(
        'fixture_mode', os.environ.get('QA_NATIVE_REFUND_MODE', FIXTURE_MODE))
    check(fixture_mode in FIXTURE_MODES, 'Unknown native refund fixture mode')
    manifest['database_ids']['native_refund'] = dict(order_ids=[], refund_ids=[], extra_keys=[], event_ids=[],
        route_key=ids['routes']['api']['source'], product_id=ids['products']['api'],
        version_id=ids['versions']['api']['source'], local_business_notifications_suppressed=True,
        global_monitors_unmodified=True, notification_claim=False, fixture_mode=fixture_mode, sla_coverage_claim=False,
        sla_immutable_facts_policy='preserve_exact_owned_facts')
    base.write_manifest(path, manifest)
    bootstrap(m, ids)
    redis = m.apisix_policy_redis()
    try:
        with owned_hooks(m, ids, path, manifest):
            route = publish(m, ids, path, manifest, client, tokens)
            raw_key = purchase(m, ids, path, manifest, client, tokens, route)
            disable_fixture_sla_logging(m, ids, path, manifest)
            url = os.getenv('QA_VCS_GATEWAY_URL', 'http://market-apisix:9080').rstrip('/')
            url += '/gateway/' + ids['routes']['api']['source'] + '/v1/echo'
            def invoke():
                return httpx.get(url, headers={'X-API-Key': raw_key}, timeout=15,
                                 trust_env=False, follow_redirects=False)
            metadata_key, month_key = charge(m, ids, path, manifest, redis, invoke)
            legs = refund(m, ids, path, manifest, client, tokens, redis=redis)
            metadata = redis.hgetall(metadata_key)
            periods = json.loads(metadata['subscription_periods'])
            check(metadata.get('quota_scope') == manifest['database_ids']['native_refund']['combination_id']
                  and len(periods) == 1 and periods[0]['monthly_quota'] == 2
                  and periods[0]['period_key'] == manifest['database_ids']['native_refund']['combination_id'] + ':1:0'
                  and redis.get(month_key) == '2', 'Refund did not retain used2 with monthly quota2')
            with m.SessionLocal() as db:
                credential = db.get(m.ApiCredential, manifest['database_ids']['native_refund']['credential_id'])
                check(credential is not None and credential.status == 'active'
                      and credential.order_id == '' and credential.route_id == route
                      and credential.enterprise_id == ids['enterprises']['buyer'],
                      'Original shared credential changed after refund')
            denied = invoke()
            check(denied.status_code == 429 and redis.get(month_key) == '2',
                  'Native gateway did not enforce reduced quota')
            negative = negative_settlement(m, path, manifest, client, tokens)
            state = manifest['database_ids']['native_refund']
            source = state['negative_settlement_source']
            check(source in {'preexisting_producer_unasserted', 'flow_invoked_production_adapter'}
                  and state.get('report_csv_verified') == 1
                  and state.get('cross_month_coverage_claim') is False,
                  'Negative settlement public evidence incomplete')
            check_negative_amounts(negative)
            proof = dict(charges=2, refunded_amount=16, monthly_quota=2, monthly_used=2,
                         next_http_status=429, simulated_payment_legs=legs,
                         negative_settlement={key: float(negative[key]) for key in NEGATIVE_AMOUNTS},
                         negative_settlement_source=source, cross_month_coverage_claim=False,
                         report_csv_verified=1,
                         delivery_audit_identity_verified=manifest['database_ids']['native_refund'][
                             'delivery_audit_identity_verified'])
            if fixture_mode == 'coverage_fault':
                fault = manifest['database_ids']['native_refund']['coverage_fault']
                check(fault.get('failure_before_funds') and fault.get('retry_completed_idempotently')
                      and fault['status'] == 'restored', 'Coverage fault proof incomplete')
                proof.update(resource_failures_before_funds=1, idempotent_retry_completed=1)
            manifest['database_ids']['native_refund']['proof'] = proof
            base.write_manifest(path, manifest)
            return proof
    finally:
        redis.close()
