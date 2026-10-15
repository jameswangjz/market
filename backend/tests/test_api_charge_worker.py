from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from app.api_charge_worker import process_once


class ChargeWorkerTests(unittest.TestCase):
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

    def test_deploy_starts_worker_only_after_api_schema_ready(self):
        source = (Path(__file__).resolve().parents[2] / 'scripts/deploy_trading_runtime.sh').read_text()
        self.assertLess(source.index('rollout status deployment/market-api '),
                        source.index('rollout status deployment/api-charge-ledger-worker '))
