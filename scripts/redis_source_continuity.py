"""Admin-only, read-only billing continuity inspection.

Run in a backend pod: PYTHONPATH=/app python redis_source_continuity.py
--actor admin@example.com --output /tmp/continuity.json
Candidates still require the existing explicit administrator resume operation.
Active health describes the current binding only, with no retrospective guarantee.
No event payloads, quota values, credentials, or exception messages are emitted.
"""
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


EPOCH = 'market:apisix:charges:source'
STREAM = 'market:apisix:charges:v1'


def seal_digest(client, stream):
    """Read-only equivalent of api_charge_sources.seal_hash; keep format identical."""
    records = []
    if client.pttl(EPOCH) != -1 or client.pttl(stream) not in (-1, -2):
        raise ValueError('expiring_evidence')
    for pattern in ('market:apisix:quota:subscription:month:*',
                    'market:apisix:charges:coverage:*'):
        for key in sorted(client.scan_iter(match=pattern)):
            if client.pttl(key) != -1:
                raise ValueError('expiring_evidence')
            kind = client.type(key)
            value = client.get(key) if kind == 'string' else client.hgetall(key) if kind == 'hash' else None
            if value is None:
                raise ValueError('invalid_evidence')
            records.append((key, kind, value))
    for key in sorted(client.scan_iter(match='market:apisix:quota:subscription:day:*')):
        value = client.get(key)
        expiry = client.execute_command('PEXPIRETIME', key)
        if value is None or type(expiry) is not int or expiry < -1:
            raise ValueError('expired_evidence')
        records.append((key, 'string', value, expiry))
    frozen = dict(client.hgetall(EPOCH))
    frozen.pop('process_run_id', None)
    frozen['status'] = 'paused'
    records.extend([(stream, client.xrange(stream)), ('marker', frozen)])
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def expected_marker(source, status):
    cutover = source.cutover - datetime(1970, 1, 1, tzinfo=timezone.utc)
    us = (cutover.days * 86400 + cutover.seconds) * 1000000 + cutover.microseconds
    return dict(run_id=source.id, stream=source.stream, status=status,
                process_run_id=source.process_run_id, not_before_us=str(us))


def inspect_source(client, source, cursor, ledger_counts):
    """Pure reads; never infer missing usage from surviving stream or AOF health."""
    report = dict(status='explicit_gap_required', continuity_proven=False,
                  missing_count=None, reasons=[], ledger=ledger_counts,
                  diagnostic_only=True, resume_authorized=False)
    reasons = report['reasons']
    stream = source.stream if source else STREAM
    physical = client.info('server').get('run_id')
    marker = client.hgetall(EPOCH)
    report['redis_run_id'] = physical
    report['source'] = None if source is None else dict(
        source_id=source.id, stream=stream, status=source.status,
        process_run_id=source.process_run_id, sealed=bool(source.sealed_hash))
    # Only known marker fields are exposed; arbitrary fields may contain secrets.
    report['marker_matches_source'] = bool(source and marker == expected_marker(
        source, 'paused' if source.status == 'paused' else 'ready'))
    report['physical_run_matches_source'] = bool(source and physical == source.process_run_id)
    kind = client.type(stream)
    first = client.xrange(stream, count=1) if kind == 'stream' else []
    last = client.xrevrange(stream, count=1) if kind == 'stream' else []
    length = client.xlen(stream) if kind == 'stream' else 0
    present = bool(cursor and kind == 'stream' and cursor.last_id != '0-0'
                   and client.xrange(stream, min=cursor.last_id, max=cursor.last_id, count=1))
    report['stream'] = dict(exists=kind != 'none', is_stream=kind == 'stream', length=length,
                            first_id=first[0][0] if first else None,
                            last_id=last[0][0] if last else None)
    registered = bool(source and cursor and cursor.redis_run_id == source.id
                      and cursor.activated_at == source.cutover)
    cursor_valid = bool(registered and (present or cursor.last_id == '0-0'))
    stream_valid = kind == 'stream' or (kind == 'none' and cursor_valid and cursor.last_id == '0-0')
    report['cursor'] = None if cursor is None else dict(
        last_id=cursor.last_id, source_id=cursor.redis_run_id,
        registered=registered, entry_present=present)
    persistence = client.info('persistence')
    report['persistence'] = {key: persistence.get(key) for key in (
        'loading', 'aof_enabled', 'aof_last_write_status', 'aof_last_bgrewrite_status',
        'rdb_last_bgsave_status', 'aof_rewrite_in_progress', 'rdb_bgsave_in_progress')}
    try:
        config = {}
        for key in ('appendonly', 'appendfsync', 'save', 'no-appendfsync-on-rewrite'):
            config[key] = client.config_get(key).get(key)
        report['persistence_config'] = config
    except Exception:
        report['persistence_config'] = {'available': False}
    if source is None:
        reasons.append('managed_source_missing')
    persistence_healthy = (persistence.get('loading') == 0
                           and persistence.get('aof_enabled') == 1
                           and persistence.get('aof_last_write_status') == 'ok'
                           and all(persistence.get(key) in (None, 'ok') for key in (
                               'aof_last_bgrewrite_status', 'rdb_last_bgsave_status')))
    active_healthy = bool(source and source.status == 'active' and cursor_valid
                          and stream_valid and report['marker_matches_source']
                          and report['physical_run_matches_source'] and persistence_healthy)
    if not stream_valid:
        reasons.append('stream_missing_or_wrong_type')
    if not registered:
        reasons.append('cursor_registration_mismatch')
    if cursor and cursor.last_id != '0-0' and not present:
        reasons.append('cursor_entry_missing')
    if not report['marker_matches_source']:
        reasons.append('marker_mismatch')
    if not report['physical_run_matches_source']:
        reasons.append('physical_run_id_mismatch')
    if active_healthy:
        report['status'] = 'active_source_healthy'
    elif not source or source.status != 'paused' or not source.sealed_hash:
        reasons.append('no_paused_seal_continuity_not_proven')
    else:
        allowed = [expected_marker(source, 'paused')]
        try:
            if source.marker_snapshot:
                allowed.append(json.loads(source.marker_snapshot))
            if physical == source.process_run_id:
                allowed.append(expected_marker(source, 'ready'))
            matches = (seal_digest(client, stream) == source.sealed_hash
                       and seal_digest(client, stream) == source.sealed_hash)
            report['seal_matches'] = matches
            if (marker in allowed and matches and registered
                    and (present or cursor.last_id == '0-0')
                    and (last[0][0] if last else '0-0') == cursor.last_id
                    and kind in ('stream', 'none')):
                report['status'] = 'safe_sealed_resume_candidate'
            else:
                reasons.append('sealed_evidence_or_drained_cursor_mismatch')
        except Exception:
            report['seal_matches'] = False
            reasons.append('seal_verification_unavailable')
    if source and source.status == 'active' and not persistence_healthy:
        reasons.append('persistence_not_healthy_or_loading')
    if client.info('server').get('run_id') != physical or client.hgetall(EPOCH) != marker:
        report['status'] = 'explicit_gap_required'
        reasons.append('source_changed_during_inspection')
    report['captured_at'] = datetime.now(timezone.utc).isoformat()
    return report


