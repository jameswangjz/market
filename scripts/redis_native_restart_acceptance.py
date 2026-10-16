"""Pause a reviewed real native refund flow for a host-driven sealed Redis restart."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from unittest.mock import patch

import r3_native_refund_runtime_acceptance as r


SNAPSHOT_FIELDS = ('events', 'month', 'day', 'day_expires_at', 'coverage', 'metadata')


def field_hashes(value):
    return {field: digest(value[field]) for field in SNAPSHOT_FIELDS}


class SnapshotChanged(r.base.AcceptanceFailure):
    def __init__(self, before, after):
        self.diagnostics = dict(status='failed', error='restart_financial_snapshot_changed',
            changed_fields=[field for field in SNAPSHOT_FIELDS if before[field] != after[field]],
            before_fields_sha256=field_hashes(before), after_fields_sha256=field_hashes(after))
        super().__init__('restart_financial_snapshot_changed:' + json.dumps(self.diagnostics, sort_keys=True))


def projection_candidate(before, after):
    return (all(before[field] == after[field] for field in SNAPSHOT_FIELDS if field != 'metadata')
            and before['metadata'].keys() == after['metadata'].keys()
            and all(before['metadata'][key] == after['metadata'][key]
                    for key in before['metadata'] if key != 'monthly_quota'))


def compare_snapshots(before, after, *, target, state, at):
    if before == after:
        return None
    if projection_candidate(before, after):
        prior, current = before['metadata'], after['metadata']
        periods = json.loads(target['subscription_periods'])
        if (len(periods) == 1 and periods[0]['start_at'] <= at < periods[0]['end_at']
                and periods[0]['order_id'] == state['order_id']
                and periods[0]['segment_id'] == state['term_id']
                and periods[0]['period_key'] == state['combination_id'] + ':1:0'
                and target['quota_scope'] == state['combination_id']
                and target['monthly_quota'] == '10'
                and type(periods[0]['monthly_quota']) is int and periods[0]['monthly_quota'] == 2
                and set(target) == set(prior) and 'monthly_quota' in prior
                and all(prior[key] == target[key] for key in target if key != 'monthly_quota')
                and prior['monthly_quota'] in (target['monthly_quota'], '2')
                and current['monthly_quota'] == '2'):
            return dict(reason='current_period_monthly_quota_projection', field='monthly_quota',
                        before=prior['monthly_quota'], after=current['monthly_quota'],
                        frozen_target=target['monthly_quota'], effective_period='2')
    raise SnapshotChanged(before, after)


def execution_target(m, db, state):
    from sqlalchemy import select
    Execution = m.refund_execution['Execution']
    execution = db.scalars(select(Execution).where(Execution.refund_id == state['refund_id'])).one_or_none()
    r.check(execution is not None and execution.status == 'completed', 'restart_execution_unproven')
    plan = json.loads(execution.resource_plan_json)
    r.check(execution.resource_plan_hash == digest(plan)
            and plan['plan_hash'] == digest({key: value for key, value in plan.items() if key != 'plan_hash'})
            and plan['refund_id'] == state['refund_id'] and plan['order_id'] == state['order_id']
            and plan['term_id'] == state['term_id'] and plan['source_run_id'] == state['source_run_id'],
            'restart_execution_unproven')
    return plan['target']


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def private_path(value):
    value = str(value)
    r.check(re.fullmatch(r'/tmp/qa-native-[a-z0-9-]+\.json', value) is not None, 'invalid_restart_path')
    return Path(value)


def write_new(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(value, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())


def read_ack(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        r.check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_uid == os.getuid() and info.st_size <= 4096, 'invalid_restart_ack')
        return json.loads(source.read(4097))


def snapshot(redis, manifest, state):
    stream = r.source_stream(state)
    r.check(stream != r.STREAM and len(state['event_ids']) == 2
            and len(set(state['event_ids'])) == 2, 'managed_two_event_fixture_required')
    events = []
    for event in state['event_ids']:
        rows = redis.xrange(stream, min=event, max=event, count=1)
        r.check(len(rows) == 1 and rows[0][0] == event, 'restart_financial_event_missing')
        r.validate_charge(event, rows[0][1], start=state['stream_start'], end=state['stream_end'],
            order_id=state['order_id'], term_id=state['term_id'], combination_id=state['combination_id'],
            route_key=manifest['routes']['api']['source'], source_run_id=state['source_run_id'])
        events.append(rows[0])
    combo = r.exact_id(state['combination_id'])
    windows = [w for w in manifest['quota_windows'] if w['scope'] == combo]
    r.check(len(windows) == 1 and windows[0]['period'] == combo + ':1:0'
            and re.fullmatch(r'\d{4}-\d{2}-\d{2}', windows[0]['day']), 'restart_quota_window_invalid')
    suffix = r.quota_token(combo) + ':' + r.quota_token(combo + ':1:0')
    month = 'market:apisix:quota:subscription:month:' + suffix
    day = 'market:apisix:quota:subscription:day:' + r.quota_token(combo) + ':' + windows[0]['day']
    consumer = [c for c in manifest['consumers'] if c['id'] == state['credential_id']]
    r.check(len(consumer) == 1 and consumer[0]['name'] == state['consumer'], 'restart_consumer_unproven')
    metadata = 'market:apisix:credential:' + consumer[0]['name']
    r.check(month in r.base.redis_key_journal(manifest) and day in r.base.redis_key_journal(manifest)
            and metadata in r.base.redis_key_journal(manifest), 'restart_keys_unjournaled')
    coverage = r.COVERAGE + suffix
    r.check(coverage in state['extra_keys'] and redis.get(month) == '2'
            and redis.pttl(month) == -1 and redis.pttl(coverage) == -1
            and redis.hgetall(coverage).get('baseline') == '0', 'restart_financial_coverage_invalid')
    return dict(events=events, month=redis.get(month), day=redis.get(day),
                day_expires_at=redis.execute_command('PEXPIRETIME', day),
                coverage=redis.hgetall(coverage), metadata=redis.hgetall(metadata))


def verify_resume(before, after, old, new):
    r.check(after[:len(before)] == before, 'restart_audit_history_changed')
    added = after[len(before):]
    for index, entry in enumerate(added):
        evidence = entry.get('evidence_sha256', '')
        if (entry.get('action') == 'resume' and entry.get('before_process_run_id') == old
                and entry.get('after_process_run_id') == new
                and isinstance(evidence, str) and re.fullmatch(r'[0-9a-f]{64}', evidence)
                and any(e.get('action') == 'seal' and e.get('evidence_sha256') == evidence
                        for e in added[:index])):
            return evidence
    r.check(False, 'restart_resume_audit_unproven')


def run(manifest, ready_file, ack_file, *, reviewed=False, snapshot_file=None,
        clock=time.monotonic, sleep=time.sleep):
    r.check(reviewed is True, 'runtime_review_required')
    manifest, ready, ack = map(private_path, (manifest, ready_file, ack_file))
    snapshot_path = private_path(snapshot_file) if snapshot_file is not None else None
    paths = [manifest, ready, ack] + ([snapshot_path] if snapshot_path is not None else [])
    r.check(len(set(paths)) == len(paths) and not any(p.exists() or p.is_symlink()
            for p in paths), 'restart_paths_must_be_new')
    original = r.cleanup_with_predelete_retry
    snapshot_failure = None
    projection = None
    retained = False

    def paused_cleanup(m, journal, path):
        nonlocal snapshot_failure, projection, retained
        state = journal['database_ids'].get('native_refund', {})
        if not state.get('proof') or state.get('local_flow_done') is not True:
            return original(m, journal, path)
        redis = m.apisix_policy_redis()
        try:
            with m.SessionLocal() as db:
                stream, cursor = r.source_context(m, db, redis)
                r.check(stream == r.source_stream(state) and cursor is not None
                        and cursor.redis_run_id == state['source_run_id'], 'restart_source_unproven')
                old = redis.info('server')['run_id']
                r.check(old == state['physical_run_id'], 'restart_initial_process_changed')
                before = deepcopy(snapshot(redis, journal, state))
                generation = db.get(m.api_charge_ledger['SourceGeneration'], state['source_run_id'])
                audits = json.loads(generation.audit_log)
            redis.close()
            redis = None
            receipt = dict(status='ready', source_id=state['source_run_id'], source_stream=stream,
                           physical_run_id=old, snapshot_sha256=digest(before),
                           fields_sha256=field_hashes(before), financial_events=2, monthly_used=2)
            if snapshot_path is not None:
                write_new(snapshot_path, dict(source_id=receipt['source_id'], before=before,
                                             snapshot_sha256=receipt['snapshot_sha256']))
            write_new(ready, receipt)
            deadline = clock() + 120
            while not ack.exists():
                r.check(clock() < deadline, 'restart_ack_timeout')
                sleep(.5)
            acknowledged = read_ack(ack)
            r.check(isinstance(acknowledged, dict) and set(acknowledged) == {
                    'source_id', 'evidence_sha256', 'before_process_run_id', 'after_process_run_id'}
                    and acknowledged['source_id'] == receipt['source_id']
                    and acknowledged['before_process_run_id'] == old
                    and isinstance(acknowledged['evidence_sha256'], str)
                    and re.fullmatch(r'[0-9a-f]{64}', acknowledged['evidence_sha256']), 'restart_ack_mismatch')
            redis = m.apisix_policy_redis()
            with m.SessionLocal() as db:
                stream_after, cursor = r.source_context(m, db, redis)
                new = redis.info('server')['run_id']
                r.check(stream_after == stream and cursor is not None
                        and cursor.redis_run_id == receipt['source_id'] and new != old
                        and re.fullmatch(r'[0-9a-f]{40}', new), 'restart_source_binding_invalid')
                generation = db.get(m.api_charge_ledger['SourceGeneration'], receipt['source_id'])
                r.check(generation.status == 'active' and generation.process_run_id == new,
                        'restart_generation_not_active')
                sealed = verify_resume(audits, json.loads(generation.audit_log), old, new)
                r.check(acknowledged == dict(source_id=receipt['source_id'], evidence_sha256=sealed,
                        before_process_run_id=old, after_process_run_id=new), 'restart_ack_mismatch')
                after = snapshot(redis, journal, state)
                if after != before:
                    try:
                        if not projection_candidate(before, after):
                            raise SnapshotChanged(before, after)
                        projection = compare_snapshots(before, after,
                            target=execution_target(m, db, state), state=state, at=redis.time()[0])
                    except SnapshotChanged as exc:
                        snapshot_failure = exc
                        raise
            state['physical_run_id'] = new
            state['restart_receipt'] = dict(source_id=receipt['source_id'], seal_sha256=sealed,
                before_physical_run_id=old, after_physical_run_id=new, snapshot_sha256=receipt['snapshot_sha256'])
            if projection is not None:
                state['restart_receipt']['metadata_projection'] = projection
            records = dict(before['events'])
            r.check(all(records.get(event) == record for event, record in
                        state.get('validated_events', {}).items()), 'restart_event_journal_changed')
            state['validated_events'] = records
            state['retain_financial_events_as_audit'] = True
            retained = True
            r.base.write_manifest(path, journal)
        finally:
            if redis is not None:
                redis.close()
        return original(m, journal, path)

    try:
        with patch.object(r, 'cleanup_with_predelete_retry', paused_cleanup):
            result = r.run(str(manifest), reviewed=True)
            if projection is not None:
                result = dict(result, metadata_projection=projection)
            if retained:
                result = dict(result, retained_financial_events=2)
                if isinstance(result.get('proof'), dict):
                    result['proof'] = dict(result['proof'], retained_financial_events=2)
            return result
    except Exception:
        if snapshot_failure is not None:
            raise snapshot_failure from None
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--ready-file', required=True)
    parser.add_argument('--ack-file', required=True)
    parser.add_argument('--snapshot-file')
    parser.add_argument('--reviewed', action='store_true')
    args = parser.parse_args()
    try:
        result = run(args.manifest, args.ready_file, args.ack_file, reviewed=args.reviewed,
                     snapshot_file=args.snapshot_file)
    except SnapshotChanged as exc:
        print(json.dumps(exc.diagnostics, sort_keys=True))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
