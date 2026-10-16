"""Evidence-gated four-task review submission; no deployment or financial writes."""
import argparse
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re

CODES = ('REDIS-OPS-001', 'REDIS-BE-001', 'REDIS-QA-001', 'TRD-R3-OPS-003')
TAG = '20261016134235-69c8174'
COMMIT = '69c817422686a5f2baf46f12fb10bf206c8de96e'
PROTECTED = 'ORD-261012005144E1B3'
CHECKS = ('pvc_bound', 'aof_everysec', 'aof_write_healthy', 'ledger_worker_ready',
          'all_core_ready', 'native_gateway_and_refund_passed',
          'planned_restart_preserved_financial_data', 'source_identity_preserved',
          'physical_process_changed', 'old_cursor_unchanged', 'protected_order_unchanged',
          'old_financial_economics_unchanged', 'historical_gap_not_assumed_zero')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read(path):
    raw = Path(path).read_bytes()
    require(len(raw) <= 10000000, 'evidence_too_large')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique,
                       parse_constant=lambda _: require(False, 'invalid_json_number'))
    require(isinstance(value, dict), 'evidence_object_required')
    return value, hashlib.sha256(raw).hexdigest()


def validate(proof, native, native_sha):
    require(proof.get('schema') == 'redis_recovery_runtime_v1' and proof.get('status') == 'passed'
            and proof.get('tag') == TAG and proof.get('commit') == COMMIT, 'runtime_identity_mismatch')
    at = datetime.fromisoformat(proof['captured_at'])
    require(at.tzinfo is not None and at.utcoffset() is not None, 'aware_capture_required')
    for name in ('source_id', 'physical_run_id'):
        require(isinstance(proof.get(name), str) and re.fullmatch('[0-9a-f]{40}', proof[name]), 'invalid_source_identity')
    require(all(proof.get('checks', {}).get(key) is True for key in CHECKS), 'runtime_checks_incomplete')
    require(type(proof.get('gap_count')) is int and proof['gap_count'] == 1, 'historical_gap_required')
    binding = proof.get('native_evidence', {})
    require(binding.get('status') == native.get('status') == 'passed'
            and isinstance(binding.get('path'), str) and binding['path']
            and binding.get('sha256') == native_sha, 'native_evidence_binding_mismatch')
    for document in (proof, native):
        require(not document.get('error') and not document.get('setup_or_execution_error'), 'evidence_error')
    actual = native.get('proof', {})
    for key, expected in dict(charges=2, monthly_quota=2, monthly_used=2, next_http_status=429).items():
        require(type(actual.get(key)) is int and actual[key] == expected, 'native_count_mismatch')
    require(Decimal(str(actual.get('refunded_amount'))) == Decimal('16'), 'native_refund_mismatch')
    retained = native.get('cleanup', {}).get('retained_financial_events')
    require(type(retained) is int and retained == 2, 'native_financial_events_not_retained')


def verify_sources(proof, main_path):
    root = Path(main_path).resolve().parent
    actual = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob('*.py')}
    require(len(actual) == 82 and proof.get('source_sha256') == actual, 'installed_source_inventory_mismatch')


class ReadOnlyRedis:
    def __init__(self, client):
        self.client = client

    def info(self, section):
        require(section == 'server', 'redis_read_not_allowed')
        return self.client.info(section)

    def hgetall(self, key):
        require(key == 'market:apisix:charges:source', 'redis_read_not_allowed')
        return self.client.hgetall(key)

    def close(self):
        self.client.close()


def live_check(m, db, client, proof):
    from sqlalchemy import select
    ledger = m.api_charge_ledger
    stream, cursor = ledger['source_context'](db, client)
    require(stream == 'market:apisix:charges:v1:' + proof['source_id'] and cursor is not None
            and cursor.redis_run_id == proof['source_id']
            and client.info('server')['run_id'] == proof['physical_run_id'], 'live_source_mismatch')
    gaps = db.scalars(select(ledger['Gap'])).all()
    require(len(gaps) == proof['gap_count'] and all(
        gap.status == 'unresolved' and gap.missing_count is None for gap in gaps), 'gap_disclosure_mismatch')


