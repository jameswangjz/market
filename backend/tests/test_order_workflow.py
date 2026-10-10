"""SQLite-only HTTP regressions using the workflow installed by main.py."""
import importlib
import json
import unittest
from datetime import timedelta, timezone
from unittest.mock import patch

import test_message_business_events as fixtures


class OrderWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.BusinessRoutesHTTPTests.setUpClass.__func__(cls)
        m = cls.m
        cls.workflow_module = importlib.import_module(m.__package__ + ".order_workflow")
        if not hasattr(m, "order_workflow"):
            raise AssertionError("main.py must install order_workflow after order_economics")

    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixtures.BusinessRoutesHTTPTests.setUp
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request
    fail_after_create = fixtures.BusinessRoutesHTTPTests.fail_after_create

    def create(self, method="consulting"):
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, "product").delivery_method = method
            db.commit()
        with patch.dict(self.m.storefront, {"versions_for": lambda db, p: [{"id": "version"}]}):
            return self.request("POST", "/api/orders", json={"product_id": "product",
                "product_version_id": "version", "buyer_enterprise_id": "buyer_tenant"})

    def review(self, order, actor="provider", expected=200, **body):
        return self.request("POST", f'/api/orders/{order["id"]}/provider-review', actor=actor,
                            expected=expected, json={"decision": "approve", **body})

    def transition(self, order, action="confirm_payment", actor="buyer", expected=200):
        return self.request("POST", f'/api/orders/{order["id"]}/transition', actor=actor,
                            expected=expected, json={"action": action})

    def counts(self):
        with self.m.SessionLocal() as db:
            return tuple(db.scalar(self.select(self.func.count()).select_from(model)) for model in (
                self.m.OrderStateLog, self.m.Payment, self.m.DeliveryTask,
                self.m.AuditLog, self.Message, self.Receipt, self.Outbox,
                self.m.SettlementMeasurement))

    def test_initial_status_and_initialize_only_changes_status(self):
        for method in ("training", "consulting", "custom", "file", "api", "model_api", "tenant_access"):
            with self.subTest(method=method):
                order = self.create(method)
                self.assertEqual(order["main_status"], "pending_provider_review" if method in self.workflow_module.OFFLINE else "pending_payment")
                with self.m.SessionLocal() as db:
                    saved = db.get(self.m.Order, order["id"])
                    before = dict(saved.__dict__)
                    with patch.object(db, "flush", side_effect=AssertionError("initialize must not flush")):
                        self.m.order_workflow["initialize_order"](db, saved, db.get(self.m.User, "buyer"))
                    self.assertEqual(before, saved.__dict__)
                    log = db.scalar(self.select(self.m.OrderStateLog).where(self.m.OrderStateLog.order_id == saved.id))
                    self.assertEqual(log.to_status, saved.main_status)

    def test_legacy_initialize_and_review_actions_unchanged(self):
        order = self.create()
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order["id"])
            saved.snapshot_version = 0
            saved.main_status = "created"
            user = db.get(self.m.User, "buyer")
            self.m.order_workflow["initialize_order"](db, saved, user)
            self.assertEqual(saved.main_status, "created")
            for action in ("submit_review", "approve"):
                self.assertFalse(self.m.order_workflow["authorize_transition"](db, user, saved, action))
            db.commit()
        self.review(order, expected=409)

    def test_offline_cannot_bypass_review(self):
        order = self.create()
        before = self.counts()
        for action in ("submit_review", "approve", "reject", "start_payment", "confirm_payment"):
            self.transition(order, action, actor="platform" if action in {"approve", "reject"} else "buyer", expected=409)
        self.assertEqual(before, self.counts())

    def test_new_online_order_cannot_enter_legacy_review_flow(self):
        order = self.create("file")
        before = self.counts()
        for action in ("submit_review", "approve", "reject"):
            self.transition(order, action, actor="platform" if action in {"approve", "reject"} else "buyer", expected=409)
            self.transition(order, action, actor="outsider", expected=403)
        self.assertEqual(before, self.counts())
        self.transition(order)

    def test_provider_roles_tenant_isolation_and_platform_no_override(self):
        order = self.create()
        before = self.counts()
        for actor in ("buyer", "buyer_admin", "outsider", "platform", "ops", "finance", "inactive", "disabled_ops"):
            status = 401 if actor == "disabled_ops" else 403
            self.review(order, actor=actor, expected=status)
            self.request("GET", f'/api/orders/{order["id"]}/provider-quote', actor=actor, expected=status)
        self.assertEqual(before, self.counts())
        with self.m.SessionLocal() as db:
            member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "provider"))
            member.role = "enterprise_admin"
            db.commit()
        quote = self.request("GET", f'/api/orders/{order["id"]}/provider-quote', actor="provider")
        self.assertEqual(quote["cost"], "10.00")
        self.review(order)

    def test_provider_inactive_membership_member_role_and_account_denied(self):
        order = self.create()
        for field, value in (("status", "disabled"), ("role", "member"), ("enterprise_id", "other_tenant")):
            with self.m.SessionLocal() as db:
                member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "provider"))
                old = getattr(member, field)
                setattr(member, field, value)
                db.commit()
            self.review(order, expected=403)
            with self.m.SessionLocal() as db:
                member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "provider"))
                setattr(member, field, old)
                db.commit()
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "provider").is_active = False
            db.commit()
        self.review(order, expected=401)

    def test_review_updates_consistent_snapshots_payment_and_internal_audit(self):
        order = self.create()
        reviewed = self.review(order, amount="120.125", cost="37.19", reason="Internal cost 37.19")
        self.assertEqual(reviewed["amount"], 120.13)
        self.assertEqual(reviewed["main_status"], "pending_payment")
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order["id"])
            snapshot = self.m.order_economics["economic_snapshot"](saved)
            self.assertEqual(snapshot["unit_price"], snapshot["amount"])
            self.assertEqual(snapshot["unit_cost"], snapshot["total_cost"])
            self.assertEqual(snapshot["total_cost"], "37.19")
            self.assertEqual(db.scalar(self.select(self.m.Payment).where(self.m.Payment.order_id == saved.id)).amount, saved.amount)
            audit = db.scalar(self.select(self.m.AuditLog).where(self.m.AuditLog.action == "provider_review_approve"))
            self.assertEqual(json.loads(audit.before_json)["economic_snapshot"]["amount"], "100.00")
            self.assertEqual(json.loads(audit.after_json)["economic_snapshot"]["amount"], "120.13")
        for path in ("/api/orders", f'/api/orders/{order["id"]}', "/api/notifications"):
            payload = json.dumps(self.request("GET", path))
            self.assertNotIn("cost", payload)
            self.assertNotIn("37.19", payload)

    def test_rejection_requires_reason_and_is_terminal(self):
        order = self.create()
        self.review(order, decision="reject", expected=400, reason="   ")
        rejected = self.review(order, decision="reject", reason="Internal cost review")
        self.assertEqual(rejected["main_status"], "rejected")
        before = self.counts()
        self.review(order, expected=409)
        for action in ("start_payment", "confirm_payment"):
            self.transition(order, action, expected=409)
        self.assertEqual(before, self.counts())

    def test_changed_values_need_reason_price_must_cover_cost(self):
        order = self.create()
        before = self.counts()
        for body in ({"amount": 101}, {"cost": 11}, {"amount": 9, "cost": 10, "reason": "change"},
                     {"decision": "reject", "amount": 120, "reason": "reject"}):
            self.review(order, expected=400, **body)
        self.assertEqual(before, self.counts())
        self.review(order, amount=100, cost=10)

    def test_malformed_money_and_request_schema(self):
        order = self.create()
        before = self.counts()
        for field in ("amount", "cost"):
            for value in (True, None, -1, "NaN", "Infinity", "-Infinity", "bad", "9999999999999", [], {}):
                with self.subTest(field=field, value=value):
                    self.review(order, expected=422, **{field: value})
        for body in ({"decision": "other"}, {"price": 100}, {"expected_updated_at": "bad"}):
            self.review(order, expected=422, **body)
        self.assertEqual(before, self.counts())

    def test_stale_optional_timestamp_and_terminal_state_prevent_double_review(self):
        order = self.create()
        quote = self.request("GET", f'/api/orders/{order["id"]}/provider-quote', actor="provider")
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order["id"])
            saved.updated_at += timedelta(seconds=1)
            db.commit()
        before = self.counts()
        self.review(order, expected=409, expected_updated_at=quote["expected_updated_at"])
        self.assertEqual(before, self.counts())
        self.review(order)
        before = self.counts()
        self.review(order, expected=409)
        self.assertEqual(before, self.counts())

    def test_review_locks_order_and_payment_and_refreshes_identity_map(self):
        order = self.create()
        statements = []
        real = self.m.Session._execute_internal
        def record(db, statement, *args, **kwargs):
            statements.append(statement)
            return real(db, statement, *args, **kwargs)
        with patch.object(self.m.Session, "_execute_internal", new=record):
            self.review(order)
        locked = [stmt for stmt in statements if getattr(stmt, "_for_update_arg", None) is not None]
        self.assertTrue(any("orders" in str(stmt) for stmt in locked))
        self.assertTrue(any("payments" in str(stmt) for stmt in locked))
        self.assertTrue(all(stmt.get_execution_options().get("populate_existing") for stmt in locked))

    def test_loaded_stale_order_is_refreshed_before_second_review(self):
        order = self.create()
        endpoint = next(route.endpoint for route in self.m.app.routes
                        if getattr(route, "name", "") == "provider_review")
        with self.m.SessionLocal() as stale_db:
            stale = stale_db.get(self.m.Order, order["id"])
            provider = stale_db.get(self.m.User, "provider")
            self.review(order, amount=130, reason="scope")
            self.assertEqual(stale.main_status, "pending_provider_review")
            before = self.counts()
            with self.assertRaises(self.m.HTTPException) as raised:
                endpoint(order["id"], self.workflow_module.ProviderReviewBody(
                    decision="approve", amount=200, reason="competing quote"), provider, stale_db)
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(stale.main_status, "pending_payment")
            self.assertEqual(float(stale.amount), 130)
            self.assertEqual(before, self.counts())

    def test_matching_optimistic_timestamp_normalizes_naive_utc_and_offset(self):
        for offset in (False, True):
            order = self.create()
            with self.m.SessionLocal() as db:
                stamp = db.get(self.m.Order, order["id"]).updated_at
            utc = self.workflow_module.utc(stamp)
            token = utc.astimezone(timezone(timedelta(hours=-7))).isoformat() if offset else utc.replace(tzinfo=None).isoformat()
            self.review(order, expected_updated_at=token)

    def test_payment_caller_locks_and_refreshes_order_before_authorizing(self):
        order = self.create("file")
        statements = []
        real = self.m.Session._execute_internal
        def record(db, statement, *args, **kwargs):
            statements.append(statement)
            return real(db, statement, *args, **kwargs)
        with patch.object(self.m.Session, "_execute_internal", new=record):
            self.transition(order)
        locked_orders = [stmt for stmt in statements
                         if getattr(stmt, "_for_update_arg", None) is not None
                         and "orders" in str(stmt)]
        self.assertTrue(locked_orders, "Payment caller must lock Order")
        self.assertTrue(locked_orders[0].get_execution_options().get("populate_existing"),
                        "Payment caller must refresh Order after acquiring lock")

    def test_quote_freezes_after_review_and_catalog_edits(self):
        order = self.create()
        self.review(order, amount=140, cost=40, reason="scope")
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order["id"])
            original = (saved.economic_snapshot_json, saved.delivery_snapshot_json)
            db.get(self.m.ProductReleaseVersion, "version").price = 500
            db.get(self.m.ProductReleaseVersion, "version").cost = 300
            db.get(self.m.Product, "product").delivery_method = "file"
            db.get(self.m.SettlementRule, "trading_rule").platform_rate = 20
            db.commit()
        self.transition(order)
        self.review(order, expected=409, amount=200, reason="new scope")
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order["id"])
            self.assertEqual(original, (saved.economic_snapshot_json, saved.delivery_snapshot_json))
            self.assertEqual(self.m.order_cost(db, saved), self.m.Decimal("40.00"))
            self.assertEqual(self.m.delivery_task_config(db, saved)[0], "consulting")

    def test_start_only_buyer_super_and_confirm_buyer_or_platform_finance(self):
        order = self.create()
        self.review(order)
        before = self.counts()
        for actor in ("buyer_admin", "provider", "outsider", "platform", "finance", "ops", "inactive"):
            self.transition(order, "start_payment", actor, expected=403)
        for actor in ("buyer_admin", "provider", "outsider", "ops", "inactive", "disabled_ops"):
            self.transition(order, actor=actor, expected=401 if actor == "disabled_ops" else 403)
        self.assertEqual(before, self.counts())
        self.transition(order, "start_payment")
        self.transition(order, actor="finance")
        for actor in ("buyer", "platform"):
            before = self.counts()
            self.transition(order, actor=actor)
            self.assertEqual(before, self.counts())

    def test_online_finance_confirm_without_start_and_paid_retry_no_side_effects(self):
        order = self.create("file")
        self.transition(order, actor="platform")
        before = self.counts()
        for actor in ("buyer", "finance", "platform"):
            self.transition(order, actor=actor)
            self.assertEqual(before, self.counts())
        self.assertEqual(before[2], 1)
        self.transition(order, "start_payment", expected=409)

    def test_role_downgrade_and_membership_disable_apply_immediately(self):
        order = self.create("file")
        for field, value in (("role", "enterprise_admin"), ("status", "disabled"), ("enterprise_id", "other_tenant")):
            with self.m.SessionLocal() as db:
                member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "buyer"))
                old = getattr(member, field)
                setattr(member, field, value)
                db.commit()
            self.transition(order, expected=403)
            with self.m.SessionLocal() as db:
                member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "buyer"))
                setattr(member, field, old)
                db.commit()

    def test_hooks_bypass_metering_for_both_payment_actions_and_finance_for_legacy(self):
        order = self.create("file")
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order["id"])
            buyer = db.get(self.m.User, "buyer")
            for action in ("start_payment", "confirm_payment"):
                self.assertIs(self.m.order_workflow["authorize_transition"](db, buyer, saved, action), True)
            saved.snapshot_version = 0
            saved.main_status = "created"
            db.commit()
        self.transition(order, actor="finance")
        self.assertEqual(self.counts()[2], 1)

    def test_finance_inactive_activation_and_buyer_membership_denied_on_paid_retry(self):
        order = self.create("file")
        self.transition(order)
        before = self.counts()
        self.transition(order, actor="buyer_admin", expected=403)
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "finance").activation_status = "pending_activation"
            member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "buyer"))
            member.status = "disabled"
            db.commit()
        self.transition(order, actor="finance", expected=403)
        self.transition(order, expected=403)
        self.assertEqual(before, self.counts())

    def test_new_buyer_payment_rechecks_personal_and_enterprise_verification(self):
        order = self.create("file")
        for model, identity, field in ((self.m.User, "buyer", "verified_status"),
                                       (self.m.Enterprise, "buyer_tenant", "verification_status")):
            with self.m.SessionLocal() as db:
                setattr(db.get(model, identity), field, "pending")
                db.commit()
            before = self.counts()
            for action in ("start_payment", "confirm_payment"):
                self.transition(order, action, expected=403)
            self.assertEqual(before, self.counts())
            with self.m.SessionLocal() as db:
                setattr(db.get(model, identity), field, "verified")
                db.commit()
        self.transition(order)
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "buyer").verified_status = "pending"
            db.commit()
        self.transition(order, expected=403)

    def test_finance_can_confirm_new_orders_without_buyer_verification(self):
        order = self.create()
        self.review(order)
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "buyer").verified_status = "pending"
            db.get(self.m.Enterprise, "buyer_tenant").verification_status = "pending"
            db.get(self.m.User, "finance").verified_status = "pending"
            db.commit()
        self.transition(order, actor="finance")
        before = self.counts()
        self.transition(order, actor="platform")
        self.assertEqual(before, self.counts())

    def test_payment_mismatch_and_corrupt_snapshot_fail_without_side_effects(self):
        order = self.create("file")
        with self.m.SessionLocal() as db:
            payment = db.scalar(self.select(self.m.Payment).where(self.m.Payment.order_id == order["id"]))
            payment.amount = 99
            db.commit()
        before = self.counts()
        self.transition(order, expected=409)
        self.assertEqual(before, self.counts())
        with self.m.SessionLocal() as db:
            db.scalar(self.select(self.m.Payment).where(self.m.Payment.order_id == order["id"])).amount = 100
            db.get(self.m.Order, order["id"]).economic_snapshot_json = "{}"
            db.commit()
        self.transition(order, expected=409)
        self.assertEqual(before, self.counts())

    def test_review_refuses_payment_started_even_if_order_state_lags(self):
        order = self.create()
        with self.m.SessionLocal() as db:
            db.scalar(self.select(self.m.Payment).where(self.m.Payment.order_id == order["id"])).status = "paying"
            db.commit()
        before = self.counts()
        self.review(order, expected=409)
        self.assertEqual(before, self.counts())

    def test_unapproved_legacy_state_cannot_unlock_new_offline_payment(self):
        order = self.create()
        for state in ("created", "pending_review", "pending_fulfillment", "fulfilling", "completed"):
            with self.m.SessionLocal() as db:
                db.get(self.m.Order, order["id"]).main_status = state
                db.commit()
            self.transition(order, expected=409)

    def test_review_notification_failure_rolls_back_quote_payment_audit_and_logs(self):
        order = self.create()
        before = self.counts()
        with self.fail_after_create("order"):
            self.review(order, expected=500, amount=200, cost=50, reason="scope")
        self.assertEqual(before, self.counts())
        with self.m.SessionLocal() as db:
            saved = db.get(self.m.Order, order["id"])
            self.assertEqual(saved.main_status, "pending_provider_review")
            self.assertEqual(float(saved.amount), 100)
            self.m.order_economics["economic_snapshot"](saved)
            self.assertEqual(float(db.scalar(self.select(self.m.Payment).where(self.m.Payment.order_id == saved.id)).amount), 100)

    def test_review_missing_order_online_order_and_paid_inconsistency(self):
        self.review({"id": "missing"}, expected=404)
        self.request("GET", "/api/orders/missing/provider-quote", actor="provider", expected=404)
        order = self.create("file")
        self.review(order, expected=409)
        self.transition(order)
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, order["id"]).paid_amount = 99
            db.commit()
        before = self.counts()
        self.transition(order, expected=409)
        self.assertEqual(before, self.counts())


if __name__ == "__main__":
    unittest.main()
