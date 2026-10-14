"""Private runtime for full worker matrix; real domain hooks, isolated HTTP vendors."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import secrets
import threading
from types import ModuleType, SimpleNamespace
from urllib.parse import urlsplit

from fastapi import FastAPI
from sqlalchemy import Boolean, Column, DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase

from app import subscription_terms, version_change_ledger, version_change_resources, version_change_saga
from app.fixture_faults import FixtureScope, reserve_fixture
from app.order_economics import money
from app.subscription_calendar import CalendarAnchor
from app.version_change_quotes import remaining_ratio


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def http_call(endpoint, secret, method, path, payload=None):
    url = urlsplit(endpoint)
    if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.path not in ('', '/'):
        raise ValueError('matrix endpoint must be exact loopback')
    connection = HTTPConnection(url.hostname, url.port, timeout=2)
    try:
        connection.request(method, path, body=None if payload is None else canonical(payload),
            headers={'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(1024 * 1024)
        if response.status >= 400:
            raise ConnectionError('isolated vendor HTTP failure')
        return json.loads(raw)
    finally:
        connection.close()


class PrivateVendors:
    """State survives worker restarts; no shared APISIX/Redis/SaaS endpoints."""
    def __init__(self, secret):
        self.secret = secret
        self.lock = threading.RLock()
        self.fixtures = {}
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def handle_request(self):
                auth = self.headers.get_all('Authorization', [])
                if len(auth) != 1 or not hmac.compare_digest(auth[0], 'Bearer ' + outer.secret):
                    self.send_error(401)
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= length <= 1024 * 1024:
                        raise ValueError('oversized payload')
                    payload = json.loads(self.rfile.read(length)) if length else None
                    with outer.lock:
                        status, value = outer.dispatch(self.command, self.path, payload)
                except (KeyError, ValueError, TypeError):
                    status, value = 404, {'error': 'unowned-exact-fixture'}
                encoded = canonical(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            do_GET = do_POST = do_PUT = handle_request
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def dispatch(self, method, path, payload):
        parts = path.split('/')
        if len(parts) < 4 or parts[1] != 'fixtures':
            raise ValueError('exact fixture required')
        value = self.fixtures[parts[2]]
        operation = '/'.join(parts[3:])
        flags = value['faults']
        if operation.startswith('health/') and method == 'GET':
            side = parts[-1]
            failed = flags.get('prepare') and side == 'target'
            return (503 if failed else 200), {'status_code': 503 if failed else 200}
        if operation.startswith('admin/'):
            category, key = parts[4], parts[5]
            if category not in ('routes', 'consumers') or key not in value[category]:
                raise ValueError('unowned vendor key')
            if method == 'GET':
                return 200, value[category][key]
            if method == 'PUT' and category == 'consumers':
                side = 'target' if payload['plugins']['market-gateway-quota']['route_key'] == value['target_route'] else 'original'
                if flags.get(side):
                    return 503, {'error': 'injected-apply'}
                value[category][key] = payload
                value['effects'].append(side)
                return 200, payload
        if operation == 'redis' and method == 'POST':
            key = payload['key']
            if key not in value['hashes']:
                raise ValueError('unowned redis key')
            if payload['command'] == 'hgetall':
                return 200, value['hashes'][key]
            if payload['command'] == 'hset':
                value['hashes'][key].update({key: str(item) for key, item in payload['mapping'].items()})
                return 200, {'ok': True}
        if operation == 'saas' and method == 'POST':
            body, action = payload['payload'], payload['operation']
            for field in ('tenant_id', 'product_id', 'enterprise_id', 'subscription_id', 'admin_user_id', 'oidc_client_id'):
                if body[field] != value['saas'][field]:
                    raise ValueError('SaaS ownership mismatch')
            if action == 'CHANGE':
                side = 'target' if body['to_version'] == 'v2' else 'original'
                if flags.get(side):
                    return 503, {'error': 'injected-change'}
                previous = value['idempotency'].get(body['idempotency_key'])
                if previous is not None and previous != body:
                    return 409, {'error': 'idempotency-mismatch'}
                value['idempotency'][body['idempotency_key']] = body
                value['saas']['version'] = body['to_version']
                value['effects'].append(side)
                return 200, {'ret': 0}
            if action == 'STATUS':
                result = dict(value['saas'], ret=0)
                if flags.get('prepare'):
                    return 503, {'error': 'injected-status'}
                return 200, result
        return 404, {'error': 'unknown-operation'}


def build_runtime(sessions, endpoint, secret, scope=None, instant=None):
    module = ModuleType('app.main')
    ns = module.__dict__
    class Base(DeclarativeBase):
        pass
    ns.update(Base=Base, SessionLocal=sessions, now=lambda: instant or datetime.now(timezone.utc),
              json=json, app=FastAPI(), current_user=lambda: None, db_session=lambda: None)
    def model(name, table, strings='', integers='', amounts='', dates='', booleans=''):
        fields = {'__tablename__': table, 'id': Column(String(36), primary_key=True,
                                                       default=lambda: secrets.token_hex(16))}
        for names, kind in ((strings, Text), (integers, Integer), (amounts, Numeric(18, 2)),
                            (dates, subscription_terms.UTCDateTime()), (booleans, Boolean)):
            for field in names.split():
                fields[field] = Column(kind)
        cls = type(name, (Base,), fields)
        ns[name] = cls
        return cls
    model('Enterprise', 'enterprises')
    model('Product', 'products', 'product_type currency settlement_rule_mode settlement_rule_json settlement_rule_id')
    model('Order', 'orders', 'order_no product_id buyer_enterprise_id buyer_user_id product_version_id product_version_code delivery_method_snapshot payment_status delivery_status main_status economic_snapshot_json delivery_snapshot_json business_type', amounts='amount paid_amount refunded_amount')
    model('Payment', 'payments', 'order_id payment_no status confirmed_by', amounts='amount', dates='created_at paid_at')
    model('Refund', 'refunds', 'refund_no order_id payment_id status reason requested_by completed_by', amounts='amount', dates='completed_at')
    model('ProductReleaseVersion', 'release_versions', 'product_id version_code status', 'rate_limit_per_minute daily_quota monthly_quota', 'price cost')
    model('SaaSProductVersion', 'saas_versions', 'product_id version_code status', amounts='monthly_price cost')
    config = model('GatewayConfig', 'gateway_configs', 'version_id route_id revision_id evidence_hash upstream_url route_key auth_mode upstream_auth_mode upstream_scope health_method health_path status', 'timeout_ms', booleans='strip_prefix')
    model('ApiGatewayRoute', 'gateway_routes', 'product_id version route_key status validated_ip payload_json')
    model('ApiCredential', 'api_credentials', 'enterprise_id route_id order_id status apisix_consumer_name key_hash key_prefix secret_encrypted', 'rate_limit_per_minute daily_quota monthly_quota total_quota')
    model('SaaSSubscription', 'saas_subscriptions', 'enterprise_id product_id version_id status external_tenant_id', dates='expires_at')
    model('SaaSIntegrationConfig', 'saas_configs', 'product_id base_url operation_path token_url client_id scope auth_mode status', 'timeout_ms')
    model('SaaSUserMapping', 'saas_mappings', 'subscription_id status user_id external_user_id')
    model('OIDCClient', 'oidc_clients', 'product_id client_id status')
    model('SaaSOperation', 'saas_operations', 'subscription_id operation status request_payload', dates='created_at')
    audit_model = model('AuditLog', 'matrix_audit', 'action target_id before_json after_json', dates='created_at')
    def audit(db, actor, action, target_type, target_id, **kwargs):
        db.add(audit_model(action=action, target_id=target_id,
            before_json=canonical(kwargs.get('before', {})), after_json=canonical(kwargs.get('after', {})),
            created_at=ns['now']()))
    ns['audit'] = audit
    ns['order_economics'] = {'economic_snapshot': lambda order: json.loads(order.economic_snapshot_json),
                             'delivery_snapshot': lambda order: json.loads(order.delivery_snapshot_json)}
    subscription_terms.install(ns)
    version_change_ledger.install(ns)
    version_change_saga.install(ns)
    prefix = '/fixtures/' + scope.change_id if scope else ''
    def call(method, path, payload=None):
        return http_call(endpoint, secret, method, prefix + path, payload)
    def health(method, url, **kwargs):
        parsed = urlsplit(url)
        if not url.startswith(endpoint + prefix + '/health/'):
            raise ValueError('health endpoint outside private fixture')
        result = http_call(endpoint, secret, method, parsed.path)
        return SimpleNamespace(status_code=result['status_code'],
                               extensions={'validated_upstream_addresses': ['127.0.0.1']})
    class Redis:
        def hgetall(self, key):
            return call('POST', '/redis', {'command': 'hgetall', 'key': key})
        def hset(self, key, mapping):
            return call('POST', '/redis', {'command': 'hset', 'key': key, 'mapping': mapping})
        def close(self):
            pass
    ns.update(apisix_enabled=lambda: True, apisix_policy_redis=Redis,
        apisix_admin_request=lambda method, path, payload=None: call(method, '/admin' + path, payload),
        apisix_route_payload=lambda route, product, db: json.loads(route.payload_json),
        validated_http_request=health,
        saas_call=lambda config, operation, payload: call('POST', '/saas', {'operation': operation, 'payload': payload}),
        version_gateway={'Config': config, 'version_ready': lambda db, product, version:
            db.scalar(__import__('sqlalchemy').select(config).where(config.version_id == version.id)).status == 'active'})
    version_change_resources.install(ns)
    return module


def seed_fixture(module, db, vendors, resource, case, token):
    ns = module.__dict__
    root = 'qa-vcs-' + secrets.token_hex(6)
    change_id, order_id, combo_id, term_id = [secrets.token_hex(16) for _ in range(4)]
    product_id = root + ('-p-model' if resource == 'model_api' else '-p-api')
    enterprise_id = root + '-e-buyer'
    scope = FixtureScope(root, change_id, order_id, product_id, enterprise_id, combo_id)
    at = ns['now']()
    anchor = CalendarAnchor(at - timedelta(days=1))
    bounds = anchor.term(3)
    old_id, target_id = secrets.token_hex(16), secrets.token_hex(16)
    kind = 'downgrade' if case in ('downgrade', 'downgrade_refund_failure') else 'no_payment' if case == 'no_payment' else 'upgrade'
    target_price = Decimal('50' if kind == 'downgrade' else '100' if kind == 'no_payment' else '150')
    ratio = remaining_ratio(bounds.start, bounds.end, at)
    original, target = money(300 * ratio), money(target_price * 3 * ratio)
    difference = target - original
    rate_keys = ('platform_rate', 'provider_rate', 'service_rate', 'expert_rate', 'channel_rate')
    rates = dict.fromkeys(rate_keys, '0')
    rule = {'mode': 'custom', 'id': '', 'version': 'product:' + product_id, 'rates': rates}
    def economic(version_id, code, price, cost):
        return {'product_id': product_id, 'product_version_id': version_id, 'version_code': code,
            'billing_unit': 'month', 'subscription_months': 3, 'currency': 'USD',
            'unit_price': str(price), 'unit_cost': str(cost), 'amount': str(price * 3),
            'total_cost': str(cost * 3), 'settlement_rule': rule}
    def delivery(amount):
        return {'method': resource, 'quota': {'rate_limit_per_minute': amount,
            'daily_quota': amount * 10, 'monthly_quota': amount * 100, 'quota_amount': 0}}
    old_delivery, target_delivery = delivery(10), delivery(20)
    order = ns['Order'](id=order_id, order_no=root, product_id=product_id,
        buyer_enterprise_id=enterprise_id, buyer_user_id=root + '-buyer', product_version_id=old_id,
        product_version_code='v1', delivery_method_snapshot=resource, payment_status='paid',
        delivery_status='accepted', main_status='active', amount=300, paid_amount=300, refunded_amount=0,
        economic_snapshot_json=canonical(economic(old_id, 'v1', Decimal(100), Decimal(30))),
        delivery_snapshot_json=canonical(old_delivery), business_type='')
    db.add_all([ns['Enterprise'](id=enterprise_id), ns['Product'](id=product_id,
        product_type='saas' if resource == 'tenant_access' else 'model' if resource == 'model_api' else 'api', currency='USD',
        settlement_rule_mode='custom', settlement_rule_json=canonical(rates)), order])
    db.flush()
    Combo, Term = ns['subscription_terms']['Combination'], ns['subscription_terms']['Term']
    combo = Combo(id=combo_id, enterprise_id=enterprise_id, product_id=product_id, delivery_method=resource,
        version_id=old_id, version_code='v1', anchor_at=anchor.origin, total_months=3, generation=1, status='active')
    db.add(combo)
    db.flush()
    db.add(Term(id=term_id, order_id=order_id, combination_id=combo_id, generation=1,
        version_id=old_id, version_code='v1', start_month=0, months=3, starts_at=bounds.start, ends_at=bounds.end))
    db.flush()
    source_payment = ns['Payment'](order_id=order_id, payment_no=root + '-pay', status='paid', amount=300,
                                  created_at=at, paid_at=at)
    db.add(source_payment)
    db.flush()
    version_model = ns['SaaSProductVersion'] if resource == 'tenant_access' else ns['ProductReleaseVersion']
    for version_id, code, price, cost, limit in ((old_id, 'v1', 100, 30, 10), (target_id, 'v2', target_price, 40, 20)):
        values = dict(id=version_id, product_id=product_id, version_code=code, status='active', cost=cost)
        values['monthly_price' if resource == 'tenant_access' else 'price'] = price
        if resource != 'tenant_access':
            values.update(rate_limit_per_minute=limit, daily_quota=limit * 10, monthly_quota=limit * 100)
        db.add(version_model(**values))
    state = {'faults': {}, 'routes': {}, 'consumers': {}, 'hashes': {}, 'effects': [], 'idempotency': {}, 'saas': {}}
    prefix = vendors.endpoint + '/fixtures/' + change_id
    quote_id = hashlib.sha256(change_id.encode()).hexdigest()
    integration = {}
    if resource != 'tenant_access':
        credential_id, consumer, raw_key = secrets.token_hex(16), root + '-consumer', secrets.token_urlsafe(32)
        route_ids = [secrets.token_hex(16), secrets.token_hex(16)]
        for index, (version_id, code, side) in enumerate(((old_id, 'v1', 'source'), (target_id, 'v2', 'target'))):
            route_key, config_id = root + '-' + side, secrets.token_hex(16)
            payload = {'uri': '/fixtures/' + root + '/' + side, 'labels': {'market_product_id': product_id}}
            state['routes'][route_key] = payload
            db.add(ns['ApiGatewayRoute'](id=route_ids[index], product_id=product_id, version=code,
                route_key=route_key, status='active', validated_ip='127.0.0.1', payload_json=canonical(payload)))
            db.add(ns['GatewayConfig'](id=config_id, version_id=version_id, route_id=route_ids[index],
                revision_id=root + '-' + side, evidence_hash=hashlib.sha256(side.encode()).hexdigest(),
                upstream_url=prefix, route_key=route_key, auth_mode='key', upstream_auth_mode='none',
                upstream_scope='', health_method='GET', health_path='/health/' + side, status='active',
                timeout_ms=1000, strip_prefix=False))
            if index:
                integration = {'config_id': config_id, 'revision_id': root + '-' + side,
                    'evidence_hash': hashlib.sha256(side.encode()).hexdigest(), 'route_id': route_ids[index],
                    'route_key': route_key, 'quote_fingerprint': quote_id}
                state['target_route'] = route_key
            else:
                state['source_route'] = route_key
        db.add(ns['ApiCredential'](id=credential_id, enterprise_id=enterprise_id, route_id=route_ids[0],
            order_id='', status='active', apisix_consumer_name=consumer, key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
            key_prefix='private', rate_limit_per_minute=10, daily_quota=100, monthly_quota=1000, total_quota=0))
        periods = version_change_resources._quota_period_plan(combo,
            [(db.get(Term, term_id), old_delivery)], {term_id: {'delivery': target_delivery}},
            state['source_route'], state['target_route'], at)['original']
        state['consumers'][consumer] = {'username': consumer, 'plugins': {'key-auth': {'key': raw_key},
            'market-gateway-quota': {'route_key': state['source_route'], 'daily_quota': 100, 'monthly_quota': 1000, 'total_quota': 0}}}
        state['hashes']['market:apisix:credential:' + consumer] = {'mode': 'subscription', 'status': 'active',
            'generation': '1', 'quota_scope': combo_id, 'subscription_periods': canonical(periods),
            'daily_quota': '100', 'monthly_quota': '1000', 'total_quota': '0'}
    else:
        sub_id, cfg_id = secrets.token_hex(16), secrets.token_hex(16)
        config = ns['SaaSIntegrationConfig'](id=cfg_id, product_id=product_id, base_url=vendors.endpoint,
            operation_path='/fixtures/' + change_id + '/saas', token_url='', client_id=root + '-client',
            scope='saas.tenant', auth_mode='oauth2', status='active', timeout_ms=1000)
        db.add_all([config, ns['SaaSSubscription'](id=sub_id, enterprise_id=enterprise_id, product_id=product_id,
            version_id=old_id, status='active', external_tenant_id=root + '-tenant', expires_at=bounds.end),
            ns['SaaSUserMapping'](subscription_id=sub_id, status='active', user_id=root + '-buyer', external_user_id=root + '-external'),
            ns['OIDCClient'](product_id=product_id, client_id=root + '-oidc', status='active')])
        config_fingerprint = hashlib.sha256(canonical({key: getattr(config, key) for key in (
            'id', 'base_url', 'operation_path', 'token_url', 'client_id', 'scope', 'auth_mode', 'timeout_ms', 'status')}).encode()).hexdigest()
        integration = {'kind': 'saas', 'config_id': cfg_id, 'config_fingerprint': config_fingerprint, 'quote_fingerprint': quote_id}
        state['saas'] = {'tenant_id': root + '-tenant', 'product_id': product_id, 'enterprise_id': enterprise_id,
            'subscription_id': sub_id, 'admin_user_id': root + '-buyer', 'oidc_client_id': root + '-oidc',
            'version': 'v1', 'status': 'active', 'admin_active': True, 'application_ready': True, 'expires_at': bounds.end.isoformat()}
    vendors.fixtures[change_id] = state
    snapshots = {term_id: {'economic': economic(target_id, 'v2', target_price, Decimal(40)),
                          'delivery': target_delivery, 'integration': integration}}
    quote = {'quote_id': quote_id, 'refund_amount': str(max(-difference, Decimal(0))),
        'payment_amount': str(max(difference, Decimal(0))), 'net_difference': str(difference), 'currency': 'USD'}
    Request = ns['VersionChangeRequest']
    request = Request(id=change_id, source_order_id=order_id, enterprise_id=enterprise_id,
        combination_id=combo_id, target_version_id=target_id, target_version_code='v2', old_version_id=old_id,
        old_version_code='v1', kind=kind, status='pending_payment' if kind == 'upgrade' else 'resource_pending',
        idempotency_key=root, request_hash=quote_id, quote_json=canonical(quote), prepared_json='{}',
        target_snapshots_json=canonical(snapshots), created_by='private-matrix')
    db.add(request)
    db.flush()
    public = {'order_id': order_id, 'combination_id': combo_id, 'currency': 'USD', 'segments': [
        {'order_id': order_id, 'remaining_starts_at': at.isoformat(), 'remaining_ratio': str(ratio),
         'original_remaining_amount': str(original), 'target_remaining_amount': str(target)}]}
    def segment(version_id, revenue, cost):
        return {'version_id': version_id, 'revenue': str(revenue), 'cost': str(cost),
                'profit': str(revenue - cost), 'rule': rule}
    prepared = {'public': public, 'combo': combo, 'digest': quote_id, 'internal': [
        {'order_id': order_id, 'term_id': term_id, 'old': segment(old_id, -original, -money(90 * ratio)),
         'target': segment(target_id, target, money(120 * ratio))}]}
    ns['version_change_ledger']['stage'](db, change_id, prepared, 'private-matrix')
    if kind == 'upgrade':
        funding_id = secrets.token_hex(16)
        paid = case != 'unpaid_expiry'
        funding = ns['Order'](id=funding_id, order_no=root + '-funding', product_id=product_id,
            buyer_enterprise_id=enterprise_id, payment_status='paid' if paid else 'paying',
            main_status='active', amount=difference, paid_amount=difference if paid else 0, refunded_amount=0,
            business_type='version_change_payment')
        payment = ns['Payment'](order_id=funding_id, payment_no=root + '-funding', status='paid' if paid else 'paying',
                                amount=difference, created_at=at, paid_at=at if paid else None)
        db.add(funding)
        db.flush()
        db.add(payment)
        db.flush()
        request.funding_order_id = funding_id
        db.add(ns['VersionChangeFund'](change_id=change_id, kind='payment', payment_id=payment.id,
            amount=difference, idempotency_key=change_id + ':payment'))
    elif kind == 'downgrade':
        db.add(ns['VersionChangeFund'](change_id=change_id, kind='refund', payment_id=source_payment.id,
            amount=-difference, idempotency_key=change_id + ':refund:' + source_payment.id))
    db.flush()
    reserve_fixture(db, ns, scope, token=token, seconds=3600)
    return scope
