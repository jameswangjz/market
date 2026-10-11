"""Disposable real PostgreSQL/MinIO online file delivery acceptance.

Run inside the API image, against an already migrated database and existing bucket:
  PYTHONPATH=/app python /tmp/online_delivery_acceptance.py

No lifespan, migrations, seeds, dependency overrides, global queue processing,
or storage mocks. HTTP confirm_payment is simulated payment, not bank evidence.
The scan report is an explicit fixture, NOT real ClamAV/Presidio scan acceptance.
The production HTTP process route locks owned orders/tasks and exercises real stat/get,
audit, notifications, metering and transaction commits. Notification failure
rollback belongs in isolated unit tests, not this live acceptance.

A private IDs-only manifest is written before mutations and retained after
cleanup. Cleanup resolves children from exact owned parents, never LIKE/prefix
deletion or bucket listing. Interrupted runs retain parent IDs/exact object keys
for manual recovery. External workers processing these fixtures cause a failed
state assertion; this script never stops or modifies those workers.

For browser acceptance, add --prepare-browser --base-url https://market.example
--deployment-tag IMAGE_TAG. A separate 0600 <manifest-stem>.browser.json contains
the browser token; the ownership manifest stays IDs-only. A third completed
order retains its unused download quota. After browser work:
  PYTHONPATH=/app python /tmp/online_delivery_acceptance.py --cleanup --manifest PATH
"""

import argparse
from datetime import timedelta
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
from urllib.parse import urlsplit
import zipfile


class AcceptanceFailure(Exception):
    """A locally authored assertion message safe to include in the JSON report."""


def check(condition, detail):
    if not condition:
        raise AcceptanceFailure(detail)


def fixture_ids(run_id=None):
    run_id = run_id or 'qa-online-' + secrets.token_hex(6)
    return {
        'run_id': run_id,
        'users': {role: run_id + '-' + role for role in
                  ('super', 'admin', 'member', 'other', 'provider', 'operator')},
        'enterprises': [run_id + '-' + role for role in ('buyer', 'seller', 'outside')],
        'products': [run_id + '-p0', run_id + '-p1'],
        'versions': [run_id + '-v0', run_id + '-v1'],
        'files': [run_id + '-f0', run_id + '-f1'],
        'object_keys': [run_id + '/good.zip', run_id + '/missing.zip'],
        'request_ids': [run_id + '-http'],
    }


