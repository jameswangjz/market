"""Bounded real-runtime acceptance for API product version migration.

This script exercises API/model-API gateway publication, an HTTP purchase and
delivery, upgrade funding, the durable version-change worker, APISIX calls and
usage continuity, including downgrade and linked simulated refunds. It does not
claim browser, restart/fault, monthly-ledger, or future-renewal acceptance. Run
only against an explicitly selected deployed PG and APISIX environment.

The 0600 manifest is created before any route or consumer is published. Cleanup
uses only exact qa-vcs roots and recorded external identifiers.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time


class AcceptanceFailure(RuntimeError):
    pass


STAGE = 'startup'


def oauth_cache_keys(ids):
    return {'market:apisix:oauth:' + value for value in ids['products'].values()}


def redis_key_journal(manifest):
    ids = fixture_ids(manifest['run_id'])
    keys = oauth_cache_keys(ids)
    keys.update('market:apisix:credential:' + item['name'] for item in manifest['consumers'])
    for window in manifest['quota_windows']:
        scope = window['scope'].replace('%', '%25').replace(':', '%3A')
        period = window['period'].replace('%', '%25').replace(':', '%3A')
        keys.update(f'market:apisix:quota:subscription:minute:{scope}:{minute}'
                    for minute in (window['minute_start'], window['minute_start'] + 1))
        keys.add(f"market:apisix:quota:subscription:day:{scope}:{window['day']}")
        keys.add(f'market:apisix:quota:subscription:month:{scope}:{period}')
    return keys


def check(condition, message):
    if not condition:
        raise AcceptanceFailure(message)


def safe_identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value) else None


def safe_http_detail(response):
    try:
        detail = response.json().get('detail')
    except (ValueError, AttributeError, TypeError):
        return ''
    if not isinstance(detail, str):
        return ''
    if len(detail) > 240 or re.search(r'(?i)\b(mk_[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)', detail):
        return '[redacted]'
    detail = re.sub(r'(?i)(api[_ -]?key|client[_ -]?secret|authorization|bearer|password|token)\s*[:=]\s*[^\s,;]+',
                    r'\1=[redacted]', detail)
    return detail


def safe_exception_metadata(exc):
    metadata = {'stage': STAGE, 'error_type': type(exc).__name__}
    if type(exc).__name__ == 'IntegrityError':
        original = getattr(exc, 'orig', None)
        diagnostic = getattr(original, 'diag', None)
        state = getattr(original, 'sqlstate', None) or getattr(original, 'pgcode', None)
        if isinstance(state, str) and re.fullmatch(r'[0-9A-Z]{5}', state):
            metadata['sqlstate'] = state
        for field in ('constraint_name', 'table_name'):
            value = safe_identifier(getattr(diagnostic, field, None)) if diagnostic else None
            if value:
                metadata[field] = value
    return metadata


def fixture_ids(run_id=None):
    run_id = run_id or 'qa-vcs-' + secrets.token_hex(6)
    ids = {
        'run_id': run_id,
        'users': {role: f'{run_id}-u-{role}' for role in ('buyer', 'provider', 'finance', 'delivery')},
        'enterprises': {role: f'{run_id}-e-{role}' for role in ('buyer', 'provider')},
        'products': {kind: f'{run_id}-p-{kind}' for kind in ('api', 'model')},
        'versions': {kind: {side: f'{run_id}-v-{kind}-{side}' for side in ('source', 'target')}
                     for kind in ('api', 'model')},
        'routes': {kind: {side: f'{run_id}-{kind}-{side}' for side in ('source', 'target')}
                   for kind in ('api', 'model')},
        'external': {'route_keys': [], 'consumer_names': [], 'redis_keys': []},
        'database_ids': {},
        'consumers': [], 'quota_windows': [], 'environment': '', 'authorized': False,
        'request_id': run_id + '-http', 'protected_order_fingerprint': '',
    }
    ids['external']['redis_keys'] = sorted(oauth_cache_keys(ids))
    return ids


def protected_order_fingerprint(db, m, order):
    from sqlalchemy import select
    product = db.get(m.Product, order.product_id)
    check(product is not None, 'Protected order product is missing')
    rows = []
    for model, column in ((m.Payment, m.Payment.order_id),
                          (m.Refund, m.Refund.order_id)):
        if hasattr(m, 'Refund') or model is m.Payment:
            rows.append((model.__tablename__, [
                {c.name: getattr(row, c.name) for c in model.__table__.columns}
                for row in db.scalars(select(model).where(column == order.id)).all()]))
    payload = {
        'order': {c.name: getattr(order, c.name) for c in m.Order.__table__.columns},
        'product': {c.name: getattr(product, c.name) for c in m.Product.__table__.columns},
        'financial_rows': rows,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def write_manifest(path, value, *, create=False):
    value['external']['redis_keys'] = sorted(redis_key_journal(value))
    path = Path(path)
    temporary = None
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    else:
        st = path.lstat()
        check(stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600,
              'Manifest must be a regular 0600 file')
        fd, temporary = tempfile.mkstemp(prefix='.vcs-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if temporary:
            os.replace(temporary, path)
            temporary = None
    finally:
        if temporary:
            os.unlink(temporary)


def read_manifest(path):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        check(stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600,
              'Manifest must be a regular 0600 file')
        with os.fdopen(fd, 'r') as stream:
            fd = -1
            value = json.load(stream)
    finally:
        if fd >= 0:
            os.close(fd)
    run_id = value.get('run_id', '')
    check(re.fullmatch(r'qa-vcs-[0-9a-f]{12}', run_id) is not None,
          'Invalid ownership marker')
    expected = fixture_ids(run_id)
    for key in ('users', 'enterprises', 'products', 'versions', 'routes'):
        check(value.get(key) == expected[key], 'Manifest ownership roots changed; refusing operation')
    check(set(value) == set(expected), 'Unexpected manifest field; refusing operation')
    ext = value['external']
    check(set(ext) == {'route_keys', 'consumer_names', 'redis_keys'}, 'Invalid external ownership list')
    expected_route_keys = {key for group in value['routes'].values() for key in group.values()}
    check(set(ext['route_keys']) <= expected_route_keys and
          set(ext['consumer_names']) == {item.get('name') for item in value['consumers']},
          'External identity journal does not match exact fixture roots')
    for item in value['consumers']:
        check(set(item) == {'id', 'name', 'route', 'allowed_routes'} and
              re.fullmatch(r'[0-9a-f]{32}', item['id']) and
              item['name'] == 'market-consumer-' + item['id'] and
              item['route'] in item['allowed_routes'] and
              set(item['allowed_routes']) <= {key for group in value['routes'].values()
                                                for key in group.values()},
              'Invalid consumer ownership journal')
    check(set(ext['redis_keys']) == redis_key_journal(value),
          'Redis journal differs from exact fixture-derived keys')
    return value


def load_main():
    os.environ.setdefault('MESSAGE_CENTER_DISABLE_BACKGROUND', 'true')
    from app import main as m
    check(m.engine.dialect.name == 'postgresql', 'PostgreSQL is required')
    check(not m.app.dependency_overrides, 'Dependency overrides are forbidden')
    check(m.apisix_enabled() and m.apisix_native_auth() and m.apisix_native_upstream(),
          'Native APISIX authentication and OAuth upstream are required')
    check(hasattr(m, 'version_change_saga') and hasattr(m, 'version_change_resources'),
          'Saga and resource adapters must be installed')
    return m


def environment_fingerprint(m):
    values = [m.engine.url.render_as_string(hide_password=False),
              os.getenv('REDIS_URL', 'redis://market-redis:6379/0'),
              os.getenv('APISIX_ADMIN_URL', 'http://market-apisix-admin:9180/apisix/admin')]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def preflight(m, manifest):
    from sqlalchemy import or_, select
    ids = fixture_ids(manifest['run_id'])
    with m.SessionLocal() as db:
        for model, values in ((m.User, list(ids['users'].values())),
                (m.Enterprise, list(ids['enterprises'].values())),
                (m.Product, list(ids['products'].values())),
                (m.ProductReleaseVersion, [v for group in ids['versions'].values()
                                           for v in group.values()])):
            check(db.scalar(select(model.id).where(model.id.in_(values)).limit(1)) is None,
                  'Fixture root collision; no cleanup authorized')
        check(db.scalar(select(m.User.id).where(m.User.email.in_(
            [uid + '@example.invalid' for uid in ids['users'].values()])).limit(1)) is None,
            'Fixture email collision; no cleanup authorized')
        check(db.scalar(select(m.Enterprise.id).where(
            m.Enterprise.credit_code.in_(ids['enterprises'].values())).limit(1)) is None,
            'Fixture credit-code collision; no cleanup authorized')
        check(db.scalar(select(m.OAuthClient.id).where(or_(
            m.OAuthClient.product_id.in_(ids['products'].values()),
            m.OAuthClient.client_id.in_([ids['run_id'] + '-oauth-' + kind for kind in ('api', 'model')]))).limit(1)) is None,
            'Fixture OAuth client collision; no cleanup authorized')
        route_keys = [key for group in ids['routes'].values() for key in group.values()]
        check(db.scalar(select(m.ApiGatewayRoute.id).where(
            m.ApiGatewayRoute.route_key.in_(route_keys)).limit(1)) is None,
            'Database route collision; no cleanup authorized')
        check(m.application_address_policy['allow_http'](db) is True,
              'Existing HTTP policy must allow the mock upstream; no settings changed')
        m.application_address_policy['validate_url']('http://market-mock-api:8300', db=db)
        protected = db.scalar(select(m.Order).where(or_(m.Order.id == 'ORD-261012005144E1B3',
            m.Order.order_no == 'ORD-261012005144E1B3')).limit(1))
        check(protected is not None, 'Protected order preservation preflight failed')
        manifest['protected_order_fingerprint'] = protected_order_fingerprint(db, m, protected)
    redis = m.apisix_policy_redis()
    try:
        check(not any(redis.exists(k) for k in manifest['external']['redis_keys']),
              'Fixture Redis key collision; no cleanup authorized')
        for key in oauth_cache_keys(ids):
            check(not redis.exists(key), 'Fixture OAuth cache key collision; no cleanup authorized')
    finally:
        redis.close()
    for group in ids['routes'].values():
        for route_key in group.values():
            check(apisix_admin(m, 'GET', '/routes/' + route_key, absent=True) is None,
                  'External APISIX route collision; no cleanup authorized')
    response = m.validated_http_request('GET', 'http://market-mock-api:8300/health',
                                         timeout=5, follow_redirects=False)
    check(response.status_code == 200 and response.json() == {
        'status': 'ok', 'service': 'market-mock-api'} and
        bool(response.extensions.get('validated_upstream_addresses')),
        'Safe mock-api health preflight failed')


def apisix_admin(m, method, path, payload=None, absent=False):
    import httpx
    response = httpx.request(method,
        os.getenv('APISIX_ADMIN_URL', 'http://market-apisix-admin:9180/apisix/admin').rstrip('/') + path,
        headers={'X-API-KEY': os.environ['APISIX_ADMIN_KEY']}, json=payload, timeout=15)
    if absent and response.status_code == 404:
        return None
    check(response.status_code in {200, 201, 204}, 'APISIX administration request failed')
    if not response.content:
        return {}
    value = response.json()
    while isinstance(value, dict):
        child = next((value[key] for key in ('node', 'value', 'route')
                      if isinstance(value.get(key), dict)), None)
        if child is None:
            break
        value = child
    return value


def request(client, token, method, path, *, expected=200, body=None):
    response = client.request(method, '/api' + path,
        headers={'Authorization': 'Bearer ' + token}, json=body)
    if response.status_code != expected:
        endpoint = path.split('?', 1)[0]
        detail = safe_http_detail(response)
        suffix = f'; detail={detail}' if detail else ''
        raise AcceptanceFailure(
            f'{method} {endpoint} returned HTTP {response.status_code}; expected {expected}{suffix}')
    return response.json() if response.content else {}


def prepare(m, ids):
    from sqlalchemy import select

    with m.SessionLocal() as db:
        collision = db.scalar(select(m.User.id).where(m.User.id.in_(ids['users'].values())))
        check(collision is None, 'Fixture user collision')
        collision = db.scalar(select(m.Product.id).where(m.Product.id.in_(ids['products'].values())))
        check(collision is None, 'Fixture product collision')
        for role, eid in ids['enterprises'].items():
            db.add(m.Enterprise(id=eid, name=eid, credit_code=eid,
                                verification_status='verified'))
        for role, uid in ids['users'].items():
            db.add(m.User(id=uid, email=uid + '@example.invalid', name=uid,
                password_hash='not-a-login-password', verified_status='verified',
                activation_status='active', is_active=True,
                platform_role={'finance': 'finance_settlement',
                    'delivery': 'delivery_monitor'}.get(role, '')))
        db.flush()
        db.add_all([
            m.Membership(user_id=ids['users']['buyer'], enterprise_id=ids['enterprises']['buyer'],
                         role='super_admin', status='active'),
            m.Membership(user_id=ids['users']['provider'], enterprise_id=ids['enterprises']['provider'],
                         role='super_admin', status='active'),
        ])
        for kind in ('api', 'model'):
            product = m.Product(id=ids['products'][kind], enterprise_id=ids['enterprises']['provider'],
                name=ids['products'][kind], product_type='model' if kind == 'model' else 'api',
                delivery_method='model_api' if kind == 'model' else 'api',
                upstream_url='http://market-mock-api:8300', status='published',
                provider_name=ids['enterprises']['provider'], settlement_rule_mode='custom',
                settlement_rule_json=json.dumps({'platform_rate': 10, 'provider_rate': 90,
                    'service_rate': 0, 'expert_rate': 0, 'channel_rate': 0}))
            db.add(product)
            db.flush()
            db.add(m.OAuthClient(product_id=product.id, client_id=ids['run_id'] + '-oauth-' + kind,
                client_secret=secrets.token_urlsafe(32), scope='resource.invoke', status='active'))
            db.add_all([
                m.ProductReleaseVersion(id=ids['versions'][kind]['source'], product_id=product.id,
                    version_code='source', compatibility_mode='compatible', price=20, cost=5,
                    rate_limit_per_minute=100, daily_quota=10000, monthly_quota=100000),
                m.ProductReleaseVersion(id=ids['versions'][kind]['target'], product_id=product.id,
                    version_code='target', compatibility_mode='compatible', price=30, cost=8,
                    rate_limit_per_minute=100, daily_quota=10000, monthly_quota=100000),
            ])
        db.commit()
        tokens = {role: m.issue_token(db.get(m.User, uid)) for role, uid in ids['users'].items()}
    return tokens


def save_external(path, manifest, kind, side, route_key=None, consumer=None):
    if route_key and route_key not in manifest['external']['route_keys']:
        manifest['external']['route_keys'].append(route_key)
    if consumer and consumer not in manifest['external']['consumer_names']:
        manifest['external']['consumer_names'].append(consumer)
    write_manifest(path, manifest)


def journal_consumer(m, path, manifest):
    from sqlalchemy import event
    def after_flush(db, *_):
        for row in db.new:
            if isinstance(row, m.ApiCredential) and row.enterprise_id in manifest['enterprises'].values():
                route = db.get(m.ApiGatewayRoute, row.route_id)
                check(route is not None and route.route_key in {
                    key for group in manifest['routes'].values() for key in group.values()},
                    'Generated credential is outside fixture routes')
                name = 'market-consumer-' + row.id
                kind = next(key for key, product_id in manifest['products'].items()
                            if product_id == route.product_id)
                allowed_routes = list(manifest['routes'][kind].values())
                item = {'id': row.id, 'name': name, 'route': route.route_key,
                        'allowed_routes': allowed_routes}
                if item not in manifest['consumers']:
                    manifest['consumers'].append(item)
                    manifest['external']['consumer_names'].append(name)
                    write_manifest(path, manifest)
    return after_flush


def journal_counter(m, path, manifest, redis, scope, period, combo_id, kind, *, fresh=False):
    now_epoch = int(redis.time()[0])
    day = datetime.fromtimestamp(now_epoch + 28800, timezone.utc).strftime('%Y-%m-%d')
    token_scope = scope.replace('%', '%25').replace(':', '%3A')
    token_period = period.replace('%', '%25').replace(':', '%3A')
    keys = [f'market:apisix:quota:subscription:minute:{token_scope}:{minute}'
            for minute in (now_epoch // 60, now_epoch // 60 + 1)]
    keys.extend([f'market:apisix:quota:subscription:day:{token_scope}:{day}',
                 f'market:apisix:quota:subscription:month:{token_scope}:{token_period}'])
    if fresh:
        check(not any(redis.exists(key) for key in keys),
              'Fresh subscription quota keys already exist; stopping before API invocation')
    manifest['database_ids'].setdefault('combinations', {})[kind] = {
        'id': combo_id, 'product_id': manifest['products'][kind],
        'enterprise_id': manifest['enterprises']['buyer']}
    window = {'scope': scope, 'period': period, 'minute_start': now_epoch // 60,
              'day': day, 'combination_id': combo_id, 'kind': kind}
    if window not in manifest['quota_windows']:
        manifest['quota_windows'].append(window)
    write_manifest(path, manifest)
    return keys


def exercise(m, ids, path, manifest, client, tokens):
    from sqlalchemy import event
    from sqlalchemy import select
    import httpx

    # Publish one independently versioned route for each source/target, using
    # the actual integration verifier and APISIX admin/runtime boundaries.
    routes = {}
    for kind in ('api', 'model'):
        product_id = ids['products'][kind]
        for side in ('source', 'target'):
            version_id = ids['versions'][kind][side]
            route_key = ids['routes'][kind][side]
            save_external(path, manifest, kind, side, route_key=route_key)
            payload = {'upstream_url': 'http://market-mock-api:8300', 'route_key': route_key,
                'version': side, 'upstream_auth_mode': 'oauth2', 'upstream_scope': 'resource.invoke',
                'rate_limit_per_minute': 100,
                'daily_quota': 10000, 'monthly_quota': 100000, 'health_path': '/health'}
            request(client, tokens['provider'], 'PUT',
                f'/products/{product_id}/versions/{version_id}/integration', body=payload)
            with m.SessionLocal() as db:
                route = db.scalar(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.route_key == route_key))
                oauth = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == product_id))
                check(route is not None and oauth is not None, 'Fixture OAuth or gateway route missing')
                route.upstream_client_id, route.upstream_client_secret = oauth.client_id, oauth.client_secret
                db.commit()
            verified = request(client, tokens['provider'], 'POST',
                f'/products/{product_id}/versions/{version_id}/integration/verify')
            check(verified.get('status') == 'verified', 'Gateway integration verification did not pass')
            with m.SessionLocal() as db:
                route = db.scalar(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.route_key == route_key))
                check(route is not None and route.status == 'active', 'Verified route is not active')
                routes[(kind, side)] = route.id
            remote = apisix_admin(m, 'GET', '/routes/' + route_key)
            check(remote.get('labels', {}).get('market_product_id') == product_id,
                  'APISIX route is not the exact fixture product route')

    orders = {}
    journal = journal_consumer(m, path, manifest)
    held_orders = set()
    def hold_delivery(db, *_):
        for task in db.new.union(db.dirty):
            if isinstance(task, m.DeliveryTask) and task.order_id in held_orders and task.status in {'in_delivery', 'retrying'}:
                task.next_retry_at = m.now() + __import__('datetime').timedelta(hours=1)
    event.listen(m.SessionLocal.class_, 'after_flush', journal)
    event.listen(m.SessionLocal.class_, 'before_flush', hold_delivery)
    redis = m.apisix_policy_redis()
    try:
      for kind in ('api', 'model'):
        oid = request(client, tokens['buyer'], 'POST', '/orders', body={
            'product_id': ids['products'][kind], 'product_version_id': ids['versions'][kind]['source'],
            'buyer_enterprise_id': ids['enterprises']['buyer'], 'subscription_months': 3})['id']
        held_orders.add(oid)
        request(client, tokens['finance'], 'POST', f'/orders/{oid}/transition', body={
            'action': 'confirm_payment', 'reason': 'qa-vcs runtime acceptance'})
        with m.SessionLocal() as db:
            task = db.scalar(select(m.DeliveryTask).where(m.DeliveryTask.order_id == oid))
            check(task is not None, 'Initial API delivery task missing')
            task_id = task.id
        request(client, tokens['delivery'], 'POST', f'/delivery-tasks/{task_id}/process',
                body={'success': True})
        with m.SessionLocal() as db:
            order = db.get(m.Order, oid)
            check(order.delivery_status in {'accepted', 'completed'}, 'Initial delivery did not complete')
            credentials = db.scalars(select(m.ApiCredential).where(
                m.ApiCredential.enterprise_id == ids['enterprises']['buyer'],
                m.ApiCredential.route_id == routes[(kind, 'source')],
                m.ApiCredential.status == 'active')).all()
            check(len(credentials) == 1 and credentials[0].order_id == '',
                  'Initial delivery did not create exactly one shared credential')
            credential_id = credentials[0].id
            manifest['database_ids'].setdefault('orders', {})[kind] = oid
            manifest['database_ids'].setdefault('credentials', {})[kind] = credential_id
            manifest['database_ids'].setdefault('source_routes', {})[kind] = routes[(kind, 'source')]
            db.commit()
        listed = request(client, tokens['buyer'], 'GET', f'/orders/{oid}/api-credentials')
        check(any(item.get('id') == credential_id and item.get('secret_download_available')
                  for item in listed.get('items', [])), 'Initial shared credential is not downloadable')
        downloaded = client.get(f'/api/orders/{oid}/api-credentials/{credential_id}/download',
            headers={'Authorization': 'Bearer ' + tokens['buyer']})
        check(downloaded.status_code == 200, 'Buyer HTTP credential download failed')
        raw_key = downloaded.json().get('api_key')
        check(isinstance(raw_key, str) and raw_key.startswith('mk_'), 'Initial credential download missing')
        consumer = next(item['name'] for item in manifest['consumers'] if item['id'] == credential_id)
        gateway = os.getenv('QA_VCS_GATEWAY_URL', 'http://market-apisix:9080').rstrip('/')
        url = f'{gateway}/gateway/{ids["routes"][kind]["source"]}/v1/echo'
        with m.SessionLocal() as db:
            combination = db.scalar(select(m.subscription_terms['Combination']).where(
                m.subscription_terms['Combination'].enterprise_id == ids['enterprises']['buyer'],
                m.subscription_terms['Combination'].product_id == ids['products'][kind]))
            check(combination is not None, 'Initial subscription combination missing')
            check(combination.enterprise_id == ids['enterprises']['buyer'] and
                  combination.product_id == ids['products'][kind],
                  'Subscription combination is not an exact fixture root')
            scope = combination.id
        metadata = redis.hgetall('market:apisix:credential:' + consumer)
        check(metadata.get('mode') == 'subscription', 'Shared credential lacks subscription quota policy')
        active = [p for p in json.loads(metadata['subscription_periods'])
                  if p['start_at'] <= int(redis.time()[0]) < p['end_at']]
        check(len(active) == 1 and metadata.get('quota_scope') == scope,
              'Expected one active source-version quota period')
        period = active[0]['period_key']
        keys = journal_counter(m, path, manifest, redis, scope, period, scope, kind, fresh=True)
        first = httpx.get(url, headers={'X-API-Key': raw_key}, timeout=15)
        check(first.status_code == 200 and first.json().get('code') == 0 and
              first.json().get('data', {}).get('provider') == 'mock-api',
              'Initial authenticated APISIX call failed')
        month_key = keys[-1]
        check(redis.get(month_key) == '1', 'Redis monthly quota counter did not record source request')
        quote = request(client, tokens['buyer'], 'POST', f'/orders/{oid}/version-change-quote', body={
            'target_version_id': ids['versions'][kind]['target'], 'compatibility_confirmed': True})
        change = request(client, tokens['buyer'], 'POST', f'/orders/{oid}/version-changes', body={
            'quote_token': quote['quote_token'], 'idempotency_key': manifest['run_id'] + '-' + kind})
        change_id = change['id']
        replay = request(client, tokens['buyer'], 'POST', f'/orders/{oid}/version-changes', body={
            'quote_token': quote['quote_token'], 'idempotency_key': manifest['run_id'] + '-' + kind})
        check(replay['id'] == change_id, 'Upgrade request replay did not preserve identity')
        request(client, tokens['buyer'], 'POST', f'/version-changes/{change_id}/start-payment', body={})
        request(client, tokens['buyer'], 'POST', f'/version-changes/{change_id}/confirm-payment', body={})
        manifest['database_ids'].setdefault('changes', {})[kind] = change_id
        write_manifest(path, manifest)

        # Observe the durable deployed worker; do not claim unrelated queue work.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current = request(client, tokens['buyer'], 'GET', f'/version-changes/{change_id}')
            if current['status'] == 'completed':
                break
            if current['status'] in {'exception', 'compensated', 'cancelled'}:
                raise AcceptanceFailure('Version-change worker entered terminal failure state')
            time.sleep(1)
        check(current['status'] == 'completed', 'Version change did not complete before timeout')
        journal_counter(m, path, manifest, redis, scope, period, scope, kind)
        target_url = f'{gateway}/gateway/{ids["routes"][kind]["target"]}/v1/echo'
        after = httpx.get(target_url, headers={'X-API-Key': raw_key}, timeout=15)
        check(after.status_code == 200 and after.json().get('code') == 0 and
              after.json().get('data', {}).get('provider') == 'mock-api',
              'Existing API key failed on target version after migration')
        old_route = httpx.get(url, headers={'X-API-Key': raw_key}, timeout=15)
        check(old_route.status_code in {401, 403}, 'Source route remained authorized after migration')
        check(redis.get(month_key) == '2', 'Redis monthly quota usage did not continue in the same period')
        with m.SessionLocal() as db:
            # Verify subscription points at target while credential and usage
            # history remain attached to the original source identity.
            order = db.get(m.Order, oid)
            combination = db.get(m.subscription_terms['Combination'], scope)
            check(combination is not None and combination.version_id == ids['versions'][kind]['target'],
                  'Subscription did not migrate to target version')
            check(order.id == oid, 'Source order identity changed')

        downgrade_quote = request(client, tokens['buyer'], 'POST', f'/orders/{oid}/version-change-quote', body={
            'target_version_id': ids['versions'][kind]['source'], 'compatibility_confirmed': True})
        downgrade = request(client, tokens['buyer'], 'POST', f'/orders/{oid}/version-changes', body={
            'quote_token': downgrade_quote['quote_token'],
            'idempotency_key': manifest['run_id'] + '-' + kind + '-downgrade'})
        manifest['database_ids'].setdefault('downgrades', {})[kind] = downgrade['id']
        write_manifest(path, manifest)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            downgraded = request(client, tokens['buyer'], 'GET', f'/version-changes/{downgrade["id"]}')
            if downgraded['status'] == 'completed':
                break
            if downgraded['status'] in {'exception', 'compensated', 'cancelled'}:
                raise AcceptanceFailure('Downgrade worker entered terminal failure state')
            time.sleep(1)
        check(downgraded['status'] == 'completed' and downgraded['refund_status'] == 'completed',
              'Downgrade resource and associated refund did not both complete')
        journal_counter(m, path, manifest, redis, scope, period, scope, kind)
        restored = httpx.get(url, headers={'X-API-Key': raw_key}, timeout=15)
        blocked_target = httpx.get(target_url, headers={'X-API-Key': raw_key}, timeout=15)
        check(restored.status_code == 200 and restored.json().get('data', {}).get('provider') == 'mock-api',
              'Original shared key did not reach the downgraded source route')
        check(blocked_target.status_code in {401, 403}, 'Higher-version route remained authorized after downgrade')
        check(redis.get(month_key) == '3', 'Downgrade reset or replaced the existing monthly quota counter')
        with m.SessionLocal() as db:
            combination = db.get(m.subscription_terms['Combination'], scope)
            check(combination.version_id == ids['versions'][kind]['source'],
                  'Downgrade did not update the effective subscription version')
            funds = db.scalars(select(m.VersionChangeFund).where(
                m.VersionChangeFund.change_id == downgrade['id'], m.VersionChangeFund.kind == 'refund')).all()
            check(bool(funds) and all(fund.refund_id and
                  db.get(m.Refund, fund.refund_id).status == 'completed' and
                  db.get(m.Refund, fund.refund_id).payment_id == fund.payment_id and
                  db.get(m.Refund, fund.refund_id).amount == fund.amount for fund in funds),
                  'Downgrade refund source or amount is not exactly linked')
        orders[kind] = {'change_id': change_id, 'status': current['status'],
                        'api_status_before_after': [first.status_code, after.status_code],
                        'old_route_after_migration': old_route.status_code,
                        'redis_month_counter_before_after': ['1', '2', '3'],
                        'downgrade_status': downgraded['status'],
                        'downgrade_refund_status': downgraded['refund_status'],
                        'downgraded_route_status': restored.status_code,
                        'higher_route_after_downgrade': blocked_target.status_code}
    finally:
        event.remove(m.SessionLocal.class_, 'after_flush', journal)
        event.remove(m.SessionLocal.class_, 'before_flush', hold_delivery)
        redis.close()
    return orders


def owned_rows(m, manifest):
    from sqlalchemy import select, tuple_
    ids = fixture_ids(manifest['run_id'])
    import version_change_quote_acceptance as guarded
    scope = {'run_id': ids['run_id'], 'users': ids['users'],
        'enterprises': list(ids['enterprises'].values()),
        'products': {index: ids['products'][kind]
                     for index, kind in enumerate(('api', 'model'))},
        'versions': {index: ids['versions'][kind]
                     for index, kind in enumerate(('api', 'model'))},
        'request_ids': [ids['request_id']]}
    with m.SessionLocal() as db:
        owned = guarded.owned_rows(m, db, scope)
        order_ids = {row[0] for row in owned.get(m.Order.__tablename__, set())}
        for order in db.scalars(select(m.Order).where(m.Order.id.in_(order_ids))):
            check(order.id != 'ORD-261012005144E1B3' and
                  order.order_no != 'ORD-261012005144E1B3',
                  'Protected order reached fixture cleanup; refusing cleanup')
        route_ids = {row[0] for row in owned.get(m.ApiGatewayRoute.__tablename__, set())}
        allowed = {v for group in ids['routes'].values() for v in group.values()}
        for route in db.scalars(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.id.in_(route_ids))):
            check(route.route_key in allowed and route.product_id in ids['products'].values(),
                  'Unowned gateway route reached cleanup')
        credential_ids = {item['id'] for item in manifest['consumers']}
        credentials = db.scalars(select(m.ApiCredential).where(
            m.ApiCredential.route_id.in_(route_ids))).all() if route_ids else []
        check({row.id for row in credentials} == credential_ids,
              'Unjournaled or missing fixture credential; refusing cleanup')
        for credential in credentials:
            route = db.get(m.ApiGatewayRoute, credential.route_id)
            item = next((item for item in manifest['consumers'] if item['id'] == credential.id), None)
            check(credential.enterprise_id == ids['enterprises']['buyer'] and
                  credential.order_id == '' and
                  credential.apisix_consumer_name == 'market-consumer-' + credential.id,
                  'Credential is not an owned initial shared credential')
            check(item is not None and route.route_key in item['allowed_routes'],
                  'Migrated credential route is outside its fixture product')
        order_ids = {row[0] for row in owned.get(m.Order.__tablename__, set())}
        task_ids = {row[0] for row in owned.get(m.DeliveryTask.__tablename__, set())}
        target_ids = {str(value) for rows in owned.values() for row in rows for value in row}
        target_ids |= order_ids | task_ids
        message_model = m.message_center['Message']
        message_ids = {row[0] for row in owned.get(message_model.__tablename__, set())}
        for message in db.scalars(select(message_model).where(message_model.id.in_(message_ids))):
            expected_targets = {'order': order_ids, 'delivery_task': task_ids}
            check(message.target_type in expected_targets and
                  message.target_id in expected_targets[message.target_type] and
                  message.tenant_id in ids['enterprises'].values() and message.category == 'order',
                  'Polymorphic message is not linked to an owned business event')
            if message.event_key.startswith('subscription_term_granted:'):
                term_id = message.event_key.removeprefix('subscription_term_granted:')
                check(message.target_type == 'order' and
                      any(term_id == row[0] for row in owned.get(
                          m.subscription_terms['Term'].__tablename__, set())),
                      'Subscription message lacks an owned term FK')
            else:
                prefix = f'business:{message.target_type}:{message.target_id}:'
                check(message.event_key.startswith(prefix), 'Message event key target mismatch')
        for notification in db.scalars(select(m.PlatformNotification).where(
                m.PlatformNotification.target_id.in_(target_ids))):
            expected_targets = {'order': order_ids, 'delivery_task': task_ids}
            check(notification.target_type in expected_targets and
                  notification.target_id in expected_targets[notification.target_type],
                  'Legacy notification is not linked to an owned order or task')
        for audit in db.scalars(select(m.AuditLog).where(
                m.AuditLog.target_id.in_(target_ids))):
            def owned_ids(model):
                return {row[0] for row in owned.get(model.__tablename__, set())}

            typed_targets = {
                'user': set(ids['users'].values()),
                'product': set(ids['products'].values()),
                'order': order_ids,
                'delivery_task': task_ids,
                'api_gateway_route': owned_ids(m.ApiGatewayRoute),
                'gateway_config_revision': owned_ids(m.GatewayConfigRevision),
                'oauth_client': owned_ids(m.OAuthClient),
                'api_credential': owned_ids(m.ApiCredential),
                'subscription_term': owned_ids(m.subscription_terms['Term']),
                'version_change': {row[0] for row in owned.get(
                    m.version_change_saga['VersionChangeRequest'].__tablename__, set())},
                'payment': {row[0] for row in owned.get(m.Payment.__tablename__, set())},
            }
            check(audit.target_type in typed_targets and
                  audit.target_id in typed_targets[audit.target_type] and
                  (not audit.order_id or audit.order_id in order_ids),
                  'Audit ownership mismatch: action=' + str(audit.action) +
                  ' target_type=' + str(audit.target_type) +
                  ' target_id=' + str(audit.target_id) +
                  ' order_id=' + str(audit.order_id or ''))
        roots = {
            m.User.__tablename__: set(ids['users'].values()),
            m.Enterprise.__tablename__: set(ids['enterprises'].values()),
            m.Product.__tablename__: set(ids['products'].values()),
            m.ProductReleaseVersion.__tablename__: {
                value for group in ids['versions'].values() for value in group.values()},
        }
        recipient_table = m.message_center['Receipt'].__tablename__
        for table in m.Base.metadata.tables.values():
            row_keys = owned.get(table.name, set())
            if not row_keys:
                continue
            for fk in table.foreign_keys:
                parent_name = fk.column.table.name
                allowed_roots = roots.get(parent_name)
                if allowed_roots is None:
                    continue
                values = {row[0] for row in db.execute(select(fk.parent).where(
                    tuple_(*table.primary_key.columns).in_(list(row_keys))))}
                outside = values - allowed_roots
                if not outside:
                    continue
                if parent_name == m.User.__tablename__ and table.name == recipient_table:
                    receipt = m.message_center['Receipt']
                    message_ids_owned = {row[0] for row in owned.get(
                        m.message_center['Message'].__tablename__, set())}
                    receipt_rows = db.scalars(select(receipt).where(
                        tuple_(*receipt.__table__.primary_key.columns).in_(list(row_keys)))).all()
                    check(all(row.message_id in message_ids_owned and
                              (row.user_id in outside or row.user_id in allowed_roots)
                              for row in receipt_rows),
                          'Recipient user FK lacks explicit owned-message receipt proof')
                    continue
                check(False, 'Owned cleanup graph references an unowned fixture root')
        return owned


def expand_fk_ids(db, metadata, owned):
    from sqlalchemy import or_, select, tuple_
    owned = {name: set(rows) for name, rows in owned.items()}
    while True:
        updated = {name: set(rows) for name, rows in owned.items()}
        for table in metadata.tables.values():
            predicates = []
            for fk in table.foreign_keys:
                parent = fk.column.table
                keys = owned.get(parent.name, set())
                if not keys:
                    continue
                pks = list(parent.primary_key.columns)
                index = next((i for i, col in enumerate(pks) if col is fk.column), None)
                if index is None:
                    continue
                vals = [row[index] for row in keys]
                predicates.append(fk.parent.in_(vals))
            if predicates:
                pk = list(table.primary_key.columns)
                rows = {tuple(row) for row in db.execute(select(*pk).where(or_(*predicates)))}
                updated.setdefault(table.name, set()).update(rows)
        if updated == owned:
            return owned
        owned = updated


def cleanup(m, manifest, path):
    from sqlalchemy import delete, func, select
    check(manifest['authorized'] is True and manifest['environment'] == environment_fingerprint(m),
          'Cleanup is not authorized for this environment')
    ids = fixture_ids(manifest['run_id'])
    with m.SessionLocal() as db:
        owned = owned_rows(m, manifest)
        for kind, item in manifest['database_ids'].get('combinations', {}).items():
            combo = db.get(m.subscription_terms['Combination'], item['id'])
            check(kind in ids['products'] and combo is not None and
                  (combo.id,) in owned.get(combo.__tablename__, set()) and
                  combo.enterprise_id == ids['enterprises']['buyer'] and
                  combo.product_id == ids['products'][kind] and
                  item == {'id': combo.id, 'product_id': combo.product_id,
                           'enterprise_id': combo.enterprise_id},
                  'Quota Combination ownership mismatch; refusing cleanup')
        expected_keys = redis_key_journal(manifest)
        check(set(manifest['external']['redis_keys']) == expected_keys,
              'Redis keys are not exactly fixture-derived')
        for window in manifest['quota_windows']:
            item = manifest['database_ids'].get('combinations', {}).get(window['kind'])
            combo = db.get(m.subscription_terms['Combination'], window['combination_id'])
            period_parts = window['period'].split(':')
            check(item is not None and item['id'] == window['combination_id'] == window['scope'] and
                  item['product_id'] == ids['products'][window['kind']] and
                  item['enterprise_id'] == ids['enterprises']['buyer'] and
                  combo is not None and len(period_parts) == 3 and
                  period_parts[0] == combo.id and period_parts[1] == str(combo.generation) and
                  period_parts[2].isdigit() and int(period_parts[2]) < combo.total_months and
                  re.fullmatch(r'\d{4}-\d{2}-\d{2}', window['day']) is not None and
                  type(window['minute_start']) is int and window['minute_start'] >= 0,
                  'Quota counter period is not tied to an exact owned Combination')
        protected = db.scalar(select(m.Order).where(
            (m.Order.id == 'ORD-261012005144E1B3') |
            (m.Order.order_no == 'ORD-261012005144E1B3')))
        check(protected is not None and protected_order_fingerprint(db, m, protected) ==
              manifest['protected_order_fingerprint'],
              'Protected order/product financial snapshot changed before cleanup')
        manifest['database_ids']['cleanup_rows'] = {
            name: sorted(map(list, rows)) for name, rows in owned.items()}
        write_manifest(path, manifest)
    # External deletes are restricted to the exact route keys and consumers
    # journaled before creation. Ownership is fully validated above.
    route_owner = {key: ids['products'][kind] for kind, group in manifest['routes'].items()
                   for key in group.values()}
    for route_key in manifest['external']['route_keys']:
        check(route_key in {v for group in fixture_ids(manifest['run_id'])['routes'].values()
                            for v in group.values()}, 'Unowned APISIX route key; refusing cleanup')
        live = apisix_admin(m, 'GET', '/routes/' + route_key, absent=True)
        if live is not None:
            check(live.get('labels', {}).get('market_product_id') == route_owner[route_key],
                  'Live route ownership mismatch; refusing cleanup')
    for consumer in manifest['external']['consumer_names']:
        item = next((x for x in manifest['consumers'] if x['name'] == consumer), None)
        check(item is not None, 'Consumer is not in the generated-ID journal')
        live = apisix_admin(m, 'GET', '/consumers/' + consumer, absent=True)
        if live is not None:
            check(live.get('username') == consumer and live.get('plugins', {}).get(
                'market-gateway-quota', {}).get('route_key') in item['allowed_routes'],
                'Live consumer ownership mismatch; refusing cleanup')
    for route_key in manifest['external']['route_keys']:
        apisix_admin(m, 'DELETE', '/routes/' + route_key, absent=True)
    for consumer in manifest['external']['consumer_names']:
        item = next(x for x in manifest['consumers'] if x['name'] == consumer)
        if apisix_admin(m, 'GET', '/consumers/' + consumer, absent=True) is not None:
            apisix_admin(m, 'DELETE', '/consumers/' + consumer, absent=True)
    redis = m.apisix_policy_redis()
    try:
        check(set(manifest['external']['redis_keys']) == redis_key_journal(manifest),
              'Redis journal changed before exact-key cleanup')
        for key in manifest['external']['redis_keys']:
            redis.delete(key)
        check(not any(redis.exists(key) for key in manifest['external']['redis_keys']),
              'Journaled Redis keys remain after cleanup')
    finally:
        redis.close()
    with m.SessionLocal() as db:
        for table in reversed(m.Base.metadata.sorted_tables):
            keys = owned.get(table.name)
            if keys:
                pks = list(table.primary_key.columns)
                from sqlalchemy import tuple_
                db.execute(delete(table).where(tuple_(*pks).in_(list(keys))))
        db.commit()
    with m.SessionLocal() as db:
        for name, rows in owned.items():
            table = m.Base.metadata.tables[name]
            pks = list(table.primary_key.columns)
            check(db.scalar(select(func.count()).select_from(table).where(
                __import__('sqlalchemy').tuple_(*pks).in_(list(rows)))) == 0,
                'Owned database rows remain after cleanup')
        protected = db.scalar(select(m.Order).where(
            (m.Order.id == 'ORD-261012005144E1B3') |
            (m.Order.order_no == 'ORD-261012005144E1B3')))
        check(protected is not None and protected_order_fingerprint(db, m, protected) ==
              manifest['protected_order_fingerprint'],
              'Protected order/product financial snapshot changed during cleanup')


def run(manifest_path=None):
    global STAGE
    STAGE = 'preflight'
    m = load_main()
    ids = fixture_ids()
    path = Path(manifest_path or ('/tmp/' + ids['run_id'] + '.json'))
    manifest = ids
    manifest['environment'] = environment_fingerprint(m)
    preflight(m, manifest)
    manifest['authorized'] = True
    # Preflight is complete before the durable private journal is created; all
    # external identities are journaled there before a route or consumer PUT.
    write_manifest(path, manifest, create=True)
    client = None
    try:
        STAGE = 'fixture-setup'
        from fastapi.testclient import TestClient
        tokens = prepare(m, ids)
        client = TestClient(m.app)
        STAGE = 'api-migration'
        result = exercise(m, ids, path, manifest, client, tokens)
        STAGE = 'cleanup'
        cleanup(m, manifest, path)
        return {'status': 'passed', 'scope': 'API/model-API migration runtime only',
                'migrations': result,
                'not_tested': ['2091-03 monthly settlement and rebuild',
                               'future-term renewal behavior'],
                'manifest': str(path), 'manifest_mode': '0600',
                'worker': 'deployed durable worker observed via HTTP status polling',
                'cleanup': 'exact owned fixtures and journaled external identities removed'}
    except Exception:
        # Keep ownership evidence for exact operator-requested cleanup.
        raise
    finally:
        if client:
            client.close()


def main():
    global STAGE
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--cleanup', action='store_true')
    mode.add_argument('--run', action='store_true')
    parser.set_defaults(mode_selected=False)
    mode.required = True
    parser.add_argument('--manifest')
    args = parser.parse_args()
    if args.cleanup:
        STAGE = 'manifest-read'
        check(args.manifest, '--cleanup requires --manifest')
        manifest = read_manifest(args.manifest)
        STAGE = 'cleanup'
        cleanup(load_main(), manifest, args.manifest)
        result = {'status': 'passed', 'cleanup': 'exact owned rows and external identities removed'}
    else:
        STAGE = 'runtime-acceptance'
        result = run(args.manifest)
    print(json.dumps(result, sort_keys=True, default=str))


if __name__ == '__main__':
    try:
        main()
    except AcceptanceFailure as exc:
        print(json.dumps({'status': 'failed', 'check': str(exc)}, sort_keys=True))
        raise SystemExit(1)
    except Exception as exc:
        # Never serialize exception strings, SQL, parameters, HTTP bodies, or tokens.
        print(json.dumps({'status': 'failed'} | safe_exception_metadata(exc), sort_keys=True))
        raise SystemExit(1)
