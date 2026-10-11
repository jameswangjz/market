import unittest
from sqlalchemy import inspect, text
from pydantic import ValidationError
import test_message_business_events as fixtures


class RegistrationConfigTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixtures.BusinessRoutesHTTPTests.setUp
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request

    def test_redirect_uri_whitelist_validation(self):
        valid = ['https://saas.example.com/callback', 'http://localhost:8000/callback',
                 'http://127.0.0.1/callback', 'http://market-mock-saas/callback']
        body = self.m.ProductBody(name='test', product_type='saas', delivery_method='tenant_access', oidc_redirect_uris=valid+valid)
        self.assertEqual(body.oidc_redirect_uris, valid)
        invalid = ['http://example.com/callback', 'https://user:pass@example.com/callback',
                   'https://example.com/callback#token', 'https://example.com/callback#',
                   'https://*.example.com/callback', 'javascript:alert(1)',
                   'https://example.com /callback', 'http://localhost.evil.com/callback',
                   'https://example.com:99999/callback', '//example.com/callback']
        for uri in invalid:
            with self.subTest(uri=uri), self.assertRaises(ValidationError):
                self.m.ProductBody(name='test', product_type='saas', delivery_method='tenant_access', oidc_redirect_uris=[uri])
        with self.assertRaises(ValidationError):
            self.m.ProductBody(name='test', product_type='api', delivery_method='api', oidc_redirect_uris=valid)

    def test_callback_and_compatibility_persist_and_reload(self):
        data = self.request('POST', '/api/products', actor='provider', json={
            'name': 'configuration fixture', 'product_type': 'saas', 'delivery_method': 'tenant_access',
            'oidc_redirect_uris': ['https://saas.example.com/callback'],
            'versions': [{'version_code': 'v1', 'price': 100, 'cost': 20,
                          'compatibility_mode': 'breaking', 'compatibility_notes': 'requires new request schema'}]})
        detail = self.request('GET', '/api/products/'+data['id'], actor='provider')
        self.assertEqual(detail['oidc_redirect_uris'], ['https://saas.example.com/callback'])
        self.assertEqual(detail['versions'][0]['compatibility_mode'], 'breaking')
        self.assertEqual(detail['versions'][0]['compatibility_notes'], 'requires new request schema')
        with self.m.SessionLocal() as db:
            version = db.get(self.m.ProductReleaseVersion, detail['versions'][0]['id'])
            db.add(self.m.Order(order_no='CONFIG-PAID', product_id=data['id'], product_version_id=version.id,
                buyer_enterprise_id='buyer_tenant', provider_enterprise_id='provider_tenant',
                buyer_user_id='buyer', product_name='test', buyer_name='buyer', amount=100, paid_amount=100))
            db.commit()
            with self.assertRaises(self.m.HTTPException):
                self.m.trading_policy['protect_sold_version'](db, version, {'compatibility_mode': 'compatible'})
        self.request('PUT', '/api/products/'+data['id'], actor='provider', expected=409, json={
            'name': 'configuration fixture', 'product_type': 'saas', 'delivery_method': 'tenant_access',
            'oidc_redirect_uris': ['https://saas.example.com/new-callback']})

    def test_compatibility_rejects_unknown_mode(self):
        with self.assertRaises(ValidationError):
            self.m.ProductVersionBody(version_code='v1', compatibility_mode='unknown')

    def test_old_schema_additions_are_complete_and_repeatable(self):
        expected = {'product_release_versions': ['cost', 'quota_unit', 'quota_amount', 'compatibility_mode', 'compatibility_notes'],
                    'saas_product_versions': ['cost', 'compatibility_mode', 'compatibility_notes']}
        with self.m.engine.begin() as connection:
            for table, columns in expected.items():
                for column in columns:
                    connection.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))
        self.m.ensure_review_and_file_schema()
        self.m.ensure_review_and_file_schema()
        for table, columns in expected.items():
            actual = {column['name'] for column in inspect(self.m.engine).get_columns(table)}
            self.assertTrue(set(columns) <= actual)


if __name__ == '__main__':
    unittest.main()
