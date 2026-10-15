"""Offline ownership and direct-RP safety guards; no application imports."""
from pathlib import Path
import tempfile
import unittest

import version_change_quote_acceptance as scope


class OwnershipTests(unittest.TestCase):
    def test_both_exact_prefixes(self):
        for prefix in ('qa-vcq-', 'qa-r3-sso-'):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'scope.json'
                ids = scope.fixture_ids(prefix + '123456abcdef')
                scope.private_json(path, ids, create=True)
                self.assertEqual(scope.read_scope(path), ids)

    def test_foreign_or_modified_ids_refused(self):
        for run_id in ('qa-r3-sso-invalid', 'foreign-123456abcdef', 'qa-r3-sso-123456abcdef'):
            with self.subTest(run_id=run_id), tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'scope.json'
                ids = scope.fixture_ids(run_id)
                ids['users']['member'] = 'existing-business-user'
                scope.private_json(path, ids, create=True)
                with self.assertRaises(scope.AcceptanceFailure):
                    scope.read_scope(path)

    def test_direct_rp_does_not_remove_request_guards(self):
        text = Path(__file__).with_name('oidc_browser_cdp.ps1').read_text()
        self.assertIn("$fixture.rpPublic -cne 'http://192.168.10.10:30083'", text)
        self.assertIn('if (-not $DirectRP -and -not $script:relayEvidence.loopback_listener_seen)', text)
        self.assertIn('function Allowed($request)', text)
        self.assertIn('$body.password -ceq $fixture.password', text)

    def test_owned_admin_uses_explicit_buyer_context(self):
        text = Path(__file__).with_name('oidc_browser_cdp.ps1').read_text()
        self.assertIn("'/console?view=orders&enterprise_id=' + $fixture.enterpriseId", text)
        self.assertIn('function OpenAdminApplications', text)


if __name__ == '__main__':
    unittest.main()
