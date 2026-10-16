"""Evidence and transaction gates, without app imports or SQLAlchemy dependency."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

import r3_ops_readonly_inventory as inventory_helper
import r3_submit_ops_review as ops

REPO = Path(__file__).resolve().parents[1]
FILES = {
    'inventory': 'docs/evidence/46-ops-inventory-final-20261016082010.json',
    'restore': 'docs/evidence/45-ops-restore-final-20261016082000.json',
    'archive': 'docs/evidence/43-release-archive-20261016081630/manifest.json',
    'checklist': 'docs/24 R3前端分组验收清单与状态口径-20261016081130.md',
}


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = {}
        for kind, path in FILES.items():
            target = self.root / (kind + ('.md' if kind == 'checklist' else '.json'))
            target.write_bytes((REPO / path).read_bytes())
            self.paths[kind] = target
        self.snapshot = self.root / 'snapshot.json'
        self.snapshot.write_bytes((REPO / 'docs/evidence/40-release-live-20261016081035.json').read_bytes())
        self.output = self.root / 'result.json'
        self.proofs, self.bound = ops.load_evidence(self.paths, self.snapshot)

    def change(self, kind, action):
        value = json.loads(self.paths[kind].read_bytes())
        action(value)
        self.paths[kind].write_text(json.dumps(value))

    def reject(self):
        with self.assertRaises((ValueError, OSError, KeyError)):
            ops.load_evidence(self.paths, self.snapshot)

    def test_actual_evidence_and_full_hashes(self):
        self.assertEqual(self.proofs['archive']['status'], 'passed_with_findings')
        for kind, binding in self.bound.items():
            self.assertEqual(binding['sha256'], hashlib.sha256(Path(binding['path']).read_bytes()).hexdigest())

    def test_inventory_gates(self):
        original = self.paths['inventory'].read_bytes()
        mutations = [lambda v: v.update(status='failed'),
                     lambda v: v['migration'].update(executed=True),
                     lambda v: v['migration'].update(approved=True),
                     lambda v: v['legacy_candidates'][0].update(product_count=1),
                     lambda v: v['protected_order']['orders'].update(count=0),
                     lambda v: v['protected_order'].update(sha256='bad'),
                     lambda v: v['tickets']['missing_evidence_review'][0].update(disposition='migrated')]
        for change in mutations:
            with self.subTest(change=change):
                self.paths['inventory'].write_bytes(original)
                self.change('inventory', change)
                self.reject()

    def test_restore_gates(self):
        original = self.paths['restore'].read_bytes()
        for field, value in [('restore_status', 'failed'), ('network', 'host'), ('cleanup_verified', False),
                             ('observations_equal', False), ('historical_migration_executed', True),
                             ('backup_sha256', '0' * 64), ('after', {})]:
            with self.subTest(field=field):
                self.paths['restore'].write_bytes(original)
                self.change('restore', lambda v: v.update({field: value}))
                self.reject()

    def test_archive_cannot_claim_ready(self):
        self.change('archive', lambda v: v.update(release_ready=True))
        self.reject()

    def test_snapshot_tampering_rejected(self):
        self.snapshot.write_bytes(self.snapshot.read_bytes() + b'\n')
        self.reject()

    def test_checklist_missing_empty_or_no_header(self):
        for text in ('', '   ', 'not a checklist', 'Bearer sensitive'):
            self.paths['checklist'].write_text(text)
            self.reject()
        self.paths['checklist'].unlink()
        self.reject()

    def test_secret_duplicate_and_symlink(self):
        for raw in (b'{"status":"passed","password":"sensitive"}',
                    b'{"status":"passed","status":"passed"}', b'{"n":NaN}'):
            with self.assertRaises(ValueError):
                ops.decode(raw)
        self.paths['inventory'].unlink()
        self.paths['inventory'].symlink_to(self.snapshot)
        self.reject()

    def test_installed_source_verification(self):
        app = self.root / 'app'
        app.mkdir()
        (app / 'main.py').write_text('source\n')
        proofs = {'snapshot': {'source_sha256': {'main.py': hashlib.sha256(b'source\n').hexdigest()}}}
        ops.verify_sources(proofs, app / 'main.py')
        (app / 'main.py').write_text('changed\n')
        with self.assertRaises(ValueError):
            ops.verify_sources(proofs, app / 'main.py')
        (app / 'extra.py').write_text('')
        with self.assertRaises(ValueError):
            ops.verify_sources(proofs, app / 'main.py')

    def mock_runtime(self):
        self.m, self.db = MagicMock(), MagicMock()
        self.m.__file__ = '/app/app/main.py'
        self.m.now.return_value = datetime(2026, 10, 16, tzinfo=timezone.utc)
        self.m.SessionLocal.return_value.__enter__.return_value = self.db
        actor = types.SimpleNamespace(email='admin@market.local', platform_role='super_admin', is_active=True, activation_status='active')
        self.tasks = [types.SimpleNamespace(code=c, status='in_progress', progress=70, acceptance='original-' + c) for c in ops.CODES]
        self.db.scalars.return_value.all.side_effect = [[actor], self.tasks]
        sql = types.ModuleType('sqlalchemy')
        sql.select = MagicMock()
        patches = [patch.dict(sys.modules, {'sqlalchemy': sql}),
                   patch.object(ops, 'verify_sources'),
                   patch.object(ops, 'protected_snapshot', return_value={'count': 1, 'sha256': 'a' * 64}),
                   patch.object(inventory_helper, 'current_types', return_value=['api']),
                   patch.object(inventory_helper, 'collect', return_value=self.proofs['inventory']),
                   patch.object(inventory_helper, 'inventory', return_value=self.proofs['inventory'])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_exact_task_transitions_and_audits(self):
        self.mock_runtime()
        result = ops.submit(self.m, self.paths, self.output, self.snapshot)
        self.assertEqual([(t.status, t.progress) for t in self.tasks], [('review', 100), ('review', 100), ('in_progress', 90)])
        self.assertEqual(result['review_submitted'], 2)
        self.assertFalse(result['release_ready'])
        self.assertEqual(self.m.audit.call_count, 3)
        for task, call in zip(self.tasks, self.m.audit.call_args_list):
            self.assertTrue(task.acceptance.startswith('original-' + task.code + '\n'))
            self.assertEqual(call.kwargs['before']['status'], 'in_progress')
            self.assertIn('waiting for explicit user', call.kwargs['after']['acceptance'])
            self.assertEqual(set(call.kwargs['after']['evidence_sha256']), set(self.bound))
        self.db.commit.assert_called_once()
        self.assertEqual(json.loads(self.output.read_text())['protected_before'], result['protected_after'])

    def test_wrong_task_state_never_commits(self):
        self.mock_runtime()
        self.tasks[0].status = 'done'
        with self.assertRaises(ValueError):
            ops.submit(self.m, self.paths, self.output, self.snapshot)
        self.db.commit.assert_not_called()
        self.m.audit.assert_not_called()

    def test_changed_financial_inventory_never_mutates(self):
        self.mock_runtime()
        changed = copy.deepcopy(self.proofs['inventory'])
        changed['all_frozen_economics']['sha256'] = '0' * 64
        inventory_helper.collect.return_value = changed
        with self.assertRaises(ValueError):
            ops.submit(self.m, self.paths, self.output, self.snapshot)
        self.assertFalse(self.output.exists())
        self.m.audit.assert_not_called()
        self.db.commit.assert_not_called()

    def test_protected_change_after_flush_prevents_commit(self):
        self.mock_runtime()
        ops.protected_snapshot.side_effect = [{'count': 1, 'sha256': 'a' * 64}, {'count': 1, 'sha256': 'b' * 64}]
        with self.assertRaises(ValueError):
            ops.submit(self.m, self.paths, self.output, self.snapshot)
        self.db.commit.assert_not_called()

    def test_existing_output_preserved(self):
        self.mock_runtime()
        self.output.write_text('keep')
        with self.assertRaises(FileExistsError):
            ops.submit(self.m, self.paths, self.output, self.snapshot)
        self.assertEqual(self.output.read_text(), 'keep')
        self.m.SessionLocal.assert_not_called()


if __name__ == '__main__':
    unittest.main()
