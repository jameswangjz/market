"""Focused workingtree BE005/008 concurrency proof, never a deployment.

Local: SSHPASS in environment; python -B script --pod API_POD
Ships only backend/app Python/JSON source in memory. Remote temporary package
and random PG schema are removed. No startup/ensure_schema or public writes.
Results go to stdout; caller archives sanitized JSON separately.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
from types import ModuleType
from unittest.mock import patch
import zlib


def owned_schema(value):
    return bool(re.fullmatch(r'qa_r3_conc_[0-9a-f]{16}', value))


def check(value, message):
    if not value:
        raise AssertionError(message)


def safe_error(exc):
    tb = exc.__traceback__
    while tb and tb.tb_next:
        tb = tb.tb_next
    return {'status': 'failed', 'error_type': type(exc).__name__,
            'line': tb.tb_lineno if tb else None}


def pack_workingtree(root):
    files = {}
    for path in sorted((root/'backend/app').rglob('*')):
        if '__pycache__' in path.parts or path.suffix not in {'.py', '.json'}:
            continue
        check(path.is_file() and not path.is_symlink(), 'Only regular source files permitted')
        name = path.relative_to(root/'backend/app').as_posix()
        files[name] = path.read_text(encoding='utf-8')
    raw = json.dumps(files, sort_keys=True).encode()
    check(0 < len(raw) < 4*1024*1024, 'Source package outside size bounds')
    manifest = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}
    return base64.b64encode(zlib.compress(raw)).decode(), manifest


def unpack_workingtree(encoded, manifest, target):
    raw = zlib.decompress(base64.b64decode(encoded))
    check(len(raw) < 4*1024*1024, 'Oversized source package')
    files = json.loads(raw)
    check(set(files) == set(manifest), 'Package file inventory mismatch')
    for name, value in files.items():
        relative = Path(name)
        check(not relative.is_absolute() and '..' not in relative.parts and
              relative.suffix in {'.py', '.json'}, 'Unsafe source path')
        check(hashlib.sha256(value.encode()).hexdigest() == manifest[name], 'Source digest mismatch')
        path = target/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')


def seed(m):
    with m.SessionLocal() as db:
        for eid in ('owned-buyer', 'owned-provider'):
            db.add(m.Enterprise(id=eid, name=eid, credit_code=eid, verification_status='verified'))
        for name, role in [('buyer', ''), ('provider', ''), ('admin', 'super_admin')]:
            db.add(m.User(id='owned-'+name, name='Owned concurrency actor', email=name+'@example.invalid',
                password_hash='unused', platform_role=role, verified_status='verified', activation_status='active'))
        db.flush()
        for name in ('buyer', 'provider'):
            db.add(m.Membership(user_id='owned-'+name, enterprise_id='owned-'+name,
                role='super_admin', status='active'))
        db.add(m.Product(id='owned-product', enterprise_id='owned-provider', name='Owned concurrency',
                         product_type='dataset', delivery_method='file', status='published'))
        for code in ('standard', 'enterprise', 'strategic'):
            db.add(m.ServiceLevel(id='owned-'+code, code=code, name='Owned '+code, status='active'))
        db.flush()
        for name in ('race', 'audit-failure', 'outbox-failure', 'snapshot'):
            db.add(m.Order(id='owned-'+name, order_no='OWNED-CONC-'+name,
                buyer_enterprise_id='owned-buyer', provider_enterprise_id='owned-provider',
                buyer_user_id='owned-buyer', product_id='owned-product',
                buyer_name='Owned buyer', product_name='Owned concurrency', amount=10, paid_amount=10,
                payment_status='paid', main_status='completed', delivery_status='accepted'))
        db.commit()


def cases(m):
    from sqlalchemy import func, or_, select, text
    workflow = importlib.import_module(m.__package__+'.after_sales_workflow')
    results = {}
    barrier = None

    def pair(functions):
        nonlocal barrier
        barrier = threading.Barrier(2, timeout=8)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(fn) for fn in functions]
            return [future.result(timeout=20) for future in futures]

    def transaction(actor, operation, synchronize=False):
        with m.SessionLocal() as db:
            check(db.scalar(text('SELECT current_schema()')).startswith('qa_r3_conc_'), 'Session escaped schema')
            pid = db.scalar(text('SELECT pg_backend_pid()'))
            user = db.get(m.User, 'owned-'+actor)
            if synchronize:
                barrier.wait()
            try:
                item = operation(db, user)
                db.flush()
                data = {'status': 200, 'pid': pid, 'id': getattr(item, 'id', None)}
                db.commit()
                return data
            except m.HTTPException as exc:
                db.rollback()
                return {'status': exc.status_code, 'pid': pid}

    def run_case(name, fn):
        try:
            results[name] = {'status': 'passed', **fn()}
        except Exception as exc:
            results[name] = safe_error(exc)

    def body(order, key):
        return workflow.TicketCreate(order_id='owned-'+order, type='owned-quality',
                                    description='Owned concurrency evidence', idempotency_key=key)

    def create_race():
        outcomes = pair([lambda i=i: transaction('buyer', lambda db, u:
            m.after_sales_workflow['create'](db, u, body('race', 'owned-create-'+str(i))), True)
            for i in range(2)])
        check(sorted(o['status'] for o in outcomes) == [200, 409], 'Expected one create and one conflict')
        check(len({o['pid'] for o in outcomes}) == 2, 'Race did not use independent PG sessions')
        with m.SessionLocal() as db:
            count = db.scalar(select(func.count()).select_from(m.AfterSalesTicket).where(m.AfterSalesTicket.order_id == 'owned-race'))
            events = db.scalar(select(func.count()).select_from(m.AfterSalesEvent))
            check(count == 1 and events == 1, 'Duplicate ticket or event')
        return {'http_equivalent_statuses': sorted(o['status'] for o in outcomes),
                'independent_pg_connections': 2, 'ticket_count': count, 'event_count': events,
                'synchronization': 'two sessions barrier before hook; real PostgreSQL row locks'}
    run_case('BE005_concurrent_same_order_same_type_create', create_race)

    def action_race():
        with m.SessionLocal() as db:
            ticket = db.scalar(select(m.AfterSalesTicket).where(m.AfterSalesTicket.order_id == 'owned-race'))
            check(ticket is not None, 'Create race fixture unavailable')
            tid, version = ticket.id, ticket.version
        actions = [('provider', 'respond'), ('buyer', 'withdraw')]
        outcomes = pair([lambda actor=actor, action=action: transaction(actor, lambda db, u:
            m.after_sales_workflow['act'](db, u, tid, workflow.TicketAction(action=action,
                reason='Owned concurrent action', expected_version=version,
                idempotency_key='owned-action-'+action)), True) for actor, action in actions])
        check(sorted(o['status'] for o in outcomes) == [200, 409], 'Expected one action and one version conflict')
        check(len({o['pid'] for o in outcomes}) == 2, 'Action race needs independent connections')
        with m.SessionLocal() as db:
            ticket = db.get(m.AfterSalesTicket, tid)
            events = db.scalar(select(func.count()).select_from(m.AfterSalesEvent).where(m.AfterSalesEvent.ticket_id == tid))
            check(ticket.version == version+1 and events == 2, 'Action conflict left duplicate version/event')
            return {'http_equivalent_statuses': [200, 409], 'independent_pg_connections': 2,
                    'expected_version': version, 'final_version': ticket.version, 'event_count': events,
                    'winning_state': ticket.status, 'different_actions': ['respond', 'withdraw']}
    run_case('BE005_same_expected_version_different_actions', action_race)

    def database_snapshot():
        models = [m.AfterSalesTicket, m.AfterSalesEvent, m.AuditLog,
                  *[m.message_center[k] for k in ('Message', 'Receipt', 'Outbox')], m.Order]
        with m.SessionLocal() as db:
            # All columns belong exclusively to this synthetic schema; never serialize values.
            return [[dict(row) for row in db.execute(select(model.__table__).order_by(model.id)).mappings()]
                    for model in models]

    def rollback_failure(kind):
        before = database_snapshot()
        real_audit = m.audit
        real_create = m.message_center['create']
        injected = []
        def audit_fail(*args, **kwargs):
            real_audit(*args, **kwargs)
            args[0].flush()
            injected.append(True)
            raise RuntimeError('Owned audit failure')
        def outbox_fail(*args, **kwargs):
            real_create(*args, **kwargs)
            args[0].flush()
            injected.append(True)
            raise RuntimeError('Owned failure after outbox flush')
        replacement = patch.object(m, 'audit', side_effect=audit_fail) if kind == 'audit' else patch.dict(
            m.message_center, {'create': outbox_fail})
        failed = False
        with replacement:
            try:
                transaction('buyer', lambda db, u: m.after_sales_workflow['create'](
                    db, u, body(kind+'-failure', 'owned-failure-'+kind)))
            except RuntimeError:
                failed = True
        check(failed and injected, 'Failure injection did not execute after real writes')
        check(database_snapshot() == before, 'Atomic rollback left ticket/event/order/audit/outbox changes')
        return {'failure_after_real_flush': True, 'exact_owned_database_snapshot_unchanged': True,
                'checked': ['tickets', 'events', 'orders', 'audits', 'messages', 'receipts', 'outbox']}
    run_case('BE005_audit_failure_atomic_rollback', lambda: rollback_failure('audit'))
    run_case('BE005_outbox_failure_atomic_rollback', lambda: rollback_failure('outbox'))

    def assignment_race():
        old = transaction('buyer', lambda db, u: m.after_sales_workflow['create'](
            db, u, body('snapshot', 'owned-old-snapshot')))
        with m.SessionLocal() as db:
            old_snapshot = db.get(m.AfterSalesTicket, old['id']).sla_snapshot_json
        def assign(level):
            def operation(db, user):
                item, _, _ = m.enterprise_sla['adjust_assignment'](db, user,
                    m.ServiceLevelAssignmentBody(enterprise_id='owned-buyer',
                        service_level_id='owned-'+level, reason='Owned concurrent adjustment'))
                return item
            return transaction('admin', operation, True)
        outcomes = pair([lambda: assign('enterprise'), lambda: assign('strategic')])
        check([o['status'] for o in outcomes] == [200, 200], 'Both serialized assignments must succeed')
        check(len({o['pid'] for o in outcomes}) == 2, 'Assignments need independent PG sessions')
        with m.SessionLocal() as db:
            at = m.now()
            assignment = m.ServiceLevelAssignment
            active = db.scalars(select(assignment).where(assignment.enterprise_id == 'owned-buyer',
                assignment.user_id == '', assignment.effective_at <= at,
                or_(assignment.expires_at.is_(None), assignment.expires_at > at))).all()
            all_rows = db.scalars(select(assignment).where(assignment.enterprise_id == 'owned-buyer')).all()
            check(len(active) == 1 and len(all_rows) == 2, 'Concurrent assignment has overlapping active rows')
            audits = db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.action == 'assign_service_level'))
            check(audits == 2, 'Assignment audit missing or duplicated')
            frozen = m.freeze_sla_snapshot(db, 'owned-buyer')
            check(frozen['service_level_id'] == active[0].service_level_id, 'New snapshot did not use winning assignment')
            check(db.get(m.AfterSalesTicket, old['id']).sla_snapshot_json == old_snapshot, 'Existing ticket snapshot changed')
            return {'http_equivalent_statuses': [200, 200], 'independent_pg_connections': 2,
                    'assignment_rows': 2, 'effective_rows': 1, 'audits': audits,
                    'new_snapshot_uses_effective_assignment': True, 'old_ticket_snapshot_unchanged': True}
    run_case('BE008_concurrent_assignment_one_effective_row', assignment_race)
    return results


def remote_run(encoded, manifest):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.schema import CreateSchema, DropSchema
    logging.disable(logging.CRITICAL)
    url = make_url(os.environ['DATABASE_URL'])
    check(url.get_backend_name() == 'postgresql', 'Real PostgreSQL required')
    schema = 'qa_r3_conc_'+secrets.token_hex(8)
    check(owned_schema(schema), 'Invalid schema')
    control = create_engine(url, hide_parameters=True, connect_args={'connect_timeout': 5})
    created = False; module = None
    result = {'status': 'failed', 'source_kind': 'workingtree', 'deployed': False,
              'captured_at': datetime.now(timezone.utc).isoformat(), 'source_sha256': manifest}
    def protected_count():
        with control.connect() as c:
            c.exec_driver_sql('SET TRANSACTION READ ONLY')
            c.exec_driver_sql("SET LOCAL statement_timeout = '4000ms'")
            return c.scalar(text('SELECT COUNT(*) FROM public.orders WHERE order_no=:n'),
                            {'n': 'ORD-261012005144E1B3'})
    result['protected_order_count_before'] = protected_count()
    try:
        with control.begin() as c:
            c.execute(CreateSchema(schema))
        created = True
        query = dict(url.query)
        query['options'] = '-csearch_path='+schema+' -cstatement_timeout=8000 -clock_timeout=5000'
        with tempfile.TemporaryDirectory(prefix='market-r3-workingtree-') as directory, ExitStack() as stack:
            target = Path(directory)/'app'
            unpack_workingtree(encoded, manifest, target)
            stack.enter_context(patch.dict(os.environ, {
                'DATABASE_URL': url.set(query=query).render_as_string(hide_password=False),
                'MESSAGE_CENTER_DISABLE_BACKGROUND': 'true', 'PYTHONDONTWRITEBYTECODE': '1'}))
            package_name = 'qa_r3_workingtree_'+secrets.token_hex(6)
            package = ModuleType(package_name); package.__path__ = [str(target)]
            sys.modules[package_name] = package
            spec = importlib.util.spec_from_file_location(package_name+'.main', target/'main.py')
            module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            with module.engine.connect() as c:
                check(c.scalar(text('SELECT current_schema()')) == schema, 'Wrong schema')
                check(c.scalar(text('SHOW search_path')) == schema, 'Public fallback forbidden')
            module.Base.metadata.create_all(module.engine)
            for name in ('request', 'get', 'post'):
                stack.enter_context(patch.object(module.httpx, name, side_effect=AssertionError('External HTTP forbidden')))
            stack.enter_context(patch.object(module.redis.Redis, 'from_url', side_effect=AssertionError('Redis forbidden')))
            stack.enter_context(patch.object(module, 'Minio', side_effect=AssertionError('Storage forbidden')))
            for name in ('SMTP', 'SMTP_SSL'):
                stack.enter_context(patch.object(module.smtplib, name, side_effect=AssertionError('Mail forbidden')))
            seed(module)
            result['cases'] = cases(module)
            result['status'] = 'passed' if all(v['status'] == 'passed' for v in result['cases'].values()) else 'failed'
            result['completed_at'] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        result['error'] = safe_error(exc)
    finally:
        if module:
            module.engine.dispose()
        if created:
            check(owned_schema(schema), 'Unsafe cleanup schema')
            with control.begin() as c:
                c.execute(DropSchema(schema, cascade=True))
            with control.connect() as c:
                check(c.scalar(text('SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=:n'),
                               {'n': schema}) == 0, 'Schema cleanup failed')
        result['cleanup'] = 'owned schema removed' if created else 'no schema created'
        result['protected_order_count_after'] = protected_count()
        result['public_business_writes'] = 0; result['development_task_changes'] = 0
        control.dispose()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pod', required=True)
    args = parser.parse_args()
    check(bool(re.fullmatch(r'market-api-[a-z0-9-]+', args.pod)), 'Invalid API pod')
    check(bool(os.environ.get('SSHPASS')), 'Provide SSHPASS privately')
    root = Path(__file__).resolve().parents[1]
    bundle, manifest = pack_workingtree(root)
    source = Path(__file__).read_text()
    payload = "__name__='r3_remote_payload'\n"+source+"\ntry:\n print(json.dumps(remote_run("+repr(bundle)+','+repr(manifest)+")))\nexcept Exception as exc:\n print(json.dumps({'status':'failed','error':safe_error(exc)}))\n"
    remote = 'kubectl --request-timeout=5s -n market exec -i '+args.pod+' -- env MESSAGE_CENTER_DISABLE_BACKGROUND=true PYTHONDONTWRITEBYTECODE=1 python -B -'
    process = subprocess.run(['sshpass', '-e', 'ssh', '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1',
        '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR',
        'root@192.168.10.10', remote], input=payload, capture_output=True, text=True, timeout=80)
    check(process.returncode == 0, 'Bounded remote execution failed; stderr suppressed')
    result = json.loads(process.stdout)
    print(json.dumps(result))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': safe_error(exc)}))
        sys.exit(1)
