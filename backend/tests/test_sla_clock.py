import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app.sla_clock import CalendarNotReady, elapsed_seconds, minute_budget, ticket_measurements


def at(value):
    return datetime.fromisoformat(value + "+08:00")


class SLAClockTests(unittest.TestCase):
    def setUp(self):
        self.calendar = {"timezone": "Asia/Shanghai", "weekdays": [0, 1, 2, 3, 4],
            "windows": [["09:00", "17:00"]], "holidays": [], "workdays": [],
            "covered_years": [2026], "version": "synthetic-test-calendar-v1"}

    def elapsed(self, start, end, **kwargs):
        return elapsed_seconds(at(start), at(end), mode="support", calendar=self.calendar, **kwargs)

    def test_nights_weekends_and_half_open_window(self):
        self.assertEqual(self.elapsed("2026-10-16T16:00", "2026-10-19T10:00"), 7200)
        self.assertEqual(self.elapsed("2026-10-16T17:00", "2026-10-19T09:00"), 0)

    def test_holiday_and_workday_override_are_frozen_inputs(self):
        self.calendar["holidays"] = ["2026-10-19"]
        self.calendar["workdays"] = ["2026-10-17"]
        self.assertEqual(self.elapsed("2026-10-16T16:00", "2026-10-19T10:00"), 9 * 3600)

    def test_pauses_are_clipped_and_unioned_not_deducted_twice(self):
        pauses = [(at("2026-10-19T10:00"), at("2026-10-19T12:00")),
                  (at("2026-10-19T11:00"), at("2026-10-19T13:00")),
                  (at("2026-10-18T00:00"), at("2026-10-19T09:30"))]
        self.assertEqual(self.elapsed("2026-10-19T09:00", "2026-10-19T17:00", pauses=pauses), 4.5 * 3600)

    def test_natural_time_ignores_support_calendar_and_holidays(self):
        self.assertEqual(elapsed_seconds(at("2026-10-16T17:00"), at("2026-10-17T17:00"), mode="natural"), 86400)

    def test_unconfigured_year_or_windows_do_not_produce_fake_compliance(self):
        with self.assertRaises(CalendarNotReady):
            self.elapsed("2027-01-01T09:00", "2027-01-01T10:00")
        self.calendar["windows"] = []
        with self.assertRaises(CalendarNotReady):
            self.elapsed("2026-10-19T09:00", "2026-10-19T10:00")

    def test_full_day_calendar_and_leap_day(self):
        self.calendar.update(weekdays=list(range(7)), windows=[["00:00", "00:00"]], covered_years=[2028])
        self.assertEqual(self.elapsed("2028-02-28T12:00", "2028-03-01T12:00"), 48 * 3600)

    def test_strategic_all_day_does_not_exclude_holidays(self):
        self.calendar.update(weekdays=list(range(7)), windows=[["00:00", "24:00"]],
            holidays=["2026-10-19"], exclude_public_holidays=False, covered_years=[])
        self.assertEqual(self.elapsed("2026-10-19T00:00", "2026-10-20T00:00"), 86400)

    def test_half_open_year_end_does_not_require_unused_next_year(self):
        self.assertEqual(self.elapsed("2026-12-31T09:00", "2027-01-01T00:00"), 28800)

    def test_overlapping_windows_do_not_duplicate_time(self):
        self.calendar["windows"] = [["09:00", "12:00"], ["11:00", "17:00"]]
        self.assertEqual(self.elapsed("2026-10-19T09:00", "2026-10-19T17:00"), 8 * 3600)

    def test_overnight_support_window(self):
        self.calendar.update(weekdays=list(range(7)), windows=[["22:00", "06:00"]])
        self.assertEqual(self.elapsed("2026-10-19T23:00", "2026-10-20T05:00"), 6 * 3600)

    def test_invalid_values_rejected_and_decimal_minutes_preserved(self):
        self.assertEqual(minute_budget(3.2), 192)
        for value in (0, -1, True, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                minute_budget(value)
        with self.assertRaises(ValueError):
            elapsed_seconds(datetime(2026, 1, 1), datetime(2026, 1, 2), mode="natural")
        with self.assertRaises(ValueError):
            self.elapsed("2026-10-19T10:00", "2026-10-19T09:00")

    def test_ticket_response_never_pauses_and_intervention_resumes_resolution(self):
        ticket = SimpleNamespace(created_at=at("2026-10-19T09:00"), first_response_at=at("2026-10-19T10:00"),
            closed_at=None, priority="normal", status="awaiting_buyer_info", evidence_status="recorded")
        event = lambda when, action, status: SimpleNamespace(occurred_at=at(when), event_type=action, to_status=status)
        events = [event("2026-10-19T09:00", "create", "awaiting_response"),
                  event("2026-10-19T10:00", "respond", "processing"),
                  event("2026-10-19T11:00", "request_info", "awaiting_buyer_info"),
                  event("2026-10-19T12:00", "request_intervention", "platform_intervention"),
                  event("2026-10-19T14:00", "review_intervention", "awaiting_buyer_info")]
        result = ticket_measurements(ticket, events, at("2026-10-19T16:00"), self.calendar)
        self.assertEqual(result["first_response_seconds"], 3600)
        self.assertEqual(result["resolution_seconds"], 4 * 3600)
        self.assertEqual(result["intervention_seconds"], 2 * 3600)

    def test_force_close_is_not_reported_as_normal_resolution(self):
        ticket = SimpleNamespace(created_at=at("2026-10-19T09:00"), first_response_at=None,
            closed_at=at("2026-10-19T10:00"), priority="normal", status="force_closed",
            evidence_status="historical_unknown")
        result = ticket_measurements(ticket, [], at("2026-10-19T17:00"), self.calendar)
        self.assertEqual(result["resolution_outcome"], "force_closed")
        self.assertEqual(result["historical_evidence"], "historical_unknown")
        self.assertFalse(result["response_completed"])
        self.assertEqual(result["resolution_seconds"], 3600)


if __name__ == "__main__":
    unittest.main()