def build_report(db, client, hooks):
    from sqlalchemy import func, select
    Generation, Cursor, Charge, Gap = (hooks[k] for k in ('SourceGeneration', 'Cursor', 'Charge', 'Gap'))
    with db.no_autoflush:
        source = db.scalar(select(Generation).order_by(
            Generation.cutover.desc(), Generation.id.desc()).limit(1))
        cursor = db.get(Cursor, source.stream if source else STREAM)
        counts = dict(total_charges=db.scalar(select(func.count()).select_from(Charge)),
                      gaps=db.scalar(select(func.count()).select_from(Gap)),
                      source_charges=None)
        if source:
            counts['source_charges'] = db.scalar(select(func.count()).select_from(Charge).where(
                Charge.source_run_id == source.id))
        before = (None if source is None else (
            source.id, source.stream, source.status, source.process_run_id,
            source.cutover, source.sealed_hash, source.marker_snapshot),
            None if cursor is None else (cursor.last_id, cursor.redis_run_id, cursor.activated_at))
        report = inspect_source(client, source, cursor, counts)
        if report['status'] in ('safe_sealed_resume_candidate', 'active_source_healthy'):
            db.refresh(source)
            db.refresh(cursor)
            latest = db.scalar(select(Generation).order_by(
                Generation.cutover.desc(), Generation.id.desc()).limit(1))
            after = ((source.id, source.stream, source.status, source.process_run_id,
                      source.cutover, source.sealed_hash, source.marker_snapshot),
                     (cursor.last_id, cursor.redis_run_id, cursor.activated_at))
            if latest is None or latest.id != source.id or before != after:
                report['status'] = 'explicit_gap_required'
                report['reasons'].append('database_source_changed_during_inspection')
        return report


def require_admin(actor):
    if (not actor or actor.platform_role != 'super_admin' or not actor.is_active
            or getattr(actor, 'activation_status', None) != 'active'):
        raise ValueError('active_system_administrator_required')


def write_report(path, report):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(report, output, indent=2)
        output.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--actor', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    client = None
    try:
        from app import main as m
        from app.api_charge_ledger import bounded_client
        from sqlalchemy import select
        with m.SessionLocal() as db:
            with db.no_autoflush:
                require_admin(db.scalar(select(m.User).where(m.User.email == args.actor)))
                client = bounded_client(vars(m))
                report = build_report(db, client, m.api_charge_ledger)
            db.rollback()
        write_report(args.output, report)
        print(json.dumps({'status': report['status']}))
        return 0
    except Exception:
        print(json.dumps({'status': 'diagnostic_failed', 'reason': 'inspection_or_output_unavailable'}))
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == '__main__':
    raise SystemExit(main())
