"""Owned PostgreSQL executor checks; synthetic resources, simulated funds only.

Reuses the bounded source-integrity harness. Never starts workers against public
data; resource calls are explicit test doubles, not APISIX or supplier evidence.
"""
import argparse
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import patch

import r3_backend_acceptance_pg as base
import r3_backend_acceptance_concurrency as transport
import r3_after_sales_group_pg_acceptance as fixture
import r3_refund_cross_version_pg_acceptance as cross


CASES = ('execute_permissions_idempotency', 'resource_failure_retry',
         'worker_restart_resume', 'completion_audit_rollback', 'multi_source_order_isolation')
SOURCES = cross.SOURCE_FILES + ('refund_execution.py', 'refund_execution_worker.py',
                              'refund_execution_resources.py')


def seed(m):
    headers = fixture.seed(m)
    source, graphs = cross.MockRedis(), {}
    for name in CASES:
        with m.SessionLocal.begin() as db:
            graphs[name] = cross.graph(m, db, source, name)
    m._owned_execution = source, graphs
    return headers


def exercise(m, client, headers):
    from sqlalchemy import func, select
    worker = importlib.import_module(m.__package__ + '.refund_execution_worker')
    implementation = importlib.import_module(m.__package__ + '.refund_execution')
    source, graphs = m._owned_execution
    hooks = m.refund_execution
    Case, Leg, Completion = m.refund_workflow['Case'], hooks['PaymentLeg'], hooks['Completion']
    results, receipts, external_calls, fail_once = {}, {}, {}, set()

    def call(method, path, actor='admin', expected=200, **kwargs):
        response = client.request(method, path, headers=headers[actor], **kwargs)
        base.check(response.status_code == expected, 'Executor ASGI status mismatch')
        return response.json() if expected < 400 else None

    def prepare(db, order, refund, envelope):
        plan = dict(refund_id=refund.id, order_id=order.id, kind='synthetic_pg_resource',
                    frozen_envelope_hash=implementation.digest(envelope))
        plan['plan_hash'] = implementation.digest(plan)
        return plan

    def apply(db, execution):
        rid = execution.refund_id
        if rid in fail_once:
            fail_once.remove(rid)
            raise RuntimeError('Synthetic resource failure')
        if rid not in receipts:
            external_calls[rid] = external_calls.get(rid, 0) + 1
            receipts[rid] = dict(refund_id=rid, resource_plan_hash=execution.resource_plan_hash,
                evidence_fingerprint=implementation.digest(dict(refund_id=rid, synthetic=True)))
        return receipts[rid]

    def version(rid):
        with m.SessionLocal() as db:
            return db.get(Case, rid).version

    def request(name, index=0, amount='10.00'):
        oid = graphs[name]['roots'][index]
        prefix = name + '-' + str(index)
        row = call('POST', '/api/orders/' + oid + '/refund-requests', actor='buyer',
            params=dict(enterprise_id='owned-buyer', business_view='buyer'),
            json=dict(amount=amount, reason='Owned execution request', idempotency_key=prefix + '-request'))
        call('POST', '/api/refunds/' + row['id'] + '/review', json=dict(decision='approve',
            reason='Owned execution approval', expected_version=row['version'], idempotency_key=prefix + '-approve'))
        return row['id']

    def execute(rid, key, operation='execute', actor='admin', expected=200, body=None):
        payload = body or dict(expected_version=version(rid), reason='Owned simulated execution',
                               idempotency_key=key)
        return call('POST', '/api/refunds/' + rid + '/' + operation, actor=actor,
                    expected=expected, json=payload), payload

    def state(rid):
        with m.SessionLocal() as db:
            refund = db.get(m.Refund, rid)
            execution = db.scalar(select(hooks['Execution']).where(hooks['Execution'].refund_id == rid))
            legs = list(db.scalars(select(Leg).where(Leg.refund_id == rid)))
            count = db.scalar(select(func.count()).select_from(Completion).where(Completion.refund_id == rid))
            return dict(refund=refund.status, execution=execution.status if execution else None,
                        completed_legs=sum(row.status == 'completed' for row in legs),
                        legs=len(legs), completions=count)

    def drive(rid, target='completed'):
        for _ in range(16):
            if state(rid)['execution'] == target:
                return state(rid)
            base.check(worker.process_once(ns=vars(m)), 'Expected owned execution work')
        raise base.AcceptanceFailure('Bounded executor did not reach target')

    def permissions():
        rid = request(CASES[0])
        for actor in ('finance', 'monitor', 'buyer', 'provider'):
            execute(rid, 'denied-' + actor, actor=actor, expected=403)
        _, body = execute(rid, 'owned-execute-repeat')
        execute(rid, 'unused', body=body)
        done = drive(rid)
        execute(rid, 'unused', body=body)
        base.check(done['completions'] == 1 and done['completed_legs'] == done['legs'] > 0,
                   'Completion or payment legs missing')
        base.check(external_calls[rid] == 1, 'Resource repeated')
        return dict(permissions_checked=True, completion_unique=True, resource_calls=external_calls[rid])

    def resource_failure():
        rid = request(CASES[1])
        fail_once.add(rid)
        execute(rid, 'owned-resource-failure')
        failed = drive(rid, 'failed')
        base.check(failed['completed_legs'] == 0, 'Funds executed before resources')
        execute(rid, 'owned-resource-retry', operation='retry')
        done = drive(rid)
        base.check(done['completions'] == 1 and external_calls[rid] == 1, 'Retry duplicated execution')
        return dict(funds_before_resource=0, authorized_retry_completed=True)

    def restart():
        rid = request(CASES[2])
        execute(rid, 'owned-restart')
        drive(rid, 'funds_pending')
        with m.SessionLocal() as db:
            from sqlalchemy import text
            owned_schema = db.scalar(text('SELECT current_schema()'))
        base.check(base.owned_schema(owned_schema), 'Child must use owned schema')
        program = '''import json,os,logging
from contextlib import ExitStack
from unittest.mock import patch
from sqlalchemy import text
logging.disable(logging.CRITICAL)
from app import main as m
from app.refund_execution_worker import process_once
with m.SessionLocal() as db:
    assert db.scalar(text('SELECT current_schema()')) == SCHEMA
    assert db.scalar(text('SHOW search_path')) == SCHEMA
with ExitStack() as stack:
    for name in ('request','get','post'):
        stack.enter_context(patch.object(m.httpx,name,side_effect=AssertionError('External HTTP forbidden')))
    stack.enter_context(patch.object(m.redis.Redis,'from_url',side_effect=AssertionError('Redis forbidden')))
    for name in ('SMTP','SMTP_SSL'):
        stack.enter_context(patch.object(m.smtplib,name,side_effect=AssertionError('Mail forbidden')))
    worked=process_once(ns=vars(m))
print(json.dumps({'pid':os.getpid(),'worked':worked}))
'''.replace('SCHEMA', repr(owned_schema))
        processes = []
        source_root = str(Path(m.__file__).resolve().parents[1])
        for _ in range(2):
            process = subprocess.run([sys.executable, '-B', '-c', program], capture_output=True,
                text=True, timeout=25, cwd=source_root,
                env={**os.environ, 'PYTHONPATH': source_root})
            base.check(process.returncode == 0, 'Owned worker child failed')
            processes.append(json.loads(process.stdout))
        base.check(len({p['pid'] for p in processes}) == 2 and all(p['worked'] for p in processes),
                   'Two independent worker processes required')
        done = drive(rid)
        base.check(done['completions'] == 1 and external_calls[rid] == 1, 'Restart duplicated execution')
        return dict(independent_worker_processes=2, completion_unique=True,
                    limitation='Fresh OS processes; not an abrupt kill during a database commit')

    def completion_rollback():
        rid = request(CASES[3])
        execute(rid, 'owned-completion-rollback')
        drive(rid, 'funds_pending')
        for _ in range(8):
            current = state(rid)
            if current['completed_legs'] == current['legs']:
                break
            worker.process_once(ns=vars(m))
        original = m.audit

        def fault(db, actor, action, *args, **kwargs):
            if action == 'ordinary_refund_completed':
                raise RuntimeError('Synthetic completion audit failure')
            return original(db, actor, action, *args, **kwargs)

        with patch.object(m, 'audit', side_effect=fault):
            worker.process_once(ns=vars(m))
        failed = state(rid)
        base.check(failed['execution'] == 'failed' and failed['completions'] == 0,
                   'Completion survived failed audit')
        base.check(failed['completed_legs'] == failed['legs'], 'Successful funds lost')
        execute(rid, 'owned-completion-retry', operation='retry')
        done = drive(rid)
        base.check(done['completions'] == 1 and external_calls[rid] == 1, 'Completion retry duplicated work')
        return dict(completion_rolled_back=True, successful_legs_preserved=True, retry_completed=True)

    def multi_source():
        name = CASES[4]
        amounts = []
        for index in range(2):
            with m.SessionLocal() as db:
                window = db.get(m.refund_segment_store['Window'], graphs[name]['windows'][index])
                amount = format(window.revenue, '.2f')
            amounts.append(amount)
            rid = request(name, index, amount)
            execute(rid, 'owned-multi-source-' + str(index))
            drive(rid)
        with m.SessionLocal() as db:
            legs = list(db.scalars(select(Leg).where(Leg.refund_id == rid)))
            base.check(len(legs) > 1 and all(leg.status == 'completed' for leg in legs),
                       'Second refund must actually execute multiple payment sources')
            for oid, amount in zip(graphs[name]['roots'], amounts):
                order = db.get(m.Order, oid)
                base.check(format(order.refunded_amount, '.2f') == amount,
                           'Economic refund must stay with its own order')
            funding_order = db.get(m.Order, graphs[name]['funding_order'])
            base.check(funding_order.refunded_amount == 0,
                       'Payment routing must not overwrite funding order economic refund')
        return dict(second_refund_payment_legs=len(legs),
                    economic_order_isolation=True, payment_and_economic_sources_separate=True)

    with patch.object(m, 'apisix_policy_redis', return_value=source), \
            patch.dict(m.refund_execution_resources, prepare=prepare, apply=apply):
        for name, run in zip(CASES, (permissions, resource_failure, restart, completion_rollback, multi_source)):
            try:
                results[name] = dict(status='passed', **run())
            except Exception as exc:
                results[name] = base.safe_error(exc)
    return results


