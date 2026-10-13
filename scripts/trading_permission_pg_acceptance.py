"""Bounded permission acceptance against production handlers in owned PostgreSQL schema."""
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import sys

from sqlalchemy import MetaData, Table, select, text, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema


PROTECTED_ORDER = 'ORD-261012005144E1B3'
FORBIDDEN_KEYS = {'cost', 'unit_cost', 'total_cost', 'provider_cost', 'cost_amount',
                  'profit_amount', 'unit_cost_snapshot', 'total_cost_snapshot', 'secret',
                  'secret_encrypted', 'client_secret', 'secret_key', 'password',
                  'api_key', 'token', 'access_token', 'upstream_url', 'integration_api_url'}
SAFE_SECRET_METADATA_KEYS = {'secret_download_available'}
CASES = (
    'anonymous_public_detail_list_export_privacy', 'buyer_provider_order_visibility_and_cost_privacy',
    'selected_enterprise_isolation', 'order_transition_role_matrix',
    'file_and_api_credential_permissions', 'disabled_and_downgraded_immediate_denial',
    'application_listing_is_member_scoped',
)


def check(value, message):
    if not value:
        raise AssertionError(message)


def protected_fingerprint(engine):
    metadata = MetaData()
    orders = Table('orders', metadata, schema='public', autoload_with=engine, resolve_fks=False)
    products = Table('products', metadata, schema='public', autoload_with=engine, resolve_fks=False)
    with engine.connect() as connection:
        order = connection.execute(select(orders).where(
            (orders.c.id == PROTECTED_ORDER) | (orders.c.order_no == PROTECTED_ORDER)
        )).mappings().first()
        product = connection.execute(select(products).where(
            products.c.id == order['product_id'])).mappings().first() if order else None
    return hashlib.sha256(json.dumps([dict(order) if order else None,
        dict(product) if product else None], sort_keys=True, default=str).encode()).hexdigest()


def assert_no_forbidden_keys(value, label='response'):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.lower()
            if normalized in SAFE_SECRET_METADATA_KEYS:
                check(isinstance(child, bool), label + ' has non-boolean safe secret metadata')
            else:
                check(normalized not in FORBIDDEN_KEYS and not any(part in normalized for part in
                    ('cost', 'profit', 'secret', 'token', 'upstream')), label + ' contains forbidden field')
            assert_no_forbidden_keys(child, label)
    elif isinstance(value, list):
        for child in value:
            assert_no_forbidden_keys(child, label)


def run():
    original = os.environ['DATABASE_URL']
    url = make_url(original)
    check(url.get_backend_name() == 'postgresql', 'PostgreSQL runtime required')
    admin = create_engine(url, pool_pre_ping=True)
    before = protected_fingerprint(admin)
    schema = 'qa_perm_' + secrets.token_hex(8)
    created = False
    m = None
    client = None
    outcomes = {name: {'status': 'not_run'} for name in CASES}
    try:
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        query = dict(url.query)
        query['options'] = (str(query.get('options', '')) + ' -csearch_path=' + schema).strip()
        os.environ['DATABASE_URL'] = url.set(query=query).render_as_string(hide_password=False)
        os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
        from app import main as m
        with m.engine.connect() as connection:
            check(connection.scalar(text('SELECT current_schema()')) == schema,
                  'Application engine escaped owned schema')
        m.Base.metadata.create_all(m.engine)
        from fastapi.testclient import TestClient
        client = TestClient(m.app)
        fixtures = seed(m)

        def case(name, fn):
            try:
                details = fn()
                status = (details or {}).pop('status', 'passed')
                outcomes[name] = {'status': status, **(details or {})}
            except Exception as exc:
                tb = exc.__traceback__
                while tb and tb.tb_next:
                    tb = tb.tb_next
                outcomes[name] = {'status': 'failed', 'error_type': type(exc).__name__,
                                  'location': f'{Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}' if tb else 'unknown'}

        case('anonymous_public_detail_list_export_privacy', lambda: anonymous_cases(client, fixtures))
        case('buyer_provider_order_visibility_and_cost_privacy', lambda: order_privacy(client, m, fixtures))
        case('selected_enterprise_isolation', lambda: selected_enterprise(client, m, fixtures))
        case('order_transition_role_matrix', lambda: transition_cases(client, m, fixtures))
        case('file_and_api_credential_permissions', lambda: file_credential_cases(client, m, fixtures))
        case('disabled_and_downgraded_immediate_denial', lambda: immediate_denial(client, m, fixtures))
        case('application_listing_is_member_scoped', lambda: application_cases(client, m, fixtures))
    finally:
        os.environ['DATABASE_URL'] = original
        if client:
            client.close()
        if m:
            m.engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        with admin.connect() as connection:
            check(connection.scalar(text('SELECT count(*) FROM information_schema.schemata '
                'WHERE schema_name=:schema'), {'schema': schema}) == 0, 'Owned schema remains')
        after = protected_fingerprint(admin)
        admin.dispose()
        check(before == after, 'Protected public order or product changed')
    failed = [name for name, result in outcomes.items() if result['status'] == 'failed']
    partial = [name for name, result in outcomes.items() if result['status'] == 'partial']
    return {'status': 'failed' if failed else 'partial' if partial else 'passed', 'scope': 'production HTTP handlers; private real PostgreSQL schema',
            'cases': outcomes, 'not_tested': ['external browser SSO protocol',
            'APISIX or real gateway credential provisioning; credential fixture is database-only',
            'verified_status downgrade as a separate mutation (membership role downgrade uses the same token)'],
            'cleanup': 'owned schema dropped; protected public order and product unchanged'}


