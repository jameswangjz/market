#!/usr/bin/env python3
"""Full isolated worker resource/funds matrix, never production app.main or vendors."""
import argparse
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import secrets
import select as io_select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

from fastapi import HTTPException

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app import version_change_worker as worker
from app.version_change_saga import RETRY_DELAY
from app.fixture_faults import (FixtureScope, reservation_metadata, release_fixture,
                               PROTECTED_ORDER, PROTECTED_PRODUCT)
from fixture_matrix_runtime import PrivateVendors, build_runtime, seed_fixture

CASES = ('upgrade', 'downgrade', 'no_payment', 'unpaid_expiry', 'prepare_failure',
         'apply_failure', 'invalid_proof', 'commit_failure', 'crash_replay',
         'restore_failure', 'upgrade_refund_failure', 'downgrade_refund_failure')
RESOURCES = ('api', 'model_api', 'tenant_access')


def matrix_engine(url, schema, sqlite_path=None):
    if sqlite_path:
        return create_engine('sqlite:///' + sqlite_path)
    if not re.fullmatch(r'qa_fixture_matrix_[0-9a-f]{32}', schema or ''):
        raise ValueError('invalid exact private matrix schema')
    engine = create_engine(url, isolation_level='READ COMMITTED', connect_args={
        'connect_timeout': 5, 'options': f'-c search_path={schema} -c statement_timeout=5000 -c lock_timeout=2000'})
    if engine.dialect.name != 'postgresql':
        raise ValueError('PostgreSQL required')
    return engine


def child(args):
    require(args.sqlite_path or args.at is None, 'logical-clock-forbidden-for-PostgreSQL')
    engine = matrix_engine(os.environ.get('TEST_FIXTURE_RESERVATION_PG_URL'), args.schema, args.sqlite_path)
    sessions = sessionmaker(engine)
    scope = FixtureScope.load(args.manifest)
    at = datetime.fromisoformat(args.at) if args.at else None
    module = build_runtime(sessions, os.environ['MATRIX_VENDOR_ENDPOINT'],
                           os.environ['MATRIX_VENDOR_SECRET'], scope, at)
    sys.modules['app.main'] = module
    original_apply = module.version_change_resources['apply']
    def apply(db, change, direction='target'):
        proof = original_apply(db, change, direction)
        if direction == 'target' and args.fault == 'proof':
            return dict(proof, request_change_id='unowned-proof')
        if direction == 'target' and args.fault == 'crash':
            print('matrix-external-applied', flush=True)
            time.sleep(20)  # Supervisor must kill this exact disposable process.
        return proof
    module.version_change_resources['apply'] = apply
    if args.fault == 'refund':
        @event.listens_for(module.Refund, 'before_insert')
        def refund_fault(*_):
            raise RuntimeError('private refund persistence fault')
    if args.fault == 'commit':
        triggered = False
        @event.listens_for(sessions, 'before_commit')
        def commit_fault(db):
            nonlocal triggered
            if not triggered and any(isinstance(row, module.VersionChangeRequest) and
                    row.status in ('completed', 'refund_pending', 'compensated') for row in db.identity_map.values()):
                triggered = True
                raise RuntimeError('private post-apply commit fault')
    stopped = threading.Event()
    def process():
        try:
            return worker.process_once(fixture_scope=scope,
                reservation_token=os.environ['QA_FIXTURE_RESERVATION_TOKEN'])
        finally:
            stopped.set()
    try:
        worker.run_worker(process, stopped, interval=0.1, batch=1, heartbeat=Path(args.heartbeat))
    finally:
        engine.dispose()


def require(condition, name):
    if not condition:
        raise AssertionError(name)


def wait_for_retry(due, *, clock=lambda: datetime.now(timezone.utc), sleeper=time.sleep):
    """Wait against real UTC in PG mode, without changing persisted deadlines."""
    if due is None:
        return 0
    started = time.monotonic()
    while True:
        remaining = (due - clock()).total_seconds()
        require(remaining <= 60, 'retry-wait-exceeds-private-case-bound')
        if remaining <= 0:
            return time.monotonic() - started
        sleeper(min(1, remaining))


def check_runtime():
    from zoneinfo import ZoneInfo
    import psycopg
    ZoneInfo('Asia/Shanghai')
    require(sys.version_info[:2] == (3, 12), 'tested-Python-3.12-required')
    require('app.main' not in sys.modules, 'production-main-imported')
    print(json.dumps({'python': sys.version.split()[0], 'dependencies': {
        name: version(name) for name in ('fastapi', 'pydantic', 'sqlalchemy', 'psycopg')},
        'production_main_imported': False, 'lupa_required': False}, sort_keys=True))


