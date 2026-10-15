"""Offline guards for QA003; never connects to SSH, PostgreSQL or storage."""
import hashlib
import base64
import io
from pathlib import Path
import tempfile
import unittest
import zlib
from unittest.mock import patch
from unittest.mock import Mock

import r3_after_sales_group_pg_acceptance as probe


class AfterSalesGroupGuards(unittest.TestCase):
    def test_expected_non_json_error_is_not_parsed_or_misreported(self):
        response=Mock(status_code=500)
        response.json.side_effect=ValueError('not JSON')
        self.assertEqual(probe.checked_json(response,500),{})
        response.json.assert_not_called()
        with self.assertRaises(probe.base.AcceptanceFailure):
            probe.checked_json(response,200)

    def test_order_fixture_ids_fit_real_pg_columns_and_are_distinct(self):
        identities=[probe.owned_order_identity('rollback-'+op+'-'+failure)
            for op in ('action','comment','attachment') for failure in ('audit','outbox')]
        self.assertEqual(len({item[0] for item in identities}),6)
        for identifier,number in identities:
            self.assertLessEqual(len(identifier),36)
            self.assertLessEqual(len(number),40)
            self.assertTrue(identifier.startswith('owned-o-'))

    def test_only_existing_owned_schema_pattern_is_used(self):
        self.assertTrue(probe.base.owned_schema('qa_r3_be_0123456789abcdef'))
        for value in ('public','qa_r3_be_','qa_r3_be_0123456789abcdef,public','qa_r3_be_0123456789abcdef;DROP'):
            self.assertFalse(probe.base.owned_schema(value))

    def test_matching_source_fingerprints_and_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            expected={}
            for name in probe.SOURCE_FILES:
                (root/name).write_text('# owned source\n')
                expected[name]=hashlib.sha256(b'# owned source\n').hexdigest()
            self.assertEqual(probe.source_fingerprint(root,expected),expected)
            (root/'after_sales_collaboration.py').write_text('# changed\n')
            with self.assertRaises(probe.base.AcceptanceFailure):
                probe.source_fingerprint(root,expected)

    def test_source_mismatch_aborts_before_harness_database_access(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(probe.base,'run') as run:
            with self.assertRaises(probe.base.AcceptanceFailure):
                probe.run_isolated(Path(directory),'installed',{})
            run.assert_not_called()

    def test_linked_source_and_extra_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in probe.SOURCE_FILES:
                (root/name).write_text('# owned\n')
            actual=probe.source_fingerprint(root)
            with self.assertRaises(probe.base.AcceptanceFailure):
                probe.source_fingerprint(root,{**actual,'unexpected.py':'0'*64})
            (root/'main.py').unlink()
            (root/'main.py').symlink_to(root/'after_sales_workflow.py')
            with self.assertRaises(probe.base.AcceptanceFailure):
                probe.source_fingerprint(root)

    def test_storage_is_owned_bounded_and_compensated(self):
        storage=probe.OwnedStorage()
        storage.put_object('owned-qa003','after-sales/owned/file',io.BytesIO(b'owned'),length=5,content_type='text/plain')
        response=storage.get_object('owned-qa003','after-sales/owned/file')
        self.assertEqual(response.read(),b'owned')
        response.close();response.release_conn()
        storage.remove_object('owned-qa003','after-sales/owned/file')
        self.assertEqual(storage.objects,{})
        for bucket,name,size in [('public','after-sales/owned/file',1),('owned-qa003','foreign/file',1),
                                 ('owned-qa003','after-sales/owned/file',4097)]:
            with self.assertRaises(probe.base.AcceptanceFailure):
                storage.put_object(bucket,name,io.BytesIO(b'x'),length=size,content_type='text/plain')

    def test_transport_integrity_guard_remains_enabled(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(AssertionError):
            encoded=base64.b64encode(zlib.compress(b'{"../main.py":"owned"}')).decode()
            probe.transport.unpack_workingtree(encoded,{'../main.py':hashlib.sha256(b'owned').hexdigest()},Path(directory))

    def test_exception_messages_cannot_leak_secrets(self):
        result=probe.base.safe_error(ValueError('PRIVATE_SENTINEL DATABASE_URL password token'))
        self.assertNotIn('PRIVATE_SENTINEL',str(result))
        self.assertEqual(set(result),{'status','error_type','line'})


if __name__=='__main__':
    unittest.main()
