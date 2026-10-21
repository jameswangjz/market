"""Audited repair of three approved mock entries, with business fingerprints."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
PRODUCTS = {
    'e101107c3f6414ab48b9ebc43de368e8': 'SaaS-APP1',
    'e0d7e27906f32c69ee11fa8a6093dadf': 'SaaS_APP3',
    '67a69ce54e10fca7f23878ed3524129b': 'SaaS_APP4',
}
ORDERS = ('ORD-261019032206CB07', 'ORD-261014032800AA14', 'ORD-261012005144E1B3')
ORIGIN = 'http://192.168.10.10:30083'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def fingerprints(m, db):
    from sqlalchemy import select
    hashes, entries = {}, {}
    for pid, name in PRODUCTS.items():
        product = db.get(m.Product, pid)
        assert product and product.name == name and product.product_type == 'saas'
        entries[pid] = product.application_url
        machine = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == pid))
        oidc = db.scalar(select(m.OIDCClient).where(m.OIDCClient.product_id == pid))
        config = db.scalar(select(m.SaaSIntegrationConfig).where(m.SaaSIntegrationConfig.product_id == pid))
        assert machine and oidc and config
        hashes['credentials:' + pid] = digest([machine.client_id, machine.client_secret,
            machine.scope, machine.status, oidc.client_id, oidc.secret_hash,
            oidc.secret_encrypted, oidc.status, config.client_id, config.client_secret])
        rows = db.scalars(select(m.SaaSSubscription).where(m.SaaSSubscription.product_id == pid)
                          .order_by(m.SaaSSubscription.id)).all()
        hashes['subscriptions:' + pid] = digest([
            {c.name: str(getattr(row, c.name)) for c in row.__table__.columns} for row in rows])
    for number in ORDERS:
        order = db.scalar(select(m.Order).where(m.Order.order_no == number))
        assert order
        models = [m.Order, m.Payment, m.Refund, m.Settlement, m.OrderStateLog]
        for model in models:
            rows = ([order] if model == m.Order else db.scalars(select(model)
                .where(model.order_id == order.id).order_by(model.id)).all())
            hashes[number + ':' + model.__tablename__] = digest([
                {c.name: str(getattr(row, c.name)) for c in row.__table__.columns} for row in rows])
    return {'business': hashes, 'entries': entries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['before', 'normalize', 'verify'], required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from sqlalchemy import select
    from app import main as m
    with m.SessionLocal() as db:
        current = fingerprints(m, db)
        baseline = json.loads(args.baseline.read_text())['snapshot'] if args.baseline else None
        if args.mode != 'before':
            assert baseline and baseline['business'] == current['business']
        changes = []
        if args.mode == 'normalize':
            assert baseline['entries'] == current['entries'], 'Application URLs changed since inventory'
            actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local',
                m.User.platform_role == 'super_admin', m.User.is_active.is_(True)))
            assert actor
            for pid, name in PRODUCTS.items():
                p = db.scalar(select(m.Product).where(m.Product.id == pid).with_for_update())
                client = db.scalar(select(m.OIDCClient).where(m.OIDCClient.product_id == pid))
                old = urlsplit(p.application_url)
                assert urlunsplit((old.scheme, old.netloc, '', '', '')) == ORIGIN
                assert not old.query and not old.fragment
                assert old.path in ('/sso/apps', '/sso/apps/',
                    '/sso/apps/' + client.client_id, '/apps/' + pid)
                target = ORIGIN + '/apps/' + pid
                if p.application_url != target:
                    before = p.application_url
                    p.application_url = target
                    m.audit(db, actor.email, 'normalize_mock_saas_entry', 'product', pid,
                        '按已确认需求修正模拟SaaS稳定产品入口；不改订单、订阅或凭据',
                        category='product', tenant_id=p.enterprise_id,
                        before={'application_url': before}, after={'application_url': target})
                    changes.append({'product': name, 'before': before, 'after': target})
            db.commit()
            current = fingerprints(m, db)
            assert baseline['business'] == current['business']
        elif args.mode == 'verify':
            for pid in PRODUCTS:
                p = db.get(m.Product, pid)
                assert p.application_url == ORIGIN + '/apps/' + pid
                from app.saas_entry import probe_saas_entry
                from types import SimpleNamespace
                assert probe_saas_entry(vars(m), db, p) == 204
                try:
                    probe_saas_entry(vars(m), db, SimpleNamespace(id=pid,
                        application_url=ORIGIN + '/missing-saas-entry-for-verification'))
                except m.HTTPException as error:
                    assert error.status_code == 502 and 'HTTP 404' in error.detail
                else:
                    raise AssertionError('Missing entry was incorrectly accepted')
                import httpx
                response = httpx.head(p.application_url, timeout=10, follow_redirects=False,
                                     headers={'X-Market-Product-Id': pid})
                assert response.status_code == 204
                page = httpx.get(p.application_url, timeout=10, follow_redirects=False)
                assert page.status_code == 200 and 'oidc_' not in page.text
                changes.append({'product': p.name, 'stable_head_status': response.status_code,
                                'public_landing_status': page.status_code,
                                'actual_safe_probe': 'passed', 'missing_entry_rejected': True})
    result = {'status': 'passed', 'mode': args.mode, 'snapshot': current, 'checks': changes}
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    print(json.dumps({'status': 'passed', 'mode': args.mode, 'products': len(changes)}))


if __name__ == '__main__':
    main()
