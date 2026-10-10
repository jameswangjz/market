"""Buyer snapshot privacy and fail-closed HTTP checks on isolated SQLite only."""
import json
import unittest
from unittest.mock import patch

import test_message_business_events as fixtures


class OrderSnapshotPrivacyTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixtures.BusinessRoutesHTTPTests.setUp
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request
    pay = fixtures.BusinessRoutesHTTPTests.pay
    fail_after_create = fixtures.BusinessRoutesHTTPTests.fail_after_create

    PRIVATE_KEYS = {
        "cost", "unit_cost", "total_cost", "cost_amount", "profit_amount",
        "unit_cost_snapshot", "total_cost_snapshot", "economic_snapshot_json",
        "delivery_snapshot_json", "settlement_rule", "settlement_rule_json",
        "settlement_rule_id", "settlement_rule_mode", "rule_version", "rates",
        "platform_rate", "provider_rate", "service_rate", "expert_rate", "channel_rate",
        "upstream_url", "integration_api_url",
    }
    PRIVATE_VALUES = (
        "37.19", "PRIVATE-RULE-VERSION", "private-upstream.invalid",
        "private-integration.invalid", "PRIVATE-UPSTREAM-TOKEN",
    )

    def body(self, **extra):
        return {"product_id": "product", "product_version_id": "version",
                "buyer_enterprise_id": "buyer_tenant", **extra}

    def create(self, **extra):
        return self.request("POST", "/api/orders", json=self.body(**extra))

    def counts(self):
        m = self.m
        with m.SessionLocal() as db:
            return tuple(db.scalar(self.select(self.func.count()).select_from(model))
                         for model in (m.Order, m.Payment, m.OrderStateLog, m.AuditLog,
                                       m.DeliveryTask, m.Settlement, self.Message,
                                       self.Receipt, self.Outbox))

    def assert_private(self, payload):
        def walk(value, path="response"):
            if isinstance(value, dict):
                for key, child in value.items():
                    self.assertNotIn(key, self.PRIVATE_KEYS, path + "." + key)
                    walk(child, path + "." + key)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, f"{path}[{index}]")
        walk(payload)
        serialized = json.dumps(payload, ensure_ascii=False)
        for marker in self.PRIVATE_VALUES:
            self.assertNotIn(marker, serialized)
        # Also inspect JSON embedded in strings, such as notification bodies.
        for key in self.PRIVATE_KEYS:
            self.assertNotIn('"' + key + '"', serialized.replace('\\"', '"'))

    def seed_private_values(self):
        m = self.m
        with m.SessionLocal() as db:
            db.get(m.ProductReleaseVersion, "version").cost = 37.19
            db.get(m.SettlementRule, "trading_rule").version = "PRIVATE-RULE-VERSION"
            product = db.get(m.Product, "product")
            product.upstream_url = "https://private-upstream.invalid/PRIVATE-UPSTREAM-TOKEN"
            product.integration_api_url = "https://private-integration.invalid/PRIVATE-UPSTREAM-TOKEN"
            db.commit()

    def mark_paid(self, order_id):
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order_id)
            saved.payment_status = "paid"
            saved.paid_amount = saved.amount
            db.commit()

    def assert_settlement_rejected(self, order_id):
        before = self.counts()
        result = self.request("POST", f"/api/settlements/generate/{order_id}",
                              actor="finance", expected=409)
        self.assertIn("detail", result)
        self.assertEqual(self.counts(), before)

    def test_buyer_quote_and_order_responses_hide_actual_private_snapshots(self):
        self.seed_private_values()
        before = self.counts()
        quote = self.request("POST", "/api/orders/quote", json=self.body())
        self.assertEqual(quote["amount"], 100)
        self.assert_private(quote)
        self.assertEqual(self.counts(), before)
        order = self.create(quote_id=quote["quote_id"])
        self.assert_private(order)
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order["id"])
            self.assertEqual(float(saved.total_cost_snapshot), 37.19)
            self.assertIn("PRIVATE-RULE-VERSION", saved.economic_snapshot_json)
            self.assertIn("private-upstream.invalid", saved.delivery_snapshot_json)
            self.assertIn("private-integration.invalid", saved.delivery_snapshot_json)
        for actor in ("buyer", "buyer_admin"):
            for path in ("/api/orders", f'/api/orders/{order["id"]}'):
                with self.subTest(actor=actor, path=path):
                    self.assert_private(self.request("GET", path, actor=actor))
        self.assert_private(self.pay(order["id"]))
        self.assert_private(self.request("GET", f'/api/orders/{order["id"]}'))

    def test_buyer_payment_and_settlement_notifications_hide_private_data(self):
        self.seed_private_values()
        order = self.create()
        self.pay(order["id"])
        settlement = self.request("POST", f'/api/settlements/generate/{order["id"]}',
                                  actor="finance")
        self.assertEqual(settlement["cost_amount"], 37.19)
        for actor in ("buyer", "buyer_admin"):
            inbox = self.request("GET", "/api/notifications", actor=actor)
            self.assertTrue(inbox["items"])
            self.assert_private(inbox)
            categories = {item["category"] for item in inbox["items"]}
            self.assertIn("order", categories)
            self.assertIn("settlement", categories)
            for item in inbox["items"]:
                self.assert_private(self.request("GET", f'/api/notifications/{item["id"]}',
                                                 actor=actor))
        self.assertEqual(self.request("GET", "/api/notifications", actor="outsider")["items"], [])

    def test_outsider_cannot_read_order_or_buyer_notification_detail(self):
        order = self.create()
        self.pay(order["id"])
        self.request("GET", f'/api/orders/{order["id"]}', actor="outsider", expected=403)
        self.assertEqual(self.request("GET", "/api/orders", actor="outsider")["items"], [])
        receipt = self.request("GET", "/api/notifications")["items"][0]
        self.request("GET", f'/api/notifications/{receipt["id"]}', actor="outsider", expected=404)

    def test_global_cost_rule_and_delivery_snapshots_survive_catalog_changes(self):
        m = self.m
        order = self.create()
        with m.SessionLocal() as db:
            saved = db.get(m.Order, order["id"])
            original = (saved.economic_snapshot_json, saved.delivery_snapshot_json,
                        saved.unit_cost_snapshot, saved.total_cost_snapshot)
            version = db.get(m.ProductReleaseVersion, "version")
            version.cost, version.price = 80, 200
            rule = db.get(m.SettlementRule, "trading_rule")
            rule.platform_rate, rule.provider_rate, rule.version = 20, 55, "v2"
            product = db.get(m.Product, "product")
            product.upstream_url = "https://changed.invalid"
            product.download_limit = 999
            db.commit()
        self.mark_paid(order["id"])
        result = self.request("POST", f'/api/settlements/generate/{order["id"]}', actor="finance")
        self.assertEqual(result["cost_amount"], 10)
        self.assertEqual(result["platform_fee"], 7.2)
        with m.SessionLocal() as db:
            saved = db.get(m.Order, order["id"])
            self.assertEqual((saved.economic_snapshot_json, saved.delivery_snapshot_json,
                              saved.unit_cost_snapshot, saved.total_cost_snapshot), original)
            rates, rule_version = m.settlement_values_for_order(db, saved, rule)
            self.assertEqual(rule_version, "global:v1")
            self.assertEqual(rates["platform_rate"], m.Decimal("8"))
        self.assert_private(self.request("GET", f'/api/orders/{order["id"]}'))

    def test_custom_rule_snapshot_survives_product_rule_replacement(self):
        m = self.m
        with m.SessionLocal() as db:
            product = db.get(m.Product, "product")
            product.settlement_rule_mode = "custom"
            product.settlement_rule_json = json.dumps(dict(platform_rate=20, provider_rate=75,
                                                          service_rate=0, expert_rate=0, channel_rate=5))
            db.commit()
        order = self.create()
        with m.SessionLocal() as db:
            saved = db.get(m.Order, order["id"])
            original = saved.economic_snapshot_json
            product = db.get(m.Product, "product")
            product.settlement_rule_mode = "global"
            product.settlement_rule_json = "{}"
            db.get(m.ProductReleaseVersion, "version").cost = 90
            db.commit()
        self.mark_paid(order["id"])
        result = self.request("POST", f'/api/settlements/generate/{order["id"]}', actor="finance")
        self.assertEqual(result["cost_amount"], 10)
        self.assertEqual(result["platform_fee"], 18)
        self.assertEqual(result["channel_fee"], 4.5)
        with m.SessionLocal() as db:
            self.assertEqual(db.get(m.Order, order["id"]).economic_snapshot_json, original)

    def test_cost_and_rule_changes_invalidate_quote_without_partial_order(self):
        m = self.m
        for field in ("cost", "rule"):
            with self.subTest(field=field):
                quote = self.request("POST", "/api/orders/quote", json=self.body())
                with m.SessionLocal() as db:
                    if field == "cost":
                        db.get(m.ProductReleaseVersion, "version").cost = 25
                    else:
                        db.get(m.SettlementRule, "trading_rule").version = "v2"
                    db.commit()
                before = self.counts()
                self.request("POST", "/api/orders", json=self.body(quote_id=quote["quote_id"]), expected=409)
                self.assertEqual(self.counts(), before)
                revised = self.request("POST", "/api/orders/quote", json=self.body())
                self.assertNotEqual(revised["quote_id"], quote["quote_id"])
                self.assertEqual(revised["amount"], quote["amount"])

    def test_missing_rule_rejects_quote_and_creation_without_writes(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.SettlementRule, "trading_rule").status = "draft"
            db.commit()
        before = self.counts()
        for path in ("/api/orders/quote", "/api/orders"):
            self.request("POST", path, json=self.body(), expected=409)
            self.assertEqual(self.counts(), before)

    def test_create_failure_after_order_payment_and_audit_flush_rolls_back(self):
        m = self.m
        before = self.counts()
        audit = m.audit
        observed = []
        def fail_after_audit(db, *args, **kwargs):
            audit(db, *args, **kwargs)
            if args[1] == "create_order":
                db.flush()
                saved = db.scalar(self.select(m.Order))
                self.assertEqual(saved.snapshot_version, 1)
                self.assertIsNotNone(db.scalar(self.select(m.Payment).where(m.Payment.order_id == saved.id)))
                self.assertIsNotNone(db.scalar(self.select(m.OrderStateLog).where(m.OrderStateLog.order_id == saved.id)))
                observed.append(saved.id)
                raise RuntimeError("Injected failure after all order rows were flushed")
        with patch.object(m, "audit", side_effect=fail_after_audit):
            self.request("POST", "/api/orders", json=self.body(), expected=500)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.create()["snapshot_version"], 1)

    def test_create_commit_failure_rolls_back_order_and_payment(self):
        before = self.counts()
        observed = []
        def fail_commit(db):
            db.flush()
            self.assertIsNotNone(db.scalar(self.select(self.m.Order)))
            self.assertIsNotNone(db.scalar(self.select(self.m.Payment)))
            observed.append(True)
            raise RuntimeError("Injected commit failure")
        with patch.object(self.m.Session, "commit", new=fail_commit):
            self.request("POST", "/api/orders", json=self.body(), expected=500)
        self.assertEqual(observed, [True])
        self.assertEqual(self.counts(), before)

    def test_payment_notification_failure_rolls_back_payment_task_and_messages(self):
        order = self.create()
        before = self.counts()
        with self.m.SessionLocal() as db:
            original = db.get(self.m.Order, order["id"]).economic_snapshot_json
        with self.fail_after_create("order"):
            self.request("POST", f'/api/orders/{order["id"]}/transition',
                         json={"action": "confirm_payment"}, expected=500)
        self.assertEqual(self.counts(), before)
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order["id"])
            self.assertEqual(saved.payment_status, "unpaid")
            self.assertEqual(float(saved.paid_amount), 0)
            self.assertEqual(saved.economic_snapshot_json, original)
            payment = db.scalar(self.select(self.m.Payment).where(self.m.Payment.order_id == saved.id))
            self.assertEqual(payment.status, "unpaid")

    def test_missing_total_cost_snapshot_fails_closed(self):
        order = self.create()
        self.mark_paid(order["id"])
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, order["id"]).total_cost_snapshot = None
            db.commit()
        self.assert_settlement_rejected(order["id"])

    def test_missing_or_malformed_economic_rule_snapshot_fails_closed(self):
        order = self.create()
        self.mark_paid(order["id"])
        with self.m.SessionLocal() as db:
            original = json.loads(db.get(self.m.Order, order["id"]).economic_snapshot_json)
        missing_rate = json.loads(json.dumps(original))
        del missing_rate["settlement_rule"]["rates"]["channel_rate"]
        for value in ("{}", "{", json.dumps(missing_rate)):
            with self.subTest(snapshot=value):
                with self.m.SessionLocal() as db:
                    db.get(self.m.Order, order["id"]).economic_snapshot_json = value
                    db.commit()
                self.assert_settlement_rejected(order["id"])

    def test_missing_economic_amount_or_cost_fields_fail_closed(self):
        for field in ("amount", "unit_cost", "total_cost"):
            with self.subTest(missing=field):
                order = self.create()
                self.mark_paid(order["id"])
                with self.m.SessionLocal() as db:
                    saved = db.get(self.m.Order, order["id"])
                    damaged = json.loads(saved.economic_snapshot_json)
                    del damaged[field]
                    saved.economic_snapshot_json = json.dumps(damaged)
                    db.commit()
                self.assert_settlement_rejected(order["id"])

    def test_missing_delivery_limit_snapshot_does_not_become_unlimited(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, "product").download_limit = 2
            db.commit()
        for damage in ("empty", "missing_limit"):
            with self.subTest(damage=damage):
                order = self.create()
                self.mark_paid(order["id"])
                with self.m.SessionLocal() as db:
                    saved = db.get(self.m.Order, order["id"])
                    delivery = json.loads(saved.delivery_snapshot_json)
                    self.assertEqual(delivery["download_limit"], 2)
                    if damage == "empty":
                        delivery = {}
                    else:
                        del delivery["download_limit"]
                    saved.delivery_snapshot_json = json.dumps(delivery)
                    db.commit()
                before = self.counts()
                self.request("GET", f'/api/orders/{order["id"]}/product-files', expected=409)
                self.assertEqual(self.counts(), before)

    def test_missing_delivery_method_snapshot_rejects_payment_without_writes(self):
        order = self.create()
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, order["id"]).delivery_method_snapshot = ""
            db.commit()
        before = self.counts()
        self.request("POST", f'/api/orders/{order["id"]}/transition',
                     json={"action": "confirm_payment"}, expected=409)
        self.assertEqual(self.counts(), before)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, order["id"]).payment_status, "unpaid")

    def test_wrong_type_rule_snapshot_fails_closed_with_409(self):
        order = self.create()
        self.mark_paid(order["id"])
        with self.m.SessionLocal() as db:
            original = json.loads(db.get(self.m.Order, order["id"]).economic_snapshot_json)
        for damaged in ([], {**original, "settlement_rule": []}):
            with self.subTest(snapshot=damaged):
                with self.m.SessionLocal() as db:
                    db.get(self.m.Order, order["id"]).economic_snapshot_json = json.dumps(damaged)
                    db.commit()
                self.assert_settlement_rejected(order["id"])


if __name__ == "__main__":
    unittest.main()
