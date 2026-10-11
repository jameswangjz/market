"""OIDC protocol HTTP tests; third-party browser sessions are not covered."""
import base64
import hashlib
import json
import os
import secrets
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import test_version_change_quotes as fixture


class OIDCProviderTests(unittest.TestCase):
    setUpClass = classmethod(fixture.VersionChangeQuoteTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.VersionChangeQuoteTests.tearDownClass.__func__)
    tearDown = fixture.VersionChangeQuoteTests.tearDown
    request = fixture.VersionChangeQuoteTests.request
    prepare = fixture.VersionChangeQuoteTests.prepare
    create = fixture.VersionChangeQuoteTests.create
    deliver = fixture.VersionChangeQuoteTests.deliver

    def setUp(self):
        fixture.VersionChangeQuoteTests.setUp(self)
        self.oid, term = self.prepare(method='tenant_access', future=False)
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        self.env = patch.dict(os.environ, OIDC_ISSUER='https://platform.example.invalid',
            OIDC_SIGNING_KEY_PEM=pem, OIDC_ALLOW_HTTP_DEVELOPMENT='false')
        self.env.start()
        self.addCleanup(self.env.stop)
        with self.m.SessionLocal() as db:
            product = db.get(self.m.Product, 'product')
            product.oidc_redirect_uris_json = json.dumps(['https://saas.example.invalid/callback'])
            order = db.get(self.m.Order, self.oid)
            subscription = db.get(self.m.SaaSSubscription, order.subscription_id)
            subscription.starts_at, subscription.expires_at = term['starts_at'], term['ends_at']
            self.sid = subscription.id
            db.add(self.m.ApplicationAccessGrant(enterprise_id='buyer_tenant', product_id='product',
                user_id='member', granted_by='buyer'))
            db.commit()
        config = self.request('POST', '/api/products/product/oidc-client', 'ops')
        self.cid, self.secret = config['client_id'], config['client_secret']

    def authorization(self, actor='member', expected=200, **overrides):
        self.verifier = 'V' * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b'=').decode()
        body = {'client_id': self.cid, 'redirect_uri': 'https://saas.example.invalid/callback',
            'scope': 'openid profile email', 'state': secrets.token_urlsafe(24),
            'nonce': secrets.token_urlsafe(24), 'code_challenge': challenge,
            'code_challenge_method': 'S256', 'enterprise_id': 'buyer_tenant'} | overrides
        response = self.request('POST', '/oidc/authorize', actor, expected, json=body)
        if expected != 200:
            return response
        self.assertEqual(parse_qs(urlsplit(response['redirect_url']).query)['state'][0], body['state'])
        self.code = parse_qs(urlsplit(response['redirect_url']).query)['code'][0]
        self.nonce = body['nonce']
        return self.code

    def exchange(self, expected=200, **overrides):
        data = {'client_id': self.cid, 'client_secret': self.secret, 'grant_type': 'authorization_code',
            'code': self.code, 'redirect_uri': 'https://saas.example.invalid/callback',
            'code_verifier': self.verifier} | overrides
        response = self.client.post('/oidc/token', data=data)
        self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        return response.json()

    def info(self, token, expected=200):
        response = self.client.get('/oidc/userinfo', headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def test_oidc_valid_code_pkce_jwks_nonce_scope_and_identity(self):
        self.authorization()
        tokens = self.exchange()
        jwk = self.client.get('/oidc/jwks').json()['keys'][0]
        public = self.m.jwt.PyJWK.from_dict(jwk).key
        claims = self.m.jwt.decode(tokens['id_token'], public, algorithms=['RS256'],
            audience=self.cid, issuer='https://platform.example.invalid')
        self.assertEqual((claims['sub'], claims['nonce'], claims['enterprise_id'], claims['tenant_id']),
            ('member', self.nonce, 'buyer_tenant', 'fixture-tenant'))
        self.assertEqual(self.info(tokens['access_token'])['sub'], 'member')
        self.assertNotIn('d', jwk)
        self.assertNotIn('refresh_token', tokens)
        for forbidden in ('password', 'secret', 'cost', 'upstream'):
            self.assertNotIn(forbidden, json.dumps(claims))

    def test_oidc_wrong_verifier_does_not_consume_code(self):
        self.authorization()
        self.assertEqual(self.exchange(400, code_verifier='X' * 64)['error'], 'invalid_grant')
        self.exchange()

    def test_oidc_replay_revokes_first_access_token(self):
        self.authorization()
        tokens = self.exchange()
        self.exchange(400)
        self.info(tokens['access_token'], 401)

    def test_oidc_wrong_client_secret_or_redirect(self):
        self.authorization()
        self.exchange(401, client_secret='bad')
        self.exchange(400, redirect_uri='https://attacker.invalid/callback')
        self.exchange()

    def test_oidc_no_platform_user_or_other_enterprise(self):
        self.authorization('ops', 403)
        self.authorization(expected=403, enterprise_id='provider_tenant')

    def test_oidc_member_grant_rechecked_at_exchange_and_userinfo(self):
        self.authorization()
        with self.m.SessionLocal() as db:
            grant = db.query(self.m.ApplicationAccessGrant).filter_by(user_id='member').one()
            grant.status = 'revoked'
            db.commit()
        self.exchange(400)
        with self.m.SessionLocal() as db:
            grant = db.query(self.m.ApplicationAccessGrant).filter_by(user_id='member').one()
            grant.status = 'active'
            db.commit()
        tokens = self.exchange()
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'member').is_active = False
            db.commit()
        self.info(tokens['access_token'], 403)

    def test_oidc_expired_or_closed_subscription_denied(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.SaaSSubscription, self.sid).status = 'closed'
            db.commit()
        self.authorization(expected=403)

    def test_oidc_callback_whitelist_and_scope(self):
        self.authorization(expected=400, redirect_uri='https://attacker.invalid/callback')
        self.authorization(expected=400, scope='resource.invoke')
        self.authorization(expected=422, code_challenge_method='plain')
        self.authorization(expected=422, state='short')

    def test_oidc_nonce_cannot_be_reused(self):
        self.authorization(nonce='nonce-unique-for-client-1234')
        self.authorization(expected=400, nonce='nonce-unique-for-client-1234')

    def test_oidc_expired_code_and_disabled_client(self):
        self.authorization()
        with self.m.SessionLocal() as db:
            db.query(self.m.OIDCCode).update({'expires_at': 0})
            db.commit()
        self.exchange(400)
        self.authorization()
        with self.m.SessionLocal() as db:
            db.query(self.m.OIDCClient).update({'status': 'disabled'})
            db.commit()
        self.exchange(400)

    def test_oidc_management_scope_and_secret_one_time(self):
        self.request('POST', '/api/products/product/oidc-client', 'member', 403)
        again = self.request('POST', '/api/products/product/oidc-client', 'ops')
        self.assertEqual(again['client_id'], self.cid)
        self.assertIsNone(again['client_secret'])

    def test_oidc_identity_scopes_minimized_and_management_tokens_rejected(self):
        self.authorization(scope='openid')
        tokens = self.exchange()
        self.assertNotIn('email', self.info(tokens['access_token']))
        self.assertNotIn('name', self.info(tokens['access_token']))
        self.info(self.tokens['member'], 401)
        result = self.client.post('/oidc/token', data={'grant_type': 'client_credentials',
            'client_id': self.cid, 'client_secret': self.secret})
        self.assertEqual(result.status_code, 400)

    def test_oidc_duplicate_form_parameters_and_body_limit(self):
        result = self.client.post('/oidc/token', content='code=a&code=b',
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(result.status_code, 400)
        result = self.client.post('/oidc/token', content='code=' + 'a' * 20000,
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(result.status_code, 413)

    def test_oidc_unconfigured_signing_key_and_http_fail_closed(self):
        with patch.dict(os.environ, OIDC_SIGNING_KEY_PEM=''):
            self.assertEqual(self.client.get('/oidc/jwks').status_code, 503)
        with patch.dict(os.environ, OIDC_ISSUER='http://platform.invalid'):
            self.assertEqual(self.client.get('/.well-known/openid-configuration').status_code, 503)

    def test_oidc_client_secret_basic_and_bad_secret(self):
        self.authorization()
        data = {'grant_type': 'authorization_code', 'code': self.code,
            'redirect_uri': 'https://saas.example.invalid/callback', 'code_verifier': self.verifier}
        denied = self.client.post('/oidc/token', data=data, auth=(self.cid, 'wrong'))
        self.assertEqual(denied.status_code, 401)
        accepted = self.client.post('/oidc/token', data=data, auth=(self.cid, self.secret))
        self.assertEqual(accepted.status_code, 200, accepted.text)

    def test_oidc_security_down_denies_existing_access_manual_down_retains_it(self):
        self.authorization()
        tokens = self.exchange()
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').status = 'draft'
            db.commit()
        self.info(tokens['access_token'])
        self.authorization()
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').status = 'security_unpublished'
            db.commit()
        self.info(tokens['access_token'], 401)
        self.exchange(400)
        self.authorization(expected=400)

    def test_oidc_failed_audit_rolls_back_consumption_and_token(self):
        self.authorization()
        with patch.object(self.m, 'audit', side_effect=RuntimeError('injected transaction failure')):
            response = self.client.post('/oidc/token', data={'client_id': self.cid,
                'client_secret': self.secret, 'grant_type': 'authorization_code', 'code': self.code,
                'redirect_uri': 'https://saas.example.invalid/callback', 'code_verifier': self.verifier})
            self.assertEqual(response.status_code, 500)
        with self.m.SessionLocal() as db:
            code = db.get(self.m.OIDCCode, hashlib.sha256(self.code.encode()).hexdigest())
            self.assertIsNone(code.consumed_at)
            self.assertIsNone(code.access_hash)
        self.exchange()
