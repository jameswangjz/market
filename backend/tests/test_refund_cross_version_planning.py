"""HTTP bridge tests with real store rows; latest/charge/funding proofs are stubs.

Funding validate/reserve/freeze are stubbed to isolate planning from the funding
graph. These do not establish Redis/gateway or full store/funding continuity.
"""
from datetime import timedelta
import hashlib
import importlib
import json
import unittest
from unittest.mock import Mock, patch
from sqlalchemy import select

import test_refund_planning as fixture


class CrossVersionPlanningTests(unittest.TestCase):
    saas = False
    @classmethod
    def setUpClass(cls):
        fixture.RefundPlanningHTTPTests.setUpClass.__func__(cls)
        for name in ('refund_segment_store', 'refund_funding_store'):
            if not hasattr(cls.m, name):
                importlib.import_module(cls.m.__package__ + '.' + name).install(vars(cls.m))
        cls.m.Base.metadata.create_all(cls.m.engine)

    tearDownClass = classmethod(fixture.RefundPlanningHTTPTests.tearDownClass.__func__)
    tearDown = fixture.RefundPlanningHTTPTests.tearDown
    request = fixture.RefundPlanningHTTPTests.request
    create_order = fixture.RefundPlanningHTTPTests.create_order
    create = fixture.RefundPlanningHTTPTests.create
    review = fixture.RefundPlanningHTTPTests.review
    monthly = fixture.RefundPlanningHTTPTests.monthly
    quote = fixture.RefundPlanningHTTPTests.quote

    def setUp(self):
        fixture.RefundPlanningHTTPTests.setUp(self)
        self.client._transport.raise_server_exceptions = True
        self.monthly('tenant_access' if self.saas else 'api')
        store = self.m.refund_segment_store
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            order.delivery_status = 'accepted'
            term = db.get(self.m.subscription_terms['Term'], 'refund-segment')
            db.add(self.m.version_change_saga['VersionChangeRequest'](id='change',
                source_order_id=order.id, enterprise_id=order.buyer_enterprise_id,
                combination_id=term.combination_id, target_version_id='version', target_version_code='v1',
                old_version_id='version', old_version_code='v1', kind='no_payment', status='completed',
                idempotency_key='fixture-change', request_hash='a' * 64, quote_json='{}',
                prepared_json='{}', target_snapshots_json='{}', created_by='ops', resource_applied=True))
            db.flush()
            revision = self.m.subscription_terms['Revision'](change_id='change', term_id=term.id,
                revision_number=1, economic_snapshot_json=order.economic_snapshot_json,
                delivery_snapshot_json=order.delivery_snapshot_json, version_id='version', version_code='v1')
            db.add(revision)
            db.flush()
            db.add(self.m.ApiGatewayRoute(id='route', product_id=order.product_id,
                route_key='fixture-route', version='v1', upstream_url='https://example.test'))
            db.flush()
            db.add(self.m.ApiCredential(id='credential', route_id='route',
                enterprise_id=order.buyer_enterprise_id, key_hash='fixture-key-hash', order_id=order.id))
            db.flush()
            self.periods = [dict(period_key='refund-combo:1:0', segment_id=term.id,
                order_id=order.id, used_at_switch=30, start_at=int(term.starts_at.timestamp()),
                end_at=int(term.ends_at.timestamp()))]
            raw = json.dumps(dict(scope='refund-combo', source_run_id='a' * 40,
                boundary_id='1-2', change_id='change', periods=self.periods))
            if self.saas:
                db.get(self.m.Payment, 'paid-source').paid_at = self.at - timedelta(days=10)
                db.add(self.m.SaaSProductVersion(id='version', product_id=order.product_id,
                    version_code='v1', name='SaaS fixture', status='active'))
                db.flush()
                db.add(self.m.SaaSSubscription(id='saas-sub', enterprise_id=order.buyer_enterprise_id,
                    product_id=order.product_id, version_id='version', external_tenant_id='tenant', status='active'))
                db.flush()
                change = db.get(self.m.version_change_saga['VersionChangeRequest'], 'change')
                change.resource_plan_json = json.dumps(dict(kind='saas', subscription_id='saas-sub', tenant_id='tenant'))
                raw = json.dumps(dict(kind='saas', scope='refund-combo', direction='target', change_id='change',
                    subscription_id='saas-sub', tenant_id='tenant', enterprise_id=order.buyer_enterprise_id,
                    product_id=order.product_id, version_id='version', version_code='v1',
                    effective_at=(self.at - timedelta(days=1)).isoformat()))
            receipt = store['Receipt'](id='receipt', change_id='change', credential_id='credential',
                direction='target', receipt_json=raw, receipt_hash=hashlib.sha256(raw.encode()).hexdigest(),
                cutover_at=self.at - timedelta(days=1), source_run_id='a' * 40,
                boundary_id='1-2', target_fingerprint='a' * 64)
            if self.saas:
                receipt.kind, receipt.subscription_id, receipt.credential_id = 'saas', 'saas-sub', None
                receipt.source_run_id = receipt.boundary_id = receipt.target_fingerprint = None
            db.add(receipt)
            self.window = store['Window'](id='window', order_id=order.id, term_id=term.id,
                change_id='change', receipt_id=receipt.id, revision_id=revision.id,
                revenue=70, cost=35, currency='CNY', starts_at=self.at - timedelta(days=1),
                ends_at=term.ends_at, available_quota=None if self.saas else 70, economic_snapshot_json=order.economic_snapshot_json,
                delivery_snapshot_json=order.delivery_snapshot_json, evidence_json='{}', payload_hash='b' * 64)
            db.add(self.window)
            db.commit()
            db.refresh(self.window)
            db.expunge(self.window)
        self.used = 7
        self.charges = Mock(side_effect=self.charge)
        self.validate = Mock(return_value=True)
        self.reserve = Mock(return_value=None)
        self.freeze = Mock(return_value=[dict(payment_id='paid-source', source_order_id=self.order_id,
            amount='20.00', currency='CNY', window_id='window', window_hash='b' * 64)])
        patches = [(store, dict(latest=lambda db, order: self.window)),
                (self.m.api_charge_ledger, dict(window_summary=self.charges))]
        if not self.saas:
            patches.append((self.m.refund_funding_store, dict(validate=self.validate, reserve=self.reserve,
                    freeze=self.freeze, eligible=lambda *a: False)))
        for target, values in patches:
            context = patch.dict(target, values)
            context.start()
            self.addCleanup(context.stop)

    def charge(self, db, **kw):
        return self.module.ChargeSummary(kw['order_id'], kw['segment_id'], kw['start'], kw['end'],
            kw['as_of'], self.used, True, 'stream-policy', ('charge-boundary:1-2', 'charge-cursor:1-10'))

    def test_quote_and_approval_freeze_window_only_usage(self):
        result = self.quote()
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['item']['charged_calls'], 7)
        args = self.charges.call_args.kwargs
        self.assertEqual((args['segment_id'], args['start'], args['boundary_id']),
                         ('refund-segment', self.window.starts_at, '1-2'))
        self.assertEqual(args['periods'], self.periods)
        row = self.create()
        self.reserve.assert_called_once()
        self.assertEqual(self.reserve.call_args.args[2].id, row['id'])
        approved = self.review(row, decision='approve')
        self.assertEqual(approved['status'], 'approved')
        self.assertFalse(approved['execution_available'])
        self.assertEqual(self.freeze.call_count, 2)
        for call in self.freeze.call_args_list:
            self.assertEqual((call.args[1].id, call.args[2].id), (self.order_id, row['id']))
        self.assertNotIn('cost', json.dumps(result))
        self.assertNotIn('economic_snapshot', json.dumps(approved))
        with self.m.SessionLocal() as db:
            plan = json.loads(db.get(self.Case, row['id']).plan_json)['economic_plan']
            self.assertEqual(plan['segment_id'], 'window')
            self.assertEqual(plan['window_hash'], 'b' * 64)
            self.assertEqual(len(plan['funding_allocations']), 1)

    def test_missing_window_never_falls_back(self):
        with patch.dict(self.m.refund_segment_store, latest=lambda *a: None):
            self.assertEqual(self.quote()['status'], 'unavailable')

    def test_missing_funding_blocks_approval(self):
        row = self.create()
        self.freeze.return_value = None
        self.review(row, decision='approve', expected=409)

    def test_future_window_requires_confirmed_zero(self):
        self.window.starts_at = self.at + timedelta(days=1)
        self.used = 0
        self.assertEqual(self.quote()['status'], 'available')
        args = self.charges.call_args.kwargs
        self.assertEqual(args['end'], args['start'])
        self.used = 1
        self.assertEqual(self.quote()['status'], 'unavailable')

    def test_prior_other_window_and_malformed_hash_reject(self):
        row = self.create()
        self.review(row, decision='approve')
        with self.m.SessionLocal() as db:
            case = db.get(self.Case, row['id'])
            raw = json.loads(case.plan_json)
            raw['economic_plan']['window_hash'] = 'c' * 64
            case.plan_json = json.dumps(raw)
            case.plan_hash = hashlib.sha256(case.plan_json.encode()).hexdigest()
            db.commit()
        self.assertEqual(self.quote()['status'], 'unavailable')
        with self.m.SessionLocal() as db:
            db.get(self.Case, row['id']).plan_hash = 'bad'
            db.commit()
        self.assertEqual(self.quote()['status'], 'unavailable')

    def test_version_change_refund_is_not_reserved_twice(self):
        row = self.create('60')
        controlled = self.m.refund_workflow['controlled_source']
        with patch.dict(self.m.refund_workflow, controlled_source=lambda db, refund, case:
                        'version_change' if refund.id == row['id'] else controlled(db, refund, case)):
            self.assertEqual(self.quote('60')['status'], 'available')


