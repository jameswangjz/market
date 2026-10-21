"""Payment/verification facts on isolated SQLite using real production routes."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select

import test_online_delivery as fixture


class OrderSlaContractTests(unittest.TestCase):
    setUpClass = classmethod(fixture.OnlineDeliveryTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.OnlineDeliveryTests.tearDownClass.__func__)
    tearDown = fixture.OnlineDeliveryTests.tearDown
    request = fixture.OnlineDeliveryTests.request
    create_order = fixture.OnlineDeliveryTests.create_order
    pay = fixture.OnlineDeliveryTests.pay
    task_id = fixture.OnlineDeliveryTests.task_id
    process = fixture.OnlineDeliveryTests.process
    counts = fixture.OnlineDeliveryTests.counts

    def setUp(self):
        fixture.OnlineDeliveryTests.setUp(self)
        self.at = datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)
        clock = patch.object(self.m, 'now', side_effect=lambda: self.at)
        clock.start()
        self.addCleanup(clock.stop)
        self.Contract = self.m.order_sla_contracts['Contract']
        with self.m.SessionLocal() as db:
            db.add_all([self.m.ServiceLevel(id=code, code=code, name=code,
                initial_response_minutes=720 if code == 'strategic' else 2880,
                urgent_initial_response_minutes=360 if code == 'strategic' else 1440,
                status='active') for code in ('strategic', 'standard')])
            db.flush()
            db.add(self.m.ServiceLevelAssignment(service_level_id='strategic', enterprise_id='buyer_tenant',
                effective_at=self.at - timedelta(days=1)))
            db.commit()

    def contract(self, order_id):
        with self.m.SessionLocal() as db:
            return self.m.order_sla_contracts['get_contract'](db, order_id)

    def test_first_payment_freezes_real_rules_deadline_and_source_and_repeats_unchanged(self):
        order_id = self.create_order()
        self.assertIsNone(self.contract(order_id))
        self.pay(order_id)
        row = self.contract(order_id)
        snapshot = json.loads(row.snapshot_json)
        self.assertEqual(snapshot['service_level_code'], 'strategic')
        self.assertIn('legacy_level_config', snapshot)
        self.assertEqual(row.deadline_at - row.paid_at, timedelta(minutes=4))
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Payment, row.source_payment_id).order_id, order_id)
        before = (row.id, row.snapshot_json, row.paid_at, row.deadline_at)
        self.at += timedelta(days=1)
        self.pay(order_id)
        after = self.contract(order_id)
        self.assertEqual((after.id, after.snapshot_json, after.paid_at, after.deadline_at), before)

    def test_matrix_revision_changes_only_new_payment_deadlines(self):
        old_id = self.create_order()
        self.pay(old_id)
        old = self.contract(old_id)
        self.request('PATCH', '/api/sla/service-levels/strategic/matrix', actor='ops', json={
            'expected_version': 0, 'metrics': {'delivery': {'target': 6, 'warning': 5}}})
        new_id = self.create_order()
        self.pay(new_id)
        new = self.contract(new_id)
        self.assertEqual(old.deadline_at - old.paid_at, timedelta(minutes=4))
        self.assertEqual(new.deadline_at - new.paid_at, timedelta(minutes=6))
        self.assertEqual(self.contract(old_id).snapshot_json, old.snapshot_json)
        self.assertEqual(json.loads(new.snapshot_json)['metrics']['delivery']['warning_seconds'], 300)

    def test_real_freeze_snapshot_integrates_with_order_assessment_and_never_follows_tier_change(self):
        order_id = self.create_order()
        self.pay(order_id)
        row = self.contract(order_id)
        assessment = importlib.import_module(self.m.__package__ + '.sla_order_assessment')
        metrics = importlib.import_module(self.m.__package__ + '.sla_metrics')
        measurement = metrics.MetricResult(value=97, status='met', sample_count=100, coverage_percent=100)
        result = assessment.assess_order_metric(row.snapshot_json, 'availability', measurement,
            buyer_enterprise_id='buyer_tenant', source_available=True)
        self.assertEqual((result.frozen_tier, result.status), ('strategic', 'breached'))
        self.request('POST', '/api/sla/service-level-assignments', actor='ops', json={
            'enterprise_id': 'buyer_tenant', 'service_level_id': 'standard', 'reason': 'New contracts only'})
        newer = self.create_order()
        self.pay(newer)
        self.assertEqual(json.loads(self.contract(newer).snapshot_json)['service_level_code'], 'standard')
        newer_result = assessment.assess_order_metric(self.contract(newer).snapshot_json, 'availability', measurement,
            buyer_enterprise_id='buyer_tenant', source_available=True)
        self.assertEqual((newer_result.frozen_tier, newer_result.status, newer_result.value), ('standard', 'met', 97))
        self.assertEqual(self.contract(order_id).snapshot_json, row.snapshot_json)
        repeated = assessment.assess_order_metric(row.snapshot_json, 'availability', measurement,
            buyer_enterprise_id='buyer_tenant', source_available=True)
        self.assertEqual(repeated, result)

    def test_legacy_paid_repeat_never_backfills_from_updated_at(self):
        order_id = self.create_order()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, order_id)
            order.payment_status, order.paid_amount = 'paid', order.amount
            payment = db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == order_id))
            payment.status, payment.paid_at = 'paid', None
            payment.updated_at = self.at - timedelta(days=100)
            db.commit()
        self.pay(order_id)
        self.assertIsNone(self.contract(order_id))
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == order_id)).paid_at)

    def test_unpaid_timestamp_is_not_evidence_of_a_prior_effective_payment(self):
        order_id = self.create_order()
        with self.m.SessionLocal() as db:
            payment = db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == order_id))
            payment.paid_at = self.at - timedelta(days=100)
            db.commit()
        self.pay(order_id)
        self.assertEqual(self.contract(order_id).paid_at.replace(tzinfo=timezone.utc), self.at)

    def test_offline_and_pure_funding_do_not_create_delivery_samples(self):
        for method, business in [('consulting', ''), ('file', 'version_change_payment'),
                                 ('tenant_access', 'upgrade'), ('tenant_access', 'downgrade')]:
            order_id = self.create_order()
            with self.m.SessionLocal() as db:
                order = db.get(self.m.Order, order_id)
                order.delivery_method_snapshot, order.business_type = method, business
                order.payment_status, order.paid_amount = 'paid', order.amount
                payment = db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == order_id))
                payment.status, payment.paid_at = 'paid', self.at
                self.assertIsNone(self.m.order_sla_contracts['freeze_payment'](
                    db, order, payment, previously_paid=False))
                db.rollback()
            self.assertIsNone(self.contract(order_id))

    def test_contract_audit_and_later_notification_failures_roll_back_payment(self):
        order_id = self.create_order()
        real_audit = self.m.audit
        def fail(db, actor, action, *args, **kwargs):
            if action == 'freeze_order_sla_contract':
                raise RuntimeError('Contract audit failure')
            return real_audit(db, actor, action, *args, **kwargs)
        for name, side_effect in [('audit', fail), ('notify_order_event', RuntimeError('Late outbox failure'))]:
            before = self.counts()
            with patch.object(self.m, name, side_effect=side_effect):
                self.request('POST', f'/api/orders/{order_id}/transition', actor='platform', expected=500, json={'action': 'confirm_payment'})
            self.assertEqual(self.counts(), before)
            self.assertIsNone(self.contract(order_id))
            with self.m.SessionLocal() as db:
                self.assertEqual(db.get(self.m.Order, order_id).payment_status, 'unpaid')
                payment = db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == order_id))
                self.assertEqual((payment.status, payment.paid_at), ('unpaid', None))
        self.pay(order_id)

    def test_only_real_verification_success_records_first_time_and_retry_cannot_overwrite(self):
        order_id = self.create_order()
        self.pay(order_id)
        task = self.task_id(order_id)
        self.storage.stat_object.side_effect = RuntimeError('Unavailable')
        self.process(task)
        self.assertIsNone(self.contract(order_id).verified_at)
        self.storage.stat_object.side_effect = None
        self.at += timedelta(seconds=60)
        self.process(task)
        first = self.contract(order_id)
        self.assertEqual(first.source_delivery_task_id, task)
        self.assertIsNotNone(first.verified_at)
        self.at += timedelta(days=1)
        self.process(task)
        self.assertEqual(self.contract(order_id).verified_at, first.verified_at)

    def test_success_fact_rolls_back_with_delivery_audit_outbox_failure(self):
        order_id = self.create_order()
        self.pay(order_id)
        task = self.task_id(order_id)
        with patch.object(self.m, 'notify_order_event', side_effect=RuntimeError('Delivery outbox failure')):
            self.request('POST', f'/api/delivery-tasks/{task}/process', actor='ops', expected=500, json={'success': True})
        self.assertIsNone(self.contract(order_id).verified_at)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.DeliveryTask, task).status, 'in_delivery')
        self.process(task)
        self.assertIsNotNone(self.contract(order_id).verified_at)

    def test_old_paid_delivery_does_not_create_historical_contract_or_success(self):
        order_id = self.create_order()
        self.pay(order_id)
        with self.m.SessionLocal() as db:
            db.query(self.Contract).filter_by(order_id=order_id).delete()
            db.commit()
        self.process(self.task_id(order_id))
        self.assertIsNone(self.contract(order_id))

    def test_deadline_month_query_includes_overdue_uncompleted_and_scopes(self):
        order_id = self.create_order()
        self.pay(order_id)
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        with self.m.SessionLocal() as db:
            query = self.m.order_sla_contracts['evaluation_query']
            rows = list(db.scalars(query(period_start=start, period_end=start + timedelta(days=30),
                product_id='product', buyer_enterprise_id='buyer_tenant')))
            self.assertEqual([row.order_id for row in rows], [order_id])
            self.assertIsNone(rows[0].verified_at)
            self.assertEqual(list(db.scalars(query(period_start=start - timedelta(days=31), period_end=start))), [])
            self.assertEqual(list(db.scalars(query(period_start=start, period_end=start + timedelta(days=30),
                buyer_enterprise_id='other_tenant'))), [])

    def test_buyer_outputs_keep_cost_and_credentials_private(self):
        order_id = self.create_order()
        self.pay(order_id)
        result = self.request('GET', '/api/orders')
        text = json.dumps(result)
        for field in ('snapshot_json', 'unit_cost_snapshot', 'economic_snapshot_json', 'client_secret', 'secret_key'):
            self.assertNotIn(field, text)
        self.request('GET', f'/api/orders/{order_id}/delivery-access', actor='outsider', expected=403)

    def legacy_context(self, db):
        product = db.get(self.m.Product, 'product')
        product.product_type, product.delivery_method = 'saas', 'tenant_access'
        version = self.m.SaaSProductVersion(id='saas-version', product_id='product', version_code='v1',
                                          name='Legacy v1', monthly_price=100, status='active')
        db.add(version)
        db.flush()
        subscription = self.m.SaaSSubscription(enterprise_id='buyer_tenant', product_id='product',
            version_id=version.id, created_by='buyer', billing_cycle='monthly')
        db.add(subscription)
        db.flush()
        return subscription, product, version

    def test_legacy_saas_purchase_and_renew_new_payments_are_not_missed(self):
        with self.m.SessionLocal() as db:
            subscription, product, version = self.legacy_context(db)
            orders = [self.m.add_saas_order(db, subscription, product, version, Decimal('100'), kind, paid=True)
                      for kind in ('purchase', 'renew')]
            order_ids = [order.id for order in orders]
            db.commit()
        for order_id in order_ids:
            row = self.contract(order_id)
            self.assertIsNotNone(row)
            self.assertEqual(row.delivery_method, 'tenant_access')
            self.assertIsNone(row.verified_at)
            with self.m.SessionLocal() as db:
                order = db.get(self.m.Order, order_id)
                payment = db.get(self.m.Payment, row.source_payment_id)
                self.assertEqual((order.amount, order.paid_amount, payment.amount),
                                 (Decimal('100.00'), Decimal('100.00'), Decimal('100.00')))
                self.assertEqual((order.payment_status, payment.status), ('paid', 'paid'))
                self.assertEqual(payment.order_id, order_id)
                self.assertEqual(payment.paid_at, row.paid_at)

    def test_legacy_saas_payment_and_contract_audit_failure_share_transaction(self):
        with self.m.SessionLocal() as db:
            context = self.legacy_context(db)
            db.commit()
            with patch.object(self.m, 'audit', side_effect=RuntimeError('Audit unavailable')):
                with self.assertRaises(RuntimeError):
                    self.m.add_saas_order(db, *context, Decimal('100'), 'purchase', paid=True)
            db.rollback()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.Order).count(), 0)
            self.assertEqual(db.query(self.m.Payment).count(), 0)
            self.assertEqual(db.query(self.Contract).count(), 0)


if __name__ == '__main__':
    unittest.main()
