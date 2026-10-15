"""FILE refunds on real PostgreSQL in the existing owned-schema harness.

No startup or public writes. Storage is explicitly mocked; seeded clean-scan
metadata is not scan/upload evidence. Funds use production simulated legs.
Local: R3_FILE_DATABASE_URL=... python -B scripts/r3_file_refund_pg.py --output NEW.json
Remote: --pod market-api-... uses the existing private bounded source transport.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
from decimal import Decimal
import hashlib
import importlib
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import r3_refund_cross_version_pg_acceptance as cross

base, transport, fixture = cross.base, cross.transport, cross.fixture
SOURCE_FILES = tuple(dict.fromkeys((*cross.SOURCE_FILES, 'refund_execution.py',
    'refund_execution_worker.py', 'refund_execution_resources.py', 'refund_file_resources.py',
    'refund_settlement.py', 'order_economics.py', 'online_delivery.py', 'settlement_metering.py')))
CASES = ('file_two_partials_delayed_settlement', 'refund_budget_two_connections',
         'download_recovery_serialization', 'funds_failure_retry', 'proof_phase_rollback')
CONTENT = b'Owned FILE refund acceptance payload. No real customer data.\n'
CHECKSUM = hashlib.sha256(CONTENT).hexdigest()


class OwnedStorage:
    """Only the exact seeded object can be read. Never connects to MinIO."""
    def __init__(self, name):
        self.name, self.reads = name, 0
        self.on_read = None
        self.lock = threading.Lock()

    def get_object(self, bucket, name):
        base.check(bucket == 'owned-file-refund' and name == self.name, 'Unknown mock object identity')
        owner = self

        class Body(io.BytesIO):
            def read(self, *args):
                if owner.on_read:
                    owner.on_read()
                with owner.lock:
                    owner.reads += 1
                return super().read(*args)

            def release_conn(self):
                pass
        return Body(CONTENT)


def seed(m):
    headers = fixture.seed(m)
    token = secrets.token_hex(12)
    fid, version = 'file-' + token, 'version-' + token
    object_name = 'qa/file-refund/' + token
    from sqlalchemy import select
    with m.SessionLocal.begin() as db:
        cross.owned(db)
        base.check(db.get(m.FileObject, fid) is None and db.scalar(select(m.FileObject.id).where(
            m.FileObject.object_name == object_name)) is None, 'Owned file identity collision')
        db.add(m.ProductReleaseVersion(id=version, product_id='owned-product', version_code='v1',
            price=100, cost=Decimal('33.33'), status='active'))
        db.flush()
        db.add(m.FileObject(id=fid, owner_id='owned-provider', product_id='owned-product', version_id=version,
            object_name=object_name, original_name='owned-refund.txt', size=len(CONTENT), checksum=CHECKSUM,
            content_type='text/plain', file_role='product_data', status='active', scan_status='clean',
            scan_report=json.dumps(dict(presidio_status='available', fixture_seed_not_scan=True))))
        db.flush()
        economic = dict(schema_version=1, product_id='owned-product', product_version_id=version,
            version_code='v1', billing_unit='order', subscription_months=1, unit_price='100.00',
            unit_cost='33.33', amount='100.00', total_cost='33.33', currency='CNY',
            settlement_rule=dict(mode='global', version='owned-file-rates-v1', rates=dict(
                platform_rate='10', provider_rate='60', service_rate='20', expert_rate='5', channel_rate='5')))
        delivery = dict(method='file', download_limit=0, files=[dict(id=fid, checksum=CHECKSUM)])
        orders = {}
        for name in (*CASES, 'other-order'):
            oid, number = fixture.owned_order_identity('file-' + token + '-' + name)
            orders[name] = oid
            db.add(m.Order(id=oid, order_no=number, buyer_user_id='owned-buyer',
                buyer_enterprise_id='owned-buyer', provider_enterprise_id='owned-provider',
                product_id='owned-product', product_version_id=version, product_version_code='v1',
                product_name='Owned FILE', buyer_name='Owned buyer', snapshot_version=1,
                subscription_months=1, delivery_method_snapshot='file', amount=100, paid_amount=100,
                unit_price_snapshot=100, unit_cost_snapshot=Decimal('33.33'), total_cost_snapshot=Decimal('33.33'),
                payment_status='paid', main_status='completed', delivery_status='accepted', refunded_amount=0,
                economic_snapshot_json=json.dumps(economic), delivery_snapshot_json=json.dumps(delivery)))
            db.flush()
            db.add(m.Payment(id='pay-' + oid, order_id=oid, payment_no='PAY-' + oid,
                             status='paid', amount=100, paid_at=m.now()))
    m._file_probe = dict(orders=orders, file_id=fid, object_name=object_name)
    return headers


def wait_for_pg_lock(m, pid):
    """Observe a real server lock wait, not an elapsed-time race assumption."""
    from sqlalchemy import text
    deadline = time.monotonic() + 1.2
    while time.monotonic() < deadline:
        with m.SessionLocal() as db:
            cross.owned(db)
            waiting = db.scalar(text('SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid'), dict(pid=pid))
        if waiting == 'Lock':
            return
        time.sleep(.01)
    raise base.AcceptanceFailure('Expected real PostgreSQL row-lock wait')


def exercise(m, client, headers):
    from sqlalchemy import select, text
    from starlette.requests import Request
    schema = importlib.import_module(m.__package__ + '.refund_schema')
    worker = importlib.import_module(m.__package__ + '.refund_execution_worker')
    hooks, resources = m.refund_execution, m.refund_file_resources
    Execution, Leg, Case = hooks['Execution'], hooks['PaymentLeg'], m.refund_workflow['Case']
    owned = m._file_probe
    storage = OwnedStorage(owned['object_name'])

    def call(method, path, actor='admin', expected=200, **kwargs):
        response = client.request(method, path, headers=headers[actor], **kwargs)
        base.check(response.status_code == expected, 'Owned FILE ASGI status mismatch')
        return response.json() if expected < 400 else None

    def request(name, amount, key, expected=200):
        return call('POST', '/api/orders/' + owned['orders'][name] + '/refund-requests',
            actor='buyer', expected=expected, params=dict(enterprise_id='owned-buyer', business_view='buyer'),
            json=dict(amount=amount, reason='Owned FILE refund request', idempotency_key=key + '-request'))

    def approve(name, amount, key):
        row = request(name, amount, key)
        row = call('POST', '/api/refunds/' + row['id'] + '/manual-assessments', json=dict(
            amount=amount, refundable_limit='100.00', used_calls=0, reason='Owned documented manual determination',
            evidence_refs=['owned-fixture-assessment:' + key], expected_version=row['version'],
            idempotency_key=key + '-assessment'))
        row = call('POST', '/api/refunds/' + row['id'] + '/review', json=dict(decision='approve',
            reason='Owned manual approval', expected_version=row['version'], idempotency_key=key + '-review'))
        base.check(row['execution_available'], 'Trusted FILE execution must be available')
        return row['id']

    def execute(rid, key, operation='execute'):
        with m.SessionLocal() as db:
            version = db.get(Case, rid).version
        call('POST', '/api/refunds/' + rid + '/' + operation, json=dict(expected_version=version,
            reason='Owned simulated execution', idempotency_key=key))

    def state(rid):
        with m.SessionLocal() as db:
            row = db.scalar(select(Execution).where(Execution.refund_id == rid))
            return row.status if row else None

    def drive(rid, target='completed'):
        for _ in range(12):
            if state(rid) == target:
                return
            base.check(worker.process_once(ns=vars(m)), 'Expected real FILE worker phase')
        raise base.AcceptanceFailure('FILE worker did not reach bounded target')

    def download(name, expected):
        response = client.get('/api/files/' + owned['file_id'] + '/download', headers=headers['buyer'],
                              params=dict(order_id=owned['orders'][name]))
        base.check(response.status_code == expected, 'Owned FILE download status mismatch')
        if expected == 200:
            base.check(len(response.content) == len(CONTENT) and
                       hashlib.sha256(response.content).hexdigest() == CHECKSUM, 'Download bytes mismatch')

    def retained(name):
        with m.SessionLocal() as db:
            file = db.get(m.FileObject, owned['file_id'])
            base.check(file.status == 'active' and file.object_name == storage.name
                and file.size == len(CONTENT) and file.checksum == CHECKSUM, 'Source file changed')
            other = db.get(m.Order, owned['orders']['other-order'])
            base.check(other.refunded_amount == 0 and other.payment_status == 'paid', 'Other order mutated')
        download(name, 409)
        download('other-order', 200)

    def two_partials():
        name = CASES[0]
        download(name, 200)
        first = approve(name, '33.33', 'file-first')
        execute(first, 'file-first-execute')
        drive(first, 'resources_applied')
        with m.SessionLocal() as db:
            base.check(all(row.status == 'prepared' for row in db.scalars(select(Leg).where(
                Leg.refund_id == first))), 'Funds ran before FILE resources')
            base.check(db.get(m.Order, owned['orders'][name]).refunded_amount == 0, 'Premature funds completion')
            base.check(db.scalar(select(resources['Recovery']).where(resources['Recovery'].refund_id == first))
                       is not None, 'FILE durable receipt missing')
        retained(name)
        drive(first)
        second = approve(name, '66.67', 'file-second')
        execute(second, 'file-second-execute')
        drive(second)
        with m.SessionLocal.begin() as db:
            settlements = [m.refund_settlement['negative_settlement'](db, db.get(m.Refund, rid))
                           for rid in (first, second)]
            base.check([row.cost_amount for row in settlements] == [Decimal('-11.11'), Decimal('-22.22')],
                       'Cumulative frozen cost reversal mismatch')
            base.check(sum(row.net_amount for row in settlements) == Decimal('-100.00') and
                       sum(row.cost_amount for row in settlements) == Decimal('-33.33'), 'Final penny lost')
            base.check(m.refund_settlement['negative_settlement'](db, db.get(m.Refund, first)).id == settlements[0].id,
                       'Delayed negative settlement replay duplicated identity')
            execution = db.scalar(select(Execution).where(Execution.refund_id == first))
            proof = json.loads(execution.proof_json)
            db.expunge(execution)
        base.check(resources['apply'](None, execution) == proof, 'Historical resource receipt changed')
        request(name, '.01', 'file-third-overflow', expected=409)
        retained(name)
        return dict(resources_before_funds=True, partial_refunds=2, negative_costs=['-11.11', '-22.22'],
                    total_cost='-33.33', old_receipt_stable=True, over_refund=409, other_order_download=200)

    def budget_race():
        barrier = threading.Barrier(2, timeout=5)
        oid = owned['orders'][CASES[1]]
        def attempt(index):
            with m.SessionLocal() as db:
                cross.owned(db)
                pid = db.scalar(text('SELECT pg_backend_pid()'))
                user = db.get(m.User, 'owned-buyer')
                body = schema.RefundRequestBody(amount=Decimal('60'), reason='Owned budget race',
                                               idempotency_key='file-budget-' + str(index))
                req = Request(dict(type='http', method='POST', path='/api/orders/' + oid + '/refund-requests',
                    query_string=b'enterprise_id=owned-buyer&business_view=buyer', headers=[]))
                barrier.wait()
                try:
                    m.refund_workflow['create'](db, user, oid, body, req)
                    db.commit()
                    return 200, pid
                except m.HTTPException as exc:
                    db.rollback()
                    return exc.status_code, pid
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, i) for i in range(2)]
            rows = [future.result(timeout=12) for future in futures]
        base.check(len({row[1] for row in rows}) == 2 and sorted(row[0] for row in rows) == [200, 409],
                   'Real PG refund budget race did not serialize')
        with m.SessionLocal() as db:
            refunds = db.scalars(select(m.Refund).where(m.Refund.order_id == oid)).all()
            base.check(len(refunds) == 1 and refunds[0].amount == Decimal('60'), 'Budget over-reserved')
        return dict(independent_pg_connections=2, statuses=[200, 409], reserved='60.00')

    def serialization():
        name = CASES[2]
        rid = approve(name, '20.00', 'file-lock')
        execute(rid, 'file-lock-execute')
        entered, release = threading.Event(), threading.Event()
        worker_pid, worker_pid_ready = [], threading.Event()
        download_pids = []

        def dependency():
            with m.SessionLocal() as db:
                cross.owned(db)
                download_pids.append(db.scalar(text('SELECT pg_backend_pid()')))
                yield db

        @contextmanager
        def sessions(*args, **kwargs):
            with m.SessionLocal(*args, **kwargs) as db:
                cross.owned(db)
                worker_pid.append(db.scalar(text('SELECT pg_backend_pid()')))
                if len(worker_pid) >= 2:
                    worker_pid_ready.set()
                yield db

        def hold_read():
            entered.set()
            base.check(release.wait(4), 'Download hold timed out')

        original_dependency = dict(m.app.dependency_overrides)
        m.app.dependency_overrides[m.db_session] = dependency
        try:
            storage.on_read = hold_read
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(download, name, 200)
                try:
                    base.check(entered.wait(3), 'Download did not reach owned storage')
                    second = pool.submit(worker.process_once, sessions, ns=vars(m))
                    base.check(worker_pid_ready.wait(3), 'Worker second connection missing')
                    base.check(download_pids[-1] != worker_pid[-1], 'Independent download/worker connections required')
                    wait_for_pg_lock(m, worker_pid[-1])
                finally:
                    release.set()
                first.result(timeout=8)
                base.check(second.result(timeout=8), 'Worker transition missing')
            storage.on_read = None
            # Reverse ordering: actual FILE apply owns the lock before HTTP authorization.
            entered.clear(); release.clear(); download_pids.clear()
            original_lock = m.refund_workflow['lock_order']
            recovery_pid = []
            def hold_recovery(db, oid):
                order = original_lock(db, oid)
                recovery_pid.append(db.scalar(text('SELECT pg_backend_pid()')))
                entered.set()
                base.check(release.wait(4), 'Recovery hold timed out')
                return order
            with m.SessionLocal() as db:
                execution = db.scalar(select(Execution).where(Execution.refund_id == rid))
                db.expunge(execution)
            with patch.dict(m.refund_workflow, lock_order=hold_recovery), ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(resources['apply'], None, execution)
                try:
                    base.check(entered.wait(3), 'FILE apply did not obtain order lock')
                    second = pool.submit(download, name, 409)
                    deadline = time.monotonic() + 1
                    while not download_pids and time.monotonic() < deadline:
                        time.sleep(.01)
                    base.check(bool(download_pids) and download_pids[-1] != recovery_pid[-1],
                               'Independent recovery/download connections required')
                    wait_for_pg_lock(m, download_pids[-1])
                finally:
                    release.set()
                proof = first.result(timeout=8)
                second.result(timeout=8)
        finally:
            storage.on_read = None
            release.set()
            m.app.dependency_overrides.clear()
            m.app.dependency_overrides.update(original_dependency)
        drive(rid)
        with m.SessionLocal() as db:
            base.check(json.loads(db.scalar(select(Execution).where(Execution.refund_id == rid)).proof_json) == proof,
                       'Worker did not replay real FILE receipt')
        retained(name)
        return dict(orderings_tested=2, server_lock_waits_observed=2, independent_connections=True,
                    authorized_first_download=200, recovery_first_download=409)

    def funds_failure():
        name = CASES[3]
        rid = approve(name, '20.00', 'file-funds')
        execute(rid, 'file-funds-execute')
        drive(rid, 'funds_pending')
        with m.SessionLocal.begin() as db:
            db.get(m.Payment, 'pay-' + owned['orders'][name]).status = 'failed'
            receipt = db.scalar(select(resources['Recovery']).where(resources['Recovery'].refund_id == rid)).proof_json
        drive(rid, 'failed')
        retained(name)
        with m.SessionLocal.begin() as db:
            db.get(m.Payment, 'pay-' + owned['orders'][name]).status = 'paid'
        execute(rid, 'file-funds-retry', 'retry')
        drive(rid)
        with m.SessionLocal() as db:
            rows = db.scalars(select(resources['Recovery']).where(resources['Recovery'].refund_id == rid)).all()
            base.check(len(rows) == 1 and rows[0].proof_json == receipt, 'Funds retry changed FILE recovery')
        retained(name)
        return dict(funds_failure_observed=True, entitlement_not_restored=True, receipt_stable=True, retry_completed=True)

    def rollback():
        name = CASES[4]
        rid = approve(name, '20.00', 'file-rollback')
        execute(rid, 'file-rollback-execute')
        drive(rid, 'resources_pending')
        publish = hooks['publish']
        def fault(db, execution, refund, case, action, before):
            if action == 'resources_applied':
                raise RuntimeError('Owned injected phase commit failure')
            return publish(db, execution, refund, case, action, before)
        with patch.dict(hooks, publish=fault):
            base.check(worker.process_once(ns=vars(m)), 'Expected rollback phase')
        base.check(state(rid) == 'failed', 'Phase fault did not fail')
        with m.SessionLocal() as db:
            execution = db.scalar(select(Execution).where(Execution.refund_id == rid))
            receipt = db.scalar(select(resources['Recovery']).where(resources['Recovery'].refund_id == rid)).proof_json
            base.check(execution.proof_json is None and all(row.status == 'prepared' for row in
                db.scalars(select(Leg).where(Leg.refund_id == rid))), 'Failed phase committed proof or funds')
        retained(name)
        execute(rid, 'file-rollback-retry', 'retry')
        drive(rid)
        with m.SessionLocal() as db:
            base.check(db.scalar(select(Execution).where(Execution.refund_id == rid)).proof_json == receipt,
                       'Rollback retry changed durable proof')
        return dict(proof_phase_rolled_back=True, durable_receipt_preserved=True, funds_before_resources=0,
                    retry_completed=True)

    results = {}
    with patch.dict(vars(m), MINIO_ENDPOINT='owned-mock:9000', MINIO_BUCKET='owned-file-refund',
                    Minio=lambda *_a, **_kw: storage):
        for name, run in zip(CASES, (two_partials, budget_race, serialization, funds_failure, rollback)):
            try:
                results[name] = dict(status='passed', **run())
            except Exception as exc:
                results[name] = base.safe_error(exc)
                break
    return results


def remote_run(encoded, manifest):
    base.check(all(name in manifest for name in SOURCE_FILES), 'FILE source inventory incomplete')
    with patch.object(cross, 'seed', seed), patch.object(cross, 'exercise', exercise), \
            patch.object(cross, 'CASE_NAMES', CASES), patch.object(cross, 'SOURCE_FILES', SOURCE_FILES):
        result = cross.remote_run(encoded, manifest)
    result.update(scope='Owned schema real PostgreSQL FILE workflow, worker, row locks and ASGI downloads',
        incremental_scope='FILE ordinary refunds with manual assessment and frozen order economics',
        external_adapters=dict(storage='Exact in-memory owned object; no MinIO network',
                               funds='Production simulated payment legs; no bank movement'),
        scan_evidence='Seeded clean/Presidio metadata only; no external upload, antivirus or PII scan performed',
        gaps=['No actual MinIO, external upload/scan, bank, ingress, browser or deployment evidence'])
    return result


def run(raw):
    from sqlalchemy.engine import make_url
    url = make_url(raw)
    base.check(url.get_backend_name() == 'postgresql' and not url.query, 'Owned PostgreSQL URL required')
    base.check(url.host in ('127.0.0.1', 'localhost', '::1') or bool(url.host and url.host.startswith('/')),
               'Local PostgreSQL only; use reviewed pod transport for remote execution')
    encoded, manifest = transport.pack_workingtree(Path(__file__).resolve().parents[1])
    with patch.dict(os.environ, DATABASE_URL=raw):
        return remote_run(encoded, manifest)


def pod_run(pod):
    base.check(bool(re.fullmatch(r'market-api-[a-z0-9-]+', pod)), 'Invalid API pod')
    base.check(bool(os.getenv('SSHPASS')), 'Private SSHPASS environment required')
    encoded, manifest = transport.pack_workingtree(Path(__file__).resolve().parents[1])
    base.check(all(name in manifest for name in SOURCE_FILES), 'FILE source inventory incomplete')
    payload = 'import sys,types\n'
    for name in ('r3_backend_acceptance_pg', 'r3_backend_acceptance_concurrency',
                 'r3_after_sales_group_pg_acceptance', 'r3_refund_workflow_pg_acceptance',
                 'r3_refund_cross_version_pg_acceptance'):
        source = Path(__file__).with_name(name + '.py').read_text()
        payload += ('m=types.ModuleType(' + repr(name) + ');sys.modules[m.__name__]=m;exec(compile('
                    + repr(source) + ',m.__name__,"exec"),m.__dict__)\n')
    payload += "__name__='file_refund_remote'\n" + Path(__file__).read_text()
    payload += '\nprint(json.dumps(remote_run(' + repr(encoded) + ',' + repr(manifest) + ')))\n'
    command = ('kubectl --request-timeout=5s -n market exec -i ' + pod
               + ' -- env MESSAGE_CENTER_DISABLE_BACKGROUND=true PYTHONDONTWRITEBYTECODE=1 python -B -')
    process = subprocess.run(['sshpass', '-e', 'ssh', '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1',
        '-o', 'LogLevel=ERROR', 'root@192.168.10.10', command], input=payload,
        capture_output=True, text=True, timeout=180)
    base.check(process.returncode == 0, 'Bounded FILE transport failed; private diagnostics suppressed')
    return json.loads(process.stdout)


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pod')
    parser.add_argument('--database-url-env', default='R3_FILE_DATABASE_URL')
    parser.add_argument('--output', type=Path, help='New sanitized JSON evidence; overwrite forbidden')
    args = parser.parse_args()
    with ExitStack() as stack:
        output = None
        try:
            if args.output:
                output = stack.enter_context(args.output.open('x', encoding='utf-8'))
            result = pod_run(args.pod) if args.pod else run(os.environ[args.database_url_env])
        except Exception as exc:
            result = base.safe_error(exc)
        encoded = json.dumps(result, sort_keys=True)
        if output:
            output.write(encoded + '\n')
        print(encoded)
    return 0 if result.get('status') == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
