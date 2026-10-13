"""BE016 read-only quote acceptance against real, already migrated PostgreSQL.

  PYTHONPATH=/app python /tmp/version_change_quote_acceptance.py
  ... --prepare-browser --base-url https://market.example --deployment-tag TAG
  ... --cleanup --manifest /tmp/qa-vcq-....json

No lifespan, schema creation, gateway requests, MinIO, or worker processing.
Paid orders and trusted on_delivery_success grants are fixtures, not payment or
delivery verification. Active/verified gateway records are synthetic DB evidence,
not APISIX/model/SaaS integration acceptance. Production catalog and quote HTTP
handlers remain real. A controlled server clock places the three-month current
term at 50% and the one-month future paid term at 100% remaining.

The ownership manifest is IDs-only and 0600. Browser credentials live in a separate
0600 .browser.json. Default execution cleans even after failure. Browser mode
retains fixtures only on success; --cleanup uses the same exact parent ownership.
"""

import argparse
from datetime import timedelta
from decimal import Decimal
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


METHODS = ('api', 'model_api', 'tenant_access')
FIXTURE_GROUPS = (*METHODS, 'browser_api')
ROLES = ('buyer', 'admin', 'member', 'other', 'provider')
VERSION_ROLES = ('source', 'up', 'down', 'cent')


class AcceptanceFailure(Exception):
    """Only authored checks, never raw SQL/SDK errors, may be printed."""


def check(condition, message):
    if not condition:
        raise AcceptanceFailure(message)


def fixture_ids(run_id=None):
    run_id = run_id or 'qa-vcq-' + secrets.token_hex(6)
    return {'run_id': run_id,
            'users': {role: run_id + '-u-' + role for role in ROLES},
            'enterprises': [run_id + '-e-' + role for role in ('buyer', 'seller', 'other')],
            'products': {method: run_id + '-p' + str(i) for i, method in enumerate(FIXTURE_GROUPS)},
            'versions': {method: {role: run_id + '-v' + str(i) + '-' + role for role in VERSION_ROLES}
                         for i, method in enumerate(FIXTURE_GROUPS)},
            'request_ids': [run_id + '-http']}


def private_json(path, value, *, create=False):
    path = Path(path)
    temporary = None
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    else:
        check(stat.S_ISREG(path.lstat().st_mode), 'Private artifact must be a regular file')
        fd, temporary = tempfile.mkstemp(prefix='.vcq-', dir=path.parent)
    completed = False
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if temporary is not None:
            os.replace(temporary, path)
            temporary = None
        completed = True
    finally:
        if temporary is not None:
            os.unlink(temporary)
        if create and not completed:
            os.unlink(path)


def read_scope(path):
    path = Path(path)
    mode = path.lstat().st_mode
    check(stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600,
          'Ownership manifest must be a regular 0600 file')
    value = json.loads(path.read_text())
    run_id = value.get('run_id', '')
    check(isinstance(run_id, str) and re.fullmatch(r'qa-vcq-[0-9a-f]{12}', run_id),
          'Invalid ownership marker')
    expected = fixture_ids(run_id)
    check(all(value.get(key) == item for key, item in expected.items()),
          'Ownership IDs were changed; cleanup refused')
    check(set(value) <= set(expected) | {'database_ids'}, 'Unexpected ownership manifest fields')
    return expected


def row_predicate(table, keys):
    from sqlalchemy import tuple_

    return tuple_(*table.primary_key.columns).in_(list(keys))


