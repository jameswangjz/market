"""Read-only deployed provider query checks; never create or modify business data."""
import argparse
import hashlib
import json
from pathlib import Path

import httpx
from sqlalchemy import select


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from app import main as m

    def fingerprint(db):
        # Only lifecycle rows, no user records, credential values or raw payloads.
        result = {}
        for name in ('Order', 'Payment', 'Refund', 'SaaSSubscription', 'SubscriptionTerm'):
            model = getattr(m, name)
            columns = list(model.__table__.columns)
            rows = db.execute(select(*columns).order_by(model.id)).all()
            values = [[str(value) if value is not None else None for value in row] for row in rows]
            result[name] = dict(count=len(rows), sha256=hashlib.sha256(
                json.dumps(values, separators=(',', ':')).encode()).hexdigest())
        return result

    with m.SessionLocal() as db:
        before = fingerprint(db)
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        assert admin and admin.is_active and admin.platform_role == 'super_admin'
        token = m.jwt.encode({'sub': admin.id, 'email': admin.email,
            'exp': int(m.now().timestamp()) + 120}, m.JWT_SECRET, algorithm='HS256')
        orders = db.scalars(select(m.Order).where(
            m.Order.delivery_method_snapshot.in_(('api', 'model_api', 'tenant_access')))
            .order_by(m.Order.created_at.desc()).limit(20)).all()
        targets = list(dict.fromkeys(order.product_id for order in orders))[:5]
    checks = []
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=20,
                      trust_env=False, follow_redirects=False) as client:
        for product_id in targets:
            path = f'/api/provider/products/{product_id}/entitlements'
            assert client.get(path).status_code == 401
            response = client.get(path, headers={'Authorization': 'Bearer ' + token},
                                  params={'page': 1, 'page_size': 5})
            assert response.status_code == 200, (response.status_code, response.text[:100])
            body = response.json()
            assert body['page_size'] == 5 and len(body['items']) <= 5
            assert response.headers['Cache-Control'] == 'no-store'
            for item in body['items']:
                assert item['product_id'] == product_id
                assert item['order_no'].startswith('ORD-')
                assert 'effective_rights' in item and 'subscription_expires_at' in item
                assert not any(key in item for key in ('client_secret', 'cost', 'buyer_user_id'))
            invalid = client.get(path, headers={'Authorization': 'Bearer ' + token},
                                 params={'page_size': 101})
            assert invalid.status_code == 422
            checks.append(dict(product_id=product_id, total=body['total'],
                sampled=len(body['items']), unauthorized=401, invalid_pagination=422,
                query_status=200))
    with m.SessionLocal() as db:
        after = fingerprint(db)
    assert before == after, 'Concurrent lifecycle changes detected; investigate before claiming no writes'
    result = dict(captured_at=m.now().isoformat(), checks=checks,
        business_rows_unchanged=True, fingerprints=after,
        scope='Actual HTTP read-only provider queries; no trading or financial mutations')
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
