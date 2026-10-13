"""PostgreSQL process/lease crash acceptance for the production version worker.

The worker and database hooks are production code. Only the external resource
boundary is a controlled local HTTP adapter; this does not test APISIX or SaaS.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import multiprocessing
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema


PROTECTED_ORDER = 'ORD-261012005144E1B3'
CONTROL = {}
EXTERNAL = {}
EXTERNAL_REQUESTS = []
EXTERNAL_LOCK = threading.Lock()
ACTIVE_CHILDREN = set()


class AcceptanceCheckFailure(AssertionError):
    pass


def check(value, message):
    if not value:
        raise AcceptanceCheckFailure(message)


def fingerprint(engine):
    metadata = MetaData()
    orders = Table('orders', metadata, schema='public', autoload_with=engine, resolve_fks=False)
    products = Table('products', metadata, schema='public', autoload_with=engine, resolve_fks=False)
    with engine.connect() as connection:
        order = connection.execute(select(orders).where(
            (orders.c.id == PROTECTED_ORDER) | (orders.c.order_no == PROTECTED_ORDER))).mappings().first()
        product = connection.execute(select(products).where(
            products.c.id == order['product_id'])).mappings().first() if order else None
    return hashlib.sha256(json.dumps([dict(order) if order else None,
        dict(product) if product else None], sort_keys=True, default=str).encode()).hexdigest()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        payload = json.loads(self.rfile.read(length))
        key = payload['operation_key']
        with EXTERNAL_LOCK:
            EXTERNAL_REQUESTS.append(key)
            result = EXTERNAL.setdefault(key, {'request_change_id': payload['change_id'],
                'resource_change_id': payload['change_id'],
                'evidence_fingerprint': hashlib.sha256(key.encode()).hexdigest()})
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        return


def install_adapter(m):
    import urllib.request
    def apply(db, row, *, direction):
        key = row.id + ':' + direction
        request = urllib.request.Request(CONTROL['url'], data=json.dumps({
            'operation_key': key, 'change_id': row.id}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=10) as response:
            proof = json.loads(response.read())
        if CONTROL.get('barrier_id') == row.id:
            CONTROL['reached'].set()
            CONTROL['release'].wait()
        return proof
    m.version_change_resources = {'apply': apply, 'prepare': lambda db, row: {'adapter': 'local-http'}}


def child_process(change_id, barrier_id, url, reached, release, start=None):
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    backend = str(Path(__file__).resolve().parents[1] / 'backend')
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from app import main as m
    from app import version_change_worker as worker
    CONTROL.update(url=url, barrier_id=barrier_id, reached=reached, release=release)
    install_adapter(m)
    if start is not None:
        start.wait()
    result = worker.process_once(worker_id='acceptance-' + str(os.getpid()))
    print(json.dumps({'change_id': change_id, 'processed': result}), flush=True)


def make_fixture(m, change_id, *, now):
    economic_old = {'billing_unit': 'month', 'product_id': 'p-' + change_id,
        'product_version_id': 'old-' + change_id, 'version_code': 'old',
        'subscription_months': 2, 'unit_price': '20.00', 'unit_cost': '10.00',
        'amount': '40.00', 'total_cost': '20.00', 'currency': 'CNY',
        'settlement_rule': {'mode': 'custom', 'id': '', 'version': 'fixture', 'rates': {
            'platform_rate': '10', 'provider_rate': '90', 'service_rate': '0',
            'expert_rate': '0', 'channel_rate': '0'}}}
    economic_target = dict(economic_old, product_version_id='new-' + change_id,
        version_code='new', unit_price='30.00', unit_cost='15.00', amount='60.00', total_cost='30.00')
    delivery = {'method': 'api', 'quota': {'rate_limit_per_minute': 60,
        'daily_quota': 100, 'monthly_quota': 1000, 'quota_amount': 1000}}
    with m.SessionLocal() as db:
        buyer, provider, uid = 'buyer-' + change_id, 'provider-' + change_id, 'user-' + change_id
        db.add_all([m.Enterprise(id=buyer, name=buyer, credit_code=buyer),
                    m.Enterprise(id=provider, name=provider, credit_code=provider),
                    m.User(id=uid, name=uid, password_hash='fixture', email=uid + '@invalid')])
        db.flush()
        product = m.Product(id='p-' + change_id, enterprise_id=provider, name='fixture',
            product_type='api', delivery_method='api', settlement_rule_mode='custom',
            settlement_rule_json='{}')
        db.add(product)
        db.flush()
        source = m.Order(id='o-' + change_id, order_no='QA-' + change_id, buyer_enterprise_id=buyer,
            buyer_user_id=uid, provider_enterprise_id=provider, product_id=product.id,
            buyer_name='fixture', product_name='fixture', product_version_id='old-' + change_id,
            product_version_code='old', main_status='completed', payment_status='paid',
            delivery_status='accepted', amount=40, paid_amount=40, subscription_months=2,
            snapshot_version=1, delivery_method_snapshot='api',
            economic_snapshot_json=json.dumps(economic_old), delivery_snapshot_json=json.dumps(delivery))
        db.add(source)
        db.flush()
        combo = m.subscription_terms['Combination'](id='c-' + change_id, enterprise_id=buyer,
            product_id=product.id, delivery_method='api', version_id='old-' + change_id,
            version_code='old', anchor_at=now - timedelta(days=1), generation=1,
            total_months=2, status='active')
        db.add(combo)
        db.flush()
        term = m.subscription_terms['Term'](id='t-' + change_id, order_id=source.id,
            combination_id=combo.id, generation=1, version_id='old-' + change_id,
            version_code='old', start_month=0, months=2, starts_at=now,
            ends_at=now + timedelta(days=60), status='active')
        db.add(term)
        change = m.VersionChangeRequest(id=change_id, source_order_id=source.id,
            enterprise_id=buyer, combination_id=combo.id,
            target_version_id='new-' + change_id, target_version_code='new',
            old_version_id='old-' + change_id, old_version_code='old', kind='upgrade',
            status='resource_pending', idempotency_key=change_id, request_hash='a' * 64,
            quote_json='{}', prepared_json='[]', resource_plan_json='{"adapter":"local-http"}',
            target_snapshots_json=json.dumps({term.id: {'economic': economic_target,
                'delivery': delivery}}), created_by='acceptance', created_at=now, updated_at=now)
        db.add(change)
        rule = economic_old['settlement_rule']
        for side, revenue, cost in [('old', Decimal('-40'), Decimal('-20')),
                ('target', Decimal('60'), Decimal('30'))]:
            db.add(m.version_change_ledger['Model'](change_id=change_id,
                request_change_id=change_id, source_order_id=source.id, combination_id=combo.id,
                side=side, revenue=revenue, cost=cost, profit=revenue-cost, currency='CNY',
                original_version_id='old-' + change_id, target_version_id='new-' + change_id,
                rule_json=json.dumps(rule, sort_keys=True, separators=(',', ':')),
                effective_at=now, status='pending', payload_fingerprint=(side[0] * 64)))
        db.commit()


def state(m, change_id):
    with m.SessionLocal() as db:
        request = db.get(m.VersionChangeRequest, change_id)
        ledgers = db.scalars(select(m.version_change_ledger['Model']).where(
            m.version_change_ledger['Model'].change_id == change_id)).all()
        revisions = db.scalars(select(m.subscription_terms['Revision']).where(
            m.subscription_terms['Revision'].change_id == change_id)).all()
        return {'status': request.status, 'owner': request.lease_owner,
            'lease_expires_at': request.lease_expires_at, 'resource_applied': request.resource_applied,
            'ledger': [row.status for row in ledgers], 'revisions': len(revisions),
            'combination_version': db.get(m.subscription_terms['Combination'], request.combination_id).version_code}


def phase_concurrent(m, ctx, url, run):
    change_id = run + '-concurrent'
    make_fixture(m, change_id, now=datetime.now(timezone.utc))
    gate = ctx.Event()
    m.engine.dispose()
    processes = [ctx.Process(target=child_process,
        args=(change_id, '', url, ctx.Event(), ctx.Event(), gate)) for _ in range(2)]
    for process in processes:
        process.start()
        ACTIVE_CHILDREN.add(process)
    gate.set()
    for process in processes:
        process.join(20)
        check(not process.is_alive() and process.exitcode == 0, 'Concurrent worker process failed or hung')
        ACTIVE_CHILDREN.discard(process)
    current = state(m, change_id)
    check(current['status'] == 'completed' and current['resource_applied'] and
          current['ledger'] == ['posted', 'posted'] and current['revisions'] == 1 and
          current['combination_version'] == 'new', 'Concurrent claim did not finalize exactly once')
    with EXTERNAL_LOCK:
        requests = sum(key.startswith(change_id + ':') for key in EXTERNAL_REQUESTS)
        effects = sum(key.startswith(change_id + ':') for key in EXTERNAL)
    check(requests == 1 and effects == 1,
          'Concurrent workers must issue exactly one HTTP apply and one unique effect')
    return {'status': 'passed', 'http_apply_requests': requests,
            'unique_external_effects': effects, 'term_revision_count': 1}


def phase_crash(m, ctx, url, run):
    change_id = run + '-crash'
    make_fixture(m, change_id, now=datetime.now(timezone.utc))
    reached, release = ctx.Event(), ctx.Event()
    m.engine.dispose()
    child = ctx.Process(target=child_process, args=(change_id, change_id, url, reached, release))
    child.start()
    ACTIVE_CHILDREN.add(child)
    check(reached.wait(30), 'Child did not reach post-external-write barrier')
    before = state(m, change_id)
    check(before['status'] == 'resource_pending' and before['owner'] and
          before['lease_expires_at'] is not None and before['ledger'] == ['pending', 'pending'] and
          before['revisions'] == 0 and not before['resource_applied'],
          'Crash barrier did not preserve durable lease and uncommitted financial state')
    from app import version_change_worker as worker_module
    skipped = worker_module.process_once(worker_id=run + '-skip')
    check(skipped is False, 'Second worker did not skip the active lease')
    os.kill(child.pid, signal.SIGKILL)
    child.join(10)
    check(child.exitcode == -signal.SIGKILL, 'Owned worker was not SIGKILLed')
    ACTIVE_CHILDREN.discard(child)
    after_kill = state(m, change_id)
    check(after_kill == before, 'SIGKILL left a partial database finalization')
    wait_seconds = max(0, (before['lease_expires_at'] - datetime.now(timezone.utc)).total_seconds()) + 1
    time.sleep(wait_seconds)
    install_adapter(m)
    recovered = worker_module.process_once(worker_id=run + '-recovery')
    check(recovered is True, 'Recovery worker did not claim the expired lease')
    final = state(m, change_id)
    check(final['status'] == 'completed' and final['resource_applied'] and
          final['ledger'] == ['posted', 'posted'] and final['revisions'] == 1 and
          final['combination_version'] == 'new', 'Recovery did not commit one complete finalization')
    with EXTERNAL_LOCK:
        requests = sum(key.startswith(change_id + ':') for key in EXTERNAL_REQUESTS)
        effects = sum(key.startswith(change_id + ':') for key in EXTERNAL)
    check(requests == 2 and effects == 1,
          'Crash replay must make two HTTP requests but produce one idempotent effect')
    return {'status': 'passed', 'lease_wait_seconds': round(wait_seconds),
            'active_lease_skip': True, 'http_apply_requests': requests,
            'unique_external_effects': effects,
            'ledger_rows_posted': 2, 'term_revision_count': 1}


def run():
    check('DATABASE_URL' in os.environ, 'Set DATABASE_URL to the dedicated PostgreSQL database')
    original_url = os.environ['DATABASE_URL']
    url = make_url(original_url)
    check(url.get_backend_name() == 'postgresql', 'PostgreSQL is required')
    admin = __import__('sqlalchemy').create_engine(url, pool_pre_ping=True)
    schema = 'qa_vc_crash_' + secrets.token_hex(8)
    created = False
    m = None
    before = fingerprint(admin)
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    CONTROL['url'] = 'http://127.0.0.1:%d/apply' % http.server_port
    try:
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        query = dict(url.query)
        query['options'] = (str(query.get('options', '')) + ' -csearch_path=' + schema).strip()
        os.environ['DATABASE_URL'] = url.set(query=query).render_as_string(hide_password=False)
        os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
        backend = str(Path(__file__).resolve().parents[1] / 'backend')
        sys.path.insert(0, backend)
        from app import main as imported
        m = imported
        with m.engine.connect() as connection:
            check(connection.scalar(text('select current_schema()')) == schema,
                  'App engine escaped the owned schema')
        m.Base.metadata.create_all(m.engine)
        install_adapter(m)
        ctx = multiprocessing.get_context('fork')
        run_id = secrets.token_hex(6)
        results = {'concurrent': phase_concurrent(m, ctx, CONTROL['url'], run_id),
                   'crash_recovery': phase_crash(m, ctx, CONTROL['url'], run_id)}
        return {'status': 'passed', 'scope': 'actual production version_change_worker.process_once + PostgreSQL; controlled local HTTP external adapter',
            'resource_fault_coverage': False, 'phases': results,
            'isolated_schema_removed': True,
            'cleanup': 'owned random schema dropped; protected public order and dependent product unchanged'}
    finally:
        os.environ['DATABASE_URL'] = original_url
        for child in list(ACTIVE_CHILDREN):
            if child.is_alive():
                child.terminate()
            child.join(timeout=10)
            if child.is_alive():
                os.kill(child.pid, signal.SIGKILL)
                child.join(timeout=5)
            ACTIVE_CHILDREN.discard(child)
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)
        if m:
            m.engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        with admin.connect() as connection:
            check(connection.scalar(text('select count(*) from information_schema.schemata '
                'where schema_name=:schema'), {'schema': schema}) == 0,
                'Owned schema cleanup failed')
        check(fingerprint(admin) == before, 'Protected public order or product changed')
        admin.dispose()


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(run(), sort_keys=True, default=str), flush=True)
    except AcceptanceCheckFailure as exc:
        print(json.dumps({'status': 'failed', 'error_type': 'AssertionError',
                          'detail': str(exc)}, sort_keys=True), flush=True)
        raise SystemExit(1) from None
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__},
                         sort_keys=True), flush=True)
        raise SystemExit(1) from None