def expand_fk_ids(db, metadata, owned):
    """Follow FK children of exact owned PKs; never expand to their parents."""
    from sqlalchemy import or_, select

    owned = {name: set(rows) for name, rows in owned.items()}
    while True:
        expanded = {name: set(rows) for name, rows in owned.items()}
        for table in metadata.tables.values():
            children = []
            for fk in table.foreign_keys:
                parent = fk.column.table
                if owned.get(parent.name):
                    values = list(db.scalars(select(fk.column).where(row_predicate(parent, owned[parent.name]))))
                    if values:
                        children.append(fk.parent.in_(values))
            if children:
                keys = {tuple(row) for row in db.execute(select(*table.primary_key.columns).where(or_(*children)))}
                if keys:
                    expanded.setdefault(table.name, set()).update(keys)
        if expanded == owned:
            return owned
        owned = expanded


def owned_rows(m, db, ids):
    from sqlalchemy import or_, select

    users = list(ids['users'].values())
    products = list(ids['products'].values())
    owned = {}

    def add(model, predicate):
        table = getattr(model, '__table__', model)
        rows = {tuple(row) for row in db.execute(select(*table.primary_key.columns).where(predicate))}
        if rows:
            owned.setdefault(table.name, set()).update(rows)

    for user in db.scalars(select(m.User).where(m.User.id.in_(users))):
        check(user.name == user.id and user.email == user.id + '@example.invalid', 'Unexpected user ownership')
    for enterprise in db.scalars(select(m.Enterprise).where(m.Enterprise.id.in_(ids['enterprises']))):
        check(enterprise.name == enterprise.id and enterprise.credit_code == enterprise.id,
              'Unexpected enterprise ownership')
    for product in db.scalars(select(m.Product).where(m.Product.id.in_(products))):
        check(product.name == product.id and product.enterprise_id == ids['enterprises'][1],
              'Unexpected product ownership')
    for member in db.scalars(select(m.Membership).where(m.Membership.user_id.in_(users))):
        check(member.enterprise_id in ids['enterprises'], 'External membership; cleanup refused')
    orders = db.scalars(select(m.Order).where(m.Order.product_id.in_(products))).all()
    check(all(o.buyer_user_id == ids['users']['buyer'] and o.buyer_enterprise_id == ids['enterprises'][0]
              and o.provider_enterprise_id == ids['enterprises'][1]
              and o.product_version_id in [v for group in ids['versions'].values() for v in group.values()]
              for o in orders), 'Unexpected order ownership')
    for model, values in ((m.User, users), (m.Enterprise, ids['enterprises']), (m.Product, products)):
        add(model, model.id.in_(values))
    owned = expand_fk_ids(db, m.Base.metadata, owned)
    targets = [value for keys in owned.values() for row in keys for value in row if isinstance(value, str)]
    order_ids = [o.id for o in orders]
    add(m.AuditLog, or_(m.AuditLog.target_id.in_(targets), m.AuditLog.order_id.in_(order_ids),
                       m.AuditLog.request_id.in_(ids['request_ids']),
                       m.AuditLog.actor.in_([uid + '@example.invalid' for uid in users])))
    center = m.message_center
    add(center['Message'], center['Message'].target_id.in_(targets))
    message_ids = [row[0] for row in owned.get(center['Message'].__table__.name, set())]
    add(center['ExpiredUrgent'], or_(center['ExpiredUrgent'].message_id.in_(message_ids),
                                    center['ExpiredUrgent'].user_id.in_(users)))
    add(m.PlatformNotification, or_(m.PlatformNotification.target_id.in_(targets),
                                   m.PlatformNotification.recipient_user_id.in_(users)))
    owned = expand_fk_ids(db, m.Base.metadata, owned)
    # A foreign key proves linkage, not ownership of an unexpected business row.
    version_products = {vid: ids['products'][group]
                        for group, versions in ids['versions'].items() for vid in versions.values()}
    for model, allowed in ((m.User, users), (m.Enterprise, ids['enterprises']),
                           (m.Product, products),
                           (m.ProductReleaseVersion, list(version_products)),
                           (m.SaaSProductVersion, list(version_products))):
        table = model.__table__
        if owned.get(table.name):
            for row in db.scalars(select(model).where(row_predicate(table, owned[table.name]))):
                check(row.id in allowed, 'External business row; cleanup refused')
                if model in (m.ProductReleaseVersion, m.SaaSProductVersion):
                    check(row.product_id == version_products[row.id], 'External version linkage; cleanup refused')
    for model in (m.Membership, m.Order):
        table = model.__table__
        if not owned.get(table.name):
            continue
        for row in db.scalars(select(model).where(row_predicate(table, owned[table.name]))):
            if model is m.Membership:
                check(row.user_id in users and row.enterprise_id in ids['enterprises'],
                      'External membership; cleanup refused')
            else:
                check(row.product_id in products and row.buyer_user_id == ids['users']['buyer']
                      and row.buyer_enterprise_id == ids['enterprises'][0]
                      and row.provider_enterprise_id == ids['enterprises'][1]
                      and version_products.get(row.product_version_id) == row.product_id,
                      'External order linkage; cleanup refused')
    return owned