def write_manifest(path, identifiers, *, create=False):
    temporary = None
    completed = False
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CREAT | os.O_EXCL, 0o600)
    else:
        check(stat.S_ISREG(path.lstat().st_mode), 'Manifest must be a regular file')
        fd, temporary = tempfile.mkstemp(prefix='.online-delivery-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(identifiers, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if temporary is not None:
            os.replace(temporary, path)
            temporary = None
        completed = True
    finally:
        if temporary is not None:
            os.unlink(temporary)
        if create and not completed:
            os.unlink(path)


def archive():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr('fixture.txt', 'Isolated online delivery acceptance fixture.\n')
    return buffer.getvalue()


def email(uid):
    return uid + '@example.invalid'


def owned_scopes(m, db, ids):
    """Discover only exact owned rows, then their FK children to a fixed point."""
    from sqlalchemy import or_, select

    predicates = {}
    tables = m.Base.metadata.tables

    def add(model, predicate):
        table = getattr(model, '__table__', model)
        predicates[table.name] = predicate

    users = list(ids['users'].values())
    for user in db.scalars(select(m.User).where(m.User.id.in_(users))):
        check(user.email == email(user.id) and user.name == user.id,
              'Refusing cleanup: unexpected user ownership')
    for enterprise in db.scalars(select(m.Enterprise).where(m.Enterprise.id.in_(ids['enterprises']))):
        check(enterprise.name == enterprise.id and enterprise.credit_code == enterprise.id,
              'Refusing cleanup: unexpected enterprise ownership')
    for product in db.scalars(select(m.Product).where(m.Product.id.in_(ids['products']))):
        check(product.name == product.id and product.enterprise_id == ids['enterprises'][1],
              'Refusing cleanup: unexpected product ownership')
    for version in db.scalars(select(m.ProductReleaseVersion).where(
            or_(m.ProductReleaseVersion.id.in_(ids['versions']),
                m.ProductReleaseVersion.product_id.in_(ids['products'])))):
        check(version.id in ids['versions'] and version.product_id ==
              ids['products'][ids['versions'].index(version.id)],
              'Refusing cleanup: unexpected version ownership')
    for member in db.scalars(select(m.Membership).where(m.Membership.user_id.in_(users))):
        check(member.enterprise_id in ids['enterprises'], 'Refusing cleanup: external membership')
    orders = db.scalars(select(m.Order).where(m.Order.product_id.in_(ids['products']))).all()
    check(all(o.buyer_user_id == ids['users']['super']
              and o.buyer_enterprise_id == ids['enterprises'][0]
              and o.provider_enterprise_id == ids['enterprises'][1]
              and o.product_version_id in ids['versions'] for o in orders),
          'Refusing cleanup: unexpected order ownership')
    for item in db.scalars(select(m.FileObject).where(or_(
            m.FileObject.id.in_(ids['files']), m.FileObject.product_id.in_(ids['products'])))):
        check(item.id in ids['files'], 'Refusing cleanup: unplanned file')
        index = ids['files'].index(item.id)
        check(item.product_id == ids['products'][index]
              and item.version_id == ids['versions'][index]
              and item.owner_id == ids['users']['provider']
              and item.object_name == ids['object_keys'][index],
              'Refusing cleanup: unexpected file ownership')
    order_ids = [o.id for o in orders]
    task_ids = list(db.scalars(select(m.DeliveryTask.id).where(m.DeliveryTask.order_id.in_(order_ids))))
    targets = users + ids['enterprises'] + ids['products'] + ids['versions'] + ids['files'] + order_ids + task_ids
    for model, values in ((m.User, users), (m.Enterprise, ids['enterprises']),
                          (m.Product, ids['products']), (m.ProductReleaseVersion, ids['versions']),
                          (m.FileObject, ids['files']), (m.Order, order_ids)):
        add(model, model.id.in_(values))
    add(m.AuditLog, or_(m.AuditLog.target_id.in_(targets), m.AuditLog.order_id.in_(order_ids),
                       m.AuditLog.actor.in_([email(uid) for uid in users]),
                       m.AuditLog.request_id.in_(ids['request_ids'])))
    center = m.message_center
    add(center['Message'], center['Message'].target_id.in_(targets))
    message_ids = list(db.scalars(select(center['Message'].id).where(
        center['Message'].target_id.in_(targets))))
    add(center['ExpiredUrgent'], or_(center['ExpiredUrgent'].message_id.in_(message_ids),
                                    center['ExpiredUrgent'].user_id.in_(users)))
    # Legacy notification targets are also scoped exactly if produced by a route.
    add(m.PlatformNotification, or_(m.PlatformNotification.target_id.in_(targets),
                                   m.PlatformNotification.recipient_user_id.in_(users)))
    while True:
        keys = {}
        for name, predicate in predicates.items():
            for column in tables[name].primary_key.columns:
                keys[column] = list(db.scalars(select(column).where(predicate)))
        expanded = dict(predicates)
        for table in tables.values():
            children = [fk.parent.in_(keys[fk.column]) for fk in table.foreign_keys
                        if keys.get(fk.column)]
            if children:
                expanded[table.name] = or_(*children, *(
                    [predicates[table.name]] if table.name in predicates else []))
        # Compare row identities, not SQL expression text (which keeps growing).
        new_keys = {}
        for name, predicate in expanded.items():
            for column in tables[name].primary_key.columns:
                new_keys[column] = list(db.scalars(select(column).where(predicate)))
        predicates = expanded
        if {c: set(v) for c, v in keys.items()} == {c: set(v) for c, v in new_keys.items()}:
            return predicates, new_keys


def save_database_ids(m, db, ids, manifest, predicates):
    from sqlalchemy import select

    identifiers = dict(ids)
    identifiers['database_ids'] = {
        name: [list(row) for row in db.execute(select(*m.Base.metadata.tables[name].primary_key.columns)
                                              .where(predicate))]
        for name, predicate in predicates.items()
    }
    write_manifest(manifest, identifiers)


def cleanup_db(m, ids, manifest):
    from sqlalchemy import delete, func, select, text

    with m.SessionLocal() as db:
        db.execute(text("SET LOCAL lock_timeout = '15s'"))
        db.scalars(select(m.Order).where(m.Order.product_id.in_(ids['products']))
                   .order_by(m.Order.id).with_for_update()).all()
        Outbox = m.message_center['Outbox']
        db.scalars(select(Outbox).where(Outbox.user_id.in_(list(ids['users'].values())))
                   .order_by(Outbox.id).with_for_update()).all()
        predicates, keys = owned_scopes(m, db, ids)
        save_database_ids(m, db, ids, manifest, predicates)
        for table in reversed(m.Base.metadata.sorted_tables):
            if table.name in predicates:
                db.execute(delete(table).where(predicates[table.name]))
        db.commit()
    with m.SessionLocal() as db:
        for name, predicate in predicates.items():
            check(db.scalar(select(func.count()).select_from(m.Base.metadata.tables[name])
                            .where(predicate)) == 0, 'Owned rows remain in ' + name)
        for table in m.Base.metadata.tables.values():
            for fk in table.foreign_keys:
                if keys.get(fk.column):
                    check(db.scalar(select(func.count()).select_from(table)
                                    .where(fk.parent.in_(keys[fk.column]))) == 0,
                          'Owned FK remains in ' + table.name)


def object_absent(storage, bucket, key):
    from minio.error import S3Error

    try:
        storage.stat_object(bucket, key)
    except S3Error as exc:
        if exc.code in {'NoSuchKey', 'NoSuchObject'}:
            return True
        raise
    return False


def prepare(m, ids, payload):
    tokens = {}
    with m.SessionLocal() as db:
        for eid in ids['enterprises']:
            db.add(m.Enterprise(id=eid, name=eid, credit_code=eid, verification_status='verified'))
        for role, uid in ids['users'].items():
            user = m.User(id=uid, email=email(uid), name=uid, password_hash='not-a-login-password',
                          verified_status='verified', activation_status='active', is_active=True,
                          platform_role='delivery_monitor' if role == 'operator' else '')
            db.add(user)
        db.flush()
        for uid in ids['users'].values():
            db.add(m.message_center['Preference'](user_id=uid, email_enabled=False))
        for role, member_role, enterprise in (
                ('super', 'super_admin', 0), ('admin', 'enterprise_admin', 0),
                ('member', 'member', 0), ('other', 'super_admin', 2), ('provider', 'super_admin', 1)):
            db.add(m.Membership(user_id=ids['users'][role], enterprise_id=ids['enterprises'][enterprise],
                                role=member_role, status='active', business_roles='provider,user'))
        for index, pid in enumerate(ids['products']):
            db.add(m.Product(id=pid, enterprise_id=ids['enterprises'][1], name=pid,
                             provider_name=ids['enterprises'][1], product_type='dataset',
                             delivery_method='file', status='published', download_limit=1,
                             settlement_rule_mode='custom', settlement_rule_json=json.dumps({
                                 'platform_rate': 20, 'provider_rate': 80, 'service_rate': 0,
                                 'expert_rate': 0, 'channel_rate': 0})))
            db.flush()
            db.add(m.ProductReleaseVersion(id=ids['versions'][index], product_id=pid,
                                          version_code='v1', price=12, cost=7, status='active'))
            db.flush()
            db.add(m.FileObject(id=ids['files'][index], owner_id=ids['users']['provider'],
                                product_id=pid, version_id=ids['versions'][index],
                                object_name=ids['object_keys'][index], original_name='fixture.zip',
                                content_type='application/zip', size=len(payload),
                                checksum=hashlib.sha256(payload).hexdigest(), file_role='product_data',
                                status='active', scan_status='clean', scanned_at=m.now(),
                                scan_report=json.dumps({'presidio_status': 'available',
                                                        'fixture': True, 'real_scan_verified': False})))
        db.commit()
        for role, uid in ids['users'].items():
            tokens[role] = m.issue_token(db.get(m.User, uid))
    return tokens


def process_owned(m, ids, tokens, order_id, expected_count):
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    # Resolve an exact owned task, then let the HTTP route take production locks.
    with m.SessionLocal() as db:
        order = db.get(m.Order, order_id)
        check(order is not None and order.product_id in ids['products'], 'Order outside fixture scope')
        tasks = db.scalars(select(m.DeliveryTask).where(m.DeliveryTask.order_id == order_id)).all()
        check(len(tasks) == 1, 'Expected exactly one owned task')
        task = tasks[0]
        check(task.retry_count == expected_count and task.status in {'in_delivery', 'retrying'},
              'Unexpected task state; another worker may have processed this fixture')
        check(task.next_retry_at is not None and task.next_retry_at <= m.now(), 'Task is not due')
        task_id = task.id
    client = TestClient(m.app)
    started = m.now()
    try:
        response = client.post(f'/api/delivery-tasks/{task_id}/process', headers={
            'Authorization': 'Bearer ' + tokens['operator'], 'X-Request-Id': ids['request_ids'][0]},
            json={'success': True})
        check(response.status_code == 200, 'Production HTTP delivery process failed')
    finally:
        client.close()
    finished = m.now()
    with m.SessionLocal() as db:
        order, task = db.get(m.Order, order_id), db.get(m.DeliveryTask, task_id)
        success = task.status == 'completed'
        result = {'success': success, 'main': order.main_status, 'delivery': order.delivery_status,
                  'status': task.status, 'retry_count': task.retry_count, 'next_retry_at': task.next_retry_at}
        if not success and task.retry_count < 3:
            check(started + timedelta(seconds=10) <= task.next_retry_at <= finished + timedelta(seconds=10),
                  'Retry is not scheduled ten seconds after failure')
        return result


def exercise(m, ids, tokens, storage, payload):
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    client = TestClient(m.app)  # Deliberately no context manager/lifespan.

    def request(method, path, role='super', expected=200, **kwargs):
        response = client.request(method, path, headers={
            'Authorization': 'Bearer ' + tokens[role], 'X-Request-Id': ids['request_ids'][0]}, **kwargs)
        check(response.status_code == expected, f'{method} {path}: expected {expected}, got {response.status_code}')
        return response

    def access(order_id, role='super', expected=200):
        return request('GET', f'/api/orders/{order_id}/delivery-access', role, expected)

    def download(order_id, index, role='super', expected=200):
        return request('GET', f"/api/files/{ids['files'][index]}/download?order_id={order_id}", role, expected)

    try:
        orders = []
        for index in range(2):
            order_id = request('POST', '/api/orders', json={
                'product_id': ids['products'][index], 'product_version_id': ids['versions'][index],
                'buyer_enterprise_id': ids['enterprises'][0], 'subscription_months': 1}).json()['id']
            orders.append(order_id)
            request('POST', f'/api/orders/{order_id}/transition', json={
                'action': 'confirm_payment', 'reason': 'Acceptance fixture: simulated payment, no bank transaction'})
            check(access(order_id).json()['ready'] is False, 'Unfulfilled order is accessible')
        good = process_owned(m, ids, tokens, orders[0], 0)
        check(good['success'] and (good['main'], good['delivery'], good['status']) ==
              ('completed', 'accepted', 'completed'), 'File did not complete automatically')
        for role in ('super', 'admin'):
            check(access(orders[0], role).json()['ready'] is True, 'Buyer administrator lacks access')
        for role in ('member', 'other'):
            access(orders[0], role, 403)
            download(orders[0], 0, role, 403)
        check(download(orders[0], 0).content == payload, 'MinIO download differs from uploaded ZIP')
        download(orders[0], 0, 'admin', 429)
        check(access(orders[0]).json()['ready'] is False, 'Exhausted quota remains ready')
        failures = []
        for count in range(3):
            failed = process_owned(m, ids, tokens, orders[1], count)
            failures.append(failed['status'])
            check(not failed['success'] and failed['retry_count'] == count + 1, 'Missing object did not fail')
            if count < 2:
                check(failed['status'] == 'retrying', 'Retry did not remain automatic')
                time.sleep(max(0, (failed['next_retry_at'] - m.now()).total_seconds()) + 0.05)
            else:
                check(failed['status'] == failed['delivery'] == 'exception'
                      and failed['next_retry_at'] is None, 'Third failure did not enter exception')
        request('POST', f'/api/orders/{orders[1]}/transition', role='operator', json={
            'action': 'retry_delivery', 'reason': 'Acceptance fixture object repair'})
        storage.put_object(m.MINIO_BUCKET, ids['object_keys'][1], BytesIO(payload), len(payload),
                           content_type='application/zip')
        recovered = process_owned(m, ids, tokens, orders[1], 3)
        check(recovered['success'] and recovered['main'] == 'completed'
              and recovered['delivery'] == 'accepted', 'Manual retry did not recover')
        check(access(orders[1], 'admin').json()['ready'] is True, 'Admin lacks recovered file access')
        check(download(orders[1], 1, 'admin').content == payload, 'Admin download failed')
        download(orders[1], 1, expected=429)
        check(access(orders[1], 'admin').json()['ready'] is False, 'Recovered order quota remains ready')
        with m.SessionLocal() as db:
            for action, count in (('delivery_succeeded', 2), ('delivery_failed', 3), ('retry_delivery', 1)):
                check(db.scalar(select(func.count()).select_from(m.AuditLog).where(
                    m.AuditLog.order_id.in_(orders), m.AuditLog.action == action)) == count,
                    'Unexpected delivery audit count for ' + action)
            for kind, count in (('payment', 2), ('fulfillment', 5), ('download', 2)):
                check(db.scalar(select(func.count()).select_from(m.SettlementMeasurement).where(
                    m.SettlementMeasurement.order_id.in_(orders),
                    m.SettlementMeasurement.measurement_type == kind)) == count,
                    'Unexpected metering count for ' + kind)
            tasks = db.scalars(select(m.DeliveryTask).where(m.DeliveryTask.order_id.in_(orders))).all()
            Message = m.message_center['Message']
            check(db.scalar(select(func.count()).select_from(Message).where(
                Message.target_id.in_([t.id for t in tasks]))) == 5,
                'Expected five real delivery outcome messages')
            recovered_task = next(t for t in tasks if t.order_id == orders[1])
            check(recovered_task.max_retries == 6 and recovered_task.retry_count == 3,
                  'Manual retry did not extend the cumulative attempt budget')
        return {'automatic_completion': True, 'manual_accept_required': False,
                'buyer_roles': ['super_admin', 'enterprise_admin'], 'denied_http': [403, 403],
                'downloads_http': [200, 429, 200, 429], 'quota_ready_after_download': False,
                'missing_object_states': failures, 'retry_delay_seconds': 10, 'manual_retry_repaired': True}
    finally:
        client.close()


def browser_order(m, ids, tokens, base_url, deployment_tag):
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    client = TestClient(m.app)
    headers = {'Authorization': 'Bearer ' + tokens['super'], 'X-Request-Id': ids['request_ids'][0]}
    try:
        response = client.post('/api/orders', headers=headers, json={
            'product_id': ids['products'][0], 'product_version_id': ids['versions'][0],
            'buyer_enterprise_id': ids['enterprises'][0], 'subscription_months': 1})
        check(response.status_code == 200, 'Browser order creation failed')
        order_id = response.json()['id']
        response = client.post(f'/api/orders/{order_id}/transition', headers=headers, json={
            'action': 'confirm_payment', 'reason': 'Browser fixture: simulated payment'})
        check(response.status_code == 200, 'Browser simulated payment failed')
        completed = process_owned(m, ids, tokens, order_id, 0)
        check(completed['success'] and completed['main'] == 'completed'
              and completed['delivery'] == 'accepted', 'Browser order did not complete')
        response = client.get(f'/api/orders/{order_id}/delivery-access', headers={
            **headers, 'Authorization': 'Bearer ' + tokens['admin']})
        check(response.status_code == 200 and response.json()['ready'] is True,
              'Browser buyer administrator cannot access untouched quota')
        with m.SessionLocal() as db:
            check(db.scalar(select(func.count()).select_from(m.FileDownloadLog).where(
                m.FileDownloadLog.order_id == order_id)) == 0, 'Browser fixture was already downloaded')
            order = db.get(m.Order, order_id)
            return {'buyerAdminToken': tokens['admin'], 'expectedOrderNo': order.order_no,
                    'baseUrl': base_url.rstrip('/'), 'deploymentTag': deployment_tag}
    finally:
        client.close()


def load_main():
    # Must precede importing production main; importing does not run lifespan.
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    logging.getLogger('sqlalchemy.engine').disabled = True
    from app import main as m
    check(m.engine.dialect.name == 'postgresql', 'Real PostgreSQL required')
    check(m.MINIO_ENDPOINT, 'Real MinIO endpoint required')
    check(not m.app.dependency_overrides, 'Normal HTTP dependencies required')
    return m


def cleanup_objects(m, storage, ids):
    errors = []
    for key in ids['object_keys']:
        try:
            storage.remove_object(m.MINIO_BUCKET, key)
            check(object_absent(storage, m.MINIO_BUCKET, key), 'Owned object remains')
        except Exception as exc:
            errors.append({'scope': 'object', 'error_type': type(exc).__name__})
    return errors


def cleanup_all(m, storage, ids, manifest):
    errors = []
    # Prove root ownership before deleting either DB rows or storage objects.
    try:
        with m.SessionLocal() as db:
            owned_scopes(m, db, ids)
    except Exception as exc:
        error = {'scope': 'ownership', 'error_type': type(exc).__name__}
        if isinstance(exc, AcceptanceFailure):
            error['check_failed'] = str(exc)
        return [error]
    try:
        cleanup_db(m, ids, manifest)
    except Exception as exc:
        errors.append({'scope': 'database', 'error_type': type(exc).__name__})
        if isinstance(exc, AcceptanceFailure):
            errors[-1]['check_failed'] = str(exc)
    errors.extend(cleanup_objects(m, storage, ids))
    return errors


def cleanup_saved(manifest_path):
    import re

    manifest = Path(manifest_path)
    mode = manifest.lstat().st_mode
    check(stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600, 'Ownership manifest must be regular and 0600')
    saved = json.loads(manifest.read_text())
    run_id = saved.get('run_id', '')
    check(isinstance(run_id, str) and re.fullmatch(r'qa-online-[0-9a-f]{12}', run_id),
          'Invalid fixture ownership marker')
    ids = fixture_ids(run_id)
    check(all(saved.get(key) == value for key, value in ids.items()), 'Ownership IDs or keys were changed')
    m = load_main()
    check(saved.get('bucket') == m.MINIO_BUCKET, 'Cleanup bucket differs from ownership manifest')
    ids['bucket'] = saved['bucket']
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY,
                      secret_key=m.MINIO_SECRET_KEY, secure=False)
    errors = cleanup_all(m, storage, ids, manifest)
    browser_path = manifest.with_suffix('.browser.json')
    if browser_path.exists() or browser_path.is_symlink():
        check(stat.S_ISREG(browser_path.lstat().st_mode), 'Browser fixture must be a regular file')
        browser_path.unlink()
    return {'status': 'failed' if errors else 'passed', 'mode': 'cleanup',
            'manifest': str(manifest), 'cleanup_errors': errors,
            'cleanup': 'failed' if errors else 'verified exact DB/FK rows and MinIO keys absent; browser token removed'}


