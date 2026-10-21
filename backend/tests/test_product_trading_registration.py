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

    def api_product(self, sold=False, model_api=False):
        m = self.m
        order = None
        if sold or model_api:
            order = self.request("POST", "/api/orders", json={"product_id": "product", "product_version_id": "version", "buyer_enterprise_id": "buyer_tenant"})
        with m.SessionLocal() as db:
            db.add(m.ProductCategory(id="api-parent", name="API"))
            db.flush()
            db.add(m.ProductCategory(id="api-child", parent_id="api-parent", name="Services"))
            db.flush()
            product = db.get(m.Product, "product")
            product.product_type = "model" if model_api else "api"
            product.delivery_method = "model_api" if model_api else "api"
            product.status = "draft"
            product.category_id = "api-child"
            product.upstream_url = "https://legacy.example/business" if sold or model_api else ""
            product.integration_api_url = "https://management.example/control"
            if order:
                row = db.get(m.Order, order["id"])
                row.paid_amount = 100
                row.delivery_method_snapshot = product.delivery_method
            db.commit()

    def add_api_config(self, version_id="version", upstream="https://business.example/v1"):
        m = self.m
        with m.SessionLocal() as db:
            version = db.get(m.ProductReleaseVersion, version_id)
            db.add(m.version_gateway["Config"](
                product_id="product", version_id=version.id, version_code=version.version_code,
                upstream_url=upstream, route_key="route-" + version.id,
                auth_mode="api_key", upstream_auth_mode="none", upstream_scope="",
                timeout_ms=30000, strip_prefix=True, health_method="GET", health_path="/health",
                rate_limit_per_minute=60, daily_quota=0, monthly_quota=0))
            db.commit()

    def api_body(self):
        detail = self.request("GET", "/api/products/product", actor="provider")
        body = {key: value for key, value in detail.items() if key in self.m.ProductBody.model_fields}
        body.pop("upstream_url", None)
        body["versions"] = [{key: value for key, value in version.items()
                             if key in self.m.ProductVersionBody.model_fields}
                            for version in detail["versions"]]
        return body

    def test_api_submission_requires_management_without_business_fallback(self):
        self.api_product()
        self.add_api_config()
        for address in ("", " ", "ftp://management.example", "https://user:secret@management.example", "https://management.example/#fragment", "http://management.example/control"):
            with self.subTest(address=address):
                with self.m.SessionLocal() as db:
                    product = db.get(self.m.Product, "product")
                    product.integration_api_url = address
                    product.upstream_url = "https://legacy.example/business"
                    db.commit()
                result = self.submit(400)
                self.assertIn("管理接口", result["detail"])
                with self.m.SessionLocal() as db:
                    self.assertEqual(db.get(self.m.Product, "product").status, "draft")

    def test_api_submission_requires_config_for_every_active_version(self):
        self.api_product()
        self.assertIn("上游", self.submit(400)["detail"])
        self.add_api_config()
        with self.m.SessionLocal() as db:
            db.add(self.m.ProductReleaseVersion(id="second", product_id="product", version_code="v2", price=100, cost=10, status="active"))
            db.commit()
        self.assertIn("上游", self.submit(400)["detail"])
        self.add_api_config("second", "https://business.example/v2")
        self.assertEqual(self.submit()["status"], "pending_review")
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Product, "product").upstream_url, "")

    def test_api_submission_validates_persisted_version_urls(self):
        self.api_product()
        self.add_api_config()
        for address in ("", "https://", "https://user:secret@business.example", "https://business.example/?query=1", "https://business.example/%252fhidden", "http://business.example/v1"):
            with self.subTest(address=address):
                with self.m.SessionLocal() as db:
                    config = db.scalar(self.select(self.m.version_gateway["Config"]))
                    config.upstream_url = address
                    db.commit()
                self.assertIn("上游", self.submit(400)["detail"])

    def test_disabled_versions_do_not_require_config(self):
        self.api_product()
        self.add_api_config()
        with self.m.SessionLocal() as db:
            db.add(self.m.ProductReleaseVersion(product_id="product", version_code="disabled", price=0, status="disabled"))
            db.commit()
        self.assertEqual(self.submit()["status"], "pending_review")

    def test_http_submission_urls_follow_admin_policy(self):
        self.api_product()
        self.add_api_config(upstream="http://business.example/v1")
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, "product").integration_api_url = "http://management.example/control"
            db.add(self.m.SystemSetting(setting_key="application_integration_allow_http", setting_value="true"))
            db.commit()
        self.assertEqual(self.submit()["status"], "pending_review")

    def test_api_drafts_allow_missing_management_and_deprecate_product_upstream(self):
        self.api_product()
        body = self.api_body()
        body["integration_api_url"] = ""
        body["upstream_url"] = "https://deprecated.example/ignored"
        created = self.request("POST", "/api/products", actor="provider", json=body)
        self.assertEqual(created["upstream_url"], "")
        self.assertEqual(created["integration_api_url"], "")
        body.pop("upstream_url")
        updated = self.request("PUT", "/api/products/product", actor="provider", json=body)
        self.assertEqual(updated["integration_api_url"], "")
        self.submit(400)

    def test_sold_api_omitted_upstream_preserves_legacy_and_all_locks(self):
        self.api_product(sold=True)
        self.add_api_config()
        body = self.api_body()
        updated = self.request("PUT", "/api/products/product", actor="provider", json=body)
        self.assertEqual(updated["upstream_url"], "https://legacy.example/business")
        self.assertTrue(self.request("GET", "/api/products/product", actor="provider")["delivery_fields_locked"])
        for changes in ({"upstream_url": ""}, {"integration_api_url": "https://changed.example"}, {"integration_api_url": ""}):
            self.request("PUT", "/api/products/product", actor="provider", json={**body, **changes}, expected=409)
        without_management = {key: value for key, value in body.items() if key != "integration_api_url"}
        self.request("PUT", "/api/products/product", actor="provider", json=without_management, expected=409)
        self.request("PUT", "/api/products/product", actor="provider",
                     json={**body, "upstream_url": "https://legacy.example/business"})
        changed = {**body, "versions": [{**body["versions"][0], "price": 120}]}
        self.request("PUT", "/api/products/product", actor="provider", json=changed, expected=409)
        self.request("PUT", "/api/products/product/versions/version/integration", actor="provider", expected=409,
                     json={"upstream_url": "https://changed.example/v1", "version": "v1", "route_key": "changed"})
        self.assertEqual(self.submit()["status"], "pending_review")

    def test_legacy_model_api_submission_and_omitted_upstream(self):
        self.api_product(model_api=True)
        self.assertIn("上游", self.submit(400)["detail"])
        self.add_api_config()
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, "product").integration_api_url = ""
            db.commit()
        self.assertIn("管理接口", self.submit(400)["detail"])
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, "product").integration_api_url = "https://management.example/control"
            db.commit()
        updated = self.request("PUT", "/api/products/product", actor="provider", json=self.api_body())
        self.assertEqual(updated["upstream_url"], "https://legacy.example/business")
        self.assertEqual(self.submit()["status"], "pending_review")

    def test_api_registration_and_submission_permissions_remain_enforced(self):
        self.api_product()
        self.add_api_config()
        body = self.api_body()
        with self.m.SessionLocal() as db:
            member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "provider"))
            member.role = "member"
            db.commit()
        for method, path in (("POST", "/api/products"), ("PUT", "/api/products/product")):
            self.request(method, path, actor="provider", json=body, expected=403)
        self.submit(403)
        self.request("POST", "/api/products/product/submit", actor="outsider", expected=404)

    def test_api_draft_rejects_invalid_management_url(self):
        self.api_product()
        body = self.api_body()
        for address in ("ftp://management.example", "https://user:secret@management.example", "https://management.example/#fragment"):
            for method, path in (("POST", "/api/products"), ("PUT", "/api/products/product")):
                self.request(method, path, actor="provider", json={**body, "integration_api_url": address}, expected=422)

    def test_new_managed_api_machine_client_allows_invoke_and_manage(self):
        m = self.m
        for method in ("api", "model_api"):
            with self.subTest(method=method), m.SessionLocal() as db:
                product = db.get(m.Product, "product")
                product.product_type = "model" if method == "model_api" else "api"
                product.delivery_method = method
                product.integration_api_url = "https://management.example/control"
                client = m.ensure_oauth_client(db, product)
                self.assertEqual(set(client.scope.split()), {"resource.invoke", "resource.manage"})
                self.assertEqual(client.status, "active")
                self.assertTrue(client.client_id)
                self.assertTrue(client.client_secret)

    def test_existing_managed_api_machine_client_preserves_keys_scopes_and_status(self):
        m = self.m
        for method in ("api", "model_api"):
            for status in ("active", "revoked", "disabled"):
                with self.subTest(method=method, status=status), m.SessionLocal() as db:
                    product = db.get(m.Product, "product")
                    product.product_type = "model" if method == "model_api" else "api"
                    product.delivery_method = method
                    product.integration_api_url = "https://management.example/control"
                    existing = m.OAuthClient(id="stable", product_id=product.id,
                        client_id="existing-id", client_secret="existing-secret",
                        scope="resource.invoke custom.scope", status=status)
                    db.add(existing)
                    db.flush()
                    for _ in range(2):
                        client = m.ensure_oauth_client(db, product)
                        db.flush()
                        db.expire(client)
                        self.assertEqual(client.id, "stable")
                        self.assertEqual(client.client_id, "existing-id")
                        self.assertEqual(client.client_secret, "existing-secret")
                        self.assertEqual(client.status, status)
                        self.assertEqual(client.scope, "resource.invoke custom.scope resource.manage")

    def test_unmanaged_api_machine_scopes_are_unchanged(self):
        m = self.m
        for method in ("api", "model_api"):
            for address in ("", " "):
                with self.subTest(method=method, address=address), m.SessionLocal() as db:
                    product = db.get(m.Product, "product")
                    product.delivery_method = method
                    product.integration_api_url = address
                    client = m.ensure_oauth_client(db, product)
                    self.assertEqual(client.scope, "resource.invoke")
                    keys = (client.client_id, client.client_secret)
                    client.scope = "custom.legacy resource.invoke"
                    client.status = "revoked"
                    db.flush()
                    self.assertIs(m.ensure_oauth_client(db, product), client)
                    self.assertEqual(client.scope, "custom.legacy resource.invoke")
                    self.assertEqual((client.client_id, client.client_secret), keys)
                    self.assertEqual(client.status, "revoked")

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

    def test_product_detail_exposes_sold_locks_without_order_identifiers(self):
        before = self.request("GET", "/api/products/product", actor="provider")
        self.assertFalse(before["delivery_fields_locked"])
        self.assertFalse(before["versions"][0]["is_sold"])
        order = self.request("POST", "/api/orders", json={"product_id": "product", "product_version_id": "version", "buyer_enterprise_id": "buyer_tenant"})
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, order["id"]).paid_amount = 100
            db.commit()
        after = self.request("GET", "/api/products/product", actor="provider")
        self.assertTrue(after["delivery_fields_locked"])
        self.assertTrue(after["versions"][0]["is_sold"])
        self.assertNotIn(order["id"], str(after))

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
