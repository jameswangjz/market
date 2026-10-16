"""Submit OPS002/ARC002 for review; retain OPS003 in_progress/90, never done.

Run inside the installed app. --inventory --restore --archive --checklist
--output are required; --snapshot overrides the archive-bound snapshot path
(otherwise relative to cwd). Copy that public snapshot into the pod as needed.
No network, deployment, migration, Redis reset or remote transport is performed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

CODES = ('TRD-R3-OPS-002', 'TRD-R3-ARC-002', 'TRD-R3-OPS-003')
PROTECTED = 'ORD-261012005144E1B3'
SHA = re.compile(r'[0-9a-f]{64}\Z')
SECRET = re.compile(r'\b(?:Bearer|Basic)\s+\S+|-----BEGIN .*PRIVATE KEY-----|'
                    r'://[^\s/@:]+:[^\s/@]+@|\beyJ[\w-]+\.[\w-]+\.[\w-]+|'
                    r'\b(?:sk-|gh[pousr]_)[\w-]{16,}|'
                    r'(?:password|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[=:]\s*\S+', re.I)


def require(ok, code):
    if not ok:
        raise ValueError(code)


def public(value):
    if isinstance(value, dict):
        for key, item in value.items():
            require(not re.search(r'token|secret|password|authorization|cookie|credential|privatekey|apikey',
                                  re.sub('[^a-z0-9]', '', key.lower()))
                    or item is None or type(item) in (bool, int, float)
                    or (isinstance(item, str) and SHA.fullmatch(item)), 'secret_key')
            public(item)
    elif isinstance(value, list):
        for item in value:
            public(item)
    elif isinstance(value, str):
        require(not SECRET.search(value), 'secret_value')


def read(path):
    path = Path(path).absolute()
    require('..' not in path.parts and not any(p.is_symlink() for p in (path, *path.parents)), 'unsafe_path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= 10000000, 'unsafe_file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(10000001)
        after = os.fstat(fd)
        require((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                and len(raw) == before.st_size, 'unstable_file')
        return raw
    finally:
        os.close(fd)


def decode(raw):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            require(key not in result, 'duplicate_key')
            result[key] = item
        return result
    value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique,
                       parse_constant=lambda _: require(False, 'invalid_number'))
    require(isinstance(value, dict), 'object_required')
    public(value)
    return value


def validate(proofs):
    inventory, restore, archive = (proofs[k] for k in ('inventory', 'restore', 'archive'))
    require(inventory.get('status') == restore.get('status') == 'passed', 'evidence_not_passed')
    require(inventory.get('database_mode') == 'READ ONLY', 'inventory_not_readonly')
    migration = inventory.get('migration', {})
    require(migration.get('executed') is False and migration.get('approved') is False
            and migration.get('requires_explicit_user_approval') is True, 'migration_boundary')
    candidates = inventory.get('legacy_candidates')
    require(isinstance(candidates, list) and candidates and all(type(c.get('product_count')) is int
            and c['product_count'] == 0 and c.get('linked_order_count') == 0 for c in candidates), 'legacy_candidates')
    protected = inventory.get('protected_order', {})
    require(protected.get('order_no') == PROTECTED and isinstance(protected.get('sha256'), str)
            and SHA.fullmatch(protected['sha256']), 'protected_inventory')
    for table in ('orders', 'products', 'payments', 'refunds'):
        row = protected.get(table, {})
        require(type(row.get('count')) is int and row['count'] >= 0
                and isinstance(row.get('sha256'), str) and SHA.fullmatch(row['sha256']), 'protected_fingerprint')
    require(protected['orders']['count'] == protected['products']['count'] == 1, 'protected_count')
    tickets = inventory.get('tickets', {})
    rows = tickets.get('missing_evidence_review')
    require(isinstance(rows, list) and type(tickets.get('review_candidate_count')) is int
            and tickets['review_candidate_count'] == len(rows)
            and all(r.get('disposition') == 'review_only' for r in rows), 'ticket_review_only')
    require(restore.get('restore_status') == 'passed' and restore.get('network') == 'none'
            and all(restore.get(k) is True for k in ('cleanup_verified', 'observations_equal'))
            and restore.get('historical_migration_executed') is False
            and restore.get('before') == restore.get('after') and bool(restore.get('before')), 'restore_gate')
    require(isinstance(restore.get('backup_sha256'), str) and SHA.fullmatch(restore['backup_sha256'])
            and restore.get('approval_manifest', {}).get('approved') is False, 'backup_gate')
    require(archive.get('status') == 'passed_with_findings' and archive.get('release_ready') is False
            and archive.get('blockers') == ['api_charge_source_continuity_lost']
            and archive.get('affected_worker') == 'api-charge-ledger-worker', 'archive_gate')


def load_evidence(paths, snapshot_path=None):
    require(set(paths) == {'inventory', 'restore', 'archive', 'checklist'}, 'input_set')
    raw = {k: read(v) for k, v in paths.items()}
    proofs = {k: decode(v) for k, v in raw.items() if k != 'checklist'}
    text = raw['checklist'].decode('utf-8-sig')
    require(bool(text.strip()) and any(line.startswith('# ') and line[2:].strip() for line in text.splitlines()), 'checklist_header')
    public(text)
    validate(proofs)
    binding = proofs['archive']['evidence']['snapshot']
    path = snapshot_path if snapshot_path is not None else binding['path']
    raw['snapshot'] = read(path)
    require(hashlib.sha256(raw['snapshot']).hexdigest() == binding['sha256'], 'snapshot_binding')
    proofs['snapshot'] = decode(raw['snapshot'])
    require(proofs['snapshot'].get('deployed_commit') == proofs['archive'].get('deployed_commit')
            and proofs['snapshot'].get('backup', {}).get('sha256') == proofs['restore']['backup_sha256'], 'snapshot_identity')
    all_paths = {**paths, 'snapshot': path}
    bound = {k: dict(path=str(Path(all_paths[k]).absolute()), sha256=hashlib.sha256(v).hexdigest()) for k, v in raw.items()}
    return proofs, bound


def verify_sources(proofs, main_path):
    root = Path(main_path).absolute().parent
    sources = proofs['snapshot'].get('source_sha256')
    require(isinstance(sources, dict) and 'main.py' in sources
            and set(sources) == {p.name for p in root.glob('*.py')}, 'source_inventory')
    for name, digest in sources.items():
        require(isinstance(name, str) and Path(name).name == name and name.endswith('.py')
                and isinstance(digest, str) and SHA.fullmatch(digest)
                and hashlib.sha256(read(root / name)).hexdigest() == digest, 'installed_source_mismatch')


def protected_snapshot(m, db):
    from sqlalchemy import or_, select
    table = m.Order.__table__
    orders = list(db.execute(select(table).where(or_(table.c.id == PROTECTED, table.c.order_no == PROTECTED))).mappings())
    require(len(orders) == 1, 'protected_count')
    order = dict(orders[0])
    payload = dict(order=order)
    for model, condition in ((m.Product, m.Product.id == order['product_id']),
            (m.Payment, m.Payment.order_id == order['id']), (m.Refund, m.Refund.order_id == order['id'])):
        payload[model.__tablename__] = sorted((dict(r) for r in db.execute(select(model.__table__).where(condition)).mappings()), key=lambda r: r['id'])
    require(len(payload[m.Product.__tablename__]) == 1, 'protected_product')
    return dict(count=1, sha256=hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest())


def submit(m, paths, output, snapshot_path=None):
    from sqlalchemy import select
    from r3_ops_readonly_inventory import collect, current_types, inventory
    proofs, bound = load_evidence(paths, snapshot_path)
    verify_sources(proofs, m.__file__)
    types = current_types(Path(m.__file__).parent)
    def guard(observed):
        for key in ('protected_order', 'all_frozen_economics', 'legacy_candidates', 'tickets', 'current_product_types'):
            require(observed.get(key) == proofs['inventory'].get(key), 'live_inventory_mismatch')
    guard(collect(m.engine, types))
    output = Path(output).absolute()
    require(not any(p.is_symlink() for p in output.parents), 'unsafe_output')
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream, m.SessionLocal() as db:
        actors = db.scalars(select(m.User).where(m.User.email == 'admin@market.local')).all()
        require(len(actors) == 1 and actors[0].platform_role == 'super_admin' and actors[0].is_active
                and actors[0].activation_status == 'active', 'actual_admin_required')
        tasks = db.scalars(select(m.DevelopmentTask).where(m.DevelopmentTask.code.in_(CODES))
                           .order_by(m.DevelopmentTask.code).with_for_update()).all()
        require(len(tasks) == 3 and {t.code for t in tasks} == set(CODES)
                and all(t.status == 'in_progress' for t in tasks), 'three_active_tasks_required')
        protected = protected_snapshot(m, db)
        guard(inventory(db.connection(), types))
        for item in bound.values():
            require(hashlib.sha256(read(item['path'])).hexdigest() == item['sha256'], 'evidence_changed')
        verify_sources(proofs, m.__file__)
        at, changes = m.now(), []
        note = ('OPS review evidence_sha256=' + json.dumps({k: v['sha256'] for k, v in bound.items()}, sort_keys=True)
                + '; OPS002/ARC002 submitted for review only. No historical migration approved or executed; '
                  'historical tickets review_only. OPS003 remains in_progress at 90, waiting for explicit user '
                  'confirmation of Redis source continuity recovery versus a new epoch; no reset authorized. '
                  'Release not ready; simulated bank/storage/monthly provider boundaries remain; no browser claims.')
        for task in tasks:
            before = dict(status=task.status, progress=task.progress, acceptance=task.acceptance)
            task.status, task.progress = ('in_progress', 90) if task.code == CODES[2] else ('review', 100)
            task.acceptance, task.updated_at = (task.acceptance or '') + '\n' + note, at
            after = dict(status=task.status, progress=task.progress, acceptance=task.acceptance,
                         evidence_sha256={k: v['sha256'] for k, v in bound.items()})
            m.audit(db, actors[0].email, 'submit_r3_ops_review', 'development_task', task.code, note, before=before, after=after)
            changes.append(dict(code=task.code, before=before, after=after))
        db.flush()
        guard(inventory(db.connection(), types))
        require(protected_snapshot(m, db) == protected, 'protected_fingerprint_changed')
        result = dict(status='submitted_for_review', changed=3, review_submitted=2, release_ready=False,
                      changes=changes, evidence=bound, protected_before=protected, protected_after=protected,
                      captured_at=at.isoformat(), historical_migration_executed=False)
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
        db.commit()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('inventory', 'restore', 'archive', 'checklist', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--snapshot', type=Path)
    args = parser.parse_args(argv)
    require(not os.path.lexists(args.output), 'output_exists')
    paths = {k: getattr(args, k) for k in ('inventory', 'restore', 'archive', 'checklist')}
    load_evidence(paths, args.snapshot)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    submit(m, paths, args.output, args.snapshot)
    print(json.dumps(dict(status='submitted_for_review', review_submitted=2, release_ready=False)))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps(dict(status='refused', error_type=type(exc).__name__)))
        raise SystemExit(1)
