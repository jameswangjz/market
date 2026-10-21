"""Shared HTTP configuration must be enforced by registration and callbacks."""
import unittest
import importlib
from datetime import timedelta

import test_message_business_events as fixture
import test_oidc_provider as oidc_fixture


class ApplicationAddressPolicyTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixture.BusinessRoutesHTTPTests.setUp
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown
    request = fixture.BusinessRoutesHTTPTests.request

    def test_default_disabled_enable_disable_and_non_admin_permission(self):
        self.assertEqual(self.request('GET', '/api/settings/application-address-policy', 'provider'),
            {'allow_http': False})
        self.request('PATCH', '/api/admin/settings/application-address-policy', 'provider', 403,
            json={'allow_http': True})
        self.request('PATCH', '/api/admin/settings/application-address-policy', 'platform', 200,
            json={'allow_http': True})
        self.assertEqual(self.request('GET', '/api/settings/application-address-policy', 'provider'),
            {'allow_http': True})
        with self.m.SessionLocal() as db:
            policy = self.m.application_address_policy
            self.assertEqual(policy['validate_url']('http://saas.example.invalid/api', db),
                'http://saas.example.invalid/api')
        self.request('PATCH', '/api/admin/settings/application-address-policy', 'platform', 200,
            json={'allow_http': False})
        with self.m.SessionLocal() as db:
            self.assertFalse(self.m.application_address_policy['allow_http'](db))

    def test_http_permission_never_allows_malformed_or_unsafe_callback_syntax(self):
        validate_redirect_uris = importlib.import_module(
            self.m.__package__ + '.registration_config').validate_redirect_uris
        uri = 'http://saas.example.invalid/callback'
        self.assertEqual(validate_redirect_uris([uri], allow_http=True), [uri])
        with self.assertRaises(ValueError):
            validate_redirect_uris([uri], allow_http=False)
        for value in ('http://user:secret@saas.example.invalid/callback',
                'http://saas.example.invalid/callback#fragment',
                'http://saas.example.invalid/*', 'http://saas.example.invalid:0/callback'):
            with self.assertRaises(ValueError):
                validate_redirect_uris([value], allow_http=True)

    def test_http_product_and_oidc_definition_follow_dynamic_setting(self):
        urls = {'upstream_url': 'http://saas.example.invalid/api',
            'application_url': 'http://saas.example.invalid/app',
            'integration_api_url': 'http://saas.example.invalid/manager'}
        for field, value in urls.items():
            with self.assertRaises(ValueError):
                self.m.ProductBody(name='HTTP', product_type='saas', **{field: value})
        self.request('PATCH', '/api/admin/settings/application-address-policy', 'platform',
            json={'allow_http': True})
        product = self.m.ProductBody(name='HTTP', product_type='saas', delivery_method='tenant_access', **urls,
            oidc_redirect_uris=['http://saas.example.invalid/callback'])
        self.assertEqual(product.application_url, urls['application_url'])
        self.request('PATCH', '/api/admin/settings/application-address-policy', 'platform',
            json={'allow_http': False})
        with self.assertRaises(ValueError):
            self.m.ProductBody(name='HTTP', product_type='saas',
                oidc_redirect_uris=['http://saas.example.invalid/callback'])


