"""Local isolated PG combination: actual Saga orchestration, simulated supplier/funds.

Run locally with R3_MONTHLY_DATABASE_URL, or explicitly --pod after review.
No lifespan, deployment, or bank calls. Only the initial paid
subscription is seeded; changes, revisions, windows and adjustments are not.
"""
import argparse
from contextlib import ExitStack, contextmanager
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import importlib
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import subprocess
from unittest.mock import patch

import r3_refund_settlement_pg_acceptance as harness

check = harness.base.check
PROTECTED = 'ORD-261012005144E1B3'
RATES = dict(platform_rate=20, provider_rate=50, service_rate=10, expert_rate=10, channel_rate=10)
PARTIES = ('platform', 'provider', 'service', 'expert', 'channel')
SHARE_FIELDS = ('platform_fee', 'provider_share', 'service_share', 'expert_fee', 'channel_fee')
CSV_SHARES = ('\u5e73\u53f0\u8fd0\u8425\u65b9', '\u6570\u636e/\u670d\u52a1\u63d0\u4f9b\u65b9',
              '\u6570\u636e\u670d\u52a1\u65b9', '\u4e13\u5bb6', '\u6e20\u9053')
ANCHOR = datetime(2026, 12, 15, tzinfo=timezone.utc)
UPGRADE = datetime(2027, 1, 2, tzinfo=timezone.utc)
DOWNGRADE = datetime(2027, 2, 2, tzinfo=timezone.utc)
REFUND_AT = datetime(2027, 3, 2, tzinfo=timezone.utc)
END = datetime(2027, 6, 15, tzinfo=timezone.utc)
MONTHS = ('2026-12', '2027-01', '2027-02', '2027-03')
CASE_NAMES = ('financial_cross_month_real_saga_combination',)
SOURCE_FILES = tuple(dict.fromkeys((*harness.SOURCES, 'version_change_quotes.py',
    'version_change_saga.py', 'version_change_worker.py', 'version_change_resources.py',
    'version_change_ledger.py', 'version_change_settlement.py', 'refund_segment_store.py',
    'refund_funding_store.py', 'subscription_calendar.py', 'subscription_terms.py',
    'order_economics.py', 'saas_combinations.py', 'oidc_provider.py', 'refund_file_resources.py')))


def money(value):
    return Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def period(month):
    year, number = map(int, month.split('-'))
    following = (year + 1, 1) if number == 12 else (year, number + 1)
    return (f'{month}-01T00:00:00+08:00', f'{following[0]:04d}-{following[1]:02d}-01T00:00:00+08:00')


def month_end(month):
    return datetime.fromisoformat(period(month)[1]) - timedelta(microseconds=1)


@contextmanager
def batch_clock(m, month):
    """Synthetic lifecycle clock, including ORM defaults bound before patching now."""
    from sqlalchemy import event
    at = month_end(month)

    def stamp(_mapper, _connection, row):
        row.created_at = at

    with ExitStack() as stack:
        stack.enter_context(patch.object(m, 'now', return_value=at))
        for model in (m.SettlementBatch, m.Settlement, m.SettlementLine, m.AuditLog):
            event.listen(model, 'before_insert', stamp)
            stack.callback(event.remove, model, 'before_insert', stamp)
        yield at


class Supplier:
    """Explicit external simulator; production adapters verify every response."""
    def __init__(self, m):
        self.m = m
        self.version = 'v1'
        self.expiry = END.isoformat()
        self.changes = {}
        self.refunds = {}
        self.last_refund = None
        self.calls = []

    def __call__(self, config, operation, payload):
        check(config.id == 'comb-config', 'Supplier config escaped fixture')
        expected = dict(tenant_id='comb-tenant', product_id='comb-product',
                        enterprise_id='owned-buyer', subscription_id='comb-sub')
        check(all(payload.get(k) == v for k, v in expected.items()), 'Supplier ownership mismatch')
        check(operation in ('CHANGE', 'STATUS', 'RENEW'), 'Unexpected supplier operation')
        self.calls.append(operation)
        evidence = {}
        if 'change_id' in payload:
            key = (payload['change_id'], payload['direction'])
            if operation == 'CHANGE':
                frozen = dict(version=payload['to_version'], effective_at=self.m.now().isoformat())
                if key in self.changes:
                    check(self.changes[key]['version'] == frozen['version'], 'Changed CHANGE replay')
                else:
                    self.changes[key] = frozen
                self.version = self.changes[key]['version']
            check(key in self.changes, 'STATUS before CHANGE')
            evidence = dict(change_id=key[0], direction=key[1],
                            effective_at=self.changes[key]['effective_at'])
        else:
            check(payload.get('action') == 'refund_shorten' and
                  payload.get('protocol') == 'refund_shorten_v1', 'Unexpected refund protocol')
            rid = payload['refund_id']
            if operation == 'RENEW':
                check(payload['idempotency_key'] == 'refund-shorten:' + rid, 'Unsafe shortening key')
                if rid not in self.refunds:
                    check(payload['expected_expires_at'] == self.expiry, 'Supplier expiry conflict')
                    check(datetime.fromisoformat(payload['expires_at']) < datetime.fromisoformat(self.expiry),
                          'Refund must shorten supplier expiry')
                    self.refunds[rid] = payload['expires_at']
                check(self.refunds[rid] == payload['expires_at'], 'Changed shortening replay')
                self.expiry, self.last_refund = self.refunds[rid], rid
            evidence = dict(refund_shorten_protocol='refund_shorten_v1', refund_shorten_supported=True,
                            refund_id=self.last_refund)
        return dict(ret=0, **expected, version=self.version, status='active',
                    admin_user_id='owned-buyer', admin_active=True, application_ready=True,
                    oidc_client_id='comb-oidc', expires_at=self.expiry, **evidence)


