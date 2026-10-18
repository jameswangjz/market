"""Bounded ordinary single-source funded-refund integration; no buyer money I/O."""
import hashlib
import json
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from .settlement_finance import SHARES, canonical, money


def install(ns, core):
    Baseline, Position = core['Baseline'], core['Position']

    def fail(reason):
        raise HTTPException(409, reason)

    def verified(db, refund):
        hook = ns.get('settlement_funded_refund_evidence', {}).get('completed_evidence')
        if not callable(hook):
            fail('funded_refund_completion_contract_required')
        return hook(db, refund)

    def bindings(db, refund, source):
        _, plan, _, _ = verified(db, refund)
        segments = plan.get('segments', [plan])
        if (len(segments) != 1 or segments[0]['source_order_id'] != refund.order_id
                or segments[0]['calculation_version'] not in ('ordinary-refund-plan-v1', 'ordinary-file-refund-plan-v1')):
            fail('funded_refund_single_ordinary_segment_adapter_required')
        return [dict(segment_id=segments[0]['segment_id'], reference_settlement_id=source.settlement_id,
                     baseline_hash=source.snapshot_hash)]

    def basis(db, refund, source):
        try:
            mapping = bindings(db, refund, source)
            result = ns['settlement_funded_refund']['basis'](db, refund, mapping)
            if result['status'] != 'ready':
                return result
            return dict(result, reference_settlement_id=source.settlement_id, baseline_hash=source.snapshot_hash)
        except HTTPException as exc:
            return dict(status='waiting', reason=exc.detail)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            return dict(status='waiting', reason='funded_refund_adapter_evidence_invalid')

    def guard_order(db, settlement):
        if not settlement.refund_id:
            return
        # Dirac's freeze contract is Order -> Refund -> ordered Baseline. Take
        # Order before the ordinary completion validator locks this Refund.
        proof = core['FundingProof']
        if db.scalar(select(proof.settlement_id).join(Baseline, Baseline.settlement_id == proof.settlement_id)
                     .where(Baseline.order_id == settlement.order_id)):
            Order = ns['Order']
            db.scalar(select(Order).where(Order.id == settlement.order_id).with_for_update()
                      .execution_options(populate_existing=True))

    def freeze(db, refund, result, actor):
        connection = db.connection()
        if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql('BEGIN')
        source = db.get(Baseline, result['reference_settlement_id'])
        frozen = ns['settlement_funded_refund']['freeze'](db, refund, bindings(db, refund, source), actor)
        for field in ('gross_amount', 'cost_amount', 'profit_amount', 'absorption_reversal', 'buyer_cash_amount'):
            if money(frozen[field]) != money(result[field]):
                fail('funded_refund_frozen_basis_changed')
        if frozen['references'] != result['references'] or frozen['shares'] != result['shares']:
            fail('funded_refund_frozen_basis_changed')
        return frozen

    def capture(db, settlement, actor='system'):
        Settlement = ns['Settlement']
        settlement = db.scalar(select(Settlement).where(Settlement.id == settlement.id).with_for_update()
                               .execution_options(populate_existing=True))
        if settlement is None or settlement.status not in ('locked', 'paid'):
            fail('locked_settlement_required')
        refund = db.get(ns['Refund'], settlement.refund_id)
        Plan = ns['settlement_funded_refund']['Plan']
        plan = db.get(Plan, refund.id)
        if plan is None or hashlib.sha256(plan.payload_json.encode()).hexdigest() != plan.payload_hash:
            fail('funded_refund_frozen_plan_required')
        payload = json.loads(plan.payload_json)
        refs = payload['references']
        if len(refs) != 1 or refs[0]['reference_settlement_id'] != settlement.reference_settlement_id:
            fail('funded_refund_single_exact_source_required')
        source = db.scalar(select(Baseline).where(Baseline.settlement_id == settlement.reference_settlement_id)
                           .with_for_update().execution_options(populate_existing=True))
        result = basis(db, refund, source)
        if result['status'] != 'ready':
            return result
        frozen = freeze(db, refund, result, actor)
        if frozen['plan_hash'] != plan.payload_hash:
            fail('funded_refund_frozen_plan_changed')
        if (any(money(getattr(settlement, k)) != money(payload[k]) for k in ('gross_amount', 'cost_amount', 'profit_amount'))
                or any(money(getattr(settlement, k)) != money(payload['shares'][k]) for k in SHARES)):
            fail('funded_refund_settlement_basis_mismatch')
        old = db.get(Baseline, settlement.id)
        if old:
            snapshot = json.loads(old.snapshot_json)
            if snapshot.get('funded_refund_plan_hash') != plan.payload_hash:
                fail('funded_refund_existing_baseline_mismatch')
            ns['settlement_funded_refund']['post'](db, refund, bindings(db, refund, source), settlement.id, actor)
            return old
        values = dict(payload, shares=payload['shares'])
        records = core['source_reversals'](db, source.settlement_id)
        core['check_source_budget'](source, records, values)
        entries = [dict(e, blocked_reason='' if e.get('blocked_reason') == 'unfunded_settlement_adjustment'
                        else e.get('blocked_reason', '')) for e in refs[0]['entries'] if money(e['amount'])]
        original = json.loads(source.snapshot_json)['entries']
        key = lambda e: tuple(e[k] for k in ('kind', 'share_kind', 'participant_type', 'participant_id', 'participant_name'))
        if len({key(e) for e in original}) != len(original):
            fail('funded_refund_duplicate_original_entries')
        amounts = {key(e): money(e['amount']) for e in original}
        previous = [e for r in records for e in r['entries']]
        for entry in entries:
            if key(entry) not in amounts or money(entry['amount'], True) + sum(
                    (money(p['amount']) for p in previous if key(p) == key(entry)), Decimal(0)) > amounts[key(entry)]:
                fail('funded_refund_subject_budget_exceeded')
        if sum((money(e['amount']) for e in entries), Decimal(0)) != -money(payload['gross_amount']):
            fail('funded_refund_entry_conservation_failed')
        order = db.get(ns['Order'], refund.order_id)
        snapshot = dict(settlement_no=settlement.settlement_no, order_no=order.order_no,
            provider_id=order.provider_enterprise_id, currency=source.currency,
            revenue=payload['gross_amount'], cost=payload['cost_amount'], profit=payload['profit_amount'],
            shares=payload['shares'], entries=entries, references=refs, reference_settlement_id=source.settlement_id,
            actual_receipts=None, receipt_records=[], bank_receipt_verified=False, blocked_reason='',
            funded_refund_plan_hash=plan.payload_hash, buyer_cash_amount=payload['buyer_cash_amount'],
            absorption_reversal=payload['absorption_reversal'])
        raw = canonical(snapshot)
        with db.begin_nested():
            baseline = Baseline(settlement_id=settlement.id, order_id=order.id, provider_enterprise_id=order.provider_enterprise_id,
                reference_settlement_id=source.settlement_id, currency=source.currency,
                revenue=money(payload['gross_amount']), cost=money(payload['cost_amount']), profit=money(payload['profit_amount']),
                snapshot_json=raw, snapshot_hash=hashlib.sha256(raw.encode()).hexdigest(), actor=str(actor))
            db.add(baseline)
            db.flush()
            for entry in entries:
                db.add(Position(settlement_id=settlement.id, order_id=order.id, currency=source.currency,
                    direction='internal' if entry['share_kind'] == 'platform_fee' else 'receivable',
                    **dict(entry, amount=money(entry['amount'], True))))
            db.flush()
            ns['settlement_funded_refund']['post'](db, refund, bindings(db, refund, source), settlement.id, actor)
            core['audit'](db, str(actor), 'finance_funded_refund_capture', settlement.id,
                plan_hash=plan.payload_hash, buyer_cash_amount=payload['buyer_cash_amount'],
                financial_gross_amount=payload['gross_amount'], absorption_reversal=payload['absorption_reversal'],
                external_paid='0.00', external_recovered='0.00')
            db.flush()
        return baseline

    hooks = dict(basis=basis, guard_order=guard_order, freeze=freeze, capture=capture)
    ns['settlement_funded_refund_capture'] = hooks
    return hooks
