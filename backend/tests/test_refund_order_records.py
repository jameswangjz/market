"""Isolated cash-record contract, without importing main or resource services."""

from datetime import datetime
from decimal import Decimal
import unittest
from unittest.mock import patch

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, create_engine, event, select
from sqlalchemy.orm import Session, declarative_base

from app.refund_order_records import COPY_FIELDS, install


class RefundOrderRecordTests(unittest.TestCase):
    def setUp(self):
        Base = declarative_base()
        order_fields = {
            '__tablename__': 'orders', 'id': Column(String, primary_key=True),
            'order_no': Column(String, unique=True),
        }
        for name in set(COPY_FIELDS) | {
            'order_type', 'business_type', 'related_order_id', 'main_status',
            'payment_status', 'delivery_status', 'after_sales_status',
            'amount', 'paid_amount', 'refunded_amount',
        }:
            kind = (DateTime if name.startswith('service_') else
                    Integer if name in {'snapshot_version', 'subscription_months'} else
                    Numeric(14, 2) if name.endswith('_snapshot') or name.endswith('amount') else String)
            order_fields[name] = Column(kind)
        self.Order = type('Order', (Base,), order_fields)

        class Refund(Base):
            __tablename__ = 'refunds'
            id = Column(String, primary_key=True)
            order_id = Column(ForeignKey('orders.id'), nullable=False)
            amount = Column(Numeric(14, 2))
            status = Column(String, default='pending')

        class Payment(Base):
            __tablename__ = 'payments'
            id = Column(String, primary_key=True)
            order_id = Column(ForeignKey('orders.id'))

        class DeliveryTask(Base):
            __tablename__ = 'delivery_tasks'
            id = Column(String, primary_key=True)
            order_id = Column(ForeignKey('orders.id'))

        self.Refund, self.Payment, self.DeliveryTask = Refund, Payment, DeliveryTask
        self.ns = dict(Base=Base, Order=self.Order, Refund=Refund)
        self.h = install(self.ns)
        self.engine = create_engine('sqlite://')
        event.listen(self.engine, 'connect', lambda conn, _: conn.execute('PRAGMA foreign_keys=ON'))
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        values = {name: 'original-' + name for name in COPY_FIELDS}
        values.update({name: datetime(2026, 10, 1) for name in COPY_FIELDS if name.startswith('service_')})
        values.update(snapshot_version=1, subscription_months=12,
                      unit_price_snapshot=100, unit_cost_snapshot=10, total_cost_snapshot=120)
        self.source = self.Order(id='source', order_no='SOURCE', order_type='purchase',
                                 business_type='subscription', amount=1200, paid_amount=1200, **values)
        self.db.add(self.source)
        self.db.flush()

    def refund(self, **values):
        row = self.Refund(order_id='source', amount=Decimal('30.50'), status='pending')
        for key, value in values.items():
            setattr(row, key, value)
        self.db.add(row)
        return row

    def test_idempotency_identity_and_no_operational_rows(self):
        refund = self.refund()
        with patch.object(self.db, 'flush', side_effect=AssertionError('unexpected flush')), \
                patch.object(self.db, 'commit', side_effect=AssertionError('unexpected commit')):
            record = self.h['sync'](self.db, refund, flush=False)
            self.assertIs(record, self.h['sync'](self.db, refund))
        self.assertTrue(refund.id)
        self.db.flush()
        self.assertIs(record, self.h['sync'](self.db, refund))
        self.db.flush()
        self.assertEqual(len(list(self.db.scalars(select(self.Order)))), 2)
        link = self.db.get(self.h['RefundOrderLink'], refund.id)
        self.assertEqual((link.order_id, link.source_order_id), (record.id, 'source'))
        for name in COPY_FIELDS:
            self.assertEqual(getattr(record, name), getattr(self.source, name))
        self.assertEqual(record.order_type, 'refund')
        self.assertEqual(record.business_type, 'refund_record')
        self.assertEqual(record.related_order_id, 'source')
        self.assertEqual(record.delivery_status, 'not_applicable')
        self.assertEqual(record.paid_amount, 0)
        self.assertEqual(list(self.db.scalars(select(self.Payment))), [])
        self.assertEqual(list(self.db.scalars(select(self.DeliveryTask))), [])
        self.assertEqual(self.source.amount, 1200)
        self.assertEqual(self.source.paid_amount, 1200)

    def test_statuses_amount_updates_and_downgrade(self):
        self.source.business_type = 'version_change_payment'
        refund = self.refund()
        record = self.h['sync'](self.db, refund)
        self.db.flush()
        for status in ('pending', 'requested', 'pending_review', 'approved', 'executing', 'failed',
                       'completed', 'rejected'):
            refund.status = status
            refund.amount = Decimal('25.00')
            self.h['sync'](self.db, refund)
            self.db.flush()
            self.assertEqual(record.amount, Decimal('-25.00'))
            self.assertEqual(record.main_status, 'refund_' + status if status in {'completed', 'rejected'}
                             else 'refund_pending')
            self.assertEqual(record.payment_status, 'refunded' if status == 'completed' else 'not_applicable')
        self.assertEqual(self.source.amount, 1200)

    def test_origin_immutable_and_snapshot_frozen(self):
        refund = self.refund()
        record = self.h['sync'](self.db, refund)
        self.db.flush()
        original_version = record.product_version_id
        self.source.product_version_id = 'upgraded'
        self.h['sync'](self.db, refund)
        self.assertEqual(record.product_version_id, original_version)
        refund.order_id = 'other'
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.h['sync'](self.db, refund)

    def test_before_flush_scan_and_disabled_no_backfill(self):
        historical = self.refund(id='historical')
        self.db.flush()
        self.assertEqual(self.h['scan_pending'](self.db), [])
        calls = []

        def before_flush(db, context, instances):
            calls.extend(self.h['scan_pending'](db))

        event.listen(self.db, 'before_flush', before_flush)
        refund = self.refund()
        self.assertEqual(self.h['scan_pending'](self.db, enabled=False), [])
        self.assertIsNone(self.h['sync'](self.db, refund, enabled=False))
        self.db.flush()
        self.assertEqual(len(calls), 1)
        refund.status = 'completed'
        self.db.flush()
        self.assertEqual(calls[-1].payment_status, 'refunded')
        self.assertIsNone(self.db.get(self.h['RefundOrderLink'], historical.id))
        self.assertIs(install(self.ns), self.h)

    def test_invalid_amount_and_source(self):
        refund = self.refund()
        for amount in ('0', '-1', 'NaN', '1.001'):
            refund.amount = Decimal(amount)
            with self.assertRaises(ValueError):
                self.h['sync'](self.db, refund)
        refund.amount = 1
        self.source.order_type = 'refund'
        with self.assertRaisesRegex(ValueError, 'positive source'):
            self.h['sync'](self.db, refund)

    def test_link_cannot_be_repointed(self):
        refund = self.refund()
        self.h['sync'](self.db, refund, flush=True)
        link = self.db.get(self.h['RefundOrderLink'], refund.id)
        link.order_id = 'replacement'
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.h['sync'](self.db, refund)

    def test_staged_records_roll_back_with_refund(self):
        refund = self.refund()
        with patch.object(self.db, 'commit', side_effect=AssertionError('unexpected commit')):
            record = self.h['sync'](self.db, refund, flush=True)
            refund_id, order_id = refund.id, record.id
            self.db.rollback()
        self.assertIsNone(self.db.get(self.Refund, refund_id))
        self.assertIsNone(self.db.get(self.Order, order_id))
        self.assertIsNone(self.db.get(self.h['RefundOrderLink'], refund_id))


if __name__ == '__main__':
    unittest.main()