def remote_run(encoded, manifest):
    with patch.object(cross, 'seed', seed), patch.object(cross, 'exercise', exercise), \
            patch.object(cross, 'CASE_NAMES', CASES), patch.object(cross, 'SOURCE_FILES', SOURCES):
        result = cross.remote_run(encoded, manifest)
    result.update(incremental_scope='BE012 owned PostgreSQL executor with synthetic resources',
        coverage=[], gaps=['No APISIX or supplier resource verification in this script',
                          'No actual bank, browser or abrupt OS-process kill'],
        external_adapters={'resources': 'Explicit synthetic idempotent adapter', 'funds': 'Platform simulated legs'})
    return result


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pod', required=True)
    args = parser.parse_args()
    base.check(bool(re.fullmatch(r'market-api-[a-z0-9-]+', args.pod)) and bool(os.getenv('SSHPASS')),
               'Bounded pod and private transport required')
    encoded, manifest = transport.pack_workingtree(Path(__file__).resolve().parents[1])
    payload = 'import sys,types\n'
    for name in ('r3_backend_acceptance_pg', 'r3_backend_acceptance_concurrency',
                 'r3_after_sales_group_pg_acceptance', 'r3_refund_workflow_pg_acceptance',
                 'r3_refund_cross_version_pg_acceptance'):
        source = Path(__file__).with_name(name + '.py').read_text()
        payload += 'm=types.ModuleType(' + repr(name) + ');sys.modules[m.__name__]=m;exec(compile(' + repr(source) + ',m.__name__,"exec"),m.__dict__)\n'
    payload += "__name__='execution_remote'\n" + Path(__file__).read_text()
    payload += '\nprint(json.dumps(remote_run(' + repr(encoded) + ',' + repr(manifest) + ')))\n'
    command = ('kubectl --request-timeout=5s -n market exec -i ' + args.pod
               + ' -- env MESSAGE_CENTER_DISABLE_BACKGROUND=true PYTHONDONTWRITEBYTECODE=1 python -B -')
    process = subprocess.run(['sshpass', '-e', 'ssh', '-o', 'ConnectTimeout=5', '-o', 'LogLevel=ERROR',
        'root@192.168.10.10', command], input=payload, capture_output=True, text=True, timeout=180)
    base.check(process.returncode == 0, 'Private transport failed')
    result = json.loads(process.stdout)
    print(json.dumps(result))
    return 0 if result.get('status') == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
