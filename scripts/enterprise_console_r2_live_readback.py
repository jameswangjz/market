"""Read deployed R2 SaaS trees and product readiness without business mutations."""
import argparse
import json
import os
from pathlib import Path

import httpx
from sqlalchemy import select


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--browser-fixture', type=Path)
    args = parser.parse_args()
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    with m.SessionLocal() as db:
        product = db.scalar(select(m.Product).where(m.Product.name == 'SaaS-APP1'))
        if not product:
            raise RuntimeError('Live SaaS fixture unavailable')
        order = db.scalar(select(m.Order).where(m.Order.product_id == product.id,
            m.Order.subscription_id != '', m.Order.payment_status == 'paid'))
        if not order:
            raise RuntimeError('Paid SaaS fixture unavailable')
        buyer = db.get(m.User, order.buyer_user_id)
        provider = db.scalar(select(m.User).join(m.Membership,
            m.User.id == m.Membership.user_id).where(
                m.Membership.enterprise_id == product.enterprise_id,
                m.Membership.role == 'super_admin', m.Membership.status == 'active',
                m.User.platform_role == '', m.User.verified_status == 'verified'))
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not all((buyer, provider, admin)):
            raise RuntimeError('Live actors unavailable')
        tokens = {name: m.issue_token(actor) for name, actor in
            [('buyer', buyer), ('provider', provider), ('admin', admin)]}
        fixture = {'tenantToken': tokens['buyer'], 'providerToken': tokens['provider'],
            'adminToken': tokens['admin'], 'orderId': order.id, 'orderNo': order.order_no,
            'productId': product.id, 'subscriptionId': order.subscription_id,
            'buyerEnterpriseId': order.buyer_enterprise_id,
            'providerEnterpriseId': product.enterprise_id}
    checks = []
    with httpx.Client(base_url='http://192.168.10.10:30080', trust_env=False, timeout=20) as client:
        def get(path, actor, expected=200, params=None):
            response = client.get(path, params=params,
                headers={'Authorization': 'Bearer ' + tokens[actor]})
            if response.status_code != expected:
                raise AssertionError(f'{actor} {path}: {response.status_code} != {expected}')
            checks.append({'actor': actor, 'path': path, 'status': expected})
            return response.json()
        tree = get('/api/saas-subscriptions/' + fixture['subscriptionId'] + '/sync-tree', 'buyer')
        for key in ('departments', 'unassigned_users', 'mapped_users', 'mapped_departments'):
            assert isinstance(tree[key], list)
        get('/api/saas-subscriptions/' + fixture['subscriptionId'] + '/sync-tree', 'provider', 403)
        get('/api/saas-subscriptions/' + fixture['subscriptionId'] + '/sync-tree', 'admin', 403)
        readiness = get('/api/products/' + fixture['productId'] + '/integration-readiness', 'provider')
        assert isinstance(readiness['visible'], bool) and isinstance(readiness['readiness'], list)
        get('/api/admin/product-categories', 'admin')
        get('/api/admin/product-categories', 'provider', 403)
        own = get('/api/products', 'provider', params={'enterprise_id': fixture['providerEnterpriseId']})
        assert all(row['enterprise_id'] == fixture['providerEnterpriseId'] for row in own['items'])
        assert fixture['productId'] in {row['id'] for row in own['items']}
        get('/api/products', 'provider', 403, {'enterprise_id': fixture['buyerEnterpriseId']})
        get('/api/enterprise-settlements', 'provider', params={'enterprise_id': fixture['providerEnterpriseId']})
    args.output.write_text(json.dumps({'status': 'passed', 'checks': checks,
        'scope': 'deployed HTTP readback; no resource mutations or synchronization performed',
        'saas_tree_counts': {key: len(tree[key]) for key in
            ('departments', 'unassigned_users', 'mapped_users', 'mapped_departments')},
        'saas_visible': readiness['visible']}, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.browser_fixture:
        fd = os.open(args.browser_fixture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(fixture, stream)
    print(json.dumps({'status': 'passed', 'checks': len(checks)}))


if __name__ == '__main__':
    main()
