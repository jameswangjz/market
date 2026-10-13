"""Isolated PostgreSQL HTTP regression for cross-month version settlements.

All timestamps are controlled synthetic ledger fixtures. The private schema is
created before importing the application and dropped only by its creator.
"""
import hashlib
import json
import logging
import os
import secrets
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

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

    original_paid = datetime(2091, 1, 31, 16, tzinfo=timezone.utc)
    upgrade_posted = datetime(2091, 2, 28, 16, tzinfo=timezone.utc)
    downgrade_posted = datetime(2091, 3, 31, 16, tzinfo=timezone.utc)
    frozen = {
        'old': {'mode': 'custom', 'id': '', 'version': 'old-frozen-v1', 'rates': {
            'platform_rate': '10', 'provider_rate': '40', 'service_rate': '20',
            'expert_rate': '20', 'channel_rate': '10'}},
        'target': {'mode': 'custom', 'id': '', 'version': 'target-frozen-v9', 'rates': {
            'platform_rate': '30', 'provider_rate': '30', 'service_rate': '15',
            'expert_rate': '15', 'channel_rate': '10'}},
    }
    with m.SessionLocal() as db:
        check(db.scalar(text('select current_schema()')) == os.environ['QA_VCC_SCHEMA'],
              'Application connection escaped its isolated schema')
        db.add_all([
            m.Enterprise(id='buyer', name='fixture buyer', credit_code='buyer', verification_status='verified'),
            m.Enterprise(id='provider', name='fixture provider', credit_code='provider', verification_status='verified'),
            m.User(id='finance', name='finance', email='finance@example.invalid',
                   password_hash='not-a-login', verified_status='verified',
                   activation_status='active', platform_role='finance_settlement'),
        ])
        db.flush()
        db.add(m.Product(id='product', name='cross-month fixture', enterprise_id='provider',
            product_type='api', delivery_method='api', status='published',
            settlement_rule_mode='custom', settlement_rule_json=json.dumps(frozen['old']['rates'])))
        db.add(m.SettlementRule(id='rule', rule_no='RULE-CROSS-MONTH', version='v1',
            name='isolated batch rule', status='active', platform_rate=10, provider_rate=90,
            service_rate=0, expert_rate=0, channel_rate=0, tax_rate=0))
        db.flush()
        db.add_all([m.ProductReleaseVersion(id=vid, product_id='product', version_code=code,
            price=price, cost=cost, status='active') for vid, code, price, cost in
            [('v1', 'v1', 20, 5), ('v2', 'v2', 30, 8)]])
        db.flush()
        economics = {'schema_version': 1, 'product_id': 'product', 'product_version_id': 'v1',
            'version_code': 'v1', 'billing_unit': 'month', 'subscription_months': 3,
            'unit_price': '20.00', 'unit_cost': '5.00', 'amount': '60.00',
            'total_cost': '15.00', 'currency': 'CNY', 'settlement_rule': frozen['old']}
        delivery = {'method': 'api', 'category': 'online', 'quota': {
            'rate_limit_per_minute': 60, 'daily_quota': 0, 'monthly_quota': 10000,
            'quota_amount': 10000}, 'files': []}
        for oid, amount, business in [('original', 60, ''), ('funding', 15, 'version_change_payment')]:
            db.add(m.Order(id=oid, order_no='ORD-CROSS-' + oid.upper(), buyer_user_id='finance',
                buyer_enterprise_id='buyer', provider_enterprise_id='provider', product_id='product',
                product_name='cross-month fixture', product_version_id='v1', product_version_code='v1',
                buyer_name='fixture buyer', amount=amount, paid_amount=amount, refunded_amount=0,
                main_status='completed', payment_status='paid', delivery_status='accepted',
                business_type=business, snapshot_version=1 if oid == 'original' else 0,
                subscription_months=3, unit_price_snapshot=20, unit_cost_snapshot=5,
                total_cost_snapshot=15, delivery_method_snapshot='api' if oid == 'original' else '',
                economic_snapshot_json=json.dumps(economics), delivery_snapshot_json=json.dumps(delivery)))
        db.flush()
        db.add_all([m.Payment(id='pay-' + oid, payment_no='PAY-CROSS-' + oid,
            order_id=oid, amount=amount, status='paid', paid_at=paid_at)
            for oid, amount, paid_at in [('original', 60, original_paid),
                                         ('funding', 15, upgrade_posted)]])
        Combo = m.subscription_terms['Combination']
        db.add(Combo(id='combination', enterprise_id='buyer', product_id='product',
            delivery_method='api', version_id='v1', version_code='v1', status='active',
            anchor_at=m.now(), total_months=3, generation=1))
        db.flush()
        for cid, kind, old, target in [('upgrade', 'upgrade', 'v1', 'v2'),
                                       ('downgrade', 'downgrade', 'v2', 'v1')]:
            db.add(m.VersionChangeRequest(id=cid, source_order_id='original', enterprise_id='buyer',
                combination_id='combination', target_version_id=target, target_version_code=target,
                old_version_id=old, old_version_code=old, kind=kind, status='completed',
                idempotency_key=cid, request_hash=(cid[0] * 64), quote_json='{}',
                prepared_json='[]', target_snapshots_json='{}', created_by='finance'))
        db.flush()
        db.add(m.Refund(id='change-refund', refund_no='REF-CROSS', order_id='original',
            payment_id='pay-original', amount=15, status='completed', completed_at=downgrade_posted))
        db.flush()
        db.add_all([m.VersionChangeFund(change_id='upgrade', kind='payment', payment_id='pay-funding',
            amount=15, idempotency_key='cross-upgrade-fund'),
            m.VersionChangeFund(change_id='downgrade', kind='refund', payment_id='pay-original',
            refund_id='change-refund', amount=15, idempotency_key='cross-downgrade-fund')])
        entries = [
            ('upgrade', 'old', '-30.00', '-7.50', 'v1', 'v2', upgrade_posted),
            ('upgrade', 'target', '45.00', '12.00', 'v1', 'v2', upgrade_posted),
            ('downgrade', 'old', '-45.00', '-12.00', 'v2', 'v1', downgrade_posted),
            ('downgrade', 'target', '30.00', '7.50', 'v2', 'v1', downgrade_posted),
        ]
        for cid, side, revenue, cost, old, target, posted in entries:
            db.add(m.VersionChangeAdjustment(change_id=cid, request_change_id=cid,
                source_order_id='original', combination_id='combination', side=side,
                revenue=Decimal(revenue), cost=Decimal(cost),
                profit=Decimal(revenue) - Decimal(cost), currency='CNY',
                original_version_id=old, target_version_id=target,
                rule_json=json.dumps(frozen[side], sort_keys=True), effective_at=posted,
                posted_at=posted, status='posted', payload_fingerprint=(cid[0] + side[0]) * 32))
        db.commit()
        token = m.issue_token(db.get(m.User, 'finance'))

    client = TestClient(m.app)
    headers = {'Authorization': 'Bearer ' + token}
    def financial_snapshot(db, batch_id):
        batch = db.get(m.SettlementBatch, batch_id)
        lines = db.scalars(select(m.SettlementLine).where(
            m.SettlementLine.batch_id == batch_id).order_by(m.SettlementLine.id)).all()
        settlement_ids = sorted({line.settlement_id for line in lines})
        settlements = [db.get(m.Settlement, sid) for sid in settlement_ids]
        return json.dumps({
            'batch': [batch.id, batch.status, str(batch.total_amount), str(batch.total_profit),
                      str(batch.period_start), str(batch.period_end)],
            'lines': sorted((line.settlement_id, line.participant_type,
                             str(line.amount), line.status) for line in lines),
            'settlements': sorted((row.id, str(row.gross_amount), str(row.cost_amount),
                                   str(row.profit_amount), row.status) for row in settlements),
        }, sort_keys=True)

    def check_adjustment_shares(db, batch_id, expected_adjustment_ids):
        Link, Adjustment, Line = (m.VersionChangeSettlementLink,
                                  m.VersionChangeAdjustment, m.SettlementLine)
        linked = db.execute(select(Link.adjustment_id, Link.settlement_id).join(
            Line, Line.settlement_id == Link.settlement_id).where(
                Line.batch_id == batch_id).distinct()).all()
        check({adjustment_id for adjustment_id, _ in linked} == set(expected_adjustment_ids),
              'Batch has unexpected or missing version-change settlement links')
        by_settlement = {settlement_id: adjustment_id for adjustment_id, settlement_id in linked}
        lines = db.scalars(select(Line).where(Line.batch_id == batch_id,
                                               Line.settlement_id.in_(by_settlement))).all()
        for settlement_id, adjustment_id in by_settlement.items():
            adjustment = db.get(Adjustment, adjustment_id)
            snapshot = json.loads(adjustment.rule_json)
            rates = {key: Decimal(str(value)) for key, value in snapshot['rates'].items()}
            profit = Decimal(str(adjustment.profit))
            expected = {
                'platform': (profit * rates['platform_rate'] / 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP),
                'provider': (profit * rates['provider_rate'] / 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP),
                'service': (profit * rates['service_rate'] / 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP),
                'expert': (profit * rates['expert_rate'] / 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP),
                'channel': (profit * rates['channel_rate'] / 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP),
            }
            expected['provider'] += profit - sum(expected.values())
            actual_lines = [line for line in lines if line.settlement_id == settlement_id]
            actual = {line.participant_type: Decimal(str(line.amount)) for line in actual_lines}
            check(len(actual_lines) == 5 and set(actual) == set(expected) and actual == expected,
                  f'{adjustment.change_id}/{adjustment.side} five-party amounts do not match its frozen rule')

    def call(method, path, body=None, expected=200):
        response = client.request(method, '/api' + path, headers=headers, json=body)
        check(response.status_code == expected,
              f'Cross-month HTTP {method} {path} returned {response.status_code}, expected {expected}')
        return response.json() if response.content else {}

    months = [
        ('2091-02', '2091-02-01T00:00:00+08:00', '2091-03-01T00:00:00+08:00', '2091-02-28', 60, 15, 45, 0),
        ('2091-03', '2091-03-01T00:00:00+08:00', '2091-04-01T00:00:00+08:00', '2091-03-31', 15, Decimal('4.50'), Decimal('10.50'), 2),
        ('2091-04', '2091-04-01T00:00:00+08:00', '2091-05-01T00:00:00+08:00', '2091-04-30', -15, Decimal('-4.50'), Decimal('-10.50'), 2),
    ]
    try:
        batches = {}
        batch_snapshots = {}
        for month, start, end, report_end, amount, cost, profit, adjustment_count in months:
            body = {'cycle': 'monthly', 'period_start': start, 'period_end': end,
                    'idempotency_key': 'cross-month-' + month}
            batch = call('POST', '/settlement-batches', body)
            check(Decimal(str(batch['total_amount'])) == amount and
                  Decimal(str(batch['total_profit'])) == profit and
                  batch['version_change_count'] == adjustment_count,
                  f'{month} batch has incorrect signed gross/profit totals')
            batches[month] = batch
            with m.SessionLocal() as db:
                batch_snapshots[month] = financial_snapshot(db, batch['id'])
            with m.SessionLocal() as db:
                adj_ids = set(db.scalars(select(m.VersionChangeSettlementLink.adjustment_id).join(
                    m.Settlement, m.Settlement.id == m.VersionChangeSettlementLink.settlement_id).join(
                    m.SettlementLine, m.SettlementLine.settlement_id == m.Settlement.id).where(
                        m.SettlementLine.batch_id == batch['id'])))
                expected = {'upgrade-old', 'upgrade-target'} if month == '2091-03' else (
                    {'downgrade-old', 'downgrade-target'} if month == '2091-04' else set())
                expected_ids = set(db.scalars(select(m.VersionChangeAdjustment.id).where(
                    m.VersionChangeAdjustment.change_id.in_(['upgrade', 'downgrade']))))
                if expected:
                    rows = db.scalars(select(m.VersionChangeAdjustment).where(
                        m.VersionChangeAdjustment.id.in_(adj_ids))).all()
                    check(len(rows) == adjustment_count and
                          {row.change_id + '-' + row.side for row in rows} == expected,
                          f'{month} batch contains wrong adjustment timestamps')
                    check(sum((Decimal(str(row.cost)) for row in rows), Decimal('0')) == cost,
                          f'{month} signed adjustment costs are incorrect')
                    check(all(json.loads(row.rule_json) == frozen[row.side] for row in rows),
                          f'{month} adjustment lost its frozen old/target rule snapshot')
                    check_adjustment_shares(db, batch['id'], {row.id for row in rows})
                else:
                    check(not adj_ids and len(expected_ids) == 4,
                          'February original batch unexpectedly contains version adjustments')
                lines = db.scalars(select(m.SettlementLine).where(
                    m.SettlementLine.batch_id == batch['id'])).all()
                settlements = {line.settlement_id for line in lines}
                check(len(lines) == 5 * len(settlements), f'{month} does not have five-party lines')
                actual_cost = sum((Decimal(str(db.get(m.Settlement, sid).cost_amount))
                                   for sid in settlements), Decimal('0'))
                check(actual_cost == cost, f'{month} batch cost does not match signed ledger cost')
                check(not db.scalar(select(func.count()).select_from(m.Settlement).where(
                    m.Settlement.order_id == 'funding')), 'Funding order was settled independently')
                check(not db.scalar(select(func.count()).select_from(m.Settlement).where(
                    m.Settlement.is_refund.is_(True))), 'Change refund was settled independently')

            # Every preceding month must remain byte-for-byte financially unchanged.
            for prior_month, prior in list(batches.items())[:-1]:
                with m.SessionLocal() as db:
                    prior_now = db.get(m.SettlementBatch, prior['id'])
                    check(prior_now.status == 'generated' and
                          Decimal(str(prior_now.total_amount)) == next(x[4] for x in months if x[0] == prior_month) and
                          Decimal(str(prior_now.total_profit)) == next(x[6] for x in months if x[0] == prior_month) and
                          financial_snapshot(db, prior['id']) == batch_snapshots[prior_month],
                          f'Generating {month} changed prior {prior_month} batch')

            replay = call('POST', '/settlement-batches', body)
            check(replay['id'] == batch['id'] and replay.get('idempotent'),
                  f'{month} replay was not idempotent')
            report = call('GET', '/settlement-reports?start=' + start[:10] + '&end=' + report_end)
            check(len(report['monthly']) == 1 and report['monthly'][0]['label'] == month and
                  Decimal(str(report['summary']['gross_amount'])) == amount and
                  Decimal(str(report['summary']['cost_amount'])) == cost and
                  Decimal(str(report['summary']['profit_amount'])) == profit,
                  f'{month} month-filtered report includes another month or wrong signed totals')
            exported = client.get('/api/settlement-reports/export', params={
                'start': start[:10], 'end': report_end, 'kind': 'summary'}, headers=headers)
            check(exported.status_code == 200 and bool(exported.content),
                  f'{month} month-filtered export failed')
        # Rebuild April and ensure it preserves the signed adjustment identities.
        april = batches['2091-04']
        rebuild = call('POST', '/settlement-batches', {
            'cycle': 'monthly', 'period_start': months[2][1], 'period_end': months[2][2],
            'idempotency_key': 'cross-month-april-rebuild', 'rebuild': True})
        check(Decimal(str(rebuild['total_amount'])) == -15 and
              Decimal(str(rebuild['total_profit'])) == Decimal('-10.50'),
              'April rebuild changed signed totals')
        with m.SessionLocal() as db:
            links = db.scalars(select(m.VersionChangeSettlementLink.adjustment_id).join(
                m.Settlement, m.Settlement.id == m.VersionChangeSettlementLink.settlement_id).join(
                m.SettlementLine, m.SettlementLine.settlement_id == m.VersionChangeSettlementLink.settlement_id).where(
                    m.SettlementLine.batch_id == rebuild['id']).distinct()).all()
            check(len(links) == 2 and len(set(links)) == 2, 'April rebuild duplicated adjustment identities')
            check_adjustment_shares(db, rebuild['id'], set(links))
            active = set(db.scalars(select(m.SettlementLine.settlement_id).where(
                m.SettlementLine.batch_id == rebuild['id'], m.SettlementLine.status != 'superseded')))
        for sid in active:
            call('POST', f'/settlements/{sid}/lock')
        call('POST', f'/settlement-batches/{rebuild["id"]}/confirm', {})
        call('POST', '/settlement-batches', {
            'cycle': 'monthly', 'period_start': months[2][1], 'period_end': months[2][2],
            'idempotency_key': 'cross-month-april-after-confirm', 'rebuild': True}, expected=409)
        return {'months': {month: {'amount': str(amount), 'cost': str(cost),
                    'profit': str(profit), 'adjustments': count}
                    for month, _, _, _, amount, cost, profit, count in months},
                'checks': ['synthetic UTC instants bucketed in Asia/Shanghai',
                    'exact per-month adjustment membership and signed totals',
                    'frozen old/target rule snapshots and five-party lines',
                    'funding and change-refund exclusion', 'month-filtered reports and exports',
                    'replay, rebuild identity, prior-batch immutability, confirmed immutability']}
    finally:
        client.close()


