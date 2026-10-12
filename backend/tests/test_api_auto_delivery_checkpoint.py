"""Evidence guard never imports app.main or touches development tasks."""
import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location('api_delivery_checkpoint',
    Path(__file__).resolve().parents[2] / 'scripts' / 'api_auto_delivery_checkpoint.py')
checkpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)


class CheckpointTests(unittest.TestCase):
    def proof(self):
        return {'status': 'passed', 'worker_exercised': True,
                'checks': ['api: manual, negative, credential, renewal, revoke',
                           'model_api: manual, negative, credential, renewal, revoke',
                           'api: deployed worker', 'model_api: deployed worker'],
                'payment': 'simulated',
                'cleanup': 'exact owned fixtures removed; native DB2 rate keys expire'}

    def test_complete_evidence_passes(self):
        checkpoint.validate(self.proof())

    def test_manual_only_or_missing_cleanup_or_partial_checks_refused(self):
        for field, value in [('worker_exercised', False), ('status', 'failed'),
                             ('cleanup', 'pending'), ('checks', []), ('payment', 'real')]:
            with self.subTest(field=field):
                proof = self.proof()
                proof[field] = value
                with self.assertRaises(ValueError):
                    checkpoint.validate(proof)
