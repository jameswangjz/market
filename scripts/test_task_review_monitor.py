"""User acceptance is distinct from completed implementation progress."""
import unittest
from datetime import datetime, timezone

from trading_task_control import inspect


class TaskReviewMonitorTests(unittest.TestCase):
    def row(self, status, progress):
        return dict(code='TRD-TEST-001', status=status, progress=progress,
                    dependencies='', updated_at=datetime.now(timezone.utc),
                    acceptance='User verifies the deployed frontend')

    def test_review_at_full_progress_is_not_an_error(self):
        result = inspect([self.row('review', 100)], ['TRD-TEST-001'])
        self.assertEqual(result['counts']['review'], 1)
        self.assertEqual(result['alerts'], [])

    def test_unfinished_at_full_progress_still_requires_transition(self):
        for status in ('todo', 'in_progress'):
            with self.subTest(status=status):
                result = inspect([self.row(status, 100)], ['TRD-TEST-001'])
                self.assertIn('unaccepted_progress_100',
                              [item['reason'] for item in result['alerts']])

    def test_validated_dependency_allows_development_without_claiming_acceptance(self):
        prerequisite = self.row('review', 100)
        dependent = self.row('todo', 0)
        dependent.update(code='TRD-TEST-002', dependencies=prerequisite['code'])
        result = inspect([prerequisite, dependent], [dependent['code']])
        self.assertEqual(result['ready'], [dependent['code']])
        self.assertEqual(result['pending_acceptance_dependencies'],
                         [{'code': dependent['code'], 'dependencies': [prerequisite['code']]}])
        self.assertEqual(prerequisite['status'], 'review')

    def test_partial_review_dependency_is_not_ready(self):
        prerequisite = self.row('review', 85)
        dependent = self.row('todo', 0)
        dependent.update(code='TRD-TEST-002', dependencies=prerequisite['code'])
        self.assertEqual(inspect([prerequisite, dependent], [dependent['code']])['ready'], [])


if __name__ == '__main__':
    unittest.main()