def seed(m):
    ids = {name: 'qa-' + name for name in ('buyerco', 'providerco', 'otherco', 'super', 'admin', 'member', 'other', 'prov',
        'platform', 'finance', 'operator', 'disabled', 'down', 'product', 'paid', 'unpaid', 'file', 'credential',
        'public_offline', 'offline_version', 'buyer2co', 'unverifiedco')}
    with m.SessionLocal() as db:
        check(db.scalar(text('SELECT current_schema()')).startswith('qa_perm_'), 'Fixture session escaped owned schema')
        for key in ('buyerco', 'providerco', 'otherco', 'buyer2co', 'unverifiedco'):
            db.add(m.Enterprise(id=ids[key], name=ids[key], credit_code=ids[key],
                verification_status='pending' if key == 'unverifiedco' else 'verified'))
        roles = {'super': '', 'admin': '', 'member': '', 'other': '', 'prov': '', 'platform': 'super_admin',
                 'finance': 'finance_settlement', 'operator': 'platform_operator', 'disabled': '', 'down': ''}
        for key, platform_role in roles.items():
            db.add(m.User(id=ids[key], name=key, email=key+'@example.invalid', password_hash='fixture-only',
                verified_status='verified', activation_status='active', platform_role=platform_role,
                is_active=key != 'disabled'))
        db.flush()
        for key, enterprise, role in [('super','buyerco','super_admin'), ('admin','buyerco','enterprise_admin'),
                ('member','buyerco','member'), ('other','otherco','super_admin'), ('disabled','buyerco','enterprise_admin'),
                ('down','buyerco','member'), ('prov','providerco','super_admin'),
                ('super','buyer2co','enterprise_admin'), ('super','unverifiedco','super_admin')]:
            db.add(m.Membership(user_id=ids[key], enterprise_id=ids[enterprise], role=role,
                                status='active', business_roles='buyer,provider,user'))
        product = m.Product(id=ids['product'], enterprise_id=ids['providerco'], name='permission fixture',
            product_type='api', delivery_method='api', status='published', upstream_url='https://private.invalid/path',
            integration_api_url='https://private.invalid/manage', price=120, settlement_rule_json='{}')
        db.add(product)
        db.flush()
        version = m.ProductReleaseVersion(id='qa-version', product_id=product.id, version_code='v1',
            price=120, cost=71, status='active')
        db.add(version)
        db.flush()
        public_offline = m.Product(id=ids['public_offline'], enterprise_id=ids['providerco'],
            name='Verified public training fixture', product_type='training', delivery_method='training',
            status='published', description='Publicly purchasable offline training acceptance fixture',
            upstream_url='https://private.invalid/upstream-secret',
            integration_api_url='https://private.invalid/private-admin',
            settlement_rule_mode='custom', settlement_rule_json=json.dumps({'platform_rate':10,
                'provider_rate':90,'service_rate':0,'expert_rate':0,'channel_rate':0}))
        db.add(public_offline)
        db.flush()
        db.add(m.ProductReleaseVersion(id=ids['offline_version'], product_id=public_offline.id,
            version_code='public-v1', description='Available offline training', price=120, cost=71,
            status='active'))
        route = m.ApiGatewayRoute(product_id=product.id, route_key='qa-route', version='v1',
            upstream_url='https://private.invalid/gateway', status='active')
        db.add(route)
        db.flush()
        db.add(m.ApiCredential(id=ids['credential'], route_id=route.id, enterprise_id=ids['buyerco'],
            order_id='', product_version_id=version.id, name='fixture credential', key_prefix='mk_fixture',
            key_hash='b' * 64, secret_encrypted='fixture-ciphertext-not-a-real-credential',
            status='active', apisix_consumer_name=''))
        saas = m.Product(id='qa-saas', enterprise_id=ids['providerco'], name='application fixture',
            product_type='saas', delivery_method='tenant_access', status='published',
            application_url='https://app.example.invalid')
        db.add(saas)
        db.flush()
        saas_version = m.SaaSProductVersion(id='qa-saas-v1', product_id=saas.id,
            version_code='v1', name='fixture', monthly_price=10, cost=2, status='active')
        db.add(saas_version)
        db.flush()
        subscription = m.SaaSSubscription(id='qa-subscription', enterprise_id=ids['buyerco'],
            product_id=saas.id, version_id=saas_version.id, status='active',
            external_tenant_id='qa-tenant', starts_at=m.now(), expires_at=m.now()+__import__('datetime').timedelta(days=30))
        db.add(subscription)
        db.add(m.ApplicationAccessGrant(enterprise_id=ids['buyerco'], product_id=saas.id,
            user_id=ids['member'], granted_by=ids['admin'], status='active'))
        db.add(m.FileObject(id=ids['file'], owner_id=ids['super'], product_id=product.id,
            version_id=version.id,
            object_name='qa-private-object', original_name='private-fixture.bin',
            content_type='application/octet-stream', size=8, checksum='a' * 64,
            file_role='product_data', version='v1', status='active', scan_status='clean'))
        economics = json.dumps({'schema_version':1,'product_id':product.id,'product_version_id':version.id,
            'version_code':'v1','billing_unit':'order','subscription_months':1,'unit_price':'120.00',
            'unit_cost':'71.00','amount':'120.00','total_cost':'71.00','currency':'CNY',
            'settlement_rule':{'mode':'custom','id':'','version':'fixture','rates':{'platform_rate':'10',
            'provider_rate':'90','service_rate':'0','expert_rate':'0','channel_rate':'0'}}})
        def order(key, payment):
            order_product = public_offline if key == 'unpaid' else product
            order_version_id = ids['offline_version'] if key == 'unpaid' else version.id
            return m.Order(id=ids[key], order_no='QA-'+key.upper(), buyer_enterprise_id=ids['buyerco'],
                buyer_user_id=ids['super'], provider_enterprise_id=ids['providerco'], product_id=order_product.id,
                main_status='created' if payment == 'unpaid' else 'completed', payment_status=payment,
                delivery_status='accepted' if payment == 'paid' else 'not_started', amount=120,
                paid_amount=120 if payment == 'paid' else 0, buyer_name='buyerco', product_name=order_product.name,
                snapshot_version=0, delivery_method_snapshot=order_product.delivery_method, economic_snapshot_json=economics,
                delivery_snapshot_json=json.dumps({'method':'api','category':'online','quota':{
                    'rate_limit_per_minute':60,'daily_quota':100,'monthly_quota':1000,'quota_amount':1000},
                    'download_limit':0,'files':[]}), product_version_id=order_version_id,
                product_version_code='public-v1' if key == 'unpaid' else 'v1')
        paid_order, unpaid_order = order('paid','paid'), order('unpaid','unpaid')
        db.add_all([paid_order, unpaid_order])
        db.flush()
        db.add_all([m.Payment(order_id=paid_order.id, payment_no='QA-PAY-PAID', amount=120, status='paid'),
                    m.Payment(order_id=unpaid_order.id, payment_no='QA-PAY-UNPAID', amount=120, status='unpaid')])
        db.commit()
    return ids


