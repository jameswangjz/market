"""Installed-code, GET-only refund ingress checks; never executes a refund."""
from datetime import timedelta
import json

import httpx
from sqlalchemy import select, text


def main():
    from app import main as m
    results = {}
    forbidden = {'cost', 'total_cost', 'cost_amount', 'economic_snapshot', 'delivery_snapshot',
                 'upstream_url', 'client_secret', 'key_secret', 'access_token'}

    def safe(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value), 'Private refund fields leaked'
            for child in value.values():
                safe(child)
        elif isinstance(value, list):
            for child in value:
                safe(child)

    with m.SessionLocal() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        for role, expected in [('super_admin', 200), ('platform_operator', 200),
                               ('finance_settlement', 403), ('delivery_monitor', 403)]:
            user = db.scalar(select(m.User).where(m.User.platform_role == role, m.User.is_active.is_(True),
                m.User.verified_status == 'verified', m.User.activation_status == 'active').limit(1))
            assert user is not None, 'Required verification role absent'
            at = m.now()
            token = m.jwt.encode({'sub': user.id, 'email': user.email, 'iat': at,
                'exp': at + timedelta(minutes=2)}, m.JWT_SECRET, algorithm='HS256')
            response = httpx.get('http://market-web/api/refunds', params={'page': 1, 'page_size': 20},
                headers={'Authorization': 'Bearer ' + token}, timeout=10)
            assert response.status_code == expected, 'Refund role response mismatch: ' + role
            if expected == 200:
                body = response.json()
                safe(body)
                assert body['page'] == 1 and body['page_size'] == 20
                assert all(item['execution_available'] is False for item in body['items'])
                results[role] = {'status': 200, 'rows': len(body['items']), 'private_fields_absent': True}
            else:
                results[role] = {'status': 403}
        cursor = db.get(m.api_charge_ledger['Cursor'], 'market:apisix:charges:v1')
        assert cursor is not None, 'Charge worker has not registered source continuity'
        protected = db.scalar(text("SELECT count(*) FROM orders WHERE order_no='ORD-261012005144E1B3'"))
        assert protected == 1, 'Protected order existence check failed'
        db.rollback()
    print(json.dumps({'status': 'passed', 'scope': 'installed actual GET through market-web',
        'roles': results, 'charge_worker_source_registered': True, 'protected_order_count': 1,
        'mutating_http_requests': 0, 'test_database_writes': 0,
        'limits': 'No quote POST, request/review/manual mutation, funds execution or browser E2E claimed'}))


if __name__ == '__main__':
    main()
