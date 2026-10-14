"""Private, owned API/SaaS purchase bridge; runtime execution belongs to parent."""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import stat
from urllib.parse import urlsplit

PROTECTED_PRODUCT = 'qa-vcq-a044ea9ea181-p2'
LIMITATION = 'Synthetic pending_integration approval precondition; not four-review publication acceptance'


def check(value, message):
    if not value:
        raise ValueError(message)


def origin(value):
    uri = urlsplit(value)
    check(uri.scheme in ('http', 'https') and uri.hostname and not uri.username and
          not uri.password and uri.path in ('', '/') and not uri.query and not uri.fragment,
          'HTTP(S) origin required')
    return value.rstrip('/')


def directory(path):
    path = Path(path)
    mode = path.lstat().st_mode
    check(stat.S_ISDIR(mode) and stat.S_IMODE(mode) == 0o700,
          'Real private 0700 directory required')
    return path


def helpers(kind):
    if kind == 'saas':
        import version_change_saas_runtime_acceptance as g
    else:
        import api_auto_delivery_runtime_acceptance as g
    return g


def protected(m):
    import version_change_saas_runtime_acceptance as saas
    with m.SessionLocal() as db:
        product = db.get(m.Product, PROTECTED_PRODUCT)
        check(product is not None, 'Protected product missing')
        data = {c.name: getattr(product, c.name) for c in product.__table__.columns}
        return {'order': saas.fingerprint(db, m), 'product': hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()}


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        mode = os.fstat(stream.fileno()).st_mode
        check(stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600, 'Private regular 0600 file required')
        return json.load(stream)


def owned_order(order, fixture):
    check(order.product_id == fixture['product_id'] and
          order.product_version_id == fixture['product_version_id'] and
          order.buyer_enterprise_id == fixture['enterprise_id'] and
          order.buyer_user_id in {a['user_id'] for a in fixture['actors'].values()} and
          order.id != 'ORD-261012005144E1B3' and order.order_no != 'ORD-261012005144E1B3' and
          order.product_id != PROTECTED_PRODUCT, 'Unowned or protected browser order')


def configure_saas(g, public_base):
    check(public_base, 'Explicit --mock-public-base-url required for SaaS')
    public_base = origin(public_base)
    g.OIDC_REDIRECT = public_base + '/sso/callback'
    os.environ['MOCK_SAAS_PUBLIC_BASE_URL'] = public_base
    return public_base