def terminate(process):
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=3)


def run_case(module, sessions, vendors, directory, schema, sqlite_path, resource, case):
    token = secrets.token_urlsafe(32)
    with sessions.begin() as db:
        scope = seed_fixture(module, db, vendors, resource, case, token)
    manifest = Path(directory) / (scope.change_id + '.json')
    descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(scope.__dict__, handle)
    state = vendors.fixtures[scope.change_id]
    Request, Combo = module.VersionChangeRequest, module.subscription_terms['Combination']
    Revision, Ledger = module.subscription_terms['Revision'], module.version_change_ledger['Model']
    baseline = None
    with sessions() as db:
        order = db.get(module.Order, scope.order_id)
        baseline = (order.economic_snapshot_json, order.delivery_snapshot_json, order.product_version_id)
    report = {'resource': resource, 'case': case, 'change_id': scope.change_id,
              'cycles': 0, 'checks': [], 'retry_wait_seconds': 0}
    started = time.monotonic()
    logical_at = module.now()
    repo = Path(__file__).resolve().parents[1]
    env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(repo / 'backend') + ':' + str(repo / 'scripts'),
        'QA_FIXTURE_RESERVATION_TOKEN': token, 'MATRIX_VENDOR_SECRET': vendors.secret,
        'MATRIX_VENDOR_ENDPOINT': vendors.endpoint}
    if not sqlite_path:
        env['TEST_FIXTURE_RESERVATION_PG_URL'] = os.environ['TEST_FIXTURE_RESERVATION_PG_URL']
    def snapshot():
        with sessions() as db:
            row = db.get(Request, scope.change_id)
            return {name: getattr(row, name) for name in ('status', 'retries', 'compensation_retries',
                'resource_plan_json', 'resource_applied', 'lease_owner', 'lease_expires_at', 'next_retry_at', 'funding_order_id')}
    def cycle(fault='none'):
        nonlocal logical_at
        before = snapshot()
        if sqlite_path:
            logical_at = max(logical_at + timedelta(seconds=1), before['next_retry_at'] or logical_at)
        else:
            due = before['next_retry_at']
            if due is not None:
                with sessions() as db:
                    previous_audit_at = db.scalar(select(module.AuditLog.created_at).where(
                        module.AuditLog.target_id == scope.change_id,
                        module.AuditLog.action == 'retry_version_change_phase')
                        .order_by(module.AuditLog.created_at.desc()).limit(1))
                if previous_audit_at is not None:
                    due = max(due, previous_audit_at + timedelta(seconds=RETRY_DELAY))
            report['retry_wait_seconds'] += wait_for_retry(due)
        command = [sys.executable, __file__, '--child', '--manifest', str(manifest), '--schema', schema,
            '--fault', fault,
            '--heartbeat', str(Path(directory) / (scope.change_id + '.heartbeat'))]
        if sqlite_path:
            command += ['--sqlite-path', sqlite_path, '--at', logical_at.isoformat()]
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        marker = ''
        try:
            if fault == 'crash':
                require(bool(io_select.select([process.stdout], [], [], 8)[0]), 'external-apply-marker-timeout')
                marker = process.stdout.readline().strip()
                require(marker == 'matrix-external-applied', 'external-apply-not-reached')
                process.kill()
            out, err = process.communicate(timeout=10)
            require(process.returncode == (-signal.SIGKILL if fault == 'crash' else 0), 'worker-subprocess-failed')
            require(token not in out + err and vendors.secret not in out + err, 'secret-output')
        finally:
            if process.poll() is None:
                terminate(process)
        report['cycles'] += 1
        require(report['cycles'] <= 14, 'case-cycle-bound')
        return snapshot()
    def until(status, fault='none', maximum=8):
        for _ in range(maximum):
            row = cycle(fault)
            if row['status'] == status:
                return row
        raise AssertionError('bounded-status-not-reached-' + status)
    def checkpoint():
        for _ in range(3):
            row = cycle()
            if row['resource_plan_json'] != '{}':
                require(not row['resource_applied'], 'plan-not-durable-before-apply')
                with sessions() as db:
                    require(not db.scalars(select(Revision).where(Revision.change_id == scope.change_id)).all(), 'premature-revision')
                report['checks'].append('durable-prepare-checkpoint')
                return
        raise AssertionError('prepare-checkpoint-missing')
    def retry_operator():
        user = SimpleNamespace(platform_role='platform_operator', is_active=True,
            activation_status='active', verified_status='verified', email='private-matrix', id='private-matrix')
        endpoint = next(route.endpoint for route in module.app.routes
                        if getattr(route, 'path', '') == '/api/version-changes/{change_id}/retry')
        with sessions() as db:
            endpoint(scope.change_id, SimpleNamespace(reason='private matrix repair'), user=user, db=db)
        report['checks'].append('real-operator-retry-handler')
    if case == 'unpaid_expiry':
        until('cancelled')
    elif case == 'prepare_failure':
        if resource == 'tenant_access':
            with sessions.begin() as db:
                config = db.scalar(select(module.SaaSIntegrationConfig).where(module.SaaSIntegrationConfig.product_id == scope.product_id))
                config.auth_mode = 'invalid-private-fault'
        else:
            state['faults']['prepare'] = True
        row = until('compensated')
        require(row['resource_plan_json'] == '{}' and row['retries'] == 3, 'prepare-exhaustion')
        report['checks'].append('prepare-exhaustion-no-apply')
    else:
        checkpoint()
        if case in ('apply_failure', 'restore_failure', 'upgrade_refund_failure'):
            state['faults']['target'] = True
            state['faults']['original'] = case == 'restore_failure'
            if case == 'apply_failure':
                row = until('compensated')
                require(row['retries'] == 3, 'apply-exhaustion')
            else:
                until('compensating')
                row = until('exception', 'refund' if case == 'upgrade_refund_failure' else 'none')
                require(row['compensation_retries'] == 3, 'compensation-exhaustion')
                with sessions() as db:
                    require(not db.scalars(select(module.Refund)).all() or
                        not db.scalars(select(module.Refund).where(module.Refund.reason == 'Version change ' + scope.change_id)).all(), 'failed-refund-persisted')
                    require(all(entry.status == 'pending' for entry in db.scalars(select(Ledger).where(Ledger.change_id == scope.change_id))), 'failure-ledger-not-rolled-back')
                state['faults'].clear()
                retry_operator()
                until('compensated')
                report['checks'].append('compensation-repaired-without-duplicate-refund')
        elif case == 'invalid_proof':
            until('compensating', 'proof')
            with sessions() as db:
                require(not db.scalars(select(Revision).where(Revision.change_id == scope.change_id)).all(), 'bad-proof-revision')
                require(all(entry.status == 'pending' for entry in db.scalars(select(Ledger).where(Ledger.change_id == scope.change_id))), 'bad-proof-posted-ledger')
            until('compensated')
            report['checks'].append('mismatched-proof-blocked-financial-posting')
        elif case in ('commit_failure', 'crash_replay'):
            row = cycle('commit' if case == 'commit_failure' else 'crash')
            require(bool(state['effects']) and state['effects'][-1] == 'target', 'external-state-not-target')
            with sessions() as db:
                require(not db.scalars(select(Revision).where(Revision.change_id == scope.change_id)).all(), 'failed-commit-revision')
                require(all(entry.status == 'pending' for entry in db.scalars(select(Ledger).where(Ledger.change_id == scope.change_id))), 'failed-commit-posted-ledger')
            if case == 'crash_replay':
                require(bool(row['lease_owner']), 'crash-lease-not-durable')
                # Exact disposable fixture reconciliation after reading owned
                # external state; never clear a lease on time alone in production.
                with sessions.begin() as db:
                    request = db.get(Request, scope.change_id)
                    request.lease_owner = ''
                    request.lease_expires_at = None
                report['checks'].append('real-kill-after-external-apply-before-db-commit')
            else:
                require(row['status'] == 'retrying' and row['retries'] == 1, 'commit-failure-not-retried')
                report['checks'].append('real-domain-post-apply-transaction-rollback')
            until('completed')
            require(state['effects'].count('target') >= 2, 'external-replay-missing')
        elif case == 'downgrade_refund_failure':
            until('refund_pending')
            row = until('exception', 'refund', maximum=3)
            require(row['resource_applied'] and row['retries'] == 3, 'downgrade-failure-lost-resource-proof')
            with sessions() as db:
                require(db.get(Combo, scope.combination_id).version_code == 'v2', 'downgrade-failure-lost-revision')
                require(not db.scalars(select(module.Refund).where(module.Refund.reason == 'Version change ' + scope.change_id)).all(), 'downgrade-failure-refund-persisted')
            retry_operator()
            until('completed')
            report['checks'].append('downgrade-refund-repair-preserves-revision')
        else:
            until('completed')
    terminal = snapshot()
    expected_compensated = case in ('prepare_failure', 'apply_failure', 'invalid_proof', 'restore_failure', 'upgrade_refund_failure')
    expected = 'cancelled' if case == 'unpaid_expiry' else 'compensated' if expected_compensated else 'completed'
    require(terminal['status'] == expected and not terminal['lease_owner'] and terminal['lease_expires_at'] is None, 'terminal-state-or-lease')
    with sessions() as db:
        request = db.get(Request, scope.change_id)
        entries = db.scalars(select(Ledger).where(Ledger.change_id == scope.change_id)).all()
        revisions = db.scalars(select(Revision).where(Revision.change_id == scope.change_id)).all()
        funds = db.scalars(select(module.VersionChangeFund).where(module.VersionChangeFund.change_id == scope.change_id)).all()
        refunds = db.scalars(select(module.Refund).where(module.Refund.reason == 'Version change ' + scope.change_id)).all()
        require(len(entries) == 2 and all(entry.status == ('posted' if expected == 'completed' else 'void') for entry in entries), 'terminal-ledger')
        if expected == 'completed':
            proof = json.loads(request.evidence_json)
            require(proof['request_change_id'] == proof['resource_change_id'] == scope.change_id and
                all(entry.resource_change_id == scope.change_id and
                    entry.resource_evidence_fingerprint == proof['evidence_fingerprint'] for entry in entries),
                'posted-ledger-proof-identity')
        require(len(revisions) == (1 if expected == 'completed' else 0), 'revision-count')
        require(db.get(Combo, scope.combination_id).version_code == ('v2' if expected == 'completed' else 'v1'), 'combination-version')
        order = db.get(module.Order, scope.order_id)
        require((order.economic_snapshot_json, order.delivery_snapshot_json, order.product_version_id) == baseline, 'original-order-mutated')
        if request.kind == 'upgrade':
            payment_funds = [item for item in funds if item.kind == 'payment']
            require(len(payment_funds) == 1, 'payment-fund-identity')
            funding = db.get(module.Order, request.funding_order_id)
            payment = db.get(module.Payment, payment_funds[0].payment_id)
            if expected_compensated:
                require(len(refunds) == 1 and len(funds) == 2 and
                    refunds[0].payment_id == payment.id and refunds[0].amount == payment_funds[0].amount and
                    funding.refunded_amount == funding.paid_amount and funding.payment_status == payment.status == 'refunded', 'upgrade-refund-link-and-amount')
            elif expected == 'cancelled':
                require(not refunds and funding.payment_status == payment.status == 'cancelled', 'unpaid-cancellation-funds')
            else:
                require(not refunds and funding.payment_status == payment.status == 'paid', 'success-payment-status')
        elif request.kind == 'downgrade':
            require(len(funds) == len(refunds) == 1 and funds[0].refund_id == refunds[0].id and
                    funds[0].payment_id == refunds[0].payment_id and funds[0].amount == refunds[0].amount,
                    'downgrade-exact-refund-link')
        else:
            require(not funds and not refunds, 'no-payment-created-funds')
        attempts = db.scalars(select(module.AuditLog).where(module.AuditLog.target_id == scope.change_id,
            module.AuditLog.action == 'retry_version_change_phase').order_by(module.AuditLog.created_at)).all()
        require(all((later.created_at - earlier.created_at).total_seconds() >= 10
                    for earlier, later in zip(attempts, attempts[1:])), 'persisted-retry-schedule')
    external_side = 'target' if expected == 'completed' else 'original'
    if resource == 'tenant_access':
        require(state['saas']['version'] == ('v2' if external_side == 'target' else 'v1'), 'external-saas-version')
    else:
        consumer = next(iter(state['consumers'].values()))
        policy = next(iter(state['hashes'].values()))
        route = state['target_route'] if external_side == 'target' else state['source_route']
        require(consumer['plugins']['market-gateway-quota']['route_key'] == route, 'external-consumer-route')
        require(all(period['route_key'] == route for period in json.loads(policy['subscription_periods'])) and
                policy['quota_scope'] == scope.combination_id and policy['generation'] == '1', 'external-quota-scope-and-restore')
    effects_before = len(state['effects'])
    cycle()
    require(len(state['effects']) == effects_before, 'terminal-replay-called-vendor')
    with sessions.begin() as db:
        release_fixture(db, module.__dict__, scope, token)
    report['checks'] += ['terminal-proof-ledger-revision-funds-invariants', 'original-order-immutable',
                         'external-resource-readback', 'terminal-replay-no-effects', 'owned-reservation-cleanup']
    report['status'] = expected
    report['wall_clock_seconds'] = round(time.monotonic() - started, 3)
    report['retry_wait_seconds'] = round(report['retry_wait_seconds'], 3)
    return report


