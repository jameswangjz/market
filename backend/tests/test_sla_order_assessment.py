"""Pure assessments using the actual enterprise_sla promise shape, no ORM/DB."""
import copy
from dataclasses import replace
import hashlib
import json
import unittest
from unittest.mock import patch

from app import enterprise_sla
from app.sla_metrics import MetricResult
from app.sla_order_assessment import RULE_KEYS, assess_order_metric


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def seal(snapshot):
    snapshot["calendar_content_hash"] = digest(snapshot["calendar"])
    snapshot["rule_version"] = digest({key: snapshot[key] for key in RULE_KEYS})
    return snapshot


def frozen(tier="standard"):
    # Use production's metric/calendar builder; reproduce freeze_sla_snapshot's
    # detached envelope and hashing, without importing main or opening a session.
    metrics, calendar = enterprise_sla._promise(tier)
    rules = dict(matrix_version=enterprise_sla.MATRIX_VERSION, service_level_code=tier,
                 calendar=calendar, metrics=metrics, legacy_level_config=None)
    snapshot = dict(schema_version=1, **rules, contract_rule_version=digest(rules),
                    enterprise_id="owned-buyer", service_level_id=None, assignment=None,
                    frozen_at="2026-10-15T00:00:00+00:00")
    return seal(snapshot)


def assess(snapshot, metric="availability", measurement=None, **kwargs):
    measurement = measurement if measurement is not None else MetricResult(97, "met", 100, coverage_percent=90)
    return assess_order_metric(snapshot, metric, measurement,
                               buyer_enterprise_id="owned-buyer", source_available=True, **kwargs)


