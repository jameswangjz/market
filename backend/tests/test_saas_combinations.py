import unittest
from unittest.mock import patch
from sqlalchemy import select, func
import test_message_business_events as fixtures


class SaaSCombinationTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request

    def setUp(self):
        fixtures.BusinessRoutesHTTPTests.setUp(self)
        with self.m.SessionLocal() as db:
            product = db.get(self.m.Product, 'product')
            product.product_type, product.delivery_method = 'saas', 'tenant_access'
            product.application_url = 'https://saas.example.com/'
            product.integration_api_url = 'https://saas.example.com/isv.php'
            db.add(self.m.SaaSProductVersion(id='sv1', product_id='product', version_code='v1', name='V1', monthly_price=100, cost=20, status='active'))
            db.add(self.m.SaaSProductVersion(id='sv2', product_id='product', version_code='v2', name='V2', monthly_price=150, cost=30, status='active'))
            db.commit()
        self.catalog = patch.dict(self.m.storefront, {'versions_for': lambda db, product: [{'id': 'sv1'}, {'id': 'sv2'}]})
        self.catalog.start()
        self.addCleanup(self.catalog.stop)

    def order(self, target='', version='sv1', expected=200):
        return self.request('POST', '/api/orders', json={'product_id': 'product', 'product_version_id': version,
            'buyer_enterprise_id': 'buyer_tenant', 'subscription_months': 3, 'saas_subscription_id': target}, expected=expected)

    def legacy(self, sid, enterprise='buyer_tenant', status='active'):
        with self.m.SessionLocal() as db:
            db.add(self.m.SaaSSubscription(id=sid, enterprise_id=enterprise, product_id='product',
                version_id='sv1', billing_cycle='monthly', status=status, external_tenant_id=sid))
            db.commit()

    def test_repeat_purchase_reserves_one_tenant_without_payment_or_provider_call(self):
        with patch.object(self.m, 'saas_call', side_effect=AssertionError('must not provision')):
            one, two = self.order(), self.order()
        self.assertEqual(one['subscription_id'], two['subscription_id'])
        self.assertNotEqual(one['id'], two['id'])
        self.assertEqual(one['payment_status'], 'unpaid')
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(self.m.SaaSSubscription)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(self.m.SaaSCombinationBinding)), 1)
            sub = db.get(self.m.SaaSSubscription, one['subscription_id'])
            self.assertIsNone(sub.starts_at)
            self.assertIsNone(sub.expires_at)
            self.assertEqual(sub.external_tenant_id, '')

    def test_transaction_rollback_removes_reservation(self):
        with self.m.SessionLocal() as db:
            user = db.get(self.m.User, 'buyer')
            quoted = self.m.order_economics['quote'](db, user, self.m.OrderBody(product_id='product', product_version_id='sv1', buyer_enterprise_id='buyer_tenant'))
            self.m.create_quoted_order(db, user, quoted)
            db.rollback()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(self.m.SaaSCombinationBinding)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(self.m.SaaSSubscription)), 0)

    def test_single_history_reused(self):
        self.legacy('historical')
        self.assertEqual(self.order()['subscription_id'], 'historical')

    def test_multiple_history_requires_explicit_target_and_preserves_others(self):
        self.legacy('old1'); self.legacy('old2')
        self.order(expected=409)
        chosen = self.order('old2')
        self.assertEqual(chosen['subscription_id'], 'old2')
        self.assertEqual(self.order()['subscription_id'], 'old2')
        self.order('old1', expected=409)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(self.m.SaaSSubscription)), 2)

    def test_cross_tenant_and_wrong_version_rejected(self):
        self.legacy('other', enterprise='provider_tenant')
        self.order('other', expected=409)
        self.order()
        self.order(version='sv2', expected=409)

    def test_closed_tenant_not_replaced(self):
        self.legacy('closed', status='closed')
        self.order(expected=409)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(self.m.SaaSSubscription)), 1)

    def test_target_listing_is_buyer_enterprise_scoped(self):
        self.legacy('old1'); self.legacy('old2')
        response = self.request('GET', '/api/products/product/saas-subscription-targets?enterprise_id=buyer_tenant')
        self.assertTrue(response['selection_required'])
        self.assertEqual({item['id'] for item in response['items']}, {'old1', 'old2'})
        self.request('GET', '/api/products/product/saas-subscription-targets?enterprise_id=provider_tenant', expected=403)

    def test_legacy_create_guard_prevents_new_independent_tenant(self):
        self.order()
        with self.m.SessionLocal() as db:
            with self.assertRaises(self.m.HTTPException) as caught:
                self.m.saas_combinations['guard_legacy_create'](db, 'buyer_tenant', 'product')
            self.assertEqual(caught.exception.status_code, 409)


if __name__ == '__main__':
    unittest.main()
