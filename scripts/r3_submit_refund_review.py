"""Evidence-gated BE012/QA005/QA006 submission for user review, never done.

Main invokes this locally inside the reviewed installed application. No remote
transport, startup, deployment, or edits to existing task scripts are provided.
FILE storage and monthly supplier adapters remain explicitly synthetic; funds
are simulated. This submission does not claim browser or bank acceptance.
"""
import argparse
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re


CODES = ('TRD-R3-BE-012', 'TRD-R3-QA-005', 'TRD-R3-QA-006')
KINDS = ('saas', 'file', 'monthly', 'native')
FILE_CASES = ('file_two_partials_delayed_settlement', 'refund_budget_two_connections',
              'download_recovery_serialization', 'funds_failure_retry', 'proof_phase_rollback')
MONTHLY_CASE = 'financial_cross_month_real_saga_combination'
PROTECTED = 'ORD-261012005144E1B3'


def require(ok, label):
    if not ok:
        raise ValueError(label)


def money(value, expected):
    require(not isinstance(value, bool) and isinstance(value, (str, int, float, Decimal)), 'invalid_money')
    amount = Decimal(str(value))
    require(amount.is_finite() and amount == Decimal(expected), 'money_mismatch')


def integer(value, expected):
    require(type(value) is int and value == expected, 'integer_mismatch')


def flags(row, *keys):
    require(all(row.get(k) is True for k in keys), 'required_flag_missing')


def all_cases(proof, names):
    cases = proof.get('cases')
    require(isinstance(cases, dict) and set(names).issubset(cases)
            and all(isinstance(c, dict) and c.get('status') == 'passed' for c in cases.values()),
            'required_cases_not_passed')
    return cases


