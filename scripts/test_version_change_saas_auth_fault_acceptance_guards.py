"""Offline ownership and scope guards for SaaS auth fault acceptance."""
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).with_name('version_change_saas_auth_fault_acceptance.py')
SPEC = importlib.util.spec_from_file_location('vcs_saas_auth_fault_acceptance', SCRIPT)
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


class SaaSAuthFaultAcceptanceGuards(unittest.TestCase):
    def test_fixture_identity_contract_is_reused_and_within_column_limits(self):
        ids = acceptance.base.fixture_ids('qa-vcs-saas-abcdef012345')
        self.assertEqual(ids['run_id'], 'qa-vcs-saas-abcdef012345')
        self.assertEqual(ids['versions']['source'], ids['run_id'] + '-v-source')
        identities = [*ids['users'].values(), *ids['enterprises'].values(), ids['product'],
                      *ids['versions'].values(), ids['oauth_client_id'], ids['request_id']]
        self.assertTrue(all(len(value) <= 36 for value in identities))

    def test_live_flow_reuses_runtime_fixture_and_real_resource_apply(self):
        source = SCRIPT.read_text()
        for fragment in ('base.preflight(m, ids)', 'base.prepare_fixture(m, ids)',
                         'base.worker_acceptance(m, ids, path, client, tokens)',
                         'base.cleanup(m, ids, path)',
                         "m.version_change_resources['apply'](db, change, 'target')",
                         "m.version_change_resources['apply'](db, change, 'original')",
                         "client.post('/oauth/revoke'", 'db.commit()', "plan['change_id'] = attempt_id",
                         "proof.get('request_change_id') == change.id",
                         "target_evidence.get('direction') == 'target'",
                         "original_evidence.get('direction') == 'original'",
                         'cleanup_auth_children(m, ids)', 'status_code == 401',
                         "rejected('expired_signed_warm_token'",
                         "rejected('insufficient_scope_for_change'",
                         'm.saas_access_token(config)',
                         "result['expired_401_cache_auto_evicted'] = False"):
            self.assertIn(fragment, source)

    def test_script_has_no_cluster_control_or_unscoped_delete_commands(self):
        source = SCRIPT.read_text().lower()
        for forbidden in ('kubectl', 'ssh ', 'flushdb', 'delete from ', 'pause_worker',
                          'version_change_worker_batch'):
            self.assertNotIn(forbidden, source)

    def test_transient_attempt_uses_fresh_identity_and_rebound_plan(self):
        source = SCRIPT.read_text()
        self.assertIn("attempt_id = secrets.token_hex(16)", source)
        self.assertIn("plan['change_id'] = attempt_id", source)
        self.assertIn('resource_plan_json=json.dumps(plan', source)
        self.assertIn('live_attempt = make_attempt(db, persisted', source)
        self.assertIn("db.get(Change, persisted_change_id)", source)

    def test_cleanup_callbacks_are_exactly_scoped_to_fixture_roots(self):
        source = SCRIPT.read_text()
        self.assertIn("m.OAuthClient.client_id == ids['oauth_client_id']", source)
        self.assertIn("m.OAuthClient.product_id == ids['product']", source)
        self.assertIn("m.OAuthRevokedToken.client_id == client_row_id", source)
        self.assertIn("m.AuditLog.target_id == client_row_id", source)
        self.assertIn('restore_owned_auth(m, ids)', source)


if __name__ == '__main__':
    unittest.main()