def save_owned(m, db, ids, path, owned):
    private_json(path, {**ids, 'database_ids': {name: sorted(map(list, rows)) for name, rows in owned.items()}})


def cleanup(m, ids, manifest):
    from sqlalchemy import delete, func, select, text

    with m.SessionLocal() as db:
        db.execute(text("SET LOCAL lock_timeout = '15s'"))
        db.scalars(select(m.Product).where(m.Product.id.in_(list(ids['products'].values())))
                   .order_by(m.Product.id).with_for_update()).all()
        db.scalars(select(m.Order).where(m.Order.product_id.in_(list(ids['products'].values())))
                   .order_by(m.Order.id).with_for_update()).all()
        for model, values in ((m.Enterprise, ids['enterprises']), (m.User, list(ids['users'].values()))):
            db.scalars(select(model).where(model.id.in_(values)).order_by(model.id).with_for_update()).all()
        Outbox = m.message_center['Outbox']
        db.scalars(select(Outbox).where(Outbox.user_id.in_(list(ids['users'].values())))
                   .order_by(Outbox.id).with_for_update()).all()
        owned = owned_rows(m, db, ids)
        save_owned(m, db, ids, manifest, owned)
        for table in reversed(m.Base.metadata.sorted_tables):
            if owned.get(table.name):
                db.execute(delete(table).where(row_predicate(table, owned[table.name])))
        db.commit()
    with m.SessionLocal() as db:
        check(not owned_rows(m, db, ids), 'Owned rows remain after cleanup')
        for table in m.Base.metadata.tables.values():
            for fk in table.foreign_keys:
                parent = fk.column.table
                columns = list(parent.primary_key.columns)
                index = next((i for i, column in enumerate(columns) if column is fk.column), None)
                if index is not None and owned.get(parent.name):
                    values = [row[index] for row in owned[parent.name]]
                    check(db.scalar(select(func.count()).select_from(table).where(fk.parent.in_(values))) == 0,
                          'Owned FK remains in ' + table.name)


def load_main():
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m

    check(m.engine.dialect.name == 'postgresql', 'Real PostgreSQL required')
    check(not m.app.dependency_overrides, 'Production HTTP dependencies required')
    check(hasattr(m, 'version_change_quotes'), 'Production version_change_quotes install required')
    check(m.apisix_enabled(), 'APISIX_ENABLED required for real catalog readiness of DB fixture evidence')
    return m


