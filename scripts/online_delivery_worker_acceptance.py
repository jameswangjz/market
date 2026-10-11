"""Verify an actually deployed worker using the retained private fixture only."""
import argparse
import json
import logging
from pathlib import Path
import re
import stat
import time


def run(path):
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select
    from online_delivery_acceptance import fixture_ids, check, load_main, owned_scopes

    manifest = Path(path)
    check(stat.S_ISREG(manifest.lstat().st_mode) and stat.S_IMODE(manifest.stat().st_mode) == 0o600,
          'Private regular ownership manifest required')
    saved = json.loads(manifest.read_text())
    check(re.fullmatch(r'qa-online-[0-9a-f]{12}', saved.get('run_id', '')), 'Invalid scope marker')
    ids = fixture_ids(saved['run_id'])
    check(all(saved.get(key) == value for key, value in ids.items()), 'Fixture scope changed')
    m = load_main()
    check(saved.get('bucket') == m.MINIO_BUCKET, 'Fixture storage mismatch')
    with m.SessionLocal() as db:
        owned_scopes(m, db, ids)
        pending_others = db.scalar(select(func.count()).select_from(m.Order).join(m.DeliveryTask,
            m.DeliveryTask.order_id == m.Order.id).where(m.Order.snapshot_version == 1,
            m.Order.delivery_method_snapshot == 'file', m.Order.product_id.not_in(ids['products']),
            m.DeliveryTask.status.in_(['in_delivery', 'retrying'])))
        check(pending_others == 0, 'Refusing smoke test while other file jobs are pending')
        buyer = db.get(m.User, ids['users']['super'])
        check(buyer is not None, 'Retained fixture required')
        headers = {'Authorization': 'Bearer '+m.issue_token(buyer), 'X-Request-Id': ids['request_ids'][0]}
    client = TestClient(m.app)
    try:
        result = client.post('/api/orders', headers=headers, json={'product_id': ids['products'][0],
            'product_version_id': ids['versions'][0], 'buyer_enterprise_id': ids['enterprises'][0]})
        check(result.status_code == 200, 'Worker fixture order creation failed')
        oid = result.json()['id']
        result = client.post(f'/api/orders/{oid}/transition', headers=headers,
            json={'action': 'confirm_payment', 'reason': 'Worker acceptance fixture: simulated payment'})
        check(result.status_code == 200, 'Worker fixture payment failed')
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            with m.SessionLocal() as db:
                order = db.get(m.Order, oid)
                task = db.scalar(select(m.DeliveryTask).where(m.DeliveryTask.order_id == oid))
                if (order.main_status, order.delivery_status, task.status) == ('completed', 'accepted', 'completed'):
                    audit = db.scalar(select(m.AuditLog).where(m.AuditLog.order_id == oid,
                        m.AuditLog.action == 'delivery_succeeded'))
                    check(audit is not None and audit.actor == 'jfsh@market.local', 'Unexpected delivery operator')
                    check(task.retry_count == 0, 'Worker did not fulfill on its first check')
                    return {'status': 'passed', 'mode': 'actual deployed worker', 'order_id': oid,
                            'operator': audit.actor, 'main_status': order.main_status,
                            'delivery_status': order.delivery_status, 'task_status': task.status,
                            'cleanup': 'retained; exact parent manifest cleanup required'}
            time.sleep(1)
        check(False, 'Deployed worker did not complete owned task within 60 seconds')
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        result = run(args.manifest)
    except Exception as exc:
        from online_delivery_acceptance import AcceptanceFailure
        result = {'status': 'failed', 'error_type': type(exc).__name__}
        if isinstance(exc, AcceptanceFailure):
            result['check_failed'] = str(exc)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
