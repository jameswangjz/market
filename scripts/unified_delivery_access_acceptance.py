"""Owned real-PG/network-HTTP contract acceptance; never deploys or starts an API.

All payment, term, file scan, route and credential records are SYNTHETIC.
No storage upload/download, APISIX invocation, provisioning, or positive OIDC.
See docs/unified-delivery-access-acceptance-runbook.md before live execution.
"""

import argparse
from datetime import timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from urllib.parse import urlsplit

import version_change_quote_acceptance as guarded

AcceptanceFailure = guarded.AcceptanceFailure
check = guarded.check
METHODS = ('file', 'api', 'model_api', 'tenant_access')
ROLES = ('buyer', 'admin', 'member', 'other', 'provider', 'platform')
VERSION_ROLES = ('source', 'current', 'future')
PROTECTED = ('ORD-261012005144E1B3', 'productqa-vcq-a044ea9ea181-p2')
LIMITS = {'rate_limit_per_minute': 17, 'daily_quota': 123,
          'monthly_quota': 456, 'quota_amount': 789}
# Only transport/recipient rows observed changing under the asynchronous worker.
# Message events, preferences, audit, business data and cleanup stay fully scoped.
ASYNC_NOTIFICATION_SNAPSHOT_TABLES = frozenset({
    'notification_deliveries', 'notification_delivery_attempts', 'notification_recipients',
})


def fixture_ids(run_id=None):
    run_id = run_id or 'qa-uda-' + secrets.token_hex(6)
    check(bool(re.fullmatch(r'qa-uda-[0-9a-f]{12}', run_id)), 'Invalid run marker')
    return {'run_id': run_id,
            'users': {r: run_id + '-u-' + r for r in ROLES},
            'enterprises': [run_id + '-e-' + r for r in ('buyer', 'seller', 'other')],
            'products': {m: run_id + '-p' + str(i) for i, m in enumerate(METHODS)},
            'versions': {m: {r: run_id + '-v' + str(i) + '-' + r for r in VERSION_ROLES}
                         for i, m in enumerate(METHODS)},
            'orders': {m: {r: run_id + '-o' + str(i) + '-' + r for r in VERSION_ROLES}
                       for i, m in enumerate(METHODS)},
            'files': [run_id + '-file'], 'request_ids': [run_id + '-http']}


def write_scope(path, ids, phase, *, create=False, database_ids=None):
    value = {**ids, 'schema': 1, 'phase': phase}
    if database_ids is not None:
        value['database_ids'] = {name: sorted(map(list, rows)) for name, rows in database_ids.items()}
    guarded.private_json(path, value, create=create)


def read_scope(path, *, include_document=False):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        mode = os.fstat(stream.fileno()).st_mode
        check(stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600,
              'Manifest must be a regular 0600 file')
        value = json.load(stream)
    check(not any(marker in json.dumps(value) for marker in PROTECTED), 'Protected scope refused')
    ids = fixture_ids(value.get('run_id', ''))
    check(all(value.get(k) == v for k, v in ids.items()), 'Changed ownership IDs refused')
    check(value.get('schema') == 1 and value.get('phase') in {'allocated', 'prepared', 'cleaned'},
          'Invalid manifest lifecycle')
    check(set(value) <= set(ids) | {'schema', 'phase', 'database_ids'}, 'Unexpected manifest fields')
    return (ids, value['phase'], value) if include_document else (ids, value['phase'])


def load_main():
    # Importing app.main registers models/routes, but never enters its lifespan.
    check(os.environ.get('DATABASE_URL', '').startswith(('postgresql://', 'postgresql+')),
          'Explicit PostgreSQL DATABASE_URL required')
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    check(m.engine.dialect.name == 'postgresql', 'Real PostgreSQL required')
    check(not m.app.dependency_overrides, 'Production dependencies required')
    return m