def seed(m):
    """Seed baseline only; all later financial history must come from handlers."""
    from sqlalchemy import select
    headers = harness.fixture.seed(m)
    with m.SessionLocal.begin() as db:
        db.add(m.Product(id='comb-product', enterprise_id='owned-provider', name='Monthly combination SaaS',
            product_type='saas', delivery_method='tenant_access', status='published', currency='CNY',
            application_url='https://supplier.invalid/app',
            settlement_rule_mode='custom', settlement_rule_json=json.dumps(RATES)))
        db.flush()
        for vid, code, price, cost in (('comb-v1', 'v1', 100, 20), ('comb-v2', 'v2', 110, 33)):
            db.add(m.SaaSProductVersion(id=vid, product_id='comb-product', name=code,
                version_code=code, monthly_price=price, cost=cost, status='active'))
        db.add(m.SaaSIntegrationConfig(id='comb-config', product_id='comb-product',
            base_url='https://supplier.invalid', token_url='https://supplier.invalid/token',
            client_id='comb-simulated-client', client_secret='simulation-only-not-a-credential',
            auth_mode='oauth2', status='active'))
        db.add(m.OIDCClient(id='comb-client', product_id='comb-product', client_id='comb-oidc',
            secret_hash='0' * 64, status='active'))
        db.add(m.SettlementRule(id='comb-rule', rule_no='COMB-RULE', version='v1', name='Combination rule',
            status='active', **RATES, tax_rate=0))
        db.flush()
        db.add(m.SaaSSubscription(id='comb-sub', enterprise_id='owned-buyer', product_id='comb-product',
            version_id='comb-v1', external_tenant_id='comb-tenant', billing_cycle='monthly',
            starts_at=ANCHOR, expires_at=END, status='active'))
        db.flush()
        db.add(m.SaaSUserMapping(subscription_id='comb-sub', user_id='owned-buyer',
            external_user_id='comb-external-admin', status='active'))
        db.add(m.saas_combinations['Binding'](enterprise_id='owned-buyer', product_id='comb-product',
            subscription_id='comb-sub'))
        economic = dict(schema_version=1, product_id='comb-product', product_version_id='comb-v1',
            version_code='v1', billing_unit='month', subscription_months=6, unit_price='100.00',
            unit_cost='20.00', amount='600.00', total_cost='120.00', currency='CNY',
            settlement_rule=dict(mode='custom', id='', version='comb-frozen', rates=RATES))
        delivery = dict(method='tenant_access', files=[], download_limit=0,
            quota=dict(rate_limit_per_minute=0, daily_quota=0, monthly_quota=0, quota_amount=0))
        db.add(m.Order(id='comb-order', order_no='OWNED-COMB-ORIGINAL', product_id='comb-product',
            product_name='Monthly combination SaaS', buyer_user_id='owned-buyer', buyer_enterprise_id='owned-buyer',
            provider_enterprise_id='owned-provider', buyer_name='Owned buyer', subscription_id='comb-sub',
            amount=600, paid_amount=600, refunded_amount=0, main_status='completed', payment_status='paid',
            delivery_status='accepted', snapshot_version=1, product_version_id='comb-v1',
            product_version_code='v1', subscription_months=6, unit_price_snapshot=100,
            unit_cost_snapshot=20, total_cost_snapshot=120, delivery_method_snapshot='tenant_access',
            economic_snapshot_json=json.dumps(economic), delivery_snapshot_json=json.dumps(delivery)))
        db.flush()
        db.add(m.Payment(id='comb-payment', order_id='comb-order', payment_no='OWNED-COMB-PAY',
            amount=600, status='paid', paid_at=ANCHOR))
        with patch.object(m, 'now', return_value=ANCHOR):
            m.subscription_terms['on_delivery_success'](db, db.get(m.Order, 'comb-order'),
                db.get(m.User, 'owned-admin'), at=ANCHOR)
        for model in (m.VersionChangeRequest, m.VersionChangeAdjustment, m.VersionChangeFund,
                      m.refund_segment_store['Receipt'], m.refund_segment_store['Window']):
            check(db.scalar(select(model).limit(1)) is None, 'Seed contains version-change history')
    return headers


