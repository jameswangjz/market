"""Pure and mocked checkpoint tests; no production imports or live services."""

import ast
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from sqlalchemy import Column, String
from sqlalchemy.orm import declarative_base


SCRIPT = Path(__file__).parents[2] / 'scripts' / 'trading_phase12_checkpoint.py'
spec = importlib.util.spec_from_file_location('oidc_checkpoint_safety', SCRIPT)
checkpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)
TAG = '20261011154640-7c6035c'


def passed_evidence(**changes):
    return {'database': 'real PostgreSQL', 'status': 'passed',
            'cleanup': 'verified no owned rows or FK references remain', 'runtime_tag': TAG, **changes}


class CheckpointValidationTests(unittest.TestCase):
    def test_start_is_only_in_progress_15(self):
        update, verified = checkpoint.checkpoint_update('start')
        self.assertEqual(checkpoint.TASK_CODE, 'TRD-BE-014')
        self.assertEqual((update['status'], update['progress']), ('in_progress', 15))
        self.assertIsNone(verified)

    def test_finish_remains_partial_in_progress_65(self):
        update, verified = checkpoint.checkpoint_update('finish', evidence=passed_evidence(), runtime_tag=TAG)
        self.assertEqual((update['status'], update['progress']), ('in_progress', 65))
        for marker in ('browser login bridge', 'BE-015', 'QA-006', 'not done'):
            self.assertIn(marker, update['note'])
        self.assertEqual(verified['runtime_image_tag'], TAG)

    def test_rejects_failed_local_or_unclean_evidence(self):
        for changes in ({'status': 'failed'}, {'status': True}, {'database': 'SQLite'},
                        {'database': 'PostgreSQL'}, {'cleanup': None}, {'cleanup': True},
                        {'cleanup': 'deferred'}, {'cleanup': 'unverified'},
                        {'cleanup': 'verified_not_really'}, {'cleanup': 'verified: failed'},
                        {'cleanup': 'verified pending'}, {'cleanup': 'verified\nfailed'},
                        {'cleanup_error_type': 'RuntimeError'}, {'cleanup_check_failed': 'remaining rows'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                checkpoint.checkpoint_update('finish', evidence=passed_evidence(**changes), runtime_tag=TAG)

    def test_rejects_missing_and_malformed_evidence(self):
        for evidence in (None, [], 'passed', {}, {'status': 'passed'}):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                checkpoint.checkpoint_update('finish', evidence=evidence, runtime_tag=TAG)
        with self.assertRaises(ValueError):
            checkpoint.checkpoint_update('done')

    def test_runtime_format_and_timestamp(self):
        for tag in ('', None, 42, 'latest', '20261011154640-7C6035C',
                    '20261011154640-7c6035', '20261311154640-7c6035c',
                    '20261011154640-7c6035c\n', '20261011154640-' + 'a' * 41):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                checkpoint.validate_service_evidence(passed_evidence(), tag)

    def test_matching_tag_is_required_and_conflicts_are_rejected(self):
        for key in checkpoint.TAG_FIELDS:
            evidence = passed_evidence()
            del evidence['runtime_tag']
            evidence[key] = TAG
            self.assertEqual(checkpoint.validate_service_evidence(evidence, TAG)['runtime_image_tag'], TAG)
            evidence[key] = '20261011154640-abcdef0'
            with self.assertRaises(ValueError):
                checkpoint.validate_service_evidence(evidence, TAG)
        evidence = passed_evidence()
        del evidence['runtime_tag']
        with self.assertRaises(ValueError):
            checkpoint.validate_service_evidence(evidence, TAG)
        with self.assertRaises(ValueError):
            checkpoint.validate_service_evidence(passed_evidence(deploymentTag='wrong'), TAG)

    def test_accepts_verified_cleanup_and_does_not_copy_credentials(self):
        for cleanup in ('verified', 'verified exact owned DB rows and FK children absent'):
            verified = checkpoint.validate_service_evidence(
                passed_evidence(cleanup=cleanup, bearerToken='do-not-copy', password='do-not-copy'), TAG)
            self.assertNotIn('do-not-copy', json.dumps(verified))

    def test_invalid_cli_evidence_is_rejected_before_production_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'invalid.json'
            path.write_text(json.dumps(passed_evidence(database='SQLite')))
            with self.assertRaisesRegex(ValueError, 'real PostgreSQL'):
                checkpoint.main(['finish', '--service-evidence', str(path), '--runtime-tag', TAG])

    def test_no_top_level_production_import_or_lifespan(self):
        tree = ast.parse(SCRIPT.read_text())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, ('app', 'app.main', 'fastapi.testclient'))
        source = SCRIPT.read_text()
        self.assertNotIn('create_all', source)
        self.assertNotIn('with TestClient', source)


class CheckpointRecordingTests(unittest.TestCase):
    def setUp(self):
        base = declarative_base()

        class User(base):
            __tablename__ = 'fixture_checkpoint_user'
            id = Column(String, primary_key=True)
            email = Column(String)

        self.db = Mock()
        self.session = Mock()
        self.session.__enter__ = Mock(return_value=self.db)
        self.session.__exit__ = Mock(return_value=False)
        self.m = SimpleNamespace(SessionLocal=Mock(return_value=self.session), User=User,
                                 issue_token=Mock(return_value='private-fixture-token'), app=object())
        self.client = Mock()
        self.client.patch.return_value.status_code = 200
        self.client.get.return_value.status_code = 200
        self.client.get.return_value.json.return_value = {'counts': {}, 'tasks': []}
        self.factory = Mock(return_value=self.client)

    def test_only_one_be014_patch_and_snapshot_get(self):
        update, verified = checkpoint.checkpoint_update('finish', evidence=passed_evidence(), runtime_tag=TAG)
        snapshot = checkpoint.record_checkpoint(self.m, self.factory, update, verified)
        self.client.patch.assert_called_once_with('/api/development/tasks/TRD-BE-014',
            headers={'Authorization': 'Bearer private-fixture-token'}, json=update)
        self.client.get.assert_called_once_with('/api/development/tasks',
            headers={'Authorization': 'Bearer private-fixture-token'})
        self.client.close.assert_called_once()
        self.assertEqual(snapshot['batch_verification']['milestone'], 'partial')
        self.assertNotIn('private-fixture-token', json.dumps(snapshot))

    def test_missing_admin_does_not_create_client_or_update_tasks(self):
        self.db.scalar.return_value = None
        with self.assertRaisesRegex(ValueError, 'administrator missing'):
            checkpoint.record_checkpoint(self.m, self.factory, {}, None)
        self.factory.assert_not_called()
        self.m.issue_token.assert_not_called()

    def test_http_failures_close_client_and_do_not_update_another_task(self):
        update, verified = checkpoint.checkpoint_update('start')
        for operation in ('patch', 'get'):
            with self.subTest(operation=operation):
                self.client.reset_mock()
                self.client.patch.return_value.status_code = 200
                self.client.get.return_value.status_code = 200
                getattr(self.client, operation).return_value.status_code = 500
                with self.assertRaises(RuntimeError):
                    checkpoint.record_checkpoint(self.m, self.factory, update, verified)
                self.client.patch.assert_called_once()
                self.assertEqual(self.client.patch.call_args.args[0], '/api/development/tasks/TRD-BE-014')
                self.client.close.assert_called_once()
                if operation == 'patch':
                    self.client.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
