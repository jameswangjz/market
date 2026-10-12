"""TRD-BE-012 disposable PostgreSQL/APISIX/Redis acceptance, run in the API image.

PYTHONPATH=/app python /tmp/api_auto_delivery_runtime_acceptance.py --manifest /tmp/qa-api-auto.json
Add --worker to wait for the existing deployed worker on two additional orders.
Recovery: use the same environment and --cleanup --manifest PATH.

No lifespan, migrations, seeds, remote execution, deployment or global queue calls.
Only exact fixture roots are inserted. Review approval is a fixture precondition;
integration save/verify, buy, simulated payment, delivery, renewal and credentials
use production HTTP routes. Real mock-api /health verifies integration; APISIX
/v1/echo verifies buyer key-auth, platform OAuth and subscription quota.
This does not verify bank payments or full product review.

Manual fixture tasks are held one hour ahead via a scoped before_flush scheduling
guard so an existing worker cannot race negative checks. Manual HTTP processing
does not require a due task. Worker-mode orders are not held. No production
dependency or network adapter is replaced. Secrets live only in memory. The 0600
ownership journal precedes external mutations, including consumer publication.
Only exact recorded Redis keys are deleted; native limit-count DB2 keys expire
after 60 seconds and are deliberately not scanned. Keep the journal for recovery
after interruption. A killed process can leave fixture tampering until cleanup.
HTTP must already be allowed by application policy; preflight never edits global
settings. Cleanup permits real recipients only under verified fixture messages.
"""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time


PROTECTED_ORDER = 'ORD-261012005144E1B3'
MOCK_URL = 'http://market-mock-api:8300'


class AcceptanceFailure(Exception):
    """Only constant, locally authored messages may be reported."""


def check(condition, message):
    if not condition:
        raise AcceptanceFailure(message)


def fixture_ids(run_id=None):
    marker = run_id or 'qa-api-auto-' + secrets.token_hex(6)
    return {'run_id': marker,
            'users': {role: marker + '-' + role for role in ('buyer', 'seller', 'ops')},
            'enterprises': [marker + '-buy', marker + '-sell'],
            'products': [marker + '-p0', marker + '-p1'],
            'versions': [marker + '-v0', marker + '-v1'],
            'routes': [marker + '-r0', marker + '-r1'],
            'request_id': marker + '-http'}


