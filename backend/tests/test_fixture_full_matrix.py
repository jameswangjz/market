"""Opt-in full domain matrix acceptance, with isolated HTTP vendor processes."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

from fixture_fault_full_matrix import CASES, child, matrix_engine, wait_for_retry


class MatrixEntryGuardTests(unittest.TestCase):
    def test_schema_rejects_public_or_search_path_expansion(self):
        for schema in ('public', None, 'qa_fixture_matrix_' + 'a' * 32 + ',public'):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                matrix_engine('unused', schema)

    def test_retry_wait_reaches_deadline_without_early_return(self):
        instant = datetime(2026, 10, 13, tzinfo=timezone.utc)
        values = {'now': instant, 'waits': []}
        def sleep(seconds):
            values['waits'].append(seconds)
            values['now'] += timedelta(seconds=seconds)
        due = instant + timedelta(seconds=10)
        wait_for_retry(due, clock=lambda: values['now'], sleeper=sleep)
        self.assertEqual(values['now'], due)
        self.assertEqual(sum(values['waits']), 10)
        self.assertTrue(all(seconds <= 1 for seconds in values['waits']))

    def test_out_of_bound_retry_deadline_fails_before_sleep(self):
        instant = datetime(2026, 10, 13, tzinfo=timezone.utc)
        with self.assertRaises(AssertionError):
            wait_for_retry(instant + timedelta(seconds=61), clock=lambda: instant,
                           sleeper=lambda _: self.fail('must not sleep'))

    def test_postgresql_child_rejects_accelerated_clock_before_database_access(self):
        with self.assertRaisesRegex(AssertionError, 'logical-clock-forbidden-for-PostgreSQL'):
            child(SimpleNamespace(sqlite_path=None, at='2026-10-13T00:00:00+00:00'))


@unittest.skipUnless(os.environ.get('TEST_FIXTURE_RESERVATION_PG_URL'),
                     'explicit pod-local private-schema PostgreSQL URL required')
class PostgreSQLFullMatrixTests(unittest.TestCase):
    def run_resource(self, resource):
        script = Path(__file__).resolve().parents[2] / 'scripts' / 'fixture_fault_full_matrix.py'
        result = subprocess.run([sys.executable, str(script), '--resource', resource],
                                capture_output=True, text=True, timeout=1200)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['result'], 'passed')
        self.assertEqual(report['database'], 'postgresql-private-schema')
        self.assertEqual(report['scheduler'],
                         'real-UTC-persisted-deadlines-minimum-ten-second-audit-spacing')
        self.assertTrue(report['schema_dropped'])
        self.assertTrue(report['protected_private_sentinels_unchanged'])
        self.assertFalse(report['original_tables_used'])
        self.assertRegex(report['schema'], r'^qa_fixture_matrix_[0-9a-f]{32}$')
        self.assertEqual({item['case'] for item in report['cases']}, set(CASES))
        self.assertEqual({item['resource'] for item in report['cases']}, {resource})
        for item in report['cases']:
            self.assertIn('terminal-proof-ledger-revision-funds-invariants', item['checks'])
            self.assertLessEqual(item['cycles'], 14)

    def test_api_all_resource_and_funds_boundaries(self):
        self.run_resource('api')

    def test_model_api_all_resource_and_funds_boundaries(self):
        self.run_resource('model_api')

    def test_saas_all_resource_and_funds_boundaries(self):
        self.run_resource('tenant_access')
