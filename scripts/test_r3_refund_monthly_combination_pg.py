"""Focused protocol/transport guards plus optional isolated real PG execution.

R3_MONTHLY_DATABASE_URL enables the subprocess integration case. No remote calls.
"""
from datetime import datetime, timezone
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import r3_refund_monthly_combination_pg as combination


class CombinationGuards(unittest.TestCase):
    def test_output_archives_final_json_and_refuses_overwrite_before_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'evidence.json'
            result = dict(status='passed', scope='reviewed simulation')
            argv = ['combination', '--pod', 'market-api-review-only', '--output', str(output)]
            with patch.object(sys, 'argv', argv), patch.object(combination, 'pod_run', return_value=result) as run, \
                    redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(combination.main(), 0)
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertEqual(json.loads(stdout.getvalue()), result)
            run.assert_called_once_with('market-api-review-only')
            before = output.read_bytes()
            with patch.object(sys, 'argv', argv), patch.object(combination, 'pod_run') as run, \
                    redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(combination.main(), 1)
            self.assertEqual(json.loads(stdout.getvalue())['error_type'], 'FileExistsError')
            self.assertEqual(output.read_bytes(), before)
            run.assert_not_called()

    def test_output_failure_contains_no_exception_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'failure.json'
            with patch.object(sys, 'argv', ['combination', '--pod', 'market-api-review-only', '--output', str(output)]), \
                    patch.object(combination, 'pod_run', side_effect=RuntimeError('private-credential-value')), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(combination.main(), 1)
            self.assertEqual(json.loads(output.read_text()), dict(status='failed', error_type='RuntimeError'))
            self.assertNotIn('private-credential-value', output.read_text())

    def supplier(self):
        m = SimpleNamespace(now=lambda: combination.UPGRADE)
        return combination.Supplier(m), SimpleNamespace(id='comb-config')

    def payload(self, **extra):
        return dict(tenant_id='comb-tenant', product_id='comb-product', enterprise_id='owned-buyer',
                    subscription_id='comb-sub', **extra)

    def test_year_boundary_uses_explicit_shanghai_natural_months(self):
        self.assertEqual(combination.period('2026-12'),
            ('2026-12-01T00:00:00+08:00', '2027-01-01T00:00:00+08:00'))
        self.assertEqual(combination.period('2027-01')[0], combination.period('2026-12')[1])
        self.assertLess(combination.ANCHOR, combination.UPGRADE)
        self.assertLess(combination.UPGRADE, combination.DOWNGRADE)
        self.assertLess(combination.DOWNGRADE, combination.REFUND_AT)

    def test_lifecycle_clock_uses_last_instant_of_shanghai_month(self):
        for month, expected, transaction in (
                ('2026-12', '2026-12-31T23:59:59.999999+08:00', combination.ANCHOR),
                ('2027-01', '2027-01-31T23:59:59.999999+08:00', combination.UPGRADE),
                ('2027-02', '2027-02-28T23:59:59.999999+08:00', combination.DOWNGRADE),
                ('2027-03', '2027-03-31T23:59:59.999999+08:00', combination.REFUND_AT)):
            with self.subTest(month=month):
                at = combination.month_end(month)
                self.assertEqual(at.isoformat(), expected)
                self.assertGreaterEqual(at, transaction)
                self.assertLess(at, datetime.fromisoformat(combination.period(month)[1]))

    def test_change_and_status_preserve_first_effective_time_and_identity(self):
        supplier, config = self.supplier()
        payload = self.payload(change_id='change', direction='target', to_version='v2')
        changed = supplier(config, 'CHANGE', payload)
        supplier.m.now = lambda: combination.DOWNGRADE
        replay = supplier(config, 'CHANGE', payload)
        status = supplier(config, 'STATUS', payload)
        self.assertEqual(changed['effective_at'], combination.UPGRADE.isoformat())
        for result in (replay, status):
            for key in ('change_id', 'direction', 'effective_at', 'subscription_id', 'tenant_id', 'version'):
                self.assertEqual(result[key], changed[key])
        with self.assertRaises(combination.harness.base.AcceptanceFailure):
            supplier(config, 'CHANGE', dict(payload, to_version='v1'))

    def test_supplier_rejects_foreign_identity_and_unrecorded_status(self):
        supplier, config = self.supplier()
        for field in ('tenant_id', 'product_id', 'enterprise_id', 'subscription_id'):
            with self.subTest(field=field), self.assertRaises(combination.harness.base.AcceptanceFailure):
                supplier(config, 'CHANGE', dict(self.payload(change_id='change', direction='target',
                    to_version='v2'), **{field: 'foreign'}))
        with self.assertRaises(combination.harness.base.AcceptanceFailure):
            supplier(config, 'STATUS', self.payload(change_id='missing', direction='target'))

    def test_shorten_is_idempotent_and_requires_expected_expiry(self):
        supplier, config = self.supplier()
        new_end = datetime(2027, 6, 1, tzinfo=timezone.utc).isoformat()
        body = self.payload(refund_id='refund', action='refund_shorten', protocol='refund_shorten_v1',
            expected_expires_at=combination.END.isoformat(), expires_at=new_end,
            idempotency_key='refund-shorten:refund')
        first = supplier(config, 'RENEW', body)
        self.assertEqual(supplier(config, 'RENEW', body), first)
        self.assertEqual(supplier(config, 'STATUS', body)['refund_id'], 'refund')
        with self.assertRaises(combination.harness.base.AcceptanceFailure):
            supplier(config, 'RENEW', dict(body, expires_at=combination.END.isoformat()))
        with self.assertRaises(combination.harness.base.AcceptanceFailure):
            supplier(config, 'RENEW', dict(body, refund_id='other', idempotency_key='refund-shorten:other'))

    def test_local_url_cannot_silently_select_remote_or_public_search_path(self):
        for raw in ('sqlite://', 'postgresql://user@192.168.10.10/db',
                    'postgresql://user@127.0.0.1/db?options=-csearch_path%3Dpublic'):
            with self.subTest(raw=raw), self.assertRaises(combination.harness.base.AcceptanceFailure):
                combination.local_url(raw)
        self.assertEqual(combination.local_url('postgresql://user@127.0.0.1/db').host, '127.0.0.1')

    def test_remote_entry_reuses_exact_original_loader_and_schema_lifecycle(self):
        manifest = {name: 'a' * 64 for name in combination.SOURCE_FILES}
        manifest['another_new_module.py'] = 'b' * 64
        prior_seed = combination.harness.cross.seed

        def invoke(encoded, actual):
            self.assertEqual(encoded, 'encoded')
            self.assertIs(actual, manifest)
            self.assertIs(combination.harness.cross.seed, combination.seed)
            self.assertEqual(combination.harness.cross.CASE_NAMES, combination.CASE_NAMES)
            self.assertEqual(combination.harness.cross.SOURCE_FILES, combination.SOURCE_FILES)
            return dict(status='passed', cleanup='owned schema removed')

        with patch.object(combination.harness.cross, 'remote_run', side_effect=invoke):
            result = combination.remote_run('encoded', manifest)
        self.assertIs(combination.harness.cross.seed, prior_seed)
        self.assertEqual(result['source_sha256'], manifest)
        self.assertFalse(result['seeded_version_changes'])
        self.assertIn('simulator', result['external_adapters']['supplier'])

    def test_incomplete_inventory_fails_before_loader_or_database(self):
        with patch.object(combination.harness.cross, 'remote_run') as invoke:
            with self.assertRaises(combination.harness.base.AcceptanceFailure):
                combination.remote_run('encoded', {})
            invoke.assert_not_called()

    def test_package_contains_every_required_module_and_rejects_tampering(self):
        root = Path(__file__).resolve().parents[1]
        encoded, manifest = combination.harness.transport.pack_workingtree(root)
        self.assertTrue(set(combination.SOURCE_FILES) <= set(manifest))
        manifest['main.py'] = '0' * 64
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AssertionError):
                combination.harness.transport.unpack_workingtree(encoded, manifest, Path(directory) / 'app')

    def test_invalid_pod_fails_without_transport(self):
        with patch.object(combination.subprocess, 'run') as execute:
            for pod in ('market-api-x;id', 'other-pod', ''):
                with self.subTest(pod=pod), self.assertRaises(combination.harness.base.AcceptanceFailure):
                    combination.pod_run(pod)
            execute.assert_not_called()

    def test_pod_transport_is_bounded_and_payload_compiles_without_execution(self):
        observed = {}

        def execute(args, **kwargs):
            observed.update(args=args, **kwargs)
            compile(kwargs['input'], '<reviewed-pod-payload>', 'exec')
            return SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='passed')))

        with patch.dict(os.environ, SSHPASS='test-private-value'), \
                patch.object(combination.subprocess, 'run', side_effect=execute):
            self.assertEqual(combination.pod_run('market-api-review-only')['status'], 'passed')
        self.assertEqual(observed['timeout'], 180)
        self.assertNotIn('test-private-value', observed['input'])
        self.assertNotIn('test-private-value', str(observed['args']))
        self.assertIn('kubectl --request-timeout=5s', observed['args'][-1])
        self.assertIn('remote_run(', observed['input'])


