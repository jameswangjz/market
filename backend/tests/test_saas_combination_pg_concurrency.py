"""Local isolated units for the PG probe; SQLite is not PG contention proof."""

import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import event, func, select
import test_message_business_events as fixtures


ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SaaSCombinationPGProbeTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown

    def setUp(self):
        fixtures.BusinessRoutesHTTPTests.setUp(self)
        self.probe = load_script("saas_combination_pg_concurrency")
        self.probe.m = self.m
        app = ModuleType("app")
        app.main = self.m
        calendar = __import__(self.m.__package__ + ".subscription_calendar", fromlist=["CalendarAnchor"])
        with patch.dict(sys.modules, {"app": app, "app.subscription_calendar": calendar}):
            self.probe.helper = load_script("subscription_pg_concurrency")

        def sqlite_timeout(conn, cursor, statement, parameters, context, executemany):
            return ("SELECT 1", ()) if statement.startswith("SET LOCAL statement_timeout") else (statement, parameters)

        event.listen(self.m.engine, "before_cursor_execute", sqlite_timeout, retval=True)
        self.addCleanup(event.remove, self.m.engine, "before_cursor_execute", sqlite_timeout)

    def test_manifest_exact_ids_permissions_symlink_and_tamper_rejection(self):
        probe = self.probe
        state = probe.new_state("product")
        path = probe.persist_manifest(state)
        link = path + ".link"
        try:
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(probe.read_manifest(path), state)
            for key, value in (("product", "product"), ("version", "sv1"),
                               ("historical", ["old1", "old2", "other"]),
                               ("users", ["buyer", "provider"]), ("scenario", "invalid"),
                               ("prefix", "qa-saas-pg-unsafe")):
                with self.subTest(key=key), self.assertRaises(AssertionError):
                    probe.validate_state({**state, key: value})
            os.symlink(path, link)
            with self.assertRaises(OSError):
                probe.read_manifest(link)
            os.chmod(path, 0o644)
            with self.assertRaises(AssertionError):
                probe.read_manifest(path)
        finally:
            os.unlink(path)
            if os.path.lexists(link):
                os.unlink(link)

    def test_capture_requires_exact_owned_lock_and_request(self):
        state = self.probe.new_state("enterprise")
        for table, oid in (("products", state["product"]), ("enterprises", state["enterprises"][0])):
            sql = f"SELECT {table}.id FROM {table} WHERE {table}.id = %(id)s FOR UPDATE"
            match = self.probe.lock_targets
            self.assertTrue(match(sql, {"id": oid}, state["requests"][0], state, table))
            self.assertTrue(match(sql, (oid,), state["requests"][1], state, table))
            for statement, parameters, rid in ((sql, {"id": "other"}, state["requests"][0]),
                    (sql, {"id": oid}, "other"), (sql, {"id": oid}, state["requests"][2]),
                    (sql.replace("FOR UPDATE", ""), {"id": oid}, state["requests"][0]),
                    (sql.replace(f"FROM {table}", "FROM unrelated"), {"id": oid}, state["requests"][0])):
                self.assertFalse(match(statement, parameters, rid, state, table))

    def test_non_pg_rejected_before_any_manifest_or_writes(self):
        with patch.object(self.probe, "persist_manifest") as persist, \
                patch.object(self.probe, "prepare") as prepare:
            with self.assertRaisesRegex(AssertionError, "Real PostgreSQL"):
                self.probe.run()
            persist.assert_not_called()
            prepare.assert_not_called()

    def test_owned_catalog_delegates_and_restores(self):
        from types import SimpleNamespace
        state = self.probe.new_state("product")
        with patch.dict(self.m.storefront, {"versions_for": unittest.mock.Mock(return_value=[{"id": "other"}])}):
            original = self.m.storefront["versions_for"]
            with self.probe.catalog(state):
                self.assertEqual(self.m.storefront["versions_for"](None, SimpleNamespace(id=state["product"])),
                                 [{"id": state["version"]}])
                self.assertEqual(self.m.storefront["versions_for"](None, SimpleNamespace(id="other")),
                                 [{"id": "other"}])
                original.assert_called_once()
            self.assertIs(self.m.storefront["versions_for"], original)

    def test_checkpoint_top_level_fields(self):
        result = {"two_blocked_sessions_observed": True, "cleanup": "verified"}
        with patch.object(self.m.engine.dialect, "name", "postgresql"), \
                patch.object(self.probe, "execute_fixture", return_value=result) as execute:
            report = self.probe.run()
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["database"], "postgresql")
            self.assertIs(report["two_blocked_sessions_observed"], True)
            self.assertEqual(report["cleanup"], "verified")
            self.assertEqual([call.args[0] for call in execute.call_args_list], list(self.probe.SCENARIOS))
            self.assertIs(self.probe.run("history")["two_blocked_sessions_observed"], False)

    def test_race_gate_capture_and_listener_restoration(self):
        # Exercise production probe orchestration with fake PID observations;
        # only a real PG invocation can establish blocking evidence.
        probe = self.probe
        for scenario in ("product", "enterprise"):
            state = probe.new_state(scenario)
            token = probe.prepare(state)
            callbacks = []

            def listen(engine, name, callback):
                self.assertEqual(name, "before_cursor_execute")
                callbacks.append(callback)

            def fake_race(model, oid, worker, pids, pid_lock):
                self.assertIs(model, self.m.Product if scenario == "product" else self.m.Enterprise)
                self.assertEqual(oid, state["product"] if scenario == "product" else state["enterprises"][0])
                responses = []
                for index in range(2):
                    context = self.m.audit_request_id.set(state["requests"][index])
                    try:
                        cursor = Mock()
                        cursor.fetchone.return_value = (100 + index,)
                        for table, owned in (("products", state["product"]),
                                             ("enterprises", state["enterprises"][0])):
                            sql = f"SELECT {table}.id FROM {table} WHERE {table}.id = %(id)s FOR UPDATE"
                            callbacks[0](None, cursor, sql, {"id": owned}, None, False)
                        # Repeated lock statements must not duplicate worker PIDs.
                        callbacks[0](None, cursor, "SELECT products.id FROM products FOR UPDATE",
                                     {"id": state["product"]}, None, False)
                    finally:
                        self.m.audit_request_id.reset(context)
                    responses.append(worker(index, Mock()))
                self.assertEqual(pids, [100, 101])
                return responses, pids

            try:
                with probe.catalog(state), patch.object(probe.event, "listen", listen), \
                        patch.object(probe.event, "remove") as remove, \
                        patch.object(probe.helper, "race", fake_race):
                    result = probe.first_purchase_race(state, token)
                    self.assertEqual(result["worker_pids"], [100, 101])
                    remove.assert_called_once_with(self.m.engine, "before_cursor_execute", callbacks[0])
                callbacks.clear()
                with patch.object(probe.event, "listen", listen), \
                        patch.object(probe.event, "remove") as remove, \
                        patch.object(probe.helper, "race", side_effect=AssertionError("race failed")):
                    with self.assertRaisesRegex(AssertionError, "race failed"):
                        probe.first_purchase_race(state, token)
                    remove.assert_called_once_with(self.m.engine, "before_cursor_execute", callbacks[0])
            finally:
                probe.cleanup(state)

    def test_manifest_precedes_every_commit_and_failure_cleanup(self):
        probe = self.probe
        paths = []
        persist = probe.persist_manifest

        def save(state):
            path = persist(state)
            paths.append((path, state))
            return path

        commits = []

        def before_commit(session):
            self.assertTrue(paths)
            path, state = paths[-1]
            self.assertEqual(probe.read_manifest(path), state)
            commits.append(True)

        event.listen(self.m.SessionLocal.class_, "before_commit", before_commit)
        try:
            with patch.object(probe, "persist_manifest", save), \
                    patch.object(probe, "first_purchase_race", side_effect=AssertionError("owned failure")):
                with self.assertRaisesRegex(AssertionError, "owned failure"):
                    probe.execute_fixture("product")
        finally:
            event.remove(self.m.SessionLocal.class_, "before_commit", before_commit)
        self.assertGreaterEqual(len(commits), 2)
        self.assertFalse(os.path.exists(paths[0][0]))
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.get(self.m.Product, paths[0][1]["product"]))

    def test_cleanup_failure_retains_recovery_manifest(self):
        probe = self.probe
        saved = []
        persist = probe.persist_manifest

        def save(state):
            path = persist(state)
            saved.append((path, state))
            return path

        with patch.object(probe, "persist_manifest", save), \
                patch.object(probe, "first_purchase_race", return_value={}), \
                patch.object(probe, "cleanup", side_effect=RuntimeError("cleanup failed")):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                probe.execute_fixture("product")
        path, state = saved[0]
        try:
            self.assertEqual(probe.read_manifest(path), state)
            probe.cleanup(state)
        finally:
            os.unlink(path)

    def test_http_sequential_reservation_history_scopes_and_precise_cleanup(self):
        probe = self.probe
        with self.m.SessionLocal() as db:
            unrelated = (db.get(self.m.Product, "product").name, db.get(self.m.User, "buyer").email)
        for scenario in ("product", "history"):
            with self.subTest(scenario=scenario):
                state = probe.new_state(scenario)
                probe.check_collisions(state)
                manifest = probe.persist_manifest(state)
                try:
                    with probe.catalog(state), probe.observe_saas_calls() as calls:
                        token = probe.prepare(state)
                        if scenario == "history":
                            self.assertTrue(probe.history_checks(state, token)["historical_tenants_unchanged"])
                        else:
                            responses = [probe.post_order(state, token, i) for i in range(2)]
                            probe.verify_orders(state, responses, subscriptions=1)
                    self.assertEqual(calls, [])
                    with self.m.SessionLocal() as db:
                        predicates = probe.scopes(db, state)
                        binding = db.scalar(select(self.m.SaaSCombinationBinding).where(
                            predicates[self.m.SaaSCombinationBinding.__tablename__]))
                        binding_id = binding.id
                        # An actor/request-free audit proves cleanup includes binding target IDs.
                        db.add(self.m.AuditLog(id=state["prefix"] + "-audit", actor="system",
                                              action="probe", target_type="saas_combination", target_id=binding_id))
                        db.commit()
                    with self.m.SessionLocal() as db:
                        probe.validate_ownership(db, state)
                        db.get(self.m.Product, state["product"]).enterprise_id = "other_tenant"
                        with self.assertRaisesRegex(AssertionError, "product ownership"):
                            probe.validate_ownership(db, state)
                        db.rollback()
                finally:
                    probe.cleanup(state)
                    os.unlink(manifest)
                probe.cleanup(state)
                with self.m.SessionLocal() as db:
                    self.assertEqual(db.scalar(select(func.count()).select_from(self.m.AuditLog).where(
                        self.m.AuditLog.target_id == binding_id)), 0)
                    self.assertEqual((db.get(self.m.Product, "product").name,
                                      db.get(self.m.User, "buyer").email), unrelated)

    def test_collision_prevents_manifest_and_cleanup(self):
        probe = self.probe
        state = probe.new_state("product")
        path = probe.persist_manifest(state)
        try:
            probe.prepare(state)
            with patch.object(probe, "new_state", return_value=state), \
                    patch.object(probe, "persist_manifest") as persist, \
                    patch.object(probe, "cleanup") as cleanup:
                with self.assertRaisesRegex(AssertionError, "collision"):
                    probe.execute_fixture("product")
                persist.assert_not_called()
                cleanup.assert_not_called()
        finally:
            probe.cleanup(state)
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
