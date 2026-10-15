"""Focused state, authorization, idempotency and atomicity tests on isolated SQLite."""
import json
import unittest
from unittest.mock import patch

import test_message_business_events as fixture


class AfterSalesWorkflowTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown
    request = fixture.BusinessRoutesHTTPTests.request
    create_order = fixture.BusinessRoutesHTTPTests.create_order

    def setUp(self):
        fixture.BusinessRoutesHTTPTests.setUp(self)
        self.freeze = patch.object(self.m, "freeze_sla_snapshot", create=True,
            return_value={"service_level_code": "standard", "version": 1,
                          "response_minutes": 2880, "calendar": {"timezone": "Asia/Shanghai"}})
        self.freeze.start()
        self.addCleanup(self.freeze.stop)
        with self.m.SessionLocal() as db:
            for enterprise in db.scalars(self.select(self.m.Enterprise)):
                enterprise.verification_status = "verified"
            db.commit()
        self.order_id = self.create_order()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            order.payment_status, order.paid_amount = "paid", order.amount
            db.commit()

    def create(self, actor="buyer", expected=200, **changes):
        body = {"order_id": self.order_id, "description": "设备数据不完整", "idempotency_key": "create-ticket-001", **changes}
        return self.request("POST", "/api/after-sales", actor=actor, expected=expected, json=body)

    def act(self, ticket, action, actor, expected=200, **changes):
        return self.request("POST", "/api/after-sales/" + ticket["id"] + "/actions", actor=actor,
            expected=expected, json={"action": action, "reason": "处理说明", "expected_version": ticket["version"],
                                   "idempotency_key": f"action-{action}-{ticket['version']}", **changes})

    def test_full_response_information_resolution_rejection_and_buyer_confirmation(self):
        ticket = self.create()
        self.assertEqual(ticket["status"], "awaiting_response")
        self.assertIsNone(ticket["first_response_at"])
        for action, actor, status in (("respond", "provider", "processing"),
                ("request_info", "provider", "awaiting_buyer_info"), ("supplement", "buyer_admin", "processing"),
                ("submit_resolution", "provider", "awaiting_buyer_confirmation"),
                ("reject_resolution", "buyer", "processing"), ("submit_resolution", "provider", "awaiting_buyer_confirmation"),
                ("confirm", "buyer_admin", "completed")):
            ticket = self.act(ticket, action, actor)
            self.assertEqual(ticket["status"], status)
        self.assertTrue(ticket["first_response_at"])
        self.assertTrue(ticket["closed_at"])
        events = self.request("GET", f"/api/after-sales/{ticket['id']}/timeline")["items"]
        self.assertEqual(len(events), 8)
        self.assertEqual([e["after"]["version"] for e in events], list(range(1, 9)))
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, self.order_id).after_sales_status, "resolved")

    def test_only_buyer_admin_creates_and_provider_cannot_confirm(self):
        for actor in ("provider", "platform", "ops", "finance", "outsider", "inactive"):
            self.create(actor, 404 if actor == "outsider" else 403)
        ticket = self.create()
        self.act(ticket, "respond", "platform", 403)
        self.act(ticket, "respond", "buyer", 403)
        ticket = self.act(ticket, "respond", "provider")
        ticket = self.act(ticket, "submit_resolution", "provider")
        self.act(ticket, "confirm", "provider", 403)
        self.act(ticket, "confirm", "platform", 403)

    def test_regular_member_disabled_membership_and_cross_tenant_are_denied(self):
        ticket = self.create()
        for actor in ("outsider", "finance", "reviewer", "quality"):
            self.request("GET", f"/api/after-sales/{ticket['id']}", actor=actor, expected=404)
        with self.m.SessionLocal() as db:
            member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "buyer_admin"))
            member.role = "member"
            db.commit()
        self.create("buyer_admin", 403, idempotency_key="another-create-key")
        self.request("GET", f"/api/after-sales/{ticket['id']}", actor="buyer_admin", expected=404)

    def test_explicit_enterprise_scope_cannot_be_ignored(self):
        ticket = self.create()
        path = f"/api/after-sales/{ticket['id']}"
        for suffix in ("", "/timeline", "/sla"):
            self.request("GET", path + suffix + "?enterprise_id=other_tenant", expected=403)
        body = {"action": "withdraw", "reason": "不可跨企业", "expected_version": 1, "idempotency_key": "wrong-scope-action"}
        self.request("POST", path + "/actions?enterprise_id=other_tenant", expected=403, json=body)
        with self.m.SessionLocal() as db:
            db.add(self.m.Membership(user_id="buyer", enterprise_id="other_tenant", role="super_admin"))
            db.commit()
        self.request("GET", path + "?enterprise_id=other_tenant", expected=404)
        self.request("GET", path + "?enterprise_id=buyer_tenant&business_view=provider", expected=404)

    def test_duplicate_creation_action_replay_and_different_payload_key_conflict(self):
        ticket = self.create()
        self.assertEqual(ticket["id"], self.create()["id"])
        self.create(expected=409, description="同键不同说明")
        self.create(expected=409, idempotency_key="new-create-key")
        processed = self.act(ticket, "respond", "provider")
        self.assertEqual(self.act(ticket, "respond", "provider")["version"], processed["version"])
        self.act(ticket, "respond", "provider", 409, reason="不同处理说明")
        with self.m.SessionLocal() as db:
            self.assertEqual(len(db.scalars(self.select(self.m.AfterSalesEvent)).all()), 2)

    def test_stale_version_and_out_of_order_actions_are_conflicts(self):
        ticket = self.create()
        self.act(ticket, "submit_resolution", "provider", 409)
        newer = self.act(ticket, "respond", "provider")
        self.act(ticket, "request_info", "provider", 409, idempotency_key="stale-action-key")
        self.act(newer, "request_info", "provider", 422, reason="  ")
        self.act(newer, "unknown", "provider", 422)

    def test_intervention_returns_to_original_stage_and_does_not_count_as_response(self):
        ticket = self.create()
        ticket = self.act(ticket, "request_intervention", "provider")
        self.assertEqual(ticket["previous_status"], "awaiting_response")
        ticket = self.act(ticket, "review_intervention", "ops")
        self.assertEqual(ticket["status"], "awaiting_response")
        self.assertIsNone(ticket["first_response_at"])
        ticket = self.act(ticket, "respond", "provider")
        ticket = self.act(ticket, "request_info", "provider")
        ticket = self.act(ticket, "request_intervention", "buyer")
        ticket = self.act(ticket, "review_intervention", "platform")
        self.assertEqual(ticket["status"], "awaiting_buyer_info")

    def test_force_close_requires_platform_operator_reason_and_terminal_is_immutable(self):
        ticket = self.create()
        self.act(ticket, "force_close", "provider", 403)
        self.act(ticket, "force_close", "ops", 422, reason=" ")
        ticket = self.act(ticket, "force_close", "ops")
        self.assertEqual(ticket["status"], "force_closed")
        self.act(ticket, "respond", "provider", 409)
        self.act(ticket, "force_close", "platform", 409)

    def test_withdraw_allows_new_ticket_but_does_not_rewrite_history(self):
        ticket = self.create()
        ticket = self.act(ticket, "withdraw", "buyer")
        created = self.create(idempotency_key="new-after-withdraw")
        self.assertNotEqual(ticket["id"], created["id"])
        self.assertEqual(self.request("GET", f"/api/after-sales/{ticket['id']}")["status"], "withdrawn")

    def test_list_blocks_unverified_users_enterprises_and_other_platform_roles(self):
        for actor in ("finance", "reviewer", "quality"):
            self.request("GET", "/api/after-sales", actor=actor, expected=403)
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "buyer").verified_status = "pending_review"
            db.commit()
        self.request("GET", "/api/after-sales", expected=403)
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "buyer").verified_status = "verified"
            db.get(self.m.Enterprise, "buyer_tenant").verification_status = "pending_review"
            db.commit()
        self.request("GET", "/api/after-sales", expected=403)

    def test_sla_snapshot_does_not_follow_later_configuration(self):
        ticket = self.create()
        with self.m.SessionLocal() as db:
            snapshot = json.loads(db.get(self.m.AfterSalesTicket, ticket["id"]).sla_snapshot_json)
        self.m.freeze_sla_snapshot.return_value = {"service_level_code": "strategic", "version": 2}
        self.act(ticket, "respond", "provider")
        with self.m.SessionLocal() as db:
            self.assertEqual(json.loads(db.get(self.m.AfterSalesTicket, ticket["id"]).sla_snapshot_json), snapshot)

    def test_sla_unknown_calendar_does_not_hide_natural_emergency_response(self):
        rule = {"responsibility": "provider", "target_seconds": 3600,
                "warning_seconds": 2880, "clock": "natural"}
        self.m.freeze_sla_snapshot.return_value = {"service_level_code": "standard",
            "metrics": {"urgent_first_response": rule, "urgent_resolution": {**rule, "clock": "support"}},
            "calendar": {"version": "not-configured", "timezone": "Asia/Shanghai", "weekdays": [0, 1, 2, 3, 4],
                         "windows": [["09:00", "17:00"]], "covered_years": []}}
        ticket = self.create(priority="urgent")
        result = self.request("GET", f"/api/after-sales/{ticket['id']}/sla")
        self.assertEqual(result["calendar_status"], "not_ready")
        response, resolution = result["items"]
        self.assertIsNotNone(response["elapsed_seconds"])
        self.assertEqual(response["status"], "running")
        self.assertEqual(resolution["status"], "insufficient_data")
        self.request("GET", f"/api/after-sales/{ticket['id']}/sla", actor="outsider", expected=404)

    def test_sla_old_missing_evidence_remains_unknown(self):
        ticket = self.create()
        with self.m.SessionLocal() as db:
            old = db.get(self.m.AfterSalesTicket, ticket["id"])
            old.evidence_status, old.sla_snapshot_json = "historical_unknown", "{}"
            db.commit()
        result = self.request("GET", f"/api/after-sales/{ticket['id']}/sla")
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["items"], [])

    def test_message_failure_rolls_back_ticket_events_order_and_audit(self):
        with patch.object(self.m, "notify_business_event", side_effect=RuntimeError("outbox failure")):
            self.create(expected=500)
        with self.m.SessionLocal() as db:
            self.assertEqual(len(db.scalars(self.select(self.m.AfterSalesTicket)).all()), 0)
            self.assertEqual(len(db.scalars(self.select(self.m.AfterSalesEvent)).all()), 0)
            self.assertEqual(db.get(self.m.Order, self.order_id).after_sales_status, "none")

    def test_legacy_entry_uses_new_permissions_and_cannot_close_directly(self):
        path = f"/api/orders/{self.order_id}/transition"
        self.request("POST", path, actor="platform", expected=403, json={"action": "submit_after_sales", "reason": "问题"})
        self.request("POST", path, json={"action": "submit_after_sales", "reason": "问题"})
        self.request("POST", path, actor="platform", expected=409, json={"action": "close_after_sales", "reason": "不能绕过工单"})
        with self.m.SessionLocal() as db:
            ticket = db.scalar(self.select(self.m.AfterSalesTicket))
            self.assertEqual(ticket.status, "awaiting_response")


if __name__ == "__main__":
    unittest.main()
