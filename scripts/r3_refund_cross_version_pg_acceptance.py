"""Owned PG BE011 cross-version acceptance, never deployment or APISIX E2E.

Private DATABASE_URL stays inside the pod; private SSHPASS is environment-only.
Reuse BE010's source transport and owned-schema seed/exercise injection. No
startup, public seed or protected-order content reads. Only sanitized JSON exits.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import r3_refund_workflow_pg_acceptance as workflow_probe

base = workflow_probe.base
transport = workflow_probe.transport
fixture = workflow_probe.fixture
SOURCE_FILES = ('main.py', 'refund_segment_store.py', 'refund_funding_store.py',
                'refund_workflow.py', 'refund_planning.py', 'api_charge_ledger.py',
                'subscription_terms.py', 'version_change_resources.py', 'refund_segment_boundary.py',
                'refund_economic_segments.py', 'refund_funding.py', 'version_change_saga.py',
                'version_change_worker.py')
GRAPH_NAMES = ('http-admin', 'http-ops', 'pool-race', 'review-race', 'wave',
               'request-audit', 'request-outbox', 'review-audit', 'review-outbox')
RUN_SECONDS = 150
TRANSPORT_SECONDS = 180
STREAM = 'market:apisix:charges:v1'
EPOCH = 'market:apisix:charges:source'
CASE_NAMES = ('asgi_admin', 'asgi_ops', 'cross_root_shared_pool_two_connections',
              'approval_version_two_connections', 'all_sources_wave_cap',
              'receipt_window_transaction_rollback_idempotency', 'request_audit_rollback',
              'request_outbox_rollback', 'review_audit_rollback', 'review_outbox_rollback',
              'numeric_streamid_real_pg_execution')
_deadline = None


def safe_error(exc):
    result = base.safe_error(exc)
    return {key: result[key] for key in ('status', 'error_type', 'line')}


def owned(db):
    from sqlalchemy import text
    base.check(base.owned_schema(db.scalar(text('SELECT current_schema()'))), 'Owned schema required')
    base.check(base.owned_schema(db.scalar(text('SHOW search_path'))), 'Exclusive owned search path required')


def identity(name):
    return 'xv-' + hashlib.sha256(name.encode()).hexdigest()[:28]


class MockRedis:
    """Synthetic external receipts/metadata/counters; does not execute Redis Lua."""
    def __init__(self):
        self.connection_pool = SimpleNamespace(connection_kwargs={})
        self.hashes = {EPOCH: {'run_id': 'a' * 40}}
        self.values = {}
        self.tail = '0-0'

    def info(self, section):
        base.check(section == 'server', 'Unexpected mock info section')
        return {'run_id': 'a' * 40}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def get(self, key):
        return self.values.get(key)

    def xrevrange(self, stream, count=1):
        base.check(stream == STREAM and count == 1, 'Unexpected mock stream request')
        return [] if self.tail == '0-0' else [(self.tail, {})]

    def close(self):
        pass


def graph(m, db, source, name, *, before_usage=0, frozen_rates=None):
    """Real models, verifier, receipt store and materializer; external intent mocked."""
    owned(db)
    rate_keys = m.order_economics['rate_keys']
    rates = {key: 0 for key in rate_keys}
    if frozen_rates is not None:
        base.check(isinstance(frozen_rates, dict) and set(frozen_rates) == set(rate_keys),
                   'Frozen rates require exactly the expected rate keys')
        try:
            values = {key: Decimal(str(value)) for key, value in frozen_rates.items()
                      if not isinstance(value, bool)}
        except (ValueError, ArithmeticError):
            raise base.AcceptanceFailure('Frozen rates must be finite nonnegative numbers') from None
        base.check(len(values) == len(rate_keys) and
                   all(value.is_finite() and value >= 0 for value in values.values()),
                   'Frozen rates must be finite nonnegative numbers')
        base.check(sum(values.values()) == Decimal(100), 'Frozen rates must sum to 100')
        rates = {key: str(value) for key, value in values.items()}
    store = m.refund_segment_store
    terms = m.subscription_terms
    calendar_module = importlib.import_module(m.__package__ + '.subscription_calendar')
    store_module = importlib.import_module(m.__package__ + '.refund_segment_store')
    boundary = importlib.import_module(m.__package__ + '.refund_segment_boundary')
    charges = importlib.import_module(m.__package__ + '.api_charge_ledger')
    economics = importlib.import_module(m.__package__ + '.order_economics')
    canonical, digest = store_module.canonical, store_module.digest
    money = economics.money
    at = m.now()
    anchor = (at - timedelta(days=7)).replace(microsecond=0)
    calendar = calendar_module.CalendarAnchor(anchor)
    pid, combo_id, change_id = (identity(name + suffix) for suffix in ('-product', '-combo', '-change'))
    old_id, target_id = identity(name + '-v1'), identity(name + '-v2')
    routes = [identity(name + '-route-' + side) for side in ('old', 'target')]
    route_keys = ['owned-' + rid for rid in routes]
    db.add(m.Product(id=pid, enterprise_id='owned-provider', name='Owned cross version',
        product_type='api', delivery_method='api', currency='CNY', status='published'))
    db.flush()
    for version_id, code in ((old_id, 'v1'), (target_id, 'v2')):
        db.add(m.ProductReleaseVersion(id=version_id, product_id=pid, version_code=code,
            price=9999, cost=7777, monthly_quota=100, daily_quota=0, rate_limit_per_minute=60, status='active'))
    for rid, key, code in zip(routes, route_keys, ('v1', 'v2')):
        db.add(m.ApiGatewayRoute(id=rid, product_id=pid, route_key=key, upstream_url='https://owned.invalid',
            version=code, status='active'))
    combo = terms['Combination'](id=combo_id, enterprise_id='owned-buyer', product_id=pid,
        delivery_method='api', version_id=old_id, version_code='v1', anchor_at=anchor,
        generation=1, total_months=2, status='active')
    db.add(combo)
    db.flush()
    rule = {'version': 'owned-frozen', 'rates': rates}
    quota = dict(rate_limit_per_minute=60, daily_quota=0, monthly_quota=100, quota_amount=0)
    delivery = dict(method='api', quota=quota, download_limit=0, files=[])
    roots, term_rows, snapshots, periods = [], [], {}, []
    for i in range(2):
        oid, tid = identity(name + '-order-' + str(i)), identity(name + '-term-' + str(i))
        bounds = calendar.period(i)
        economic = dict(schema_version=1, product_id=pid, product_version_id=old_id, version_code='v1',
            billing_unit='month', subscription_months=1, unit_price='10.00', unit_cost='2.00',
            amount='10.00', total_cost='2.00', currency='CNY', settlement_rule=rule)
        order = m.Order(id=oid, order_no='OWNED-XV-' + oid, product_id=pid, product_name='Owned cross version',
            buyer_enterprise_id='owned-buyer', buyer_user_id='owned-buyer', buyer_name='Owned buyer',
            provider_enterprise_id='owned-provider', amount=10, paid_amount=10, refunded_amount=0,
            payment_status='paid', main_status='completed', delivery_status='accepted', snapshot_version=1,
            product_version_id=old_id, product_version_code='v1', subscription_months=1,
            unit_price_snapshot=10, unit_cost_snapshot=2, total_cost_snapshot=2, delivery_method_snapshot='api',
            economic_snapshot_json=canonical(economic), delivery_snapshot_json=canonical(delivery))
        db.add(order)
        db.flush()
        db.add(m.Payment(id=identity(name + '-pay-' + str(i)), order_id=oid, payment_no='OWNED-XV-' + oid,
            status='paid', amount=10, paid_at=anchor + timedelta(seconds=i)))
        term = terms['Term'](id=tid, order_id=oid, combination_id=combo_id, generation=1,
            version_id=old_id, version_code='v1', start_month=i, months=1,
            starts_at=bounds.start, ends_at=bounds.end, status='active')
        db.add(term)
        target = dict(economic, product_version_id=target_id, version_code='v2', unit_price='50.00',
            unit_cost='10.00', amount='50.00', total_cost='10.00')
        snapshots[tid] = dict(economic=target, delivery=delivery)
        periods.append(dict(start_at=int(bounds.start.timestamp()), end_at=int(bounds.end.timestamp()),
            period_key=f'{combo_id}:1:{i}', route_key=route_keys[1], order_id=oid, segment_id=tid,
            monthly_quota=100, daily_quota=0, rate_limit_per_minute=60))
        roots.append(oid)
        term_rows.append(term)
    funding_id, funding_payment = identity(name + '-funding'), identity(name + '-upgrade-payment')
    remaining_economics = []
    for term in term_rows:
        duration = term.ends_at - term.starts_at
        remaining = max(timedelta(0), min(duration, term.ends_at - at))
        with localcontext() as context:
            context.prec = 40
            ratio = Decimal(remaining // timedelta(microseconds=1)) / Decimal(duration // timedelta(microseconds=1))
            remaining_economics.append((money(Decimal(10)*ratio), money(Decimal(2)*ratio),
                                        money(Decimal(50)*ratio), money(Decimal(10)*ratio)))
    upgrade_amount = sum((target - old for old, _old_cost, target, _cost in remaining_economics), Decimal(0))
    db.add(m.Order(id=funding_id, order_no='OWNED-XV-' + funding_id, product_id=pid,
        product_name='Owned upgrade funding', buyer_enterprise_id='owned-buyer', buyer_user_id='owned-buyer',
        buyer_name='Owned buyer', provider_enterprise_id='owned-provider', amount=upgrade_amount, paid_amount=upgrade_amount,
        refunded_amount=0, payment_status='paid', main_status='completed', delivery_status='accepted',
        business_type='version_change_payment', related_order_id=roots[0], economic_snapshot_json='{}'))
    db.flush()
    db.add(m.Payment(id=funding_payment, order_id=funding_id, payment_no='OWNED-XV-' + funding_id,
        status='paid', amount=upgrade_amount, paid_at=at - timedelta(seconds=1)))
    credential = m.ApiCredential(id=identity(name + '-credential'), route_id=routes[1], enterprise_id='owned-buyer',
        order_id='', key_hash=digest(name), apisix_consumer_name='owned-' + name, status='active',
        monthly_quota=100, daily_quota=0, rate_limit_per_minute=60)
    db.add(credential)
    change = m.version_change_saga['VersionChangeRequest'](id=change_id, source_order_id=roots[0],
        enterprise_id='owned-buyer', combination_id=combo_id, target_version_id=target_id, target_version_code='v2',
        old_version_id=old_id, old_version_code='v1', kind='upgrade', status='completed',
        idempotency_key='owned-' + name, request_hash=digest(name),
        quote_json=canonical(dict(currency='CNY', payment_amount=format(upgrade_amount, '.2f'))),
        prepared_json='{}', target_snapshots_json=canonical(snapshots), funding_order_id=funding_id,
        resource_applied=True, created_by='owned-admin', created_at=at, completed_at=at)
    db.add(change)
    db.flush()
    db.add(m.version_change_saga['VersionChangeFund'](change_id=change_id, kind='payment',
        payment_id=funding_payment, amount=upgrade_amount, idempotency_key='owned-fund-' + name))
    target_policy = dict(mode='subscription', status='active', quota_scope=combo_id,
        subscription_periods=json.dumps(periods, separators=(',', ':')), daily_quota='0', monthly_quota='100', total_quota='0')
    original_policy = dict(target_policy, subscription_periods=json.dumps(
        [dict(p, route_key=route_keys[0], monthly_quota=10) for p in periods], separators=(',', ':')))
    plan = dict(kind='api', change_id=change_id, old_route_id=routes[0], target_route_id=routes[1],
        old_route_key=route_keys[0], target_route_key=route_keys[1],
        credentials=[dict(id=credential.id, consumer=credential.apisix_consumer_name, status='active')],
        charge_boundary=dict(mode='strict', credential_id=credential.id, original=original_policy, target=target_policy))
    change.resource_plan_json = canonical(plan)
    cursor = db.get(m.api_charge_ledger['Cursor'], STREAM)
    if cursor is None:
        db.add(m.api_charge_ledger['Cursor'](stream=STREAM, last_id='0-0', redis_run_id='a'*40,
            activated_at=anchor - timedelta(days=1)))
    credential_key = 'market:apisix:credential:' + credential.apisix_consumer_name
    boundary_periods = store_module.boundary_periods(target_policy)
    payload = dict(change_id=change_id, direction='target', expected=original_policy, target=target_policy,
        periods=boundary_periods, credential_key=credential_key)
    payload['period_json'] = [canonical(p) for p in boundary_periods]
    payload['target_fingerprint'] = digest(target_policy)
    cutover_us = charges.microseconds(at)
    receipt_periods = [dict(p, used_at_switch=before_usage if i == 0 else 0,
                           available_quota=100 - before_usage if i == 0 else 100)
                       for i, p in enumerate(boundary_periods)]
    raw = dict(schema='refund_segment_boundary', version=1, change_id=change_id, direction='target', scope=combo_id,
        source_run_id='a'*40, boundary_id='1-2' if before_usage else '0-0', cutover_us=cutover_us,
        target_fingerprint=digest(target_policy), payload_fingerprint=digest(payload), periods=receipt_periods)
    source.hashes[credential_key] = target_policy
    source.values[boundary.NAMESPACE + change_id + ':target:' + hashlib.sha256(credential_key.encode()).hexdigest()] = canonical(raw)
    for p in periods:
        counter, coverage = charges.period_keys(combo_id, p['period_key'])
        source.values[counter] = str(before_usage if p['segment_id'] == term_rows[0].id else 0)
        source.hashes[coverage] = {'baseline': '0'}
    # Main sessions disable autoflush; the verifier must see Cursor and Fund rows.
    db.flush()
    receipt = store['record_receipt'](db, change, credential, 'target', raw, client=source)
    safe = dict(change_id=change_id, direction='target', kind='api', evidence=dict(route_id=routes[1],
        route_key=route_keys[1], credential_ids=[credential.id], receipt_ids=[receipt.id]))
    proof = dict(request_change_id=change_id, resource_change_id=change_id, evidence_fingerprint=digest(safe))
    change.evidence_json = canonical(proof)
    for term, values in zip(term_rows, remaining_economics):
        old_revenue, old_cost, target_revenue, target_cost = values
        for side, revenue, cost in (('old', -old_revenue, -old_cost), ('target', target_revenue, target_cost)):
            db.add(m.version_change_ledger['Model'](change_id=change_id, request_change_id=change_id,
                source_order_id=term.order_id, combination_id=combo_id, side=side, revenue=revenue, cost=cost,
                profit=revenue-cost, currency='CNY', original_version_id=old_id, target_version_id=target_id,
                rule_json=json.dumps(rule, sort_keys=True, separators=(',', ':'), ensure_ascii=False), effective_at=at,
                status='posted', payload_fingerprint=digest(dict(term=term.id, side=side)), resource_change_id=change_id,
                resource_evidence_fingerprint=proof['evidence_fingerprint'], posted_at=at))
    db.flush()
    terms['apply_version_change'](db, combo, target_id, 'v2', snapshots, at, change_id)
    windows = store['materialize'](db, change, proof)
    base.check(len(windows) == 2, 'Two windows required')
    for oid in roots:
        base.check(store['latest'](db, db.get(m.Order, oid)) is not None, 'Real latest rejected graph')
    return dict(roots=roots, combo=combo_id, change=change_id, windows=[w.id for w in windows],
        receipt=receipt.id, funding_payment=funding_payment, periods=receipt_periods, cutover=at,
        terms=[t.id for t in term_rows], proof=proof, funding_order=funding_id,
        pool_amount=format(Decimal(20) + upgrade_amount, '.2f'))


def seed(m):
    headers = fixture.seed(m)
    for name in ('refund_segment_store', 'refund_funding_store', 'api_charge_ledger'):
        base.check(isinstance(getattr(m, name, None), dict), 'Required main hook missing')
    base.check(callable(m.subscription_terms.get('verified_revision')), 'Real revision verifier missing')
    source = MockRedis()
    graphs = {}
    for name in GRAPH_NAMES:
        with m.SessionLocal.begin() as db:
            graphs[name] = graph(m, db, source, name)
    m._owned_cross_version = (source, graphs)
    return headers


def exercise(m, client, headers):
    from sqlalchemy import func, select, text
    from starlette.requests import Request
    source, graphs = m._owned_cross_version
    schema = importlib.import_module(m.__package__ + '.refund_schema')
    Case, Event = (m.refund_workflow[key] for key in ('Case', 'Event'))
    Allocation = m.refund_funding_store['FundingAllocation']
    Outbox = m.message_center['Outbox']
    results = {}

    def snapshot():
        with m.SessionLocal() as db:
            owned(db)
            return {table.name: [tuple(row) for row in db.execute(select(table).order_by(*table.primary_key.columns))]
                    for table in m.Base.metadata.sorted_tables if len(table.primary_key.columns)}

    def call(method, path, actor='buyer', expected=200, **kwargs):
        response = client.request(method, path, headers=headers[actor], **kwargs)
        base.check(response.status_code == expected, 'Unexpected ASGI status')
        return response.json() if expected < 400 else None

    def create(db, user, oid, amount, key):
        request = Request(dict(type='http', method='POST', path='/api/orders/' + oid + '/refund-requests',
            query_string=b'enterprise_id=owned-buyer&business_view=buyer', headers=[]))
        return m.refund_workflow['create'](db, user, oid, schema.RefundRequestBody(amount=Decimal(amount),
            reason='Owned cross-version request', idempotency_key=key), request)

    def review(db, user, rid, key):
        return m.refund_workflow['review'](db, user, rid, schema.RefundReviewBody(decision='approve',
            reason='Owned cross-version review', expected_version=1, idempotency_key=key))

    def transaction(actor, operation, barrier=None):
        with m.SessionLocal() as db:
            owned(db)
            backend_pid = db.scalar(text('SELECT pg_backend_pid()'))
            user = db.get(m.User, 'owned-' + actor)
            if barrier:
                barrier.wait(timeout=8)
            try:
                refund = operation(db, user)
                db.flush()
                rid = refund.id
                db.commit()
                return dict(status=200, pid=backend_pid, id=rid)
            except m.HTTPException as exc:
                db.rollback()
                return dict(status=exc.status_code, pid=backend_pid, id=None)

    def pair(operations):
        barrier = threading.Barrier(2, timeout=8)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(transaction, actor, operation, barrier) for actor, operation in operations]
            rows = [future.result(timeout=15) for future in futures]
        base.check(len({row['pid'] for row in rows}) == 2, 'Independent PG connections required')
        return rows

    def allocations(rid):
        with m.SessionLocal() as db:
            return [tuple(row) for row in db.execute(select(Allocation.payment_id, Allocation.source_order_id,
                Allocation.amount, Allocation.currency, Allocation.window_id, Allocation.window_hash)
                .where(Allocation.refund_id == rid).order_by(Allocation.rowid))]

    def pool_check(g):
        with m.SessionLocal() as db:
            paid = dict(db.execute(select(m.Payment.id, m.Payment.amount).join(m.Order,
                m.Order.id == m.Payment.order_id).where(m.Order.product_id == db.get(m.Order, g['roots'][0]).product_id)).all())
            held = dict(db.execute(select(Allocation.payment_id, func.sum(Allocation.amount)).join(m.Refund,
                m.Refund.id == Allocation.refund_id).where(Allocation.payment_id.in_(paid),
                    m.Refund.status.in_(('requested', 'approved', 'executing', 'failed', 'completed')))
                .group_by(Allocation.payment_id)).all())
            base.check(sum(paid.values()) == Decimal(g['pool_amount']), 'Fixture pool must include both Term payments and upgrade')
            base.check(all(0 <= value <= paid[key] for key, value in held.items()), 'Source pool oversubscribed')
            base.check(sum(held.values(), Decimal(0)) <= sum(paid.values()), 'Wave total exceeds pool')
            return sum(held.values(), Decimal(0))

    def http_chain(actor):
        g = graphs['http-' + actor]
        oid = g['roots'][0]
        call('POST', '/api/orders/' + oid + '/refund-quote', json={'amount': '20.00'})
        refund = call('POST', '/api/orders/' + oid + '/refund-requests',
            params={'enterprise_id': 'owned-buyer', 'business_view': 'buyer'},
            json=dict(amount='20.00', reason='Owned ASGI request', idempotency_key='owned-http-' + actor))
        rid = refund['id']
        held = allocations(rid)
        base.check(held and held[0][0] == g['funding_payment'], 'Request did not reserve upgrade first')
        body = dict(decision='approve', reason='Owned ASGI review', expected_version=1, idempotency_key='owned-review-' + actor)
        call('POST', '/api/refunds/' + rid + '/review', 'finance', 403, json=body)
        call('POST', '/api/refunds/' + rid + '/review', actor, json=body)
        base.check(allocations(rid) == held, 'Approval reordered frozen request allocations')
        with m.SessionLocal() as db:
            case = db.get(Case, rid)
            base.check(case.plan_hash == hashlib.sha256(case.plan_json.encode()).hexdigest(), 'Case plan hash mismatch')
            base.check(db.get(m.Order, g['funding_order']).economic_snapshot_json == '{}', 'Funding fixture must have empty economics')
        return dict(quote=200, request=200, approval=200, finance=403, lifo_preserved=True, transport='registered ASGI')

    def pool_race():
        g = graphs['pool-race']
        rows = pair([('buyer', lambda db, u, i=i: create(db, u, g['roots'][i], '60.00', 'owned-pool-' + str(i))) for i in range(2)])
        base.check(sorted(row['status'] for row in rows) == [200, 409], 'Shared cross-root pool must reject oversubscription')
        base.check(pool_check(g) == Decimal(60), 'Cross-root reservation amount mismatch')
        winner = next(row for row in rows if row['status'] == 200)
        base.check(allocations(winner['id'])[0][0] == g['funding_payment'], 'LIFO upgrade allocation missing')
        released = transaction('ops', lambda db, u: m.refund_workflow['review'](db, u, winner['id'],
            schema.RefundReviewBody(decision='reject', reason='Owned cash hold release', expected_version=1,
                                    idempotency_key='owned-pool-reject-release')))
        base.check(released['status'] == 200 and pool_check(g) == 0, 'Rejection did not release winning cash hold')
        return dict(http_equivalent_statuses=[200, 409], independent_pg_connections=2,
                    reserved_before_release='60.00', reserved_after_reject='0.00')

    def review_race():
        g = graphs['review-race']
        row = transaction('buyer', lambda db, u: create(db, u, g['roots'][0], '20.00', 'owned-review-create'))
        base.check(row['status'] == 200, 'Review request failed')
        held = allocations(row['id'])
        rows = pair([(actor, lambda db, u, actor=actor: review(db, u, row['id'], 'owned-review-race-' + actor))
                     for actor in ('admin', 'ops')])
        base.check(sorted(item['status'] for item in rows) == [200, 409], 'Competing review versions must conflict')
        base.check(allocations(row['id']) == held, 'Concurrent approval changed original allocation')
        with m.SessionLocal() as db:
            base.check(db.get(Case, row['id']).version == 2, 'Review committed twice')
            base.check(db.scalar(select(func.count()).select_from(Event).where(Event.refund_id == row['id'])) == 2,
                'Concurrent review duplicated events')
        return dict(http_equivalent_statuses=[200, 409], independent_pg_connections=2, version=2)

    def wave():
        g = graphs['wave']
        total_cents = int(Decimal(g['pool_amount']) * 100)
        for i in range(4):
            amount = Decimal(total_cents // 4 if i < 3 else total_cents - 3*(total_cents // 4)) / 100
            row = transaction('buyer', lambda db, u, i=i, amount=amount:
                              create(db, u, g['roots'][i % 2], str(amount), 'owned-wave-' + str(i)))
            base.check(row['status'] == 200, 'All-source wave failed before full pool')
            pool_check(g)
        extra = transaction('buyer', lambda db, u: create(db, u, g['roots'][0], '0.01', 'owned-wave-overflow'))
        base.check(extra['status'] == 409 and pool_check(g) == Decimal(g['pool_amount']), 'Wave exceeded all-source pool')
        return dict(sources=3, waves=4, reserved=g['pool_amount'], overflow=409)

    def rollback(operation, kind):
        name = operation + '-' + kind
        g = graphs[name]
        rid = None
        if operation == 'review':
            row = transaction('buyer', lambda db, u: create(db, u, g['roots'][0], '20.00', 'owned-pre-' + name))
            base.check(row['status'] == 200, 'Rollback request failed')
            rid = row['id']
        before = snapshot()
        injected = []
        real = m.audit if kind == 'audit' else m.message_center['create']
        def fault(*args, **kwargs):
            real(*args, **kwargs)
            db = args[0]
            db.flush()
            if operation == 'request':
                base.check(db.scalar(select(func.count()).select_from(Allocation)) > len(before[Allocation.__tablename__]),
                    'Request must flush real funding allocation before injected fault')
            else:
                base.check(bool(db.get(Case, rid).plan_hash), 'Review must freeze real economic plan before fault')
            if kind == 'outbox':
                base.check(db.scalar(select(func.count()).select_from(Outbox)) > len(before[Outbox.__tablename__]),
                    'Real Outbox insertion required')
            injected.append(True)
            raise RuntimeError('Owned injected rollback')
        injection = patch.object(m, 'audit', side_effect=fault) if kind == 'audit' else patch.dict(m.message_center, create=fault)
        failed = False
        with injection:
            try:
                transaction('buyer' if operation == 'request' else 'ops', lambda db, u:
                    create(db, u, g['roots'][0], '20.00', 'owned-fault-' + name) if operation == 'request'
                    else review(db, u, rid, 'owned-fault-' + name))
            except RuntimeError:
                failed = True
        base.check(failed and injected and snapshot() == before, 'Fault left partial PG state')
        return dict(real_flush=True, allocations_caseplan_windows_audit_outbox_unchanged=True)

    def receipt_window_rollback():
        before = snapshot()
        with m.SessionLocal() as db:
            g = graph(m, db, source, 'receipt-rollback')
            change = db.get(m.version_change_saga['VersionChangeRequest'], g['change'])
            again = m.refund_segment_store['materialize'](db, change, g['proof'])
            base.check(sorted(row.id for row in again) == sorted(g['windows']), 'Materialize replay duplicated windows')
            db.rollback()
        base.check(snapshot() == before, 'Receipt/window transaction rollback left rows')
        return dict(receipt_and_window_rollback=True, repeat_materialize_same_ids=True)

    def numeric_watermark():
        ledger_module = importlib.import_module(m.__package__ + '.api_charge_ledger')
        Charge, Cursor = (m.api_charge_ledger[key] for key in ('Charge', 'Cursor'))
        with m.SessionLocal.begin() as db:
            g = graph(m, db, source, 'numeric-watermark', before_usage=1)
            period = g['periods'][0]
            for stream_id in ('1-2', '1-10'):
                db.add(Charge(id=stream_id, order_id=g['roots'][0], segment_id=g['terms'][0], scope=g['combo'],
                    period_key=period['period_key'], route_key=period['route_key'], quantity=1,
                    charged_at_us=ledger_module.microseconds(g['cutover'])-1, policy=ledger_module.POLICY, source_run_id='a'*40))
            db.get(Cursor, STREAM).last_id = '1-10'
        source.tail = '1-10'
        counter, _coverage = ledger_module.period_keys(g['combo'], g['periods'][0]['period_key'])
        source.values[counter] = '2'
        with m.SessionLocal() as db:
            owned(db)
            numeric = ledger_module.stream_position_sql(Charge.id, 'postgresql')
            ids = list(db.scalars(select(Charge.id).where(Charge.scope == g['combo'], numeric > (1, 2))))
            base.check(ids == ['1-10'], 'Executed PG numeric StreamID comparison failed')
            observed_at = m.now()
            summary = m.api_charge_ledger['window_summary'](db, order_id=g['roots'][0], segment_id=g['terms'][0],
                start=g['cutover'], end=observed_at, as_of=observed_at, source_run_id='a'*40, boundary_id='1-2',
                scope=g['combo'], periods=g['periods'], receipt_evidence=('receipt:' + g['receipt'],))
            base.check(summary.complete and summary.charged_calls == 1, 'Real watermark summary must include 1-10 after 1-2')
        return dict(pg_sql_executed=True, boundary='1-2', after='1-10', charged_calls=1,
                    external_source='MockRedis; timestamps deliberately precede cutover')

    cases = [('asgi_admin', lambda: http_chain('admin')), ('asgi_ops', lambda: http_chain('ops')),
             ('cross_root_shared_pool_two_connections', pool_race), ('approval_version_two_connections', review_race),
             ('all_sources_wave_cap', wave), ('receipt_window_transaction_rollback_idempotency', receipt_window_rollback),
             *[(op + '_' + kind + '_rollback', lambda o=op, k=kind: rollback(o, k))
               for op in ('request', 'review') for kind in ('audit', 'outbox')],
             ('numeric_streamid_real_pg_execution', numeric_watermark)]
    with patch.object(m, 'apisix_policy_redis', return_value=source):
        for name, run in cases:
            if _deadline is not None and time.monotonic() >= _deadline:
                results[name] = safe_error(TimeoutError('Owned deadline exhausted'))
                continue
            try:
                results[name] = dict(status='passed', **run())
            except Exception as exc:
                results[name] = safe_error(exc)
    return results


def remote_run(encoded, manifest):
    global _deadline
    logging.disable(logging.CRITICAL)
    _deadline = time.monotonic() + RUN_SECONDS
    previous = signal.getsignal(signal.SIGALRM)
    def timeout(_signum, _frame):
        raise TimeoutError('Owned acceptance deadline')
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(RUN_SECONDS)
    try:
        with tempfile.TemporaryDirectory(prefix='qa-r3-cross-version-') as directory:
            root = Path(directory)
            transport.unpack_workingtree(encoded, manifest, root / 'app')
            base.check(not any(name == 'app' or name.startswith('app.') for name in sys.modules), 'Fresh source process required')
            base.check(all(name in manifest for name in SOURCE_FILES), 'Required source inventory incomplete')
            sys.path.insert(0, str(root))
            try:
                with patch.object(base, 'seed', seed), patch.object(base, 'exercise', exercise):
                    result = base.run()
            finally:
                sys.path.remove(str(root))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        _deadline = None
    for error_key in ('setup_or_execution_error', 'error'):
        if isinstance(result.get(error_key), dict):
            result[error_key] = {key: value for key, value in result[error_key].items()
                                 if key in ('status', 'error_type', 'line')}
    protected = result.get('protected_order_count_before') == result.get('protected_order_count_after') == 1
    result.setdefault('cases', {})['protected_count_only'] = ({'status': 'passed', 'before': 1, 'after': 1}
        if protected else safe_error(AssertionError('Protected count mismatch')))
    result.update(coverage=[], incremental_scope='BE011 cross-version API funding/store scenarios only',
        source_kind='workingtree', deployed=False,
        source_sha256={name: manifest[name] for name in SOURCE_FILES},
        source_package_sha256=hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        scope=('Actual main models, PG owned schema and registered ASGI; synthetic external receipts/MockRedis; '
               'hook races are HTTP-equivalent; not an actual APISIX complete chain'),
        external_adapters={'Redis': 'MockRedis synthetic intent, metadata and counters',
                           'target_resource': 'Simulated target already applied; no APISIX call'},
        gaps=['Not an APISIX/Lua/gateway complete chain', 'No ingress, bank execution, notification delivery or deployment',
              'Worker source is hashed and packaged, not executed by this script',
              'SaaS revised segments lack trusted actual switch-time evidence; not covered',
              'Requires parent main/workflow all-Term funding bridge; missing integration fails cases'])
    result['status'] = ('passed' if set(CASE_NAMES).issubset(result['cases'])
                        and not result.get('setup_or_execution_error') and result.get('cleanup') == 'owned schema removed'
                        and all(case['status'] == 'passed' for case in result['cases'].values()) else 'failed')
    return result


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pod', required=True)
    args = parser.parse_args()
    base.check(bool(re.fullmatch(r'market-api-[a-z0-9-]+', args.pod)), 'Invalid pod')
    base.check(bool(os.getenv('SSHPASS')), 'Private SSHPASS required')
    encoded, manifest = transport.pack_workingtree(Path(__file__).resolve().parents[1])
    payload = 'import sys,types\n'
    for name in ('r3_backend_acceptance_pg', 'r3_backend_acceptance_concurrency',
                 'r3_after_sales_group_pg_acceptance', 'r3_refund_workflow_pg_acceptance'):
        code = Path(__file__).with_name(name + '.py').read_text()
        payload += 'm=types.ModuleType(' + repr(name) + ');sys.modules[m.__name__]=m;exec(compile(' + repr(code) + ',m.__name__,"exec"),m.__dict__)\n'
    payload += "__name__='cross_version_remote'\n" + Path(__file__).read_text()
    payload += '\ntry:\n print(json.dumps(remote_run(' + repr(encoded) + ',' + repr(manifest) + ')))\nexcept Exception as exc:\n print(json.dumps({"status":"failed","error":safe_error(exc)}))\n'
    command = ('kubectl --request-timeout=5s -n market exec -i ' + args.pod
               + ' -- env MESSAGE_CENTER_DISABLE_BACKGROUND=true PYTHONDONTWRITEBYTECODE=1 python -B -')
    process = subprocess.run(['sshpass', '-e', 'ssh', '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1',
        '-o', 'LogLevel=ERROR', 'root@192.168.10.10', command], input=payload, capture_output=True,
        text=True, timeout=TRANSPORT_SECONDS)
    base.check(process.returncode == 0, 'Bounded transport failed; private diagnostics suppressed')
    result = json.loads(process.stdout)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get('status') == 'passed' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': safe_error(exc)}))
        raise SystemExit(1)
