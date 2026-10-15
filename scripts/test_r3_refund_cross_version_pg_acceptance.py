"""Offline guards only: no application import, PG, SSH or startup execution."""
import ast
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

import r3_refund_cross_version_pg_acceptance as probe


class CrossVersionGuards(unittest.TestCase):
    def test_owned_schema_and_exclusive_search_path(self):
        for schema in ('public', 'qa_r3_be_', 'qa_r3_be_0123456789abcdef,public',
                       'qa_r3_be_0123456789abcdef;DROP SCHEMA public'):
            self.assertFalse(probe.base.owned_schema(schema))
        self.assertTrue(probe.base.owned_schema('qa_r3_be_0123456789abcdef'))
        class DB:
            def __init__(self, values):
                self.values = iter(values)
            def scalar(self, statement):
                return next(self.values)
        probe.owned(DB(['qa_r3_be_0123456789abcdef'] * 2))
        with self.assertRaises(AssertionError):
            probe.owned(DB(['qa_r3_be_0123456789abcdef', 'public']))

    def test_inherited_public_access_is_only_protected_count(self):
        source = Path(probe.base.__file__).read_text()
        tree = ast.parse(source)
        public_sql = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                      and isinstance(node.value, str) and 'public.orders' in node.value]
        self.assertEqual(['SELECT COUNT(*) FROM public.orders WHERE order_no=:number'], public_sql)
        self.assertEqual('ORD-261012005144E1B3', probe.base.PROTECTED)
        self.assertIn("c.exec_driver_sql('SET TRANSACTION READ ONLY')", source)
        own_source = Path(probe.__file__).read_text()
        self.assertNotIn('public.orders', own_source)
        self.assertNotIn('DATABASE_URL', [node.attr for node in ast.walk(ast.parse(own_source))
                                        if isinstance(node, ast.Attribute)])

    def test_no_startup_or_stubbed_business_evidence(self):
        source = Path(probe.__file__).read_text()
        tree = ast.parse(source)
        forbidden = {'startup', 'ensure_schema', 'seed_data', 'create_all', 'run_startup'}
        calls = [node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                 for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, (ast.Attribute, ast.Name))]
        self.assertFalse(forbidden.intersection(calls))
        self.assertNotIn('prepare_refund_plan=', source)
        self.assertNotIn("patch.dict(m.refund_segment_store", source)
        self.assertNotIn("patch.dict(m.subscription_terms", source)
        self.assertIn("store['record_receipt']", source)
        self.assertIn("store['materialize']", source)
        self.assertIn("store['latest']", source)
        self.assertIn("terms['apply_version_change']", source)
        self.assertIn("['window_summary']", source)
        self.assertIn('MockRedis', source)

    def test_only_two_new_files_needed_transport_integrity(self):
        files = {name: '# owned source\n' for name in probe.SOURCE_FILES}
        raw = json.dumps(files).encode()
        encoded = base64.b64encode(zlib.compress(raw)).decode()
        manifest = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}
        with tempfile.TemporaryDirectory() as directory:
            probe.transport.unpack_workingtree(encoded, manifest, Path(directory) / 'good')
            changed = dict(manifest, **{'main.py': '0' * 64})
            with self.assertRaises(AssertionError):
                probe.transport.unpack_workingtree(encoded, changed, Path(directory) / 'bad')

    def test_errors_are_anonymous_and_transport_bounded(self):
        for exc in (ValueError('PRIVATE_DATABASE_URL_PASSWORD'),
                    probe.base.AcceptanceFailure('PRIVATE_SSHPASS_TOKEN')):
            result = probe.safe_error(exc)
            self.assertEqual({'status', 'error_type', 'line'}, set(result))
            self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertGreaterEqual(probe.RUN_SECONDS, 120)
        self.assertLessEqual(probe.TRANSPORT_SECONDS, 180)
        self.assertLess(probe.RUN_SECONDS, probe.TRANSPORT_SECONDS)
        source = Path(probe.__file__).read_text()
        self.assertNotIn('process.stderr', source)
        self.assertNotIn('print(payload', source)
        self.assertNotIn('print(headers', source)
        self.assertIn('logging.disable(logging.CRITICAL)', source)

    def test_fixture_ids_and_graph_case_inventory(self):
        names = (*probe.GRAPH_NAMES, 'receipt-rollback', 'numeric-watermark')
        ids = [probe.identity(name + suffix) for name in names for suffix in
               ('-product', '-combo', '-change', '-order-0', '-order-1', '-funding', '-upgrade-payment')]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(len(value) <= 36 for value in ids))
        self.assertTrue(all(len('OWNED-XV-' + probe.identity(name)) <= 40 for name in names))
        self.assertEqual(11, len(probe.CASE_NAMES))

    def test_remote_injection_and_result_cannot_fakepass_missing_cases(self):
        files = {name: '# offline\n' for name in probe.SOURCE_FILES}
        encoded = base64.b64encode(zlib.compress(json.dumps(files).encode())).decode()
        manifest = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}
        def result(cases, count=1):
            self.assertIs(probe.base.seed, probe.seed)
            self.assertIs(probe.base.exercise, probe.exercise)
            return dict(cases=cases, cleanup='owned schema removed',
                        protected_order_count_before=count, protected_order_count_after=count)
        with patch.object(probe.base, 'run', side_effect=lambda: result({})):
            self.assertEqual('failed', probe.remote_run(encoded, manifest)['status'])
        cases = {name: {'status': 'passed'} for name in probe.CASE_NAMES}
        with patch.object(probe.base, 'run', side_effect=lambda: result(cases, 0)):
            self.assertEqual('failed', probe.remote_run(encoded, manifest)['status'])
        with patch.object(probe.base, 'run', side_effect=lambda: result(cases)):
            report = probe.remote_run(encoded, manifest)
            self.assertEqual('passed', report['status'])
            self.assertFalse(report['deployed'])
            self.assertEqual([], report['coverage'])
            self.assertIn('MockRedis', report['external_adapters']['Redis'])

    def test_source_manifest_includes_unexecuted_dependencies(self):
        self.assertTrue({'refund_segment_boundary.py', 'refund_economic_segments.py', 'refund_funding.py',
                         'version_change_saga.py', 'version_change_worker.py'}.issubset(probe.SOURCE_FILES))
        self.assertEqual(len(probe.SOURCE_FILES), len(set(probe.SOURCE_FILES)))
        source = Path(probe.__file__).read_text()
        self.assertIn('SaaS revised segments lack trusted actual switch-time evidence', source)
        self.assertIn('Worker source is hashed and packaged, not executed', source)

    def test_streamid_is_executed_and_finance_is_denied(self):
        source = Path(probe.__file__).read_text()
        self.assertIn("db.scalars(select(Charge.id)", source)
        self.assertIn('numeric > (1, 2)', source)
        self.assertIn("ids == ['1-10']", source)
        self.assertIn("'finance', 403", source)
        self.assertIn("economic_snapshot_json='{}'", source)
        self.assertIn("decision='reject'", source)


if __name__ == '__main__':
    unittest.main()
