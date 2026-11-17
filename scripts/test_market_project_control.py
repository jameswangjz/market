"""Offline project stop/start ordering; no live cluster or filesystem changes."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('project', Path(__file__).with_name('market_project_control.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class FakeController(m.Controller):
    def __init__(self, statuses=(), **args):
        names = m.CORE | m.EDGES | {'other-worker'}
        state = {'directory': '/unused', 'phase': 'snapshot', 'workloads': [
            {'kind': 'Deployment', 'metadata': {'name': name, 'uid': name}, 'spec': {'replicas': 3 if name == 'market-apisix' else 1}}
            for name in names], 'cronjobs': [{'metadata': {'name': 'timer'}, 'spec': {'suspend': False}}],
                 'maintenance': [], 'sealed_source_verified': False}
        super().__init__(SimpleNamespace(actor='admin@market.local', reason='approved', accept_gap=False,
                                        keep_cronjobs_suspended=False, **args), state)
        self.calls = []
        self.statuses = iter(statuses)

    def save(self, phase=None):
        if phase: self.state['phase'] = phase
        self.calls.append(('phase', phase))

    def validate(self): self.calls.append(('validate',))
    def command(self, *args, **kwargs): self.calls.append(('command', args)); return b''
    def get(self, _): return {'items': []}
    def suspend_jobs(self): self.calls.append(('suspend_jobs',))
    def backup(self): self.calls.append(('backup',))
    def scale(self, names, restore=False, wait=False): self.calls.append(('scale', set(names), restore, wait))
    def wait_stopped(self, names): self.calls.append(('wait', set(names)))
    def maintenance(self, action):
        self.calls.append(('maintenance', action))
        return {'status': next(self.statuses)}


class ProjectControlTests(unittest.TestCase):
    def test_stop_seals_before_stopping_redis_and_postgres(self):
        control = FakeController(['active_source_healthy', 'safe_sealed_resume_candidate'])
        with patch.object(m.time, 'sleep'): control.stop()
        self.assertEqual(control.state['phase'], 'stopped')
        prepare = control.calls.index(('maintenance', 'prepare'))
        for index, call in enumerate(control.calls):
            if call[0] == 'scale' and 'market-redis' in call[1]: self.assertGreater(index, prepare)
        scales = [call for call in control.calls if call[0] == 'scale']
        self.assertEqual(scales[-1][1], {'market-postgres'})
        self.assertTrue(control.state['sealed_source_verified'])

    def test_unproven_source_does_not_stop_storage(self):
        control = FakeController(['explicit_gap_required'])
        with self.assertRaises(RuntimeError): control.stop()
        self.assertFalse(any(call[0] == 'scale' and 'market-redis' in call[1] for call in control.calls))

    def test_verified_seal_can_finish_an_interrupted_shutdown_without_api(self):
        control = FakeController()
        control.state['sealed_source_verified'] = True
        with patch.object(m.time, 'sleep'): control.stop()
        self.assertFalse(any(call[0] == 'maintenance' for call in control.calls))
        self.assertEqual(control.state['phase'], 'stopped')

    def test_start_restores_storage_before_source_and_opens_ingress_last(self):
        control = FakeController(['safe_sealed_resume_candidate', 'active_source_healthy'])
        control.start()
        resume = control.calls.index(('maintenance', 'resume'))
        edges = next(i for i, call in enumerate(control.calls) if call[0] == 'scale' and call[1] == m.EDGES)
        self.assertGreater(edges, resume)
        self.assertEqual(control.state['phase'], 'running')
        self.assertTrue(all(call[2] for call in control.calls if call[0] == 'scale'))

    def test_start_does_not_open_ingress_without_continuity(self):
        control = FakeController(['explicit_gap_required'])
        with self.assertRaises(RuntimeError): control.start()
        self.assertFalse(any(call[0] == 'scale' and call[1] == m.EDGES for call in control.calls))
        self.assertFalse(any(call[0] == 'maintenance' and call[1] == 'recover' for call in control.calls))

    def test_reviewed_gap_and_keep_cronjobs_suspended(self):
        control = FakeController(['explicit_gap_required', 'active_source_healthy'])
        control.args.accept_gap = True
        control.args.keep_cronjobs_suspended = True
        control.start()
        self.assertIn(('maintenance', 'recover'), control.calls)
        self.assertEqual(control.state['phase'], 'running_cron_paused')
        self.assertFalse(any(call[0] == 'command' and 'cronjob' in call[1] for call in control.calls))

    def test_already_stopped_does_not_repeat_maintenance(self):
        control = FakeController()
        control.state['phase'] = 'stopped'
        control.stop()
        self.assertFalse(any(call[0] == 'maintenance' for call in control.calls))


if __name__ == '__main__': unittest.main()
