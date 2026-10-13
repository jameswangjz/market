"""Enterprise console tenant isolation and provider dashboard coverage."""
from pathlib import Path
import sys
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qa_acceptance_harness as harness


class EnterpriseConsoleScopeTests(harness.AcceptanceHTTPCase):
    def setUp(self):
        super().setUp()
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.Membership(user_id="buyer", enterprise_id="provider_tenant", role="member"))
            db.add(m.Product(id="buyer-product", enterprise_id="buyer_tenant", name="Buyer private draft",
                product_type="dataset", provider_name="Buyer", provider_type="enterprise", status="draft",
                delivery_method="file", catalog_name="Test", description="Private", usage_scenarios="Test",
                pricing_strategy="Test", authorization_conditions="Test", data_source_statement="Test",
                compliance_statement="Test"))
            db.add(m.Product(id="buyer-published", enterprise_id="buyer_tenant", name="Buyer published",
                product_type="dataset", provider_name="Buyer", provider_type="enterprise", status="published",
                delivery_method="file", catalog_name="Test", description="Published", usage_scenarios="Test",
                pricing_strategy="Test", authorization_conditions="Test", data_source_statement="Test",
                compliance_statement="Test"))
            db.add(m.Product(id="other-sale-product", enterprise_id="other_tenant", name="Other sale",
                product_type="dataset", provider_name="Other", provider_type="enterprise", status="published",
                delivery_method="file", catalog_name="Test", description="Other", usage_scenarios="Test",
                pricing_strategy="Test", authorization_conditions="Test", data_source_statement="Test",
                compliance_statement="Test"))
            db.flush()
            db.add_all([
                m.Order(id="provider-sale", order_no="R2-PROVIDER-SALE", buyer_enterprise_id="other_tenant",
                    buyer_user_id="outsider", provider_enterprise_id="buyer_tenant", product_id="buyer-published",
                    main_status="in_delivery", payment_status="refunding", amount=120, paid_amount=120, refunded_amount=20,
                    buyer_name="Other", product_name="Buyer published"),
                m.Order(id="buyer-order", order_no="R2-BUYER-ORDER", buyer_enterprise_id="buyer_tenant",
                    buyer_user_id="buyer", provider_enterprise_id="other_tenant", product_id="other-sale-product",
                    main_status="completed", payment_status="paid", amount=900, paid_amount=900, refunded_amount=0,
                    buyer_name="Buyer", product_name="Other sale"),
            ])
            db.get(m.User, "buyer").verified_status = "pending"
            db.commit()

    def test_unverified_member_sees_only_selected_enterprise_products(self):
        own = self.request("GET", "/api/products", actor="buyer")
        self.assertEqual({item["id"] for item in own["items"]}, {"buyer-product", "buyer-published"})
        selected = self.request("GET", "/api/products?enterprise_id=provider_tenant", actor="buyer")
        self.assertEqual({item["id"] for item in selected["items"]}, {"product"})
        self.request("GET", "/api/products?enterprise_id=other_tenant", actor="buyer", expected=403)
        self.assertEqual(self.request("GET", "/api/products/buyer-product", actor="buyer")["id"], "buyer-product")
        response = self.client.get("/api/products/product", headers={
            "Authorization": "Bearer " + self.tokens["buyer"], "X-Enterprise-Id": "provider_tenant"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], "product")
        self.request("GET", "/api/products/product", actor="buyer", expected=404)

    def test_members_cannot_edit_and_dashboard_is_super_admin_provider_only(self):
        self.request("POST", "/api/products/buyer-product/submit", actor="buyer", expected=403)
        self.request("GET", "/api/dashboard", actor="buyer_admin", expected=403)
        dashboard = self.request("GET", "/api/dashboard", actor="buyer")
        self.assertEqual(dashboard["metrics"]["products"], 1)
        self.assertEqual(dashboard["metrics"]["orders"], 1)
        self.assertEqual(dashboard["metrics"]["active_orders"], 1)
        self.assertEqual(dashboard["metrics"]["revenue"], 120)
        self.assertEqual(dashboard["metrics"]["refunds"], 20)
        self.assertEqual(dashboard["metrics"]["net_revenue"], 100)
        self.assertEqual(self.request("GET", "/api/dashboard", actor="platform")["metrics"]["revenue"], 1020)

    def test_provider_admin_downloads_own_file_without_order_member_is_denied(self):
        payload = SimpleNamespace(read=lambda: b"provider-owned", close=lambda: None, release_conn=lambda: None)
        client = SimpleNamespace(get_object=Mock(return_value=payload))
        self.network.enter_context(patch.object(self.m, "Minio", return_value=client))
        self.network.enter_context(patch.object(self.m, "MINIO_ENDPOINT", "storage.invalid"))
        self.network.enter_context(patch.object(self.m, "MINIO_BUCKET", "test-bucket"))
        with self.m.SessionLocal() as db:
            db.add(self.m.FileObject(id="provider-private-file", owner_id="buyer", product_id="product",
                version_id="version", object_name="private", original_name="private.bin", content_type="application/octet-stream",
                file_role="product_data", size=14, scan_status="clean"))
            db.commit()
        allowed = self.client.get("/api/files/provider-private-file/download?enterprise_id=provider_tenant",
            headers={"Authorization": "Bearer " + self.tokens["provider"]})
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.content, b"provider-owned")
        denied = self.client.get("/api/files/provider-private-file/download?enterprise_id=provider_tenant",
            headers={"Authorization": "Bearer " + self.tokens["buyer"]})
        self.assertEqual(denied.status_code, 403, denied.text)
        switched = self.client.get("/api/files/provider-private-file/download?enterprise_id=buyer_tenant",
            headers={"Authorization": "Bearer " + self.tokens["buyer"]})
        self.assertEqual(switched.status_code, 403, switched.text)
        report = self.client.get("/api/files/provider-private-file/scan-report.pdf?enterprise_id=buyer_tenant",
            headers={"Authorization": "Bearer " + self.tokens["buyer"]})
        self.assertEqual(report.status_code, 403, report.text)

    def test_explicit_scope_header_is_rejected_without_active_membership(self):
        response = self.client.get("/api/products", headers={
            "Authorization": "Bearer " + self.tokens["buyer"], "X-Enterprise-Id": "other_tenant"})
        self.assertEqual(response.status_code, 403, response.text)

    def test_oauth_http_scope_and_verified_editor_permissions(self):
        m = self.m
        with m.SessionLocal() as db:
            product = db.get(m.Product, "product")
            product.product_type = "api"
            product.status = "pending_integration"
            db.add(m.OAuthClient(product_id="product", client_id="scope-client", client_secret="scope-secret", status="active"))
            db.commit()
        def download(actor, query="", tenant=""):
            headers = {"Authorization": "Bearer " + self.tokens[actor]}
            if tenant:
                headers["X-Enterprise-Id"] = tenant
            return self.client.get("/api/products/product/oauth-credentials-download" + query, headers=headers)
        self.assertEqual(download("provider", tenant="provider_tenant").status_code, 200)
        self.assertEqual(download("provider", "?enterprise_id=provider_tenant", "other_tenant").status_code, 400)
        self.assertEqual(download("provider", tenant="other_tenant").status_code, 403)
        self.assertEqual(download("buyer", tenant="provider_tenant").status_code, 403)
        with m.SessionLocal() as db:
            membership = m.require_enterprise_editor(db, db.get(m.User, "buyer_admin"), m.Membership, "buyer_tenant")
            self.assertEqual(membership.role, "enterprise_admin")
            db.get(m.User, "buyer_admin").verified_status = "pending"
            db.commit()
        self.request("POST", "/api/products/buyer-product/submit", actor="buyer_admin", expected=403)
