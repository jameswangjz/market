"""Standard-library tests: no app.main, database, network, or credentials."""
import copy
import importlib
from pathlib import Path
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = subprocess.check_output(['git', 'show', 'HEAD:scripts/trading_task_control.py'], cwd=ROOT)
EXPORTED = Path('/tmp/clr2-monitor-source-20261018/trading_task_control.py')
if EXPORTED.exists() and EXPORTED.read_bytes() != SOURCE:
    raise AssertionError('Exported trading controller differs from committed HEAD')
COMMITTED = ModuleType('trading_task_control')
COMMITTED.__file__ = str(EXPORTED) if EXPORTED.exists() else 'HEAD:scripts/trading_task_control.py'
exec(compile(SOURCE, COMMITTED.__file__, 'exec'), COMMITTED.__dict__)
with patch.dict(sys.modules, {'trading_task_control': COMMITTED}):
    monitor = importlib.import_module('clearing_v2_monitor')


class ClearingMonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        self.rows = [dict(code=t[0], status='todo', progress=0, dependencies=t[3],
                          acceptance=t[4], updated_at=self.now.isoformat()) for t in monitor.expected_tasks()]

    def test_selects_exactly_17_and_does_not_mutate_or_use_trd_validate(self):
        rows = self.rows + [dict(self.rows[0], code='TRD-BE-001', status='blocked')]
        before = copy.deepcopy(rows)
        with patch.object(COMMITTED, 'validate', side_effect=AssertionError('TRD-only validate')):
            result = monitor.inspect_tasks(rows, self.now)
        self.assertEqual((result['total'], result['expected_total']), (17, 17))
        self.assertEqual(sum(result['counts'].values()), 17)
        self.assertFalse(any(a['code'].startswith('TRD-') for a in result['alerts']))
        self.assertEqual(rows, before)

    def test_missing_task_is_reported(self):
        code = self.rows.pop()['code']
        result = monitor.inspect_tasks(self.rows, self.now)
        self.assertEqual(result['total'], 16)
        self.assertIn(dict(code=code, reason='missing_task'), result['alerts'])

    def test_inspect_uses_committed_copy_and_not_dirty_workspace(self):
        self.assertIs(monitor.inspect, COMMITTED.inspect)
        self.assertEqual(monitor.inspect.__code__.co_filename, COMMITTED.__file__)

    def test_stale_progress_and_boundary(self):
        row = self.rows[0]
        row.update(status='in_progress', progress=30, updated_at=(self.now - timedelta(minutes=61)).isoformat())
        self.assertIn('stale_progress', [a['reason'] for a in monitor.inspect_tasks(self.rows, self.now)['alerts']])
        row['updated_at'] = (self.now - timedelta(minutes=60)).isoformat()
        self.assertNotIn('stale_progress', [a['reason'] for a in monitor.inspect_tasks(self.rows, self.now)['alerts']])

    def test_dependency_and_metadata_drift_use_approved_graph(self):
        self.rows[1].update(status='in_progress', progress=30, dependencies='')
        alerts = monitor.inspect_tasks(self.rows, self.now)['alerts']
        self.assertIn('unfinished_dependency', [a['reason'] for a in alerts])
        self.assertIn('dependency_drift', [a['reason'] for a in alerts])

    def test_review_100_is_pending_acceptance_not_done(self):
        self.rows[0].update(status='review', progress=100)
        self.rows[1].update(status='in_progress', progress=30)
        result = monitor.inspect_tasks(self.rows, self.now)
        self.assertEqual(result['counts']['review'], 1)
        self.assertEqual(result['counts']['done'], 0)
        self.assertIn(dict(code=self.rows[1]['code'], dependencies=[self.rows[0]['code']]), result['pending_acceptance_dependencies'])
        self.assertNotIn('unfinished_dependency', [a['reason'] for a in result['alerts'] if a['code'] == self.rows[1]['code']])
        self.rows[0]['progress'] = 90
        self.assertIn('unfinished_dependency', [a['reason'] for a in monitor.inspect_tasks(self.rows, self.now)['alerts']])

    def test_hour_and_fingerprint_dedup(self):
        alerts = [dict(code='CLR2-BE-001', reason='stale_progress', minutes=61)]
        key = monitor.notification_key(alerts, self.now)
        self.assertEqual(key, monitor.notification_key([dict(alerts[0], minutes=70)], self.now + timedelta(minutes=10)))
        self.assertNotEqual(key, monitor.notification_key(alerts, self.now + timedelta(hours=1)))
        self.assertNotEqual(key, monitor.notification_key([dict(code='CLR2-BE-002', reason='blocked')], self.now))

    def test_no_backend_import_for_inspection(self):
        before = set(sys.modules)
        monitor.inspect_tasks(self.rows, self.now)
        self.assertFalse(any(name == 'app.main' or name.endswith('.main') for name in set(sys.modules) - before))

    def test_missing_runtime_database_fails_before_backend_import(self):
        with patch.dict('os.environ', {}, clear=True), patch('sys.stderr'):
            with self.assertRaises(SystemExit) as error:
                monitor.main([])
        self.assertEqual(error.exception.code, 2)

    def test_notify_only_active_super_admin_and_scoped_event_key(self):
        class Field:
            def __eq__(self, value):
                return ('equals', value)
            def is_(self, value):
                return ('is', value)
        user = SimpleNamespace(id=Field(), platform_role=Field(), is_active=Field(), activation_status=Field())
        select = Mock()
        db, create = Mock(), Mock()
        db.scalars.return_value = ['root-user']
        result = dict(alerts=[dict(code='CLR2-BE-001', reason='blocked')])
        with patch.dict(sys.modules, {'sqlalchemy': SimpleNamespace(select=select)}):
            self.assertTrue(monitor.notify(db, user, {'create': create}, result, self.now))
        self.assertEqual(select.return_value.where.call_args.args,
                         (('equals', 'super_admin'), ('is', True), ('equals', 'active')))
        self.assertEqual(create.call_args.args[1], ['root-user'])
        self.assertTrue(create.call_args.kwargs['event_key'].startswith('clr2-monitor:2026101812:'))
        db.commit.assert_called_once()

    def test_no_alert_notification_has_no_writes(self):
        db, create = Mock(), Mock()
        self.assertFalse(monitor.notify(db, None, {'create': create}, dict(alerts=[]), self.now))
        db.commit.assert_not_called()
        create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
