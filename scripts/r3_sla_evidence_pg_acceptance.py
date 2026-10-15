"""Focused QA004 installed-app PG probe; never startup/seed the business schema.

Inside the target API container, with the three existing helpers alongside:
 python -B r3_sla_evidence_pg_acceptance.py --expected-source-manifest FILE
Helpers: r3_backend_acceptance_pg, r3_backend_acceptance_concurrency,
r3_after_sales_group_pg_acceptance. The expected manifest must contain exactly
SOURCE_FILES. New module must be installed first. Measurements are synthetic;
database transactions and read-only checks use actual isolated PostgreSQL.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import re
from unittest.mock import patch

import r3_backend_acceptance_pg as base
import r3_after_sales_group_pg_acceptance as fixture


SOURCE_FILES=('main.py','sla_evidence.py','sla_online_assessments.py',
              'sla_online_sources.py','ticket_sla_clocks.py', 'sla_gateway_events.py',
              'sla_online_facts.py', 'after_sales_workflow.py', 'enterprise_sla.py')


def verify_sources(root,expected):
    base.check(isinstance(expected,dict) and set(expected)==set(SOURCE_FILES),'Invalid source manifest scope')
    actual={}
    for name in SOURCE_FILES:
        path=root/name
        base.check(path.is_file() and not path.is_symlink(),'Required regular installed source missing')
        base.check(isinstance(expected[name],str) and re.fullmatch(r'[a-f0-9]{64}',expected[name]),'Invalid source digest')
        actual[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    base.check(actual==expected,'Source mismatch; no database action performed')
    return actual


def seed(m):
    headers=fixture.seed(m)
    at=datetime(2026,10,15,tzinfo=timezone.utc)
    with m.SessionLocal.begin() as db:
        db.add(m.Order(id='owned-evidence-order',order_no='OWNED-QA004',
            product_id='owned-product',product_name='Owned evidence',buyer_name='Owned buyer',
            buyer_user_id='owned-buyer',buyer_enterprise_id='owned-buyer',provider_enterprise_id='owned-provider',
            payment_status='paid',paid_amount=10,amount=10,delivery_status='accepted',main_status='completed'))
        db.flush()
        db.add(m.AfterSalesTicket(id='owned-evidence-ticket',ticket_no='OWNED-QA004-TICKET',
            order_id='owned-evidence-order',type='owned-quality',description='Owned historical fixture',
            status='processing',version=1,evidence_status='historical_unknown',sla_snapshot_json='{}',created_at=at))
        Assessment=m.sla_online_assessments['Assessment']
        db.add(Assessment(id='owned-evidence-result',order_id='owned-evidence-order',contract_id=None,
            snapshot_json=None,period_start=at,period_end=datetime(2026,11,1,tzinfo=timezone.utc),
            calculated_at=at,version=1,input_hash='0'*64,metrics_json='{}',source_json='{}',status='insufficient_data'))
        db.add(m.Order(id='owned-live-evidence-order',order_no='OWNED-QA004-LIVE',
            product_id='owned-product',product_name='Owned evidence',buyer_name='Owned buyer',
            buyer_user_id='owned-buyer',buyer_enterprise_id='owned-buyer',provider_enterprise_id='owned-provider',
            payment_status='paid',paid_amount=10,amount=10,delivery_status='accepted',main_status='completed',
            snapshot_version=1,delivery_method_snapshot='api',business_type='purchase'))
        db.flush()
        frozen=m.freeze_sla_snapshot(db,'owned-buyer')
        db.add(m.Payment(id='owned-evidence-payment',order_id='owned-live-evidence-order',
            payment_no='OWNED-QA004-PAY',status='paid',amount=10,paid_at=at))
        db.add(m.DeliveryTask(id='owned-evidence-task',order_id='owned-live-evidence-order',
            method='api',delivery_mode='online',status='completed'))
        db.flush()
        db.add(m.order_sla_contracts['Contract'](order_id='owned-live-evidence-order',
            source_payment_id='owned-evidence-payment',snapshot_json=json.dumps(frozen),paid_at=at,
            deadline_at=at+timedelta(seconds=frozen['metrics']['delivery']['target_seconds']),
            verified_at=at+timedelta(seconds=1),source_delivery_task_id='owned-evidence-task',
            delivery_method='api',delivery_eligible=True))
    return headers


def exercise(m,client,headers):
    from sqlalchemy import event,select,func
    from sqlalchemy.orm import Session
    result={}
    ticket='/api/after-sales/owned-evidence-ticket/sla/evidence'
    source='/api/sla/results/owned-evidence-result/sources?metric=availability'

    def snapshot():
        # This engine has the harness-checked, owned-schema-only search_path.
        # Keep synthetic values private; only equality is reported.
        with m.SessionLocal() as db:
            return {table.name:[tuple(row) for row in db.execute(select(table).order_by(*table.primary_key.columns))]
                    for table in m.Base.metadata.sorted_tables}

    def call(path,actor='buyer',expected=200):
        response=client.get(path,headers=headers[actor])
        base.check(response.status_code==expected,'Unexpected QA004 GET status')
        return response.json() if expected==200 else {}

    def readonly():
        before=snapshot()
        def begin(connection):
            connection.exec_driver_sql('SET TRANSACTION READ ONLY')
        def forbidden(*args,**kwargs):
            raise AssertionError('GET attempted materialization or commit')
        event.listen(m.engine,'begin',begin)
        try:
            with patch.object(Session,'commit',side_effect=forbidden), \
                 patch.object(m,'notify_business_event',side_effect=forbidden), \
                 patch.object(m,'resolve_ticket_calendar',side_effect=forbidden), \
                 patch.dict(m.ticket_sla_clocks,refresh=forbidden), \
                 patch.dict(m.sla_online_sources,measure_order=forbidden):
                row=call(ticket)
                old=call(source,'ops')
        finally:
            event.remove(m.engine,'begin',begin)
        base.check(row['items']==[] and row['reason']=='no_persisted_clocks' and row['materialized'] is False,
                   'GET invented historical clocks')
        base.check(old['evidence_status']=='unknown' and old['items']==[] and old['metric_result'] is None,
                   'GET invented historical source association')
        base.check(snapshot()==before,'GET changed owned database rows')
        return {'pg_read_only_transaction':True,'all_owned_tables_unchanged':True,
                'missing_clocks_and_sources_not_backfilled':True}

    def permissions():
        before=snapshot()
        for actor in ('buyer','provider','admin','ops','monitor'):
            call(ticket,actor)
        for actor in ('member','outsider','finance'):
            call(ticket,actor,404)
        call(ticket+'?enterprise_id=owned-other',expected=403)
        for actor in ('admin','ops'):
            call(source,actor)
        for actor in ('buyer','provider','member','outsider','monitor','finance'):
            call(source,actor,403)
        base.check(snapshot()==before,'Permission GETs changed owned rows')
        return {'ticket_own_parties_and_platform':200,'ticket_unrelated':404,
                'explicit_enterprise_scope':403,'online_sources_admin_operator_only':True}

    def pagination():
        before=snapshot()
        for size in (10,12,20,50,100):
            row=call(ticket+'?page_size='+str(size))
            base.check(row['page_size']==size and row['total']==0,'Empty clock pagination metadata invalid')
        call(ticket+'?page=0',expected=422)
        call(ticket+'?page_size=101',expected=422)
        call(source+'&version=0','ops',422)
        call(source+'&page=0','ops',422)
        call(source+'&page_size=101','ops',422)
        base.check(call(source+'&page=99','ops')['items']==[],'Historical unknown page invented facts')
        base.check(snapshot()==before,'Pagination GETs changed owned rows')
        return {'sizes':[10,12,20,50,100],'invalid_parameters':422,'empty_unknown_pages_stable':True}

    def versions_and_atomicity():
        from app.sla_metrics import MetricResult
        from app.sla_evidence import bundle
        Assessment=m.sla_online_assessments['Assessment']
        Version=m.sla_evidence['Version']
        at=datetime(2026,10,15,2,tzinfo=timezone.utc)
        source_value={'measurements':{'availability':MetricResult(0,'breached',205,coverage_percent=100)},
            'source_available':{'availability':True},'source_version':'qa004-synthetic-pg',
            'request_source_cutoff':at.isoformat(),'evidence_digest':'owned-synthetic-v1',
            'evidence':{'availability':bundle([{'kind':'probe','fact_id':'owned-probe-'+str(i),
                'occurred_at':at,'status':'failed','included':True,'secret':'PRIVATE_SENTINEL'}
                for i in range(205)],start=at-timedelta(hours=1),end=at)}}
        def evaluate(expected=200):
            response=client.post('/api/sla/evaluate',headers=headers['ops'])
            base.check(response.status_code==expected,'Unexpected owned evaluation outcome')
            if expected!=200:return None
            return next(x for x in response.json()['items'] if x['order_id']=='owned-live-evidence-order')
        def sources(row,**query):
            path='/api/sla/results/'+row['id']+'/sources?metric=availability'
            return call(path+''.join('&'+key+'='+str(value) for key,value in query.items()),'ops')
        with patch.object(m,'now',side_effect=lambda:at), \
             patch.dict(m.sla_online_sources,measure_order=lambda *args:source_value):
            first=evaluate();original=sources(first)
            base.check(original['total']==200 and original['source_total']==205 and original['truncated'],
                       'Retained pagination and observed facts were conflated')
            base.check(len(sources(first,page_size=100,page=2)['items'])==100 and
                       not sources(first,page_size=100,page=3)['items'],'Retained page boundary invalid')
            base.check('PRIVATE_SENTINEL' not in json.dumps(original),'Evidence leaked non-whitelisted fields')
            base.check(evaluate()['version']==first['version'],'Identical evaluation created another version')
            with m.SessionLocal() as db:
                contract=m.order_sla_contracts['get_contract'](db,'owned-live-evidence-order')
                frozen=contract.snapshot_json
            source_value['measurements']['availability']=MetricResult(100,'met',205,coverage_percent=100)
            for fact in source_value['evidence']['availability']['items']:fact['status']='healthy'
            at+=timedelta(minutes=5)
            source_value['request_source_cutoff']=at.isoformat()
            source_value['evidence_digest']='owned-synthetic-v2'
            second=evaluate()
            base.check(second['version']==first['version']+1,'Changed evidence did not produce one new version')
            base.check(sources(second,version=first['version'])==original,'Old facts or metadata changed after re-evaluation')
            base.check(sources(second)['metric_result']['value']==100,'Current evidence uses old metric values')
            with m.SessionLocal.begin() as db:
                db.get(m.ServiceLevel,'owned-standard').initial_response_minutes=999
            base.check(evaluate()['version']==second['version'],'Current tier edit changed frozen assessment')
            with m.SessionLocal() as db:
                base.check(m.order_sla_contracts['get_contract'](db,'owned-live-evidence-order').snapshot_json==frozen,
                           'Current tier edit changed original order snapshot')
                base.check(db.scalar(select(func.count()).select_from(Version))==2,'Version associations duplicated')
            source_value['measurements']['availability']=MetricResult(0,'breached',205,coverage_percent=100)
            source_value['evidence_digest']='owned-synthetic-failure'
            for operation in ('audit','notify_business_event'):
                before=snapshot()
                original_operation=getattr(m,operation)
                def fail_after_flush(*args,_original=original_operation,**kwargs):
                    _original(*args,**kwargs)
                    args[0].flush()
                    raise RuntimeError('Owned fault after database flush')
                with patch.object(m,operation,side_effect=fail_after_flush):evaluate(500)
                base.check(snapshot()==before,'Evaluation/evidence/audit/Outbox failed to roll back together')
            final=evaluate()
            base.check(final['version']==second['version']+1,'Retry did not recover one version')
            base.check(evaluate()['version']==final['version'],'Retry replay created another version')
        return {'controlled_measurements':True,'real_pg_transactions':True,'versions_immutable':True,
                'metadata_and_fact_snapshot_consistent':True,'retained_total':200,'source_total':205,
                'tier_edit_did_not_rewrite_frozen_order':True,
                'audit_and_outbox_flushed_faults_rolled_back':True,'retry_idempotent':True}

    for name,fn in [('GET_readonly_no_historical_backfill',readonly),
                    ('GET_enterprise_and_platform_permissions',permissions),('GET_empty_pagination_contract',pagination),
                    ('version_snapshot_pagination_atomicity',versions_and_atomicity)]:
        try:
            result[name]={'status':'passed',**fn()}
        except Exception as exc:
            result[name]=base.safe_error(exc)
    return result


def run(expected):
    spec=importlib.util.find_spec('app')
    base.check(spec is not None and spec.origin is not None,'Installed app package missing')
    hashes=verify_sources(Path(spec.origin).resolve().parent,expected)
    with patch.object(base,'seed',seed),patch.object(base,'exercise',exercise):
        result=base.run()
    result.update(status='passed' if result.get('cases') and all(c['status']=='passed' for c in result['cases'].values()) else 'failed',
        coverage=['TRD-R3-QA-004'],source_kind='installed',source_sha256=hashes,
        scope='Real PG/ASGI evidence read-only, versions and atomic rollback; controlled synthetic measurements',
        gaps=['No browser/ingress or real gateway sample collection in this focused probe',
              'No whole QA004 acceptance claim without the separate frozen-calendar and UI evidence'])
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-source-manifest',required=True)
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        result=run(json.loads(Path(args.expected_source_manifest).read_text()))
    except Exception as exc:
        result={'status':'failed','error':base.safe_error(exc)}
    print(json.dumps(result,ensure_ascii=True))
    raise SystemExit(1 if result['status']=='failed' else 0)
