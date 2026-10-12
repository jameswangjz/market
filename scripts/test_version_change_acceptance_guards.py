"""Offline safety checks for private runtime acceptance manifests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import version_change_fault_runtime_acceptance as fault
import version_change_trade_monthly_acceptance as monthly


class AcceptanceManifestTests(unittest.TestCase):
    def test_shorter_atomic_rewrite_has_no_trailing_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            fault.save(path, {'phase': 'a much longer prior phase'}, create=True)
            fault.save(path, {'phase': 'done'})
            self.assertEqual(json.loads(path.read_text()), {'phase': 'done'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_monthly_manifest_requires_private_owner_and_file_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'monthly.json'
            payload = {'source_order_ids': ['original'], 'change_ids': ['change'],
                       'finance_user_id': 'qa-vcs-abcdef012345-u-finance'}
            fault.save(path, payload, create=True)
            self.assertEqual(monthly.read_manifest(path)['finance_user_id'],
                             'qa-vcs-abcdef012345-u-finance')
            path.chmod(0o644)
            with self.assertRaises(AssertionError):
                monthly.read_manifest(path)
            path.chmod(0o600)
            payload['finance_user_id'] = 'admin'
            fault.save(path, payload)
            with self.assertRaises(AssertionError):
                monthly.read_manifest(path)

    def test_existing_manifest_is_not_overwritten_on_create(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'manifest.json'
            fault.save(path, {'keep': True}, create=True)
            with self.assertRaises(FileExistsError):
                fault.save(path, {'keep': False}, create=True)
            self.assertEqual(json.loads(path.read_text()), {'keep': True})


if __name__ == '__main__':
    unittest.main()
