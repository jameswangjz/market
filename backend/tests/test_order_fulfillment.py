"""Offline fulfillment hook regressions using real main models and SQLite.

No main integration is required: callers below exercise the documented locking
and transaction contract explicitly, with real audit/message/metering helpers.
Run: python -m unittest discover -s backend/tests -p test_order_fulfillment.py -v
"""
import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_message_business_events as fixtures


class OrderFulfillmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.BusinessRoutesHTTPTests.setUpClass.__func__(cls)
        cls.module = importlib.import_module(cls.m.__package__ + ".order_fulfillment")

    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request
    fail_after_create = fixtures.BusinessRoutesHTTPTests.fail_after_create
    recipients = fixtures.BusinessRoutesHTTPTests.recipients

    def setUp(self):
        fixtures.BusinessRoutesHTTPTests.setUp(self)
        self.ns = dict(vars(self.m))
        self.hooks = self.module.install(self.ns)
        with self.m.SessionLocal() as db:
            db.get(self.m.Enterprise, "buyer_tenant").verification_status = "verified"
            db.commit()

    def create(self, method="consulting", paid=True):
        m = self.m
        with m.SessionLocal() as db:
            db.get(m.Product, "product").delivery_method = method
            db.commit()
        with patch.dict(m.storefront, {"versions_for": lambda db, product: [{"id": "version"}]}):
            oid = self.request("POST", "/api/orders", json={"product_id": "product",
                "product_version_id": "version", "buyer_enterprise_id": "buyer_tenant"})["id"]
        if paid:
            with m.SessionLocal() as db:
                order = self.locked(db, oid)
                order.main_status = "pending_fulfillment"
                order.payment_status = "paid"
                order.paid_amount = order.amount
                payment = db.scalar(self.select(m.Payment).where(m.Payment.order_id == oid))
                payment.status = "paid"
                payment.paid_at = m.now()
                m.create_delivery_task(db, order)
                with patch.object(db, "commit", side_effect=AssertionError("hook must not commit")):
                    handled = self.hooks["after_payment"](db, order, db.get(m.User, "buyer"))
                self.assertEqual(handled, method in self.module.OFFLINE)
                db.commit()
        return oid

    def locked(self, db, oid):
        return db.scalar(self.select(self.m.Order).where(self.m.Order.id == oid)
                         .with_for_update().execution_options(populate_existing=True))

    def task(self, db, oid):
        return db.scalar(self.select(self.m.DeliveryTask).where(self.m.DeliveryTask.order_id == oid))

    def transition(self, oid, action, actor="provider", reason="", expected=None):
        with self.m.SessionLocal() as db:
            order = self.locked(db, oid)
            body = SimpleNamespace(action=action, reason=reason)
            if expected:
                with self.assertRaises(self.m.HTTPException) as raised:
                    self.hooks["handle_transition"](db, db.get(self.m.User, actor), order, body)
                self.assertEqual(raised.exception.status_code, expected)
                db.rollback()
                return
            with patch.object(db, "commit", side_effect=AssertionError("hook must not commit")):
                handled = self.hooks["handle_transition"](db, db.get(self.m.User, actor), order, body)
            self.assertIs(handled, True)
            db.commit()
            return self.m.order_out(order)

    def assert_state(self, oid, main, delivery=None):
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            task = self.task(db, oid)
            self.assertEqual(order.main_status, main)
            self.assertEqual(order.delivery_status, delivery or main)
            self.assertEqual(task.status, "completed" if main == "completed" else main)
            self.assertIsNone(task.next_retry_at)

    def counts(self):
        with self.m.SessionLocal() as db:
            return tuple(db.scalar(self.select(self.func.count()).select_from(model)) for model in (
                self.m.OrderStateLog, self.m.DeliveryTask, self.m.AuditLog, self.Message,
                self.Receipt, self.Outbox, self.m.SettlementMeasurement, self.m.DeliveryAttachment))

    def pending(self, oid):
        self.transition(oid, "start_delivery")
        self.transition(oid, "submit_delivery")

    def test_all_offline_methods_initialize_single_manual_task_and_paid_repeat(self):
        for method in sorted(self.module.OFFLINE):
            with self.subTest(method=method):
                oid = self.create(method)
                self.assert_state(oid, "awaiting_start")
                before = self.counts()
                with self.m.SessionLocal() as db:
                    order = self.locked(db, oid)
                    task = self.task(db, oid)
                    self.assertEqual((task.method, task.delivery_mode, task.assignee),
                                     (method, "manual", "provider_tenant"))
                    self.assertTrue(self.hooks["after_payment"](db, order, db.get(self.m.User, "buyer")))
                    db.commit()
                self.assertEqual(before, self.counts())

    def test_full_lifecycle_accept_completes_without_confirm_order(self):
        oid = self.create()
        self.transition(oid, "start_delivery")
        self.assert_state(oid, "in_delivery")
        self.transition(oid, "submit_delivery", reason="Delivered")
        self.assert_state(oid, "pending_acceptance")
        result = self.transition(oid, "accept_delivery", "buyer_admin")
        self.assertEqual(result["main_status"], "completed")
        self.assert_state(oid, "completed", "accepted")
        before = self.counts()
        self.transition(oid, "accept_delivery", "buyer", expected=409)
        self.transition(oid, "confirm_order", "buyer", expected=409)
        self.assertEqual(before, self.counts())

    def test_rejection_resubmission_preserves_task_and_all_evidence(self):
        oid = self.create()
        with self.m.SessionLocal() as db:
            task = self.task(db, oid)
            tid = task.id
            db.add(self.m.DeliveryAttachment(id="evidence", task_id=tid,
                   file_id="product_data", description="Original evidence", uploaded_by="provider"))
            db.commit()
        self.pending(oid)
        for cycle in range(2):
            self.transition(oid, "reject_delivery", "buyer_admin", "  Revise scope  ")
            self.assert_state(oid, "rectifying")
            with self.m.SessionLocal() as db:
                task = self.task(db, oid)
                self.assertEqual(task.last_error, "Revise scope")
                self.assertEqual(task.id, tid)
                self.assertEqual(db.get(self.m.DeliveryAttachment, "evidence").description, "Original evidence")
            self.transition(oid, "submit_delivery", reason=f"Revision {cycle}")
            with self.m.SessionLocal() as db:
                self.assertEqual(self.task(db, oid).last_error, "")
        self.transition(oid, "accept_delivery", "buyer")
        with self.m.SessionLocal() as db:
            self.assertEqual(self.task(db, oid).id, tid)
            self.assertIsNotNone(db.get(self.m.DeliveryAttachment, "evidence"))

    def test_audits_state_logs_metering_and_party_notifications(self):
        oid = self.create()
        self.pending(oid)
        self.transition(oid, "reject_delivery", "buyer", "PRIVATE reason")
        self.transition(oid, "submit_delivery")
        self.transition(oid, "accept_delivery", "buyer")
        with self.m.SessionLocal() as db:
            logs = db.scalars(self.select(self.m.OrderStateLog).where(self.m.OrderStateLog.order_id == oid)).all()
            for action in self.module.PARTY_ACTIONS:
                self.assertEqual({log.domain for log in logs if log.action == action}, {"main", "delivery"})
            audit = db.scalar(self.select(self.m.AuditLog).where(
                self.m.AuditLog.order_id == oid, self.m.AuditLog.action == "reject_delivery"))
            self.assertEqual(audit.tenant_id, "buyer_tenant")
            self.assertEqual(json.loads(audit.before_json)["main_status"], "pending_acceptance")
            self.assertEqual(json.loads(audit.after_json)["reason"], "PRIVATE reason")
            messages = db.scalars(self.select(self.Message).where(self.Message.target_id == oid)).all()
            self.assertEqual(len(messages), 5)
            self.assertEqual(len({message.event_key for message in messages}), 5)
            self.assertFalse(any("PRIVATE" in message.body for message in messages))
            measurements = db.scalars(self.select(self.m.SettlementMeasurement).where(
                self.m.SettlementMeasurement.order_id == oid)).all()
            self.assertEqual(len(measurements), 4)
            self.assertEqual({item.measurement_type for item in measurements}, {"offline_delivery"})
        self.assertEqual(self.recipients(target_id=oid), {"buyer", "buyer_admin", "provider"})

    def test_party_roles_wrong_tenants_inactive_accounts_and_platform_denied(self):
        oid = self.create()
        before = self.counts()
        for actor in ("buyer", "buyer_admin", "outsider", "inactive", "platform", "ops", "finance", "disabled_ops"):
            self.transition(oid, "start_delivery", actor, expected=403)
            self.transition(oid, "submit_delivery", actor, expected=403)
        self.assertEqual(before, self.counts())
        self.pending(oid)
        before = self.counts()
        for actor in ("provider", "outsider", "inactive", "platform", "ops", "finance", "disabled_ops"):
            for action in ("accept_delivery", "reject_delivery"):
                self.transition(oid, action, actor, reason="Reason", expected=403)
        self.assertEqual(before, self.counts())

    def test_membership_changes_and_activation_rechecked_for_every_action(self):
        oid = self.create()
        for actor, action in (("provider", "start_delivery"), ("buyer", "accept_delivery")):
            if actor == "buyer":
                self.pending(oid)
            for model, field, value in (("membership", "status", "disabled"),
                ("membership", "role", "member"), ("membership", "enterprise_id", "other_tenant"),
                ("user", "activation_status", "pending_activation"), ("user", "is_active", False),
                ("user", "platform_role", "super_admin")):
                with self.subTest(actor=actor, field=field):
                    with self.m.SessionLocal() as db:
                        obj = db.get(self.m.User, actor) if model == "user" else db.scalar(
                            self.select(self.m.Membership).where(self.m.Membership.user_id == actor))
                        old = getattr(obj, field)
                        setattr(obj, field, value)
                        db.commit()
                    before = self.counts()
                    self.transition(oid, action, actor, expected=403)
                    self.assertEqual(before, self.counts())
                    with self.m.SessionLocal() as db:
                        obj = db.get(self.m.User, actor) if model == "user" else db.scalar(
                            self.select(self.m.Membership).where(self.m.Membership.user_id == actor))
                        setattr(obj, field, old)
                        db.commit()

    def test_provider_enterprise_admin_can_start_and_submit(self):
        oid = self.create()
        with self.m.SessionLocal() as db:
            db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "provider")).role = "enterprise_admin"
            db.commit()
        self.pending(oid)
        self.transition(oid, "reject_delivery", "buyer_admin", "Revise")
        self.transition(oid, "submit_delivery")

    def test_out_of_order_duplicate_and_terminal_actions_emit_nothing(self):
        oid = self.create()
        for phase, actions in (("awaiting_start", ("submit_delivery", "accept_delivery", "reject_delivery")),
                               ("in_delivery", ("start_delivery", "accept_delivery", "reject_delivery")),
                               ("pending_acceptance", ("start_delivery", "submit_delivery")),
                               ("rectifying", ("start_delivery", "accept_delivery", "reject_delivery")),
                               ("completed", tuple(self.module.PARTY_ACTIONS))):
            before = self.counts()
            for action in actions:
                self.transition(oid, action, "buyer" if action in {"accept_delivery", "reject_delivery"} else "provider",
                                reason="Reason", expected=409)
            self.assertEqual(before, self.counts(), phase)
            if phase == "awaiting_start":
                self.transition(oid, "start_delivery")
            elif phase in {"in_delivery", "rectifying"}:
                self.transition(oid, "submit_delivery")
                if phase == "rectifying":
                    self.transition(oid, "accept_delivery", "buyer")
            elif phase == "pending_acceptance":
                self.transition(oid, "reject_delivery", "buyer", "Reason")

    def test_reject_requires_nonblank_reason(self):
        oid = self.create()
        self.pending(oid)
        before = self.counts()
        for reason in ("", "   ", "\t\n", None):
            self.transition(oid, "reject_delivery", "buyer", reason, expected=400)
        self.assertEqual(before, self.counts())
        self.assert_state(oid, "pending_acceptance")

    def test_unpaid_refunding_refunded_and_paid_amount_mismatch_block_all_actions(self):
        oid = self.create()
        for field, value in (("payment_status", "unpaid"), ("payment_status", "refunding"),
                             ("payment_status", "refunded"), ("paid_amount", 99), ("refunded_amount", 1)):
            with self.m.SessionLocal() as db:
                order = db.get(self.m.Order, oid)
                old = getattr(order, field)
                setattr(order, field, value)
                db.commit()
            before = self.counts()
            self.transition(oid, "start_delivery", expected=409)
            self.assertEqual(before, self.counts())
            with self.m.SessionLocal() as db:
                setattr(db.get(self.m.Order, oid), field, old)
                db.commit()

    def test_missing_duplicate_mismatched_and_desynchronized_tasks_fail_closed(self):
        oid = self.create()
        for field, value in (("method", "file"), ("delivery_mode", "automatic"), ("status", "preparing")):
            with self.m.SessionLocal() as db:
                task = self.task(db, oid)
                old = getattr(task, field)
                setattr(task, field, value)
                db.commit()
            before = self.counts()
            self.transition(oid, "start_delivery", expected=409)
            self.assertEqual(before, self.counts())
            with self.m.SessionLocal() as db:
                setattr(self.task(db, oid), field, old)
                db.commit()
        with self.m.SessionLocal() as db:
            db.add(self.m.DeliveryTask(id="duplicate", order_id=oid, method="consulting", status="awaiting_start"))
            db.commit()
        self.transition(oid, "start_delivery", expected=409)
        with self.m.SessionLocal() as db:
            db.delete(db.get(self.m.DeliveryTask, "duplicate"))
            db.delete(self.task(db, oid))
            db.commit()
        self.transition(oid, "start_delivery", expected=409)

    def test_corrupt_snapshots_and_order_state_mismatch_block_mutation(self):
        oid = self.create()
        for field, value in (("economic_snapshot_json", "{}"), ("delivery_snapshot_json", "{}"),
                             ("delivery_status", "preparing"), ("main_status", "cancelled")):
            with self.m.SessionLocal() as db:
                order = db.get(self.m.Order, oid)
                old = getattr(order, field)
                setattr(order, field, value)
                db.commit()
            before = self.counts()
            self.transition(oid, "start_delivery", expected=409)
            self.assertEqual(before, self.counts())
            with self.m.SessionLocal() as db:
                setattr(db.get(self.m.Order, oid), field, old)
                db.commit()

    def test_frozen_snapshot_survives_catalog_edits(self):
        oid = self.create()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            snapshots = order.economic_snapshot_json, order.delivery_snapshot_json
            db.get(self.m.Product, "product").delivery_method = "file"
            db.get(self.m.ProductReleaseVersion, "version").price = 999
            db.commit()
        self.pending(oid)
        self.transition(oid, "accept_delivery", "buyer")
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            self.assertEqual((order.economic_snapshot_json, order.delivery_snapshot_json), snapshots)
            self.assertEqual(self.task(db, oid).method, "consulting")

    def test_legacy_actions_and_task_result_paths_cannot_bypass_lifecycle(self):
        oid = self.create()
        before = self.counts()
        for action in self.module.LEGACY_ACTIONS:
            actor = "buyer" if action == "confirm_order" else "provider"
            self.transition(oid, action, actor, expected=409)
        with self.m.SessionLocal() as db:
            order = self.locked(db, oid)
            task = self.task(db, oid)
            for actor in ("provider", "buyer", "platform", "ops"):
                with self.assertRaises(self.m.HTTPException) as raised:
                    self.hooks["require_task_result"](db, db.get(self.m.User, actor), order, task)
                self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(before, self.counts())

    def test_attachment_upload_provider_only_and_freezes_during_acceptance(self):
        oid = self.create()
        for phase in ("awaiting_start", "in_delivery", "pending_acceptance", "rectifying", "completed"):
            with self.m.SessionLocal() as db:
                order = self.locked(db, oid)
                task = self.task(db, oid)
                for actor in ("buyer", "buyer_admin", "outsider", "platform", "ops", "inactive"):
                    with self.assertRaises(self.m.HTTPException) as raised:
                        self.hooks["require_attachment_upload"](db, db.get(self.m.User, actor), order, task)
                    self.assertEqual(raised.exception.status_code, 403)
                if phase in {"pending_acceptance", "completed"}:
                    with self.assertRaises(self.m.HTTPException) as raised:
                        self.hooks["require_attachment_upload"](db, db.get(self.m.User, "provider"), order, task)
                    self.assertEqual(raised.exception.status_code, 409)
                else:
                    self.hooks["require_attachment_upload"](db, db.get(self.m.User, "provider"), order, task)
            if phase == "awaiting_start":
                self.transition(oid, "start_delivery")
            elif phase == "in_delivery":
                self.transition(oid, "submit_delivery")
            elif phase == "pending_acceptance":
                self.transition(oid, "reject_delivery", "buyer", "Revise")
            elif phase == "rectifying":
                self.transition(oid, "submit_delivery")
                self.transition(oid, "accept_delivery", "buyer")

    def test_online_and_legacy_orders_delegate_without_side_effects(self):
        for method, version in (("file", 1), ("api", 1), ("consulting", 0)):
            oid = self.create(method, paid=False)
            with self.m.SessionLocal() as db:
                order = self.locked(db, oid)
                order.snapshot_version = version
                db.commit()
            before = self.counts()
            with self.m.SessionLocal() as db:
                order = self.locked(db, oid)
                user = db.get(self.m.User, "platform")
                for action in self.module.PARTY_ACTIONS | self.module.LEGACY_ACTIONS | {"cancel_order"}:
                    self.assertIs(self.hooks["handle_transition"](db, user, order,
                        SimpleNamespace(action=action, reason="")), False)
                self.assertIs(self.hooks["after_payment"](db, order, user), False)
                self.hooks["require_task_result"](db, user, order, None)
                self.hooks["require_attachment_upload"](db, user, order, None)
                db.commit()
            self.assertEqual(before, self.counts())

    def test_unrelated_actions_delegate_for_offline_orders(self):
        oid = self.create()
        before = self.counts()
        with self.m.SessionLocal() as db:
            order = self.locked(db, oid)
            for action in ("confirm_payment", "approve_refund", "submit_after_sales", "close_order", "unknown"):
                self.assertIs(self.hooks["handle_transition"](db, db.get(self.m.User, "buyer"), order,
                    SimpleNamespace(action=action, reason="")), False)
        self.assertEqual(before, self.counts())

    def test_after_payment_never_rewinds_active_fulfillment(self):
        oid = self.create()
        self.transition(oid, "start_delivery")
        before = self.counts()
        with self.m.SessionLocal() as db:
            with self.assertRaises(self.m.HTTPException) as raised:
                self.hooks["after_payment"](db, self.locked(db, oid), db.get(self.m.User, "buyer"))
            self.assertEqual(raised.exception.status_code, 409)
            db.rollback()
        self.assertEqual(before, self.counts())
        self.assert_state(oid, "in_delivery")

    def test_notification_failure_rolls_back_state_evidence_logs_audit_metering_and_outbox(self):
        oid = self.create()
        self.transition(oid, "start_delivery")
        before = self.counts()
        with self.fail_after_create("order"):
            with self.assertRaisesRegex(RuntimeError, "Injected failure"):
                self.transition(oid, "submit_delivery")
        self.assertEqual(before, self.counts())
        self.assert_state(oid, "in_delivery")

    def test_successful_transition_can_be_rolled_back_by_caller(self):
        oid = self.create()
        before = self.counts()
        with self.m.SessionLocal() as db:
            self.assertTrue(self.hooks["handle_transition"](db, db.get(self.m.User, "provider"),
                self.locked(db, oid), SimpleNamespace(action="start_delivery", reason="")))
            db.rollback()
        self.assertEqual(before, self.counts())
        self.assert_state(oid, "awaiting_start")


    def test_http_payment_retry_at_every_stage_never_rewinds_or_duplicates_tasks(self):
        oid = self.create(paid=False)
        self.request("POST", f"/api/orders/{oid}/provider-review", "provider", json={"decision": "approve"})
        first = self.request("POST", f"/api/orders/{oid}/transition", json={"action": "confirm_payment"})
        self.assertEqual(first["main_status"], "awaiting_start")
        for action, actor, reason, phase in ((None, None, "", "awaiting_start"),
                ("start_delivery", "provider", "", "in_delivery"),
                ("submit_delivery", "provider", "", "pending_acceptance"),
                ("reject_delivery", "buyer_admin", "Revise", "rectifying"),
                ("submit_delivery", "provider", "", "pending_acceptance"),
                ("accept_delivery", "buyer", "", "completed")):
            if action:
                self.request("POST", f"/api/orders/{oid}/transition", actor,
                    json={"action": action, "reason": reason})
            before = self.counts()
            with self.m.SessionLocal() as db:
                order = db.get(self.m.Order, oid)
                task = self.task(db, oid)
                original = (order.updated_at, task.id, task.status, task.note, task.last_error)
            for retry_actor in ("buyer", "finance", "platform"):
                result = self.request("POST", f"/api/orders/{oid}/transition", retry_actor,
                    json={"action": "confirm_payment"})
                self.assertEqual(result["main_status"], phase)
                self.assertEqual(before, self.counts())
            with self.m.SessionLocal() as db:
                order = db.get(self.m.Order, oid)
                task = self.task(db, oid)
                self.assertEqual((order.updated_at, task.id, task.status, task.note, task.last_error), original)
            self.assert_state(oid, phase, "accepted" if phase == "completed" else phase)

    def test_http_legacy_process_and_transition_bypass_emit_nothing(self):
        oid = self.create()
        with self.m.SessionLocal() as db:
            tid = self.task(db, oid).id
        before = self.counts()
        for success in (True, False):
            self.request("POST", f"/api/delivery-tasks/{tid}/process", "ops", expected=409,
                         json={"success": success, "error": "Legacy bypass"})
        for action, actor in (("create_task", "provider"), ("retry_delivery", "provider"),
                              ("mark_exception", "provider"), ("confirm_order", "buyer")):
            self.request("POST", f"/api/orders/{oid}/transition", actor, expected=409, json={"action": action})
        self.assertEqual(before, self.counts())
        self.assert_state(oid, "awaiting_start")

    def test_http_payment_notification_failure_rolls_back_initialization(self):
        oid = self.create(paid=False)
        self.request("POST", f"/api/orders/{oid}/provider-review", "provider", json={"decision": "approve"})
        before = self.counts()
        with self.fail_after_create("order"):
            self.request("POST", f"/api/orders/{oid}/transition", expected=500,
                         json={"action": "confirm_payment"})
        self.assertEqual(before, self.counts())
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            self.assertEqual((order.main_status, order.delivery_status, order.payment_status),
                             ("pending_payment", "not_started", "unpaid"))
            self.assertEqual(order.paid_amount, 0)
            self.assertIsNone(self.task(db, oid))
            payment = db.scalar(self.select(self.m.Payment).where(self.m.Payment.order_id == oid))
            self.assertEqual(payment.status, "unpaid")
            self.assertIsNone(payment.paid_at)

    def test_http_each_fulfillment_notification_failure_rolls_back_all_side_effects(self):
        oid = self.create()
        tid = self.seed_attachment(oid)
        for action, actor, reason, phase in (("start_delivery", "provider", "", "awaiting_start"),
                ("submit_delivery", "provider", "", "in_delivery"),
                ("reject_delivery", "buyer", "PRIVATE reason", "pending_acceptance"),
                ("submit_delivery", "provider", "Revision", "rectifying"),
                ("accept_delivery", "buyer", "", "pending_acceptance")):
            before = self.counts()
            with self.m.SessionLocal() as db:
                task = self.task(db, oid)
                previous = (task.note, task.last_error)
            with self.fail_after_create("order"):
                self.request("POST", f"/api/orders/{oid}/transition", actor, expected=500,
                    json={"action": action, "reason": reason})
            self.assertEqual(before, self.counts())
            self.assert_state(oid, phase)
            with self.m.SessionLocal() as db:
                task = self.task(db, oid)
                self.assertEqual((task.note, task.last_error), previous)
                self.assertEqual(task.id, tid)
                self.assertIsNotNone(db.get(self.m.DeliveryAttachment, "delivery_evidence"))
            self.request("POST", f"/api/orders/{oid}/transition", actor,
                json={"action": action, "reason": reason})

    def test_http_upload_requires_provider_and_clean_scan_preserves_evidence(self):
        oid = self.create()
        with self.m.SessionLocal() as db:
            tid = self.task(db, oid).id
        storage = Mock()
        storage.bucket_exists.return_value = True
        path = f"/api/delivery-tasks/{tid}/attachments"
        with patch.object(self.m, "MINIO_ENDPOINT", "storage.invalid"), \
                patch.object(self.m, "Minio", return_value=storage), \
                patch.object(self.m, "clamav_scan_stream", return_value=("clean", "clean")) as scan:
            before = self.counts()
            for actor in ("buyer", "buyer_admin", "platform", "ops", "outsider"):
                self.request("POST", path, actor, expected=403,
                             files={"upload": ("evidence.txt", b"Evidence", "text/plain")})
            self.assertEqual(before, self.counts())
            scan.assert_not_called()
            storage.put_object.assert_not_called()
            for status, code in (("infected", 400), ("pending", 503), ("unavailable", 503), ("error", 503)):
                scan.return_value = status, "Scan result"
                self.request("POST", path, "provider", expected=code,
                             files={"upload": ("evidence.txt", b"Evidence", "text/plain")})
                self.assertEqual(before, self.counts())
                storage.put_object.assert_not_called()
            scan.return_value = "clean", "clean"
            evidence = self.request("POST", path, "provider",
                files={"upload": ("evidence.txt", b"Evidence", "text/plain")})
            storage.put_object.assert_called_once()
            self.pending(oid)
            self.request("POST", path, "provider", expected=409,
                         files={"upload": ("evidence.txt", b"Changed", "text/plain")})
            self.transition(oid, "reject_delivery", "buyer", "Revise")
            second = self.request("POST", path, "provider",
                files={"upload": ("revision.txt", b"Revised evidence", "text/plain")})
            self.assertNotEqual(evidence["id"], second["id"])
            self.transition(oid, "submit_delivery")
            self.transition(oid, "accept_delivery", "buyer")
            listed = self.request("GET", path)["items"]
            self.assertEqual({item["id"] for item in listed}, {evidence["id"], second["id"]})
            self.request("POST", path, "provider", expected=409,
                         files={"upload": ("late.txt", b"Late", "text/plain")})
            self.assertEqual(storage.put_object.call_count, 2)

    def seed_attachment(self, oid, owner="provider"):
        with self.m.SessionLocal() as db:
            tid = self.task(db, oid).id
            db.add(self.m.FileObject(id="delivery_file", owner_id=owner, object_name="evidence",
                original_name="evidence.txt", content_type="text/plain", file_role="delivery_attachment",
                status="active", scan_status="clean"))
            db.flush()
            db.add(self.m.DeliveryAttachment(id="delivery_evidence", task_id=tid,
                file_id="delivery_file", uploaded_by=owner, description="Evidence"))
            db.commit()
        return tid

    def test_http_task_listing_isolates_parties_and_membership_changes(self):
        oid = self.create()
        with self.m.SessionLocal() as db:
            db.add(self.m.Order(id="other_order", order_no="OTHER-ORDER",
                buyer_enterprise_id="other_tenant", provider_enterprise_id="other_tenant",
                buyer_user_id="outsider", product_id="product", buyer_name="Other", product_name="Test"))
            db.flush()
            db.add(self.m.DeliveryTask(id="other_task", order_id="other_order", method="consulting"))
            db.commit()
        for actor in ("buyer", "buyer_admin", "provider"):
            items = self.request("GET", "/api/delivery-tasks", actor)["items"]
            self.assertEqual({item["order_id"] for item in items}, {oid})
        self.assertEqual({item["id"] for item in self.request("GET", "/api/delivery-tasks", "outsider")["items"]}, {"other_task"})
        for actor in ("platform", "ops"):
            self.assertEqual(len(self.request("GET", "/api/delivery-tasks", actor)["items"]), 2)
        for actor in ("finance", "reviewer", "quality"):
            self.assertEqual(self.request("GET", "/api/delivery-tasks", actor)["items"], [])
        for field, value in (("role", "member"), ("status", "disabled")):
            with self.m.SessionLocal() as db:
                member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "provider"))
                old = getattr(member, field)
                setattr(member, field, value)
                db.commit()
            self.assertEqual(self.request("GET", "/api/delivery-tasks", "provider")["items"], [])
            with self.m.SessionLocal() as db:
                member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "provider"))
                setattr(member, field, old)
                db.commit()

    def test_http_attachment_list_download_party_isolation_and_no_owner_reviewer_bypass(self):
        oid = self.create()
        tid = self.seed_attachment(oid)
        storage = Mock()
        storage.get_object.return_value.read.return_value = b"Evidence"
        with patch.object(self.m, "MINIO_ENDPOINT", "storage.invalid"), patch.object(self.m, "Minio", return_value=storage):
            for actor in ("buyer", "buyer_admin", "provider", "platform", "ops"):
                items = self.request("GET", f"/api/delivery-tasks/{tid}/attachments", actor)["items"]
                self.assertEqual([item["file_id"] for item in items], ["delivery_file"])
                response = self.client.get("/api/files/delivery_file/download",
                    headers={"Authorization": "Bearer " + self.tokens[actor]})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.content, b"Evidence")
            storage.get_object.reset_mock()
            for actor in ("outsider", "finance", "reviewer", "quality"):
                self.request("GET", f"/api/delivery-tasks/{tid}/attachments", actor, expected=403)
                self.request("GET", "/api/files/delivery_file/download", actor, expected=403)
            for field, value in (("role", "member"), ("status", "disabled")):
                with self.m.SessionLocal() as db:
                    member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "provider"))
                    old = getattr(member, field)
                    setattr(member, field, value)
                    db.commit()
                self.request("GET", f"/api/delivery-tasks/{tid}/attachments", "provider", expected=403)
                self.request("GET", "/api/files/delivery_file/download", "provider", expected=403)
                with self.m.SessionLocal() as db:
                    member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "provider"))
                    setattr(member, field, old)
                    db.commit()
            storage.get_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
