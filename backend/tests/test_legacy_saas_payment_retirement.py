"""Retired inline-paid SaaS routes must direct buyers to canonical orders.

Task regression: legacy OPEN, RENEW, and CHANGE must stop before provider I/O
or financial writes, including the formerly positive-paid downgrade path.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import event, func, select

import test_message_business_events as fixtures


class LegacySaaSPaymentRetirementTests(unittest.TestCase):
    Fixture = fixtures.BusinessRoutesHTTPTests
    setUpClass = classmethod(Fixture.setUpClass.__func__)
    tearDownClass = classmethod(Fixture.tearDownClass.__func__)
    tearDown = Fixture.tearDown
    request = Fixture.request

    def setUp(self):
        self.Fixture.setUp(self)
        instant = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
        clock = patch.object(self.m, 'now', return_value=instant)
        clock.start()
        self.addCleanup(clock.stop)
        with self.m.SessionLocal() as db:
            db.get(self.m.Enterprise, 'buyer_tenant').verification_status = 'verified'
            product = db.get(self.m.Product, 'product')
            product.product_type, product.delivery_method = 'saas', 'tenant_access'
            for version_id, price in (('current', 100), ('higher', 150), ('lower', 50)):
                db.add(self.m.SaaSProductVersion(id=version_id, product_id='product',
                    version_code=version_id, name=version_id, monthly_price=price,
                    annual_price=price * 12, cost=10, status='active'))
            db.add(self.m.SaaSIntegrationConfig(product_id='product', status='active',
                base_url='https://provider.example.invalid', operation_path='/isv.php',
                client_id='fixture-client', client_secret='fixture-secret'))
            db.flush()
            db.add(self.m.SaaSSubscription(id='legacy', enterprise_id='buyer_tenant',
                product_id='product', version_id='current', billing_cycle='monthly',
                status='active', external_tenant_id='legacy-tenant', created_by='buyer',
                starts_at=instant - timedelta(days=10), expires_at=instant + timedelta(days=20)))
            db.commit()

    def facts(self):
        with self.m.SessionLocal() as db:
            subscription = db.get(self.m.SaaSSubscription, 'legacy')
            state = {column.name: getattr(subscription, column.name)
                     for column in self.m.SaaSSubscription.__table__.columns}
            models = (self.m.Order, self.m.Payment, self.m.Refund, self.m.SaaSOperation,
                      self.m.SaaSSubscription, self.m.OrderStateLog, self.m.AuditLog,
                      self.m.DeliveryTask, self.Message, self.Receipt, self.Outbox)
            return state, tuple(db.scalar(select(func.count()).select_from(model)) for model in models)

    def assert_retired(self, path, body):
        before = self.facts()
        writes = []

        def observe(connection, cursor, statement, parameters, context, many):
            if statement.lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')):
                writes.append(statement)

        event.listen(self.m.engine, 'before_cursor_execute', observe)
        try:
            with patch.object(self.m, 'execute_saas_operation') as operation, \
                    patch.object(self.m, 'saas_call') as remote, \
                    patch.object(self.m, 'validated_http_request') as http, \
                    patch.object(self.m, 'add_saas_order') as create_order:
                for _ in range(2):
                    result = self.request('POST', path, actor='buyer', expected=409, json=body)
                    self.assertIn('订单', result['detail'])
                    self.assertIn('不再隐式完成支付', result['detail'])
                operation.assert_not_called()
                remote.assert_not_called()
                http.assert_not_called()
                create_order.assert_not_called()
        finally:
            event.remove(self.m.engine, 'before_cursor_execute', observe)
        self.assertEqual(writes, [])
        self.assertEqual(self.facts(), before)

    def test_legacy_open_rejected_before_provisioning_or_payment(self):
        self.assert_retired('/api/products/product/saas-subscriptions',
                            {'version_id': 'current', 'billing_cycle': 'monthly'})

    def test_legacy_renew_rejected_without_extending_term_or_creating_payment(self):
        self.assert_retired('/api/saas-subscriptions/legacy/renew', {'billing_cycle': 'monthly'})

    def test_legacy_upgrade_and_downgrade_rejected_before_financial_or_remote_effects(self):
        for target in ('higher', 'lower'):
            with self.subTest(target=target):
                self.assert_retired('/api/saas-subscriptions/legacy/change-version',
                                    {'version_id': target, 'billing_cycle': 'monthly'})

    def test_buyer_authorization_remains_required_before_retirement_response(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'buyer').verified_status = 'pending'
            db.commit()
        before = self.facts()
        for path, body in (
            ('/api/products/product/saas-subscriptions', {'version_id': 'current'}),
            ('/api/saas-subscriptions/legacy/renew', {}),
            ('/api/saas-subscriptions/legacy/change-version', {'version_id': 'lower'}),
        ):
            with self.subTest(path=path):
                self.request('POST', path, actor='buyer', expected=403, json=body)
        self.assertEqual(self.facts(), before)


if __name__ == '__main__':
    unittest.main()
