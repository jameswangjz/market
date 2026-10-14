"""Offline behavioral guards; these tests do not supply runtime evidence."""
from io import BytesIO
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import four_type_online_delivery_acceptance as harness
import zipfile


class OwnershipGuards(unittest.TestCase):
    def journal(self, path):
        return {'run_id': 'qa-four-012345abcdef', 'environment': 'a' * 64,
                'protected': 'b' * 64,
                'api': str(path.with_name(path.name + '.api.json')),
                'saas': str(path.with_name(path.name + '.saas.json')),
                'file': str(path.with_name(path.name + '.file.json')),
                'retry': str(path.with_name(path.name + '.retry.json'))}

    def test_changed_child_path_or_fingerprint_refused(self):
        path = Path('/tmp/owned.json')
        harness.validate_journal(self.journal(path), path)
        for field, value in [('api', '/tmp/another.json'), ('protected', ''),
                             ('run_id', 'qa-four-*')]:
            saved = self.journal(path)
            saved[field] = value
            with self.assertRaises(harness.api.AcceptanceFailure):
                harness.validate_journal(saved, path)

    def test_private_journal_exclusive_create(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'owned.json'
            saved = self.journal(path)
            harness.api.write_manifest(path, saved, create=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                harness.api.write_manifest(path, saved, create=True)

    def test_symlink_create_refused_without_changing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            harness.api.write_manifest(target, {'sentinel': True}, create=True)
            link = Path(directory) / 'link'
            link.symlink_to(target)
            before = target.read_bytes()
            with self.assertRaises(OSError):
                harness.api.write_manifest(link, {}, create=True)
            self.assertEqual(target.read_bytes(), before)


class ScanGuards(unittest.TestCase):
    def scanner(self, clean='clean', infected='infected', status='available', findings=None):
        calls = []

        def scan(stream, size):
            self.assertIsInstance(stream, BytesIO)
            payload = stream.read()
            self.assertEqual(len(payload), size)
            calls.append(payload)
            return (clean if len(calls) == 1 else infected), 'engine response'

        return SimpleNamespace(clamav_scan_stream=scan, presidio_analyze=lambda text: (
            findings if findings is not None else [{'entity': 'EMAIL_ADDRESS',
                'matched_text': 'acceptance.person@example.com', 'source': 'presidio'}], status))

    def test_real_probe_contract(self):
        self.assertEqual(harness.scan_probe(self.scanner())['status'], 'passed')

    def test_disabled_unavailable_false_clean_and_missing_detection_fail(self):
        for options in ({'clean': 'disabled'}, {'clean': 'unavailable'},
                        {'infected': 'clean'}, {'status': 'unavailable'}, {'findings': []},
                        {'findings': [{'entity': 'PERSON'}]}):
            with self.subTest(options=options), self.assertRaises(harness.api.AcceptanceFailure):
                harness.scan_probe(self.scanner(**options))


class RuntimeReportingGuards(unittest.TestCase):
    def snapshot(self, value=None):
        business = value or {}
        return {'business': business, 'business_hash': harness.digest(business),
                'observational': {}, 'observational_hash': harness.digest({})}
    def test_all_components_required_for_four_type_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(manifest=str(Path(directory) / 'owned.json'),
                                   cleanup=False, parent_reviewed=True, gateway_url='http://unused.invalid')
            with patch.object(harness.api, 'load_main', return_value=object()), \
                 patch.object(harness.api, 'environment', return_value='a' * 64), \
                 patch.object(harness, 'protected_fingerprint', return_value='b' * 64), \
                 patch.object(harness, 'protected_snapshot', return_value=self.snapshot()), \
                 patch.object(harness, 'scan_probe', return_value={'status': 'passed'}), \
                 patch.object(harness.api, 'run', return_value={'status': 'passed'}), \
                 patch.object(harness, 'saas_initial', return_value={'status': 'passed'}), \
                 patch.object(harness, 'file_initial', return_value={'status': 'passed'}), \
                 patch.object(harness, 'api_missing_route_retries', return_value={'status': 'passed'}):
                result = harness.run(args)
            self.assertEqual(result['status'], 'passed')
            self.assertIn('not deployed daemon', result['driver'])

    def test_review_gate_precedes_import_and_any_mutation(self):
        with patch.object(harness.api, 'load_main') as load:
            with self.assertRaisesRegex(harness.api.AcceptanceFailure, 'Parent review required'):
                harness.run(SimpleNamespace(parent_reviewed=False))
            load.assert_not_called()

    def test_protected_change_fails_even_after_component_success(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(manifest=str(Path(directory) / 'owned.json'),
                                   cleanup=False, parent_reviewed=True, gateway_url='http://unused.invalid')
            with patch.object(harness.api, 'load_main', return_value=object()), \
                 patch.object(harness.api, 'environment', return_value='a' * 64), \
                 patch.object(harness, 'protected_fingerprint', return_value='b' * 64), \
                 patch.object(harness, 'protected_snapshot', side_effect=[self.snapshot(), self.snapshot(),
                              self.snapshot({'products': {'["protected"]': {'name': 'c' * 64}}})]), \
                 patch.object(harness, 'scan_probe', return_value={'status': 'passed'}), \
                 patch.object(harness.api, 'run', return_value={'status': 'passed'}), \
                 patch.object(harness, 'saas_initial', return_value={'status': 'passed'}), \
                 patch.object(harness, 'file_initial', return_value={'status': 'passed'}), \
                 patch.object(harness, 'api_missing_route_retries', return_value={'status': 'passed'}):
                with self.assertRaisesRegex(harness.api.AcceptanceFailure, 'Protected core business snapshot changed'):
                    harness.run(args)


class RecoveryGuards(unittest.TestCase):
    def test_message_edges_have_two_exact_ownership_paths_only(self):
        self.assertEqual(harness.message_edge_ownership('own-message', 'real-reviewer',
                         {'own-message'}, {'fixture-user'}), 'owned_message')
        self.assertEqual(harness.message_edge_ownership('global-alert', 'fixture-user',
                         {'own-message'}, {'fixture-user'}), 'fixture_user_external_message')
        with self.assertRaisesRegex(harness.api.AcceptanceFailure, 'unowned message recipient'):
            harness.message_edge_ownership('global-alert', 'other-user', {'own-message'}, {'fixture-user'})
    def test_observational_changes_do_not_change_business_comparison(self):
        before = RuntimeReportingGuards().snapshot({'products': {'["p"]': {'name': 'a' * 64}}})
        after = copy.deepcopy(before)
        after['observational'] = {'application_health_slots': {'["slot"]': {'slot': 'b' * 64}}}
        after['observational_hash'] = harness.digest(after['observational'])
        harness.validate_protected_state(after)
        harness.assert_protected_core(before, after)
        self.assertIn('application_health_slots', harness.snapshot_differences(before['observational'], after['observational']))

    def recovery(self, directory):
        path = Path(directory) / 'old.json'
        saved = OwnershipGuards().journal(path)
        harness.api.write_manifest(path, saved, create=True)
        baseline = Path(directory) / 'trusted.json'
        harness.api.write_manifest(baseline, {'protected_order_fingerprint': 'd' * 64}, create=True)
        return SimpleNamespace(manifest=str(path), cleanup=True, parent_reviewed=True,
                               recover_observational_drift=True, trusted_baseline=str(baseline), recovery_report=None)

    def test_trusted_mismatch_refuses_before_cleanup_or_evidence_write(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.recovery(directory)
            m = SimpleNamespace(SessionLocal=lambda: nullcontext(None))
            with patch.object(harness.api, 'load_main', return_value=m), \
                 patch.object(harness.api, 'environment', return_value='a' * 64), \
                 patch.object(harness.saas, 'fingerprint', return_value='e' * 64), \
                 patch.object(harness, 'cleanup_owned_children') as cleanup:
                with self.assertRaisesRegex(harness.api.AcceptanceFailure, 'mismatches trusted earlier baseline'):
                    harness.recover_observational_drift(args)
                cleanup.assert_not_called()
                self.assertFalse(Path(args.manifest + '.recovery.json').exists())

    def test_legacy_recovery_preserves_original_hash_and_failed_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.recovery(directory)
            original = Path(args.manifest).read_bytes()
            m = SimpleNamespace(SessionLocal=lambda: nullcontext(None))
            state = RuntimeReportingGuards().snapshot()
            with patch.object(harness.api, 'load_main', return_value=m), \
                 patch.object(harness.api, 'environment', return_value='a' * 64), \
                 patch.object(harness.saas, 'fingerprint', return_value='d' * 64), \
                 patch.object(harness, 'protected_snapshot', return_value=state), \
                 patch.object(harness, 'protected_fingerprint', return_value='c' * 64), \
                 patch.object(harness, 'cleanup_owned_children') as cleanup:
                result = harness.recover_observational_drift(args)
                cleanup.assert_called_once()
            self.assertEqual(Path(args.manifest).read_bytes(), original)
            self.assertEqual(result['status'], 'recovered_cleanup')
            self.assertEqual(result['original_acceptance'], 'failed')
            self.assertFalse(result['legacy_full_fingerprint_matches'])
            self.assertTrue(result['trusted_core_verified_pre_post'])


class ArchiveAndJournalGuards(unittest.TestCase):
    def journal(self):
        ids = harness.file_ids('qa-four-file-012345abcdef')
        ids.update(files=['c' * 32], object_keys=[ids['users']['provider'] +
                   '/20261014/123456abcdef-' + ids['run_id'] + '.zip'], environment='a' * 64,
                   bucket='existing', authorized=True)
        return ids

    def test_real_archive_sample_and_eicar_are_inside_zip(self):
        for content in (b'Contact acceptance.person@example.com', harness.EICAR):
            with zipfile.ZipFile(BytesIO(harness.archive_payload(content))) as archive:
                self.assertEqual(archive.namelist(), ['evidence.txt'])
                self.assertEqual(archive.read('evidence.txt'), content)

    def test_foreign_object_path_and_file_ids_are_refused(self):
        harness.validate_file_journal(self.journal())
        for key, value in [('object_keys', ['outside/key.zip']),
                           ('object_keys', ['../escape']), ('files', ['ORD-protected']),
                           ('files', ['c' * 32, 'd' * 32]), ('object_keys', [])]:
            ids = self.journal()
            ids[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(harness.api.AcceptanceFailure):
                harness.validate_file_journal(ids)

    def test_scan_readiness_requires_persisted_clamav_and_archive_presidio(self):
        report = {'clamav_status': 'clean', 'clamav_report': 'stream: OK',
                  'presidio_status': 'available', 'sample_scanned': True,
                  'presidio_findings': [{'entity': 'EMAIL_ADDRESS', 'source': 'presidio',
                                        'matched_text': 'acceptance.person@example.com'}]}
        harness.assert_scan_report(report)
        for key, value in [('clamav_status', 'disabled'), ('presidio_status', 'unavailable'),
                           ('clamav_report', ''), ('sample_scanned', False), ('presidio_findings', [])]:
            bad = dict(report, **{key: value})
            with self.subTest(key=key), self.assertRaises(harness.api.AcceptanceFailure):
                harness.assert_scan_report(bad)


class RetryTimingGuards(unittest.TestCase):
    def attempts(self):
        origin = datetime(2026, 10, 14, tzinfo=timezone.utc)
        rows = []
        for index in range(3):
            start = origin + timedelta(seconds=index * 10.2)
            finish = start + timedelta(seconds=.1)
            rows.append({'started': start.isoformat(), 'finished': finish.isoformat(),
                         'outcome': False, 'error_present': True, 'retry_count': index + 1,
                         'max_retries': 3, 'main': 'paid',
                         'status': 'retrying' if index < 2 else 'exception',
                         'delivery': 'in_delivery' if index < 2 else 'exception',
                         'next_retry_at': (finish + timedelta(seconds=10)).isoformat() if index < 2 else None})
        return rows

    def test_three_real_deadlines_contract(self):
        harness.assert_retry_sequence(self.attempts())

    def test_shortened_deadline_early_retry_false_success_and_fourth_attempt_refused(self):
        for index, field, value in [(0, 'next_retry_at', '2026-10-14T00:00:02+00:00'),
                                    (1, 'started', '2026-10-14T00:00:05+00:00'),
                                    (2, 'status', 'retrying'), (2, 'retry_count', 2),
                                    (0, 'outcome', True), (0, 'error_present', False)]:
            rows = copy.deepcopy(self.attempts())
            rows[index][field] = value
            with self.subTest(field=field), self.assertRaises(harness.api.AcceptanceFailure):
                harness.assert_retry_sequence(rows)
        with self.assertRaises(harness.api.AcceptanceFailure):
            harness.assert_retry_sequence(self.attempts() + [self.attempts()[-1]])


@unittest.skipUnless(importlib.util.find_spec('sqlalchemy') and importlib.util.find_spec('fastapi'),
                     'Production route unit test requires the backend dependency environment')
class IsolatedFileRoutes(unittest.TestCase):
    """SQLite route/cleanup wiring with explicit scanner/storage unit doubles.

    Never invokes the runtime entry point, shared DB or real network services.
    """
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parents[1] / 'backend/tests/test_message_business_events.py'
        spec = importlib.util.spec_from_file_location('four_type_unit_fixture', path)
        cls.fixture_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixture_module)
        cls.fixture_module.BusinessRoutesHTTPTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.fixture_module.BusinessRoutesHTTPTests.tearDownClass()

    def test_file_upload_quota_reports_and_exact_cleanup_route_wiring(self):
        from minio.error import S3Error
        from sqlalchemy import event, select, func
        fixture = self.fixture_module.BusinessRoutesHTTPTests(methodName='runTest')
        fixture.setUp()
        m = fixture.m
        with m.SessionLocal() as db:
            db.add(m.ProductCategory(id='unit-review-parent', name='Unit review'))
            db.flush()
            db.add(m.ProductCategory(id='unit-review-child', name='Archive', parent_id='unit-review-parent'))
            db.commit()
        objects = {}
        writes = []
        class Response(BytesIO):
            def release_conn(self):
                pass
        class Storage:
            def __init__(self, *args, **kwargs):
                pass
            def bucket_exists(self, bucket):
                return True
            def stat_object(self, bucket, key):
                if key not in objects:
                    raise S3Error(response=None, code='NoSuchKey', message='unit missing object',
                                  resource=key, request_id='unit', host_id='unit')
                return SimpleNamespace(size=len(objects[key]))
            def put_object(self, bucket, key, data, length, **kwargs):
                # Prove journal-before-PUT, rather than just looking at final state.
                saved = harness.read_private(journal)
                self_test.assertIn(key, saved['object_keys'])
                self_test.assertEqual(saved['files'], [])
                objects[key] = data.read(length)
                writes.append(key)
            def get_object(self, bucket, key, offset=0, length=None):
                data = objects[key][offset:]
                return Response(data if length is None else data[:length])
            def remove_object(self, bucket, key):
                objects.pop(key, None)
        self_test = self
        def scanner(stream, size):
            content = stream.read(size)
            if content.startswith(b'\x89PNG'):
                from PIL import Image
                with Image.open(BytesIO(content)) as image:
                    self.assertEqual(image.size, (380, 280))
                self.assertNotIn(harness.EICAR, content, 'Appended PNG EICAR must not be an infected-positive probe')
                return 'clean', 'unit PNG stream OK'
            with zipfile.ZipFile(BytesIO(content)) as archive:
                infected = harness.EICAR in archive.read('evidence.txt')
            return ('infected', 'unit EICAR FOUND') if infected else ('clean', 'unit stream OK')
        findings = [{'entity': 'EMAIL_ADDRESS', 'source': 'presidio',
                     'matched_text': 'acceptance.person@example.com'}]
        def presidio(text):
            self.assertIn('acceptance.person@example.com', text)
            return findings, 'available'
        # PostgreSQL-only lock timeout has no SQLite equivalent in this unit test.
        def sqlite_statement(connection, cursor, statement, parameters, context, many):
            if statement == "SET LOCAL lock_timeout = '15s'":
                return 'SELECT 1', parameters
            return statement, parameters
        event.listen(m.engine, 'before_cursor_execute', sqlite_statement, retval=True)
        try:
            with tempfile.TemporaryDirectory() as directory:
                journal = Path(directory) / 'file.json'
                with patch.object(m, 'Minio', Storage), \
                     patch.object(m, 'MINIO_ENDPOINT', 'isolated-unit-storage'), \
                     patch.object(m, 'now', side_effect=lambda: datetime.now(timezone.utc).replace(tzinfo=None)), \
                     patch.object(m, 'clamav_scan_stream', side_effect=scanner), \
                     patch.object(m, 'presidio_analyze', side_effect=presidio), \
                     patch.object(m, 'clamav_version', return_value='unit'), \
                     patch.object(m, 'presidio_version', return_value='unit'):
                    result = harness.file_initial(m, journal)
                    self.assertEqual(result['paid_download_http'], [200, 200, 429])
                    self.assertEqual(result['readiness'], [False, True, False])
                    self.assertEqual(len(writes), 3)
                    self.assertEqual([step['after'] for step in result['review_chain']],
                                     ['quality_review', 'security_review', 'operation_review', 'published'])
                    self.assertEqual(result['reviewer_inbox_stages'], 4)
                    self.assertEqual(result['logo_rejection_http'],
                                     {'malformed_image': 400, 'wrong_dimensions': 400, 'svg_injection': 400})
                    self.assertEqual(result['raw_logo_clamav']['status'], 'clean')
                    self.assertEqual(len(result['raw_logo_clamav']['sha256']), 64)
                    self.assertNotIn('infected_logo_http', result)
                    self.assertEqual(objects, {})
                    saved = harness.read_private(journal)
                    self.assertTrue(saved['cleaned'])
                    self.assertEqual(len(saved['files']), 1)
                    self.assertEqual(len(saved['logos']), 2)
                    with m.SessionLocal() as db:
                        self.assertIsNotNone(db.get(m.Product, 'product'))
                        self.assertIsNotNone(db.get(m.FileObject, 'product_data'))
                        self.assertEqual(db.scalar(select(func.count()).select_from(m.ProductSecurityScan)), 0)
                    harness.cleanup_file(m, saved, journal)
        finally:
            event.remove(m.engine, 'before_cursor_execute', sqlite_statement)
            fixture.tearDown()

    def test_exact_order_driver_uses_production_handler_and_rejects_early_retry(self):
        from sqlalchemy import select, func
        fixture = self.fixture_module.BusinessRoutesHTTPTests(methodName='runTest')
        fixture.setUp()
        m = fixture.m
        try:
            with m.SessionLocal() as db:
                db.get(m.Enterprise, 'buyer_tenant').verification_status = 'verified'
                db.commit()
            clock = datetime(2026, 10, 14)
            ids = {'products': ['product'], 'users': {'ops': 'ops'}}
            with patch.object(m, 'now', side_effect=lambda: clock), \
                 patch.dict(m.online_delivery, {'process_once': lambda: self.fail('Global queue selector invoked')}):
                oid = fixture.create_order()
                with harness.held_payment_tasks(m, oid):
                    fixture.pay(oid)
                attempts = []
                for count in range(3):
                    attempts.append(harness.process_online_delivery_once(m, ids, oid, count, release=count == 0))
                    if count < 2:
                        with self.assertRaisesRegex(harness.api.AcceptanceFailure, 'Owned task not due'):
                            harness.process_online_delivery_once(m, ids, oid, count + 1)
                        clock += timedelta(seconds=10.05)
                harness.assert_retry_sequence(attempts)
                with m.SessionLocal() as db:
                    self.assertEqual(db.scalar(select(func.count()).select_from(m.AuditLog).where(
                        m.AuditLog.order_id == oid, m.AuditLog.action == 'delivery_failed')), 3)
                    task = db.scalar(select(m.DeliveryTask).where(m.DeliveryTask.order_id == oid))
                    self.assertEqual((task.status, task.retry_count, task.next_retry_at), ('exception', 3, None))
                fixture.request('POST', f"/api/delivery-tasks/{attempts[-1]['task_id']}/process",
                                actor='ops', expected=409, json={'success': True})
        finally:
            fixture.tearDown()

    def test_new_model_serving_application_is_canonical_api_product(self):
        from fastapi import HTTPException
        fixture = self.fixture_module.BusinessRoutesHTTPTests(methodName='runTest')
        fixture.setUp()
        m = fixture.m
        try:
            ids = harness.api.fixture_ids()
            harness.api.prepare(m, ids)
            with m.SessionLocal() as db:
                products = [db.get(m.Product, pid) for pid in ids['products']]
                self.assertEqual([(row.product_type, row.delivery_method) for row in products],
                                 [('api', 'api'), ('api', 'api')])
                self.assertEqual(products[1].description, 'Model-serving API application')
                for product in products:
                    m.trading_policy['validate_product_delivery'](db, product)
                model_file = SimpleNamespace(id='unit-model-file', product_type='model', delivery_method='file')
                m.trading_policy['validate_product_delivery'](db, model_file)
                with self.assertRaises(HTTPException) as caught:
                    m.trading_policy['validate_product_delivery'](db, None, 'model', 'api')
                self.assertEqual(caught.exception.status_code, 400)
        finally:
            fixture.tearDown()

    def protected_fixture(self, fixture):
        m = fixture.m
        with m.SessionLocal() as db:
            db.get(m.Enterprise, 'buyer_tenant').verification_status = 'verified'
            db.add(m.Product(id=harness.PROTECTED_PRODUCT, name=harness.PROTECTED_PRODUCT,
                             enterprise_id='provider_tenant', product_type='api', delivery_method='api'))
            db.commit()
        oid = fixture.create_order()
        with m.SessionLocal() as db:
            db.get(m.Order, oid).order_no = harness.saas.PROTECTED_ORDER
            db.commit()
        return harness.protected_snapshot(m)

    def test_actual_health_rows_are_observational_but_still_protected_from_cleanup(self):
        from sqlalchemy import select
        fixture = self.fixture_module.BusinessRoutesHTTPTests(methodName='runTest')
        fixture.setUp()
        m = fixture.m
        try:
            before = self.protected_fixture(fixture)
            Health = m.application_health['Observation']
            with m.SessionLocal() as db:
                db.add(Health(product_id=harness.PROTECTED_PRODUCT, version_id='unit-version',
                    last_slot=1, last_checked_at=m.now(), last_status='healthy', updated_at=m.now()))
                db.commit()
            after = harness.protected_snapshot(m)
            harness.assert_protected_core(before, after)
            self.assertNotEqual(before['observational_hash'], after['observational_hash'])
            with m.SessionLocal() as db:
                with self.assertRaisesRegex(harness.api.AcceptanceFailure, 'intersects marked protected'):
                    harness.reject_protected_candidates(m, db, {Health.__tablename__:
                        Health.product_id == harness.PROTECTED_PRODUCT}, after)
                db.get(m.Product, harness.PROTECTED_PRODUCT).description = 'business change'
                db.commit()
            changed = harness.protected_snapshot(m)
            with self.assertRaisesRegex(harness.api.AcceptanceFailure, 'core business snapshot changed'):
                harness.assert_protected_core(before, changed)
            self.assertIn(m.Product.__tablename__, harness.snapshot_differences(before['business'], changed['business']))
        finally:
            fixture.tearDown()

    def test_delete_time_guard_refuses_protected_root_even_after_owned_preflight(self):
        from sqlalchemy import delete
        fixture = self.fixture_module.BusinessRoutesHTTPTests(methodName='runTest')
        fixture.setUp()
        m = fixture.m
        try:
            protected = self.protected_fixture(fixture)
            ids = harness.api.fixture_ids()
            ids.update(authorized=True, consumers=[], quota_windows=[], environment=harness.api.environment(m))
            harness.api.prepare(m, ids)
            with tempfile.TemporaryDirectory() as directory:
                saved = {key: str(Path(directory) / (key + '.json')) for key in ('api', 'retry', 'saas', 'file')}
                harness.api.write_manifest(Path(saved['api']), ids, create=True)
                def wrong_cleanup(runtime, identifiers, path):
                    with runtime.SessionLocal() as db:
                        db.execute(delete(runtime.Product).where(runtime.Product.id == harness.PROTECTED_PRODUCT))
                        db.commit()
                with patch.object(harness.api, 'cleanup', side_effect=wrong_cleanup):
                    with self.assertRaisesRegex(harness.api.AcceptanceFailure, 'intersects marked protected'):
                        harness.cleanup_owned_children(m, saved, protected)
            with m.SessionLocal() as db:
                self.assertIsNotNone(db.get(m.Product, harness.PROTECTED_PRODUCT))
        finally:
            fixture.tearDown()

    def test_file_cleanup_preserves_external_message_and_other_recipients(self):
        from sqlalchemy import event, select, func
        fixture = self.fixture_module.BusinessRoutesHTTPTests(methodName='runTest')
        fixture.setUp()
        m = fixture.m
        Receipt, Delivery, Attempt, Expired = (m.message_center[key] for key in
                                             ('Receipt', 'Delivery', 'DeliveryAttempt', 'ExpiredUrgent'))
        ids = harness.file_ids()
        def sqlite_statement(connection, cursor, statement, parameters, context, many):
            return ('SELECT 1', parameters) if statement == "SET LOCAL lock_timeout = '15s'" else (statement, parameters)
        class Storage:
            def __init__(self, *args, **kwargs):
                pass
        event.listen(m.engine, 'before_cursor_execute', sqlite_statement, retval=True)
        try:
            with patch.object(m, 'MINIO_ENDPOINT', 'isolated-edge-test'), patch.object(m, 'Minio', Storage):
                ids.update(files=[], logos={}, object_keys=[], environment=harness.file_environment(m),
                           bucket=m.MINIO_BUCKET, authorized=True)
                with m.SessionLocal() as db:
                    for eid in ids['enterprises']:
                        db.add(m.Enterprise(id=eid, name=eid, credit_code=eid))
                    for uid in ids['users'].values():
                        db.add(m.User(id=uid, name=uid, email=uid + '@example.invalid', password_hash='unit'))
                    db.flush()
                    db.add(m.Product(id=ids['products'][0], name=ids['products'][0],
                                     enterprise_id=ids['enterprises'][1], product_type='dataset', delivery_method='file'))
                    db.flush()
                    db.add(m.ProductReleaseVersion(id=ids['versions'][0], product_id=ids['products'][0],
                                                   version_code='v1', price=12, cost=7))
                    db.flush()
                    owned = m.message_center['create'](db, ['reviewer'], 'Owned review', 'Unit',
                        'product', ids['products'][0], category='review', tenant_id=ids['enterprises'][1],
                        event_key=f"review:product:{ids['products'][0]}:pending_review:product_manager:unit")
                    external = m.message_center['create'](db, [ids['users']['release'], 'ops'], 'Global alert', 'Unit',
                        'development_task', '', category='ops', event_key='unit-global-external-alert')
                    db.flush()
                    own_receipt = db.scalar(select(Receipt).where(Receipt.message_id == external.id,
                                                                Receipt.user_id == ids['users']['release']))
                    own_delivery = db.scalar(select(Delivery).where(Delivery.message_id == external.id,
                        Delivery.user_id == ids['users']['release']).order_by(Delivery.id))
                    db.add(Expired(receipt_id=own_receipt.id, message_id=external.id,
                                   user_id=ids['users']['release'], expired_at=m.now()))
                    db.add(Attempt(delivery_id=own_delivery.id, lease_token='unit-token', attempt_no=1))
                    db.commit()
                    external_id, owned_id = external.id, owned.id
                    external_before = {c.name: getattr(external, c.name) for c in external.__table__.columns}
                    real_receipt_ids = set(db.scalars(select(Receipt.id).where(Receipt.message_id == external_id,
                                                                            Receipt.user_id == 'ops')))
                    real_delivery_ids = set(db.scalars(select(Delivery.id).where(Delivery.message_id == external_id,
                                                                              Delivery.user_id == 'ops')))
                    predicates, _ = harness.file_scopes(m, db, ids)
                    self.assertNotIn(external_id, set(db.scalars(select(m.message_center['Message'].id)
                        .where(predicates[m.message_center['Message'].__tablename__]))))
                    # Even a classified personal edge cannot bypass marked-protected-row checks.
                    marked = {'business': {}, 'observational': {Receipt.__tablename__: {
                        json.dumps([own_receipt.id]): {}}}}
                    with self.assertRaisesRegex(harness.api.AcceptanceFailure, 'intersects marked protected'):
                        harness.reject_protected_candidates(m, db, predicates, marked)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'file.json'
                    harness.api.write_manifest(path, ids, create=True)
                    harness.cleanup_file(m, ids, path)
                with m.SessionLocal() as db:
                    message = db.get(m.message_center['Message'], external_id)
                    self.assertEqual({c.name: getattr(message, c.name) for c in message.__table__.columns}, external_before)
                    self.assertIsNone(db.get(m.message_center['Message'], owned_id))
                    self.assertIsNotNone(db.get(m.User, 'ops'))
                    self.assertIsNotNone(db.get(m.User, 'reviewer'))
                    self.assertEqual(set(db.scalars(select(Receipt.id).where(Receipt.message_id == external_id))), real_receipt_ids)
                    self.assertEqual(set(db.scalars(select(Delivery.id).where(Delivery.message_id == external_id))), real_delivery_ids)
                    self.assertEqual(db.scalar(select(func.count()).select_from(Attempt)), 0)
                    self.assertEqual(db.scalar(select(func.count()).select_from(Expired)), 0)
                    self.assertTrue(all(db.get(m.User, uid) is None for uid in ids['users'].values()))
        finally:
            event.remove(m.engine, 'before_cursor_execute', sqlite_statement)
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
