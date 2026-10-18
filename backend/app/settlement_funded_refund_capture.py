"""Exact-source funded-refund integration; no buyer money I/O."""
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

    def bindings(db, refund, source=None):
        _, plan, _, _ = verified(db, refund)
        segments = plan.get('segments', [plan])
        result = []
        if not segments or len({s['segment_id'] for s in segments}) != len(segments):
            fail('funded_refund_unique_segments_required')
        if (source is None and len(segments) == 1 and segments[0]['calculation_version'] in
                ('ordinary-refund-plan-v1', 'ordinary-file-refund-plan-v1')):
            Settlement = ns['Settlement']
            candidates = db.scalars(select(Baseline).join(Settlement, Settlement.id == Baseline.settlement_id).where(
                Baseline.order_id == refund.order_id, Baseline.revenue > 0, Settlement.status != 'superseded')).all()
            Adjustment = ns.get('VersionChangeAdjustment')
            if (len(candidates) != 1 or Adjustment is not None and db.scalar(select(Adjustment.id).where(
                    Adjustment.source_order_id == refund.order_id, Adjustment.status == 'posted'))):
                fail('funded_refund_exact_single_ordinary_baseline_required')
            source = candidates[0]
        for segment in segments:
            if segment['source_order_id'] != refund.order_id:
                fail('funded_refund_segment_order_mismatch')
            if segment['calculation_version'] == 'economic-segment-refund-v1':
                hook = ns.get('version_change_settlement', {}).get('refund_segment_basis')
                if not callable(hook):
                    fail('funded_refund_exact_segment_contract_required')
                ref = hook(db, refund, segment)
                if ref.get('status') != 'ready':
                    fail(dict(segment_id=segment['segment_id'], reason=ref.get('reason', 'segment_not_ready')))
                result.append(dict(ref, segment_id=segment['segment_id']))
            elif (len(segments) == 1 and source is not None and segment['calculation_version'] in
                  ('ordinary-refund-plan-v1', 'ordinary-file-refund-plan-v1')):
                result.append(dict(segment_id=segment['segment_id'], reference_settlement_id=source.settlement_id,
                                   baseline_hash=source.snapshot_hash))
            else:
                fail('funded_refund_exact_source_binding_required')
        if len({r['reference_settlement_id'] for r in result}) != len(result):
            fail('funded_refund_duplicate_source_baseline')
        return result

    def basis(db, refund, source=None, *, apply_exception=True):
        try:
            mapping = bindings(db, refund, source)
            result = ns['settlement_funded_refund']['basis'](db, refund, mapping, apply_exception=apply_exception)
            if result['status'] != 'ready':
                return result
            reference = mapping[0] if len(mapping) == 1 else {}
            metadata = {k: reference[k] for k in ('segment_id', 'window_hash', 'segment_context_hash',
                         'cumulative_refund_amount', 'original_amount') if k in reference}
            return dict(result, **metadata, reference_settlement_id=reference.get('reference_settlement_id', ''),
                        baseline_hash=reference.get('baseline_hash', ''))
        except HTTPException as exc:
            return dict(status='waiting', reason=exc.detail)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            return dict(status='waiting', reason='funded_refund_adapter_evidence_invalid')

    def segment_basis(db, refund, *, apply_exception=True):
        """None means ordinary segments; waiting is never an ordinary fallback."""
        try:
            Proof = core['FundingProof']
            if db.scalar(select(Proof.settlement_id).join(Baseline, Baseline.settlement_id == Proof.settlement_id)
                         .where(Baseline.order_id == refund.order_id)) is None:
                return None
            mapping = bindings(db, refund)
            funded = False
            for ref in mapping:
                source = db.get(Baseline, ref['reference_settlement_id'])
                if not source or source.snapshot_hash != ref['baseline_hash']:
                    fail('funded_refund_exact_source_binding_required')
                seen = set()
                while source is not None:
                    if source.settlement_id in seen:
                        fail('funded_refund_source_ancestry_cycle')
                    seen.add(source.settlement_id)
                    if db.get(core['FundingProof'], source.settlement_id):
                        funded = True
                    context = json.loads(source.snapshot_json).get('segment_context') or {}
                    if not context:
                        break
                    projection = context.get('version_funding_projection') or {}
                    if projection.get('policy') in ('return_unused_to_original_payer_not_target',
                                                    'restore_unused_platform_absorption_not_target'):
                        break
                    parent = db.get(Baseline, context.get('source_settlement_id'))
                    if (parent is None or parent.order_id != refund.order_id
                            or parent.snapshot_hash != context.get('source_baseline_hash')):
                        fail('funded_refund_source_ancestry_invalid')
                    source = parent
            return basis(db, refund, apply_exception=apply_exception) if funded else None
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
        source = db.get(Baseline, result['reference_settlement_id']) if result.get('reference_settlement_id') else None
        frozen = ns['settlement_funded_refund']['freeze'](db, refund, bindings(db, refund, source), actor)
        for field in ('gross_amount', 'cost_amount', 'profit_amount', 'absorption_reversal', 'buyer_cash_amount'):
            if money(frozen[field]) != money(result[field]):
                fail('funded_refund_frozen_basis_changed')
        if frozen['references'] != result['references'] or frozen['shares'] != result['shares']:
            fail('funded_refund_frozen_basis_changed')
        if money(frozen.get('external_funding_return', 0)) != money(result.get('external_funding_return', 0)):
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
        if (not refs or len({r['segment_id'] for r in refs}) != len(refs)
                or len({r['reference_settlement_id'] for r in refs}) != len(refs)):
            fail('funded_refund_unique_exact_sources_required')
        reference = refs[0]['reference_settlement_id'] if len(refs) == 1 else ''
        if settlement.reference_settlement_id != reference:
            fail('funded_refund_exact_sources_mismatch')
        ids = sorted(r['reference_settlement_id'] for r in refs)
        sources = {b.settlement_id: b for b in db.scalars(select(Baseline).where(Baseline.settlement_id.in_(ids))
            .order_by(Baseline.settlement_id).with_for_update().execution_options(populate_existing=True))}
        if set(sources) != set(ids):
            fail('funded_refund_exact_sources_missing')
        source = sources[reference] if reference else None
        result = basis(db, refund, source)
        if result['status'] != 'ready':
            return result
        frozen = freeze(db, refund, result, actor)
        if frozen['plan_hash'] != plan.payload_hash:
            fail('funded_refund_frozen_plan_changed')
        if (any(money(getattr(settlement, k)) != money(payload[k]) for k in ('gross_amount', 'cost_amount', 'profit_amount'))
                or any(money(getattr(settlement, k)) != money(payload['shares'][k]) for k in SHARES)):
            fail('funded_refund_settlement_basis_mismatch')
        cost = ns.get('settlement_funded_cost_exception', {})
        if payload.get('funded_cost_exception_application_hash') and not cost:
            fail('funded_cost_capture_contract_required')
        application = cost['validate_capture'](db, refund.id, payload) if cost else None
        old = db.get(Baseline, settlement.id)
        if old:
            snapshot = json.loads(old.snapshot_json)
            if snapshot.get('funded_refund_plan_hash') != plan.payload_hash:
                fail('funded_refund_existing_baseline_mismatch')
            if len(refs) > 1:
                rows = db.scalars(select(core['RefundSource']).where(core['RefundSource'].settlement_id == settlement.id)).all()
                if (len(rows) != len(refs) or {(r.segment_id, r.source_settlement_id, r.source_hash) for r in rows} !=
                        {(r['segment_id'], r['reference_settlement_id'], r['baseline_hash']) for r in refs}):
                    fail('funded_refund_existing_source_mapping_mismatch')
                for row in rows:
                    ref = next(r for r in refs if r['segment_id'] == row.segment_id)
                    if row.evidence_json != canonical(ref) or row.evidence_hash != hashlib.sha256(row.evidence_json.encode()).hexdigest():
                        fail('funded_refund_existing_source_mapping_mismatch')
            ns['settlement_funded_refund']['post'](db, refund, bindings(db, refund, source), settlement.id, actor)
            if application:
                posting = db.get(cost['Posting'], refund.id)
                if not posting or posting.settlement_id != settlement.id or posting.plan_hash != application.plan_hash:
                    fail('funded_cost_existing_posting_mismatch')
            return old
        key = lambda e: tuple(e[k] for k in ('kind', 'share_kind', 'participant_type', 'participant_id', 'participant_name'))
        slices = []
        for ref in refs:
            original_source = sources[ref['reference_settlement_id']]
            if (original_source.snapshot_hash != ref['baseline_hash'] or original_source.order_id != refund.order_id
                    or original_source.currency != payload['currency']):
                fail('funded_refund_source_scope_mismatch')
            records = core['source_reversals'](db, original_source.settlement_id)
            normal = ref.get('default_basis', ref)
            core['check_source_budget'](original_source, records,
                dict(ref, profit_amount=normal['profit_amount'], shares=normal['shares']))
            entries = [dict(e, blocked_reason='' if e.get('blocked_reason') == 'unfunded_settlement_adjustment'
                            else e.get('blocked_reason', '')) for e in ref['entries'] if money(e['amount'])]
            original = json.loads(original_source.snapshot_json)['entries']
            if len({key(e) for e in original}) != len(original):
                fail('funded_refund_duplicate_original_entries')
            amounts = {key(e): money(e['amount']) for e in original}
            previous = [e for r in records for e in r['entries'] if e['kind'] not in ('loss', 'funding_return')]
            for entry in entries:
                if entry['kind'] in ('loss', 'funding_return'):
                    continue
                if key(entry) not in amounts or money(entry['amount'], True) + sum(
                        (money(p['amount']) for p in previous if key(p) == key(entry)), Decimal(0)) > amounts[key(entry)]:
                    fail('funded_refund_subject_budget_exceeded')
            returns = []
            for entitlement in ref.get('funding_returns', []):
                bearer, funding = entitlement.get('bearer', {}), ref['funding']
                if (entitlement.get('currency') != payload['currency'] or entitlement.get('status') != 'unpaid'
                        or entitlement.get('source_settlement_id') != ref['reference_settlement_id']
                        or entitlement.get('execution_policy') != 'offset_or_independent_review_payout'
                        or funding.get('source') != 'external_receipt' or bearer != funding.get('bearer')
                        or not all(bearer.get(k) for k in ('participant_type', 'participant_id', 'participant_name'))
                        or any(entitlement.get(k) != funding.get(v) for k, v in (
                            ('funding_request_id', 'request_id'), ('funding_proof_hash', 'proof_hash'),
                            ('manual_evidence_id', 'manual_evidence_id'), ('manual_evidence_hash', 'manual_evidence_hash')))):
                    fail('funded_refund_payer_return_evidence_invalid')
                amount = money(entitlement['amount'])
                if amount < 0:
                    fail('funded_refund_payer_return_evidence_invalid')
                if amount:
                    returns.append(dict(kind='funding_return', share_kind='', amount=str(amount), blocked_reason='',
                                        **{k: bearer[k] for k in ('participant_type', 'participant_id', 'participant_name')}))
            if sum((money(e['amount']) for e in returns), Decimal(0)) != money(ref.get('external_funding_return', 0)):
                fail('funded_refund_payer_return_conservation_failed')
            slices.append((ref, entries, returns))
        entries = [e for _, group, _ in slices for e in group]
        if sum((money(e['amount']) for e in entries), Decimal(0)) != -money(payload['gross_amount']):
            fail('funded_refund_entry_conservation_failed')
        return_entries = [e for _, _, group in slices for e in group]
        if (sum((money(e['amount']) for e in return_entries), Decimal(0)) != money(payload.get('external_funding_return', 0))
                or -money(payload['gross_amount']) != money(payload['buyer_cash_amount']) + money(payload['absorption_reversal'])
                    + money(payload.get('external_funding_return', 0))):
            fail('funded_refund_cash_and_internal_conservation_failed')
        order = db.get(ns['Order'], refund.order_id)
        snapshot = dict(settlement_no=settlement.settlement_no, order_no=order.order_no,
            provider_id=order.provider_enterprise_id, currency=payload['currency'],
            revenue=payload['gross_amount'], cost=payload['cost_amount'], profit=payload['profit_amount'],
            shares=payload['shares'], entries=entries + return_entries, references=refs, reference_settlement_id=reference,
            actual_receipts=None, receipt_records=[], bank_receipt_verified=False, blocked_reason='',
            funded_refund_plan_hash=plan.payload_hash, buyer_cash_amount=payload['buyer_cash_amount'],
            absorption_reversal=payload['absorption_reversal'])
        if return_entries:
            snapshot.update(external_funding_return=payload['external_funding_return'],
                            funding_returns=[r for ref in refs for r in ref.get('funding_returns', [])])
        if application:
            snapshot.update(funded_cost_exception_application_hash=application.plan_hash)
        raw = canonical(snapshot)
        with db.begin_nested():
            baseline = Baseline(settlement_id=settlement.id, order_id=order.id, provider_enterprise_id=order.provider_enterprise_id,
                reference_settlement_id=reference, currency=payload['currency'],
                revenue=money(payload['gross_amount']), cost=money(payload['cost_amount']), profit=money(payload['profit_amount']),
                snapshot_json=raw, snapshot_hash=hashlib.sha256(raw.encode()).hexdigest(), actor=str(actor))
            db.add(baseline)
            db.flush()
            for ref, group, returns in slices:
                if len(refs) > 1:
                    encoded = canonical(ref)
                    db.add(core['RefundSource'](settlement_id=settlement.id, segment_id=ref['segment_id'],
                        source_settlement_id=ref['reference_settlement_id'], source_hash=ref['baseline_hash'],
                        evidence_json=encoded, evidence_hash=hashlib.sha256(encoded.encode()).hexdigest()))
                for entry in group + returns:
                    position = Position(settlement_id=settlement.id, order_id=order.id, currency=payload['currency'],
                        direction=('payable' if entry['kind'] == 'funding_return' else
                                   'internal' if entry['share_kind'] == 'platform_fee' else 'receivable'),
                        **dict(entry, amount=money(entry['amount'], True)))
                    db.add(position)
                    db.flush()
                    db.add(core['RefundPositionSource'](position_id=position.id, settlement_id=settlement.id,
                        segment_id=ref['segment_id'], source_settlement_id=ref['reference_settlement_id']))
            db.flush()
            ns['settlement_funded_refund']['post'](db, refund, bindings(db, refund, source), settlement.id, actor)
            if cost:
                cost['post'](db, application, settlement.id)
            core['audit'](db, str(actor), 'finance_funded_refund_capture', settlement.id,
                plan_hash=plan.payload_hash, buyer_cash_amount=payload['buyer_cash_amount'],
                financial_gross_amount=payload['gross_amount'], absorption_reversal=payload['absorption_reversal'],
                external_paid='0.00', external_recovered='0.00')
            db.flush()
        return baseline

    def reconcile_returns(db, settlement_ids, actor='system'):
        """Only an exact-source payer debt can extinguish a return entitlement."""
        core['lock_finance'](db)
        Mapping = core['RefundPositionSource']
        rows = db.execute(select(Position, Mapping).join(Mapping, Mapping.position_id == Position.id).where(
            Position.kind == 'funding_return', Mapping.settlement_id.in_(settlement_ids) |
            Mapping.source_settlement_id.in_(settlement_ids))).all()
        returns = [p for p, _ in rows]
        mapped = {r.position_id: r for _, r in rows}
        source_ids = {mapped[p.id].source_settlement_id for p in returns}
        if not returns:
            return dict(offset_count=0, transaction_ids=[], amounts={})
        mapped.update({r.position_id: r for r in db.scalars(select(Mapping).where(Mapping.source_settlement_id.in_(source_ids)))})
        ids = {p.settlement_id for p in returns} | source_ids | {
            r.settlement_id for r in mapped.values() if r.source_settlement_id in source_ids}
        Settlement, Line, Batch = (ns[k] for k in ('Settlement', 'SettlementLine', 'SettlementBatch'))
        memberships = db.execute(select(Line.settlement_id, Line.batch_id).where(
            Line.settlement_id.in_(ids), Line.status != 'superseded')).all()
        batch_ids = {bid for _, bid in memberships}
        batches = {b.id: b.status for b in db.scalars(select(Batch).where(Batch.id.in_(batch_ids))
            .order_by(Batch.id).with_for_update().execution_options(populate_existing=True))}
        active = {s.id for s in db.scalars(select(Settlement).where(Settlement.id.in_(ids)).order_by(Settlement.id)
            .with_for_update().execution_options(populate_existing=True)) if s.status in ('locked', 'paid')}

        def eligible(sid):
            owners = {bid for source, bid in memberships if source == sid}
            return sid in active and len(owners) == 1 and all(batches[bid] in (
                'confirmed', 'payment_processing', 'partial_paid', 'paid') for bid in owners)

        positions = db.scalars(select(Position).where(Position.settlement_id.in_(ids)).order_by(Position.id)
            .with_for_update().execution_options(populate_existing=True)).all()
        totals, txids = {}, []
        with db.begin_nested():
            for payable in positions:
                if (payable.kind != 'funding_return' or payable.id not in mapped or not eligible(payable.settlement_id)
                        or payable.direction != 'payable' or core['cash_block'](db, payable) or not payable.participant_id):
                    continue
                exact = mapped[payable.id].source_settlement_id
                for debt in positions:
                    source = mapped[debt.id].source_settlement_id if debt.id in mapped else debt.settlement_id
                    if (source != exact or debt.direction != 'receivable' or not eligible(debt.settlement_id)
                            or core['cash_block'](db, debt) or any(getattr(debt, k) != getattr(payable, k)
                                for k in ('participant_type', 'participant_id', 'currency'))):
                        continue
                    amount = min(core['balance'](db, debt), core['balance'](db, payable))
                    if amount <= 0:
                        continue
                    value = dict(allocations=[dict(position_id=debt.id, amount=str(amount))],
                        counter_position_id=payable.id, policy='exact_funding_payer_return_v1',
                        original_settlement_id=exact, segment_id=mapped[payable.id].segment_id)
                    tx = core['Transaction'](kind='offset', mode='manual', reference='funding-return:' + payable.id,
                        note='Exact-source payer entitlement offset; no cash movement', creator_id=str(actor),
                        payload_json=canonical(value))
                    db.add(tx)
                    db.flush()
                    db.add(core['Confirmation'](transaction_id=tx.id, reviewer_id='baseline_policy', note='Exact-source payer return'))
                    db.flush()
                    db.add_all([core['Allocation'](transaction_id=tx.id, position_id=p.id, amount=amount)
                                for p in (debt, payable)])
                    core['audit'](db, str(actor), 'finance_funding_return_offset', tx.id,
                        source_settlement_id=exact, amount=str(amount), currency=payable.currency, cash_moved=False)
                    db.flush()
                    txids.append(tx.id)
                    totals[payable.currency] = totals.get(payable.currency, Decimal(0)) + amount
        return dict(offset_count=len(txids), transaction_ids=txids, amounts={k: str(v) for k, v in totals.items()})

    hooks = dict(basis=basis, segment_basis=segment_basis, bindings=bindings, reconcile_returns=reconcile_returns,
                 guard_order=guard_order, freeze=freeze, capture=capture)
    ns['settlement_funded_refund_capture'] = hooks
    ns['settlement_funded_refund'].setdefault('raw_funded_basis',
        lambda db, refund: basis(db, refund, apply_exception=False))
    return hooks
