"""Activation faults must preserve gateway policy, counters, and manual gates."""
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from sqlalchemy import event, select

import test_api_auto_delivery as api_fixture
import test_order_service_lifecycle as lifecycle_fixture
import test_saas_auto_delivery as saas_fixture


class ApiActivationFaultTests(unittest.TestCase):
    Fixture = api_fixture.ApiAutoDeliveryTests
    Lifecycle = lifecycle_fixture.OrderServiceLifecycleTests
    setUpClass = classmethod(Fixture.setUpClass.__func__)
    tearDownClass = classmethod(Fixture.tearDownClass.__func__)
    setUp = Lifecycle.setUp
    tearDown = Fixture.tearDown
    request = Fixture.request
    task_id = Fixture.task_id
    create = Fixture.create
    step = Fixture.step
    admin_request = Fixture.admin_request
    redis_eval = Fixture.redis_eval
    pay = Lifecycle.pay
    finish = Lifecycle.finish
    due = Lifecycle.due
    due_for = Lifecycle.due_for
    facts = Lifecycle.facts
    renewal = Lifecycle.renewal
    assert_waiting = Lifecycle.assert_waiting

    def prepared_activation(self):
        # Redis HGETALL returns a detached mapping, not the fixture's backing dict.
        self.redis.hgetall.side_effect = lambda key: copy.deepcopy(self.redis_values.get(key, {}))
        _, renewal, start = self.renewal()
        self.clock.return_value = start
        self.due_for(renewal)
        self.assert_waiting(renewal, 'activation_verified')
        with self.m.SessionLocal() as db:
            existing = db.scalar(select(self.m.ApiCredential).where(
                self.m.ApiCredential.enterprise_id == 'buyer_tenant', self.m.ApiCredential.route_id == 'route'))
            consumer = existing.apisix_consumer_name
            db.add(self.m.ApiCredential(id='second-credential', route_id='route',
                enterprise_id='buyer_tenant', key_hash='second-key-hash', status='active',
                apisix_consumer_name='second-consumer', daily_quota=12, monthly_quota=300,
                rate_limit_per_minute=30))
            db.commit()
        first_key = 'market:apisix:credential:' + consumer
        second_key = 'market:apisix:credential:second-consumer'
        self.redis_values[first_key]['fixture_marker'] = 'first-consumer-original'
        self.redis_values[second_key] = dict(self.redis_values[first_key], fixture_marker='second-consumer-original')
        # Keep usage in distinct keys so metadata restoration cannot reset it.
        self.counters = {
            f'market:quota:{consumer}:day:20261119': '17',
            f'market:quota:{consumer}:month:202611': '41',
            'market:quota:second-consumer:total': '63',
        }
        self.redis_values.update(self.counters)
        self.metadata = {key: copy.deepcopy(self.redis_values[key]) for key in (first_key, second_key)}
        self.baseline_facts = self.facts(renewal)
        return renewal

    def assert_restored(self):
        for key, expected in self.metadata.items():
            self.assertEqual(self.redis_values[key], expected, key)
        for key, expected in self.counters.items():
            self.assertEqual(self.redis_values[key], expected, key)

    def publishing_failure(self, failure):
        renewal = self.prepared_activation()
        original = self.m.order_sla_contracts['record_verified_delivery']
        published = []

        def record(db, order, task):
            if order.id == renewal:
                for key, before in self.metadata.items():
                    self.assertNotEqual(self.redis_values[key]['subscription_periods'], before['subscription_periods'])
                published.append(order.id)
                if failure == 'sla':
                    raise RuntimeError('injected SLA recording failure')
            return original(db, order, task)

        audit_original = self.m.audit

        def audit(*args, **kwargs):
            if failure == 'audit' and len(args) > 2 and args[2] == 'verify_order_sla_delivery':
                raise RuntimeError('injected post-publication SLA audit failure')
            return audit_original(*args, **kwargs)

        with patch.dict(self.m.order_sla_contracts, {'record_verified_delivery': record}), \
                patch.object(self.m, 'audit', side_effect=audit):
            self.due_for(renewal)
        self.assertEqual(published, [renewal])
        self.assert_waiting(renewal, 'retrying', 'exception')
        self.assert_restored()
        self.assertEqual(self.facts(renewal), self.baseline_facts)

    def test_successful_redis_publish_then_sla_failure_restores_each_policy(self):
        self.publishing_failure('sla')

    def test_successful_redis_publish_then_audit_failure_restores_each_policy(self):
        self.publishing_failure('audit')

    def root_failure(self, fail_commit):
        renewal = self.prepared_activation()
        with self.m.SessionLocal() as db:
            self.assertTrue(self.lifecycle['process_due'](db, db.get(self.m.User, 'ops')))
            for key, before in self.metadata.items():
                self.assertNotEqual(self.redis_values[key]['subscription_periods'], before['subscription_periods'])
            self.assertEqual(db.get(self.State, renewal).status, 'active')
            if fail_commit:
                def reject_commit(session):
                    raise RuntimeError('injected root commit failure')

                event.listen(db, 'before_commit', reject_commit)
                try:
                    with self.assertRaisesRegex(RuntimeError, 'injected root commit failure'):
                        db.commit()
                finally:
                    event.remove(db, 'before_commit', reject_commit)
            db.rollback()
        self.assert_restored()
        self.assert_waiting(renewal, 'activation_verified')
        self.assertEqual(self.facts(renewal), self.baseline_facts)

    def test_root_rollback_restores_published_policies_via_existing_hook(self):
        self.root_failure(False)

    def test_root_commit_failure_restores_policies_on_rollback(self):
        self.root_failure(True)

    def test_security_unpublication_between_activation_phases_blocks_publication(self):
        renewal = self.prepared_activation()
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').status = 'security_unpublished'
            db.commit()
        refresh = Mock(wraps=self.m.subscription_gateway['refresh'])
        with patch.dict(self.m.subscription_gateway, {'refresh': refresh}):
            self.due_for(renewal)
        refresh.assert_not_called()
        self.assert_waiting(renewal, 'retrying', 'exception')
        self.assert_restored()

    def test_refund_write_does_not_wake_or_restart_manual_activation(self):
        _, renewal, start = self.renewal()
        self.clock.return_value = start
        with patch.object(self.m, 'validated_http_request', side_effect=TimeoutError('health unavailable')):
            for _ in range(3):
                self.due_for(renewal)
                self.clock.return_value += timedelta(seconds=10)
        self.assert_waiting(renewal, 'needs_manual', 'exception')
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, renewal)
            state = db.get(self.State, renewal)
            deadline = state.next_check_at
            order.refunded_amount = 10
            payment = db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == renewal))
            refund = self.m.Refund(refund_no='REF-MANUAL-ACTIVATION', order_id=renewal,
                payment_id=payment.id, amount=1, status='requested', reason='second refund request',
                requested_by='buyer')
            db.add(refund)
            db.commit()
            link = db.get(self.m.RefundOrderLink, refund.id)
            self.assertIsNotNone(link)  # Proves the real before_flush scanner ran.
            self.assertEqual(db.get(self.State, renewal).next_check_at, deadline)
        with patch.object(self.m, 'validated_http_request') as health:
            self.assertFalse(self.due())
        health.assert_not_called()
        with self.m.SessionLocal() as db:
            state = db.get(self.State, renewal)
            self.assertEqual((state.status, state.attempts, state.next_check_at), ('needs_manual', 3, deadline))


