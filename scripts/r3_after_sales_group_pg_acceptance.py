"""QA003 collaboration acceptance in a fresh owned PostgreSQL schema only.

Workingtree: SSHPASS=... python -B script.py --pod market-api-...
Installed image: python -B script.py --installed --expected-source-manifest FILE
No lifespan/startup, seeding of public, browser, ingress or real storage/scan.
Uses the existing PG isolation harness and workingtree integrity transport.
Only sanitized outcomes and source fingerprints are printed.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import importlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

import r3_backend_acceptance_pg as base
import r3_backend_acceptance_concurrency as transport


SOURCE_FILES = ('main.py', 'after_sales_workflow.py', 'after_sales_collaboration.py',
                'ticket_sla_clocks.py', 'enterprise_sla.py')


def owned_order_identity(name):
    digest = hashlib.sha256(name.encode()).hexdigest()[:20]
    return 'owned-o-'+digest, 'OWNED-QA003-'+digest


def checked_json(response, expected):
    # Starlette's expected fault-injection 500 is intentionally plain text.
    base.check(response.status_code == expected, 'Unexpected HTTP status')
    return response.json() if expected < 400 else {}


def source_fingerprint(root, expected=None):
    result = {}
    for name in SOURCE_FILES:
        path = root / name
        base.check(path.is_file() and not path.is_symlink(), 'Required regular source missing')
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected is not None:
        base.check(isinstance(expected, dict) and set(expected) == set(SOURCE_FILES), 'Invalid source manifest scope')
        base.check(all(isinstance(v, str) and re.fullmatch(r'[a-f0-9]{64}', v) for v in expected.values()),
                   'Invalid source digest')
        base.check(result == expected, 'Source mismatch; no database action permitted')
    return result


class OwnedStorage:
    """Bounded in-memory MinIO adapter. No network or original object reads."""
    def __init__(self):
        self.objects = {}
        self.removed = []

    def put_object(self, bucket, name, stream, *, length, content_type):
        base.check(name.startswith('after-sales/') and bucket == 'owned-qa003', 'Unexpected mock storage scope')
        base.check(0 < length <= 4096, 'Mock object outside owned size limit')
        content = stream.read(length + 1)
        base.check(len(content) == length, 'Mock storage length mismatch')
        self.objects[name] = content

    def remove_object(self, bucket, name):
        base.check(bucket == 'owned-qa003' and name.startswith('after-sales/'), 'Unsafe mock removal')
        self.objects.pop(name, None)
        self.removed.append(name)

    def get_object(self, bucket, name):
        base.check(bucket == 'owned-qa003' and name in self.objects, 'Unknown mock object')
        response = io.BytesIO(self.objects[name])
        response.release_conn = lambda: None
        return response


def seed(m):
    from sqlalchemy import text
    roles = {'buyer': '', 'provider': '', 'member': '', 'outsider': '', 'inactive': '',
             'admin': 'super_admin', 'ops': 'platform_operator', 'monitor': 'delivery_monitor',
             'finance': 'finance_settlement'}
    with m.SessionLocal.begin() as db:
        base.check(base.owned_schema(db.scalar(text('SELECT current_schema()'))), 'Seed escaped isolated schema')
        for tenant in ('buyer', 'provider', 'other'):
            db.add(m.Enterprise(id='owned-'+tenant, name='Owned '+tenant,
                credit_code='owned-'+tenant, verification_status='verified'))
        for name, role in roles.items():
            db.add(m.User(id='owned-'+name, name='Owned QA003 actor', email=name+'@example.invalid',
                password_hash='unused', platform_role=role, is_active=name != 'inactive',
                activation_status='active', verified_status='verified'))
        db.flush()
        for name, tenant, role in (('buyer', 'buyer', 'super_admin'), ('provider', 'provider', 'super_admin'),
                ('member', 'buyer', 'member'), ('outsider', 'other', 'super_admin'), ('inactive', 'buyer', 'super_admin')):
            db.add(m.Membership(user_id='owned-'+name, enterprise_id='owned-'+tenant, role=role, status='active'))
        db.add(m.Product(id='owned-product', enterprise_id='owned-provider', name='Owned QA003',
                         product_type='dataset', delivery_method='file', status='published'))
        db.add(m.ServiceLevel(id='owned-standard', code='standard', name='Owned standard', status='active'))
        db.flush()
        return {name: {'Authorization': 'Bearer '+m.issue_token(db.get(m.User, 'owned-'+name))} for name in roles}


def exercise(m, client, headers):
    from sqlalchemy import func, select, text
    workflow = importlib.import_module(m.__package__+'.after_sales_workflow')
    collaboration = m.after_sales_workflow['collaboration']
    Comment, Attachment, Link = (collaboration[k] for k in ('Comment', 'Attachment', 'EventAttachment'))
    Message, Receipt, Outbox = (m.message_center[k] for k in ('Message', 'Receipt', 'Outbox'))
    results, storage = {}, OwnedStorage()

    def count(db, Model, *where):
        return db.scalar(select(func.count()).select_from(Model).where(*where))

    def call(method, path, actor='buyer', expected=200, **kwargs):
        response = client.request(method, path, headers=headers[actor], **kwargs)
        base.check(response.status_code == expected, 'Unexpected HTTP status for owned '+method+' '+path)
        return checked_json(response,expected)

    def create(name):
        order_id, order_no = owned_order_identity(name)
        with m.SessionLocal.begin() as db:
            db.add(m.Order(id=order_id, order_no=order_no,
                buyer_user_id='owned-buyer', buyer_enterprise_id='owned-buyer',
                provider_enterprise_id='owned-provider', product_id='owned-product',
                product_name='Owned QA003', buyer_name='Owned buyer', payment_status='paid',
                main_status='completed', delivery_status='accepted', amount=10, paid_amount=10))
        return call('POST', '/api/after-sales', json={'order_id':order_id,
            'type':'owned-quality', 'description':'Owned QA003 evidence', 'idempotency_key':'owned-create-'+name})

    def action(ticket, name, actor, expected=200, **extra):
        return call('POST', '/api/after-sales/'+ticket['id']+'/actions', actor, expected,
            json={'action':name, 'reason':'Owned QA003 action', 'expected_version':ticket['version'],
                  'idempotency_key':'owned-'+ticket['id']+'-'+str(ticket['version'])+'-'+name, **extra})

    def comment(ticket, key, actor='provider', expected=200, **extra):
        return call('POST', '/api/after-sales/'+ticket['id']+'/comments', actor, expected,
            json={'content':'Owned QA003 immutable comment', 'expected_version':ticket['version'],
                  'idempotency_key':key, **extra})

    def upload(ticket, key, actor='provider', expected=200, purpose='evidence', **extra):
        return call('POST', '/api/after-sales/'+ticket['id']+'/attachments', actor, expected,
            files={'upload':('owned-evidence.txt', b'owned evidence', 'text/plain')},
            data={'expected_version':ticket['version'], 'idempotency_key':key, 'purpose':purpose, **extra})

    def frozen(ticket):
        with m.SessionLocal() as db:
            row = db.get(m.AfterSalesTicket, ticket['id'])
            return (row.status, row.version, row.first_response_at, row.closed_at, row.sla_snapshot_json,
                    count(db, m.AfterSalesEvent, m.AfterSalesEvent.ticket_id == row.id))

    def snapshot():
        models = [m.AfterSalesTicket, m.AfterSalesEvent, m.Order, m.AuditLog,
                  Message, Receipt, Outbox, m.FileObject, Comment, Attachment, Link, m.TicketSlaClock]
        with m.SessionLocal() as db:
            return {model.__tablename__: [dict(r) for r in db.execute(select(model.__table__).order_by(model.id)).mappings()]
                    for model in models}

    def case(name, fn):
        try:
            results[name] = {'status':'passed', **fn()}
        except Exception as exc:
            results[name] = base.safe_error(exc)

    def event_atomicity(ticket):
        with m.SessionLocal() as db:
            events = list(db.scalars(select(m.AfterSalesEvent).where(m.AfterSalesEvent.ticket_id == ticket['id'])
                .order_by(m.AfterSalesEvent.occurred_at, m.AfterSalesEvent.id)))
            base.check(len(events) == ticket['version'], 'State/event sequence diverged')
            for event in events:
                base.check(event.actor_id and event.actor_role, 'Missing event actor')
                base.check(count(db, m.AuditLog, m.AuditLog.target_id == ticket['id'],
                    m.AuditLog.action == 'after_sales_'+event.event_type,
                    m.AuditLog.result == 'success') >= 1, 'State event audit absent')
                message = db.scalar(select(Message).where(Message.event_key == 'after-sales:'+event.operation_key))
                base.check(message is not None and count(db, Outbox, Outbox.message_id == message.id) > 0,
                           'State event Outbox absent')
            base.check([json.loads(e.after_json)['version'] for e in events] == list(range(1,len(events)+1)),
                       'Event versions not contiguous')
            return len(events)

    def state_chain():
        ticket = create('chain')
        sequence = [('respond','provider','processing'), ('request_info','provider','awaiting_buyer_info'),
            ('supplement','buyer','processing'), ('submit_resolution','provider','awaiting_buyer_confirmation'),
            ('reject_resolution','buyer','processing'), ('submit_resolution','provider','awaiting_buyer_confirmation'),
            ('confirm','buyer','completed')]
        for name, actor, state in sequence:
            ticket = action(ticket,name,actor)
            base.check(ticket['status'] == state, 'Happy-path state mismatch')
        return {'events_with_audit_and_outbox':event_atomicity(ticket), 'final_state':ticket['status']}

    def intervention():
        ticket = create('intervention')
        ticket = action(ticket,'request_intervention','buyer')
        ticket = action(ticket,'respond_intervention','monitor')
        base.check(ticket['status'] == 'platform_intervention' and frozen(ticket)[2] is None,
                   'Platform reply incorrectly counts as provider response')
        ticket = action(ticket,'review_intervention','ops')
        base.check(ticket['status'] == 'awaiting_response', 'Intervention lost original state')
        action(ticket,'force_close','monitor',403)
        action(ticket,'force_close','provider',403)
        action(ticket,'force_close','ops',422,reason=' ')
        ticket = action(ticket,'force_close','ops')
        action(ticket,'respond','provider',409)
        return {'events_with_audit_and_outbox':event_atomicity(ticket), 'monitor_force_close':403,
                'blank_reason':422, 'terminal_action':409, 'final_state':ticket['status']}

    def comments_and_replay():
        ticket = create('comments')
        before = frozen(ticket)
        attachment = upload(ticket,'owned-comment-attachment')
        first = comment(ticket,'owned-comment-replay',attachments=[attachment['id']])
        again = comment(ticket,'owned-comment-replay',attachments=[attachment['id']])
        base.check(first['id'] == again['id'], 'Replay duplicated comment')
        comment(ticket,'owned-comment-replay',expected=409,content='changed')
        base.check(frozen(ticket) == before, 'Comment/upload mutated state, frozen SLA or first response')
        with m.SessionLocal() as db:
            base.check(count(db,Comment,Comment.ticket_id == ticket['id']) == 1, 'Unexpected comment count')
            messages = list(db.scalars(select(Message).where(Message.target_id == ticket['id'])))
            base.check(len({row.event_key for row in messages}) == len(messages), 'Duplicate notification event key')
            for message in messages:
                jobs = list(db.execute(select(Outbox.user_id,Outbox.channel).where(Outbox.message_id == message.id)))
                base.check(jobs and len(jobs) == len(set(jobs)), 'Missing or duplicate Outbox receipts')
        return {'replay_unique':True, 'changed_payload':409, 'first_response_still_null':before[2] is None,
                'ticket_and_event_chain_unchanged':True, 'storage_adapter':'in-memory mock', 'scanner':'mock clean'}

    def permissions():
        ticket = create('permissions')
        attachment = upload(ticket,'owned-permission-attachment')
        statuses = {}
        for actor, denied in [('member',404),('outsider',404),('finance',404),('inactive',401)]:
            for suffix in ('comments','attachments','attachments/'+attachment['id']+'/download'):
                call('GET','/api/after-sales/'+ticket['id']+'/'+suffix,actor,denied)
            comment(ticket,'owned-denied-'+actor,actor,denied)
            statuses[actor] = denied
        call('GET','/api/after-sales/'+ticket['id']+'/comments?enterprise_id=owned-other',expected=403)
        comment(ticket,'owned-denied-blank',expected=422,content=' ')
        return {'evidence_route_denials':statuses,'explicit_scope':403,'blank_comment':422}

    def terminal():
        ticket = create('terminal')
        evidence = upload(ticket,'owned-preclosed-evidence')
        ticket = action(ticket,'force_close','ops')
        before = frozen(ticket)
        upload(ticket,'owned-closed-evidence',expected=409)
        audit_attachment = upload(ticket,'owned-closed-audit',actor='monitor',purpose='audit_comment')
        comment(ticket,'owned-closed-invalid-reference',expected=404,attachments=[evidence['id']])
        comment(ticket,'owned-closed-audit-comment','monitor',attachments=[audit_attachment['id']])
        action(ticket,'force_close','admin',409)
        base.check(frozen(ticket) == before, 'Terminal audit evidence changed closed event chain')
        return {'normal_evidence_rejected':409,'preclosed_normal_attachment_reference_rejected':404,
                'audit_comment_attachment_allowed':True,'state_chain_unchanged':True,
                'comment_contract':'Comments remain immutable audit annotations; no separate comment purpose field'}

    def rollback(operation, failure):
        ticket = create('rollback-'+operation+'-'+failure)
        before, objects = snapshot(), dict(storage.objects)
        injected = []
        real = m.audit if failure == 'audit' else m.message_center['create']
        def fail(*args,**kwargs):
            real(*args,**kwargs)
            args[0].flush()
            injected.append(True)
            raise RuntimeError('Owned failure after real PG flush')
        replacement = patch.object(m,'audit',side_effect=fail) if failure == 'audit' else patch.dict(
            m.message_center, {'create':fail})
        with replacement:
            if operation == 'comment':
                comment(ticket,'owned-failure-'+operation+'-'+failure,expected=500)
            elif operation == 'attachment':
                upload(ticket,'owned-failure-'+operation+'-'+failure,expected=500)
            else:
                action(ticket,'respond','provider',500)
        base.check(injected, 'Failure injection did not flush real PG writes')
        base.check(snapshot() == before, 'Failure left ticket/evidence/audit/message/Outbox changes')
        base.check(storage.objects == objects, 'Failed upload left mock storage object')
        return {'real_pg_flush_before_failure':True,'database_snapshot_unchanged':True,
                'mock_storage_compensated':True,'operation':operation,'injected_at':failure}

    def race(kind):
        ticket = create('race-'+kind)
        barrier = threading.Barrier(2,timeout=5)
        def worker(index):
            with m.SessionLocal() as db:
                base.check(base.owned_schema(db.scalar(text('SELECT current_schema()'))), 'Race escaped schema')
                pid = db.scalar(text('SELECT pg_backend_pid()'))
                actor, name = [('provider','respond'),('buyer','withdraw')][index]
                user = db.get(m.User,'owned-'+actor)
                barrier.wait()
                try:
                    if kind == 'create':
                        body = workflow.TicketCreate(order_id='owned-race-target', type='owned-quality',
                            description='Owned concurrent create',idempotency_key='owned-race-create-'+str(index))
                        item = m.after_sales_workflow['create'](db,db.get(m.User,'owned-buyer'),body)
                    else:
                        item = m.after_sales_workflow['act'](db,user,ticket['id'],workflow.TicketAction(
                            action=name,reason='Owned competing action',expected_version=ticket['version'],
                            idempotency_key='owned-race-'+name))
                    db.flush(); db.commit()
                    return 200,pid,item.id
                except m.HTTPException as exc:
                    db.rollback()
                    return exc.status_code,pid,None
        if kind == 'create':
            with m.SessionLocal.begin() as db:
                db.add(m.Order(id='owned-race-target',order_no='OWNED-QA003-RACE-TARGET',
                    buyer_user_id='owned-buyer',buyer_enterprise_id='owned-buyer',provider_enterprise_id='owned-provider',
                    product_id='owned-product',product_name='Owned QA003',buyer_name='Owned buyer',
                    payment_status='paid',main_status='completed',delivery_status='accepted',amount=10,paid_amount=10))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker,index) for index in range(2)]
            outcomes = [future.result(timeout=20) for future in futures]
        base.check(sorted(row[0] for row in outcomes) == [200,409], 'Concurrent operation did not yield 200/409')
        base.check(len({row[1] for row in outcomes}) == 2,'Race reused a PG connection')
        with m.SessionLocal() as db:
            if kind == 'create':
                rows=list(db.scalars(select(m.AfterSalesTicket).where(m.AfterSalesTicket.order_id == 'owned-race-target')))
                base.check(len(rows)==1,'Create race duplicated ticket')
                target=rows[0]
            else:
                target=db.get(m.AfterSalesTicket,ticket['id'])
            base.check(target.version == (1 if kind == 'create' else 2),'Race changed version more than once')
            base.check(count(db,m.AfterSalesEvent,m.AfterSalesEvent.ticket_id==target.id)==target.version,
                       'Race left extra events')
        return {'statuses':[200,409],'independent_pg_connections':2,'unique_state_commit':True}

    with ExitStack() as mocks:
        for name,value in {'MINIO_ENDPOINT':'owned-storage.invalid','MINIO_BUCKET':'owned-qa003',
                           'MINIO_ACCESS_KEY':'owned-unused','MINIO_SECRET_KEY':'owned-unused'}.items():
            mocks.enter_context(patch.object(m,name,value))
        mocks.enter_context(patch.object(m,'Minio',return_value=storage))
        mocks.enter_context(patch.object(m,'clamav_scan_stream',return_value=('clean','owned synthetic scan')))
        case('QA003_state_sequence_event_audit_outbox',state_chain)
        case('QA003_intervention_permissions_force_close',intervention)
        case('QA003_comment_attachment_replay_preserves_first_response',comments_and_replay)
        case('QA003_evidence_permissions_and_scope',permissions)
        case('QA003_terminal_audit_comment_restrictions',terminal)
        for operation in ('action','comment','attachment'):
            for failure in ('audit','outbox'):
                case('QA003_'+operation+'_'+failure+'_rollback',lambda op=operation,f=failure:rollback(op,f))
        for kind in ('create','version'):
            case('QA003_concurrent_'+kind+'_409',lambda k=kind:race(k))
    return results


def run_isolated(source_root, source_kind, expected=None):
    logging.disable(logging.CRITICAL)
    fingerprint = source_fingerprint(source_root,expected)
    base.check('app.main' not in sys.modules,'Fresh process required')
    with patch.object(base,'seed',seed),patch.object(base,'exercise',exercise):
        result = base.run()
    result.update(coverage=['TRD-R3-QA-003'],source_kind=source_kind,
                  deployed=source_kind=='installed',source_sha256=fingerprint,
                  external_adapters={'MinIO':'bounded in-memory mock','ClamAV':'mock clean scan'},
                  scope='Actual PostgreSQL owned schema and ASGI handlers; no ingress or real external services')
    if result.get('cases'):
        result['status']='passed' if all(v['status']=='passed' for v in result['cases'].values()) else 'failed'
    result['gaps']=['No real MinIO/ClamAV, browser, ingress, notification delivery or deployed workingtree claim']
    return result


def remote_run(encoded,manifest):
    with tempfile.TemporaryDirectory(prefix='market-qa003-source-') as directory:
        root=Path(directory)
        transport.unpack_workingtree(encoded,manifest,root/'app')
        base.check(not any(k=='app' or k.startswith('app.') for k in sys.modules),'Application already imported')
        sys.path.insert(0,str(root))
        try:
            result=run_isolated(root/'app','workingtree')
            result['source_package_sha256']=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
            return result
        finally:
            sys.path.remove(str(root))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--pod')
    mode.add_argument('--installed',action='store_true')
    parser.add_argument('--expected-source-manifest')
    args=parser.parse_args()
    if args.installed:
        base.check(bool(args.expected_source_manifest),'Installed source manifest required before DB action')
        spec=importlib.util.find_spec('app')
        base.check(spec is not None and spec.origin is not None,'Installed package missing')
        result=run_isolated(Path(spec.origin).resolve().parent,'installed',
            json.loads(Path(args.expected_source_manifest).read_text()))
    else:
        base.check(bool(re.fullmatch(r'market-api-[a-z0-9-]+',args.pod)),'Invalid API pod')
        base.check(bool(os.getenv('SSHPASS')),'Private SSHPASS environment required')
        root=Path(__file__).resolve().parents[1]
        encoded,manifest=transport.pack_workingtree(root)
        payload='import sys,types\n'
        for name in ('r3_backend_acceptance_pg','r3_backend_acceptance_concurrency'):
            source=Path(__file__).with_name(name+'.py').read_text()
            payload+='m=types.ModuleType('+repr(name)+');sys.modules[m.__name__]=m;exec(compile('+repr(source)+',m.__name__,"exec"),m.__dict__)\n'
        payload+="__name__='qa003_remote'\n"+Path(__file__).read_text()
        payload+='\ntry:\n print(json.dumps(remote_run('+repr(encoded)+','+repr(manifest)+')))\nexcept Exception as exc:\n print(json.dumps({"status":"failed","error":base.safe_error(exc)}))\n'
        command='kubectl --request-timeout=5s -n market exec -i '+args.pod+' -- env MESSAGE_CENTER_DISABLE_BACKGROUND=true PYTHONDONTWRITEBYTECODE=1 python -B -'
        process=subprocess.run(['sshpass','-e','ssh','-o','ConnectTimeout=5','-o','ConnectionAttempts=1',
            '-o','LogLevel=ERROR','root@192.168.10.10',command],input=payload,capture_output=True,text=True,timeout=120)
        base.check(process.returncode==0,'Bounded remote run failed; private diagnostics suppressed')
        result=json.loads(process.stdout)
    print(json.dumps(result,ensure_ascii=True))
    return 0 if result.get('status')=='passed' else 1


if __name__=='__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'status':'failed','error':base.safe_error(exc)}))
        sys.exit(1)
