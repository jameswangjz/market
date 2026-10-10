"""Business notification unit mocks and isolated production HTTP route tests.

Run with: python -m unittest discover -s backend/tests -p test_message_business_events.py -v
The two mock suites use only the standard library and never import app.main.
"""
import ast
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
from datetime import datetime, timedelta, timezone


MAIN_PATH = Path(__file__).resolve().parents[1] / "app" / "main.py"


class QueryField:
    def __getattr__(self, name):
        return self

    def __call__(self, *args, **kwargs):
        return self

    def __eq__(self, other):
        return self

    def __le__(self, other):
        return self


def isolated_helpers(names):
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in functions} != set(names):
        raise AssertionError("Requested production helpers are missing")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    namespace = {
        "json": json, "hashlib": hashlib, "timezone": timezone, "timedelta": timedelta,
        "now": lambda: datetime(2026, 10, 9, 12, tzinfo=timezone.utc),
        "select": MagicMock(), "or_": Mock(),
        "AuditLog": lambda **values: SimpleNamespace(**values),
        "audit_request_id": SimpleNamespace(get=lambda: "isolated-request"),
    }
    for name in ("User", "Membership", "Order", "Product", "SaaSSubscription", "ApiCredential", "ApiGatewayRoute", "Settlement", "SettlementLine", "SettlementBatch"):
        namespace[name] = QueryField()
    exec(compile(ast.fix_missing_locations(module), "<isolated production helpers>", "exec"), namespace)
    return namespace


class BusinessHelperMockTests(unittest.TestCase):
    def setUp(self):
        self.before_modules = set(sys.modules)
        self.ns = isolated_helpers({"audit", "notify_business_event", "notify_order_event", "notify_settlement_event", "notify_platform_role"})
        self.db = Mock()
        self.order = SimpleNamespace(id="o", buyer_user_id="owner", buyer_enterprise_id="a", provider_enterprise_id="b", order_no="ORD", subscription_id="")
        self.db.get.return_value = self.order
        self.real_notify = self.ns["notify_business_event"]
        self.notify = Mock()
        self.ns["notify_business_event"] = self.notify

    def tearDown(self):
        self.assertFalse(any(name.endswith(".main") and name not in self.before_modules for name in sys.modules))

    def test_unrelated_audit_does_not_flush_or_notify(self):
        self.ns["audit"](self.db, "actor", "notification_read", "notification", "x", before={"secret": "PRIVATE"})
        self.db.add.assert_called_once()
        self.db.flush.assert_not_called()
        self.db.get.assert_not_called()
        self.notify.assert_not_called()
        self.db.commit.assert_not_called()

    def test_settlement_audit_does_not_recurse_on_notification_audit(self):
        self.notify.side_effect = lambda *args, **kwargs: self.ns["audit"](self.db, "system", "notification_send", "notification", "message")
        self.ns["audit"](self.db, "actor", "generate_settlement", "settlement", "SET", order_id="o", before={"secret": "PRIVATE"})
        self.db.flush.assert_called_once()
        self.assertEqual(self.notify.call_count, 3)
        self.assertEqual(self.db.add.call_count, 4)
        for call in self.notify.call_args_list:
            self.assertNotIn("PRIVATE", call.args[2])
            self.assertIn("SET", call.kwargs["event_key"])
        self.db.commit.assert_not_called()

    def test_flush_error_propagates_without_partial_commit(self):
        self.db.flush.side_effect = RuntimeError("flush failed")
        with self.assertRaisesRegex(RuntimeError, "flush failed"):
            self.ns["audit"](self.db, "actor", "generate_settlement", "settlement", "SET", order_id="o")
        self.notify.assert_not_called()
        self.db.commit.assert_not_called()

    def test_platform_stage_version_is_stable_and_new_cycle_differs(self):
        product = SimpleNamespace(id="p", enterprise_id="a", status="pending_review", updated_at=self.ns["now"]())
        self.db.get.return_value = product
        for _ in range(2):
            self.ns["notify_platform_role"](self.db, "business_reviewer", "title", "body", "product", "p")
        first, second = (call.kwargs["event_key"] for call in self.notify.call_args_list)
        self.assertEqual(first, second)
        product.updated_at += timedelta(seconds=1)
        self.ns["notify_platform_role"](self.db, "business_reviewer", "title", "body", "product", "p")
        self.assertNotEqual(first, self.notify.call_args.kwargs["event_key"])
        self.assertEqual(self.notify.call_args.kwargs["platform_roles"], ["business_reviewer", "super_admin", "product_manager"])
        self.db.commit.assert_not_called()

    def test_explicit_platform_key_requires_no_flush(self):
        self.db.get.return_value = SimpleNamespace(id="p", enterprise_id="a")
        self.ns["notify_platform_role"](self.db, "platform_operator", "title", "body", "product", "p", event_key="explicit:p:version")
        self.db.flush.assert_not_called()
        self.assertEqual(self.notify.call_args.kwargs["event_key"], "explicit:p:version")

    def test_order_scope_and_non_event_are_explicit(self):
        self.ns["notify_order_event"](self.db, self.order, "confirm_payment", "paid")
        self.assertEqual(self.notify.call_args.kwargs["recipient_ids"], ["owner"])
        self.assertEqual(self.notify.call_args.kwargs["enterprise_ids"], ["a", "b"])
        self.notify.reset_mock()
        self.ns["notify_order_event"](self.db, self.order, "download_file", "time")
        self.notify.assert_not_called()

    def test_create_receives_same_session_without_commit(self):
        self.db.scalars.return_value.all.return_value = ["owner"]
        create = Mock()
        self.ns["message_center"] = {"create": create}
        self.real_notify(self.db, "title", "body", "order", "o", event_key="order:o:paid", category="order", tenant_id="a", recipient_ids=["owner"])
        self.assertIs(create.call_args.args[0], self.db)
        self.assertEqual(create.call_args.kwargs, dict(severity="important", event_key="order:o:paid", category="order", tenant_id="a"))
        self.db.commit.assert_not_called()
        self.db.flush.assert_not_called()

    def test_empty_recipients_and_long_key(self):
        create = Mock()
        self.ns["message_center"] = {"create": create}
        self.real_notify(self.db, "title", "body", "order", "o", event_key="empty", category="order")
        create.assert_not_called()
        self.db.scalars.return_value.all.return_value = ["owner"]
        self.real_notify(self.db, "title", "body", "order", "o", event_key="x" * 200, category="order", recipient_ids=["owner"])
        self.assertEqual(create.call_args.kwargs["event_key"], "business:" + hashlib.sha256(("x" * 200).encode()).hexdigest())


