"""Expiry authorization scope and bounded transaction scheduling."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import HTTPException
from sqlalchemy import Column, String, create_engine
from sqlalchemy.orm import Session, declarative_base

from app.api_provider_expiry_worker import blocked, run


class BlockerTests(unittest.TestCase):
    def setUp(self):
        Base = declarative_base()

        class Order(Base):
            __tablename__ = 'orders'
            id = Column(String, primary_key=True)
            buyer_enterprise_id = Column(String)
            product_id = Column(String)

        class Refund(Base):
            __tablename__ = 'refunds'
            id = Column(String, primary_key=True)
            order_id = Column(String)

        class Execution(Base):
            __tablename__ = 'executions'
            id = Column(String, primary_key=True)
            refund_id = Column(String)
            status = Column(String)

        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        self.db = Session(engine)
        self.addCleanup(self.db.close)
        self.combo = SimpleNamespace(id='combo', generation=2, status='active')
        self.lock = Mock(return_value=self.combo)
        self.delivery, self.saga = Mock(), Mock()
        self.ns = dict(subscription_terms={'lock_combination': self.lock},
            api_auto_delivery={'assert_no_pending': self.delivery},
            version_change_saga={'assert_no_pending': self.saga, 'Plan': object()},
            refund_execution={'Execution': Execution}, Refund=Refund, Order=Order)
        self.body = dict(enterprise_id='buyer', product_id='product', combination_id='combo', generation=2)
        self.execution = Execution(id='execution', refund_id='refund', status='completed')
        self.order = Order(id='order', buyer_enterprise_id='buyer', product_id='product')
        self.db.add_all([self.order, Refund(id='refund', order_id='order'), self.execution])
        self.db.commit()

    def test_refund_states_and_scope(self):
        for status in ('prepared', 'resources_pending', 'resources_applied', 'funds_pending', 'failed'):
            with self.subTest(status=status):
                self.execution.status = status
                self.assertTrue(blocked(self.db, self.body, ns=self.ns))
        self.execution.status = 'completed'
        self.assertFalse(blocked(self.db, self.body, ns=self.ns))
        self.execution.status = 'failed'
        for field in ('buyer_enterprise_id', 'product_id'):
            value = getattr(self.order, field)
            setattr(self.order, field, 'other')
            self.assertFalse(blocked(self.db, self.body, ns=self.ns))
            setattr(self.order, field, value)
        self.lock.assert_called_with(self.db, 'buyer', 'product')

    def test_identity_status_and_pending_guards(self):
        for field, value in (('id', 'other'), ('generation', 3), ('status', 'revoked')):
            original = getattr(self.combo, field)
            setattr(self.combo, field, value)
            self.assertTrue(blocked(self.db, self.body, ns=self.ns))
            setattr(self.combo, field, original)
        for guard in (self.delivery, self.saga):
            guard.side_effect = HTTPException(409, 'sensitive provider detail')
            self.assertTrue(blocked(self.db, self.body, ns=self.ns))
            guard.side_effect = None
        # Completed saga with an unused Plan is authorized by assert_no_pending.
        self.assertFalse(blocked(self.db, self.body, ns=self.ns))
        self.saga.assert_called_with(self.db, 'buyer', 'product')


class TimingTests(unittest.TestCase):
    def worker(self, tick):
        self.elapsed, self.sleeps, self.commits, self.times = 0, [], 0, []
        owner = self

        class DB:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def commit(self):
                owner.commits += 1

        def expire(db, **kwargs):
            self.times.append(self.elapsed)
            return tick(len(self.times))

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.elapsed += seconds

        return run(ns={'SessionLocal': DB, 'api_provider_lifecycle': {'expire_once': expire}},
            monotonic=lambda: self.elapsed, sleep=sleep,
            now=lambda: datetime(2026, 10, 19, tzinfo=timezone.utc) + timedelta(seconds=self.elapsed))

    def test_three_failed_attempts_committed_then_stop(self):
        def tick(n):
            if n == 1:
                return [dict(status='prepared')]
            if n % 2 == 0:
                return [dict(status='reserved')]
            attempts = (n - 1) // 2
            return [dict(status='needs_manual' if attempts == 3 else 'prepared', attempts=attempts)]
        self.assertEqual(self.worker(tick), 0)
        self.assertEqual(self.times, [0, 0, 0, 10, 10, 20, 20])
        self.assertEqual(self.commits, 7)
        self.assertEqual(self.sleeps, [10, 10])

    def test_budget_and_terminal_states(self):
        self.worker(lambda n: [dict(status='reserved')])
        self.assertEqual(self.elapsed, 40)
        self.assertEqual(self.sleeps, [10] * 4)
        for status in ('needs_manual', 'verified', 'blocked'):
            self.worker(lambda n: [dict(status=status)])
            self.assertEqual(self.times, [0])
        self.worker(lambda n: [])
        self.assertEqual(self.times, [0])

    def test_safe_error(self):
        def fail(n):
            raise RuntimeError('secret-token provider-response')
        with self.assertLogs('app.api_provider_expiry_worker', level='ERROR') as captured:
            self.assertEqual(self.worker(fail), 1)
        self.assertNotIn('secret-token', str(captured.output))
        self.assertEqual(self.commits, 0)


if __name__ == '__main__':
    unittest.main()
