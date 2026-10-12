"""Order scoped access entry points for API and SaaS deliveries."""
import os
import unittest
from unittest.mock import patch

import test_browser_sso as sso_fixture
import test_version_change_quotes as api_fixture


class UnifiedDeliveryAccessTests(unittest.TestCase):
    setUpClass = classmethod(api_fixture.VersionChangeQuoteTests.setUpClass.__func__)
    tearDownClass = classmethod(api_fixture.VersionChangeQuoteTests.tearDownClass.__func__)
    tearDown = api_fixture.VersionChangeQuoteTests.tearDown
    request = api_fixture.VersionChangeQuoteTests.request
    prepare = api_fixture.VersionChangeQuoteTests.prepare
    create = api_fixture.VersionChangeQuoteTests.create
    deliver = api_fixture.VersionChangeQuoteTests.deliver
    counts = api_fixture.VersionChangeQuoteTests.counts

    def setUp(self):
        api_fixture.VersionChangeQuoteTests.setUp(self)

    def test_api_access_is_read_only_and_does_not_expose_credential_data(self):
        oid, _ = self.prepare(method='api', future=False)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            order.main_status, order.delivery_status = 'completed', 'accepted'
            route = self.m.ApiGatewayRoute(product_id='product', route_key='fixture-route',
                upstream_url='https://private-upstream.invalid', version=order.product_version_code,
                status='active', rate_limit_per_minute=99, daily_quota=999)
            db.add(route)
            db.flush()
            db.add(self.m.ApiCredential(route_id=route.id, enterprise_id='buyer_tenant',
                key_hash='private-hash-fixture', apisix_consumer_name='fixture-consumer', status='active',
                expires_at=self.m.now()+api_fixture.timedelta(days=1)))
            db.commit()
        before = self.counts()
        with patch.dict(os.environ, {'API_GATEWAY_PUBLIC_URL': 'https://gateway.example.invalid'}):
            response = self.client.get(f'/api/orders/{oid}/delivery-access',
                headers={'Authorization': 'Bearer '+self.tokens['buyer']})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            body = response.json()
            self.assertTrue(body['ready'], body['reason'])
            self.assertEqual(body['api']['shared_scope'], 'enterprise')
            self.assertEqual(body['api']['gateway_base_path'], '/gateway/fixture-route')
            self.assertEqual(body['api']['gateway_url'], 'https://gateway.example.invalid/gateway/fixture-route')
            self.assertEqual(body['api']['version_code'], 'v1')
            self.assertEqual(set(body['api']['limits']), {
                'rate_limit_per_minute', 'daily_quota', 'monthly_quota', 'quota_amount'})
        for value in ('', 'javascript:alert(1)', 'https://user:pass@gateway.example.invalid',
                'https://gateway.example.invalid/path', 'https://gateway.example.invalid?q=private'):
            with self.subTest(public_url=value), patch.dict(os.environ, {'API_GATEWAY_PUBLIC_URL': value}):
                response = self.client.get(f'/api/orders/{oid}/delivery-access',
                    headers={'Authorization': 'Bearer '+self.tokens['buyer']})
                body = response.json()
                self.assertFalse(body['ready'])
                self.assertEqual(body['reason'], '公共API网关地址未配置或无效')
                self.assertEqual(body['api']['gateway_url'], '')
                self.assertEqual(body['api']['gateway_base_path'], '/gateway/fixture-route')
        self.assertNotIn('private-hash', response.text)
        self.assertNotIn('private-upstream', response.text)
        self.assertEqual(self.counts(), before)

    def test_api_access_rejects_unsynced_credentials_mismatched_method_and_bad_route_key(self):
        oid, _ = self.prepare(method='api', future=False)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            order.main_status, order.delivery_status = 'completed', 'accepted'
            route = self.m.ApiGatewayRoute(product_id='product', route_key='safe-route',
                upstream_url='https://private-upstream.invalid', version=order.product_version_code,
                status='active')
            db.add(route)
            db.flush()
            credential = self.m.ApiCredential(route_id=route.id, enterprise_id='buyer_tenant',
                key_hash='unsynced-key-hash', status='active')
            db.add(credential)
            db.commit()
            rid, cid = route.id, credential.id
        with patch.dict(os.environ, {'API_GATEWAY_PUBLIC_URL': 'https://gateway.example.invalid'}):
            path = f'/api/orders/{oid}/delivery-access'
            self.assertFalse(self.request('GET', path)['ready'])
            with self.m.SessionLocal() as db:
                db.get(self.m.ApiCredential, cid).apisix_consumer_name = 'fixture-consumer'
                db.get(self.m.ApiGatewayRoute, rid).route_key = 'unsafe/route'
                db.commit()
            invalid = self.request('GET', path)
            self.assertFalse(invalid['ready'])
            self.assertEqual(invalid['api']['gateway_base_path'], '')
            self.assertEqual(invalid['api']['gateway_url'], '')
            with self.m.SessionLocal() as db:
                db.get(self.m.ApiGatewayRoute, rid).route_key = 'safe-route'
                combo = db.scalar(self.m.select(self.m.subscription_terms['Combination']).where(
                    self.m.subscription_terms['Combination'].enterprise_id == 'buyer_tenant',
                    self.m.subscription_terms['Combination'].product_id == 'product'))
                combo.delivery_method = 'model_api'
                db.commit()
            mismatch = self.request('GET', path)
            self.assertFalse(mismatch['ready'])
            self.assertIn('API订阅', mismatch['reason'])

    def test_api_credential_management_uses_current_entitlement_route_read_only(self):
        oid, _ = self.prepare(method='api', future=False)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            old_route = self.m.ApiGatewayRoute(product_id='product', route_key='historical-route',
                upstream_url='https://old-upstream.invalid', version=order.product_version_code,
                status='active')
            current_route = self.m.ApiGatewayRoute(product_id='product', route_key='current-route',
                upstream_url='https://current-upstream.invalid', version='v2', status='active')
            db.add_all([old_route, current_route])
            db.commit()
            route_ids = (old_route.id, current_route.id)
        before = self.counts()
        with self.m.SessionLocal() as db:
            access_rows_before = (db.query(self.m.ApiGatewayRoute).count(),
                db.query(self.m.ApiCredential).count())
        entitlement = {'version_code': 'v2'}
        with patch.dict(self.m.subscription_terms,
                {'current_entitlement': lambda db, enterprise_id, product_id: entitlement}), \
                patch.object(self.m, 'refresh_enterprise_api_credentials') as refresh:
            result = self.request('GET', f'/api/orders/{oid}/api-credentials')
        refresh.assert_called_once()
        self.assertEqual(result['route_key'], 'current-route')
        self.assertEqual(result['items'], [])
        self.assertEqual(self.counts(), before)
        with self.m.SessionLocal() as db:
            self.assertEqual((db.query(self.m.ApiGatewayRoute).count(),
                db.query(self.m.ApiCredential).count()), access_rows_before)
            self.assertEqual([db.get(self.m.ApiGatewayRoute, rid).status for rid in route_ids],
                ['active', 'active'])



