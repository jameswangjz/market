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
    @staticmethod
    def fault_manifest(kind, *, include_kind=True):
        run_id = 'qa-vcs-abcdef012345'
        ids = fault.base.fixture_ids(run_id)
        combo_id = run_id + '-combo-' + kind
        ids['database_ids'] = {
            'fault_order': run_id + '-order-' + kind,
            'changes': {kind: 'a' * 32},
            'orders': {kind: run_id + '-order-' + kind},
            'combinations': {kind: {'id': combo_id, 'product_id': ids['products'][kind],
                                    'enterprise_id': ids['enterprises']['buyer']}},
        }
        ids['consumers'] = [{'id': 'cred-' + kind,
            'name': 'market-consumer-cred-' + kind,
            'route': ids['routes'][kind]['source'],
            'allowed_routes': list(ids['routes'][kind].values())}]
        value = {'base': ids, 'fault': 'restart', 'phase': 'handoff'}
        if include_kind:
            value['resource_kind'] = kind
        return value

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

    def test_fault_manifest_defaults_legacy_scope_to_api_and_accepts_model_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            api_path = Path(directory) / 'api.json'
            fault.save(api_path, self.fault_manifest('api', include_kind=False), create=True)
            api_manifest = fault.load(api_path)
            self.assertEqual(api_manifest.get('resource_kind', 'api'), 'api')

            model_path = Path(directory) / 'model.json'
            fault.save(model_path, self.fault_manifest('model'), create=True)
            self.assertEqual(fault.load(model_path)['resource_kind'], 'model')

    def test_fault_manifest_rejects_cross_resource_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'model.json'
            value = self.fault_manifest('model')
            value['base']['database_ids']['changes']['api'] = 'b' * 32
            fault.save(path, value, create=True)
            with self.assertRaises(fault.AcceptanceFailure):
                fault.load(path)

    def test_fault_manifest_rejects_model_resource_with_api_route_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'model.json'
            value = self.fault_manifest('model')
            value['fault_route_key'] = value['base']['routes']['api']['target']
            value['target_route_original'] = {'labels': {
                'market_product_id': value['base']['products']['api']}}
            fault.save(path, value, create=True)
            with self.assertRaises(fault.AcceptanceFailure):
                fault.load(path)


if __name__ == '__main__':
    unittest.main()
