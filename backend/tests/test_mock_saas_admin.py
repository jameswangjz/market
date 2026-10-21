"""Private admin API and HTML contract; no deployed browser claims."""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_mock_saas_oidc import MockSaaSOIDCTests, PUBLIC


class MockSaaSAdminTests(unittest.TestCase):
    setUpClass = classmethod(MockSaaSOIDCTests.setUpClass.__func__)
    tearDownClass = classmethod(MockSaaSOIDCTests.tearDownClass.__func__)
    setUp = MockSaaSOIDCTests.setUp
    remote = MockSaaSOIDCTests.remote
    machine = MockSaaSOIDCTests.machine
    open_tenant = MockSaaSOIDCTests.open_tenant

    def save(self, **changes):
        return self.client.put('/admin/api/clients', headers=self.admin_headers,
                               json={**self.config, **changes})

    def test_authentication_and_disabled(self):
        for method, path in [('get', '/admin/api/clients'), ('put', '/admin/api/clients'),
                             ('post', '/admin/api/check')]:
            for headers in ({}, {'X-Mock-Admin-Secret': 'wrong'}):
                response = getattr(self.client, method)(path, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.headers['cache-control'], 'no-store')
        with patch.dict(os.environ, MOCK_SAAS_ENABLE_TEST_ADMIN='false'):
            disabled = self.module.create_app(transport=self.transport)
        with TestClient(disabled) as client:
            for method, path in [('get', '/admin'), ('get', '/admin/assets/admin.js'),
                                 ('get', '/admin/api/clients'), ('put', '/admin/api/clients'),
                                 ('post', '/admin/api/check')]:
                self.assertEqual(getattr(client, method)(path).status_code, 404)

    def test_metadata_and_html_never_include_secrets(self):
        response = self.client.get('/admin/api/clients', headers=self.admin_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['redirect_uri'], PUBLIC + '/sso/callback')
        self.assertEqual(data['issuer'], self.config['issuer'])
        self.assertEqual(data['public_base'], PUBLIC)
        self.assertEqual(data['clients'][0]['application_url'], PUBLIC + '/apps/fixture-product')
        self.assertTrue(data['clients'][0]['secret_configured'])
        self.assertNotIn('client_secret', data['clients'][0])
        page = self.client.get('/admin')
        for response in (response, page):
            for secret in (self.config['client_secret'], self.admin_headers['X-Mock-Admin-Secret']):
                self.assertNotIn(secret, response.text)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        for name in ('admin-key', 'admin-login', 'client-form', 'client-save',
                     'clients-list', 'admin-status', 'discovery-check', 'application-url'):
            self.assertIn('id="' + name + '"', page.text)
        script = self.client.get('/admin/assets/admin.js').text
        for forbidden in ('localStorage', 'sessionStorage', 'innerHTML', 'alert(', 'prompt(', 'confirm('):
            self.assertNotIn(forbidden, script)
        self.assertIn('name="application_url" id="application-url" readonly', page.text)
        self.assertIn('平台OIDC连接检查', page.text)
        self.assertIn('navigator.clipboard.writeText', script)
        self.assertIn('input.select()', script)
        self.assertIn('delete body.application_url', script)

    def test_application_url_quotes_product_and_is_only_metadata(self):
        response = self.save(client_id='another-client', product_id='product ?#&中文')
        self.assertEqual(response.status_code, 201)
        expected = PUBLIC + '/apps/product%20%3F%23%26%E4%B8%AD%E6%96%87'
        self.assertEqual(response.json()['application_url'], expected)
        self.assertNotIn('another-client', expected)
        self.assertNotIn('application_url', self.store.client('another-client'))
        data = self.client.get('/admin/api/clients', headers=self.admin_headers).json()
        self.assertEqual(next(c for c in data['clients'] if c['client_id'] == 'another-client')['application_url'], expected)

    def test_create_exact_settings_and_secret_required(self):
        for changes in ({'issuer': 'https://untrusted.invalid'},
                        {'redirect_uri': PUBLIC + '/sso/callback/'}, {'client_secret': ''},
                        {'client_secret': None}):
            self.assertEqual(self.save(client_id='new-client', **changes).status_code, 400)
        body = {k: v for k, v in self.config.items() if k != 'client_secret'}
        body['client_id'] = 'new-client'
        self.assertEqual(self.client.put('/admin/api/clients', headers=self.admin_headers,
                                        json=body).status_code, 400)
        response = self.save(client_id='new-client')
        self.assertEqual(response.status_code, 201)
        self.assertNotIn(self.config['client_secret'], response.text)
        self.assertIsNotNone(self.module.create_app(transport=self.transport).state.store.client('new-client'))

    def test_update_preserves_secret_and_denies_product_rebind(self):
        for secret in ('', None):
            self.assertEqual(self.save(client_secret=secret, product_name='Updated').status_code, 200)
            self.assertEqual(self.store.client('fixture-client')['client_secret'], self.config['client_secret'])
        body = {k: v for k, v in self.config.items() if k != 'client_secret'}
        self.assertEqual(self.client.put('/admin/api/clients', headers=self.admin_headers,
                                        json=body).status_code, 200)
        self.assertEqual(self.save(product_id='another-product').status_code, 409)
        self.assertEqual(self.store.client('fixture-client')['product_id'], self.config['product_id'])

    def test_rotation_cleans_only_owned_client_state(self):
        self.assertEqual(self.save(client_id='other-client').status_code, 201)
        with self.store.connect() as db:
            for index, client_id in enumerate(('fixture-client', 'other-client')):
                db.execute('INSERT INTO flows VALUES (?,?,?,?,?,?,?,?)',
                           (str(index), str(index), client_id, 'enterprise', 'nonce', 'verifier', 9999999999, 0))
                db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',
                           (str(index), client_id, '{}', 'private-token', 9999999999))
                db.execute('INSERT INTO mappings VALUES (?,?,?,?,?)',
                           (client_id, 'user', 'tenant', 'enterprise', 'subscription'))
        self.assertEqual(self.save(client_secret='').status_code, 200)
        self.assertEqual(self.save(product_id='wrong', client_secret='rotated').status_code, 409)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM flows').fetchone()[0], 2)
        response = self.save(client_secret='rotated')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('rotated', response.text)
        with self.store.connect() as db:
            for table in ('flows', 'sessions', 'mappings'):
                rows = db.execute('SELECT client_id FROM ' + table).fetchall()
                self.assertEqual([row[0] for row in rows], ['other-client'])
        self.assertIsNotNone(self.store.tenant('fixture-tenant'))

    def test_check_fixed_backchannel_and_untrusted_metadata(self):
        response = self.client.post('/admin/api/check', headers=self.admin_headers,
                                    json={'url': 'https://untrusted.invalid'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['scope'], 'connectivity_and_configuration_only')
        self.assertEqual(self.requests[-1].url.path, '/.well-known/openid-configuration')
        self.metadata_changes['token_endpoint'] = 'https://untrusted.invalid/token'
        self.assertEqual(self.client.post('/admin/api/check', headers=self.admin_headers).status_code, 503)

    def test_validation_does_not_echo_secret(self):
        response = self.save(client_secret={'private': 'never-echo-this'})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('never-echo-this', response.text)


if __name__ == '__main__':
    unittest.main()
