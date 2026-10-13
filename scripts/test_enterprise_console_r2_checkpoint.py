import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import enterprise_console_r2_checkpoint as checkpoint


class Batch2CheckpointTests(unittest.TestCase):
    def evidence(self):
        return {
            'status': 'passed',
            'backend': {'tests': 99, 'failures': 0, 'errors': 0},
            'postgres': {'status': 'passed', 'cleanup_verified': True},
            'frontend': {'status': 'passed'},
            'deployment': {'tag': 'verified-parent-tag'},
            'coverage': list(checkpoint.R2_CODES),
        }

    def test_all_twenty_tasks_and_explicit_remaining_gaps(self):
        updates = checkpoint.batch2_updates(self.evidence())
        self.assertEqual(len(updates), 20)
        for code in checkpoint.R2_CODES:
            expected = {
                'TRD-R2-BE-008': ('in_progress', 85),
                'TRD-R2-QA-002': ('in_progress', 80),
                'TRD-R2-ARC-002': ('in_progress', 85),
            }.get(code, ('review', 100))
            self.assertEqual(updates[code][:2], expected)
            self.assertIn('gpt6.1-sol low', updates[code][2])
            self.assertIn('模拟测试不代表真实外部', updates[code][2])

    def test_uncovered_tasks_keep_persisted_status_and_progress(self):
        evidence = self.evidence()
        evidence['coverage'] = ['TRD-R2-BE-003']
        updates = checkpoint.batch2_updates(evidence)
        self.assertEqual(updates['TRD-R2-BE-003'][:2], ('review', 100))
        for code in ('TRD-R2-BE-001', 'TRD-R2-FE-001', 'TRD-R2-OPS-001', 'TRD-R2-QA-001'):
            self.assertEqual(updates[code][:2], (None, None))

    def test_evidence_fails_closed(self):
        cases = [
            ('status', 'failed'), ('backend.tests', 0), ('backend.tests', True),
            ('backend.failures', 1), ('backend.errors', 1), ('backend.errors', '0'),
            ('postgres.status', 'failed'), ('postgres.cleanup_verified', False),
            ('postgres.cleanup_verified', 'true'), ('frontend.status', 'failed'),
            ('deployment.tag', ' '), ('coverage', ['BE-003']), ('coverage', {}),
        ]
        for field, value in cases:
            evidence = copy.deepcopy(self.evidence())
            target = evidence
            parts = field.split('.')
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                checkpoint.batch2_updates(evidence)
        for field in ('status', 'backend', 'postgres', 'frontend', 'deployment'):
            evidence = self.evidence()
            del evidence[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                checkpoint.batch2_updates(evidence)

    def test_missing_evidence_rejects_before_application_import(self):
        result = subprocess.run(
            [sys.executable, str(Path(checkpoint.__file__)), '--batch2', '--output', 'unused.json'],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn('--evidence is required', result.stderr)
        self.assertNotIn('ModuleNotFoundError', result.stderr)

    def test_batch1_verified_remains_available_without_evidence(self):
        db = MagicMock()
        db.__enter__.return_value = db
        db.scalar.side_effect = [SimpleNamespace(id='actor'), *[
            SimpleNamespace(status='todo', progress=0) for _ in range(4)]]
        app = SimpleNamespace(
            SessionLocal=Mock(return_value=db), User=SimpleNamespace(email='email'),
            DevelopmentTask=SimpleNamespace(code='code'),
            DevelopmentTaskUpdate=lambda **kwargs: SimpleNamespace(**kwargs),
            update_development_task=Mock(),
            development_tasks=Mock(return_value={'counts': {}, 'items': []}),
        )
        with patch.dict(sys.modules, {'app': SimpleNamespace(main=app)}), \
                patch('sqlalchemy.select', return_value=MagicMock()), \
                patch.object(sys, 'argv', ['checkpoint', '--verified', '--output', 'unused.json']), \
                patch.object(Path, 'write_text'), patch('builtins.print'), patch.dict('os.environ'):
            checkpoint.main()
        calls = {call.args[0]: call.args[1] for call in app.update_development_task.call_args_list}
        self.assertEqual(set(calls), {'TRD-R2-ARC-001', 'TRD-R2-FE-001', 'TRD-R2-FE-002', 'TRD-R2-FE-003'})
        self.assertEqual((calls['TRD-R2-FE-001'].status, calls['TRD-R2-FE-001'].progress),
                         ('in_progress', 80))

    def test_done_tasks_untouched_and_uncovered_progress_preserved(self):
        evidence = self.evidence()
        evidence['coverage'] = []
        tasks = [SimpleNamespace(code=code, status='in_progress', progress=35)
                 for code in checkpoint.R2_CODES]
        tasks[0].status = 'done'
        tasks[0].progress = 100
        db = MagicMock()
        db.scalar.side_effect = [SimpleNamespace(id='actor'), *tasks]
        app = SimpleNamespace(
            SessionLocal=Mock(return_value=db), User=SimpleNamespace(email='email'),
            DevelopmentTask=SimpleNamespace(code='code'),
            DevelopmentTaskUpdate=lambda **kwargs: SimpleNamespace(**kwargs),
            update_development_task=Mock(),
            development_tasks=Mock(return_value={'counts': {}, 'items': []}),
        )
        db.__enter__.return_value = db
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_path = root / 'evidence.json'
            evidence_path.write_text(json.dumps(evidence), encoding='utf-8')
            with patch.dict(sys.modules, {'app': SimpleNamespace(main=app)}), \
                    patch('sqlalchemy.select', return_value=MagicMock()), \
                    patch.object(sys, 'argv', ['checkpoint', '--batch2', '--evidence',
                                              str(evidence_path), '--output', str(root / 'tasks.json')]), \
                    patch('builtins.print'), patch.dict('os.environ'):
                checkpoint.main()
            self.assertTrue((root / 'tasks.json').exists())
        calls = {call.args[0]: call.args[1] for call in app.update_development_task.call_args_list}
        self.assertEqual(len(calls), 19)
        self.assertNotIn('TRD-R2-ARC-001', calls)
        self.assertEqual((calls['TRD-R2-FE-001'].status, calls['TRD-R2-FE-001'].progress),
                         ('in_progress', 35))


if __name__ == '__main__':
    unittest.main()