def gateway_fixture(m, db, product, version, run_id, at):
    """Synthetic persisted evidence only; never call verify/publish adapters."""
    fields = dict(upstream_url='https://fixture.example.invalid', route_key=version.id,
                  auth_mode='api_key', upstream_auth_mode='none', upstream_scope='resource.invoke',
                  timeout_ms=10000, strip_prefix=True, health_method='GET', health_path='/health',
                  rate_limit_per_minute=17, daily_quota=123, monthly_quota=456)
    route = m.ApiGatewayRoute(product_id=product.id, version=version.version_code,
                              status='active', created_by=run_id, **fields)
    db.add(route)
    db.flush()
    payload = m.apisix_route_payload(route, product, db)
    revision = m.GatewayConfigRevision(route_id=route.id, product_id=product.id, revision=1,
        status='active', config_json=json.dumps(payload), created_by=run_id,
        error_message='Synthetic acceptance fixture; no gateway integration verified')
    db.add(revision)
    db.flush()
    record = m.GatewayPublishRecord(route_id=route.id, revision_id=revision.id, target='apisix',
        status='succeeded', completed_at=at, created_by=run_id,
        response_json=json.dumps({'fixture': True, 'gateway_verified': False}))
    db.add(record)
    db.flush()
    # This is the gateway evidence identity format, not quote/economic arithmetic.
    identity = {**fields, 'product_id': product.id, 'version_id': version.id,
                'version_code': version.version_code, 'route_id': route.id,
                'upstream_client_id': route.upstream_client_id,
                'upstream_client_secret': route.upstream_client_secret,
                'quotas': [version.rate_limit_per_minute, version.daily_quota, version.monthly_quota],
                'payload': payload}
    config = m.version_gateway['Config'](product_id=product.id, version_id=version.id,
        version_code=version.version_code, route_id=route.id, status='verified', healthy=True,
        verified_at=at, verified_by=run_id, health_status_code=200, revision_id=revision.id,
        publish_record_id=record.id,
        evidence_hash=hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        **fields)
    db.add(config)
    db.flush()
    check(m.version_gateway['version_ready'](db, product, version), 'Fixture catalog gateway evidence is not ready')


