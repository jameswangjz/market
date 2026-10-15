"""Worker transaction/budget tests without importing app.main or network I/O."""
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import Mock

from app.sla_probe_worker import run


class WorkerTests(unittest.TestCase):
    def test_cronjob_uses_telemetry_secret_and_exact_private_gateway_allowlist(self):
        manifest = (Path(__file__).resolve().parents[2] / 'k8s/sla-online-monitor.yaml').read_text()
        self.assertIn('secretKeyRef: {name: market-sla-telemetry, key: SLA_PROBE_SECRET}', manifest)
        self.assertRegex(manifest, r'UPSTREAM_ALLOWED_HOSTS[^\n]*market-apisix')
        self.assertIn('configMapKeyRef: {name: market-config, key: SLA_PROBE_SOURCE_CIDRS}', manifest)
        self.assertIn('command: [python, -m, app.sla_online_worker]', manifest)
        worker = (Path(__file__).resolve().parents[1] / 'app/sla_online_worker.py').read_text()
        self.assertIn('from .sla_probe_worker import run as collect', worker)
        self.assertIn('concurrencyPolicy: Forbid', manifest)
        self.assertIn('activeDeadlineSeconds: 240', manifest)

    def harness(self, error=False):
        transactions = []
        @contextmanager
        def session():
            db = SimpleNamespace(commit=Mock(), rollback=Mock())
            transactions.append(db)
            yield db
        def page(db, *, after_id, limit):
            return [item for item in ['a', 'b'] if item > after_id][:limit]
        def collect(db, identity, *, instant):
            if error and identity == 'a':
                raise RuntimeError('private transport detail must not be logged')
            return {'created': 1, 'source_missing': 1}
        ns = dict(SessionLocal=session, now=lambda: datetime(2026, 10, 15, tzinfo=timezone.utc),
            sla_probe_runtime={'page': Mock(side_effect=page), 'collect_one': Mock(side_effect=collect)})
        return ns, transactions

    def test_keyset_pages_and_per_contract_commit(self):
        ns, transactions = self.harness()
        result = run(ns, page_size=1, monotonic=lambda: 0)
        self.assertEqual(result['processed'], 2)
        self.assertEqual(result['source_missing'], 2)
        self.assertTrue(result['complete'])
        self.assertEqual(sum(db.commit.call_count for db in transactions), 2)
        self.assertEqual([call.kwargs['after_id'] for call in ns['sla_probe_runtime']['page'].call_args_list], ['', 'a', 'b'])

    def test_failure_rolls_back_and_continues_without_logging_private_message(self):
        ns, transactions = self.harness(error=True)
        with self.assertLogs(level='ERROR') as logs:
            result = run(ns, monotonic=lambda: 0)
        self.assertEqual(result['failures'], 1)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(sum(db.rollback.call_count for db in transactions), 1)
        self.assertNotIn('private transport detail', str(logs.output))

    def test_budget_stops_before_next_probe_and_input_bounds(self):
        ns, _ = self.harness()
        times = iter([0, 0, 1, 5])
        result = run(ns, max_seconds=2, monotonic=lambda: next(times))
        self.assertEqual(result['processed'], 1)
        self.assertFalse(result['complete'])
        for budget in [0, -1, True, float('nan'), float('inf')]:
            with self.assertRaises(ValueError):
                run(ns, max_seconds=budget)
        for size in [0, 101, True]:
            with self.assertRaises(ValueError):
                run(ns, page_size=size)


if __name__ == '__main__':
    unittest.main()