def owned_rows(m, db, ids):
    from sqlalchemy import select

    owned = guarded.owned_rows(m, db, ids)
    allowed_orders = {v for group in ids['orders'].values() for v in group.values()}
    roots = {m.User.__table__.name: set(ids['users'].values()),
             m.Enterprise.__table__.name: set(ids['enterprises']),
             m.Product.__table__.name: set(ids['products'].values())}
    # FK reachability alone must never adopt a real account or its business data.
    for name, keys in owned.items():
        table = m.Base.metadata.tables[name]
        rows = db.execute(select(table).where(guarded.row_predicate(table, keys))).mappings().all()
        for row in rows:
            for fk in table.foreign_keys:
                allowed = roots.get(fk.column.table.name)
                if allowed is not None and row[fk.parent.name] is not None:
                    check(row[fk.parent.name] in allowed, 'External root linkage; operation refused')
            if name == m.Order.__table__.name:
                check(row['id'] in allowed_orders and row['order_no'] == row['id'],
                      'Unplanned order; operation refused')
            if name == m.Product.__table__.name:
                check(row['status'] in {'draft', 'security_unpublished'}, 'Public fixture; operation refused')
            if name == m.FileObject.__table__.name:
                check(row['id'] in ids['files'] and row['owner_id'] == ids['users']['provider']
                      and row['object_name'] == ids['run_id'] + '/not-uploaded',
                      'Unplanned file; operation refused')
            if name == m.ApiGatewayRoute.__table__.name:
                check(row['created_by'] == ids['run_id'], 'Unowned route; operation refused')
            if name == m.ApiCredential.__table__.name:
                check(row['created_by'] == ids['run_id'], 'Unowned credential; operation refused')
    return owned


def preflight(m, db, ids):
    from sqlalchemy import func, or_, select

    groups = ((m.User, list(ids['users'].values())), (m.Enterprise, ids['enterprises']),
              (m.Product, list(ids['products'].values())), (m.FileObject, ids['files']),
              (m.Order, [v for g in ids['orders'].values() for v in g.values()]),
              (m.ProductReleaseVersion, [v for g in ids['versions'].values() for v in g.values()]),
              (m.SaaSProductVersion, [v for g in ids['versions'].values() for v in g.values()]))
    for model, values in groups:
        predicate = model.id.in_(values)
        if model is m.User:
            predicate = or_(predicate, model.email.in_([v + '@example.invalid' for v in values]))
        if model is m.Enterprise:
            predicate = or_(predicate, model.credit_code.in_(values))
        if model is m.Order:
            predicate = or_(predicate, model.order_no.in_(values))
        check(db.scalar(select(func.count()).select_from(model).where(predicate)) == 0, 'Fixture collision')


def order_fixture(m, ids, method, role, limits=None):
    pid, vid = ids['products'][method], ids['versions'][method][role]
    oid = ids['orders'][method][role]
    economic = {'schema_version': 1, 'product_id': pid, 'product_version_id': vid,
                'version_code': role, 'billing_unit': 'order' if method == 'file' else 'month',
                'subscription_months': 1, 'unit_price': '12', 'unit_cost': '7',
                'amount': '12', 'total_cost': '7', 'currency': 'CNY',
                'settlement_rule': {'version': 'synthetic', 'rates': {
                    'platform_rate': 20, 'provider_rate': 80, 'service_rate': 0, 'expert_rate': 0, 'channel_rate': 0}}}
    delivery = {'method': method, 'download_limit': 2, 'quota': LIMITS if limits is None else limits,
                'files': [{'id': ids['files'][0], 'checksum': 'synthetic-checksum'}] if method == 'file' else []}
    return m.Order(id=oid, order_no=oid, buyer_enterprise_id=ids['enterprises'][0],
        buyer_user_id=ids['users']['buyer'], provider_enterprise_id=ids['enterprises'][1],
        product_id=pid, buyer_name=ids['enterprises'][0], product_name=pid,
        product_version_id=vid, product_version_code=role, snapshot_version=1,
        subscription_months=1, amount=12, paid_amount=12, refunded_amount=0,
        unit_price_snapshot=12, unit_cost_snapshot=7, total_cost_snapshot=7,
        economic_snapshot_json=json.dumps(economic), delivery_snapshot_json=json.dumps(delivery),
        delivery_method_snapshot=method, main_status='completed', delivery_status='accepted',
        payment_status='paid')