class SaaSActivationFaultTests(unittest.TestCase):
    Fixture = saas_fixture.SaaSAutoDeliveryTests
    setUpClass = classmethod(Fixture.setUpClass.__func__)
    tearDownClass = classmethod(Fixture.tearDownClass.__func__)
    tearDown = Fixture.tearDown
    request = Fixture.request
    pay = Fixture.pay
    task_id = Fixture.task_id
    create = Fixture.create
    step = Fixture.step
    finish = lifecycle_fixture.OrderServiceLifecycleTests.finish
    remote = Fixture.remote
    due = lifecycle_fixture.OrderServiceLifecycleTests.due
    due_for = lifecycle_fixture.OrderServiceLifecycleTests.due_for
    assert_waiting = lifecycle_fixture.OrderServiceLifecycleTests.assert_waiting

    def setUp(self):
        self.Fixture.setUp(self)
        clock = patch.object(self.m, 'now', return_value=datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.lifecycle = self.m.order_service_lifecycle
        self.State = self.lifecycle['State']

    def test_subscription_closed_between_phases_blocks_service_and_sla(self):
        first = self.create()
        self.pay(first)
        self.assertEqual(self.finish(first), 'completed')
        with self.m.SessionLocal() as db:
            start = lifecycle_fixture.utc(db.get(self.m.Order, first).service_ends_at)
        renewal = self.create()
        self.pay(renewal)
        self.assertEqual(self.finish(renewal), 'awaiting_effective')
        self.clock.return_value = start
        self.due_for(renewal)
        self.assert_waiting(renewal, 'activation_verified')
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, renewal)
            db.get(self.m.SaaSSubscription, order.subscription_id).status = 'closed'
            db.commit()
        self.entry_request.reset_mock()
        self.due_for(renewal)
        self.assert_waiting(renewal, 'retrying', 'exception')
        self.entry_request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
