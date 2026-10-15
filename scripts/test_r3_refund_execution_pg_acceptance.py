"""Offline guards for the owned-schema execution probe."""
import importlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import r3_refund_execution_pg_acceptance as probe


class ExecutionProbeGuards(unittest.TestCase):
    def test_invalid_pod_stops_before_transport_or_packaging(self):
        with patch('sys.argv', ['probe', '--pod', 'market-api-x; unsafe']), \
                patch.dict(os.environ, SSHPASS='synthetic'), \
                patch.object(probe.transport, 'pack_workingtree') as pack, \
                patch.object(probe.subprocess, 'run') as run:
            with self.assertRaises(probe.base.AcceptanceFailure):
                probe.main()
            pack.assert_not_called()
            run.assert_not_called()

    def test_transport_requires_private_password(self):
        with patch('sys.argv', ['probe', '--pod', 'market-api-owned']), \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(probe.transport, 'pack_workingtree') as pack:
            with self.assertRaises(probe.base.AcceptanceFailure):
                probe.main()
            pack.assert_not_called()

    def test_evidence_does_not_claim_real_resources_or_bank(self):
        synthetic = dict(status='passed', cases={}, coverage=['must-not-claim-task'])
        with patch.object(probe.cross, 'remote_run', return_value=synthetic):
            result = probe.remote_run('encoded', {})
        self.assertEqual(result['coverage'], [])
        self.assertIn('synthetic', result['incremental_scope'])
        self.assertIn('simulated', result['external_adapters']['funds'])
        self.assertTrue(any('bank' in value for value in result['gaps']))

    def test_inventory_covers_executor_resources_and_worker(self):
        root = Path(__file__).resolve().parents[1] / 'backend/app'
        for name in ('refund_execution.py', 'refund_execution_resources.py', 'refund_execution_worker.py'):
            self.assertIn(name, probe.SOURCES)
            self.assertTrue((root / name).is_file())
        self.assertEqual(len(set(probe.CASES)), 5)


if __name__ == '__main__':
    unittest.main()
