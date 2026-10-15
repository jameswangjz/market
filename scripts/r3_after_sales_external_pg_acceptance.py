"""Bounded installed-source QA003 real ClamAV/MinIO integration.

Run only after rollout, inside the API container with these existing helpers:
  python -B r3_after_sales_external_pg_acceptance.py --expected-source-manifest FILE
The manifest must match r3_after_sales_group_pg_acceptance.SOURCE_FILES exactly.
Fresh PG schema only; no startup or public writes. Real storage writes are
restricted to exact random after-sales/ticket/object names recorded BEFORE PUT.
No browser/ingress proof. No secrets, endpoints or response bodies are printed.
"""
from contextlib import ExitStack
import argparse
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from unittest.mock import patch

import r3_after_sales_group_pg_acceptance as group


NORMAL = b'Owned QA003 real scanner/storage roundtrip.\n'
# The standard harmless antivirus test signature is assembled only in memory.
EICAR = (b'X5O!P%@AP[4\\PZX54(P^)7CC)7}$' + b'EICAR-STANDARD-' +
         b'ANTIVIRUS-TEST-FILE!$H+H*')


def check_cluster_targets(m):
    suffixes=('', '.market', '.market.svc', '.market.svc.cluster.local')
    group.base.check(m.MINIO_ENDPOINT in {'market-minio'+s+':9000' for s in suffixes},
                     'Only the existing in-cluster MinIO endpoint is allowed')
    group.base.check(m.CLAMAV_HOST in {'market-clamav'+s for s in suffixes} and m.CLAMAV_PORT==3310,
                     'Only the existing in-cluster ClamAV endpoint is allowed')


class ObjectOwnership:
    def __init__(self):
        fd, name = tempfile.mkstemp(prefix='qa-r3-after-sales-external-',suffix='.json')
        os.close(fd)
        self.journal = Path(name)
        self.tickets = set()
        self.records = {}
        self.schema = None
        self.persist()

    def persist(self):
        value = {'schema':self.schema, 'tickets':sorted(self.tickets),
                 'objects':[{'bucket':bucket,'object_name':name,'removed':record['removed']}
                    for (bucket,name),record in self.records.items()]}
        fd, temporary = tempfile.mkstemp(prefix='.qa003-',dir=self.journal.parent)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump(value,stream,sort_keys=True)
                stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,self.journal)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def permitted(self, bucket, name):
        match = re.fullmatch(r'after-sales/([a-f0-9]{32})/[a-f0-9]{32}',name or '')
        return bool(isinstance(bucket,str) and bucket and match and match[1] in self.tickets)

    def register(self, client, bucket, name):
        group.base.check(self.permitted(bucket,name),'Object outside exact owned ticket scope')
        group.base.check((bucket,name) not in self.records,'Object intent registered twice')
        self.records[bucket,name]={'client':client,'removed':False}
        self.persist()

    def cleanup(self):
        failures = 0
        for (bucket,name),record in self.records.items():
            try:
                group.base.check(self.permitted(bucket,name),'Object cleanup ownership mismatch')
                record['client'].remove_object(bucket,name)
                try:
                    record['client'].stat_object(bucket,name)
                except Exception as exc:
                    if getattr(exc,'code',None) not in {'NoSuchKey','NoSuchObject'}:
                        raise
                else:
                    raise AssertionError('Owned object remains after deletion')
                record['removed']=True
            except Exception:
                failures += 1
        self.persist()
        return {'status':'passed' if failures==0 else 'failed', 'tracked_objects':len(self.records),
                'removed_and_head_verified':sum(r['removed'] for r in self.records.values()),
                'cleanup_failures':failures,'private_ownership_journal':str(self.journal),
                'journal_sha256':hashlib.sha256(self.journal.read_bytes()).hexdigest()}