def prepare(m, ids, path, *, browser_profile=False):
    from app.subscription_calendar import CalendarAnchor

    with m.SessionLocal() as db:
        preflight(m, db, ids)
        at = m.now()
        for eid in ids['enterprises']:
            db.add(m.Enterprise(id=eid, name=eid, credit_code=eid, verification_status='verified'))
        for role, uid in ids['users'].items():
            db.add(m.User(id=uid, name=uid, email=uid + '@example.invalid',
                password_hash='disabled-synthetic-login', email_verified=True,
                verified_status='verified', activation_status='active', is_active=True,
                platform_role='super_admin' if role == 'platform' else ''))
        db.flush()
        for role in ROLES:
            db.add(m.message_center['Preference'](user_id=ids['users'][role], email_enabled=False))
            enterprise = 2 if role == 'other' else 1 if role == 'provider' else 0
            membership = 'member' if role == 'member' else 'enterprise_admin' if role == 'admin' else 'super_admin'
            db.add(m.Membership(user_id=ids['users'][role], enterprise_id=ids['enterprises'][enterprise],
                role=membership, status='active', business_roles='provider,user'))
        for method, pid in ids['products'].items():
            limits = ({**LIMITS, 'daily_quota': 0, 'monthly_quota': 0, 'quota_amount': 0}
                      if browser_profile and method == 'api' else LIMITS)
            db.add(m.Product(id=pid, name=pid, enterprise_id=ids['enterprises'][1],
                provider_name=ids['enterprises'][1], status='draft', delivery_method=method,
                product_type={'file': 'dataset', 'api': 'api', 'model_api': 'model', 'tenant_access': 'saas'}[method],
                application_url='https://synthetic.example.invalid/app'))
            db.flush()
            for role, vid in ids['versions'][method].items():
                values = dict(id=vid, product_id=pid, version_code=role, status='active', cost=7)
                version = (m.SaaSProductVersion(name=role, monthly_price=12, **values)
                           if method == 'tenant_access' else m.ProductReleaseVersion(price=12, **limits, **values))
                db.add(version)
            db.flush()
            for role in VERSION_ROLES:
                order = order_fixture(m, ids, method, role, limits)
                m.order_economics['economic_snapshot'](order)
                m.order_economics['delivery_snapshot'](order)
                db.add(order)
            db.flush()
            if method in {'api', 'model_api'}:
                anchor = at - timedelta(days=1)
                bounds = CalendarAnchor(anchor).term(1)
                combination = m.subscription_terms['Combination'](enterprise_id=ids['enterprises'][0],
                    product_id=pid, delivery_method=method, version_id=ids['versions'][method]['current'],
                    version_code='current', anchor_at=anchor, generation=1, total_months=1, status='active')
                db.add(combination)
                db.flush()
                db.add(m.subscription_terms['Term'](order_id=ids['orders'][method]['current'],
                    combination_id=combination.id, generation=1, version_id=combination.version_id,
                    version_code='current', start_month=0, months=1, starts_at=bounds.start, ends_at=bounds.end))
                for role in VERSION_ROLES:
                    route = m.ApiGatewayRoute(product_id=pid, version=role, status='active',
                        route_key=ids['versions'][method][role], upstream_url='https://synthetic-upstream.invalid',
                        created_by=ids['run_id'])
                    db.add(route)
                    db.flush()
                    if role == 'current':
                        db.add(m.ApiCredential(route_id=route.id, enterprise_id=ids['enterprises'][0],
                            order_id='', key_hash=hashlib.sha256((pid + '-synthetic').encode()).hexdigest(),
                            status='active', expires_at=at + timedelta(days=20), created_by=ids['run_id'],
                            apisix_consumer_name=('' if browser_profile and method == 'model_api' else
                                                 ids['run_id'] + '-SYNTHETIC-NOT-APISIX-PROOF')))
            if method == 'tenant_access':
                subscription = m.SaaSSubscription(enterprise_id=ids['enterprises'][0], product_id=pid,
                    version_id=ids['versions'][method]['source'], status='active',
                    external_tenant_id=ids['run_id'], starts_at=at, expires_at=at + timedelta(days=20),
                    created_by=ids['run_id'])
                db.add(subscription)
                db.flush()
                db.get(m.Order, ids['orders'][method]['source']).subscription_id = subscription.id
        db.add(m.FileObject(id=ids['files'][0], owner_id=ids['users']['provider'],
            product_id=ids['products']['file'], version_id=ids['versions']['file']['source'],
            object_name=ids['run_id'] + '/not-uploaded', original_name='synthetic.txt',
            size=1, checksum='synthetic-checksum', file_role='product_data', status='draft', scan_status='clean',
            scan_report=json.dumps({'presidio_status': 'available', 'synthetic': True, 'real_scan_verified': False})))
        db.flush()
        owned = owned_rows(m, db, ids)
        write_scope(path, ids, 'allocated', database_ids=owned)
        db.commit()
    write_scope(path, ids, 'prepared', database_ids=owned)


