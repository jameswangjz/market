"""Offline checks; never invoke a real kubectl."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from redis_persistence_migrate import Migration, main, verify_snapshot


class MigrationTests(unittest.TestCase):
    def test_migration_order_and_private_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            migration = Migration(directory, Path(directory) / 'report')
            deployment = {'spec': {'replicas': 1, 'template': {'spec': {
                'containers': [{'name': 'redis', 'image': 'redis:7-alpine'}]}}}}
            pods = {'items': [{'metadata': {'name': 'old', 'uid': 'original'},
                               'spec': {'nodeName': 'node'}}]}
            calls = []
            def command(*args, **kwargs):
                calls.append(args)
                if kwargs.get('destination'):
                    kwargs['destination'].write(b'RDB')
                    return ''
                if args[:3] == ('exec', migration.helper, '--') and args[3] == 'sha256sum':
                    import hashlib
                    return hashlib.sha256(b'RDB').hexdigest() + ' /data/dump.rdb'
                return ''
            def snapshot(pod, port):
                calls.append(('snapshot', port))
                return {'keys': {}, 'now_ms': 1}
            def redis(pod, port, *args, **kwargs):
                calls.append(('redis', port, *args))
                return 'OK'
            service = {'spec': {'selector': {'app': 'market-redis'}}}
            with patch.object(migration, 'obj', side_effect=[deployment, pods, service]), \
                    patch.object(migration, 'command', side_effect=command), \
                    patch.object(migration, 'wait_ready', return_value='new'), \
                    patch.object(migration, 'redis', side_effect=redis), \
                    patch.object(migration, 'snapshot', side_effect=snapshot), \
                    patch.object(migration, 'aof_ready', return_value=True), \
                    patch.object(migration, 'wait_service_isolated'), \
                    patch.object(migration, 'patch', side_effect=lambda p: calls.append(('patch', p))), \
                    patch.object(migration, 'cleanup'):
                self.assertTrue(migration.run())
            stages = [('redis', 6379, 'CLIENT', 'PAUSE', '120000', 'WRITE'),
                      ('snapshot', 6379), ('redis', 6379, 'SAVE'), ('patch', 6380),
                      ('snapshot', 6380), ('redis', 6380, 'CONFIG', 'SET', 'appendonly', 'yes'),
                      ('patch', 6379)]
            positions = [calls.index(stage) for stage in stages]
            self.assertEqual(positions, sorted(positions))
            selector_patches = [(i, json.loads(c[-1])) for i, c in enumerate(calls)
                                if c[:3] == ('patch', 'service', 'market-redis')]
            self.assertEqual(len(selector_patches), 2)
            self.assertLess(selector_patches[0][0], calls.index(('patch', 6380)))
            self.assertGreater(selector_patches[1][0], calls.index(('patch', 6379)))
            self.assertGreater(selector_patches[1][0], max(i for i, c in enumerate(calls) if c == ('snapshot', 6379)))
            self.assertEqual(selector_patches[1][1][-1]['value'], {'app': 'market-redis'})
            for name in ('dump.rdb', 'snapshot.json'):
                path = Path(migration.report['backup_dir']) / name
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_execute_required_before_any_command(self):
        with patch('redis_persistence_migrate.subprocess.run') as run:
            with self.assertRaises(SystemExit):
                main(['--backup-dir', '/unused', '--output', '/unused/report'])
            run.assert_not_called()

    def test_existing_output_never_touches_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report'
            output.write_text('keep')
            with patch('redis_persistence_migrate.subprocess.run') as run:
                with self.assertRaises(FileExistsError):
                    Migration(directory, output).run()
                run.assert_not_called()
            self.assertEqual(output.read_text(), 'keep')

    def test_existing_pvc_refuses_before_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            migration = Migration(directory, Path(directory) / 'report')
            deployment = {'spec': {'template': {'spec': {'volumes': [
                {'persistentVolumeClaim': {'claimName': 'market-redis-data'}}]}}}}
            with patch.object(migration, 'obj', return_value=deployment), patch.object(migration, 'command') as cmd:
                self.assertFalse(migration.run())
                cmd.assert_not_called()
            self.assertIn('already migrated', migration.report['error'])
            self.assertEqual(os.stat(migration.output).st_mode & 0o777, 0o600)

    def test_snapshot_allows_only_natural_expiration(self):
        persistent = {'digest': 'd', 'expires': -1, 'kind': 'string'}
        expiring = {'digest': 'e', 'expires': 100, 'kind': 'string'}
        before = {'keys': {'a': persistent, 'b': expiring}, 'now_ms': 50}
        verify_snapshot(before, {'keys': {'a': persistent}, 'now_ms': 101})
        for after in ({'keys': {}, 'now_ms': 101},
                      {'keys': {'a': persistent}, 'now_ms': 99},
                      {'keys': {'a': {**persistent, 'digest': 'changed'}}, 'now_ms': 101}):
            with self.assertRaises(RuntimeError):
                verify_snapshot(before, after)

    def test_cleanup_requires_exact_owned_helper(self):
        migration = Migration('/unused', '/unused/report')
        migration.helper_created = True
        for owner, expected in [('other', 1), (migration.owner, 3)]:
            pod = {'metadata': {'name': migration.helper,
                               'labels': {'market-redis-migration': owner}}}
            replies = [json.dumps(pod), '', ''] if expected == 3 else [json.dumps(pod)]
            with patch.object(migration, 'command', side_effect=replies) as cmd:
                migration.cleanup()
                self.assertEqual(cmd.call_count, expected)
                if expected == 3:
                    self.assertEqual(cmd.call_args_list[1].args[:3], ('delete', 'pod', migration.helper))
                    self.assertIn('--wait=true', cmd.call_args_list[1].args)
                    self.assertTrue(migration.report['helper_cleanup_verified'])

    def test_failure_after_pause_unpauses_original_uid_only(self):
        migration = Migration('/unused', '/unused/report')
        migration.old = {'name': 'old', 'uid': 'original'}
        migration.paused = True
        for uid, expected in [('replacement', 0), ('original', 1)]:
            with patch.object(migration, 'command', return_value=json.dumps({'metadata': {'uid': uid}})), \
                    patch.object(migration, 'redis', return_value='OK') as redis:
                migration.cleanup()
                self.assertEqual(redis.call_count, expected)
                if expected:
                    redis.assert_called_once_with('old', 6379, 'CLIENT', 'UNPAUSE', bounded=False)

    def test_failure_keeps_service_isolated_unless_original_live(self):
        for metadata, restored in [({'uid': 'original'}, True),
                                   ({'uid': 'other'}, False),
                                   ({'uid': 'original', 'deletionTimestamp': 'now'}, False)]:
            with self.subTest(metadata=metadata):
                migration = Migration('/unused', '/unused/report')
                migration.old = {'name': 'old', 'uid': 'original'}
                migration.paused = True
                migration.original_selector = {'app': 'market-redis', 'extra': 'keep'}
                migration.service_isolated = True
                def command(*args, **kwargs):
                    if args[0] == 'get':
                        return json.dumps({'metadata': metadata})
                    return ''
                with patch.object(migration, 'command', side_effect=command) as cmd, \
                        patch.object(migration, 'redis', return_value='OK'):
                    migration.cleanup()
                patches = [c for c in cmd.call_args_list if c.args[0] == 'patch']
                self.assertEqual(bool(patches), restored)
                self.assertEqual(migration.report['service_isolated'], not restored)
                if restored:
                    self.assertEqual(json.loads(patches[0].args[-1])[-1]['value'], migration.original_selector)
                else:
                    self.assertIn('manual_recovery', migration.report)

    def test_helper_delete_timeout_is_not_verified(self):
        migration = Migration('/unused', '/unused/report')
        migration.helper_created = True
        pod = {'metadata': {'name': migration.helper, 'labels': {'market-redis-migration': migration.owner}}}
        with patch.object(migration, 'command', side_effect=[json.dumps(pod), RuntimeError('timeout')]):
            migration.cleanup()
        self.assertFalse(migration.report['helper_cleanup_verified'])

    def test_patch_is_deployment_only_and_isolates_restore(self):
        migration = Migration('/unused', '/unused/report')
        for port in (6380, 6379):
            with patch.object(migration, 'command') as cmd:
                migration.patch(port)
                args = cmd.call_args.args
                self.assertEqual(args[:3], ('patch', 'deployment', 'market-redis'))
                spec = json.loads(args[-1])['spec']
                self.assertEqual(spec['strategy']['type'], 'Recreate')
                container = spec['template']['spec']['containers'][0]
                self.assertEqual('--appendonly' in container['args'], port == 6380)
                for probe in ('startupProbe', 'livenessProbe', 'readinessProbe'):
                    self.assertEqual(container[probe]['exec']['command'], ['redis-cli', '-p', str(port), 'ping'])


if __name__ == '__main__':
    unittest.main()