class UnifiedSaaSDeliveryAccessTests(sso_fixture.BrowserSSOTests):
    def test_saas_order_access_and_launch_reuse_application_policy(self):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.oid)
            order.main_status, order.delivery_status = 'completed', 'accepted'
            db.get(self.m.Product, 'product').application_url = 'https://saas.example.invalid/app'
            db.commit()
        path = f'/api/orders/{self.oid}/delivery-access'
        response = self.client.get(path, headers={'Authorization': 'Bearer '+self.tokens['buyer']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertTrue(response.json()['ready'], response.json()['reason'])
        launch = self.client.post(f'/api/orders/{self.oid}/delivery-launch',
            headers={'Authorization': 'Bearer '+self.tokens['buyer']})
        self.assertEqual(launch.status_code, 200, launch.text)
        self.assertEqual(launch.headers['Cache-Control'], 'no-store')
        self.assertIn('enterprise_id=buyer_tenant', launch.json()['launch_url'])
        denied = self.client.post(f'/api/orders/{self.oid}/delivery-launch',
            headers={'Authorization': 'Bearer '+self.tokens['member']})
        self.assertEqual(denied.status_code, 403)

    def test_historical_order_remains_accessible_after_subscription_version_switch(self):
        from datetime import timedelta

        with self.m.SessionLocal() as db:
            combo = db.scalar(self.m.select(self.m.subscription_terms['Combination']).where(
                self.m.subscription_terms['Combination'].enterprise_id == 'buyer_tenant',
                self.m.subscription_terms['Combination'].product_id == 'product'))
            subscription = db.get(self.m.SaaSSubscription, self.sid)
            db.get(self.m.Product, 'product').application_url = 'https://saas.example.invalid/app'
            old_term = db.scalar(self.m.select(self.m.subscription_terms['Term']).where(
                self.m.subscription_terms['Term'].order_id == self.oid))
            combo_id, generation = combo.id, combo.generation
            start_month, months = old_term.start_month, old_term.months
            starts_at, ends_at = old_term.starts_at, old_term.ends_at
            subscription.version_id = 'v2'
            combo.version_id, combo.version_code = 'v2', 'v2'
            db.commit()
        newer_id = self.create(months, version='v2', method='tenant_access')
        with self.m.SessionLocal() as db:
            old_order = db.get(self.m.Order, self.oid)
            old_order.main_status, old_order.delivery_status = 'completed', 'accepted'
            newer_order = db.get(self.m.Order, newer_id)
            newer_order.main_status, newer_order.delivery_status = 'completed', 'accepted'
            newer_order.subscription_id = self.sid
            term = self.m.subscription_terms['Term'](order_id=newer_order.id,
                combination_id=combo_id, generation=generation, version_id='v2', version_code='v2',
                start_month=start_month, months=months,
                starts_at=starts_at, ends_at=ends_at, status='active')
            db.add(term)
            db.commit()
        response = self.client.get(f'/api/orders/{self.oid}/delivery-access',
            headers={'Authorization': 'Bearer '+self.tokens['buyer']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['ready'], response.json()['reason'])

    def test_get_readiness_and_post_share_application_url_validation(self):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.oid)
            order.main_status, order.delivery_status = 'completed', 'accepted'
            db.commit()
        for unsafe in ('javascript:alert(1)', r'https://saas.example.invalid\\evil', 'https://user@saas.example.invalid'):
            with self.subTest(url=unsafe):
                with self.m.SessionLocal() as db:
                    db.get(self.m.Product, 'product').application_url = unsafe
                    db.commit()
                response = self.client.get(f'/api/orders/{self.oid}/delivery-access',
                    headers={'Authorization': 'Bearer '+self.tokens['buyer']})
                self.assertFalse(response.json()['ready'])
                self.assertEqual(response.json()['reason'], '应用访问地址无效')
                launch = self.client.post(f'/api/orders/{self.oid}/delivery-launch',
                    headers={'Authorization': 'Bearer '+self.tokens['buyer']})
                self.assertEqual(launch.status_code, 409)


if __name__ == '__main__':
    unittest.main()
