"""Offline guards and opt-in real PG FILE execution in a fresh subprocess.

R3_FILE_DATABASE_URL must point to a disposable local database accepted by the
original harness (including its protected count-only sentinel). No remote tests.
"""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import r3_file_refund_pg as probe


class FileProbeTests(unittest.TestCase):
    def test_manifest_contains_real_file_execution_and_storage_gates(self):
        for name in ('refund_file_resources.py', 'refund_execution.py', 'refund_execution_worker.py',
                     'refund_planning.py', 'refund_settlement.py', 'main.py', 'online_delivery.py'):
            self.assertIn(name, probe.SOURCE_FILES)
        root = Path(__file__).resolve().parents[1]
        encoded, manifest = probe.transport.pack_workingtree(root)
        self.assertEqual(manifest['refund_file_resources.py'], hashlib.sha256(
            (root / 'backend/app/refund_file_resources.py').read_bytes()).hexdigest())
        self.assertTrue(set(probe.SOURCE_FILES) <= manifest.keys())
        with tempfile.TemporaryDirectory() as directory:
            changed = dict(manifest, **{'refund_file_resources.py': '0' * 64})
            with self.assertRaises(Exception):
                probe.transport.unpack_workingtree(encoded, changed, Path(directory) / 'app')

    def test_mock_storage_only_accepts_exact_owned_identity_and_bytes(self):
        storage = probe.OwnedStorage('qa/file-refund/owned')
        body = storage.get_object('owned-file-refund', storage.name)
        self.assertEqual(body.read(), probe.CONTENT)
        body.close()
        body.release_conn()
        for bucket, name in (('production', storage.name), ('owned-file-refund', 'other')):
            with self.assertRaises(probe.base.AcceptanceFailure):
                storage.get_object(bucket, name)
        self.assertFalse(hasattr(storage, 'remove_object'))

    def test_local_runner_rejects_remote_and_search_path_options_before_packaging(self):
        with patch.object(probe.transport, 'pack_workingtree') as pack:
            for url in ('sqlite://', 'postgresql://user@remote.invalid/db',
                        'postgresql://user@127.0.0.1/db?options=-csearch_path=public'):
                with self.assertRaises(probe.base.AcceptanceFailure):
                    probe.run(url)
            pack.assert_not_called()

    def test_invalid_pod_and_missing_private_password_stop_before_packaging(self):
        with patch.object(probe.transport, 'pack_workingtree') as pack, patch.dict(os.environ, {}, clear=True):
            for pod in ('market-api-x; unsafe', 'another-service', 'market-api-owned'):
                with self.assertRaises(probe.base.AcceptanceFailure):
                    probe.pod_run(pod)
            pack.assert_not_called()

    def test_output_refuses_overwrite_before_any_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'evidence.json'
            path.touch()
            with patch.object(sys, 'argv', ['probe', '--pod', 'market-api-owned', '--output', str(path)]), \
                    patch.object(probe, 'pod_run') as run, redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(), 1)
            run.assert_not_called()
            self.assertEqual(path.read_bytes(), b'')

    def test_output_writes_final_sanitized_json_and_suppresses_private_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'evidence.json'
            with patch.object(sys, 'argv', ['probe', '--pod', 'market-api-owned', '--output', str(output)]), \
                    patch.object(probe, 'pod_run', side_effect=RuntimeError('private-secret-database-url')), \
                    redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(probe.main(), 1)
            result = json.loads(output.read_text())
            self.assertEqual(result['error_type'], 'RuntimeError')
            self.assertNotIn('private-secret', output.read_text() + stdout.getvalue())

    def test_pod_transport_payload_compiles_and_has_bounded_original_transport(self):
        manifest = {name: 'a' * 64 for name in probe.SOURCE_FILES}
        with patch.dict(os.environ, SSHPASS='private-fixture'), \
                patch.object(probe.transport, 'pack_workingtree', return_value=('encoded', manifest)), \
                patch.object(probe.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout='{"status":"passed"}')) as run:
            self.assertEqual(probe.pod_run('market-api-owned'), dict(status='passed'))
        args, kwargs = run.call_args
        self.assertEqual(kwargs['timeout'], 180)
        self.assertEqual(args[0][:3], ['sshpass', '-e', 'ssh'])
        self.assertIn('--request-timeout=5s', args[0][-1])
        self.assertNotIn('private-fixture', repr(args) + kwargs['input'])
        compile(kwargs['input'], '<file-probe-transport>', 'exec')

    def test_wrapper_reuses_original_guards_and_declares_mock_scan_and_funds(self):
        manifest = {name: 'a' * 64 for name in probe.SOURCE_FILES}
        result = dict(status='passed')
        original_seed = probe.cross.seed
        def checked(*args):
            self.assertIs(probe.cross.seed, probe.seed)
            self.assertIs(probe.cross.exercise, probe.exercise)
            self.assertEqual(probe.cross.CASE_NAMES, probe.CASES)
            self.assertEqual(probe.cross.SOURCE_FILES, probe.SOURCE_FILES)
            return result
        with patch.object(probe.cross, 'remote_run', side_effect=checked):
            actual = probe.remote_run('encoded', manifest)
        self.assertIs(probe.cross.seed, original_seed)
        self.assertIn('Seeded', actual['scan_evidence'])
        self.assertIn('no MinIO network', actual['external_adapters']['storage'])
        self.assertIn('no bank', actual['external_adapters']['funds'])


@unittest.skipUnless(os.getenv('R3_FILE_DATABASE_URL'), 'Requires explicit disposable local PostgreSQL URL')
class RealPostgreSQLTests(unittest.TestCase):
    def test_actual_worker_and_independent_pg_lock_waits(self):
        root = Path(__file__).resolve().parents[1]
        process = subprocess.run([sys.executable, '-B', str(root / 'scripts/r3_file_refund_pg.py')],
            cwd=root, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), capture_output=True,
            text=True, timeout=175)
        self.assertEqual(process.returncode, 0, process.stdout)
        result = json.loads(process.stdout)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['cleanup'], 'owned schema removed')
        self.assertEqual(result['protected_order_count_before'], 1)
        self.assertEqual(result['protected_order_count_after'], 1)
        self.assertFalse(result['protected_order_content_read'])
        cases = result['cases']
        self.assertTrue(all(case['status'] == 'passed' for case in cases.values()))
        self.assertEqual(cases['download_recovery_serialization']['server_lock_waits_observed'], 2)
        self.assertEqual(cases['refund_budget_two_connections']['independent_pg_connections'], 2)
        self.assertEqual(cases['file_two_partials_delayed_settlement']['total_cost'], '-33.33')
        self.assertTrue(cases['funds_failure_retry']['receipt_stable'])
        self.assertTrue(cases['proof_phase_rollback']['durable_receipt_preserved'])


if __name__ == '__main__':
    unittest.main()
