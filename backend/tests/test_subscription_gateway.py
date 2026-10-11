"""HTTP integration of subscription grants and non-additive gateway policy."""
import json
import unittest
from unittest.mock import patch

import test_message_business_events as fixtures


class SubscriptionGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.BusinessRoutesHTTPTests.setUpClass.__func__(cls)

    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixtures.BusinessRoutesHTTPTests.setUp
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request

    def purchase(self, months=1):
        m = self.m
        with m.SessionLocal() as db:
            db.get(m.Product, "product").delivery_method = "api"
            version = db.get(m.ProductReleaseVersion, "version")
            version.rate_limit_per_minute, version.daily_quota, version.monthly_quota = 60, 10, 1000
            db.commit()
        with patch.dict(m.storefront, {"versions_for": lambda db, p: [{"id": "version"}]}):
            result = self.request("POST", "/api/orders", json={"product_id": "product",
                "product_version_id": "version", "buyer_enterprise_id": "buyer_tenant",
                "subscription_months": months})
        return result

    def deliver(self, order):
        oid = order["id"]
        self.request("POST", f"/api/orders/{oid}/transition", json={"action": "confirm_payment"})
        with self.m.SessionLocal() as db:
            tid = db.scalar(self.select(self.m.DeliveryTask.id).where(self.m.DeliveryTask.order_id == oid))
        with patch.dict(self.m.subscription_gateway, {"verify_delivery": lambda *args: True}):
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
        self.request("POST", f"/api/delivery-tasks/{tid}/process", actor="platform", expected=409, json={"success": True})
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.scalar(self.select(self.m.SubscriptionTerm.id)))
            self.assertEqual(db.get(self.m.DeliveryTask, tid).status, "in_delivery")

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
            route = self.m.ApiGatewayRoute(product_id="product", version="v1", route_key="test", upstream_url="https://example.com")
            db.add(route); db.flush()
            policy = self.m.subscription_gateway["policy"](db, "buyer_tenant", route)
            self.assertEqual(policy["monthly_quota"], 1000)
            self.assertEqual(policy["total_quota"], 0)

    def test_metadata_uses_frozen_quota_and_preserves_revoked_status(self):
        order = self.purchase()
        self.deliver(order)
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order["id"])
            route = self.m.ApiGatewayRoute(product_id="product", version=saved.product_version_code,
                route_key="test", upstream_url="https://example.com")
            db.add(route); db.flush()
            credential = self.m.ApiCredential(route_id=route.id, enterprise_id="buyer_tenant", key_hash="own-key",
                status="revoked", apisix_consumer_name="own-consumer")
            db.get(self.m.ProductReleaseVersion, "version").monthly_quota = 999999
            data = self.m.subscription_gateway["metadata"](db, credential, route)
            self.assertEqual(data["mode"], "subscription")
            self.assertEqual(data["status"], "revoked")
            self.assertEqual(json.loads(data["subscription_periods"])[0]["monthly_quota"], 1000)
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
            route = self.m.ApiGatewayRoute(product_id="product", version=saved.product_version_code,
                route_key="own-route", upstream_url="https://example.com")
            db.add(route); db.flush()
            credential = self.m.ApiCredential(route_id=route.id, enterprise_id="buyer_tenant", key_hash="own-key",
                status="active", apisix_consumer_name="own-consumer")
            client = Mock()
            client.hgetall.return_value = {"status": "active"}
            client.scan_iter.return_value = iter(["own-existing-counter"])
            client.mget.return_value = [8]
            with patch.object(self.m, "apisix_enabled", return_value=True), patch.object(self.m, "apisix_policy_redis", return_value=client):
                with self.assertRaises(self.m.HTTPException) as error:
                    self.m.subscription_gateway["sync"](db, credential, route)
                self.assertEqual(error.exception.status_code, 409)
            client.hset.assert_not_called()
            client.delete.assert_not_called()
            client.set.assert_not_called()


if __name__ == "__main__":
    unittest.main()
