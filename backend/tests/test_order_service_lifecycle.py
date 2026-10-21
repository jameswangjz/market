"""Real-model service timing, activation, and monthly settlement regressions."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select

import test_api_auto_delivery as api_fixture
import test_version_change_saga_integration as change_fixture


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


class OrderServiceLifecycleTests(unittest.TestCase):
    Fixture = api_fixture.ApiAutoDeliveryTests
    setUpClass = classmethod(Fixture.setUpClass.__func__)
    tearDownClass = classmethod(Fixture.tearDownClass.__func__)
    tearDown = Fixture.tearDown
    request = Fixture.request
    task_id = Fixture.task_id
    create = Fixture.create
    step = Fixture.step
    admin_request = Fixture.admin_request
    redis_eval = Fixture.redis_eval

    def setUp(self):
        self.Fixture.setUp(self)
        self.at = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
        clock = patch.object(self.m, 'now', return_value=self.at)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.lifecycle = self.m.order_service_lifecycle
        self.State = self.lifecycle['State']
        self.Term = self.m.subscription_terms['Term']

    def pay(self, order_id):
        return self.request('POST', f'/api/orders/{order_id}/transition', actor='platform',
                            json={'action': 'confirm_payment'})

    def finish(self, order_id):
        for _ in range(16):
            self.step(order_id)
            with self.m.SessionLocal() as db:
                task = db.get(self.m.DeliveryTask, self.task_id(order_id))
                if task.status in {'completed', 'awaiting_effective'}:
                    return task.status
                self.assertNotEqual(task.status, 'exception', task.last_error)
        self.fail('API delivery did not complete')

    def due(self):
        with self.m.SessionLocal() as db:
            result = self.lifecycle['process_due'](db, db.get(self.m.User, 'ops'))
            db.commit()
            return result

    def due_for(self, order_id):
        def state():
            with self.m.SessionLocal() as db:
                row = db.get(self.State, order_id)
                return row.status, row.attempts, utc(row.next_check_at)

        before = state()
        # Expiry and activation can share a timestamp; UUID ordering is arbitrary.
        for _ in range(4):
            self.assertTrue(self.due())
            after = state()
            if after != before:
                if after[2] is None or after[2] > self.clock.return_value:
                    for _ in range(4):
                        if not self.due():
                            return
                    self.fail('Other due service work did not drain')
                return
        self.fail('Target service state did not advance')

    def facts(self, order_id):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, order_id)
            terms = db.scalars(select(self.Term).where(self.Term.order_id == order_id)).all()
            payment = db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == order_id))
            return (order.economic_snapshot_json, order.delivery_snapshot_json, order.amount,
                    order.total_cost_snapshot, order.paid_amount, order.refunded_amount,
                    order.payment_status, payment.id, payment.amount, payment.status, utc(payment.paid_at),
                    tuple((term.id, utc(term.starts_at), utc(term.ends_at), term.months) for term in terms))

    def renewal(self):
        first = self.create()
        self.finish(first)
        with self.m.SessionLocal() as db:
            start = utc(db.get(self.m.Order, first).service_ends_at)
        renewal = self.create(2)
        self.finish(renewal)
        return first, renewal, start

    def assert_waiting(self, order_id, status='scheduled', delivery='awaiting_effective'):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, order_id)
            state = db.get(self.State, order_id)
            self.assertEqual((order.main_status, order.delivery_status), ('pending_fulfillment', delivery))
            self.assertEqual(state.status, status)
            task = db.scalar(select(self.m.DeliveryTask).where(self.m.DeliveryTask.order_id == order_id))
            self.assertEqual(task.status, 'awaiting_effective')
            self.assertIsNone(order.service_effective_at)
            contract = self.m.order_sla_contracts['get_contract'](db, order_id)
            self.assertIsNotNone(contract)
            self.assertIsNone(contract.verified_at)

    def activate(self, order_id, start):
        self.clock.return_value = start
        self.due_for(order_id)
        self.assert_waiting(order_id, status='activation_verified')
        self.due_for(order_id)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, order_id)
            self.assertEqual((order.main_status, order.delivery_status), ('fulfilling', 'accepted'))
            self.assertEqual(utc(order.service_effective_at), start)
            self.assertEqual(db.get(self.State, order_id).status, 'active')
            task = db.scalar(select(self.m.DeliveryTask).where(self.m.DeliveryTask.order_id == order_id))
            self.assertEqual(task.status, 'completed')
            contract = self.m.order_sla_contracts['get_contract'](db, order_id)
            self.assertEqual(utc(contract.verified_at), start)

    def test_paid_api_delivery_fulfills_until_expiry_without_economic_changes(self):
        order_id = self.create(3)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, order_id).payment_status, 'paid')
            self.assertIsNone(self.m.order_sla_contracts['get_contract'](db, order_id).verified_at)
        self.finish(order_id)
        before = self.facts(order_id)
        self.assertEqual(len(before[-1]), 1)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, order_id)
            self.assertEqual((order.main_status, order.delivery_status), ('fulfilling', 'accepted'))
            self.assertEqual(order.order_type, 'new_purchase')
            self.assertEqual(utc(order.service_starts_at), self.at)
            self.assertEqual(utc(order.service_effective_at), self.at)
            end = utc(order.service_ends_at)
            self.assertEqual(end, before[-1][0][2])
        self.assertFalse(self.due())
        self.assertFalse(self.m.online_delivery['process_once']())
        self.clock.return_value = end
        self.assertTrue(self.due())
        self.assertFalse(self.due())
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, order_id).main_status, 'completed')
            self.assertEqual(db.get(self.State, order_id).status, 'completed')
        self.assertEqual(self.facts(order_id), before)

    def test_future_renewal_has_no_early_sla_and_activates_in_two_commits(self):
        first, renewal, start = self.renewal()
        before = self.facts(renewal)
        first_before = self.facts(first)
        self.assert_waiting(renewal)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, renewal)
            self.assertEqual(order.order_type, 'renewal')
            self.assertEqual(utc(order.service_starts_at), start)
        self.clock.return_value = start - timedelta(microseconds=1)
        self.assertFalse(self.due())
        self.assert_waiting(renewal)
        self.activate(renewal, start)
        self.assertFalse(self.due())
        self.assertEqual(self.facts(renewal), before)
        self.assertEqual(self.facts(first), first_before)

    def test_failed_activation_retries_three_times_ten_seconds_then_manual(self):
        _, renewal, start = self.renewal()
        before = self.facts(renewal)
        self.clock.return_value = start
        with patch.object(self.m, 'validated_http_request', side_effect=TimeoutError('PRIVATE HEALTH SECRET')) as health:
            for attempt in range(1, 4):
                self.due_for(renewal)
                self.assert_waiting(renewal, 'retrying' if attempt < 3 else 'needs_manual', 'exception')
                with self.m.SessionLocal() as db:
                    state = db.get(self.State, renewal)
                    self.assertEqual(state.attempts, attempt)
                    self.assertNotIn('PRIVATE', state.last_error)
                    next_at = utc(state.next_check_at)
                if attempt < 3:
                    self.assertEqual(next_at, self.clock.return_value + timedelta(seconds=10))
                    self.clock.return_value += timedelta(seconds=9)
                    self.assertFalse(self.due())
                    self.clock.return_value += timedelta(seconds=1)
                else:
                    self.clock.return_value += timedelta(seconds=10)
                    self.assertFalse(self.due())
            self.assertEqual(health.call_count, 3)
        self.assertEqual(self.facts(renewal), before)
        self.request('POST', f'/api/orders/{renewal}/service-retry', actor='buyer', expected=403)
        self.request('POST', f'/api/orders/{renewal}/service-retry', actor='ops')
        self.due_for(renewal)
        self.assert_waiting(renewal, 'activation_verified', 'exception')
        self.due_for(renewal)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, renewal).main_status, 'fulfilling')
            self.assertEqual(db.get(self.State, renewal).attempts, 0)
        self.assertEqual(self.facts(renewal), before)

    def test_activation_publish_failure_does_not_record_effective_or_sla(self):
        _, renewal, start = self.renewal()
        before = self.facts(renewal)
        self.clock.return_value = start
        self.due_for(renewal)
        self.assert_waiting(renewal, 'activation_verified')
        with patch.dict(self.m.subscription_gateway, {'refresh': unittest.mock.Mock(side_effect=RuntimeError('private Redis failure'))}):
            self.due_for(renewal)
        self.assert_waiting(renewal, 'retrying', 'exception')
        self.assertEqual(self.facts(renewal), before)

    def test_monthly_settlement_includes_paid_pending_and_excludes_operation_records(self):
        pending = self.create()
        funding = self.create()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, pending)
            order.main_status, order.delivery_status = 'pending_fulfillment', 'awaiting_effective'
            operation = db.get(self.m.Order, funding)
            operation.business_type, operation.order_type = 'version_change_payment', 'upgrade'
            with patch.dict(self.m.storefront, {'versions_for': lambda db, product: [{'id': 'version'}]}):
                quoted = self.m.order_economics['quote'](db, db.get(self.m.User, 'buyer'),
                    self.m.OrderBody(product_id='product', product_version_id='version', buyer_enterprise_id='buyer_tenant'))
            refund = self.m.create_quoted_order(db, db.get(self.m.User, 'buyer'), quoted)
            refund.business_type, refund.order_type, refund.amount = 'refund', 'refund', -25
            refund.main_status, refund.payment_status = 'completed', 'refunded'
            db.flush()
            for payment in db.scalars(select(self.m.Payment).where(self.m.Payment.order_id == refund.id)):
                db.delete(payment)
            refund_id = refund.id
            db.commit()
        result = self.request('POST', '/api/settlement-batches', actor='platform', json={
            'cycle': 'monthly', 'period_start': '2026-10-01T00:00:00Z',
            'period_end': '2026-11-01T00:00:00Z', 'idempotency_key': 'service-pending-monthly'})
        with self.m.SessionLocal() as db:
            settlements = db.scalars(select(self.m.Settlement).join(self.m.SettlementLine,
                self.m.SettlementLine.settlement_id == self.m.Settlement.id).where(
                self.m.SettlementLine.batch_id == result['id'])).unique().all()
            self.assertEqual({row.order_id for row in settlements}, {pending})
            self.assertIsNone(db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == refund_id)))


class VersionOperationServiceLifecycleTests(unittest.TestCase):
    Fixture = change_fixture.VersionChangeSagaIntegrationTests
    setUpClass = classmethod(Fixture.setUpClass.__func__)
    tearDownClass = classmethod(Fixture.tearDownClass.__func__)
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    request = Fixture.request
    create = Fixture.create
    deliver = Fixture.deliver
    prepare = Fixture.prepare
    quote = Fixture.quote
    request_change = Fixture.request_change

    def test_operation_end_covers_all_terms_and_requires_verified_resource_change(self):
        for price in (150, 50):
            with self.subTest(price=price):
                source = self.prepare(target_price=price)
                quote = self.quote(source)
                created = self.request_change(source, quote, key=f'operation-{price}')
                lifecycle = self.m.order_service_lifecycle
                apply = lifecycle.get('sync_change') or lifecycle['change_applied']
                end = max(datetime.fromisoformat(item['ends_at']) for item in quote['segments'])
                with self.m.SessionLocal() as db:
                    change = db.get(self.m.VersionChangeRequest, created['id'])
                    operation = db.get(self.m.Order, change.funding_order_id)
                    self.assertEqual(utc(operation.service_ends_at), end)
                    self.assertEqual(utc(operation.service_starts_at), utc(operation.created_at))
                    original = (operation.amount, operation.payment_status, operation.economic_snapshot_json)
                    source_end = db.scalar(select(self.m.subscription_terms['Term'].ends_at).where(
                        self.m.subscription_terms['Term'].order_id == source))
                    self.assertGreater(end, utc(source_end))
                    change.status, change.resource_applied = 'completed', False
                    apply(db, change)
                    self.assertIsNone(operation.service_effective_at)
                    self.assertNotEqual(operation.main_status, 'fulfilling')
                    change.resource_applied = True
                    apply(db, change)
                    db.flush()
                    effective = operation.service_effective_at
                    self.assertEqual(operation.main_status, 'fulfilling')
                    self.assertEqual(utc(effective), utc(self.m.now()))
                    apply(db, change)
                    self.assertEqual(operation.service_effective_at, effective)
                    self.assertEqual((operation.amount, operation.payment_status, operation.economic_snapshot_json), original)
                    self.assertEqual(utc(operation.service_ends_at), end)
                    self.assertEqual(db.scalar(select(self.m.subscription_terms['Term'].ends_at).where(
                        self.m.subscription_terms['Term'].order_id == source)), source_end)
                    change.status = 'cancelled'
                    db.commit()


if __name__ == '__main__':
    unittest.main()
