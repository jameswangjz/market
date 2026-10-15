import unittest
from datetime import datetime, timedelta, timezone

from app.refund_entitlements import plan_refund


class RefundPlanningTests(unittest.TestCase):
    def api(self, **kw):
        args = dict(method='api', original_amount='300', requested_amount='60',
                    monthly_quota=1000, months=3, used_calls=600)
        return plan_refund(**(args | kw))

    def time(self, **kw):
        start = datetime(2026, 1, 31, tzinfo=timezone.utc)
        args = dict(method='tenant_access', original_amount='100', requested_amount='20',
                    starts_at=start, ends_at=start + timedelta(days=28), at=start + timedelta(days=7))
        return plan_refund(**(args | kw))

    def test_multimonth_finite_quota(self):
        result = self.api()
        self.assertEqual(result['original_quota'], 3000)
        self.assertEqual(result['recovery_delta'], 600)
        self.assertEqual(result['remaining_unused_calls'], 1800)

    def test_repeat_uses_original_not_remaining_price(self):
        result = self.api(refunded_amount='60')
        self.assertEqual(result['recovery_delta'], 600)
        self.assertEqual(result['cumulative_recovery'], 1200)

    def test_consumed_calls_cannot_be_refunded(self):
        with self.assertRaises(ValueError):
            self.api(requested_amount='240.01')

    def test_full_unused_refund_preserves_usage(self):
        result = self.api(requested_amount='240')
        self.assertEqual(result['remaining_total_quota'], 600)
        self.assertEqual(result['remaining_unused_calls'], 0)

    def test_time_refund_removes_tail(self):
        result = self.time()
        self.assertEqual(result['effective_ends_at'], datetime(2026, 2, 22, 9, 36, tzinfo=timezone.utc))

    def test_unlimited_api_uses_time(self):
        self.assertEqual(self.time(method='api')['basis'], 'time')

    def test_time_cannot_remove_consumed_interval(self):
        with self.assertRaises(ValueError):
            self.time(requested_amount='75.01')

    def test_final_time_refund_reclaims_remaining_tail(self):
        result = self.time(requested_amount='75')
        self.assertEqual(result['effective_ends_at'], datetime(2026, 2, 7, tzinfo=timezone.utc))

    def test_zero_negative_nan_and_subcent_rejected(self):
        for value in ('0', '-1', 'NaN', 'Infinity', '0.001'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.api(requested_amount=value)

    def test_invalid_integer_inputs(self):
        for kw in ({'monthly_quota': True}, {'months': 0}, {'months': 37}, {'used_calls': -1}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.api(**kw)

    def test_usage_over_budget_rejected(self):
        with self.assertRaises(ValueError):
            self.api(used_calls=3001)

    def test_naive_or_missing_dates_rejected(self):
        with self.assertRaises(ValueError):
            self.time(at=datetime(2026, 2, 1))

    def test_expired_segment_no_refund(self):
        with self.assertRaises(ValueError):
            self.time(at=datetime(2027, 1, 1, tzinfo=timezone.utc))

    def test_model_api_same_policy(self):
        self.assertEqual(self.api(method='model_api')['basis'], 'calls')

    def test_cent_rounding_complete_recovery(self):
        result = self.api(original_amount='1', requested_amount='0.66', monthly_quota=3,
                          months=1, used_calls=1)
        self.assertEqual(result['remaining_unused_calls'], 0)


if __name__ == '__main__':
    unittest.main()
