"""Lifecycle guards around the optional finance ledger; no commits or cash writes."""
from fastapi import HTTPException
from sqlalchemy import select


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
    used = position is not None and allocation is not None and db.scalar(select(allocation.id).join(
        position, position.id == allocation.position_id).where(position.settlement_id.in_(ids)))
    if referenced or used:
        raise HTTPException(409, 'funded_or_refund_referenced_baseline_requires_explicit_adjustment')


def capture(ns, db, settlement, actor):
    hook = ns.get('settlement_finance', {}).get('capture')
    if not callable(hook):
        raise HTTPException(409, 'settlement_finance_unavailable')
    result = hook(db, settlement, actor)
    if isinstance(result, dict):
        raise HTTPException(409, dict(code='final_baseline_not_ready', **result))
    return result


def describe(ns, db, settlement_id):
    hook = ns.get('settlement_finance', {}).get('describe')
    return hook(db, settlement_id) if callable(hook) else None


def funds(ns, db, batch_id):
    hook = ns.get('settlement_finance', {}).get('batch_status')
    return hook(db, batch_id) if callable(hook) else dict(
        positive_payment_complete=False, funds_settled=False, reason='settlement_finance_unavailable')