def prepare(m, ids, anchor, *, include_browser=False):
    from sqlalchemy import select

    orders = {}
    with m.SessionLocal() as db:
        for eid in ids['enterprises']:
            db.add(m.Enterprise(id=eid, name=eid, credit_code=eid, verification_status='verified'))
        for uid in ids['users'].values():
            db.add(m.User(id=uid, email=uid + '@example.invalid', name=uid, password_hash='not-a-login-password',
                          verified_status='verified', activation_status='active', is_active=True))
        db.flush()
        for role, membership, enterprise in (('buyer', 'super_admin', 0), ('admin', 'enterprise_admin', 0),
                ('member', 'member', 0), ('other', 'super_admin', 2), ('provider', 'super_admin', 1)):
            db.add(m.Membership(user_id=ids['users'][role], enterprise_id=ids['enterprises'][enterprise],
                                role=membership, status='active', business_roles='provider,user'))
            db.add(m.message_center['Preference'](user_id=ids['users'][role], email_enabled=False))
        db.flush()
        for group, pid in ids['products'].items():
            if group == 'browser_api' and not include_browser:
                continue
            method = 'api' if group == 'browser_api' else group
            product = m.Product(id=pid, enterprise_id=ids['enterprises'][1], name=pid,
                provider_name=ids['enterprises'][1], product_type={'api': 'api', 'model_api': 'model',
                'tenant_access': 'saas'}[method], delivery_method=method, status='published',
                application_url='https://fixture.example.invalid/app',
                settlement_rule_mode='custom', settlement_rule_json=json.dumps({
                    'platform_rate': 20, 'provider_rate': 80, 'service_rate': 0, 'expert_rate': 0, 'channel_rate': 0}))
            db.add(product)
            db.flush()
            if method == 'tenant_access':
                db.add(m.SaaSIntegrationConfig(product_id=pid, base_url='https://fixture.example.invalid',
                    token_url='https://fixture.example.invalid/oauth/token', operation_path='/isv.php',
                    auth_mode='oauth2', client_id=ids['run_id'], client_secret='synthetic-fixture-not-a-secret',
                    status='active', updated_by=ids['run_id']))
            for role, price in (('source', 100), ('up', 150), ('down', 50), ('cent', Decimal('150.01'))):
                values = dict(id=ids['versions'][group][role], product_id=pid, version_code=role,
                              status='active', cost=30,
                              compatibility_mode='breaking' if role == 'up' else 'compatible',
                              compatibility_notes='Fixture breaking change' if role == 'up' else '')
                if method == 'tenant_access':
                    version = m.SaaSProductVersion(name=role, monthly_price=price, **values)
                else:
                    version = m.ProductReleaseVersion(price=price, rate_limit_per_minute=17,
                        daily_quota=123, monthly_quota=456, **values)
                db.add(version)
                db.flush()
                if method != 'tenant_access':
                    gateway_fixture(m, db, product, version, ids['run_id'], anchor)
            orders[group] = []
            for months in (3, 1):
                user = db.get(m.User, ids['users']['buyer'])
                body = m.OrderBody(product_id=pid, product_version_id=ids['versions'][group]['source'],
                    buyer_enterprise_id=ids['enterprises'][0], subscription_months=months)
                quoted = m.order_economics['quote'](db, user, body, lock=True)
                order = m.create_quoted_order(db, user, quoted)
                order.payment_status, order.paid_amount = 'paid', order.amount
                order.main_status, order.delivery_status = 'completed', 'accepted'
                db.flush()
                payment = db.scalar(select(m.Payment).where(m.Payment.order_id == order.id))
                payment.status, payment.paid_at = 'paid', anchor
                db.flush()
                check(order.amount == (300 if months == 3 else 100), 'Unexpected frozen fixture amount')
                if months == 3:
                    check(order.total_cost_snapshot == 90, 'Unexpected frozen fixture cost')
                term = m.subscription_terms['on_delivery_success'](db, order, user, at=anchor)
                check(term is not None and term.months == months, 'Trusted fixture term grant failed')
                orders[group].append(order.id)
            if method == 'tenant_access':
                subscription = db.get(m.SaaSSubscription, order.subscription_id)
                check(subscription is not None and subscription.product_id == pid,
                      'Missing owned SaaS subscription fixture')
                subscription.status = 'active'
                subscription.external_tenant_id = ids['run_id'] + '-tenant'
                subscription.starts_at = anchor
        db.commit()
        # Auth tokens follow the real wall clock, independently of quote server time.
        tokens = {role: m.issue_token(db.get(m.User, uid)) for role, uid in ids['users'].items()}
    return orders, tokens


def readonly_snapshot(m, ids):
    from sqlalchemy import select

    with m.SessionLocal() as db:
        owned = owned_rows(m, db, ids)
        return {name: sorted((tuple(row) for row in db.execute(select(table).where(row_predicate(table, keys)))), key=repr)
                for name, keys in owned.items()
                if not name.startswith('notification_') and name not in {'audit_logs', 'platform_notifications'}
                for table in [m.Base.metadata.tables[name]]}


def assert_hidden(value):
    if isinstance(value, dict):
        for key, child in value.items():
            check(not any(word in key.lower() for word in ('cost', 'secret', 'upstream', 'settlement_rule')),
                  'Quote DTO/token exposed an internal field')
            assert_hidden(child)
    elif isinstance(value, list):
        for child in value:
            assert_hidden(child)


