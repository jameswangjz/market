"""Publication and repeatable provider downloads, using production HTTP routes."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_oidc_provider as fixture


class SaaSPublicationCredentialTests(unittest.TestCase):
    setUpClass = classmethod(fixture.OIDCProviderTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.OIDCProviderTests.tearDownClass.__func__)
    tearDown = fixture.OIDCProviderTests.tearDown
    request = fixture.OIDCProviderTests.request
    prepare = fixture.OIDCProviderTests.prepare
    create = fixture.OIDCProviderTests.create
    deliver = fixture.OIDCProviderTests.deliver

    def setUp(self):
        fixture.OIDCProviderTests.setUp(self)
        with self.m.SessionLocal() as db:
            product = db.get(self.m.Product, 'product')
            product.application_url = 'https://saas.example.invalid/app'
            product.integration_api_url = 'https://saas.example.invalid/isv.php'
            db.commit()

    def download(self, actor='provider', endpoint='oidc-credentials-download'):
        return self.client.get('/api/products/product/' + endpoint,
            headers={'Authorization': 'Bearer ' + self.tokens[actor]})

    def pending_final_review(self):
        with self.m.SessionLocal() as db:
            db.query(self.m.OIDCClient).delete()
            db.query(self.m.OAuthClient).delete()
            product = db.get(self.m.Product, 'product')
            product.status = 'operation_review'
            db.commit()

    def test_final_review_creates_distinct_clients_and_repeat_downloads(self):
        self.pending_final_review()
        result = self.request('POST', '/api/products/product/review', 'ops',
            json={'decision': 'approve', 'comment': 'Approved for integration'})
        self.assertEqual(result['status'], 'pending_integration')
        first = self.download()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers['cache-control'], 'no-store')
        credentials = first.json()
        self.assertEqual(credentials['code_challenge_method'], 'S256')
        self.assertEqual(self.download().json(), credentials)
        machine = self.download(endpoint='oauth-credentials-download')
        self.assertEqual(machine.status_code, 200, machine.text)
        self.assertIn('saas.department', machine.text)
        self.assertNotIn(credentials['client_secret'], machine.text)
        with self.m.SessionLocal() as db:
            oidc = db.query(self.m.OIDCClient).one()
            oauth = db.query(self.m.OAuthClient).one()
            self.assertNotEqual(oidc.client_id, oauth.client_id)
            self.assertNotEqual(oidc.secret_encrypted, credentials['client_secret'])
            for log in db.query(self.m.AuditLog).all():
                self.assertNotIn(credentials['client_secret'], str(log.detail))
                self.assertNotIn(oauth.client_secret, str(log.detail))
            db.get(self.m.Product, 'product').status = 'operation_review'
            db.commit()
        self.request('POST', '/api/products/product/review', 'ops',
            json={'decision': 'approve', 'comment': 'Republish'})
        self.assertEqual(self.download().json(), credentials)

    def test_only_verified_provider_admins_can_download(self):
        self.assertEqual(self.download().status_code, 200)
        for actor in ('buyer', 'outsider', 'member', 'platform', 'ops', 'finance'):
            self.assertEqual(self.download(actor).status_code, 403, actor)
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'provider').verified_status = 'pending'
            db.commit()
        self.assertEqual(self.download().status_code, 403)

    def test_unapproved_product_cannot_download(self):
        for state in ('draft', 'pending_review', 'quality_review', 'security_review', 'rejected'):
            with self.m.SessionLocal() as db:
                db.get(self.m.Product, 'product').status = state
                db.commit()
            self.assertEqual(self.download().status_code, 409, state)

    def test_missing_encryption_key_rolls_back_final_approval(self):
        self.pending_final_review()
        with patch.dict(os.environ, OIDC_CREDENTIAL_ENCRYPTION_KEY=''):
            self.request('POST', '/api/products/product/review', 'ops', 503,
                json={'decision': 'approve', 'comment': 'Not committed'})
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Product, 'product').status, 'operation_review')
            self.assertEqual(db.query(self.m.OIDCClient).count(), 0)
            self.assertEqual(db.query(self.m.OAuthClient).count(), 0)

    def test_legacy_hash_only_or_disabled_client_is_not_silently_rotated(self):
        with self.m.SessionLocal() as db:
            client = db.query(self.m.OIDCClient).one()
            saved = client.client_id
            client.secret_encrypted = None
            db.commit()
        self.assertEqual(self.download().status_code, 409)
        self.request('POST', '/api/products/product/oidc-client', 'ops', 409)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.OIDCClient).one().client_id, saved)

    def test_provider_verification_publishes_without_claiming_browser_sso(self):
        self.pending_final_review()
        self.request('POST', '/api/products/product/review', 'ops',
            json={'decision': 'approve', 'comment': 'Approved'})
        with patch.object(self.m, 'saas_access_token', return_value='private-fixture-token'), \
                patch.object(self.m, 'validated_http_request', return_value=SimpleNamespace(status_code=200)):
            result = self.request('POST', '/api/products/product/saas-integration/verify', 'provider')
        self.assertEqual(result['product']['status'], 'published')
        self.assertFalse(result['browser_sso_end_to_end_tested'])
        self.assertEqual(result['integration']['verification_status'], 'verified')


if __name__ == '__main__':
    unittest.main()