def exercise(m, client, headers):
    from sqlalchemy import func, select
    check(not m.app.dependency_overrides, 'Dependency overrides forbidden')
    saga_worker = importlib.import_module(m.__package__ + '.version_change_worker')
    refund_worker = importlib.import_module(m.__package__ + '.refund_execution_worker')
    supplier = Supplier(m)
    changes, refunds, batches, snapshots = [], [], {}, {}
    expected = {}

    def call(method, path, body=None, actor='admin', expected_status=200, params=None):
        response = client.request(method, '/api' + path, json=body, headers=headers[actor], params=params)
        check(response.status_code == expected_status,
              f'{method} {path}: HTTP {response.status_code}, expected {expected_status}')
        return response.json() if expected_status < 400 else None

    def snapshot(bid):
        with m.SessionLocal() as db:
            ids = select(m.SettlementLine.settlement_id).where(m.SettlementLine.batch_id == bid)
            return tuple(tuple(tuple(row) for row in db.execute(select(model.__table__).where(where)
                .order_by(*model.__table__.primary_key.columns))) for model, where in (
                    (m.SettlementBatch, m.SettlementBatch.id == bid),
                    (m.SettlementLine, m.SettlementLine.batch_id == bid),
                    (m.Settlement, m.Settlement.id.in_(ids))))

    def unchanged():
        for month, old in snapshots.items():
            check(snapshot(batches[month]['id']) == old, 'Earlier paid batch changed: ' + month)

    def lifecycle(month, bid, *, confirmed=False, paid=False):
        at = month_end(month)
        transaction_at = dict(zip(MONTHS, (ANCHOR, UPGRADE, DOWNGRADE, REFUND_AT)))[month]
        with m.SessionLocal() as db:
            batch = db.get(m.SettlementBatch, bid)
            lines = list(db.scalars(select(m.SettlementLine).where(m.SettlementLine.batch_id == bid)))
            settlements = list(db.scalars(select(m.Settlement).where(
                m.Settlement.id.in_({line.settlement_id for line in lines}))))
            audits = list(db.scalars(select(m.AuditLog).where(m.AuditLog.batch_no == batch.batch_no)))
            check(audits and any(row.action == 'create_settlement_batch' for row in audits),
                  'Missing batch lifecycle audit')
            timestamps = [row.created_at for row in (batch, *lines, *settlements, *audits)]
            for required, field, action in ((confirmed, 'confirmed_at', 'confirm_settlement_batch'),
                                             (paid, 'paid_at', 'pay_settlement_batch')):
                if required:
                    check(getattr(batch, field) is not None and any(row.action == action for row in audits),
                          'Missing batch lifecycle timestamp/audit: ' + field)
                    timestamps.append(getattr(batch, field))
            check(all(value is not None and transaction_at <= value == at for value in timestamps),
                  'Batch lifecycle predates transaction or differs from synthetic month end: ' + month)

    def generate(month, suffix='', **extra):
        start, end = period(month)
        with batch_clock(m, month):
            result = call('POST', '/settlement-batches', dict(cycle='monthly', period_start=start,
                period_end=end, order_ids=['comb-order'], idempotency_key='comb-' + month + suffix, **extra))
        lifecycle(month, result['id'])
        return result

    def inspect(month, batch, expected_rows):
        frozen_rows = {}
        with m.SessionLocal() as db:
            lines = list(db.scalars(select(m.SettlementLine).where(m.SettlementLine.batch_id == batch['id'])))
            ids = {line.settlement_id for line in lines}
            rows = list(db.scalars(select(m.Settlement).where(m.Settlement.id.in_(ids))))
            check(len(rows) == expected_rows and len(lines) == expected_rows * 5, 'Monthly membership/count mismatch')
            totals = tuple(sum((getattr(row, field) for row in rows), Decimal(0))
                for field in ('net_amount', 'cost_amount', 'profit_amount'))
            check(totals == expected[month], 'Monthly signed economics mismatch: ' + month)
            check(money(batch['total_amount']) == totals[0] and money(batch['total_profit']) == totals[2],
                  'Batch totals mismatch')
            for row in rows:
                own = [line for line in lines if line.settlement_id == row.id]
                shares = {line.participant_type: line.amount for line in own}
                check(set(shares) == set(PARTIES) and sum(shares.values()) == row.profit_amount,
                      'Five-party lines do not reconcile')
                check(all(value != 0 for value in shares.values()), 'All five shares must be nonzero')
                check(tuple(shares[p] for p in PARTIES) == tuple(getattr(row, k) for k in SHARE_FIELDS),
                      'Line shares differ from settlement')
                frozen_rows[row.settlement_no] = dict(shares=tuple(shares[p] for p in PARTIES),
                    amounts=(row.net_amount, row.cost_amount, row.profit_amount), refund_id=row.refund_id,
                    order_id=row.order_id)
                if month == MONTHS[0]:
                    check(row.order_id == 'comb-order' and not row.is_refund, 'Original membership mismatch')
                elif month == MONTHS[3]:
                    check(row.refund_id in refunds and row.is_refund, 'Ordinary refund identity lost')
                    exact = tuple(money(Decimal('-32') * Decimal(rate) / 100) for rate in RATES.values())
                    check(tuple(shares[p] for p in PARTIES) == exact, 'Frozen refund five-party shares mismatch')
                else:
                    link = db.scalar(select(m.VersionChangeSettlementLink).where(
                        m.VersionChangeSettlementLink.settlement_id == row.id))
                    adjustment = db.get(m.VersionChangeAdjustment, link.adjustment_id) if link else None
                    cid = changes[0 if month == MONTHS[1] else 1]
                    check(adjustment is not None and adjustment.change_id == cid,
                          'Wrong Saga adjustment in month')
                    rates = json.loads(adjustment.rule_json)['rates']
                    exact = [money(adjustment.profit * Decimal(str(rates[k])) / 100) for k in RATES]
                    exact[1] += adjustment.profit - sum(exact)
                    check(tuple(shares[p] for p in PARTIES) == tuple(exact), 'Frozen Saga shares mismatch')
            # Supplemental payments and Saga refunds are represented by adjustments only.
            fund_orders = select(m.Order.id).where(m.Order.business_type == 'version_change_payment')
            saga_refunds = select(m.VersionChangeFund.refund_id).where(m.VersionChangeFund.kind == 'refund')
            check(not db.scalar(select(m.Settlement.id).where(m.Settlement.order_id.in_(fund_orders))),
                  'Upgrade funding was settled twice')
            check(not db.scalar(select(m.Settlement.id).where(m.Settlement.refund_id.in_(saga_refunds))),
                  'Downgrade refund was settled twice')
        start, end = period(month)
        params = dict(start=start, end=end)
        report = call('GET', '/settlement-reports', params=params)
        check(len(report['items']) == expected_rows and {row['id'] for row in report['items']} == ids,
              'Report duplicates or omits monthly identities')
        check(tuple(money(report['summary'][k]) for k in ('net_amount', 'cost_amount', 'profit_amount')) == totals,
              'Report totals mismatch')
        check(len(report['monthly']) == 1 and report['monthly'][0]['label'] == month, 'Wrong report month')
        response = client.get('/api/settlement-reports/export', headers=headers['admin'], params=params)
        check(response.status_code == 200, 'CSV export failed')
        exported = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
        check(len(exported) == expected_rows and {r['settlement_no'] for r in exported} ==
              {r['settlement_no'] for r in report['items']}, 'CSV membership mismatch')
        for row in exported:
            check(sum(Decimal(row[k]) for k in CSV_SHARES) == Decimal(row['profit_amount']), 'CSV shares mismatch')
            frozen = frozen_rows[row['settlement_no']]
            check(tuple(Decimal(row[k]) for k in CSV_SHARES) == frozen['shares'] and
                  tuple(Decimal(row[k]) for k in ('net_amount', 'cost_amount', 'profit_amount')) == frozen['amounts'],
                  'CSV changed frozen per-row amounts')
            if month == MONTHS[3]:
                check(row['refund_id'] == frozen['refund_id'] and row['order_id'] == frozen['order_id'] and
                      row['source_kind'] == 'ordinary_refund', 'CSV lost ordinary refund identity')
        check(tuple(sum((Decimal(row[k]) for row in exported), Decimal(0))
                    for k in ('net_amount', 'cost_amount', 'profit_amount')) == totals, 'CSV totals mismatch')
        return ids

    def paid_month(month, count):
        batch = generate(month)
        ids = inspect(month, batch, count)
        check(generate(month)['id'] == batch['id'], 'Monthly replay changed identity')
        with batch_clock(m, month):
            for sid in ids:
                call('POST', '/settlements/' + sid + '/lock', {})
            call('POST', '/settlement-batches/' + batch['id'] + '/confirm', {})
            call('POST', '/settlement-batches/' + batch['id'] + '/pay', {})
        lifecycle(month, batch['id'], confirmed=True, paid=True)
        batches[month] = batch
        snapshots[month] = snapshot(batch['id'])
        unchanged()

    def change(target, kind, at, month):
        with patch.object(m, 'now', return_value=at):
            quote = call('POST', '/orders/comb-order/version-change-quote',
                         dict(target_version_id=target), actor='buyer')
            check(quote['action'] == kind, 'Unexpected quote action')
            body = dict(quote_token=quote['quote_token'], idempotency_key='comb-' + kind)
            row = call('POST', '/orders/comb-order/version-changes', body, actor='buyer')
            cid = row['id']
            check(call('POST', '/orders/comb-order/version-changes', body, actor='buyer')['id'] == cid,
                  'Saga request replay duplicated change')
            if kind == 'upgrade':
                call('POST', '/version-changes/' + cid + '/start-payment', dict(reason='Simulated payment'), actor='buyer')
                call('POST', '/version-changes/' + cid + '/confirm-payment', dict(reason='Simulated payment'), actor='buyer')
            for _ in range(8):
                check(saga_worker.process_once(m.SessionLocal, worker_id='combination'), 'Saga worker stalled')
                with m.SessionLocal() as db:
                    current = db.get(m.VersionChangeRequest, cid)
                    check(not current.last_error and current.status not in ('retrying', 'compensating', 'exception'),
                          'Saga orchestration failed: ' + str(current.last_error))
                    if current.status == 'completed':
                        break
            else:
                check(False, 'Saga completion bound exceeded')
            with m.SessionLocal() as db:
                current = db.get(m.VersionChangeRequest, cid)
                adjustments = list(db.scalars(select(m.VersionChangeAdjustment).where(m.VersionChangeAdjustment.change_id == cid)))
                check(current.resource_applied and len(adjustments) == 2 and
                      {a.side for a in adjustments} == {'old', 'target'} and
                      all(a.status == 'posted' and a.posted_at == at and a.resource_change_id == cid for a in adjustments),
                      'Saga did not post both verified adjustments')
                for model in (m.subscription_terms['Revision'], m.refund_segment_store['Receipt'], m.refund_segment_store['Window']):
                    check(db.scalar(select(func.count()).select_from(model).where(model.change_id == cid)) == 1,
                          'Saga must create exactly one revision, receipt and economic window')
                check(db.scalar(select(m.AuditLog.id).where(m.AuditLog.target_id == cid,
                    m.AuditLog.action == 'apply_version_change_resource')) is not None, 'Missing production Saga audit')
                net, cost, profit = (sum((getattr(a, field) for a in adjustments), Decimal(0))
                                    for field in ('revenue', 'cost', 'profit'))
                signed = money(quote['payment_amount']) if kind == 'upgrade' else -money(quote['refund_amount'])
                check(net == signed, 'Saga cash delta and adjustment delta differ')
                expected[month] = net, cost, profit
            changes.append(cid)
        paid_month(month, 2)

    # These are the only substituted business boundary functions.
    with patch.object(m, 'saas_call', side_effect=supplier):
        expected[MONTHS[0]] = Decimal('600'), Decimal('120'), Decimal('480')
        paid_month(MONTHS[0], 1)
        change('comb-v2', 'upgrade', UPGRADE, MONTHS[1])
        change('comb-v1', 'downgrade', DOWNGRADE, MONTHS[2])
        with patch.object(m, 'now', return_value=REFUND_AT):
            for index in range(2):
                row = call('POST', '/orders/comb-order/refund-requests', dict(amount='40.00',
                    reason='Cross-month ordinary partial refund', idempotency_key=f'comb-request-{index}'),
                    actor='buyer', params=dict(enterprise_id='owned-buyer', business_view='buyer'))
                rid = row['id']
                approved = call('POST', '/refunds/' + rid + '/review', dict(decision='approve',
                    reason='Approve frozen economics', expected_version=row['version'], idempotency_key=f'comb-review-{index}'))
                body = dict(expected_version=approved['version'], reason='Simulated funds', idempotency_key=f'comb-execute-{index}')
                call('POST', '/refunds/' + rid + '/execute', body)
                for _ in range(16):
                    with m.SessionLocal() as db:
                        execution = db.scalar(select(m.refund_execution['Execution']).where(
                            m.refund_execution['Execution'].refund_id == rid))
                        check(execution.status != 'failed', 'Ordinary execution failed: ' + str(execution.error_code))
                        if execution.status == 'completed':
                            break
                    check(refund_worker.process_once(ns=vars(m)), 'Ordinary worker stalled')
                else:
                    check(False, 'Ordinary refund completion bound exceeded')
                with m.SessionLocal() as db:
                    refund = db.get(m.Refund, rid)
                    completion = db.get(m.refund_execution['Completion'], rid)
                    check(refund.status == 'completed' and refund.completed_at == REFUND_AT and completion is not None,
                          'Missing ordinary completion')
                    plan = json.loads(completion.economic_plan_json)
                    segments = plan.get('segments', [plan])
                    check(all(p['calculation_version'] == 'economic-segment-refund-v1' for p in segments),
                          'Refund did not use Saga economic window')
                    check(sum(Decimal(p['cost_reversal_delta']) for p in segments) == Decimal('8'),
                          'Refund cost not frozen to final economic segment')
                    legs = list(db.scalars(select(m.refund_execution['PaymentLeg']).where(
                        m.refund_execution['PaymentLeg'].refund_id == rid)))
                    check(legs and sum(leg.amount for leg in legs) == Decimal('40') and all(
                        leg.status == 'completed' and leg.receipt == 'simulated:' + rid + ':' + leg.payment_id for leg in legs),
                        'Simulated original-channel funding legs mismatch')
                    check(db.scalar(select(func.count()).select_from(m.refund_execution_resources['Recovery']).where(
                        m.refund_execution_resources['Recovery'].refund_id == rid)) == 1, 'Recovery is not unique')
                refunds.append(rid)
                unchanged()

    with m.SessionLocal() as db:
        allocations = list(db.scalars(select(m.refund_funding_store['FundingAllocation']).where(
            m.refund_funding_store['FundingAllocation'].refund_id.in_(refunds))))
        upgrade_fund = db.scalar(select(m.VersionChangeFund).where(m.VersionChangeFund.change_id == changes[0],
            m.VersionChangeFund.kind == 'payment'))
        check({a.payment_id for a in allocations if a.refund_id == refunds[0]} == {upgrade_fund.payment_id},
              'First refund must use latest upgrade payment')
        check({a.payment_id for a in allocations if a.refund_id == refunds[1]} == {upgrade_fund.payment_id, 'comb-payment'},
              'Second refund must span latest upgrade and original payment')
        check(sum(a.amount for a in allocations if a.payment_id == upgrade_fund.payment_id) == upgrade_fund.amount,
              'Latest upgrade payment was not exhausted exactly')
        for payment in db.scalars(select(m.Payment)):
            occupied = m.refund_funding_store['payment_reserved_total'](db, payment.id)
            check(money(occupied) <= payment.amount, 'Combined downgrade/ordinary refunds exceed source cash')
        sub = db.get(m.SaaSSubscription, 'comb-sub')
        check(sub.expires_at == datetime.fromisoformat(supplier.expiry) < END, 'SaaS tail recovery missing')
    check(len(supplier.changes) == 2 and len(supplier.refunds) == 2, 'Unexpected external operation count')

    # Catalog changes must not rewrite any frozen financial input.
    with m.SessionLocal.begin() as db:
        for version in db.scalars(select(m.SaaSProductVersion)):
            version.monthly_price, version.cost = 9999, 7777
        product = db.get(m.Product, 'comb-product')
        product.settlement_rule_json = json.dumps(dict(platform_rate=100, provider_rate=0,
            service_rate=0, expert_rate=0, channel_rate=0))
    expected[MONTHS[3]] = Decimal('-80'), Decimal('-16'), Decimal('-64')
    first = generate(MONTHS[3])
    negative_ids = inspect(MONTHS[3], first, 2)
    check(generate(MONTHS[3])['id'] == first['id'], 'Refund batch replay changed identity')
    rebuilt = generate(MONTHS[3], '-rebuild', rebuild=True)
    check(rebuilt['id'] != first['id'] and inspect(MONTHS[3], rebuilt, 2) == negative_ids,
          'Refund rebuild duplicated settlement identities')
    with m.SessionLocal() as db:
        old = list(db.scalars(select(m.SettlementLine).where(m.SettlementLine.batch_id == first['id'])))
        check(len(old) == 10 and all(line.status == 'superseded' for line in old), 'Old lines remain active')
        check(db.scalar(select(func.count()).select_from(m.refund_settlement['Model'])) == 2,
              'Ordinary refund settlement links duplicated')
        check(db.scalar(select(func.count()).select_from(m.VersionChangeSettlementLink)) == 4,
              'Saga settlement links duplicated')
    with batch_clock(m, MONTHS[3]):
        for sid in negative_ids:
            call('POST', '/settlements/' + sid + '/lock', {})
        call('POST', '/settlement-batches/' + rebuilt['id'] + '/confirm', {})
    lifecycle(MONTHS[3], first['id'])
    lifecycle(MONTHS[3], rebuilt['id'], confirmed=True)
    start, end = period(MONTHS[3])
    before = snapshot(rebuilt['id'])
    with batch_clock(m, MONTHS[3]):
        call('POST', '/settlement-batches', dict(cycle='monthly', period_start=start, period_end=end,
            order_ids=['comb-order'], idempotency_key='comb-confirmed-rebuild', rebuild=True), expected_status=409)
    check(snapshot(rebuilt['id']) == before, 'Confirmed refund batch changed')
    unchanged()
    return dict(status='passed', saga_orchestration='production quote/request/payment/worker',
        seeded_version_changes=False, completed_sagas=2, saga_adjustments=4, ordinary_refunds=2,
        ordinary_payment_legs=len(allocations), economic_windows=2, resource_recoveries=2,
        latest_payment_first=True, cross_source_refund=True, prior_paid_batches_unchanged=True,
        five_party_reports_and_csv=True, replay_and_rebuild=True, confirmed_rebuild_rejected=True,
        clock_synthetic=True, batch_lifecycle_after_transactions=True,
        batch_lifecycle_clock={month: month_end(month).isoformat() for month in MONTHS},
        months={month: dict(zip(('net', 'cost', 'profit'), map(str, expected[month]))) for month in MONTHS},
        external_adapters=dict(supplier='In-memory CHANGE/STATUS/RENEW protocol simulator',
                               funds='Production simulated payment/refund records; no bank movement'),
        gaps=['No browser, deployed SaaS HTTP, native gateway, real bank, or SIGKILL evidence'])