class ScheduledHookMockTests(unittest.TestCase):
    def setUp(self):
        self.before_modules = set(sys.modules)
        self.ns = isolated_helpers({"notification_scheduled_events"})
        self.clock = self.ns["now"]()
        self.db, self.notify, self.client = Mock(), Mock(), Mock()
        self.ns["notify_business_event"] = self.notify
        self.redis_error = type("RedisFailure", (Exception,), {})
        self.ns["redis"] = SimpleNamespace(RedisError=self.redis_error, Redis=Mock())
        self.ns["redis"].Redis.from_url.return_value = self.client
        self.ns["os"] = SimpleNamespace(getenv=lambda key, default: default)
        self.client.hgetall.return_value = {}
        self.client.mget.return_value = ["0", "0", "0"]
        self.db.scalar.return_value = "owner"
        self.db.get.return_value = SimpleNamespace(route_key="route-key", status="active", daily_quota=5, monthly_quota=15)
        self.credential = SimpleNamespace(id="cred", apisix_consumer_name="consumer", expires_at=None, route_id="route", enterprise_id="a", created_by="owner", daily_quota=3, monthly_quota=10, total_quota=20, status="active")
        self.subscription = SimpleNamespace(id="sub", status="active", enterprise_id="a", created_by="owner", expires_at=self.clock)

    def tearDown(self):
        self.assertFalse(any(name.endswith(".main") and name not in self.before_modules for name in sys.modules))

    def run_hook(self, subscriptions=(), credentials=()):
        self.db.scalars.side_effect = [SimpleNamespace(all=lambda: list(subscriptions)), SimpleNamespace(all=lambda: list(credentials))]
        return self.ns["notification_scheduled_events"](self.db)

    def test_expired_and_seven_day_boundary_without_mutation(self):
        future = SimpleNamespace(**vars(self.subscription))
        future.id, future.expires_at = "future", self.clock + timedelta(days=7)
        self.run_hook([self.subscription, future])
        self.assertEqual(self.notify.call_count, 2)
        self.assertIn(":expired:", self.notify.call_args_list[0].kwargs["event_key"])
        self.assertIn(":expiring:", self.notify.call_args_list[1].kwargs["event_key"])
        self.assertEqual(self.subscription.status, "active")
        self.db.commit.assert_not_called()
        self.db.flush.assert_not_called()

    def test_lua_exact_keys_equal_limits_and_metadata_override(self):
        self.client.hgetall.return_value = {"daily_quota": "2", "monthly_quota": "0"}
        self.client.mget.return_value = ["2", "999", "20"]
        self.assertEqual(self.run_hook(credentials=[self.credential])["quota_candidates"], 2)
        self.client.mget.assert_called_once_with(["market:apisix:quota:day:route-key:consumer:2026-10-09", "market:apisix:quota:month:route-key:consumer:2026-10", "market:apisix:quota:total:route-key:consumer"])
        self.assertEqual(self.credential.status, "active")

    def test_redis_failure_preserves_saas(self):
        self.client.hgetall.side_effect = self.redis_error("offline")
        self.assertTrue(self.run_hook([self.subscription], [self.credential])["redis_unavailable"])
        self.notify.assert_called_once()
        self.db.rollback.assert_not_called()
        self.client.close.assert_called_once()

    def test_invalid_redis_url_preserves_saas(self):
        self.ns["redis"].Redis.from_url.side_effect = ValueError("bad URL")
        self.assertTrue(self.run_hook([self.subscription], [self.credential])["redis_unavailable"])
        self.notify.assert_called_once()

    def test_missing_invalid_counters_and_revoked_metadata(self):
        self.client.mget.return_value = [None, "invalid", "0"]
        self.run_hook(credentials=[self.credential])
        self.notify.assert_not_called()
        self.client.hgetall.return_value = {"status": "revoked"}
        self.client.mget.return_value = ["99", "99", "99"]
        self.run_hook(credentials=[self.credential])
        self.notify.assert_not_called()

    def test_repeat_key_is_stable_and_new_limit_differs(self):
        self.client.mget.return_value = ["3", "0", "0"]
        self.run_hook(credentials=[self.credential])
        first = self.notify.call_args.kwargs["event_key"]
        self.run_hook(credentials=[self.credential])
        self.assertEqual(first, self.notify.call_args.kwargs["event_key"])
        self.client.hgetall.return_value = {"daily_quota": "2"}
        self.run_hook(credentials=[self.credential])
        self.assertNotEqual(first, self.notify.call_args.kwargs["event_key"])

    def test_database_create_error_is_not_swallowed(self):
        self.notify.side_effect = RuntimeError("flush failed")
        with self.assertRaisesRegex(RuntimeError, "flush failed"):
            self.run_hook([self.subscription])
        self.db.commit.assert_not_called()


