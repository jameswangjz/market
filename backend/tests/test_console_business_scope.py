"""Provider/buyer query isolation for the enterprise console."""
from pathlib import Path
import asyncio
from decimal import Decimal
import sys
from unittest.mock import patch

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

    def test_real_order_routes_include_public_product_identity_without_private_snapshots(self):
        m = self.m
        with m.SessionLocal() as db:
            order = db.get(m.Order, "lens-buy")
            order.delivery_method_snapshot = "saas"
            order.subscription_id = "saas-subscription"
            order.unit_cost_snapshot = 19
            order.total_cost_snapshot = 38
            order.economic_snapshot_json = '{"credential":"private-secret"}'
            db.commit()
        public_fields = {"id", "order_no", "buyer_user_id", "buyer_enterprise_id",
            "provider_enterprise_id", "buyer_name", "product_id", "product_name",
            "product_version_id", "product_version_code", "product_version_name", "billing_cycle",
            "subscription_months", "delivery_method", "snapshot_version", "unit_price",
            "subscription_id", "business_type", "related_order_id", "main_status", "payment_status",
            "delivery_status", "after_sales_status", "amount", "paid_amount", "refunded_amount",
            "created_at", "updated_at"}
        for mode, expected_ids in (("buyer", {"lens-buy"}), ("provider", {"lens-sell"}),
                ("", {"lens-buy", "lens-sell"})):
            response = self.request("GET", "/api/orders", actor="buyer_admin",
                params={"enterprise_id": "buyer_tenant", **({"business_view": mode} if mode else {})})
            self.assertEqual({row["id"] for row in response["items"]}, expected_ids)
            for row in response["items"]:
                self.assertEqual(set(row), public_fields | {"provider_name"})
                self.assertEqual(row["product_id"], "lens-product")
        detail = self.request("GET", "/api/orders/lens-buy", actor="buyer_admin",
            params={"enterprise_id": "buyer_tenant", "business_view": "buyer"})["order"]
        self.assertEqual(set(detail), public_fields)
        self.assertEqual((detail["product_id"], detail["subscription_id"], detail["delivery_method"]),
            ("lens-product", "saas-subscription", "saas"))
        self.assertNotIn("private-secret", str(detail))
        self.request("GET", "/api/orders/lens-buy", actor="reviewer", expected=403)

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
        detail = self.request("GET", "/api/enterprise-settlements/sell-settlement?business_view=provider", actor="buyer_admin")
        self.assertEqual(detail["id"], data["items"][0]["id"])
        self.assertEqual(len(detail["participants"]), 5)
        self.request("GET", "/api/enterprise-settlements/buy-settlement?business_view=provider", actor="buyer_admin", expected=404)
        self.request("GET", "/api/enterprise-settlements/sell-settlement?business_view=buyer", actor="buyer_admin", expected=403)
        for actor in ("buyer", "platform", "reviewer"):
            self.request("GET", "/api/enterprise-settlements?business_view=provider", actor=actor, expected=403)
        for query in ("business_view=buyer", "business_view=provider&enterprise_id=other_tenant"):
            self.request("GET", "/api/enterprise-settlements?" + query, actor="buyer_admin", expected=403)
        self.request("GET", "/api/enterprise-settlements?business_view=invalid", actor="buyer_admin", expected=422)
        response = self.client.get("/api/enterprise-settlements?business_view=provider", headers={
            "Authorization": "Bearer " + self.tokens["buyer_admin"], "X-Enterprise-Id": "other_tenant"})
        self.assertEqual(response.status_code, 403)
        self.request("GET", "/api/settlements", actor="buyer_admin", expected=403)
        self.request("GET", "/api/settlement-reports/export", actor="buyer_admin", expected=403)
        with m.SessionLocal() as db:
            db.get(m.User, "buyer_admin").verified_status = "pending"
            db.commit()
        self.request("GET", "/api/enterprise-settlements?business_view=provider", actor="buyer_admin", expected=403)

    def seed_view_messages(self):
        m = self.m
        with m.SessionLocal() as db:
            for key, target, tenant, recipients in (
                ("sell", "lens-sell", "other_tenant", ["buyer_admin"]),
                ("buy", "lens-buy", "buyer_tenant", ["buyer_admin"]),
                ("not-recipient", "lens-sell", "buyer_tenant", ["outsider"]),
                ("unclassified", "unknown", "buyer_tenant", ["buyer_admin"]),
            ):
                m.message_center["create"](db, recipients, key, "safe notice", "order", target,
                    event_key="scope:" + key, category="order", tenant_id=tenant, delivery=False)
            db.commit()
        return {row["title"]: row["id"] for row in self.request("GET", "/api/notifications", actor="buyer_admin")["items"]}

    def test_messages_and_summary_follow_order_party_not_notification_tenant(self):
        ids = self.seed_view_messages()
        for mode, title in (("provider", "sell"), ("buyer", "buy")):
            data = self.request("GET", "/api/notifications?enterprise_id=buyer_tenant&business_view=" + mode, actor="buyer_admin")
            self.assertEqual({item["title"] for item in data["items"]}, {title, "unclassified"})
            self.assertEqual((data["total"], data["unread"]), (2, 2))
            self.assertEqual({item["title"] for item in data["recent"]}, {title, "unclassified"})
            counts = self.request("GET", "/api/notifications/summary?business_view=" + mode, actor="buyer_admin")
            self.assertEqual(counts["unread"], 2)
            self.request("GET", "/api/notifications/" + ids[title] + "?business_view=" + mode, actor="buyer_admin")
        self.assertEqual(set(ids), {"sell", "buy", "unclassified"})
        self.request("GET", "/api/notifications?business_view=buyer", actor="buyer")
        self.request("GET", "/api/notifications?enterprise_id=other_tenant&business_view=buyer", actor="buyer_admin", expected=403)
        self.request("GET", "/api/notifications?business_view=invalid", actor="buyer_admin", expected=422)

    def test_message_detail_actions_and_read_all_cannot_cross_explicit_view(self):
        ids = self.seed_view_messages()
        suffix = "?business_view=provider&enterprise_id=buyer_tenant"
        for method, path in (("GET", ""), ("POST", "/read"), ("DELETE", "")):
            self.request(method, "/api/notifications/" + ids["buy"] + path + suffix, actor="buyer_admin", expected=404)
        self.request("POST", "/api/notifications/batch-actions" + suffix, actor="buyer_admin", expected=404,
            json={"ids": [ids["sell"], ids["buy"]], "action": "read"})
        data = self.request("GET", "/api/notifications/summary" + suffix, actor="buyer_admin")
        self.assertEqual(data["unread"], 2)
        result = self.request("POST", "/api/notifications/read-all" + suffix, actor="buyer_admin",
            json={"cutoff": self.m.now().isoformat()})
        self.assertEqual((result["processed"], result["unread"]), (2, 0))
        self.assertEqual(self.request("GET", "/api/notifications/summary?business_view=buyer", actor="buyer_admin")["unread"], 1)

    def test_audit_order_link_overrides_tenant_for_business_view(self):
        m = self.m
        with m.SessionLocal() as db:
            db.add_all([m.AuditLog(actor="test", action=key, target_type="order", target_id=oid,
                tenant_id="buyer_tenant", order_id=oid, category="order")
                for key, oid in (("scope-sell", "lens-sell"), ("scope-buy", "lens-buy"))])
            db.commit()
        for mode, key in (("provider", "scope-sell"), ("buyer", "scope-buy")):
            data = self.request("GET", "/api/audit-logs?business_view=" + mode, actor="buyer_admin")
            self.assertEqual({row["action"] for row in data["items"]}, {key})
            categories = self.request("GET", "/api/audit-logs/categories?business_view=" + mode, actor="buyer_admin")
            self.assertEqual(next(row["count"] for row in categories["items"] if row["category"] == "order"), 1)

    def settlement_fixture(self):
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.Settlement(id="scope-set", settlement_no="SCOPE-SET", order_id="lens-sell",
                gross_amount=100, cost_amount=10, profit_amount=90, provider_share=60))
            db.add(m.SettlementBatch(id="scope-batch", batch_no="SCOPE-BATCH", idempotency_key="scope-batch", status="generated"))
            db.flush()
            db.add_all([m.SettlementLine(batch_id="scope-batch", settlement_id="scope-set", participant_type=kind,
                participant_name=kind, amount=0) for kind in ("platform", "provider", "service", "expert", "channel")])
            db.get(m.Membership, db.scalar(self.select(m.Membership.id).where(m.Membership.user_id == "buyer", m.Membership.status == "active"))).role = "enterprise_admin"
            db.commit()
        return {"gross_amount": .04, "cost_amount": .01, "profit_amount": .03, "platform_rate": 10,
            "provider_rate": 10, "service_rate": 10, "expert_rate": 10, "channel_rate": 60, "reason": "Confirmed economic correction"}

    def test_provider_proposal_requires_platform_counterparty_and_exact_rounding_before_lock(self):
        body = self.settlement_fixture()
        proposal = self.request("POST", "/api/settlements/scope-set/proposals?business_view=provider", actor="buyer_admin", json=body)
        provider_detail = self.request("GET", "/api/enterprise-settlements/scope-set", actor="buyer_admin")
        self.assertEqual(provider_detail["actions"], {"can_propose": False, "can_lock": False})
        self.assertFalse(provider_detail["proposals"][0]["actions"]["can_decide"])
        platform_detail = self.request("GET", "/api/settlements/scope-set/detail", actor="finance")
        self.assertTrue(platform_detail["proposals"][0]["actions"]["can_decide"])
        path = "/api/settlement-proposals/" + proposal["id"] + "/decision"
        for actor in ("buyer_admin", "buyer"):
            self.request("POST", path + "?business_view=provider", actor=actor, expected=409, json={"decision": "approve"})
        self.request("POST", "/api/settlements/scope-set/lock", actor="buyer_admin", expected=409)
        self.request("POST", path, actor="finance", json={"decision": "approve", "comment": "Platform approved"})
        self.request("POST", path, actor="finance", expected=409, json={"decision": "approve"})
        detail = self.request("GET", "/api/enterprise-settlements/scope-set?business_view=provider", actor="buyer_admin")
        self.assertEqual(detail["batch"]["id"], "scope-batch")
        self.assertEqual(detail["actions"], {"can_propose": True, "can_lock": True})
        self.assertEqual(sum(Decimal(str(x["amount"])) for x in detail["participants"]), Decimal(".03"))
        self.assertEqual(detail["proposals"][0]["reviewed_by"], "finance@example.invalid")
        self.assertIn("decide_settlement_adjustment_proposal", {x["action"] for x in detail["lifecycle"]})
        self.assertEqual(self.request("POST", "/api/settlements/scope-set/lock", actor="buyer_admin")["status"], "locked")
        self.request("POST", "/api/settlements/scope-set/proposals", actor="buyer_admin", expected=409, json=body)
        locked = self.request("GET", "/api/enterprise-settlements/scope-set", actor="buyer_admin")
        self.assertEqual(locked["actions"], {"can_propose": False, "can_lock": False})

    def test_platform_proposal_requires_original_provider_and_rejection_is_visible(self):
        body = self.settlement_fixture()
        proposal = self.request("POST", "/api/settlements/scope-set/proposals", actor="finance", json=body)
        detail = self.request("GET", "/api/enterprise-settlements/scope-set", actor="buyer_admin")
        self.assertTrue(detail["proposals"][0]["actions"]["can_decide"])
        path = "/api/settlement-proposals/" + proposal["id"] + "/decision"
        self.request("POST", path, actor="ops", expected=409, json={"decision": "approve"})
        self.request("POST", path, actor="outsider", expected=404, json={"decision": "approve"})
        self.request("POST", path + "?business_view=buyer", actor="buyer_admin", expected=403, json={"decision": "approve"})
        self.request("POST", path, actor="buyer_admin", json={"decision": "reject", "comment": "Original provider rejects"})
        detail = self.request("GET", "/api/settlements/scope-set/detail", actor="buyer_admin")
        self.assertEqual(detail["proposals"][0]["review_comment"], "Original provider rejects")
        self.assertFalse(detail["actions"]["can_lock"])
        self.request("POST", "/api/settlements/scope-set/lock", actor="finance", expected=409)
        self.request("GET", "/api/settlement-batches", actor="buyer_admin", expected=403)
        self.request("POST", "/api/settlement-batches/scope-batch/pay", actor="buyer_admin", expected=403, json={})
        with self.m.SessionLocal() as db:
            db.get(self.m.SettlementBatch, "scope-batch").status = "confirmed"
            db.commit()
        self.request("POST", "/api/settlements/scope-set/proposals", actor="buyer_admin", expected=409, json=body)

    def test_legacy_proposals_keep_platform_second_operator_rule_and_history_stays_local(self):
        import json
        body = self.settlement_fixture()
        with self.m.SessionLocal() as db:
            db.get(self.m.Settlement, "scope-set").status = "disputed"
            db.add(self.m.SettlementAdjustmentProposal(id="legacy-proposal", settlement_id="scope-set",
                proposed_by="finance@example.invalid", reason="Historical proposal", values_json=json.dumps(body)))
            db.add(self.m.Settlement(id="foreign-set", settlement_no="FOREIGN-SET", order_id="lens-buy"))
            db.flush()
            db.add(self.m.SettlementAdjustmentProposal(id="foreign-proposal", settlement_id="foreign-set",
                proposed_by="outsider", reason="foreign", values_json=json.dumps(body)))
            db.add(self.m.AuditLog(actor="outsider", action="foreign-batch-event", target_type="settlement",
                target_id="foreign-settlement", batch_no="SCOPE-BATCH", business_domain="settlement", detail="foreign-secret"))
            db.add(self.m.AuditLog(actor="outsider", action="foreign-proposal-event", target_type="settlement_proposal",
                target_id="foreign-proposal", order_id="lens-sell", batch_no="SCOPE-BATCH", business_domain="settlement", detail="foreign-secret"))
            db.commit()
        path = "/api/settlement-proposals/legacy-proposal/decision"
        provider_detail = self.request("GET", "/api/enterprise-settlements/scope-set", actor="buyer_admin")
        self.assertEqual(provider_detail["proposals"][0]["proposer_side"], "")
        self.assertFalse(provider_detail["proposals"][0]["actions"]["can_decide"])
        platform_detail = self.request("GET", "/api/settlements/scope-set/detail", actor="ops")
        self.assertEqual(platform_detail["proposals"][0]["proposer_side"], "")
        self.assertEqual(platform_detail["proposals"][0]["proposed_by"], "finance@example.invalid")
        self.assertTrue(platform_detail["proposals"][0]["actions"]["can_decide"])
        self.request("POST", path, actor="buyer_admin", expected=409, json={"decision": "approve"})
        self.request("POST", path, actor="finance", expected=409, json={"decision": "approve"})
        self.request("POST", path, actor="ops", json={"decision": "approve"})
        detail = self.request("GET", "/api/enterprise-settlements/scope-set", actor="buyer_admin")
        self.assertNotIn("foreign-secret", str(detail))
        decision = next(x for x in detail["lifecycle"] if x["action"] == "decide_settlement_adjustment_proposal")
        self.assertEqual((decision["actor"], decision["before"]["status"], decision["after"]["proposal_status"]),
            ("ops@example.invalid", "disputed", "accepted"))
        self.assertEqual(decision["result"], "success")
        self.assertTrue(decision["created_at"])
        self.assertTrue(detail["actions"]["can_lock"])

    def test_profit_allocation_preserves_cents_without_negative_participants(self):
        from importlib import import_module
        allocate = import_module(self.m.__package__ + ".enterprise_settlement_scope").proposal_amounts
        for profit in (Decimal(".01"), Decimal(".03"), Decimal("123456.78")):
            amounts = allocate(profit, {k + "_rate": 20 for k in ("platform", "provider", "service", "expert", "channel")})
            self.assertEqual(sum(amounts.values()), profit)
            self.assertTrue(all(amount >= 0 for amount in amounts.values()))

    def test_message_default_scope_sender_context_and_drafts_are_current_enterprise_only(self):
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.Membership(user_id="buyer_admin", enterprise_id="other_tenant", role="enterprise_admin"))
            db.flush()
            for eid in ("buyer_tenant", "other_tenant"):
                m.message_center["create"](db, ["buyer_admin"], eid, "legacy urgent notice", "legacy", "legacy",
                    event_key="legacy-scope:" + eid, category="security", tenant_id=eid, delivery=False)
            db.commit()
        inbox = self.request("GET", "/api/notifications", actor="buyer_admin")
        self.assertEqual([x["title"] for x in inbox["items"]], ["buyer_tenant"])
        context = self.request("GET", "/api/notifications/sender-context?enterprise_id=buyer_tenant", actor="buyer_admin")
        self.assertEqual([x["id"] for x in context["enterprises"]], ["buyer_tenant"])
        self.assertTrue(all(x["enterprise_ids"] == ["buyer_tenant"] for x in context["users"]))
        draft = {"title": "Draft", "content": "Safe", "tenant_id": "other_tenant", "draft": True}
        item = self.request("POST", "/api/notifications/send?enterprise_id=other_tenant", actor="buyer_admin", json=draft)
        self.request("POST", "/api/notifications/send?enterprise_id=buyer_tenant", actor="buyer_admin", expected=403, json=draft)
        self.assertEqual(self.request("GET", "/api/notifications/sent?folder=drafts", actor="buyer_admin")["total"], 0)
        self.request("GET", "/api/notifications/sent/" + item["id"], actor="buyer_admin", expected=404)
        self.request("PUT", "/api/notifications/sent/" + item["id"], actor="buyer_admin", expected=404, json=draft)
        self.request("POST", "/api/notifications/sent/" + item["id"] + "/publish", actor="buyer_admin", expected=404)

    def test_sse_stops_after_membership_revocation(self):
        from starlette.requests import Request
        m = self.m
        endpoint = next(r.endpoint for r in m.app.routes if r.path == "/api/notifications/stream")
        request = Request({"type": "http", "method": "GET", "path": "/api/notifications/stream", "query_string": b"enterprise_id=buyer_tenant",
            "headers": [(b"authorization", ("Bearer " + self.tokens["buyer_admin"]).encode())]})
        async def disconnected():
            return False
        request.is_disconnected = disconnected
        async def no_sleep(_):
            pass
        async def consume():
            with m.SessionLocal() as db:
                user = db.get(m.User, "buyer_admin")
                scope = {"enterprise_id": "buyer_tenant", "mode": "both"}
                response = await endpoint(request, user, db, scope)
                stream = response.body_iterator
                self.assertIn("connected", await anext(stream))
                membership = db.scalar(self.select(m.Membership).where(m.Membership.user_id == user.id))
                membership.status = "inactive"
                db.commit()
                self.assertIn("auth_expired", await anext(stream))
                await stream.aclose()
        with patch("asyncio.sleep", no_sleep):
            asyncio.run(consume())

    def test_attachment_owner_scope_and_recipient_view_checks_precede_storage(self):
        m = self.m
        ids = self.seed_view_messages()
        Attachment, Link = (m.message_center[k] for k in ("Attachment", "MessageAttachment")) if "MessageAttachment" in m.message_center else (m.message_center["Attachment"], next(x.class_ for x in m.Base.registry.mappers if x.local_table.name == "notification_message_attachments"))
        with m.SessionLocal() as db:
            db.add_all([Attachment(id="owned-other", owner_id="buyer_admin", tenant_id="other_tenant", name="x.txt", object_name="other", size=1),
                Attachment(id="owned-here", owner_id="buyer_admin", tenant_id="buyer_tenant", name="x.txt", object_name="here", size=1),
                Attachment(id="received-buy", owner_id="outsider", tenant_id="buyer_tenant", name="x.txt", object_name="buy", size=1)])
            db.flush()
            receipt = db.get(self.Receipt, ids["buy"])
            db.add(Link(message_id=receipt.message_id, attachment_id="received-buy"))
            db.commit()
        self.request("GET", "/api/notifications/attachments/owned-other/download", actor="buyer_admin", expected=404)
        self.request("GET", "/api/notifications/attachments/received-buy/download?business_view=provider", actor="buyer_admin", expected=404)
        draft = {"title": "Draft", "content": "Safe", "tenant_id": "buyer_tenant", "draft": True, "attachment_ids": ["owned-other"]}
        self.request("POST", "/api/notifications/send", actor="buyer_admin", expected=403, json=draft)
        from types import SimpleNamespace
        stored = SimpleNamespace(stream=lambda _: [b"x"], close=lambda: None, release_conn=lambda: None)
        with patch.object(m, "MINIO_ENDPOINT", "isolated.invalid"), patch.object(m, "Minio") as storage:
            storage.return_value.get_object.return_value = stored
            self.request("GET", "/api/notifications/attachments/owned-here/download", actor="buyer_admin")
            self.request("GET", "/api/notifications/attachments/received-buy/download?business_view=buyer", actor="buyer_admin")

    def test_delivery_attachment_queries_recheck_current_enterprise_and_view(self):
        with self.m.SessionLocal() as db:
            db.add(self.m.DeliveryTask(id="scope-delivery", order_id="lens-buy", method="file", status="pending"))
            db.commit()
        self.request("GET", "/api/delivery-tasks/scope-delivery/attachments?business_view=buyer", actor="buyer_admin")
        self.request("GET", "/api/delivery-tasks/scope-delivery/attachments?business_view=provider", actor="buyer_admin", expected=404)
        self.request("GET", "/api/delivery-tasks/scope-delivery/attachments?enterprise_id=other_tenant", actor="buyer_admin", expected=403)

    def test_attachment_tenant_migration_is_additive_repeatable_and_old_writer_compatible(self):
        from sqlalchemy import inspect, text
        m = self.m
        legacy_insert = text("""INSERT INTO notification_attachments
            (id, owner_id, name, object_name, size, content_type, scan_result, created_at)
            VALUES (:id, 'buyer_admin', 'legacy.txt', :id, 1, 'text/plain', 'clean', CURRENT_TIMESTAMP)""")
        with m.engine.begin() as connection:
            connection.execute(text("ALTER TABLE notification_attachments DROP COLUMN tenant_id"))
            connection.execute(legacy_insert, {"id": "legacy-before-migration"})
        self.assertNotIn("tenant_id", {c["name"] for c in inspect(m.engine).get_columns("notification_attachments")})
        m.ensure_review_and_file_schema()
        m.ensure_review_and_file_schema()
        columns = [c["name"] for c in inspect(m.engine).get_columns("notification_attachments")]
        self.assertEqual(columns.count("tenant_id"), 1)
        with m.engine.begin() as connection:
            # The old image writes only its original columns after rollback.
            connection.execute(legacy_insert, {"id": "legacy-after-migration"})
            rows = connection.execute(text("SELECT id, owner_id, name, tenant_id FROM notification_attachments ORDER BY id")).all()
        self.assertEqual({row.id for row in rows}, {"legacy-before-migration", "legacy-after-migration"})
        self.assertTrue(all(row.tenant_id == "" and row.owner_id == "buyer_admin" for row in rows))
        self.request("GET", "/api/notifications/attachments/legacy-before-migration/download", actor="buyer_admin", expected=404)

    def test_platform_global_manual_notices_are_shared_only_with_their_recipients(self):
        from types import SimpleNamespace
        m = self.m
        Attachment = m.message_center["Attachment"]
        with m.SessionLocal() as db:
            db.add(Attachment(id="global-notice-attachment", owner_id="platform", name="notice.txt",
                object_name="global-notice", size=1, tenant_id=""))
            for title, tenant, sender, target in (
                ("Current company", "buyer_tenant", "buyer_admin", ""),
                ("Foreign company", "other_tenant", "platform", ""),
                ("Ambiguous global business", "", "system", "order"),
                ("Untrusted global manual", "", "buyer_admin", ""),
            ):
                m.message_center["create"](db, ["buyer_admin"], title, "safe notice", target, "unknown" if target else "",
                    event_key="shared:" + title, category="announcement", tenant_id=tenant, sender_id=sender, delivery=False)
            db.commit()
        for category in ("announcement", "system"):
            self.request("POST", "/api/notifications/send", actor="platform", json={
                "title": "Global " + category, "content": "Platform manual notice", "category": category,
                "recipient_ids": ["buyer_admin"], "attachment_ids": ["global-notice-attachment"]})
        self.request("POST", "/api/notifications/send", actor="platform", json={
            "title": "Someone else's global notice", "content": "Not addressed to buyer admin", "recipient_ids": ["outsider"]})
        expected = {"Current company", "Global announcement", "Global system"}
        for mode in ("", "provider", "buyer"):
            query = "?enterprise_id=buyer_tenant" + ("&business_view=" + mode if mode else "")
            inbox = self.request("GET", "/api/notifications" + query, actor="buyer_admin")
            self.assertEqual({x["title"] for x in inbox["items"]}, expected)
            self.assertEqual((inbox["total"], inbox["unread"]), (3, 3))
            rid = next(x["id"] for x in inbox["items"] if x["title"] == "Global system")
            detail = self.request("GET", "/api/notifications/" + rid + query, actor="buyer_admin")
            self.assertEqual(detail["attachments"][0]["id"], "global-notice-attachment")
            stored = SimpleNamespace(stream=lambda _: [b"x"], close=lambda: None, release_conn=lambda: None)
            with patch.object(m, "MINIO_ENDPOINT", "isolated.invalid"), patch.object(m, "Minio") as storage:
                storage.return_value.get_object.return_value = stored
                self.request("GET", "/api/notifications/attachments/global-notice-attachment/download" + query, actor="buyer_admin")
        self.request("GET", "/api/notifications/" + rid + "?business_view=buyer", actor="buyer", expected=404)
        self.request("GET", "/api/notifications/attachments/global-notice-attachment/download?business_view=buyer", actor="buyer", expected=404)
        result = self.request("POST", "/api/notifications/read-all?enterprise_id=buyer_tenant&business_view=provider",
            actor="buyer_admin", json={"cutoff": m.now().isoformat()})
        self.assertEqual(result["processed"], 3)
        self.assertEqual(self.request("GET", "/api/notifications/summary?business_view=buyer", actor="buyer_admin")["unread"], 0)
        with m.SessionLocal() as db:
            hidden = db.scalar(self.select(self.Message.id).where(self.Message.title == "Foreign company"))
            self.assertIsNone(db.scalar(self.select(self.Receipt.first_read_at).where(self.Receipt.message_id == hidden,
                self.Receipt.user_id == "buyer_admin")))

    def test_company_sender_can_preview_uploaded_draft_sent_and_recalled_attachments(self):
        from types import SimpleNamespace
        m = self.m
        stored = SimpleNamespace(stream=lambda _: [b"notice"], close=lambda: None, release_conn=lambda: None)
        query = "?enterprise_id=buyer_tenant&business_view=provider"
        with patch.object(m, "MINIO_ENDPOINT", "isolated.invalid"), patch.object(m, "Minio") as storage, \
                patch.object(m, "clamav_scan_stream", return_value=("clean", "isolated")):
            storage.return_value.get_object.return_value = stored
            uploaded = self.request("POST", "/api/notifications/attachments" + query, actor="buyer_admin",
                files={"upload": ("notice.txt", b"notice", "text/plain")})
            aid = uploaded["id"]
            download = "/api/notifications/attachments/" + aid + "/download"
            self.request("GET", download + query, actor="buyer_admin")
            body = {"title": "Company draft", "content": "Owner preview", "tenant_id": "buyer_tenant",
                "recipient_ids": ["buyer"], "attachment_ids": [aid], "draft": True}
            draft = self.request("POST", "/api/notifications/send" + query, actor="buyer_admin", json=body)
            self.request("GET", download + query, actor="buyer_admin")
            self.request("POST", "/api/notifications/sent/" + draft["id"] + "/publish" + query, actor="buyer_admin")
            self.request("GET", download + query, actor="buyer_admin")
            self.request("GET", download + "?enterprise_id=buyer_tenant&business_view=buyer", actor="buyer")
            with m.SessionLocal() as db:
                self.assertFalse(db.scalar(self.select(self.Receipt.id).where(self.Receipt.message_id == draft["id"],
                    self.Receipt.user_id == "buyer_admin")))
                db.add(m.Membership(user_id="buyer_admin", enterprise_id="other_tenant", role="enterprise_admin"))
                db.commit()
            self.request("GET", download + "?enterprise_id=other_tenant&business_view=provider", actor="buyer_admin", expected=404)
            self.request("POST", "/api/notifications/sent/" + draft["id"] + "/recall" + query, actor="buyer_admin",
                json={"reason": "Owner recalls notice"})
            self.request("GET", download + query, actor="buyer_admin")
            self.request("GET", download + "?enterprise_id=buyer_tenant&business_view=buyer", actor="buyer", expected=404)
            with m.SessionLocal() as db:
                membership = db.scalar(self.select(m.Membership).where(m.Membership.user_id == "buyer_admin",
                    m.Membership.enterprise_id == "buyer_tenant"))
                membership.role = "member"
                db.commit()
            self.request("GET", download + query, actor="buyer_admin", expected=404)
            with m.SessionLocal() as db:
                for membership in db.scalars(self.select(m.Membership).where(m.Membership.user_id == "buyer_admin")):
                    membership.status = "inactive"
                db.commit()
            self.request("GET", download, actor="buyer_admin", expected=404)
            self.request("GET", download + query, actor="buyer_admin", expected=403)

    def test_linked_owner_preview_requires_current_message_author_and_business_view(self):
        from types import SimpleNamespace
        m = self.m
        Attachment = m.message_center["Attachment"]
        Link = next(x.class_ for x in m.Base.registry.mappers if x.local_table.name == "notification_message_attachments")
        with m.SessionLocal() as db:
            for key, sender, target in (("buyer-link", "buyer_admin", "lens-buy"), ("foreign-author", "outsider", "lens-sell")):
                message = m.message_center["create"](db, [], key, "safe notice", "order", target,
                    sender_id=sender, tenant_id="buyer_tenant", event_key="owner:" + key, status="draft", delivery=False)
                db.add(Attachment(id=key, owner_id="buyer_admin", tenant_id="buyer_tenant", name="x.txt", object_name=key, size=1))
                db.flush()
                db.add(Link(message_id=message.id, attachment_id=key))
            db.commit()
        stored = SimpleNamespace(stream=lambda _: [b"x"], close=lambda: None, release_conn=lambda: None)
        with patch.object(m, "MINIO_ENDPOINT", "isolated.invalid"), patch.object(m, "Minio") as storage:
            storage.return_value.get_object.return_value = stored
            self.request("GET", "/api/notifications/attachments/buyer-link/download?business_view=buyer", actor="buyer_admin")
            self.request("GET", "/api/notifications/attachments/buyer-link/download?business_view=provider", actor="buyer_admin", expected=404)
            self.request("GET", "/api/notifications/attachments/foreign-author/download?business_view=provider", actor="buyer_admin", expected=404)
