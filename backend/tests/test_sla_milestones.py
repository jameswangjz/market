"""Pure milestone boundaries: no API startup, SQL, network, or current time."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from app.sla_milestones import MilestoneEvent, reduce_milestones


class MilestoneTests(unittest.TestCase):
    def setUp(self):
        self.base = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
        self.snapshot = {'metrics': {key: {
            'clock': 'natural', 'responsibility': party, 'target_seconds': 600,
            'warning_seconds': 480, 'target': 10, 'unit': 'minutes', 'direction': 'upper',
        } for key, party in [('rectification', 'provider'), ('intervention_review', 'platform'),
                            ('intervention_response', 'platform'), ('urgent_intervention_response', 'platform')]},
            'calendar': {'version': 'synthetic-v1', 'timezone': 'UTC', 'weekdays': [0, 1, 2, 3, 4],
                'windows': [['09:00', '17:00']], 'covered_years': [2026], 'holidays': [], 'workdays': []}}

    def event(self, id, action, seconds, state='processing'):
        return MilestoneEvent(id, action, self.base + timedelta(seconds=seconds), state)

    def reduce(self, events, seconds=1200, **kwargs):
        return reduce_milestones(events, self.snapshot, as_of=self.base + timedelta(seconds=seconds),
                                 history_complete=True, **kwargs)

    def test_rectification_multiple_rounds_preserve_source_ids_and_frozen_target(self):
        events = [self.event('r1', 'reject_resolution', 0), self.event('s1', 'submit_resolution', 60),
                  self.event('r2', 'reject_resolution', 120), self.event('s2', 'submit_resolution', 240)]
        original = deepcopy(self.snapshot)
        result = self.reduce(events)
        self.assertEqual([row.round for row in result], [1, 2])
        self.assertEqual([row.source_event_ids for row in result], [('r1', 's1'), ('r2', 's2')])
        self.assertEqual([row.elapsed_seconds for row in result], [60, 120])
        self.assertTrue(all(row.lifecycle == 'completed' and row.responsibility == 'provider' for row in result))
        self.assertEqual(self.snapshot, original)
        self.snapshot['metrics']['rectification']['target_seconds'] = 999
        self.assertEqual(result[0].frozen_target['target_seconds'], 600)

    def test_only_buyer_info_pauses_rectification_intervention_does_not(self):
        rows = self.reduce([self.event('r', 'reject_resolution', 0),
            self.event('i', 'request_info', 60, 'awaiting_buyer_info'),
            self.event('p', 'request_intervention', 180, 'platform_intervention'),
            self.event('v', 'review_intervention', 300),
            self.event('s', 'submit_resolution', 420, 'awaiting_buyer_confirmation')])
        rect = next(row for row in rows if row.kind == 'rectification')
        self.assertEqual(rect.elapsed_seconds, 300)
        self.assertEqual(next(row for row in rows if row.kind == 'intervention_review').elapsed_seconds, 120)

    def test_open_buyer_wait_is_clipped_at_as_of_and_future_submit_ignored(self):
        rows = self.reduce([self.event('r', 'reject_resolution', 0),
            self.event('i', 'request_info', 60, 'awaiting_buyer_info'),
            self.event('s', 'submit_resolution', 1200)], seconds=600)
        self.assertEqual((rows[0].elapsed_seconds, rows[0].lifecycle, rows[0].ended_at), (60, 'open', None))

    def test_intervention_rounds_do_not_infer_first_response_from_review(self):
        rows = self.reduce([self.event('p1', 'request_intervention', 0, 'platform_intervention'),
            self.event('v1', 'review_intervention', 120),
            self.event('p2', 'request_intervention', 180, 'platform_intervention'),
            self.event('v2', 'review_intervention', 420)])
        reviews = [row for row in rows if row.kind == 'intervention_review']
        responses = [row for row in rows if row.kind == 'intervention_response']
        self.assertEqual([row.elapsed_seconds for row in reviews], [120, 240])
        self.assertEqual([row.round for row in responses], [1, 2])
        self.assertTrue(all(row.status == 'insufficient_data' and row.elapsed_seconds is None
                            and row.reason == 'missing_response_event' for row in responses))

    def test_future_explicit_response_uses_urgent_frozen_metric_independently(self):
        self.snapshot['metrics']['urgent_intervention_response']['target_seconds'] = 100
        self.snapshot['metrics']['urgent_intervention_response']['warning_seconds'] = 80
        rows = self.reduce([self.event('p', 'request_intervention', 0, 'platform_intervention'),
            self.event('a', 'respond_intervention', 100, 'platform_intervention'),
            self.event('v', 'review_intervention', 300)], priority='urgent')
        response, review = rows
        self.assertEqual((response.metric_key, response.elapsed_seconds, response.status),
                         ('urgent_intervention_response', 100, 'warning'))
        self.assertEqual((review.elapsed_seconds, review.lifecycle), (300, 'completed'))

    def test_terminal_close_never_completes_open_milestones_or_counts_after_close(self):
        for terminal in ('completed', 'withdrawn', 'force_closed'):
            for seconds, status in ((300, 'closed_without_confirmation'),
                                    (600, 'closed_without_confirmation'), (601, 'breached')):
                rows = self.reduce([self.event('r', 'reject_resolution', 0),
                    self.event('p', 'request_intervention', 0, 'platform_intervention'),
                    self.event('c', 'force_close', seconds, terminal)], seconds=5000)
                for row in rows:
                    self.assertEqual(row.lifecycle, 'closed_incomplete')
                    if row.kind == 'intervention_response':
                        self.assertEqual(row.status, 'insufficient_data')
                    else:
                        self.assertEqual((row.elapsed_seconds, row.status), (seconds, status))

    def test_support_window_and_natural_clock_follow_snapshot(self):
        self.snapshot['metrics']['rectification']['clock'] = 'support'
        self.base = self.base.replace(hour=16)
        rows = self.reduce([self.event('r', 'reject_resolution', 0),
            self.event('s', 'submit_resolution', 18 * 3600)], seconds=18 * 3600)
        self.assertEqual(rows[0].elapsed_seconds, 2 * 3600)
        self.snapshot['metrics']['rectification']['clock'] = 'natural'
        self.assertEqual(self.reduce([self.event('r', 'reject_resolution', 0)],
                                    seconds=18 * 3600)[0].elapsed_seconds, 18 * 3600)

    def test_unconfigured_year_is_insufficient_not_zero_or_success(self):
        self.snapshot['metrics']['rectification']['clock'] = 'support'
        self.snapshot['calendar']['covered_years'] = []
        row = self.reduce([self.event('r', 'reject_resolution', 0)])[0]
        self.assertEqual((row.status, row.reason, row.elapsed_seconds),
                         ('insufficient_data', 'calendar_not_ready', None))

    def test_historical_missing_evidence_and_orphan_review_are_unknown(self):
        events = [self.event('r', 'reject_resolution', 0), self.event('s', 'submit_resolution', 120)]
        row = reduce_milestones(events, self.snapshot, as_of=self.base + timedelta(seconds=300))[0]
        self.assertEqual((row.status, row.reason), ('insufficient_data', 'incomplete_history'))
        orphan = self.reduce([self.event('v', 'review_intervention', 60)])[0]
        self.assertEqual((orphan.started_at, orphan.reason), (None, 'missing_start_event'))
        self.assertEqual(self.reduce([]), ())

    def test_missing_or_invalid_frozen_target_is_not_substituted_with_live_defaults(self):
        for value in (None, {'clock': 'natural', 'target_seconds': -1},
                      {'clock': 'natural', 'target_seconds': float('nan')},
                      {'clock': 'natural', 'target_seconds': True}):
            self.snapshot['metrics']['rectification'] = value
            row = self.reduce([self.event('r', 'reject_resolution', 0)])[0]
            self.assertEqual(row.status, 'insufficient_data')
            self.assertIsNone(row.elapsed_seconds)

    def test_warning_and_breach_boundaries(self):
        for seconds, status in [(479, 'within_target'), (480, 'warning'), (599, 'warning'),
                                (600, 'warning'), (601, 'breached')]:
            row = self.reduce([self.event('r', 'reject_resolution', 0)], seconds=seconds)[0]
            self.assertEqual(row.status, status)

    def test_frozen_direction_must_be_explicit_upper_bound(self):
        target = self.snapshot['metrics']['rectification']
        for direction in ('lower', None):
            target['direction'] = direction
            row = self.reduce([self.event('r', 'reject_resolution', 0)])[0]
            self.assertEqual((row.status, row.reason, row.elapsed_seconds),
                             ('insufficient_data', 'invalid_frozen_target', None))
        del target['direction']
        self.assertEqual(self.reduce([self.event('r', 'reject_resolution', 0)])[0].reason,
                         'invalid_frozen_target')

    def test_reject_duplicate_ids_naive_times_and_reversed_history(self):
        first = self.event('r', 'reject_resolution', 0)
        for events in ([first, first], [self.event('later', 'reject_resolution', 60), first],
                       [MilestoneEvent('naive', 'reject_resolution', datetime(2026, 1, 1), 'processing')]):
            with self.assertRaises(ValueError):
                self.reduce(events)
        with self.assertRaises(ValueError):
            self.reduce([self.event('c', 'force_close', 0, 'force_closed'),
                         self.event('r', 'reject_resolution', 1)])


if __name__ == '__main__':
    unittest.main()
