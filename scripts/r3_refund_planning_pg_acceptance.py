"""BE011 incremental owned-schema PG package. Run only after source freeze.

SSHPASS privately supplied; python -B script.py --pod market-api-...
Reuses BE010 transport/schema safety, but replaces its seed/exercise: none of
the passed eight BE010 cases are repeated. Synthetic original SaaS time segment
and explicit human determinations only; no fabricated actual gateway charges,
resource/funds execution, startup or public writes.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import argparse
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import patch

import r3_refund_workflow_pg_acceptance as prior


AT=datetime(2026,10,15,tzinfo=timezone.utc)
CASES=('manual','limit','audit','outbox','version','cumulative')
SOURCE_FILES=(*prior.SOURCE_FILES,'refund_planning.py','refund_entitlements.py')


def oid(name):
    return prior.fixture.owned_order_identity('planning-'+name)[0]


def seed(m):
    headers=prior.fixture.seed(m)
    with m.SessionLocal.begin() as db:
        db.add(m.Product(id='owned-planning-saas',enterprise_id='owned-provider',name='Owned planning SaaS',
            product_type='saas',delivery_method='tenant_access',status='draft',price=100))
        db.flush()
        db.add(m.SaaSProductVersion(id='owned-planning-version',product_id='owned-planning-saas',
            name='Owned original',version_code='v1',monthly_price=100,cost=20,status='active'))
        db.flush()
        for name in CASES:
            _,number=prior.fixture.owned_order_identity('planning-'+name)
            db.add(m.Order(id=oid(name),order_no=number,buyer_user_id='owned-buyer',buyer_enterprise_id='owned-buyer',
                provider_enterprise_id='owned-provider',product_id='owned-product',product_name='Owned planning',
                buyer_name='Owned buyer',amount=100,paid_amount=100,payment_status='paid',
                main_status='completed',delivery_status='accepted',snapshot_version=0,refunded_amount=0))
            db.flush()
            db.add(m.Payment(id='owned-plan-pay-'+name,order_id=oid(name),payment_no='OWNED-PLAN-PAY-'+name,
                status='paid',amount=100,paid_at=AT-timedelta(days=15)))
        order=db.get(m.Order,oid('cumulative'))
        order.product_id='owned-planning-saas'
        order.product_version_id='owned-planning-version';order.product_version_code='v1'
        order.snapshot_version=1;order.subscription_months=1;order.delivery_method_snapshot='tenant_access'
        order.unit_price_snapshot=100;order.unit_cost_snapshot=20;order.total_cost_snapshot=20
        order.economic_snapshot_json=json.dumps(dict(schema_version=1,product_id=order.product_id,
            product_version_id=order.product_version_id,version_code='v1',billing_unit='month',
            subscription_months=1,unit_price='100.00',unit_cost='20.00',amount='100.00',total_cost='20.00',
            settlement_rule={'version':'owned-original','rates':{k:0 for k in m.order_economics['rate_keys']}}))
        order.delivery_snapshot_json=json.dumps({'method':'tenant_access','download_limit':0,
            'quota':{'rate_limit_per_minute':0,'daily_quota':0,'monthly_quota':0,'quota_amount':0}})
        Combo,Term=(m.subscription_terms[k] for k in ('Combination','Term'))
        db.add(Combo(id='owned-planning-combo',enterprise_id='owned-buyer',product_id=order.product_id,
            delivery_method='tenant_access',version_id=order.product_version_id,version_code='v1',
            anchor_at=AT-timedelta(days=14),generation=1,total_months=1))
        db.flush()
        db.add(Term(id='owned-planning-segment',order_id=order.id,combination_id='owned-planning-combo',
            generation=1,version_id=order.product_version_id,version_code='v1',start_month=0,months=1,
            starts_at=AT-timedelta(days=14),ends_at=AT+timedelta(days=14),status='active'))
    return headers


def exercise(m,client,headers):
    from sqlalchemy import func,select
    from starlette.requests import Request
    schema=importlib.import_module(m.__package__+'.refund_schema')
    Case,Event=(m.refund_workflow[k] for k in ('Case','Event'))
    Manual=m.refund_planning['ManualAssessment']
    Message,Receipt,Outbox=(m.message_center[k] for k in ('Message','Receipt','Outbox'))
    results={}
    def call(method,path,actor='ops',expected=200,**kwargs):
        response=client.request(method,path,headers=headers[actor],**kwargs)
        prior.base.check(response.status_code==expected,'Unexpected owned BE011 HTTP status')
        return response.json() if expected==200 else {}
    def create(name,amount,key):
        with m.SessionLocal.begin() as db:
            request=Request({'type':'http','method':'POST','path':'/api/orders/'+oid(name)+'/refund-requests',
                'query_string':b'enterprise_id=owned-buyer&business_view=buyer','headers':[]})
            row=m.refund_workflow['create'](db,db.get(m.User,'owned-buyer'),oid(name),
                schema.RefundRequestBody(amount=Decimal(amount),reason='Owned planning request',idempotency_key=key),request)
            db.flush()
            return {'id':row.id,'amount':format(Decimal(row.amount),'.2f'),'version':1}
    def body(row,key,**extra):
        return {'amount':row['amount'],'refundable_limit':'70.00','used_calls':30,
            'reason':'Owned explicit human documentary determination','evidence_refs':['owned-human-statement'],
            'expected_version':row['version'],'idempotency_key':key,**extra}
    def manual(row,payload,actor='ops',expected=200):
        return call('POST','/api/refunds/'+row['id']+'/manual-assessments',actor,expected,json=payload)
    def approve(row,key,expected=200):
        return call('POST','/api/refunds/'+row['id']+'/review',expected=expected,json={
            'decision':'approve','reason':'Owned approved frozen plan','expected_version':row['version'],'idempotency_key':key})
    def snapshot():
        models=[m.Order,m.Payment,m.Refund,Case,Event,Manual,m.AuditLog,Message,Receipt,Outbox,m.Settlement]
        with m.SessionLocal() as db:
            return {model.__tablename__:[tuple(r) for r in db.execute(select(model.__table__).order_by(
                *model.__table__.primary_key.columns))] for model in models}
    def permissions_and_replay():
        row=create('manual','20.00','owned-manual-request')
        payload=body(row,'owned-manual-assess')
        for actor in ('buyer','provider','member','outsider','finance','monitor'):
            manual(row,payload,actor,403)
        assessed=manual(row,payload)
        prior.base.check(assessed['version']==2,'Manual determination did not increment version')
        prior.base.check(manual(row,payload)['version']==2,'Manual replay duplicated version')
        manual(row,{**payload,'used_calls':31},expected=409)
        approve(row,'owned-manual-stale-approve',409)
        approved=approve(assessed,'owned-manual-approve')
        prior.base.check(approved['status']=='approved' and approved['execution_available'] is False,
                         'Manual approval executed resources/funds')
        with m.SessionLocal() as db:
            rows=list(db.scalars(select(Manual).where(Manual.refund_id==row['id'])))
            prior.base.check(len(rows)==1 and rows[0].case_version==2 and rows[0].assessed_by=='owned-ops',
                             'Manual identity/version association incorrect')
            case=db.get(Case,row['id']);plan=json.loads(case.plan_json)['economic_plan']
            prior.base.check(plan['source_trust']=='manual_determination' and plan['automatic_usage_trusted'] is False
                and plan['resource_recovery_status']=='unavailable' and plan['cost_basis_status']=='unavailable',
                'Manual decision falsely represented automatic usage/recovery/cost evidence')
            audits=db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.target_id==row['id'],
                m.AuditLog.action=='manual_refund_determination'))
            prior.base.check(audits==1,'Manual replay duplicated determination audit')
        return {'unauthorized_roles':403,'manual_version':2,'replay_unique':True,'stale_approve':409,
                'source_trust':'manual_determination','automatic_usage_trusted':False,'determination_audits':1}
    def limit_rechecked():
        row=create('limit','20.00','owned-limit-request')
        assessed=manual(row,body(row,'owned-limit-manual',refundable_limit='20.00'))
        create('limit','1.00','owned-limit-new-reservation')
        approve(assessed,'owned-limit-approval',409)
        with m.SessionLocal() as db:
            case=db.get(Case,row['id'])
            prior.base.check(case.version==2 and case.plan_hash is None,'Failed approval froze a plan')
        return {'new_reservation_rechecked_at_approval':409,'no_plan_frozen_on_failure':True}
    def version_race():
        row=create('version','20.00','owned-version-request')
        barrier=__import__('threading').Barrier(2,timeout=5)
        def worker(actor):
            payload=body(row,'owned-concurrent-manual-'+actor)
            barrier.wait()
            return client.post('/api/refunds/'+row['id']+'/manual-assessments',json=payload,headers=headers[actor]).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(worker,actor) for actor in ('admin','ops')]
            statuses=[future.result(timeout=20) for future in futures]
        prior.base.check(sorted(statuses)==[200,409],'Concurrent manual versions did not conflict')
        with m.SessionLocal() as db:
            count=db.scalar(select(func.count()).select_from(Manual).where(Manual.refund_id==row['id']))
            prior.base.check(count==1 and db.get(Case,row['id']).version==2,'Manual race duplicated assessment/version')
        return {'ASGI_statuses':[200,409],'manual_rows':1,'case_version':2}
    def rollback(kind):
        row=create(kind,'20.00','owned-rollback-request-'+kind)
        before=snapshot();injected=[]
        real=m.audit if kind=='audit' else m.message_center['create']
        def fault(*args,**kwargs):
            value=real(*args,**kwargs)
            # Target the explicit manual audit AFTER workflow event + Outbox,
            # rather than failing before the audited determination exists.
            target=kind!='audit' or len(args)>2 and args[2]=='manual_refund_determination'
            if target:
                args[0].flush()
                if kind=='outbox':
                    prior.base.check(args[0].scalar(select(func.count()).select_from(Outbox))>
                        len(before[Outbox.__tablename__]),'New Outbox not flushed')
                injected.append(True)
                raise RuntimeError('Owned BE011 fault after actual PG flush')
            return value
        injection=patch.object(m,'audit',side_effect=fault) if kind=='audit' else patch.dict(m.message_center,create=fault)
        with injection:
            manual(row,body(row,'owned-rollback-'+kind),expected=500)
        prior.base.check(injected and snapshot()==before,'Manual/case/event/audit/message/Outbox failed atomic rollback')
        return {'actual_flush_before_failure':True,'exact_owned_snapshot_unchanged':True,'fault':kind}
    def cumulative():
        first=approve(create('cumulative','20.00','owned-cumulative-first'),'owned-cumulative-approve-first')
        with m.SessionLocal() as db:
            original=db.get(Case,first['id']).plan_json
        second=approve(create('cumulative','30.00','owned-cumulative-second'),'owned-cumulative-approve-second')
        with m.SessionLocal.begin() as db:
            one=json.loads(db.get(Case,first['id']).plan_json)['economic_plan']
            two=json.loads(db.get(Case,second['id']).plan_json)['economic_plan']
            duration=28*86400*1000000
            prior.base.check(one['basis']==two['basis']=='time' and one['recovery_delta']==duration//5
                and two['recovery_delta']==duration*3//10 and two['cumulative_recovery']==duration//2,
                'Cumulative recovery not based on original denominator')
            prior.base.check(datetime.fromisoformat(two['effective_ends_at'])==AT,'Final refundable tail not recovered')
            db.get(m.Product,'owned-planning-saas').price=999
            db.get(m.SaaSProductVersion,'owned-planning-version').monthly_price=999
        quote=call('POST','/api/orders/'+oid('cumulative')+'/refund-quote?enterprise_id=owned-buyer&business_view=buyer',
                   actor='buyer',json={'amount':'0.01'})
        prior.base.check(quote['status']=='unavailable','Cumulative reserved refund exceeded original remaining balance')
        with m.SessionLocal() as db:
            prior.base.check(db.get(Case,first['id']).plan_json==original,'Later plan/catalog update rewrote frozen plan')
            prior.base.check(db.get(m.Order,oid('cumulative')).payment_status=='paid' and
                db.scalar(select(func.count()).select_from(m.Settlement))==0,'Planning executed economic settlement')
        return {'basis':'original SaaS time segment','original_duration_microseconds':duration,
                'cumulative_recovery':duration//2,'frozen_first_plan_unchanged':True,
                'catalog_change_not_retroactive':True,'gateway_usage_adapter_used':False,'executor_used':False}
    with patch.object(m,'now',return_value=AT):
        for name,fn in [('manual_permissions_version_replay',permissions_and_replay),
            ('manual_approval_rechecks_reservations',limit_rechecked),('manual_same_version_race',version_race),
            ('manual_determination_audit_rollback',lambda:rollback('audit')),
            ('manual_determination_outbox_rollback',lambda:rollback('outbox')),
            ('cumulative_original_time_plan_and_immutability',cumulative)]:
            try:
                results[name]={'status':'passed',**fn()}
            except Exception as exc:
                results[name]=prior.base.safe_error(exc)
    return results


def remote_run(encoded,manifest):
    with patch.object(prior,'seed',seed),patch.object(prior,'exercise',exercise):
        result=prior.remote_run(encoded,manifest)
    result.update(coverage=['TRD-R3-BE-011'],source_sha256={name:manifest[name] for name in SOURCE_FILES},
        scope='BE011 incremental owned-schema PG/ASGI; no repeated BE010 eight-case group',
        gaps=['Synthetic original SaaS time segment and explicit human documentary determination',
              'Actual gateway charge-ledger integration is not exercised; missing usage never invented',
              'No resource/funds/real-bank execution, deployment or browser claim'])
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--pod',required=True)
    args=parser.parse_args()
    prior.base.check(bool(re.fullmatch(r'market-api-[a-z0-9-]+',args.pod)),'Invalid API pod')
    prior.base.check(bool(os.getenv('SSHPASS')),'Private SSHPASS environment required')
    encoded,manifest=prior.transport.pack_workingtree(Path(__file__).resolve().parents[1])
    payload='import sys,types\n'
    for name in ('r3_backend_acceptance_pg','r3_backend_acceptance_concurrency',
                 'r3_after_sales_group_pg_acceptance','r3_refund_workflow_pg_acceptance'):
        code=Path(__file__).with_name(name+'.py').read_text()
        payload+='m=types.ModuleType('+repr(name)+');sys.modules[m.__name__]=m;exec(compile('+repr(code)+',m.__name__,"exec"),m.__dict__)\n'
    payload+="__name__='be011_pg_payload'\n"+Path(__file__).read_text()
    payload+='\ntry:\n print(json.dumps(remote_run('+repr(encoded)+','+repr(manifest)+')))\nexcept Exception as exc:\n print(json.dumps({"status":"failed","error":prior.base.safe_error(exc)}))\n'
    command='kubectl --request-timeout=5s -n market exec -i '+args.pod+' -- env MESSAGE_CENTER_DISABLE_BACKGROUND=true PYTHONDONTWRITEBYTECODE=1 python -B -'
    completed=subprocess.run(['sshpass','-e','ssh','-o','ConnectTimeout=5','-o','ConnectionAttempts=1',
        '-o','LogLevel=ERROR','root@192.168.10.10',command],input=payload,capture_output=True,text=True,timeout=120)
    prior.base.check(completed.returncode==0,'Bounded BE011 PG transport failed; diagnostics suppressed')
    result=json.loads(completed.stdout);print(json.dumps(result))
    return 0 if result.get('status')=='passed' else 1


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status':'failed','error':prior.base.safe_error(exc)}));raise SystemExit(1)