def token(m, user_id):
    with m.SessionLocal() as db:
        return {'Authorization': 'Bearer ' + m.issue_token(db.get(m.User, user_id))}


def anonymous_cases(client, f):
    detail = client.get('/api/storefront/products/'+f['public_offline'])
    check(detail.status_code == 200, 'Valid published offline product is not publicly visible')
    public = detail.json()
    check(public['id'] == f['public_offline'] and public['delivery_method'] == 'training'
          and public['versions'] and public['minimum_price'] == 120,
          'Public detail is not the valid priced offline fixture')
    assert_no_forbidden_keys(public, 'public offline detail')
    listing = client.get('/api/storefront/products')
    check(listing.status_code == 200, 'Public storefront product list did not return 200')
    assert_no_forbidden_keys(listing.json(), 'public storefront list')
    listed = next((item for item in listing.json()['items'] if item['id'] == f['public_offline']), None)
    check(listed is not None and listed['delivery_method'] == 'training',
          'Valid offline fixture is absent from public storefront list')
    assert_no_forbidden_keys(listed, 'public offline list item')
    paths = ['/api/orders/'+f['paid'], '/api/orders', '/api/settlement-reports/export']
    denied = []
    for path in paths:
        response = client.get(path)
        check(response.status_code in (401, 403), 'Anonymous private order/export route was not denied')
        denied.append({'path':path, 'status':response.status_code})
    check('private.invalid' not in detail.text and '71' not in json.dumps(public),
          'Public storefront exposed upstream details or fixture cost')
    return {'offline_detail_status':detail.status_code, 'offline_list_status':listing.status_code,
            'offline_item_listed':True, 'anonymous_private_routes_denied':denied}