def export_browser(m, ids, manifest, base_url, gateway_origin, deployment_tag):
    """Only called after NEW prepare; never mints a token for an adopted account."""
    from sqlalchemy import select

    saved, phase = read_scope(manifest)
    check(stat.S_IMODE(Path(manifest).parent.stat().st_mode) == 0o700,
          'Browser fixture directory must be 0700')
    check(saved == ids and phase == 'prepared', 'Browser export requires exact newly prepared scope')
    with m.SessionLocal() as db:
        owned_rows(m, db, ids)
        user = db.get(m.User, ids['users']['buyer'])
        check(user is not None and user.name == user.id and user.email == user.id + '@example.invalid'
              and user.email_verified and user.verified_status == 'verified' and not user.platform_role,
              'Browser token requires exact verified synthetic buyer')
        orders = {}
        for method in METHODS:
            order = db.get(m.Order, ids['orders'][method]['source'])
            check(order is not None and order.buyer_user_id == user.id
                  and order.buyer_enterprise_id == ids['enterprises'][0]
                  and order.product_id == ids['products'][method], 'Browser order outside owned scope')
            entry = {'id': order.id, 'orderNo': order.order_no, 'method': method,
                     'ready': method in {'file', 'api'}}
            if method in {'api', 'model_api'}:
                entitlement = m.subscription_terms['current_entitlement'](db, ids['enterprises'][0], order.product_id)
                check(entitlement is not None, 'Browser entitlement unavailable at live clock')
                entry.update(versionCode=entitlement['version_code'], expiresAt=entitlement['expires_at'].isoformat(),
                    gatewayUrl=validate_url(gateway_origin) + '/gateway/' + ids['versions'][method]['current'],
                    limits={k: entitlement['limits'].get(k, 0) for k in (
                        'rate_limit_per_minute', 'daily_quota', 'monthly_quota', 'quota_amount')})
            orders[method] = entry
        hashes = list(db.scalars(select(m.ApiCredential.key_hash).where(m.ApiCredential.created_by == ids['run_id'])))
        value = {'schema': 1, 'profile': 'unified-delivery-browser-negative-saas', 'runId': ids['run_id'],
                 'userId': user.id, 'enterpriseId': ids['enterprises'][0], 'orders': orders,
                 'baseUrl': validate_url(base_url), 'gatewayOrigin': validate_url(gateway_origin),
                 'deploymentTag': deployment_tag, 'buyerToken': m.issue_token(user),
                 'sensitiveSentinels': hashes + ['synthetic-upstream.invalid', 'synthetic-checksum'],
                 'evidence': 'synthetic payment/terms/scan/routes/consumer metadata; NOT actual APISIX or SSO proof'}
    browser = Path(manifest).with_suffix('.browser.json')
    guarded.private_json(browser, value, create=True)
    return browser


def remove_browser_fixture(manifest):
    browser = Path(manifest).with_suffix('.browser.json')
    if browser.exists() or browser.is_symlink():
        mode = browser.lstat().st_mode
        check(stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600,
              'Browser fixture must be a regular 0600 file')
        browser.unlink()


def snapshot(m, ids):
    from sqlalchemy import select

    with m.SessionLocal() as db:
        owned = owned_rows(m, db, ids)
        return {name: sorted((tuple(row) for row in db.execute(select(m.Base.metadata.tables[name]).where(
                    guarded.row_predicate(m.Base.metadata.tables[name], keys)))), key=repr)
                for name, keys in owned.items() if name not in ASYNC_NOTIFICATION_SNAPSHOT_TABLES}


def snapshot_differences(metadata, before, after):
    """Describe mismatches using schema names only; never row IDs or values."""
    differences = []
    for name in sorted(set(before) | set(after)):
        table = metadata.tables[name]
        columns = list(table.columns)
        indices = [columns.index(column) for column in table.primary_key.columns]
        def indexed(rows):
            return {tuple(row[i] for i in indices): row for row in rows}
        old, new = indexed(before.get(name, [])), indexed(after.get(name, []))
        fields = set()
        if set(old) != set(new):
            fields.add('row_identity')
        for key in set(old) & set(new):
            fields.update(column.name for i, column in enumerate(columns) if old[key][i] != new[key][i])
        if fields:
            differences.append({'table': name, 'fields': sorted(fields)})
    return differences


def assert_snapshot_restored(m, ids, before, message):
    after = snapshot(m, ids)
    if after != before:
        error = AcceptanceFailure(message)
        error.differences = snapshot_differences(m.Base.metadata, before, after)
        raise error


def temporary_fixture_change(m, ids, model, pk, values, action):
    from sqlalchemy import select, update

    before = snapshot(m, ids)
    table = model.__table__
    with m.SessionLocal() as db:
        owned = owned_rows(m, db, ids)
        check((pk,) in owned.get(table.name, set()), 'Scenario row outside owned scope')
        predicate = guarded.row_predicate(table, {(pk,)})
        row = db.execute(select(table).where(predicate)).mappings().one()
        names = set(values) | {c.name for c in table.columns if c.onupdate is not None}
        if 'updated_at' in table.c:
            names.add('updated_at')
        original = {name: row[name] for name in names}
        db.execute(update(table).where(predicate).values(**values))
        db.commit()
    try:
        return action()
    finally:
        with m.SessionLocal() as db:
            owned = owned_rows(m, db, ids)
            check((pk,) in owned.get(table.name, set()), 'Owned scenario row disappeared')
            # ORM dirty tracking can omit an equal timestamp and trigger onupdate.
            # Core explicitly supplies every original automatic-update field.
            db.execute(update(table).where(predicate).values(**original))
            db.commit()
        assert_snapshot_restored(m, ids, before, 'Owned rows not restored after individual scenario')


