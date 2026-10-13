"""Provider/buyer query isolation for the enterprise console."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qa_acceptance_harness as harness


class ConsoleBusinessScopeTests(harness.AcceptanceHTTPCase):
    def setUp(self):
        super().setUp()
        m = self.m
        with m.SessionLocal() as db:
            db.scalar(self.select(m.Membership).where(m.Membership.user_id == "buyer",
                m.Membership.enterprise_id == "buyer_tenant")).role = "member"
            db.add(m.Product(id="lens-product", enterprise_id="buyer_tenant", name="Lens product",
                product_type="dataset", provider_name="Tenant", provider_type="enterprise", status="published",
                delivery_method="file", catalog_name="Test", description="Test", usage_scenarios="Test",
                pricing_strategy="Test", authorization_conditions="Test", data_source_statement="Test",
                compliance_statement="Test"))
            db.flush()
            db.add_all([
                m.Order(id="lens-sell", order_no="LENS-SELL", buyer_enterprise_id="other_tenant",
                    buyer_user_id="outsider", provider_enterprise_id="buyer_tenant", product_id="lens-product",
                    main_status="completed", payment_status="paid", amount=100, paid_amount=100,
                    buyer_name="Other", product_name="Lens product"),
                m.Order(id="lens-buy", order_no="LENS-BUY", buyer_enterprise_id="buyer_tenant",
                    buyer_user_id="buyer", provider_enterprise_id="other_tenant", product_id="lens-product",
                    main_status="completed", payment_status="paid", amount=200, paid_amount=200,
                    buyer_name="Tenant", product_name="Other product"),
            ])
            db.commit()

    def test_provider_buyer_and_legacy_union_are_current_enterprise_scoped(self):
        def orders(query):
            return {row["id"] for row in self.request("GET", "/api/orders?" + query,
                actor="buyer_admin")["items"]}
        self.assertEqual(orders("business_view=provider"), {"lens-sell"})
        self.assertEqual(orders("business_view=buyer"), {"lens-buy"})
        self.assertEqual(orders(""), {"lens-sell", "lens-buy"})
        self.request("GET", "/api/orders?business_view=provider&enterprise_id=other_tenant",
            actor="buyer_admin", expected=403)

    def test_ordinary_member_keeps_legacy_order_list_but_cannot_toggle_view(self):
        self.assertEqual({row["id"] for row in self.request("GET", "/api/orders", actor="buyer")["items"]},
            {"lens-buy"})
        self.request("GET", "/api/orders?business_view=provider", actor="buyer", expected=403)

    def test_enterprise_audit_response_omits_internal_payloads(self):
        with self.m.SessionLocal() as db:
            db.add(self.m.AuditLog(actor="audit@example.invalid", action="safe-action", target_type="order",
                target_id="lens-buy", detail="private internal note", category="order", tenant_id="buyer_tenant",
                order_id="lens-buy", before_json='{"credential":"secret"}', after_json='{"cost":99}'))
            db.commit()
        response = self.request("GET", "/api/audit-logs?business_view=buyer", actor="buyer_admin")
        item = next(row for row in response["items"] if row["action"] == "safe-action")
        self.assertEqual(set(item), {"id", "action", "target_type", "target_id", "result", "category", "order_id", "order_no", "created_at"})
        self.assertNotIn("secret", str(item))

    def test_conflicting_header_and_query_scope_is_rejected(self):
        response = self.client.get("/api/orders?enterprise_id=other_tenant", headers={
            "Authorization": "Bearer " + self.tokens["buyer_admin"],
            "X-Enterprise-Id": "buyer_tenant",
        })
        self.assertEqual(response.status_code, 400, response.text)

    def test_order_detail_explicit_context_isolated_and_legacy_call_preserved(self):
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.Membership(user_id="buyer_admin", enterprise_id="provider_tenant", role="enterprise_admin"))
            db.commit()
        self.assertEqual(self.request("GET", "/api/orders/lens-buy", actor="buyer_admin")["order"]["id"], "lens-buy")
        headers = {"Authorization": "Bearer " + self.tokens["buyer_admin"], "X-Enterprise-Id": "provider_tenant"}
        response = self.client.get("/api/orders/lens-buy", headers=headers)
        self.assertEqual(response.status_code, 404, response.text)
        headers["X-Enterprise-Id"] = "buyer_tenant"
        self.assertEqual(self.client.get("/api/orders/lens-buy", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/orders/lens-buy?business_view=provider", headers=headers).status_code, 404)
        self.assertEqual(self.client.get("/api/orders/lens-buy?enterprise_id=other_tenant", headers=headers).status_code, 400)
        headers["X-Enterprise-Id"] = "other_tenant"
        self.assertEqual(self.client.get("/api/orders/lens-buy", headers=headers).status_code, 403)

    def test_provider_settlement_view_is_scoped_redacted_and_read_only(self):
        m = self.m
        with m.SessionLocal() as db:
            db.add_all([
                m.Settlement(id="sell-settlement", settlement_no="SELL-SET", order_id="lens-sell",
                    provider_share=67, cost_amount=12, platform_fee=8),
                m.Settlement(id="buy-settlement", settlement_no="BUY-SET", order_id="lens-buy", provider_share=134),
            ])
            db.commit()
        data = self.request("GET", "/api/enterprise-settlements?business_view=provider", actor="buyer_admin")
        self.assertEqual({item["id"] for item in data["items"]}, {"sell-settlement"})
        self.assertEqual(set(data["items"][0]), {"id", "settlement_no", "order_id", "order_no",
            "product_name", "provider_share", "status", "created_at"})
        self.assertEqual(data["items"][0]["provider_share"], 67)
        for actor in ("buyer", "platform", "reviewer"):
            self.request("GET", "/api/enterprise-settlements?business_view=provider", actor=actor, expected=403)
        for query in ("business_view=buyer", "business_view=provider&enterprise_id=other_tenant"):
            self.request("GET", "/api/enterprise-settlements?" + query, actor="buyer_admin", expected=403)
        self.request("GET", "/api/enterprise-settlements?business_view=invalid", actor="buyer_admin", expected=422)
        response = self.client.get("/api/enterprise-settlements?business_view=provider", headers={
            "Authorization": "Bearer " + self.tokens["buyer_admin"], "X-Enterprise-Id": "other_tenant"})
        self.assertEqual(response.status_code, 403)
        self.request("GET", "/api/settlements", actor="buyer_admin", expected=403)
        self.request("POST", "/api/settlements/sell-settlement/lock", actor="buyer_admin", expected=403)
        self.request("GET", "/api/settlement-reports/export", actor="buyer_admin", expected=403)
        with m.SessionLocal() as db:
            db.get(m.User, "buyer_admin").verified_status = "pending"
            db.commit()
        self.request("GET", "/api/enterprise-settlements?business_view=provider", actor="buyer_admin", expected=403)
