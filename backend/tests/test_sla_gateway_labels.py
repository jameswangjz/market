"""Real payload generation remains compatible with unversioned legacy routes."""
import unittest

import test_order_sla_contracts as fixture


class GatewayLabelTests(unittest.TestCase):
    setUpClass = classmethod(fixture.OrderSlaContractTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.OrderSlaContractTests.tearDownClass.__func__)
    setUp = fixture.OrderSlaContractTests.setUp
    tearDown = fixture.OrderSlaContractTests.tearDown
    request = fixture.OrderSlaContractTests.request
    create_order = fixture.OrderSlaContractTests.create_order

    def test_unknown_legacy_version_has_no_empty_apisix_labels(self):
        route = self.m.ApiGatewayRoute(id='legacy-route', route_key='legacy-route',
            product_id='product', upstream_url='https://example.invalid', version='legacy-no-version',
            rate_limit_per_minute=60, daily_quota=0, monthly_quota=0,
            health_path='/health', health_method='GET', upstream_auth_mode='none')
        with self.m.SessionLocal() as db:
            product = db.get(self.m.Product, 'product')
            labels = self.m.apisix_route_payload(route, product, db)['labels']
        self.assertNotIn('market_version_id', labels)
        self.assertTrue(all(isinstance(value, str) and 0 < len(value) <= 256 for value in labels.values()))
        self.assertEqual(labels['market_route_id'], 'legacy-route')


if __name__ == '__main__':
    unittest.main()