class FrozenOrderAssessmentTests(unittest.TestCase):
    def test_shared_measurement_different_frozen_orders(self):
        shared = MetricResult(97, "met", 100, coverage_percent=90)
        standard = assess(frozen(), measurement=shared)
        strategic = assess(frozen("strategic"), measurement=shared)
        self.assertEqual(standard.status, "met")
        self.assertEqual(strategic.status, "breached")
        self.assertEqual(standard.value, strategic.value)
        self.assertEqual(standard.target, 90)
        self.assertEqual(strategic.target, 98)

    def test_current_assignment_and_live_matrix_not_consulted(self):
        snapshot = frozen()
        before = assess(snapshot)
        with patch.object(enterprise_sla, "_promise", side_effect=AssertionError("live tier lookup")), \
             patch("app.sla_metrics.threshold", side_effect=AssertionError("default matrix lookup")):
            self.assertEqual(assess(snapshot), before)

    def test_custom_frozen_threshold_not_tier_defaults(self):
        snapshot = frozen()
        snapshot["metrics"]["availability"].update(target=99, warning=99.5)
        seal(snapshot)
        result = assess(snapshot)
        self.assertEqual(result.frozen_tier, "standard")
        self.assertEqual(result.target, 99)
        self.assertEqual(result.status, "breached")

    def test_upstream_breached_status_reclassified_not_preserved(self):
        snapshot = frozen()
        snapshot["metrics"]["latency_p95"].update(target=8000, warning=7200)
        seal(snapshot)
        result = assess(snapshot, "latency_p95", MetricResult(7000, "breached", 100, completeness_percent=95))
        self.assertEqual(result.status, "met")

    def test_error_and_delivery_use_frozen_rules(self):
        self.assertEqual(assess(frozen("enterprise"), "request_error_rate",
                               MetricResult(6, "met", 100)).status, "breached")
        self.assertEqual(assess(frozen("strategic"), "delivery_timeliness",
                               MetricResult(96, "met", 10)).status, "breached")

    def test_lower_target_and_warning_boundaries(self):
        snapshot = frozen("strategic")
        for value, status in ((97.9, "breached"), (98, "warning"), (98.4, "warning"), (98.5, "met")):
            with self.subTest(value=value):
                self.assertEqual(assess(snapshot, measurement=MetricResult(value, "met", 100,
                    coverage_percent=90)).status, status)

    def test_upper_target_and_warning_boundaries(self):
        snapshot = frozen("enterprise")
        for value, status in ((3.9, "met"), (4, "warning"), (5, "warning"), (5.1, "breached")):
            with self.subTest(value=value):
                self.assertEqual(assess(snapshot, "request_error_rate", MetricResult(value, "met", 100)).status, status)

    def test_missing_snapshot_never_defaults_standard(self):
        for snapshot in (None, "", {}):
            with self.subTest(snapshot=snapshot):
                result = assess(snapshot)
                self.assertEqual(result.status, "insufficient_data")
                self.assertIsNone(result.target)
                self.assertIsNone(result.frozen_tier)

    def test_invalid_snapshot_shapes_hash_and_enterprise(self):
        cases = ["invalid-json", "[]", {"schema_version": 1}]
        for field, value in (("schema_version", True), ("rule_version", "bad"),
                             ("contract_rule_version", ""), ("calendar_content_hash", "bad"),
                             ("enterprise_id", "other-enterprise"), ("frozen_at", "2026-10-15T00:00:00")):
            snapshot = frozen(); snapshot[field] = value; cases.append(snapshot)
        snapshot = frozen(); snapshot["metrics"]["availability"]["target"] = 99; cases.append(snapshot)
        for snapshot in cases:
            with self.subTest(snapshot_type=type(snapshot).__name__):
                self.assertEqual(assess(snapshot).status, "insufficient_data")

    def test_hashed_but_illegal_thresholds_units_and_clock(self):
        for changes in ({"target": 101}, {"target": True}, {"warning": 80},
                        {"target": -1}, {"target": float("nan")}, {"direction": "upper"},
                        {"unit": "milliseconds"}, {"clock": "support"}):
            with self.subTest(changes=changes):
                snapshot = frozen(); snapshot["metrics"]["availability"].update(changes); seal(snapshot)
                self.assertEqual(assess(snapshot).status, "insufficient_data")
        snapshot = frozen(); snapshot["metrics"]["latency_p95"].update(target=0, warning=0); seal(snapshot)
        self.assertEqual(assess(snapshot, "latency_p95").status, "insufficient_data")

    def test_source_missing_and_upstream_insufficient(self):
        snapshot = frozen(); measured = MetricResult(100, "met", 100, coverage_percent=100)
        for available in (False, None, "true", 1):
            self.assertEqual(assess_order_metric(snapshot, "availability", measured,
                buyer_enterprise_id="owned-buyer", source_available=available).status, "insufficient_data")
        result = assess(snapshot, measurement=replace(measured, status="insufficient_data"))
        self.assertEqual(result.reason, "upstream_insufficient")

    def test_sample_gates_enforced_even_if_input_says_met(self):
        for metric, measured in (("availability", MetricResult(100, "met", 99, coverage_percent=100)),
            ("availability", MetricResult(100, "met", 100, coverage_percent=89.99)),
            ("availability", MetricResult(100, "met", 100)),
            ("request_error_rate", MetricResult(0, "met", 99)),
            ("latency_p95", MetricResult(1, "met", 99, completeness_percent=100)),
            ("latency_p95", MetricResult(1, "met", 100, completeness_percent=94.99)),
            ("delivery_timeliness", MetricResult(100, "met", 9))):
            with self.subTest(metric=metric):
                self.assertEqual(assess(frozen(), metric, measured).status, "insufficient_data")

    def test_invalid_measurements_rejected(self):
        for measured in (MetricResult(None, "met", 100), MetricResult(float("inf"), "met", 100),
                         MetricResult(101, "met", 100), MetricResult(99, "met", True),
                         MetricResult(99, "invented", 100), MetricResult(10**1000, "met", 100)):
            with self.subTest(value_type=type(measured.value).__name__):
                self.assertEqual(assess(frozen(), measurement=measured).status, "insufficient_data")

    def test_json_input_detached_and_duplicate_fields_rejected(self):
        snapshot = frozen(); original = copy.deepcopy(snapshot)
        result = assess(json.dumps(snapshot, ensure_ascii=False))
        self.assertEqual(result.status, "met")
        self.assertEqual(snapshot, original)
        self.assertEqual(result.rule_version, snapshot["rule_version"])
        duplicate = json.dumps(snapshot)[:-1] + ',"schema_version":1}'
        self.assertEqual(assess(duplicate).status, "insufficient_data")

    def test_enriched_calendar_preserves_original_contract_version(self):
        snapshot = frozen()
        contract_version = snapshot["contract_rule_version"]
        snapshot["calendar"].update(covered_years=[2026], holidays=["2026-10-01"],
                                    holiday_calendar_status="available")
        seal(snapshot)
        result = assess(snapshot)
        self.assertEqual(result.status, "met")
        self.assertEqual(result.contract_rule_version, contract_version)
        self.assertNotEqual(result.rule_version, contract_version)

    def test_missing_measurement_or_metric_does_not_create_value(self):
        result = assess_order_metric(frozen(), "availability", None,
            buyer_enterprise_id="owned-buyer", source_available=True)
        self.assertEqual(result.status, "insufficient_data")
        self.assertIsNone(result.value)
        snapshot = frozen(); del snapshot["metrics"]["availability"]; seal(snapshot)
        self.assertEqual(assess(snapshot).reason, "invalid_snapshot")
        self.assertEqual(assess(frozen(), "unknown").reason, "unsupported_metric")


if __name__ == "__main__":
    unittest.main()
