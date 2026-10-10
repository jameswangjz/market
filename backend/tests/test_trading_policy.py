"""HTTP tests against isolated production routes; no development data writes."""
import unittest
import test_message_business_events as fixtures


class TradingPolicyTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixtures.BusinessRoutesHTTPTests.setUp
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request

    def create(self, enterprise="buyer_tenant", actor="buyer", expected=200):
        return self.request("POST", "/api/orders", actor=actor, expected=expected,
                            json={"product_id": "product", "product_version_id": "version",
                                  "buyer_enterprise_id": enterprise})

    def test_explicit_second_enterprise_is_used(self):
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.Membership(user_id="buyer", enterprise_id="other_tenant", role="enterprise_admin"))
            db.commit()
        order = self.create("other_tenant")
        with m.SessionLocal() as db:
            self.assertEqual(db.get(m.Order, order["id"]).buyer_enterprise_id, "other_tenant")
        options = self.request("GET", "/api/storefront/buyer-enterprises")["items"]
        self.assertEqual({x["id"] for x in options if x["eligible"]}, {"buyer_tenant", "other_tenant"})

    def test_enterprise_selection_required(self):
        self.request("POST", "/api/orders", expected=422, json={"product_id": "product"})
        self.create("", expected=422)

    def test_foreign_enterprise_rejected(self):
        self.create("provider_tenant", expected=403)
        self.create("missing", expected=403)

    def test_personal_verification_required(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "buyer").verified_status = "pending"
            db.commit()
        self.create(expected=403)
        options = self.request("GET", "/api/storefront/buyer-enterprises")["items"]
        self.assertFalse(options[0]["eligible"])

    def test_enterprise_verification_required(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.Enterprise, "buyer_tenant").verification_status = "pending_review"
            db.commit()
        self.create(expected=403)

    def test_member_and_revoked_membership_rejected(self):
        m = self.m
        with m.SessionLocal() as db:
            member = db.scalar(self.select(m.Membership).where(m.Membership.user_id == "buyer"))
            member.role = "member"
            db.commit()
        self.create(expected=403)
        with m.SessionLocal() as db:
            member = db.scalar(self.select(m.Membership).where(m.Membership.user_id == "buyer"))
            member.role = "super_admin"
            member.status = "disabled"
            db.commit()
        self.create(expected=403)

    def test_platform_and_pending_activation_rejected(self):
        self.create(actor="platform", expected=403)
        self.create(actor="inactive", expected=403)
        self.assertEqual(self.request("GET", "/api/storefront/buyer-enterprises", actor="platform")["items"], [])

    def test_disabled_user_rejected_immediately(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "buyer").is_active = False
            db.commit()
        self.create(expected=401)

    def test_review_roles_without_enterprises_can_read_product_queue(self):
        for actor in ("platform", "ops", "reviewer", "quality"):
            self.assertEqual(len(self.request("GET", "/api/products", actor=actor)["items"]), 1)
        self.request("GET", "/api/products", actor="finance", expected=403)

    def test_unverified_console_catalog_does_not_leak_private_fields(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "buyer").verified_status = "pending"
            db.get(self.m.Product, "product").upstream_url = "https://private.invalid"
            db.commit()
        for path in ("/api/products", "/api/products/product"):
            response = self.request("GET", path)
            text = str(response)
            for private in ("cost", "upstream_url", "settlement_rule", "https://private.invalid"):
                self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main()
