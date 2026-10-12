"""Monthly financial HTTP acceptance in a newly created private PostgreSQL schema.

Uses production models/routes with synthetic paid original/funding/refund and
posted ledger fixtures. This tests real PostgreSQL settlement transactions, not
resource activation, banks, or a deployed gateway. No lifespan or public-schema
seed runs. The unique schema is dropped only if this process created it.
"""
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import MetaData, Table, create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema


def check(value, message):
    if not value:
        raise AssertionError(message)


def protected_fingerprint(engine):
    metadata = MetaData()
    orders = Table('orders', metadata, schema='public', autoload_with=engine, resolve_fks=False)
    products = Table('products', metadata, schema='public', autoload_with=engine, resolve_fks=False)
    with engine.connect() as connection:
        order = connection.execute(select(orders).where(
            orders.c.order_no == 'ORD-261012005144E1B3')).mappings().first()
        product = connection.execute(select(products).where(
            products.c.id == order['product_id'])).mappings().first() if order else None
    return hashlib.sha256(json.dumps(
        [dict(order) if order else None, dict(product) if product else None],
        sort_keys=True, default=str).encode()).hexdigest()


def exercise(m):
    from fastapi.testclient import TestClient
    at = datetime(2091, 3, 15, tzinfo=timezone.utc)
    rule_snapshot = {'mode': 'custom', 'id': '', 'version': 'frozen-fixture-v1',
        'rates': {'platform_rate': '10', 'provider_rate': '90', 'service_rate': '0',
                  'expert_rate': '0', 'channel_rate': '0'}}
    with m.SessionLocal() as db:
        check(db.scalar(text('select current_schema()')) == os.environ['QA_VCS_SCHEMA'],
              'Financial connection escaped its isolated schema')
        db.add_all([m.Enterprise(id='buyer', name='buyer', credit_code='buyer',
                                verification_status='verified'),
                    m.Enterprise(id='provider', name='provider', credit_code='provider',
                                verification_status='verified'),
                    m.User(id='finance', name='finance', email='finance@example.invalid',
                           password_hash='not-a-login', verified_status='verified',
                           activation_status='active', platform_role='finance_settlement')])
        db.flush()
        db.add(m.Product(id='product', name='isolated financial fixture',
            enterprise_id='provider', product_type='api', delivery_method='api',
            status='published', settlement_rule_mode='custom',
            settlement_rule_json=json.dumps(rule_snapshot['rates'])))
        db.add(m.SettlementRule(id='rule', rule_no='RULE-ISOLATED', version='v1',
            name='isolated rule', status='active', platform_rate=10, provider_rate=90,
            service_rate=0, expert_rate=0, channel_rate=0, tax_rate=0))
        db.flush()
        db.add_all([m.ProductReleaseVersion(id=vid, product_id='product', version_code=code,
            price=price, cost=cost, status='active') for vid, code, price, cost in
            [('v1', 'v1', 20, 5), ('v2', 'v2', 30, 8)]])
        db.flush()
        economics = {'schema_version': 1, 'product_id': 'product',
            'product_version_id': 'v1', 'version_code': 'v1', 'billing_unit': 'month',
            'subscription_months': 3, 'unit_price': '20.00', 'unit_cost': '5.00',
            'amount': '60.00', 'total_cost': '15.00', 'currency': 'CNY',
            'settlement_rule': rule_snapshot}
        delivery = {'method': 'api', 'category': 'online', 'quota': {
            'rate_limit_per_minute': 60, 'daily_quota': 0, 'monthly_quota': 10000,
            'quota_amount': 10000}, 'files': []}
        for oid, amount, business in [('original', 60, ''), ('funding', 15, 'version_change_payment')]:
            db.add(m.Order(id=oid, order_no='ORD-ISOLATED-' + oid.upper(),
                buyer_user_id='finance', buyer_enterprise_id='buyer',
                provider_enterprise_id='provider', product_id='product',
                product_name='isolated financial fixture', product_version_id='v1',
                product_version_code='v1', buyer_name='buyer', amount=amount,
                paid_amount=amount, refunded_amount=0, main_status='completed',
                payment_status='paid', delivery_status='accepted', business_type=business,
                snapshot_version=1 if oid == 'original' else 0, subscription_months=3,
                unit_price_snapshot=20, unit_cost_snapshot=5, total_cost_snapshot=15,
                delivery_method_snapshot='api' if oid == 'original' else '',
                economic_snapshot_json=json.dumps(economics),
                delivery_snapshot_json=json.dumps(delivery)))
        db.flush()
        db.add_all([m.Payment(id='pay-' + oid, payment_no='PAY-ISOLATED-' + oid,
            order_id=oid, amount=amount, status='paid', paid_at=at) for oid, amount in
            [('original', 60), ('funding', 15)]])
        Combo = m.subscription_terms['Combination']
        db.add(Combo(id='combination', enterprise_id='buyer', product_id='product',
            delivery_method='api', version_id='v1', version_code='v1', status='active',
            anchor_at=m.now(), total_months=3, generation=1))
        db.flush()
        for cid, kind, old, target in [('upgrade', 'upgrade', 'v1', 'v2'),
                                        ('downgrade', 'downgrade', 'v2', 'v1')]:
            db.add(m.VersionChangeRequest(id=cid, source_order_id='original',
                enterprise_id='buyer', combination_id='combination',
                target_version_id=target, target_version_code=target,
                old_version_id=old, old_version_code=old, kind=kind, status='completed',
                idempotency_key=cid, request_hash='a' * 64, quote_json='{}',
                prepared_json='[]', target_snapshots_json='{}', created_by='finance'))
        db.flush()
        db.add(m.Refund(id='change-refund', refund_no='REF-ISOLATED', order_id='original',
            payment_id='pay-original', amount=15, status='completed', completed_at=at))
        db.flush()
        db.add_all([m.VersionChangeFund(change_id='upgrade', kind='payment',
            payment_id='pay-funding', amount=15, idempotency_key='fund-payment'),
            m.VersionChangeFund(change_id='downgrade', kind='refund', payment_id='pay-original',
            refund_id='change-refund', amount=15, idempotency_key='fund-refund')])
        entries = [('upgrade', 'old', '-30.00', '-7.50', 'v1', 'v2'),
                   ('upgrade', 'target', '45.00', '12.00', 'v1', 'v2'),
                   ('downgrade', 'old', '-45.00', '-12.00', 'v2', 'v1'),
                   ('downgrade', 'target', '30.00', '7.50', 'v2', 'v1')]
        for cid, side, revenue, cost, old, target in entries:
            db.add(m.VersionChangeAdjustment(change_id=cid, request_change_id=cid,
                source_order_id='original', combination_id='combination', side=side,
                revenue=Decimal(revenue), cost=Decimal(cost),
                profit=Decimal(revenue) - Decimal(cost), currency='CNY',
                original_version_id=old, target_version_id=target,
                rule_json=json.dumps(rule_snapshot, sort_keys=True), effective_at=m.now(),
                posted_at=at, status='posted', payload_fingerprint='b' * 64))
        db.commit()
        token = m.issue_token(db.get(m.User, 'finance'))

    client = TestClient(m.app)
    headers = {'Authorization': 'Bearer ' + token}
    def call(method, path, body=None, expected=200):
        response = client.request(method, '/api' + path, headers=headers, json=body)
        check(response.status_code == expected,
              f'Financial HTTP {method} {path} returned {response.status_code}, expected {expected}')
        return response.json()

    period = {'cycle': 'monthly', 'period_start': '2091-03-01T00:00:00+08:00',
              'period_end': '2091-04-01T00:00:00+08:00'}
    try:
        first = call('POST', '/settlement-batches', dict(period, idempotency_key='first'))
        check(first['version_change_count'] == 4 and first['total_amount'] == 60
              and first['total_profit'] == 45, 'Signed monthly totals do not conserve original economics')
        with m.SessionLocal() as db:
            links = dict(db.execute(select(m.VersionChangeSettlementLink.adjustment_id,
                                          m.VersionChangeSettlementLink.settlement_id)).all())
            check(len(links) == 4, 'Expected exactly four uniquely linked adjustment settlements')
            check(db.scalar(select(func.count()).select_from(m.Settlement).where(
                m.Settlement.order_id == 'funding')) == 0, 'Funding payment was settled twice')
            check(db.scalar(select(func.count()).select_from(m.Settlement).where(
                m.Settlement.is_refund.is_(True))) == 0, 'Version-change refund was settled twice')
            lines = db.scalars(select(m.SettlementLine).where(
                m.SettlementLine.batch_id == first['id'])).all()
            check(len(lines) == 25 and sum((line.amount for line in lines), Decimal(0)) == 45,
                  'Five-party lines do not preserve signed profits')
        replay = call('POST', '/settlement-batches', dict(period, idempotency_key='first'))
        check(replay['id'] == first['id'] and replay['idempotent'], 'Batch replay created another batch')
        for settlement_id in links.values():
            detail = call('GET', f'/settlements/{settlement_id}/detail')
            check(detail['version_change']['side'] in {'old', 'target'} and
                  detail['version_change']['rule_version'] == 'frozen-fixture-v1' and
                  any(item['action'] == 'settle_version_change_adjustment' for item in detail['lifecycle']),
                  'Adjustment provenance or precise lifecycle audit is missing')
        call('POST', '/settlements/generate/funding', expected=409)
        second = call('POST', '/settlement-batches', dict(period, idempotency_key='second', rebuild=True))
        check(second['total_amount'] == 60 and second['total_profit'] == 45,
              'Rebuild changed signed monthly totals')
        with m.SessionLocal() as db:
            current_links = dict(db.execute(select(m.VersionChangeSettlementLink.adjustment_id,
                                                   m.VersionChangeSettlementLink.settlement_id)).all())
            check(current_links == links, 'Rebuild duplicated financial adjustment identities')
            active_ids = set(db.scalars(select(m.SettlementLine.settlement_id).where(
                m.SettlementLine.batch_id == second['id'], m.SettlementLine.status != 'superseded')))
            check(db.get(m.SettlementBatch, first['id']).status == 'superseded', 'Prior batch was not superseded')
        reports = call('GET', '/settlement-reports')
        check(reports['summary']['gross_amount'] == 60 and
              reports['summary']['cost_amount'] == 15 and
              reports['summary']['profit_amount'] == 45 and
              sum(item['amount'] for item in reports['summary']['participants']) == 45,
              'Rebuild report includes duplicate or incorrect financial totals')
        check([item['label'] for item in reports['monthly']] == ['2091-03'],
              'Report month follows creation date instead of financial period')
        filtered = call('GET', '/settlement-reports?start=2091-03-01&end=2091-03-31')
        check(filtered['summary']['profit_amount'] == 45,
              'Natural-month filter does not use the financial period')
        exported = client.get('/api/settlement-reports/export', headers=headers)
        check(exported.status_code == 200 and bool(exported.content), 'Monthly report export is unavailable')
        for sid in active_ids:
            call('POST', f'/settlements/{sid}/lock')
        call('POST', f'/settlement-batches/{second["id"]}/confirm', {})
        call('POST', '/settlement-batches', dict(period, idempotency_key='third', rebuild=True), expected=409)
        return {'original_amount': 60, 'original_cost': 15, 'profit': 45,
                'signed_adjustments': 4, 'active_settlements': 5, 'five_party_lines': 25,
                'checks': ['signed totals', 'funding and refund exclusion', 'idempotency',
                           'provenance and audit', 'rebuild identity', 'report export',
                           'confirmed batch immutable']}
    finally:
        client.close()


