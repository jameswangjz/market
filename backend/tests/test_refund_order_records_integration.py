"""Real main before_flush integration on disposable, FK-enforced SQLite.

Downgrade links are persisted fixture evidence, not a saga execution proof.
No extra refund listener is installed and no external services are contacted.
"""

from datetime import timedelta
from decimal import Decimal
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import test_refund_workflow as fixture


class RefundOrderRecordsIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(fixture.RefundWorkflowTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.RefundWorkflowTests.tearDownClass.__func__)
    setUp = fixture.RefundWorkflowTests.setUp
    tearDown = fixture.RefundWorkflowTests.tearDown
    request = fixture.RefundWorkflowTests.request
    create_order = fixture.RefundWorkflowTests.create_order

    def new_refund(self, db, **values):
        refund = self.m.Refund(refund_no='REF-HOOK-INTEGRATION', order_id=self.order_id,
                               payment_id='paid-source', amount=Decimal('20.00'), status='pending')
        for name, value in values.items():
            setattr(refund, name, value)
        db.add(refund)
        return refund

    def record(self, db, refund_id):
        links = list(db.scalars(select(self.m.RefundOrderLink).where(
            self.m.RefundOrderLink.refund_id == refund_id)))
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].source_order_id, self.order_id)
        return db.get(self.m.Order, links[0].order_id)

    def assert_no_operational_rows(self, db, record):
        for model in (self.m.Payment, self.m.DeliveryTask,
                      self.m.subscription_terms['Term']):
            self.assertEqual(list(db.scalars(select(model).where(model.order_id == record.id))), [])

    def test_new_refund_flush_creates_one_negative_order_without_recursive_flush(self):
        with self.m.SessionLocal() as db:
            self.assertEqual(db.connection().exec_driver_sql('PRAGMA foreign_keys').scalar(), 1)
            source = db.get(self.m.Order, self.order_id)
            source.service_starts_at = self.m.now()
            source.service_ends_at = source.service_starts_at + timedelta(days=30)
            source.service_effective_at = source.service_starts_at
            # Compare stored timestamps: SQLite drops timezone information.
            db.flush()
            db.refresh(source)
            before = (source.amount, source.paid_amount)
            counts = {model: db.query(model).count() for model in (self.m.Order, self.m.Payment, self.m.DeliveryTask)}
            refund = self.new_refund(db)
            self.assertIsNone(refund.id)
            with patch.object(db, 'flush', wraps=db.flush) as flush, \
                    patch.object(db, 'commit', side_effect=AssertionError('hook committed')):
                db.flush()
                self.assertEqual(flush.call_count, 1)
            self.assertTrue(refund.id)
            record = self.record(db, refund.id)
            self.assertEqual((record.amount, record.paid_amount), (Decimal('-20.00'), 0))
            self.assertEqual((record.order_type, record.business_type, record.related_order_id),
                             ('refund', 'refund_record', source.id))
            self.assertEqual((record.main_status, record.payment_status, record.delivery_status),
                             ('refund_pending', 'not_applicable', 'not_applicable'))
            for name in ('buyer_enterprise_id', 'buyer_user_id', 'provider_enterprise_id',
                         'product_id', 'product_version_id', 'product_version_code',
                         'product_version_name', 'service_starts_at', 'service_ends_at',
                         'service_effective_at', 'economic_snapshot_json'):
                self.assertEqual(getattr(record, name), getattr(source, name))
            self.assert_no_operational_rows(db, record)
            self.assertEqual(db.query(self.m.Order).count(), counts[self.m.Order] + 1)
            for model in (self.m.Payment, self.m.DeliveryTask):
                self.assertEqual(db.query(model).count(), counts[model])
            self.assertEqual((source.amount, source.paid_amount), before)
            db.flush()
            self.assertEqual(self.record(db, refund.id).id, record.id)

    def test_dirty_refund_completion_amount_and_rejection_update_same_record(self):
        with self.m.SessionLocal() as db:
            refund = self.new_refund(db, status='requested')
            db.flush()
            refund_id, record_id = refund.id, self.record(db, refund.id).id
            db.commit()
        with self.m.SessionLocal() as db:
            refund = db.get(self.m.Refund, refund_id)
            for status in ('approved', 'executing', 'failed', 'completed', 'rejected'):
                with self.subTest(status=status):
                    refund.status = status
                    refund.amount = Decimal('15.25')
                    if status == 'completed':
                        refund.completed_at = self.m.now()
                    db.flush()
                    record = self.record(db, refund_id)
                    self.assertEqual(record.id, record_id)
                    self.assertEqual(record.amount, Decimal('-15.25'))
                    self.assertEqual(record.main_status, 'refund_' + status
                                     if status in {'completed', 'rejected'} else 'refund_pending')
                    self.assertEqual(record.payment_status,
                                     'refunded' if status == 'completed' else 'not_applicable')
                    self.assert_no_operational_rows(db, record)
            self.assertEqual(db.query(self.m.RefundOrderLink).count(), 1)
            self.assertEqual(db.get(self.m.Order, self.order_id).amount, Decimal('100.00'))

    def test_http_request_replay_uses_installed_hook(self):
        body = dict(amount='20.00', reason='Integration refund', idempotency_key='refund-hook-http')
        path = f'/api/orders/{self.order_id}/refund-requests'
        first = self.request('POST', path, json=body)
        self.assertEqual(self.request('POST', path, json=body)['id'], first['id'])
        with self.m.SessionLocal() as db:
            record = self.record(db, first['id'])
            self.assertEqual(record.amount, Decimal('-20.00'))
            self.assertEqual(db.query(self.m.RefundOrderLink).count(), 1)
            self.assert_no_operational_rows(db, record)

    def test_downgrade_fund_has_separate_cash_record_and_no_positive_income(self):
        with self.m.SessionLocal() as db:
            source = db.get(self.m.Order, self.order_id)
            Combination = self.m.subscription_terms['Combination']
            db.add(Combination(id='hook-combination', enterprise_id=source.buyer_enterprise_id,
                product_id=source.product_id, delivery_method='tenant_access', version_id='version',
                version_code='v1', anchor_at=self.m.now(), total_months=1))
            operation = self.m.Order(id='hook-downgrade-operation', order_no='HOOK-DOWNGRADE',
                buyer_enterprise_id=source.buyer_enterprise_id, buyer_user_id=source.buyer_user_id,
                provider_enterprise_id=source.provider_enterprise_id, product_id=source.product_id,
                buyer_name=source.buyer_name, product_name=source.product_name,
                order_type='downgrade', business_type='version_change_payment',
                related_order_id=source.id, amount=-20, paid_amount=0,
                payment_status='not_applicable', delivery_status='not_applicable')
            db.add(operation)
            db.flush()
            db.add(self.m.VersionChangeRequest(id='hook-downgrade', source_order_id=source.id,
                enterprise_id=source.buyer_enterprise_id, combination_id='hook-combination',
                target_version_id='version', target_version_code='v1', old_version_id='version',
                old_version_code='v2', kind='downgrade', status='completed',
                funding_order_id=operation.id, idempotency_key='hook-downgrade', request_hash='a' * 64,
                quote_json='{}', prepared_json='{}', target_snapshots_json='{}', created_by='ops'))
            refund = self.new_refund(db, status='completed', completed_at=self.m.now())
            db.flush()
            db.add(self.m.VersionChangeFund(change_id='hook-downgrade', kind='refund',
                payment_id='paid-source', refund_id=refund.id, amount=20,
                idempotency_key='hook-downgrade-fund'))
            db.flush()
            self.assertTrue(self.m.version_change_settlement['is_change_refund'](db, refund))
            cash = self.record(db, refund.id)
            self.assertNotEqual(cash.id, operation.id)
            self.assertEqual((cash.related_order_id, cash.amount, cash.order_type),
                             (source.id, Decimal('-20.00'), 'refund'))
            self.assert_no_operational_rows(db, cash)
            self.assert_no_operational_rows(db, operation)
            cash_id, operation_id = cash.id, operation.id
            self.assertEqual(db.query(self.m.Settlement).count(), 0)
            db.commit()
        # Exercise the real positive-income batch selection with both negative
        # orders explicitly requested, so absence is not just an unrun query.
        self.request('POST', '/api/settlement-batches', actor='finance', json={
            'order_ids': [cash_id, operation_id], 'idempotency_key': 'hook-negative-orders'})
        with self.m.SessionLocal() as db:
            self.assertEqual(list(db.scalars(select(self.m.Settlement).where(
                self.m.Settlement.order_id.in_([cash_id, operation_id])))), [])
            self.assertEqual(db.query(self.m.SettlementLine).count(), 0)
            self.assertEqual(db.query(self.m.RefundOrderLink).count(), 1)

    def test_history_read_and_unrelated_flush_do_not_materialize_records(self):
        # Core SQL represents a pre-hook historical row, bypassing ORM events.
        with self.m.engine.begin() as connection:
            connection.execute(self.m.Refund.__table__.insert().values(
                id='historical-hook-refund', refund_no='REF-HOOK-HISTORY', order_id=self.order_id,
                payment_id='paid-source', amount=5, status='completed', completed_at=self.m.now()))
        with self.m.SessionLocal() as db:
            db.get(self.m.Refund, 'historical-hook-refund')
            db.get(self.m.Order, self.order_id).buyer_name = 'Unrelated source update'
            db.flush()
            self.assertEqual(db.query(self.m.RefundOrderLink).count(), 0)
            self.assertEqual(db.query(self.m.Order).filter_by(business_type='refund_record').count(), 0)
            self.new_refund(db)
            db.flush()
            self.assertEqual(db.query(self.m.RefundOrderLink).count(), 1)
            self.assertIsNone(db.get(self.m.RefundOrderLink, 'historical-hook-refund'))

    def test_listener_ignores_other_engine(self):
        other = create_engine('sqlite://')
        self.addCleanup(other.dispose)
        self.m.Base.metadata.create_all(other)
        with Session(other) as db, patch.dict(self.m.refund_order_records, {
                'scan_pending': lambda _: self.fail('listener crossed engine boundary')}):
            self.new_refund(db, id='foreign-engine')
            db.flush()
            self.assertEqual(db.query(self.m.RefundOrderLink).count(), 0)

    def test_unique_link_constraints_are_enforced(self):
        with self.m.SessionLocal() as db:
            first = self.new_refund(db)
            second = self.new_refund(db, refund_no='REF-HOOK-SECOND')
            db.flush()
            first_record = self.record(db, first.id)
            for values in (
                dict(refund_id=first.id, order_id=self.order_id, source_order_id=self.order_id),
                dict(refund_id=second.id, order_id=first_record.id, source_order_id=self.order_id),
            ):
                with self.subTest(values=values), self.assertRaises(IntegrityError):
                    with db.begin_nested():
                        db.execute(self.m.RefundOrderLink.__table__.insert().values(**values))

    def test_completed_cash_record_cannot_generate_income_settlement_directly(self):
        with self.m.SessionLocal() as db:
            # Legacy sources are valid refund origins too. Use one so an
            # unrelated v1 snapshot validation cannot mask a missing guard.
            db.get(self.m.Order, self.order_id).snapshot_version = 0
            refund = self.new_refund(db, status='completed', completed_at=self.m.now())
            db.flush()
            record_id = self.record(db, refund.id).id
            db.commit()
        response = self.client.post(f'/api/settlements/generate/{record_id}',
            headers={'Authorization': 'Bearer ' + self.tokens['finance']}, json={})
        self.assertIn(response.status_code, (400, 409), response.text)
        with self.m.SessionLocal() as db:
            self.assertEqual(list(db.scalars(select(self.m.Settlement).where(
                self.m.Settlement.order_id == record_id))), [])


if __name__ == '__main__':
    unittest.main()
