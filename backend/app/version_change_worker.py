"""Crash-recoverable worker for version-change resource and refund phases."""
import importlib
import logging
import os
from pathlib import Path
import signal
import threading
import time
from datetime import timedelta

from sqlalchemy import select, or_

from .version_change_saga import MAX_ATTEMPTS, RETRY_DELAY

HEARTBEAT = Path('/tmp/version-change-heartbeat')
logger = logging.getLogger(__name__)
LEASE_SECONDS = 60


def process_once(SessionLocal=None, *, worker_id=None, now=None, fixture_scope=None,
                 excluded_scopes=(), reservation_token=None):
    from .fixture_faults import (FixtureScope, registry_present, registry_exclusion,
                                 reservation_active)
    if fixture_scope is not None and not isinstance(fixture_scope, FixtureScope):
        raise ValueError('explicit fixture scope required')
    if not isinstance(excluded_scopes, tuple) or any(
            not isinstance(scope, FixtureScope) for scope in excluded_scopes):
        raise ValueError('explicit exclusion scopes required')
    if fixture_scope is not None and excluded_scopes:
        raise ValueError('selection and exclusion cannot be combined')
    main = importlib.import_module('app.main')
    SessionLocal = SessionLocal or main.SessionLocal
    saga = main.version_change_saga
    ns = main.__dict__
    hooks = saga
    Request = hooks['VersionChangeRequest']
    instant = now() if now else ns['now']()
    owner = worker_id or f'{os.getpid()}:{threading.get_ident()}'
    filters = []
    if fixture_scope is not None:
        filters.append(fixture_scope.predicate(ns, Request))
    if excluded_scopes:
        filters.append(Request.id.not_in(tuple(scope.change_id for scope in excluded_scopes)))
    with SessionLocal() as db:
        registered = registry_present(db)
        if fixture_scope is not None and (not registered or reservation_token is None):
            raise ValueError('dedicated worker requires an owned reservation')
        if registered and fixture_scope is None:
            filters.append(registry_exclusion(Request))
        row = db.scalar(select(Request).where(
            *filters,
            Request.status.in_(('pending_payment','resource_pending','compensating','refund_pending','retrying')),
            or_(Request.next_retry_at.is_(None), Request.next_retry_at <= instant),
            or_(Request.lease_expires_at.is_(None), Request.lease_expires_at <= instant)
        ).order_by(Request.created_at).with_for_update(skip_locked=True).limit(1))
        if row is None:
            db.rollback(); return False
        # Recheck after acquiring the request lock using a fresh PG statement.
        if fixture_scope is None and not registered and registry_present(db):
            filters.append(registry_exclusion(Request))
        if db.scalar(select(Request.id).where(Request.id == row.id, *filters)) is None:
            db.rollback(); return False
        if fixture_scope is not None:
            reservation_active(db, fixture_scope, reservation_token)
        row.lease_owner = owner
        row.lease_expires_at = instant + timedelta(seconds=LEASE_SECONDS)
        row.updated_at = instant
        change_id, phase = row.id, row.status
        db.commit()

    # The intent/lease is durable before crossing the resource boundary.
    with SessionLocal() as db:
        row = db.scalar(select(Request).where(Request.id == change_id,
            Request.lease_owner == owner, *filters).with_for_update())
        if row is None:
            db.rollback(); return False
        if fixture_scope is not None:
            reservation_active(db, fixture_scope, reservation_token)
        try:
            resource = ns.get('version_change_resources')
            if not isinstance(resource, dict):
                raise RuntimeError('resource adapter unavailable')
            if phase == 'pending_payment':
                funding = db.get(ns['Order'], row.funding_order_id) if row.funding_order_id else None
                if funding and funding.payment_status == 'paid':
                    row.status = 'resource_pending'; row.next_retry_at = instant
                else:
                    ns['version_change_ledger']['void'](db, row.id, 'version change quote expired before payment',
                                                        'version-change-worker')
                    if funding:
                        funding.main_status = 'cancelled'; funding.payment_status = 'cancelled'
                        payment = db.scalar(select(ns['Payment']).where(ns['Payment'].order_id == funding.id))
                        if payment and payment.status != 'paid':
                            payment.status = 'cancelled'
                    row.status = 'cancelled'; row.completed_at = instant
                    row.next_retry_at = None
                ns['audit'](db, 'version-change-worker', 'expire_version_change_payment',
                    'version_change', row.id, category='version_change', business_domain='version_change',
                    order_id=getattr(row, 'source_order_id', ''), before={'status': phase},
                    after={'status': row.status, 'funding_order_id': row.funding_order_id})
            elif phase in {'resource_pending', 'retrying'}:
                _assert_current(ns, db, row, instant)
                if not row.resource_plan_json or row.resource_plan_json == '{}':
                    descriptor = resource['prepare'](db, row)
                    _verify_safe_descriptor(descriptor)
                    row.resource_plan_json = ns['json'].dumps(descriptor, sort_keys=True)
                    row.lease_owner = ''; row.lease_expires_at = None
                    db.commit()
                    # Re-enter on next cycle after the descriptor commit.
                    return True
                proof = resource['apply'](db, row, direction='target')
                _verify_proof(row, proof)
                activation_at = ns['now']()
                _assert_current(ns, db, row, activation_at)
                row.evidence_json = ns['json'].dumps(proof, sort_keys=True)
                row.resource_applied = True
                if row.kind == 'upgrade':
                    ns['version_change_ledger']['finalize'](db, row.id, proof, 'version-change-worker')
                    ns['subscription_terms']['apply_version_change'](db,
                        db.get(ns['subscription_terms']['Combination'], row.combination_id),
                        row.target_version_id, row.target_version_code,
                        ns['json'].loads(row.target_snapshots_json), activation_at, row.id)
                    row.status = 'completed'; row.completed_at = activation_at
                elif row.kind == 'downgrade':
                    ns['subscription_terms']['apply_version_change'](db,
                        db.get(ns['subscription_terms']['Combination'], row.combination_id),
                        row.target_version_id, row.target_version_code,
                        ns['json'].loads(row.target_snapshots_json), activation_at, row.id)
                    row.status = 'refund_pending'
                else:
                    ns['version_change_ledger']['finalize'](db, row.id, proof, 'version-change-worker')
                    ns['subscription_terms']['apply_version_change'](db,
                        db.get(ns['subscription_terms']['Combination'], row.combination_id),
                        row.target_version_id, row.target_version_code,
                        ns['json'].loads(row.target_snapshots_json), activation_at, row.id)
                    row.status = 'completed'; row.completed_at = activation_at
                materialize = ns.get('refund_segment_store', {}).get('materialize')
                if callable(materialize):
                    materialize(db, row, proof)
                ns['audit'](db, 'version-change-worker', 'apply_version_change_resource',
                    'version_change', row.id, category='version_change', business_domain='version_change',
                    order_id=getattr(row, 'source_order_id', ''), before={'status': phase},
                    after={'status': row.status, 'resource_applied': True})
                row.retries = 0; row.next_retry_at = None; row.last_error = ''
            elif phase == 'compensating':
                if row.resource_plan_json and row.resource_plan_json != '{}':
                    proof = resource['apply'](db, row, direction='original')
                    _verify_proof(row, proof)
                else:
                    proof = {'request_change_id': row.id}
                ns['version_change_ledger']['void'](db, row.id, 'resource application compensated', 'version-change-worker')
                if row.kind == 'upgrade':
                    ns['version_change_saga']['refund_upgrade_funding'](db, row, proof)
                elif row.kind == 'downgrade':
                    for reservation in db.scalars(select(ns['VersionChangeFund']).where(
                            ns['VersionChangeFund'].change_id == row.id,
                            ns['VersionChangeFund'].kind == 'refund',
                            ns['VersionChangeFund'].refund_id.is_(None))).all():
                        db.delete(reservation)
                row.evidence_json = ns['json'].dumps(proof, sort_keys=True)
                row.status = 'compensated'
                row.completed_at = instant
                ns['audit'](db, 'version-change-worker', 'compensate_version_change',
                    'version_change', row.id, category='version_change', business_domain='version_change',
                    order_id=getattr(row, 'source_order_id', ''), before={'status': phase},
                    after={'status': row.status, 'resource_restored': True})
            elif phase == 'refund_pending':
                ns['version_change_saga']['process_downgrade_refunds'](db, row)
                row.status = 'completed'; row.completed_at = instant
                proof = ns['json'].loads(row.evidence_json or '{}')
                ns['version_change_ledger']['finalize'](db, row.id, proof, 'version-change-worker')
                ns['audit'](db, 'version-change-worker', 'complete_version_change_refunds',
                    'version_change', row.id, category='version_change', business_domain='version_change',
                    order_id=getattr(row, 'source_order_id', ''), before={'status': phase}, after={'status': row.status})
            elif phase == 'exception':
                # Operator retry sets a concrete phase before this worker claims it.
                db.rollback(); return False
            lifecycle = ns.get('order_service_lifecycle')
            if lifecycle:
                lifecycle['change_applied'](db, row)
            row.lease_owner = ''; row.lease_expires_at = None
            row.updated_at = instant
            db.commit()
            return True
        except Exception as exc:
            db.rollback()
            with SessionLocal() as failed_db:
                row = failed_db.scalar(select(Request).where(Request.id == change_id,
                    Request.lease_owner == owner).with_for_update())
                if row is None:
                    failed_db.rollback(); return False
                row.last_error = type(exc).__name__[:80]
                row.lease_owner = ''; row.lease_expires_at = None
                if phase == 'compensating':
                    row.compensation_retries += 1
                    row.status = 'exception' if row.compensation_retries >= MAX_ATTEMPTS else 'compensating'
                    row.next_retry_at = (instant + timedelta(seconds=RETRY_DELAY)
                                         if row.status == 'compensating' else None)
                elif phase in {'resource_pending', 'retrying'}:
                    row.retries += 1
                    if row.retries >= MAX_ATTEMPTS:
                        row.status = 'compensating'
                        row.compensation_started = True
                        row.compensation_retries = 0
                        row.next_retry_at = instant + timedelta(seconds=RETRY_DELAY)
                    else:
                        row.status = 'retrying'
                        row.next_retry_at = instant + timedelta(seconds=RETRY_DELAY)
                else:
                    row.retries += 1
                    if row.retries >= MAX_ATTEMPTS:
                        row.status = 'exception'
                        row.next_retry_at = None
                    else:
                        row.status = phase
                        row.next_retry_at = instant + timedelta(seconds=RETRY_DELAY)
                row.updated_at = instant
                ns['audit'](failed_db, 'version-change-worker', 'retry_version_change_phase',
                    'version_change', row.id, category='version_change', business_domain='version_change',
                    order_id=getattr(row, 'source_order_id', ''), before={'status': phase},
                    after={'status': row.status, 'retry_count': row.retries,
                           'compensation_retry_count': row.compensation_retries,
                           'error_type': type(exc).__name__})
                failed_db.commit()
            logger.error('Version change phase failed: %s', type(exc).__name__)
            return True