class ScopedMinio:
    def __init__(self, client, ownership, bucket):
        self.client,self.ownership,self.bucket=client,ownership,bucket

    def put_object(self,bucket,name,stream,*,length,content_type,**kwargs):
        group.base.check(bucket==self.bucket and 0 < length <= 4096,'Unbounded or wrong-bucket object write')
        self.ownership.register(self.client,bucket,name)
        return self.client.put_object(bucket,name,stream,length=length,content_type=content_type,**kwargs)

    def get_object(self,bucket,name,**kwargs):
        group.base.check(bucket==self.bucket and (bucket,name) in self.ownership.records,
                         'Non-owned storage read forbidden')
        return self.client.get_object(bucket,name,**kwargs)

    def remove_object(self,bucket,name,**kwargs):
        group.base.check(bucket==self.bucket and (bucket,name) in self.ownership.records,
                         'Non-owned storage cleanup forbidden')
        return self.client.remove_object(bucket,name,**kwargs)


def external_exercise(m,client,headers,report):
    from minio import Minio
    from sqlalchemy import func,select,text
    ownership=ObjectOwnership()
    results={}
    real_scan=m.clamav_scan_stream
    scans=[]
    def scan(*args,**kwargs):
        group.base.check(0 < kwargs.get('timeout_seconds',0) <= 15,'Production scan timeout missing/unbounded')
        result=real_scan(*args,**kwargs)
        scans.append(result[0])
        return result
    def storage(*args,**kwargs):
        # Restores only real SDK construction, retaining production PoolManager
        # timeout/retry kwargs supplied by collaboration.storage_client().
        return ScopedMinio(Minio(*args,**kwargs),ownership,m.MINIO_BUCKET)
    def call(method,path,expected=200,actor='buyer',**kwargs):
        response=client.request(method,path,headers=headers[actor],**kwargs)
        group.base.check(response.status_code==expected,'Unexpected owned external HTTP status')
        return response
    def upload(ticket,key,content=NORMAL,purpose='evidence',expected=200):
        return call('POST','/api/after-sales/'+ticket['id']+'/attachments',expected,
            files={'upload':('owned-evidence.txt',content,'text/plain')},
            data={'expected_version':ticket['version'],'idempotency_key':key,'purpose':purpose})
    def state(ticket):
        with m.SessionLocal() as db:
            row=db.get(m.AfterSalesTicket,ticket['id'])
            return (row.status,row.version,row.first_response_at,row.closed_at,row.sla_snapshot_json,
                db.scalar(select(func.count()).select_from(m.AfterSalesEvent).where(
                    m.AfterSalesEvent.ticket_id==row.id)))
    try:
        check_cluster_targets(m)
        group.base.check(m.CLAMAV_ENABLED and bool(m.MINIO_ENDPOINT),'Real scanner/storage configuration unavailable')
        with m.SessionLocal.begin() as db:
            ownership.schema=db.scalar(text('SELECT current_schema()'))
            group.base.check(group.base.owned_schema(ownership.schema),'External probe escaped owned schema')
            order_id,order_no=group.owned_order_identity('external-services')
            db.add(m.Order(id=order_id,order_no=order_no,buyer_user_id='owned-buyer',
                buyer_enterprise_id='owned-buyer',provider_enterprise_id='owned-provider',product_id='owned-product',
                product_name='Owned external QA003',buyer_name='Owned buyer',payment_status='paid',
                main_status='completed',delivery_status='accepted',amount=10,paid_amount=10))
        ticket=call('POST','/api/after-sales',json={'order_id':order_id,'type':'owned-external',
            'description':'Owned real external acceptance','idempotency_key':'owned-external-create'}).json()
        group.base.check(bool(re.fullmatch(r'[a-f0-9]{32}',ticket['id'])),'Unexpected owned ticket identity')
        ownership.tickets.add(ticket['id']);ownership.persist()
        with ExitStack() as adapters:
            adapters.enter_context(patch.object(m,'Minio',side_effect=storage))
            adapters.enter_context(patch.object(m,'clamav_scan_stream',side_effect=scan))
            before=state(ticket)
            attachment=upload(ticket,'owned-external-normal').json()
            group.base.check(attachment['scan_status']=='clean','Normal file not scanned clean')
            download=call('GET','/api/after-sales/'+ticket['id']+'/attachments/'+attachment['id']+'/download',actor='provider')
            group.base.check(download.content==NORMAL,'Real MinIO downloaded bytes differ')
            group.base.check(state(ticket)==before,'Upload/download changed first response or state')
            results['normal_real_scan_upload_download']={'status':'passed','bytes':len(NORMAL),
                'sha256':hashlib.sha256(NORMAL).hexdigest(),'first_response_unchanged':True}
            object_count=len(ownership.records)
            with m.SessionLocal() as db:
                files_before=db.scalar(select(func.count()).select_from(m.FileObject))
            upload(ticket,'owned-external-eicar',EICAR,expected=400)
            group.base.check(scans[-1]=='infected','EICAR rejection not proven by real scanner')
            group.base.check(len(ownership.records)==object_count,'EICAR reached object storage')
            with m.SessionLocal() as db:
                group.base.check(db.scalar(select(func.count()).select_from(m.FileObject))==files_before,
                                 'Rejected EICAR left file row')
            results['real_eicar_rejected_before_storage']={'status':'passed','http_status':400,'new_objects':0,'new_files':0}
            ticket=call('POST','/api/after-sales/'+ticket['id']+'/actions',actor='ops',json={
                'action':'force_close','reason':'Owned external terminal check','expected_version':ticket['version'],
                'idempotency_key':'owned-external-force-close'}).json()
            closed=state(ticket)
            scans_before=len(scans)
            upload(ticket,'owned-external-terminal-normal',expected=409)
            group.base.check(len(scans)==scans_before and len(ownership.records)==object_count,
                             'Terminal rejected evidence reached external services')
            audit_attachment=upload(ticket,'owned-external-terminal-audit',purpose='audit_comment').json()
            content=call('GET','/api/after-sales/'+ticket['id']+'/attachments/'+audit_attachment['id']+'/download')
            group.base.check(content.content==NORMAL and state(ticket)==closed,'Terminal audit changed immutable chain')
            results['terminal_audit_comment_real_storage']={'status':'passed','normal_evidence':409,
                'audit_comment':200,'bytes_equal':True,'closed_chain_unchanged':True}
    except Exception as exc:
        results['execution_failure']=group.base.safe_error(exc)
    finally:
        report['object_cleanup']=ownership.cleanup()
        results['exact_owned_object_cleanup']={'status':report['object_cleanup']['status'],
            'tracked_objects':report['object_cleanup']['tracked_objects'],
            'removed_and_head_verified':report['object_cleanup']['removed_and_head_verified']}
        report['real_scan_results']=scans
    return results


def run(expected):
    spec=importlib.util.find_spec('app')
    group.base.check(spec is not None and spec.origin is not None,'Installed app package unavailable')
    root=Path(spec.origin).resolve().parent
    group.source_fingerprint(root,expected)
    report={}
    with patch.object(group,'exercise',side_effect=lambda m,c,h:external_exercise(m,c,h,report)):
        result=group.run_isolated(root,'installed',expected)
    result.update(report)
    result['scope']='Installed source; owned PG schema; actual cluster ClamAV and exact owned MinIO objects; ASGI not ingress'
    result['external_adapters']={'MinIO':'real SDK with exact-object ownership guard',
                                 'ClamAV':'real installed scanner with production timeout'}
    result['gaps']=['No browser/ingress or full QA003 task acceptance; no SMTP/global configuration edits']
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-source-manifest',required=True)
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        result=run(json.loads(Path(args.expected_source_manifest).read_text()))
    except Exception as exc:
        result={'status':'failed','error':group.base.safe_error(exc)}
    print(json.dumps(result,ensure_ascii=True))
    return 0 if result.get('status')=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
