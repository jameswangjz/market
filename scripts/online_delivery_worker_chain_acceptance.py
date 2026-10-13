"""Observe real file delivery worker retries without processing its tasks manually.

Uses exact disposable DB/MinIO fixtures and synthetic scan metadata. This is
worker/payment/download acceptance, not real malware/PII scanning acceptance.
"""
from io import BytesIO
import json
import logging
from pathlib import Path
import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from online_delivery_acceptance import (
    AcceptanceFailure, archive, check, cleanup_all, email, fixture_ids,
    load_main, object_absent, prepare, write_manifest,
)


def run():
    m = load_main()
    ids = fixture_ids()
    ids['bucket'] = m.MINIO_BUCKET
    manifest = Path('/tmp') / (ids['run_id'] + '-worker.json')
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY,
                      secret_key=m.MINIO_SECRET_KEY, secure=False)
    check(storage.bucket_exists(m.MINIO_BUCKET), 'Existing bucket required')
    for key in ids['object_keys']:
        check(object_absent(storage, m.MINIO_BUCKET, key), 'Object collision')
    with m.SessionLocal() as db:
        for model, values in ((m.User, list(ids['users'].values())),
                              (m.Enterprise, ids['enterprises']),
                              (m.Product, ids['products']),
                              (m.ProductReleaseVersion, ids['versions']),
                              (m.FileObject, ids['files'])):
            check(not db.scalars(select(model.id).where(model.id.in_(values))).all(), 'DB collision')
        check(not db.scalars(select(m.User.id).where(m.User.email.in_(
            [email(uid) for uid in ids['users'].values()]))).all(), 'Email collision')
    write_manifest(manifest, ids, create=True)
    result = {'status': 'failed', 'scope': 'actual file worker and MinIO delivery',
              'scan': 'synthetic metadata; no ClamAV/Presidio scanning assertion',
              'payment': 'simulated confirmation; no bank channel'}
    client = TestClient(m.app)
    try:
        payload = archive()
        storage.put_object(m.MINIO_BUCKET, ids['object_keys'][0], BytesIO(payload), len(payload),
                           content_type='application/zip')
        tokens = prepare(m, ids, payload)

        def request(method, path, role='super', expected=200, **kwargs):
            response = client.request(method, path, headers={
                'Authorization': 'Bearer ' + tokens[role], 'X-Request-Id': ids['request_ids'][0]}, **kwargs)
            check(response.status_code == expected, f'Expected HTTP {expected}, got {response.status_code}')
            return response

        def wait(order_id, expected):
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                with m.SessionLocal() as db:
                    order = db.get(m.Order, order_id)
                    task = db.scalar(select(m.DeliveryTask).where(m.DeliveryTask.order_id == order_id))
                    if task and task.status == expected:
                        return order.main_status, order.delivery_status, task.id, task.retry_count
                time.sleep(.5)
            raise AcceptanceFailure('Actual worker did not reach expected state: ' + expected)

        orders = []
        for index in range(2):
            order_id = request('POST', '/api/orders', json={
                'product_id': ids['products'][index], 'product_version_id': ids['versions'][index],
                'buyer_enterprise_id': ids['enterprises'][0], 'subscription_months': 1}).json()['id']
            orders.append(order_id)
            request('POST', f'/api/orders/{order_id}/transition', json={
                'action': 'confirm_payment', 'reason': 'Owned real-worker simulated payment verification'})
        good = wait(orders[0], 'completed')
        check(good[:2] == ('completed', 'accepted') and good[3] == 0, 'Valid file did not fulfill')
        failed = wait(orders[1], 'exception')
        check(failed[1] == 'exception' and failed[3] == 3, 'Three failures did not enter manual exception')
        with m.SessionLocal() as db:
            logs = db.scalars(select(m.AuditLog).where(m.AuditLog.order_id == orders[1],
                m.AuditLog.action == 'delivery_failed').order_by(m.AuditLog.created_at, m.AuditLog.id)).all()
            check(len(logs) == 3 and all(x.actor == 'jfsh@market.local' for x in logs),
                  'Failure attempts not executed by deployed worker')
            intervals = [(logs[i].created_at - logs[i-1].created_at).total_seconds() for i in (1, 2)]
            check(all(x >= 10 for x in intervals), 'Automatic retry interval shorter than ten seconds')
            task = db.get(m.DeliveryTask, failed[2])
            check(task.next_retry_at is None, 'Exception continues automatic retries')
        request('GET', f'/api/orders/{orders[0]}/delivery-access', role='member', expected=403)
        request('GET', f'/api/files/{ids["files"][0]}/download?order_id={orders[0]}', role='member', expected=403)
        check(request('GET', f'/api/files/{ids["files"][0]}/download?order_id={orders[0]}').content == payload,
              'Actual download content mismatch')
        request('GET', f'/api/files/{ids["files"][0]}/download?order_id={orders[0]}', role='admin', expected=429)
        check(request('GET', f'/api/orders/{orders[0]}/delivery-access').json()['ready'] is False,
              'Exhausted file quota remains ready')
        storage.put_object(m.MINIO_BUCKET, ids['object_keys'][1], BytesIO(payload), len(payload),
                           content_type='application/zip')
        request('POST', f'/api/orders/{orders[1]}/transition', role='operator', json={
            'action': 'retry_delivery', 'reason': 'Owned missing object restored'})
        recovered = wait(orders[1], 'completed')
        check(recovered[:2] == ('completed', 'accepted') and recovered[3] == 3,
              'Manual recovery reset attempts or did not fulfill')
        check(request('GET', f'/api/files/{ids["files"][1]}/download?order_id={orders[1]}', role='admin').content == payload,
              'Recovered file is not downloadable')
        result.update(status='passed', automatic_completion=True, attempts=3,
                      retry_intervals_seconds=intervals, manual_recovery=True,
                      member_denied=True, download_quota_enforced=True)
    except Exception as exc:
        result.update(status='failed', error_type=type(exc).__name__)
        if isinstance(exc, AcceptanceFailure):
            result['check_failed'] = str(exc)
    finally:
        client.close()
        errors = cleanup_all(m, storage, ids, manifest)
        result['cleanup_verified'] = not errors
        if errors:
            result.update(status='failed', cleanup_errors=errors)
    return result


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        outcome = run()
    except Exception as exc:
        outcome = {'status': 'failed', 'error_type': type(exc).__name__, 'phase': 'preflight'}
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    raise SystemExit(0 if outcome['status'] == 'passed' else 1)