def run():
    original_url = os.environ['DATABASE_URL']
    url = make_url(original_url)
    check(url.get_backend_name() == 'postgresql', 'PostgreSQL is required')
    admin_engine = create_engine(url, pool_pre_ping=True)
    schema = 'qa_vcs_monthly_' + secrets.token_hex(8)
    created = False
    main_module = None
    before = protected_fingerprint(admin_engine)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        print(json.dumps({'phase': 'isolated schema created', 'schema': schema}), flush=True)
        query = dict(url.query)
        query['options'] = str(query.get('options', '')) + ' -csearch_path=' + schema
        os.environ['DATABASE_URL'] = url.set(query=query).render_as_string(hide_password=False)
        os.environ['QA_VCS_SCHEMA'] = schema
        os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
        from app import main as main_module
        with main_module.engine.connect() as connection:
            check(connection.scalar(text('select current_schema()')) == schema,
                  'Application engine does not use the isolated schema')
        main_module.Base.metadata.create_all(main_module.engine)
        result = exercise(main_module)
    finally:
        os.environ['DATABASE_URL'] = original_url
        if main_module:
            main_module.engine.dispose()
        if created:
            with admin_engine.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        with admin_engine.connect() as connection:
            check(connection.scalar(text('select count(*) from information_schema.schemata '
                'where schema_name=:schema'), {'schema': schema}) == 0,
                'Isolated financial schema was not removed')
        after = protected_fingerprint(admin_engine)
        admin_engine.dispose()
        check(before == after, 'Protected order or dependent product changed during acceptance')
    return {'status': 'passed', 'scope': 'isolated PostgreSQL financial HTTP',
            'synthetic_fixture': True, 'result': result,
            'cleanup': 'isolated schema removed; protected public order and product unchanged',
            'not_tested': ['bank transfers', 'gateway resource activation', 'browser flow']}


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(run(), sort_keys=True))
    except AssertionError as exc:
        print(json.dumps({'status': 'failed', 'check': str(exc)}))
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        raise SystemExit(1)