@unittest.skipUnless(os.getenv('R3_MONTHLY_DATABASE_URL'), 'Requires explicit local disposable PostgreSQL URL')
class PostgreSQLCombination(unittest.TestCase):
    def test_incomplete_supplier_receipt_fails_and_disposes_schema(self):
        program = '''
import os
from unittest.mock import patch
from sqlalchemy import create_engine, text
import r3_refund_monthly_combination_pg as c
engine = create_engine(os.environ['R3_MONTHLY_DATABASE_URL'])
def schemas():
    with engine.connect() as db:
        return set(db.scalars(text("SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'qa_r3_be_%'")))
before = schemas()
original = c.Supplier.__call__
def incomplete(self, config, operation, payload):
    response = original(self, config, operation, payload)
    if operation == 'CHANGE':
        response.pop('effective_at', None)
    return response
try:
    with patch.object(c.Supplier, '__call__', incomplete):
        c.run(os.environ['R3_MONTHLY_DATABASE_URL'])
except c.harness.base.AcceptanceFailure as exc:
    assert str(exc).startswith('Saga orchestration failed:'), str(exc)
else:
    raise AssertionError('Incomplete receipt accepted as full Saga evidence')
assert schemas() == before, 'Failure leaked owned schema'
engine.dispose()
print('incomplete receipt rejected; owned schema disposed')
'''
        completed = subprocess.run([sys.executable, '-B', '-c', program], capture_output=True,
            text=True, timeout=60, env=dict(os.environ, PYTHONPATH=str(Path(combination.__file__).resolve().parent)))
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        self.assertIn('incomplete receipt rejected; owned schema disposed', completed.stdout)

    def test_real_financial_cross_month_combination(self):
        script = Path(combination.__file__).resolve()
        completed = subprocess.run([sys.executable, '-B', str(script)], capture_output=True, text=True, timeout=100)
        result = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, result)
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['cleanup'], 'owned schema removed')
        self.assertFalse(result['protected_order_content_read'])
        self.assertFalse(result['seeded_version_changes'])
        self.assertEqual((result['completed_sagas'], result['saga_adjustments'], result['ordinary_refunds']), (2, 4, 2))
        self.assertEqual(result['ordinary_payment_legs'], 3)
        self.assertTrue(result['prior_paid_batches_unchanged'])
        self.assertTrue(result['five_party_reports_and_csv'])
        self.assertTrue(result['clock_synthetic'])
        self.assertTrue(result['batch_lifecycle_after_transactions'])
        self.assertEqual(result['batch_lifecycle_clock'],
            {month: combination.month_end(month).isoformat() for month in combination.MONTHS})
        self.assertEqual(result['months']['2027-03'], dict(net='-80', cost='-16', profit='-64'))


if __name__ == '__main__':
    unittest.main()
