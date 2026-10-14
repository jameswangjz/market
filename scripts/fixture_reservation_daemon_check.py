#!/usr/bin/env python3
"""Bounded real worker/HTTP subprocess check in a disposable private PG schema.

Synthetic HTTP prepare adapter only: does not validate apply/proof/funds/restore.
"""
import argparse
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
import os
import re
from pathlib import Path
import secrets
import select as io_select
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType
from urllib.parse import urlsplit
import uuid

from sqlalchemy import DateTime, Integer, String, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, mapped_column, sessionmaker

from app.fixture_faults import (FixtureScope, reservation_metadata, reservations,
                               reserve_fixture, release_fixture)
from app import version_change_worker as worker


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = 'fixture_orders'
    id = mapped_column(String, primary_key=True)
    order_no = mapped_column(String)
    product_id = mapped_column(String)
    buyer_enterprise_id = mapped_column(String)


class Combo(Base):
    __tablename__ = 'fixture_combinations'
    id = mapped_column(String, primary_key=True)
    product_id = mapped_column(String)
    enterprise_id = mapped_column(String)
    generation = mapped_column(Integer, default=1)
    status = mapped_column(String, default='active')
    version_id = mapped_column(String, default='old')
    version_code = mapped_column(String, default='v1')
    anchor_at = mapped_column(DateTime(timezone=True))
    total_months = mapped_column(Integer, default=3)


class Term(Base):
    __tablename__ = 'fixture_terms'
    id = mapped_column(String, primary_key=True)
    order_id = mapped_column(String)
    combination_id = mapped_column(String)
    generation = mapped_column(Integer, default=1)


class Change(Base):
    __tablename__ = 'fixture_changes'
    id = mapped_column(String, primary_key=True)
    source_order_id = mapped_column(String)
    enterprise_id = mapped_column(String)
    combination_id = mapped_column(String)
    status = mapped_column(String, default='resource_pending')
    next_retry_at = mapped_column(DateTime(timezone=True))
    lease_expires_at = mapped_column(DateTime(timezone=True))
    lease_owner = mapped_column(String, default='')
    created_at = mapped_column(DateTime(timezone=True))
    updated_at = mapped_column(DateTime(timezone=True))
    resource_plan_json = mapped_column(String, default='{}')
    retries = mapped_column(Integer, default=0)
    compensation_retries = mapped_column(Integer, default=0)
    compensation_started = mapped_column(Integer, default=0)
    last_error = mapped_column(String, default='')
    old_version_id = mapped_column(String, default='old')
    old_version_code = mapped_column(String, default='v1')


def private_engine(url, schema):
    if not isinstance(schema, str) or not re.fullmatch(r'qa_fixture_daemon_[0-9a-f]{32}', schema):
        raise ValueError('invalid private schema')
    engine = create_engine(url, isolation_level='READ COMMITTED', connect_args={
        'options': f'-c search_path={schema} -c statement_timeout=5000 -c lock_timeout=2000',
        'connect_timeout': 5})
    if engine.dialect.name != 'postgresql':
        raise ValueError('PostgreSQL required')
    return engine


def runtime(sessions):
    # Install only the narrow namespace needed by the real worker; never import
    # production app.main or initialize its services, queues, or background tasks.
    module = ModuleType('app.main')
    module.SessionLocal = sessions
    module.Order = Order
    module.now = lambda: datetime.now(timezone.utc)
    module.json = json
    module.version_change_saga = {'VersionChangeRequest': Change}
    module.subscription_terms = {'Combination': Combo, 'Term': Term}
    module.audit = lambda *args, **kwargs: None
    return module