def assert_public(body, hidden=()):
    check(isinstance(body, dict), 'HTTP body must be an object')
    encoded = json.dumps(body)
    check(not any(value and value in encoded for value in hidden), 'Sensitive persisted value leaked')
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                check(key.lower() not in {'key', 'api_key', 'credential', 'apisix_consumer_name'} and
                      not any(s in key.lower() for s in ('secret', 'upstream', 'cost', 'hash', 'token', 'key_prefix')),
                      'Sensitive response field')
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            check('synthetic-upstream' not in value and 'synthetic-checksum' not in value,
                  'Sensitive fixture value leaked')
    visit(body)


def validate_url(base_url):
    url = urlsplit(base_url)
    check(url.scheme in {'http', 'https'} and url.hostname and not url.username and not url.password
          and not url.query and not url.fragment and url.path in {'', '/'}
          and url.port != 0 and '\\' not in base_url
          and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in base_url), 'Invalid API origin')
    return base_url.rstrip('/')


def exercise(m, ids, base_url, gateway_origin, gateway_unavailable=False):
    import httpx
    from sqlalchemy import select

    base_url = validate_url(base_url)
    gateway_origin = validate_url(gateway_origin)
    with m.SessionLocal() as db:
        owned_rows(m, db, ids)
        tokens = {r: m.issue_token(db.get(m.User, uid)) for r, uid in ids['users'].items()}
        hidden = [c.key_hash for c in db.scalars(select(m.ApiCredential).where(
            m.ApiCredential.created_by == ids['run_id']))]
    cases = []
    scenario = 'baseline'
    with httpx.Client(base_url=base_url, follow_redirects=False, timeout=20, trust_env=False) as client:
        def request(method, delivery, role='buyer', expected=200, ready=None):
            oid = ids['orders'][delivery]['source']
            before = snapshot(m, ids)
            response = client.request(method, f'/api/orders/{oid}/delivery-' + ('access' if method == 'GET' else 'launch'),
                headers={'Authorization': 'Bearer ' + tokens[role], 'X-Request-Id': ids['request_ids'][0]})
            assert_snapshot_restored(m, ids, before, 'HTTP request changed owned rows, credentials, routes or audit')
            check(response.status_code == expected, 'Unexpected HTTP status')
            body = response.json()
            assert_public(body, hidden)
            if method == 'GET' and expected == 200:
                check(response.headers.get('cache-control') == 'no-store', 'Access is cacheable')
                check(set(body) == {'order_id', 'method', 'ready', 'reason', 'files', 'api', 'saas'}, 'Unexpected access fields')
                check(body['order_id'] == oid and body['method'] == delivery, 'Wrong access identity')
                check(body['ready'] is ready and isinstance(body['reason'], str), 'Unexpected readiness')
                if not ready:
                    check(bool(body['reason']), 'Blocked access lacks reason')
            cases.append({'scenario': scenario, 'method': method, 'delivery': delivery,
                          'role': role, 'http': expected, 'ready': ready})
            return body

        def changed(model, pk, values, action):
            nonlocal scenario
            previous_scenario = scenario
            scenario = 'fixture_change:' + model.__table__.name + ':' + ','.join(values)
            try:
                return temporary_fixture_change(m, ids, model, pk, values, action)
            finally:
                scenario = previous_scenario

        baseline = snapshot(m, ids)
        for method in METHODS:
            for role in ('buyer', 'admin'):
                body = request('GET', method, role, ready=(method != 'tenant_access' and
                    not (gateway_unavailable and method in {'api', 'model_api'})))
                if method in {'api', 'model_api'}:
                    api = body['api']
                    check(set(api) == {'gateway_base_path', 'gateway_url', 'credentials_path', 'shared_scope', 'version_code', 'expires_at', 'limits'},
                          'Unexpected API fields')
                    check(api['version_code'] == 'current' and api['gateway_base_path'] ==
                          '/gateway/' + ids['versions'][method]['current'], 'Order version used instead of current entitlement')
                    check(api['credentials_path'] == '/api/orders/' + ids['orders'][method]['source'] + '/api-credentials'
                          and api['shared_scope'] == 'enterprise', 'Wrong credential management scope')
                    check(api['gateway_url'] == ('' if gateway_unavailable else
                        gateway_origin + '/gateway/' + ids['versions'][method]['current']), 'Wrong public gateway address')
                    with m.SessionLocal() as db:
                        entitlement = m.subscription_terms['current_entitlement'](db, ids['enterprises'][0], ids['products'][method])
                        check(entitlement is not None and api['expires_at'] == entitlement['expires_at'].isoformat(),
                              'Wrong entitlement expiry')
                        check(api['limits'] == {k: entitlement['limits'].get(k, 0) for k in (
                            'rate_limit_per_minute', 'daily_quota', 'monthly_quota', 'quota_amount')}, 'Wrong entitlement limits')
                if method == 'file':
                    check(len(body['files']) == 1 and body['files'][0]['id'] == ids['files'][0]
                          and body['files'][0]['remaining_downloads'] == 2, 'Wrong frozen file access')
            for role in ('member', 'other', 'provider', 'platform'):
                request('GET', method, role, expected=403)
                request('POST', method, role, expected=403)
            request('POST', method, expected=409)
            oid = ids['orders'][method]['source']
            for values in ({'payment_status': 'unpaid'}, {'paid_amount': 1}, {'delivery_status': 'not_started'},
                           {'refunded_amount': 1}, {'main_status': 'cancelled'}, {'snapshot_version': 0}):
                changed(m.Order, oid, values, lambda: request('GET', method, ready=False))
            changed(m.Product, ids['products'][method], {'status': 'security_unpublished'},
                    lambda: request('GET', method, ready=False))
            if method in {'api', 'model_api'}:
                with m.SessionLocal() as db:
                    route = db.scalar(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.product_id == ids['products'][method],
                        m.ApiGatewayRoute.version == 'current'))
                    credential = db.scalar(select(m.ApiCredential).where(m.ApiCredential.route_id == route.id))
                    combination = db.scalar(select(m.subscription_terms['Combination']).where(
                        m.subscription_terms['Combination'].product_id == ids['products'][method]))
                    route_id, credential_id, combination_id = route.id, credential.id, combination.id
                for values in ({'status': 'revoked'}, {'expires_at': m.now() - timedelta(days=1)},
                               {'order_id': oid}, {'apisix_consumer_name': ''}):
                    def no_credential():
                        body = request('GET', method, ready=False)
                        check(body['api'] and body['api']['credentials_path'], 'Missing credential configuration entry')
                    changed(m.ApiCredential, credential_id, values, no_credential)
                changed(m.ApiGatewayRoute, route_id, {'status': 'disabled'}, lambda: request('GET', method, ready=False))
                for bad_key in ('bad/slug', '../upstream', 'bad?key=secret'):
                    def bad_route():
                        body = request('GET', method, ready=False)
                        check(body['api'] and body['api']['gateway_url'] == '' and body['api']['gateway_base_path'] == '',
                              'Unsafe route exposed a gateway URL')
                    changed(m.ApiGatewayRoute, route_id, {'route_key': bad_key}, bad_route)
                # Real clock, calendar-consistent expired/future terms: no clock patch.
                with m.SessionLocal() as db:
                    term = db.scalar(select(m.subscription_terms['Term']).where(
                        m.subscription_terms['Term'].combination_id == combination_id))
                    term_id = term.id
                from app.subscription_calendar import CalendarAnchor
                for anchor in (m.now() - timedelta(days=90), m.now() + timedelta(days=30)):
                    bounds = CalendarAnchor(anchor).term(1)
                    changed(m.subscription_terms['Combination'], combination_id, {'anchor_at': anchor}, lambda:
                        changed(m.subscription_terms['Term'], term_id, {'starts_at': bounds.start, 'ends_at': bounds.end},
                                lambda: request('GET', method, ready=False)))
            if method == 'tenant_access':
                with m.SessionLocal() as db:
                    sid = db.get(m.Order, oid).subscription_id
                for values in ({'enterprise_id': ids['enterprises'][2]}, {'product_id': ids['products']['api']}):
                    changed(m.SaaSSubscription, sid, values, lambda: (
                        request('GET', method, ready=False), request('POST', method, expected=409)))
                # Without an OIDC client every URL remains blocked. This verifies
                # fail-closed HTTP behavior, not which policy rejected it first.
                for address in ('javascript:alert(1)', 'https://user:pass@example.invalid/app',
                                'https://example.invalid\\unsafe', 'https://example.invalid:0/app'):
                    changed(m.Product, ids['products'][method], {'application_url': address}, lambda: (
                        request('GET', method, ready=False), request('POST', method, expected=409)))
        assert_snapshot_restored(m, ids, baseline, 'Owned rows not restored after scenario changes')
    return cases


