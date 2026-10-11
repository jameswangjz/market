"""Independent RP tests: mocked OP HTTP, real RSA; no deployed browser evidence."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
import time
from types import ModuleType
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from authlib.jose import JsonWebKey, JsonWebToken
from authlib.oidc.core.util import create_half_hash
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx


ROOT = Path(__file__).parents[2] / 'mock-saas' / 'app'
ISSUER = 'http://platform.example.invalid'
PUBLIC = 'http://127.0.0.1:30082'
INTERNAL = 'http://market-api:8000'


class MockSaaSOIDCTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = '_mock_saas_test_' + secrets.token_hex(6)
        package = ModuleType(cls.package)
        package.__path__ = [str(ROOT)]
        sys.modules[cls.package] = package
        spec = importlib.util.spec_from_file_location(cls.package + '.main', ROOT / 'main.py')
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        cls.jwk = JsonWebKey.import_key(key.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)).as_dict()
        cls.jwk.update(kid='fixture-key', alg='RS256', use='sig')

    @classmethod
    def tearDownClass(cls):
        for name in list(sys.modules):
            if name == cls.package or name.startswith(cls.package + '.'):
                del sys.modules[name]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / 'private.sqlite3'
        self.env = patch.dict(os.environ, MOCK_SAAS_DB_PATH=str(self.db_path),
            MOCK_SAAS_ENABLE_TEST_ADMIN='true', MOCK_SAAS_ADMIN_SECRET='fixture-admin-secret',
            OIDC_ALLOW_HTTP_DEVELOPMENT='true', OIDC_ISSUER=ISSUER,
            MOCK_SAAS_PUBLIC_BASE_URL=PUBLIC, OIDC_BACKCHANNEL_BASE_URL=INTERNAL)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.claim_changes = {}
        self.info_changes = {}
        self.metadata_changes = {}
        self.alg = 'RS256'
        self.info_status = 200
        self.introspection_active = True
        self.machine_product = 'fixture-product'
        self.machine_scope = 'saas.tenant saas.user saas.department'
        self.remote_codes = set()
        self.signing_pem = self.pem
        self.requests = []
        self.current_identity = {}
        self.transport = httpx.MockTransport(self.remote)
        self.app = self.module.create_app(transport=self.transport)
        self.store = self.app.state.store
        self.client = TestClient(self.app, base_url=PUBLIC, follow_redirects=False)
        self.addCleanup(self.client.close)
        self.admin_headers = {'X-Mock-Admin-Secret': 'fixture-admin-secret'}
        self.config = {'client_id': 'fixture-client', 'client_secret': 'fixture-client-secret',
            'redirect_uri': PUBLIC + '/sso/callback', 'product_id': 'fixture-product', 'issuer': ISSUER,
            'product_name': '协作应用'}
        response = self.client.post('/_test/oidc/clients', json=self.config, headers=self.admin_headers)
        self.assertEqual(response.status_code, 201, response.text)
        self.open_tenant()

    def remote(self, request):
        self.requests.append(request)
        self.assertEqual(str(request.url).split('?')[0], INTERNAL + request.url.path)
        path = request.url.path
        if path == '/oauth/introspect':
            return httpx.Response(200, json={'active': self.introspection_active,
                'product_id': self.machine_product, 'scope': self.machine_scope})
        if path == '/.well-known/openid-configuration':
            return httpx.Response(200, json={'issuer': ISSUER, 'authorization_endpoint': ISSUER + '/oidc/authorize',
                'token_endpoint': ISSUER + '/oidc/token', 'jwks_uri': ISSUER + '/oidc/jwks',
                'userinfo_endpoint': ISSUER + '/oidc/userinfo', **self.metadata_changes})
        if path == '/oidc/jwks':
            return httpx.Response(200, json={'keys': [self.jwk]})
        if path == '/oidc/token':
            self.assertTrue(request.headers['authorization'].startswith('Basic '))
            basic = base64.b64decode(request.headers['authorization'].split(' ', 1)[1]).decode()
            self.assertEqual(basic, 'fixture-client:fixture-client-secret')
            form = parse_qs(request.content.decode())
            self.assertEqual(form['grant_type'], ['authorization_code'])
            self.assertEqual(form['redirect_uri'], [PUBLIC + '/sso/callback'])
            self.assertEqual(form['code_verifier'], [self.flow['verifier']])
            code = form['code'][0]
            if code in self.remote_codes:
                return httpx.Response(400, json={'error': 'invalid_grant'})
            self.remote_codes.add(code)
            access = 'private-access-' + code
            identity = {'sub': 'fixture-user', 'enterprise_id': 'fixture-enterprise',
                        'subscription_id': 'fixture-subscription', 'tenant_id': 'fixture-tenant'}
            self.current_identity = identity
            claims = {'iss': ISSUER, 'aud': 'fixture-client', 'exp': int(time.time()) + 300,
                'iat': int(time.time()), 'nonce': self.flow['nonce'],
                'at_hash': create_half_hash(access, 'RS256').decode(), **identity, **self.claim_changes}
            key = 'not-a-public-key' if self.alg == 'HS256' else self.signing_pem
            token = JsonWebToken([self.alg]).encode({'alg': self.alg, 'kid': 'fixture-key',
                'jku': 'http://evil.example.invalid/keys'}, claims, key).decode()
            return httpx.Response(200, json={'access_token': access, 'token_type': 'Bearer',
                                          'expires_in': 300, 'id_token': token})
        if path == '/oidc/userinfo':
            self.assertTrue(request.headers['authorization'].startswith('Bearer private-access-'))
            return httpx.Response(self.info_status, json={**self.current_identity, 'name': '李明',
                                                        'email': 'fixture@example.invalid', **self.info_changes})
        raise AssertionError('Unexpected remote URL')

    def machine(self, operation, **body):
        response = self.client.post('/isv.php', json={'type': operation, 'tenant_id': 'fixture-tenant', **body},
                                    headers={'Authorization': 'Bearer fixture-machine-token'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def open_tenant(self):
        return self.machine('OPEN', product_id='fixture-product', subscription_id='fixture-subscription',
            enterprise_id='fixture-enterprise', enterprise_name='测试企业', version='1')

    def login(self, **params):
        response = self.client.get('/sso/apps/fixture-client', params={'enterprise_id': 'fixture-enterprise', **params})
        self.assertEqual(response.status_code, 302, response.text)
        self.query = parse_qs(urlsplit(response.headers['location']).query)
        self.assertEqual(self.query['scope'], ['openid profile email'])
        self.assertEqual(self.query['response_type'], ['code'])
        self.assertEqual(self.query['code_challenge_method'], ['S256'])
        with self.store.connect() as db:
            self.flow = dict(db.execute('SELECT * FROM flows WHERE state_hash = ?',
                (hashlib.sha256(self.query['state'][0].encode()).hexdigest(),)).fetchone())
        expected = base64.urlsafe_b64encode(hashlib.sha256(self.flow['verifier'].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(self.query['code_challenge'], [expected])
        self.code = 'fixture-code-' + secrets.token_hex(6)
        return response

    def callback(self, **changes):
        return self.client.get('/sso/callback', params={'state': self.query['state'][0], 'code': self.code, **changes})

    def signed_in(self):
        self.login()
        response = self.callback()
        self.assertEqual(response.status_code, 303, response.text)
        return response

    def test_real_rsa_success_pkce_cookie_and_private_page(self):
        login = self.login()
        self.assertIn('HttpOnly', login.headers['set-cookie'])
        self.assertIn('SameSite=lax', login.headers['set-cookie'])
        self.assertIn('Path=/sso', login.headers['set-cookie'])
        response = self.callback()
        self.assertEqual(response.status_code, 303, response.text)
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        page = self.client.get('/app')
        self.assertEqual(page.status_code, 200, page.text)
        for name in ('协作应用', '测试企业', '李明'):
            self.assertIn(name, page.text)
        for forbidden in ('fixture-client-secret', 'private-access', 'fixture-user', 'fixture-subscription', 'fixture-tenant'):
            self.assertNotIn(forbidden, page.text)
        self.assertEqual(page.headers['cache-control'], 'no-store')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM mappings').fetchone()[0], 1)

    def test_wrong_state_and_missing_browser_cookie_do_not_exchange(self):
        self.login()
        self.assertEqual(self.callback(state='wrong-state').status_code, 400)
        with TestClient(self.app, base_url=PUBLIC, follow_redirects=False) as other:
            self.assertEqual(other.get('/sso/callback', params={'state': self.query['state'][0],
                             'code': self.code}).status_code, 400)
        self.assertEqual(len(self.remote_codes), 0)
        self.assertEqual(self.callback().status_code, 303)

    def test_flow_once_and_duplicate_callback_parameters(self):
        self.login()
        state = self.query['state'][0]
        self.assertEqual(self.client.get('/sso/callback?state=' + state + '&state=' + state + '&code=' + self.code).status_code, 400)
        self.assertEqual(self.callback().status_code, 303)
        self.assertEqual(self.callback().status_code, 400)
        self.assertEqual(len(self.remote_codes), 1)

    def test_atomic_flow_consumption(self):
        self.login()
        state = self.query['state'][0]
        cookie = self.client.cookies.get('mock_saas_oidc_flow')
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.store.consume_flow(state, cookie), range(2)))
        self.assertEqual(sum(row is not None for row in results), 1)

    def test_invalid_signed_claims_fail_and_consume_flow(self):
        for changes in ({'nonce': 'wrong'}, {'iss': 'http://evil.example.invalid'}, {'aud': 'other-client'},
                        {'exp': int(time.time()) - 10}, {'iat': int(time.time()) + 3600},
                        {'at_hash': 'wrong'}, {'enterprise_id': 'other-enterprise'}, {'sub': ''}):
            with self.subTest(changes=changes):
                self.claim_changes = changes
                self.login()
                self.assertEqual(self.callback().status_code, 401)
                self.assertEqual(self.callback().status_code, 400)
                with self.store.connect() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)

    def test_wrong_algorithms_fail(self):
        for alg in ('HS256', 'RS512'):
            with self.subTest(alg=alg):
                self.alg = alg
                self.login()
                self.assertEqual(self.callback().status_code, 401)

    def test_invalid_signature_fails(self):
        self.signing_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        self.login()
        self.assertEqual(self.callback().status_code, 401)

    def test_reused_remote_code_in_new_flow_is_denied(self):
        self.login()
        used_code = self.code
        self.assertEqual(self.callback().status_code, 303)
        self.login()
        self.code = used_code
        self.assertEqual(self.callback().status_code, 401)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)

    def test_existing_mapping_cannot_silently_change_enterprise_or_subscription(self):
        self.signed_in()
        with self.store.connect() as db:
            db.execute('UPDATE mappings SET subscription_id = ?', ('different-subscription',))
        self.assertEqual(self.client.get('/app').status_code, 401)
        self.login()
        self.assertEqual(self.callback().status_code, 401)

    def test_invalid_admin_input_does_not_echo_supplied_secret(self):
        response = self.client.post('/_test/oidc/clients', headers=self.admin_headers,
            json={**self.config, 'client_secret': 'do-not-echo-' * 400})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('do-not-echo', response.text)

    def test_untrusted_metadata_cannot_redirect_backchannel_requests(self):
        for key in ('issuer', 'token_endpoint', 'jwks_uri', 'userinfo_endpoint', 'authorization_endpoint'):
            with self.subTest(key=key):
                self.metadata_changes = {key: 'http://evil.example.invalid/' + key}
                self.assertEqual(self.client.get('/sso/apps/fixture-client',
                    params={'enterprise_id': 'fixture-enterprise'}).status_code, 503)
        self.assertTrue(all(request.url.host == 'market-api' for request in self.requests))

    def test_userinfo_identity_mismatches_fail(self):
        for key in ('sub', 'enterprise_id', 'subscription_id', 'tenant_id'):
            with self.subTest(key=key):
                self.info_changes = {key: 'other'}
                self.login()
                self.assertEqual(self.callback().status_code, 401)

    def test_closed_wrong_or_never_opened_tenant_denied(self):
        self.machine('CLOSE')
        self.machine('CHANGE', to_version='2')
        self.login()
        self.assertEqual(self.callback().status_code, 401)
        self.open_tenant()
        with self.store.connect() as db:
            row = db.execute('SELECT payload FROM tenants WHERE tenant_id = ?', ('fixture-tenant',)).fetchone()
            tenant = json.loads(row['payload'])
            tenant['product_id'] = 'other-product'
            db.execute('UPDATE tenants SET payload = ? WHERE tenant_id = ?', (json.dumps(tenant), 'fixture-tenant'))
        self.login()
        self.assertEqual(self.callback().status_code, 401)
        with self.store.connect() as db:
            db.execute('DELETE FROM tenants WHERE tenant_id = ?', ('fixture-tenant',))
        self.login()
        self.assertEqual(self.callback().status_code, 401)

    def test_each_app_request_rechecks_op_and_revocation_invalidates_session(self):
        self.signed_in()
        self.assertEqual(self.client.get('/app').status_code, 200)
        self.assertEqual(self.client.get('/app').status_code, 200)
        self.assertEqual(sum(request.url.path == '/oidc/userinfo' for request in self.requests), 3)
        self.info_status = 403
        self.assertEqual(self.client.get('/app').status_code, 401)
        self.info_status = 200
        self.assertEqual(self.client.get('/app').status_code, 401)

    def test_local_close_or_user_unassign_rejects_existing_session(self):
        self.signed_in()
        self.machine('CLOSE')
        self.assertEqual(self.client.get('/app').status_code, 401)
        self.open_tenant()
        self.signed_in()
        self.machine('USER_UNASSIGN', user_id='mock-user-fixture-user')
        self.assertEqual(self.client.get('/app').status_code, 401)
        self.login()
        self.assertEqual(self.callback().status_code, 401)
        self.machine('USER_ASSIGN', user_id='fixture-user')
        self.signed_in()

    def test_expired_flow_and_session(self):
        self.login()
        with self.store.connect() as db:
            db.execute('UPDATE flows SET expires_at = 0')
        self.assertEqual(self.callback().status_code, 400)
        self.signed_in()
        with self.store.connect() as db:
            row = db.execute('SELECT expires_at FROM sessions').fetchone()
            self.assertLessEqual(row['expires_at'], int(time.time()) + 300)
            db.execute('UPDATE sessions SET expires_at = 0')
        self.assertEqual(self.client.get('/app').status_code, 401)

    def test_private_database_and_restart_persist_tenant_flow_and_session(self):
        self.login()
        restarted = self.module.create_app(transport=self.transport)
        with TestClient(restarted, base_url=PUBLIC, follow_redirects=False) as client:
            client.cookies.update(self.client.cookies)
            response = client.get('/sso/callback', params={'state': self.query['state'][0], 'code': self.code})
            self.assertEqual(response.status_code, 303, response.text)
            cookies = dict(client.cookies)
        with TestClient(self.module.create_app(transport=self.transport), base_url=PUBLIC) as client:
            client.cookies.update(cookies)
            self.assertEqual(client.get('/app').status_code, 200)
        self.assertEqual(stat.S_IMODE(self.db_path.stat().st_mode), 0o600)

    def test_admin_default_disabled_secret_required_and_no_secret_echo(self):
        response = self.client.post('/_test/oidc/clients', json=self.config)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.delete('/_test/oidc/clients/fixture-client').status_code, 403)
        with patch.dict(os.environ, MOCK_SAAS_ENABLE_TEST_ADMIN='false'):
            disabled = self.module.create_app(transport=self.transport)
        with TestClient(disabled, base_url=PUBLIC) as client:
            self.assertEqual(client.post('/_test/oidc/clients', json=self.config,
                                        headers=self.admin_headers).status_code, 404)
        self.assertEqual(self.client.post('/_test/oidc/clients', json=self.config,
                                         headers=self.admin_headers).status_code, 409)
        other = {**self.config, 'client_id': 'other-client'}
        response = self.client.post('/_test/oidc/clients', json=other, headers=self.admin_headers)
        self.assertEqual(response.status_code, 201)
        self.assertNotIn('fixture-client-secret', response.text)
        self.assertNotIn('client_secret', response.text)

    def test_exact_client_cleanup_preserves_other_client_and_machine_state(self):
        self.signed_in()
        self.login()
        other = {**self.config, 'client_id': 'other-client', 'product_id': 'other-product'}
        self.client.post('/_test/oidc/clients', json=other, headers=self.admin_headers)
        with self.store.connect() as db:
            db.execute('INSERT INTO flows VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                ('other-state', 'other-browser', 'other-client', 'other-enterprise', 'nonce', 'verifier', int(time.time()) + 300, 0))
            db.execute('INSERT INTO mappings VALUES (?, ?, ?, ?, ?)',
                ('other-client', 'other-user', 'other-tenant', 'other-enterprise', 'other-subscription'))
            db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?)',
                ('other-session', 'other-client', '{}', 'private-other-token', int(time.time()) + 300))
        response = self.client.delete('/_test/oidc/clients/fixture-client', headers=self.admin_headers)
        self.assertTrue(response.json()['deleted'])
        with self.store.connect() as db:
            for table in ('clients', 'flows', 'sessions', 'mappings'):
                self.assertEqual(db.execute(f'SELECT client_id FROM {table}').fetchall()[0]['client_id'], 'other-client')
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM tenants').fetchone()[0], 1)

    def test_logout_invalidates_only_rp_session(self):
        self.signed_in()
        self.assertEqual(self.client.post('/sso/logout', headers={'Origin': 'http://evil.example.invalid'}).status_code, 403)
        response = self.client.post('/sso/logout', headers={'Origin': PUBLIC})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/app').status_code, 401)

    def test_html_escapes_names(self):
        self.info_changes = {'name': '<script>alert(1)</script>'}
        self.signed_in()
        page = self.client.get('/app')
        self.assertNotIn('<script>', page.text)
        self.assertIn('&lt;script&gt;', page.text)

    def test_legacy_health_machine_oauth_and_operations(self):
        self.assertEqual(self.client.get('/health').json()['service'], 'mock-saas')
        response = self.client.post('/oauth/token', data={'grant_type': 'client_credentials',
                                                         'client_id': 'legacy', 'client_secret': 'legacy'})
        self.assertEqual(response.json()['access_token'], 'mock-token')
        self.assertEqual(self.client.post('/oauth/token', data={}).status_code, 401)
        self.assertEqual(self.machine('RENEW')['status'], 'active')
        self.assertEqual(self.machine('CHANGE', to_version='2')['version'], '2')
        self.assertEqual(self.machine('USER_ASSIGN', user_id='fixture-user')['user_id'], 'mock-user-fixture-user')
        self.assertEqual(self.machine('DEPT_CREATE', department_id='engineering')['department_id'], 'mock-dept-engineering')
        self.assertEqual(self.machine('DEPT_REMOVE', department_id='engineering')['status'], 'deleted')
        self.assertFalse(self.machine('NOT_SUPPORTED')['retryable'])
        self.introspection_active = False
        self.assertEqual(self.client.post('/isv.php', json={'type': 'OPEN'},
                         headers={'Authorization': 'Bearer revoked'}).status_code, 401)

    def test_http_development_must_be_explicit_and_redirect_exact(self):
        for changes in ({'issuer': 'http://evil.example.invalid'}, {'redirect_uri': 'http://evil.example.invalid/callback'}):
            response = self.client.post('/_test/oidc/clients', headers=self.admin_headers,
                                       json={**self.config, 'client_id': 'other', **changes})
            self.assertEqual(response.status_code, 400)
        with patch.dict(os.environ, OIDC_ALLOW_HTTP_DEVELOPMENT='false'):
            production = self.module.create_app(transport=self.transport)
        with TestClient(production, base_url=PUBLIC) as client:
            self.assertEqual(client.get('/sso/apps/fixture-client',
                             params={'enterprise_id': 'fixture-enterprise'}).status_code, 503)

    def raw_machine(self, operation, **body):
        return self.client.post('/isv.php', json={'type': operation, 'tenant_id': 'fixture-tenant', **body},
                                headers={'Authorization': 'Bearer fixture-machine-token'})

    def machine_snapshot(self):
        with self.store.connect() as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                    for table in ('tenants', 'users', 'departments')}

    def test_cross_product_open_and_all_existing_tenant_operations_are_denied(self):
        before = self.machine_snapshot()
        response = self.raw_machine('OPEN', product_id='other-product', enterprise_id='fixture-enterprise',
                                    subscription_id='fixture-subscription', tenant_id='new-tenant')
        self.assertEqual(response.status_code, 403)
        self.machine_product = 'other-product'
        for operation in ('OPEN', 'CLOSE', 'RENEW', 'CHANGE', 'USER_ASSIGN', 'USER_UNASSIGN',
                          'DEPT_CREATE', 'DEPT_REMOVE'):
            with self.subTest(operation=operation):
                response = self.raw_machine(operation, product_id='other-product',
                    enterprise_id='fixture-enterprise', subscription_id='fixture-subscription',
                    user_id='fixture-user', department_id='engineering', to_version='3')
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(self.machine_snapshot(), before)

    def test_operation_scopes_are_exact_and_missing_scope_never_mutates(self):
        before = self.machine_snapshot()
        for operation, wrong_scope in (('OPEN', 'resource.invoke'), ('CLOSE', 'saas.user'),
                ('RENEW', ''), ('CHANGE', 'saas.tenant_extra'), ('USER_ASSIGN', 'saas.tenant'),
                ('USER_UNASSIGN', 'saas.department'), ('DEPT_CREATE', 'saas.user'), ('DEPT_REMOVE', None)):
            with self.subTest(operation=operation):
                self.machine_scope = wrong_scope
                response = self.raw_machine(operation, product_id='fixture-product',
                    enterprise_id='fixture-enterprise', subscription_id='fixture-subscription',
                    user_id='fixture-user', department_id='engineering')
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(self.machine_snapshot(), before)

    def test_missing_machine_product_and_open_identity_are_rejected(self):
        before = self.machine_snapshot()
        for product in ('', None):
            self.machine_product = product
            self.assertEqual(self.raw_machine('CLOSE').status_code, 403)
        self.machine_product = 'fixture-product'
        identity = {'product_id': 'fixture-product', 'enterprise_id': 'fixture-enterprise',
                    'subscription_id': 'fixture-subscription', 'tenant_id': 'new-tenant'}
        for key in ('product_id', 'enterprise_id', 'subscription_id'):
            with self.subTest(key=key):
                incomplete = {k: v for k, v in identity.items() if k != key}
                self.assertEqual(self.raw_machine('OPEN', **incomplete).status_code, 400)
        self.assertEqual(self.machine_snapshot(), before)

    def test_existing_tenant_identity_cannot_be_rebound_and_body_product_cannot_lie(self):
        before = self.machine_snapshot()
        for changes in ({'enterprise_id': 'other-enterprise'}, {'subscription_id': 'other-subscription'}):
            response = self.raw_machine('OPEN', **{'product_id': 'fixture-product',
                'enterprise_id': 'fixture-enterprise', 'subscription_id': 'fixture-subscription', **changes})
            self.assertEqual(response.status_code, 409)
        for operation in ('CLOSE', 'USER_ASSIGN', 'DEPT_CREATE'):
            response = self.raw_machine(operation, product_id='other-product',
                                        user_id='fixture-user', department_id='engineering')
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.machine_snapshot(), before)

    def test_non_open_missing_product_uses_only_owned_existing_tenant_and_renew_persists(self):
        self.machine_scope = 'saas.tenant'
        renewed = self.raw_machine('RENEW')
        self.assertEqual(renewed.status_code, 200)
        tenant = self.store.tenant('fixture-tenant')
        self.assertEqual(tenant['status'], 'active')
        self.assertEqual(tenant['expires_at'], renewed.json()['expires_at'])
        self.assertEqual(tenant['product_id'], 'fixture-product')
        self.assertEqual(self.raw_machine('CLOSE', tenant_id='missing-tenant').status_code, 404)
        self.machine('CLOSE')
        self.machine('RENEW')
        self.assertFalse(self.store.tenant('fixture-tenant')['login_open'])
        self.open_tenant()
        self.assertTrue(self.store.tenant('fixture-tenant')['login_open'])

    def test_restore_open_inherits_immutable_existing_identity(self):
        original = self.store.tenant('fixture-tenant')
        self.machine('CLOSE')
        response = self.raw_machine('OPEN', enterprise_id='fixture-enterprise', version='2')
        self.assertEqual(response.status_code, 200, response.text)
        restored = self.store.tenant('fixture-tenant')
        self.assertTrue(restored['login_open'])
        self.assertEqual(restored['status'], 'opened')
        self.assertEqual(restored['version'], '2')
        for key in ('product_id', 'enterprise_id', 'subscription_id'):
            self.assertEqual(restored[key], original[key])
        self.signed_in()

    def test_restore_open_rejects_cross_identity_and_cross_product(self):
        self.machine('CLOSE')
        before = self.machine_snapshot()
        for changes, expected in (({'enterprise_id': 'other-enterprise'}, 409),
                ({'subscription_id': 'other-subscription'}, 409),
                ({'product_id': 'other-product'}, 403)):
            with self.subTest(changes=changes):
                response = self.raw_machine('OPEN', **changes)
                self.assertEqual(response.status_code, expected)
                self.assertEqual(self.machine_snapshot(), before)
        self.machine_product = 'other-product'
        self.assertEqual(self.raw_machine('OPEN', enterprise_id='fixture-enterprise').status_code, 403)
        self.assertEqual(self.machine_snapshot(), before)

    def test_repeated_external_user_unassign_preserves_identity_and_is_idempotent(self):
        self.signed_in()
        external = 'mock-user-fixture-user'
        self.machine('USER_UNASSIGN', user_id=external)
        first = self.machine_snapshot()
        self.machine('USER_UNASSIGN', user_id=external)
        self.assertEqual(self.machine_snapshot(), first)
        with self.store.connect() as db:
            rows = db.execute('SELECT user_id, payload FROM users').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['user_id'], 'fixture-user')
        self.assertEqual(json.loads(rows[0]['payload'])['external_user_id'], external)
        self.assertEqual(json.loads(rows[0]['payload'])['status'], 'deleted')
        self.assertEqual(self.client.get('/app').status_code, 401)
        self.machine('USER_UNASSIGN', user_id='mock-user-not-local')
        self.assertEqual(self.machine_snapshot(), first)


if __name__ == '__main__':
    unittest.main()