def validate(proofs):
    require(set(proofs) == set(KINDS), 'four_evidence_inputs_required')
    for proof in proofs.values():
        require(isinstance(proof, dict) and proof.get('status') == 'passed'
                and not any(proof.get(k) for k in ('error', 'setup_or_execution_error')), 'evidence_not_passed')
    saas, file, monthly, native = (proofs[k] for k in KINDS)
    flags(saas, 'protected_preserved')
    require(saas.get('cleanup') == dict(oidc='removed', public_roots='removed', role='removed',
            schema='removed', tenant='retained_closed'), 'saas_cleanup_incomplete')
    integer(saas.get('public_business_orders'), 0)
    p = saas['proof']
    flags(p, 'resources_before_funds', 'idempotent')
    before, after = (datetime.fromisoformat(p[k]) for k in ('expected_expires_at', 'expires_at'))
    require(before.tzinfo is not None and after.tzinfo is not None and after < before, 'saas_tail_not_shortened')
    earlier = saas['earlier_refund_blocked']
    require(earlier.get('status') == 'passed', 'earlier_case_not_passed')
    flags(earlier, 'unrefunded_renewal_blocked', 'no_execution_effects')
    require(p.get('refund_id') != earlier.get('refund_id') and all(
        isinstance(r, str) and re.fullmatch(r'[0-9a-f]{32}', r)
        for r in (p.get('refund_id'), earlier.get('refund_id'))), 'saas_refund_identities_invalid')
    owned = saas.get('owned_evidence', {})
    for table, count in dict(refunds=2, ordinary_refund_cases=2, ordinary_refund_executions=1,
            ordinary_refund_local_completions=1, ordinary_refund_payment_legs=1, refund_resource_recoveries=1).items():
        rows = owned.get(table)
        require(isinstance(rows, list) and len(rows) == count, 'saas_receipt_count_mismatch')
        require(all(isinstance(r, list) and len(r) == 2 and isinstance(r[0], list) and len(r[0]) == 1
                    and re.fullmatch(r'[0-9a-f]{32}', r[0][0]) and re.fullmatch(r'[0-9a-f]{64}', r[1])
                    for r in rows), 'saas_receipt_invalid')
        require(len({r[0][0] for r in rows}) == count, 'saas_duplicate_receipt')
    require({r[0][0] for r in owned['refunds']} == {p['refund_id'], earlier['refund_id']}
            and {r[0][0] for r in owned['ordinary_refund_cases']} == {p['refund_id'], earlier['refund_id']}
            and owned['ordinary_refund_local_completions'][0][0][0] == p['refund_id'],
            'saas_receipts_not_bound_to_refunds')
    for proof in (file, monthly):
        require(proof.get('cleanup') == 'owned schema removed', 'pg_cleanup_incomplete')
        integer(proof.get('protected_order_count_before'), 1)
        integer(proof.get('protected_order_count_after'), 1)
        integer(proof.get('public_write_operations'), 0)
        sentinel = all_cases(proof, ('protected_count_only',))['protected_count_only']
        integer(sentinel.get('before'), 1)
        integer(sentinel.get('after'), 1)
    cases = all_cases(file, (*FILE_CASES, 'protected_count_only'))
    p = cases[FILE_CASES[0]]
    flags(p, 'resources_before_funds', 'old_receipt_stable')
    integer(p.get('partial_refunds'), 2)
    require(p.get('negative_costs') == ['-11.11', '-22.22'], 'file_cost_deltas_mismatch')
    money(p.get('total_cost'), '-33.33')
    integer(p.get('over_refund'), 409)
    integer(p.get('other_order_download'), 200)
    p = cases[FILE_CASES[1]]
    integer(p.get('independent_pg_connections'), 2)
    require(p.get('statuses') == [200, 409], 'file_budget_race_mismatch')
    money(p.get('reserved'), '60.00')
    p = cases[FILE_CASES[2]]
    integer(p.get('orderings_tested'), 2)
    integer(p.get('server_lock_waits_observed'), 2)
    flags(p, 'independent_connections')
    integer(p.get('authorized_first_download'), 200)
    integer(p.get('recovery_first_download'), 409)
    flags(cases[FILE_CASES[3]], 'funds_failure_observed', 'entitlement_not_restored', 'receipt_stable', 'retry_completed')
    flags(cases[FILE_CASES[4]], 'proof_phase_rolled_back', 'durable_receipt_preserved', 'retry_completed')
    integer(cases[FILE_CASES[4]].get('funds_before_resources'), 0)
    p = all_cases(monthly, (MONTHLY_CASE, 'protected_count_only'))[MONTHLY_CASE]
    for key, count in dict(completed_sagas=2, economic_windows=2, ordinary_payment_legs=3,
                           ordinary_refunds=2, resource_recoveries=2, saga_adjustments=4).items():
        integer(p.get(key), count)
    flags(p, 'confirmed_rebuild_rejected', 'cross_source_refund', 'five_party_reports_and_csv',
          'latest_payment_first', 'prior_paid_batches_unchanged', 'replay_and_rebuild')
    require(p.get('seeded_version_changes') is False and monthly.get('seeded_version_changes') is False,
            'monthly_saga_is_seeded')
    require(p.get('saga_orchestration') == 'production quote/request/payment/worker', 'monthly_saga_not_executed')
    expected = {'2026-12': ('600', '120', '480'), '2027-01': ('54.07', '70.29', '-16.22'),
                '2027-02': ('-43.85', '-57.00', '13.15'), '2027-03': ('-80', '-16', '-64')}
    require(set(p.get('months', {})) == set(expected), 'monthly_windows_mismatch')
    for month, amounts in expected.items():
        for field, amount in zip(('net', 'cost', 'profit'), amounts):
            money(p['months'][month].get(field), amount)
    flags(native, 'protected_preserved')
    p = native['proof']
    for key, count in dict(charges=2, monthly_quota=2, monthly_used=2, next_http_status=429,
            simulated_payment_legs=1, report_csv_verified=1, delivery_audit_identity_verified=1,
            resource_failures_before_funds=1, idempotent_retry_completed=1).items():
        integer(p.get(key), count)
    money(p.get('refunded_amount'), '16')
    for key, amount in dict(gross_amount='-16', cost_amount='-4', profit_amount='-12', platform_fee='-2.4',
            provider_share='-6', service_share='-1.2', expert_fee='-1.2', channel_fee='-1.2').items():
        money(p['negative_settlement'].get(key), amount)
    cleanup = native.get('cleanup', {})
    counts = cleanup.get('deleted_tablecounts', {})
    require(isinstance(counts, dict) and all(type(n) is int and n > 0 for n in counts.values()),
            'native_cleanup_counts_invalid')
    for table in ('orders', 'refunds', 'ordinary_refund_executions', 'ordinary_refund_local_completions',
                  'refund_resource_recoveries', 'settlements'):
        integer(counts.get(table), 1)
    require(type(cleanup.get('retained_sla_count')) is int and cleanup['retained_sla_count'] >= 0
            and cleanup['retained_sla_count'] == p.get('retained_sla_count'), 'native_retained_facts_mismatch')


def verify_sources(proofs, main_path):
    root = Path(main_path).resolve().parent
    required = {'main.py', 'refund_execution.py', 'refund_execution_worker.py',
                'refund_execution_resources.py', 'refund_workflow.py', 'refund_planning.py'}
    for kind, key in (('saas', 'installed_source_sha256'), ('file', 'source_sha256'), ('monthly', 'source_sha256')):
        sources = proofs[kind].get(key)
        require(isinstance(sources, dict) and required.issubset(sources), 'source_inventory_incomplete')
        if kind == 'file':
            require('refund_file_resources.py' in sources, 'file_resource_source_missing')
        for name, digest in sources.items():
            path = PurePosixPath(name)
            require(not path.is_absolute() and '..' not in path.parts and str(path) == name
                    and isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest), 'source_entry_invalid')
            actual = (root / name).resolve()
            require(actual.is_relative_to(root) and actual.is_file()
                    and hashlib.sha256(actual.read_bytes()).hexdigest() == digest, 'installed_source_mismatch')
        if kind == 'saas':
            require(set(sources) == {p.relative_to(root).as_posix() for p in root.rglob('*.py')},
                    'installed_python_inventory_mismatch')


