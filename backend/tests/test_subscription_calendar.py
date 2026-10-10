"""Run from repo root: python3 -B -m unittest discover -s backend/tests
-p test_subscription_calendar.py -v
"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, localcontext
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.subscription_calendar import (  # noqa: E402
    CalendarAnchor, SubscriptionPeriod, TimeRange, validate_months,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
TICK = timedelta(microseconds=1)


def local(year, month, day, hour=10, minute=11, second=12, microsecond=345678):
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=SHANGHAI)


class CalendarAnchorTests(unittest.TestCase):
    def test_original_day_recovers_after_short_month_without_sticky_month_end(self):
        cases = [(local(2025, 1, 31), [(2025, 2, 28), (2025, 3, 31)]),
                 (local(2025, 1, 30), [(2025, 2, 28), (2025, 3, 30)]),
                 (local(2025, 2, 28), [(2025, 3, 28), (2025, 4, 28)]),
                 (local(2025, 4, 30), [(2025, 5, 30), (2025, 6, 30)])]
        for origin, dates in cases:
            anchor = CalendarAnchor(origin)
            for offset, (year, month, day) in enumerate(dates, 1):
                with self.subTest(origin=origin, offset=offset):
                    self.assertEqual(anchor.boundary(offset), local(year, month, day).astimezone(timezone.utc))
                    self.assertIs(anchor.boundary(offset).tzinfo, timezone.utc)
            self.assertEqual(anchor.boundary(0), origin.astimezone(timezone.utc))

    def test_leap_year_and_year_rollover(self):
        anchor = CalendarAnchor(local(2024, 1, 31))
        self.assertEqual(anchor.boundary(1), local(2024, 2, 29))
        self.assertEqual(anchor.boundary(2), local(2024, 3, 31))
        leap = CalendarAnchor(local(2024, 2, 29))
        for offset, expected in [(1, local(2024, 3, 29)), (12, local(2025, 2, 28)),
                                 (13, local(2025, 3, 29)), (48, local(2028, 2, 29))]:
            with self.subTest(offset=offset):
                self.assertEqual(leap.boundary(offset), expected)
        self.assertEqual(CalendarAnchor(local(2025, 12, 31)).boundary(1), local(2026, 1, 31))

    def test_business_date_is_shanghai_even_when_utc_date_is_previous_day(self):
        origin = datetime(2025, 1, 30, 16, 5, 6, 789123, tzinfo=timezone.utc)
        expected = datetime(2025, 3, 30, 16, 5, 6, 789123, tzinfo=timezone.utc)
        anchor = CalendarAnchor(origin)
        self.assertEqual(anchor.boundary(2), expected)
        other_zone = origin.astimezone(timezone(timedelta(hours=-7)))
        self.assertEqual(CalendarAnchor(other_zone), anchor)
        self.assertEqual(CalendarAnchor(other_zone).boundary(2), expected)

    def test_purchase_month_validation_is_strict(self):
        anchor = CalendarAnchor(local(2025, 1, 31))
        for months in (1, 36):
            self.assertEqual(validate_months(months), months)
            self.assertEqual(anchor.term(months).end, anchor.boundary(months))
        for months in (0, -1, 37, 100):
            with self.subTest(months=months), self.assertRaises(ValueError):
                anchor.term(months)
        for months in (True, False, 1.0, Decimal(1), "1", None):
            with self.subTest(months=months), self.assertRaises(TypeError):
                anchor.term(months)

    def test_renewal_offsets_exceed_36_without_reanchoring(self):
        anchor = CalendarAnchor(local(2025, 1, 31))
        original = anchor.term(1)
        renewal = anchor.term(36, start_month=1)
        later = anchor.term(36, start_month=37)
        self.assertEqual(original.end, renewal.start)
        self.assertEqual(renewal.end, later.start)
        self.assertEqual(renewal.end, local(2028, 2, 29))
        self.assertEqual(later.end, local(2031, 2, 28))
        self.assertEqual(anchor.period(38).start, local(2028, 3, 31))
        self.assertEqual(original, anchor.term(1))

    def test_half_open_quota_period_indexes_at_boundaries(self):
        anchor = CalendarAnchor(local(2025, 1, 31))
        self.assertIsNone(anchor.month_index(anchor.origin - TICK))
        self.assertIsNone(anchor.period_at(anchor.origin - TICK))
        for index in (0, 1, 2, 11, 35, 36, 72):
            with self.subTest(index=index):
                period = anchor.period(index)
                self.assertEqual(period.index, index)
                self.assertEqual(anchor.month_index(period.start), index)
                self.assertEqual(anchor.month_index(period.end - TICK), index)
                self.assertEqual(anchor.month_index(period.end), index + 1)
                self.assertEqual(anchor.period_at(period.start), period)
                self.assertTrue(period.contains(period.start))
                self.assertFalse(period.contains(period.end))
                self.assertEqual(period.end, anchor.period(index + 1).start)
        expired = anchor.term(1)
        self.assertFalse(expired.contains(expired.end))
        self.assertEqual(anchor.month_index(expired.end), 1)

    def test_shanghai_month_change_does_not_reset_subscription_month(self):
        anchor = CalendarAnchor(local(2025, 1, 31))
        self.assertEqual(anchor.month_index(local(2025, 2, 1, 0, 0, 0, 0)), 0)
        self.assertEqual(anchor.month_index(local(2025, 2, 28, 10, 11, 11)), 0)
        self.assertEqual(anchor.month_index(local(2025, 3, 1)), 1)

    def test_invalid_offsets_and_calendar_overflow(self):
        anchor = CalendarAnchor(local(2025, 1, 31))
        for value, error in [(-1, ValueError), (True, TypeError), (1.5, TypeError),
                             ("2", TypeError), (None, TypeError)]:
            for operation in (anchor.boundary, anchor.period,
                              lambda k: anchor.term(1, start_month=k)):
                with self.subTest(value=value, operation=operation), self.assertRaises(error):
                    operation(value)
        with self.assertRaises(ValueError):
            CalendarAnchor(local(9999, 12, 31)).boundary(1)
        with self.assertRaises(ValueError):
            anchor.boundary(10**100)

    def test_many_anchors_have_monotone_contiguous_periods_and_matching_indexes(self):
        for year in (2023, 2024):
            for month in range(1, 13):
                for day in (28, 29, 30, 31):
                    try:
                        origin = local(year, month, day)
                    except ValueError:
                        continue
                    anchor = CalendarAnchor(origin)
                    for index in range(49):
                        with self.subTest(origin=origin, index=index):
                            period = anchor.period(index)
                            self.assertLess(period.start, period.end)
                            self.assertEqual(anchor.month_index(period.start), index)
                            self.assertEqual(anchor.month_index(period.end - TICK), index)
                            self.assertEqual(period.end, anchor.period(index + 1).start)
                            business = period.start.astimezone(SHANGHAI)
                            self.assertEqual((business.hour, business.minute, business.second,
                                              business.microsecond), (10, 11, 12, 345678))


class TimeRangeTests(unittest.TestCase):
    def test_clamps_future_active_and_expired_seconds_and_ratios(self):
        start = local(2025, 1, 31)
        term = TimeRange(start, start + timedelta(seconds=3))
        for delta, seconds, ratio in [(-1, "3", "1"), (0, "3", "1"),
                                       (1.5, "1.5", "0.5"), (3, "0", "0"), (4, "0", "0")]:
            with self.subTest(delta=delta):
                at = start + timedelta(seconds=delta)
                self.assertEqual(term.remaining_seconds(at), Decimal(seconds))
                self.assertEqual(term.remaining_ratio(at), Decimal(ratio))
                self.assertIsInstance(term.remaining_ratio(at), Decimal)
        self.assertEqual(term.duration_seconds, Decimal(3))
        self.assertTrue(term.contains(start))
        self.assertFalse(term.contains(start - TICK))
        self.assertFalse(term.contains(term.end))
        self.assertIs(term.start.tzinfo, timezone.utc)
        self.assertIs(term.end.tzinfo, timezone.utc)

    def test_nonterminating_fraction_and_microseconds_do_not_use_float(self):
        start = local(2025, 1, 31)
        term = TimeRange(start, start + timedelta(seconds=3))
        expected = Decimal("0." + "3" * 50)
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(term.remaining_ratio(start + timedelta(seconds=2)), expected)
        micro = TimeRange(start, start + timedelta(microseconds=3))
        self.assertEqual(micro.duration_seconds, Decimal("0.000003"))
        self.assertEqual(micro.remaining_seconds(micro.end - TICK), Decimal("0.000001"))
        self.assertEqual(micro.remaining_ratio(micro.end - TICK), expected)
        long = TimeRange(datetime(1, 1, 1, tzinfo=timezone.utc),
                         datetime(9999, 12, 31, tzinfo=timezone.utc))
        self.assertEqual(long.remaining_seconds(long.end - TICK), Decimal("0.000001"))

    def test_proration_denominator_is_full_source_order_not_quota_or_lifetime(self):
        anchor = CalendarAnchor(local(2025, 1, 31))
        original = anchor.term(3)
        future = anchor.term(2, start_month=3)
        at = anchor.boundary(1)
        self.assertEqual(original.duration_seconds, Decimal(89 * 86400))
        self.assertEqual(original.remaining_seconds(at), Decimal(61 * 86400))
        with localcontext() as context:
            context.prec = 50
            self.assertEqual(original.remaining_ratio(at), Decimal(61) / Decimal(89))
        self.assertEqual(future.remaining_ratio(at), Decimal(1))
        self.assertNotEqual(original.remaining_ratio(at), anchor.period(1).remaining_ratio(at))
        self.assertNotEqual(original.remaining_ratio(at), anchor.term(5).remaining_ratio(at))

    def test_aware_inputs_required_and_invalid_ranges_rejected(self):
        class NoOffset(tzinfo):
            def utcoffset(self, dt):
                return None

        start = local(2025, 1, 31)
        term = TimeRange(start, start + timedelta(seconds=1))
        for invalid, error in [(datetime(2025, 1, 1), ValueError),
                               (datetime(2025, 1, 1, tzinfo=NoOffset()), ValueError),
                               ("2025-01-01", TypeError), (None, TypeError)]:
            for operation in (CalendarAnchor, term.contains, term.remaining_seconds,
                              term.remaining_ratio, CalendarAnchor(start).month_index,
                              lambda at: TimeRange(at, term.end),
                              lambda at: TimeRange(term.start, at)):
                with self.subTest(invalid=invalid, operation=operation), self.assertRaises(error):
                    operation(invalid)
        for end in (start, start - TICK):
            with self.assertRaises(ValueError):
                TimeRange(start, end)
        with self.assertRaises(ValueError):
            SubscriptionPeriod(term.start, term.end, -1)
        with self.assertRaises(ValueError):
            CalendarAnchor(datetime.max.replace(tzinfo=timezone.utc))

    def test_anchor_and_ranges_are_immutable(self):
        anchor = CalendarAnchor(local(2025, 1, 31))
        with self.assertRaises(FrozenInstanceError):
            anchor.origin = local(2025, 2, 28)
        with self.assertRaises(FrozenInstanceError):
            anchor.term(1).end = anchor.boundary(2)
        with self.assertRaises(FrozenInstanceError):
            anchor.period(0).index = 1


if __name__ == "__main__":
    unittest.main()
