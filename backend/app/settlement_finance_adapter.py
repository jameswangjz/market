"""Lifecycle guards around the optional finance ledger; no commits or cash writes."""
import json
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import select

SHARES = ('platform_fee', 'provider_share', 'service_share', 'expert_fee', 'channel_fee')


def proportional_values(baseline, amount, *, basis_revenue=None, basis_values=None):
    """Allocate a bounded final baseline slice; cumulative callers subtract slices."""
    cent = Decimal('.01')
    total = Decimal(str(baseline.revenue if basis_revenue is None else basis_revenue))
    amount = Decimal(str(amount))
    shares = {k: Decimal(str(v)) for k, v in json.loads(baseline.snapshot_json)['shares'].items()}
    values = basis_values or dict(cost=Decimal(str(baseline.cost)), profit=Decimal(str(baseline.profit)), shares=shares)
    if (not amount.is_finite() or not total.is_finite() or total <= 0 or not 0 <= amount <= total
            or amount != amount.quantize(cent) or total != total.quantize(cent)
            or set(values['shares']) != set(SHARES)
            or values['cost'] < 0 or values['profit'] < 0 or values['cost'] + values['profit'] != total
            or any(v < 0 for v in values['shares'].values()) or sum(values['shares'].values()) != values['profit']):
        raise HTTPException(409, 'invalid_final_segment_slice')
    cost = (values['cost'] * amount / total).quantize(cent, rounding=ROUND_HALF_UP)
    profit = amount - cost
    raw = {k: v * profit / values['profit'] if values['profit'] else Decimal(0) for k, v in values['shares'].items()}
    parts = {k: v.quantize(cent, rounding=ROUND_DOWN) for k, v in raw.items()}
    remainder = int((profit - sum(parts.values())) / cent)
    for k in sorted(raw, key=lambda k: (-(raw[k] - parts[k]), SHARES.index(k)))[:remainder]:
        parts[k] += cent
    if sum(parts.values()) != profit:
        raise HTTPException(409, 'segment_profit_allocation_not_conserved')
    return dict(cost=cost, profit=profit, shares=parts)


def protect(ns, db, settlement_ids):
    ids = set(settlement_ids)
    if not ids:
        return
    baseline = ns.get('settlement_finance', {}).get('Baseline')
    settlement = ns['Settlement']
    if (baseline is not None and db.scalar(select(baseline.settlement_id).where(
            baseline.settlement_id.in_(ids)))) or db.scalar(select(settlement.id).where(
                settlement.reference_settlement_id.in_(ids))):
        raise HTTPException(409, 'frozen_baseline_or_refund_reference_requires_explicit_adjustment')


def protect_rebuild(ns, db, settlement_ids):
    ids = set(settlement_ids)
    if not ids:
        return
    finance = ns.get('settlement_finance', {})
    settlement = ns['Settlement']
    baseline, position, allocation = (finance.get(k) for k in ('Baseline', 'Position', 'Allocation'))
    referenced = db.scalar(select(settlement.id).where(settlement.reference_settlement_id.in_(ids)))
    if baseline is not None:
        referenced = referenced or db.scalar(select(baseline.settlement_id).where(
            baseline.reference_settlement_id.in_(ids)))
    exception = ns.get('settlement_parties', {}).get('CostException')
    if exception is not None:
        referenced = referenced or db.scalar(select(exception.id).where(exception.baseline_id.in_(ids)))
    binding = ns.get('version_change_settlement', {}).get('SegmentBaselineLink')
    if binding is not None:
        referenced = referenced or db.scalar(select(binding.window_id).where(binding.source_settlement_id.in_(ids)))
    funding_link = finance.get('SegmentFundingLink')
    if funding_link is not None:
        referenced = referenced or db.scalar(select(funding_link.source_negative_id).where(
            funding_link.source_negative_id.in_(ids) | funding_link.target_settlement_id.in_(ids)))
    used = position is not None and allocation is not None and db.scalar(select(allocation.id).join(
        position, position.id == allocation.position_id).where(position.settlement_id.in_(ids)))
    if referenced or used:
        raise HTTPException(409, 'funded_or_refund_referenced_baseline_requires_explicit_adjustment')


def capture(ns, db, settlement, actor):
    version_change = ns.get('version_change_settlement', {}).get('describe')
    if callable(version_change) and version_change(db, settlement.id) is not None:
        return capture_version_change(ns, db, settlement, actor)
    hook = ns.get('settlement_finance', {}).get('capture')
    if not callable(hook):
        raise HTTPException(409, 'settlement_finance_unavailable')
    result = hook(db, settlement, actor)
    if isinstance(result, dict):
        raise HTTPException(409, dict(code='final_baseline_not_ready', **result))
    return result


def capture_version_change(ns, db, settlement, actor='system'):
    """Call only the segment-aware core; absence must not block legacy business locking."""
    hooks = ns.get('version_change_settlement', {})
    context = hooks['financial_context'](db, settlement)
    if context['status'] != 'ready':
        return context
    if settlement.status not in ('locked', 'paid'):
        return dict(status='waiting', reason='locked_segment_settlement_required')
    values = context['values']
    if (any(Decimal(str(getattr(settlement, k))) != Decimal(values[k]) for k in
            ('gross_amount', 'cost_amount', 'profit_amount')) or
            any(Decimal(str(getattr(settlement, k))) != Decimal(v) for k, v in values['shares'].items())):
        return dict(status='waiting', reason='final_segment_projection_not_applied')
    core = ns.get('settlement_finance', {}).get('capture_segment')
    if not callable(core):
        return dict(status='waiting', reason='segment_finance_capture_contract_required', context_hash=context['context_hash'])
    try:
        with db.begin_nested():
            result = core(db, settlement, context, actor)
            if isinstance(result, dict):
                return result
            if context['side'] == 'target':
                entries = json.loads(result.snapshot_json)['entries']
                for role, field in (('service', 'service_share'), ('expert', 'expert_fee'), ('channel', 'channel_fee')):
                    expected = sorted((r.get('participant_type') or role, r['participant_id'], r['participant_name'], Decimal(str(r['amount'])))
                        for r in context['participant_allocations'].get(role, []) if Decimal(str(r['amount'])))
                    actual = sorted((e['participant_type'], e['participant_id'], e['participant_name'], Decimal(str(e['amount'])))
                        for e in entries if e['kind'] == 'profit' and e['share_kind'] == field)
                    if expected != actual:
                        raise HTTPException(409, 'target_participant_snapshot_not_captured')
                hooks['bind_baseline'](db, settlement, actor)
    except HTTPException as exc:
        if exc.detail == 'target_participant_snapshot_not_captured':
            return dict(status='waiting', reason=exc.detail)
        raise
    if context['side'] == 'old':
        target = hooks['paired_target'](db, context['adjustment_id'])
        if target is not None and target.status == 'locked':
            capture_version_change(ns, db, target, actor)
    return result


def describe(ns, db, settlement_id):
    hook = ns.get('settlement_finance', {}).get('describe')
    return hook(db, settlement_id) if callable(hook) else None


def funds(ns, db, batch_id):
    hook = ns.get('settlement_finance', {}).get('batch_status')
    return hook(db, batch_id) if callable(hook) else dict(
        positive_payment_complete=False, funds_settled=False, reason='settlement_finance_unavailable')
