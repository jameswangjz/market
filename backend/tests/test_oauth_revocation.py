"""Machine-token revocation and live client checks through production HTTP."""
import base64
from datetime import datetime, timezone
import hashlib
import unittest

import test_subscription_terms as fixture


class OAuthRevocationTests(unittest.TestCase):
    setUpClass = classmethod(fixture.SubscriptionTermsTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.SubscriptionTermsTests.tearDownClass.__func__)
    tearDown = fixture.SubscriptionTermsTests.tearDown

    def setUp(self):
        fixture.SubscriptionTermsTests.setUp(self)
        self.m.now.return_value = datetime.now(timezone.utc)
        with self.m.SessionLocal() as db:
            db.add(self.m.OAuthClient(id='machine', product_id='product', client_id='machine-client',
                client_secret='fixture-secret', scope='saas.tenant resource.invoke', status='active'))
            db.add(self.m.Product(id='foreign-product', name='foreign', enterprise_id='provider_tenant', product_type='api'))
            db.flush()
            db.add(self.m.OAuthClient(id='foreign', product_id='foreign-product', client_id='foreign-client',
                client_secret='foreign-secret', scope='resource.invoke', status='active'))
            db.commit()

    def mint(self, client='machine-client', secret='fixture-secret', expected=200):
        response = self.client.post('/oauth/token', data={'grant_type': 'client_credentials',
            'client_id': client, 'client_secret': secret})
        self.assertEqual(response.status_code, expected)
        return response.json().get('access_token')

    def active(self, token):
        return self.client.post('/oauth/introspect', data={'token': token}).json()['active']

    def revoke(self, token, secret='fixture-secret', basic=False):
        body = {'token': token, 'client_id': 'machine-client', 'client_secret': secret}
        headers = {}
        if basic:
            body = {'token': token}
            headers['Authorization'] = 'Basic ' + base64.b64encode(
                ('machine-client:' + secret).encode()).decode()
        return self.client.post('/oauth/revoke', data=body, headers=headers)

    def test_individual_revocation_is_durable_idempotent_and_does_not_revoke_other_tokens(self):
        first, second = self.mint(), self.mint()
        self.assertNotEqual(first, second)
        self.assertTrue(self.active(first))
        self.assertEqual(self.revoke(first, basic=True).status_code, 200)
        self.assertFalse(self.active(first))
        self.assertTrue(self.active(second))
        self.assertEqual(self.revoke(first).status_code, 200)
        self.assertTrue(self.active(self.mint()))
        with self.m.SessionLocal() as db:
            rows = db.query(self.m.OAuthRevokedToken).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].token_hash, hashlib.sha256(first.encode()).hexdigest())
            for record in db.query(self.m.AuditLog).all():
                self.assertNotIn(first, str(record.detail))
                self.assertNotIn('fixture-secret', str(record.detail))

    def test_revoked_client_rejects_existing_tokens_and_new_issuance(self):
        token = self.mint()
        with self.m.SessionLocal() as db:
            db.get(self.m.OAuthClient, 'machine').status = 'revoked'
            db.commit()
        self.assertFalse(self.active(token))
        self.mint(expected=401)

    def test_scope_removal_and_product_mismatch_fail_closed(self):
        token = self.mint()
        with self.m.SessionLocal() as db:
            db.get(self.m.OAuthClient, 'machine').scope = 'resource.invoke'
            db.commit()
        self.assertFalse(self.active(token))
        token = self.mint()
        with self.m.SessionLocal() as db:
            db.delete(db.get(self.m.OAuthClient, 'foreign'))
            db.flush()
            db.get(self.m.OAuthClient, 'machine').product_id = 'foreign-product'
            db.commit()
        self.assertFalse(self.active(token))

    def test_unknown_and_foreign_token_revocation_do_not_disclose_or_mutate(self):
        foreign = self.mint('foreign-client', 'foreign-secret')
        for token in ('unknown', foreign):
            response = self.revoke(token)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertTrue(self.active(foreign))
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.OAuthRevokedToken).count(), 0)

    def test_wrong_client_auth_and_missing_token_rejected(self):
        token = self.mint()
        self.assertEqual(self.revoke(token, secret='incorrect').status_code, 401)
        self.assertEqual(self.revoke('').status_code, 400)
        self.assertTrue(self.active(token))


if __name__ == '__main__':
    unittest.main()
