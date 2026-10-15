"""Actual ordinary execution metering on disposable SQLite and isolated Redis."""
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select

import test_refund_execution as fixture


class RefundExecutionMeteringTests(unittest.TestCase):
    setUpClass = classmethod(fixture.RefundExecutionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.RefundExecutionTests.tearDownClass.__func__)
    setUp = fixture.RefundExecutionTests.setUp
    tearDown = fixture.RefundExecutionTests.tearDown
    request = fixture.RefundExecutionTests.request
    create_order = fixture.RefundExecutionTests.create_order
    monthly = fixture.RefundExecutionTests.monthly
    create = fixture.RefundExecutionTests.create
    review = fixture.RefundExecutionTests.review
    ledger = fixture.RefundExecutionTests.ledger
    approve = fixture.RefundExecutionTests.approve
    execute = fixture.RefundExecutionTests.execute
    step = fixture.RefundExecutionTests.step

    def measurements(self, db):
        Model = self.m.SettlementMeasurement
        return db.scalars(select(Model).where(Model.order_id == self.order_id,
                                              Model.measurement_type == 'refund')).all()

    def assert_measurement(self, db, refund_id):
        rows = self.measurements(db)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.quantity, Decimal('20'))
        self.assertEqual(row.unit, 'currency')
        self.assertEqual(row.source, 'refund')
        self.assertEqual(row.source_id, refund_id)
        self.assertEqual(row.event_key, refund_id + ':completed')
        self.assertEqual(row.actor_id, 'refund-execution-worker')
        self.assertEqual(json.loads(row.evidence_json), dict(refund_id=refund_id,
            payment_id='paid-source', basis='negative_adjustment', funds_mode='simulated'))
        return row.id

    def test_actual_completion_records_exact_refund_once_on_replay(self):
        approved = self.approve()
        self.execute(approved)
        with patch.dict(self.m.settlement_metering,
                        record_order_event=lambda *a, **kw: self.fail('order observer called')):
            for _ in range(5):
                self.step()
        self.assertFalse(self.step())
        self.execute(approved)  # Replay the original execution operation key.
        self.assertFalse(self.step())
        with self.m.SessionLocal() as db:
            self.assertIsNotNone(db.get(self.Completion, approved['id']))
            self.assert_measurement(db, approved['id'])

    def test_metering_failure_rolls_back_completion_preserves_legs_and_retry_records_once(self):
        approved = self.approve()
        self.execute(approved)
        for _ in range(4):
            self.step()
        with self.m.SessionLocal() as db:
            leg = db.scalar(select(self.Leg))
            receipt, completed_at = leg.receipt, leg.completed_at
            self.assertEqual(leg.status, 'completed')
        hooks = self.m.settlement_metering
        record = hooks['record']

        def fail_after_record(*args, **kwargs):
            record(*args, **kwargs)
            raise RuntimeError('injected metering failure after insert')

        with patch.dict(hooks, record=fail_after_record):
            self.step()
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.get(self.Completion, approved['id']))
            refund = db.get(self.m.Refund, approved['id'])
            self.assertEqual(refund.status, 'failed')
            self.assertIsNone(refund.completed_at)
            self.assertIsNone(db.scalar(select(self.Execution)).completed_at)
            self.assertEqual(db.get(self.m.Order, self.order_id).refunded_amount, 0)
            self.assertEqual(self.measurements(db), [])
            leg = db.scalar(select(self.Leg))
            self.assertEqual((leg.status, leg.receipt, leg.completed_at),
                             ('completed', receipt, completed_at))
        failed = self.request('GET', '/api/refunds/' + approved['id'], actor='ops')
        self.execute(failed, operation='retry')
        self.step()
        self.assertFalse(self.step())
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Refund, approved['id']).status, 'completed')
            self.assertIsNotNone(db.get(self.Completion, approved['id']))
            self.assertEqual(db.get(self.m.Order, self.order_id).refunded_amount, Decimal('20'))
            self.assert_measurement(db, approved['id'])
            leg = db.scalar(select(self.Leg))
            self.assertEqual((leg.receipt, leg.completed_at), (receipt, completed_at))