def order_privacy(client, m, f):
    buyer = token(m, f['super']); provider = token(m, f['prov'])
    detail = client.get('/api/orders/'+f['paid'], headers=buyer)
    check(detail.status_code == 200, 'Buyer cannot read own order')
    assert_no_forbidden_keys(detail.json(), 'buyer order detail')
    check('cost' not in detail.text.lower() and 'FIXTURE_SENTINEL' not in detail.text,
          'Buyer order detail disclosed snapshot cost/secret')
    lst = client.get('/api/orders', headers=buyer)
    check(lst.status_code == 200 and all('cost' not in item for item in lst.json()['items']),
          'Buyer order list disclosed cost')
    assert_no_forbidden_keys(lst.json(), 'buyer order list')
    foreign = client.get('/api/orders/'+f['paid'], headers=token(m, f['other']))
    check(foreign.status_code in (403,404), 'Foreign enterprise accessed order')
    buyer_export = client.get('/api/settlement-reports/export', headers=buyer)
    check(buyer_export.status_code == 403, 'Buyer accessed platform settlement export')
    finance_export = client.get('/api/settlement-reports/export', headers=token(m, f['finance']))
    check(finance_export.status_code == 200, 'Finance role could not export authorized settlement report')
    check('https://private.invalid' not in finance_export.text and 'secret' not in finance_export.text.lower(),
          'Settlement export disclosed upstream or credential data')
    provider_detail = client.get('/api/orders/'+f['paid'], headers=provider)
    check(provider_detail.status_code == 200, 'Provider enterprise admin cannot read own-side order')
    assert_no_forbidden_keys(provider_detail.json(), 'provider order detail')
    check('cost' not in provider_detail.text.lower() and 'FIXTURE_SENTINEL' not in provider_detail.text,
          'Provider order detail disclosed cost/secret')
    return {'buyer_detail': detail.status_code, 'buyer_list': lst.status_code,
            'provider_detail': provider_detail.status_code, 'foreign_detail': foreign.status_code,
            'buyer_export': buyer_export.status_code, 'finance_export': finance_export.status_code}