def load_evidence(paths):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result

    proofs, bound = {}, {}
    for kind in KINDS:
        raw = Path(paths[kind]).read_bytes()
        require(len(raw) <= 10000000, 'evidence_too_large')
        proofs[kind] = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique,
                                  parse_constant=lambda _: require(False, 'invalid_json_number'))
        bound[kind] = dict(path=str(Path(paths[kind]).resolve()), sha256=hashlib.sha256(raw).hexdigest())
    validate(proofs)
    return proofs, bound


def protected_snapshot(m, db):
    from sqlalchemy import or_, select
    table = m.Order.__table__
    orders = list(db.execute(select(table).where(or_(table.c.id == PROTECTED,
                                                   table.c.order_no == PROTECTED))).mappings())
    require(len(orders) == 1, 'protected_count_changed')
    order = dict(orders[0])
    payload = dict(order=order)
    for model, condition in ((m.Product, m.Product.id == order['product_id']),
            (m.Payment, m.Payment.order_id == order['id']), (m.Refund, m.Refund.order_id == order['id'])):
        payload[model.__tablename__] = sorted((dict(r) for r in db.execute(
            select(model.__table__).where(condition)).mappings()), key=lambda r: r['id'])
    require(len(payload[m.Product.__tablename__]) == 1, 'protected_product_missing')
    return dict(count=1, sha256=hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest())


def submit(m, paths, output):
    from sqlalchemy import select
    proofs, bound = load_evidence(paths)
    verify_sources(proofs, m.__file__)
    digest = hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream, m.SessionLocal() as db:
        actors = db.scalars(select(m.User).where(m.User.email == 'admin@market.local')).all()
        require(len(actors) == 1 and actors[0].platform_role == 'super_admin'
                and actors[0].is_active and actors[0].activation_status == 'active', 'actual_admin_required')
        actor = actors[0]
        tasks = db.scalars(select(m.DevelopmentTask).where(m.DevelopmentTask.code.in_(CODES))
                           .order_by(m.DevelopmentTask.code).with_for_update()).all()
        require({t.code for t in tasks} == set(CODES) and len(tasks) == 3
                and all(t.status == 'in_progress' for t in tasks), 'three_active_tasks_required')
        protected = protected_snapshot(m, db)
        # Recheck disk bindings and installed source before the first mutation.
        for item in bound.values():
            require(hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() == item['sha256'], 'evidence_changed')
        verify_sources(proofs, m.__file__)
        at, changes = m.now(), []
        note = ('Current execution record: ' + at.isoformat() + '; evidence_bundle_sha256=' + digest
                + '; evidence_sha256=' + json.dumps({k: v['sha256'] for k, v in bound.items()}, sort_keys=True)
                + '; financial combination uses real handlers/PostgreSQL; SaaS uses deployed OAuth/HTTP '
                  'but is not a complete browser journey. FILE storage and monthly supplier are synthetic; '
                  'funds simulated. Submitted as review for user frontend acceptance; no bank or complete '
                  'browser automation acceptance is claimed.')
        for task in tasks:
            before = dict(status=task.status, progress=task.progress, acceptance=task.acceptance)
            task.status, task.progress, task.updated_at = 'review', 100, at
            task.acceptance = (task.acceptance or '') + '\n' + note
            after = dict(status=task.status, progress=task.progress, acceptance=task.acceptance,
                         evidence_sha256={k: v['sha256'] for k, v in bound.items()}, evidence_bundle_sha256=digest)
            m.audit(db, actor.email, 'submit_r3_refund_review', 'development_task', task.code,
                    note, before=before, after=after)
            changes.append(dict(code=task.code, before=before['status'], after='review'))
        db.flush()
        require(protected_snapshot(m, db) == protected, 'protected_fingerprint_changed')
        states = db.scalars(select(m.DevelopmentTask.status)).all()
        counts = {s: states.count(s) for s in ('todo', 'in_progress', 'review', 'blocked', 'done')}
        result = dict(status='submitted_for_review', changed=3, actor=actor.email, counts=counts,
            total=len(states), changes=changes, evidence=bound, evidence_bundle_sha256=digest,
            protected_before=protected, protected_after=protected, captured_at=at.isoformat())
        db.commit()
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write('\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for kind in KINDS:
        parser.add_argument('--' + kind, required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    require(not os.path.lexists(args.output), 'output_already_exists')
    paths = {k: getattr(args, k) for k in KINDS}
    load_evidence(paths)  # No app import or DB connection for missing/failed evidence.
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    result = submit(m, paths, args.output)
    print(json.dumps({k: result[k] for k in ('status', 'changed', 'total', 'counts')}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps(dict(status='refused', error_type=type(exc).__name__)))
        raise SystemExit(1)
