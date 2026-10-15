"""Owned QA001 training registration scope. No startup hooks.

In the API environment: prepare --manifest /tmp/<name>.json, then transfer the
separate .login.json privately to Windows. Run inspect after the browser, then
cleanup. Preparation writes the manifest before DB writes. Failures retain it.
prepare is DB-only. inspect captures exactly one owned draft and its upload IDs.
cleanup deletes exact guarded DB rows and the journaled owned MinIO object.

Run inspect --require-saved to verify one draft/version/logo including real stored
SVG bytes. Browser leaves a separate private <login-file>.receipt.json on Windows;
retain it for recovery and remove that exact file after successful DB cleanup.
No publication, review, delivery, gateway, or pagination proof is claimed.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

import version_change_quote_acceptance as safe

ORIGIN = 'http://192.168.10.10:30080'
PROTECTED = {'ORD-261012005144E1B3', 'qa-vcq-a044ea9ea181-p2'}


def ids(run=None):
    run = run or 'qa-r3-reg-' + secrets.token_hex(6)
    safe.check(isinstance(run, str) and re.fullmatch(r'qa-r3-reg-[0-9a-f]{12}', run),
               'Invalid run ID')
    return dict(run_id=run, user=run+'-u', enterprise=run+'-e', membership=run+'-m', origin=ORIGIN,
                mode='single-training-save', product_name=run+'-training',
                logo_name=run+'-logo.svg')


def logo_text():
    return '<svg xmlns="http://www.w3.org/2000/svg" width="380" height="280" viewBox="0 0 380 280"><rect width="380" height="280" fill="#147d64"/></svg>'


def paths(value):
    path = Path(value).absolute()
    safe.check(path.parent in (Path('/tmp'), Path('/var/tmp')) and not path.is_symlink(),
               'Manifest must be a direct temporary file')
    return path, path.with_suffix('.login.json')


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        safe.check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
                   'Private file must be regular 0600')
        return json.load(stream)


def read_scope(path):
    value = read_private(path)
    safe.check(isinstance(value, dict) and isinstance(value.get('run_id'), str), 'Missing run ID')
    expected = ids(value.get('run_id'))
    safe.check(all(value.get(k) == v for k, v in expected.items()) and
               set(value) <= set(expected) | {'database_ids', 'captured', 'objects'}, 'Changed ownership manifest')
    safe.check(not PROTECTED.intersection((value['user'], value['enterprise'])),
               'Protected ID')
    return expected


def load_main():
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    safe.check(m.engine.dialect.name == 'postgresql', 'PostgreSQL required')
    safe.check(not m.app.dependency_overrides, 'Overridden dependencies refused')
    return m


def owned_rows(m, db, scope):
    from sqlalchemy import or_, select
    uid, eid = scope['user'], scope['enterprise']
    user, enterprise = db.get(m.User, uid), db.get(m.Enterprise, eid)
    if user:
        safe.check(user.name == uid and user.email == uid+'@example.invalid' and
                   not user.platform_role, 'Changed user owner')
    if enterprise:
        safe.check(enterprise.name == eid and enterprise.credit_code == eid,
                   'Changed enterprise owner')
    members = db.scalars(select(m.Membership).where(or_(
        m.Membership.user_id == uid, m.Membership.enterprise_id == eid))).all()
    safe.check(all(x.id == scope['membership'] and x.user_id == uid and x.enterprise_id == eid and
                   x.role == 'super_admin' and x.status == 'active' and
                   x.business_roles == 'provider,user' for x in members), 'Foreign membership')
    products = db.scalars(select(m.Product).where(m.Product.enterprise_id == eid).with_for_update()).all()
    safe.check(len(products) <= 1, 'More than one product; cleanup refused')
    for product in products:
        safe.check(product.id not in PROTECTED and product.name == scope['product_name'] and
                   product.product_type == 'training' and product.delivery_method == 'training' and
                   product.status == 'draft' and product.provider_name == eid and
                   product.description == scope['run_id']+' synthetic training' and
                   product.authorization_conditions == scope['run_id']+' isolated QA only',
                   'Unexpected product ownership')
    pids = [p.id for p in products]
    safe.check(not db.scalar(select(m.Order.id).where(or_(m.Order.product_id.in_(pids),
        m.Order.buyer_user_id == uid, m.Order.buyer_enterprise_id == eid,
        m.Order.provider_enterprise_id == eid)).limit(1)), 'Unexpected order')
    versions = db.scalars(select(m.ProductReleaseVersion).where(
        m.ProductReleaseVersion.product_id.in_(pids))).all()
    safe.check(len(versions) == len(products) and all(v.version_code == 'v1.0' and
        v.description == scope['run_id']+' training version' and float(v.price) == 12 and
        float(v.cost) == 7 and v.status == 'active' for v in versions), 'Unexpected versions')
    files = db.scalars(select(m.FileObject).where(or_(m.FileObject.owner_id == uid,
        m.FileObject.product_id.in_(pids)))).all()
    safe.check(len(files) <= 1, 'Unexpected file count')
    for item in files:
        safe.check(item.owner_id == uid and item.product_id in pids and not item.version_id and
            item.file_role == 'product_logo' and item.original_name == scope['logo_name'] and
            item.content_type == 'image/svg+xml' and item.size == len(logo_text().encode()) and
            item.checksum == hashlib.sha256(logo_text().encode()).hexdigest() and
            re.fullmatch(re.escape(uid)+r'/[0-9]{8}/[0-9a-f]{12}-'+re.escape(scope['logo_name']),
                         item.object_name), 'Unexpected file owner/object')
        safe.check(len(db.scalars(select(m.FileObject.id).where(
            m.FileObject.object_name == item.object_name)).all()) == 1, 'Shared storage object')
    fids = [item.id for item in files]
    safe.check(all(not p.logo_file_id or p.logo_file_id in fids for p in products) and
               all(not p.logo_thumbnail_file_id for p in products), 'Unexpected product logo')
    references = db.scalars(select(m.Product).where(or_(m.Product.logo_file_id.in_(fids),
        m.Product.logo_thumbnail_file_id.in_(fids)))).all()
    safe.check(all(p.id in pids for p in references), 'Foreign logo reference')
    owned = {}
    for model, key in ((m.User, uid), (m.Enterprise, eid)):
        if db.get(model, key):
            owned[model.__table__.name] = {(key,)}
    for model, rows in ((m.Product, products), (m.ProductReleaseVersion, versions),
                        (m.FileObject, files)):
        if rows:
            owned[model.__table__.name] = {(row.id,) for row in rows}
    # Login audit has actor/target strings rather than ownership FKs.
    audit = m.AuditLog.__table__
    audit_keys = {tuple(row) for row in db.execute(select(*audit.primary_key.columns).where(
        or_(m.AuditLog.actor == uid+'@example.invalid', m.AuditLog.target_id.in_([uid, *pids, *fids]))))}
    if audit_keys:
        for row in db.scalars(select(m.AuditLog).where(safe.row_predicate(audit, audit_keys))):
            targets = {'login': [uid], 'create_product': pids, 'update_product': pids,
                       'upload_file': fids}
            safe.check(row.actor == uid+'@example.invalid' and row.target_id in targets.get(row.action, [])
                       and not row.order_id, 'Unexpected audit linkage')
        owned[audit.name] = audit_keys
    owned = safe.expand_fk_ids(db, m.Base.metadata, owned)
    allowed = {model.__table__.name for model in
               (m.User, m.Enterprise, m.Membership, m.Product, m.ProductReleaseVersion,
                m.FileObject, m.AuditLog, m.message_center['Preference'])}
    safe.check(set(owned) <= allowed, 'Unexpected FK child: cleanup refused')
    safe.check(not any(PROTECTED.intersection(map(str, row))
                       for rows in owned.values() for row in rows), 'Protected row')
    return owned


def prepare(m, manifest, login):
    from sqlalchemy import select
    scope = ids()
    safe.check(not manifest.exists() and not login.exists(), 'Files must be absent')
    password = secrets.token_urlsafe(32)
    safe.check(bool(m.MINIO_ENDPOINT), 'Real logo storage must be configured')
    with m.SessionLocal() as db:
        for model, key in ((m.User, scope['user']), (m.Enterprise, scope['enterprise']),
                           (m.Membership, scope['membership'])):
            safe.check(not db.scalar(select(model.id).where(model.id == key)), 'ID collision')
    safe.private_json(manifest, scope, create=True)
    # Preserve the recovery manifest even when preparation or private-file output fails.
    safe.private_json(login, dict(**scope, email=scope['user']+'@example.invalid',
                                 password=password), create=True)
    with m.SessionLocal() as db:
        for model, key in ((m.User, scope['user']), (m.Enterprise, scope['enterprise']),
                           (m.Membership, scope['membership'])):
            safe.check(not db.scalar(select(model.id).where(model.id == key)), 'ID collision')
        db.add(m.User(id=scope['user'], name=scope['user'], email=scope['user']+'@example.invalid',
                      password_hash=m.hash_password(password), email_verified=True,
                      verified_status='verified', activation_status='active', is_active=True))
        db.add(m.Enterprise(id=scope['enterprise'], name=scope['enterprise'],
                            credit_code=scope['enterprise'], verification_status='verified'))
        db.flush()
        db.add(m.Membership(id=scope['membership'], user_id=scope['user'], enterprise_id=scope['enterprise'],
                            role='super_admin', status='active', business_roles='provider,user'))
        db.add(m.message_center['Preference'](user_id=scope['user'], email_enabled=False))
        db.commit()
    return dict(status='prepared', mode=scope['mode'], manifest=str(manifest),
                private_login=str(login), uncovered=['valid public page-2 jump',
                'public page-2 F5', 'public page-size reset', 'foreign-provider 404'])


def inspect_or_cleanup(m, manifest, login, cleanup=False, require_saved=False):
    from sqlalchemy import delete, select, text
    scope = read_scope(manifest)
    if cleanup and login.exists():
        credential = read_private(login)
        safe.check(all(credential.get(k) == v for k, v in scope.items()), 'Changed login artifact')
    with m.SessionLocal() as db:
        db.execute(text("SET LOCAL lock_timeout = '10s'"))
        for model, key in ((m.User, scope['user']), (m.Enterprise, scope['enterprise'])):
            db.scalars(select(model).where(model.id == key).with_for_update()).all()
        owned = owned_rows(m, db, scope)
        products = db.scalars(select(m.Product).where(m.Product.enterprise_id == scope['enterprise'])).all()
        files = db.scalars(select(m.FileObject).where(m.FileObject.owner_id == scope['user'])).all()
        versions = db.scalars(select(m.ProductReleaseVersion).where(
            m.ProductReleaseVersion.product_id.in_([p.id for p in products]))).all()
        captured = dict(products=[p.id for p in products], versions=[v.id for v in versions],
                        files=[f.id for f in files])
        previous = read_private(manifest).get('captured')
        if previous:
            safe.check(all(set(captured[k]) <= set(previous[k]) for k in captured),
                       'Captured IDs changed')
        if require_saved:
            safe.check(len(products) == len(files) == len(versions) == 1 and
                       products[0].logo_file_id == files[0].id, 'Complete single saved product required')
        objects = [item.object_name for item in files]
        safe.private_json(manifest, {**scope, 'captured': captured, 'objects': objects,
            'database_ids': {name: sorted(map(list, keys)) for name, keys in owned.items()}})
        if require_saved:
            safe.check(bool(m.MINIO_ENDPOINT), 'Real logo storage required')
            client = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY,
                             secret_key=m.MINIO_SECRET_KEY, secure=False)
            response = client.get_object(m.MINIO_BUCKET, objects[0])
            try:
                safe.check(response.read(len(logo_text().encode())+1) == logo_text().encode(),
                           'Stored logo bytes differ')
            finally:
                response.close()
                response.release_conn()
        if cleanup:
            if objects:
                safe.check(bool(m.MINIO_ENDPOINT), 'Storage configuration missing')
                client = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY,
                                 secret_key=m.MINIO_SECRET_KEY, secure=False)
                for object_name in objects:
                    client.remove_object(m.MINIO_BUCKET, object_name)
            for table in reversed(m.Base.metadata.sorted_tables):
                if owned.get(table.name):
                    db.execute(delete(table).where(safe.row_predicate(table, owned[table.name])))
            db.commit()
    if cleanup:
        with m.SessionLocal() as db:
            safe.check(not owned_rows(m, db, scope), 'Owned rows remain')
            for table in m.Base.metadata.tables.values():
                for fk in table.foreign_keys:
                    parent = fk.column.table
                    columns = list(parent.primary_key.columns)
                    index = next((i for i, col in enumerate(columns) if col is fk.column), None)
                    if index is not None and owned.get(parent.name):
                        keys = [row[index] for row in owned[parent.name]]
                        safe.check(not db.scalar(select(fk.parent).where(fk.parent.in_(keys)).limit(1)),
                                   'FK leftovers')
        # Validate credential ownership before removing the exact private artifact.
        if login.exists():
            credential = read_private(login)
            safe.check(all(credential.get(k) == v for k, v in scope.items()), 'Changed login artifact')
            login.unlink()
    return dict(status='cleaned' if cleanup else 'inspected', products=len(products),
                owned_counts={name: len(rows) for name, rows in owned.items()},
                manifest=str(manifest))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'inspect', 'cleanup'))
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--require-saved', action='store_true', help='inspect: require one draft/version/real logo')
    args = parser.parse_args()
    if args.require_saved and args.action != 'inspect':
        parser.error('--require-saved is only valid with inspect')
    try:
        manifest, login = paths(args.manifest)
        m = load_main()
        result = prepare(m, manifest, login) if args.action == 'prepare' else inspect_or_cleanup(
            m, manifest, login, args.action == 'cleanup', args.require_saved)
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(json.dumps(dict(status='failed', error_type=type(exc).__name__,
                              recovery='Retain private manifest; review ownership before cleanup')))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