class ApplicationListingScopeTests(unittest.TestCase):
    setUpClass = classmethod(oidc_fixture.OIDCProviderTests.setUpClass.__func__)
    tearDownClass = classmethod(oidc_fixture.OIDCProviderTests.tearDownClass.__func__)
    tearDown = oidc_fixture.OIDCProviderTests.tearDown
    request = oidc_fixture.OIDCProviderTests.request
    prepare = oidc_fixture.OIDCProviderTests.prepare
    create = oidc_fixture.OIDCProviderTests.create
    deliver = oidc_fixture.OIDCProviderTests.deliver

    def setUp(self):
        oidc_fixture.OIDCProviderTests.setUp(self)
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').application_url = 'https://saas.example.invalid/app'
            db.commit()

    def listing(self, enterprise_id='buyer_tenant', expected=200):
        return self.request('GET', '/api/applications', 'member', expected,
            params={'enterprise_id': enterprise_id})

    def launch(self, expected=200):
        return self.request('POST', f'/api/applications/{self.sid}/launch', 'member', expected)

    def test_authorized_ordinary_non_purchaser_lists_and_launches_without_secrets(self):
        with self.m.SessionLocal() as db:
            self.assertNotEqual(db.get(self.m.Order, self.oid).buyer_user_id, 'member')
        response = self.listing()
        self.assertEqual(len(response['items']), 1)
        item = response['items'][0]
        self.assertEqual((item['subscription_id'], item['enterprise_id']), (self.sid, 'buyer_tenant'))
        self.assertTrue(item['available'])
        self.assertFalse(item['is_enterprise_admin'])
        launch = self.launch()
        self.assertEqual(launch, {'launch_url': 'https://saas.example.invalid/app?product_id=product&enterprise_id=buyer_tenant'})
        for result in (response, launch):
            self.assertNotIn(self.secret, str(result))
            for forbidden in ('secret', 'cost', 'upstream', 'password', 'management', 'access_token'):
                self.assertNotIn(forbidden, str(result).lower())

    def test_explicit_scope_filters_but_legacy_clients_keep_all_active_memberships(self):
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.Membership(user_id='member', enterprise_id='other_tenant', role='member'))
            db.add(m.ApplicationAccessGrant(enterprise_id='other_tenant', product_id='product',
                user_id='member', granted_by='buyer'))
            db.add(m.SaaSSubscription(id='other-subscription', enterprise_id='other_tenant',
                product_id='product', version_id='version', status='closed'))
            db.commit()
        self.assertEqual({item['enterprise_id'] for item in self.listing()['items']}, {'buyer_tenant'})
        self.assertEqual({item['enterprise_id'] for item in self.listing('other_tenant')['items']}, {'other_tenant'})
        for response in (self.request('GET', '/api/applications', 'member'), self.listing('')):
            self.assertEqual({item['enterprise_id'] for item in response['items']},
                {'buyer_tenant', 'other_tenant'})
        self.request('POST', '/api/applications/other-subscription/launch', 'member', 403)

    def test_cross_enterprise_filter_and_launch_denied_even_with_stray_grant(self):
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.ApplicationAccessGrant(enterprise_id='other_tenant', product_id='product',
                user_id='member', granted_by='buyer'))
            db.add(m.SaaSSubscription(id='foreign-subscription', enterprise_id='other_tenant',
                product_id='product', version_id='version', status='active'))
            db.commit()
        for enterprise_id in ('other_tenant', 'provider_tenant', 'missing'):
            result = self.listing(enterprise_id, 403)
            self.assertNotIn('foreign-subscription', str(result))
        self.assertEqual({item['subscription_id'] for item in self.listing()['items']}, {self.sid})
        self.request('POST', '/api/applications/foreign-subscription/launch', 'member', 403)

    def test_revoked_grant_is_hidden_and_cannot_launch(self):
        with self.m.SessionLocal() as db:
            grant = db.query(self.m.ApplicationAccessGrant).filter_by(user_id='member').one()
            grant.status = 'revoked'
            db.commit()
        self.assertEqual(self.listing()['items'], [])
        self.assertEqual(self.request('GET', '/api/applications', 'member')['items'], [])
        self.launch(403)

    def test_disabled_or_removed_membership_denies_explicit_scope_and_launch(self):
        for status in ('disabled', 'removed'):
            with self.subTest(status=status):
                with self.m.SessionLocal() as db:
                    member = db.query(self.m.Membership).filter_by(user_id='member',
                        enterprise_id='buyer_tenant').one()
                    member.status = status
                    db.commit()
                self.listing(expected=403)
                self.assertEqual(self.request('GET', '/api/applications', 'member')['items'], [])
                self.launch(403)

    def test_disabled_user_is_rejected_before_listing_or_launch(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'member').is_active = False
            db.commit()
        self.listing(expected=401)
        self.launch(401)

    def test_expired_subscription_is_unavailable_without_launch_link(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.SaaSSubscription, self.sid).expires_at = self.m.now() - timedelta(seconds=1)
            db.commit()
        item = self.listing()['items'][0]
        self.assertFalse(item['available'])
        self.assertTrue(item['blocked_reason'])
        self.assertNotIn('launch_url', item)
        self.assertNotIn('application_url', item)
        self.launch(403)

    def test_listing_and_launch_share_invalid_address_validation(self):
        for address in ('', 'javascript:alert(1)', 'https://user:private@saas.example.invalid/app',
                        'https://saas.example.invalid:0/app', 'https://saas.example.invalid\\app',
                        'https://saas.example.invalid/app\n'):
            with self.subTest(address=address):
                with self.m.SessionLocal() as db:
                    db.get(self.m.Product, 'product').application_url = address
                    db.commit()
                item = self.listing()['items'][0]
                self.assertFalse(item['available'])
                self.assertTrue(item['blocked_reason'])
                self.assertNotIn('launch_url', item)
                self.assertNotIn('private', str(item))
                self.launch(409)


if __name__ == '__main__':
    unittest.main()
