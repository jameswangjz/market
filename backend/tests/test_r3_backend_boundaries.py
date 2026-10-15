"""R3 permission and registration boundaries on isolated SQLite routes."""
import unittest

import test_message_business_events as fixtures


class R3BackendBoundariesTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixtures.BusinessRoutesHTTPTests.setUp
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request

    def test_development_denies_other_platform_enterprise_and_personal_roles(self):
        m = self.m
        for role in ("", "platform_operator", "finance_settlement", "product_manager",
                     "quality_reviewer", "security_compliance", "delivery_monitor", "ywsh"):
            with self.subTest(role=role):
                with m.SessionLocal() as db:
                    db.get(m.User, "ops").platform_role = role
                    db.commit()
                self.request("GET", "/api/development/tasks", actor="ops", expected=403)
                self.request("PATCH", "/api/development/tasks/missing", actor="ops",
                             expected=403, json={"progress": 50})
        for actor in ("buyer", "buyer_admin", "provider", "outsider"):
            self.request("GET", "/api/development/tasks", actor=actor, expected=403)
            self.request("PATCH", "/api/development/tasks/missing", actor=actor,
                         expected=403, json={"progress": 50})

    def test_super_admin_can_read_statistics_and_mutate(self):
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.DevelopmentTask(code="R3-TEST", title="Test", owner="test", area="backend"))
            db.commit()
        result = self.request("GET", "/api/development/tasks", actor="platform")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["code"], "R3-TEST")
        result = self.request("PATCH", "/api/development/tasks/R3-TEST", actor="platform",
                              json={"status": "done", "progress": 50})
        self.assertEqual(result["progress"], 100)
        self.request("PATCH", "/api/development/tasks/missing", actor="platform",
                     expected=404, json={"progress": 50})

    def test_create_and_update_reject_retired_and_forged_types(self):
        m = self.m
        with m.SessionLocal() as db:
            db.get(m.Product, "product").status = "draft"
            db.commit()
        for kind, delivery in (("report", "file"), ("application", "file"),
                               ("unknown", "file"), ("model", "model_api"),
                               ("dataset", "api"), ("api", "file"),
                               ("saas", "file"), ("training", "consulting"),
                               ("consulting", "custom"), ("custom", "training")):
            for method, path in (("POST", "/api/products"), ("PUT", "/api/products/product")):
                with self.subTest(kind=kind, delivery=delivery, method=method):
                    self.request(method, path, actor="provider", expected=400,
                                 json={"name": "Invalid", "product_type": kind,
                                       "delivery_method": delivery})
        with m.SessionLocal() as db:
            self.assertEqual(db.get(m.Product, "product").product_type, "dataset")

    def test_retired_product_and_order_snapshots_remain_readable(self):
        m = self.m
        order = self.request("POST", "/api/orders", json={"product_id": "product",
                             "product_version_id": "version", "buyer_enterprise_id": "buyer_tenant"})
        for kind in ("report", "application"):
            with self.subTest(kind=kind):
                with m.SessionLocal() as db:
                    db.get(m.Product, "product").product_type = kind
                    db.commit()
                product = self.request("GET", "/api/products/product", actor="provider")
                self.assertEqual(product["product_type"], kind)
                result = self.request("GET", "/api/orders/" + order["id"])
                self.assertEqual(result["order"]["delivery_method"], "file")

    def test_sold_model_api_compatibility_does_not_allow_new_registration(self):
        m = self.m
        order = self.request("POST", "/api/orders", json={"product_id": "product",
                             "product_version_id": "version", "buyer_enterprise_id": "buyer_tenant"})
        with m.SessionLocal() as db:
            product = db.get(m.Product, "product")
            product.product_type, product.delivery_method = "model", "model_api"
            snapshot = db.get(m.Order, order["id"])
            snapshot.delivery_method_snapshot, snapshot.paid_amount = "model_api", 100
            db.commit()
            validate = m.trading_policy["validate_product_delivery"]
            validate(db, product, "model", "model_api")
            with self.assertRaises(m.HTTPException) as error:
                validate(db, None, "model", "model_api")
            self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