def local_url(raw):
    from sqlalchemy.engine import make_url
    url = make_url(raw)
    check(url.get_backend_name() == 'postgresql', 'PostgreSQL required')
    check(url.host in ('127.0.0.1', 'localhost', '::1') or
          (url.host is not None and url.host.startswith('/')), 'Local PostgreSQL only; no remote operations')
    check(not url.query, 'Database URL options are forbidden; runner owns search_path')
    return url


def run(raw):
    """Create before import, no startup, count-only public access, always dispose."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.schema import CreateSchema, DropSchema
    check('app.main' not in sys.modules, 'Run in a fresh process before importing app.main')
    url = local_url(raw)
    schema = 'qa_r3_be_' + secrets.token_hex(8)
    admin = create_engine(url, pool_pre_ping=True, hide_parameters=True, connect_args={'connect_timeout': 5})
    module, created, result = None, False, None

    def protected_count():
        with admin.connect() as conn:
            exists = conn.scalar(text("SELECT to_regclass('public.orders') IS NOT NULL"))
            if not exists:
                return None
            return conn.scalar(text('SELECT count(*) FROM public.orders WHERE order_no=:number'), dict(number=PROTECTED))

    before = protected_count()
    try:
        with admin.begin() as conn:
            conn.execute(CreateSchema(schema))
        created = True
        isolated = url.update_query_dict({'options': '-csearch_path=' + schema +
            ' -cstatement_timeout=5000 -clock_timeout=2000'})
        env = dict(DATABASE_URL=isolated.render_as_string(hide_password=False),
            MESSAGE_CENTER_DISABLE_BACKGROUND='true', REDIS_URL='redis://127.0.0.1:1/0')
        with patch.dict(os.environ, env), ExitStack() as stack:
            backend = str(Path(__file__).resolve().parents[1] / 'backend')
            stack.enter_context(patch.object(sys, 'path', [backend, *sys.path]))
            module = importlib.import_module('app.main')

            def guard(conn, cursor, statement, parameters, context, executemany):
                check(not re.search(r'\bpublic\s*\.', statement, re.I), 'Application public access forbidden')

            event.listen(module.engine, 'before_cursor_execute', guard)
            with module.engine.connect() as conn:
                check(conn.scalar(text('SELECT current_schema()')) == schema and
                      conn.scalar(text('SHOW search_path')) == schema, 'Exclusive schema required')
            module.Base.metadata.create_all(module.engine)
            for name in ('request', 'get', 'post'):
                stack.enter_context(patch.object(module.httpx, name, side_effect=AssertionError('External HTTP forbidden')))
            stack.enter_context(patch.object(module.redis.Redis, 'from_url', side_effect=AssertionError('Redis forbidden')))
            stack.enter_context(patch.object(module, 'Minio', side_effect=AssertionError('Object storage forbidden')))
            for name in ('SMTP', 'SMTP_SSL'):
                stack.enter_context(patch.object(module.smtplib, name, side_effect=AssertionError('Mail forbidden')))
            headers = seed(module)
            # No lifespan: no startup seeding or background workers.
            client = TestClient(module.app, raise_server_exceptions=False)
            try:
                result = exercise(module, client, headers)
            finally:
                client.close()
    finally:
        if module is not None:
            module.engine.dispose()
        try:
            if created:
                with admin.begin() as conn:
                    conn.execute(DropSchema(schema, cascade=True))
                with admin.connect() as conn:
                    check(conn.scalar(text('SELECT count(*) FROM information_schema.schemata WHERE schema_name=:schema'),
                        dict(schema=schema)) == 0, 'Owned schema disposal failed')
            check(protected_count() == before, 'Protected public order count changed')
        finally:
            admin.dispose()
    result.update(cleanup='owned schema removed', protected_order_content_read=False,
        protected_order_count_before=before, protected_order_count_after=before,
        scope='Local isolated PostgreSQL production Saga/refund/monthly combination',
        source_sha256={path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((Path(__file__).resolve().parents[1] / 'backend' / 'app').glob('*.py'))})
    return result


def remote_run(encoded, manifest):
    """Reuse original verified source loader and restricted-schema lifecycle."""
    check(all(name in manifest for name in SOURCE_FILES), 'Combination source inventory incomplete')

    def cases(m, client, headers):
        try:
            return {CASE_NAMES[0]: exercise(m, client, headers)}
        except Exception as exc:
            return {CASE_NAMES[0]: harness.cross.safe_error(exc)}

    with patch.object(harness.cross, 'seed', seed), patch.object(harness.cross, 'exercise', cases), \
            patch.object(harness.cross, 'CASE_NAMES', CASE_NAMES), \
            patch.object(harness.cross, 'SOURCE_FILES', SOURCE_FILES):
        result = harness.cross.remote_run(encoded, manifest)
    result.update(scope='Isolated PostgreSQL real financial cross-month Saga combination',
        incremental_scope='QA006 actual upgrade/downgrade orchestration then ordinary refunds and monthly reports',
        source_sha256=dict(manifest), seeded_version_changes=False,
        external_adapters=dict(supplier='Explicit in-memory CHANGE/STATUS/RENEW simulator',
                               funds='Production simulated payments/refunds; no bank movement'),
        gaps=['No actual supplier HTTP, native gateway, browser, deployment, real bank, or SIGKILL evidence'])
    return result


def pod_run(pod):
    """Bounded opt-in transport; never invoked by local tests."""
    check(bool(re.fullmatch(r'market-api-[a-z0-9-]+', pod)), 'Invalid API pod')
    check(bool(os.getenv('SSHPASS')), 'Private SSHPASS environment required')
    encoded, manifest = harness.transport.pack_workingtree(Path(__file__).resolve().parents[1])
    check(all(name in manifest for name in SOURCE_FILES), 'Combination source inventory incomplete')
    payload = 'import sys,types\n'
    modules = ('r3_backend_acceptance_pg', 'r3_backend_acceptance_concurrency',
        'r3_after_sales_group_pg_acceptance', 'r3_refund_workflow_pg_acceptance',
        'r3_refund_cross_version_pg_acceptance', 'r3_refund_execution_pg_acceptance',
        'r3_refund_settlement_pg_acceptance')
    for name in modules:
        source = Path(__file__).with_name(name + '.py').read_text()
        payload += ('m=types.ModuleType(' + repr(name) + ');sys.modules[m.__name__]=m;exec(compile('
                    + repr(source) + ',m.__name__,"exec"),m.__dict__)\n')
    payload += "__name__='monthly_combination_remote'\n" + Path(__file__).read_text()
    payload += '\nprint(json.dumps(remote_run(' + repr(encoded) + ',' + repr(manifest) + ')))\n'
    command = ('kubectl --request-timeout=5s -n market exec -i ' + pod
               + ' -- env MESSAGE_CENTER_DISABLE_BACKGROUND=true PYTHONDONTWRITEBYTECODE=1 python -B -')
    process = subprocess.run(['sshpass', '-e', 'ssh', '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1',
        '-o', 'LogLevel=ERROR', 'root@192.168.10.10', command], input=payload,
        capture_output=True, text=True, timeout=180)
    check(process.returncode == 0, 'Bounded transport failed; private diagnostics suppressed')
    return json.loads(process.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url-env', default='R3_MONTHLY_DATABASE_URL')
    parser.add_argument('--pod', help='Reviewed opt-in existing private transport')
    parser.add_argument('--output', type=Path, help='New sanitized JSON evidence file; overwrite forbidden')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    with ExitStack() as stack:
        output = None
        try:
            # Reserve evidence before touching the database or private transport.
            if args.output is not None:
                output = stack.enter_context(args.output.open('x', encoding='utf-8'))
            result = pod_run(args.pod) if args.pod else run(os.environ[args.database_url_env])
        except Exception as exc:
            # Exceptions may contain SQL parameters or credentials. Emit type only.
            result = dict(status='failed', error_type=type(exc).__name__)
            if isinstance(exc, harness.base.AcceptanceFailure):
                result['check'] = str(exc)
        encoded = json.dumps(result, sort_keys=True)
        if output is not None:
            output.write(encoded + '\n')
            output.flush()
        print(encoded)
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
