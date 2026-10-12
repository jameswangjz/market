"""Bounded deployed SaaS version-change acceptance using market-mock-saas.

Run with --run only against the dedicated local Kubernetes fixture. The private
0600 manifest journals fixture roots and external mock identities before they
are created. A failed run retains that journal for exact --cleanup recovery.
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
import traceback


class AcceptanceFailure(RuntimeError):
    pass


STAGE = 'startup'
PROTECTED_ORDER = 'ORD-261012005144E1B3'
MOCK_SAAS = 'http://market-mock-saas:8200'
OIDC_ISSUER = 'http://192.168.10.10:30080'
OIDC_REDIRECT = 'http://192.168.10.10:30083/sso/callback'


def check(condition, message):
    if not condition:
        raise AcceptanceFailure(message)


def fixture_ids(run_id=None):
    run_id = run_id or 'qa-vcs-saas-' + secrets.token_hex(6)
    return {
        'run_id': run_id,
        'users': {role: f'{run_id}-u-{role}' for role in ('buyer', 'provider', 'finance', 'delivery')},
        'enterprises': {role: f'{run_id}-e-{role}' for role in ('buyer', 'provider')},
        'product': run_id + '-p-saas',
        'versions': {side: run_id + '-v-' + side for side in ('source', 'upgrade', 'downgrade')},
        'oauth_client_id': run_id + '-machine',
        'external': {'tenant': None, 'tenant_created': False,
                      'oidc_client_id': None, 'oidc_client_created': False},
        'database_ids': {'orders': [], 'changes': [], 'subscription_id': None,
                         'combination_id': None, 'terms': [], 'term_contracts': {},
                         'order_snapshots': {},
                         'payments': [], 'refunds': []},
        'environment': '', 'authorized': False, 'request_id': run_id + '-http',
        'protected_order_fingerprint': '',
    }


def fingerprint(db, m):
    from sqlalchemy import select
    order = db.scalar(select(m.Order).where(
        (m.Order.id == PROTECTED_ORDER) | (m.Order.order_no == PROTECTED_ORDER)))
    check(order is not None, 'Protected order missing; refusing fixture authorization')
    product = db.get(m.Product, order.product_id)
    check(product is not None, 'Protected order product missing')
    financial = []
    for model in (m.Payment, m.Refund):
        financial.append((model.__tablename__, [
            {c.name: getattr(row, c.name) for c in model.__table__.columns}
            for row in db.scalars(select(model).where(model.order_id == order.id)).all()]))
    payload = {'order': {c.name: getattr(order, c.name) for c in m.Order.__table__.columns},
        'product': {c.name: getattr(product, c.name) for c in m.Product.__table__.columns},
        'financial': financial}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def write_manifest(path, value, create=False):
    path = Path(path)
    temporary = None
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    else:
        info = path.lstat()
        check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
              'Manifest must be regular and mode 0600')
        fd, temporary = tempfile.mkstemp(prefix='.vcs-saas-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            serializable = {key: item for key, item in value.items()
                            if key != '_manifest_writer'}
            json.dump(serializable, stream, indent=2, sort_keys=True)
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
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0))
    try:
        info = os.fstat(fd)
        check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
              'Manifest must be regular and mode 0600')
        with os.fdopen(fd, 'r') as stream:
            fd = -1
            value = json.load(stream)
    finally:
        if fd >= 0:
            os.close(fd)
    run_id = value.get('run_id', '')
    check(re.fullmatch(r'qa-vcs-saas-[0-9a-f]{12}', run_id) is not None,
          'Invalid generated fixture ownership marker')
    expected = fixture_ids(run_id)
    for key in ('users', 'enterprises', 'product', 'versions', 'oauth_client_id'):
        check(value.get(key) == expected[key], 'Manifest fixture roots were changed')
    check(set(value) == set(expected), 'Unexpected manifest fields')
    check(set(value['external']) == {'tenant', 'tenant_created',
          'oidc_client_id', 'oidc_client_created'}, 'Invalid external journal')
    ext = value['external']
    check(type(ext['tenant_created']) is bool and type(ext['oidc_client_created']) is bool and
          (ext['tenant'] is None or re.fullmatch(r'tenant-[0-9a-f]{12}', ext['tenant']) is not None) and
          (ext['oidc_client_id'] is None or
           re.fullmatch(r'oidc_[0-9a-f]{32}', ext['oidc_client_id']) is not None),
          'Invalid generated external identity journal')
    check(re.fullmatch(r'[0-9a-f]{64}', value.get('protected_order_fingerprint', '')) is not None,
          'Protected order fingerprint is invalid')
    return value


def environment_fingerprint(m):
    raw = m.engine.url.render_as_string(hide_password=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def load_main():
    os.environ.setdefault('MESSAGE_CENTER_DISABLE_BACKGROUND', 'true')
    from app import main as m
    check(m.engine.dialect.name == 'postgresql', 'Dedicated PostgreSQL fixture required')
    check(not m.app.dependency_overrides, 'Dependency overrides are forbidden')
    check(hasattr(m, 'version_change_saga') and hasattr(m, 'version_change_resources'),
          'Version-change Saga/resource adapters are not installed')
    check(isinstance(m.online_delivery, dict) and
          callable(m.online_delivery.get('handles')) and callable(m.online_delivery.get('process')),
          'SaaS automatic delivery adapter is not installed')
    return m


def mock_admin_secret():
    value = os.getenv('MOCK_SAAS_ADMIN_SECRET', '')
    if not value:
        path = Path('/tmp/market-mock-admin-key')
        if path.is_file() and not path.is_symlink():
            value = path.read_text().strip()
    check(bool(value), 'Fixture mock-SaaS admin secret is unavailable')
    return value


def preflight(m, ids):
    from sqlalchemy import select
    with m.SessionLocal() as db:
        for model, values in ((m.User, ids['users'].values()),
            (m.Enterprise, ids['enterprises'].values()), (m.Product, [ids['product']]),
            (m.SaaSProductVersion, ids['versions'].values())):
            check(db.scalar(select(model.id).where(model.id.in_(list(values))).limit(1)) is None,
                  'Generated fixture identity collision; no cleanup is authorized')
        check(db.scalar(select(m.OAuthClient.id).where(
            m.OAuthClient.client_id == ids['oauth_client_id']).limit(1)) is None,
            'Generated OAuth client collision; no cleanup is authorized')
        protected = fingerprint(db, m)
        ids['protected_order_fingerprint'] = protected
        check(m.application_address_policy['allow_http'](db) is True,
              'Existing local HTTP development policy is required; settings will not be changed')
        m.application_address_policy['validate_url'](MOCK_SAAS + '/isv.php', db=db)
    ids['environment'] = environment_fingerprint(m)
    # Health is read-only; no tenant, client, queue item, or shared setting is touched.
    response = m.validated_http_request('GET', MOCK_SAAS + '/health', timeout=5,
                                        follow_redirects=False)
    check(response.status_code == 200, 'market-mock-saas health check failed')
    mock_admin_secret()


def request(client, token, method, path, expected=200, body=None):
    response = client.request(method, '/api' + path,
        headers={'Authorization': 'Bearer ' + token}, json=body)
    check(response.status_code == expected,
          f'{method} {path} returned HTTP {response.status_code}, expected {expected}')
    return response.json() if response.content else {}


def prepare_fixture(m, ids):
    from sqlalchemy import select
    with m.SessionLocal() as db:
        for role, eid in ids['enterprises'].items():
            db.add(m.Enterprise(id=eid, name=eid, credit_code=eid, verification_status='verified'))
        for role, uid in ids['users'].items():
            db.add(m.User(id=uid, email=uid + '@example.invalid', name=uid,
                password_hash='not-a-login-password', verified_status='verified',
                activation_status='active', is_active=True,
                platform_role={'finance': 'finance_settlement', 'delivery': 'delivery_monitor'}.get(role, '')))
        db.flush()
        Preference = m.message_center['Preference']
        db.add_all([Preference(user_id=uid, email_enabled=False)
                    for uid in ids['users'].values()])
        db.add_all([
            m.Membership(user_id=ids['users']['buyer'], enterprise_id=ids['enterprises']['buyer'],
                         role='super_admin', status='active'),
            m.Membership(user_id=ids['users']['provider'], enterprise_id=ids['enterprises']['provider'],
                         role='super_admin', status='active')])
        product = m.Product(id=ids['product'], enterprise_id=ids['enterprises']['provider'],
            name=ids['product'], product_type='saas', delivery_method='tenant_access',
            status='pending_integration', currency='CNY', price=100,
            application_url='http://192.168.10.10:30083/sso/apps/' + ids['oauth_client_id'],
            integration_api_url=MOCK_SAAS + '/isv.php',
            oidc_redirect_uris_json=json.dumps([OIDC_REDIRECT]), provider_name=ids['enterprises']['provider'],
            settlement_rule_mode='custom', settlement_rule_json=json.dumps({'platform_rate': 10,
                'provider_rate': 90, 'service_rate': 0, 'expert_rate': 0, 'channel_rate': 0}))
        db.add(product)
        db.flush()
        machine_secret = secrets.token_urlsafe(40)
        machine = m.OAuthClient(product_id=product.id, client_id=ids['oauth_client_id'],
            client_secret=machine_secret, scope='saas.tenant saas.user saas.department', status='active')
        db.add(machine)
        config = m.SaaSIntegrationConfig(product_id=product.id, base_url=MOCK_SAAS,
            operation_path='/isv.php', token_url=MOCK_SAAS + '/oauth/token',
            client_id=machine.client_id, client_secret=machine_secret,
            scope=machine.scope, auth_mode='oauth2', timeout_ms=10000, status='active',
            verification_status='pending', updated_by=ids['users']['provider'])
        db.add(config)
        prices = {'source': 100, 'upgrade': 160, 'downgrade': 70}
        for side, version_id in ids['versions'].items():
            db.add(m.SaaSProductVersion(id=version_id, product_id=product.id,
                version_code=side, name=side, compatibility_mode='compatible',
                monthly_price=prices[side], quarterly_price=prices[side] * 3,
                annual_price=prices[side] * 12, cost=prices[side] * .4, status='active'))
        db.flush()
        client, oidc_secret = m.oidc_provider['ensure_client'](db, product,
            ids['users']['provider'])
        check(client is not None and client.product_id == product.id,
              'Fixture OIDC client creation failed')
        ids['external']['oidc_client_id'] = client.client_id
        product.application_url = 'http://192.168.10.10:30083/sso/apps/' + client.client_id
        db.commit()
        tokens = {role: m.issue_token(db.get(m.User, uid)) for role, uid in ids['users'].items()}
    return tokens, oidc_secret


def register_mock_client(ids, client_secret):
    import httpx
    client_id = ids['external']['oidc_client_id']
    check(isinstance(client_id, str) and client_id, 'OIDC client identity missing')
    # Durable ownership is written before the external create call.
    ids['external']['oidc_client_created'] = True
    ids['_manifest_writer'](ids)
    response = httpx.post(MOCK_SAAS + '/_test/oidc/clients',
        headers={'X-Mock-Admin-Secret': mock_admin_secret()}, json={
            'client_id': client_id, 'client_secret': client_secret,
            'redirect_uri': OIDC_REDIRECT, 'product_id': ids['product'],
            'issuer': OIDC_ISSUER, 'product_name': ids['product']}, timeout=10)
    if response.status_code != 201:
        if response.status_code == 409:
            ids['external']['oidc_client_id'] = None
            ids['external']['oidc_client_created'] = False
            ids['_manifest_writer'](ids)
        raise AcceptanceFailure('Mock SaaS fixture OIDC registration failed')


def create_change(client, tokens, order_id, target_id, idempotency):
    quote = request(client, tokens['buyer'], 'POST', f'/orders/{order_id}/version-change-quote',
        body={'target_version_id': target_id, 'compatibility_confirmed': True})
    check(quote.get('execution_ready') is False, 'Quote sanitizer execution_ready contract changed')
    return request(client, tokens['buyer'], 'POST', f'/orders/{order_id}/version-changes', body={
        'quote_token': quote['quote_token'], 'idempotency_key': idempotency})


def wait_change(client, token, change_id, expected, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = request(client, token, 'GET', f'/version-changes/{change_id}')
        if current.get('status') == expected:
            return current
        if current.get('status') in {'exception', 'compensated', 'cancelled'}:
            raise AcceptanceFailure('Durable version-change worker reached terminal failure')
        time.sleep(2)
    raise AcceptanceFailure('Durable version-change worker timed out')


def worker_acceptance(m, ids, path, client, tokens):
    from sqlalchemy import select
    order_id = request(client, tokens['buyer'], 'POST', '/orders', body={
        'product_id': ids['product'], 'product_version_id': ids['versions']['source'],
        'buyer_enterprise_id': ids['enterprises']['buyer'], 'subscription_months': 3})['id']
    ids['database_ids']['orders'].append(order_id)
    with m.SessionLocal() as db:
        source = db.get(m.Order, order_id)
        check(source is not None and source.subscription_id,
              'Order did not reserve its exact SaaS subscription')
        ids['database_ids']['subscription_id'] = source.subscription_id
        ids['external']['tenant'] = 'tenant-' + source.subscription_id[:12]
    path_write(ids)
    request(client, tokens['finance'], 'POST', f'/orders/{order_id}/transition', body={
        'action': 'confirm_payment', 'reason': 'qa-vcs-saas runtime acceptance purchase'})
    with m.SessionLocal() as db:
        task = db.scalar(select(m.DeliveryTask).where(m.DeliveryTask.order_id == order_id))
        check(task is not None, 'Fixture purchase delivery task missing')
        task_id = task.id
    deliver_staged(m, ids, path, client, tokens, task_id)
    with m.SessionLocal() as db:
        order = db.get(m.Order, order_id)
        check(order.delivery_status in {'accepted', 'completed'}, 'Fixture SaaS delivery is incomplete')
        sub = db.scalar(select(m.SaaSSubscription).where(
            m.SaaSSubscription.enterprise_id == ids['enterprises']['buyer'],
            m.SaaSSubscription.product_id == ids['product']))
        combo = db.scalar(select(m.subscription_terms['Combination']).where(
            m.subscription_terms['Combination'].enterprise_id == ids['enterprises']['buyer'],
            m.subscription_terms['Combination'].product_id == ids['product']))
        check(sub and sub.external_tenant_id and combo, 'SaaS subscription or combination missing')
        check(sub.external_tenant_id == ids['external']['tenant'],
              'SaaS OPEN returned a tenant outside the journaled identity')
        ids['external']['tenant_created'] = True
        ids['database_ids']['combination_id'] = combo.id
        path_write(ids)
    # A second adjacent purchased term validates both current and future terms.
    renewal_id = request(client, tokens['buyer'], 'POST', '/orders', body={
        'product_id': ids['product'], 'product_version_id': ids['versions']['source'],
        'buyer_enterprise_id': ids['enterprises']['buyer'], 'subscription_months': 1})['id']
    ids['database_ids']['orders'].append(renewal_id)
    path_write(ids)
    request(client, tokens['finance'], 'POST', f'/orders/{renewal_id}/transition', body={
        'action': 'confirm_payment', 'reason': 'qa-vcs-saas future-term acceptance'})
    with m.SessionLocal() as db:
        task = db.scalar(select(m.DeliveryTask).where(m.DeliveryTask.order_id == renewal_id))
        check(task is not None, 'Future-term renewal delivery task missing')
        task_id = task.id
    deliver_staged(m, ids, path, client, tokens, task_id)

    with m.SessionLocal() as db:
        combo_id = ids['database_ids']['combination_id']
        terms = db.scalars(select(m.subscription_terms['Term']).where(
            m.subscription_terms['Term'].combination_id == combo_id).order_by(
                m.subscription_terms['Term'].start_month)).all()
        check(len(terms) >= 2, 'Current and future fixture terms were not both created')
        ids['database_ids']['terms'] = [term.id for term in terms]
        ids['database_ids']['term_contracts'] = {
            term.id: {'order_id': term.order_id, 'combination_id': term.combination_id,
                'generation': term.generation, 'start_month': term.start_month,
                'months': term.months, 'starts_at': term.starts_at.isoformat(),
                'ends_at': term.ends_at.isoformat(), 'status': term.status}
            for term in terms}
        ids['database_ids']['order_snapshots'] = {}
        for term in terms:
            term_order = db.get(m.Order, term.order_id)
            ids['database_ids']['order_snapshots'][term.order_id] = {
                'product_version_id': term_order.product_version_id,
                'product_version_code': term_order.product_version_code,
                'snapshot_version': term_order.snapshot_version,
                'subscription_months': term_order.subscription_months,
                'unit_price_snapshot': str(term_order.unit_price_snapshot),
                'unit_cost_snapshot': str(term_order.unit_cost_snapshot),
                'total_cost_snapshot': str(term_order.total_cost_snapshot),
                'delivery_method_snapshot': term_order.delivery_method_snapshot,
                'economic_snapshot_json': term_order.economic_snapshot_json,
                'delivery_snapshot_json': term_order.delivery_snapshot_json}
    path_write(ids)

    upgrade = create_change(client, tokens, order_id, ids['versions']['upgrade'], ids['run_id'] + '-upgrade')
    upgrade_id = upgrade['id']
    ids['database_ids']['changes'].append(upgrade_id)
    path_write(ids)
    request(client, tokens['buyer'], 'POST', f'/version-changes/{upgrade_id}/start-payment', body={})
    request(client, tokens['buyer'], 'POST', f'/version-changes/{upgrade_id}/confirm-payment',
            body={'reason': 'qa-vcs-saas simulated funding payment'})
    wait_change(client, tokens['buyer'], upgrade_id, 'completed')

    downgrade = create_change(client, tokens, order_id, ids['versions']['downgrade'], ids['run_id'] + '-downgrade')
    downgrade_id = downgrade['id']
    ids['database_ids']['changes'].append(downgrade_id)
    path_write(ids)
    wait_change(client, tokens['buyer'], downgrade_id, 'completed')

    with m.SessionLocal() as db:
        sub = db.get(m.SaaSSubscription, ids['database_ids']['subscription_id'])
        change_rows = db.scalars(select(m.version_change_saga['VersionChangeRequest']).where(
            m.version_change_saga['VersionChangeRequest'].id.in_([upgrade_id, downgrade_id]))).all()
        funds = db.scalars(select(m.version_change_saga['VersionChangeFund']).where(
            m.version_change_saga['VersionChangeFund'].change_id.in_([upgrade_id, downgrade_id]))).all()
        combo = db.get(m.subscription_terms['Combination'], ids['database_ids']['combination_id'])
        terms = db.scalars(select(m.subscription_terms['Term']).where(
            m.subscription_terms['Term'].combination_id == combo.id)).all()
        check(sub and sub.version_id == ids['versions']['downgrade'],
              'Live SaaS subscription did not reach downgrade version')
        check(len(change_rows) == 2 and all(row.status == 'completed' and row.resource_applied for row in change_rows),
              'Upgrade/downgrade did not complete with verified resource application')
        payments = [fund for fund in funds if fund.kind == 'payment']
        refunds = [fund for fund in funds if fund.kind == 'refund']
        check(payments and refunds, 'Upgrade payment and downgrade refund funds are both required')
        contracts = ids['database_ids']['term_contracts']
        order_snapshots = ids['database_ids']['order_snapshots']
        check(len(terms) >= 2 and {term.id for term in terms} == set(contracts),
              'Current and future subscription term identities changed')
        effective_versions = {}
        contract_mismatches = []
        order_snapshot_mismatches = []
        for term in terms:
            expected_contract = contracts[term.id]
            current_contract = {'order_id': term.order_id, 'combination_id': term.combination_id,
                'generation': term.generation, 'start_month': term.start_month,
                'months': term.months, 'starts_at': term.starts_at.isoformat(),
                'ends_at': term.ends_at.isoformat(), 'status': term.status}
            if current_contract != expected_contract:
                contract_mismatches.append({'term_id': term.id, 'original': expected_contract,
                                            'current': current_contract})
            term_order = db.get(m.Order, term.order_id)
            effective = m.subscription_terms['effective_snapshot'](db, term_order, term)
            effective_versions[term.id] = {'version_id': effective['version_id'],
                                           'version_code': effective['version_code']}
            current_order_snapshot = {'product_version_id': term_order.product_version_id,
                'product_version_code': term_order.product_version_code,
                'snapshot_version': term_order.snapshot_version,
                'subscription_months': term_order.subscription_months,
                'unit_price_snapshot': str(term_order.unit_price_snapshot),
                'unit_cost_snapshot': str(term_order.unit_cost_snapshot),
                'total_cost_snapshot': str(term_order.total_cost_snapshot),
                'delivery_method_snapshot': term_order.delivery_method_snapshot,
                'economic_snapshot_json': term_order.economic_snapshot_json,
                'delivery_snapshot_json': term_order.delivery_snapshot_json}
            if current_order_snapshot != order_snapshots[term.order_id]:
                order_snapshot_mismatches.append({'order_id': term.order_id,
                    'changed_fields': sorted(key for key in current_order_snapshot
                        if current_order_snapshot[key] != order_snapshots[term.order_id].get(key))})
        check(not contract_mismatches,
              'Original subscription term boundaries or generation changed: ' +
              json.dumps(contract_mismatches, sort_keys=True))
        check(not order_snapshot_mismatches,
              'Original order version/economic/delivery snapshot changed: ' +
              json.dumps(order_snapshot_mismatches, sort_keys=True))
        check(all(term.version_id == ids['versions']['downgrade'] and
                  term.version_code == 'downgrade' and
                  effective_versions[term.id] == {'version_id': ids['versions']['downgrade'],
                                                  'version_code': 'downgrade'}
                  for term in terms),
              'Current term versions or effective snapshots did not reach downgrade target')
        Revision = m.subscription_terms['Revision']
        revisions = db.scalars(select(Revision).where(
            Revision.term_id.in_([term.id for term in terms]),
            Revision.change_id.in_([upgrade_id, downgrade_id])).order_by(
                Revision.term_id, Revision.revision_number)).all()
        revisions_by_term = {}
        for revision in revisions:
            revisions_by_term.setdefault(revision.term_id, []).append(revision)
        expected_revision_codes = {upgrade_id: 'upgrade', downgrade_id: 'downgrade'}
        check(all(len(revisions_by_term.get(term.id, [])) == 2 and
                  {revision.change_id: revision.version_code
                   for revision in revisions_by_term[term.id]} == expected_revision_codes and
                  {revision.version_id for revision in revisions_by_term[term.id]} ==
                      {ids['versions']['upgrade'], ids['versions']['downgrade']}
                  for term in terms),
              'Each current/future term must have append-only upgrade and downgrade revisions')
        ledger = m.VersionChangeAdjustment
        adjustments = db.scalars(select(ledger).where(ledger.change_id.in_([upgrade_id, downgrade_id]))).all()
        funding_ids = {row.funding_order_id for row in change_rows if row.funding_order_id}
        funding_orders = db.scalars(select(m.Order).where(m.Order.id.in_(funding_ids))).all()
        funding_payments = db.scalars(select(m.Payment).where(m.Payment.order_id.in_(funding_ids))).all()
        funding_tasks = db.scalars(select(m.DeliveryTask).where(m.DeliveryTask.order_id.in_(funding_ids))).all()
        refund_rows = db.scalars(select(m.Refund).where(m.Refund.id.in_(
            [fund.refund_id for fund in refunds if fund.refund_id]))).all()
        source_payment_rows = db.scalars(select(m.Payment).where(m.Payment.order_id.in_(
            set(ids['database_ids']['orders']) | funding_ids))).all()
        config = db.scalar(select(m.SaaSIntegrationConfig).where(
            m.SaaSIntegrationConfig.product_id == ids['product']))
        check(adjustments and all(row.status == 'posted' and row.posted_at for row in adjustments),
              'Version-change ledger adjustments are not posted')
        money = lambda value: Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        change_by_id = {row.id: row for row in change_rows}
        posted_totals = {}
        expected_source_orders = {term.order_id for term in terms}
        for change_id in (upgrade_id, downgrade_id):
            change_adjustments = [row for row in adjustments if row.change_id == change_id]
            by_source = {}
            for adjustment in change_adjustments:
                by_source.setdefault(adjustment.source_order_id, []).append(adjustment)
                check(money(adjustment.profit) == money(adjustment.revenue) - money(adjustment.cost),
                      'Posted version-change ledger profit does not equal revenue minus cost')
            check(len(change_adjustments) == 2 * len(terms) and
                  set(by_source) == expected_source_orders and
                  all(len(rows) == 2 and {row.side for row in rows} == {'old', 'target'}
                      for rows in by_source.values()),
                  'Each posted change requires exactly old/target rows for every owned term')
            expected_net = money(json.loads(change_by_id[change_id].quote_json)['net_difference'])
            actual_net = money(sum((money(row.revenue) for row in change_adjustments), Decimal('0.00')))
            check(actual_net == expected_net,
                  'Posted version-change revenue does not reconcile to the frozen quote net difference')
            posted_totals[change_id] = {'row_count': len(change_adjustments),
                                        'net_revenue': str(actual_net)}
        check(all(order.business_type == 'version_change_payment' and
                  order.snapshot_version == 0 and order.delivery_method_snapshot == ''
                  for order in funding_orders) and not funding_tasks,
              'Version-change funding incorrectly entered normal fulfillment')
        check(funding_payments and all(payment.status == 'paid' for payment in funding_payments),
              'Upgrade funding payment is not paid')
        check(refund_rows and all(refund.status == 'completed' for refund in refund_rows),
              'Downgrade associated refunds are not completed')
        payment_by_id = {payment.id: payment for payment in funding_payments}
        refund_by_id = {refund.id: refund for refund in refund_rows}
        source_payment_by_id = {payment.id: payment for payment in source_payment_rows}
        check(all(fund.payment_id in payment_by_id and
                  payment_by_id[fund.payment_id].amount == fund.amount for fund in payments) and
              all(fund.refund_id in refund_by_id and
                  refund_by_id[fund.refund_id].amount == fund.amount and
                  refund_by_id[fund.refund_id].payment_id == fund.payment_id and
                  fund.payment_id in source_payment_by_id and
                  source_payment_by_id[fund.payment_id].status == 'paid' for fund in refunds),
              'Version-change fund links do not match the actual payment/refund amounts')
        originals = db.scalars(select(m.Order).where(m.Order.id.in_(ids['database_ids']['orders']))).all()
        check(all(order.refunded_amount == 0 for order in originals),
              'Downgrade incorrectly changed an original paid order refund balance')
        check(config is not None, 'Verified SaaS integration config disappeared')
        status = m.saas_call(config, 'STATUS', {'tenant_id': ids['external']['tenant'],
            'product_id': ids['product'], 'enterprise_id': ids['enterprises']['buyer'],
            'subscription_id': sub.id, 'version': 'downgrade',
            'admin_user_id': ids['users']['buyer'], 'oidc_client_id': ids['external']['oidc_client_id']})
        check(isinstance(status, dict) and status.get('ret') == 0 and
              status.get('tenant_id') == ids['external']['tenant'] and
              status.get('product_id') == ids['product'] and
              status.get('enterprise_id') == ids['enterprises']['buyer'] and
              status.get('subscription_id') == sub.id and
              status.get('version') == 'downgrade' and status.get('application_ready') is True and
              status.get('admin_active') is True and
              status.get('admin_user_id') == ids['users']['buyer'] and
              status.get('oidc_client_id') == ids['external']['oidc_client_id'],
              'Independent live mock SaaS STATUS did not verify downgraded tenant')
        ids['database_ids']['payments'] = [fund.payment_id for fund in payments if fund.payment_id]
        ids['database_ids']['refunds'] = [fund.refund_id for fund in refunds if fund.refund_id]
        path_write(ids)
        return {'upgrade_status': change_rows[0].status, 'downgrade_status': change_rows[1].status,
            'fund_payment_count': len(payments), 'fund_refund_count': len(refunds),
            'migrated_term_count': len(terms), 'posted_adjustment_count': len(adjustments),
            'posted_change_totals': posted_totals}


def path_write(ids):
    writer = ids.get('_manifest_writer')
    if writer:
        writer(ids)


def deliver_staged(m, ids, path, client, tokens, task_id):
    last = {}
    for _ in range(20):
        with m.SessionLocal() as db:
            task = db.get(m.DeliveryTask, task_id)
            order = db.get(m.Order, task.order_id) if task else None
            check(task is not None and order is not None, 'Fixture delivery task disappeared')
            last = {'task': task.status, 'order': order.main_status,
                    'delivery': order.delivery_status, 'retry_count': task.retry_count}
            if task.status == 'completed' and order.main_status == 'completed' and order.delivery_status == 'accepted':
                order_id = order.id
                completed = True
            else:
                order_id, completed = order.id, False
            if completed:
                ready, proof = delivery_ready_proof(m, ids, order_id, task_id)
                check(ready, 'Automatic SaaS delivery lacks readiness proof: ' + proof)
                return
            if task.status not in {'in_delivery', 'retrying'}:
                raise AcceptanceFailure('Unexpected automatic delivery state: ' +
                    json.dumps(last, sort_keys=True))
        request(client, tokens['delivery'], 'POST', f'/delivery-tasks/{task_id}/process',
                body={'success': True})
        with m.SessionLocal() as db:
            task = db.get(m.DeliveryTask, task_id)
            order = db.get(m.Order, task.order_id) if task else None
            check(task is not None and order is not None, 'Fixture delivery task disappeared')
            last = {'task': task.status, 'order': order.main_status,
                    'delivery': order.delivery_status, 'retry_count': task.retry_count}
            sub = db.get(m.SaaSSubscription, order.subscription_id) if order.subscription_id else None
            if sub and sub.external_tenant_id:
                check(sub.external_tenant_id == ids['external']['tenant'],
                      'Delivery attempted to bind an unjournaled SaaS tenant')
                ids['external']['tenant_created'] = True
                path_write(ids)
            if task.status == 'exception':
                raise AcceptanceFailure('Automatic SaaS delivery entered exception: ' +
                    json.dumps(last, sort_keys=True) + ', error_present=' + str(bool(task.last_error)))
        time.sleep(1)
    raise AcceptanceFailure('Automatic SaaS delivery timed out: ' + json.dumps(last, sort_keys=True))


def delivery_ready_proof(m, ids, order_id, task_id):
    from sqlalchemy import select
    with m.SessionLocal() as db:
        order, task = db.get(m.Order, order_id), db.get(m.DeliveryTask, task_id)
        sub = db.get(m.SaaSSubscription, order.subscription_id) if order and order.subscription_id else None
        combo = db.scalar(select(m.subscription_terms['Combination']).where(
            m.subscription_terms['Combination'].enterprise_id == ids['enterprises']['buyer'],
            m.subscription_terms['Combination'].product_id == ids['product']))
        binding = db.scalar(select(m.SaaSCombinationBinding).where(
            m.SaaSCombinationBinding.enterprise_id == ids['enterprises']['buyer'],
            m.SaaSCombinationBinding.product_id == ids['product']))
        mapping = db.scalar(select(m.SaaSUserMapping).where(
            m.SaaSUserMapping.subscription_id == sub.id,
            m.SaaSUserMapping.user_id == ids['users']['buyer'],
            m.SaaSUserMapping.status == 'active')) if sub else None
        term = db.scalar(select(m.subscription_terms['Term']).where(
            m.subscription_terms['Term'].order_id == order_id))
        onboarding = db.scalar(select(m.Order).where(
            m.Order.subscription_id == sub.id,
            m.Order.product_id == ids['product'],
            m.Order.buyer_enterprise_id == ids['enterprises']['buyer'],
            m.Order.business_type != 'version_change_payment',
            m.Order.snapshot_version == 1,
            m.Order.payment_status == 'paid').order_by(m.Order.created_at, m.Order.id).limit(1)) if sub else None
        open_operation = db.scalar(select(m.SaaSOperation).where(
            m.SaaSOperation.subscription_id == sub.id,
            m.SaaSOperation.idempotency_key ==
                f'delivery:{sub.id}:{onboarding.id}:OPEN')) if sub and onboarding else None
        ops = db.scalars(select(m.SaaSOperation).where(
            m.SaaSOperation.subscription_id == sub.id,
            m.SaaSOperation.idempotency_key.like(f'delivery:{sub.id}:{order_id}:%'))).all() if sub else []
        operation_states = {op.operation: op.status for op in ops}
        base_ok = bool(order and task and task.status == 'completed' and
            order.main_status == 'completed' and order.delivery_status == 'accepted' and
            order.payment_status == 'paid' and sub and sub.status == 'active' and
            sub.external_tenant_id == ids['external']['tenant'] and
            sub.version_id == ids['versions']['source'] and combo and
            combo.version_id == ids['versions']['source'] and
            binding and binding.subscription_id == sub.id and mapping and term)
        if not base_ok:
            return False, json.dumps({'task': task.status if task else 'missing',
                'order': order.main_status if order else 'missing',
                'delivery': order.delivery_status if order else 'missing',
                'subscription': sub.status if sub else 'missing', 'mapping': bool(mapping),
                'term': bool(term), 'combination': bool(combo),
                'operations': operation_states}, sort_keys=True)
        effective = m.subscription_terms['effective_snapshot'](db, order, term)
        if effective['version_id'] != ids['versions']['source']:
            return False, 'effective source term version mismatch'
        responses = {}
        for op in ops:
            if op.status == 'succeeded':
                try:
                    responses.setdefault(op.operation, []).append(json.loads(op.response_payload or '{}'))
                except (TypeError, ValueError):
                    continue
        try:
            open_response = json.loads(open_operation.response_payload or '{}') if open_operation else {}
        except (TypeError, ValueError):
            open_response = {}
        client = db.scalar(select(m.OIDCClient).where(
            m.OIDCClient.product_id == ids['product'], m.OIDCClient.status == 'active'))
        expected = {'tenant_id': sub.external_tenant_id, 'product_id': ids['product'],
            'enterprise_id': ids['enterprises']['buyer'], 'subscription_id': sub.id}
        verified = bool(client and client.client_id == ids['external']['oidc_client_id'] and
            onboarding and open_operation and open_operation.status == 'succeeded' and
            open_response.get('ret') == 0 and open_response.get('status') == 'opened' and
            open_response.get('version') == 'source' and
            all(open_response.get(k) == v for k, v in expected.items()) and
            any(r.get('ret') == 0 and r.get('status') == 'active' and
                r.get('platform_user_id') == ids['users']['buyer'] and
                r.get('external_user_id') == mapping.external_user_id and
                all(r.get(k) == v for k, v in expected.items()) for r in responses.get('USER_ASSIGN', [])) and
            any(r.get('ret') == 0 and r.get('status') == 'active' and
                all(r.get(k) == v for k, v in expected.items()) for r in responses.get('RENEW', [])) and
            any(r.get('ret') == 0 and r.get('status') == 'active' and
                r.get('admin_active') is True and r.get('application_ready') is True and
                r.get('admin_user_id') == ids['users']['buyer'] and
                r.get('oidc_client_id') == client.client_id and
                all(r.get(k) == v for k, v in expected.items()) for r in responses.get('STATUS', [])))
        config = db.scalar(select(m.SaaSIntegrationConfig).where(
            m.SaaSIntegrationConfig.product_id == ids['product'],
            m.SaaSIntegrationConfig.status == 'active'))
    if not verified or not config:
        return False, json.dumps({'operations': operation_states,
            'mapping': bool(mapping), 'oidc_client': bool(client), 'config': bool(config)}, sort_keys=True)
    live = m.saas_call(config, 'STATUS', dict(expected, version='source',
        admin_user_id=ids['users']['buyer'], oidc_client_id=client.client_id))
    if not (isinstance(live, dict) and live.get('ret') == 0 and live.get('status') == 'active' and
            live.get('version') == 'source' and
            live.get('admin_active') is True and live.get('application_ready') is True and
            all(live.get(k) == v for k, v in expected.items())):
        return False, 'live mock STATUS did not verify tenant readiness'
    return True, 'verified'


def owned_rows(m, ids):
    from sqlalchemy import select
    import version_change_quote_acceptance as guarded
    scope = {'run_id': ids['run_id'], 'users': ids['users'],
        'enterprises': [ids['enterprises']['buyer'], ids['enterprises']['provider']],
        'products': {'saas': ids['product']},
        'versions': {'saas': ids['versions']}, 'request_ids': [ids['request_id']]}
    with m.SessionLocal() as db:
        owned = guarded.owned_rows(m, db, scope)
        orders = {row[0] for row in owned.get(m.Order.__tablename__, set())}
        check(PROTECTED_ORDER not in orders, 'Protected order reached cleanup ownership graph')
        for order in db.scalars(select(m.Order).where(m.Order.id.in_(orders))):
            check(order.product_id == ids['product'] and
                  order.buyer_enterprise_id == ids['enterprises']['buyer'] and
                  order.provider_enterprise_id == ids['enterprises']['provider'] and
                  order.buyer_user_id == ids['users']['buyer'],
                  'Cleanup ownership graph contains an external order')
        return owned


def mock_saas_status(m, config, identity):
    import httpx
    check(config.base_url.rstrip('/') == MOCK_SAAS and
          '/' + config.operation_path.lstrip('/') == '/isv.php',
          'Fixture SaaS STATUS endpoint is not the exact local mock')
    url = config.base_url.rstrip('/') + '/' + config.operation_path.lstrip('/')
    token = m.saas_access_token(config)
    response = httpx.post(url, json={'type': 'STATUS', **identity},
        headers={'Accept': 'application/json', 'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + token}, timeout=config.timeout_ms / 1000,
        follow_redirects=False, trust_env=False)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload


def cleanup(m, ids, manifest_path):
    import httpx
    from sqlalchemy import delete, func, select, tuple_
    check(ids.get('authorized') is True and ids.get('environment') == environment_fingerprint(m),
          'Cleanup environment does not match the authorized manifest')
    owned = owned_rows(m, ids)
    with m.SessionLocal() as db:
        protected = db.scalar(select(m.Order).where(
            (m.Order.id == PROTECTED_ORDER) | (m.Order.order_no == PROTECTED_ORDER)))
        check(protected is not None and fingerprint(db, m) == ids['protected_order_fingerprint'],
              'Protected order fingerprint changed before cleanup')
    # Close only a journaled tenant with a matching persisted subscription identity.
    tenant = ids['external'].get('tenant')
    if tenant:
        with m.SessionLocal() as db:
            sub = db.get(m.SaaSSubscription, ids['database_ids'].get('subscription_id'))
            check(sub is not None and sub.id == ids['database_ids'].get('subscription_id') and
                  sub.product_id == ids['product'] and sub.enterprise_id == ids['enterprises']['buyer'],
                  'Tenant ownership proof failed; refusing external cleanup')
            check(tenant == 'tenant-' + sub.id[:12] and
                  (not sub.external_tenant_id or sub.external_tenant_id == tenant),
                  'Journaled tenant does not match its generated subscription identity')
            config = db.scalar(select(m.SaaSIntegrationConfig).where(
                m.SaaSIntegrationConfig.product_id == ids['product']))
            check(config is not None, 'Fixture SaaS credentials unavailable for tenant close')
            identity = {'tenant_id': tenant, 'product_id': ids['product'],
                'enterprise_id': sub.enterprise_id, 'subscription_id': sub.id}
            status_code, status = mock_saas_status(m, config, identity)
            if status_code == 404:
                check(isinstance(status, dict) and status.get('detail') == 'tenant_not_found' and
                      ids['external'].get('tenant_created') is False,
                      'Only an exact not-found for a never-created tenant permits cleanup to continue')
            else:
                check(status_code == 200 and isinstance(status, dict) and status.get('ret') == 0 and
                      all(status.get(key) == value for key, value in identity.items()),
                      'Mock SaaS STATUS did not prove exact tenant ownership')
                ids['external']['tenant_created'] = True
                write_manifest(manifest_path, ids)
                if status.get('status') != 'closed':
                    payload = dict(identity, idempotency_key=ids['run_id'] + '-cleanup-close')
                    result = m.saas_call(config, 'CLOSE', payload)
                    check(isinstance(result, dict) and result.get('ret') == 0 and
                          result.get('tenant_id') == tenant and result.get('status') == 'closed',
                          'Mock SaaS tenant close was not verified')
                closed_code, closed = mock_saas_status(m, config, identity)
                check(closed_code == 200 and isinstance(closed, dict) and closed.get('ret') == 0 and
                      closed.get('status') == 'closed' and
                      all(closed.get(key) == value for key, value in identity.items()),
                      'Post-close STATUS did not verify the exact closed tenant')
    client_id = ids['external'].get('oidc_client_id')
    if client_id and ids['external'].get('oidc_client_created'):
        response = httpx.delete(MOCK_SAAS + '/_test/oidc/clients/' + client_id,
            headers={'X-Mock-Admin-Secret': mock_admin_secret()}, timeout=10)
        try:
            removed = response.json()
        except ValueError:
            removed = None
        check(response.status_code == 200 and isinstance(removed, dict) and
              removed.get('client_id') == client_id and type(removed.get('deleted')) is bool,
              'Exact fixture mock OIDC client cleanup failed')
    with m.SessionLocal() as db:
        protected = db.scalar(select(m.Order).where(
            (m.Order.id == PROTECTED_ORDER) | (m.Order.order_no == PROTECTED_ORDER)))
        check(protected is not None and fingerprint(db, m) == ids['protected_order_fingerprint'],
              'Protected order fingerprint changed before cleanup')
        for table in reversed(m.Base.metadata.sorted_tables):
            rows = owned.get(table.name)
            if rows:
                db.execute(delete(table).where(tuple_(*table.primary_key.columns).in_(list(rows))))
        db.commit()
    with m.SessionLocal() as db:
        protected = db.scalar(select(m.Order).where(
            (m.Order.id == PROTECTED_ORDER) | (m.Order.order_no == PROTECTED_ORDER)))
        check(protected is not None and fingerprint(db, m) == ids['protected_order_fingerprint'],
              'Protected order fingerprint changed during cleanup')
        for table_name, rows in owned.items():
            if rows:
                table = m.Base.metadata.tables[table_name]
                check(db.scalar(select(func.count()).select_from(table).where(
                    tuple_(*table.primary_key.columns).in_(list(rows)))) == 0,
                    'Owned database rows remain after cleanup')
    Path(manifest_path).unlink()


def run(manifest_path=None):
    global STAGE
    m = load_main()
    ids = fixture_ids()
    path = Path(manifest_path or '/tmp/' + ids['run_id'] + '.json')
    STAGE = 'preflight'
    preflight(m, ids)
    ids['authorized'] = True
    # Install writer only in memory; it is intentionally omitted from JSON.
    def writer(value, create=False):
        write_manifest(path, value, create=create)
    ids['_manifest_writer'] = writer
    writer(ids, create=True)
    client = None
    try:
        STAGE = 'fixture-setup'
        from fastapi.testclient import TestClient
        tokens, machine_secret = prepare_fixture(m, ids)
        # Persist the OIDC identity before the mock service can create it.
        writer(ids)
        register_mock_client(ids, machine_secret)
        client = TestClient(m.app)
        STAGE = 'integration-verification'
        request(client, tokens['provider'], 'POST', f"/products/{ids['product']}/saas-integration/verify")
        STAGE = 'saas-version-change-worker'
        result = worker_acceptance(m, ids, path, client, tokens)
        STAGE = 'cleanup'
        cleanup(m, ids, path)
        return {'status': 'passed', 'scope': 'market-mock-saas upgrade/downgrade worker',
            'results': result, 'worker': 'deployed worker observed via HTTP polling',
            'cleanup': 'fixture database rows removed; exact tenant closed; exact mock OIDC client removed',
            'manifest_mode': '0600'}
    except Exception:
        raise
    finally:
        if client:
            client.close()


def main():
    global STAGE
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--run', action='store_true')
    modes.add_argument('--cleanup', action='store_true')
    parser.add_argument('--manifest')
    args = parser.parse_args()
    if args.cleanup:
        check(args.manifest, '--cleanup requires --manifest')
        STAGE = 'manifest-read'
        ids = read_manifest(args.manifest)
        STAGE = 'cleanup'
        cleanup(load_main(), ids, args.manifest)
        result = {'status': 'passed', 'cleanup': 'exact journaled fixture removed'}
    else:
        result = run(args.manifest)
    print(json.dumps(result, sort_keys=True))


def failure_frame(exc):
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return None
    frame = frames[-1]
    return {'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}


if __name__ == '__main__':
    try:
        main()
    except AcceptanceFailure as exc:
        print(json.dumps({'status': 'failed', 'check': str(exc),
                          'frame': failure_frame(exc)}, sort_keys=True))
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'stage': STAGE,
                          'error_type': type(exc).__name__,
                          'frame': failure_frame(exc)}, sort_keys=True))
        raise SystemExit(1)
