"""Buyer API credential storage schema and list response contract."""
import unittest

from sqlalchemy import inspect

import test_version_change_quotes as fixture


class ApiCredentialStorageTests(unittest.TestCase):
    setUpClass = classmethod(fixture.VersionChangeQuoteTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.VersionChangeQuoteTests.tearDownClass.__func__)
    tearDown = fixture.VersionChangeQuoteTests.tearDown
    request = fixture.VersionChangeQuoteTests.request
    create = fixture.VersionChangeQuoteTests.create
    deliver = fixture.VersionChangeQuoteTests.deliver
    prepare = fixture.VersionChangeQuoteTests.prepare

    def setUp(self):
        fixture.VersionChangeQuoteTests.setUp(self)

    def test_schema_and_order_list_report_download_availability_without_secret(self):
        oid, _ = self.prepare(future=False)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            route = self.m.ApiGatewayRoute(product_id=order.product_id,
                route_key='credential-storage-route', upstream_url='https://upstream.invalid',
                version=order.product_version_code, status='active')
            db.add(route)
            db.flush()
            credential = self.m.ApiCredential(route_id=route.id,
                enterprise_id=order.buyer_enterprise_id, order_id='',
                key_hash='credential-storage-hash', secret_encrypted='encrypted-fixture-only',
                apisix_consumer_name='fixture-consumer', status='active')
            db.add(credential)
            db.commit()
            column_names = {column['name'] for column in inspect(db.bind).get_columns('api_credentials')}
            self.assertIn('secret_encrypted', column_names)

        result = self.request('GET', f'/api/orders/{oid}/api-credentials')
        self.assertEqual(len(result['items']), 1)
        listed = result['items'][0]
        self.assertTrue(listed['secret_download_available'])
        self.assertNotIn('secret_encrypted', listed)
        self.assertNotIn('api_key', listed)
        self.assertNotIn('encrypted-fixture-only', str(result))


if __name__ == '__main__':
    unittest.main()
