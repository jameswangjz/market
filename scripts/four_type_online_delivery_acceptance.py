"""Scoped TRD-BE-012/TRD-QA-003 real four-type runtime acceptance.

Runs only when explicitly invoked in the existing API image. No deployment,
lifespan, global queue, financial clearing, or task-management mutations.
No runtime mutations until the parent invokes --parent-reviewed.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import secrets
import time
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

import api_auto_delivery_runtime_acceptance as api
import version_change_saas_runtime_acceptance as saas
import online_delivery_acceptance as online
import version_change_quote_acceptance as guarded

PROTECTED_PRODUCT = 'qa-vcq-a044ea9ea181-p2'
STAGE = 'startup'
EICAR = (b'X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-'
         b'ANTIVIRUS-TEST-FILE!$H+H*')
REVIEW_STEPS = (
    ('business', 'review', 'pending_review', 'quality_review', 'business_approve_product'),
    ('quality', 'review', 'quality_review', 'security_review', 'quality_approve_product'),
    ('security', 'security-review', 'security_review', 'operation_review', 'approve_product_security'),
    ('release', 'review', 'operation_review', 'published', 'operation_approve_product'),
)
OBSERVATIONAL_TABLES = frozenset({
    'application_health_observations', 'application_health_slots',
    'notification_messages', 'notification_recipients', 'notification_deliveries',
    'notification_delivery_attempts', 'notification_expired_urgent',
    'platform_notifications',
})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def protected_snapshot(m):
    """Column digests allow exact diffs without saving protected secrets or amounts."""
    from sqlalchemy import or_, select, text
    tables = m.Base.metadata.tables
    with m.SessionLocal() as db:
        if m.engine.dialect.name == 'postgresql':
            db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        order = db.scalar(select(m.Order).where(or_(m.Order.id == saas.PROTECTED_ORDER,
                                                   m.Order.order_no == saas.PROTECTED_ORDER)))
        api.check(order is not None, 'Protected order missing')
        product_ids = {PROTECTED_PRODUCT, order.product_id}
        api.check(set(db.scalars(select(m.Product.id).where(m.Product.id.in_(product_ids)))) == product_ids,
                  'Protected product missing')
        predicates = {m.Product.__tablename__: m.Product.id.in_(product_ids),
                      m.Order.__tablename__: m.Order.id == order.id}
        previous = None
        while True:
            keys = {column: set(db.scalars(select(column).where(predicate)))
                    for name, predicate in predicates.items()
                    for column in tables[name].primary_key.columns}
            if keys == previous:
                break
            previous = keys
            for table in tables.values():
                children = [fk.parent.in_(keys[fk.column]) for fk in table.foreign_keys
                            if keys.get(fk.column)]
                if children:
                    predicates[table.name] = or_(*children, *(
                        [predicates[table.name]] if table.name in predicates else []))
        # Polymorphic notifications are observational, but still protected from deletion.
        targets = {value for values in keys.values() for value in values if isinstance(value, str)}
        Message = m.message_center['Message']
        predicates[Message.__tablename__] = or_(Message.target_id.in_(targets), *(
            [predicates[Message.__tablename__]] if Message.__tablename__ in predicates else []))
        message_ids = list(db.scalars(select(Message.id).where(predicates[Message.__tablename__])))
        for key in ('Receipt', 'Delivery', 'ExpiredUrgent'):
            model = m.message_center[key]
            predicates[model.__tablename__] = or_(model.message_id.in_(message_ids), *(
                [predicates[model.__tablename__]] if model.__tablename__ in predicates else []))
        # Include all descendants of those exact notification rows, never their User parents.
        previous = None
        while True:
            keys = {column: set(db.scalars(select(column).where(predicate)))
                    for name, predicate in predicates.items()
                    for column in tables[name].primary_key.columns}
            if keys == previous:
                break
            previous = keys
            for table in tables.values():
                children = [fk.parent.in_(keys[fk.column]) for fk in table.foreign_keys
                            if keys.get(fk.column)]
                if children:
                    predicates[table.name] = or_(*children, *(
                        [predicates[table.name]] if table.name in predicates else []))
        business, observational = {}, {}
        for name, predicate in predicates.items():
            table = tables[name]
            rows = {}
            for row in db.execute(select(table).where(predicate)).mappings():
                identity = json.dumps([row[c.name] for c in table.primary_key.columns], default=str)
                rows[identity] = {column.name: digest(row[column.name]) for column in table.columns}
            if rows:
                (observational if name in OBSERVATIONAL_TABLES else business)[name] = rows
    return {'business': business, 'business_hash': digest(business),
            'observational': observational, 'observational_hash': digest(observational)}


def snapshot_differences(before, after):
    differences = {}
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name, {}), after.get(name, {})
        changed = {identity: [column for column in sorted(set(old[identity]) | set(new[identity]))
                              if old[identity].get(column) != new[identity].get(column)]
                   for identity in old.keys() & new.keys() if old[identity] != new[identity]}
        if old != new:
            differences[name] = {'added': sorted(new.keys() - old.keys()),
                                 'removed': sorted(old.keys() - new.keys()), 'changed_columns': changed}
    return differences


def assert_protected_core(expected, current):
    api.check(expected['business_hash'] == current['business_hash'],
              'Protected core business snapshot changed; observational recovery cannot authorize this')


def message_edge_ownership(message_id, user_id, owned_messages, fixture_users):
    if message_id in owned_messages:
        return 'owned_message'
    api.check(user_id in fixture_users, 'File cleanup reached an unowned message recipient')
    return 'fixture_user_external_message'


def reject_protected_candidates(m, db, predicates, protected):
    from sqlalchemy import select
    protected_rows = {**protected['business'], **protected['observational']}
    for name, predicate in predicates.items():
        table = m.Base.metadata.tables[name]
        candidates = {json.dumps(list(row), default=str) for row in db.execute(
            select(*table.primary_key.columns).where(predicate))}
        api.check(not candidates.intersection(protected_rows.get(name, {})),
                  'Cleanup ownership graph intersects marked protected rows')


def validate_protected_state(state):
    api.check(isinstance(state, dict) and set(state) == {
        'business', 'business_hash', 'observational', 'observational_hash'}, 'Invalid protected snapshot fields')
    for label in ('business', 'observational'):
        api.check(isinstance(state[label], dict) and digest(state[label]) == state[label + '_hash'],
                  'Protected snapshot integrity mismatch')
        for name, rows in state[label].items():
            api.check(isinstance(name, str) and isinstance(rows, dict), 'Invalid protected table snapshot')
            for identity, columns in rows.items():
                api.check(isinstance(identity, str) and isinstance(json.loads(identity), list) and
                          isinstance(columns, dict) and all(isinstance(column, str) and
                          isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)
                          for column, value in columns.items()), 'Invalid protected column snapshot')


def cleanup_owned_children(m, saved, protected):
    """Validate every child before any mutation, then retain a delete-time guard."""
    from sqlalchemy import event
    children = []
    for key, helper in (('api', api), ('retry', api), ('saas', saas), ('file', None)):
        child = Path(saved[key])
        if not child.exists() and not child.is_symlink():
            continue
        ids = helper.read_manifest(child) if helper else read_private(child)
        api.check(ids.get('authorized') is True, 'Child cleanup not authorized')
        if helper is api:
            api.check(ids.get('environment') == api.environment(m), 'Child cleanup environment mismatch')
            with m.SessionLocal() as db:
                predicates = api.owned_scopes(m, db, ids)
                reject_protected_candidates(m, db, predicates, protected)
        elif helper is saas:
            api.check(ids.get('environment') == saas.environment_fingerprint(m), 'Child cleanup environment mismatch')
            owned = saas.owned_rows(m, ids)
            with m.SessionLocal() as db:
                predicates = {name: guarded.row_predicate(m.Base.metadata.tables[name], rows)
                              for name, rows in owned.items()}
                reject_protected_candidates(m, db, predicates, protected)
        else:
            validate_file_journal(ids)
            api.check(ids['environment'] == file_environment(m) and ids['bucket'] == m.MINIO_BUCKET,
                      'Child file environment mismatch')
            with m.SessionLocal() as db:
                predicates, _ = file_scopes(m, db, ids)
                reject_protected_candidates(m, db, predicates, protected)
        children.append((helper, ids, child))

    def guard_delete(execution):
        if execution.is_delete:
            statement = execution.statement
            name = statement.table.name
            api.check(bool(statement._where_criteria), 'Unscoped cleanup delete refused')
            reject_protected_candidates(m, execution.session,
                                        {name: statement.whereclause}, protected)
    event.listen(m.SessionLocal.class_, 'do_orm_execute', guard_delete)
    try:
        for helper, ids, child in children:
            if helper:
                helper.cleanup(m, ids, child)
            else:
                cleanup_file(m, ids, child)
    finally:
        event.remove(m.SessionLocal.class_, 'do_orm_execute', guard_delete)


def inspect_protected(args):
    path = Path(args.manifest).absolute()
    saved = read_private(path)
    validate_journal(saved, path)
    m = api.load_main()
    api.check(saved['environment'] == api.environment(m), 'Inspection environment mismatch')
    current = protected_snapshot(m)
    with m.SessionLocal() as db:
        order_hash = saas.fingerprint(db, m)
    return {'status': 'inspection_only', 'current_business_hash': current['business_hash'],
            'current_protected_order_fingerprint': order_hash,
            'legacy_full_fingerprint_matches': protected_fingerprint(m) == saved['protected'],
            'historical_core_comparison': 'available' if 'protected_state' in saved else 'unavailable in hash-only root',
            'business_changes': snapshot_differences(saved['protected_state']['business'], current['business'])
                if 'protected_state' in saved else None,
            'observational_tables': {name: len(rows) for name, rows in current['observational'].items()}}


def inspect_file_message_edges(args):
    from sqlalchemy import select
    path = Path(args.manifest).absolute()
    saved = read_private(path)
    validate_journal(saved, path)
    m = api.load_main()
    api.check(saved['environment'] == api.environment(m), 'Inspection environment mismatch')
    ids = read_private(Path(saved['file']))
    validate_file_journal(ids)
    api.check(ids['environment'] == file_environment(m), 'Inspection file environment mismatch')
    summary = []
    with m.SessionLocal() as db:
        predicates, _ = file_scopes(m, db, ids)
        Message = m.message_center['Message']
        messages = set(db.scalars(select(Message.id).where(predicates[Message.__tablename__]))) \
            if Message.__tablename__ in predicates else set()
        for key in ('Receipt', 'Delivery', 'ExpiredUrgent'):
            table = m.message_center[key].__table__
            if table.name not in predicates:
                continue
            for row in db.execute(select(table).where(predicates[table.name])).mappings():
                summary.append({'table': table.name, 'pk': [row[c.name] for c in table.primary_key.columns],
                    'message_id': row['message_id'], 'user_id': row['user_id'],
                    'ownership': message_edge_ownership(row['message_id'], row['user_id'], messages,
                                                       set(ids['users'].values()))})
    return {'status': 'inspection_only', 'resolved_file_journal': saved['file'],
            'owned_message_ids': sorted(messages), 'classified_edges': summary,
            'external_message_rows_are_not_cleanup_candidates': True}


def recover_observational_drift(args):
    global STAGE
    STAGE = 'observational-drift-trusted-baseline-preflight'
    api.check(args.cleanup and args.recover_observational_drift and args.parent_reviewed,
              'Recovery requires cleanup, explicit observational drift flag and parent review')
    api.check(bool(getattr(args, 'trusted_baseline', None)), 'Trusted earlier accepted baseline artifact required')
    path = Path(args.manifest).absolute()
    original = read_private(path)
    validate_journal(original, path)
    api.check('protected_state' not in original, 'New snapshot journals use normal core-verified cleanup')
    trusted = read_private(Path(args.trusted_baseline))
    trusted_hash = trusted.get('protected_order_fingerprint')
    api.check(isinstance(trusted_hash, str) and re.fullmatch(r'[0-9a-f]{64}', trusted_hash),
              'Trusted artifact lacks protected order/product/payment/refund fingerprint')
    m = api.load_main()
    api.check(original['environment'] == api.environment(m), 'Recovery environment mismatch')
    with m.SessionLocal() as db:
        api.check(saas.fingerprint(db, m) == trusted_hash,
                  'Protected core mismatches trusted earlier baseline; cleanup refused')
    protected = protected_snapshot(m)
    original_bytes_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    report_path = Path(getattr(args, 'recovery_report', None) or str(path) + '.recovery.json')
    api.check(report_path.absolute() not in {path, Path(args.trusted_baseline).absolute(),
              *(Path(original[key]) for key in ('api', 'retry', 'saas', 'file'))}, 'Recovery report collides with evidence')
    report = {'status': 'recovery_preflight', 'original_acceptance': 'failed',
              'original_root_hash': original['protected'], 'original_root_bytes_sha256': original_bytes_hash,
              'legacy_full_fingerprint_matches': protected_fingerprint(m) == original['protected'],
              'trusted_artifact_sha256': digest(trusted), 'trusted_protected_order_fingerprint': trusted_hash,
              'protected_core_before': protected['business_hash'],
              'historical_protected_product_graph_comparison': 'unavailable in old hash-only root; no baseline reset',
              'observational_tables': {name: len(rows) for name, rows in protected['observational'].items()}}
    api.write_manifest(report_path, report, create=True)
    try:
        STAGE = 'observational-drift-owned-only-cleanup'
        cleanup_owned_children(m, original, protected)
        current = protected_snapshot(m)
        assert_protected_core(protected, current)
        with m.SessionLocal() as db:
            api.check(saas.fingerprint(db, m) == trusted_hash, 'Trusted protected core changed during recovery')
        api.check(hashlib.sha256(path.read_bytes()).hexdigest() == original_bytes_hash,
                  'Original hash-only root evidence changed during recovery')
        report.update(status='recovered_cleanup', protected_core_after=current['business_hash'],
                      trusted_core_verified_pre_post=True,
                      observational_changes=snapshot_differences(protected['observational'], current['observational']))
    except BaseException as exc:
        current = protected_snapshot(m)
        with m.SessionLocal() as db:
            trusted_unchanged = saas.fingerprint(db, m) == trusted_hash
        report.update(status='failed_cleanup', error_type=type(exc).__name__,
                      protected_core_after=current['business_hash'],
                      trusted_core_verified_pre_post=trusted_unchanged,
                      protected_business_unchanged=protected['business_hash'] == current['business_hash'],
                      original_root_unchanged=hashlib.sha256(path.read_bytes()).hexdigest() == original_bytes_hash)
        api.write_manifest(report_path, report)
        raise
    api.write_manifest(report_path, report)
    return {**report, 'recovery_report': str(report_path), 'scope': 'owned cleanup only; original acceptance remains failed'}


def read_private(path):
    import os
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        mode = os.fstat(stream.fileno()).st_mode
        api.check(stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600, 'Private journal required')
        return json.load(stream)


def archive_payload(content=b'Contact acceptance.person@example.com for evidence.\n'):
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('evidence.txt', content)
    return stream.getvalue()


def assert_scan_report(report):
    api.check(report.get('clamav_status') == 'clean' and bool(report.get('clamav_report'))
              and report.get('presidio_status') == 'available' and report.get('sample_scanned') is True,
              'Persisted archive scan report is not ready')
    api.check(any(item.get('entity') == 'EMAIL_ADDRESS' and
                  item.get('matched_text') == 'acceptance.person@example.com' and
                  item.get('source') == 'presidio'
                  for item in report.get('presidio_findings', [])),
              'Persisted archive report lacks real Presidio email detection')


def file_ids(marker=None):
    marker = marker or 'qa-four-file-' + secrets.token_hex(6)
    return {'run_id': marker,
            'users': {role: marker + '-' + role for role in
                      ('super', 'admin', 'member', 'other', 'provider', 'operator',
                       'business', 'quality', 'security', 'release')},
            'enterprises': [marker + '-' + role for role in ('buyer', 'seller', 'outside')],
            'products': [marker + '-p0'], 'versions': [marker + '-v0'],
            'request_ids': [marker + '-http']}


def file_environment(m):
    values = [api.environment(m), m.MINIO_ENDPOINT, m.MINIO_BUCKET,
              m.MINIO_ACCESS_KEY, m.MINIO_SECRET_KEY]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def validate_file_journal(ids):
    marker = ids.get('run_id', '')
    api.check(isinstance(marker, str) and re.fullmatch(r'qa-four-file-[0-9a-f]{12}', marker),
              'Invalid file fixture marker')
    roots = file_ids(marker)
    api.check(all(ids.get(key) == value for key, value in roots.items()), 'File roots changed')
    api.check(set(ids) <= set(roots) | {'files', 'logos', 'object_keys', 'environment', 'bucket',
              'authorized', 'cleaned', 'database_ids'}, 'Unknown file journal fields')
    api.check(isinstance(ids.get('files'), list) and len(ids['files']) <= 1 and
              all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value)
                  for value in ids['files']), 'Invalid generated file IDs')
    keys = ids.get('object_keys')
    api.check(isinstance(keys, list) and len(keys) <= 3 and len(set(keys)) == len(keys) and all(
        isinstance(key, str) and re.fullmatch(re.escape(ids['users']['provider']) +
        r'/[0-9]{8}/[0-9a-f]{12}-' + re.escape(marker) + r'(\.zip|\.logo\.png(?:\.thumb\.png)?)', key) for key in keys),
        'Invalid owned upload object key')
    logos = ids.get('logos', {})
    api.check(isinstance(logos, dict) and set(logos) <= {'product_logo', 'product_logo_thumbnail'}
              and all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value)
                      for value in logos.values()), 'Invalid generated logo IDs')
    api.check(len(set(ids['files'] + list(logos.values()))) == len(ids['files']) + len(logos)
              and len(ids['files']) + len(logos) <= len(keys), 'File lacks unique pre-upload object journal')
    api.check(isinstance(ids.get('environment'), str) and
              re.fullmatch(r'[0-9a-f]{64}', ids['environment']), 'Invalid file environment fingerprint')


def file_scopes(m, db, ids):
    from sqlalchemy import select
    validate_file_journal(ids)
    scope = {'run_id': ids['run_id'], 'users': dict(ids['users'], buyer=ids['users']['super']),
             'enterprises': ids['enterprises'], 'products': {'file': ids['products'][0]},
             'versions': {'file': {'source': ids['versions'][0]}}, 'request_ids': ids['request_ids']}
    owned = guarded.owned_rows(m, db, scope)
    predicates = {name: guarded.row_predicate(m.Base.metadata.tables[name], rows)
                  for name, rows in owned.items()}
    keys = {column: [row[index] for row in owned[name]] for name in owned
            for index, column in enumerate(m.Base.metadata.tables[name].primary_key.columns)}
    asset_ids = ids['files'] + list(ids.get('logos', {}).values())
    for model, allowed in ((m.User, ids['users'].values()), (m.Enterprise, ids['enterprises']),
                           (m.Product, ids['products']), (m.ProductReleaseVersion, ids['versions']),
                           (m.FileObject, asset_ids)):
        api.check(set(keys.get(model.__table__.c.id, [])) <= set(allowed),
                  'File cleanup reached an external root')
    if m.FileObject.__tablename__ in predicates:
        for item in db.scalars(select(m.FileObject).where(predicates[m.FileObject.__tablename__])):
            is_dataset = item.id in ids['files']
            expected_role = 'product_data' if is_dataset else next(
                role for role, fid in ids['logos'].items() if fid == item.id)
            api.check(item.product_id == ids['products'][0] and item.owner_id == ids['users']['provider']
                      and item.object_name in ids['object_keys'] and item.file_role == expected_role and
                      item.version_id == (ids['versions'][0] if is_dataset else None),
                      'File cleanup asset ownership mismatch')
    order_predicate = predicates.get(m.Order.__tablename__)
    order_ids = set()
    if order_predicate is not None:
        for row in db.scalars(select(m.Order).where(order_predicate)):
            api.check(row.id != saas.PROTECTED_ORDER and row.order_no != saas.PROTECTED_ORDER and
                      row.product_id == ids['products'][0] and row.product_version_id == ids['versions'][0]
                      and row.buyer_user_id == ids['users']['super'] and
                      row.buyer_enterprise_id == ids['enterprises'][0] and
                      row.provider_enterprise_id == ids['enterprises'][1],
                      'File cleanup reached an external or protected order')
            order_ids.add(row.id)
    allowed = {m.User.__tablename__: set(ids['users'].values()),
               m.Enterprise.__tablename__: set(ids['enterprises']),
               m.Product.__tablename__: set(ids['products']),
               m.ProductReleaseVersion.__tablename__: set(ids['versions']),
               m.FileObject.__tablename__: set(asset_ids), m.Order.__tablename__: order_ids}
    Message = m.message_center['Message']
    messages = set(keys.get(Message.__table__.c.id, []))
    task_ids = set(keys.get(m.DeliveryTask.__table__.c.id, []))
    if Message.__tablename__ in predicates:
        for message in db.scalars(select(Message).where(predicates[Message.__tablename__])):
            if message.target_type == 'product':
                api.check(message.target_id in ids['products'] and message.category == 'review' and
                          message.tenant_id == ids['enterprises'][1] and
                          message.event_key.startswith('review:product:' + message.target_id + ':'),
                          'Review message lacks owned product target')
                continue
            api.check(message.target_type in {'order', 'delivery_task'} and
                      message.target_id in (order_ids if message.target_type == 'order' else task_ids) and
                      message.tenant_id == ids['enterprises'][0], 'File message lacks owned business target')
    recipient_tables = {m.message_center[key].__tablename__ for key in ('Receipt', 'Delivery', 'ExpiredUrgent')}
    for name, predicate in predicates.items():
        table = m.Base.metadata.tables[name]
        if name in recipient_tables:
            for edge in db.execute(select(table).where(predicate)).mappings():
                message_edge_ownership(edge['message_id'], edge['user_id'], messages, allowed[m.User.__tablename__])
                if name == m.message_center['ExpiredUrgent'].__tablename__:
                    receipt = db.get(m.message_center['Receipt'], edge['receipt_id'])
                    api.check(receipt is None or (receipt.message_id == edge['message_id'] and
                              receipt.user_id == edge['user_id']), 'Expired transport edge metadata mismatch')
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            if parent not in allowed:
                continue
            if name in recipient_tables and parent == m.User.__tablename__ and fk.parent.name == 'user_id':
                continue  # Exact fixture messages may have real platform-role recipients.
            api.check(set(db.scalars(select(fk.parent).where(predicate))) - {None} <= allowed[parent],
                      'File descendant references an external root')
    return predicates, keys


def cleanup_file(m, ids, path):
    from sqlalchemy import delete, func, select, text
    validate_file_journal(ids)
    api.check(ids.get('authorized') is True and ids['environment'] == file_environment(m)
              and ids.get('bucket') == m.MINIO_BUCKET, 'File cleanup environment/authorization mismatch')
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY,
                      secret_key=m.MINIO_SECRET_KEY, secure=False)
    with m.SessionLocal() as db:
        db.execute(text("SET LOCAL lock_timeout = '15s'"))
        db.scalars(select(m.Order).where(m.Order.product_id.in_(ids['products']))
                   .order_by(m.Order.id).with_for_update()).all()
        predicates, _ = file_scopes(m, db, ids)
        ids['database_ids'] = {name: [list(row) for row in db.execute(
            select(*m.Base.metadata.tables[name].primary_key.columns).where(predicate))]
            for name, predicate in predicates.items()}
        api.write_manifest(path, ids)
        # Objects go first; an interrupted cleanup retains DB ownership proofs.
        for key in ids['object_keys']:
            storage.remove_object(m.MINIO_BUCKET, key)
            api.check(online.object_absent(storage, m.MINIO_BUCKET, key), 'Owned upload object remains')
        for table in reversed(m.Base.metadata.sorted_tables):
            if table.name in predicates:
                db.execute(delete(table).where(predicates[table.name]))
        db.commit()
    with m.SessionLocal() as db:
        for name, predicate in predicates.items():
            api.check(db.scalar(select(func.count()).select_from(m.Base.metadata.tables[name])
                                .where(predicate)) == 0, 'File fixture or scan report remains')
    ids['cleaned'] = True
    api.write_manifest(path, ids)


@contextmanager
def held_payment_tasks(m, order_id):
    from sqlalchemy import event
    def hold(db, *_):
        for row in db.new.union(db.dirty):
            if isinstance(row, m.DeliveryTask) and row.order_id == order_id:
                row.next_retry_at = m.now() + timedelta(hours=1)
    event.listen(m.SessionLocal.class_, 'before_flush', hold)
    try:
        yield
    finally:
        event.remove(m.SessionLocal.class_, 'before_flush', hold)


def process_online_delivery_once(m, ids, oid, expected_count, release=False):
    """Exact-order script driver for the same process handler used by process_once.

    Production eligible/verify/audit/notify/flush behavior and a real commit.
    No global queue selection; no daemon scheduling or batch-setting claim.
    """
    from sqlalchemy import select
    started = m.now()
    with m.SessionLocal() as db:
        order = db.scalar(select(m.Order).where(m.Order.id == oid).with_for_update())
        api.check(order is not None and order.product_id in ids['products'] and
                  order.id != saas.PROTECTED_ORDER and order.order_no != saas.PROTECTED_ORDER,
                  'Driver order outside owned fixture')
        tasks = db.scalars(select(m.DeliveryTask).where(m.DeliveryTask.order_id == oid)
                           .with_for_update()).all()
        api.check(len(tasks) == 1, 'Driver requires one exact owned task')
        task = tasks[0]
        api.check(task.retry_count == expected_count and task.status in {'in_delivery', 'retrying'},
                  'Another worker processed the fixture or unexpected retry state')
        if release:
            api.check(expected_count == 0, 'Only initial scheduling hold may be released')
            task.next_retry_at = m.now()
        api.check(task.next_retry_at is not None and task.next_retry_at <= m.now(), 'Owned task not due')
        actor_id = ids['users'].get('operator', ids['users'].get('ops'))
        actor = db.get(m.User, actor_id)
        api.check(actor is not None, 'Owned delivery actor missing')
        outcome = m.online_delivery['process'](db, order, task, actor, automatic=True)
        db.commit()
        finished = m.now()
        result = {'started': started.isoformat(), 'finished': finished.isoformat(),
                  'task_id': task.id, 'outcome': outcome, 'status': task.status,
                  'retry_count': task.retry_count, 'max_retries': task.max_retries,
                  'main': order.main_status, 'delivery': order.delivery_status,
                  'next_retry_at': task.next_retry_at.isoformat() if task.next_retry_at else None,
                  'error_present': bool(task.last_error)}
        if task.status == 'retrying':
            api.check(started + timedelta(seconds=10) <= task.next_retry_at <=
                      finished + timedelta(seconds=10), 'Production retry deadline is not ten seconds')
        return result


def file_initial(m, path):
    global STAGE
    from fastapi.testclient import TestClient
    from sqlalchemy import event, func, or_, select
    from sqlalchemy.orm import aliased
    ids = file_ids()
    ids.update(files=[], logos={}, object_keys=[], environment=file_environment(m), bucket=m.MINIO_BUCKET)
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY,
                      secret_key=m.MINIO_SECRET_KEY, secure=False)
    api.check(m.MINIO_ENDPOINT and storage.bucket_exists(m.MINIO_BUCKET), 'Existing MinIO bucket required')
    with m.SessionLocal() as db:
        parent = aliased(m.ProductCategory)
        category = db.scalar(select(m.ProductCategory).join(parent, m.ProductCategory.parent_id == parent.id)
                             .where(parent.parent_id.is_(None)).order_by(m.ProductCategory.id).limit(1))
        api.check(category is not None, 'Existing second-level registration category required')
        category_id = category.id
        for model, values in ((m.User, list(ids['users'].values())), (m.Enterprise, ids['enterprises']),
                              (m.Product, ids['products']), (m.ProductReleaseVersion, ids['versions'])):
            predicate = model.id.in_(values)
            if model is m.User:
                predicate = or_(predicate, model.email.in_([value + '@example.invalid' for value in values]))
            if model is m.Enterprise:
                predicate = or_(predicate, model.credit_code.in_(values))
            api.check(db.scalar(select(model.id).where(predicate).limit(1)) is None,
                      'File fixture root collision; cleanup not authorized')
    ids['authorized'] = True
    api.write_manifest(path, ids, create=True)
    with m.SessionLocal() as db:
        for eid in ids['enterprises']:
            db.add(m.Enterprise(id=eid, name=eid, credit_code=eid, verification_status='verified'))
        for role, uid in ids['users'].items():
            db.add(m.User(id=uid, name=uid, email=uid + '@example.invalid', password_hash='unused',
                          verified_status='verified', activation_status='active', is_active=True,
                          platform_role={'operator': 'delivery_monitor', 'business': 'product_manager',
                                         'quality': 'quality_reviewer', 'security': 'security_compliance',
                                         'release': 'platform_operator'}.get(role, '')))
        db.flush()
        for uid in ids['users'].values():
            db.add(m.message_center['Preference'](user_id=uid, email_enabled=False))
        for role, membership, index in (('super', 'super_admin', 0), ('admin', 'enterprise_admin', 0),
                                        ('member', 'member', 0), ('other', 'super_admin', 2),
                                        ('provider', 'super_admin', 1)):
            db.add(m.Membership(user_id=ids['users'][role], enterprise_id=ids['enterprises'][index],
                                role=membership, status='active', business_roles='provider,user'))
        db.add(m.Product(id=ids['products'][0], name=ids['products'][0],
            enterprise_id=ids['enterprises'][1], provider_name=ids['enterprises'][1],
            product_type='dataset', delivery_method='file', status='draft', download_limit=2,
            settlement_rule_mode='custom', settlement_rule_json=json.dumps({
                'platform_rate': 20, 'provider_rate': 80, 'service_rate': 0, 'expert_rate': 0, 'channel_rate': 0})))
        db.flush()
        db.add(m.ProductReleaseVersion(id=ids['versions'][0], product_id=ids['products'][0],
            version_code='v1', price=12, cost=7, status='active'))
        db.commit()
        tokens = {role: m.issue_token(db.get(m.User, uid)) for role, uid in ids['users'].items()}

    raw_scan_calls = []
    expected_logo = {}
    real_scan = m.clamav_scan_stream
    def observe_raw_scan(stream, size):
        position = stream.tell()
        content = stream.read(size)
        stream.seek(position)
        api.check(position == 0 and len(content) == size, 'Production raw scan input differs from uploaded bytes')
        started = m.now()
        result = real_scan(stream, size)
        raw_scan_calls.append({'sha256': hashlib.sha256(content).hexdigest(), 'size': size,
                               'status': result[0], 'started': started.isoformat(),
                               'finished': m.now().isoformat()})
        return result

    real_minio = m.Minio
    class JournalMinio(real_minio):
        # Forward real I/O unchanged, but journal the server-generated key before PUT.
        def put_object(self, bucket, object_name, *args, **kwargs):
            if object_name.endswith(ids['run_id'] + '.logo.png'):
                api.check(any(call['sha256'] == expected_logo.get('sha256') and
                              call['size'] == expected_logo.get('size') and call['status'] == 'clean'
                              for call in raw_scan_calls), 'Raw clean-logo ClamAV proof missing before storage/thumbnail')
            api.check(bucket == ids['bucket'] and object_name not in ids['object_keys']
                      and len(ids['object_keys']) < 3, 'Unexpected upload write')
            prospective = dict(ids, object_keys=ids['object_keys'] + [object_name])
            validate_file_journal(prospective)
            api.check(online.object_absent(self, bucket, object_name), 'Upload key collision')
            ids['object_keys'].append(object_name)
            api.write_manifest(path, ids)
            return super().put_object(bucket, object_name, *args, **kwargs)

        def make_bucket(self, *args, **kwargs):
            raise api.AcceptanceFailure('Acceptance must not create storage buckets')

    def journal_file(db, *_):
        for row in db.new:
            if isinstance(row, m.FileObject) and row.product_id in ids['products']:
                api.check(row.owner_id == ids['users']['provider'] and
                          row.object_name in ids['object_keys'], 'Unexpected generated upload identity')
                if row.file_role == 'product_data':
                    api.check(row.version_id == ids['versions'][0] and not ids['files'],
                              'Unexpected dataset upload identity')
                    ids['files'].append(row.id)
                else:
                    api.check(row.file_role in {'product_logo', 'product_logo_thumbnail'} and
                              row.version_id is None and row.file_role not in ids['logos'],
                              'Unexpected logo upload identity')
                    ids['logos'][row.file_role] = row.id
                validate_file_journal(ids)
                api.write_manifest(path, ids)

    client = TestClient(m.app)
    payload = archive_payload()
    def request(method, route, role='super', expected=200, **kwargs):
        response = client.request(method, '/api' + route, headers={
            'Authorization': 'Bearer ' + tokens[role], 'X-Request-Id': ids['request_ids'][0],
            'X-Enterprise-Id': ids['enterprises'][1 if role == 'provider' else 2 if role == 'other' else 0]},
            **kwargs)
        api.check(response.status_code == expected,
                  f'File {method} {route} expected {expected}, got {response.status_code}')
        return response
    def upload(content, expected):
        return request('POST', '/files/upload', 'provider', expected,
            data={'product_id': ids['products'][0], 'version_id': ids['versions'][0], 'file_role': 'product_data'},
            files={'upload': (ids['run_id'] + '.zip', content, 'application/zip')})
    event.listen(m.SessionLocal.class_, 'after_flush', journal_file)
    try:
        request('PUT', '/products/' + ids['products'][0], 'provider', json={
            'name': ids['products'][0], 'product_type': 'dataset', 'category_id': category_id,
            'provider_name': ids['enterprises'][1], 'description': 'Owned archive review acceptance',
            'usage_scenarios': 'Isolated delivery verification', 'pricing_strategy': 'Fixed per-order price',
            'authorization_conditions': 'Fixture buyer enterprise administrators only',
            'data_source_statement': 'Synthetic test data authored for this fixture',
            'compliance_statement': 'Synthetic data; no real personal records',
            'delivery_method': 'file', 'download_limit': 2, 'settlement_rule_mode': 'custom',
            'settlement_rule': {'platform_rate': 20, 'provider_rate': 80, 'service_rate': 0,
                                'expert_rate': 0, 'channel_rate': 0},
            'versions': [{'version_code': 'v1', 'price': 12, 'cost': 7, 'status': 'active'}]})
        # Submission must fail before the required logo/dataset exist.
        request('POST', f"/products/{ids['products'][0]}/submit", 'provider', 400)
        with patch.object(m, 'Minio', JournalMinio), patch.object(m, 'clamav_scan_stream', observe_raw_scan):
            STAGE = 'file-invalid-archive-rejection'
            upload(b'not an archive', 400)
            STAGE = 'file-zipped-eicar-rejection'
            infected_archive = archive_payload(EICAR)
            upload(infected_archive, 400)
            api.check(any(call['sha256'] == hashlib.sha256(infected_archive).hexdigest() and
                          call['status'] == 'infected' for call in raw_scan_calls),
                      'ZIP EICAR rejection lacks real infected ClamAV response')
            api.check(not ids['files'] and not ids['object_keys'], 'Rejected archives left upload artifacts')
            from PIL import Image
            logo = BytesIO()
            Image.new('RGB', (380, 280), (24, 116, 80)).save(logo, format='PNG')
            wrong_dimensions = BytesIO()
            Image.new('RGB', (381, 280), (24, 116, 80)).save(wrong_dimensions, format='PNG')
            invalid_logos = (
                ('malformed-image', '.logo.png', b'not a PNG image', 'image/png'),
                ('wrong-dimensions', '.logo.png', wrong_dimensions.getvalue(), 'image/png'),
                ('svg-injection', '.logo.svg', b'<svg width="380" height="280"><script>alert(1)</script></svg>', 'image/svg+xml'),
            )
            for label, suffix, content, media_type in invalid_logos:
                STAGE = 'file-logo-' + label + '-rejection'
                request('POST', '/files/upload', 'provider', 400, data={
                    'product_id': ids['products'][0], 'file_role': 'product_logo'},
                    files={'upload': (ids['run_id'] + suffix, content, media_type)})
                api.check(not ids['logos'] and not ids['object_keys'], 'Rejected logo left upload artifacts')
            expected_logo.update(sha256=hashlib.sha256(logo.getvalue()).hexdigest(), size=len(logo.getvalue()))
            STAGE = 'file-clean-logo-raw-clamav-upload'
            logo_uploaded = request('POST', '/files/upload', 'provider', data={
                'product_id': ids['products'][0], 'file_role': 'product_logo'},
                files={'upload': (ids['run_id'] + '.logo.png', logo.getvalue(), 'image/png')}).json()
            api.check(logo_uploaded['clamav_status'] == 'clean' and
                      ids['logos'].get('product_logo') == logo_uploaded['id'], 'Real logo ClamAV upload failed')
            STAGE = 'file-clean-archive-clamav-presidio-upload'
            uploaded = upload(payload, 200).json()
        logo_scans = [call for call in raw_scan_calls if call['sha256'] == expected_logo['sha256']]
        api.check(len(logo_scans) == 1 and logo_scans[0]['status'] == 'clean', 'Expected one real clean raw-logo scan')
        STAGE = 'file-review-delivery-download-cleanup'
        api.check(ids['files'] == [uploaded['id']] and uploaded['object_name'] in ids['object_keys'] and
                  set(ids['logos']) == {'product_logo', 'product_logo_thumbnail'} and len(ids['object_keys']) == 3,
                  'Upload response differs from precommit ownership journal')
        with m.SessionLocal() as db:
            item = db.get(m.FileObject, ids['files'][0])
            api.check(item and item.scan_status == 'clean' and item.scanned_at is not None and
                      item.size == len(payload) and item.checksum == hashlib.sha256(payload).hexdigest(),
                      'Uploaded archive metadata differs from real payload')
            assert_scan_report(json.loads(item.scan_report))
            product = db.get(m.Product, ids['products'][0])
            logo_row = db.get(m.FileObject, ids['logos']['product_logo'])
            logo_report = json.loads(logo_row.scan_report)
            api.check(product.status == 'draft' and not m.product_security_policy_violations(product) and
                      product.logo_file_id == ids['logos']['product_logo'] and
                      product.logo_thumbnail_file_id == ids['logos']['product_logo_thumbnail'] and
                      logo_row.scan_status == 'clean' and logo_row.scanned_at is not None and
                      logo_report.get('clamav_status') == 'clean' and bool(logo_report.get('clamav_report')),
                      'Registered metadata/logo binding is incomplete')
        request('POST', f"/products/{ids['products'][0]}/review", 'provider', 409,
                json={'decision': 'approve', 'comment': 'Cannot bypass submission'})
        submitted = request('POST', f"/products/{ids['products'][0]}/submit", 'provider').json()
        api.check(submitted['status'] == 'pending_review', 'Real registration submit failed')
        for unauthorized in ('provider', 'quality'):
            request('POST', f"/products/{ids['products'][0]}/review", unauthorized, 403,
                    json={'decision': 'approve', 'comment': 'Wrong stage actor must be denied'})
        review_evidence = []
        for role, route, before, after, action in REVIEW_STEPS:
            reviewed = request('POST', f"/products/{ids['products'][0]}/{route}", role,
                               json={'decision': 'approve', 'comment': 'Owned fixture ' + role + ' review'}).json()
            api.check(reviewed['status'] == after, 'Actual four-stage review transition failed')
            with m.SessionLocal() as db:
                audits = db.scalars(select(m.AuditLog).where(m.AuditLog.target_id == ids['products'][0],
                    m.AuditLog.action == action)).all()
                api.check(len(audits) == 1 and audits[0].actor == ids['users'][role] + '@example.invalid'
                          and json.loads(audits[0].before_json)['status'] == before and
                          json.loads(audits[0].after_json)['status'] == after,
                          'Four-stage review audit actor/count/state mismatch')
                review_evidence.append({'actor': audits[0].actor, 'action': action, 'before': before,
                                       'after': after, 'audit_id': audits[0].id})
        with m.SessionLocal() as db:
            scan = db.scalar(select(m.ProductSecurityScan).where(m.ProductSecurityScan.product_id == ids['products'][0]))
            api.check(scan is not None and scan.status == 'approved' and
                      scan.reviewed_by == ids['users']['security'] + '@example.invalid',
                      'Quality-created security report lacks actual security approval')
            report = json.loads(scan.report_json)
            samples = [item for item in report['files'] if item['file_id'] == ids['files'][0]]
            api.check(report['presidio_status'] == 'available' and len(samples) == 1 and
                      samples[0]['presidio_status'] == 'available' and samples[0]['sample_scanned'] is True,
                      'Quality-review aggregate stored archive scan failed')
            scan_id = scan.id
            api.check(db.scalar(select(func.count()).select_from(m.AuditLog).where(
                m.AuditLog.target_id == ids['products'][0], m.AuditLog.action == 'update_product')) == 1,
                'Real registration metadata update audit missing')
            for action in ('submit_product_review', 'review_complete_integration_gate'):
                api.check(db.scalar(select(func.count()).select_from(m.AuditLog).where(
                    m.AuditLog.target_id == ids['products'][0], m.AuditLog.action == action)) == 1,
                    'Submission/publication audit count mismatch')
            upload_audits = db.scalars(select(m.AuditLog).where(m.AuditLog.action == 'upload_file',
                m.AuditLog.target_id.in_(ids['files'] + list(ids['logos'].values())))).all()
            api.check(len(upload_audits) == 2 and {row.target_id for row in upload_audits} ==
                      {ids['files'][0], ids['logos']['product_logo']} and
                      all(row.actor == ids['users']['provider'] + '@example.invalid' for row in upload_audits),
                      'Real dataset/logo upload audit count or provider actor mismatch')
            Message, Receipt = m.message_center['Message'], m.message_center['Receipt']
            for state, role in (('pending_review', 'business'), ('quality_review', 'quality'),
                                ('security_review', 'security'), ('operation_review', 'release')):
                messages = db.scalars(select(Message).where(Message.target_type == 'product',
                    Message.target_id == ids['products'][0], Message.category == 'review',
                    Message.event_key.startswith(f"review:product:{ids['products'][0]}:{state}:"))).all()
                api.check(len(messages) == 1 and db.scalar(select(Receipt.id).where(
                    Receipt.message_id == messages[0].id, Receipt.user_id == ids['users'][role])) is not None,
                    'Four-stage reviewer notification count/receipt mismatch')
        for kind in ('combined', 'clamav', 'presidio'):
            pdf = request('GET', f"/files/{ids['files'][0]}/scan-report.pdf?report_type={kind}", 'provider')
            api.check(pdf.content.startswith(b'%PDF-') and
                      pdf.headers.get('content-type', '').startswith('application/pdf'), 'Real scan PDF missing')
        request('GET', f"/files/{ids['files'][0]}/scan-report.pdf", 'other', 403)
        oid = request('POST', '/orders', json={'product_id': ids['products'][0],
            'product_version_id': ids['versions'][0], 'buyer_enterprise_id': ids['enterprises'][0],
            'subscription_months': 1}).json()['id']
        with held_payment_tasks(m, oid):
            request('POST', f'/orders/{oid}/transition', json={
                'action': 'confirm_payment', 'reason': 'Owned file acceptance simulated payment'})
        with m.SessionLocal() as db:
            paid = db.get(m.Order, oid)
            api.check(paid.payment_status == 'paid' and paid.paid_amount == paid.amount and
                      m.order_economics['delivery_snapshot'](paid)['download_limit'] == 2,
                      'Owned file order is not fully paid with frozen downloadlimit(2)')
        api.check(request('GET', f'/orders/{oid}/delivery-access').json()['ready'] is False,
                  'Paid file order ready before actual delivery')
        delivered = process_online_delivery_once(m, ids, oid, 0, release=True)
        api.check(delivered['outcome'] is True and delivered['status'] == 'completed' and
                  delivered['main'] == 'completed' and delivered['delivery'] == 'accepted',
                  'Real file initial delivery failed')
        for role in ('super', 'admin'):
            access = request('GET', f'/orders/{oid}/delivery-access', role).json()
            api.check(access['ready'] is True and len(access['files']) == 1 and
                      access['files'][0]['remaining_downloads'] == 2,
                      'Paid buyer administrator file readiness failed')
        url = f"/files/{ids['files'][0]}/download?order_id={oid}"
        for role in ('member', 'other'):
            request('GET', f'/orders/{oid}/delivery-access', role, 403)
            request('GET', url, role, 403)
        anonymous = client.get('/api' + url)
        api.check(anonymous.status_code == 401, 'Anonymous paid-order download permitted')
        for index, role in enumerate(('super', 'admin')):
            api.check(request('GET', url, role).content == payload, 'Paid download differs from real archive')
            access = request('GET', f'/orders/{oid}/delivery-access', role).json()
            api.check(access['files'][0]['used_downloads'] == index + 1 and
                      access['files'][0]['remaining_downloads'] == 1 - index,
                      'Order-level shared file download quota did not decrement exactly once')
        request('GET', url, expected=429)
        api.check(request('GET', f'/orders/{oid}/delivery-access').json()['ready'] is False,
                  'Downloadlimit(2) exhausted but order remains ready')
        with m.SessionLocal() as db:
            api.check(db.scalar(select(func.count()).select_from(m.FileDownloadLog).where(
                m.FileDownloadLog.order_id == oid, m.FileDownloadLog.success.is_(True))) == 2,
                'Denied file access consumed quota or successful download log missing')
            api.check(db.scalar(select(func.count()).select_from(m.AuditLog).where(
                m.AuditLog.order_id == oid, m.AuditLog.action == 'delivery_succeeded')) == 1,
                'File delivery success audit missing or duplicated')
        cleanup_file(m, ids, path)
        return {'status': 'passed', 'scan': 'real archive ClamAV + Presidio; persisted report and aggregate scan',
                'registration': 'production provider PUT full metadata, then submit',
                'review_chain': review_evidence, 'reviewer_inbox_stages': 4,
                'logo': 'real 380x280 PNG ClamAV upload; generated thumbnail; exact cleanup',
                'logo_rejection_http': {'malformed_image': 400, 'wrong_dimensions': 400, 'svg_injection': 400},
                'raw_logo_clamav': logo_scans[0], 'upload_audit_count': 2,
                'archive_rejection_http': [400, 400], 'reports': ['combined', 'clamav', 'presidio'],
                'scan_report_id': scan_id, 'readiness': [False, True, False],
                'paid_download_http': [200, 200, 429], 'denied_http': [403, 403, 401],
                'download_limit': 2, 'cleanup': 'exact upload object, scan reports and DB descendants absent'}
    finally:
        event.remove(m.SessionLocal.class_, 'after_flush', journal_file)
        client.close()


def assert_retry_sequence(attempts):
    api.check(len(attempts) == 3, 'Expected exactly three real failed attempts')
    for index, attempt in enumerate(attempts):
        api.check(attempt['outcome'] is False and attempt['error_present'] is True and
                  attempt['retry_count'] == index + 1 and attempt['max_retries'] == 3 and
                  attempt['main'] != 'completed' and
                  attempt['status'] == ('retrying' if index < 2 else 'exception') and
                  attempt['delivery'] == ('in_delivery' if index < 2 else 'exception'),
                  'Real failed-attempt state mismatch')
        start, finish = datetime.fromisoformat(attempt['started']), datetime.fromisoformat(attempt['finished'])
        api.check(start <= finish, 'Attempt clock moved backwards')
        if index < 2:
            deadline = datetime.fromisoformat(attempt['next_retry_at'])
            api.check(start + timedelta(seconds=10) <= deadline <= finish + timedelta(seconds=10),
                      'Retry deadline is not ten seconds after production failure')
        else:
            api.check(attempt['next_retry_at'] is None, 'Third failure scheduled another automatic attempt')
        if index:
            prior_deadline = datetime.fromisoformat(attempts[index - 1]['next_retry_at'])
            api.check(start >= prior_deadline, 'Script retried before the real production deadline')


def api_missing_route_retries(m, path):
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select
    ids = api.fixture_ids()
    ids.update(consumers=[], quota_windows=[], environment=api.environment(m))
    api.write_manifest(path, ids, create=True)
    api.preflight(m, ids)
    ids['authorized'] = True
    api.write_manifest(path, ids)
    tokens = api.prepare(m, ids)
    client = TestClient(m.app)
    def request(method, route, role='buyer', expected=200, body=None):
        response = client.request(method, '/api' + route, headers={
            'Authorization': 'Bearer ' + tokens[role], 'X-Request-Id': ids['request_id']}, json=body)
        api.check(response.status_code == expected,
                  f'Retry {method} {route} expected {expected}, got {response.status_code}')
        return response.json()
    try:
        base = f"/products/{ids['products'][0]}/versions/{ids['versions'][0]}/integration"
        request('PUT', base, 'ops', body={'upstream_url': api.MOCK_URL, 'route_key': ids['routes'][0],
            'version': 'v1', 'upstream_auth_mode': 'oauth2', 'health_path': '/health',
            'rate_limit_per_minute': 60, 'daily_quota': 100, 'monthly_quota': 1000})
        with m.SessionLocal() as db:
            route = db.scalar(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.product_id == ids['products'][0]))
            oauth = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == ids['products'][0]))
            api.check(route is not None and oauth is not None, 'Owned API integration credentials missing')
            route.upstream_client_id, route.upstream_client_secret = oauth.client_id, oauth.client_secret
            db.commit()
        verified = request('POST', base + '/verify', 'ops')
        api.check(verified['status'] == 'verified' and verified['healthy'], 'Retry fixture real health verification failed')
        oid = request('POST', '/orders', body={'product_id': ids['products'][0],
            'product_version_id': ids['versions'][0], 'buyer_enterprise_id': ids['enterprises'][0],
            'subscription_months': 1})['id']
        live = api.admin(m, 'GET', '/routes/' + ids['routes'][0])
        api.check(live.get('labels', {}).get('market_product_id') == ids['products'][0],
                  'Missing-route fault refuses an unowned external route')
        api.admin(m, 'DELETE', '/routes/' + ids['routes'][0])
        with held_payment_tasks(m, oid):
            request('POST', f'/orders/{oid}/transition', body={
                'action': 'confirm_payment', 'reason': 'Owned missing-route simulated payment'})
        attempts = []
        for index in range(3):
            api.check(api.admin(m, 'GET', '/routes/' + ids['routes'][0], absent=True) is None,
                      'Owned route unexpectedly restored during failure acceptance')
            attempts.append(process_online_delivery_once(m, ids, oid, index, release=index == 0))
            with m.SessionLocal() as db:
                Term = m.subscription_terms['Term']
                api.check(db.scalar(select(Term.id).where(Term.order_id == oid)) is None,
                          'Failed delivery granted a subscription term')
                api.check(db.scalar(select(m.ApiCredential.id).where(
                    m.ApiCredential.route_id.in_(select(m.ApiGatewayRoute.id).where(
                        m.ApiGatewayRoute.product_id.in_(ids['products']))))) is None,
                    'Failed missing-route delivery issued a credential')
            if index < 2:
                deadline = datetime.fromisoformat(attempts[-1]['next_retry_at'])
                time.sleep(max(0, (deadline - m.now()).total_seconds()) + 0.05)
        assert_retry_sequence(attempts)
        tid = attempts[-1]['task_id']
        request('POST', f'/delivery-tasks/{tid}/process', 'ops', expected=409, body={'success': True})
        with m.SessionLocal() as db:
            audits = db.scalars(select(m.AuditLog).where(m.AuditLog.order_id == oid,
                m.AuditLog.action == 'delivery_failed').order_by(m.AuditLog.created_at, m.AuditLog.id)).all()
            api.check(len(audits) == 3, 'Expected three persisted failure audits')
            audit_evidence = []
            for index, audit in enumerate(audits):
                after = json.loads(audit.after_json)
                before = json.loads(audit.before_json)
                api.check(audit.target_id == tid and audit.actor == ids['users']['ops'] + '@example.invalid'
                          and before['retry_count'] == index and
                          after['retry_count'] == index + 1 and
                          after['task_status'] == attempts[index]['status'] and
                          audit.risk_level == ('high' if index == 2 else 'warning') and
                          datetime.fromisoformat(attempts[index]['started']) <= audit.created_at <=
                          datetime.fromisoformat(attempts[index]['finished']),
                          'Failure audit does not match its real timed attempt')
                audit_evidence.append({'id': audit.id, 'at': audit.created_at.isoformat(), 'after': after})
            api.check(db.scalar(select(func.count()).select_from(m.AuditLog).where(
                m.AuditLog.order_id == oid, m.AuditLog.action == 'delivery_succeeded')) == 0,
                'Missing-route failure produced a success audit')
            Message, Receipt = m.message_center['Message'], m.message_center['Receipt']
            event = f'business:delivery_task:{tid}:delivery_failed:{tid}:exception:3'
            message = db.scalar(select(Message).where(Message.event_key == event))
            api.check(message is not None and db.scalar(select(Receipt.id).where(
                Receipt.message_id == message.id, Receipt.user_id == ids['users']['ops'])) is not None,
                'Third failure lacks manual operator handoff inbox evidence')
            task = db.get(m.DeliveryTask, tid)
            api.check(task.status == 'exception' and task.retry_count == 3 and task.next_retry_at is None,
                      'Blocked fourth processing changed terminal failure state')
        api.cleanup(m, ids, path)
        return {'status': 'passed', 'fault': 'real exact-owned APISIX route deleted after verified purchase',
                'driver': 'script process_online_delivery_once -> production online_delivery.process; not daemon',
                'attempts': attempts, 'failure_audits': audit_evidence,
                'manual_handoff': 'exception:3 + operator inbox receipt; fourth automatic process HTTP 409',
                'cleanup': 'exact DB/Admin/Redis fixture cleanup'}
    finally:
        client.close()


def protected_fingerprint(m):
    from sqlalchemy import select
    with m.SessionLocal() as db:
        product = db.get(m.Product, PROTECTED_PRODUCT)
        api.check(product is not None, 'Protected product missing')
        payload = {'order': saas.fingerprint(db, m), 'product': {
            c.name: getattr(product, c.name) for c in m.Product.__table__.columns}}
        # Include exact FK descendants of the protected product as well.
        tables = m.Base.metadata.tables
        predicates = {m.Product.__tablename__: m.Product.id == PROTECTED_PRODUCT}
        previous = None
        while True:
            keys = {column: set(db.scalars(select(column).where(predicate)))
                    for name, predicate in predicates.items()
                    for column in tables[name].primary_key.columns}
            if keys == previous:
                break
            previous = keys
            from sqlalchemy import or_
            for table in tables.values():
                children = [fk.parent.in_(keys[fk.column]) for fk in table.foreign_keys
                            if keys.get(fk.column)]
                if children:
                    predicates[table.name] = or_(*children, *(
                        [predicates[table.name]] if table.name in predicates else []))
        payload['descendants'] = {name: sorted([
            dict(row._mapping) for row in db.execute(select(tables[name]).where(predicate))],
            key=lambda row: json.dumps(row, sort_keys=True, default=str))
            for name, predicate in predicates.items()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def scan_probe(m):
    clean = b'Owned delivery acceptance sample.\n'
    status, _ = m.clamav_scan_stream(BytesIO(clean), len(clean))
    api.check(status == 'clean', 'Real ClamAV clean probe failed')
    eicar = (b'X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-'
             b'ANTIVIRUS-TEST-FILE!$H+H*')
    status, _ = m.clamav_scan_stream(BytesIO(eicar), len(eicar))
    api.check(status == 'infected', 'Real ClamAV EICAR rejection failed')
    findings, status = m.presidio_analyze('Contact acceptance.person@example.com for evidence.')
    api.check(status == 'available' and any(
        item.get('entity') == 'EMAIL_ADDRESS' and
        item.get('matched_text') == 'acceptance.person@example.com' and
        item.get('source') == 'presidio' for item in findings),
        'Real Presidio detection probe failed')
    return {'status': 'passed', 'scope': 'real engine probes only; no file delivery claim'}


def saas_initial(m, path):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    ids = saas.fixture_ids()
    saas.preflight(m, ids)
    ids['authorized'] = True
    ids['_manifest_writer'] = lambda value, create=False: saas.write_manifest(path, value, create)
    ids['_manifest_writer'](ids, create=True)
    # Retain ownership journal on failure, including ambiguous remote OPEN.
    tokens, secret = saas.prepare_fixture(m, ids)
    ids['_manifest_writer'](ids)
    saas.register_mock_client(ids, secret)
    client = TestClient(m.app)
    try:
        saas.request(client, tokens['provider'], 'POST',
                     f"/products/{ids['product']}/saas-integration/verify")
        oid = saas.request(client, tokens['buyer'], 'POST', '/orders', body={
            'product_id': ids['product'], 'product_version_id': ids['versions']['source'],
            'buyer_enterprise_id': ids['enterprises']['buyer'], 'subscription_months': 1})['id']
        ids['database_ids']['orders'].append(oid)
        with m.SessionLocal() as db:
            order = db.get(m.Order, oid)
            api.check(order and order.subscription_id, 'Owned SaaS subscription missing')
            ids['database_ids']['subscription_id'] = order.subscription_id
            ids['external']['tenant'] = 'tenant-' + order.subscription_id[:12]
        ids['_manifest_writer'](ids)
        saas.request(client, tokens['finance'], 'POST', f'/orders/{oid}/transition', body={
            'action': 'confirm_payment', 'reason': 'Scoped initial delivery simulated payment'})
        with m.SessionLocal() as db:
            task = db.scalar(select(m.DeliveryTask).where(m.DeliveryTask.order_id == oid))
            api.check(task is not None, 'Owned SaaS delivery task missing')
            tid = task.id
        saas.deliver_staged(m, ids, path, client, tokens, tid)
        ready, _ = saas.delivery_ready_proof(m, ids, oid, tid)
        api.check(ready, 'Initial SaaS OPEN/STATUS readiness proof failed')
        saas.cleanup(m, ids, path)
        return {'status': 'passed', 'scope': 'initial delivery; identity-bound OPEN/STATUS; exact cleanup'}
    finally:
        client.close()


def validate_journal(saved, path):
    api.check(set(saved) in ({'run_id', 'environment', 'protected', 'api', 'saas', 'file', 'retry'},
                            {'run_id', 'environment', 'protected', 'api', 'saas', 'file', 'retry', 'protected_state'}),
              'Unknown unified journal fields')
    api.check(isinstance(saved['run_id'], str) and
              re.fullmatch(r'qa-four-[0-9a-f]{12}', saved['run_id']), 'Invalid unified marker')
    for key in ('environment', 'protected'):
        api.check(isinstance(saved[key], str) and re.fullmatch(r'[0-9a-f]{64}', saved[key]),
                  'Invalid unified fingerprint')
    for key in ('api', 'saas', 'file', 'retry'):
        api.check(saved[key] == str(path.with_name(path.name + '.' + key + '.json')),
                  'Child journal path changed')
    if 'protected_state' in saved:
        validate_protected_state(saved['protected_state'])


def run(args):
    global STAGE
    STAGE = 'parent-review-gate'
    if getattr(args, 'inspect_owned_message_edges', False):
        return inspect_file_message_edges(args)
    if getattr(args, 'inspect_protected', False):
        return inspect_protected(args)
    api.check(getattr(args, 'parent_reviewed', False) is True,
              'Parent review required before any runtime mutation')
    if getattr(args, 'recover_observational_drift', False):
        return recover_observational_drift(args)
    path = Path(args.manifest).absolute()
    if args.cleanup:
        saved = read_private(path)
        validate_journal(saved, path)
    else:
        saved = {'run_id': 'qa-four-' + secrets.token_hex(6)}
        saved.update({key: str(path.with_name(path.name + '.' + key + '.json'))
                      for key in ('api', 'saas', 'file', 'retry')})
        api.check(all(not Path(saved[k]).exists() and not Path(saved[k]).is_symlink()
                      for k in ('api', 'saas', 'file', 'retry')), 'Child journal collision')
    m = api.load_main()
    STAGE = 'protected-preflight'
    if args.cleanup:
        api.check(saved['environment'] == api.environment(m), 'Cleanup environment changed')
    else:
        saved.update(environment=api.environment(m), protected=protected_fingerprint(m),
                     protected_state=protected_snapshot(m))
        api.write_manifest(path, saved, create=True)
    if 'protected_state' in saved:
        assert_protected_core(saved['protected_state'], protected_snapshot(m))
    else:
        api.check(protected_fingerprint(m) == saved['protected'],
                  'Legacy protected fingerprint changed; use reviewed trusted-baseline recovery, not baseline reset')
    result = {}
    try:
        if args.cleanup:
            STAGE = 'cleanup'
            cleanup_owned_children(m, saved, protected_snapshot(m))
            result.update(status='passed', scope='cleanup only')
            return result
        STAGE = 'real-scanner-probes'
        scans = scan_probe(m)
        STAGE = 'real-file-upload-delivery-download-cleanup'
        file_result = file_initial(m, Path(saved['file']))
        STAGE = 'api-modelAPI-delivery-cleanup'
        api_result = api.run(SimpleNamespace(manifest=saved['api'], cleanup=False,
            gateway_url=args.gateway_url, worker=False, worker_timeout=60, browser_fixture=None))
        STAGE = 'saas-initial-open-status-cleanup'
        saas_result = saas_initial(m, Path(saved['saas']))
        STAGE = 'api-missing-route-three-failures-manual-cleanup'
        retry_result = api_missing_route_retries(m, Path(saved['retry']))
        api.check(all(result.get('status') == 'passed' for result in
                      (scans, file_result, api_result, saas_result, retry_result)),
                  'A runtime component did not pass')
        result.update({'status': 'passed', 'file': file_result, 'api_modelAPI': api_result, 'saas': saas_result,
                'scanners': scans, 'retry_to_manual': retry_result,
                'acceptance_boundary': 'file provider registration update, real logo and four-stage review; '
                    'four-type initial delivery; API/modelAPI/SaaS approvals remain fixture preconditions; '
                    'parent runtime and TRD-QA-003 signoff required',
                'driver': 'script production process-handler transactions; not deployed daemon evidence',
                'payment': 'simulated HTTP confirmation; no bank evidence'})
        return result
    finally:
        if 'protected_state' in saved:
            current = protected_snapshot(m)
            assert_protected_core(saved['protected_state'], current)
            result.update(protected_business_unchanged=True, observational_changes=snapshot_differences(
                saved['protected_state']['observational'], current['observational']))
        else:
            api.check(protected_fingerprint(m) == saved['protected'], 'Protected fingerprint changed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--gateway-url', default='http://market-apisix:9080')
    parser.add_argument('--cleanup', action='store_true')
    parser.add_argument('--inspect-protected', action='store_true', help='Read-only protected state diagnosis')
    parser.add_argument('--inspect-owned-message-edges', action='store_true', help='Read-only exact file inbox-edge diagnosis')
    parser.add_argument('--recover-observational-drift', action='store_true')
    parser.add_argument('--trusted-core-baseline', '--trusted-baseline', dest='trusted_baseline',
                        help='Private earlier accepted browser ownership artifact; never rewritten')
    parser.add_argument('--recovery-report', help='New private report path for this recovery attempt')
    parser.add_argument('--parent-reviewed', action='store_true',
                        help='Parent has reviewed and authorized this runtime execution')
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as exc:
        import traceback
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        result = {'status': 'failed', 'error_type': type(exc).__name__,
                  'stage': STAGE, 'file': Path(frame.filename).name, 'line': frame.lineno,
                  'recovery': 'Retain private journals; resolve ownership before --cleanup'}
        if isinstance(exc, (api.AcceptanceFailure, online.AcceptanceFailure, guarded.AcceptanceFailure)):
            result['check_failed'] = str(exc)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] in {'passed', 'inspection_only', 'recovered_cleanup'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