def prepare(path, kind, base_url, mock_public_base=None):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    path = directory(path)
    check(not list(path.iterdir()), 'Fresh empty fixture directory required')
    base_url = origin(base_url)
    g = helpers(kind)
    if kind == 'saas':
        mock_public_base = configure_saas(g, mock_public_base)
    m = g.load_main()
    ids = g.fixture_ids()
    if kind != 'saas':
        ids.update(consumers=[], quota_windows=[], database_ids={}, environment=g.environment(m),
                   cleaned=False, authorized=False)
    baseline = protected(m)
    g.preflight(m, ids)
    ids['authorized'] = True
    manifest = path / 'ownership.json'
    g.write_manifest(manifest, ids, create=True)
    meta = {'kind': kind, 'run_id': ids['run_id'], 'protected': baseline, 'mock_public_base': mock_public_base,
            'orders': [], 'limitation': LIMITATION}
    g.write_manifest(path / 'bridge.json', meta, create=True)
    if kind == 'saas':
        ids['_manifest_writer'] = lambda value: g.write_manifest(manifest, value)
        tokens, secret = g.prepare_fixture(m, ids)
        g.path_write(ids)
        with m.SessionLocal() as db:
            db.get(m.Product, ids['product']).application_url = mock_public_base + '/sso/apps/' + ids['external']['oidc_client_id']
            # Only the source is sellable in this purchase fixture; preserve helper roots for cleanup.
            for side in ('upgrade', 'downgrade'):
                version_row = db.get(m.SaaSProductVersion, ids['versions'][side])
                check(version_row is not None and version_row.product_id == ids['product'], 'Unowned SaaS version')
                version_row.status = 'inactive'
            db.commit()
        g.register_mock_client(ids, secret)
        product, version, enterprise = ids['product'], ids['versions']['source'], ids['enterprises']['buyer']
        admin, super_user, finance = ids['users']['delivery'], ids['users']['buyer'], ids['users']['finance']
    else:
        tokens = g.prepare(m, ids)
        index = 1 if kind == 'model-serving' else 0
        product, version, enterprise = ids['products'][index], ids['versions'][index], ids['enterprises'][0]
        admin, super_user, finance = ids['users']['ops'], ids['users']['buyer'], None
    client = TestClient(m.app)
    try:
        if kind == 'saas':
            verified = g.request(client, tokens['provider'], 'POST', '/products/' + product + '/saas-integration/verify')
            check(verified.get('verification_status') == 'verified', 'SaaS integration verification failed')
        else:
            route_key = ids['routes'][index]
            route_path = '/api/products/' + product + '/versions/' + version + '/integration'
            def request(method, suffix='', body=None):
                response = client.request(method, route_path + suffix,
                    headers={'Authorization': 'Bearer ' + tokens['ops']}, json=body)
                check(response.status_code == 200, 'Fixture integration HTTP failed')
                return response.json()
            request('PUT', body={'upstream_url': g.MOCK_URL, 'route_key': route_key, 'version': 'v1',
                'upstream_auth_mode': 'oauth2', 'health_path': '/health',
                'rate_limit_per_minute': 60, 'daily_quota': 100, 'monthly_quota': 1000})
            with m.SessionLocal() as db:
                route = db.scalar(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.route_key == route_key))
                oauth = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == product))
                check(route is not None and oauth is not None, 'Owned route/client missing')
                route.upstream_client_id, route.upstream_client_secret = oauth.client_id, oauth.client_secret
                db.commit()
            check(request('POST', '/verify').get('status') == 'verified', 'API verification failed')
    finally:
        client.close()
    actors = {}
    with m.SessionLocal() as db:
        db.add(m.Membership(user_id=admin, enterprise_id=enterprise, role='enterprise_admin', status='active'))
        for role, uid in [('super', super_user), ('admin', admin)] + ([('finance', finance)] if finance else []):
            user = db.get(m.User, uid)
            if role == 'admin':
                user.platform_role = ''
            password = secrets.token_urlsafe(30)
            user.password_hash = m.hash_password(password)
            user.email_verified = True
            actors[role] = {'user_id': uid, 'email': user.email, 'password': password, 'token': m.issue_token(user)}
        db.commit()
        published = db.get(m.Product, product)
        check(published.status == 'published' and
              (published.product_type, published.delivery_method) ==
              (('saas', 'tenant_access') if kind == 'saas' else ('api', 'api')),
              'Current canonical product was not published by integration verification')
    fixture = {'run_id': ids['run_id'], 'kind': kind, 'base_url': base_url,
        'product_id': product, 'product_version_id': version, 'enterprise_id': enterprise,
        'actors': actors, 'limitation': LIMITATION}
    check(protected(m) == baseline, 'Protected scope changed during preparation')
    g.write_manifest(path / 'browser.json', fixture, create=True)
    g.write_manifest(manifest, ids)
    return {'status': 'prepared', 'browser_file': str(path / 'browser.json'),
            'ownership_file': str(manifest), 'kind': kind, 'orders_created': 0, 'limitation': LIMITATION}