def cleanup_synthetic_policy(m, ids, path, db):
    """Remove only the scheduler-created hash for this exact synthetic consumer."""
    from sqlalchemy import select

    if not m.apisix_enabled():
        return 0
    scopes = set(db.scalars(select(m.subscription_terms['Combination'].id).where(
        m.subscription_terms['Combination'].enterprise_id == ids['enterprises'][0],
        m.subscription_terms['Combination'].product_id.in_(ids['products'].values()))))
    saved_ids, _, saved = read_scope(path, include_document=True)
    check(saved_ids == ids, 'Synthetic Redis manifest mismatch')
    if not scopes:
        # Exact cleaned ownership manifests retain discovered PKs for recovery.
        check(saved.get('phase') == 'cleaned', 'Missing owned subscription scope')
        table = m.subscription_terms['Combination'].__table__.name
        scopes = {key[0] for key in saved.get('database_ids', {}).get(table, [])}
    client = m.apisix_policy_redis()
    key = 'market:apisix:credential:' + ids['run_id'] + '-SYNTHETIC-NOT-APISIX-PROOF'
    script = """
    if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
    if redis.call('TYPE', KEYS[1]).ok ~= 'hash' then return -1 end
    if redis.call('HGET', KEYS[1], 'mode') ~= 'subscription' then return -1 end
    local scope = redis.call('HGET', KEYS[1], 'quota_scope')
    for _, expected in ipairs(ARGV) do
      if scope == expected then return redis.call('DEL', KEYS[1]) end
    end
    return -1
    """
    removed = client.eval(script, 1, key, *sorted(scopes))
    check(removed in (0, 1), 'Synthetic Redis policy ownership mismatch')
    check(not client.exists(key), 'Synthetic Redis policy remains')
    return removed


