"""Offline proof gating for the bounded fault-matrix progress update."""
import copy
import json
from pathlib import Path
import unittest

from fault_matrix_checkpoint import validate


class FaultMatrixProofTests(unittest.TestCase):
    def setUp(self):
        self.proof = json.loads((Path(__file__).resolve().parents[1] / 'docs' / 'evidence' /
            'version-change-available-fault-matrix-20261013.json').read_text())

    def test_actual_reviewed_proof_is_accepted(self):
        validate(self.proof)

    def test_incomplete_or_unreviewed_proof_is_rejected(self):
        changes = [
            lambda value: value.update(executed_check_count=100),
            lambda value: value.update(cleanup_verified=False),
            lambda value: value.update(shared_services_stopped=True),
            lambda value: value.update(worker_batch_changed=True),
            lambda value: value.update(findings=[]),
            lambda value: value['groups']['postgresql_worker'].update(isolated_schema_removed=False),
            lambda value: value['groups']['api_model_adapter'].update(status='unknown'),
        ]
        for change in changes:
            with self.subTest(change=change):
                value = copy.deepcopy(self.proof)
                change(value)
                with self.assertRaises(ValueError):
                    validate(value)


if __name__ == '__main__':
    unittest.main()
