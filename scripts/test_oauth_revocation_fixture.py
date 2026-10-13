"""Guard generated identities and evidence gating without touching a database."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import oauth_revocation_runtime_acceptance as acceptance


class OAuthFixtureTests(unittest.TestCase):
    def test_generated_database_ids_fit_existing_columns(self):
        ids = acceptance.fixture_ids()
        for key in ('enterprise_id', 'product_id', 'client_row_id'):
            self.assertLessEqual(len(ids[key]), 36)

    def test_manifest_round_trip_is_private_and_bound_to_generated_roots(self):
        ids = acceptance.fixture_ids()
        ids['environment'] = 'a' * 64
        ids['protected_order_fingerprint'] = 'b' * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            acceptance.write_manifest(path, ids, create=True)
            self.assertEqual(acceptance.read_manifest(path), ids)
            ids['enterprise_id'] = 'foreign-enterprise'
            acceptance.write_manifest(path, ids)
            with self.assertRaises(acceptance.AcceptanceFailure):
                acceptance.read_manifest(path)

    def test_checkpoint_rejects_incomplete_proof_before_database_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'proof.json'
            path.write_text(json.dumps({'status': 'passed'}))
            result = subprocess.run([sys.executable,
                str(Path(__file__).with_name('version_change_saga_checkpoint.py')),
                '--phase', 'oauth-revocation', '--proof', str(path)],
                capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Live OAuth revocation', result.stderr)


if __name__ == '__main__':
    unittest.main()
