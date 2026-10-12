"""Shared HTTP configuration must be enforced by registration and callbacks."""
import unittest
import importlib

import test_message_business_events as fixture


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


if __name__ == '__main__':
    unittest.main()
