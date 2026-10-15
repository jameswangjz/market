"""Disposable SQLite store tests with actual isolated Redis boundary receipts."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import Column, Integer, String, Text, Numeric, create_engine, select, event
from sqlalchemy.orm import declarative_base, Session

from app.refund_segment_store import install, canonical
from app.refund_segment_boundary import freeze_policy_switch
from app.subscription_terms import UTCDateTime
import test_refund_segment_boundary as boundary_fixture


class StoreTests(unittest.TestCase):
    setUpClass = classmethod(boundary_fixture.BoundaryTests.setUpClass.__func__)
    tearDownClass = classmethod(boundary_fixture.BoundaryTests.tearDownClass.__func__)

    def setUp(self):
        self.client.flushdb()
        Base = declarative_base()
        def model(name, fields):
            tables = dict(Order='orders', Term='subscription_terms', Revision='subscription_term_revisions',
                          Request='version_change_requests', Credential='api_credentials', Subscription='saas_subscriptions')
            return type(name, (Base,), dict(__tablename__=tables.get(name, name.lower()), id=Column(String, primary_key=True), **fields))
        self.Order = model('Order', {k: Column(String) for k in ('product_id', 'buyer_enterprise_id')})
        self.Combo = model('Combo', dict({k: Column(String) for k in ('enterprise_id', 'product_id', 'delivery_method')},
            generation=Column(Integer), anchor_at=Column(UTCDateTime())))
        self.Term = model('Term', dict({k: Column(String) for k in ('order_id', 'combination_id', 'status')},
            generation=Column(Integer), start_month=Column(Integer), months=Column(Integer),
            starts_at=Column(UTCDateTime()), ends_at=Column(UTCDateTime())))
        self.Revision = model('Revision', dict({k: Column(Text) for k in ('change_id', 'term_id',
            'version_id', 'version_code', 'economic_snapshot_json', 'delivery_snapshot_json')}, revision_number=Column(Integer)))
        self.Request = model('Request', dict({k: Column(Text) for k in ('combination_id', 'enterprise_id',
            'target_version_id', 'target_version_code', 'status', 'evidence_json', 'target_snapshots_json', 'resource_plan_json')},
            resource_applied=Column(Integer), created_at=Column(UTCDateTime())))
        self.Ledger = model('Ledger', dict({k: Column(Text) for k in ('change_id', 'source_order_id', 'side',
            'status', 'combination_id', 'request_change_id', 'target_version_id', 'currency', 'rule_json',
            'resource_change_id', 'resource_evidence_fingerprint')}, revenue=Column(Numeric(18, 2)),
            cost=Column(Numeric(18, 2)), effective_at=Column(UTCDateTime())))
        self.Cursor = type('Cursor', (Base,), dict(__tablename__='cursor', stream=Column(String, primary_key=True),
            redis_run_id=Column(String), activated_at=Column(UTCDateTime())))
        self.Route = model('Route', dict(product_id=Column(String)))
        self.Credential = model('Credential', {k: Column(String) for k in ('enterprise_id', 'route_id', 'status', 'apisix_consumer_name')})
        self.Subscription = model('Subscription', {k: Column(String) for k in
            ('enterprise_id', 'product_id', 'external_tenant_id', 'version_id', 'status')})
        self.Config = model('Config', dict(status=Column(String)))
        self.now = datetime.fromtimestamp(self.client.time()[0], timezone.utc)
        self.ns = dict(Base=Base, Order=self.Order, ApiGatewayRoute=self.Route,
            SaaSSubscription=self.Subscription, SaaSIntegrationConfig=self.Config,
            api_charge_ledger={'Cursor': self.Cursor}, version_change_ledger={'Model': self.Ledger},
            version_change_saga={'VersionChangeRequest': self.Request}, now=lambda: self.now,
            subscription_terms=dict(Combination=self.Combo, Term=self.Term, Revision=self.Revision,
                effective_snapshot=lambda db, order, term: dict(economic=self.economic, delivery=self.delivery)))
        self.hooks = install(self.ns)
        self.engine = create_engine('sqlite://')
        @event.listens_for(self.engine, 'connect')
        def enable_foreign_keys(connection, _record):
            connection.execute('PRAGMA foreign_keys=ON')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        anchor = self.now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        from app.subscription_calendar import CalendarAnchor
        calendar = CalendarAnchor(anchor)
        self.combo = self.Combo(id='combo', enterprise_id='buyer', product_id='product', delivery_method='api',
            generation=1, anchor_at=anchor)
        self.db.add_all([self.combo, self.Route(id='new', product_id='product'),
            self.Route(id='old', product_id='product')])
        self.periods = []
        self.economic = dict(product_id='product', product_version_id='v2', version_code='two',
            subscription_months=1, amount='100.00', total_cost='20.00', currency='CNY', settlement_rule={'a': 1})
        self.delivery = dict(method='api', quota={'monthly_quota': 100})
        snapshots = {}
        for i in range(2):
            period = calendar.period(i)
            self.db.add_all([self.Order(id=f'o{i}', product_id='product', buyer_enterprise_id='buyer'),
                self.Term(id=f't{i}', order_id=f'o{i}', combination_id='combo', generation=1, start_month=i,
                    months=1, starts_at=period.start, ends_at=period.end, status='active')])
            self.periods.append(dict(start_at=int(period.start.timestamp()), end_at=int(period.end.timestamp()),
                period_key=f'combo:1:{i}', route_key='new', monthly_quota=100, order_id=f'o{i}', segment_id=f't{i}',
                daily_quota=0, rate_limit_per_minute=10))
            snapshots[f't{i}'] = dict(economic=self.economic, delivery=self.delivery)
        self.credential = self.Credential(id='cred', enterprise_id='buyer', route_id='new', status='active', apisix_consumer_name='store')
        self.db.add(self.credential)
        self.target = dict(mode='subscription', status='active', quota_scope='combo',
            subscription_periods=json.dumps(self.periods, separators=(',', ':')), daily_quota='0', monthly_quota='100', total_quota='0')
        self.original = dict(self.target, subscription_periods=json.dumps([dict(p, route_key='old') for p in self.periods], separators=(',', ':')))
        self.plan = dict(kind='api', old_route_id='old', target_route_id='new', target_route_key='new',
            credentials=[dict(id='cred', consumer='store', status='active')],
            charge_boundary=dict(mode='strict', credential_id='cred', original=self.original, target=self.target))
        self.proof = dict(request_change_id='change', resource_change_id='change', evidence_fingerprint='f'*64)
        self.change = self.Request(id='change', combination_id='combo', enterprise_id='buyer', target_version_id='v2',
            target_version_code='two', status='refund_pending', evidence_json=canonical(self.proof), resource_applied=1,
            target_snapshots_json=canonical(snapshots), resource_plan_json=canonical(self.plan))
        self.db.add(self.change)
        run = self.client.info('server')['run_id']
        self.client.hset('market:apisix:charges:source', mapping={'run_id': run})
        self.db.add(self.Cursor(stream='market:apisix:charges:v1', redis_run_id=run, activated_at=anchor-timedelta(days=1)))
        self.client.hset('market:apisix:credential:store', mapping=self.original)
        from app.quota_cutover import token
        for p in self.periods:
            self.client.hset('market:apisix:charges:coverage:combo:' + token(p['period_key']), mapping={'baseline': '0'})
        self.db.commit()
        from app.refund_segment_store import boundary_periods
        self.receipt = freeze_policy_switch(self.client, credential_key='market:apisix:credential:store',
            change_id='change', direction='target', expected_policy=self.original, target_policy=self.target,
            periods=boundary_periods(self.target))

    def record(self):
        from app.refund_segment_store import digest
        row = self.hooks['record_receipt'](self.db, self.change, self.credential, 'target', self.receipt, client=self.client)
        self.proof['evidence_fingerprint'] = digest(dict(change_id='change', direction='target', kind='api',
            evidence=dict(route_id='new', route_key='new', credential_ids=['cred'], receipt_ids=[row.id])))
        self.change.evidence_json = canonical(self.proof)
        return row

    def revisions(self):
        from app.order_economics import money
        from decimal import Decimal
        for i in range(2):
            term = self.db.get(self.Term, f't{i}')
            ratio = Decimal(max(timedelta(0), min(term.ends_at-term.starts_at, term.ends_at-self.now)) // timedelta(microseconds=1)) / Decimal((term.ends_at-term.starts_at)//timedelta(microseconds=1))
            self.db.add(self.Revision(id=f'r{i}', change_id='change', term_id=term.id, version_id='v2', version_code='two',
                revision_number=1, economic_snapshot_json=canonical(self.economic), delivery_snapshot_json=canonical(self.delivery)))
            self.db.add(self.Ledger(id=f'l{i}', change_id='change', source_order_id=f'o{i}', side='target', status='pending',
                combination_id='combo', request_change_id='change', target_version_id='v2', currency='CNY',
                rule_json=canonical(self.economic['settlement_rule']), revenue=money(Decimal(100)*ratio), cost=money(Decimal(20)*ratio),
                effective_at=self.now, resource_change_id='change', resource_evidence_fingerprint=self.proof['evidence_fingerprint']))
        self.db.flush()

    def test_receipt_idempotency_conflict_and_rollback(self):
        row = self.record()
        self.assertEqual(row.id, self.record().id)
        bad = copy.deepcopy(self.receipt)
        bad['cutover_us'] += 1
        with self.assertRaises(ValueError):
            self.hooks['record_receipt'](self.db, self.change, self.credential, 'target', bad, client=self.client)
        self.db.rollback()
        self.assertEqual([], self.db.scalars(select(self.hooks['Receipt'])).all())

    def test_missing_epoch_and_numeric_boolean_rejected(self):
        self.client.delete('market:apisix:charges:source')
        with self.assertRaises(ValueError):
            self.record()

    def test_receipt_immutable_and_registration_must_precede_cutover(self):
        cursor = self.db.get(self.Cursor, 'market:apisix:charges:v1')
        cursor.activated_at = self.now + timedelta(days=1)
        with self.assertRaises(ValueError):
            self.record()
        self.db.rollback()
        row = self.record()
        self.db.commit()
        row.boundary_id = '1-0'
        with self.assertRaises(ValueError):
            self.db.flush()
        self.db.rollback()
        self.db.delete(row)
        with self.assertRaises(ValueError):
            self.db.flush()

    def test_windows_current_future_and_same_transaction_rollback(self):
        self.record()
        self.revisions()
        windows = self.hooks['materialize'](self.db, self.change, self.proof)
        self.assertEqual(['o0', 'o1'], [w.order_id for w in windows])
        self.assertEqual([100, 100], [w.available_quota for w in windows])
        self.assertEqual(windows, self.hooks['materialize'](self.db, self.change, self.proof))
        self.assertIsNone(self.hooks['latest'](self.db, self.db.get(self.Order, 'o0')))
        self.db.rollback()
        self.assertEqual([], self.db.scalars(select(self.hooks['Window'])).all())
        self.assertEqual([], self.db.scalars(select(self.hooks['Receipt'])).all())

    def test_strict_missing_receipt_revision_proof_and_legacy(self):
        with self.assertRaises(HTTPException):
            self.hooks['materialize'](self.db, self.change, self.proof)
        self.revisions()
        with self.assertRaises(HTTPException):
            self.hooks['materialize'](self.db, self.change, self.proof)
        self.record()
        self.change.resource_applied = 0
        with self.assertRaises(HTTPException):
            self.hooks['materialize'](self.db, self.change, self.proof)
        self.change.resource_plan_json = '{}'
        self.assertEqual([], self.hooks['materialize'](self.db, self.change, self.proof))

    def test_latest_waits_posting_and_never_falls_back(self):
        self.record()
        self.revisions()
        windows = self.hooks['materialize'](self.db, self.change, self.proof)
        self.change.status = 'completed'
        order = self.db.get(self.Order, 'o0')
        self.assertIsNone(self.hooks['latest'](self.db, order))
        self.db.get(self.Ledger, 'l0').status = 'posted'
        self.assertEqual(windows[0].id, self.hooks['latest'](self.db, order).id)
        self.assertEqual(windows[0].id, self.hooks['latest'](self.db, order).id)
        self.db.add(self.Revision(id='new-revision', change_id='unproven', term_id='t0', version_id='v2', version_code='two',
            revision_number=2, economic_snapshot_json=canonical(self.economic), delivery_snapshot_json=canonical(self.delivery)))
        self.db.flush()
        self.assertIsNone(self.hooks['latest'](self.db, order))

    def test_window_immutable_and_conflicting_replay(self):
        self.record()
        self.revisions()
        window = self.hooks['materialize'](self.db, self.change, self.proof)[0]
        self.db.commit()
        window.available_quota = 99
        with self.assertRaises(ValueError):
            self.db.flush()
        self.db.rollback()
        self.db.delete(window)
        with self.assertRaises(ValueError):
            self.db.flush()

    def test_unverified_effective_snapshot_and_conflicting_window_replay(self):
        self.record()
        self.revisions()
        effective = self.ns['subscription_terms']['effective_snapshot']
        self.ns['subscription_terms']['effective_snapshot'] = lambda *_: dict(economic={}, delivery={})
        with self.assertRaises(HTTPException):
            self.hooks['materialize'](self.db, self.change, self.proof)
        self.ns['subscription_terms']['effective_snapshot'] = effective
        self.hooks['materialize'](self.db, self.change, self.proof)
        changed = dict(self.proof, evidence_fingerprint='e'*64)
        self.change.evidence_json = canonical(changed)
        with self.assertRaises(HTTPException):
            self.hooks['materialize'](self.db, self.change, changed)

    def test_zero_remaining_is_zero_and_unlimited_is_none(self):
        from app.refund_segment_store import boundary_periods
        from app.quota_cutover import token
        for p in self.periods:
            if p['order_id'] == 'o1':
                p['monthly_quota'] = 0
        self.target['subscription_periods'] = json.dumps(self.periods, separators=(',', ':'))
        self.plan['charge_boundary']['target'] = self.target
        self.change.resource_plan_json = canonical(self.plan)
        self.client.hset('market:apisix:credential:store', mapping=self.original)
        from app import refund_segment_boundary as boundary
        for key in self.client.scan_iter(boundary.NAMESPACE + '*'):
            self.client.delete(key)
        self.client.set('market:apisix:quota:subscription:month:combo:' + token('combo:1:0'), '100')
        self.receipt = freeze_policy_switch(self.client, credential_key='market:apisix:credential:store',
            change_id='change', direction='target', expected_policy=self.original, target_policy=self.target,
            periods=boundary_periods(self.target))
        self.record()
        self.revisions()
        windows = self.hooks['materialize'](self.db, self.change, self.proof)
        self.assertEqual(0, windows[0].available_quota)
        self.assertIsNone(windows[1].available_quota)


class SaasStoreTests(unittest.TestCase):
    setUpClass = classmethod(StoreTests.setUpClass.__func__)
    tearDownClass = classmethod(StoreTests.tearDownClass.__func__)
    revisions = StoreTests.revisions

    def setUp(self):
        from app.refund_segment_store import digest
        StoreTests.setUp(self)
        self.combo.delivery_method = 'tenant_access'
        self.delivery['method'] = 'tenant_access'
        snapshots = json.loads(self.change.target_snapshots_json)
        for snapshot in snapshots.values():
            snapshot['delivery'] = self.delivery
        self.change.target_snapshots_json = canonical(snapshots)
        self.change.created_at = self.now - timedelta(seconds=10)
        self.subscription = self.Subscription(id='sub', enterprise_id='buyer', product_id='product',
            external_tenant_id='tenant', version_id='v2', status='active')
        self.config = self.Config(id='cfg', status='active')
        self.db.add_all([self.subscription, self.config])
        fingerprint = digest({key: getattr(self.config, key, None) for key in (
            'id', 'base_url', 'operation_path', 'token_url', 'client_id', 'scope', 'auth_mode', 'timeout_ms', 'status')})
        self.plan = dict(kind='saas', change_id='change', subscription_id='sub', tenant_id='tenant',
            product_id='product', enterprise_id='buyer', config_id='cfg', config_fingerprint=fingerprint)
        self.change.resource_plan_json = canonical(self.plan)
        effective = dict(change_id='change', direction='target', effective_at=self.now.isoformat(), ret=0)
        self.responses = dict(change=dict(effective, token='must-not-persist'), status=dict(effective,
            subscription_id='sub', tenant_id='tenant', product_id='product', enterprise_id='buyer', version='two', status='active'))
        self.db.commit()

    def record(self, responses=None, direction='target'):
        from app.refund_segment_store import digest
        row = self.hooks['record_receipt'](self.db, self.change, None, direction,
            responses if responses is not None else self.responses, subscription=self.subscription)
        evidence = dict(subscription_id='sub', tenant_id='tenant', version_id='v2')
        if row is not None:
            evidence['receipt_ids'] = [row.id]
        self.proof['evidence_fingerprint'] = digest(dict(change_id='change', direction='target', kind='saas', evidence=evidence))
        self.change.evidence_json = canonical(self.proof)
        return row

    def test_saas_receipt_window_atomic_idempotent_and_safe(self):
        row = self.record()
        self.assertEqual('saas', row.kind)
        self.assertEqual('sub', row.subscription_id)
        self.assertIsNone(row.credential_id)
        self.assertIsNone(row.source_run_id)
        self.assertNotIn('must-not-persist', row.receipt_json)
        self.assertEqual(row.id, self.record().id)
        self.revisions()
        windows = self.hooks['materialize'](self.db, self.change, self.proof)
        self.assertEqual([None, None], [w.available_quota for w in windows])
        self.assertEqual(windows, self.hooks['materialize'](self.db, self.change, self.proof))
        self.db.rollback()
        self.assertEqual([], self.db.scalars(select(self.hooks['Receipt'])).all())
        self.assertEqual([], self.db.scalars(select(self.hooks['Window'])).all())

    def test_saas_actual_time_prorates_frozen_ledger_and_latest(self):
        effective_at = self.now + timedelta(seconds=5)
        self.ns['now'] = lambda: self.now + timedelta(seconds=10)
        for response in self.responses.values():
            response['effective_at'] = effective_at.isoformat()
        self.record()
        self.revisions()
        window = self.hooks['materialize'](self.db, self.change, self.proof)[0]
        self.assertEqual(effective_at, window.starts_at)
        self.assertLessEqual(window.revenue, self.db.get(self.Ledger, 'l0').revenue)
        self.change.status = 'completed'
        self.db.get(self.Ledger, 'l0').status = 'posted'
        order = self.db.get(self.Order, 'o0')
        self.assertEqual(window, self.hooks['latest'](self.db, order))
        self.assertEqual(window, self.hooks['latest'](self.db, order))
        self.config.status = 'disabled'
        self.assertIsNone(self.hooks['latest'](self.db, order))

    def test_saas_missing_is_legacy_partial_mismatch_naive_future_and_early_reject(self):
        self.assertIsNone(self.record(dict(change={'ret': 0}, status={'ret': 0})))
        empty = dict(ret=0, change_id=None, direction=None, effective_at=None)
        self.assertIsNone(self.record(dict(change=empty, status=empty)))
        self.assertEqual([], self.hooks['materialize'](self.db, self.change, self.proof))
        cases = []
        for field, value in [('direction', 'original'), ('change_id', 'other'),
                             ('effective_at', self.now.replace(tzinfo=None).isoformat()),
                             ('effective_at', (self.now + timedelta(seconds=1)).isoformat()),
                             ('effective_at', (self.now - timedelta(days=1)).isoformat())]:
            bad = copy.deepcopy(self.responses)
            for response in bad.values():
                response[field] = value
            cases.append(bad)
        bad = copy.deepcopy(self.responses)
        del bad['status']['effective_at']
        cases.append(bad)
        bad = copy.deepcopy(self.responses)
        bad['status']['effective_at'] = (self.now-timedelta(seconds=1)).isoformat()
        cases.append(bad)
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(HTTPException) as error:
                self.record(bad)
            self.assertEqual(409, error.exception.status_code)

    def test_saas_first_timestamp_cannot_change_or_disappear(self):
        first = self.record()
        same = copy.deepcopy(self.responses)
        for response in same.values():
            response['effective_at'] = self.now.astimezone(timezone(timedelta(hours=8))).isoformat()
        self.assertEqual(first.id, self.record(same).id)
        bad = copy.deepcopy(self.responses)
        for response in bad.values():
            response['effective_at'] = (self.now - timedelta(seconds=1)).isoformat()
        for responses in (bad, dict(change={}, status={})):
            with self.assertRaises(HTTPException):
                self.record(responses)

    def test_saas_compensation_has_separate_direction_receipt(self):
        target = self.record()
        self.change.old_version_id, self.change.old_version_code = 'v1', 'one'
        original = copy.deepcopy(self.responses)
        for response in original.values():
            response['direction'] = 'original'
        original['status']['version'] = 'one'
        restored = self.record(original, direction='original')
        self.assertNotEqual(target.id, restored.id)
        self.assertEqual('original', restored.direction)
        self.assertEqual(2, len(self.db.scalars(select(self.hooks['Receipt'])).all()))

    def test_saas_missing_bound_receipt_and_wrong_subscription_fail_closed(self):
        self.record()
        self.revisions()
        self.proof['evidence_fingerprint'] = 'e'*64
        self.change.evidence_json = canonical(self.proof)
        with self.assertRaises(HTTPException):
            self.hooks['materialize'](self.db, self.change, self.proof)
        self.subscription.enterprise_id = 'foreign'
        with self.assertRaises(HTTPException):
            self.record()


if __name__ == '__main__':
    unittest.main()