def exercise(m, ids, orders, tokens, current):
    """Production HTTP contract checks; expected amounts are literal assertions."""
    from fastapi.testclient import TestClient
    import jwt
    from sqlalchemy import update
    from unittest.mock import patch

    before = readonly_snapshot(m, ids)
    client = TestClient(m.app)  # No lifespan context or dependency overrides.
    results = {}

    def request(method, path, role='admin', expected=200, **kwargs):
        response = client.request(method, path, headers={
            'Authorization': 'Bearer ' + tokens[role], 'X-Request-Id': ids['request_ids'][0]}, **kwargs)
        check(response.status_code == expected,
              f'{method} {path}: expected {expected}, got {response.status_code}')
        if expected == 200:
            assert_hidden(response.json())
        return response

    def quote(oid, target, role='admin', confirmed=True, expected=200):
        return request('POST', f'/api/orders/{oid}/version-change-quote', role, expected,
            json={'target_version_id': target, 'compatibility_confirmed': confirmed})

    def validate(oid, token, role='admin', expected=200):
        return request('POST', f'/api/orders/{oid}/version-change-quote/validate', role, expected,
                       json={'quote_token': token})

    def amount(value, expected, name):
        check(Decimal(str(value)) == Decimal(expected), 'Unexpected ' + name)

    try:
        for method in METHODS:
            owned_orders = orders[method]
            oid = owned_orders[0]
            options_path = f'/api/orders/{oid}/version-change-options'
            for role in ('buyer', 'admin'):
                options = request('GET', options_path, role).json()
                check(options['delivery_method'] == method and options['current_version_id'] ==
                      ids['versions'][method]['source'], 'Options lost the source version/method')
                check({v['id'] for v in options['versions']} == {
                    ids['versions'][method][role] for role in ('up', 'down', 'cent')},
                    'Options omitted a ready target or included the source')
            for role in ('member', 'other'):
                request('GET', options_path, role, 403)
                quote(oid, ids['versions'][method]['up'], role, expected=403)
            breaking_http = 409 if method == 'tenant_access' else 200
            quote(oid, ids['versions'][method]['up'], confirmed=False, expected=breaking_http)
            # Literal expected segment amounts: no duplication of quote arithmetic.
            for target, action, net, payment, refund, current_delta, future_delta in (
                    ('up', 'upgrade', '125.00', '125.00', '0.00', '75.00', '50.00'),
                    ('down', 'downgrade', '-125.00', '0.00', '125.00', '-75.00', '-50.00'),
                    ('cent', 'upgrade', '125.03', '125.03', '0.00', '75.02', '50.01')):
                body = quote(oid, ids['versions'][method][target]).json()
                check(body['action'] == action and body['execution_ready'] is False,
                      'Read-only quote reported execution readiness or wrong action')
                amount(body['net_difference'], net, 'total difference')
                amount(body['payment_amount'], payment, 'payment amount')
                amount(body['refund_amount'], refund, 'refund amount')
                segments = body['segments']
                check(len(segments) == 2 and [s['order_id'] for s in segments] == owned_orders,
                      'Quote omitted/reordered current or future paid segment')
                amount(segments[0]['remaining_ratio'], '0.5', 'current remaining ratio')
                amount(segments[1]['remaining_ratio'], '1', 'future remaining ratio')
                amount(segments[0]['original_amount'], '300', 'current frozen price')
                amount(segments[1]['original_amount'], '100', 'future frozen price')
                amount(segments[0]['net_difference'], current_delta, 'current segment difference')
                amount(segments[1]['net_difference'], future_delta, 'future segment difference')
                claims = jwt.decode(body['quote_token'], options={'verify_signature': False})
                assert_hidden(claims)
                check(claims['sub'] == ids['users']['admin'] and claims['order_id'] == oid,
                      'Quote token lost user/source binding')
                check(validate(oid, body['quote_token']).json() == body,
                      'Validation changed the frozen quote')
                for role in ('member', 'other'):
                    validate(oid, body['quote_token'], role, 403)
                with patch.object(m, 'now', return_value=current + timedelta(seconds=301)):
                    validate(oid, body['quote_token'], expected=409)
                # A changed future source must invalidate a quote even if amounts
                # are identical. Restore every field so the final snapshot is exact.
                with m.SessionLocal() as db:
                    source = db.get(m.Order, owned_orders[1])
                    original = source.updated_at
                    db.execute(update(m.Order).where(m.Order.id == source.id).values(
                        updated_at=original + timedelta(seconds=1)))
                    db.commit()
                try:
                    validate(oid, body['quote_token'], expected=409)
                finally:
                    with m.SessionLocal() as db:
                        db.execute(update(m.Order).where(m.Order.id == owned_orders[1]).values(updated_at=original))
                        db.commit()
                check(validate(oid, body['quote_token']).json() == body, 'Restored source did not restore quote validity')
            # Keep the paid future order but detach its owned term temporarily.
            # All original rows/identities are restored before snapshot comparison.
            Term = m.subscription_terms['Term']
            from sqlalchemy import delete, select

            with m.SessionLocal() as db:
                term = db.scalar(select(Term).where(Term.order_id == owned_orders[1]))
                term_values = {column.name: getattr(term, column.name) for column in Term.__table__.columns}
                db.execute(delete(Term).where(Term.id == term.id))
                db.commit()
            try:
                request('GET', options_path, expected=409)
                quote(oid, ids['versions'][method]['up'], expected=409)
            finally:
                with m.SessionLocal() as db:
                    db.add(Term(**term_values))
                    db.commit()
            results[method] = {'upgrade': '125.00', 'downgrade_refund': '125.00',
                               'cent_upgrade': '125.03', 'expired_http': 409, 'changed_future_http': 409,
                               'paid_future_without_term_http': 409,
                               'breaking_compatibility_http': breaking_http}
        check(readonly_snapshot(m, ids) == before,
              'Quote/options/validate changed owned business rows or added order/payment/settlement/metering')
        return {'methods': results, 'quote_http': 'production TestClient routes with real PG sessions',
                'readonly_business_rows': True, 'cost_hidden_dto_and_decoded_jwt': True,
                'buyer_admin_allowed': True, 'member_other_http': 403,
                'breaking_compatibility_http': {method: result['breaking_compatibility_http'] for method, result in results.items()},
                'future_paid_segment_included': True}
    finally:
        client.close()


