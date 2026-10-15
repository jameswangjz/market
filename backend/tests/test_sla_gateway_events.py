"""Real receiver and owned in-memory SQL tables; no deployed app or network."""
from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import String, create_engine, event, func, select
from sqlalchemy.orm import DeclarativeBase, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from app.sla_gateway_events import ENDPOINT, MAX_BYTES, install


START = datetime(2026, 10, 15, tzinfo=timezone.utc)
KEY = "owned-telemetry-secret-" + "x" * 40


def fact(index=1, **changes):
    identity = "gw_" + format(index, "032x")
    return {"event_id": identity, "request_id": identity, "product_id": "owned-product",
            "version_code": "v1", "entry_id": "owned-route-key", "route_id": "owned-route-key",
            "credential_id": "owned-credential", "completed_at_ms": int(START.timestamp() * 1000) + index,
            "status_code": 200, "duration_ms": 10, "authorized": True, "published_route": True,
            "sequence": index + 1, **changes}


class GatewayEventsTests(unittest.TestCase):
    def setUp(self):
        class Base(DeclarativeBase):
            pass
        class Route(Base):
            __tablename__ = "owned_routes"
            id = mapped_column(String, primary_key=True)
            route_key = mapped_column(String)
            product_id = mapped_column(String)
            version = mapped_column(String)
        class Version(Base):
            __tablename__ = "owned_versions"
            id = mapped_column(String, primary_key=True)
            product_id = mapped_column(String)
            version_code = mapped_column(String)
        class Credential(Base):
            __tablename__ = "owned_credentials"
            id = mapped_column(String, primary_key=True)
            route_id = mapped_column(String)
            enterprise_id = mapped_column(String)
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        @event.listens_for(self.engine, "connect")
        def connected(connection, _):
            connection.isolation_level = None
        @event.listens_for(self.engine, "begin")
        def begun(connection):
            connection.exec_driver_sql("BEGIN")
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.ns = dict(Base=Base, app=FastAPI(), SessionLocal=self.Session, ApiGatewayRoute=Route,
                       ProductReleaseVersion=Version, ApiCredential=Credential)
        self.hooks = install(self.ns)
        self.Model, self.Window = self.hooks["Model"], self.hooks["SourceWindow"]
        Base.metadata.create_all(self.engine)
        with self.Session.begin() as db:
            db.add(Route(id="owned-route", route_key="owned-route-key", product_id="owned-product", version="v1"))
            db.add(Version(id="owned-version", product_id="owned-product", version_code="v1"))
            db.add(Credential(id="owned-credential", route_id="owned-route", enterprise_id="owned-enterprise"))
        self.secret = patch.dict(os.environ, {"SLA_GATEWAY_INGEST_SECRET": KEY})
        self.secret.start()
        self.client = TestClient(self.ns["app"])

    def tearDown(self):
        self.client.close(); self.secret.stop(); self.engine.dispose()

    def post(self, events=None, **kwargs):
        return self.client.post(ENDPOINT, json={"events": events if events is not None else [fact()]},
                                headers={"X-SLA-Gateway-Key": KEY}, **kwargs)

    def count(self):
        with self.Session() as db:
            return db.scalar(select(func.count()).select_from(self.Model))

    def aggregate(self, **changes):
        with self.Session() as db:
            return self.hooks["aggregate"](db, product_id="owned-product", version_id="owned-version",
                entry_id="owned-route-key", start=START, end=START + timedelta(minutes=5), **changes)

    def seal_owned_window_for_unit_test(self):
        self.ns["sla_gateway_expected_producers"] = lambda db, start, end: ["apisix"]
        self.assertEqual(self.seal(self.count()).status_code, 200)

    def seal(self, count, **changes):
        payload = {"producer_id": "apisix", "window_slot": int(START.timestamp()) // 300,
                   "expected_count": count, "last_sequence": count,
                   "producer_started_at_ms": int(START.timestamp() * 1000),
                   "closed_at_ms": int((START + timedelta(minutes=5)).timestamp() * 1000), **changes}
        return self.client.post(ENDPOINT, json={"events": [], "window_seals": [payload]},
                                headers={"X-SLA-Gateway-Key": KEY})

    def test_missing_wrong_or_admin_jwt_auth_cannot_write(self):
        for headers in ({}, {"X-SLA-Gateway-Key": "wrong"}, {"Authorization": "Bearer admin"}):
            r = self.client.post(ENDPOINT, json={"events": [fact()]}, headers=headers)
            self.assertEqual(r.status_code, 401)
        self.assertEqual(self.count(), 0)
        with patch.dict(os.environ, {"SLA_GATEWAY_INGEST_SECRET": ""}):
            self.assertEqual(self.post().status_code, 503)

    def test_internal_receiver_hidden_from_openapi(self):
        self.assertNotIn(ENDPOINT, self.ns["app"].openapi()["paths"])

    def test_terminal_retries_one_fact_and_server_scope_mapping(self):
        body = [fact(upstream_statuses=[502, 502, 200])]
        self.assertEqual(self.post(body).json(), {"accepted": 1, "duplicates": 0})
        self.assertEqual(self.post(body).json(), {"accepted": 0, "duplicates": 1})
        with self.Session() as db:
            row = db.scalar(select(self.Model))
            self.assertEqual((row.version_id, row.enterprise_id), ("owned-version", "owned-enterprise"))
            self.assertTrue(row.scope_resolved)
            self.assertEqual(json.loads(row.upstream_statuses_json), [502, 502, 200])
            self.assertFalse(db.get(self.Window, ("apisix", int(START.timestamp()) // 300)).complete)
        self.assertEqual(self.count(), 1)

    def test_changed_same_event_conflict_and_atomic_batch(self):
        self.post()
        self.assertEqual(self.post([fact(2), fact(status_code=500)]).status_code, 409)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.post([fact(3), fact(3, duration_ms=20)]).status_code, 409)
        self.assertEqual(self.count(), 1)

    def test_caller_transaction_rollback_not_hidden_commit(self):
        with self.Session() as db:
            self.hooks["ingest"](db, [fact()]); db.rollback()
        self.assertEqual(self.count(), 0)

    def test_caps_and_unknown_sensitive_fields_rejected_without_echo(self):
        self.assertEqual(self.post([fact(i) for i in range(101)]).status_code, 422)
        for extra in ({"query": "private"}, {"headers": {"Authorization": "private"}},
                      {"body": "private"}, {"secret": "private"}):
            response = self.post([fact(**extra)])
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("private", response.text)
        r = self.client.post(ENDPOINT, content=b"x" * (MAX_BYTES + 1),
            headers={"X-SLA-Gateway-Key": KEY, "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.count(), 0)

    def test_stream_cap_without_content_length(self):
        r = self.client.post(ENDPOINT, content=iter([b"x" * 32768] * 3),
            headers={"X-SLA-Gateway-Key": KEY, "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)

    def test_bad_schema_identity_duplicate_keys_and_nan_rejected(self):
        for changes in ({"request_id": "caller-id"}, {"event_id": "caller-id"}, {"duration_ms": True},
                        {"authorized": "true"}, {"status_code": True}, {"duration_ms": -1},
                        {"traffic": "probe"}, {"reason": "buyer_credentials"},
                        {"responsibility": "provider"}):
            self.assertEqual(self.post([fact(**changes)]).status_code, 422)
        for raw in ('{"events":[],"events":[]}', '{"events":[' + json.dumps(fact(duration_ms=float("nan"))) + ']}'):
            self.assertEqual(self.client.post(ENDPOINT, content=raw,
                headers={"X-SLA-Gateway-Key": KEY, "Content-Type": "application/json"}).status_code, 422)

    def test_receipts_never_claim_complete_even_with_100_good_requests(self):
        self.assertEqual(self.post([fact(i) for i in range(100)]).status_code, 200)
        result = self.aggregate(source_available=True)
        self.assertFalse(result.source_available)
        self.assertEqual(result.error_rate.status, "insufficient_data")
        self.assertEqual(result.latency_p95.status, "insufficient_data")
        self.ns["sla_gateway_expected_producers"] = lambda *args: ["apisix"]
        self.assertFalse(self.aggregate(source_available=True).source_available)

    def test_failure_counts_persist_monotonically_replay_safe_cannot_seal(self):
        status = {"producer_id": "apisix", "window_slot": int(START.timestamp()) // 300, "failure_count": 3}
        for count in (3, 1, 3):
            status["failure_count"] = count
            r = self.client.post(ENDPOINT, json={"events": [fact()], "source_statuses": [status]},
                                  headers={"X-SLA-Gateway-Key": KEY})
            self.assertEqual(r.status_code, 200)
        with self.Session() as db:
            window = db.get(self.Window, ("apisix", status["window_slot"]))
            self.assertEqual((window.failure_count, window.received_count, window.complete), (3, 1, False))
        status["complete"] = True
        self.assertEqual(self.client.post(ENDPOINT, json={"events": [fact()], "source_statuses": [status]},
            headers={"X-SLA-Gateway-Key": KEY}).status_code, 422)

    def test_fourxx_unknown_attribution_insufficient_not_buyer_exclusion(self):
        rows = [fact(i) for i in range(100)]
        rows[0] = fact(0, status_code=400)
        self.post(rows); self.seal_owned_window_for_unit_test()
        result = self.aggregate(source_available=True)
        self.assertEqual(result.unresolved_requests, 1)
        self.assertEqual(result.error_rate.status, "insufficient_data")

    def test_proven_exclusion_platform_failure_and_failed_latency(self):
        rows = [fact(i, duration_ms=i) for i in range(100)]
        rows.append(fact(100, status_code=429, reason="contract_quota", responsibility="caller", evidence_id="quota-check"))
        rows[0] = fact(0, status_code=401, duration_ms=9000, authorized=False, reason="forward_auth_failure",
                       responsibility="platform", evidence_id="forward-token-failure")
        self.post(rows[:100]); self.post(rows[100:]); self.seal_owned_window_for_unit_test()
        result = self.aggregate(source_available=True)
        self.assertEqual(result.error_rate.sample_count, 100)
        self.assertEqual(result.error_rate.value, 1)
        self.assertEqual(result.latency_p95.sample_count, 100)
        self.assertTrue(any(d.fact.duration_ms == 9000 and d.included for d in result.decisions))

    def test_undefined_scope_or_missing_producer_window_forces_insufficient(self):
        self.post([fact(i) for i in range(100)])
        self.post([fact(101, version_id="wrong-version")])
        self.seal_owned_window_for_unit_test()
        self.assertFalse(self.aggregate(source_available=True).source_available)

    def test_half_open_period_and_enterprise_filter(self):
        self.post([fact(0), fact(2, completed_at_ms=int((START + timedelta(minutes=5)).timestamp() * 1000))])
        self.assertEqual(len(self.aggregate().decisions), 1)
        self.assertEqual(len(self.aggregate(enterprise_id="foreign").decisions), 0)
        self.assertEqual(len(self.aggregate(credential_id="foreign").decisions), 0)

    def test_closed_window_count_and_contiguous_sequence_really_enable_metrics(self):
        self.post([fact(i) for i in range(100)])
        self.assertFalse(self.aggregate(source_available=True).source_available)
        self.seal_owned_window_for_unit_test()
        result = self.aggregate(source_available=True)
        self.assertTrue(result.source_available)
        self.assertEqual(result.error_rate.status, "met")
        self.assertEqual(result.latency_p95.status, "met")
        self.assertEqual(self.seal(100).status_code, 200)
        self.assertEqual(self.seal(101).status_code, 409)

    def test_seal_before_delivery_remains_gap_until_exact_replay_finishes(self):
        self.assertEqual(self.seal(3).status_code, 200)
        self.post([fact(0), fact(2)])
        self.ns["sla_gateway_expected_producers"] = lambda *args: ["apisix"]
        self.assertFalse(self.aggregate(source_available=True).source_available)
        self.post([fact(1)])
        self.assertTrue(self.aggregate(source_available=True).source_available)
        self.post([fact(1)])
        with self.Session() as db:
            window = db.get(self.Window, ("apisix", int(START.timestamp()) // 300))
            self.assertEqual((window.received_count, window.expected_count, window.complete), (3, 3, True))

    def test_sequence_collision_is_409_and_atomic(self):
        self.post([fact(0)])
        self.assertEqual(self.post([fact(2), fact(100, sequence=1)]).status_code, 409)
        self.assertEqual(self.count(), 1)

    def test_partial_start_missing_roster_and_missing_worker_stay_insufficient(self):
        self.post([fact(i) for i in range(100)])
        self.assertEqual(self.seal(100, producer_started_at_ms=int(START.timestamp() * 1000) + 1).status_code, 200)
        self.ns["sla_gateway_expected_producers"] = lambda *args: ["apisix"]
        self.assertFalse(self.aggregate(source_available=True).source_available)
        # A second fully closed producer cannot substitute for the missing one.
        with self.Session.begin() as db:
            window = db.get(self.Window, ("apisix", int(START.timestamp()) // 300))
            window.producer_started_at_ms = int(START.timestamp() * 1000)
            window.complete = True
        self.ns["sla_gateway_expected_producers"] = lambda *args: ["apisix", "absent-worker"]
        self.assertFalse(self.aggregate(source_available=True).source_available)

    def test_zero_window_and_early_close_validation(self):
        self.assertEqual(self.seal(0, closed_at_ms=int(START.timestamp() * 1000)).status_code, 422)
        self.assertEqual(self.seal(0).status_code, 200)
        self.ns["sla_gateway_expected_producers"] = lambda *args: ["apisix"]
        result = self.aggregate(source_available=True)
        self.assertTrue(result.source_available)
        self.assertEqual(result.error_rate.status, "insufficient_data")
        self.assertIsNone(result.error_rate.value)

    def test_default_registry_partial_start_and_retired_worker(self):
        from unittest.mock import Mock
        born = int(START.timestamp() * 1000) + 1
        self.post([fact(i) for i in range(1, 101)])
        # Reindex this owned fixture to a complete producer sequence.
        with self.Session.begin() as db:
            for row in db.scalars(select(self.Model).order_by(self.Model.sequence)):
                row.sequence -= 1
        self.seal(100, producer_started_at_ms=born)
        client = Mock()
        client.hgetall.return_value = {
            "apisix": str(born),
            "retired": json.dumps({"started_at_ms": born - 600000,
                "stopped_at_ms": born - 1, "retirement_evidence_id": "owned-retirement"})}
        with patch("redis.Redis.from_url", return_value=client):
            with self.Session() as db:
                result = self.hooks["aggregate"](db, product_id="owned-product", version_id="owned-version",
                    entry_id="owned-route-key", start=START + timedelta(milliseconds=1),
                    end=START + timedelta(minutes=5), source_available=True)
            self.assertTrue(result.source_available)
            self.assertFalse(self.aggregate(source_available=True).source_available)
            client.hgetall.return_value["unknown-worker"] = str(born)
            self.assertFalse(self.aggregate(source_available=True).source_available)

    def test_proven_native_401_does_not_poison_business_source(self):
        self.post([fact(i) for i in range(100)])
        self.post([fact(100, status_code=401, authorized=False,
            credential_id="not-a-valid-credential", reason="buyer_credentials",
            responsibility="caller", evidence_id="key-auth:owned-proof")])
        self.seal_owned_window_for_unit_test()
        self.assertTrue(self.aggregate(source_available=True).source_available)
        with self.Session.begin() as db:
            db.get(self.Model, fact(100)["event_id"]).evidence_id = ""
        self.assertFalse(self.aggregate(source_available=True).source_available)

    def test_trusted_unknown_scope_probe_does_not_poison_business_source(self):
        self.post([fact(i) for i in range(100)])
        self.post([fact(100, traffic="probe", traffic_evidence_id="signed-probe",
                        credential_id="dedicated-not-api-credential")])
        self.seal_owned_window_for_unit_test()
        self.assertTrue(self.aggregate(source_available=True).source_available)
        with self.Session.begin() as db:
            row = db.get(self.Model, fact(100)["event_id"])
            row.traffic_evidence_id = ""
        self.assertFalse(self.aggregate(source_available=True).source_available)


if __name__ == "__main__":
    unittest.main()