def protected_snapshot(m, db):
    from sqlalchemy import or_, select
    table = m.Order.__table__
    orders = [dict(row) for row in db.execute(select(table).where(
        or_(table.c.id == PROTECTED, table.c.order_no == PROTECTED))).mappings()]
    require(len(orders) == 1, 'protected_order_missing')
    order = orders[0]
    payload = {'order': order}
    for model, condition in ((m.Product, m.Product.id == order['product_id']),
            (m.Payment, m.Payment.order_id == order['id']), (m.Refund, m.Refund.order_id == order['id'])):
        payload[model.__tablename__] = sorted((dict(r) for r in db.execute(
            select(model.__table__).where(condition)).mappings()), key=lambda r: r['id'])
    require(len(payload[m.Product.__tablename__]) == 1, 'protected_product_missing')
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def submit(m, evidence, native_path, output):
    from sqlalchemy import select
    proof, evidence_sha = read(evidence)
    native, native_sha = read(native_path)
    validate(proof, native, native_sha)
    verify_sources(proof, m.__file__)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out, m.SessionLocal() as db, closing(ReadOnlyRedis(m.apisix_policy_redis())) as client:
        actors = db.scalars(select(m.User).where(m.User.email == 'admin@market.local')).all()
        require(len(actors) == 1 and actors[0].platform_role == 'super_admin' and actors[0].is_active
                and actors[0].activation_status == 'active', 'actual_active_admin_required')
        actor = actors[0]
        tasks = db.scalars(select(m.DevelopmentTask).where(m.DevelopmentTask.code.in_(CODES))
                           .order_by(m.DevelopmentTask.code).with_for_update()).all()
        require(len(tasks) == 4 and {t.code for t in tasks} == set(CODES)
                and all(t.status == 'in_progress' for t in tasks), 'four_active_tasks_required')
        protected = protected_snapshot(m, db)
        live_check(m, db, client, proof)
        require(read(evidence)[1] == evidence_sha and read(native_path)[1] == native_sha, 'evidence_changed')
        verify_sources(proof, m.__file__)
        at = m.now()
        note = ('Redis recovery submitted for review; runtime_sha256=' + evidence_sha
                + '; native_sha256=' + native_sha + '; tag=' + TAG
                + '; one historical gap remains unresolved, missing_count=NULL; unknown usage is not zero. '
                  'Native refund funds are simulated; no bank acceptance claimed.')
        changes = []
        for task in tasks:
            before = dict(status=task.status, progress=task.progress, acceptance=task.acceptance)
            task.status, task.progress, task.updated_at = 'review', 100, at
            task.acceptance = (task.acceptance or '') + '\n' + note
            after = dict(status=task.status, progress=task.progress, acceptance=task.acceptance)
            m.audit(db, actor.email, 'submit_redis_recovery_review', 'development_task', task.code,
                    note, before=before, after=after)
            changes.append(dict(code=task.code, before=before, after=after))
        db.flush()
        require(protected_snapshot(m, db) == protected, 'protected_fingerprint_changed')
        live_check(m, db, client, proof)
        result = dict(status='submitted_for_review', changed=4, changes=changes, actor=actor.email,
                      evidence_sha256=evidence_sha, native_sha256=native_sha, gap_count=1,
                      gap_status='unresolved', missing_count=None, protected_before=protected,
                      protected_after=protected, captured_at=at.isoformat())
        db.commit()
        json.dump(result, out, indent=2)
        out.write('\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--native', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    require(not os.path.lexists(args.output), 'output_already_exists')
    proof, _ = read(args.evidence)
    native, digest = read(args.native)
    validate(proof, native, digest)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    result = submit(m, args.evidence, args.native, args.output)
    print(json.dumps(dict(status=result['status'], changed=result['changed'])))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps(dict(status='refused', error_type=type(error).__name__)))
        raise SystemExit(1)
