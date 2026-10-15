"""Pure standard-library SLA tests; never import app.main or access services."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from backend.app.sla_metrics import (
    DeliveryFact, Threshold, availability, delivery_timeliness, latency_p95,
    month_bounds, request_error_rate, threshold,
)


class SLAMetricsTests(unittest.TestCase):
    def test_no_data_does_not_become_full_availability_or_zero_latency(self):
        results = (availability(0, 0, 100), availability(0, 0, 0),
                   request_error_rate(0, 0), latency_p95([]), latency_p95([None] * 100))
        for result in results:
            self.assertIsNone(result.value)
            self.assertEqual(result.status, "insufficient_data")

    def test_availability_sample_and_coverage_boundaries(self):
        self.assertEqual(availability(99, 99, 100).status, "insufficient_data")
        self.assertEqual(availability(100, 100, 111).status, "met")
        self.assertEqual(availability(100, 100, 112).status, "insufficient_data")
        exact = availability(180, 180, 200)
        self.assertEqual(exact.coverage_percent, 90)
        self.assertEqual(exact.status, "met")
        self.assertEqual(availability(179, 179, 200).status, "insufficient_data")
        failure = availability(0, 99, 100)
        self.assertEqual(failure.value, 0)
        self.assertEqual(failure.status, "insufficient_data")

    def test_error_rate_sample_boundary_and_observed_errors(self):
        insufficient = request_error_rate(99, 99)
        self.assertEqual(insufficient.value, 100)
        self.assertEqual(insufficient.status, "insufficient_data")
        self.assertEqual(request_error_rate(11, 100).status, "breached")
        self.assertEqual(request_error_rate(8, 100).status, "warning")
        self.assertEqual(request_error_rate(7, 100).status, "met")

    def test_thresholds_all_tiers_and_breach_precedence(self):
        for tier, lower, low_warning, upper, up_warning in (
                ("standard", 90, 92, 10, 8), ("enterprise", 95, 96, 5, 4),
                ("strategic", 98, 98.4, 3, 2.4)):
            for metric in ("availability", "delivery"):
                rule = threshold(metric, tier)
                self.assertEqual((rule.target, rule.warning), (lower, low_warning))
                for value, status in ((lower - .01, "breached"), (lower, "warning"),
                                      (low_warning, "warning"), (low_warning + .01, "met")):
                    self.assertEqual(rule.classify(value, sufficient=True), status)
            rule = threshold("errors", tier)
            self.assertEqual((rule.target, rule.warning), (upper, up_warning))
            for value, status in ((upper + .01, "breached"), (upper, "warning"),
                                  (up_warning, "warning"), (up_warning - .01, "met")):
                self.assertEqual(rule.classify(value, sufficient=True), status)
            self.assertEqual(rule.classify(100, sufficient=False), "insufficient_data")

    def test_p95_nearest_rank_not_mean_or_interpolation(self):
        result = latency_p95(reversed(range(1, 101)))
        self.assertEqual(result.value, 95)
        self.assertEqual(result.status, "met")
        self.assertEqual(latency_p95(range(1, 102)).value, 96)
        self.assertEqual(latency_p95([1] * 94 + [9000] * 6).value, 9000)
        self.assertEqual(latency_p95([1] * 94 + [9000] * 6).status, "breached")
        self.assertEqual(latency_p95([0] * 100).value, 0)

    def test_p95_samples_and_completeness_are_separate_gates(self):
        self.assertEqual(latency_p95([1] * 99).status, "insufficient_data")
        self.assertEqual(latency_p95([1] * 100).status, "met")
        exact = latency_p95([6000] * 190 + [None] * 10)
        self.assertEqual(exact.completeness_percent, 95)
        self.assertEqual(exact.status, "breached")
        missing = latency_p95([6000] * 189 + [None] * 11)
        self.assertEqual(missing.value, 6000)
        self.assertEqual(missing.status, "insufficient_data")
        self.assertEqual(latency_p95([1] * 95 + [None] * 5).status, "insufficient_data")

    def test_latency_warning_and_target_bounds(self):
        for tier, target, warning in (("standard", 5000, 4000),
                                      ("enterprise", 4000, 3200), ("strategic", 3000, 2400)):
            for value, status in ((warning - 1, "met"), (warning, "warning"),
                                  (target, "warning"), (target + 1, "breached")):
                self.assertEqual(latency_p95([value] * 100, tier=tier).status, status)

    def test_invalid_counts_thresholds_and_durations(self):
        for args in ((-1, 100, 100), (101, 100, 100), (1, 1, 0),
                     (True, 100, 100), (1, 1.0, 100), (1, 101, 100)):
            with self.assertRaises(ValueError):
                availability(*args)
        for args in ((-1, 100), (101, 100), (0, -1), (True, 100)):
            with self.assertRaises(ValueError):
                request_error_rate(*args)
        for value in (-1, float("nan"), float("inf"), "1", True):
            with self.assertRaises(ValueError):
                latency_p95([value])
        for action in (lambda: availability(1, 1, 1, min_samples=0),
                       lambda: availability(1, 1, 1, min_coverage=101),
                       lambda: latency_p95([], min_completeness=-1),
                       lambda: request_error_rate(0, 0, min_samples=True),
                       lambda: threshold("unknown"), lambda: threshold("errors", "unknown"),
                       lambda: Threshold(90, 80, "lower"), lambda: Threshold(10, 20, "upper"),
                       lambda: Threshold(1, 1, "unknown"),
                       lambda: Threshold(float("nan"), 1, "upper")):
            with self.assertRaises(ValueError):
                action()

    def fact(self, order_id="order", **values):
        start, _ = month_bounds(2026, 10)
        defaults = dict(order_id=order_id, method="api", paid_at=start - timedelta(days=1),
                        deadline=start + timedelta(days=1), first_verified_success_at=None)
        return DeliveryFact(**(defaults | values))

    def delivery(self, facts, **values):
        _, end = month_bounds(2026, 10)
        return delivery_timeliness(facts, **(dict(year=2026, month=10, as_of=end) | values))

    def test_delivery_sample_gate_and_overdue_uncompleted_failures(self):
        failed = [self.fact(str(i)) for i in range(10)]
        self.assertEqual(self.delivery(failed[:9]).status, "insufficient_data")
        result = self.delivery(failed)
        self.assertEqual((result.sample_count, result.value, result.status), (10, 0, "breached"))
        on_time = [replace(fact, first_verified_success_at=fact.deadline) for fact in failed]
        self.assertEqual(self.delivery(on_time).status, "met")
        late = replace(on_time[0], first_verified_success_at=on_time[0].deadline + timedelta(seconds=1))
        self.assertEqual(self.delivery([late] + on_time[1:]).value, 90)

    def test_delivery_deadline_month_half_open_and_pending(self):
        start, end = month_bounds(2026, 10)
        self.assertEqual(start, datetime(2026, 9, 30, 16, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 10, 31, 16, tzinfo=timezone.utc))
        facts = [self.fact("start", deadline=start),
                 self.fact("prior", deadline=start - timedelta(seconds=1)),
                 self.fact("next", deadline=end, first_verified_success_at=end - timedelta(days=1)),
                 self.fact("late", deadline=start + timedelta(days=1), first_verified_success_at=end)]
        result = self.delivery(facts)
        self.assertEqual((result.sample_count, result.value), (2, 0))
        result = self.delivery(facts, as_of=start)
        self.assertEqual(result.sample_count, 1)
        self.assertEqual(month_bounds(2026, 12)[1], month_bounds(2027, 1)[0])

    def test_delivery_exclusions_deduplication_and_online_methods(self):
        facts = [self.fact(method=method) for method in ("training", "consulting", "custom")]
        facts = [replace(fact, order_id=str(i)) for i, fact in enumerate(facts)]
        facts += [self.fact("unpaid", paid=False), self.fact("cancelled", cancelled_before_delivery=True),
                  self.fact("test", isolated_test=True), self.fact("economic", requires_delivery=False)]
        result = self.delivery(facts)
        self.assertEqual((result.sample_count, result.value, result.status), (0, None, "insufficient_data"))
        for method in ("file", "api", "model_api", "tenant_access"):
            fact = self.fact(method=method)
            self.assertEqual(self.delivery([fact, fact]).sample_count, 1)
        with self.assertRaises(ValueError):
            self.delivery([self.fact(), self.fact(method="file")])

    def test_delivery_rejects_invalid_facts_dates_and_months(self):
        fact = self.fact()
        for values in (dict(order_id=""), dict(method="unknown"), dict(paid=1),
                       dict(deadline=fact.paid_at - timedelta(seconds=1)),
                       dict(first_verified_success_at=fact.paid_at - timedelta(seconds=1)),
                       dict(paid_at=datetime(2026, 10, 1)), dict(deadline="2026-10-01")):
            with self.assertRaises(ValueError):
                self.fact(**values)
        for year, month in ((0, 1), (2026, 0), (2026, 13), (2026, True), (9999, 1)):
            with self.assertRaises(ValueError):
                month_bounds(year, month)
        with self.assertRaises(ValueError):
            self.delivery([fact], as_of=datetime(2026, 10, 1))
        with self.assertRaises(ValueError):
            self.delivery([None])


if __name__ == "__main__":
    unittest.main()