def run(manifest_path=None, *, prepare_browser=False, base_url=None, deployment_tag=None):
    from sqlalchemy import func, or_, select
    from unittest.mock import patch

    if prepare_browser:
        url = urlsplit(base_url or '')
        check(url.scheme in {'http', 'https'} and url.hostname and not url.username
              and not url.password and not url.query and not url.fragment,
              'Browser base URL must be HTTP(S) without credentials, query or fragment')
        check(bool(deployment_tag and deployment_tag.strip()), 'Browser deployment tag required')
    m = load_main()
    ids = fixture_ids()
    ids['bucket'] = m.MINIO_BUCKET
    manifest = Path(manifest_path) if manifest_path else Path('/tmp') / (ids['run_id'] + '.json')
    browser_path = manifest.with_suffix('.browser.json')
    check(not prepare_browser or (not browser_path.exists() and not browser_path.is_symlink()),
          'Browser fixture path already exists')
    check(not manifest.exists() and not manifest.is_symlink(), 'Ownership manifest path already exists')
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY,
                      secret_key=m.MINIO_SECRET_KEY, secure=False)
    check(storage.bucket_exists(m.MINIO_BUCKET), 'Existing MinIO bucket required; script never creates it')
    # Finish every collision check before permitting any cleanup or mutations.
    for key in ids['object_keys']:
        check(object_absent(storage, m.MINIO_BUCKET, key), 'Object collision; refusing ownership')
    with m.SessionLocal() as db:
        for model, values in ((m.User, list(ids['users'].values())), (m.Enterprise, ids['enterprises']),
                              (m.Product, ids['products']), (m.ProductReleaseVersion, ids['versions']),
                              (m.FileObject, ids['files'])):
            predicate = model.id.in_(values)
            if model is m.User:
                predicate = or_(predicate, m.User.email.in_([email(uid) for uid in values]))
            if model is m.Enterprise:
                predicate = or_(predicate, m.Enterprise.credit_code.in_(values))
            check(db.scalar(select(func.count()).select_from(model).where(predicate)) == 0,
                  'Fixture collision; refusing existing rows')
    # A recoverable ownership manifest is valid only after all collision checks.
    write_manifest(manifest, ids, create=True)
    payload = archive()
    result = {'status': 'failed', 'database': 'real PostgreSQL', 'storage': 'real MinIO',
              'payment': 'HTTP simulated confirmation; no bank/payment-provider verification',
              'scan': 'fixture report; no real ClamAV/Presidio scan acceptance',
              'processing': 'production HTTP process on owned task; global queue not invoked',
              'notification_failure_rollback': 'not exercised live; isolated unit-test concern',
              'notification_recipients': 'owned fixture users only; production message creation preserved',
              'manifest': str(manifest), 'manifest_mode': '0600'}

    original_create = m.message_center['create']
    retained = False
    browser_written = False

    def isolated_create(db, recipients, title, content, target_type='', target_id='', **kwargs):
        if target_type == 'order':
            order = db.get(m.Order, target_id)
        elif target_type == 'delivery_task':
            task = db.get(m.DeliveryTask, target_id)
            order = db.get(m.Order, task.order_id) if task else None
        else:
            order = None
        if order is not None and order.product_id in ids['products']:
            recipients = [uid for uid in recipients if uid in ids['users'].values()]
        return original_create(db, recipients, title, content, target_type, target_id, **kwargs)

    try:
        storage.put_object(m.MINIO_BUCKET, ids['object_keys'][0], BytesIO(payload), len(payload),
                           content_type='application/zip')
        tokens = prepare(m, ids, payload)
        with patch.dict(m.message_center, {'create': isolated_create}):
            result.update(exercise(m, ids, tokens, storage, payload))
            if prepare_browser:
                browser_fixture = browser_order(m, ids, tokens, base_url, deployment_tag)
                with m.SessionLocal() as db:
                    predicates, _ = owned_scopes(m, db, ids)
                    save_database_ids(m, db, ids, manifest, predicates)
                write_manifest(browser_path, browser_fixture, create=True)
                browser_written = True
                result['browser_fixture'] = str(browser_path)
                result['browser_fixture_mode'] = '0600'
                result['browser_order_quota'] = 'one download available; not downloaded by preparation'
                retained = True
        result['status'] = 'passed'
    except Exception as exc:
        result['error_type'] = type(exc).__name__
        if isinstance(exc, AcceptanceFailure):
            result['check_failed'] = str(exc)
    finally:
        if retained:
            result['cleanup'] = 'deferred; use --cleanup --manifest with this ownership manifest'
        else:
            cleanup_errors = cleanup_all(m, storage, ids, manifest)
            if browser_written:
                browser_path.unlink()
            if cleanup_errors:
                result['status'] = 'failed'
                result['cleanup_errors'] = cleanup_errors
            else:
                result['cleanup'] = 'verified exact owned DB rows, FK children and MinIO keys absent'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', help='New private manifest path; existing files are refused')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--prepare-browser', action='store_true', help='Retain fixtures and write a private browser token file')
    modes.add_argument('--cleanup', action='store_true', help='Clean exact fixtures from an existing ownership manifest')
    parser.add_argument('--base-url', help='Browser application URL for --prepare-browser')
    parser.add_argument('--deployment-tag', help='Deployed image tag recorded for browser verification')
    args = parser.parse_args()
    if args.cleanup and not args.manifest:
        parser.error('--cleanup requires --manifest')
    if args.prepare_browser and (not args.base_url or not args.deployment_tag):
        parser.error('--prepare-browser requires --base-url and --deployment-tag')
    # Avoid SDK/SQL exception logs exposing credentials or statement parameters.
    logging.disable(logging.CRITICAL)
    try:
        result = cleanup_saved(args.manifest) if args.cleanup else run(
            args.manifest, prepare_browser=args.prepare_browser,
            base_url=args.base_url, deployment_tag=args.deployment_tag)
    except Exception as exc:
        result = {'status': 'failed', 'error_type': type(exc).__name__,
                  'phase': 'cleanup' if args.cleanup else 'preflight'}
        if isinstance(exc, AcceptanceFailure):
            result['check_failed'] = str(exc)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