def load_isolated_main():
    """Load production models/routes in a unique package, without startup/seed."""
    package_name = "message_business_test_" + secrets.token_hex(6)
    package = ModuleType(package_name)
    package.__path__ = [str(MAIN_PATH.parent)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(package_name + ".main", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///:memory:", "MESSAGE_CENTER_DISABLE_BACKGROUND": "true", "JWT_SECRET": "isolated-business-notification-test-secret"}):
        spec.loader.exec_module(module)
    return module


class BusinessRoutesHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_isolated_main()

    @classmethod
    def tearDownClass(cls):
        cls.m.engine.dispose()
        package = cls.m.__package__
        for name in list(sys.modules):
            if name == package or name.startswith(package + "."):
                del sys.modules[name]

    def setUp(self):
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine, event, func, select
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        self.select, self.func = select, func
        m = self.m
        m.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        @event.listens_for(m.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        m.SessionLocal = sessionmaker(bind=m.engine, autoflush=False)
        m.Base.metadata.create_all(m.engine)
        self.Message, self.Receipt, self.Outbox = (m.message_center[key] for key in ("Message", "Receipt", "Outbox"))
        with m.SessionLocal() as db:
            accounts = [("buyer", ""), ("buyer_admin", ""), ("provider", ""), ("outsider", ""), ("inactive", ""), ("platform", "super_admin"), ("ops", "platform_operator"), ("finance", "finance_settlement"), ("reviewer", "business_reviewer"), ("quality", "quality_reviewer"), ("disabled_ops", "platform_operator")]
            db.add_all([m.User(id=uid, name=uid, password_hash="unused", email=uid + "@example.invalid", phone="13800000000" if uid == "buyer" else None, platform_role=role, verified_status="verified", is_active=uid != "disabled_ops", activation_status="pending_activation" if uid == "inactive" else "active") for uid, role in accounts])
            db.add_all([m.Enterprise(id=eid, name=eid, credit_code=eid) for eid in ("buyer_tenant", "provider_tenant", "other_tenant")])
            db.flush()
            db.add_all([m.Membership(user_id=uid, enterprise_id=eid, role=role) for uid, eid, role in [("buyer", "buyer_tenant", "super_admin"), ("buyer_admin", "buyer_tenant", "enterprise_admin"), ("provider", "provider_tenant", "super_admin"), ("outsider", "other_tenant", "super_admin"), ("inactive", "buyer_tenant", "enterprise_admin"), ("platform", "buyer_tenant", "super_admin")]])
            product = m.Product(id="product", enterprise_id="provider_tenant", name="Test product", product_type="dataset", provider_name="Provider", provider_type="enterprise", status="published", delivery_method="file", logo_file_id="product_logo", catalog_name="Test", description="Test", usage_scenarios="Test", pricing_strategy="Test", authorization_conditions="Test", data_source_statement="Test", compliance_statement="Test")
            db.add(product)
            db.flush()
            db.add(m.ProductReleaseVersion(id="version", product_id="product", version_code="v1", description="v1", price=100, cost=10, status="active"))
            db.add(m.SettlementRule(id="trading_rule", rule_no="RULE-TRADING", name="Trading fixture", version="v1", status="active", platform_rate=8, provider_rate=67, service_rate=20, expert_rate=5, channel_rate=0))
            db.flush()
            db.add_all([
                m.FileObject(id="product_logo", owner_id="provider", product_id="product", object_name="logo", original_name="logo.png", content_type="image/png", file_role="product_logo", scan_status="clean"),
                m.FileObject(id="product_data", owner_id="provider", product_id="product", version_id="version", object_name="dataset", original_name="dataset.zip", file_role="product_data", scan_status="clean"),
            ])
            db.add_all([m.FileObject(id=fid, owner_id="buyer", object_name=fid, original_name=fid) for fid in ("front", "back", "license")])
            db.commit()
            self.tokens = {uid: m.issue_token(db.get(m.User, uid)) for uid, _ in accounts}
        self.client = TestClient(m.app, raise_server_exceptions=False)
        self.network = ExitStack()
        for name in ("request", "get", "post"):
            self.network.enter_context(patch.object(m.httpx, name, side_effect=AssertionError("Third-party HTTP is forbidden")))
        self.network.enter_context(patch.object(m.redis.Redis, "from_url", side_effect=AssertionError("Unmocked Redis is forbidden")))
        self.network.enter_context(patch.object(m, "Minio", side_effect=AssertionError("Unmocked object storage is forbidden")))

    def tearDown(self):
        self.network.close()
        self.client.close()
        self.m.engine.dispose()

    def request(self, method, path, actor="buyer", expected=200, **kwargs):
        response = self.client.request(method, path, headers={"Authorization": "Bearer " + self.tokens[actor]}, **kwargs)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json() if response.headers.get("content-type", "").startswith("application/json") else None

    def counts(self):
        with self.m.SessionLocal() as db:
            return tuple(db.scalar(self.select(self.func.count()).select_from(model)) for model in (self.Message, self.Receipt, self.Outbox))

    def recipients(self, **filters):
        with self.m.SessionLocal() as db:
            messages = db.scalars(self.select(self.Message).filter_by(**filters)).all()
            self.assertTrue(messages, filters)
            return set(db.scalars(self.select(self.Receipt.user_id).where(self.Receipt.message_id.in_([item.id for item in messages]))))

    def fail_after_create(self, category):
        create = self.m.message_center["create"]
        def injected(*args, **kwargs):
            message = create(*args, **kwargs)
            if kwargs.get("category") == category:
                args[0].flush()
                raise RuntimeError("Injected failure after message/receipt/outbox flush")
            return message
        return patch.dict(self.m.message_center, {"create": injected})

    def create_order(self):
        return self.request("POST", "/api/orders", json={"product_id": "product", "product_version_id": "version", "buyer_enterprise_id": "buyer_tenant"})["id"]

    def pay(self, order_id):
        return self.request("POST", f"/api/orders/{order_id}/transition", json={"action": "confirm_payment"})

    def task_id(self, order_id):
        with self.m.SessionLocal() as db:
            return db.scalar(self.select(self.m.DeliveryTask.id).where(self.m.DeliveryTask.order_id == order_id))

    def personal_body(self):
        return dict(id_name="PRIVATE NAME", id_number="PRIVATE ID", id_front_file_id="front", id_back_file_id="back", phone="13800000000", phone_code="123456")

    def test_personal_review_http_scope_resubmission_and_get_no_events(self):
        application = self.request("POST", "/api/verification/personal", json=self.personal_body())
        aid = application["id"]
        self.assertEqual(self.recipients(target_id=aid), {"platform", "ops"})
        self.request("POST", f"/api/admin/verifications/personal/{aid}/review", actor="platform", json={"decision": "reject", "comment": "PRIVATE REVIEW"})
        self.assertEqual(self.recipients(target_id=aid, title="个人实名认证审核结果"), {"buyer"})
        self.request("PUT", f"/api/verification/personal/{aid}", json=self.personal_body())
        self.request("POST", f"/api/admin/verifications/personal/{aid}/review", actor="ops", json={"decision": "approve"})
        before = self.counts()
        self.request("GET", "/api/verification/personal/me")
        self.assertEqual(self.counts(), before)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.User, "buyer").verified_status, "verified")
            self.assertFalse(any("PRIVATE" in message.body for message in db.scalars(self.select(self.Message))))

    def test_enterprise_review_http_notifies_actual_applicant_only(self):
        body = dict(license_file_id="license", enterprise_name="New tenant", credit_code="NEW", enterprise_type="Test", legal_representative="PRIVATE NAME")
        enterprise = self.request("POST", "/api/verification/enterprise", json=body)
        self.assertEqual(self.recipients(target_id=enterprise["id"]), {"platform", "ops"})
        self.request("POST", f"/api/admin/verifications/enterprise/{enterprise['id']}/review", actor="platform", json={"decision": "approve"})
        self.assertEqual(self.recipients(target_id=enterprise["id"], title="企业实名认证审核结果"), {"buyer"})

    def test_product_review_http_distinguishes_repeat_cycles(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, "product").status = "draft"
            db.commit()
        self.request("POST", "/api/products/product/submit", actor="provider")
        self.request("POST", "/api/products/product/review", actor="reviewer", json={"decision": "approve"})
        self.assertEqual(self.recipients(title="产品待质量审核"), {"platform", "quality"})
        self.request("POST", "/api/products/product/review", actor="quality", json={"decision": "reject", "comment": "Revise"})
        self.request("POST", "/api/products/product/submit", actor="provider")
        with self.m.SessionLocal() as db:
            keys = db.scalars(self.select(self.Message.event_key).where(self.Message.title == "产品待业务审核")).all()
            self.assertEqual(len(keys), 2)
            self.assertNotEqual(keys[0], keys[1])

    def test_payment_delivery_and_two_partial_refunds_http(self):
        oid = self.create_order()
        self.pay(oid)
        self.assertEqual(self.recipients(event_key=f"business:order:{oid}:confirm_payment:paid"), {"buyer", "buyer_admin", "provider"})
        task = self.task_id(oid)
        self.request("POST", f"/api/delivery-tasks/{task}/process", actor="ops", json={"success": False, "error": "PRIVATE ERROR"})
        self.assertEqual(self.recipients(target_id=task, title="订单自动交付异常"), {"buyer", "buyer_admin", "provider", "platform", "ops"})
        self.request("POST", f"/api/delivery-tasks/{task}/process", actor="ops", json={"success": True})
        self.request("POST", f"/api/orders/{oid}/transition", json={"action": "accept_delivery"})
        for amount in (20, 10):
            self.request("POST", f"/api/orders/{oid}/transition", actor="platform", json={"action": "approve_refund", "refund_amount": amount})
            self.request("POST", f"/api/orders/{oid}/transition", actor="platform", json={"action": "complete_refund"})
        with self.m.SessionLocal() as db:
            self.assertEqual(float(db.get(self.m.Order, oid).refunded_amount), 30)
            self.assertEqual(db.scalar(self.select(self.func.count()).select_from(self.m.Refund)), 2)
            self.assertEqual(db.scalar(self.select(self.func.count()).select_from(self.m.Settlement).where(self.m.Settlement.is_refund.is_(True))), 2)
            keys = db.scalars(self.select(self.Message.event_key).where(self.Message.title == "订单退款完成")).all()
            self.assertEqual(len(set(keys)), 2)
            self.assertFalse(any("PRIVATE" in message.body for message in db.scalars(self.select(self.Message))))

    def test_settlement_batch_http_lifecycle_and_tenant_receipts(self):
        oid = self.create_order()
        self.pay(oid)
        batch = self.request("POST", "/api/settlement-batches", actor="finance", json={"order_ids": [oid], "idempotency_key": "http-batch"})
        bid = batch["id"]
        with self.m.SessionLocal() as db:
            sid = db.scalar(self.select(self.m.Settlement.id).where(self.m.Settlement.order_id == oid))
        self.request("POST", f"/api/settlements/{sid}/lock", actor="finance")
        self.request("POST", f"/api/settlement-batches/{bid}/confirm", actor="finance", json={})
        self.request("POST", f"/api/settlement-batches/{bid}/pay", actor="finance", json={})
        self.assertEqual(self.recipients(target_type="settlement_batch", title="清算批次付款完成", tenant_id="buyer_tenant"), {"buyer", "buyer_admin"})
        self.assertEqual(self.recipients(target_type="settlement_batch", title="清算批次付款完成", tenant_id="provider_tenant"), {"provider"})
        self.assertEqual(self.recipients(target_type="settlement_batch", title="清算批次付款完成", tenant_id=""), {"platform", "ops", "finance"})
        outsider = self.request("GET", "/api/notifications", actor="outsider")
        self.assertEqual(outsider["items"], [])
        before = self.counts()
        self.request("POST", f"/api/settlement-batches/{bid}/pay", actor="finance", expected=409, json={})
        self.assertEqual(self.counts(), before)

    def test_review_create_failure_rolls_back_application_and_outbox(self):
        before = self.counts()
        with self.fail_after_create("review"):
            self.request("POST", "/api/verification/personal", expected=500, json=self.personal_body())
        self.assertEqual(self.counts(), before)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(self.select(self.func.count()).select_from(self.m.IdentityVerification)), 0)
            self.assertEqual(db.get(self.m.User, "buyer").verified_status, "verified")

    def test_review_result_failure_rolls_back_decision_and_user(self):
        aid = self.request("POST", "/api/verification/personal", json=self.personal_body())["id"]
        before = self.counts()
        with self.fail_after_create("review"):
            self.request("POST", f"/api/admin/verifications/personal/{aid}/review", actor="platform", expected=500, json={"decision": "approve"})
        self.assertEqual(self.counts(), before)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.IdentityVerification, aid).status, "pending_review")
            self.assertEqual(db.get(self.m.User, "buyer").verified_status, "pending_review")

    def test_payment_create_failure_rolls_back_payment_task_and_outbox(self):
        oid = self.create_order()
        before = self.counts()
        with self.fail_after_create("order"):
            self.request("POST", f"/api/orders/{oid}/transition", expected=500, json={"action": "confirm_payment"})
        self.assertEqual(self.counts(), before)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, oid).payment_status, "unpaid")
            self.assertEqual(db.scalar(self.select(self.m.Payment.status).where(self.m.Payment.order_id == oid)), "unpaid")
            self.assertEqual(db.scalar(self.select(self.func.count()).select_from(self.m.DeliveryTask)), 0)

    def test_delivery_create_failure_rolls_back_task_state_and_outbox(self):
        oid = self.create_order()
        self.pay(oid)
        task = self.task_id(oid)
        before = self.counts()
        with self.fail_after_create("order"):
            self.request("POST", f"/api/delivery-tasks/{task}/process", actor="ops", expected=500, json={"success": True})
        self.assertEqual(self.counts(), before)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.DeliveryTask, task).status, "in_delivery")
            self.assertEqual(db.get(self.m.Order, oid).delivery_status, "in_delivery")

    def test_settlement_create_failure_rolls_back_batch_lines_and_outbox(self):
        oid = self.create_order()
        self.pay(oid)
        before = self.counts()
        with self.fail_after_create("settlement"):
            self.request("POST", "/api/settlement-batches", actor="finance", expected=500, json={"order_ids": [oid], "idempotency_key": "rollback-batch"})
        self.assertEqual(self.counts(), before)
        with self.m.SessionLocal() as db:
            for model in (self.m.Settlement, self.m.SettlementBatch, self.m.SettlementLine):
                self.assertEqual(db.scalar(self.select(self.func.count()).select_from(model)), 0)

    def test_settlement_payment_failure_rolls_back_locked_lines_and_outbox(self):
        oid = self.create_order()
        self.pay(oid)
        bid = self.request("POST", "/api/settlement-batches", actor="finance", json={"order_ids": [oid], "idempotency_key": "pay-rollback"})["id"]
        with self.m.SessionLocal() as db:
            sid = db.scalar(self.select(self.m.Settlement.id).where(self.m.Settlement.order_id == oid))
        self.request("POST", f"/api/settlements/{sid}/lock", actor="finance")
        self.request("POST", f"/api/settlement-batches/{bid}/confirm", actor="finance", json={})
        before = self.counts()
        with self.fail_after_create("settlement"):
            self.request("POST", f"/api/settlement-batches/{bid}/pay", actor="finance", expected=500, json={})
        self.assertEqual(self.counts(), before)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.SettlementBatch, bid).status, "confirmed")
            self.assertEqual(db.get(self.m.Settlement, sid).status, "locked")
            self.assertEqual(set(db.scalars(self.select(self.m.SettlementLine.status).where(self.m.SettlementLine.batch_id == bid))), {"locked"})

    def test_forbidden_reviewer_delivery_and_settlement_emit_nothing(self):
        oid = self.create_order()
        self.pay(oid)
        aid = self.request("POST", "/api/verification/personal", json=self.personal_body())["id"]
        task = self.task_id(oid)
        before = self.counts()
        self.request("POST", f"/api/admin/verifications/personal/{aid}/review", actor="outsider", expected=403, json={"decision": "approve"})
        self.request("POST", f"/api/delivery-tasks/{task}/process", actor="outsider", expected=403, json={"success": True})
        self.request("POST", "/api/settlement-batches", actor="outsider", expected=403, json={"order_ids": [oid]})
        self.assertEqual(self.counts(), before)

    def seed_scheduled_events(self):
        m = self.m
        clock = m.now()
        with m.SessionLocal() as db:
            db.add(m.Product(id="scheduled_product", enterprise_id="provider_tenant", name="SaaS", product_type="saas", delivery_method="tenant_access", provider_name="Provider", provider_type="enterprise"))
            db.flush()
            db.add(m.SaaSProductVersion(id="saas_version", product_id="scheduled_product", version_code="v1", name="SaaS version", annual_price=100, status="active"))
            db.add(m.ApiGatewayRoute(id="route", product_id="scheduled_product", route_key="test-route", upstream_url="https://example.invalid", status="active"))
            db.flush()
            db.add(m.ApiCredential(id="credential", route_id="route", enterprise_id="buyer_tenant", apisix_consumer_name="test-consumer", key_hash="test-credential", created_by="buyer@example.invalid", daily_quota=9, monthly_quota=9, total_quota=9))
            for sid, days, status in (("expired", -1, "active"), ("expiring", 7, "active"), ("later", 8, "active"), ("closed", -1, "closed")):
                db.add(m.SaaSSubscription(id=sid, enterprise_id="buyer_tenant", product_id="scheduled_product", version_id="saas_version", status=status, created_by="buyer@example.invalid", expires_at=clock + timedelta(days=days)))
            db.commit()
        client = Mock()
        client.hgetall.return_value = {"status": "active", "daily_quota": "2", "monthly_quota": "0", "total_quota": "4"}
        client.mget.return_value = ["2", "999", "4"]
        return clock, client

    def test_scheduled_scan_real_create_commit_rollback_and_deduplication(self):
        m = self.m
        clock, client = self.seed_scheduled_events()
        with patch.object(m, "now", return_value=clock), patch.object(m.redis.Redis, "from_url", return_value=client):
            with m.SessionLocal() as db:
                summary = m.notification_scheduled_events(db)
                self.assertEqual(summary, {"saas_candidates": 2, "quota_candidates": 2, "redis_unavailable": False})
                db.flush()
                self.assertEqual(db.scalar(self.select(self.func.count()).select_from(self.Message)), 4)
                db.rollback()
            self.assertEqual(self.counts(), (0, 0, 0))
            self.assertEqual(m.message_center["scheduled_scan"]()["status"], "scanned")
            committed = self.counts()
            self.assertEqual(committed[:2], (4, 8))
            self.assertGreaterEqual(committed[2], 8)
            m.message_center["scheduled_scan"]()
            self.assertEqual(self.counts(), committed)
        self.assertEqual(self.recipients(category="system"), {"buyer", "buyer_admin"})
        with m.SessionLocal() as db:
            self.assertEqual(db.get(m.ApiCredential, "credential").status, "active")
            self.assertEqual(set(db.scalars(self.select(m.SaaSSubscription.status))), {"active", "closed"})

    def test_scheduled_scan_redis_failure_still_commits_saas(self):
        m = self.m
        clock, client = self.seed_scheduled_events()
        client.hgetall.side_effect = m.redis.ConnectionError("offline")
        with patch.object(m, "now", return_value=clock), patch.object(m.redis.Redis, "from_url", return_value=client):
            self.assertEqual(m.message_center["scheduled_scan"]()["status"], "scanned")
        self.assertEqual(self.counts()[0], 2)
        self.assertEqual(self.recipients(category="system"), {"buyer", "buyer_admin"})

    def test_saas_renew_http_real_operation_commits_and_owner_receipts(self):
        m = self.m
        self.seed_scheduled_events()
        with m.SessionLocal() as db:
            db.add(m.SaaSIntegrationConfig(product_id="scheduled_product", status="active"))
            db.commit()
        with patch.object(m, "saas_call", return_value={"status": "renewed"}) as supplier:
            self.request("POST", "/api/saas-subscriptions/expired/renew", actor="buyer_admin", expected=403, json={"billing_cycle": "annual"})
            supplier.assert_not_called()
            renewed = self.request("POST", "/api/saas-subscriptions/expired/renew", actor="buyer", json={"billing_cycle": "annual"})
        supplier.assert_called_once()
        self.assertEqual(renewed["subscription"]["status"], "active")
        self.assertEqual(self.recipients(title="SaaS 订阅已到期"), {"buyer", "buyer_admin"})
        self.assertEqual(self.recipients(title="订单支付完成"), {"buyer", "buyer_admin", "provider"})
        with m.SessionLocal() as db:
            self.assertEqual(db.scalar(self.select(m.SaaSOperation.status)), "succeeded")
            self.assertEqual(db.scalar(self.select(m.Payment.status)), "paid")
            self.assertEqual(db.scalar(self.select(m.Order.buyer_user_id)), "buyer")

    def test_saas_order_resolves_creator_id_email_and_phone(self):
        m = self.m
        self.seed_scheduled_events()
        with m.SessionLocal() as db:
            subscription = db.get(m.SaaSSubscription, "expired")
            product = db.get(m.Product, "scheduled_product")
            version = db.get(m.SaaSProductVersion, "saas_version")
            for creator in ("buyer", "buyer@example.invalid", "13800000000"):
                with self.subTest(creator=creator):
                    subscription.created_by = creator
                    order = m.add_saas_order(db, subscription, product, version, 100, "renew")
                    self.assertEqual(order.buyer_user_id, "buyer")
            db.commit()
            self.assertEqual(db.scalar(self.select(self.func.count()).select_from(m.Order)), 3)

    def test_saas_order_missing_historical_creator_falls_back_to_enterprise_super_admin(self):
        m = self.m
        self.seed_scheduled_events()
        with m.SessionLocal() as db:
            subscription = db.get(m.SaaSSubscription, "expired")
            for creator in ("", "deleted@example.invalid"):
                with self.subTest(creator=creator):
                    subscription.created_by = creator
                    order = m.add_saas_order(db, subscription, db.get(m.Product, "scheduled_product"), db.get(m.SaaSProductVersion, "saas_version"), 100, "renew", paid=True)
                    self.assertEqual(order.buyer_user_id, "buyer")
            db.commit()
        self.assertEqual(self.recipients(title="订单支付完成"), {"buyer", "buyer_admin", "provider"})

    def test_saas_order_no_eligible_buyer_returns_422_before_flush(self):
        m = self.m
        self.seed_scheduled_events()
        with m.SessionLocal() as db:
            subscription = db.get(m.SaaSSubscription, "expired")
            subscription.created_by = "deleted@example.invalid"
            db.get(m.User, "buyer").is_active = False
            db.flush()
            with patch.object(db, "flush", side_effect=AssertionError("must not flush without buyer")):
                with self.assertRaises(m.HTTPException) as raised:
                    m.add_saas_order(db, subscription, db.get(m.Product, "scheduled_product"), db.get(m.SaaSProductVersion, "saas_version"), 100, "renew", paid=True)
            self.assertEqual(raised.exception.status_code, 422)
            self.assertFalse(any(isinstance(item, (m.Order, m.Payment, self.Message)) for item in db.new))
            db.rollback()
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_saas_order_fallback_rejects_inactive_membership_activation_and_platform_role(self):
        m = self.m
        self.seed_scheduled_events()
        with m.SessionLocal() as db:
            subscription = db.get(m.SaaSSubscription, "expired")
            subscription.created_by = ""
            buyer = db.get(m.User, "buyer")
            membership = db.scalar(self.select(m.Membership).where(m.Membership.user_id == "buyer", m.Membership.enterprise_id == "buyer_tenant"))
            product = db.get(m.Product, "scheduled_product")
            version = db.get(m.SaaSProductVersion, "saas_version")
            for obj, field, rejected in ((membership, "status", "disabled"), (membership, "role", "enterprise_admin"), (buyer, "activation_status", "pending_activation"), (buyer, "platform_role", "platform_operator")):
                with self.subTest(field=field, value=rejected):
                    original = getattr(obj, field)
                    setattr(obj, field, rejected)
                    db.flush()
                    with self.assertRaises(m.HTTPException) as raised:
                        m.add_saas_order(db, subscription, product, version, 100, "renew")
                    self.assertEqual(raised.exception.status_code, 422)
                    setattr(obj, field, original)
                    db.flush()
            db.rollback()
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_sla_evaluation_http_breach_has_only_platform_recipients(self):
        m = self.m
        self.seed_scheduled_events()
        with m.SessionLocal() as db:
            db.add(m.SLAProfile(id="profile", name="Test SLA", status="active"))
            db.add(m.ApiUsage(route_id="route", credential_id="credential", enterprise_id="buyer_tenant", method="POST", status_code=500))
            db.commit()
        results = self.request("POST", "/api/sla/evaluate", actor="ops")["items"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "breached")
        self.assertEqual(self.recipients(target_id=results[0]["id"], category="system"), {"platform", "ops"})


if __name__ == "__main__":
    unittest.main()
