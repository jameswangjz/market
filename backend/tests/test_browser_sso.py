"""Browser binding, consent replay and buyer-scoped application permissions."""
import base64
import hashlib
import secrets
import unittest
from urllib.parse import parse_qs, urlsplit

import test_oidc_provider as fixture


class BrowserSSOTests(unittest.TestCase):
    setUpClass = classmethod(fixture.OIDCProviderTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.OIDCProviderTests.tearDownClass.__func__)
    setUp = fixture.OIDCProviderTests.setUp
    tearDown = fixture.OIDCProviderTests.tearDown
    request = fixture.OIDCProviderTests.request
    prepare = fixture.OIDCProviderTests.prepare
    create = fixture.OIDCProviderTests.create
    deliver = fixture.OIDCProviderTests.deliver

    def begin(self, **changes):
        self.client.cookies.clear()
        verifier = 'V' * 64
        query = {'client_id': self.cid, 'redirect_uri': 'https://saas.example.invalid/callback',
            'response_type': 'code', 'scope': 'openid profile email', 'state': secrets.token_urlsafe(24),
            'nonce': secrets.token_urlsafe(24), 'code_challenge': base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode(),
            'code_challenge_method': 'S256', 'enterprise_id': 'buyer_tenant'} | changes
        result = self.client.get('/oidc/authorize', params=query, follow_redirects=False)
        self.assertEqual(result.status_code, 303, result.text)
        self.assertIn('HttpOnly', result.headers['set-cookie'])
        self.assertIn('SameSite=lax', result.headers['set-cookie'])
        self.assertIn('Secure', result.headers['set-cookie'])
        rid = parse_qs(urlsplit(result.headers['location']).query)['request'][0]
        self.assertEqual(len(rid), 64)
        return '/api/oidc/browser/' + rid

    def headers(self, role='member', origin='https://platform.example.invalid'):
        return {'Authorization': 'Bearer ' + self.tokens[role], 'Origin': origin}

    def cookie_for_test(self):
        # TestClient uses HTTP while this fixture deliberately enforces secure cookies.
        cookie = self.client.cookies.get('market_oidc_flow')
        self.client.cookies.clear()
        self.client.cookies.set('market_oidc_flow', cookie)

    def test_consent_bridge_replay_and_cookie_binding(self):
        path = self.begin()
        self.cookie_for_test()
        response = self.client.get(path, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('nonce', response.json())
        self.assertEqual(self.client.post(path+'/approve', headers=self.headers(origin='https://evil.invalid')).status_code, 403)
        response = self.client.post(path+'/approve', headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('code=', response.json()['redirect_url'])
        self.assertEqual(self.client.post(path+'/approve', headers=self.headers()).status_code, 410)

    def test_reject_and_expiry(self):
        path = self.begin()
        self.cookie_for_test()
        response = self.client.post(path+'/reject', headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('error=access_denied', response.json()['redirect_url'])
        path = self.begin()
        self.cookie_for_test()
        with self.m.SessionLocal() as db:
            db.get(self.m.OIDCBrowserRequest, path.rsplit('/', 1)[1]).expires_at = 0
            db.commit()
        self.assertEqual(self.client.get(path, headers=self.headers()).status_code, 410)

    def test_cross_browser_and_cross_enterprise_denied(self):
        path = self.begin()
        self.client.cookies.clear()
        self.assertEqual(self.client.get(path, headers=self.headers()).status_code, 403)
        path = self.begin(enterprise_id='provider_tenant')
        self.cookie_for_test()
        self.assertEqual(self.client.get(path, headers=self.headers()).status_code, 403)

    def test_duplicate_invalid_query_denied(self):
        self.assertEqual(self.client.get('/oidc/authorize?client_id=x&client_id=y').status_code, 400)
        self.assertEqual(self.client.get('/oidc/authorize?unknown=value').status_code, 400)

    def test_application_entry_and_grant_revocation(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, 'product').application_url = 'https://saas.example.invalid/app'
            db.commit()
        result = self.request('GET', '/api/applications', 'member')
        self.assertEqual(len(result['items']), 1)
        self.assertTrue(result['items'][0]['available'])
        launch = self.request('POST', f'/api/applications/{self.sid}/launch', 'member')
        self.assertIn('enterprise_id=buyer_tenant', launch['launch_url'])
        self.request('GET', f'/api/applications/{self.sid}/members', 'member', 403)
        self.request('GET', f'/api/applications/{self.sid}/members', 'provider', 403)
        self.request('GET', '/api/applications', 'ops', 403)
        self.request('POST', f'/api/applications/{self.sid}/grants', 'buyer', json={'user_id': 'member', 'granted': False})
        self.assertEqual(self.request('GET', '/api/applications', 'member')['items'], [])
        self.request('POST', f'/api/applications/{self.sid}/launch', 'member', 403)
        self.request('POST', f'/api/applications/{self.sid}/grants', 'buyer', json={'user_id': 'member', 'granted': True})
        self.assertEqual(len(self.request('GET', '/api/applications', 'member')['items']), 1)

    def test_application_no_secrets_or_costs_and_expired_launch(self):
        response = self.request('GET', '/api/applications', 'buyer')
        for forbidden in ('secret', 'cost', 'upstream', 'password'):
            self.assertNotIn(forbidden, str(response).lower())
        with self.m.SessionLocal() as db:
            db.get(self.m.SaaSSubscription, self.sid).status = 'closed'
            db.commit()
        self.request('POST', f'/api/applications/{self.sid}/launch', 'buyer', 403)

    def test_disabled_member_can_be_revoked_without_regaining_access(self):
        with self.m.SessionLocal() as db:
            membership = db.query(self.m.Membership).filter_by(user_id='member', enterprise_id='buyer_tenant').one()
            membership.status = 'disabled'
            db.commit()
        self.request('POST', f'/api/applications/{self.sid}/grants', 'buyer',
            json={'user_id': 'member', 'granted': False})
        self.request('POST', f'/api/applications/{self.sid}/grants', 'buyer', 403,
            json={'user_id': 'member', 'granted': True})
        with self.m.SessionLocal() as db:
            membership = db.query(self.m.Membership).filter_by(user_id='member', enterprise_id='buyer_tenant').one()
            membership.status = 'active'
            db.commit()
        self.request('POST', f'/api/applications/{self.sid}/launch', 'member', 403)

    def test_grant_scope_unique_and_repeated_grants_reuse_row(self):
        from sqlalchemy.exc import IntegrityError
        with self.m.SessionLocal() as db:
            db.add(self.m.ApplicationAccessGrant(enterprise_id='buyer_tenant', product_id='product',
                user_id='member', granted_by='buyer'))
            with self.assertRaises(IntegrityError):
                db.commit()
        for granted in (False, True, True):
            self.request('POST', f'/api/applications/{self.sid}/grants', 'buyer',
                json={'user_id': 'member', 'granted': granted})
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.ApplicationAccessGrant).filter_by(enterprise_id='buyer_tenant',
                product_id='product', user_id='member').count(), 1)


if __name__ == '__main__':
    unittest.main()
