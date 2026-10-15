"""Offline ownership and direct-RP safety guards; no application imports."""
from pathlib import Path
import tempfile
import unittest
import importlib.util
import json
from types import SimpleNamespace
from unittest.mock import patch

import version_change_quote_acceptance as scope
import oidc_browser_acceptance as browser


class OwnershipTests(unittest.TestCase):
    def test_cleanup_against_actual_mock_store(self):
        spec = importlib.util.spec_from_file_location('qa_mock_store',
            Path(__file__).resolve().parents[1] / 'mock-saas/app/store.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / 'scope.json'
            ids = scope.fixture_ids('qa-r3-sso-123456abcdef')
            scope.private_json(manifest, ids, create=True)
            owned = dict(run_id=ids['run_id'], product_id=ids['products']['tenant_access'],
                         enterprise_id=ids['enterprises'][0], tenant_id=ids['run_id'] + '-tenant',
                         subscription_id='owned-subscription', client_id='oidc_' + 'a' * 32,
                         rp_base='http://market-mock-saas:8200', rp_cleanup_verified=False)
            scope.private_json(browser.control_path(manifest), owned, create=True)
            store = module.Store(str(Path(root) / 'mock.sqlite3'))
            with store.connect() as db:
                db.execute('INSERT INTO clients VALUES (?,?)', (owned['client_id'], json.dumps(owned)))
                db.execute('INSERT INTO clients VALUES (?,?)', ('foreign', '{}'))
                db.execute('INSERT INTO tenants VALUES (?,?)', (owned['tenant_id'], json.dumps(owned)))
                identity = {**owned, 'sub': ids['users']['member']}
                db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',
                           ('owned-session', owned['client_id'], json.dumps(identity), 'private-token', 1))
                db.execute('INSERT INTO mappings VALUES (?,?,?,?,?)',
                           (owned['client_id'], identity['sub'], owned['tenant_id'],
                            owned['enterprise_id'], owned['subscription_id']))
            with patch.dict(browser.os.environ, {'MOCK_SAAS_DB_PATH': str(store.path)}):
                browser.cleanup_rp(SimpleNamespace(manifest=str(manifest), rp_store=str(store.path)))
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT client_id FROM clients').fetchall()[0][0], 'foreign')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
            self.assertTrue(browser.read_control(manifest, ids)['rp_cleanup_verified'])

    def test_state_allowlist_rejects_before_application_import(self):
        for state in ('disable-user', '', 'restore-all', 'account-disabled;anything'):
            with self.subTest(state=state), patch.object(scope, 'load_main') as load:
                with self.assertRaises(scope.AcceptanceFailure):
                    browser.set_owned_state(SimpleNamespace(set_owned_state=state))
                load.assert_not_called()

    def test_control_requires_exact_owned_subscription_context(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / 'scope.json'
            ids = scope.fixture_ids('qa-r3-sso-123456abcdef')
            owned = dict(run_id=ids['run_id'], product_id=ids['products']['tenant_access'],
                         enterprise_id=ids['enterprises'][0], tenant_id=ids['run_id'] + '-tenant',
                         subscription_id='owned-subscription', client_id='oidc_' + 'a' * 32,
                         rp_base='http://market-mock-saas:8200')
            scope.private_json(browser.control_path(manifest), owned, create=True)
            self.assertEqual(browser.read_control(manifest, ids), owned)
            for key in ('run_id', 'product_id', 'enterprise_id', 'tenant_id', 'client_id'):
                with self.subTest(key=key):
                    scope.private_json(browser.control_path(manifest), {**owned, key: 'foreign'})
                    with self.assertRaises(scope.AcceptanceFailure):
                        browser.read_control(manifest, ids)
            browser.control_path(manifest).chmod(0o644)
            with self.assertRaises(scope.AcceptanceFailure):
                browser.read_control(manifest, ids)

    def test_negative_scenarios_are_separate_from_baseline_evidence(self):
        text = Path(__file__).with_name('oidc_browser_cdp.ps1').read_text()
        for state in browser.OWNED_STATES:
            self.assertIn("'" + state + "'", text)
        self.assertIn("$result.application_access='not-exercised'", text)
        self.assertIn("$script:responses['/app'] -ne 401", text)
        self.assertIn("$_.name -ceq 'mock_saas_session'", text)

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
