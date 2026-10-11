"""Isolated probe regressions; SQLite here does not claim PG contention proof.

The ready-to-run script requires PostgreSQL and proves blocking with pg_stat_activity.
"""
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

import test_message_business_events as fixtures


ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OfflinePGProbeTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown

    def setUp(self):
        fixtures.BusinessRoutesHTTPTests.setUp(self)
        self.probe = load_script("offline_payment_pg_concurrency")
        self.probe.m = self.m
        app = ModuleType("app")
        app.main = self.m
        calendar = importlib.import_module(self.m.__package__ + ".subscription_calendar")
        with patch.dict(sys.modules, {"app": app, "app.subscription_calendar": calendar}):
            self.probe.helper = load_script("subscription_pg_concurrency")

    def test_manifest_exact_ids_permissions_and_tamper_rejection(self):
        probe = self.probe
        state = probe.new_state("training")
        path = probe.persist_manifest(state)
        try:
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(probe.read_manifest(path), state)
            for key, value in (("product", "product"), ("users", ["buyer", "provider"]),
                               ("method", "file"), ("prefix", "qa-off-pay-pg-unsafe")):
                with self.subTest(key=key), self.assertRaises(AssertionError):
                    probe.validate_state({**state, key: value})
            os.chmod(path, 0o644)
            with self.assertRaises(AssertionError):
                probe.read_manifest(path)
        finally:
            os.unlink(path)

    def test_capture_requires_exact_order_request_and_order_lock(self):
        match = self.probe.lock_targets
        sql = "SELECT orders.id FROM orders WHERE orders.id = %(id)s FOR UPDATE"
        self.assertTrue(match(sql, {"id": "own"}, "request", "own", ["request"]))
        self.assertTrue(match(sql, ("own",), "request", "own", ["request"]))
        for statement, params, request in ((sql, {"id": "other"}, "request"),
                                           (sql, {"id": "own"}, "other"),
                                           (sql.replace("FOR UPDATE", ""), {"id": "own"}, "request"),
                                           (sql.replace("FROM orders", "FROM products"),
                                            {"id": "own"}, "request")):
            self.assertFalse(match(statement, params, request, "own", ["request"]))

    def test_non_postgresql_rejected_before_manifest_or_writes(self):
        with patch.object(self.probe, "persist_manifest") as persist:
            with self.assertRaisesRegex(AssertionError, "Real PostgreSQL"):
                self.probe.run()
            persist.assert_not_called()

    def test_all_methods_http_scenarios_rollback_retry_and_owned_scopes(self):
        probe = self.probe
        original = self.m.storefront["versions_for"]
        unrelated_before = None
        with self.m.SessionLocal() as db:
            unrelated_before = (db.get(self.m.Product, "product").name,
                                db.get(self.m.User, "buyer").email)
        for method in probe.METHODS:
            with self.subTest(method=method):
                state = probe.new_state(method)
                probe.check_collisions(state)
                tokens = probe.prepare(state)

                def versions(db, product):
                    return ([{"id": state["version"]}] if product.id == state["product"]
                            else original(db, product))

                # Sequential HTTP exercises probe assertions and failure recovery.
                # Only the actual PostgreSQL invocation uses the blocking race.
                def sequential(http, oid, calls, ordered=False):
                    return {"http_statuses": [http.request(path, body, actor, expected=None).status_code
                                               for path, body, actor in calls]}

                with patch.dict(self.m.storefront, {"versions_for": versions}), \
                        patch.object(probe, "http_race", sequential):
                    result = probe.exercise(state, probe.HTTP(state, tokens))
                self.assertTrue(result["notification_failure_rollback_retry"])
                with self.m.SessionLocal() as db:
                    predicates = probe.scopes(db, state)
                    tasks = list(db.scalars(self.select(self.m.DeliveryTask.id).where(
                        predicates[self.m.DeliveryTask.__tablename__])))
                    self.assertEqual(len(tasks), 4)
                    probe.validate_ownership(db, state)
                    db.get(self.m.Product, state["product"]).enterprise_id = "other_tenant"
                    with self.assertRaisesRegex(AssertionError, "product ownership"):
                        probe.validate_ownership(db, state)
                    db.rollback()
                from sqlalchemy import event

                def sqlite_timeout(conn, cursor, statement, parameters, context, executemany):
                    if statement.startswith("SET LOCAL statement_timeout"):
                        return "SELECT 1", ()
                    return statement, parameters

                # Translate only PG's timeout setup; run real scoped deletion,
                # parent discovery and all FK-leftover checks on isolated SQLite.
                event.listen(self.m.engine, "before_cursor_execute", sqlite_timeout, retval=True)
                try:
                    probe.cleanup(state)
                    probe.cleanup(state)  # Recovery after successful cleanup is safe.
                finally:
                    event.remove(self.m.engine, "before_cursor_execute", sqlite_timeout)
                with self.m.SessionLocal() as db:
                    self.assertEqual((db.get(self.m.Product, "product").name,
                                      db.get(self.m.User, "buyer").email), unrelated_before)
                    self.assertEqual(db.scalar(self.select(self.func.count()).select_from(
                        self.m.DeliveryTask).where(self.m.DeliveryTask.id.in_(tasks))), 0)

    def test_failure_patch_delegates_unrelated_orders_and_requests(self):
        probe = self.probe
        from types import SimpleNamespace
        with patch.object(self.m, "notify_order_event") as original:
            with probe.fail_notification_once("own", "owned-request") as failures:
                token = self.m.audit_request_id.set("other-request")
                try:
                    self.m.notify_order_event(None, SimpleNamespace(id="own"), "confirm_payment", "paid")
                finally:
                    self.m.audit_request_id.reset(token)
                token = self.m.audit_request_id.set("owned-request")
                try:
                    self.m.notify_order_event(None, SimpleNamespace(id="other"), "confirm_payment", "paid")
                finally:
                    self.m.audit_request_id.reset(token)
                self.assertEqual(failures, [])
                self.assertEqual(original.call_count, 2)


if __name__ == "__main__":
    unittest.main()
