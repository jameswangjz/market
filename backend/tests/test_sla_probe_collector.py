"""Owned in-memory SQLite only; all HTTP and policy adapters are mocks."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from app.sla_probe_collector import ProbeTarget, install


NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def target(**changes):
    return replace(ProbeTarget("owned-product", "owned-version", "gateway-entry",
        NOW - timedelta(days=1), NOW + timedelta(days=1), "published-event", "service-enrollment",
        True, False, url="https://business.example.invalid/probe", entry_kind="api_gateway",
        source_evidence_id="business-probe-contract", config_version="owned-revision"), **changes)


class ProbeCollectorTests(unittest.TestCase):
    def setUp(self):
        class Base(DeclarativeBase):
            pass
        self.engine = create_engine("sqlite://", poolclass=StaticPool)
        # Explicit SQLite transaction boundaries make SAVEPOINT rollback tests
        # exercise caller ownership rather than sqlite legacy autocommit behavior.
        @event.listens_for(self.engine, "connect")
        def connected(connection, _):
            connection.isolation_level = None
        @event.listens_for(self.engine, "begin")
        def begun(connection):
            connection.exec_driver_sql("BEGIN")
        self.request = Mock(return_value=SimpleNamespace(status_code=204, close=Mock()))
        self.policy = Mock(side_effect=lambda url, **_: url)
        self.ns = {"Base": Base, "now": lambda: NOW, "validated_http_request": self.request,
                   "application_address_policy": {"validate_url": self.policy},
                   "verify_version": Mock(side_effect=AssertionError("must never publish")),
                   "verify_saas_integration_service": Mock(side_effect=AssertionError("must never publish"))}
        self.hooks = install(self.ns)
        self.Slot = self.hooks["Slot"]
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.ns["verify_version"].assert_not_called()
        self.ns["verify_saas_integration_service"].assert_not_called()
        self.engine.dispose()

    def collect(self, targets=None, instant=NOW):
        with self.Session() as db:
            result = self.hooks["collect"](db, targets if targets is not None else [target()], instant=instant)
            db.commit()
            return result

    def rows(self):
        with self.Session() as db:
            return db.scalars(select(self.Slot).order_by(self.Slot.slot)).all()

    def test_install_and_success_safe_transport_contract(self):
        self.assertIs(self.ns["sla_probe_collector"], self.hooks)
        self.assertEqual(self.collect()["healthy"], 1)
        row = self.rows()[0]
        self.assertEqual(row.scope, "business_entry")
        self.assertEqual(row.status_code, 204)
        self.assertIsNotNone(row.completed_at)
        self.assertEqual(len(row.config_digest), 64)
        self.assertNotIn("url", json.loads(row.config_summary_json))
        self.request.assert_called_once_with("HEAD", target().url, timeout=5,
                                             follow_redirects=False, headers={})
        self.policy.assert_called_once()

    def test_real_safe_adapter_entrypoint_used_not_bare_httpx(self):
        from app import safe_upstream
        bounded = AsyncMock(return_value=httpx.Response(204))
        self.ns["validated_http_request"] = Mock(wraps=safe_upstream.validated_http_request)
        forbidden = AssertionError("collector must not use bare httpx")
        with patch.object(safe_upstream, "_validated_http_request_async", bounded), \
             patch.object(httpx, "request", side_effect=forbidden), \
             patch.object(httpx, "get", side_effect=forbidden), \
             patch.object(httpx, "head", side_effect=forbidden), \
             patch.object(httpx, "Client", side_effect=forbidden), \
             patch.object(httpx, "AsyncClient", side_effect=forbidden):
            self.assertEqual(self.collect()["healthy"], 1)
        self.ns["validated_http_request"].assert_called_once_with(
            "HEAD", target().url, timeout=5, follow_redirects=False, headers={})
        bounded.assert_awaited_once_with("HEAD", target().url, 5, False, {})

    def test_same_slot_and_later_slot_idempotency(self):
        self.collect()
        self.assertEqual(self.collect(instant=NOW + timedelta(seconds=299))["duplicates"], 1)
        self.assertEqual(self.collect(instant=NOW + timedelta(seconds=300))["created"], 1)
        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(len(self.rows()), 2)

    def test_same_slot_config_change_preserves_initial_fact(self):
        self.collect(); initial = self.rows()[0].config_digest
        self.collect([target(url="https://changed.example.invalid/business", config_version="next")])
        self.assertEqual(self.rows()[0].config_digest, initial)
        self.assertEqual(self.request.call_count, 1)

    def test_distinct_product_version_entry_keys(self):
        facts = [target(), target(entry_id="second"), target(version_id="another"), target(product_id="other")]
        self.assertEqual(self.collect(facts)["created"], 4)

    def test_conflicting_batch_refused_before_http_or_insert(self):
        with self.assertRaises(ValueError):
            self.collect([target(), target(config_version="conflict")])
        self.assertEqual(self.rows(), [])
        self.request.assert_not_called()

    def test_downlisted_active_sold_contract_stays_in_scope(self):
        result = self.collect([target(currently_published=False, active_sold_contract=True,
                                      contract_evidence_id="owned-active-contract")])
        self.assertEqual(result["healthy"], 1)
        self.assertEqual(self.rows()[0].contract_evidence_id, "owned-active-contract")

    def test_downlisted_no_contract_skipped_unknown_contract_recorded(self):
        self.assertEqual(self.collect([target(currently_published=False)])["skipped"], 1)
        self.assertEqual(self.collect([target(currently_published=False, active_sold_contract=None)])["source_missing"], 1)
        self.request.assert_not_called()

    def test_missing_publication_or_enrollment_never_probed(self):
        for index, changes in enumerate(({"publication_evidence_id": ""}, {"eligibility_evidence_id": ""})):
            self.assertEqual(self.collect([target(entry_id=str(index), **changes)])["source_missing"], 1)
        self.request.assert_not_called()

    def test_service_half_open_and_no_retroactive_slot(self):
        self.assertEqual(self.collect([target(service_from=NOW + timedelta(seconds=1))])["skipped"], 1)
        self.assertEqual(self.collect([target(service_until=NOW)])["skipped"], 1)
        self.assertEqual(self.collect([target(service_from=NOW)])["healthy"], 1)

    def test_management_upstream_and_missing_url_are_not_business_probes(self):
        changes = ({"source_scope": "management"}, {"source_scope": "api_upstream"},
                   {"url": None}, {"source_evidence_id": ""}, {"entry_kind": "unsupported"})
        for index, values in enumerate(changes):
            self.assertEqual(self.collect([target(entry_id=str(index), **values)])["source_missing"], 1)
        self.request.assert_not_called()

    def test_saas_and_authenticated_api_require_explicit_identity_source(self):
        for index, values in enumerate(({"entry_kind": "saas_business"}, {"requires_identity": True})):
            self.assertEqual(self.collect([target(entry_id=str(index), **values)])["source_missing"], 1)
        self.assertTrue(all(r.reason_code == "business_identity_source_missing" and r.completed_at is not None for r in self.rows()))
        self.request.assert_not_called()

    def test_ephemeral_headers_not_persisted(self):
        self.ns["sla_probe_headers"] = Mock(return_value={"Authorization": "Bearer owned-private"})
        self.collect([target(entry_kind="saas_business")])
        row = self.rows()[0]
        self.assertEqual(row.status, "healthy")
        self.assertNotIn("owned-private", row.config_summary_json)
        self.assertNotIn("Authorization", row.config_summary_json)
        self.assertEqual(self.request.call_args.kwargs["headers"], {"Authorization": "Bearer owned-private"})

    def test_http_failure_redirect_and_unknown_attribution(self):
        for index, code in enumerate((302, 401, 503)):
            self.request.return_value.status_code = code
            self.assertEqual(self.collect([target(entry_id=str(index))])["failed"], 1)
        self.assertTrue(all(r.responsibility == "unknown" for r in self.rows()))

    def test_attribution_needs_explicit_evidence(self):
        self.request.return_value.status_code = 503
        self.collect([target(entry_id="without", failure_responsibility="provider")])
        self.collect([target(entry_id="with", failure_responsibility="platform",
                             responsibility_evidence_id="platform-incident")])
        self.assertEqual({r.entry_id: r.responsibility for r in self.rows()},
                         {"without": "unknown", "with": "platform"})

    def test_timeout_network_error_and_adapter_error_sanitized(self):
        for index, (exc, status, reason) in enumerate((
            (httpx.ReadTimeout("private connection detail"), "failed", "probe_timeout"),
            (httpx.ConnectError("private address"), "failed", "connection_failure"),
            (RuntimeError("private token"), "unknown", "probe_adapter_error"))):
            self.request.side_effect = exc
            self.collect([target(entry_id=str(index))])
            row = next(r for r in self.rows() if r.entry_id == str(index))
            self.assertEqual((row.status, row.reason_code), (status, reason))
            self.assertNotIn("private", row.reason_code)

    def test_unsafe_urls_and_policy_rejection_no_http(self):
        for index, url in enumerate(("https://x.invalid/?token=private", "https://u:p@x.invalid/", "https://x.invalid/#x")):
            self.assertEqual(self.collect([target(entry_id=str(index), url=url)])["source_missing"], 1)
        self.policy.side_effect = ValueError("private policy detail")
        self.assertEqual(self.collect([target()])["source_missing"], 1)
        self.request.assert_not_called()

    def test_missing_adapters_and_resolver_are_source_missing(self):
        with self.Session() as db:
            self.assertEqual(self.hooks["run_once"](db)["status"], "source_missing")
            self.ns["sla_probe_targets"] = Mock(return_value=None)
            self.assertEqual(self.hooks["run_once"](db)["status"], "source_missing")
        self.ns.pop("validated_http_request")
        self.assertEqual(self.collect()["source_missing"], 1)

    def test_missing_source_is_not_an_availability_success(self):
        from app.sla_online_facts import ProbeFact, aggregate_probes
        self.collect([target(source_scope="api_upstream")])
        row = self.rows()[0]
        canonical = ProbeFact(row.slot, row.status if row.status in {"healthy", "failed"} else "unknown")
        result = aggregate_probes([canonical], expected_slots=[row.slot], source_available=True)
        self.assertEqual(result.status, "insufficient_data")
        self.assertEqual(result.sample_count, 0)
        self.assertIsNone(result.value)

    def test_run_once_passes_db_and_time_no_commit(self):
        self.ns["sla_probe_targets"] = Mock(return_value=[target()])
        with self.Session() as db:
            self.assertEqual(self.hooks["run_once"](db, instant=NOW)["healthy"], 1)
            self.ns["sla_probe_targets"].assert_called_once_with(db, NOW)
            db.rollback()
        self.assertEqual(self.rows(), [])

    def test_rollback_retry_and_no_automatic_cleanup(self):
        old = NOW - timedelta(days=181)
        self.collect([target(service_from=old - timedelta(days=1))], instant=old)
        self.collect()
        self.assertEqual(len(self.rows()), 2)
        with self.Session() as db:
            self.hooks["collect"](db, [target(entry_id="rolled-back")], instant=NOW)
            db.rollback()
        self.assertEqual(self.collect([target(entry_id="rolled-back")])["healthy"], 1)

    def test_pruning_180_day_floor_boundary_and_caller_rollback(self):
        for age in (181, 180, 179):
            instant = NOW - timedelta(days=age)
            self.collect([target(service_from=instant - timedelta(days=1))], instant=instant)
        with self.Session() as db:
            for retention in (30, 179, True):
                with self.assertRaises(ValueError):
                    self.hooks["prune"](db, as_of=NOW, retention_days=retention)
            self.assertEqual(self.hooks["prune"](db, as_of=NOW), 1)
            db.rollback()
        self.assertEqual(len(self.rows()), 3)
        with self.Session() as db:
            self.assertEqual(self.hooks["prune"](db, as_of=NOW), 1)
            db.commit()
        self.assertEqual(len(self.rows()), 2)

    def test_invalid_method_timeout_and_time_rejected(self):
        for changes in ({"method": "POST"}, {"timeout_seconds": 6}, {"timeout_seconds": 0.01}, {"timeout_seconds": True},
                        {"service_from": NOW.replace(tzinfo=None)}, {"service_until": NOW - timedelta(days=2)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                target(**changes)
        self.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