def reconcile(path, cleanup=False):
    from sqlalchemy import select
    path = directory(path)
    meta = read_private(path / 'bridge.json')
    g = helpers(meta['kind'])
    if meta['kind'] == 'saas':
        configure_saas(g, meta['mock_public_base'])
    ids = g.read_manifest(path / 'ownership.json')
    check(meta['run_id'] == ids['run_id'], 'Bridge ownership mismatch')
    m = g.load_main()
    check(ids['environment'] == (g.environment_fingerprint(m) if meta['kind'] == 'saas' else g.environment(m)),
          'Fixture environment mismatch')
    check(protected(m) == meta['protected'], 'Protected scope changed; refusing cleanup')
    # Preparation failures may not yet have a browser file. Recovery still uses validated roots.
    if (path / 'browser.json').exists():
        fixture = read_private(path / 'browser.json')
        expected_product = ids['product'] if meta['kind'] == 'saas' else ids['products'][1 if meta['kind'] == 'model-serving' else 0]
        expected_version = ids['versions']['source'] if meta['kind'] == 'saas' else ids['versions'][1 if meta['kind'] == 'model-serving' else 0]
        expected_enterprise = ids['enterprises']['buyer'] if meta['kind'] == 'saas' else ids['enterprises'][0]
        check(fixture['run_id'] == ids['run_id'] and fixture['product_id'] == expected_product and
              fixture['product_version_id'] == expected_version and fixture['enterprise_id'] == expected_enterprise and
              fixture['actors']['super']['user_id'] == ids['users']['buyer'] and
              fixture['actors']['admin']['user_id'] == ids['users']['delivery' if meta['kind'] == 'saas' else 'ops'],
              'Browser fixture roots changed')
        with m.SessionLocal() as db:
            orders = db.scalars(select(m.Order).where(m.Order.product_id == expected_product)).all()
            check(len(orders) <= 1, 'Multiple browser orders; manual ownership review required')
            for order in orders:
                owned_order(order, fixture)
                check(order.buyer_user_id == ids['users']['buyer'], 'Only fixture super-admin may own browser order')
            meta['orders'] = [o.id for o in orders]
            if meta['kind'] == 'saas' and orders:
                sub = db.get(m.SaaSSubscription, orders[0].subscription_id)
                check(sub is not None and sub.product_id == expected_product and
                      sub.enterprise_id == fixture['enterprise_id'], 'Unowned SaaS subscription')
                ids['database_ids']['orders'] = meta['orders']
                ids['database_ids']['subscription_id'] = sub.id
                ids['external']['tenant'] = 'tenant-' + sub.id[:12]
            elif meta['kind'] != 'saas':
                routes = db.scalars(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.product_id.in_(ids['products']))).all()
                for credential in db.scalars(select(m.ApiCredential).where(m.ApiCredential.route_id.in_([r.id for r in routes]))):
                    route = next(r for r in routes if r.id == credential.route_id)
                    check(credential.enterprise_id == ids['enterprises'][0] and route.route_key in ids['routes'] and
                          credential.apisix_consumer_name == 'market-consumer-' + credential.id,
                          'Unowned worker consumer')
                    item = {'id': credential.id, 'name': credential.apisix_consumer_name, 'route': route.route_key}
                    if item not in ids['consumers']:
                        ids['consumers'].append(item)
        g.write_manifest(path / 'bridge.json', meta)
        g.write_manifest(path / 'ownership.json', ids)
    if cleanup:
        g.cleanup(m, ids, path / 'ownership.json')
        check(protected(m) == meta['protected'], 'Protected scope changed during cleanup')
        (path / 'browser.json').unlink(missing_ok=True)
    return {'status': 'cleaned' if cleanup else 'journaled', 'order_count': len(meta['orders']),
            'protected_scope_unchanged': True, 'limitation': LIMITATION}