def run():
    original_url = os.environ['DATABASE_URL']
    url = make_url(original_url)
    check(url.get_backend_name() == 'postgresql', 'PostgreSQL is required')
    admin = create_engine(url, pool_pre_ping=True)
    schema = 'qa_vcc_month_' + secrets.token_hex(8)
    created = False
    app_module = None
    before = protected_fingerprint(admin)
    result = None
    try:
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        query = dict(url.query)
        query['options'] = str(query.get('options', '')) + ' -csearch_path=' + schema
        os.environ['DATABASE_URL'] = url.set(query=query).render_as_string(hide_password=False)
        os.environ['QA_VCC_SCHEMA'] = schema
        os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
        backend = str(Path(__file__).resolve().parents[1] / 'backend')
        sys.path.insert(0, backend)
        from app import main as app_module
        with app_module.engine.connect() as connection:
            check(connection.scalar(text('select current_schema()')) == schema,
                  'Application engine does not use the isolated schema')
        app_module.Base.metadata.create_all(app_module.engine)
        result = exercise(app_module)
    finally:
        os.environ['DATABASE_URL'] = original_url
        if app_module:
            app_module.engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        with admin.connect() as connection:
            check(connection.scalar(text('select count(*) from information_schema.schemata '
                'where schema_name=:schema'), {'schema': schema}) == 0,
                'Private schema cleanup failed')
        after = protected_fingerprint(admin)
        admin.dispose()
        check(before == after, 'Protected order or dependent product changed')
    return {'status': 'passed', 'scope': 'isolated PostgreSQL production HTTP',
            'synthetic_fixture': True, 'result': result,
            'cleanup': 'private schema removed; protected public order and product unchanged',
            'not_tested': ['real-clock trades', 'subscription-term activation', 'bank transfers', 'gateway activation']}


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        evidence = run()
        print(json.dumps(evidence, sort_keys=True), flush=True)
    except AssertionError as exc:
        print(json.dumps({'status': 'failed', 'check': str(exc)}), flush=True)
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1)
