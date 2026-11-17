"""Offline flow guards: no Kubernetes, database, Redis or restart is contacted."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('maintenance', Path(__file__).with_name('market_redis_maintenance.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
HEALTHY, SEALED, GAP = 'active_source_healthy', 'safe_sealed_resume_candidate', 'explicit_gap_required'


class MaintenanceTests(unittest.TestCase):
    def runtime(self, *statuses):
        runtime = Mock()
        runtime.diagnose.side_effect = [{'status': value, 'persistence': {
            'loading': 0, 'aof_enabled': 1, 'aof_last_write_status': 'ok'}} for value in statuses]
        return runtime

    def test_status_never_changes_state(self):
        runtime = self.runtime(GAP)
        self.assertEqual(m.perform('status', runtime)['source_status'], GAP)
        runtime.backup.assert_not_called()
        runtime.source.assert_not_called()
        runtime.restart.assert_not_called()

    def test_planned_restart_orders_backup_seal_restart_verify_resume(self):
        runtime = self.runtime(HEALTHY, SEALED, HEALTHY)
        self.assertTrue(m.perform('planned-restart', runtime)['redis_restarted'])
        self.assertEqual([call[0] for call in runtime.mock_calls], [
            'diagnose', 'backup', 'source', 'restart', 'diagnose', 'source', 'diagnose', 'wait_worker'])
        self.assertEqual([call.args[0] for call in runtime.source.call_args_list], ['seal', 'resume'])

    def test_seal_failure_prevents_restart_and_gap(self):
        runtime = self.runtime(HEALTHY)
        runtime.source.side_effect = RuntimeError('seal failed')
        with self.assertRaises(RuntimeError): m.perform('planned-restart', runtime)
        runtime.restart.assert_not_called()
        self.assertEqual(runtime.source.call_count, 1)

    def test_restart_failure_does_not_resume_or_open(self):
        runtime = self.runtime(HEALTHY)
        runtime.restart.side_effect = RuntimeError('restart timeout')
        with self.assertRaises(RuntimeError): m.perform('planned-restart', runtime)
        self.assertEqual([c.args[0] for c in runtime.source.call_args_list], ['seal'])

    def test_unverified_seal_after_restart_never_auto_opens_gap(self):
        runtime = self.runtime(HEALTHY, GAP)
        with self.assertRaisesRegex(RuntimeError, 'seal verification failed'):
            m.perform('planned-restart', runtime)
        self.assertEqual([c.args[0] for c in runtime.source.call_args_list], ['seal'])

    def test_repeated_recover_is_noop_when_healthy(self):
        runtime = self.runtime(HEALTHY)
        self.assertEqual(m.perform('recover', runtime)['result'], 'already_healthy_no_change')
        runtime.backup.assert_not_called()
        runtime.source.assert_not_called()

    def test_recover_preserves_gap_without_restarting_redis(self):
        runtime = self.runtime(GAP, HEALTHY)
        self.assertTrue(m.perform('recover', runtime)['gap_opened'])
        runtime.source.assert_called_once_with('open')
        runtime.restart.assert_not_called()
        runtime.backup.assert_called_once()

    def test_resume_uses_only_verified_seal_without_restart(self):
        runtime = self.runtime(SEALED, HEALTHY)
        m.perform('resume', runtime)
        runtime.source.assert_called_once_with('resume')
        runtime.restart.assert_not_called()

    def test_recover_refuses_sealed_source_and_unknown_status(self):
        for status in (SEALED, 'unknown'):
            runtime = self.runtime(status)
            with self.assertRaises(RuntimeError): m.perform('recover', runtime)
            runtime.source.assert_not_called()

    def test_planned_restart_and_resume_refuse_wrong_starting_state(self):
        for action, status in [('planned-restart', GAP), ('resume', HEALTHY), ('resume', GAP)]:
            runtime = self.runtime(status)
            with self.assertRaises(RuntimeError): m.perform(action, runtime)
            runtime.backup.assert_not_called()
            runtime.restart.assert_not_called()

    def test_backup_failure_blocks_all_mutations(self):
        runtime = self.runtime(GAP)
        runtime.backup.side_effect = RuntimeError('backup failed')
        with self.assertRaises(RuntimeError): m.perform('recover', runtime)
        runtime.source.assert_not_called()

    def test_unhealthy_persistence_blocks_recovery_before_backup_or_open(self):
        runtime = self.runtime(GAP)
        runtime.diagnose.side_effect = [{'status': GAP, 'persistence': {'aof_enabled': 0}}]
        with self.assertRaisesRegex(RuntimeError, 'persistence is not healthy'):
            m.perform('recover', runtime)
        runtime.source.assert_not_called()
        runtime.backup.assert_not_called()

    def test_unhealthy_persistence_after_restart_never_resumes(self):
        runtime = self.runtime(HEALTHY)
        runtime.diagnose.side_effect = [
            {'status': HEALTHY, 'persistence': {'loading': 0, 'aof_enabled': 1, 'aof_last_write_status': 'ok'}},
            {'status': SEALED, 'persistence': {'loading': 0, 'aof_enabled': 1, 'aof_last_write_status': 'err'}}]
        with self.assertRaisesRegex(RuntimeError, 'persistence is not healthy'):
            m.perform('planned-restart', runtime)
        self.assertEqual([call.args[0] for call in runtime.source.call_args_list], ['seal'])

    def test_mutating_cli_requires_reason_and_explicit_consent(self):
        for argv in [['recover'], ['planned-restart', '--reason', 'test'],
                     ['recover', '--reason', 'test'], ['resume'], ['status', '--accept-gap']]:
            with self.assertRaises(SystemExit): m.arguments(argv)
        self.assertEqual(m.arguments(['status']).action, 'status')
        self.assertEqual(m.arguments(['recover', '--reason', 'approved', '--accept-gap']).action, 'recover')


if __name__ == '__main__':
    unittest.main()