def inspect_messages(path):
    """Read-only reproduction of API helper candidate scope; no cleanup authorization."""
    from sqlalchemy import or_, select, text
    path = directory(path)
    meta = read_private(path / 'bridge.json')
    check(meta['kind'] in ('api', 'model-serving'), 'API message diagnostic only')
    g = helpers(meta['kind'])
    ids = g.read_manifest(path / 'ownership.json')
    check(ids['run_id'] == meta['run_id'], 'Bridge ownership mismatch')
    m = g.load_main()
    check(ids['environment'] == g.environment(m), 'Diagnostic environment mismatch')
    check(protected(m) == meta['protected'], 'Protected scope changed')
    tables = m.Base.metadata.tables
    users = list(ids['users'].values())
    roots = {m.User.__tablename__: users, m.Enterprise.__tablename__: ids['enterprises'],
             m.Product.__tablename__: ids['products'], m.ProductReleaseVersion.__tablename__: ids['versions']}
    predicates = {name: tables[name].c.id.in_(values) for name, values in roots.items()}
    Message, Receipt, Delivery = (m.message_center[k] for k in ('Message', 'Receipt', 'Delivery'))
    with m.SessionLocal() as db:
        db.execute(text('SET TRANSACTION READ ONLY'))
        for _ in range(40):
            keys = {column: set(db.scalars(select(column).where(predicate)))
                    for name, predicate in predicates.items() for column in tables[name].primary_key.columns}
            check(sum(map(len, keys.values())) <= 10000, 'Diagnostic graph exceeds bound')
            expanded = dict(predicates)
            for table in tables.values():
                children = [fk.parent.in_(keys[fk.column]) for fk in table.foreign_keys if keys.get(fk.column)]
                if children:
                    expanded[table.name] = or_(*children, *([predicates[table.name]] if table.name in predicates else []))
            targets = [v for values in keys.values() for v in values if isinstance(v, str)]
            for model in (m.AuditLog, Message, m.PlatformNotification):
                extra = [model.__table__.c.target_id.in_(targets)]
                if model is m.AuditLog:
                    extra += [model.actor.in_([u + '@example.invalid' for u in users]), model.request_id == ids['request_id']]
                expanded[model.__tablename__] = or_(*extra, *([expanded[model.__tablename__]] if model.__tablename__ in expanded else []))
            expired = m.message_center.get('ExpiredUrgent')
            if expired is not None:
                expanded[expired.__tablename__] = or_(expired.message_id.in_(keys.get(Message.__table__.c.id, set())), expired.user_id.in_(users))
            new_keys = {column: set(db.scalars(select(column).where(predicate)))
                        for name, predicate in expanded.items() for column in tables[name].primary_key.columns}
            predicates = expanded
            if keys == new_keys:
                break
        else:
            raise ValueError('Diagnostic graph did not converge')
        order_ids = new_keys.get(m.Order.__table__.c.id, set())
        task_ids = new_keys.get(m.DeliveryTask.__table__.c.id, set())
        candidates = predicates.get(Message.__tablename__, Message.id.in_([]))
        rows = db.execute(select(Message.id, Message.target_type, Message.target_id,
            Message.category, Message.tenant_id, Message.event_key).where(candidates).order_by(Message.id).limit(201)).all()
        check(len(rows) <= 200, 'Diagnostic message count exceeds bound')
        report = []
        for row in rows:
            tenant = 'buyer' if row.tenant_id == ids['enterprises'][0] else 'seller' if row.tenant_id == ids['enterprises'][1] else 'empty' if not row.tenant_id else 'outside-fixture'
            target_owned = (row.target_type == 'order' and row.target_id in order_ids) or (row.target_type == 'delivery_task' and row.target_id in task_ids)
            receipts = list(db.scalars(select(Receipt.user_id).where(Receipt.message_id == row.id)))
            deliveries = list(db.scalars(select(Delivery.user_id).where(Delivery.message_id == row.id)))
            prefix = 'business:' + row.target_type + ':' + row.target_id + ':'
            action = row.event_key[len(prefix):].split(':', 1)[0] if row.event_key.startswith(prefix) else ''
            action = action if action in {'start_payment', 'confirm_payment', 'delivery_succeeded', 'delivery_failed'} else 'other'
            report.append({'message_id': row.id, 'target_type': row.target_type, 'category': row.category,
                'tenant_scope': tenant, 'target_owned_order_or_task': target_owned,
                'helper_business_ownership_passes': target_owned and tenant == 'buyer' and row.category == 'order',
                'event_kind': 'business' if row.event_key.startswith(prefix) else 'subscription_term_granted' if row.event_key.startswith('subscription_term_granted:') else 'other',
                'event_action': action, 'owned_receipts': sum(u in users for u in receipts),
                'outside_receipts': sum(u not in users for u in receipts),
                'owned_deliveries': sum(u in users for u in deliveries),
                'outside_deliveries': sum(u not in users for u in deliveries)})
    return {'status': 'inspected', 'read_only': True, 'candidate_count': len(report),
            'messages': report, 'protected_scope_unchanged': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'journal', 'cleanup', 'inspect-messages'])
    parser.add_argument('--fixture-dir', required=True)
    parser.add_argument('--kind', choices=['api', 'model-serving', 'saas'], default='api')
    parser.add_argument('--base-url')
    parser.add_argument('--mock-public-base-url')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if args.mode == 'prepare':
        check(args.base_url, '--base-url required')
        result = prepare(args.fixture_dir, args.kind, args.base_url, args.mock_public_base_url)
    elif args.mode == 'inspect-messages':
        result = inspect_messages(args.fixture_dir)
    else:
        result = reconcile(args.fixture_dir, args.mode == 'cleanup')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__,
                          'recovery': 'Retain private journals; parent performs exact cleanup'}))
        raise SystemExit(1)
