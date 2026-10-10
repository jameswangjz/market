"""Production metering routes, FK-enabled isolated SQLite; no startup or network."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_message_business_events as fixtures


class SettlementMeteringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = fixtures.load_isolated_main()

    @classmethod
    def tearDownClass(cls):
        fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__(cls)

    setUp = fixtures.BusinessRoutesHTTPTests.setUp
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request
    create_order = fixtures.BusinessRoutesHTTPTests.create_order
    pay = fixtures.BusinessRoutesHTTPTests.pay
    task_id = fixtures.BusinessRoutesHTTPTests.task_id

    def measurements(self, order_id):
        with self.m.SessionLocal() as db:
            return [self.m.settlement_metering["output"](item) for item in db.scalars(self.select(self.m.SettlementMeasurement).where(self.m.SettlementMeasurement.order_id == order_id))]

    def manual(self, order_id, actor="buyer_admin", expected=200, **values):
        return self.request("POST", "/api/settlement-measurements", actor=actor, expected=expected,
                            json={"order_id": order_id, "measurement_type": "download", "quantity": 2, "unit": "count", **values})

    def test_manual_pending_linkage_and_idempotent_legacy_and_explicit_keys(self):
        oid = self.create_order()
        first = self.manual(oid, source="运营工作台", source_id="ticket-001")
        repeated = self.manual(oid, source="运营工作台", source_id="ticket-001")
        self.assertEqual(first["id"], repeated["id"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(first["validation_status"], "pending")
        self.assertEqual(first["source"], "manual")
        self.assertEqual(first["source_id"], "ticket-001")
        self.assertEqual(first["actor_id"], "buyer_admin")
        self.assertEqual(self.request("GET", f"/api/settlement-measurements/{first['id']}")["source_id"], "ticket-001")
        linked = self.request("GET", "/api/settlement-measurements", params={"order_id": oid, "source": "manual", "source_id": "ticket-001", "event_key": first["event_key"]})
        self.assertEqual(linked["total"], 1)
        self.request("GET", f"/api/settlement-measurements/{first['id']}", actor="outsider", expected=403)
        second = self.manual(oid, event_key="event-two", source="apisix_redis")
        self.assertEqual(second["source"], "manual")
        self.manual(oid, event_key="event-two", source="apisix_redis", quantity=3, expected=409)
        self.assertEqual(len(self.measurements(oid)), 2)
        with self.m.SessionLocal() as db:
            logs = db.scalars(self.select(self.m.AuditLog).where(self.m.AuditLog.action == "create_settlement_measurement")).all()
            self.assertEqual(len(logs), 2)
            self.assertTrue(all(log.target_id in {first["id"], second["id"]} for log in logs))

    def test_manual_role_order_scope_and_platform_membership_are_enforced(self):
        oid = self.create_order()
        for actor in ("outsider", "reviewer", "quality", "inactive"):
            with self.subTest(actor=actor):
                self.manual(oid, actor=actor, expected=403)
        self.manual(oid, actor="disabled_ops", expected=401)
        with self.m.SessionLocal() as db:
            membership = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "buyer_admin"))
            membership.role = "member"
            db.commit()
        self.manual(oid, actor="buyer_admin", expected=403)
        self.manual(oid, actor="provider", event_key="provider-evidence")
        self.manual(oid, actor="finance", event_key="finance-evidence")

    def test_type_quantity_unit_and_time_validation(self):
        oid = self.create_order()
        clock = datetime.now(timezone.utc)
        invalid = [{"quantity": -1}, {"quantity": "NaN"}, {"quantity": "Infinity"},
                   {"quantity": "1000000000000"}, {"quantity": "0.0000001"}, {"quantity": "1.5"},
                   {"measurement_type": "payment"}, {"measurement_type": "unknown"}, {"unit": "CNY"},
                   {"measurement_type": "consulting_hours", "unit": "count"},
                   {"period_start": clock.isoformat()},
                   {"period_start": clock.isoformat(), "period_end": (clock - timedelta(days=1)).isoformat()},
                   {"period_start": clock.isoformat(), "period_end": (clock + timedelta(days=1)).isoformat()},
                   {"validation_status": "validated"}]
        for values in invalid:
            with self.subTest(values=values):
                self.manual(oid, expected=422, **values)
        result = self.manual(oid, measurement_type="consulting_hours", unit="hour", quantity="1.25",
                             period_start=(clock - timedelta(days=1)).isoformat(), period_end=(clock - timedelta(hours=1)).isoformat())
        self.assertEqual(result["quantity"], 1.25)

    def test_list_detail_and_transition_cross_tenant_access(self):
        oid = self.create_order()
        self.manual(oid)
        self.request("GET", f"/api/orders/{oid}", actor="outsider", expected=403)
        self.request("GET", f"/api/settlement-measurements?order_id={oid}", actor="outsider", expected=403)
        self.assertEqual(self.request("GET", "/api/settlement-measurements", actor="outsider")["items"], [])
        self.assertEqual(self.request("GET", "/api/orders", actor="outsider")["items"], [])
        for action in ("confirm_payment", "approve_refund", "complete_refund", "submit_after_sales", "create_task", "approve"):
            self.request("POST", f"/api/orders/{oid}/transition", actor="outsider", expected=403, json={"action": action})
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, oid).payment_status, "unpaid")

    def test_demoted_enterprise_order_creator_cannot_pay_cancel_accept_or_create(self):
        oid = self.create_order()
        m = self.m
        with m.SessionLocal() as db:
            creator_membership = db.scalar(self.select(m.Membership).where(m.Membership.user_id == "buyer", m.Membership.enterprise_id == "buyer_tenant"))
            creator_membership.role = "member"
            db.commit()
        for action in ("start_payment", "confirm_payment", "cancel_order", "accept_delivery", "reject_delivery", "confirm_order", "submit_after_sales", "submit_review"):
            with self.subTest(action=action):
                self.request("POST", f"/api/orders/{oid}/transition", actor="buyer", expected=403, json={"action": action})
        self.request("POST", "/api/orders", expected=403, json={"product_id": "product", "product_version_id": "version", "buyer_enterprise_id": "buyer_tenant"})
        with m.SessionLocal() as db:
            self.assertEqual(db.get(m.Order, oid).payment_status, "unpaid")
            self.assertEqual(db.scalar(self.select(self.func.count()).select_from(m.Order)), 1)
        self.request("POST", f"/api/orders/{oid}/transition", actor="buyer_admin", json={"action": "confirm_payment"})
        self.assertEqual(len(self.measurements(oid)), 1)

    def test_inactive_enterprise_admin_and_platform_membership_cannot_pay_for_buyer(self):
        oid = self.create_order()
        m = self.m
        with m.SessionLocal() as db:
            membership = db.scalar(self.select(m.Membership).where(m.Membership.user_id == "buyer", m.Membership.enterprise_id == "buyer_tenant"))
            membership.status = "disabled"
            db.commit()
        for actor in ("buyer", "platform", "ops", "finance"):
            self.request("POST", f"/api/orders/{oid}/transition", actor=actor, expected=403, json={"action": "confirm_payment"})
        self.assertEqual(self.measurements(oid), [])

    def test_personal_buyer_permission_requires_no_enterprise_subject_and_verification(self):
        # Current Order FK requires an enterprise; this is a permission-helper test, not a personal-order HTTP claim.
        m = self.m
        with m.SessionLocal() as db:
            buyer, other = db.get(m.User, "buyer"), db.get(m.User, "outsider")
            personal = SimpleNamespace(buyer_enterprise_id=None, buyer_user_id=buyer.id)
            for action in ("start_payment", "confirm_payment", "cancel_order", "accept_delivery"):
                m.settlement_metering["require_transition"](db, buyer, personal, action)
                with self.assertRaises(m.HTTPException) as raised:
                    m.settlement_metering["require_transition"](db, other, personal, action)
                self.assertEqual(raised.exception.status_code, 403)
            buyer.verified_status = "pending_review"
            with self.assertRaises(m.HTTPException) as raised:
                m.settlement_metering["require_transition"](db, buyer, personal, "confirm_payment")
            self.assertEqual(raised.exception.status_code, 403)

    def test_paid_refund_and_delivery_observations_link_to_actual_entities(self):
        oid = self.create_order()
        self.pay(oid)
        with self.m.SessionLocal() as db:
            paid_at = db.scalar(self.select(self.m.Payment.paid_at).where(self.m.Payment.order_id == oid))
        self.pay(oid)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(self.select(self.m.Payment.paid_at).where(self.m.Payment.order_id == oid)), paid_at)
        payments = [item for item in self.measurements(oid) if item["measurement_type"] == "payment"]
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0]["quantity"], 100)
        self.assertEqual(payments[0]["evidence"]["payment_id"], payments[0]["source_id"])
        tid = self.task_id(oid)
        self.request("POST", f"/api/delivery-tasks/{tid}/process", actor="ops", json={"success": False, "error": "PRIVATE FAILURE"})
        failure = next(item for item in self.measurements(oid) if item["source"] == "delivery")
        self.assertEqual(failure["validation_status"], "pending")
        self.assertNotIn("PRIVATE FAILURE", str(failure))
        self.request("POST", f"/api/delivery-tasks/{tid}/process", actor="ops", json={"success": True})
        self.request("POST", f"/api/orders/{oid}/transition", json={"action": "accept_delivery"})
        for amount in (20, 10):
            self.request("POST", f"/api/orders/{oid}/transition", actor="finance", json={"action": "approve_refund", "refund_amount": amount})
            self.request("POST", f"/api/orders/{oid}/transition", expected=409, json={"action": "confirm_payment"})
            self.request("POST", f"/api/orders/{oid}/transition", actor="finance", json={"action": "complete_refund"})
        refunds = [item for item in self.measurements(oid) if item["measurement_type"] == "refund"]
        self.assertEqual(sorted(item["quantity"] for item in refunds), [10, 20])
        self.assertEqual(len({item["source_id"] for item in refunds}), 2)
        summary = self.request("GET", f"/api/orders/{oid}/settlement-measurements/summary")
        self.assertEqual(summary["paid_amount"], 100)
        self.assertEqual(summary["completed_refund_amount"], 30)
        self.assertEqual(summary["measurement_billing_effect"], "none")

    def product_file(self, oid):
        self.pay(oid)
        with self.m.SessionLocal() as db:
            db.add(self.m.FileObject(id="download-file", owner_id="provider", product_id="product", version_id="version",
                                     object_name="data", original_name="data.csv", file_role="product_data", size=9999, status="active"))
            db.commit()
        response = Mock()
        response.read.return_value = b"actual content"
        storage = Mock()
        storage.get_object.return_value = response
        return storage

    def download(self, oid, storage, actor="buyer", expected=200):
        with patch.object(self.m, "MINIO_ENDPOINT", "storage.invalid"), patch.object(self.m, "Minio", return_value=storage):
            response = self.client.get(f"/api/files/download-file/download?order_id={oid}", headers={"Authorization": "Bearer " + self.tokens[actor]})
        self.assertEqual(response.status_code, expected, response.text)
        return response

    def test_real_download_collects_actual_bytes_and_log_after_object_read(self):
        oid = self.create_order()
        storage = self.product_file(oid)
        result = self.download(oid, storage)
        self.assertEqual(result.content, b"actual content")
        self.download(oid, storage, actor="buyer_admin")
        downloads = [item for item in self.measurements(oid) if item["measurement_type"] == "download"]
        self.assertEqual(len(downloads), 2)
        self.assertEqual(len({item["event_key"] for item in downloads}), 2)
        self.assertTrue(all(item["evidence"]["response_bytes"] == 14 for item in downloads))
        self.assertTrue(all(item["validation_status"] == "observed" for item in downloads))
        with self.m.SessionLocal() as db:
            self.assertTrue(all(db.get(self.m.FileDownloadLog, item["source_id"]).success for item in downloads))

    def test_denied_missing_and_limited_downloads_never_count_as_success(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.Product, "product").download_limit = 1
            db.commit()
        oid = self.create_order()
        storage = self.product_file(oid)
        self.download(oid, storage, actor="outsider", expected=403)
        storage.get_object.side_effect = RuntimeError("missing object")
        self.download(oid, storage, expected=404)
        storage.get_object.side_effect = None
        self.download(oid, storage)
        self.download(oid, storage, expected=429)
        self.assertEqual(len([item for item in self.measurements(oid) if item["measurement_type"] == "download"]), 1)

    def test_payment_measurement_business_and_notifications_roll_back_together(self):
        oid = self.create_order()
        real_audit = self.m.audit
        def fail_after_meter(db, actor, action, *args, **kwargs):
            if action == "confirm_payment":
                raise RuntimeError("after metering")
            return real_audit(db, actor, action, *args, **kwargs)
        with patch.object(self.m, "audit", side_effect=fail_after_meter):
            self.request("POST", f"/api/orders/{oid}/transition", expected=500, json={"action": "confirm_payment"})
        self.assertEqual(self.measurements(oid), [])
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, oid).payment_status, "unpaid")
            self.assertEqual(db.scalar(self.select(self.func.count()).select_from(self.Message)), 0)

    def test_manual_metering_and_audit_failure_roll_back_without_prior_business_write(self):
        oid = self.create_order()
        with patch.object(self.m, "audit", side_effect=RuntimeError("failed audit")):
            self.manual(oid, expected=500)
        self.assertEqual(self.measurements(oid), [])

    def test_repeated_automatic_event_id_is_order_scoped_and_does_not_commit(self):
        oid = self.create_order()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            record = self.m.settlement_metering["record"]
            item, duplicate = record(db, order, "download", 1, "count", "file_download", "source-event", "log-id")
            again, duplicate = record(db, order, "download", 1, "count", "file_download", "source-event", "log-id")
            self.assertTrue(duplicate)
            self.assertEqual(item.id, again.id)
            db.rollback()
        self.assertEqual(self.measurements(oid), [])

    def test_business_flush_errors_are_not_hidden_as_metering_duplicates(self):
        oid = self.create_order()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, oid)
            with patch.object(db, "flush", side_effect=RuntimeError("business constraint")):
                with self.assertRaisesRegex(RuntimeError, "business constraint"):
                    self.m.settlement_metering["record"](db, order, "download", 1, "count", "file_download", "event")

    def test_two_concurrent_sessions_persist_one_event(self):
        from sqlalchemy import create_engine, event
        from sqlalchemy.engine import URL
        from sqlalchemy.orm import sessionmaker
        m = self.m
        with TemporaryDirectory(prefix="settlement-metering-") as directory:
            engine = create_engine(URL.create("sqlite", database=str(Path(directory) / "metering.db")), connect_args={"timeout": 10, "check_same_thread": False})
            @event.listens_for(engine, "connect")
            def enable_foreign_keys(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
            m.Base.metadata.create_all(engine)
            sessions = sessionmaker(bind=engine, autoflush=False)
            with sessions() as db:
                db.add(m.User(id="u", name="u", password_hash="unused"))
                db.add(m.Enterprise(id="e", name="e", credit_code="e"))
                db.flush()
                db.add(m.Product(id="p", enterprise_id="e", name="p", product_type="dataset"))
                db.flush()
                db.add(m.Order(id="o", order_no="o", buyer_user_id="u", buyer_enterprise_id="e", provider_enterprise_id="e", product_id="p", buyer_name="u", product_name="p"))
                db.commit()
            barrier = Barrier(2)
            def collect():
                with sessions() as db:
                    order = db.get(m.Order, "o")
                    real_get = db.get
                    first = True
                    def synchronize(model, identity, **kwargs):
                        nonlocal first
                        result = real_get(model, identity, **kwargs)
                        if model is m.SettlementMeasurement and first:
                            first = False
                            barrier.wait(timeout=10)
                        return result
                    with patch.object(db, "get", side_effect=synchronize):
                        item, duplicate = m.settlement_metering["record"](db, order, "download", 1, "count", "file_download", "same-event")
                        db.commit()
                        return item.id, duplicate
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _: collect(), range(2)))
                self.assertEqual(len({result[0] for result in results}), 1)
                self.assertEqual(sorted(result[1] for result in results), [False, True])
                with sessions() as db:
                    self.assertEqual(db.scalar(self.select(self.func.count()).select_from(m.SettlementMeasurement)), 1)
            finally:
                engine.dispose()

    def api_credential(self, oid, model=False):
        self.pay(oid)
        with self.m.SessionLocal() as db:
            if model:
                db.get(self.m.Product, "product").product_type = "model"
            db.add(self.m.ApiGatewayRoute(id="meter-route", product_id="product", route_key="meter-route-key", upstream_url="https://example.invalid", status="active"))
            db.flush()
            db.add(self.m.ApiCredential(id="meter-credential", route_id="meter-route", enterprise_id="buyer_tenant", order_id="",
                                        key_hash="meter-key", apisix_consumer_name="test-meter-consumer"))
            db.commit()

    def snapshot(self, oid, actor="buyer_admin", expected=200, **values):
        return self.request("POST", "/api/settlement-measurements/api-snapshot", actor=actor, expected=expected,
                            json={"order_id": oid, "credential_id": "meter-credential", **values})

    def redis_client(self, count="5"):
        client = Mock()
        client.connection_pool.connection_kwargs = {"db": 0}
        client.get.return_value = count
        return client

    def test_api_model_snapshot_precise_keys_dedup_and_nonadditive_aggregate(self):
        oid = self.create_order()
        self.api_credential(oid, model=True)
        client = self.redis_client()
        clock = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
        with patch.object(self.m, "now", return_value=clock), patch.object(self.m.redis.Redis, "from_url", return_value=client):
            one = self.snapshot(oid)
            two = self.snapshot(oid)
            self.assertEqual(one["id"], two["id"])
            self.assertTrue(two["idempotent"])
            client.get.assert_called_with("market:apisix:quota:day:meter-route-key:test-meter-consumer:2026-10-09")
            client.get.return_value = "9"
            self.snapshot(oid)
            self.snapshot(oid, period="month")
            client.get.assert_called_with("market:apisix:quota:month:meter-route-key:test-meter-consumer:2026-10")
            self.snapshot(oid, period="total")
            client.get.assert_called_with("market:apisix:quota:total:meter-route-key:test-meter-consumer")
        self.assertEqual(one["measurement_type"], "model_call")
        self.assertEqual(one["validation_status"], "observed")
        summary = self.request("GET", f"/api/orders/{oid}/settlement-measurements/summary")
        self.assertEqual(len(summary["snapshots"]), 3)
        self.assertFalse(any(row["measurement_type"] == "model_call" for row in summary["event_totals"]))
        self.assertEqual(summary["paid_amount"], 100)

    def test_snapshot_permission_and_credential_order_scope_before_redis(self):
        oid = self.create_order()
        self.api_credential(oid)
        for actor in ("outsider", "provider", "reviewer", "quality"):
            self.snapshot(oid, actor=actor, expected=403)
        with self.m.SessionLocal() as db:
            db.get(self.m.ApiCredential, "meter-credential").enterprise_id = "other_tenant"
            db.commit()
        self.snapshot(oid, expected=403)
        self.assertEqual(len(self.measurements(oid)), 1)

    def test_shared_credential_two_orders_have_one_unattributed_snapshot_and_links(self):
        first = self.create_order()
        self.api_credential(first)
        second = self.create_order()
        self.pay(second)
        client = self.redis_client("17")
        with patch.object(self.m.redis.Redis, "from_url", return_value=client):
            one = self.snapshot(first)
            two = self.snapshot(second)
        self.assertEqual(one["id"], two["id"])
        self.assertTrue(two["idempotent"])
        self.assertEqual(two["requested_order_id"], second)
        self.assertEqual(one["scope"], "credential_shared")
        self.assertEqual(set(one["related_order_ids"]), {first, second})
        self.assertIsNone(one["order_usage_quantity"])
        self.assertEqual(one["order_id_role"], "storage_anchor")
        for oid in (first, second):
            summary = self.request("GET", f"/api/orders/{oid}/settlement-measurements/summary")
            self.assertTrue(summary["snapshots_non_additive"])
            self.assertEqual([item["id"] for item in summary["snapshots"]], [one["id"]])
            self.assertFalse(any(item["measurement_type"] == "api_call" for item in summary["event_totals"]))
            self.assertEqual(summary["paid_amount"], 100)
            linked = self.request("GET", "/api/settlement-measurements", params={"order_id": oid, "source": "apisix_redis"})
            self.assertEqual([item["id"] for item in linked["items"]], [one["id"]])
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(self.select(self.func.count()).select_from(self.m.SettlementMeasurement).where(self.m.SettlementMeasurement.sample_kind == "snapshot")), 1)
            self.assertEqual(set(db.scalars(self.select(self.m.SettlementMeasurementOrder.order_id).where(self.m.SettlementMeasurementOrder.measurement_id == one["id"]))), {first, second})
        self.request("GET", f"/api/settlement-measurements/{one['id']}", actor="outsider", expected=403)

    def test_shared_entitlements_exclude_unpaid_closed_cancelled_and_refunded_orders(self):
        first = self.create_order()
        self.api_credential(first)
        unpaid = self.create_order()
        second = self.create_order()
        self.pay(second)
        client = self.redis_client("3")
        with patch.object(self.m.redis.Redis, "from_url", return_value=client):
            self.snapshot(unpaid, expected=403)
            for field, value in (("main_status", "closed"), ("main_status", "cancelled"), ("refunded_amount", 1), ("payment_status", "refunding")):
                with self.subTest(field=field, value=value):
                    with self.m.SessionLocal() as db:
                        order = db.get(self.m.Order, second)
                        original = getattr(order, field)
                        setattr(order, field, value)
                        db.commit()
                    self.snapshot(second, expected=403)
                    result = self.snapshot(first)
                    self.assertEqual(result["related_order_ids"], [first])
                    with self.m.SessionLocal() as db:
                        setattr(db.get(self.m.Order, second), field, original)
                        db.commit()

    def test_snapshot_missing_invalid_and_unavailable_remain_pending(self):
        oid = self.create_order()
        self.api_credential(oid)
        client = self.redis_client()
        for value in (None, "NaN", "-1", "1.5", "1000000000000", "bad"):
            with self.subTest(value=value), patch.object(self.m.redis.Redis, "from_url", return_value=client):
                client.get.return_value = value
                result = self.snapshot(oid)
                self.assertEqual(result["validation_status"], "pending")
                self.assertEqual(result["billing_effect"], "none")
        client.get.side_effect = self.m.redis.ConnectionError("offline")
        with patch.object(self.m.redis.Redis, "from_url", return_value=client):
            self.assertEqual(self.snapshot(oid)["validation_status"], "pending")
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, oid).payment_status, "paid")

    def test_counter_reset_is_pending_and_snapshot_audit_failure_rolls_back(self):
        oid = self.create_order()
        self.api_credential(oid)
        client = self.redis_client("100")
        with patch.object(self.m.redis.Redis, "from_url", return_value=client):
            self.snapshot(oid)
            client.get.return_value = "1"
            result = self.snapshot(oid)
            self.assertEqual(result["validation_status"], "pending")
            self.assertEqual(result["quantity"], 1)
            client.get.return_value = "200"
            before = len(self.measurements(oid))
            with patch.object(self.m, "audit", side_effect=RuntimeError("snapshot audit failed")):
                self.snapshot(oid, expected=500)
        self.assertEqual(len(self.measurements(oid)), before)

    def test_offline_fulfillment_is_traced_and_never_claimed_validated(self):
        oid = self.create_order()
        self.pay(oid)
        with self.m.SessionLocal() as db:
            task = db.get(self.m.DeliveryTask, self.task_id(oid))
            task.delivery_mode = "manual"
            db.commit()
        self.request("POST", f"/api/delivery-tasks/{self.task_id(oid)}/process", actor="ops", json={"success": True})
        item = next(item for item in self.measurements(oid) if item["measurement_type"] == "offline_delivery")
        self.assertEqual(item["source_id"], self.task_id(oid))
        self.assertEqual(item["validation_status"], "observed")

    def test_paid_saas_order_hook_has_real_buyer_payment_and_payment_time(self):
        m = self.m
        fixtures.BusinessRoutesHTTPTests.seed_scheduled_events(self)
        with m.SessionLocal() as db:
            order = m.add_saas_order(db, db.get(m.SaaSSubscription, "expired"), db.get(m.Product, "scheduled_product"), db.get(m.SaaSProductVersion, "saas_version"), Decimal("100"), "renew", paid=True)
            oid = order.id
            db.commit()
        measurements = self.measurements(oid)
        self.assertEqual(len(measurements), 1)
        self.assertEqual(measurements[0]["actor_id"], "buyer")
        self.assertIsNotNone(measurements[0]["evidence"]["paid_at"])
        self.assertEqual(self.request("GET", f"/api/orders/{oid}/settlement-measurements/summary")["paid_amount"], 100)

    def test_pending_usage_and_open_after_sales_do_not_change_monthly_settlement(self):
        oid = self.create_order()
        self.pay(oid)
        self.manual(oid, quantity=999999999999)
        self.request("POST", f"/api/orders/{oid}/transition", json={"action": "submit_after_sales"})
        clock = datetime.now(timezone.utc)
        self.request("POST", "/api/settlement-batches", actor="finance", json={"cycle": "monthly", "order_ids": [oid],
                      "period_start": clock.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(),
                      "period_end": (clock.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(),
                      "idempotency_key": "metering-does-not-bill"})
        with self.m.SessionLocal() as db:
            settlement = db.scalar(self.select(self.m.Settlement).where(self.m.Settlement.order_id == oid, self.m.Settlement.is_refund.is_(False)))
            self.assertIsNotNone(settlement)
            self.assertEqual(Decimal(str(settlement.net_amount)), Decimal("100"))
            self.assertEqual(db.get(self.m.Order, oid).after_sales_status, "processing")

    def test_legacy_schema_upgrade_is_additive_and_idempotent(self):
        from sqlalchemy import inspect
        oid = self.create_order()
        engine = self.m.engine
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE settlement_measurements")
            connection.exec_driver_sql("CREATE TABLE settlement_measurements (id VARCHAR(36) PRIMARY KEY, order_id VARCHAR(36), measurement_type VARCHAR(50), quantity NUMERIC(18,6), unit VARCHAR(30), source VARCHAR(120), period_start DATETIME, period_end DATETIME, validation_status VARCHAR(30), validation_message TEXT, created_at DATETIME)")
            connection.exec_driver_sql("INSERT INTO settlement_measurements (id, order_id, source, validation_status) VALUES ('legacy', ?, 'platform', 'validated')", (oid,))
        self.m.ensure_review_and_file_schema()
        self.m.ensure_review_and_file_schema()
        columns = {column["name"] for column in inspect(engine).get_columns("settlement_measurements")}
        self.assertTrue({"event_key", "source_id", "actor_id", "sample_kind", "scope", "evidence_json"} <= columns)
        with engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT sample_kind, evidence_json, event_key, validation_status FROM settlement_measurements WHERE id='legacy'").one(), ("event", "{}", "legacy:legacy", "pending"))


if __name__ == "__main__":
    unittest.main()
