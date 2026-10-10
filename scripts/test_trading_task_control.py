import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading_task_control import inspect, validate


class TaskControlTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 10, tzinfo=timezone.utc)
        self.rows = [{'code': 'TRD-A', 'status': 'done', 'progress': 100, 'dependencies': '',
                      'acceptance': 'done', 'updated_at': self.now},
                     {'code': 'TRD-B', 'status': 'todo', 'progress': 0, 'dependencies': 'TRD-A',
                      'acceptance': 'pending', 'updated_at': self.now}]

    def test_real_plan_is_acyclic(self):
        plan = json.loads((Path(__file__).parents[1] / 'docs/产品交易履约开发任务-20261010.json').read_text())
        self.assertEqual(len(validate(plan)), 42)

    def test_ready_dependencies_and_counts(self):
        result = inspect(self.rows, ['TRD-A', 'TRD-B'], self.now)
        self.assertEqual(result['ready'], ['TRD-B'])
        self.assertEqual(result['counts']['done'], 1)
        self.assertFalse(result['alerts'])

    def test_stale_progress_and_unchanged_state(self):
        self.rows[1].update(status='in_progress', progress=50, updated_at=self.now-timedelta(minutes=61))
        result = inspect(self.rows, ['TRD-B'], self.now)
        self.assertEqual(result['alerts'][0]['reason'], 'stale_progress')
        self.assertEqual(self.rows[1]['status'], 'in_progress')

    def test_blocked_reason_missing_and_bad_counts(self):
        self.rows[0]['progress'] = 99
        self.rows[1]['status'] = 'blocked'
        reasons = {a['reason'] for a in inspect(self.rows, ['TRD-A', 'TRD-B', 'TRD-C'], self.now)['alerts']}
        self.assertEqual(reasons, {'done_progress_mismatch', 'blocked', 'missing_task'})

    def test_in_progress_unfinished_dependency_and_100(self):
        self.rows[0]['status'] = 'review'
        self.rows[1].update(status='in_progress', progress=100)
        reasons = {a['reason'] for a in inspect(self.rows, ['TRD-B'], self.now)['alerts']}
        self.assertEqual(reasons, {'unfinished_dependency', 'unaccepted_progress_100'})

    def test_cycle_rejected(self):
        plan = {'tasks': [{'code': 'TRD-A', 'title': 'A', 'acceptance': 'A', 'status': 'todo', 'progress': 0, 'dependencies': ['TRD-B']},
                          {'code': 'TRD-B', 'title': 'B', 'acceptance': 'B', 'status': 'todo', 'progress': 0, 'dependencies': ['TRD-A']}]}
        with self.assertRaisesRegex(ValueError, 'cycle'):
            validate(plan)

    def test_naive_timestamp_treated_as_utc(self):
        self.rows[1].update(status='in_progress', updated_at=self.now.replace(tzinfo=None))
        self.assertFalse(inspect(self.rows, ['TRD-B'], self.now)['alerts'])


if __name__ == '__main__':
    unittest.main()