def cleanup(m, ids, path):
    from sqlalchemy import delete, func, select, text

    with m.SessionLocal() as db:
        db.execute(text("SET LOCAL lock_timeout = '15s'"))
        for model, values in ((m.Product, list(ids['products'].values())),
                              (m.Enterprise, ids['enterprises']), (m.User, list(ids['users'].values()))):
            db.scalars(select(model).where(model.id.in_(values)).order_by(model.id).with_for_update()).all()
        owned = owned_rows(m, db, ids)
        _, phase = read_scope(path)
        if not owned and phase == 'cleaned':
            cleanup_synthetic_policy(m, ids, path, db)
            return
        write_scope(path, ids, 'prepared', database_ids=owned)
        cleanup_synthetic_policy(m, ids, path, db)
        for table in reversed(m.Base.metadata.sorted_tables):
            if owned.get(table.name):
                db.execute(delete(table).where(guarded.row_predicate(table, owned[table.name])))
        db.commit()
    with m.SessionLocal() as db:
        check(not owned_rows(m, db, ids), 'Owned rows remain')
        for table in m.Base.metadata.tables.values():
            for fk in table.foreign_keys:
                parent = fk.column.table
                columns = list(parent.primary_key.columns)
                index = next((i for i, c in enumerate(columns) if c is fk.column), None)
                if index is not None and owned.get(parent.name):
                    values = [row[index] for row in owned[parent.name]]
                    check(db.scalar(select(func.count()).select_from(table).where(fk.parent.in_(values))) == 0,
                          'Owned FK remains')
    write_scope(path, ids, 'cleaned', database_ids=owned)


