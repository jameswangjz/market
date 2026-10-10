"""Isolated HTTP and persistence regression for TRD-BE-005."""
import json
import unittest
from unittest.mock import patch

import test_message_business_events as fixtures


class OrderEconomicsTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixtures.BusinessRoutesHTTPTests.setUp
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request

    def body(self, **extra):
        return {"product_id": "product", "product_version_id": "version", "buyer_enterprise_id": "buyer_tenant", **extra}

    def quote(self, **extra):
        return self.request("POST", "/api/orders/quote", json=self.body(**extra))

    def create(self, **extra):
        return self.request("POST", "/api/orders", json=self.body(**extra))

    def monthly(self, method="api", saas=False):
        m = self.m
        with m.SessionLocal() as db:
            product = db.get(m.Product, "product")
            product.delivery_method = method
            if saas:
                product.product_type = "saas"
                db.add(m.SaaSProductVersion(id="saas_version", product_id=product.id, name="V1", version_code="v1", monthly_price=100, cost=10, status="active"))
            db.commit()

    def test_quote_has_no_persistent_side_effects_and_hides_internal_fields(self):
        quote = self.quote()
        self.assertEqual(quote["amount"], 100)
        self.assertEqual(quote["billing_unit"], "order")
        self.assertEqual(len(quote["quote_id"]), 64)
        self.assertNotIn("cost", json.dumps(quote))
        self.assertNotIn("settlement_rule", quote)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(self.select(self.func.count()).select_from(self.m.Order)), 0)
            self.assertEqual(db.scalar(self.select(self.func.count()).select_from(self.m.Payment)), 0)

    def test_monthly_price_cost_multiply_for_api_model_and_saas(self):
        m = self.m
        for method in ("api", "model_api", "tenant_access"):
            self.monthly(method)
            with patch.dict(m.storefront, {"versions_for": lambda db, p: [{"id": "version"}]}):
                quote = self.quote(subscription_months=3)
                order = self.create(subscription_months=3, quote_id=quote["quote_id"])
            self.assertEqual(order["amount"], 300)
            self.assertEqual(order["subscription_months"], 3)
            with m.SessionLocal() as db:
                saved = db.get(m.Order, order["id"])
                self.assertEqual(float(saved.total_cost_snapshot), 30)
                self.assertEqual(m.order_cost(db, saved), m.Decimal("30.00"))
                self.assertEqual(float(db.scalar(self.select(m.Payment).where(m.Payment.order_id == saved.id)).amount), 300)
        self.monthly("tenant_access", saas=True)
        with patch.dict(m.storefront, {"versions_for": lambda db, p: [{"id": "saas_version"}]}):
            order = self.create(product_version_id="saas_version", subscription_months=2)
        self.assertEqual(order["amount"], 200)

    def test_month_validation_and_non_monthly_rejection(self):
        for months in (0, 37, 1.5, True, "3"):
            self.request("POST", "/api/orders/quote", json=self.body(subscription_months=months), expected=422)
        self.request("POST", "/api/orders", json=self.body(subscription_months=2), expected=400)
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, "product").delivery_method = "consulting"
            db.commit()
        self.request("POST", "/api/orders/quote", json=self.body(subscription_months=2), expected=400)

    def test_quote_rejects_foreign_enterprise_pending_member_and_platform_account(self):
        self.request("POST", "/api/orders/quote", actor="outsider", json=self.body(), expected=403)
        self.request("POST", "/api/orders/quote", actor="inactive", json=self.body(), expected=403)
        self.request("POST", "/api/orders/quote", actor="platform", json=self.body(), expected=403)

    def test_changed_quote_rejected_without_creating_order(self):
        quote = self.quote()
        with self.m.SessionLocal() as db:
            db.get(self.m.ProductReleaseVersion, "version").price = 120
            db.commit()
        self.request("POST", "/api/orders", json=self.body(quote_id=quote["quote_id"]), expected=409)
        self.assertEqual(self.quote()["amount"], 120)

    def test_order_retains_cost_rule_and_delivery_after_catalog_edits(self):
        m = self.m
        order = self.create()
        with m.SessionLocal() as db:
            version = db.get(m.ProductReleaseVersion, "version")
            version.cost = 99
            version.price = 200
            rule = db.get(m.SettlementRule, "trading_rule")
            rule.platform_rate = 20
            product = db.get(m.Product, "product")
            product.delivery_method = "consulting"
            product.download_limit = 999
            db.commit()
            saved = db.get(m.Order, order["id"])
            self.assertEqual(saved.snapshot_version, 1)
            self.assertEqual(m.order_cost(db, saved), m.Decimal("10.00"))
            rates, version = m.settlement_values_for_order(db, saved, rule)
            self.assertEqual(rates["platform_rate"], m.Decimal("8"))
            self.assertEqual(version, "global:v1")
            self.assertEqual(m.delivery_task_config(db, saved)[0], "file")
            self.assertEqual(json.loads(saved.delivery_snapshot_json)["download_limit"], 0)
        for path in ("/api/orders", f'/api/orders/{order["id"]}'):
            response = self.request("GET", path)
            self.assertNotIn("cost", json.dumps(response))
            self.assertNotIn("upstream_url", json.dumps(response))

    def test_custom_rule_is_snapshotted_with_channel_share(self):
        m = self.m
        with m.SessionLocal() as db:
            p = db.get(m.Product, "product")
            p.settlement_rule_mode = "custom"
            p.settlement_rule_json = json.dumps({"platform_rate": 20, "provider_rate": 75, "service_rate": 0, "expert_rate": 0, "channel_rate": 5})
            db.commit()
        order = self.create()
        with m.SessionLocal() as db:
            saved = db.get(m.Order, order["id"])
            saved.paid_amount = saved.amount; saved.payment_status = "paid"
            db.commit()
        settlement = self.request("POST", f'/api/settlements/generate/{order["id"]}', actor="finance")
        self.assertEqual(settlement["channel_fee"], 4.5)
        self.assertEqual(settlement["platform_fee"], 18)

    def test_unavailable_version_and_missing_rule_fail_closed(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.ProductReleaseVersion, "version").status = "disabled"
            db.commit()
        self.request("POST", "/api/orders", json=self.body(), expected=400)
        with self.m.SessionLocal() as db:
            db.get(self.m.ProductReleaseVersion, "version").status = "active"
            db.get(self.m.SettlementRule, "trading_rule").status = "draft"
            db.commit()
        self.request("POST", "/api/orders", json=self.body(), expected=409)

    def test_price_less_than_cost_and_money_overflow_fail(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.ProductReleaseVersion, "version").cost = 101
            db.commit()
        self.request("POST", "/api/orders", json=self.body(), expected=409)
        self.monthly()
        with self.m.SessionLocal() as db:
            v = db.get(self.m.ProductReleaseVersion, "version")
            v.cost = 0; v.price = 999999999999
            db.commit()
        with patch.dict(self.m.storefront, {"versions_for": lambda db, p: [{"id": "version"}]}):
            self.request("POST", "/api/orders", json=self.body(subscription_months=36), expected=400)


if __name__ == "__main__":
    unittest.main()