class SaaSCrossVersionPlanningTests(unittest.TestCase):
    """Real Receipt/Window and FundingStore; latest verification is the sole stub.

    Provider CHANGE/STATUS proof validation belongs to SegmentStore tests. These
    tests exercise its verified Window contract without Redis or provider I/O.
    """
    saas = True
    setUpClass = classmethod(CrossVersionPlanningTests.setUpClass.__func__)
    tearDownClass = classmethod(CrossVersionPlanningTests.tearDownClass.__func__)
    tearDown = CrossVersionPlanningTests.tearDown
    setUp = CrossVersionPlanningTests.setUp
    monthly = CrossVersionPlanningTests.monthly
    request = CrossVersionPlanningTests.request
    create_order = CrossVersionPlanningTests.create_order
    create = CrossVersionPlanningTests.create
    review = CrossVersionPlanningTests.review
    quote = CrossVersionPlanningTests.quote
    charge = CrossVersionPlanningTests.charge
    manual = fixture.RefundPlanningHTTPTests.manual

    def test_saas_time_plan_freezes_real_funding_without_redis_watermark(self):
        with patch.object(self.m, 'apisix_policy_redis', side_effect=AssertionError('Redis forbidden')):
            quote = self.quote()
            self.assertEqual(quote['status'], 'available')
            self.assertEqual(quote['item']['basis'], 'time')
            self.assertIsNone(quote['item']['charge_policy'])
            self.charges.assert_not_called()
            row = self.create()
            approved = self.review(row, decision='approve')
        self.assertFalse(approved['execution_available'])
        self.assertNotIn('economic_snapshot', json.dumps(approved))
        Allocation = self.m.refund_funding_store['FundingAllocation']
        with self.m.SessionLocal() as db:
            allocations = list(db.scalars(select(Allocation)))
            self.assertEqual(len(allocations), 1)
            self.assertEqual(allocations[0].payment_id, 'paid-source')
            case = db.get(self.Case, row['id'])
            plan = json.loads(case.plan_json)['economic_plan']
            self.assertEqual(plan['basis'], 'time')
            self.assertIsNone(plan['charge_ledger'])
            self.assertEqual(plan['funding_allocations'][0]['window_hash'], self.window.payload_hash)
            self.assertEqual(hashlib.sha256(case.plan_json.encode()).hexdigest(), case.plan_hash)

    def test_saas_wrong_subscription_tenant_effective_time_or_change_denies_quote(self):
        Receipt = self.m.refund_segment_store['Receipt']
        for field, value in [('subscription_id', 'wrong'), ('tenant_id', 'wrong'), ('change_id', 'wrong'),
                ('effective_at', self.at.isoformat()), ('effective_at', self.at.replace(tzinfo=None).isoformat()),
                ('version_code', 'wrong'), ('enterprise_id', 'wrong')]:
            with self.subTest(field=field, value=value), self.m.SessionLocal() as db:
                receipt = db.get(Receipt, 'receipt')
                # Alter a detached read result, preserving immutable DB evidence.
                db.expunge(receipt)
                raw = json.loads(receipt.receipt_json)
                raw[field] = value
                receipt.receipt_json = json.dumps(raw)
                receipt.receipt_hash = hashlib.sha256(receipt.receipt_json.encode()).hexdigest()
                original = self.m.SessionLocal.class_.get
                def read(session, model, identity, *args, **kwargs):
                    return receipt if model is Receipt and identity == 'receipt' else original(
                        session, model, identity, *args, **kwargs)
                with patch.object(self.m.SessionLocal.class_, 'get', read):
                    self.assertEqual(self.quote()['status'], 'unavailable')
        self.charges.assert_not_called()

    def test_saas_quota_or_missing_verified_window_requires_manual(self):
        self.window.available_quota = 70
        self.assertEqual(self.quote()['status'], 'unavailable')
        self.window.available_quota = None
        with patch.dict(self.m.refund_segment_store, latest=lambda *args: None):
            self.assertEqual(self.quote()['status'], 'unavailable')
            row = self.create()
            self.assertTrue(row['planning']['manual_assessment_allowed'])
            self.review(row, decision='approve', expected=409)
            assessed = self.manual(row)
            approved = self.review(assessed, decision='approve', idempotency_key='approve-saas-manual')
            self.assertFalse(approved['execution_available'])
            with self.m.SessionLocal() as db:
                plan = json.loads(db.get(self.Case, row['id']).plan_json)['economic_plan']
                self.assertEqual(plan['basis'], 'manual')
                self.assertEqual(plan['cost_basis_status'], 'unavailable')
                self.assertEqual(plan['resource_recovery_status'], 'unavailable')
        self.charges.assert_not_called()