def run(mode, manifest, base_url=None, gateway_origin='http://192.168.10.10:30082', gateway_unavailable=False,
        *, prepare_browser=False, deployment_tag=None):
    if prepare_browser:
        check(mode == 'prepare', 'Browser export only supports new prepare')
        validate_url(base_url or '')
        validate_url(gateway_origin)
        check(bool(deployment_tag and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', deployment_tag)),
              'Browser deployment tag required')
    if mode == 'prepare':
        ids = fixture_ids()
        path = Path(manifest) if manifest else Path('/tmp') / (ids['run_id'] + '.json')
        if prepare_browser:
            browser = path.with_suffix('.browser.json')
            check(stat.S_IMODE(path.parent.stat().st_mode) == 0o700,
                  'Browser fixture directory must be 0700')
            check(not browser.exists() and not browser.is_symlink(), 'Browser fixture path already exists')
        write_scope(path, ids, 'allocated', create=True)
        phase = 'allocated'
    else:
        path = Path(manifest)
        ids, phase = read_scope(path)  # Must precede application import/DB access.
    if mode == 'test':
        check(phase == 'prepared', 'Test requires prepared manifest')
        validate_url(base_url or '')
    m = load_main()
    result = {'status': 'failed', 'mode': mode, 'manifest': str(path), 'manifest_mode': '0600',
              'evidence': 'real PostgreSQL + network HTTP contract; all fixtures synthetic',
              'limits': ['no APISIX/model upstream proof', 'no payment/provisioning proof',
                         'no file storage/download/scan proof', 'SaaS negative only; no OIDC positive or launch audit proof'],
              'cleanup_verified': False, 'four_delivery_e2e': False,
              'snapshot_excluded_tables': sorted(ASYNC_NOTIFICATION_SNAPSHOT_TABLES),
              'asynchronous_notification_rows': 'not measured by snapshots; ownership and exact FK cleanup still include them',
              'cleanup': 'deferred; retained private ownership manifest'}
    if mode == 'prepare':
        prepare(m, ids, path, browser_profile=prepare_browser)
        if prepare_browser:
            browser = export_browser(m, ids, path, base_url, gateway_origin, deployment_tag)
            result['browser_fixture'] = str(browser)
            result['browser_fixture_mode'] = '0600'
            result['browser_profile'] = 'API zero quotas; model API missing consumer; SaaS missing OIDC'
    elif mode == 'test':
        from sqlalchemy import text
        # The periodic notification scan also refreshes credential policies.
        # Its existing transaction lease is released when this context closes.
        with m.SessionLocal() as scheduling_lease:
            scheduling_lease.execute(text("SET LOCAL lock_timeout = '15s'"))
            scheduling_lease.execute(text('SELECT pg_advisory_xact_lock(72810421)'))
            result['cases'] = exercise(m, ids, base_url, gateway_origin, gateway_unavailable)
        result['scheduler_isolation'] = 'Periodic scan transaction lease held during test; delivery queue remained running; released on session close'
        result['readonly_business_rows'] = True
        result['readonly_audit_and_business_events'] = True
    else:
        from sqlalchemy import text
        with m.SessionLocal() as scheduling_lease:
            scheduling_lease.execute(text("SET LOCAL lock_timeout = '15s'"))
            scheduling_lease.execute(text('SELECT pg_advisory_xact_lock(72810421)'))
            cleanup(m, ids, path)
        remove_browser_fixture(path)
        result['cleanup_verified'] = True
        result['cleanup'] = 'verified exact owned rows and FK children absent; private manifest retained'
    result['status'] = 'passed'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('prepare', 'test', 'cleanup'), required=True)
    parser.add_argument('--manifest')
    parser.add_argument('--base-url')
    parser.add_argument('--gateway-origin', default='http://192.168.10.10:30082')
    parser.add_argument('--expect-gateway-unavailable', action='store_true')
    parser.add_argument('--prepare-browser', action='store_true')
    parser.add_argument('--deployment-tag')
    args = parser.parse_args()
    if args.mode != 'prepare' and not args.manifest:
        parser.error('test/cleanup require --manifest')
    if args.mode == 'test' and not args.base_url:
        parser.error('test requires --base-url')
    if args.prepare_browser and (args.mode != 'prepare' or not args.base_url or not args.deployment_tag):
        parser.error('--prepare-browser requires mode prepare, --base-url and --deployment-tag')
    logging.disable(logging.CRITICAL)
    if args.mode == 'prepare' and args.manifest is None:
        directory = Path(tempfile.mkdtemp(prefix='qa-uda-browser-', dir='/tmp')) if args.prepare_browser else Path('/tmp')
        args.manifest = str(directory / (fixture_ids()['run_id'] + '.json'))
    try:
        result = run(args.mode, args.manifest, args.base_url, args.gateway_origin, args.expect_gateway_unavailable,
                     prepare_browser=args.prepare_browser, deployment_tag=args.deployment_tag)
    except Exception as exc:
        # Never expose SQL, response bodies, tokens, connection URLs, or SDK errors.
        result = {'status': 'failed', 'mode': args.mode, 'error_type': type(exc).__name__,
                  'manifest': args.manifest,
                  'cleanup_verified': False, 'four_delivery_e2e': False,
                  'snapshot_excluded_tables': sorted(ASYNC_NOTIFICATION_SNAPSHOT_TABLES),
                  'asynchronous_notification_rows': 'not measured by snapshots; ownership and exact FK cleanup still include them',
                  'cleanup': 'not verified; retain private manifest and use cleanup mode'}
        if isinstance(exc, AcceptanceFailure):
            result['check_failed'] = str(exc)
            if hasattr(exc, 'differences'):
                result['differences'] = exc.differences
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
