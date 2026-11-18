from pathlib import Path
import unittest
import tempfile
import threading
from unittest.mock import MagicMock, patch

from app.api_charge_worker import process_once, supervised_cycle, error_code
from app.online_delivery_worker import run_worker


class ChargeWorkerTests(unittest.TestCase):
    def test_blocked_cycle_removes_readiness_and_keeps_retrying_without_fake_success(self):
        with tempfile.TemporaryDirectory() as directory:
            heartbeat, status = Path(directory) / 'success', Path(directory) / 'status.json'
            heartbeat.touch()
            state = {}
            with patch('app.api_charge_worker.process_once', side_effect=ValueError(
                    'Managed financial source unavailable; explicit continuity review required')) as collect, \
                    patch('app.api_charge_worker.logger') as log:
                for _ in range(3):
                    self.assertFalse(supervised_cycle({}, state, heartbeat=heartbeat, status=status, clock=lambda: 10))
                self.assertFalse(heartbeat.exists())
                self.assertEqual(collect.call_count, 3)
                self.assertEqual(log.error.call_count, 1)
                import json
                self.assertEqual(json.loads(status.read_text())['error'], 'source_continuity_review_required')
            with patch('app.api_charge_worker.process_once', return_value=False):
                self.assertFalse(supervised_cycle({}, state, heartbeat=heartbeat, status=status))
            self.assertTrue(heartbeat.exists())
            self.assertIsNone(state['error'])
            self.assertEqual(json.loads(status.read_text())['status'], 'ready')

    def test_error_diagnostics_do_not_log_external_exception_secrets(self):
        self.assertEqual(error_code(RuntimeError('redis://secret:password@host')), 'collection_dependency_error')
        self.assertEqual(error_code(ValueError('untrusted event payload')), 'charge_evidence_invalid')

    def test_continuity_pause_keeps_process_heartbeat_but_not_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            ready, alive, status = (Path(directory) / name for name in ('ready', 'alive', 'status.json'))
            stopped = threading.Event()
            def cycle():
                result = supervised_cycle({}, {}, heartbeat=ready, status=status)
                stopped.set()
                return result
            with patch('app.api_charge_worker.process_once', side_effect=ValueError(
                    'Managed financial source unavailable; explicit continuity review required')):
                run_worker(cycle, stopped, heartbeat=alive)
            self.assertTrue(alive.exists())
            self.assertFalse(ready.exists())

    def test_cursor_and_facts_commit_once_after_ingest(self):
        session = MagicMock()
        session.__enter__.return_value = session
        ingest = MagicMock(return_value={'inserted': 2})
        ns = dict(SessionLocal=MagicMock(return_value=session), apisix_policy_redis=MagicMock(),
                  api_charge_ledger={'ingest': ingest})
        self.assertTrue(process_once(ns))
        session.commit.assert_called_once_with()
        ns['apisix_policy_redis'].return_value.close.assert_called_once_with()
        self.assertEqual(ingest.call_args.kwargs['limit'], 500)

    def test_failed_event_does_not_commit_or_ack(self):
        session = MagicMock()
        session.__enter__.return_value = session
        ns = dict(SessionLocal=MagicMock(return_value=session), apisix_policy_redis=MagicMock(),
                  api_charge_ledger={'ingest': MagicMock(side_effect=ValueError('bad event'))})
        with self.assertRaises(ValueError):
            process_once(ns)
        session.commit.assert_not_called()
        ns['apisix_policy_redis'].return_value.close.assert_called_once_with()
        ns['apisix_policy_redis'].return_value.xdel.assert_not_called()

    def test_manifest_probe_expressions_compile_and_have_stable_image(self):
        import yaml
        root = Path(__file__).resolve().parents[2]
        deployment = yaml.safe_load((root / 'k8s/api-charge-ledger.yaml').read_text())
        container = deployment['spec']['template']['spec']['containers'][0]
        self.assertEqual(container['image'], 'market-api:REPLACE_WITH_DEPLOYED_TAG')
        self.assertFalse(deployment['spec']['template']['spec']['automountServiceAccountToken'])
        for key in ('startupProbe', 'readinessProbe', 'livenessProbe'):
            command = container[key]['exec']['command'][-1]
            compile(command, '<heartbeat>', 'exec')
            with patch('pathlib.Path.stat') as stat, patch('time.time', return_value=100):
                stat.return_value.st_mtime = 90
                exec(command, {})
        self.assertIn('api-charge-ledger-alive', container['startupProbe']['exec']['command'][-1])
        self.assertIn('api-charge-ledger-alive', container['livenessProbe']['exec']['command'][-1])
        self.assertIn('api-charge-ledger-heartbeat', container['readinessProbe']['exec']['command'][-1])

    def test_deploy_starts_worker_only_after_api_schema_ready(self):
        source = (Path(__file__).resolve().parents[2] / 'scripts/deploy_trading_runtime.sh').read_text()
        self.assertLess(source.index('rollout status deployment/market-api '),
                        source.index('rollout status deployment/api-charge-ledger-worker '))
