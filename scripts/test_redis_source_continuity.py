"""Offline continuity diagnostics regression tests; no live Redis or database."""
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


spec = importlib.util.spec_from_file_location('continuity', Path(__file__).with_name('redis_source_continuity.py'))
continuity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(continuity)
NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


class ReadOnlyRedis:
    """Only the permitted read commands exist on this fake."""
    def __init__(self, source):
        self.physical = 'b' * 40
        self.marker = continuity.expected_marker(source, 'ready')
        self.events = [('1-0', {'private': 'SECRET_EVENT'})]
        self.exists = True

    def info(self, section):
        if section == 'server':
            return {'run_id': self.physical, 'secret': 'SECRET_INFO'}
        return dict(aof_enabled=1, aof_last_write_status='ok', loading=0,
                    secret='SECRET_INFO')

    def hgetall(self, key):
        assert key == continuity.EPOCH
        return dict(self.marker)

    def type(self, key):
        return 'stream' if self.exists else 'none'

    def xrange(self, key, min='-', max='+', count=None):
        rows = self.events if self.exists else []
        if min != '-':
            rows = [r for r in rows if r[0] == min == max]
        return copy.deepcopy(rows[:count] if count else rows)

    def xrevrange(self, key, count=None):
        return list(reversed(self.xrange(key)))[:count]

    def xlen(self, key):
        return len(self.events)

    def config_get(self, key):
        return {key: {'appendonly': 'yes', 'appendfsync': 'everysec',
                      'save': '', 'no-appendfsync-on-rewrite': 'no'}[key]}

    def pttl(self, key):
        return -1

    def scan_iter(self, match):
        return iter(())


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.source = SimpleNamespace(id='a' * 40, stream=continuity.STREAM + ':' + 'a' * 40,
            process_run_id='c' * 40, cutover=NOW, status='active', sealed_hash=None,
            marker_snapshot='{}')
        self.cursor = SimpleNamespace(last_id='1-0', redis_run_id=self.source.id, activated_at=NOW)
        self.client = ReadOnlyRedis(self.source)

    def report(self):
        return continuity.inspect_source(self.client, self.source, self.cursor,
                                         dict(total_charges=1, source_charges=1, gaps=1))

    def seal(self):
        self.source.status = 'paused'
        self.client.marker = continuity.expected_marker(self.source, 'paused')
        # Independent known encoding of the production seal format.
        frozen = dict(self.client.marker)
        frozen.pop('process_run_id')
        records = [(self.source.stream, self.client.events), ('marker', frozen)]
        self.source.sealed_hash = hashlib.sha256(json.dumps(
            records, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def test_intact_unsealed_aof_does_not_prove_continuity(self):
        result = self.report()
        self.assertEqual(result['status'], 'explicit_gap_required')
        self.assertFalse(result['continuity_proven'])
        self.assertIsNone(result['missing_count'])
        self.assertTrue(result['cursor']['entry_present'])
        self.assertEqual(result['persistence']['aof_enabled'], 1)
        self.assertIn('no_paused_seal_continuity_not_proven', result['reasons'])
        self.assertIn('physical_run_id_mismatch', result['reasons'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_missing_stream(self):
        self.client.exists = False
        result = self.report()
        self.assertEqual(result['status'], 'explicit_gap_required')
        self.assertIn('stream_missing_or_wrong_type', result['reasons'])
        self.assertIn('cursor_entry_missing', result['reasons'])

    def test_active_source_healthy_without_retrospective_guarantee(self):
        self.client.physical = self.source.process_run_id
        result = self.report()
        self.assertEqual(result['status'], 'active_source_healthy')
        self.assertEqual(result['reasons'], [])
        self.assertFalse(result['continuity_proven'])
        self.assertFalse(result['resume_authorized'])
        self.assertIsNone(result['missing_count'])

    def test_active_empty_stream_at_zero_cursor_is_healthy(self):
        self.client.physical = self.source.process_run_id
        self.cursor.last_id = '0-0'
        self.client.events = []
        for exists in (True, False):
            with self.subTest(exists=exists):
                self.client.exists = exists
                result = self.report()
                self.assertEqual(result['status'], 'active_source_healthy')
                self.assertEqual(result['reasons'], [])
                self.assertFalse(result['continuity_proven'])

    def test_active_health_requires_binding_cursor_and_persistence(self):
        self.client.physical = self.source.process_run_id
        for change in ('marker', 'cursor', 'registration', 'physical', 'loading', 'aof', 'write'):
            with self.subTest(change=change):
                source, cursor, client = copy.deepcopy((self.source, self.cursor, self.client))
                if change == 'marker':
                    client.marker['status'] = 'paused'
                elif change == 'cursor':
                    cursor.last_id = '2-0'
                elif change == 'registration':
                    cursor.redis_run_id = 'f' * 40
                elif change == 'physical':
                    client.physical = 'f' * 40
                else:
                    info = client.info
                    field, value = {'loading': ('loading', 1), 'aof': ('aof_enabled', 0),
                                    'write': ('aof_last_write_status', 'err')}[change]
                    client.info = lambda section: (info(section) | {field: value}
                                                    if section == 'persistence' else info(section))
                result = continuity.inspect_source(client, source, cursor, {})
                self.assertEqual(result['status'], 'explicit_gap_required')

    def test_active_health_requires_cursor_record(self):
        self.client.physical = self.source.process_run_id
        self.cursor = None
        self.assertEqual(self.report()['status'], 'explicit_gap_required')

    def test_marker_and_cursor_mismatch(self):
        self.client.marker['run_id'] = 'd' * 40
        self.cursor.redis_run_id = 'e' * 40
        result = self.report()
        self.assertIn('marker_mismatch', result['reasons'])
        self.assertIn('cursor_registration_mismatch', result['reasons'])
        self.assertEqual(result['status'], 'explicit_gap_required')

    def test_sealed_candidate_is_not_resume_authorization(self):
        self.seal()
        result = self.report()
        self.assertEqual(result['status'], 'safe_sealed_resume_candidate')
        self.assertTrue(result['seal_matches'])
        self.assertFalse(result['resume_authorized'])
        self.assertFalse(result['continuity_proven'])

    def test_changed_seal_and_undrained_cursor_rejected(self):
        self.seal()
        self.client.events.append(('2-0', {}))
        self.assertEqual(self.report()['status'], 'explicit_gap_required')
        self.seal()
        self.assertEqual(self.report()['status'], 'explicit_gap_required')

    def test_expiring_or_changing_seal_evidence_rejected(self):
        self.seal()
        self.client.pttl = lambda key: 1000
        self.assertIn('seal_verification_unavailable', self.report()['reasons'])
        self.client.pttl = lambda key: -1
        with patch.object(continuity, 'seal_digest', side_effect=[self.source.sealed_hash, 'changed']):
            self.assertEqual(self.report()['status'], 'explicit_gap_required')

    def test_seal_matches_production_implementation(self):
        # Extract only the nested pure hash function, without installing transition hooks.
        import ast
        production = Path(__file__).resolve().parents[1] / 'backend/app/api_charge_sources.py'
        tree = ast.parse(production.read_text())
        install = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'install')
        function = next(n for n in install.body if isinstance(n, ast.FunctionDef) and n.name == 'seal_hash')
        namespace = dict(hashlib=hashlib, json=json, epoch=continuity.EPOCH)
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(production), 'exec'), namespace)
        self.seal()
        self.assertEqual(continuity.seal_digest(self.client, self.source.stream),
                         namespace['seal_hash'](self.client, self.source))

    def test_missing_cursor_and_missing_generation(self):
        self.cursor = None
        self.assertEqual(self.report()['status'], 'explicit_gap_required')
        self.source = None
        self.assertIn('managed_source_missing', self.report()['reasons'])

    def test_run_changes_during_inspection(self):
        self.seal()
        info = self.client.info
        calls = []
        def changed(section):
            result = info(section)
            if section == 'server':
                calls.append(section)
                if len(calls) > 1:
                    result['run_id'] = 'f' * 40
            return result
        self.client.info = changed
        result = self.report()
        self.assertIn('source_changed_during_inspection', result['reasons'])
        self.assertEqual(result['status'], 'explicit_gap_required')

    def test_config_denied_does_not_leak_exception(self):
        self.client.config_get = MagicMock(side_effect=RuntimeError('SECRET_PASSWORD'))
        result = self.report()
        self.assertEqual(result['persistence_config'], {'available': False})
        self.assertNotIn('SECRET', json.dumps(result))

    def test_admin_authorization(self):
        for actor in (None, SimpleNamespace(platform_role='member', is_active=True),
                      SimpleNamespace(platform_role='super_admin', is_active=False)):
            with self.assertRaises(ValueError):
                continuity.require_admin(actor)
        continuity.require_admin(SimpleNamespace(platform_role='super_admin', is_active=True,
                                                 activation_status='active'))

    def test_admin_activation_status_must_be_active(self):
        for status in ('inactive', 'pending', None):
            with self.subTest(status=status), self.assertRaises(ValueError):
                continuity.require_admin(SimpleNamespace(platform_role='super_admin', is_active=True,
                                                         activation_status=status))
        with self.assertRaises(ValueError):
            continuity.require_admin(SimpleNamespace(platform_role='super_admin', is_active=True))

    def test_output_exclusive_private_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            continuity.write_report(path, self.report())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                continuity.write_report(path, {})
            link = Path(directory) / 'link.json'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                continuity.write_report(link, {})
            self.assertEqual(path.read_bytes(), original)

    def test_database_reads_only_and_counts(self):
        from sqlalchemy import Column, DateTime, MetaData, String, Table
        from sqlalchemy.orm import registry
        mapper = registry(metadata=MetaData())
        hooks = {}
        for name, fields in {
            'SourceGeneration': ['id', 'stream', 'cutover'], 'Cursor': ['stream'],
            'Charge': ['id', 'source_run_id'], 'Gap': ['id']}.items():
            model = type(name, (), {})
            table = Table(name, mapper.metadata, *[
                Column(field, DateTime(timezone=True) if field == 'cutover' else String,
                       primary_key=index == 0) for index, field in enumerate(fields)])
            mapper.map_imperatively(model, table)
            hooks[name] = model
        db = MagicMock()
        db.scalar.side_effect = [self.source, 7, 2, 1]
        db.get.return_value = self.cursor
        result = continuity.build_report(db, self.client, hooks)
        self.assertEqual(result['ledger'], dict(total_charges=7, gaps=2, source_charges=1))
        for call in db.scalar.call_args_list:
            self.assertTrue(call.args[0].is_select)
        for method in ('commit', 'flush', 'add', 'delete', 'execute'):
            getattr(db, method).assert_not_called()
        self.seal()
        db.scalar.side_effect = [self.source, 7, 2, 1, self.source]
        def change_cursor(row):
            if row is self.cursor:
                row.last_id = '2-0'
        db.refresh.side_effect = change_cursor
        result = continuity.build_report(db, self.client, hooks)
        self.assertEqual(result['status'], 'explicit_gap_required')
        self.assertIn('database_source_changed_during_inspection', result['reasons'])


if __name__ == '__main__':
    unittest.main()
