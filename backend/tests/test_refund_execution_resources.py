"""Actual DB entitlement overlays and isolated Redis/native gateway recovery."""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select

import test_subscription_terms as terms_fixture
import test_refund_segment_boundary as redis_fixture
from app.refund_planning import OriginalSegment, plan_segment


def aware_time(value):
    return datetime.fromisoformat(value)


class ExecutionResourcesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        terms_fixture.SubscriptionTermsTests.setUpClass.__func__(cls)
        cls.redis_owner = type('OwnedRedis', (), {})
        redis_fixture.BoundaryTests.setUpClass.__func__(cls.redis_owner)

    @classmethod
    def tearDownClass(cls):
        redis_fixture.BoundaryTests.tearDownClass.__func__(cls.redis_owner)
        terms_fixture.SubscriptionTermsTests.tearDownClass.__func__(cls)

    tearDown = terms_fixture.SubscriptionTermsTests.tearDown
    create = terms_fixture.SubscriptionTermsTests.create
    deliver = terms_fixture.SubscriptionTermsTests.deliver
    periods = terms_fixture.SubscriptionTermsTests.periods
    entitlement = terms_fixture.SubscriptionTermsTests.entitlement

    def setUp(self):
        terms_fixture.SubscriptionTermsTests.setUp(self)
        self.redis = self.redis_owner.client
        self.redis.flushdb()
        self.at = datetime.fromtimestamp(self.redis.time()[0], timezone.utc)
        self.m.now.return_value = self.at
        self.resources = self.m.refund_execution_resources
        self.source_patch = patch.object(self.m, 'apisix_policy_redis', return_value=self.redis)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        # redis.Redis.close releases a connection; this disposable pool reconnects.

    def fixture(self, *, months=2, amount='50.00', used=4, unlimited=False, persist_execution=True, cross=False):
        from app.refund_execution_resources import canonical, digest
        oid = self.create(months=months)
        self.deliver(oid, at=self.at)
        other = self.create()
        self.deliver(other, at=self.at)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            term = db.scalar(select(self.T).where(self.T.order_id == oid))
            combo = db.get(self.C, term.combination_id)
            if unlimited:
                for root in (order, db.get(self.m.Order, other)):
                    delivery = json.loads(root.delivery_snapshot_json)
                    delivery['quota']['monthly_quota'] = 0
                    root.delivery_snapshot_json = json.dumps(delivery)
                db.flush()
            quota = 0 if unlimited else 123
            economic = self.m.order_economics['economic_snapshot'](order)
            delivery = self.m.order_economics['delivery_snapshot'](order)
            segment = OriginalSegment(term.id, oid, Decimal(order.amount), 'api', quota, months,
                term.starts_at, term.ends_at, economic, delivery)
            plan = plan_segment(segment, amount, at=self.at, used_calls=used)
            plan['charge_ledger'] = dict(complete=True)
            db.add(self.m.ApiGatewayRoute(id='execution-route', product_id='product', version='v1',
                route_key='execution-route', upstream_url='https://owned.invalid', status='active'))
            db.flush()
            db.add(self.m.ApiCredential(id='execution-credential', route_id='execution-route', enterprise_id='buyer_tenant',
                order_id='', key_hash='f'*64, apisix_consumer_name='execution-test', status='active'))
            payment = self.m.Payment(id='execution-payment', order_id=oid, payment_no='execution-payment',
                amount=order.amount, status='paid', paid_at=self.at)
            db.add(payment)
            db.flush()
            refund = self.m.Refund(id='execution-refund', refund_no='execution-refund', order_id=oid,
                payment_id=payment.id, amount=Decimal(amount), status='approved', reason='Owned execution')
            db.add(refund)
            db.flush()
            envelope = dict(schema_version=1, refund_id=refund.id, order_id=oid, payment_id=payment.id,
                amount=amount, basis=plan['basis'], evidence_ids=['owned-charge-proof'], economic_plan=plan)
            encoded = canonical(envelope)
            import hashlib
            case = self.m.refund_workflow['Case'](refund_id=refund.id, requested_by_id='buyer', source='ordinary',
                version=2, plan_json=encoded, plan_hash=hashlib.sha256(encoded.encode()).hexdigest())
            db.add(case)
            run = self.redis.info('server')['run_id']
            self.redis.hset('market:apisix:charges:source', mapping={'run_id': run})
            db.add(self.m.api_charge_ledger['Cursor'](stream='market:apisix:charges:v1', last_id='0-0',
                redis_run_id=run, activated_at=self.at-timedelta(seconds=1)))
            db.commit()
            periods = self.hooks['quota_periods'](db, 'buyer_tenant', 'product')
            periods = [{k: v for k, v in p.items() if k not in ('version_id', 'version_code')} | {'route_key': 'execution-route'}
                       for p in periods]
            self.key = 'market:apisix:credential:execution-test'
            self.redis.hset(self.key, mapping=dict(mode='subscription', status='active', quota_scope=combo.id,
                subscription_periods=json.dumps(periods, separators=(',', ':'))))
            from app.api_charge_ledger import period_keys
            for p in periods:
                counter, coverage = period_keys(combo.id, p['period_key'])
                self.redis.set(counter, str(used if p['order_id'] == oid and p['start_at'] <= self.at.timestamp() else 0))
                self.redis.hset(coverage, mapping={'baseline': '0'})
            resource = self.resources['prepare'](db, order, refund, envelope)
            funding_payment = payment.id
            funding_order = order.id
            if cross:
                db.add(self.m.Order(id='upgrade-source', order_no='OWNED-UPGRADE-SOURCE', product_id='product',
                    buyer_enterprise_id='buyer_tenant', buyer_user_id='buyer', provider_enterprise_id=order.provider_enterprise_id,
                    product_name='Owned upgrade', buyer_name='Owned', amount=90, paid_amount=90, payment_status='paid',
                    business_type='version_change_payment', related_order_id=oid, economic_snapshot_json='{}'))
                db.flush()
                db.add(self.m.Payment(id='upgrade-payment', order_id='upgrade-source', payment_no='OWNED-UPGRADE-PAYMENT',
                    amount=90, status='paid', paid_at=self.at))
                db.flush()
                funding_payment, funding_order = 'upgrade-payment', 'upgrade-source'
            execution = self.m.refund_execution['Execution'](id='owned-execution', refund_id=refund.id,
                status='resources_pending', resume_status='resources_pending', plan_hash=case.plan_hash,
                resource_plan_json=canonical(resource), resource_plan_hash=digest(resource),
                funding_json=canonical([dict(payment_id=funding_payment, source_order_id=funding_order,
                    amount=amount, currency='CNY')]), requested_by='ops')
            if persist_execution:
                db.add(execution)
            db.commit()
            return oid, other, resource

    def apply(self, *, rollback=False, record_phase=True, execution_id='owned-execution'):
        with self.m.SessionLocal() as db:
            execution = db.get(self.m.refund_execution['Execution'], execution_id)
            proof = self.resources['apply'](db, execution)
            if record_phase:
                execution.status = 'resources_applied'
                execution.proof_json = json.dumps(proof, sort_keys=True, separators=(',', ':'))
            db.rollback() if rollback else db.commit()
            return proof

    def test_finite_scope_replay_and_external_success_db_rollback(self):
        oid, other, plan = self.fixture()
        before_other = [p for p in json.loads(plan['expected']['subscription_periods']) if p['order_id'] == other]
        first = self.apply(rollback=True)
        with self.m.SessionLocal() as db:
            self.assertEqual([], db.scalars(select(self.resources['Recovery'])).all())
        self.assertFalse(any(p['order_id'] == oid for p in self.periods()))
        self.assertTrue(any(p['order_id'] == other for p in self.periods()))
        self.assertEqual(first, self.apply())
        self.assertEqual(first, self.apply())
        actual = json.loads(self.redis.hget(self.key, 'subscription_periods'))
        self.assertEqual(before_other, [p for p in actual if p['order_id'] == other])
        rebuilt = [p for p in self.periods() if p['order_id'] == oid]
        self.assertEqual([p['monthly_quota'] for p in actual if p['order_id'] == oid], [p['monthly_quota'] for p in rebuilt])

    def test_pending_recovery_is_visible_and_money_failure_keeps_limits(self):
        oid, other, plan = self.fixture()
        self.apply(record_phase=False)
        restricted = self.periods()
        with self.m.SessionLocal() as db:
            execution = db.get(self.m.refund_execution['Execution'], 'owned-execution')
            execution.status, execution.resume_status = 'failed', 'funds_pending'
            recovery = db.scalar(select(self.resources['Recovery']))
            execution.proof_json = recovery.proof_json
            db.get(self.m.Refund, 'execution-refund').status = 'failed'
            db.commit()
        self.assertEqual(restricted, self.periods())
        self.assertIsNotNone(self.entitlement(self.at))

    def test_partial_completed_refund_uses_overlay_unknown_refund_denies(self):
        oid, other, plan = self.fixture()
        self.apply()
        with self.m.SessionLocal() as db:
            refund = db.get(self.m.Refund, 'execution-refund')
            refund.status = 'completed'
            order = db.get(self.m.Order, oid)
            order.payment_status = 'refunding'
            order.refunded_amount = refund.amount
            self.complete(db)
            db.commit()
        self.assertTrue(any(p['order_id'] == oid for p in self.periods()))
        self.assertTrue(any(p['order_id'] == other for p in self.periods()))
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, oid).refunded_amount += Decimal('0.01')
            db.commit()
        self.assertFalse(any(p['order_id'] == oid for p in self.periods()))

    def complete(self, db, payment_id='execution-payment'):
        from app.refund_execution_resources import canonical, digest
        execution = db.get(self.m.refund_execution['Execution'], 'owned-execution')
        refund = db.get(self.m.Refund, 'execution-refund')
        case = db.get(self.m.refund_workflow['Case'], refund.id)
        payment = db.get(self.m.Payment, payment_id)
        allocation = dict(payment_id=payment.id, source_order_id=payment.order_id, amount=str(refund.amount), currency='CNY')
        execution.status = 'completed'
        db.add(self.m.refund_execution['PaymentLeg'](refund_id=refund.id, payment_id=payment.id,
            source_order_id=payment.order_id, amount=refund.amount, currency='CNY', status='completed',
            receipt='simulated:' + refund.id + ':' + payment.id, completed_at=self.at))
        db.add(self.m.refund_execution['Completion'](refund_id=refund.id, execution_id=execution.id,
            plan_hash=case.plan_hash, proof_hash=digest(json.loads(execution.proof_json)),
            funding_hash=digest([allocation]), economic_plan_json=canonical(json.loads(case.plan_json)['economic_plan']),
            amount=refund.amount, completed_at=self.at))

    def test_cross_version_refund_exceeds_original_payment_keeps_remaining_entitlement(self):
        from app.refund_execution_resources import canonical, digest
        oid, other, plan = self.fixture(months=1, cross=True)
        self.apply()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            original = json.loads(order.economic_snapshot_json)
            target = dict(original, product_version_id='v2', version_code='v2')
            original.update(amount='10.00', unit_price='10.00')
            order.economic_snapshot_json = canonical(original)
            order.amount = order.paid_amount = order.unit_price_snapshot = Decimal(10)
            order.payment_status, order.refunded_amount = 'refunding', Decimal(50)
            db.get(self.m.Payment, 'execution-payment').amount = Decimal(10)
            term = db.scalar(select(self.T).where(self.T.order_id == oid))
            combo = db.get(self.C, term.combination_id)
            term.version_id = combo.version_id = 'v2'
            term.version_code = combo.version_code = 'v2'
            delivery = json.loads(order.delivery_snapshot_json)
            change = self.m.version_change_saga['VersionChangeRequest'](id='owned-upgrade', source_order_id=oid,
                enterprise_id='buyer_tenant', combination_id=combo.id, old_version_id='version', old_version_code='v1',
                target_version_id='v2', target_version_code='v2', kind='upgrade', status='completed',
                idempotency_key='owned-upgrade', request_hash='f'*64, quote_json='{"currency":"CNY","payment_amount":"90.00"}',
                prepared_json='{}', target_snapshots_json=canonical({term.id: dict(economic=target, delivery=delivery)}),
                funding_order_id='upgrade-source', resource_applied=True, created_by='ops')
            db.add(change)
            db.flush()
            db.add(self.m.subscription_terms['Revision'](id='owned-upgrade-revision', change_id=change.id,
                term_id=term.id, revision_number=1, version_id='v2', version_code='v2',
                economic_snapshot_json=canonical(target), delivery_snapshot_json=canonical(delivery)))
            db.add(self.m.version_change_ledger['Model'](change_id=change.id, request_change_id=change.id,
                source_order_id=oid, combination_id=combo.id, side='target', revenue=100,
                cost=Decimal(target['total_cost']), profit=100-Decimal(target['total_cost']), currency=target['currency'],
                original_version_id='version', target_version_id='v2', rule_json=canonical(target['settlement_rule']),
                effective_at=self.at, status='posted', payload_fingerprint='f'*64))
            db.get(self.m.Refund, 'execution-refund').status = 'completed'
            db.flush()
            self.assertEqual('v2', self.hooks['effective_snapshot'](db, order, term)['version_id'])
            self.complete(db, 'upgrade-payment')
            db.commit()
        self.assertTrue(any(p['order_id'] == oid for p in self.periods()))
        self.assertIsNotNone(self.entitlement(self.at))

    def test_zero_finite_never_unlimited_and_native_gateway_denies(self):
        oid, other, plan = self.fixture(months=1, amount='100.00', used=0)
        self.apply()
        rows = json.loads(self.redis.hget(self.key, 'subscription_periods'))
        self.assertFalse(any(p['order_id'] == oid for p in rows))
        self.assertTrue(any(p['order_id'] == other for p in rows))
        from test_subscription_quota import Gateway
        self.assertEqual(403, Gateway(self.redis, self.at.timestamp()).request(consumer='execution-test', route='execution-route'))

    def test_exhausted_finite_limit_preserves_used_counter_and_denies_next_call(self):
        oid, other, plan = self.fixture(months=1, amount='96.75', used=4)
        self.apply()
        rows = json.loads(self.redis.hget(self.key, 'subscription_periods'))
        owned = next(p for p in rows if p['order_id'] == oid)
        self.assertEqual(4, owned['monthly_quota'])
        from app.api_charge_ledger import period_keys
        counter, _ = period_keys(plan['combination_id'], owned['period_key'])
        self.assertEqual('4', self.redis.get(counter))
        from test_subscription_quota import Gateway
        self.assertEqual(429, Gateway(self.redis, self.at.timestamp()).request(consumer='execution-test', route='execution-route'))
        self.assertEqual('4', self.redis.get(counter))

    def test_atomic_concurrent_usage_rejects_without_policy_change(self):
        oid, other, plan = self.fixture()
        from app.api_charge_ledger import period_keys
        check = plan['checks'][0]
        counter, _coverage = period_keys(plan['combination_id'], check['period_key'])
        self.redis.set(counter, str(check['limit'] + 1))
        before = self.redis.hgetall(self.key)
        with self.assertRaises(Exception):
            self.apply()
        self.assertEqual(before, self.redis.hgetall(self.key))

    def test_native_gateway_debit_and_recovery_race_are_serialized(self):
        from app.refund_execution_resources import SCRIPT, PREFIX, EPOCH, canonical
        from app.api_charge_ledger import period_keys
        from test_subscription_quota import Gateway
        oid, other, plan = self.fixture(months=1, amount='100.00', used=0)
        keys = [plan['key'], PREFIX + plan['refund_id'], EPOCH]
        for row in plan['checks']:
            keys.extend(period_keys(plan['combination_id'], row['period_key']))
        payload = canonical(dict(plan, fields=sorted(plan['expected'])))
        barrier = threading.Barrier(2, timeout=5)
        def recover():
            barrier.wait()
            try:
                self.redis.eval(SCRIPT, len(keys), *keys, payload)
                return True
            except Exception:
                return False
        def debit():
            gateway = Gateway(self.redis, self.at.timestamp())
            barrier.wait()
            return gateway.request(consumer='execution-test', route='execution-route')
        with ThreadPoolExecutor(max_workers=2) as pool:
            recovery, charge = pool.submit(recover), pool.submit(debit)
            recovered, status = recovery.result(timeout=10), charge.result(timeout=10)
        if recovered:
            self.assertIn(status, (403, 503))
        else:
            self.assertEqual(200, status)
            self.assertEqual(plan['expected'], self.redis.hgetall(self.key))

    def test_replay_epoch_change_and_lost_counter_fail_closed(self):
        oid, other, plan = self.fixture()
        self.apply(rollback=True)
        self.redis.hset('market:apisix:charges:source', 'run_id', 'b'*40)
        with self.assertRaises(Exception):
            self.apply()
        self.redis.hset('market:apisix:charges:source', 'run_id', plan['run_id'])
        from app.api_charge_ledger import period_keys
        current = next(p for p in json.loads(plan['expected']['subscription_periods'])
                       if p['order_id'] == oid and p['start_at'] <= self.at.timestamp())
        counter, _ = period_keys(plan['combination_id'], current['period_key'])
        self.redis.delete(counter)
        with self.assertRaises(Exception):
            self.apply()

    def test_time_tail_preserves_unrelated_renewal(self):
        oid, other, plan = self.fixture(months=1, amount='50.00', used=0, unlimited=True)
        self.apply()
        self.assertEqual(plan['entitlement_periods'][0]['end_at'], self.entitlement(self.at)['period_ends_at'].timestamp())
        self.assertTrue(any(p['order_id'] == other for p in self.periods()))

    def saas_fixture(self, *, retain_future=False):
        from app.refund_execution_resources import canonical, digest
        import hashlib
        oid, other, _plan = self.fixture(months=1, amount='50.00', used=0, unlimited=True, persist_execution=False)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            term = db.scalar(select(self.T).where(self.T.order_id == oid))
            combo = db.get(self.C, term.combination_id)
            combo.delivery_method = order.delivery_method_snapshot = 'tenant_access'
            db.add(self.m.SaaSProductVersion(id='owned-saas-version', product_id='product', version_code='v1',
                name='Owned SaaS', status='active'))
            db.flush()
            delivery = json.loads(order.delivery_snapshot_json)
            delivery['method'] = 'tenant_access'
            order.delivery_snapshot_json = json.dumps(delivery)
            if not retain_future:
                db.scalar(select(self.T).where(self.T.order_id == other)).status = 'revoked'
            db.add(self.m.SaaSSubscription(id='owned-sub', enterprise_id='buyer_tenant', product_id='product',
                version_id='owned-saas-version', status='active', external_tenant_id='owned-tenant',
                starts_at=term.starts_at, expires_at=term.ends_at))
            db.add(self.m.SaaSIntegrationConfig(id='owned-config', product_id='product',
                base_url='https://owned.invalid', status='active'))
            refund = db.get(self.m.Refund, 'execution-refund')
            refund.amount = Decimal('33.33')
            economic = plan_segment(OriginalSegment(term.id, oid, Decimal(order.amount), 'tenant_access', 0, 1,
                term.starts_at, term.ends_at, self.m.order_economics['economic_snapshot'](order), delivery),
                '33.33', at=self.at)
            case = db.get(self.m.refund_workflow['Case'], refund.id)
            envelope = json.loads(case.plan_json)
            envelope.update(amount='33.33', basis='time', economic_plan=economic)
            case.plan_json = canonical(envelope)
            case.plan_hash = hashlib.sha256(case.plan_json.encode()).hexdigest()
            db.commit()
            self.saas_state = dict(ret=0, tenant_id='owned-tenant', enterprise_id='buyer_tenant',
                product_id='product', subscription_id='owned-sub', expires_at=term.ends_at.isoformat(),
                refund_shorten_protocol='refund_shorten_v1', refund_shorten_supported=True)
            self.renew_calls = []
            def call(config, operation, payload):
                if operation == 'RENEW':
                    self.assertEqual('refund_shorten', payload['action'])
                    self.assertEqual(self.saas_state['expires_at'], payload['expected_expires_at'])
                    self.assertLess(datetime.fromisoformat(payload['expires_at']), datetime.fromisoformat(self.saas_state['expires_at']))
                    self.renew_calls.append(payload)
                    self.saas_state.update(expires_at=payload['expires_at'], refund_id=payload['refund_id'])
                return dict(self.saas_state)
            self.saas_patch = patch.object(self.m, 'saas_call', side_effect=call)
            self.saas_patch.start()
            self.addCleanup(self.saas_patch.stop)
            resource = self.resources['prepare'](db, order, refund, envelope)
            execution = self.m.refund_execution['Execution'](id='owned-execution', refund_id=refund.id,
                status='resources_pending', resume_status='resources_pending', plan_hash=case.plan_hash,
                resource_plan_json=canonical(resource), resource_plan_hash=digest(resource), requested_by='ops',
                funding_json=canonical([dict(payment_id='execution-payment', source_order_id=oid, amount='33.33', currency='CNY')]))
            db.add(execution)
            db.commit()
            return oid, resource

    def test_saas_exact_microseconds_replay_and_sso_cutoff(self):
        oid, plan = self.saas_fixture()
        cutoff = datetime.fromisoformat(plan['expires_at'])
        self.assertNotEqual(0, cutoff.microsecond)
        first = self.apply(rollback=True)
        self.assertEqual(first, self.apply())
        self.assertEqual(1, len(self.renew_calls))
        self.assertIsNotNone(self.entitlement(cutoff - timedelta(microseconds=1)))
        self.assertIsNone(self.entitlement(cutoff))

    def test_saas_two_partial_refunds_use_cumulative_original_denominator(self):
        from app.refund_execution_resources import canonical, digest
        import hashlib
        oid, first = self.saas_fixture()
        first_proof = self.apply(record_phase=False)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            original_term = db.scalar(select(self.T).where(self.T.order_id == oid))
            original_end = original_term.ends_at
            refund = db.get(self.m.Refund, 'execution-refund')
            first_economic = json.loads(db.get(self.m.refund_workflow['Case'], refund.id).plan_json)['economic_plan']
            delivery = self.m.order_economics['delivery_snapshot'](order)
            economic = plan_segment(OriginalSegment(original_term.id, oid, Decimal(order.amount), 'tenant_access', 0, 1,
                original_term.starts_at, original_end, self.m.order_economics['economic_snapshot'](order), delivery),
                '20.00', at=self.at, prior_amount='33.33', prior_recovery=first_economic['cumulative_recovery'])
            second_refund = self.m.Refund(id='second-refund', refund_no='second-refund', order_id=oid,
                payment_id='execution-payment', amount=20, status='approved', reason='Owned second refund')
            db.add(second_refund)
            db.flush()
            envelope = dict(schema_version=1, refund_id='second-refund', order_id=oid, payment_id='execution-payment',
                amount='20.00', basis='time', evidence_ids=['owned-second-proof'], economic_plan=economic)
            encoded = canonical(envelope)
            case = self.m.refund_workflow['Case'](refund_id='second-refund', requested_by_id='buyer', source='ordinary',
                version=2, plan_json=encoded, plan_hash=hashlib.sha256(encoded.encode()).hexdigest())
            db.add(case)
            db.commit()
            second = self.resources['prepare'](db, order, second_refund, envelope)
            self.assertEqual(first['expires_at'], second['expected_expires_at'])
            db.add(self.m.refund_execution['Execution'](id='second-execution', refund_id='second-refund',
                status='resources_pending', resume_status='resources_pending', plan_hash=case.plan_hash,
                resource_plan_json=canonical(second), resource_plan_hash=digest(second), funding_json='[]', requested_by='ops'))
            db.commit()
        proof = self.apply(execution_id='second-execution')
        self.assertEqual(proof, self.apply(execution_id='second-execution'))
        self.assertEqual(first_proof, self.apply())
        self.assertEqual(2, len(self.renew_calls))
        self.assertLess(aware_time(second['expires_at']), aware_time(first['expires_at']))
        self.assertEqual(aware_time(second['expires_at']), self.entitlement(self.at)['expires_at'])
        with self.m.SessionLocal() as db:
            term = db.scalar(select(self.T).where(self.T.order_id == oid))
            self.assertEqual(original_end, term.ends_at)
            self.assertEqual(2, len(db.scalars(select(self.resources['Recovery'])).all()))

    def test_older_api_recovery_proof_resumes_after_stricter_same_term_refund(self):
        from app.refund_execution_resources import canonical, digest
        import hashlib
        oid, other, first = self.fixture(months=1)
        first_proof = self.apply(record_phase=False)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            term = db.scalar(select(self.T).where(self.T.order_id == oid))
            previous = json.loads(db.get(self.m.refund_workflow['Case'], 'execution-refund').plan_json)['economic_plan']
            economic = plan_segment(OriginalSegment(term.id, oid, Decimal(order.amount), 'api', 123, 1,
                term.starts_at, term.ends_at, self.m.order_economics['economic_snapshot'](order),
                self.m.order_economics['delivery_snapshot'](order)), '10.00', at=self.at, used_calls=4,
                prior_amount='50.00', prior_recovery=previous['cumulative_recovery'])
            economic['charge_ledger'] = {'complete': True}
            refund = self.m.Refund(id='later-refund', refund_no='later-refund', order_id=oid,
                payment_id='execution-payment', amount=10, status='approved', reason='Owned later refund')
            db.add(refund)
            db.flush()
            envelope = dict(schema_version=1, refund_id=refund.id, order_id=oid, payment_id='execution-payment',
                amount='10.00', basis='calls', evidence_ids=['owned-later-proof'], economic_plan=economic)
            encoded = canonical(envelope)
            case = self.m.refund_workflow['Case'](refund_id=refund.id, requested_by_id='buyer', source='ordinary',
                version=2, plan_json=encoded, plan_hash=hashlib.sha256(encoded.encode()).hexdigest())
            db.add(case)
            db.commit()
            later = self.resources['prepare'](db, order, refund, envelope)
            db.add(self.m.refund_execution['Execution'](id='later-execution', refund_id=refund.id,
                status='resources_pending', resume_status='resources_pending', plan_hash=case.plan_hash,
                resource_plan_json=canonical(later), resource_plan_hash=digest(later), funding_json='[]', requested_by='ops'))
            db.commit()
        self.apply(execution_id='later-execution')
        actual = self.redis.hgetall(self.key)
        with patch.object(self.redis, 'eval', side_effect=AssertionError('Durable recovery replay must not write Redis')):
            self.assertEqual(first_proof, self.apply())
        self.assertEqual(actual, self.redis.hgetall(self.key))
        self.assertEqual(later['target'], actual)

    def test_saas_middle_term_fails_closed(self):
        with self.assertRaises(self.m.HTTPException):
            self.saas_fixture(retain_future=True)
        self.assertEqual([], self.renew_calls)

    def test_saas_missing_capability_and_stale_expiry_fail_closed(self):
        oid, plan = self.saas_fixture()
        self.saas_state['refund_shorten_supported'] = False
        with self.assertRaises(self.m.HTTPException):
            self.apply()
        self.saas_state['refund_shorten_supported'] = True
        self.saas_state['expires_at'] = (datetime.fromisoformat(plan['expected_expires_at']) - timedelta(seconds=1)).isoformat()
        with self.assertRaises(self.m.HTTPException):
            self.apply()
        self.assertEqual([], self.renew_calls)

    def test_manual_and_unbound_execution_are_not_automatic(self):
        oid, other, plan = self.fixture()
        with self.m.SessionLocal() as db:
            execution = db.get(self.m.refund_execution['Execution'], 'owned-execution')
            execution.status = 'prepared'
            db.commit()
        with self.assertRaises(self.m.HTTPException):
            self.apply()

    def test_frozen_manual_envelope_is_explicitly_unsupported(self):
        from app.refund_execution_resources import canonical
        import hashlib
        oid, other, plan = self.fixture(persist_execution=False)
        with self.m.SessionLocal() as db:
            case = db.get(self.m.refund_workflow['Case'], 'execution-refund')
            envelope = json.loads(case.plan_json)
            envelope['basis'] = 'manual'
            case.plan_json = canonical(envelope)
            case.plan_hash = hashlib.sha256(case.plan_json.encode()).hexdigest()
            db.commit()
            with self.assertRaises(self.m.HTTPException):
                self.resources['prepare'](db, db.get(self.m.Order, oid), db.get(self.m.Refund, 'execution-refund'), envelope)

    def test_replay_preserves_later_unrelated_metadata_update(self):
        oid, other, plan = self.fixture()
        first = self.apply()
        rows = json.loads(self.redis.hget(self.key, 'subscription_periods'))
        for row in rows:
            if row['order_id'] == other:
                row['monthly_quota'] = 77
        self.redis.hset(self.key, 'subscription_periods', json.dumps(rows, separators=(',', ':')))
        self.assertEqual(first, self.apply())
        self.assertEqual(rows, json.loads(self.redis.hget(self.key, 'subscription_periods')))

    def check_api_time_rounding(self, boundary):
        import hashlib
        from app.refund_execution_resources import canonical, digest
        oid, other, _ = self.fixture(months=1, amount='50.00', used=0, unlimited=True, persist_execution=False)
        rows = json.loads(self.redis.hget(self.key, 'subscription_periods'))
        owned = next(row for row in rows if row['order_id'] == oid)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        if boundary == 'subsecond':
            exact = self.at + timedelta(hours=1, microseconds=1)
            expected = self.at + timedelta(hours=1, seconds=1)
        elif boundary == 'second':
            exact = expected = self.at + timedelta(hours=1)
        elif boundary == 'day':
            exact = self.at.replace(hour=23, minute=59, second=59, microsecond=999999)
            expected = exact + timedelta(microseconds=1)
        elif boundary == 'month':
            year, month = (self.at.year + 1, 1) if self.at.month == 12 else (self.at.year, self.at.month + 1)
            expected = datetime(year, month, 1, tzinfo=timezone.utc)
            exact = expected - timedelta(microseconds=1)
        else:
            owned['end_at'] -= 1
            self.redis.hset(self.key, 'subscription_periods', json.dumps(rows, separators=(',', ':')))
            expected = epoch + timedelta(seconds=owned['end_at'])
            exact = expected + timedelta(microseconds=1)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            refund = db.get(self.m.Refund, 'execution-refund')
            case = db.get(self.m.refund_workflow['Case'], refund.id)
            envelope = json.loads(case.plan_json)
            # Exercise the resource adapter's frozen-cutoff contract independently
            # of the planner's monetary arithmetic.
            envelope['economic_plan']['effective_ends_at'] = exact.isoformat()
            financial_keys = ('refund_amount', 'cumulative_refund_amount', 'original_amount')
            financial = {key: envelope['economic_plan'][key] for key in financial_keys}
            case.plan_json = canonical(envelope)
            case.plan_hash = hashlib.sha256(case.plan_json.encode()).hexdigest()
            db.commit()
            plan = self.resources['prepare'](db, order, refund, envelope)
            self.assertEqual(exact.isoformat(), plan['effective_ends_at'])
            self.assertEqual(expected.isoformat(), plan['gateway_effective_ends_at'])
            self.assertEqual('ceil_to_second_clamped_to_native_period_end', plan['rounding_policy'])
            target = json.loads(plan['target']['subscription_periods'])
            owned_target = [row for row in target if row['order_id'] == oid]
            self.assertEqual((expected - epoch) // timedelta(seconds=1), max(row['end_at'] for row in owned_target))
            self.assertTrue(all(row['end_at'] <= owned['end_at'] for row in owned_target))
            self.assertEqual([row for row in rows if row['order_id'] == other],
                             [row for row in target if row['order_id'] == other])
            self.assertEqual(financial, {key: json.loads(case.plan_json)['economic_plan'][key] for key in financial_keys})
            db.add(self.m.refund_execution['Execution'](id='owned-execution', refund_id=refund.id,
                status='resources_pending', resume_status='resources_pending', plan_hash=case.plan_hash,
                resource_plan_json=canonical(plan), resource_plan_hash=digest(plan), funding_json='[]', requested_by='ops'))
            db.commit()
        self.apply()
        self.assertEqual(expected, self.entitlement(self.at)['period_ends_at'])
        self.assertEqual(target, json.loads(self.redis.hget(self.key, 'subscription_periods')))

    def test_api_time_rounding_subsecond(self):
        self.check_api_time_rounding('subsecond')

    def test_api_time_rounding_exact_second(self):
        self.check_api_time_rounding('second')

    def test_api_time_rounding_cross_day(self):
        self.check_api_time_rounding('day')

    def test_api_time_rounding_month_end(self):
        self.check_api_time_rounding('month')

    def test_api_time_rounding_original_end_clamp(self):
        self.check_api_time_rounding('clamp')


if __name__ == '__main__':
    unittest.main()