def preflight(m, ids):
    from sqlalchemy import func, or_, select

    with m.SessionLocal() as db:
        for model, values in ((m.User, list(ids['users'].values())), (m.Enterprise, ids['enterprises']),
                (m.Product, list(ids['products'].values())),
                (m.ProductReleaseVersion, [v for method in ('api', 'model_api', 'browser_api')
                                          for v in ids['versions'][method].values()]),
                (m.SaaSProductVersion, list(ids['versions']['tenant_access'].values()))):
            predicate = model.id.in_(values)
            if model is m.User:
                predicate = or_(predicate, model.email.in_([uid + '@example.invalid' for uid in values]))
            elif model is m.Enterprise:
                predicate = or_(predicate, model.credit_code.in_(values))
            check(db.scalar(select(func.count()).select_from(model).where(predicate)) == 0, 'Fixture collision')


def controlled_clock(anchor):
    from app.subscription_calendar import CalendarAnchor

    bounds = CalendarAnchor(anchor).term(3)
    return bounds.start + (bounds.end - bounds.start) / 2


def run(manifest_path=None, *, prepare_browser=False, base_url=None, deployment_tag=None):
    from contextlib import ExitStack
    from unittest.mock import patch

    if prepare_browser:
        url = urlsplit(base_url or '')
        check(url.scheme in {'http', 'https'} and url.hostname and not url.username
              and not url.password and not url.query and not url.fragment, 'Invalid browser base URL')
        check(bool(deployment_tag and deployment_tag.strip()), 'Browser deployment tag required')
    m = load_main()
    ids = fixture_ids()
    manifest = Path(manifest_path) if manifest_path else Path('/tmp') / (ids['run_id'] + '.json')
    browser = manifest.with_suffix('.browser.json')
    check(not browser.exists() and not browser.is_symlink(), 'Browser artifact path exists')
    preflight(m, ids)
    private_json(manifest, ids, create=True)
    real_now = m.now()
    anchor = real_now - timedelta(days=45)
    current = controlled_clock(anchor)
    result = {'status': 'failed', 'database': 'real PostgreSQL',
              'gateway_evidence': 'synthetic active/verified PG fixtures; no gateway integration verification',
              'payment_delivery': 'paid snapshots and trusted on_delivery_success fixtures; not live verification',
              'clock': 'controlled server time: current three-month term 50%, future one-month term 100%',
              'manifest': str(manifest), 'manifest_mode': '0600'}
    retained = False
    try:
        with ExitStack() as guards:
            # Fixture construction and quote routes must never provision anything.
            for name in ('apisix_admin_request', 'publish_apisix_route', 'execute_saas_operation',
                         'validated_http_request'):
                if hasattr(m, name):
                    guards.enter_context(patch.object(m, name, side_effect=AcceptanceFailure(
                        'Unexpected external gateway/provisioning call')))
            orders, tokens = prepare(m, ids, anchor, include_browser=prepare_browser)
            with patch.object(m, 'now', return_value=current):
                result.update(exercise(m, ids, orders, tokens, current))
        if prepare_browser:
            with m.SessionLocal() as db:
                order = db.get(m.Order, orders['browser_api'][0])
                live_entitlement = m.subscription_terms['current_entitlement'](
                    db, ids['enterprises'][0], order.product_id, at=m.now())
                check(live_entitlement is not None, 'Browser combination is not active at real server time')
                fixture = {'buyerAdminToken': tokens['admin'], 'expectedOrderNo': order.order_no,
                           'baseUrl': base_url.rstrip('/'), 'deploymentTag': deployment_tag}
                save_owned(m, db, ids, manifest, owned_rows(m, db, ids))
            private_json(browser, fixture, create=True)
            result['browser_fixture'] = str(browser)
            result['browser_fixture_mode'] = '0600'
            result['browser_clock'] = 'live server clock; numeric 50% assertions apply only to in-process acceptance'
            retained = True
        result['status'] = 'passed'
    except Exception as exc:
        result['error_type'] = type(exc).__name__
        if isinstance(exc, AcceptanceFailure):
            result['check_failed'] = str(exc)
    finally:
        if retained:
            result['cleanup'] = 'deferred; use --cleanup with this manifest'
        else:
            try:
                cleanup(m, ids, manifest)
                result['cleanup'] = 'verified exact owned DB rows and FK children absent'
            except Exception as exc:
                result['status'], result['cleanup_error_type'] = 'failed', type(exc).__name__
                if isinstance(exc, AcceptanceFailure):
                    result['cleanup_check_failed'] = str(exc)
    return result