def write_manifest(path, saved, create=False):
    path = Path(path)
    temporary = None
    if create:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    else:
        mode = path.lstat().st_mode
        check(stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600,
              'Private regular manifest required')
        fd, temporary = tempfile.mkstemp(prefix='.api-auto-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(saved, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary:
            os.replace(temporary, path)
            temporary = None
    finally:
        if temporary:
            os.unlink(temporary)


def read_manifest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        mode = os.fstat(stream.fileno()).st_mode
        check(stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600,
              'Private regular manifest required')
        saved = json.load(stream)
    validate_manifest(saved)
    return saved


def token(value):
    return value.replace('%', '%25').replace(':', '%3A')


def quota_keys(scope, period, epoch):
    prefix = 'market:apisix:quota:subscription:'
    day = datetime.fromtimestamp(epoch + 28800, timezone.utc).strftime('%Y-%m-%d')
    return [prefix + 'minute:' + token(scope) + ':' + str(int(epoch) // 60),
            prefix + 'day:' + token(scope) + ':' + day,
            prefix + 'month:' + token(scope) + ':' + token(period)]


def validate_manifest(saved):
    marker = saved.get('run_id', '')
    check(isinstance(marker, str) and re.fullmatch(r'qa-api-auto-[0-9a-f]{12}', marker),
          'Invalid fixture marker')
    check(all(saved.get(k) == v for k, v in fixture_ids(marker).items()),
          'Fixture roots changed')
    check(set(saved) <= set(fixture_ids(marker)) | {
        'consumers', 'quota_windows', 'database_ids', 'environment', 'cleaned', 'authorized'},
        'Unknown manifest fields')
    consumers = saved.get('consumers', [])
    for item in consumers:
        check(set(item) == {'id', 'name', 'route'} and
              re.fullmatch(r'[0-9a-f]{32}', item['id']) and
              item['name'] == 'market-consumer-' + item['id'] and
              item['route'] in saved['routes'], 'Invalid consumer journal')
    for window in saved.get('quota_windows', []):
        check(set(window) == {'scope', 'period', 'start', 'end', 'product'} and
              re.fullmatch(r'[0-9a-f]{32}', window['scope']) and
              window['product'] in saved['products'] and
              isinstance(window['period'], str) and 0 < len(window['period']) <= 200 and
              not any(c in window['period'] for c in '*?[]') and
              isinstance(window['start'], int) and isinstance(window['end'], int) and
              0 <= window['start'] <= window['end'] <= window['start'] + 60,
              'Invalid quota key journal')
    return saved


def redis_keys(saved):
    validate_manifest(saved)
    keys = {'market:apisix:credential:' + c['name'] for c in saved.get('consumers', [])}
    keys.update('market:apisix:oauth:' + product for product in saved['products'])
    for window in saved.get('quota_windows', []):
        for epoch in range(window['start'], window['end'] + 1):
            keys.update(quota_keys(window['scope'], window['period'], epoch))
    return sorted(keys)


def load_main():
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    check(m.engine.dialect.name == 'postgresql', 'Real PostgreSQL required')
    check(not m.app.dependency_overrides, 'Production HTTP dependencies required')
    check(m.apisix_enabled() and m.apisix_native_auth() and m.apisix_native_upstream(),
          'Native APISIX authentication and upstream required')
    return m


def environment(m):
    # Fingerprints prevent recovery against a different DB/Redis/Admin target.
    values = [m.engine.url.render_as_string(hide_password=False),
              os.getenv('REDIS_URL', 'redis://market-redis:6379/0'),
              os.getenv('APISIX_ADMIN_URL', 'http://market-apisix-admin:9180/apisix/admin')]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def preflight(m, saved):
    from sqlalchemy import or_, select
    with m.SessionLocal() as db:
        for model, values in ((m.User, list(saved['users'].values())),
                              (m.Enterprise, saved['enterprises']), (m.Product, saved['products']),
                              (m.ProductReleaseVersion, saved['versions'])):
            check(db.scalar(select(model.id).where(model.id.in_(values)).limit(1)) is None,
                  'Fixture root collision; no cleanup authorized')
        check(db.scalar(select(m.User.id).where(m.User.email.in_(
            [uid + '@example.invalid' for uid in saved['users'].values()])).limit(1)) is None,
            'Fixture email collision; no cleanup authorized')
        check(db.scalar(select(m.Enterprise.id).where(
            m.Enterprise.credit_code.in_(saved['enterprises'])).limit(1)) is None,
            'Fixture credit code collision; no cleanup authorized')
        check(db.scalar(select(m.OAuthClient.id).where(or_(
            m.OAuthClient.client_id.in_([saved['run_id'] + '-oauth-' + str(i) for i in range(2)]),
            m.OAuthClient.product_id.in_(saved['products']))).limit(1)) is None,
            'Fixture OAuth client collision; no cleanup authorized')
        check(db.scalar(select(m.ApiGatewayRoute.id).where(
            m.ApiGatewayRoute.route_key.in_(saved['routes'])).limit(1)) is None,
            'Database route collision; no cleanup authorized')
        check(m.application_address_policy['allow_http'](db) is True,
              'Existing application policy must allow HTTP; no settings changed')
        m.application_address_policy['validate_url'](MOCK_URL, db=db)
    response = m.validated_http_request('GET', MOCK_URL + '/health', timeout=5, follow_redirects=False)
    check(response.status_code == 200 and response.json() == {
        'status': 'ok', 'service': 'market-mock-api'}, 'Safe mock-api preflight health failed')
    check(bool(response.extensions.get('validated_upstream_addresses')) and
          bool(response.extensions.get('validated_upstream_ip')), 'Safe upstream destination evidence missing')
    for key in saved['routes']:
        check(admin(m, 'GET', '/routes/' + key, absent=True) is None,
              'External route collision; no cleanup authorized')
    redis = m.apisix_policy_redis()
    try:
        check(not any(redis.exists(key) for key in redis_keys(saved)),
              'Redis fixture collision; no cleanup authorized')
    finally:
        redis.close()


def unwrap(value):
    while isinstance(value, dict):
        child = next((value[k] for k in ('node', 'value', 'route')
                      if isinstance(value.get(k), dict)), None)
        if child is None:
            return value
        value = child
    return value


def owned_scopes(m, db, saved):
    """Exact parent validation, then FK descendants and polymorphic audit/messages."""
    from sqlalchemy import or_, select

    ids = fixture_ids(saved['run_id'])
    users = list(ids['users'].values())
    roots = {m.User.__table__.name: users, m.Enterprise.__table__.name: ids['enterprises'],
             m.Product.__table__.name: ids['products'],
             m.ProductReleaseVersion.__table__.name: ids['versions']}
    tables = m.Base.metadata.tables
    predicates = {name: tables[name].c.id.in_(values) for name, values in roots.items()}
    for row in db.scalars(select(m.User).where(m.User.id.in_(users))):
        check(row.name == row.id and row.email == row.id + '@example.invalid',
              'Unexpected fixture user ownership')
    for row in db.scalars(select(m.Enterprise).where(m.Enterprise.id.in_(ids['enterprises']))):
        check(row.name == row.id and row.credit_code == row.id, 'Unexpected enterprise ownership')
    for row in db.scalars(select(m.Product).where(m.Product.id.in_(ids['products']))):
        check(row.name == row.id and row.enterprise_id == ids['enterprises'][1],
              'Unexpected product ownership')

    while True:
        keys = {column: set(db.scalars(select(column).where(predicate)))
                for name, predicate in predicates.items()
                for column in tables[name].primary_key.columns}
        expanded = dict(predicates)
        for table in tables.values():
            children = [fk.parent.in_(keys[fk.column]) for fk in table.foreign_keys
                        if keys.get(fk.column)]
            if children:
                expanded[table.name] = or_(*children, *([predicates[table.name]]
                    if table.name in predicates else []))
        targets = [value for values in keys.values() for value in values if isinstance(value, str)]
        if targets:
            for model in (m.AuditLog, m.message_center['Message'], m.PlatformNotification):
                table = model.__table__
                extra = [table.c.target_id.in_(targets)]
                if model is m.AuditLog:
                    extra += [model.actor.in_([u + '@example.invalid' for u in users]),
                              model.request_id == ids['request_id']]
                expanded[table.name] = or_(*extra, *([expanded[table.name]]
                    if table.name in expanded else []))
            expired = m.message_center.get('ExpiredUrgent')
            if expired is not None:
                table = expired.__table__
                message_ids = keys.get(m.message_center['Message'].__table__.c.id, set())
                expanded[table.name] = or_(expired.message_id.in_(message_ids), expired.user_id.in_(users))
        new_keys = {column: set(db.scalars(select(column).where(predicate)))
                    for name, predicate in expanded.items()
                    for column in tables[name].primary_key.columns}
        predicates = expanded
        if keys == new_keys:
            break

    # A relation to a fixture does not authorize deleting another root or order.
    for name, values in roots.items():
        check(new_keys.get(tables[name].c.id, set()) <= set(values),
              'Cleanup reached an unowned root')
    for row in db.scalars(select(m.ProductReleaseVersion).where(
            predicates[m.ProductReleaseVersion.__table__.name])):
        check(row.id in ids['versions'] and row.product_id ==
              ids['products'][ids['versions'].index(row.id)], 'Unexpected version ownership')
    order_ids = set()
    order_table = m.Order.__table__.name
    if order_table in predicates:
        for row in db.scalars(select(m.Order).where(predicates[order_table])):
            check(row.order_no != PROTECTED_ORDER and row.id != PROTECTED_ORDER and
                  row.product_id in ids['products'] and
                  row.product_version_id == ids['versions'][ids['products'].index(row.product_id)] and
                  row.buyer_user_id == ids['users']['buyer'] and
                  row.buyer_enterprise_id == ids['enterprises'][0] and
                  row.provider_enterprise_id == ids['enterprises'][1], 'Unowned or protected order reached')
            order_ids.add(row.id)
    for model in (m.ApiGatewayRoute, m.ApiCredential):
        name = model.__table__.name
        if name not in predicates:
            continue
        for row in db.scalars(select(model).where(predicates[name])):
            if model is m.ApiGatewayRoute:
                check(row.product_id in ids['products'] and row.route_key ==
                      ids['routes'][ids['products'].index(row.product_id)], 'Unowned gateway route reached')
            else:
                route = db.get(m.ApiGatewayRoute, row.route_id)
                check(route and route.product_id in ids['products'] and
                      row.enterprise_id == ids['enterprises'][0] and row.order_id == '',
                      'Unowned credential reached')
    # Only verified fixture business messages can own real-user inbox artifacts.
    message_model = m.message_center['Message']
    message_table = message_model.__table__.name
    message_ids = set()
    task_ids = new_keys.get(m.DeliveryTask.__table__.c.id, set())
    if message_table in predicates:
        for row in db.scalars(select(message_model).where(predicates[message_table])):
            target_ids = {'order': order_ids, 'delivery_task': task_ids}
            check(row.target_type in target_ids and row.target_id in target_ids[row.target_type] and
                  row.tenant_id == ids['enterprises'][0] and row.category == 'order',
                  'Message lacks exact fixture business ownership')
            if row.event_key.startswith('subscription_term_granted:'):
                term_id = row.event_key.removeprefix('subscription_term_granted:')
                check(re.fullmatch(r'[0-9a-f]{32}', term_id), 'Invalid subscription grant event ID')
                Term, Combo = m.subscription_terms['Term'], m.subscription_terms['Combination']
                term = db.get(Term, term_id)
                order = db.get(m.Order, row.target_id)
                combo = db.get(Combo, term.combination_id) if term else None
                check(row.target_type == 'order' and term and order and combo and
                      term.id in new_keys.get(Term.__table__.c.id, set()) and
                      term.order_id == order.id and term.version_id == order.product_version_id and
                      combo.enterprise_id == ids['enterprises'][0] and combo.product_id == order.product_id and
                      combo.version_id == order.product_version_id,
                      'Subscription grant message lacks owned term and combination proof')
                message_ids.add(row.id)
                continue
            prefix = 'business:' + row.target_type + ':' + row.target_id + ':'
            check(row.event_key.startswith(prefix), 'Message business event ownership mismatch')
            action, separator, version = row.event_key[len(prefix):].partition(':')
            if row.target_type == 'order':
                check(action == 'confirm_payment' and separator and version == 'paid',
                      'Unexpected fixture order message event')
            else:
                match = re.fullmatch(re.escape(row.target_id) + r':(completed|retrying|exception):[0-9]+', version)
                check(separator and match and action in {'delivery_succeeded', 'delivery_failed'} and
                      (match[1] == 'completed') == (action == 'delivery_succeeded'),
                      'Unexpected fixture task message event')
            message_ids.add(row.id)
    recipient_tables = set()
    for key in ('Receipt', 'Delivery'):
        model = m.message_center.get(key)
        if model is not None and model.__table__.name in predicates:
            table = model.__table__
            check(set(db.scalars(select(table.c.message_id).where(predicates[table.name]))) <= message_ids,
                  'Recipient or delivery belongs to an unowned message')
            recipient_tables.add(table.name)
    expired = m.message_center.get('ExpiredUrgent')
    if expired is not None and expired.__table__.name in predicates:
        check(set(db.scalars(select(expired.message_id).where(
            predicates[expired.__table__.name]))) <= message_ids, 'Expired receipt belongs to an unowned message')
    legacy_table = m.PlatformNotification.__table__.name
    if legacy_table in predicates:
        for row in db.scalars(select(m.PlatformNotification).where(predicates[legacy_table])):
            check(row.target_type in {'order', 'delivery_task'} and
                  row.target_id in (order_ids if row.target_type == 'order' else task_ids),
                  'Legacy notification lacks exact fixture target')
    # Validate root references on every candidate row, not just reachable roots.
    allowed_roots = {**roots, order_table: order_ids}
    for name, predicate in predicates.items():
        for fk in tables[name].foreign_keys:
            parent = fk.column.table.name
            if parent in allowed_roots:
                references = set(db.scalars(select(fk.parent).where(predicate))) - {None}
                if parent == m.User.__table__.name and name in recipient_tables and fk.parent.name == 'user_id':
                    continue  # Exact message ownership was checked above; never select the real User.
                check(references <= set(allowed_roots[parent]), 'Child references an unowned fixture root')
    return predicates


def admin(m, method, path, payload=None, absent=False):
    # Production wrapper raises on 404; recovery needs idempotent exact DELETE.
    import httpx
    response = httpx.request(method,
        os.getenv('APISIX_ADMIN_URL', 'http://market-apisix-admin:9180/apisix/admin').rstrip('/') + path,
        headers={'X-API-KEY': os.environ['APISIX_ADMIN_KEY']}, json=payload, timeout=15)
    if absent and response.status_code == 404:
        return None
    if response.status_code not in {200, 201, 204}:
        # Report a schema field, never echo the upstream body or supplied secrets.
        field = ''
        try:
            detail = response.json().get('error_msg', '')
            match = re.search(r'property ["\']([a-z_]+)["\']', detail)
            field = '; schema field=' + match[1] if match else ''
        except (ValueError, TypeError, AttributeError):
            pass
        check(False, f'APISIX administration failed: {method}, HTTP {response.status_code}' + field)
    return unwrap(response.json()) if response.content else {}


def cleanup(m, saved, path):
    from sqlalchemy import delete, func, select, text

    validate_manifest(saved)
    check(saved.get('authorized') is True, 'Preflight did not authorize fixture cleanup')
    check(saved.get('environment') == environment(m), 'Recovery environment mismatch')
    with m.SessionLocal() as db:
        db.execute(text("SET LOCAL lock_timeout = '15s'"))
        # Workers lock orders first. Hold those locks until external and DB cleanup finish.
        db.scalars(select(m.Order).where(m.Order.product_id.in_(saved['products']))
                   .order_by(m.Order.id).with_for_update()).all()
        predicates = owned_scopes(m, db, saved)
        for window in saved.get('quota_windows', []):
            combo = db.get(m.subscription_terms['Combination'], window['scope'])
            check((combo is not None and combo.product_id == window['product'] and
                   combo.enterprise_id == saved['enterprises'][0]) or
                  (saved.get('cleaned') is True and combo is None), 'Quota scope ownership mismatch')
        saved['database_ids'] = {name: [list(row) for row in db.execute(
            select(*m.Base.metadata.tables[name].primary_key.columns).where(predicate))]
            for name, predicate in predicates.items()}
        write_manifest(path, saved)
        for item in saved.get('consumers', []):
            row = db.get(m.ApiCredential, item['id'])
            if row:
                route = db.get(m.ApiGatewayRoute, row.route_id)
                check(row.enterprise_id == saved['enterprises'][0] and
                      route.route_key == item['route'], 'Consumer ownership mismatch')
            live = admin(m, 'GET', '/consumers/' + item['name'], absent=True)
            if live is not None:
                check(live.get('username') == item['name'] and
                      live.get('plugins', {}).get('market-gateway-quota', {}).get('route_key') == item['route'],
                      'Live consumer ownership mismatch')
                admin(m, 'DELETE', '/consumers/' + item['name'], absent=True)
                check(admin(m, 'GET', '/consumers/' + item['name'], absent=True) is None,
                      'Owned consumer remains')
        # Also recover a committed credential journal entry after interrupted HTTP.
        with_consumers = {c['id'] for c in saved.get('consumers', [])}
        route_ids = list(db.scalars(select(m.ApiGatewayRoute.id).where(
            m.ApiGatewayRoute.product_id.in_(saved['products']))))
        check(all(row.id in with_consumers for row in db.scalars(select(m.ApiCredential).where(
            m.ApiCredential.route_id.in_(route_ids)))), 'Unjournaled credential requires manual recovery')
        for index, route_key in enumerate(saved['routes']):
            live = admin(m, 'GET', '/routes/' + route_key, absent=True)
            if live is not None:
                check(live.get('labels', {}).get('market_product_id') == saved['products'][index],
                      'Live route ownership mismatch')
                admin(m, 'DELETE', '/routes/' + route_key, absent=True)
                check(admin(m, 'GET', '/routes/' + route_key, absent=True) is None, 'Owned route remains')
        redis = m.apisix_policy_redis()
        try:
            keys = redis_keys(saved)
            if keys:
                redis.delete(*keys)
                check(not any(redis.exists(key) for key in keys), 'Owned Redis key remains')
        finally:
            redis.close()
        for table in reversed(m.Base.metadata.sorted_tables):
            if table.name in predicates:
                db.execute(delete(table).where(predicates[table.name]))
        db.commit()
        for name, predicate in predicates.items():
            check(db.scalar(select(func.count()).select_from(m.Base.metadata.tables[name])
                            .where(predicate)) == 0, 'Owned database rows remain')
    saved['cleaned'] = True
    write_manifest(path, saved)


def prepare(m, saved):
    with m.SessionLocal() as db:
        for eid in saved['enterprises']:
            db.add(m.Enterprise(id=eid, name=eid, credit_code=eid, verification_status='verified'))
        for role, uid in saved['users'].items():
            db.add(m.User(id=uid, name=uid, email=uid + '@example.invalid', password_hash='unused',
                          verified_status='verified', activation_status='active', is_active=True,
                          platform_role='platform_operator' if role == 'ops' else ''))
        db.flush()
        for uid in saved['users'].values():
            db.add(m.message_center['Preference'](user_id=uid, email_enabled=False))
        for role, index in (('buyer', 0), ('seller', 1)):
            db.add(m.Membership(user_id=saved['users'][role], enterprise_id=saved['enterprises'][index],
                                role='super_admin', status='active', business_roles='provider,user'))
        for index, method in enumerate(('api', 'model_api')):
            db.add(m.Product(id=saved['products'][index], name=saved['products'][index],
                enterprise_id=saved['enterprises'][1], provider_name=saved['enterprises'][1],
                product_type='api' if method == 'api' else 'model', delivery_method=method, status='pending_integration',
                settlement_rule_mode='custom', settlement_rule_json=json.dumps({
                    'platform_rate': 20, 'provider_rate': 80, 'service_rate': 0,
                    'expert_rate': 0, 'channel_rate': 0})))
            db.flush()
            db.add(m.ProductReleaseVersion(id=saved['versions'][index], product_id=saved['products'][index],
                version_code='v1', price=12, cost=7, status='active', rate_limit_per_minute=60,
                daily_quota=100, monthly_quota=1000))
            # Review precondition includes the approved product's platform OAuth client.
            db.add(m.OAuthClient(product_id=saved['products'][index],
                client_id=saved['run_id'] + '-oauth-' + str(index), client_secret=secrets.token_urlsafe(32),
                scope='resource.invoke', status='active'))
        db.commit()
        return {role: m.issue_token(db.get(m.User, uid)) for role, uid in saved['users'].items()}


def consumer_journal(m, saved, path):
    """after_flush sees generated IDs; persist recovery names before sync PUT."""
    def journal(db, *_):
        for row in db.new:
            if isinstance(row, m.ApiCredential) and row.enterprise_id == saved['enterprises'][0]:
                route = db.get(m.ApiGatewayRoute, row.route_id)
                check(route and route.route_key in saved['routes'] and route.product_id in saved['products'],
                      'Credential outside owned route')
                check(isinstance(row.id, str) and re.fullmatch(r'[0-9a-f]{32}', row.id),
                      'Credential ID must be generated before publication')
                item = {'id': row.id, 'name': 'market-consumer-' + row.id, 'route': route.route_key}
                if item not in saved['consumers']:
                    saved['consumers'].append(item)
                    write_manifest(path, saved)
    return journal


def exercise(m, saved, path, gateway_url, worker=False, worker_timeout=60, browser_fixture=None):
    import httpx
    from fastapi.testclient import TestClient
    from sqlalchemy import event, select

    held_orders = set()
    checks = []
    browser_orders = []
    session_class = m.SessionLocal.class_

    def hold_manual_tasks(db, *_):
        for row in db.new.union(db.dirty):
            if isinstance(row, m.DeliveryTask) and row.order_id in held_orders and row.status in {'in_delivery', 'retrying'}:
                row.next_retry_at = m.now() + timedelta(hours=1)

    journal_consumers = consumer_journal(m, saved, path)
    event.listen(session_class, 'before_flush', hold_manual_tasks)
    event.listen(session_class, 'after_flush', journal_consumers)
    client = TestClient(m.app)  # No lifespan/background jobs.
    redis = m.apisix_policy_redis()
    try:
        tokens = prepare(m, saved)

        def request(method, url, role='buyer', body=None):
            response = client.request(method, url, headers={'Authorization': 'Bearer ' + tokens[role],
                'X-Request-Id': saved['request_id']}, json=body)
            check(response.status_code == 200, 'Production HTTP acceptance request failed')
            return response.json()

        def task_state(oid):
            with m.SessionLocal() as db:
                order = db.get(m.Order, oid)
                check(order and order.product_id in saved['products'] and order.order_no != PROTECTED_ORDER,
                      'Order outside fixture scope')
                tasks = db.scalars(select(m.DeliveryTask).where(m.DeliveryTask.order_id == oid)).all()
                check(len(tasks) == 1 and tasks[0].delivery_mode == 'automatic', 'Expected one automatic task')
                task = tasks[0]
                return {'id': task.id, 'status': task.status, 'retries': task.retry_count,
                        'main': order.main_status, 'delivery': order.delivery_status}

        def process(oid, success=True):
            before = task_state(oid)
            request('POST', '/api/delivery-tasks/' + before['id'] + '/process', 'ops', {'success': True})
            after = task_state(oid)
            if success:
                check((after['status'], after['main'], after['delivery']) ==
                      ('completed', 'completed', 'accepted'), 'Automatic delivery did not complete')
                with m.SessionLocal() as db:
                    Term = m.subscription_terms['Term']
                    check(db.scalar(select(Term).where(Term.order_id == oid)) is not None,
                          'Successful delivery lacks subscription term')
                    check(db.scalar(select(m.AuditLog).where(m.AuditLog.order_id == oid,
                        m.AuditLog.action == 'delivery_succeeded')) is not None, 'Delivery audit missing')
            else:
                check(after['status'] == 'retrying' and after['retries'] == before['retries'] + 1 and
                      after['main'] != 'completed' and after['delivery'] != 'accepted',
                      'Negative delivery attempt incorrectly succeeded')
                with m.SessionLocal() as db:
                    Term = m.subscription_terms['Term']
                    check(db.scalar(select(Term).where(Term.order_id == oid)) is None,
                          'Failed attempt started a subscription term')
            return after

        def pay(oid, hold=True):
            if hold:
                held_orders.add(oid)
            request('POST', '/api/orders/' + oid + '/transition', body={
                'action': 'confirm_payment', 'reason': 'Isolated QA simulated payment; no bank transaction'})

        def buy(index, hold=True):
            oid = request('POST', '/api/orders', body={'product_id': saved['products'][index],
                'product_version_id': saved['versions'][index],
                'buyer_enterprise_id': saved['enterprises'][0], 'subscription_months': 1})['id']
            pay(oid, hold)
            return oid

        def renew(oid):
            result = request('POST', '/api/orders/' + oid + '/renewal', body={
                'subscription_months': 1, 'idempotency_key': saved['run_id'] + '-' + secrets.token_hex(6)})
            pay(result['id'])
            process(result['id'])
            return result['id']

        def invoke(route, raw_key, scope, period, product, expected=200):
            # Pre-journal exact possible counter keys across bounded request/clock rollover.
            start = int(redis.time()[0])
            saved['quota_windows'].append({'scope': scope, 'period': period,
                                          'start': start - 2, 'end': start + 25, 'product': product})
            write_manifest(path, saved)
            response = httpx.get(gateway_url.rstrip('/') + '/gateway/' + route + '/v1/echo',
                                 headers={'X-API-Key': raw_key}, timeout=15, follow_redirects=False)
            check(response.status_code == expected, 'APISIX buyer invocation status mismatch')
            if expected == 200:
                payload = response.json()
                check(payload.get('code') == 0 and payload.get('data', {}).get('provider') == 'mock-api',
                      'APISIX response did not come from known mock-api echo')

        for index in range(2):
            base = '/api/products/' + saved['products'][index] + '/versions/' + saved['versions'][index] + '/integration'
            integration_body = {'upstream_url': MOCK_URL, 'route_key': saved['routes'][index],
                'version': 'v1', 'upstream_auth_mode': 'oauth2', 'health_path': '/' + saved['run_id'] + '-missing',
                'rate_limit_per_minute': 60, 'daily_quota': 100, 'monthly_quota': 1000}
            request('PUT', base, 'ops', integration_body)
            # The production review hook normally attaches these credentials. This
            # synthetic approved product skips review; attach only its own client.
            with m.SessionLocal() as db:
                route = db.scalar(select(m.ApiGatewayRoute).where(
                    m.ApiGatewayRoute.product_id == saved['products'][index]))
                oauth = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == route.product_id))
                route.upstream_client_id, route.upstream_client_secret = oauth.client_id, oauth.client_secret
                db.commit()
            negative = request('POST', base + '/verify', 'ops')
            check(negative['status'] == 'failed' and negative['health_status_code'] == 404,
                  'Actual mock health-path failure did not block publication')
            check(admin(m, 'GET', '/routes/' + saved['routes'][index], absent=True) is None,
                  'Unhealthy integration published a route')
            request('PUT', base, 'ops', integration_body | {'health_path': '/health'})
            verified = request('POST', base + '/verify', 'ops')
            check(verified['status'] == 'verified' and verified['healthy'], 'Version verify/publish failed')
            with m.SessionLocal() as db:
                Config = m.version_gateway['Config']
                config = db.scalar(select(Config).where(Config.version_id == saved['versions'][index]))
                check(config.revision_id and config.publish_record_id, 'Publish evidence missing')
                route_id = config.route_id
                approved_payload = m.apisix_route_payload(db.get(m.ApiGatewayRoute, route_id),
                    db.get(m.Product, saved['products'][index]), db)
            live = admin(m, 'GET', '/routes/' + saved['routes'][index])
            check(live.get('labels', {}).get('market_product_id') == saved['products'][index],
                  'Published route labels mismatch')
            oid = buy(index)
            check(task_state(oid)['status'] == 'in_delivery', 'Owned task not initially pending')
            with m.SessionLocal() as db:
                check(db.scalar(select(m.subscription_terms['Term']).where(
                    m.subscription_terms['Term'].order_id == oid)) is None, 'Payment started a subscription term')

            # A health-path evidence tamper must block delivery. This proves
            # fail-closed evidence, not an upstream outage or a live health retry.
            with m.SessionLocal() as db:
                config = db.scalar(select(Config).where(Config.version_id == saved['versions'][index]))
                original_path = config.health_path
                config.health_path = '/' + saved['run_id'] + '-missing'
                db.commit()
            try:
                process(oid, success=False)
            finally:
                with m.SessionLocal() as db:
                    config = db.scalar(select(Config).where(Config.version_id == saved['versions'][index]))
                    config.health_path = original_path
                    db.commit()

            # Tamper only the exact owned live route; restore even when assertion fails.
            # GET includes read-only id/create_time/update_time metadata which is
            # not an accepted Admin PUT body. Mutate the approved writable payload.
            changed = dict(approved_payload, methods=['POST'])
            admin(m, 'PUT', '/routes/' + saved['routes'][index], changed)
            try:
                process(oid, success=False)
            finally:
                admin(m, 'PUT', '/routes/' + saved['routes'][index], approved_payload)
            process(oid)
            with m.SessionLocal() as db:
                Term = m.subscription_terms['Term']
                original_term = db.scalar(select(Term).where(Term.order_id == oid))
                original_term_id = original_term.id
            request('POST', '/api/delivery-tasks/' + task_state(oid)['id'] + '/process', 'ops', {'success': True})
            with m.SessionLocal() as db:
                term_ids = list(db.scalars(select(Term.id).where(Term.order_id == oid)))
                check(term_ids == [original_term_id], 'Repeated processing duplicated subscription grant')
                rows = db.scalars(select(m.ApiCredential).where(m.ApiCredential.route_id == route_id)).all()
                check(len(rows) == 1 and rows[0].status == 'active' and rows[0].secret_encrypted,
                      'Initial delivery must create one encrypted shared credential')
                credential_id = rows[0].id
                check(db.scalar(select(m.AuditLog).where(m.AuditLog.order_id == oid,
                    m.AuditLog.action == 'issue_initial_api_credential')) is not None,
                    'Automatic credential issuance audit missing')
            listed = request('GET', '/api/orders/' + oid + '/api-credentials')
            credential = next(row for row in listed['items'] if row['id'] == credential_id)
            check(credential.get('secret_download_available') is True and 'api_key' not in credential
                  and 'secret_encrypted' not in credential, 'Credential listing leaks a secret or lacks download')
            download_path = '/api/orders/' + oid + '/api-credentials/' + credential_id + '/download'
            downloaded = client.get(download_path, headers={'Authorization': 'Bearer ' + tokens['buyer']})
            check(downloaded.status_code == 200 and 'no-store' in downloaded.headers.get('cache-control', '')
                  and 'attachment;' in downloaded.headers.get('content-disposition', ''),
                  'Buyer secret download must be an authenticated non-cacheable attachment')
            raw_key = downloaded.json().get('api_key')
            check(isinstance(raw_key, str) and raw_key.startswith('mk_'), 'Downloaded initial buyer secret missing')
            for role in ('seller', 'ops'):
                denied = client.get(download_path, headers={'Authorization': 'Bearer ' + tokens[role]})
                check(denied.status_code == 403, 'Provider or platform role downloaded buyer secret')
            shared_initial = request('POST', '/api/orders/' + oid + '/api-credentials', body={})
            check(shared_initial['id'] == credential_id and 'api_key' not in shared_initial,
                  'Manual create unexpectedly rotated the automatic initial credential')
            access = request('GET', '/api/orders/' + oid + '/delivery-access')
            check(access.get('ready') is True, 'Automatic credential issuance did not make order access ready')
            with m.SessionLocal() as db:
                audit_row = db.scalar(select(m.AuditLog).where(m.AuditLog.order_id == oid,
                    m.AuditLog.action == 'download_api_credential'))
                check(audit_row is not None, 'Credential download audit was not committed')
                check(raw_key not in repr({c.name: getattr(audit_row, c.name)
                    for c in m.AuditLog.__table__.columns}), 'Credential secret leaked into audit')
            item = next(c for c in saved['consumers'] if c['id'] == credential['id'])
            metadata = redis.hgetall('market:apisix:credential:' + item['name'])
            check(metadata.get('mode') == 'subscription' and metadata.get('status') == 'active',
                  'Shared subscription policy missing')
            periods = json.loads(metadata['subscription_periods'])
            now_epoch = int(redis.time()[0])
            active = [p for p in periods if p['start_at'] <= now_epoch < p['end_at']]
            check(len(active) == 1, 'Expected one active quota period')
            period, scope = active[0]['period_key'], metadata['quota_scope']
            invoke(item['route'], raw_key, scope, period, saved['products'][index])
            first_window = saved['quota_windows'][-1]
            day_keys = {quota_keys(scope, period, epoch)[1]
                        for epoch in range(first_window['start'], first_window['end'] + 1)}
            daily_before = {key: redis.get(key) for key in day_keys}
            check(sum(int(value or 0) for value in daily_before.values()) == 1,
                  'APISIX did not count the first daily request')
            month_key = quota_keys(scope, period, now_epoch)[2]
            before = redis.get(month_key)
            check(before == '1', 'APISIX did not count the first request')
            renewal = renew(oid)
            shared = request('POST', '/api/orders/' + renewal + '/api-credentials', body={})
            check(shared['id'] == credential['id'] and 'api_key' not in shared,
                  'Renewal did not retain the shared credential')
            check(redis.get(month_key) == before, 'Renewal reset subscription usage')
            check(all(redis.get(key) == value for key, value in daily_before.items()),
                  'Renewal reset daily usage')
            with m.SessionLocal() as db:
                combo = db.get(m.subscription_terms['Combination'], scope)
                check(combo.total_months == 2, 'Renewal did not extend the shared subscription')
            invoke(item['route'], raw_key, scope, period, saved['products'][index])
            check(redis.get(month_key) == '2', 'Shared credential did not preserve counting')
            if browser_fixture:
                with m.SessionLocal() as db:
                    browser_orders.append({'id': oid, 'order_no': db.get(m.Order, oid).order_no,
                        'product_id': saved['products'][index], 'credential_id': credential['id'],
                        'method': 'api' if index == 0 else 'model_api'})
            else:
                request('POST', '/api/orders/' + oid + '/api-credentials/' + credential['id'] + '/revoke')
                renew(oid)
                listed = request('GET', '/api/orders/' + oid + '/api-credentials')
                revoked = next(c for c in listed['items'] if c['id'] == credential['id'])
                check(revoked['status'] == 'revoked', 'Renewal reactivated a revoked credential')
                response = httpx.get(gateway_url.rstrip('/') + '/gateway/' + item['route'] + '/v1/echo',
                                     headers={'X-API-Key': raw_key}, timeout=15)
                check(response.status_code in {401, 403}, 'Revoked credential still invokes APISIX')
                check(redis.get(month_key) == '2', 'Revoked invocation changed usage')
            raw_key = None
            checks.append(('api' if index == 0 else 'model_api') + ': manual, negative, credential, renewal' +
                          ('' if browser_fixture else ', revoke'))
            if worker:
                worker_oid = buy(index, hold=False)
                deadline = time.monotonic() + worker_timeout
                while time.monotonic() < deadline and task_state(worker_oid)['status'] != 'completed':
                    time.sleep(1)
                state = task_state(worker_oid)
                check((state['status'], state['main'], state['delivery'], state['retries']) ==
                      ('completed', 'completed', 'accepted', 0), 'Deployed worker did not complete owned task')
                with m.SessionLocal() as db:
                    audit = db.scalar(select(m.AuditLog).where(m.AuditLog.order_id == worker_oid,
                        m.AuditLog.action == 'delivery_succeeded'))
                    check(audit and audit.actor == 'jfsh@market.local', 'Unexpected worker delivery actor')
                    check(db.scalar(select(Term).where(Term.order_id == worker_oid)) is not None,
                          'Worker success lacks subscription term')
                    check(db.get(m.ApiCredential, credential['id']).status == ('active' if browser_fixture else 'revoked'),
                          'Worker delivery changed shared credential lifecycle')
                checks.append(('api' if index == 0 else 'model_api') + ': deployed worker')
        if browser_fixture:
            write_manifest(browser_fixture, {'run_id': saved['run_id'], 'token': tokens['buyer'],
                'orders': browser_orders, 'enterprise_id': saved['enterprises'][0]}, create=True)
        return {'status': 'passed', 'checks': checks, 'worker_exercised': worker,
                'payment': 'simulated', 'upstream': 'mock-api /v1/echo with platform OAuth',
                'negative_health': 'real 404 before publication; delivery health-path evidence tamper; no service outage'}
    finally:
        event.remove(session_class, 'before_flush', hold_manual_tasks)
        event.remove(session_class, 'after_flush', journal_consumers)
        client.close()
        redis.close()