def selected_enterprise(client, m, f):
    h = token(m, f['super'])
    memberships = client.get('/api/storefront/buyer-enterprises', headers=h)
    check(memberships.status_code == 200, 'Buyer enterprise picker did not return 200')
    assert_no_forbidden_keys(memberships.json(), 'buyer enterprise choices')
    choices = {item['id']:item for item in memberships.json()['items']}
    check(f['buyerco'] in choices and f['buyer2co'] in choices,
          'Multi-enterprise user does not receive both eligible buyer enterprise choices')
    check(choices[f['buyerco']]['eligible'] and choices[f['buyer2co']]['eligible'],
          'Verified active memberships are not eligible as buyer enterprises')
    quotes = []
    for enterprise_id in (f['buyerco'], f['buyer2co']):
        response = client.post('/api/orders/quote', headers=h, json={
            'product_id':f['public_offline'], 'product_version_id':f['offline_version'],
            'buyer_enterprise_id':enterprise_id, 'subscription_months':1})
        check(response.status_code == 200, 'Order quote rejected selected buyer enterprise')
        assert_no_forbidden_keys(response.json(), 'selected enterprise quote')
        quotes.append(response.json())
    check(quotes[0]['quote_id'] != quotes[1]['quote_id'],
          'Quote identity did not bind to submitted buyer_enterprise_id')
    check(all(item['delivery_method'] == 'training' for item in quotes),
          'Selected-enterprise quote did not use the offline product')
    foreign = client.post('/api/orders/quote', headers=token(m, f['other']), json={
        'product_id':f['public_offline'], 'product_version_id':f['offline_version'],
        'buyer_enterprise_id':f['buyerco'], 'subscription_months':1})
    check(foreign.status_code == 403, 'Foreign buyer enterprise quote was not forbidden')
    created = []
    for enterprise_id, quote in zip((f['buyerco'], f['buyer2co']), quotes):
        response = client.post('/api/orders', headers=h, json={
            'product_id':f['public_offline'], 'product_version_id':f['offline_version'],
            'buyer_enterprise_id':enterprise_id, 'subscription_months':1,
            'quote_id':quote['quote_id']})
        check(response.status_code == 200, 'Authorized quoted offline checkout was rejected')
        payload = response.json()
        check(payload['buyer_enterprise_id'] == enterprise_id,
              'Checkout response does not reflect its selected buyer enterprise')
        with m.SessionLocal() as db:
            order = db.get(m.Order, payload['id'])
            check(order is not None and order.buyer_enterprise_id == enterprise_id,
                  'Persisted checkout is not bound to its selected buyer enterprise')
            check(order.delivery_method_snapshot == 'training', 'Checkout used a non-offline product')
        created.append({'order_id':payload['id'], 'buyer_enterprise_id':enterprise_id})
    member_checkout = client.post('/api/orders', headers=token(m, f['member']), json={
        'product_id':f['public_offline'], 'product_version_id':f['offline_version'],
        'buyer_enterprise_id':f['buyerco'], 'subscription_months':1})
    check(member_checkout.status_code == 403, 'Ordinary member checkout was not forbidden')
    unverified_checkout = client.post('/api/orders', headers=h, json={
        'product_id':f['public_offline'], 'product_version_id':f['offline_version'],
        'buyer_enterprise_id':f['unverifiedco'], 'subscription_months':1})
    check(unverified_checkout.status_code == 403, 'Unverified enterprise checkout was not forbidden')
    anonymous_checkout = client.post('/api/orders', json={
        'product_id':f['public_offline'], 'product_version_id':f['offline_version'],
        'buyer_enterprise_id':f['buyerco'], 'subscription_months':1})
    check(anonymous_checkout.status_code == 401, 'Anonymous checkout was not forbidden')
    return {'buyer_enterprises_status':memberships.status_code,
            'eligible_enterprise_ids':[f['buyerco'],f['buyer2co']],
            'post_quote_statuses':[200,200], 'foreign_buyer_quote_status':foreign.status_code,
            'checkout_orders':created, 'ordinary_member_checkout_status':member_checkout.status_code,
            'unverified_enterprise_checkout_status':unverified_checkout.status_code,
            'anonymous_checkout_status':anonymous_checkout.status_code}