def _verify_proof(change, proof):
    import re
    if (not isinstance(proof, dict) or proof.get('request_change_id') != change.id
            or proof.get('resource_change_id') != change.id
            or not isinstance(proof.get('evidence_fingerprint'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', proof['evidence_fingerprint'])):
        raise ValueError('resource proof verification failed')


def _verify_safe_descriptor(value):
    forbidden = {'secret', 'token', 'password', 'authorization', 'credential_secret', 'raw_key'}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in forbidden or normalized.endswith('_secret') or normalized.endswith('_token'):
                raise ValueError('unsafe resource plan descriptor')
            _verify_safe_descriptor(child)
    elif isinstance(value, list):
        for child in value:
            _verify_safe_descriptor(child)
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError('invalid resource plan descriptor')


def _assert_current(ns, db, change, instant):
    from .subscription_calendar import CalendarAnchor
    from .subscription_terms import utc
    Combination = ns['subscription_terms']['Combination']
    combo = db.scalar(select(Combination).where(Combination.id == change.combination_id)
                      .with_for_update())
    if (not combo or combo.status != 'active' or combo.version_id != change.old_version_id
            or combo.version_code != change.old_version_code
            or utc(instant) >= CalendarAnchor(combo.anchor_at).boundary(combo.total_months)):
        raise ValueError('subscription changed or expired before activation')


def read_config(environ):
    try:
        interval = float(environ.get('VERSION_CHANGE_WORKER_INTERVAL', '2'))
        batch = int(environ.get('VERSION_CHANGE_WORKER_BATCH', '10'))
    except (TypeError, ValueError):
        raise ValueError('invalid version change worker configuration') from None
    if not 0 < interval <= 60 or not 1 <= batch <= 100:
        raise ValueError('invalid version change worker configuration')
    return interval, batch


def read_fixture_config(environ):
    from .fixture_faults import FixtureScope
    selection = environ.get('VERSION_CHANGE_FIXTURE_SCOPE_FILE')
    exclusions = environ.get('VERSION_CHANGE_EXCLUDED_SCOPE_FILES')
    for key in ('VERSION_CHANGE_FIXTURE_SCOPE_FILE', 'VERSION_CHANGE_EXCLUDED_SCOPE_FILES'):
        if key in environ and not environ[key]:
            raise ValueError('configured fixture scope cannot be empty')
    if selection is not None and exclusions is not None:
        raise ValueError('selection and exclusion cannot be combined')
    scope = FixtureScope.load(selection) if selection is not None else None
    excluded = tuple(FixtureScope.load(path) for path in exclusions.split(',')) if exclusions is not None else ()
    return scope, excluded


def run_worker(process, stopped, *, interval=2, batch=10, heartbeat=HEARTBEAT,
               monotonic=time.monotonic):
    interval, batch = read_config({'VERSION_CHANGE_WORKER_INTERVAL': str(interval),
                                   'VERSION_CHANGE_WORKER_BATCH': str(batch)})
    while not stopped.is_set():
        started = monotonic()
        try:
            for _ in range(batch):
                if stopped.is_set() or monotonic() - started >= 15:
                    break
                processed = process()
                heartbeat.touch()
                if not processed:
                    break
        except Exception as exc:
            logger.error('Version change worker cycle failed: %s', type(exc).__name__)
        stopped.wait(max(0, interval - (monotonic() - started)))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    stopped, handlers = threading.Event(), {}
    try:
        interval, batch = read_config(os.environ)
        scope, excluded = read_fixture_config(os.environ)
        os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
        for sig in (signal.SIGTERM, signal.SIGINT):
            handlers[sig] = signal.signal(sig, lambda *_: stopped.set())
        heartbeat = (Path('/tmp') / f'{scope.run_id}-{scope.change_id}-heartbeat'
                     if scope is not None else HEARTBEAT)
        run_worker(lambda: process_once(fixture_scope=scope, excluded_scopes=excluded,
                   reservation_token=os.environ.get('QA_FIXTURE_RESERVATION_TOKEN')),
                   stopped, interval=interval, batch=batch, heartbeat=heartbeat)
    except Exception as exc:
        logger.error('Version change worker failed: %s', type(exc).__name__)
        return 1
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
