"""Isolated HTTP and executor state-machine tests; no real maintenance occurs."""
import importlib
import json
import unittest
import uuid
from datetime import timedelta
from unittest.mock import Mock

import test_message_business_events as fixture


def snapshot(status):
    return {'status': status, 'captured_at': '2026-11-17T00:00:00+00:00',
            'persistence': {'loading': 0, 'aof_enabled': 1, 'aof_last_write_status': 'ok'}}


class RedisMaintenanceTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown
    request = fixture.BusinessRoutesHTTPTests.request

    def setUp(self):
        fixture.BusinessRoutesHTTPTests.setUp(self)
        self.w = importlib.import_module(self.m.__package__ + '.redis_maintenance_worker')
        self.Task = self.m.redis_maintenance['Task']

    def submit(self, action='inspect', actor='platform', expected=200, **changes):
        body = {'action': action, 'reason': '维护测试原因', 'accept_gap': action == 'recover',
                'idempotency_key': str(uuid.uuid4()), **changes}
        return self.request('POST', '/api/admin/maintenance/redis/tasks', actor=actor, expected=expected, json=body)

    def fake(self, *statuses):
        runtime = Mock()
        runtime.inspect.side_effect = [snapshot(status) for status in statuses]
        runtime.backup.return_value = {'backup_completed': True, 'sha256': 'test', 'bytes': 10}
        runtime.source.return_value = {'status': 'paused', 'sealed_hash': 'test-seal', 'source_id': 'source-test'}
        return runtime

    def run_task(self, runtime):
        self.w.process_once(vars(self.m), runtime)

    def load_task(self, task):
        with self.m.SessionLocal() as db:
            return self.m.redis_maintenance['task_out'](db.get(self.Task, task['id']))

    def test_only_system_admin_has_read_and_write_permissions(self):
        for actor in ('buyer', 'provider', 'ops', 'finance', 'reviewer', 'quality'):
            self.request('GET', '/api/admin/maintenance/redis', actor=actor, expected=403)
            self.submit(actor=actor, expected=403)
        data = self.request('GET', '/api/admin/maintenance/redis', actor='platform')
        self.assertIsNone(data['snapshot'])

    def test_explicit_gap_reason_no_restart_action_and_no_external_calls_on_submit(self):
        self.submit('recover', accept_gap=False, expected=400)
        self.submit('prepare', reason=' ', expected=400)
        self.submit('restart', expected=400)
        self.submit('inspect', accept_gap=True, expected=400)
        task = self.submit('prepare')
        self.assertEqual(task['status'], 'queued')
        self.assertNotIn('evidence_path', task)

    def test_idempotency_and_only_one_active_task(self):
        key = str(uuid.uuid4())
        first = self.submit('inspect', idempotency_key=key)
        second = self.submit('inspect', idempotency_key=key)
        self.assertEqual(first['id'], second['id'])
        self.submit('prepare', idempotency_key=key, expected=409)
        self.submit('inspect', expected=409)

    def test_prepare_backups_seals_verifies_without_any_restart(self):
        task = self.submit('prepare')
        runtime = self.fake('active_source_healthy', 'safe_sealed_resume_candidate')
        self.run_task(runtime)
        data = self.load_task(task)
        self.assertEqual(data['status'], 'succeeded')
        runtime.source.assert_called_once_with('seal', 'platform', '维护测试原因')
        self.assertEqual([s['stage'] for s in data['steps']], ['inspect', 'backup', 'seal', 'verify_seal'])
        self.assertEqual(data['steps'][2]['result']['sealed_hash'], 'test-seal')
        overview = self.request('GET', '/api/admin/maintenance/redis', actor='platform')
        self.assertEqual(overview['snapshot']['status'], 'safe_sealed_resume_candidate')
        self.assertIsNone(overview['active_task'])

    def test_backup_failure_never_seals_or_opens_and_error_is_safe(self):
        task = self.submit('prepare')
        runtime = self.fake('active_source_healthy', 'active_source_healthy')
        runtime.backup.side_effect = RuntimeError('postgres://user:SECRET@server')
        self.run_task(runtime)
        data = self.load_task(task)
        self.assertEqual(data['status'], 'failed')
        self.assertEqual(data['stage'], 'backup')
        self.assertNotIn('SECRET', json.dumps(data, default=str))
        runtime.source.assert_not_called()

    def test_seal_verification_failure_does_not_fallback_to_gap(self):
        task = self.submit('prepare')
        runtime = self.fake('active_source_healthy', 'explicit_gap_required', 'explicit_gap_required')
        self.run_task(runtime)
        self.assertEqual(self.load_task(task)['status'], 'failed')
        runtime.source.assert_called_once_with('seal', 'platform', '维护测试原因')

    def test_resume_requires_sealed_evidence(self):
        task = self.submit('resume')
        runtime = self.fake('explicit_gap_required', 'explicit_gap_required')
        self.run_task(runtime)
        self.assertEqual(self.load_task(task)['status'], 'failed')
        runtime.backup.assert_not_called()
        runtime.source.assert_not_called()

    def test_recover_never_rotates_healthy_source(self):
        task = self.submit('recover')
        runtime = self.fake('active_source_healthy', 'active_source_healthy')
        self.run_task(runtime)
        self.assertEqual(self.load_task(task)['status'], 'failed')
        runtime.source.assert_not_called()

    def test_resume_and_gap_recover_wait_for_fresh_collector_health(self):
        for action, status, operation in [('resume', 'safe_sealed_resume_candidate', 'resume'),
                                          ('recover', 'explicit_gap_required', 'open')]:
            task = self.submit(action)
            runtime = self.fake(status, 'active_source_healthy')
            def resumed(*args):
                self.m.redis_maintenance['collector_heartbeat']('ready')
                return {'status': 'active', 'source_id': 'new'}
            runtime.source.side_effect = resumed
            self.run_task(runtime)
            self.assertEqual(self.load_task(task)['status'], 'succeeded')
            runtime.source.assert_called_once_with(operation, 'platform', '维护测试原因')

    def test_interrupted_task_is_not_replayed(self):
        task = self.submit('prepare')
        with self.m.SessionLocal() as db:
            db.get(self.Task, task['id']).status = 'running'
            db.commit()
        runtime = self.fake()
        self.run_task(runtime)
        self.assertEqual(self.load_task(task)['status'], 'manual_review')
        runtime.inspect.assert_not_called()
        runtime.source.assert_not_called()

    def test_expired_request_or_removed_admin_privilege_stops_before_inspection(self):
        task = self.submit('prepare')
        with self.m.SessionLocal() as db:
            db.get(self.Task, task['id']).created_at = self.m.now() - timedelta(minutes=10)
            db.commit()
        runtime = self.fake('active_source_healthy')
        self.run_task(runtime)
        self.assertEqual(self.load_task(task)['status'], 'failed')
        runtime.source.assert_not_called()

    def test_persistence_failure_prevents_backup_and_source_transition(self):
        task = self.submit('recover')
        runtime = self.fake('explicit_gap_required', 'explicit_gap_required')
        bad = snapshot('explicit_gap_required')
        bad['persistence']['aof_enabled'] = 0
        runtime.inspect.side_effect = [bad, bad]
        self.run_task(runtime)
        self.assertEqual(self.load_task(task)['status'], 'failed')
        runtime.source.assert_not_called()
        runtime.backup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
