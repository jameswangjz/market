import unittest
from unittest.mock import MagicMock, patch

from app import sla_online_worker as worker


class OnlineWorkerTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.session = MagicMock()
        self.session.__enter__.return_value = self.db
        self.evaluate = MagicMock(return_value=[{'id': 'one'}])
        self.ns = {'SessionLocal': MagicMock(return_value=self.session),
                   'sla_online_assessments': {'evaluate': self.evaluate}}
        self.probes = {'complete': True, 'failures': 0}

    def test_collect_then_evaluate_in_one_owned_assessment_transaction(self):
        with patch.object(worker, 'collect', return_value=self.probes):
            result = worker.run(self.ns)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['assessments'], 1)
        self.evaluate.assert_called_once_with(self.db, 'system:sla-online-monitor')
        self.db.commit.assert_called_once()

    def test_incomplete_or_failed_probe_sweep_is_not_claimed_success(self):
        for probes in ({'complete': False, 'failures': 0}, {'complete': True, 'failures': 1}):
            with self.subTest(probes=probes), patch.object(worker, 'collect', return_value=probes):
                self.assertEqual(worker.run(self.ns)['status'], 'incomplete')
        self.evaluate.assert_not_called()
        self.ns['SessionLocal'].assert_not_called()

    def test_assessment_failure_rolls_back(self):
        self.evaluate.side_effect = RuntimeError('atomic failure')
        with patch.object(worker, 'collect', return_value=self.probes):
            with self.assertRaises(RuntimeError):
                worker.run(self.ns)
        self.db.rollback.assert_called_once()
        self.db.commit.assert_not_called()

    def test_budget_after_collection_does_not_launch_assessment(self):
        with patch.object(worker, 'collect', return_value=self.probes), \
             patch.object(worker.time, 'monotonic', side_effect=[0, 211]):
            self.assertEqual(worker.run(self.ns)['status'], 'incomplete')
        self.evaluate.assert_not_called()

    def test_expiry_audit_commits_even_when_probes_are_incomplete(self):
        expire = MagicMock(return_value=1)
        self.ns['enterprise_sla'] = {'expire_due': expire}
        with patch.object(worker, 'collect', return_value={'complete': False, 'failures': 0}):
            self.assertEqual(worker.run(self.ns)['status'], 'incomplete')
        expire.assert_called_once_with(self.db)
        self.db.commit.assert_called_once()
        self.evaluate.assert_not_called()

    def test_expiry_failure_rolls_back_before_probing(self):
        self.ns['enterprise_sla'] = {'expire_due': MagicMock(side_effect=RuntimeError('expiry failed'))}
        with patch.object(worker, 'collect') as collect:
            with self.assertRaises(RuntimeError):
                worker.run(self.ns)
        self.db.rollback.assert_called_once()
        collect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