def cleanup_saved(path):
    ids = read_scope(path)  # Validate local ownership before importing production.
    m = load_main()
    cleanup(m, ids, Path(path))
    browser = Path(path).with_suffix('.browser.json')
    if browser.exists() or browser.is_symlink():
        check(stat.S_ISREG(browser.lstat().st_mode), 'Browser artifact must be a regular file')
        browser.unlink()
    return {'status': 'passed', 'mode': 'cleanup', 'manifest': str(path),
            'cleanup': 'verified exact owned DB/FK rows absent; browser token removed'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--prepare-browser', action='store_true')
    modes.add_argument('--cleanup', action='store_true')
    parser.add_argument('--base-url')
    parser.add_argument('--deployment-tag')
    args = parser.parse_args()
    if args.cleanup and not args.manifest:
        parser.error('--cleanup requires --manifest')
    if args.prepare_browser and (not args.base_url or not args.deployment_tag):
        parser.error('--prepare-browser requires --base-url and --deployment-tag')
    logging.disable(logging.CRITICAL)
    try:
        result = cleanup_saved(args.manifest) if args.cleanup else run(args.manifest,
            prepare_browser=args.prepare_browser, base_url=args.base_url, deployment_tag=args.deployment_tag)
    except Exception as exc:
        result = {'status': 'failed', 'error_type': type(exc).__name__}
        if isinstance(exc, AcceptanceFailure):
            result['check_failed'] = str(exc)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