def child(args):
    engine = private_engine(os.environ['TEST_FIXTURE_RESERVATION_PG_URL'], args.schema)
    sessions = sessionmaker(engine)
    module = runtime(sessions)
    sys.modules['app.main'] = module
    scope, excluded = worker.read_fixture_config(os.environ)
    stopped = threading.Event()
    deadline = time.monotonic() + 4
    def prepare(db, change):
        endpoint = urlsplit(os.environ['FIXTURE_CHECK_HTTP_URL'])
        if (endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1'
                or endpoint.query or endpoint.fragment or scope is None
                or endpoint.path != f'/fixtures/{scope.run_id}/{change.id}/health'):
            raise ValueError('non-exact loopback endpoint')
        connection = HTTPConnection(endpoint.hostname, endpoint.port, timeout=1)
        try:
            connection.request('GET', endpoint.path, headers={
                'Authorization': 'Bearer ' + os.environ['QA_FIXTURE_FAULT_SECRET']})
            print('http-prepare-dispatched', flush=True)
            response = connection.getresponse()
            if response.status != 200:
                response.read()
                raise ConnectionError('synthetic HTTP fixture unavailable')
            value = json.loads(response.read())
        finally:
            connection.close()
        if value.get('change_id') != change.id or value.get('mode') != 'healthy':
            raise ValueError('HTTP fixture identity mismatch')
        return {'kind': 'fixture-http-prepare', 'change_id': change.id}
    module.version_change_resources = {'prepare': prepare}
    def process():
        if time.monotonic() >= deadline:
            stopped.set()
            return False
        try:
            processed = worker.process_once(fixture_scope=scope, excluded_scopes=excluded,
                reservation_token=os.environ.get('QA_FIXTURE_RESERVATION_TOKEN'))
            with sessions() as db:
                prepared = bool(scope and db.get(Change, scope.change_id).resource_plan_json != '{}')
            if not processed or prepared or time.monotonic() >= deadline:
                stopped.set()
            return processed
        except ValueError:
            # Explicit rejection is observable to the supervising harness.
            print('reservation-rejected', flush=True)
            stopped.set()
            return False
    try:
        worker.run_worker(process, stopped, interval=0.1, batch=1,
                          heartbeat=Path(args.heartbeat))
    finally:
        engine.dispose()


def stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate(timeout=3)


def check(condition, label, checks):
    if not condition:
        raise AssertionError(label)
    checks.append(label)


