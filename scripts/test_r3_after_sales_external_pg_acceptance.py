"""External-mode guards only; no network, database or actual scanner calls."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import r3_after_sales_external_pg_acceptance as probe


class MissingObject(Exception):
    code='NoSuchKey'


class FakeMinio:
    def __init__(self):
        self.objects={}
        self.removals=[]
        self.fail_after_put=False
        self.fail_remove=False

    def put_object(self,bucket,name,stream,*,length,**kwargs):
        self.objects[bucket,name]=stream.read(length)
        if self.fail_after_put:
            raise TimeoutError('OWNED_PRIVATE_SENTINEL')

    def get_object(self,bucket,name,**kwargs):
        return io.BytesIO(self.objects[bucket,name])

    def remove_object(self,bucket,name,**kwargs):
        if self.fail_remove:
            raise TimeoutError('OWNED_PRIVATE_SENTINEL')
        self.removals.append((bucket,name))
        self.objects.pop((bucket,name),None)

    def stat_object(self,bucket,name):
        if (bucket,name) not in self.objects:
            raise MissingObject()
        return object()


class ExternalProbeGuards(unittest.TestCase):
    def owned(self):
        owner=probe.ObjectOwnership()
        self.addCleanup(lambda:owner.journal.unlink(missing_ok=True))
        owner.tickets.add('a'*32)
        client=FakeMinio()
        return owner,client,probe.ScopedMinio(client,owner,'configured-owned-bucket')

    def test_exact_owned_object_write_and_cleanup_head_verification(self):
        owner,client,scoped=self.owned()
        name='after-sales/'+'a'*32+'/'+'b'*32
        scoped.put_object('configured-owned-bucket',name,io.BytesIO(b'owned'),length=5,content_type='text/plain')
        self.assertEqual(scoped.get_object('configured-owned-bucket',name).read(),b'owned')
        result=owner.cleanup()
        self.assertEqual(result['status'],'passed')
        self.assertEqual(result['removed_and_head_verified'],1)
        self.assertEqual(client.objects,{})
        self.assertEqual(owner.journal.stat().st_mode & 0o777,0o600)

    def test_wrong_bucket_foreign_ticket_and_oversize_never_reach_sdk(self):
        owner,client,scoped=self.owned()
        valid='after-sales/'+'a'*32+'/'+'b'*32
        for bucket,name,size in [('wrong',valid,1),('configured-owned-bucket','after-sales/'+'c'*32+'/'+'b'*32,1),
                ('configured-owned-bucket',valid,4097),('configured-owned-bucket','after-sales/../business',1)]:
            with self.assertRaises(probe.group.base.AcceptanceFailure):
                scoped.put_object(bucket,name,io.BytesIO(b'x'),length=size,content_type='text/plain')
        self.assertEqual(client.objects,{})
        self.assertEqual(owner.records,{})

    def test_put_intent_is_recorded_before_sdk_response_failure(self):
        owner,client,scoped=self.owned()
        client.fail_after_put=True
        name='after-sales/'+'a'*32+'/'+'b'*32
        with self.assertRaises(TimeoutError):
            scoped.put_object('configured-owned-bucket',name,io.BytesIO(b'owned'),length=5,content_type='text/plain')
        self.assertEqual(len(json.loads(owner.journal.read_text())['objects']),1)
        self.assertEqual(owner.cleanup()['removed_and_head_verified'],1)

    def test_failed_cleanup_preserves_exact_owned_journal_and_retries(self):
        owner,client,scoped=self.owned()
        name='after-sales/'+'a'*32+'/'+'b'*32
        scoped.put_object('configured-owned-bucket',name,io.BytesIO(b'x'),length=1,content_type='text/plain')
        client.fail_remove=True
        result=owner.cleanup()
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('OWNED_PRIVATE_SENTINEL',str(result))
        self.assertEqual(json.loads(owner.journal.read_text())['objects'][0]['object_name'],name)
        client.fail_remove=False
        self.assertEqual(owner.cleanup()['status'],'passed')

    def test_non_owned_read_or_remove_is_rejected(self):
        owner,client,scoped=self.owned()
        for operation in (scoped.get_object,scoped.remove_object):
            with self.assertRaises(probe.group.base.AcceptanceFailure):
                operation('configured-owned-bucket','original/business/object')
        self.assertEqual(client.removals,[])

    def test_cluster_targets_reject_external_or_wrong_services(self):
        m=SimpleNamespace(MINIO_ENDPOINT='market-minio:9000',CLAMAV_HOST='market-clamav',CLAMAV_PORT=3310)
        probe.check_cluster_targets(m)
        for field,value in [('MINIO_ENDPOINT','external.invalid:9000'),('CLAMAV_HOST','external.invalid'),('CLAMAV_PORT',80)]:
            other=SimpleNamespace(**vars(m));setattr(other,field,value)
            with self.assertRaises(probe.group.base.AcceptanceFailure):
                probe.check_cluster_targets(other)

    def test_source_guard_aborts_before_database_or_external_operations(self):
        with patch.object(probe.importlib.util,'find_spec',return_value=SimpleNamespace(origin='/missing/app/__init__.py')), \
             patch.object(probe.group,'run_isolated') as run:
            with self.assertRaises(probe.group.base.AcceptanceFailure):
                probe.run({})
            run.assert_not_called()

    def test_standard_test_signature_is_memory_only_and_expected_length(self):
        self.assertEqual(len(probe.EICAR),68)
        self.assertIn(b'EICAR-STANDARD-ANTIVIRUS-TEST-FILE',probe.EICAR)


if __name__=='__main__':
    unittest.main()