def run(args):
    path = Path(args.manifest)
    if args.cleanup:
        saved = read_manifest(path)  # Validate before importing any DB module.
        check(saved.get('authorized') is True, 'Preflight did not authorize fixture cleanup')
        m = load_main()
        cleanup(m, saved, path)
        return {'status': 'passed', 'cleanup': 'exact owned fixtures removed'}
    saved = fixture_ids()
    saved.update(consumers=[], quota_windows=[])
    write_manifest(path, saved, create=True)
    m = load_main()
    saved['environment'] = environment(m)
    write_manifest(path, saved)
    preflight(m, saved)  # Collision refusal must not authorize cleanup of existing rows.
    saved['authorized'] = True
    write_manifest(path, saved)
    try:
        result = exercise(m, saved, path, args.gateway_url, args.worker, args.worker_timeout,
                          getattr(args, 'browser_fixture', None))
    except BaseException:
        cleanup(m, saved, path)
        raise
    else:
        if getattr(args, 'browser_fixture', None):
            result['cleanup'] = 'pending browser acceptance; exact owned cleanup required'
            return result
        cleanup(m, saved, path)
    result['cleanup'] = 'exact owned fixtures removed; native DB2 rate keys expire'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--cleanup', action='store_true')
    parser.add_argument('--gateway-url', default='http://market-apisix:9080')
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--worker-timeout', type=int, choices=range(1, 121), default=60)
    parser.add_argument('--browser-fixture', help='Private 0600 token file; retain fixtures for browser, then --cleanup')
    args = parser.parse_args(argv)
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        try:
            result = run(args)
        except Exception as exc:
            result = {'status': 'failed', 'error_type': type(exc).__name__,
                      'recovery': 'Use private manifest with --cleanup in the same environment'}
            if isinstance(exc, AcceptanceFailure):
                result['check_failed'] = str(exc)
    finally:
        logging.disable(previous_logging_disable)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
