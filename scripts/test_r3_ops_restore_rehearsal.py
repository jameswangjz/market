import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import r3_ops_restore_rehearsal as ops


class RestoreTests(unittest.TestCase):
    def run_case(self, fail_restore=False, wrong_owner=False, changed=False,
                 engine=None, missing=None, inspect_network=None,
                 network_label='["none"]'):
        calls = []
        expected_engine = engine or ['nerdctl', '--namespace', 'k8s.io']
        state = {'exists': False, 'owner': None, 'snapshots': 0}

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            self.assertEqual(argv[:len(expected_engine)], expected_engine)
            argv = [argv[0], *argv[len(expected_engine):]]
            self.assertLessEqual(kwargs['timeout'], 600)
            out = b''
            if argv[1] == 'run':
                state['exists'] = True
                state['name'] = argv[argv.index('--name') + 1]
                state['owner'] = argv[argv.index('--label') + 1].split('=', 1)[1]
                self.assertIn('--network=none', argv)
                self.assertNotIn('-p', argv)
                self.assertNotIn('-v', argv)
            elif argv[1] == 'inspect':
                out = json.dumps([{'Config': {'Labels': {ops.LABEL: 'other' if wrong_owner else state['owner'],
                                                        'nerdctl/networks': network_label}},
                                   **(inspect_network if inspect_network is not None else
                                      {'HostConfig': {'NetworkMode': 'none'}})}]).encode()
            elif argv[1] == 'ps':
                self.assertNotIn('--filter', argv)
                names = [state['name'] + '-unrelated', 'other-container']
                if state['exists']:
                    names.append(state['name'])
                out = '\n'.join(names).encode()
            elif argv[1] == 'rm':
                state['exists'] = False
            elif 'psql' in argv and 'stdin' in kwargs:
                if fail_restore:
                    raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
            elif 'input' in kwargs:
                query = kwargs['input'].decode()
                self.assertIn('REPEATABLE READ READ ONLY', query)
                self.assertIn("statement_timeout='4000ms'", query)
                self.assertTrue(query.endswith('ROLLBACK;\n'))
                if 'information_schema' in query:
                    rows = [{'table': t, 'column': c, 'numeric': False}
                            for t, cols in {'products': ['id', 'product_type', 'status'],
                                            'orders': ['product_id', 'main_status'],
                                            'file_objects': ['product_id', 'status'],
                                            'product_release_versions': ['product_id', 'status']}.items()
                            for c in cols if t != missing]
                else:
                    self.assertIn('data_report', query)
                    self.assertIn('row_content_md5', query)
                    self.assertIn('md5(to_jsonb(t)::text)', query)
                    self.assertIn('COLLATE "C"', query)
                    state['snapshots'] += 1
                    rows = [{'table': 'orders', 'rows': state['snapshots'] if changed else 3}]
                out = '\n'.join(json.dumps(r) for r in rows).encode()
            return subprocess.CompletedProcess(argv, 0, stdout=out)

        with tempfile.TemporaryDirectory() as folder:
            backup = Path(folder) / 'backup.sql'
            backup.write_text('-- plain SQL backup\n')
            with patch.object(ops.subprocess, 'run', side_effect=run):
                try:
                    result = ops.rehearse(backup, engine=engine)
                except (ValueError, subprocess.TimeoutExpired):
                    if not (fail_restore or wrong_owner or changed or missing == 'orders' or inspect_network is not None):
                        raise
                    result = None
        return result, state, calls

    def test_success_evidence_and_cleanup(self):
        result, state, _ = self.run_case()
        self.assertFalse(state['exists'])
        self.assertTrue(result['cleanup_verified'])
        self.assertFalse(result['production_rollback_verified'])
        self.assertFalse(result['synthetic'])
        self.assertEqual(len(result['backup_sha256']), 64)
        self.assertEqual(result['before'], result['after'])
        self.assertEqual(result['approval_manifest']['status'], 'proposed_only')
        self.assertFalse(result['historical_migration_executed'])
        self.assertEqual(result['restore_status'], 'passed')
        self.assertEqual(result['approval_manifest']['proposed_mapping'],
                         {kind: 'dataset' for kind in ops.TYPES})

    def test_explicit_engine_routes_all_commands(self):
        engine = ['nerdctl', '--namespace', 'k8s.io']
        result, state, calls = self.run_case(engine=engine)
        self.assertEqual(result['engine_argv'], engine)
        self.assertTrue(all(argv[:3] == engine for argv, _ in calls))
        self.assertFalse(state['exists'])

    def test_docker_explicit_override(self):
        result, _, _ = self.run_case(engine=['docker'])
        self.assertEqual(result['engine_argv'], ['docker'])

    def test_optional_inventory_gap(self):
        result, state, _ = self.run_case(missing='file_objects')
        self.assertEqual(result['before']['inventory_gaps'][0]['table'], 'file_objects')
        self.assertFalse(state['exists'])

    def test_required_orders_missing_fails(self):
        result, state, _ = self.run_case(missing='orders')
        self.assertIsNone(result)
        self.assertFalse(state['exists'])

    def test_invalid_engine_before_commands(self):
        with patch.object(ops.subprocess, 'run') as run:
            for engine in ([], '', 'nerdctl', [1], ['']):
                with self.assertRaises(ValueError):
                    ops.rehearse('unused', engine=engine)
            run.assert_not_called()

    def test_restore_timeout_cleans_up(self):
        result, state, _ = self.run_case(fail_restore=True)
        self.assertIsNone(result)
        self.assertFalse(state['exists'])

    def test_unknown_network_fails_but_owned_container_removed(self):
        result, state, _ = self.run_case(inspect_network={'HostConfig': {}})
        self.assertIsNone(result)
        self.assertFalse(state['exists'])

    def test_nerdctl_network_shape(self):
        result, state, _ = self.run_case(inspect_network={
            'HostConfig': {}, 'NetworkSettings': {'Networks': {}, 'IPAddress': ''}})
        self.assertIsNotNone(result)
        self.assertFalse(state['exists'])

    def test_nerdctl_network_conflicts_cleanup_owned_only(self):
        for network in ({'Networks': {'bridge': {}}, 'IPAddress': ''},
                        {'Networks': {}, 'IPAddress': '10.0.0.1'},
                        {'Networks': {}, 'IPAddress': '', 'Ports': {'5432/tcp': [{}]}},
                        {'IPAddress': ''}):
            result, state, calls = self.run_case(inspect_network={
                'HostConfig': {}, 'NetworkSettings': network})
            self.assertIsNone(result)
            self.assertFalse(state['exists'])
            removals = [argv for argv, _ in calls if 'rm' in argv]
            self.assertEqual(len(removals), 1)
            self.assertIn(state['name'], removals[0])

    def test_nerdctl_requires_explicit_none_label(self):
        for label in (None, 'invalid', '[]', '["bridge"]', '["none","bridge"]'):
            result, state, _ = self.run_case(network_label=label, inspect_network={
                'HostConfig': {}, 'NetworkSettings': {'Networks': {}, 'IPAddress': ''}})
            self.assertIsNone(result)
            self.assertFalse(state['exists'])

    def test_foreign_container_never_removed(self):
        _, state, calls = self.run_case(wrong_owner=True)
        self.assertTrue(state['exists'])
        self.assertFalse(any('rm' in argv for argv, _ in calls))

    def test_changed_summary_fails_and_cleans_up(self):
        result, state, _ = self.run_case(changed=True)
        self.assertIsNone(result)
        self.assertFalse(state['exists'])

    def test_reject_invalid_inputs_before_docker(self):
        with tempfile.TemporaryDirectory() as folder:
            backup = Path(folder) / 'backup'
            for content, image, timeout in [(b'', 'postgres:16', 120),
                                             (b'PGDMPdata', 'postgres:16', 120),
                                             (b'-- sql', 'arbitrary:latest', 120),
                                             (b'-- sql', 'postgres:16', 601)]:
                backup.write_bytes(content)
                with patch.object(ops.subprocess, 'run') as run:
                    with self.assertRaises(ValueError):
                        ops.rehearse(backup, image, timeout)
                    run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
