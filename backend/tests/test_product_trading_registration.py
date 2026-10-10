import unittest
import test_message_business_events as fixtures


class ProductTradingRegistrationTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixtures.BusinessRoutesHTTPTests.setUp
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request

    def draft(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, "product").status = "draft"
            db.commit()

    def submit(self, expected=200):
        return self.request("POST", "/api/products/product/submit", actor="provider", expected=expected)

    def test_delivery_classification_and_removed_storage(self):
        policy = self.m.trading_policy
        for method in ("file", "api", "model_api", "tenant_access"):
            self.assertEqual(policy["delivery_category"](method), "online")
        for method in ("training", "consulting", "custom"):
            self.assertEqual(policy["delivery_category"](method), "offline")
        with self.assertRaises(self.m.HTTPException):
            policy["delivery_category"]("object_storage")

    def test_submission_requires_clean_logo(self):
        self.draft()
        with self.m.SessionLocal() as db:
            db.get(self.m.FileObject, "product_logo").scan_status = "unavailable"
            db.commit()
        self.submit(400)
        with self.m.SessionLocal() as db:
            db.get(self.m.FileObject, "product_logo").scan_status = "clean"
            db.commit()
        self.assertEqual(self.submit()["status"], "pending_review")

    def test_every_file_version_requires_clean_bound_file(self):
        self.draft()
        with self.m.SessionLocal() as db:
            db.get(self.m.FileObject, "product_data").status = "deleted"
            db.commit()
        self.submit(400)

    def test_price_below_cost_rejected_at_submission(self):
        self.draft()
        with self.m.SessionLocal() as db:
            db.get(self.m.ProductReleaseVersion, "version").cost = 101
            db.commit()
        self.submit(400)

    def test_zero_daily_quota_allowed(self):
        self.assertEqual(self.m.ProductVersionBody(version_code="v1", daily_quota=0).daily_quota, 0)

    def test_version_update_keeps_identity(self):
        m = self.m
        with m.SessionLocal() as db:
            product = db.get(m.Product, "product")
            m.trading_policy["update_versions"](db, product, [{"version_code": "v1", "price": 110, "cost": 10}])
            db.commit()
            self.assertEqual(product.versions[0].id, "version")
            self.assertEqual(db.get(m.FileObject, "product_data").version_id, "version")

    def test_sold_version_and_file_cannot_be_replaced(self):
        m = self.m
        order = self.request("POST", "/api/orders", json={"product_id": "product", "product_version_id": "version", "buyer_enterprise_id": "buyer_tenant"})
        with m.SessionLocal() as db:
            db.get(m.Order, order["id"]).paid_amount = 100
            db.commit()
            version = db.get(m.ProductReleaseVersion, "version")
            m.trading_policy["protect_sold_version"](db, version, {"price": 100.0, "cost": 10.0})
            for action in (
                lambda: m.trading_policy["protect_sold_version"](db, version, {"price": 120}),
                lambda: m.trading_policy["protect_sold_file"](db, version),
                lambda: m.trading_policy["update_versions"](db, db.get(m.Product, "product"), [{"version_code": "v2", "price": 120}]),
            ):
                with self.assertRaises(m.HTTPException) as caught:
                    action()
                self.assertEqual(caught.exception.status_code, 409)

    def test_unselected_version_with_files_is_archived_not_deleted(self):
        m = self.m
        with m.SessionLocal() as db:
            product = db.get(m.Product, "product")
            m.trading_policy["update_versions"](db, product, [{"version_code": "v2", "price": 110}])
            db.commit()
            self.assertEqual(db.get(m.ProductReleaseVersion, "version").status, "disabled")
            self.assertEqual(db.get(m.FileObject, "product_data").version_id, "version")


if __name__ == "__main__":
    unittest.main()
