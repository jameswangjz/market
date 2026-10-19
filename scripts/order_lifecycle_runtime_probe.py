"""Installed HTTP permission checks using an already-paid, nonprotected order.

Never confirm an unpaid payment or create a business fixture. Successful platform
requests must be read-only idempotent paid confirmations, verified by fingerprints.
"""
import hashlib
import json
import os
import sys


def run():
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    sys.path.insert(0, '/app')
    import httpx
    from sqlalchemy import select, inspect
    from app import main as m

    def fingerprints(db):
        result = {}
        for model in (m.Order, m.Payment, m.Refund):
            digest = hashlib.sha256()
            for row in db.execute(select(*model.__table__.columns).order_by(model.id)):
                digest.update(json.dumps(list(row), default=str, separators=(',', ':')).encode())
                digest.update(b'\n')
            result[model.__tablename__] = digest.hexdigest()
        return result

    with m.SessionLocal() as db:
        columns = {column['name'] for column in inspect(m.engine).get_columns('orders')}
        assert {'order_type', 'service_starts_at', 'service_ends_at', 'service_effective_at'} <= columns
        assert inspect(m.engine).has_table('order_service_states')
        assert inspect(m.engine).has_table('refund_order_links')
        before = fingerprints(db)
        rows = db.execute(select(m.Order, m.User).join(m.User, m.User.id == m.Order.buyer_user_id)
            .join(m.Membership, (m.Membership.user_id == m.User.id)
                  & (m.Membership.enterprise_id == m.Order.buyer_enterprise_id))
            .join(m.Payment, m.Payment.order_id == m.Order.id).where(
                m.Order.order_no != 'ORD-261012005144E1B3', m.Order.payment_status == 'paid',
                m.Order.refunded_amount == 0, m.Order.amount > 0, m.Payment.status == 'paid',
                m.Order.business_type != 'version_change_payment',
                m.Membership.role == 'super_admin', m.Membership.status == 'active',
                m.User.is_active.is_(True), m.User.activation_status == 'active',
                m.User.verified_status == 'verified', m.User.platform_role == '')
            .order_by(m.Order.created_at.desc())).all()
        assert rows, 'No eligible already-paid buyer order; no mutation attempted'
        eligible = []
        Request = m.version_change_saga['VersionChangeRequest']
        for candidate, buyer in rows:
            pending = db.scalar(select(Request.id).where(Request.enterprise_id == candidate.buyer_enterprise_id,
                Request.combination_id.in_(select(m.SubscriptionCombination.id).where(
                    m.SubscriptionCombination.product_id == candidate.product_id)),
                Request.status.in_(m.version_change_saga['OPEN_STATUSES'])))
            if not pending:
                eligible.append((candidate, buyer))
        assert eligible, 'No stable paid order; no mutation attempted'
        order, buyer = eligible[0]
        tokens = {'buyer': m.issue_token(buyer)}
        for name, role in (('finance', 'finance_settlement'), ('platform', 'super_admin'), ('operations', 'platform_operator')):
            user = db.scalar(select(m.User).where(m.User.platform_role == role, m.User.is_active.is_(True),
                m.User.activation_status == 'active', m.User.verified_status == 'verified'))
            assert user is not None, 'Required active platform role unavailable'
            tokens[name] = m.issue_token(user)
        order_id = order.id
    statuses = {}
    with httpx.Client(base_url='http://127.0.0.1:8000', trust_env=False, timeout=10) as client:
        for name, expected in (('buyer', 403), ('finance', 403), ('platform', 200), ('operations', 200)):
            response = client.post(f'/api/orders/{order_id}/transition',
                headers={'Authorization': 'Bearer ' + tokens[name]}, json={'action': 'confirm_payment'})
            statuses[name] = response.status_code
            assert response.status_code == expected, 'Unexpected payment permission response: ' + name
        response = client.get(f'/api/orders/{order_id}', headers={'Authorization': 'Bearer ' + tokens['platform']})
        assert response.status_code == 200
        assert {'order_type', 'service_starts_at', 'service_ends_at', 'service_activation_status'} <= response.json()['order'].keys()
    with m.SessionLocal() as db:
        assert fingerprints(db) == before, 'Business data changed; inspect concurrent activity before concluding'
    return {'status': 'passed', 'transport': 'installed HTTP', 'payment_permissions': statuses,
            'schema': 'verified', 'business_fingerprints': 'unchanged',
            'new_order_or_payment_created': False, 'protected_order_mutated': False}


if __name__ == '__main__':
    try:
        print(json.dumps(run(), sort_keys=True))
    except Exception:
        print(json.dumps({'status': 'failed', 'detail': 'Inspect runtime role, state and concurrent activity; no unpaid confirmation is attempted'}))
        raise SystemExit(1)
