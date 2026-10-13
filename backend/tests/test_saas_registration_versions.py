import unittest

import test_message_business_events as fixtures


class SaaSRegistrationVersionTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixtures.BusinessRoutesHTTPTests.setUp
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request

    def setUp(self):
        fixtures.BusinessRoutesHTTPTests.setUp(self)
        with self.m.SessionLocal() as db:
            self.m.product_categories['seed'](db)
            db.commit()

    def product(self, db):
        m = self.m
        product = m.Product(id="saas-product", enterprise_id="provider_tenant", name="SaaS",
                            product_type="saas", status="draft", delivery_method="tenant_access")
        db.add(product)
        db.flush()
        return product

    def test_registration_creates_storefront_version_with_canonical_id_and_fields(self):
        m = self.m
        with m.SessionLocal() as db:
            product = self.product(db)
            m.trading_policy["update_versions"](db, product, [{
                "version_code": "starter", "description": "Starter tier", "price": 40,
                "cost": 12, "compatibility_mode": "breaking", "compatibility_notes": "API change",
                "status": "active",
            }])
            canonical = product.versions[0]
            row = db.scalar(self.select(m.SaaSProductVersion).where(
                m.SaaSProductVersion.product_id == product.id))
            self.assertEqual(row.id, canonical.id)
            self.assertEqual(row.version_code, "starter")
            self.assertEqual(row.monthly_price, 40)
            self.assertEqual(row.cost, 12)
            self.assertEqual(row.description, "Starter tier")
            self.assertEqual(row.compatibility_mode, "breaking")
            self.assertEqual(row.compatibility_notes, "API change")
            self.assertEqual(row.status, "active")

    def test_existing_specialized_identity_is_preserved_and_updated(self):
        m = self.m
        with m.SessionLocal() as db:
            product = self.product(db)
            db.add(m.ProductReleaseVersion(id="canonical", product_id=product.id,
                version_code="pro", description="New description", price=90, cost=25, status="active"))
            db.add(m.SaaSProductVersion(id="stable-saas-id", product_id=product.id,
                version_code="pro", name="Existing", monthly_price=20, cost=5, status="draft"))
            db.flush()
            result = m.trading_policy["sync_saas_versions"](db, product)
            row = db.get(m.SaaSProductVersion, "stable-saas-id")
            self.assertEqual(result["updated"], 1)
            self.assertIsNone(db.get(m.SaaSProductVersion, "canonical"))
            self.assertEqual(row.monthly_price, 90)
            self.assertEqual(row.cost, 25)
            self.assertEqual(row.description, "New description")

    def test_repair_retains_unknown_specialized_rows_by_default(self):
        m = self.m
        with m.SessionLocal() as db:
            product = self.product(db)
            db.add_all([
                m.SaaSProductVersion(id="referenced", product_id=product.id, version_code="old-sub", name="Old", status="active"),
                m.SaaSProductVersion(id="unused", product_id=product.id, version_code="old-unused", name="Old", status="active"),
            ])
            db.flush()
            db.add(m.SaaSSubscription(id="subscription", enterprise_id="buyer_tenant",
                product_id=product.id, version_id="referenced", status="closed"))
            db.flush()
            result = m.trading_policy["sync_saas_versions"](db, product)
            self.assertEqual(result["retained"], 2)
            self.assertEqual(result["deleted"], 0)
            self.assertEqual(db.get(m.SaaSProductVersion, "referenced").status, "active")
            self.assertIsNotNone(db.get(m.SaaSProductVersion, "unused"))

    def test_explicit_removal_rejects_referenced_specialized_version(self):
        m = self.m
        with m.SessionLocal() as db:
            product = self.product(db)
            db.add(m.ProductReleaseVersion(id="canonical", product_id=product.id,
                version_code="legacy", price=40, cost=10, status="active"))
            db.add(m.SaaSProductVersion(id="legacy-row", product_id=product.id,
                version_code="legacy", name="Legacy", monthly_price=40, cost=10, status="active"))
            db.flush()
            db.add(m.SaaSSubscription(id="legacy-subscription", enterprise_id="buyer_tenant",
                product_id=product.id, version_id="legacy-row", status="closed"))
            db.flush()
            with self.assertRaises(m.HTTPException) as caught:
                m.trading_policy["update_versions"](db, product, [])
            self.assertEqual(caught.exception.status_code, 409)

    def test_explicit_removal_deletes_unreferenced_specialized_version(self):
        m = self.m
        with m.SessionLocal() as db:
            product = self.product(db)
            db.add(m.ProductReleaseVersion(id="canonical", product_id=product.id,
                version_code="legacy", price=40, cost=10, status="active"))
            db.add(m.SaaSProductVersion(id="legacy-row", product_id=product.id,
                version_code="legacy", name="Legacy", monthly_price=40, cost=10, status="active"))
            db.flush()
            m.trading_policy["update_versions"](db, product, [])
            self.assertIsNone(db.get(m.SaaSProductVersion, "legacy-row"))

    def test_duplicate_specialized_codes_are_rejected(self):
        m = self.m
        with m.SessionLocal() as db:
            product = self.product(db)
            db.add_all([
                m.SaaSProductVersion(id="duplicate-one", product_id=product.id, version_code="legacy", name="One"),
                m.SaaSProductVersion(id="duplicate-two", product_id=product.id, version_code="legacy", name="Two"),
            ])
            db.flush()
            with self.assertRaises(m.HTTPException) as caught:
                m.trading_policy["sync_saas_versions"](db, product)
            self.assertEqual(caught.exception.status_code, 409)

    def test_canonical_id_collision_with_another_product_is_conflict(self):
        m = self.m
        with m.SessionLocal() as db:
            product = self.product(db)
            other = m.Product(id="other-saas", enterprise_id="provider_tenant", name="Other",
                              product_type="saas", status="draft", delivery_method="tenant_access")
            db.add(other)
            db.flush()
            db.add(m.ProductReleaseVersion(id="colliding-id", product_id=product.id,
                version_code="new", price=40, cost=10, status="active"))
            db.add(m.SaaSProductVersion(id="colliding-id", product_id=other.id,
                version_code="other", name="Other"))
            db.flush()
            with self.assertRaises(m.HTTPException) as caught:
                m.trading_policy["sync_saas_versions"](db, product)
            self.assertEqual(caught.exception.status_code, 409)

    def test_registration_rejects_invalid_canonical_money(self):
        m = self.m
        with m.SessionLocal() as db:
            product = self.product(db)
            with self.assertRaises(m.HTTPException) as caught:
                m.trading_policy["update_versions"](db, product, [{
                    "version_code": "invalid", "price": 10, "cost": 11,
                }])
            self.assertEqual(caught.exception.status_code, 400)

    def test_http_creation_publishes_matching_storefront_and_quote_version_id(self):
        m = self.m
        created = self.request("POST", "/api/products", actor="provider", json={
            "name": "Registered SaaS", "product_type": "saas", "delivery_method": "tenant_access",
            "catalog_name": "其他",
            "application_url": "https://app.example.invalid", "versions": [{
                "version_code": "monthly", "description": "Monthly plan", "price": 75,
                "cost": 20, "status": "active",
            }],
        })
        product_id = created["id"]
        with m.SessionLocal() as db:
            product = db.get(m.Product, product_id)
            product.status = "published"
            db.add(m.SaaSIntegrationConfig(product_id=product_id,
                base_url="https://saas.example.invalid", token_url="https://saas.example.invalid/token",
                client_id="client", client_secret="secret", status="active"))
            canonical_id = product.versions[0].id
            storefront_version = db.scalar(self.select(m.SaaSProductVersion).where(
                m.SaaSProductVersion.product_id == product_id))
            self.assertEqual(storefront_version.id, canonical_id)
            db.commit()

        storefront = self.request("GET", "/api/storefront/products")
        listed = next(item for item in storefront["items"] if item["id"] == product_id)
        self.assertEqual([version["id"] for version in listed["versions"]], [canonical_id])
        quote = self.request("POST", "/api/orders/quote", json={
            "product_id": product_id, "product_version_id": canonical_id,
            "buyer_enterprise_id": "buyer_tenant", "subscription_months": 2,
        })
        self.assertEqual(quote["product_version_id"], canonical_id)
        self.assertEqual(quote["amount"], 150)


if __name__ == "__main__":
    unittest.main()
