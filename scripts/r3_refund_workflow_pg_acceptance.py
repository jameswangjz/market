"""BE010 only: owned-schema PG concurrency/rollback, no startup/public writes.

SSHPASS privately supplied; python -B script.py --pod market-api-...
Reuses the existing PG harness and integrity-checked workingtree transport.
Approval cases use an explicit synthetic trusted MANUAL planner, never invented
gateway usage. No BE011 price correctness, BE012 executor, bank or deployment claim.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from decimal import Decimal
import argparse
import hashlib
import importlib
import json
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
import r3_after_sales_group_pg_acceptance as fixture


SOURCE_FILES=('main.py','refund_workflow.py','refund_schema.py')
CASES=('budget','same-key','review','release','request-audit','request-outbox','review-audit','review-outbox')


def seed(m):
    headers=fixture.seed(m)
    with m.SessionLocal.begin() as db:
        for name in CASES:
            oid,number=fixture.owned_order_identity('refund-'+name)
            db.add(m.Order(id=oid,order_no=number,buyer_user_id='owned-buyer',buyer_enterprise_id='owned-buyer',
                provider_enterprise_id='owned-provider',product_id='owned-product',product_name='Owned refund',
                buyer_name='Owned buyer',amount=100,paid_amount=100,payment_status='paid',
                main_status='completed',delivery_status='accepted',snapshot_version=0,refunded_amount=0))
            db.flush()
            db.add(m.Payment(id='owned-pay-'+name,order_id=oid,payment_no='OWNED-PAY-'+name,
                status='paid',amount=100,paid_at=m.now()))
    return headers


def exercise(m,client,headers):
    from sqlalchemy import func,select,text
    from starlette.requests import Request
    schema=importlib.import_module(m.__package__+'.refund_schema')
    hooks=m.refund_workflow
    Case,Event=hooks['Case'],hooks['Event']
    Message,Receipt,Outbox=(m.message_center[k] for k in ('Message','Receipt','Outbox'))
    results={}
    def order_id(name):
        return fixture.owned_order_identity('refund-'+name)[0]
    def request_body(amount,key):
        return schema.RefundRequestBody(amount=Decimal(amount),reason='Owned PG refund request',idempotency_key=key)
    def create(db,user,name,amount,key):
        request=Request({'type':'http','method':'POST','path':'/api/orders/'+order_id(name)+'/refund-requests',
            'query_string':b'enterprise_id=owned-buyer&business_view=buyer','headers':[]})
        return hooks['create'](db,user,order_id(name),request_body(amount,key),request)
    def review(db,user,rid,decision,key):
        return hooks['review'](db,user,rid,schema.RefundReviewBody(decision=decision,
            reason='Owned PG review',expected_version=1,idempotency_key=key))
    def transaction(actor,operation,barrier=None):
        with m.SessionLocal() as db:
            base.check(base.owned_schema(db.scalar(text('SELECT current_schema()'))),'Escaped owned schema')
            pid=db.scalar(text('SELECT pg_backend_pid()'))
            user=db.get(m.User,'owned-'+actor)
            if barrier:
                barrier.wait()
            try:
                refund=operation(db,user)
                db.flush()
                rid=refund.id
                db.commit()
                return {'status':200,'pid':pid,'id':rid}
            except m.HTTPException as exc:
                db.rollback()
                return {'status':exc.status_code,'pid':pid,'id':None}
    def pair(operations):
        barrier=threading.Barrier(2,timeout=5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(transaction,actor,fn,barrier) for actor,fn in operations]
            rows=[f.result(timeout=20) for f in futures]
        base.check(len({row['pid'] for row in rows})==2,'Race must use two PG connections')
        return rows
    def stats(name):
        with m.SessionLocal() as db:
            rows=list(db.scalars(select(m.Refund).where(m.Refund.order_id==order_id(name))))
            ids=[row.id for row in rows]
            reserved=sum((Decimal(row.amount) for row in rows if row.status in ('pending','requested','approved','executing','failed')),Decimal(0))
            events=db.scalar(select(func.count()).select_from(Event).where(Event.refund_id.in_(ids)))
            return len(rows),reserved,events
    def snapshot():
        models=[m.Order,m.Payment,m.Refund,Case,Event,m.AuditLog,Message,Receipt,Outbox,m.Settlement]
        with m.SessionLocal() as db:
            return {model.__tablename__:[tuple(row) for row in db.execute(select(model.__table__).order_by(
                *model.__table__.primary_key.columns))] for model in models}
    def manual_plan(db,order,refund):
        return schema.PreparedRefundPlan(refund_id=refund.id,order_id=order.id,payment_id=refund.payment_id,
            amount=Decimal(refund.amount),calculation_version='owned-pg-manual-adapter',as_of=m.now(),
            evidence_ids=('owned-synthetic-manual-proof',),basis='manual',
            economic_plan={'fixture_only':True,'source_order_id':order.id,'amount':str(refund.amount)})
    def budget_race():
        rows=pair([('buyer',lambda db,u,i=i:create(db,u,'budget','60.00','owned-budget-'+str(i))) for i in range(2)])
        base.check(sorted(r['status'] for r in rows)==[200,409],'Budget oversubscription not rejected')
        base.check(stats('budget')==(1,Decimal('60'),1),'Budget reservation duplicated')
        return {'http_equivalent_statuses':[200,409],'independent_pg_connections':2,'reserved':'60.00','refunds':1,'events':1}
    def same_key():
        rows=pair([('buyer',lambda db,u:create(db,u,'same-key','70.00','owned-same-create-key'))]*2)
        base.check([r['status'] for r in rows]==[200,200] and rows[0]['id']==rows[1]['id'],'Concurrent same-key replay is not idempotent')
        base.check(stats('same-key')==(1,Decimal('70'),1),'Same key duplicated budget or event')
        before=snapshot()
        changed=transaction('buyer',lambda db,u:create(db,u,'same-key','71.00','owned-same-create-key'))
        base.check(changed['status']==409 and snapshot()==before,'Changed key payload left changes')
        return {'http_equivalent_statuses':[200,200],'same_refund':True,'reserved':'70.00','changed_payload':409}
    def version_race():
        row=transaction('buyer',lambda db,u:create(db,u,'review','20.00','owned-review-create'))
        base.check(row['status']==200,'Review fixture failed')
        with patch.dict(vars(m),prepare_refund_plan=manual_plan):
            rows=pair([(actor,lambda db,u,d=decision:review(db,u,row['id'],d,'owned-race-'+d))
                       for actor,decision in [('admin','approve'),('ops','reject')]])
        base.check(sorted(r['status'] for r in rows)==[200,409],'Same-version review did not conflict')
        with m.SessionLocal() as db:
            case=db.get(Case,row['id']);refund=db.get(m.Refund,row['id'])
            events=db.scalar(select(func.count()).select_from(Event).where(Event.refund_id==row['id']))
            base.check(case.version==2 and events==2,'Review version/event duplicated')
            if refund.status=='approved':
                base.check(bool(case.plan_hash) and case.plan_hash==hashlib.sha256(case.plan_json.encode()).hexdigest(),
                           'Approved plan not frozen with hash')
            else:
                base.check(refund.status=='rejected' and case.plan_hash is None,'Rejected review froze a plan')
        return {'http_equivalent_statuses':[200,409],'version':2,'events':2,
                'planner':'synthetic trusted manual adapter; not real usage/economic verification'}
    def release():
        row=transaction('buyer',lambda db,u:create(db,u,'release','80.00','owned-release-first'))
        base.check(row['status']==200,'Release fixture failed')
        denied=transaction('buyer',lambda db,u:create(db,u,'release','21.00','owned-release-overflow'))
        base.check(denied['status']==409,'Requested budget not held')
        rejected=transaction('ops',lambda db,u:review(db,u,row['id'],'reject','owned-release-review'))
        base.check(rejected['status']==200,'Rejection failed')
        replay=transaction('ops',lambda db,u:review(db,u,row['id'],'reject','owned-release-review'))
        base.check(replay['status']==200 and stats('release')==(1,Decimal('0'),2),'Review replay duplicated occupancy/event')
        full=transaction('buyer',lambda db,u:create(db,u,'release','100.00','owned-release-full'))
        base.check(full['status']==200 and stats('release')==(2,Decimal('100'),3),'Rejected reservation was not released exactly')
        return {'overflow':409,'rejection_releases_budget':True,'review_replay_unique':True,'new_reserved':'100.00'}
    def rollback(operation,failure):
        name=operation+'-'+failure
        rid=None
        if operation=='review':
            row=transaction('buyer',lambda db,u:create(db,u,name,'20.00','owned-create-'+name))
            base.check(row['status']==200,'Rollback review fixture failed')
            rid=row['id']
        before=snapshot();injected=[]
        real=m.audit if failure=='audit' else m.message_center['create']
        def fault(*args,**kwargs):
            real(*args,**kwargs)
            args[0].flush()
            if failure=='outbox':
                base.check(args[0].scalar(select(func.count()).select_from(Outbox))>len(before[Outbox.__tablename__]),
                           'New real Outbox rows not flushed')
            injected.append(True)
            raise RuntimeError('Owned failure after real PG writes')
        injection=patch.object(m,'audit',side_effect=fault) if failure=='audit' else patch.dict(m.message_center,create=fault)
        failed=False
        with injection,patch.dict(vars(m),prepare_refund_plan=manual_plan):
            try:
                transaction('buyer' if operation=='request' else 'ops',lambda db,u:
                    create(db,u,name,'20.00','owned-fault-'+name) if operation=='request'
                    else review(db,u,rid,'approve','owned-fault-'+name))
            except RuntimeError:
                failed=True
        base.check(failed and injected and snapshot()==before,'Refund/case/event/budget/audit/message rollback failed')
        return {'real_flush_before_failure':True,'exact_owned_snapshot_unchanged':True,'operation':operation,'fault':failure}
    for name,fn in [('budget_two_connections',budget_race),('same_idempotency_key_two_connections',same_key),
                    ('same_review_version_two_connections',version_race),('reservation_release_and_replay',release),
                    *[(op+'_'+kind+'_rollback',lambda o=op,k=kind:rollback(o,k))
                      for op in ('request','review') for kind in ('audit','outbox')]]:
        try:
            results[name]={'status':'passed',**fn()}
        except Exception as exc:
            results[name]=base.safe_error(exc)
    return results


def remote_run(encoded,manifest):
    with tempfile.TemporaryDirectory(prefix='qa-r3-refund-source-') as directory:
        root=Path(directory)
        transport.unpack_workingtree(encoded,manifest,root/'app')
        base.check(not any(n=='app' or n.startswith('app.') for n in sys.modules),'Fresh application process required')
        sys.path.insert(0,str(root))
        try:
            with patch.object(base,'seed',seed),patch.object(base,'exercise',exercise):
                result=base.run()
        finally:
            sys.path.remove(str(root))
    result.update(coverage=['TRD-R3-BE-010'],source_kind='workingtree',deployed=False,
        source_sha256={name:manifest[name] for name in SOURCE_FILES},
        scope='BE010 real PG owned-schema hooks; concurrency statuses are HTTP-equivalent, not ingress',
        gaps=['No BE011 gateway-charge computation or historical manual economic verification',
              'Approval uses explicit synthetic manual trusted planner','No BE012 executor, bank, UI or deployment claim'])
    if result.get('cases'):
        result['status']='passed' if all(c['status']=='passed' for c in result['cases'].values()) else 'failed'
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--pod',required=True)
    args=parser.parse_args()
    base.check(bool(re.fullmatch(r'market-api-[a-z0-9-]+',args.pod)),'Invalid API pod')
    base.check(bool(os.getenv('SSHPASS')),'Private SSHPASS environment required')
    encoded,manifest=transport.pack_workingtree(Path(__file__).resolve().parents[1])
    payload='import sys,types\n'
    for name in ('r3_backend_acceptance_pg','r3_backend_acceptance_concurrency','r3_after_sales_group_pg_acceptance'):
        code=Path(__file__).with_name(name+'.py').read_text()
        payload+='m=types.ModuleType('+repr(name)+');sys.modules[m.__name__]=m;exec(compile('+repr(code)+',m.__name__,"exec"),m.__dict__)\n'
    payload+="__name__='refund_pg_payload'\n"+Path(__file__).read_text()
    payload+='\ntry:\n print(json.dumps(remote_run('+repr(encoded)+','+repr(manifest)+')))\nexcept Exception as exc:\n print(json.dumps({"status":"failed","error":base.safe_error(exc)}))\n'
    command='kubectl --request-timeout=5s -n market exec -i '+args.pod+' -- env MESSAGE_CENTER_DISABLE_BACKGROUND=true PYTHONDONTWRITEBYTECODE=1 python -B -'
    completed=subprocess.run(['sshpass','-e','ssh','-o','ConnectTimeout=5','-o','ConnectionAttempts=1',
        '-o','LogLevel=ERROR','root@192.168.10.10',command],input=payload,capture_output=True,text=True,timeout=120)
    base.check(completed.returncode==0,'Bounded PG transport failed; diagnostics suppressed')
    result=json.loads(completed.stdout);print(json.dumps(result))
    return 0 if result.get('status')=='passed' else 1


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status':'failed','error':base.safe_error(exc)}));raise SystemExit(1)
