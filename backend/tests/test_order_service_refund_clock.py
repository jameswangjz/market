"""Refund cutoff clocks on real lifecycle models with isolated overlay evidence."""

from datetime import timedelta
from decimal import Decimal
import json
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import inspect, select

import test_order_service_lifecycle as lifecycle_fixture
from test_order_service_lifecycle import utc


class OrderServiceRefundClockTests(unittest.TestCase):
    ServiceLifecycleTests = lifecycle_fixture.OrderServiceLifecycleTests
    Fixture = ServiceLifecycleTests.Fixture
    setUpClass = classmethod(ServiceLifecycleTests.setUpClass.__func__)
    tearDownClass = classmethod(ServiceLifecycleTests.tearDownClass.__func__)
    setUp = ServiceLifecycleTests.setUp
    tearDown = ServiceLifecycleTests.tearDown
    request = ServiceLifecycleTests.request
    task_id = ServiceLifecycleTests.task_id
    create = ServiceLifecycleTests.create
    step = ServiceLifecycleTests.step
    admin_request = ServiceLifecycleTests.admin_request
    redis_eval = ServiceLifecycleTests.redis_eval
    pay = ServiceLifecycleTests.pay
    finish = ServiceLifecycleTests.finish
    due = ServiceLifecycleTests.due
    due_for = ServiceLifecycleTests.due_for
    renewal = ServiceLifecycleTests.renewal
    assert_waiting = ServiceLifecycleTests.assert_waiting

    def financial_facts(self, order_id):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, order_id)
            snapshot = json.loads(order.economic_snapshot_json)
            payment = db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == order_id))
            term = db.scalar(select(self.Term).where(self.Term.order_id == order_id))
            return (order.amount, order.paid_amount, order.unit_price_snapshot,
                    order.unit_cost_snapshot, order.total_cost_snapshot,
                    order.economic_snapshot_json, order.delivery_snapshot_json,
                    snapshot['currency'], payment.id, payment.amount, utc(payment.paid_at),
                    term.id, utc(term.starts_at), utc(term.ends_at), term.months)

    def seed_refund(self, order_id, *, full=False, status='completed'):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, order_id)
            payment = db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == order_id))
            amount = order.paid_amount if full else Decimal('10.00')
            order.refunded_amount = amount if status == 'completed' else 0
            refund = self.m.Refund(refund_no='CLOCK-' + order_id, order_id=order_id,
                payment_id=payment.id, amount=amount, status=status,
                completed_at=self.clock.return_value if status == 'completed' else None)
            db.add(refund)
            db.flush()
            self.assertTrue(refund.id)
            if status == 'completed':
                self.assertEqual(utc(db.get(self.State, order_id).next_check_at), self.clock.return_value)
            refund_id = refund.id
            db.commit()
            return refund_id

    def wake_again(self, order_id, refund_id):
        with self.m.SessionLocal() as db:
            state = db.get(self.State, order_id)
            expected_next = utc(state.next_check_at) if state.status == 'needs_manual' else self.clock.return_value
            refund = db.get(self.m.Refund, refund_id)
            refund.reason = refund.reason + ' changed'
            db.flush()
            self.assertEqual(utc(state.next_check_at), expected_next)
            db.commit()

    def assert_stopped(self, order_id):
        with self.m.SessionLocal() as db:
            order, state = db.get(self.m.Order, order_id), db.get(self.State, order_id)
            self.assertEqual((order.main_status, order.delivery_status), ('refunded', 'revoked'))
            self.assertEqual(state.status, 'stopped')
            self.assertIsNone(state.next_check_at)

    def test_new_full_refund_wakes_active_service_and_stops_without_expiry_wait(self):
        order_id = self.create()
        self.finish(order_id)
        before = self.financial_facts(order_id)
        self.assertGreater(before[-2], self.clock.return_value)
        self.seed_refund(order_id, full=True)
        overlay = Mock(side_effect=AssertionError('full refund does not need an overlay'))
        with patch.dict(self.m.refund_execution_resources, entitlement=overlay):
            self.due_for(order_id)
        self.assert_stopped(order_id)
        self.assertFalse(self.due())
        self.assertEqual(self.financial_facts(order_id), before)

    def test_dirty_full_refund_wakes_future_service_and_never_activates(self):
        _, order_id, start = self.renewal()
        before = self.financial_facts(order_id)
        refund_id = self.seed_refund(order_id, full=True, status='approved')
        with self.m.SessionLocal() as db:
            self.assertEqual(utc(db.get(self.State, order_id).next_check_at), start)
            order = db.get(self.m.Order, order_id)
            refund = db.get(self.m.Refund, refund_id)
            order.refunded_amount = order.paid_amount
            refund.status, refund.completed_at = 'completed', self.clock.return_value
            db.flush()
            self.assertEqual(utc(db.get(self.State, order_id).next_check_at), self.clock.return_value)
            db.commit()
        verify, refresh = Mock(), Mock()
        with patch.dict(self.m.subscription_gateway, verify_delivery=verify, refresh=refresh):
            self.due_for(order_id)
        verify.assert_not_called()
        refresh.assert_not_called()
        self.assert_stopped(order_id)
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.get(self.m.Order, order_id).service_effective_at)
            self.assertIsNone(self.m.order_sla_contracts['get_contract'](db, order_id).verified_at)
        self.assertEqual(self.financial_facts(order_id), before)

    def test_partial_refund_denies_incompatible_or_empty_periods_and_escalates(self):
        order_id = self.create()
        self.finish(order_id)
        before = self.financial_facts(order_id)
        original_end = before[-2]
        refund_id = self.seed_refund(order_id)
        cutoff = self.clock.return_value + timedelta(days=2)
        overlays = ({'payment_compatible': True, 'periods': []},
                    {'periods': [{'ends_at': cutoff.isoformat()}]},
                    {'payment_compatible': False, 'periods': [{'ends_at': cutoff.isoformat()}]})
        for attempt, overlay in enumerate(overlays, 1):
            with self.subTest(overlay=overlay):
                self.wake_again(order_id, refund_id)
                evidence = Mock(return_value=overlay)
                with patch.dict(self.m.refund_execution_resources, entitlement=evidence):
                    self.due_for(order_id)
                evidence.assert_called_once()
                self.assertEqual(inspect(evidence.call_args.args[1]).identity, (order_id,))
                self.assertEqual(inspect(evidence.call_args.args[2]).identity, (before[-4],))
                with self.m.SessionLocal() as db:
                    order, state = db.get(self.m.Order, order_id), db.get(self.State, order_id)
                    self.assertEqual(utc(order.service_ends_at), original_end)
                    self.assertEqual((order.main_status, order.delivery_status),
                                     ('pending_fulfillment', 'exception'))
                    self.assertEqual(state.status, 'retrying' if attempt < 3 else 'needs_manual')
                    self.assertEqual(state.attempts, attempt)
                    self.assertEqual(utc(state.next_check_at), self.clock.return_value + timedelta(seconds=10)
                                     if attempt < 3 else original_end)
                self.assertEqual(self.financial_facts(order_id), before)

    def test_empty_overlay_denies_active_service(self):
        order_id = self.create()
        self.finish(order_id)
        before = self.financial_facts(order_id)
        self.seed_refund(order_id)
        with patch.dict(self.m.refund_execution_resources, entitlement=Mock(return_value={})):
            self.due_for(order_id)
        with self.m.SessionLocal() as db:
            order, state = db.get(self.m.Order, order_id), db.get(self.State, order_id)
            self.assertEqual((order.main_status, order.delivery_status), ('pending_fulfillment', 'exception'))
            self.assertEqual((state.status, state.attempts), ('retrying', 1))
            self.assertEqual(utc(state.next_check_at), self.clock.return_value + timedelta(seconds=10))
        self.assertEqual(self.financial_facts(order_id), before)

    def test_refund_wake_does_not_reset_manual_escalation_or_reactivate_service(self):
        order_id = self.create()
        self.finish(order_id)
        before = self.financial_facts(order_id)
        refund_id = self.seed_refund(order_id)
        denied = Mock(return_value={'payment_compatible': False, 'periods': []})
        with patch.dict(self.m.refund_execution_resources, entitlement=denied):
            for attempt in range(1, 4):
                self.due_for(order_id)
                with self.m.SessionLocal() as db:
                    state = db.get(self.State, order_id)
                    self.assertEqual(state.attempts, attempt)
                    self.assertEqual(state.status, 'retrying' if attempt < 3 else 'needs_manual')
                if attempt < 3:
                    self.clock.return_value += timedelta(seconds=9)
                    self.assertFalse(self.due())
                    self.clock.return_value += timedelta(seconds=1)
        verify, refresh = Mock(), Mock()
        compatible = {'payment_compatible': True, 'periods': [{'ends_at': before[-2].isoformat()}]}
        with patch.dict(self.m.subscription_gateway, verify_delivery=verify, refresh=refresh):
            for overlay in (denied.return_value, compatible):
                with self.subTest(overlay=overlay):
                    self.wake_again(order_id, refund_id)
                    with self.m.SessionLocal() as db:
                        state = db.get(self.State, order_id)
                        self.assertEqual((state.status, state.attempts), ('needs_manual', 3))
                        self.assertEqual(utc(state.next_check_at), before[-2])
                    self.assertFalse(self.due())
                    # Even a stale queued clock cannot bypass manual review.
                    with self.m.SessionLocal() as db:
                        db.get(self.State, order_id).next_check_at = self.clock.return_value
                        db.commit()
                    with patch.dict(self.m.refund_execution_resources, entitlement=Mock(return_value=overlay)):
                        self.assertTrue(self.due())
                    with self.m.SessionLocal() as db:
                        order, state = db.get(self.m.Order, order_id), db.get(self.State, order_id)
                        self.assertEqual((order.main_status, order.delivery_status),
                                         ('pending_fulfillment', 'exception'))
                        self.assertEqual((state.status, state.attempts), ('needs_manual', 3))
                        state.next_check_at = before[-2]
                        db.commit()
        verify.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual(self.financial_facts(order_id), before)

    def test_compatible_partial_overlay_shrinks_clock_and_cannot_extend_it(self):
        order_id = self.create()
        self.finish(order_id)
        before = self.financial_facts(order_id)
        refund_id = self.seed_refund(order_id)
        cutoff = self.clock.return_value + timedelta(days=5)
        # Multiple periods use their latest end, accepting both stored formats.
        overlay = {'payment_compatible': True, 'periods': [
            {'end_at': (cutoff - timedelta(days=1)).timestamp()}, {'ends_at': cutoff.isoformat()}]}
        with patch.dict(self.m.refund_execution_resources, entitlement=Mock(return_value=overlay)):
            self.due_for(order_id)
        with self.m.SessionLocal() as db:
            self.assertEqual(utc(db.get(self.m.Order, order_id).service_ends_at), cutoff)
            self.assertEqual(utc(db.get(self.State, order_id).next_check_at), cutoff)
        self.wake_again(order_id, refund_id)
        longer = {'payment_compatible': True, 'periods': [{'end_at': before[-2].timestamp()}]}
        with patch.dict(self.m.refund_execution_resources, entitlement=Mock(return_value=longer)):
            self.due_for(order_id)
            with self.m.SessionLocal() as db:
                self.assertEqual(utc(db.get(self.m.Order, order_id).service_ends_at), cutoff)
            self.clock.return_value = cutoff
            self.due_for(order_id)
        with self.m.SessionLocal() as db:
            order, state = db.get(self.m.Order, order_id), db.get(self.State, order_id)
            self.assertEqual((order.main_status, state.status), ('completed', 'completed'))
            self.assertIsNone(state.next_check_at)
        self.assertEqual(self.financial_facts(order_id), before)

    def test_partial_future_refund_waits_then_expires_before_start_without_activation(self):
        _, order_id, start = self.renewal()
        before = self.financial_facts(order_id)
        refund_id = self.seed_refund(order_id)
        cutoff = start - timedelta(days=1)
        overlay = {'payment_compatible': True, 'periods': [{'ends_at': cutoff.isoformat()}]}
        verify, refresh = Mock(), Mock()
        with patch.dict(self.m.refund_execution_resources, entitlement=Mock(return_value=overlay)), \
                patch.dict(self.m.subscription_gateway, verify_delivery=verify, refresh=refresh):
            self.due_for(order_id)
            self.assert_waiting(order_id)
            with self.m.SessionLocal() as db:
                self.assertEqual(utc(db.get(self.State, order_id).next_check_at), cutoff)
            self.wake_again(order_id, refund_id)
            self.due_for(order_id)
            self.assert_waiting(order_id)
            self.clock.return_value = cutoff
            self.due_for(order_id)
        verify.assert_not_called()
        refresh.assert_not_called()
        with self.m.SessionLocal() as db:
            order, state = db.get(self.m.Order, order_id), db.get(self.State, order_id)
            self.assertEqual((order.main_status, state.status), ('completed', 'completed'))
            self.assertIsNone(order.service_effective_at)
            self.assertIsNone(state.next_check_at)
            self.assertIsNone(self.m.order_sla_contracts['get_contract'](db, order_id).verified_at)
        self.assertLess(self.clock.return_value, start)
        self.assertEqual(self.financial_facts(order_id), before)


if __name__ == '__main__':
    unittest.main()
