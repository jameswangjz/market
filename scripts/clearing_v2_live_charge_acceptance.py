"""Reviewed OPS001 native charge/refund acceptance; retain all facts and fixtures.

Run only after review, inside the API environment with existing native helpers.
No restart, source transition, direct ingest, cleanup, or replay is supported.
The private 0600 manifest contains credentials. Never print or export its content.
"""
import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import patch

import r3_native_refund_flow as flow

base = flow.base
guards = flow.guards
KEY = 'clearing_v2_live_charge'


def journal(path, manifest, phase, status):
    state = manifest['database_ids'][KEY]
    state.setdefault('acceptance_status', state['status'])
    state['phase'] = phase
    state['status'] = status
    if phase not in ('disable_owned_route', 'disable_fixture_accounts'):
        state['acceptance_status'] = status
    state['journal'].append(dict(phase=phase, status=status,
                                at=datetime.now(timezone.utc).isoformat()))
    base.write_manifest(path, manifest)


def current_source(m):
    redis = m.apisix_policy_redis()
    try:
        with m.SessionLocal() as db:
            stream, cursor = guards.source_context(m, db, redis)
            base.check(cursor is not None and stream == guards.STREAM + ':' + cursor.redis_run_id,
                       'managed_registered_source_required')
            state = dict(source_stream=stream, source_run_id=cursor.redis_run_id,
                         physical_run_id=redis.info('server')['run_id'])
            guards.source_stream(state)
            return state
    finally:
        redis.close()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        default=lambda item: item.isoformat()).encode()).hexdigest()


