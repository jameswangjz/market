"""Definitions are visible, but never claim substeps were executed."""
import json
from pathlib import Path
import unittest

from app.development_steps import subtasks_for


class ClearingDevelopmentStepsTests(unittest.TestCase):
    def test_packaged_clearing_steps_are_visible_and_not_inferred_from_parent_progress(self):
        path = Path(__file__).resolve().parents[1] / 'app' / 'clearing_development_steps.json'
        definitions = json.loads(path.read_text(encoding='utf-8'))['tasks']
        self.assertEqual(len(definitions), 17)
        self.assertEqual(sum(map(len, definitions.values())), 57)
        for code, titles in definitions.items():
            steps = subtasks_for(code)
            self.assertEqual([s['title'] for s in steps], titles)
            self.assertEqual(len({s['id'] for s in steps}), len(steps))
            self.assertEqual({s['status'] for s in steps}, {'untracked'})

    def test_unknown_task_does_not_invent_steps(self):
        self.assertEqual(subtasks_for('CLR2-NOT-A-TASK'), [])


if __name__ == '__main__':
    unittest.main()
