"""Read-only checks of deployed role retirement and existing token rejection."""
import argparse
import json
import os
from pathlib import Path

import httpx
from sqlalchemy import or_, select


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m

    with m.SessionLocal() as db:
        retired = db.scalars(select(m.User).where(or_(
            m.User.username == 'ywsh', m.User.platform_role == 'business_reviewer'))).all()
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        assert admin is not None
        admin_token = m.issue_token(admin)
        checks = []
        for account in retired:
            assert not account.is_active
            assert account.activation_status in {'disabled', 'deleted'}
            assert account.platform_role != 'product_manager'
            checks.append({'check': 'retired_account_disabled', 'passed': True})
        protected_order = db.scalar(select(m.Order).where(
            m.Order.order_no == 'ORD-261012005144E1B3'))
        protected_product = db.scalar(select(m.Product).where(
            m.Product.name == 'qa-vcq-a044ea9ea181-p2'))
        assert protected_order is not None and protected_product is not None
        tokens = [(account.email or account.username, m.issue_token(account)) for account in retired]
    with httpx.Client(base_url='http://192.168.10.10:30080',
                      trust_env=False, timeout=20) as client:
        catalog = client.get('/api/admin/platform-roles',
            headers={'Authorization': 'Bearer ' + admin_token})
        catalog.raise_for_status()
        body = catalog.json()
        assert 'business_reviewer' not in body['roles']
        assert len(body['roles']) == 6
        assert not any(row['username'] == 'ywsh' or row['role'] == 'business_reviewer'
                       for row in body['items'])
        checks.append({'check': 'six_role_catalog', 'passed': True})
        for identifier, token in tokens:
            response = client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + token})
            assert response.status_code == 401
            response = client.post('/api/auth/login', json={
                'identifier': identifier, 'password': 'Admin123!'})
            assert response.status_code == 401
            checks.append({'check': 'retired_password_and_token_rejected', 'passed': True})
    result = {'status': 'passed', 'checks': checks, 'retired_accounts': len(retired),
              'protected_objects_present': True,
              'boundary': 'read-only runtime checks; historical preservation and migration idempotency tested separately'}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