def row_values(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def stream_fingerprint(redis, stream):
    kind = redis.type(stream)
    base.check(kind in ('none', 'stream'), 'historical_stream_wrong_type')
    count = redis.xlen(stream) if kind == 'stream' else 0
    hasher = hashlib.sha256()
    position, read = '-', 0
    while kind == 'stream':
        rows = redis.xrange(stream, min=position, max='+', count=500)
        if not rows:
            break
        for event_id, values in rows:
            # Hash every full event, retaining no payload in the manifest.
            hasher.update(bytes.fromhex(digest([event_id, values])))
            read += 1
        position = '(' + rows[-1][0]
    base.check(redis.type(stream) == kind and read == count and
               (redis.xlen(stream) if kind == 'stream' else 0) == count,
               'historical_stream_changed_during_snapshot')
    return dict(type=kind, count=count, sha256=hasher.hexdigest())


def historical_fingerprint(m, source):
    """Read every old stream and full old cursor/Gap row; export only digests/counts."""
    from sqlalchemy import select
    Generation, Cursor, Gap = (m.api_charge_ledger[key] for key in ('SourceGeneration', 'Cursor', 'Gap'))
    with m.SessionLocal() as db:
        with db.no_autoflush:
            generations = list(db.scalars(select(Generation)))
            cursors = list(db.scalars(select(Cursor)))
            gaps = list(db.scalars(select(Gap)))
            streams = {guards.STREAM}
            for row in generations:
                streams.update(value for value in (row.stream, row.old_stream) if value)
            streams.update(row.stream for row in cursors)
            streams.update(row.old_stream for row in gaps if row.old_stream)
            old_cursors = [row_values(row) for row in cursors if row.stream != source['source_stream']]
            gap_rows = [row_values(row) for row in gaps]
    redis = m.apisix_policy_redis()
    try:
        streams.update(redis.scan_iter(match=guards.STREAM + '*'))
        streams.discard(source['source_stream'])
        stream_rows = {name: stream_fingerprint(redis, name) for name in sorted(streams)}
    finally:
        redis.close()
    return dict(streams=stream_rows, stream_count=len(stream_rows),
                cursors=dict(count=len(old_cursors), sha256=digest(sorted(old_cursors, key=digest))),
                gaps=dict(count=len(gap_rows), sha256=digest(sorted(gap_rows, key=digest))))


def verify_preserved(m, manifest):
    state = manifest['database_ids'][KEY]
    state['historical_verified'] = False
    base.check(current_source(m) == state['source'], 'source_changed_preserve_for_review')
    base.check(historical_fingerprint(m, state['source']) == state['historical_before'],
               'historical_stream_cursor_or_gap_changed')
    with m.SessionLocal() as db:
        guards._protected(m, db, manifest)
    state['historical_verified'] = True


def summary(manifest):
    state = manifest['database_ids'][KEY]
    native = manifest['database_ids'].get('native_refund', {})
    return dict(status=state['status'], phase=state['phase'],
                acceptance_status=state.get('acceptance_status', state['status']),
                historical_preserved=state.get('historical_verified', False),
                charges=native.get('proof', {}).get('charges'),
                fixtures_and_credentials_retained=True, financial_facts_retained=True,
                retrospective_continuity_claim=False,
                fixture_accounts_disabled=state.get('fixture_accounts_disabled', False),
                route_disabled=state.get('route_disabled', False))


def owned_accounts(m, db, manifest):
    """Validate all six immutable fixture roots before changing any account."""
    ids = base.fixture_ids(manifest['run_id'])
    base.check(all(manifest[key] == ids[key] for key in ('users', 'enterprises')),
               'fixture_identity_roots_mismatch')
    for eid in ids['enterprises'].values():
        enterprise = db.get(m.Enterprise, eid, with_for_update=True)
        base.check(enterprise is not None and enterprise.id == eid and
                   enterprise.name == eid and enterprise.credit_code == eid,
                   'fixture_enterprise_ownership_mismatch')
    users = []
    for uid in ids['users'].values():
        user = db.get(m.User, uid, with_for_update=True)
        base.check(user is not None and user.id == uid and user.name == uid and
                   user.email == uid + '@example.invalid', 'fixture_user_ownership_mismatch')
        users.append(user)
    return users


def disable_accounts(m, path, manifest):
    journal(path, manifest, 'disable_fixture_accounts', 'started')
    with m.SessionLocal() as db:
        users = owned_accounts(m, db, manifest)
        for user in users:
            user.is_active = False
        db.commit()
    with m.SessionLocal() as db:
        base.check(all(user.is_active is False for user in owned_accounts(m, db, manifest)),
                   'fixture_account_disable_verification_failed')
    manifest['database_ids'][KEY]['fixture_accounts_disabled'] = True
    journal(path, manifest, 'disable_fixture_accounts', 'completed')


def quiesce(m, path, manifest):
    """Disable only the exact owned route; retain consumer and every audit fact."""
    ids = base.fixture_ids(manifest['run_id'])
    state = manifest['database_ids'][KEY]
    native = manifest['database_ids'].get('native_refund', {})
    route = ids['routes']['api']['source']
    base.check(manifest['authorized'] is True and manifest['environment'] ==
               base.environment_fingerprint(m), 'environment_mismatch')
    base.check(native.get('route_key') == route and native.get('product_id') == ids['products']['api']
               and route in manifest['external']['route_keys'], 'owned_route_not_journaled')
    verify_preserved(m, manifest)
    with m.SessionLocal() as db:
        owned_accounts(m, db, manifest)
    endpoint = '/routes/' + route
    live = base.apisix_admin(m, 'GET', endpoint)
    base.check(live.get('id') == route and live.get('labels', {}).get('market_product_id') ==
               ids['products']['api'], 'foreign_external_route')
    document = deepcopy(live)
    for field in ('create_time', 'update_time'):
        document.pop(field, None)
    document['status'] = 0
    journal(path, manifest, 'disable_owned_route', 'started')
    base.apisix_admin(m, 'PUT', endpoint, document)
    actual = base.apisix_admin(m, 'GET', endpoint)
    for field in ('create_time', 'update_time'):
        actual.pop(field, None)
    base.check(actual == document, 'route_disable_verification_failed')
    state['route_disabled'] = True
    verify_preserved(m, manifest)
    journal(path, manifest, 'disable_owned_route', 'completed')
    disable_accounts(m, path, manifest)
    return summary(manifest)


def exercise(m, ids, path, manifest, client, tokens):
    """Journal before each native phase; never replay an interrupted phase."""
    def wrapped(name, original):
        def call(*args, **kwargs):
            journal(path, manifest, name, 'started')
            result = original(*args, **kwargs)
            if name == 'purchase':
                manifest['database_ids'][KEY]['credentials']['api_key'] = result
            journal(path, manifest, name, 'completed')
            return result
        return call
    with ExitStack() as stack:
        for name in ('bootstrap', 'publish', 'purchase', 'charge', 'refund', 'negative_settlement'):
            stack.enter_context(patch.object(flow, name, wrapped(name, getattr(flow, name))))
        return flow.exercise(m, ids, path, manifest, client, tokens, fixture_mode='baseline')


def run(path, *, reviewed=False):
    base.check(reviewed is True, 'runtime_review_required')
    path = Path(path)
    base.check(not path.exists() and not path.is_symlink(), 'manifest_exists_no_replay')
    m = base.load_main()
    manifest = base.fixture_ids()
    base.preflight(m, manifest)
    source = current_source(m)
    historical = historical_fingerprint(m, source)
    manifest['environment'] = base.environment_fingerprint(m)
    manifest['authorized'] = True
    manifest['database_ids'][KEY] = dict(status='started', acceptance_status='started',
        phase='preflight', journal=[], historical_before=historical,
        source=source, credentials={}, retention_policy='retain_all_fixtures_and_financial_facts')
    base.write_manifest(path, manifest, create=True)
    client = None
    try:
        journal(path, manifest, 'prepare', 'started')
        tokens = base.prepare(m, manifest)
        manifest['database_ids'][KEY]['credentials']['tokens'] = tokens
        journal(path, manifest, 'prepare', 'completed')
        from fastapi.testclient import TestClient
        client = TestClient(m.app)
        proof = exercise(m, manifest, path, manifest, client, tokens)
        native = manifest['database_ids']['native_refund']
        base.check(all(native.get(key) == value for key, value in source.items())
                   and current_source(m) == source, 'source_changed_preserve_for_review')
        base.check(proof.get('charges') == 2 and proof.get('monthly_used') == 2
                   and proof.get('refunded_amount') == 16, 'native_proof_incomplete')
        verify_preserved(m, manifest)
        native['local_flow_done'] = True
        journal(path, manifest, 'acceptance', 'passed')
        return quiesce(m, path, manifest)
    except Exception:
        # Persist only a fixed label; SQL/HTTP exceptions can contain credentials.
        journal(path, manifest, manifest['database_ids'][KEY]['phase'], 'failed_preserved')
        raise
    finally:
        if client is not None:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'inspect', 'disable-owned-route'))
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--reviewed', action='store_true')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        if args.action == 'run':
            result = run(args.manifest, reviewed=args.reviewed)
        else:
            if args.action != 'inspect':
                base.check(args.reviewed, 'runtime_review_required')
            manifest = base.read_manifest(args.manifest)
            base.check(KEY in manifest['database_ids'], 'wrong_acceptance_manifest')
            result = (summary(manifest) if args.action == 'inspect' else
                      quiesce(base.load_main(), args.manifest, manifest))
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps(dict(status='failed', details_suppressed=True,
                              automatic_cleanup=False, preserve_private_manifest=True)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
