"""HTTP integration of subscription grants and non-additive gateway policy."""
import json
import os
import unittest
from unittest.mock import Mock
from unittest.mock import patch
from cryptography.fernet import Fernet

import test_message_business_events as fixtures


class SubscriptionGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.BusinessRoutesHTTPTests.setUpClass.__func__(cls)

    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request

    def setUp(self):
        fixtures.BusinessRoutesHTTPTests.setUp(self)
        encryption_env = patch.dict(os.environ, {
            'API_CREDENTIAL_ENCRYPTION_KEY': Fernet.generate_key().decode(),
            'API_GATEWAY_PUBLIC_URL': 'https://gateway.example.invalid',
        })
        encryption_env.start()
        self.addCleanup(encryption_env.stop)
        self.redis_hashes = {}
        self.redis = Mock()
        self.redis.hgetall.side_effect = lambda key: dict(self.redis_hashes.get(key, {}))
        self.redis.hset.side_effect = self.redis_hset
        self.redis.delete.side_effect = self.redis_delete
        self.redis.scan.side_effect = lambda cursor, match, count: (0, [])
        self.redis.dbsize.return_value = 0
        self.redis.info.side_effect = lambda section: (
            {'cluster_enabled': 0} if section == 'cluster' else {'role': 'master'})
        self.redis.eval.side_effect = self.redis_eval
        self.redis_patch = patch.object(self.m, 'apisix_policy_redis', return_value=self.redis)
        self.redis_patch.start()
        self.addCleanup(self.redis_patch.stop)
        self.apisix_consumers = {}

    def redis_hset(self, key, mapping):
        self.redis_hashes.setdefault(key, {}).update(
            {str(field): str(value) for field, value in mapping.items()})

    def redis_delete(self, key):
        return int(self.redis_hashes.pop(key, None) is not None)

    def redis_eval(self, _script, numkeys, *args):
        if "HSETNX" in _script and "baseline" in _script:
            self.redis_hashes.setdefault(args[1], {}).setdefault('baseline', '0')
            return 1
        data = json.loads(args[numkeys])
        self.redis_hashes.setdefault(args[0], {}).update(
            {str(field): str(value) for field, value in data.items()})
        return [1]

    def admin(self, method, path, payload=None):
        if method == 'GET' and path.startswith('/routes/'):
            return self.expected_route
        if path.startswith('/consumers/'):
            name = path.rsplit('/', 1)[-1]
            if method == 'PUT':
                if not isinstance(payload, dict) or payload.get('username') != name:
                    raise AssertionError((method, path, payload))
                self.apisix_consumers[name] = json.loads(json.dumps(payload))
                return {'node': {'value': self.apisix_consumers[name]}}
            if method == 'GET':
                return {'node': {'value': self.apisix_consumers[name]}}
            if method == 'DELETE':
                self.apisix_consumers.pop(name, None)
                return {'node': {'value': {'username': name}}}
        raise AssertionError((method, path, payload))

    def purchase(self, months=1):
        m = self.m
        with m.SessionLocal() as db:
            product = db.get(m.Product, "product")
            product.product_type, product.delivery_method = "api", "api"
            version = db.get(m.ProductReleaseVersion, "version")
            version.rate_limit_per_minute, version.daily_quota, version.monthly_quota = 60, 10, 1000
            db.commit()
        with patch.dict(m.storefront, {"versions_for": lambda db, p: [{"id": "version"}]}):
            result = self.request("POST", "/api/orders", json={"product_id": "product",
                "product_version_id": "version", "buyer_enterprise_id": "buyer_tenant",
                "subscription_months": months})
        with m.SessionLocal() as db:
            route = db.get(m.ApiGatewayRoute, 'api-route')
            if not route:
                route = m.ApiGatewayRoute(id='api-route', product_id='product', version='v1',
                    route_key='api-route', upstream_url='https://example.com', status='active')
                db.add(route)
                db.flush()
                config = m.version_gateway['Config'](id='api-config', product_id='product',
                    version_id='version', route_id=route.id, upstream_url=route.upstream_url,
                    route_key=route.route_key, version_code='v1', auth_mode='api_key',
                    upstream_auth_mode=route.upstream_auth_mode, upstream_scope=route.upstream_scope,
                    timeout_ms=route.timeout_ms, strip_prefix=route.strip_prefix,
                    health_method=route.health_method, health_path=route.health_path,
                    rate_limit_per_minute=60, daily_quota=10, monthly_quota=1000,
                    status='verified', healthy=True, verified_at=m.now(), health_status_code=200,
                    evidence_hash='test-evidence')
                db.add(config)
            expected_route = m.apisix_route_payload(route, db.get(m.Product, 'product'), db)
            db.commit()
        self.expected_route = expected_route
        return result

    def deliver(self, order):
        oid = order["id"]
        self.request("POST", f"/api/orders/{oid}/transition", json={"action": "confirm_payment"})
        with self.m.SessionLocal() as db:
            tid = db.scalar(self.select(self.m.DeliveryTask.id).where(self.m.DeliveryTask.order_id == oid))
        with patch.dict(self.m.subscription_gateway, {"verify_delivery": lambda *args: True}), \
                patch.object(self.m, 'apisix_enabled', return_value=True), \
                patch.object(self.m, 'apisix_admin_request', side_effect=self.admin):
            return self.request("POST", f"/api/delivery-tasks/{tid}/process", actor="platform", json={"success": True})

    def test_paid_not_delivered_has_no_entitlement_or_credentials(self):
        order = self.purchase()
        self.request("POST", f'/api/orders/{order["id"]}/transition', json={"action": "confirm_payment"})
        with self.m.SessionLocal() as db:
            self.assertIsNone(self.m.subscription_terms["current_entitlement"](db, "buyer_tenant", "product"))
        self.request("GET", f'/api/orders/{order["id"]}/api-credentials', expected=409)

    def test_unverified_route_cannot_start_subscription_clock(self):
        order = self.purchase()
        self.request("POST", f'/api/orders/{order["id"]}/transition', json={"action": "confirm_payment"})
        with self.m.SessionLocal() as db:
            tid = db.scalar(self.select(self.m.DeliveryTask.id).where(self.m.DeliveryTask.order_id == order["id"]))
        with patch.object(self.m, 'apisix_enabled', return_value=True), \
                patch.object(self.m, 'apisix_admin_request', return_value={'node': {'value': {'uri': '/wrong'}}}):
            result = self.request("POST", f"/api/delivery-tasks/{tid}/process", actor="platform", json={"success": True})
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.scalar(self.select(self.m.SubscriptionTerm.id)))
            self.assertEqual(result['task']['status'], 'retrying')
            self.assertEqual(db.get(self.m.DeliveryTask, tid).status, "retrying")

    def test_two_delivered_orders_queue_periods_and_do_not_add_quota(self):
        first = self.purchase(1)
        self.deliver(first)
        second = self.purchase(2)
        self.deliver(second)
        with self.m.SessionLocal() as db:
            periods = self.m.subscription_terms["quota_periods"](db, "buyer_tenant", "product")
            self.assertEqual(len(periods), 3)
            self.assertEqual({p["monthly_quota"] for p in periods}, {1000})
            self.assertEqual(len({p["period_key"] for p in periods}), 3)
            combo = db.scalar(self.select(self.m.SubscriptionCombination))
            self.assertEqual(combo.total_months, 3)
            route = db.get(self.m.ApiGatewayRoute, 'api-route')
            policy = self.m.subscription_gateway["policy"](db, "buyer_tenant", route)
            self.assertEqual(policy["monthly_quota"], 1000)
            self.assertEqual(policy["total_quota"], 0)

    def test_metadata_uses_frozen_quota_and_preserves_revoked_status(self):
        order = self.purchase()
        self.deliver(order)
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order["id"])
            route = db.get(self.m.ApiGatewayRoute, 'api-route')
            credential = self.m.ApiCredential(route_id=route.id, enterprise_id="buyer_tenant", key_hash="own-key",
                status="revoked", apisix_consumer_name="own-consumer")
            db.get(self.m.ProductReleaseVersion, "version").monthly_quota = 999999
            data = self.m.subscription_gateway["metadata"](db, credential, route)
            self.assertEqual(data["mode"], "subscription")
            self.assertEqual(data["status"], "revoked")
            self.assertEqual(json.loads(data["subscription_periods"])[0]["monthly_quota"], 1000)
            period = json.loads(data["subscription_periods"])[0]
            self.assertEqual(period['order_id'], saved.id)
            self.assertTrue(period['segment_id'])
            self.assertEqual(data["total_quota"], 0)

    def test_zero_credential_quota_is_not_replaced_by_route_default(self):
        route = self.m.ApiGatewayRoute(rate_limit_per_minute=60, daily_quota=100, monthly_quota=1000)
        item = self.m.ApiCredential(daily_quota=0, monthly_quota=0, rate_limit_per_minute=10)
        result = self.m.api_credential_out(item, route)
        self.assertEqual((result["daily_quota"], result["monthly_quota"]), (0, 0))

    def test_legacy_usage_migration_fails_closed_without_reset(self):
        from unittest.mock import Mock
        order = self.purchase()
        self.deliver(order)
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order["id"])
            route = db.get(self.m.ApiGatewayRoute, 'api-route')
            credential = self.m.ApiCredential(route_id=route.id, enterprise_id="buyer_tenant", key_hash="own-key",
                status="active", apisix_consumer_name="own-consumer")
            client = Mock()
            client.hgetall.return_value = {"status": "active"}
            client.scan_iter.return_value = iter(["own-existing-counter"])
            client.mget.return_value = [8]
            client.eval.return_value = [0]
            with patch.object(self.m, "apisix_enabled", return_value=True), patch.object(self.m, "apisix_policy_redis", return_value=client):
                with self.assertRaises(self.m.HTTPException) as error:
                    self.m.subscription_gateway["sync"](db, credential, route)
                self.assertEqual(error.exception.status_code, 409)
            client.hset.assert_not_called()
            client.delete.assert_not_called()
            client.set.assert_not_called()

    def test_subscription_scope_conflict_returns_409_without_direct_write(self):
        order = self.purchase()
        self.deliver(order)
        with self.m.SessionLocal() as db:
            route = db.get(self.m.ApiGatewayRoute, 'api-route')
            credential = self.m.ApiCredential(route_id=route.id, enterprise_id='buyer_tenant',
                key_hash='own-key', status='active', apisix_consumer_name='own-consumer')
            client = Mock()
            client.hgetall.return_value = {'mode': 'subscription', 'quota_scope': 'other-combo'}
            client.eval.return_value = [0]
            with patch.object(self.m, 'apisix_enabled', return_value=True), \
                    patch.object(self.m, 'apisix_policy_redis', return_value=client):
                with self.assertRaises(self.m.HTTPException) as error:
                    self.m.subscription_gateway['sync'](db, credential, route)
            self.assertEqual(error.exception.status_code, 409)
            client.hset.assert_not_called()
            client.delete.assert_not_called()
            client.set.assert_not_called()

    def test_session_exposes_verified_enterprise_and_active_memberships(self):
        with self.m.SessionLocal() as db:
            db.add(self.m.Membership(user_id="buyer", enterprise_id="other_tenant",
                                   role="enterprise_admin", status="active"))
            db.add(self.m.Membership(user_id="buyer", enterprise_id="provider_tenant",
                                   role="enterprise_admin", status="disabled"))
            db.commit()
        data = self.request("GET", "/api/auth/me")
        self.assertEqual(data["enterprise"]["verification_status"], "verified")
        self.assertEqual(data["membership_status"], "active")
        self.assertEqual({member["enterprise_id"] for member in data["user"]["enterprise_memberships"]},
                         {"buyer_tenant", "other_tenant"})
        self.assertTrue(all(member["status"] == "active" for member in data["user"]["enterprise_memberships"]))

    def test_platform_session_never_adopts_legacy_enterprise_membership(self):
        data = self.request("GET", "/api/auth/me", actor="platform")
        self.assertIsNone(data["enterprise"])
        self.assertEqual(data["membership_status"], "none")
        self.assertEqual(data["user"]["enterprise_memberships"], [])

    def test_real_session_and_subscription_contract_enables_safe_renewal(self):
        order = self.purchase(2)
        self.deliver(order)
        session = self.request("GET", "/api/auth/me")
        item = self.request("GET", f'/api/orders/{order["id"]}/subscription')["item"]
        self.assertTrue(item["can_renew"])
        self.assertEqual(session["enterprise"]["verification_status"], "verified")
        member = next(m for m in session["user"]["enterprise_memberships"]
                      if m["enterprise_id"] == order["buyer_enterprise_id"])
        self.assertEqual(member["role"], "super_admin")
        term = next(t for t in item["terms"] if t["order_id"] == order["id"])
        self.assertEqual(term["generation"], item["generation"])
        self.assertEqual(term["start_month"], 0)
        self.assertEqual(term["months"], 2)
        self.assertEqual(order["product_version_id"], item["version_id"])
        self.assertEqual(order["product_version_code"], item["version_code"])


if __name__ == "__main__":
    unittest.main()
