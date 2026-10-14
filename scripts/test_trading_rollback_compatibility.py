import json
import os
import subprocess
import unittest
from unittest.mock import patch

import trading_rollback_compatibility as compatibility


class CompatibilityTests(unittest.TestCase):
    container = 'market-rollback-' + 'a' * 32

    def execute(self, failure=None, mode='none', new_detail_checks=False):
        calls = []
        def command(args, **kwargs):
            calls.append((args, kwargs))
            if 'inspect' in args and 'image' not in args:
                output = [{'HostConfig': {'NetworkMode': mode}, 'Config': {
                    'Labels': {'market.isolated-rollback': 'true'}}, 'State': {'Running': True}}]
            elif 'image' in args:
                output = [{'Id': 'sha256:' + ('1' if args[-1] == 'new' else '2') * 64}]
            elif 'run' in args:
                self.assertTrue(kwargs['stdin'].read(20))
                if failure == 'run':
                    raise subprocess.CalledProcessError(1, args, stderr=b'sensitive response')
                extra = 2 if 'REHEARSAL_NEW_DETAIL_CHECKS=true' in args else 0
                output = {'protected_unchanged': True, 'http_get_checks': 7 + extra,
                          'shared_http_get_checks': 7, 'new_detail_capability_checks': extra,
                          **{key: 'a' * 64 for key in ('finance_read_sha256', 'provider_read_sha256',
                                                     'public_read_sha256', 'protected_sha256')}}
                if failure == 'mismatch' and 'sha256:' + '2' * 64 in args:
                    output['protected_sha256'] = 'b' * 64
                if failure == 'incomplete':
                    output['http_get_checks'] = 0
            else:
                return subprocess.CompletedProcess(args, 0, stdout=b'', stderr=b'')
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(output).encode(), stderr=b'')
        with patch.object(compatibility.subprocess, 'run', side_effect=command):
            if failure or mode != 'none':
                with self.assertRaises(subprocess.CalledProcessError if failure == 'run' else ValueError):
                    compatibility.run_images('old', 'new', ['nerdctl', '--namespace', 'k8s.io'], self.container)
            else:
                report = compatibility.run_images('old', 'new', ['nerdctl', '--namespace', 'k8s.io'], self.container,
                                                  new_detail_checks=new_detail_checks)
                self.assertTrue(report['cleanup_verified'])
                self.assertFalse(report['full_production_rollback'])
        return calls

    def test_isolated_immutable_images_and_cleanup(self):
        calls = self.execute()
        runs = [args for args, _ in calls if 'run' in args]
        self.assertEqual(len(runs), 2)
        for args in runs:
            self.assertIn('--network=container:' + self.container, args)
            self.assertIn('DATABASE_URL=' + compatibility.DATABASE_URL, args)
            self.assertIn('MESSAGE_CENTER_DISABLE_BACKGROUND=true', args)
            self.assertIn('--pull=never', args)
            self.assertNotIn('-p', args)
            self.assertNotIn('-v', args)
        self.assertIn('sha256:' + '1' * 64, runs[0])
        self.assertIn('sha256:' + '2' * 64, runs[1])
        self.assertEqual(sum('rm' in args for args, _ in calls), 2)
        self.assertEqual(sum('ps' in args for args, _ in calls), 2)

    def test_failed_probe_cleans_up(self):
        calls = self.execute(failure='run')
        self.assertIn('rm', calls[-2][0])
        self.assertIn('ps', calls[-1][0])

    def test_detail_checks_enabled_only_for_new_image(self):
        runs = [args for args, _ in self.execute(new_detail_checks=True) if 'run' in args]
        self.assertIn('REHEARSAL_NEW_DETAIL_CHECKS=true', runs[0])
        self.assertIn('REHEARSAL_NEW_DETAIL_CHECKS=false', runs[1])

    def test_shared_routes_do_not_require_old_image_detail_or_foreign_rows(self):
        calls = []
        def get(path, headers=None, expected=200):
            calls.append(path)
            if path.startswith('/api/enterprise-settlements/'):
                self.fail('old image lacks the new detail route')
            if expected != 200:
                return None
            if path == '/api/storefront/categories':
                return {'items': [{'id': 'public'}]}
            fields = compatibility.FINANCE_FIELDS if path == '/api/settlements' else compatibility.PROVIDER_FIELDS
            return {'items': [{key: 'own' if key == 'id' else 1 for key in fields}]}
        compatibility.read_checks(get, {}, {}, {'own'})
        self.assertEqual(len(calls), 7)
        with self.assertRaisesRegex(ValueError, 'isolated schema fixture'):
            compatibility.read_checks(get, {}, {}, {'own'}, new_detail_checks=True)

    def test_nonisolated_target_rejected_before_api_run(self):
        self.assertFalse(any('run' in args for args, _ in self.execute(mode='bridge')))

    def test_changed_fingerprint_and_incomplete_probe_rejected(self):
        self.execute(failure='mismatch')
        self.execute(failure='incomplete')

    def test_finance_shape_rejects_empty_missing_boolean_nan(self):
        for value in ({'items': []}, {'items': [{}]},
                      {'items': [{'id': 'x', 'provider_share': True}]},
                      {'items': [{'id': 'x', 'provider_share': float('nan')}]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                compatibility.validate_rows(value, ('id', 'provider_share'))
        rows = compatibility.validate_rows({'items': [{'id': 'b', 'provider_share': 2},
                                                      {'id': 'a', 'provider_share': 1}]},
                                           ('id', 'provider_share'))
        self.assertEqual([row['id'] for row in rows], ['a', 'b'])

    def test_probe_refuses_nonisolated_database_environment(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://live.invalid/db'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'isolated probe environment'):
                compatibility.probe()


if __name__ == '__main__':
    unittest.main()
