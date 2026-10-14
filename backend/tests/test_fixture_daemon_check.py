"""Harness entry guards plus opt-in real PostgreSQL subprocess validation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from fixture_reservation_daemon_check import private_engine


class DaemonHarnessGuardTests(unittest.TestCase):
    def test_private_schema_requires_exact_generated_identifier(self):
        for schema in (None, 'public', 'qa_fixture_daemon_',
                       'qa_fixture_daemon_' + 'a' * 32 + ',public',
                       'qa_fixture_daemon_' + 'a' * 31):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                private_engine('unused', schema)

    def test_missing_private_database_url_fails_before_starting(self):
        script = Path(__file__).resolve().parents[2] / 'scripts' / 'fixture_reservation_daemon_check.py'
        env = os.environ.copy()
        env.pop('TEST_FIXTURE_RESERVATION_PG_URL', None)
        result = subprocess.run([sys.executable, str(script)], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), 'ValueError')
        self.assertEqual(result.stdout, '')


@unittest.skipUnless(os.environ.get('TEST_FIXTURE_RESERVATION_PG_URL'),
                     'explicit disposable PostgreSQL URL required')
class PostgreSQLDaemonHarnessTests(unittest.TestCase):
    def test_real_worker_guards_http_recovery_and_crash_quarantine(self):
        script = Path(__file__).resolve().parents[2] / 'scripts' / 'fixture_reservation_daemon_check.py'
        result = subprocess.run([sys.executable, str(script)], capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['result'], 'passed')
        self.assertTrue(report['schema_dropped'])
        self.assertEqual(report['scope'], 'guards-and-http-prepare-only')
        self.assertRegex(report['schema'], r'^qa_fixture_daemon_[0-9a-f]{32}$')
        for required in ('wrong-token-rejected', 'http-503-durable-retry',
                         'http-healthy-prepare-recovered', 'real-worker-killed',
                         'crash-quarantine-no-processing', 'owned-cleanup'):
            self.assertIn(required, report['checks'])
