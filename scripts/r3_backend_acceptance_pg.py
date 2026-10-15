"""BE001..004 focused deployed-handler acceptance in an owned PG schema.

Run inside the deployed API image: python -B r3_backend_acceptance_pg.py
DATABASE_URL is consumed privately. Never run startup/seed or import task data.
Only an unguessable qa_r3_be_* schema is created/dropped; public is SELECT-only.
HTTP uses the deployed ASGI app in-process, not ingress or an external SaaS.
Print sanitized case outcomes only, never response bodies or exception messages.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
from unittest.mock import patch


PROTECTED = 'ORD-261012005144E1B3'
INVALID_TYPES = [('report', 'file'), ('application', 'file'), ('unknown', 'file'),
                 ('model', 'model_api'), ('dataset', 'api'), ('api', 'file'),
                 ('saas', 'file'), ('training', 'consulting'),
                 ('consulting', 'custom'), ('custom', 'training')]
PLATFORM_ROLES = ['super_admin', 'platform_operator', 'finance_settlement',
                  'product_manager', 'quality_reviewer', 'security_compliance',
                  'delivery_monitor', 'ywsh']


class AcceptanceFailure(AssertionError):
    """Only labels constructed by this script may enter public evidence."""


def check(condition, label):
    if not condition:
        raise AcceptanceFailure(label)


def owned_schema(name):
    return bool(re.fullmatch(r'qa_r3_be_[0-9a-f]{16}', name))


def safe_error(exc):
    tb = exc.__traceback__
    while tb and tb.tb_next:
        tb = tb.tb_next
    result = {'status': 'failed', 'error_type': type(exc).__name__,
              'line': tb.tb_lineno if tb else None}
    if isinstance(exc, AcceptanceFailure):
        result['check'] = str(exc)
    return result


def seed(m):
    from sqlalchemy import select
    instant = m.now()
    roles = {r: r for r in PLATFORM_ROLES}
    roles.update(buyer='', provider='', member='', outsider='', personal='')
    with m.SessionLocal() as db:
        for eid in ('owned-buyer', 'owned-provider', 'owned-other'):
            db.add(m.Enterprise(id=eid, name=eid, credit_code=eid,
                                verification_status='verified'))
        for name, role in roles.items():
            db.add(m.User(id='owned-'+name, name='Owned acceptance actor',
                email=name+'@example.invalid', password_hash='unused-owned-fixture',
                platform_role=role, is_active=True, activation_status='active', verified_status='verified'))
        db.flush()
        for name, eid, role in [('buyer', 'buyer', 'enterprise_admin'),
                                ('provider', 'provider', 'super_admin'),
                                ('member', 'buyer', 'member'), ('outsider', 'other', 'super_admin')]:
            db.add(m.Membership(id='owned-membership-'+name, user_id='owned-'+name,
                enterprise_id='owned-'+eid, role=role, status='active'))
        for index in range(25):
            pid = f'owned-product-{index:02}'
            db.add(m.Product(id=pid, enterprise_id='owned-provider', name=pid,
                product_type='dataset', delivery_method='file', status='published', updated_at=instant))
        db.add(m.Product(id='owned-edit', enterprise_id='owned-provider', name='Owned editable',
                         product_type='dataset', delivery_method='file', status='draft'))
        for kind in ('report', 'application'):
            db.add(m.Product(id='owned-'+kind, enterprise_id='owned-provider', name='Owned legacy',
                product_type=kind, delivery_method='file', status='draft'))
        db.add(m.Product(id='owned-saas', enterprise_id='owned-provider', name='Owned SaaS',
            product_type='saas', delivery_method='tenant_access', status='published',
            application_url='https://owned.example.invalid/app',
            oidc_redirect_uris_json='["https://owned.example.invalid/callback"]'))
        db.flush()
        for index in range(25):
            pid = f'owned-product-{index:02}'
            db.add(m.ProductReleaseVersion(id=pid+'-v', product_id=pid, version_code='v1',
                                          price=10, cost=1, status='active'))
        db.add(m.SaaSProductVersion(id='owned-saas-v', product_id='owned-saas', name='Owned',
                                   version_code='v1', monthly_price=10, cost=1, status='active'))
        db.flush()
        economic = dict(schema_version=1, product_id='owned-saas', product_version_id='owned-saas-v',
            version_code='v1', billing_unit='month', subscription_months=1, unit_price='10',
            unit_cost='1', amount='10', total_cost='1', settlement_rule={'version': 'owned-v1',
            'rates': {k: 0 for k in m.order_economics['rate_keys']}})
        db.add(m.Order(id='owned-paid', order_no='OWNED-R3-PAID', buyer_enterprise_id='owned-buyer',
            provider_enterprise_id='owned-provider', buyer_user_id='owned-buyer', product_id='owned-saas',
            buyer_name='Owned buyer', product_name='Owned SaaS', amount=10, paid_amount=10,
            payment_status='paid', main_status='completed', delivery_status='accepted', snapshot_version=1,
            product_version_id='owned-saas-v', product_version_code='v1', subscription_months=1,
            unit_price_snapshot=10, unit_cost_snapshot=1, total_cost_snapshot=1,
            delivery_method_snapshot='tenant_access', economic_snapshot_json=json.dumps(economic),
            delivery_snapshot_json=json.dumps({'method': 'tenant_access', 'download_limit': 0,
                'quota': {k: 0 for k in ('daily_quota', 'monthly_quota', 'rate_limit_per_minute', 'quota_amount')}})))
        for kind in ('report', 'application'):
            db.add(m.Order(id='owned-legacy-'+kind, order_no='OWNED-LEGACY-'+kind,
                buyer_enterprise_id='owned-buyer', provider_enterprise_id='owned-provider',
                buyer_user_id='owned-buyer', product_id='owned-'+kind, buyer_name='Owned buyer',
                product_name='Owned legacy snapshot', delivery_method_snapshot='file', snapshot_version=0))
        db.flush()
        term = m.subscription_terms['on_delivery_success'](
            db, db.get(m.Order, 'owned-paid'), db.get(m.User, 'owned-super_admin'),
            at=instant-timedelta(minutes=1))
        db.add(m.SaaSSubscription(id='owned-sub', enterprise_id='owned-buyer', product_id='owned-saas',
            version_id='owned-saas-v', status='active', external_tenant_id='owned-tenant',
            starts_at=term.starts_at, expires_at=term.ends_at, created_by='owned-buyer'))
        db.flush()
        db.get(m.Order, 'owned-paid').subscription_id = 'owned-sub'
        db.add(m.SaaSCombinationBinding(id='owned-binding', enterprise_id='owned-buyer',
            product_id='owned-saas', subscription_id='owned-sub'))
        db.add(m.ApplicationAccessGrant(id='owned-grant', enterprise_id='owned-buyer',
            product_id='owned-saas', user_id='owned-member', granted_by='owned-buyer', status='active'))
        db.add(m.oidc_provider['Client'](id='owned-oidc', product_id='owned-saas',
            client_id='owned-client', secret_hash='0'*64, status='active'))
        for index in range(26):
            db.add(m.AuditLog(id=f'owned-audit-{index:02}', actor='owned@example.invalid', action='owned-test',
                target_type='owned_test', target_id='owned-common', category='owned-r3', created_at=instant,
                tenant_id='owned-other' if index == 25 else 'owned-buyer',
                detail='OWNED_PRIVATE_SENTINEL', before_json='{"secret":"OWNED_PRIVATE_SENTINEL"}'))
        Message, Receipt = (m.message_center[k] for k in ('Message', 'Receipt'))
        for index in range(25):
            mid = f'owned-message-{index:02}'
            db.add(Message(id=mid, event_key=mid, title='Owned R3', category='owned-r3',
                sender_id='owned-buyer', tenant_id='owned-buyer', status='sent', sent_at=instant,
                expires_at=instant+timedelta(days=1)))
        db.flush()
        for index in range(25):
            db.add(Receipt(id=f'owned-receipt-{index:02}', message_id=f'owned-message-{index:02}',
                           user_id='owned-buyer', created_at=instant))
        db.commit()
        return {name: {'Authorization': 'Bearer '+m.issue_token(db.get(m.User, 'owned-'+name))}
                for name in roles}


def exercise(m, client, headers):
    from sqlalchemy import select, func
    cases = {}

    def call(method, path, actor='buyer', expected=200, **kwargs):
        response = client.request(method, path, headers=headers[actor], **kwargs)
        check(response.status_code == expected,
              f'{method} {path} actor={actor} expected={expected} actual={response.status_code}')
        return response

    def case(name, fn):
        try:
            cases[name] = {'status': 'passed', **(fn() or {})}
        except Exception as exc:
            cases[name] = safe_error(exc)
        return cases[name]

    def dev():
        call('GET', '/api/development/tasks', 'super_admin')
        call('PATCH', '/api/development/tasks/owned-missing', 'super_admin', 404, json={'progress': 50})
        denied = [role for role in headers if role != 'super_admin']
        for actor in denied:
            call('GET', '/api/development/tasks', actor, 403)
            call('PATCH', '/api/development/tasks/owned-missing', actor, 403, json={'progress': 50})
        return {'nonadmin_actors': denied, 'nonadmin_denials': 2*len(denied),
                'admin_get': 200, 'admin_missing_patch': 404, 'task_states_changed': 0}
    case('BE001_get_patch_role_matrix', dev)
    paths = {r.path for r in m.app.routes}
    cases['BE001_detail_export_sse_contract'] = {'status': 'gap',
        'reason': 'No dedicated development detail/export/SSE route found; generic notification SSE not a development endpoint',
        'registered_development_paths': sorted(p for p in paths if p.startswith('/api/development'))}

    def types():
        def state():
            with m.SessionLocal() as db:
                return (db.scalar(select(func.count()).select_from(m.Product)),
                        dict(db.execute(select(m.Product.__table__).where(m.Product.id == 'owned-edit')).mappings().one()))
        before = state()
        for kind, delivery in INVALID_TYPES:
            for method, path in [('POST', '/api/products'), ('PUT', '/api/products/owned-edit')]:
                call(method, path, 'provider', 400, params={'enterprise_id': 'owned-provider'},
                     json={'name': 'Owned invalid', 'product_type': kind, 'delivery_method': delivery})
        check(state() == before, 'Invalid payload changed owned product rows')
        for kind in ('report', 'application'):
            response = call('GET', '/api/products/owned-'+kind, 'provider')
            check(response.json()['product_type'] == kind, 'Legacy product is not readable')
            order = call('GET', '/api/orders/owned-legacy-'+kind, 'buyer').json()
            check(order['order']['delivery_method'] == 'file', 'Legacy order snapshot is not readable')
        return {'invalid_payloads_rejected': 20, 'unchanged_owned_products': True,
                'legacy_product_reads': 2, 'legacy_order_snapshot_reads': 2}
    case('BE002_invalid_registration_and_legacy_reads', types)

    def paginate():
        surfaces = [('/api/storefront/products', {}, None, 'owned-product-', False),
            ('/api/audit-logs', {'enterprise_id': 'owned-buyer', 'category': 'owned-r3'}, 'buyer', 'owned-audit-', True),
            ('/api/audit-logs/owned-audit-00/timeline', {'enterprise_id': 'owned-buyer'}, 'buyer', 'owned-audit-', False),
            ('/api/notifications', {'enterprise_id': 'owned-buyer', 'category': 'owned-r3'}, 'buyer', 'owned-receipt-', True),
            ('/api/notifications/sent', {'enterprise_id': 'owned-buyer', 'category': 'owned-r3'}, 'buyer', 'owned-message-', True)]
        count = 0
        for path, params, actor, prefix, reverse in surfaces:
            ids = [f'{prefix}{i:02}' for i in range(25)]
            if reverse:
                ids.reverse()
            for size in (10, 20, 50, 100, 12):
                seen = []
                for page in range(1, (25+size-1)//size+2):
                    data = call('GET', path, actor or 'super_admin', params=dict(params, page=page, page_size=size)).json()
                    check(data['total'] == 25, 'Filtered total mismatch')
                    got = [row['id'] for row in data['items']]
                    check(got == ids[(page-1)*size:page*size], 'Unstable page ordering or isolation')
                    seen.extend(got)
                    count += 1
                check(seen == ids, 'Pagination duplicates or omissions')
                repeat = call('GET', path, actor or 'super_admin', params=dict(params, page=1, page_size=size)).json()
                check([row['id'] for row in repeat['items']] == ids[:size], 'Repeat page unstable')
            for bad in ({'page': 0}, {'page': -1}, {'page': 'bad'}, {'page_size': 0},
                        {'page_size': 101}, {'page_size': 'bad'}):
                call('GET', path, actor or 'super_admin', 422, params={**params, **bad})
        return {'surfaces': [row[0] for row in surfaces], 'pages_checked': count,
                'sizes': [10, 20, 50, 100, 12], 'invalid_parameter_denials': 30,
                'tie_timestamp_fixture_rows_per_surface': 25}
    case('BE003_stable_pages_and_parameter_bounds', paginate)

    def audit_scope():
        expected = {'buyer': (200, 25), 'provider': (200, 0), 'outsider': (200, 1),
                    'member': (403, None), 'personal': (403, None)}
        for actor, (status, total) in expected.items():
            eid = {'buyer': 'buyer', 'member': 'buyer', 'provider': 'provider', 'outsider': 'other'}.get(actor)
            params = {'category': 'owned-r3', **({'enterprise_id': 'owned-'+eid} if eid else {})}
            r = call('GET', '/api/audit-logs', actor, status, params=params)
            if total is not None:
                check(r.json()['total'] == total, 'Enterprise audit total leaked')
                check('OWNED_PRIVATE_SENTINEL' not in r.text, 'Enterprise audit private fields leaked')
        for actor in PLATFORM_ROLES:
            status = 200 if actor in {'super_admin', 'platform_operator', 'finance_settlement', 'security_compliance'} else 403
            r = call('GET', '/api/audit-logs', actor, status, params={'category': 'owned-r3'})
            if status == 200:
                check(r.json()['total'] == 26, 'Platform audit scope mismatch')
        call('GET', '/api/audit-logs', 'buyer', 403, params={'enterprise_id': 'owned-other'})
        call('GET', '/api/audit-logs/owned-audit-25/timeline', 'buyer', 404)
        return {'enterprise_admin_member_personal_matrix': True, 'platform_role_matrix': True,
                'cross_enterprise_denied': True, 'foreign_timeline_denied': True}
    case('BE003_audit_actor_and_enterprise_isolation', audit_scope)

    def apps():
        def listing(available):
            data = call('GET', '/api/applications', 'member', params={'enterprise_id': 'owned-buyer'}).json()
            check(len(data['items']) == 1 and data['items'][0]['available'] is available,
                  'Member application availability mismatch')
            check(not any(k in json.dumps(data).lower() for k in ('secret', 'access_token', 'password')),
                  'MyApps includes secret fields')
        listing(True)
        call('GET', '/api/applications', 'provider', params={'enterprise_id': 'owned-provider'})
        call('GET', '/api/applications', 'member', 403, params={'enterprise_id': 'owned-other'})
        for actor in PLATFORM_ROLES:
            call('GET', '/api/applications', actor, 403)
        verifier = 'V'*64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        def authorize(expected=200):
            response = call('POST', '/oidc/authorize', 'member', expected, json={
                'client_id': 'owned-client', 'redirect_uri': 'https://owned.example.invalid/callback',
                'scope': 'openid', 'state': secrets.token_urlsafe(24), 'nonce': secrets.token_urlsafe(24),
                'code_challenge': challenge, 'code_challenge_method': 'S256', 'enterprise_id': 'owned-buyer'})
            if expected == 200:
                check('code=' in response.json()['redirect_url'], 'No authorization code issued')
        authorize()
        negatives = []
        for model, key, field, bad in [(m.ApplicationAccessGrant, 'owned-grant', 'status', 'revoked'),
                (m.Membership, 'owned-membership-member', 'status', 'disabled'),
                (m.Membership, 'owned-membership-member', 'status', 'removed'),
                (m.User, 'owned-member', 'is_active', False),
                (m.SaaSSubscription, 'owned-sub', 'expires_at', m.now()-timedelta(seconds=1))]:
            with m.SessionLocal() as db:
                row = db.get(model, key); old = getattr(row, field); setattr(row, field, bad); db.commit()
            try:
                denial = 401 if model == m.User else 403
                authorize(denial)
                if model == m.SaaSSubscription:
                    listing(False)
                elif model == m.User or model == m.Membership:
                    call('GET', '/api/applications', 'member', denial, params={'enterprise_id': 'owned-buyer'})
                else:
                    data = call('GET', '/api/applications', 'member', params={'enterprise_id': 'owned-buyer'}).json()
                    check(data['items'] == [], 'Revoked grant still listed')
                negatives.append({'model': model.__tablename__, 'field': field,
                    'scenario': 'disabled' if model == m.User else bad if isinstance(bad, str) else 'expired',
                    'authorization_status': denial})
            finally:
                with m.SessionLocal() as db:
                    setattr(db.get(model, key), field, old); db.commit()
        return {'nonpurchaser_member_available': True, 'authorization_code_issued': True,
                'negative_scenarios': negatives, 'myapps_secret_fields_absent': True,
                'external_saas_browser_login': 'not tested', 'token_exchange_and_existing_sessions': 'not tested'}
    case('BE004_member_myapps_and_oidc_eligibility', apps)
    return cases


def run():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.schema import CreateSchema, DropSchema
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient
    url = make_url(os.environ['DATABASE_URL'])
    check(url.get_backend_name() == 'postgresql', 'Real PostgreSQL required')
    check('app.main' not in sys.modules, 'Fresh process required before application import')
    schema = 'qa_r3_be_'+secrets.token_hex(8)
    check(owned_schema(schema), 'Invalid owned schema')
    control = create_engine(url, hide_parameters=True, connect_args={'connect_timeout': 5})
    m = None; client = None; created = False; result = {'status': 'failed'}
    captured = datetime.now(timezone.utc).isoformat()
    def protected_count():
        with control.connect() as c:
            c.exec_driver_sql('SET TRANSACTION READ ONLY')
            c.exec_driver_sql("SET LOCAL statement_timeout = '4000ms'")
            return c.scalar(text('SELECT COUNT(*) FROM public.orders WHERE order_no=:number'), {'number': PROTECTED})
    before = protected_count()
    try:
        with control.begin() as c:
            c.execute(CreateSchema(schema))
        created = True
        query = dict(url.query)
        query['options'] = '-csearch_path='+schema+' -cstatement_timeout=5000 -clock_timeout=2000'
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                'DATABASE_URL': url.set(query=query).render_as_string(hide_password=False),
                'MESSAGE_CENTER_DISABLE_BACKGROUND': 'true', 'PYTHONDONTWRITEBYTECODE': '1',
                'OIDC_ISSUER': 'https://owned-platform.example.invalid', 'OIDC_ALLOW_HTTP_DEVELOPMENT': 'false',
                'OIDC_SIGNING_KEY_PEM': key}))
            from app import main as m
            with m.engine.connect() as c:
                check(c.scalar(text('SELECT current_schema()')) == schema, 'Engine escaped owned schema')
                check(c.scalar(text('SHOW search_path')) == schema, 'Search path includes public')
            m.Base.metadata.create_all(m.engine)
            for name in ('request', 'get', 'post'):
                stack.enter_context(patch.object(m.httpx, name, side_effect=AssertionError('External HTTP forbidden')))
            stack.enter_context(patch.object(m.redis.Redis, 'from_url', side_effect=AssertionError('Redis forbidden')))
            stack.enter_context(patch.object(m, 'Minio', side_effect=AssertionError('Object storage forbidden')))
            for name in ('SMTP', 'SMTP_SSL'):
                stack.enter_context(patch.object(m.smtplib, name, side_effect=AssertionError('Mail forbidden')))
            headers = seed(m)
            client = TestClient(m.app, raise_server_exceptions=False)
            cases = exercise(m, client, headers)
            result = {'status': 'failed' if any(c['status'] == 'failed' for c in cases.values()) else 'partial',
                'captured_at': captured, 'completed_at': datetime.now(timezone.utc).isoformat(),
                'scope': 'deployed ASGI handlers with actual PostgreSQL owned schema; not ingress',
                'owned_schema': schema, 'cases': cases,
                'gaps': ['BE001 dedicated detail/export/SSE contract absent',
                         'BE003 five existing paginated surfaces only, not all console pages',
                         'BE004 external SaaS browser login, token exchange and existing-session revocation not covered'],
                'public_write_operations': 0, 'task_status_changes': 0,
                'fixture_note': 'Only owned schema; no live business or production secret rows read. Ephemeral signing key generated locally.'}
    except Exception as exc:
        result = {'status': 'failed', 'captured_at': captured, 'setup_or_execution_error': safe_error(exc)}
    finally:
        if client:
            client.close()
        if m:
            m.engine.dispose()
        if created:
            check(owned_schema(schema), 'Refusing cleanup outside owned schema')
            with control.begin() as c:
                c.execute(DropSchema(schema, cascade=True))
            with control.connect() as c:
                check(c.scalar(text('SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=:s'),
                               {'s': schema}) == 0, 'Owned schema remains')
        result['cleanup'] = 'owned schema removed' if created else 'no schema created'
        result['protected_order_count_before'] = before
        result['protected_order_count_after'] = protected_count()
        result['protected_order_content_read'] = False
        control.dispose()
    return result


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(run(), ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': safe_error(exc)}))