def supervise(args):
    url = os.environ.get('TEST_FIXTURE_RESERVATION_PG_URL')
    require(args.local_smoke or bool(url), 'private-pod-PG-URL-required')
    schema = 'qa_fixture_matrix_' + uuid.uuid4().hex
    report = {'schema': None if args.local_smoke else schema,
        'database': 'sqlite-smoke' if args.local_smoke else 'postgresql-private-schema',
        'vendor_boundary': 'authenticated-isolated-HTTP-mocks', 'cases': [], 'result': 'failed',
        'scheduler': ('accelerated-local-smoke-only' if args.local_smoke
                      else 'real-UTC-persisted-deadlines-minimum-ten-second-audit-spacing')}
    control, engine, vendors, created = None, None, None, False
    with tempfile.TemporaryDirectory(prefix='fixture-full-matrix-') as directory:
        try:
            sqlite_path = str(Path(directory) / 'matrix.db') if args.local_smoke else None
            if not args.local_smoke:
                control = create_engine(url, connect_args={'connect_timeout': 5,
                    'options': '-c statement_timeout=5000 -c lock_timeout=2000'})
                require(control.dialect.name == 'postgresql', 'PostgreSQL-required')
                with control.begin() as db:
                    db.execute(text(f'CREATE SCHEMA {schema}'))
                created = True
            engine = matrix_engine(url, schema, sqlite_path)
            sessions = sessionmaker(engine)
            vendors = PrivateVendors(secrets.token_urlsafe(32))
            module = build_runtime(sessions, vendors.endpoint, vendors.secret)
            module.Base.metadata.create_all(engine)
            reservation_metadata.create_all(engine)
            with sessions.begin() as db:
                db.add(module.Product(id=PROTECTED_PRODUCT, product_type='api'))
                db.add(module.Order(id=PROTECTED_ORDER, order_no=PROTECTED_ORDER,
                    product_id=PROTECTED_PRODUCT, business_type='protected-private-sentinel'))
            def protected_snapshot():
                with sessions() as db:
                    return [[getattr(db.get(model, key), column.name) for column in model.__table__.columns]
                            for model, key in ((module.Product, PROTECTED_PRODUCT), (module.Order, PROTECTED_ORDER))]
            protected_before = protected_snapshot()
            print(json.dumps({'private_schema': report['schema'], 'database': report['database']}),
                  file=sys.stderr, flush=True)
            for resource in RESOURCES if args.resource == 'all' else (args.resource,):
                for case in CASES if args.case == 'all' else (args.case,):
                    item = run_case(module, sessions, vendors, directory, schema, sqlite_path, resource, case)
                    report['cases'].append(item)
                    print(json.dumps({'case_passed': item['case'], 'resource': resource}), file=sys.stderr, flush=True)
                    require(protected_snapshot() == protected_before, 'protected-private-sentinel-mutated')
            report['protected_private_sentinels_unchanged'] = True
            report['original_tables_used'] = False
            report['result'] = 'passed'
        except Exception as exc:
            report['error_type'] = type(exc).__name__
            if isinstance(exc, AttributeError):
                report['missing_attribute'] = exc.name
            if isinstance(exc, HTTPException):
                report['domain_error'] = exc.detail
            if isinstance(exc, AssertionError):
                report['failed_check'] = str(exc)
            raise
        finally:
            if vendors is not None:
                vendors.close()
            if engine is not None:
                engine.dispose()
            try:
                if created:
                    with control.begin() as db:
                        db.execute(text(f'DROP SCHEMA {schema} CASCADE'))
                    report['schema_dropped'] = True
            finally:
                if control is not None:
                    control.dispose()
                print(json.dumps(report, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resource', choices=('all',) + RESOURCES, default='all')
    parser.add_argument('--case', choices=('all',) + CASES, default='all')
    parser.add_argument('--local-smoke', action='store_true')
    parser.add_argument('--check-runtime', action='store_true')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    for name in ('manifest', 'schema', 'sqlite-path', 'at', 'heartbeat'):
        parser.add_argument('--' + name, help=argparse.SUPPRESS)
    parser.add_argument('--fault', choices=('none', 'proof', 'commit', 'crash', 'refund'), default='none', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.check_runtime:
            check_runtime()
        else:
            child(args) if args.child else supervise(args)
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
