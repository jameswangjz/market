"""Actual app models and helpers in isolated SQLite, without startup or workers."""
import unittest
from unittest.mock import patch

from sqlalchemy import event, func, select

import test_message_business_events as fixture


class DeliveryTaskAuditIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = fixture.load_isolated_main()

    @classmethod
    def tearDownClass(cls):
        fixture.BusinessRoutesHTTPTests.tearDownClass.__func__(cls)

    def setUp(self):
        m = self.m
        m.Base.metadata.create_all(m.engine)
        with m.SessionLocal.begin() as db:
            db.add(m.User(id='buyer', name='Buyer', email='buyer@example.invalid',
                          password_hash='unused'))
            db.add_all([m.Enterprise(id=key, name=key, credit_code=key)
                        for key in ('buyer-enterprise', 'provider-enterprise')])
            db.flush()
            for method in ('api', 'manual'):
                db.add(m.Product(id=method, enterprise_id='provider-enterprise',
                                 name=method, product_type='api' if method == 'api' else 'dataset',
                                 delivery_method=method))
            db.flush()
            for method in ('api', 'manual'):
                db.add(m.Order(id=method, order_no='ORD-' + method,
                    buyer_enterprise_id='buyer-enterprise', buyer_user_id='buyer',
                    provider_enterprise_id='provider-enterprise', product_id=method,
                    buyer_name='Buyer', product_name=method, main_status='paid',
                    payment_status='paid', delivery_status='not_started'))

    def tearDown(self):
        self.m.Base.metadata.drop_all(self.m.engine)

    def test_generated_identity_and_order_states_reach_hooks_before_audit(self):
        m = self.m
        for method, main_status, delivery_status in (
                ('api', 'fulfilling', 'in_delivery'), ('manual', 'paid', 'preparing')):
            with self.subTest(method=method):
                observed = []
                with m.SessionLocal() as db:
                    order = db.get(m.Order, method)
                    def observe(session, *_):
                        for row in session.new:
                            if isinstance(row, m.DeliveryTask):
                                observed.append((row.id, order.main_status, order.delivery_status))
                    event.listen(db, 'before_flush', observe)
                    task = m.create_delivery_task(db, order, actor='system')
                    self.assertRegex(task.id, r'^[0-9a-f]{32}$')
                    self.assertEqual(observed, [(None, main_status, delivery_status)])
                    task_id = task.id
                    pending = [row for row in db.new if isinstance(row, m.AuditLog)]
                    self.assertEqual(len(pending), 1)
                    self.assertEqual(pending[0].target_id, task_id)
                    db.commit()
                with m.SessionLocal() as db:
                    audit = db.scalar(select(m.AuditLog).where(
                        m.AuditLog.action == 'create_delivery_task', m.AuditLog.order_id == method))
                    self.assertEqual(audit.target_id, db.get(m.DeliveryTask, task_id).id)
                    self.assertEqual(audit.target_type, 'delivery_task')

    def test_audit_failure_rolls_back_task_audit_and_order_state(self):
        m = self.m
        real_audit = m.audit
        task_ids = []
        def fail_after_audit(db, *args, **kwargs):
            task_ids.append(args[3])
            real_audit(db, *args, **kwargs)
            db.flush()
            self.assertEqual(db.scalar(select(func.count()).select_from(m.DeliveryTask)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(m.AuditLog)), 1)
            raise RuntimeError('Audit transaction failure')
        with patch.object(m, 'audit', side_effect=fail_after_audit):
            with self.assertRaisesRegex(RuntimeError, 'Audit transaction failure'):
                with m.SessionLocal.begin() as db:
                    m.create_delivery_task(db, db.get(m.Order, 'api'), actor='system')
        self.assertRegex(task_ids[0], r'^[0-9a-f]{32}$')
        with m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(m.DeliveryTask)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(m.AuditLog)), 0)
            order = db.get(m.Order, 'api')
            self.assertEqual((order.main_status, order.delivery_status), ('paid', 'not_started'))


if __name__ == '__main__':
    unittest.main()
