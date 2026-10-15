import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app import sla_metrics as metrics
from app.sla_online_facts import (
    ProbeFact, RequestFact, aggregate_deliveries, aggregate_probes,
    aggregate_requests, classify_request, expected_probe_slots, request_decisions,
)


UTC = timezone.utc
START = datetime(2026, 10, 1, tzinfo=UTC)


def request(key="one", **changes):
    return replace(RequestFact(key, "gateway", 200, 10, True, True), **changes)


class RequestFactsTests(unittest.TestCase):
    def test_only_evidenced_caller_errors_excluded(self):
        reasons = {400: "caller_parameters", 422: "caller_parameters",
                   401: "buyer_credentials", 403: "buyer_credentials",
                   404: "undeclared_path", 429: "contract_quota", 499: "client_cancel"}
        for code, reason in reasons.items():
            with self.subTest(code=code):
                fact = request(status_code=code, reason=reason, responsibility="caller")
                self.assertTrue(classify_request(fact).included)
                self.assertFalse(classify_request(replace(fact, trusted_evidence=True)).included)
                self.assertTrue(classify_request(replace(fact, trusted_evidence=True,
                                                         responsibility="unknown")).included)

    def test_platform_failures_override_failed_auth_or_missing_route(self):
        for code, reason in ((401, "forward_auth_failure"), (403, "forward_auth_failure"),
                             (404, "missing_published_route"), (429, "incorrect_rate_limit"),
                             (422, "valid_input_failure"), (504, "timeout")):
            with self.subTest(code=code):
                row = classify_request(request(status_code=code, reason=reason,
                    responsibility="platform", trusted_evidence=True,
                    authorized=False, published_route=False))
                self.assertTrue(row.included)
                self.assertTrue(row.attributable_failure)

    def test_unknown_attribution_cannot_be_zero_errors(self):
        rows = [request(str(i)) for i in range(100)] + [request("unknown", status_code=499)]
        result = aggregate_requests(rows, source_available=True)
        self.assertEqual(result.unresolved_requests, 1)
        self.assertIsNone(result.error_rate.value)
        self.assertEqual(result.error_rate.status, "insufficient_data")
        self.assertEqual(result.error_rate.sample_count, 101)

    def test_trusted_traffic_only_not_client_headers(self):
        for traffic in ("probe", "isolated_test", "internal"):
            fact = request(traffic=traffic)
            self.assertTrue(classify_request(fact).included)
            self.assertFalse(classify_request(replace(fact, trusted_evidence=True)).included)

    def test_maintenance_needs_contract_and_prior_approval(self):
        fact = request(status_code=503, trusted_evidence=True, responsibility="provider")
        self.assertTrue(classify_request(fact).attributable_failure)
        self.assertTrue(classify_request(replace(fact, maintenance_preapproved=True)).included)
        self.assertFalse(classify_request(replace(fact, maintenance_preapproved=True,
                                                 maintenance_contract_exclusion=True)).included)

    def test_gateway_dedup_and_upstream_attempts(self):
        primary = request()
        resource = replace(primary, source="resource", duration_ms=1)
        attempt = replace(primary, terminal=False, status_code=503)
        decisions = request_decisions([resource, primary, primary, attempt, attempt])
        counted = [item for item in decisions if item.included]
        self.assertEqual(len(counted), 1)
        self.assertEqual(counted[0].fact.duration_ms, 10)
        with self.assertRaises(ValueError):
            request_decisions([primary, replace(primary, status_code=500)])

    def test_resource_without_gateway_is_insufficient(self):
        result = aggregate_requests([request(str(i), source="resource") for i in range(100)],
                                    source_available=True)
        self.assertEqual(result.error_rate.status, "insufficient_data")
        self.assertEqual(result.latency_p95.status, "insufficient_data")

    def test_redirect_requires_contract(self):
        self.assertIsNone(classify_request(request(status_code=302)).attributable_failure)
        self.assertFalse(classify_request(request(status_code=302, redirect_allowed=True)).attributable_failure)

    def test_p95_includes_failed_duration_and_completeness_boundaries(self):
        rows = [request(str(i), duration_ms=i) for i in range(99)]
        rows.append(request("failed", status_code=500, duration_ms=9999,
                            responsibility="provider", trusted_evidence=True))
        complete = aggregate_requests(rows, source_available=True)
        self.assertEqual(complete.latency_p95.value, 94)
        self.assertEqual(complete.error_rate.value, 1)
        self.assertEqual(complete.latency_p95.sample_count, 100)
        enough = aggregate_requests(rows + [request("missing"+str(i), duration_ms=None)
                                           for i in range(5)], source_available=True)
        self.assertNotEqual(enough.latency_p95.status, "insufficient_data")
        insufficient = aggregate_requests(rows + [request("missing"+str(i), duration_ms=None)
                                                 for i in range(6)], source_available=True)
        self.assertEqual(insufficient.latency_p95.status, "insufficient_data")
        self.assertEqual(insufficient.latency_p95.sample_count, 100)

    def test_minimum_samples_and_missing_source(self):
        for rows, available in (([], True), ([request(str(i)) for i in range(99)], True),
                                ([request(str(i)) for i in range(100)], False)):
            result = aggregate_requests(rows, source_available=available)
            self.assertEqual(result.error_rate.status, "insufficient_data")
            self.assertEqual(result.latency_p95.status, "insufficient_data")
        self.assertIsNone(aggregate_requests([], source_available=False).error_rate.value)

    def test_invalid_duration_and_flags_rejected(self):
        for change in ({"duration_ms": -1}, {"duration_ms": float("nan")},
                       {"authorized": 1}, {"terminal": "false"}, {"status_code": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                request(**change)

    def test_p95_exact_95_percent_completeness(self):
        facts = [request(str(i), duration_ms=i) for i in range(190)]
        facts += [request("missing"+str(i), duration_ms=None) for i in range(10)]
        result = aggregate_requests(facts, source_available=True)
        self.assertEqual(result.latency_p95.completeness_percent, 95)
        self.assertNotEqual(result.latency_p95.status, "insufficient_data")
        result = aggregate_requests(facts + [request("extra-missing", duration_ms=None)], source_available=True)
        self.assertEqual(result.latency_p95.status, "insufficient_data")

    def test_source_string_not_truthy_success(self):
        with self.assertRaises(ValueError):
            aggregate_requests([], source_available="false")


class ProbeAndDeliveryTests(unittest.TestCase):
    def test_half_open_expected_slots_with_partial_service_window(self):
        slots = expected_probe_slots(START + timedelta(seconds=1), START + timedelta(minutes=15))
        self.assertEqual(len(slots), 2)
        self.assertEqual(list(expected_probe_slots(START, START)), [])
        with self.assertRaises(ValueError):
            expected_probe_slots(START.replace(tzinfo=None), START)

    def test_availability_100_samples_and_90_percent_coverage(self):
        rows = [ProbeFact(i, "healthy") for i in range(100)]
        self.assertEqual(aggregate_probes(rows, expected_slots=range(111), source_available=True).status, "met")
        low = aggregate_probes(rows, expected_slots=range(112), source_available=True)
        self.assertEqual(low.status, "insufficient_data")
        self.assertEqual(low.value, 100)
        self.assertEqual(aggregate_probes(rows[:99], expected_slots=range(99), source_available=True).status,
                         "insufficient_data")

    def test_missing_claimed_probe_never_success_or_full_coverage(self):
        rows = [ProbeFact(i, "healthy") for i in range(100)] + [ProbeFact(100, "claimed")]
        result = aggregate_probes(rows, expected_slots=range(120), source_available=True)
        self.assertEqual(result.sample_count, 100)
        self.assertLess(result.coverage_percent, 90)
        self.assertEqual(result.status, "insufficient_data")
        self.assertIsNone(aggregate_probes([], expected_slots=range(100), source_available=False).value)
        with self.assertRaises(ValueError):
            aggregate_probes([ProbeFact(1, "healthy"), ProbeFact(1, "failed")],
                             expected_slots=range(10), source_available=True)

    def test_exact_coverage_boundary_and_failed_probe(self):
        rows = [ProbeFact(i, "healthy" if i < 107 else "failed") for i in range(108)]
        result = aggregate_probes(rows, expected_slots=range(120), source_available=True)
        self.assertEqual(result.coverage_percent, 90)
        self.assertEqual(result.sample_count, 108)
        self.assertLess(result.value, 100)
        self.assertNotEqual(result.status, "insufficient_data")
        self.assertEqual(aggregate_probes(rows[:-1], expected_slots=range(120),
                                         source_available=True).status, "insufficient_data")

    def test_delivery_deadline_month_not_success_month_and_failure_denominator(self):
        deadline = datetime(2026, 10, 31, 15, 59, tzinfo=UTC)
        facts = [metrics.DeliveryFact(str(i), "file", START, deadline,
                                     deadline if i < 9 else None) for i in range(10)]
        # Beijing Nov 1 00:00 belongs to November, even if completed in October.
        facts.append(metrics.DeliveryFact("next", "api", START, deadline + timedelta(minutes=1), START))
        facts.append(metrics.DeliveryFact("offline", "training", START, deadline, START))
        result = aggregate_deliveries(facts + facts[:1], missing_frozen_facts=0,
            source_available=True, year=2026, month=10, as_of=deadline + timedelta(days=1))
        self.assertEqual(result.sample_count, 10)
        self.assertEqual(result.value, 90)
        self.assertEqual(result.status, "warning")
        self.assertEqual(aggregate_deliveries(facts[:9], missing_frozen_facts=0,
            source_available=True, year=2026, month=10, as_of=deadline).status, "insufficient_data")

    def test_missing_frozen_facts_or_source_prevents_compliance(self):
        facts = [metrics.DeliveryFact(str(i), "tenant_access", START,
                                     START + timedelta(minutes=10), START) for i in range(10)]
        for missing, source in ((1, True), (0, False)):
            result = aggregate_deliveries(facts, missing_frozen_facts=missing,
                source_available=source, year=2026, month=10, as_of=START + timedelta(days=1))
            self.assertEqual(result.status, "insufficient_data")
        future = aggregate_deliveries(facts, missing_frozen_facts=0, source_available=True,
            year=2026, month=10, as_of=START)
        self.assertEqual(future.sample_count, 0)


if __name__ == "__main__":
    unittest.main()
