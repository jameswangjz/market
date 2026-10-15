"""BE011 owned PostgreSQL SaaS quote/hold acceptance; no CLI or execution.

Caller transports reviewed sources and invokes remote_run. Provider CHANGE and
STATUS are explicit synthetic fixtures, never real HTTP or Redis evidence.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import importlib
import json
from unittest.mock import patch

import r3_refund_cross_version_pg_acceptance as cross

base = cross.base
SOURCE_FILES = tuple(dict.fromkeys((*cross.SOURCE_FILES, 'refund_schema.py',
    'subscription_calendar.py', 'order_economics.py')))
CASE_NAMES = ('saas_quote_request_review', 'saas_wrong_tenant', 'saas_missing_receipt')
ANCHOR = datetime(2027, 2, 1, tzinfo=timezone.utc)
SWITCH = ANCHOR + timedelta(days=7)
AS_OF = ANCHOR + timedelta(days=14)
END = datetime(2027, 3, 1, tzinfo=timezone.utc)
DAY_US = 86400 * 1000000


def graph(m, db, name, *, missing=False):
    """Real immutable recorder/materializer/latest; synthetic provider responses."""
    cross.owned(db)
    module = importlib.import_module(m.__package__ + '.refund_segment_store')
    canonical, digest = module.canonical, module.digest
    ident = lambda suffix: cross.identity('saas-' + name + suffix)
    pid, oid, tid, cid, change_id, sid, cfgid = map(ident,
        ('product', 'order', 'term', 'combo', 'change', 'subscription', 'config'))
    old, target, funding, payment = map(ident, ('v1', 'v2', 'funding', 'upgrade-payment'))
    terms, store = m.subscription_terms, m.refund_segment_store
    rule = dict(version='owned-frozen', rates={key: 0 for key in m.order_economics['rate_keys']})
    economic = dict(schema_version=1, product_id=pid, product_version_id=old, version_code='v1',
        billing_unit='month', subscription_months=1, unit_price='28.00', unit_cost='5.60',
        amount='28.00', total_cost='5.60', currency='CNY', settlement_rule=rule)
    delivery = dict(method='tenant_access', quota=dict(rate_limit_per_minute=0, daily_quota=0,
        monthly_quota=0, quota_amount=0), files=[], download_limit=0)
    db.add(m.Product(id=pid, enterprise_id='owned-provider', name='Owned SaaS planning',
        product_type='saas', delivery_method='tenant_access', currency='CNY', status='published'))
    db.flush()
    for vid, code in ((old, 'v1'), (target, 'v2')):
        db.add(m.SaaSProductVersion(id=vid, product_id=pid, version_code=code,
            name='Owned version', monthly_price=9999, cost=7777, status='active'))
    config = m.SaaSIntegrationConfig(id=cfgid, product_id=pid,
        base_url='https://owned.invalid', status='active')
    db.add(config)
    db.flush()
    combo = terms['Combination'](id=cid, enterprise_id='owned-buyer', product_id=pid,
        delivery_method='tenant_access', version_id=old, version_code='v1', anchor_at=ANCHOR,
        generation=1, total_months=1, status='active')
    db.add(combo)
    db.flush()
    for source_id, amount, kind in ((oid, 28, ''), (funding, 21, 'version_change_payment')):
        db.add(m.Order(id=source_id, order_no='OWNED-SAAS-' + source_id, product_id=pid,
            product_name='Owned SaaS', buyer_enterprise_id='owned-buyer', buyer_user_id='owned-buyer',
            buyer_name='Owned buyer', provider_enterprise_id='owned-provider', amount=amount,
            paid_amount=amount, refunded_amount=0, payment_status='paid', main_status='completed',
            delivery_status='accepted', business_type=kind, related_order_id=oid if kind else None,
            snapshot_version=1, product_version_id=old, product_version_code='v1', subscription_months=1,
            unit_price_snapshot=28, unit_cost_snapshot=Decimal('5.60'), total_cost_snapshot=Decimal('5.60'),
            delivery_method_snapshot='tenant_access', economic_snapshot_json=canonical(economic) if not kind else '{}',
            delivery_snapshot_json=canonical(delivery)))
        db.flush()
        db.add(m.Payment(id=payment if kind else ident('payment'), order_id=source_id,
            payment_no='OWNED-SAAS-' + source_id, amount=amount, status='paid',
            paid_at=SWITCH if kind else ANCHOR))
    db.add(terms['Term'](id=tid, order_id=oid, combination_id=cid, generation=1,
        version_id=old, version_code='v1', start_month=0, months=1,
        starts_at=ANCHOR, ends_at=END, status='active'))
    sub = m.SaaSSubscription(id=sid, enterprise_id='owned-buyer', product_id=pid,
        version_id=target, external_tenant_id=ident('tenant'), status='active')
    db.add(sub)
    revised = dict(economic, product_version_id=target, version_code='v2', unit_price='56.00',
        unit_cost='11.20', amount='56.00', total_cost='11.20')
    config_hash = digest({key: getattr(config, key) for key in ('id', 'base_url', 'operation_path',
        'token_url', 'client_id', 'scope', 'auth_mode', 'timeout_ms', 'status')})
    plan = dict(kind='saas', change_id=change_id, subscription_id=sid, tenant_id=sub.external_tenant_id,
        product_id=pid, enterprise_id='owned-buyer', config_id=cfgid, config_fingerprint=config_hash)
    change = m.version_change_saga['VersionChangeRequest'](id=change_id, source_order_id=oid,
        enterprise_id='owned-buyer', combination_id=cid, target_version_id=target, target_version_code='v2',
        old_version_id=old, old_version_code='v1', kind='upgrade', status='completed',
        idempotency_key=ident('key'), request_hash=digest(name), created_by='owned-admin',
        created_at=SWITCH - timedelta(seconds=1), completed_at=SWITCH, resource_applied=True,
        quote_json=canonical(dict(currency='CNY', payment_amount='21.00')), prepared_json='{}',
        target_snapshots_json=canonical({tid: dict(economic=revised, delivery=delivery)}),
        funding_order_id=funding, resource_plan_json=canonical(plan))
    db.add(change)
    db.flush()
    db.add(m.version_change_saga['VersionChangeFund'](change_id=change_id, kind='payment',
        payment_id=payment, amount=21, idempotency_key=ident('fund')))
    effective = dict(ret=0, change_id=change_id, direction='target', effective_at=SWITCH.isoformat())
    responses = dict(change=effective, status=dict(effective, subscription_id=sid,
        tenant_id=sub.external_tenant_id, product_id=pid, enterprise_id='owned-buyer', version='v2', status='active'))
    receipt = None if missing else store['record_receipt'](db, change, None, 'target', responses, subscription=sub)
    if receipt:
        base.check(store['record_receipt'](db, change, None, 'target', responses, subscription=sub).id == receipt.id,
                   'Receipt replay changed identity')
    evidence = dict(subscription_id=sid, tenant_id=sub.external_tenant_id, version_id=target)
    if receipt:
        evidence['receipt_ids'] = [receipt.id]
    proof = dict(request_change_id=change_id, resource_change_id=change_id,
        evidence_fingerprint=digest(dict(change_id=change_id, direction='target', kind='saas', evidence=evidence)))
    change.evidence_json = canonical(proof)
    for side, revenue, cost in (('old', '-21.00', '-4.20'), ('target', '42.00', '8.40')):
        db.add(m.version_change_ledger['Model'](change_id=change_id, request_change_id=change_id,
            source_order_id=oid, combination_id=cid, side=side, revenue=Decimal(revenue), cost=Decimal(cost),
            profit=Decimal(revenue)-Decimal(cost), currency='CNY', original_version_id=old, target_version_id=target,
            rule_json=canonical(rule), effective_at=SWITCH, status='posted', payload_fingerprint=digest(side),
            resource_change_id=change_id, resource_evidence_fingerprint=proof['evidence_fingerprint'], posted_at=SWITCH))
    db.flush()
    terms['apply_version_change'](db, combo, target, 'v2', {tid: dict(economic=revised, delivery=delivery)}, SWITCH, change_id)
    windows = store['materialize'](db, change, proof)
    base.check(len(windows) == (0 if missing else 1), 'Unexpected materialized windows')
    if windows:
        base.check(store['latest'](db, db.get(m.Order, oid)).id == windows[0].id, 'Real latest rejected SaaS graph')
        base.check(store['materialize'](db, change, proof)[0].id == windows[0].id, 'Window replay changed identity')
    return dict(order=oid, subscription=sid, upgrade=payment, original=ident('payment'))


def seed(m):
    headers = cross.fixture.seed(m)
    with patch.object(m, 'now', return_value=AS_OF):
        for name in CASE_NAMES:
            with m.SessionLocal.begin() as db:
                g = graph(m, db, name, missing=name == CASE_NAMES[2])
                if name == CASE_NAMES[1]:
                    db.get(m.SaaSSubscription, g['subscription']).external_tenant_id = 'wrong-tenant'
                if not hasattr(m, '_owned_saas_planning'):
                    m._owned_saas_planning = {}
                m._owned_saas_planning[name] = g
    return headers


def exercise(m, client, headers):
    from sqlalchemy import func, select
    results = {}
    Allocation = m.refund_funding_store['FundingAllocation']
    Case = m.refund_workflow['Case']

    def run(name):
        g = m._owned_saas_planning[name]
        path = '/api/orders/' + g['order']
        def call(suffix, body, actor='buyer', status=200):
            response = client.post(path + suffix, json=body, headers=headers[actor],
                params=dict(enterprise_id='owned-buyer', business_view='buyer'))
            base.check(response.status_code == status, 'Unexpected registered ASGI status')
            return response.json() if status < 400 else None
        quote = call('/refund-quote', dict(amount='28.00'))
        negative = name != CASE_NAMES[0]
        base.check(quote['status'] == ('unavailable' if negative else 'available'), 'SaaS quote trust mismatch')
        if not negative:
            item = quote['item']
            base.check(item['basis'] == 'time' and item['charge_policy'] is None and
                item['as_of'] == AS_OF.isoformat() and item['refundable_balance_before'] == '28.00' and
                item['recovery_delta'] == 14 * DAY_US and item['effective_ends_at'] == AS_OF.isoformat(),
                'Time quote scalar mismatch')
        row = call('/refund-requests', dict(amount='28.00', reason='Owned SaaS', idempotency_key=name))
        with m.SessionLocal() as db:
            cross.owned(db)
            held = list(db.execute(select(Allocation.payment_id, Allocation.amount).where(
                Allocation.refund_id == row['id']).order_by(Allocation.rowid)))
        if negative:
            base.check(not held, 'Untrusted SaaS graph acquired holds')
        else:
            base.check([tuple(r) for r in held] == [(g['upgrade'], Decimal('21.00')),
                (g['original'], Decimal('7.00'))], 'Real multi-source hold mismatch')
            base.check(call('/refund-quote', dict(amount='0.01'))['status'] == 'unavailable', 'Pending hold ignored')
        response = client.post('/api/refunds/' + row['id'] + '/review', headers=headers['admin'],
            json=dict(decision='approve', reason='Owned SaaS approval', expected_version=row['version'],
                      idempotency_key=name + '-review'))
        base.check(response.status_code == (409 if negative else 200), 'Approval did not fail closed')
        with m.SessionLocal() as db:
            cross.owned(db)
            base.check(db.scalar(select(func.count()).select_from(m.refund_execution['Completion'])) == 0,
                       'Quote-only acceptance created Completion')
            refund = db.get(m.Refund, row['id'])
            base.check(refund.status == ('requested' if negative else 'approved'), 'Refund state mismatch')
            if not negative:
                window = m.refund_segment_store['latest'](db, db.get(m.Order, g['order']))
                receipt = db.get(m.refund_segment_store['Receipt'], window.receipt_id)
                base.check((window.revenue, window.cost, window.starts_at, window.ends_at) ==
                    (Decimal('42.00'), Decimal('8.40'), SWITCH, END), 'Stored window scalars mismatch')
                base.check(receipt.kind == 'saas' and all(getattr(receipt, key) is None for key in
                    ('credential_id', 'source_run_id', 'boundary_id', 'target_fingerprint')),
                    'SaaS receipt contains API identifiers')
                case = db.get(Case, row['id'])
                plan = json.loads(case.plan_json)['economic_plan']
                base.check(hashlib.sha256(case.plan_json.encode()).hexdigest() == case.plan_hash,
                           'Frozen plan hash mismatch')
                base.check(plan['cost_reversal_delta'] == '5.60' and plan['charge_ledger'] is None and
                    plan['as_of'] == AS_OF.isoformat() and plan['recovery_delta'] == 14 * DAY_US,
                    'Frozen cost/time mismatch')
                base.check(list(db.execute(select(Allocation.payment_id, Allocation.amount).where(
                    Allocation.refund_id == row['id']).order_by(Allocation.rowid))) == held,
                    'Review changed funding holds')
                base.check(plan['funding_allocations'] == list(m.refund_funding_store['frozen'](db, refund)),
                           'Frozen funding differs from scalar rows')
        return dict(status='passed', fail_closed=negative, completion_rows=0)

    with patch.object(m, 'now', return_value=AS_OF), patch.object(m, 'apisix_policy_redis',
            side_effect=AssertionError('SaaS must not access Redis')), patch.object(m, 'saas_call',
            side_effect=AssertionError('Provider HTTP forbidden')):
        for name in CASE_NAMES:
            try:
                results[name] = run(name)
            except Exception as exc:
                results[name] = cross.safe_error(exc)
    return results


def remote_run(encoded, manifest):
    with patch.object(cross, 'seed', seed), patch.object(cross, 'exercise', exercise), \
            patch.object(cross, 'CASE_NAMES', CASE_NAMES), patch.object(cross, 'SOURCE_FILES', SOURCE_FILES):
        result = cross.remote_run(encoded, manifest)
    guards = (result.get('public_write_operations') == 0 and result.get('protected_order_content_read') is False
        and result.get('protected_order_count_before') == result.get('protected_order_count_after') == 1
        and base.owned_schema(result.get('owned_schema', '')) and result.get('cleanup') == 'owned schema removed')
    if not guards:
        result['status'] = 'failed'
    result.update(incremental_scope='BE011 SaaS cross-version quote and funding only',
        scope='Real models, immutable receipt/materialize/latest, PostgreSQL scalars and registered ASGI',
        external_adapters=dict(provider='Explicit synthetic CHANGE/STATUS fixture; no real HTTP', Redis='Forbidden'),
        gaps=['No provider HTTP, ingress, deployment, execution, bank or concurrency coverage'])
    return result