def supervise():
    url = os.environ.get('TEST_FIXTURE_RESERVATION_PG_URL')
    if not url:
        raise ValueError('TEST_FIXTURE_RESERVATION_PG_URL required; disposable database only')
    schema = 'qa_fixture_daemon_' + uuid.uuid4().hex
    control = create_engine(url, connect_args={'connect_timeout': 5,
        'options': '-c statement_timeout=5000 -c lock_timeout=2000'})
    if control.dialect.name != 'postgresql':
        raise ValueError('PostgreSQL required')
    engine = None
    created = False
    checks = []
    report = {'schema': schema, 'checks': checks, 'scope': 'guards-and-http-prepare-only'}
    try:
        with control.begin() as db:
            db.execute(text(f'CREATE SCHEMA {schema}'))
        created = True
        engine = private_engine(url, schema)
        Base.metadata.create_all(engine)
        reservation_metadata.create_all(engine)
        sessions = sessionmaker(engine)
        module = runtime(sessions)
        root = 'qa-vcs-' + secrets.token_hex(6)
        scope = FixtureScope(root, uuid.uuid4().hex, root + '-order', root + '-p-api',
                             root + '-e-buyer', root + '-combo')
        token, secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        instant = module.now()
        sibling = uuid.uuid4().hex
        with sessions.begin() as db:
            db.add(Order(id=scope.order_id, order_no=root, product_id=scope.product_id,
                         buyer_enterprise_id=scope.enterprise_id))
            db.add(Combo(id=scope.combination_id, product_id=scope.product_id,
                         enterprise_id=scope.enterprise_id, anchor_at=instant - timedelta(days=1)))
            db.add(Term(id=root + '-term', order_id=scope.order_id,
                        combination_id=scope.combination_id))
            for change_id in (scope.change_id, sibling):
                db.add(Change(id=change_id, source_order_id=scope.order_id,
                    enterprise_id=scope.enterprise_id, combination_id=scope.combination_id,
                    created_at=instant))
            db.flush()
            reserve_fixture(db, module.__dict__, scope, token=token, seconds=120)
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='fixture-daemon-') as directory:
            manifest = Path(directory) / 'scope.json'
            descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w') as handle:
                json.dump(scope.__dict__, handle)
            env = {'PATH': os.environ.get('PATH', ''),
                'PYTHONPATH': str(repo / 'backend') + ':' + str(repo / 'scripts'),
                'TEST_FIXTURE_RESERVATION_PG_URL': url,
                'QA_FIXTURE_RESERVATION_TOKEN': token, 'QA_FIXTURE_FAULT_SECRET': secret}
            def start_worker(selection=True, owned_token=token, endpoint=''):
                child_env = env | {'QA_FIXTURE_RESERVATION_TOKEN': owned_token,
                                  'FIXTURE_CHECK_HTTP_URL': endpoint}
                if selection:
                    child_env['VERSION_CHANGE_FIXTURE_SCOPE_FILE'] = str(manifest)
                return subprocess.Popen([sys.executable, __file__, '--child',
                    '--schema', schema, '--heartbeat', str(Path(directory) / 'heartbeat')],
                    env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            def run(selection=True, owned_token=token, endpoint=''):
                process = start_worker(selection, owned_token, endpoint)
                try:
                    stdout, stderr = process.communicate(timeout=8)
                finally:
                    if process.poll() is None:
                        stop(process)
                check(process.returncode == 0, 'worker-subprocess-exited', checks)
                check(token not in stdout + stderr and owned_token not in stdout + stderr
                      and secret not in stdout + stderr,
                      'no-token-output', checks)
                return stdout
            run(selection=False)
            output = run(owned_token=secrets.token_urlsafe(32))
            check('reservation-rejected' in output, 'wrong-token-rejected', checks)
            with sessions() as db:
                check(all(row.resource_plan_json == '{}' and not row.lease_owner
                          for row in db.scalars(select(Change))), 'guards-left-rows-unchanged', checks)
            for mode in ('unavailable', 'healthy'):
                proxy = subprocess.Popen([sys.executable, str(repo / 'scripts' / 'isolated_fixture_fault.py'),
                    '--manifest', str(manifest), '--mode', mode], env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    check(bool(io_select.select([proxy.stdout], [], [], 5)[0]),
                          'proxy-started-' + mode, checks)
                    endpoint = proxy.stdout.readline().strip()
                    parsed = urlsplit(endpoint)
                    check(parsed.hostname == '127.0.0.1' and parsed.path.endswith(scope.change_id + '/health'),
                          'exact-loopback-' + mode, checks)
                    run(endpoint=endpoint)
                finally:
                    out, err = stop(proxy)
                    check(token not in out + err and secret not in out + err, 'proxy-no-token-output', checks)
                with sessions.begin() as db:
                    row = db.get(Change, scope.change_id)
                    check(not row.lease_owner and row.lease_expires_at is None,
                          'worker-lease-cleared-' + mode, checks)
                    if mode == 'unavailable':
                        check(row.status == 'retrying' and row.retries == 1 and row.resource_plan_json == '{}',
                              'http-503-durable-retry', checks)
                        row.next_retry_at = None  # Only this private test row; enable bounded recovery.
                    else:
                        check(row.resource_plan_json != '{}', 'http-healthy-prepare-recovered', checks)
                    check(db.get(Change, sibling).resource_plan_json == '{}', 'sibling-unprocessed-' + mode, checks)
            with sessions.begin() as db:
                db.execute(reservations.update().where(reservations.c.change_id == scope.change_id)
                    .values(expires_at=module.now() - timedelta(seconds=1)))
            run(selection=False)
            check('reservation-rejected' in run(), 'expired-dedicated-rejected', checks)
            with sessions.begin() as db:
                reserve_fixture(db, module.__dict__, scope, token=token, seconds=120,
                                require_existing=True)
                row = db.get(Change, scope.change_id)
                row.resource_plan_json = '{}'
                row.status = 'resource_pending'
                row.next_retry_at = None
            # Kill a real worker after its durable lease, during bounded HTTP I/O.
            proxy = subprocess.Popen([sys.executable, str(repo / 'scripts' / 'isolated_fixture_fault.py'),
                '--manifest', str(manifest), '--mode', 'timeout', '--delay', '3'], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            victim = None
            try:
                check(bool(io_select.select([proxy.stdout], [], [], 5)[0]), 'crash-proxy-started', checks)
                endpoint = proxy.stdout.readline().strip()
                check(endpoint.startswith('http://127.0.0.1:'), 'crash-loopback', checks)
                victim = start_worker(endpoint=endpoint)
                deadline = time.monotonic() + 4
                claimed = False
                dispatched = False
                while time.monotonic() < deadline and victim.poll() is None:
                    if io_select.select([victim.stdout], [], [], 0.02)[0]:
                        dispatched = victim.stdout.readline().strip() == 'http-prepare-dispatched'
                    with sessions() as db:
                        row = db.get(Change, scope.change_id)
                        claimed = bool(row.lease_owner and row.lease_expires_at)
                    if claimed and dispatched:
                        victim.kill()
                        break
                    time.sleep(0.02)
                check(claimed, 'real-worker-durable-lease-observed', checks)
                check(dispatched, 'real-worker-http-request-dispatched', checks)
                out, err = stop(victim)
                check(victim.returncode == -9, 'real-worker-killed', checks)
                check(token not in out + err and secret not in out + err, 'crash-no-token-output', checks)
            finally:
                if victim is not None:
                    stop(victim)
                stop(proxy)
            with sessions.begin() as db:
                row = db.get(Change, scope.change_id)
                check(bool(row.lease_owner) and row.resource_plan_json == '{}',
                      'crash-lease-remains-durable', checks)
                db.execute(reservations.update().where(reservations.c.change_id == scope.change_id)
                    .values(expires_at=module.now() - timedelta(seconds=1)))
                # Expire the request lease too: shared exclusion must still hold.
                row.lease_expires_at = module.now() - timedelta(seconds=1)
            run(selection=False)
            check('reservation-rejected' in run(), 'crash-expired-dedicated-rejected', checks)
            for action in ('renew', 'release'):
                with sessions.begin() as db:
                    rejected = False
                    try:
                        if action == 'renew':
                            reserve_fixture(db, module.__dict__, scope, token=token,
                                            require_existing=True)
                        else:
                            release_fixture(db, module.__dict__, scope, token)
                    except ValueError:
                        rejected = True
                    check(rejected, 'crash-outstanding-lease-blocks-' + action, checks)
            with sessions.begin() as db:
                row = db.get(Change, scope.change_id)
                check(bool(row.lease_owner) and row.resource_plan_json == '{}',
                      'crash-quarantine-no-processing', checks)
                # Exact private fixture only: HTTP prepare has no external writes.
                # This explicit reconciliation is never used on production rows.
                row.lease_owner = ''
                row.lease_expires_at = None
                db.flush()
                release_fixture(db, module.__dict__, scope, token)
                check(db.scalar(select(reservations.c.change_id)) is None, 'owned-cleanup', checks)
        report['result'] = 'passed'
    except Exception as exc:
        report['result'] = 'failed'
        report['error_type'] = type(exc).__name__
        if isinstance(exc, AssertionError):
            report['failed_check'] = str(exc)
        raise
    finally:
        if engine is not None:
            engine.dispose()
        cleanup_failed = False
        try:
            if created:
                with control.begin() as db:
                    db.execute(text(f'DROP SCHEMA {schema} CASCADE'))
                report['schema_dropped'] = True
        except Exception as exc:
            cleanup_failed = True
            report['schema_dropped'] = False
            report['result'] = 'failed'
            report['cleanup_error_type'] = type(exc).__name__
        finally:
            control.dispose()
            print(json.dumps(report, sort_keys=True), flush=True)
        if cleanup_failed:
            raise RuntimeError('private schema cleanup failed') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--schema', help=argparse.SUPPRESS)
    parser.add_argument('--heartbeat', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        child(args) if args.child else supervise()
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