def transition_cases(client, m, f):
    url = '/api/orders/'+f['unpaid']+'/transition'
    def send(role, action):
        return client.post(url, headers=token(m,f[role]), json={'action':action})
    enterprise_admin = send('admin','start_payment')
    check(enterprise_admin.status_code == 403, 'Enterprise admin unexpectedly initiated buyer payment')
    started = send('super','start_payment')
    check(started.status_code == 200, 'Buyer super-admin could not start payment')
    confirmed = send('finance','confirm_payment')
    check(confirmed.status_code == 200, 'Finance settlement could not confirm payment')
    with m.SessionLocal() as db:
        order = db.get(m.Order, f['unpaid'])
        check(order.delivery_method_snapshot == 'training',
              'Payment role matrix order is not an offline product order')
    return {'payment_fixture_delivery_method':'training',
            'enterprise_admin_start_payment': enterprise_admin.status_code,
            'buyer_super_admin_start_payment': started.status_code,
            'finance_confirm_payment': confirmed.status_code}


def file_credential_cases(client, m, f):
    checks = []
    owner = client.get('/api/orders/'+f['paid']+'/api-credentials', headers=token(m,f['super']))
    check(owner.status_code == 200 and len(owner.json().get('items', [])) == 1,
          'Authorized buyer admin did not read its database-only credential row')
    assert_no_forbidden_keys(owner.json(), 'authorized credential listing')
    check('mk_fixture' in owner.text and 'secret_encrypted' not in owner.text,
          'Credential metadata response is missing expected prefix or exposed encrypted secret')
    check(owner.json()['items'][0]['secret_download_available'] is True,
          'Safe credential availability metadata was not present as a boolean')
    for role in ('member','other','platform','finance','operator'):
        response = client.get('/api/orders/'+f['paid']+'/api-credentials', headers=token(m,f[role]))
        checks.append({'role': role, 'status': response.status_code})
        if response.status_code == 200:
            assert_no_forbidden_keys(response.json(), role + ' credential listing')
            check(not response.json().get('items'), 'Unauthorized actor received credential rows')
        else:
            check(response.status_code in (401,403,404,409), 'Unexpected API credential permission status')
    file_response = client.get('/api/files/'+f['file']+'/download', headers=token(m,f['member']),
                               params={'order_id': f['paid']})
    file_status = file_response.status_code
    check(file_status == 403, 'Ordinary member downloaded protected file')
    return {'credential_list': checks, 'ordinary_member_file_download': file_status,
            'gateway_provisioning': 'not_exercised'}


def immediate_denial(client, m, f):
    h = token(m, f['disabled'])
    check(client.get('/api/orders', headers=h).status_code in (401,403), 'Disabled account retained access')
    admin_token = token(m, f['admin'])
    credential_path = '/api/orders/'+f['paid']+'/api-credentials'
    before = client.get(credential_path, headers=admin_token)
    check(before.status_code == 200, 'Enterprise admin fixture lacks API credential access before role downgrade')
    with m.SessionLocal() as db:
        membership = db.query(m.Membership).filter_by(user_id=f['admin'], enterprise_id=f['buyerco']).one()
        membership.role = 'member'
        db.commit()
    after = client.get(credential_path, headers=admin_token)
    check(after.status_code == 403, 'Same token retained admin API credential access after role downgrade')
    return {'disabled': 'denied', 'same_token_admin_before_downgrade':before.status_code,
            'same_token_member_after_downgrade':after.status_code}


def application_cases(client, m, f):
    response = client.get('/api/applications', headers=token(m,f['member']))
    check(response.status_code == 200, 'Eligible member application listing denied')
    assert_no_forbidden_keys(response.json(), 'member application listing')
    items = response.json().get('items', [])
    check([item.get('subscription_id') for item in items] == ['qa-subscription'],
          'Member application listing exposed another subscription or omitted own authorized app')
    return {'member_listing_status': response.status_code, 'own_application_count': len(items),
            'external_sso': 'not_exercised'}


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        result = run()
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(0 if result['status'] == 'passed' else 1)
    except AssertionError:
        print(json.dumps({'status':'failed','failure':'acceptance assertion; inspect safe case metadata'}))
        raise SystemExit(1) from None
    except Exception as exc:
        print(json.dumps({'status':'failed','error_type':type(exc).__name__}))
        raise SystemExit(1) from None
