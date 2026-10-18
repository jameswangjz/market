"""Confirmed funded-refund financial slices, with no buyer-cash or Position writes.

Integration is opt-in: the refund owner supplies completed_evidence(db, refund)
under ns['settlement_funded_refund_evidence']. It must be the original full
completion/resource/payment-leg validator, returning its existing four-tuple.
Bindings are exact segment_id/reference_settlement_id/baseline_hash mappings.
The caller owns commit and authorization; freeze takes Order, Refund, then
sorted Baseline locks. Core capture must preserve funded_refund_plan_hash.
Neither installation nor this module calls or replaces existing refund hooks.
"""
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction

from fastapi import HTTPException
from sqlalchemy import Column, DateTime, ForeignKey, String, Text, event, select

from .settlement_finance import SHARES, canonical, money

CENT = Decimal('.01')


def digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def fail(reason):
    raise HTTPException(409, reason)


def parsed(raw, expected):
    if digest(raw) != expected:
        fail('funded_refund_evidence_hash_mismatch')
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        fail('funded_refund_evidence_json_invalid')


def allocation(weights, target):
    """Capped highest-averages apportionment, monotone for every cumulative cent.

    Lower quotas skip the bulk of seats; fewer than len(weights) remain.
    Exact rational comparisons avoid floating-point ties and Alabama reversals.
    """
    weights = [int(money(w) / CENT) for w in weights]
    seats = int(money(target) / CENT)
    total = sum(weights)
    if any(w < 0 for w in weights) or not 0 <= seats <= total:
        fail('funded_refund_allocation_exceeds_source')
    counts = [w * seats // total if total else 0 for w in weights]
    for _ in range(seats - sum(counts)):
        candidates = [i for i, w in enumerate(weights) if counts[i] < w]
        index = min(candidates, key=lambda i: (Fraction(counts[i] + 1, weights[i]), i))
        counts[index] += 1
    return [CENT * count for count in counts]


def install(ns):
    if 'settlement_funded_refund' in ns:
        return ns['settlement_funded_refund']
    finance = ns['settlement_finance']
    Baseline, Position = finance['Baseline'], finance['Position']

    class Plan(ns['Base']):
        __tablename__ = 'settlement_funded_refund_plans'
        refund_id = Column(ForeignKey('refunds.id'), primary_key=True)
        order_id = Column(String(36), nullable=False, index=True)
        payload_json = Column(Text, nullable=False)
        payload_hash = Column(String(64), nullable=False)
        actor = Column(String(180), nullable=False)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class Slice(ns['Base']):
        __tablename__ = 'settlement_funded_refund_slices'
        refund_id = Column(ForeignKey(Plan.refund_id), primary_key=True)
        source_settlement_id = Column(ForeignKey(Baseline.settlement_id), primary_key=True, index=True)
        segment_id = Column(String(120), nullable=False)
        source_hash = Column(String(64), nullable=False)
        payload_json = Column(Text, nullable=False)
        payload_hash = Column(String(64), nullable=False)

    class Posting(ns['Base']):
        __tablename__ = 'settlement_funded_refund_postings'
        refund_id = Column(ForeignKey(Plan.refund_id), primary_key=True)
        settlement_id = Column(ForeignKey(Baseline.settlement_id), nullable=False, unique=True)
        plan_hash = Column(String(64), nullable=False)
        payload_json = Column(Text, nullable=False)
        payload_hash = Column(String(64), nullable=False)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def immutable(*_):
        raise ValueError('funded refund evidence is immutable')

    for model in (Plan, Slice, Posting):
        event.listen(model, 'before_update', immutable)
        event.listen(model, 'before_delete', immutable)

    def audit(db, actor, action, refund_id, **values):
        ns['audit'](db, str(actor), action, 'settlement_funded_refund', refund_id,
            category='settlement', business_domain='settlement', after=values)

    def completed(db, refund):
        hook = ns.get('settlement_funded_refund_evidence', {}).get('completed_evidence')
        if not callable(hook):
            fail('funded_refund_completion_contract_required')
        verified, plan, raw, values = hook(db, refund)
        evidence = json.loads(raw)
        if (verified.id != refund.id or verified.order_id != refund.order_id or verified.status != 'completed'
                or not isinstance(verified.completed_at, datetime) or money(verified.amount, True) != money(refund.amount, True)
                or evidence['economic_plan'] != plan or money(values[0]) != money(refund.amount)
                or any(not evidence.get(k) for k in ('completion_execution_id', 'plan_hash', 'proof_hash', 'funding_hash', 'recovery_id'))):
            fail('funded_refund_completion_mismatch')
        return verified, plan, digest(raw)

    def source_evidence(db, baseline, paid):
        snapshot = parsed(baseline.snapshot_json, baseline.snapshot_hash)
        if snapshot.get('actual_receipts') is None:
            fail('funded_refund_source_cash_basis_unproven')
        shares = {k: money(snapshot['shares'][k]) for k in SHARES}
        if (baseline.currency != snapshot['currency'] or baseline.revenue != money(snapshot['revenue'])
                or baseline.cost != money(snapshot['cost']) or baseline.profit != money(snapshot['profit'])
                or baseline.revenue - baseline.cost != baseline.profit or sum(shares.values()) != baseline.profit
                or baseline.revenue < paid or not 0 <= baseline.cost <= paid or any(v < 0 for v in shares.values())
                or money(snapshot['actual_receipts'], True) != paid or snapshot.get('cost_exception_application')):
            fail('funded_refund_source_cash_or_economics_mismatch')
        records = snapshot['receipt_records']
        payment_ids = [r['payment_id'] for r in records]
        if not records or len(set(payment_ids)) != len(records) or sum(money(r['amount']) for r in records) != paid:
            fail('funded_refund_source_receipts_required')
        for record in records:
            payment = db.get(ns['Payment'], record['payment_id'])
            if (payment is None or payment.order_id != baseline.order_id or payment.status != 'paid'
                    or record['status'] != 'paid' or money(payment.amount) != money(record['amount'])):
                fail('funded_refund_source_receipt_mismatch')
        positions = db.scalars(select(Position).where(Position.settlement_id == baseline.settlement_id)).all()
        fields = ('kind', 'share_kind', 'participant_type', 'participant_id', 'participant_name')
        def key(e):
            return tuple(e[k] for k in fields) + (str(money(e['amount'])),)
        if Counter(key(e) for e in snapshot['entries']) != Counter(key({**{k: getattr(p, k) for k in fields}, 'amount': p.amount}) for p in positions):
            fail('funded_refund_source_positions_mismatch')
        if any(p.blocked_reason not in ('', 'unfunded_settlement_adjustment') or not p.participant_id for p in positions):
            fail('funded_refund_source_subjects_unproven')
        if any(p.order_id != baseline.order_id or p.currency != baseline.currency
                or p.direction != ('internal' if p.share_kind == 'platform_fee' else 'payable')
                or p.kind == 'cost' and p.participant_id != baseline.provider_enterprise_id for p in positions):
            fail('funded_refund_source_position_scope_mismatch')
        gap = baseline.revenue - paid
        if not gap:
            return snapshot, dict(amount='0.00', source='none', payment_ids=payment_ids)
        proof = db.get(finance['FundingProof'], baseline.settlement_id)
        if proof is None:
            fail('funded_refund_independent_funding_proof_required')
        request = db.get(finance['FundingRequest'], proof.request_id)
        decision = db.get(finance['FundingDecision'], proof.request_id)
        absorption = db.get(finance['FundingAbsorption'], proof.request_id)
        payload = parsed(request.payload_json, request.payload_hash)
        if payload['kind'] != 'platform_absorption':
            fail('funded_refund_funding_source_unsupported')
        evidence = parsed(proof.evidence_json, proof.evidence_hash)
        fact = parsed(absorption.evidence_json, absorption.evidence_hash)
        if (request.settlement_id != baseline.settlement_id or absorption.settlement_id != baseline.settlement_id
                or payload['settlement_id'] != baseline.settlement_id or payload['baseline_hash'] != baseline.snapshot_hash
                or payload['currency'] != baseline.currency or payload['kind'] != 'platform_absorption'
                or money(payload['amount']) != gap or money(payload['actual_receipts']) != paid
                or (payload['bearer']['participant_type'], payload['bearer']['participant_id']) != ('platform', 'platform')
                or decision.decision != 'approved' or not decision.note.strip() or decision.reviewer_id == request.creator_id
                or proof.verifier_id == request.creator_id or proof.source_key != 'platform:' + baseline.settlement_id
                or evidence['request_id'] != request.id or evidence['reviewer_id'] != decision.reviewer_id
                or evidence['verifier_id'] != proof.verifier_id or evidence['absorption_request_id'] != request.id
                or any(evidence.get(k) != v for k, v in payload.items()) or evidence.get('bank_receipt_verified') is not False
                or absorption.amount != gap or absorption.currency != baseline.currency
                or fact['request_id'] != request.id or fact['settlement_id'] != baseline.settlement_id
                or fact['baseline_hash'] != baseline.snapshot_hash or money(fact['amount']) != gap
                or fact['currency'] != baseline.currency or money(fact['cash_amount']) != 0
                or (fact['participant_type'], fact['participant_id']) != ('platform', 'platform')
                or fact['creator_id'] != request.creator_id or fact['reviewer_id'] != decision.reviewer_id
                or fact['verifier_id'] != proof.verifier_id or fact['evidence_refs'] != payload['evidence_refs']):
            fail('funded_refund_funding_evidence_mismatch')
        tx = db.get(finance['Transaction'], fact['transaction_id'])
        confirmation = db.get(finance['Confirmation'], fact['transaction_id'])
        if (tx is None or confirmation is None or tx.kind != 'internal_absorption' or tx.mode != 'manual'
                or tx.reference != 'funding:' + request.id or tx.creator_id != request.creator_id
                or confirmation.reviewer_id != proof.verifier_id or not isinstance(confirmation.created_at, datetime)
                or not isinstance(tx.created_at, datetime) or not isinstance(baseline.created_at, datetime)
                or evidence['transaction_id'] != tx.id):
            fail('funded_refund_funding_transaction_mismatch')
        def utc(dt):
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        if utc(confirmation.created_at) < max(utc(tx.created_at), utc(baseline.created_at)) or utc(confirmation.created_at) > datetime.now(timezone.utc):
            fail('funded_refund_funding_time_unproven')
        declared = {a['position_id']: money(a['amount'], True) for a in fact['allocations']}
        tx_allocations = json.loads(tx.payload_json)['allocations']
        actual = db.scalars(select(finance['Allocation']).where(finance['Allocation'].transaction_id == tx.id)).all()
        if (not declared or len(declared) != len(fact['allocations']) or len(declared) != len(tx_allocations)
                or len(actual) != len(declared) or sum(declared.values()) != gap
                or {a['position_id']: money(a['amount'], True) for a in tx_allocations} != declared
                or {a.position_id: money(a.amount, True) for a in actual} != declared):
            fail('funded_refund_funding_allocations_mismatch')
        for pid, value in declared.items():
            p = next((p for p in positions if p.id == pid), None)
            if (p is None or (p.kind, p.share_kind, p.direction, p.participant_type, p.participant_id, p.currency) !=
                    ('profit', 'platform_fee', 'internal', 'platform', 'platform', baseline.currency) or value > p.amount):
                fail('funded_refund_absorption_source_mismatch')
        if sum(p.amount for p in positions if p.direction == 'payable') > paid:
            fail('funded_refund_external_payables_exceed_cash')
        return snapshot, dict(amount=str(gap), source='platform_absorption', request_id=request.id,
            proof_hash=proof.evidence_hash, absorption_hash=absorption.evidence_hash,
            transaction_id=tx.id, allocations={k: str(v) for k, v in declared.items()},
            confirmed_at=utc(confirmation.created_at).isoformat(), payment_ids=payment_ids,
            bearer=dict(participant_type='platform', participant_id='platform'), bank_receipt_verified=False)

    def target(baseline, snapshot, paid, cumulative, gap):
        gross = (baseline.revenue * cumulative / paid).quantize(CENT, rounding=ROUND_HALF_UP)
        cost = (baseline.cost * cumulative / paid).quantize(CENT, rounding=ROUND_HALF_UP)
        profit = gross - cost
        shares = dict(zip(SHARES, allocation([snapshot['shares'][k] for k in SHARES], profit)))
        # The residual is the rounded funded revenue, not another rounded cash leg.
        absorption = gross - cumulative
        if not 0 <= absorption <= gap:
            fail('funded_refund_absorption_exceeds_source')
        entries = []
        groups = sorted({(e['kind'], e['share_kind']) for e in snapshot['entries']})
        for kind, field in groups:
            group = sorted([e for e in snapshot['entries'] if (e['kind'], e['share_kind']) == (kind, field)], key=canonical)
            if kind not in ('cost', 'profit') or kind == 'profit' and field not in SHARES:
                fail('funded_refund_source_entry_kind_unsupported')
            values = allocation([e['amount'] for e in group], cost if kind == 'cost' else shares[field])
            entries.extend(dict(e, amount=str(value), blocked_reason='') for e, value in zip(group, values))
        if sum(money(e['amount']) for e in entries) != gross:
            fail('funded_refund_source_entry_conservation_failed')
        return dict(gross=gross, cost=cost, profit=profit, shares=shares, absorption=absorption, entries=entries)

    def build(db, refund, bindings, *, lock=False):
        Order, Refund, Settlement = ns['Order'], ns['Refund'], ns['Settlement']
        order_query = select(Order).where(Order.id == refund.order_id)
        order = db.scalar(order_query.with_for_update().execution_options(populate_existing=True) if lock else order_query)
        if order is None:
            fail('funded_refund_order_missing')
        if lock:
            refund = db.scalar(select(Refund).where(Refund.id == refund.id).with_for_update().execution_options(populate_existing=True))
        if refund is None or refund.order_id != order.id:
            fail('funded_refund_order_identity_mismatch')
        refund, plan, completion_hash = completed(db, refund)
        change = ns.get('version_change_settlement', {}).get('is_change_refund')
        if callable(change) and change(db, refund):
            fail('funded_refund_ordinary_only')
        exception = ns.get('settlement_parties', {}).get('cost_exception')
        if callable(exception) and exception(db, refund.id):
            fail('funded_refund_cost_exception_combination_requires_explicit_plan')
        segments = plan.get('segments', [plan])
        supplied = {b['segment_id']: b for b in bindings}
        if (not segments or len(supplied) != len(bindings) or len({p['segment_id'] for p in segments}) != len(segments)
                or set(supplied) != {p['segment_id'] for p in segments}):
            fail('funded_refund_complete_unique_bindings_required')
        ids = sorted(b['reference_settlement_id'] for b in bindings)
        if len(set(ids)) != len(ids):
            fail('funded_refund_duplicate_source_baseline')
        query = select(Baseline).where(Baseline.settlement_id.in_(ids)).order_by(Baseline.settlement_id)
        sources = {b.settlement_id: b for b in db.scalars(query.with_for_update().execution_options(populate_existing=True) if lock else query)}
        if set(sources) != set(ids):
            fail('funded_refund_source_baseline_missing')
        Payment = ns['Payment']
        paid_total = sum((money(p.amount, True) for p in db.scalars(select(Payment).where(
            Payment.order_id == order.id, Payment.status == 'paid'))), Decimal(0))
        completed_total = sum((money(r.amount, True) for r in db.scalars(select(Refund).where(
            Refund.order_id == order.id, Refund.status == 'completed'))), Decimal(0))
        if completed_total > paid_total:
            fail('funded_refund_buyer_cash_cap_exceeded')
        refs, used_payments = [], set()
        old_plan = db.get(Plan, refund.id)
        old_payload = parsed(old_plan.payload_json, old_plan.payload_hash) if old_plan else None
        for segment in sorted(segments, key=lambda p: supplied[p['segment_id']]['reference_settlement_id']):
            binding = supplied[segment['segment_id']]
            b = sources[binding['reference_settlement_id']]
            settlement = db.get(Settlement, b.settlement_id)
            if (segment['source_order_id'] != order.id or b.order_id != order.id or b.provider_enterprise_id != order.provider_enterprise_id
                    or b.snapshot_hash != binding['baseline_hash'] or settlement is None or settlement.status not in ('locked', 'paid')
                    or b.currency != segment['economic_snapshot']['currency'] or b.revenue <= 0):
                fail('funded_refund_source_scope_mismatch')
            Batch, Line = ns['SettlementBatch'], ns['SettlementLine']
            batches = db.scalars(select(Batch).join(Line, Line.batch_id == Batch.id).where(
                Line.settlement_id == b.settlement_id, Line.status != 'superseded', Batch.status != 'superseded')).all()
            if not batches or any(batch.status not in ('confirmed', 'payment_processing', 'partial_paid', 'paid', 'archived') for batch in batches):
                fail('funded_refund_source_batch_unconfirmed')
            paid, cumulative, amount = (money(segment[k], True) for k in ('original_amount', 'cumulative_refund_amount', 'refund_amount'))
            if not amount <= cumulative <= paid:
                fail('funded_refund_source_cash_cap_exceeded')
            snapshot, funding = source_evidence(db, b, paid)
            if used_payments.intersection(funding['payment_ids']):
                fail('funded_refund_overlapping_source_cash_basis')
            used_payments.update(funding['payment_ids'])
            for reserved in db.scalars(select(Plan).where(Plan.order_id == order.id, Plan.refund_id != refund.id)):
                history = parsed(reserved.payload_json, reserved.payload_hash)
                for older in history['references']:
                    if (older['reference_settlement_id'] != b.settlement_id
                            and set(older['funding']['payment_ids']).intersection(funding['payment_ids'])):
                        fail('funded_refund_overlapping_reserved_cash_basis')
            prior, prior_ids = Decimal(0), []
            for old in db.scalars(select(Slice).where(Slice.source_settlement_id == b.settlement_id, Slice.refund_id != refund.id)):
                previous = parsed(old.payload_json, old.payload_hash)
                parent = db.get(Plan, old.refund_id)
                parent_payload = parsed(parent.payload_json, parent.payload_hash) if parent else {}
                if (old.source_hash != b.snapshot_hash or old.segment_id != segment['segment_id']
                        or [r for r in parent_payload.get('references', []) if r['reference_settlement_id'] == b.settlement_id] != [previous]):
                    fail('funded_refund_prior_source_mismatch')
                prior += money(previous['buyer_cash_amount'])
                prior_ids.append(old.refund_id)
            if prior != cumulative - amount and not old_payload:
                fail('funded_refund_cumulative_history_conflict')
            previous = target(b, snapshot, paid, cumulative - amount, money(funding['amount']))
            current = target(b, snapshot, paid, cumulative, money(funding['amount']))
            shares = {k: previous['shares'][k] - current['shares'][k] for k in SHARES}
            entries = [dict(e, amount=str(money(e['amount']) - money(p['amount']))) for e, p in zip(current['entries'], previous['entries'])]
            if any(money(e['amount']) < 0 for e in entries):
                fail('funded_refund_nonmonotone_entry_reversal')
            ref = dict(segment_id=segment['segment_id'], source_order_id=order.id, reference_settlement_id=b.settlement_id,
                baseline_hash=b.snapshot_hash, currency=b.currency, original_amount=str(paid), buyer_cash_amount=str(amount),
                cumulative_refund_amount=str(cumulative), gross_amount=str(previous['gross'] - current['gross']),
                cost_amount=str(previous['cost'] - current['cost']), profit_amount=str(previous['profit'] - current['profit']),
                shares={k: str(v) for k, v in shares.items()}, entries=entries, funding=funding,
                absorption_reversal=str(current['absorption'] - previous['absorption']),
                cumulative_absorption_reversal=str(current['absorption']), prior_refund_ids=sorted(prior_ids), segment_plan_hash=digest(canonical(segment)))
            if old_payload:
                old_refs = [r for r in old_payload['references'] if r['reference_settlement_id'] == b.settlement_id]
                if len(old_refs) != 1:
                    fail('funded_refund_frozen_source_mismatch')
                ref['prior_refund_ids'] = old_refs[0]['prior_refund_ids']
            # Legacy/other adapters must not already have consumed this source.
            posted_ids = select(Posting.settlement_id)
            for other in db.scalars(select(Baseline).where(Baseline.reference_settlement_id == b.settlement_id,
                    Baseline.revenue < 0, Baseline.settlement_id.not_in(posted_ids))):
                other_settlement = db.get(Settlement, other.settlement_id)
                other_snapshot = parsed(other.snapshot_json, other.snapshot_hash)
                if (old_plan is None or other_settlement.refund_id != refund.id
                        or other_snapshot.get('funded_refund_plan_hash') != old_plan.payload_hash):
                    fail('funded_refund_existing_other_reversal_requires_review')
            MultiSource = finance.get('RefundSource')
            if MultiSource is not None:
                for other_id in db.scalars(select(MultiSource.settlement_id).where(
                        MultiSource.source_settlement_id == b.settlement_id, MultiSource.settlement_id.not_in(posted_ids))):
                    other = db.get(Baseline, other_id)
                    other_settlement = db.get(Settlement, other_id)
                    other_snapshot = parsed(other.snapshot_json, other.snapshot_hash) if other else {}
                    if (old_plan is None or other_settlement is None or other_settlement.refund_id != refund.id
                            or other_snapshot.get('funded_refund_plan_hash') != old_plan.payload_hash):
                        fail('funded_refund_existing_other_reversal_requires_review')
            refs.append(ref)
        if sum(money(r['buyer_cash_amount']) for r in refs) != money(refund.amount):
            fail('funded_refund_completed_cash_total_mismatch')
        if not any(r['funding']['source'] == 'platform_absorption' for r in refs):
            fail('funded_refund_not_a_funded_source')
        if len({r['currency'] for r in refs}) != 1:
            fail('funded_refund_mixed_currency')
        if old_payload:
            slices = db.scalars(select(Slice).where(Slice.refund_id == refund.id)).all()
            if (len(slices) != len(old_payload['references']) or sorted(
                    (parsed(s.payload_json, s.payload_hash) for s in slices), key=canonical) !=
                    sorted(old_payload['references'], key=canonical)):
                fail('funded_refund_frozen_slices_mismatch')
        return dict(schema_version=1, kind='confirmed_funded_refund', refund_id=refund.id, order_id=order.id,
            rounding_version='cumulative_halfup_gross_cost_highest_averages_roles_subjects_v1',
            currency=refs[0]['currency'], completion_evidence_hash=completion_hash, completion_plan_hash=digest(canonical(plan)),
            buyer_cash_amount=str(money(refund.amount)), references=refs,
            **{k: str(sum(money(r[k]) for r in refs)) for k in ('gross_amount', 'cost_amount', 'profit_amount', 'absorption_reversal')},
            shares={k: str(sum(money(r['shares'][k]) for r in refs)) for k in SHARES},
            adjustment_kind='internal_funding_absorption_reversal', external_paid='0.00', external_recovered='0.00', bank_receipt_verified=False)

    def basis(db, refund, bindings):
        try:
            return dict(status='ready', **build(db, refund, bindings))
        except HTTPException as exc:
            return dict(status='waiting', reason=exc.detail)
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
            return dict(status='waiting', reason='funded_refund_evidence_incomplete')

    def freeze(db, refund, bindings, actor='system'):
        with db.begin_nested():
            try:
                payload = build(db, refund, bindings, lock=True)
            except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
                fail('funded_refund_evidence_incomplete')
            raw = canonical(payload)
            old = db.get(Plan, refund.id)
            if old:
                if old.payload_json != raw or old.payload_hash != digest(raw):
                    fail('funded_refund_frozen_plan_changed')
            else:
                db.add(Plan(refund_id=refund.id, order_id=refund.order_id, payload_json=raw, payload_hash=digest(raw), actor=str(actor)))
                db.flush()
                for ref in payload['references']:
                    encoded = canonical(ref)
                    db.add(Slice(refund_id=refund.id, source_settlement_id=ref['reference_settlement_id'], segment_id=ref['segment_id'],
                        source_hash=ref['baseline_hash'], payload_json=encoded, payload_hash=digest(encoded)))
                db.flush()
                audit(db, actor, 'funded_refund_freeze', refund.id, plan_hash=digest(raw),
                    buyer_cash_amount=payload['buyer_cash_amount'], financial_gross_amount=payload['gross_amount'],
                    absorption_reversal=payload['absorption_reversal'], bank_receipt_verified=False)
            return dict(status='ready', **payload, plan_hash=digest(raw))

    def post(db, refund, bindings, settlement_id, actor='system'):
        with db.begin_nested():
            frozen = freeze(db, refund, bindings, actor)
            b = db.get(Baseline, settlement_id)
            settlement = db.get(ns['Settlement'], settlement_id)
            if b is None or settlement is None:
                fail('funded_refund_captured_negative_baseline_required')
            snapshot = parsed(b.snapshot_json, b.snapshot_hash)
            if (settlement.refund_id != refund.id or settlement.order_id != refund.order_id
                    or b.provider_enterprise_id != db.get(ns['Order'], refund.order_id).provider_enterprise_id
                    or settlement.status not in ('locked', 'paid') or b.order_id != refund.order_id
                    or b.currency != frozen['currency'] or snapshot.get('funded_refund_plan_hash') != frozen['plan_hash']
                    or any(money(getattr(b, field)) != money(frozen[key]) for field, key in
                        (('revenue', 'gross_amount'), ('cost', 'cost_amount'), ('profit', 'profit_amount')))
                    or any(money(snapshot['shares'][k]) != money(frozen['shares'][k]) for k in SHARES)):
                fail('funded_refund_capture_plan_mismatch')
            refs = frozen['references']
            if (len(refs) == 1 and b.reference_settlement_id != refs[0]['reference_settlement_id']
                    or len(refs) > 1 and (b.reference_settlement_id or
                        {(r['reference_settlement_id'], r['baseline_hash']) for r in snapshot.get('references', [])} !=
                        {(r['reference_settlement_id'], r['baseline_hash']) for r in refs})):
                fail('funded_refund_capture_sources_mismatch')
            fields = ('kind', 'share_kind', 'participant_type', 'participant_id', 'participant_name')
            def key(entry):
                return tuple(entry[k] for k in fields) + (str(money(entry['amount'])),)
            expected = Counter(key(e) for r in refs for e in r['entries'] if money(e['amount']))
            actual_positions = db.scalars(select(Position).where(Position.settlement_id == settlement_id)).all()
            if (Counter(key(e) for e in snapshot['entries'] if money(e['amount'])) != expected
                    or Counter(key({**{k: getattr(p, k) for k in fields}, 'amount': p.amount}) for p in actual_positions) != expected
                    or any(p.direction != ('internal' if p.share_kind == 'platform_fee' else 'receivable')
                        or p.order_id != refund.order_id or p.currency != b.currency for p in actual_positions)):
                fail('funded_refund_capture_positions_mismatch')
            value = dict(refund_id=refund.id, settlement_id=b.settlement_id, baseline_hash=b.snapshot_hash,
                plan_hash=frozen['plan_hash'], absorption_reversal=frozen['absorption_reversal'],
                references=[dict(reference_settlement_id=r['reference_settlement_id'], baseline_hash=r['baseline_hash'],
                    funding=r['funding'], absorption_reversal=r['absorption_reversal']) for r in refs],
                direction='internal', participant_type='platform', participant_id='platform',
                external_paid='0.00', external_recovered='0.00', bank_receipt_verified=False)
            raw = canonical(value)
            old = db.get(Posting, refund.id)
            if old:
                if old.payload_json != raw or old.payload_hash != digest(raw):
                    fail('funded_refund_posting_changed')
            else:
                db.add(Posting(refund_id=refund.id, settlement_id=b.settlement_id, plan_hash=frozen['plan_hash'], payload_json=raw, payload_hash=digest(raw)))
                db.flush()
                audit(db, actor, 'funded_refund_post', refund.id, settlement_id=b.settlement_id,
                    plan_hash=frozen['plan_hash'], evidence_hash=digest(raw), absorption_reversal=frozen['absorption_reversal'],
                    external_paid='0.00', external_recovered='0.00', bank_receipt_verified=False)
            return dict(status='ready', **value, evidence_hash=digest(raw))

    def describe(db, settlement_id):
        """One supplementary internal credit, never another ordinary profit row.

        Authorization belongs to the report caller. Historical projection uses
        this immutable posting time, not the date of a refund's business month.
        """
        row = db.scalar(select(Posting).where(Posting.settlement_id == settlement_id))
        if row is None:
            return None
        value = parsed(row.payload_json, row.payload_hash)
        plan = db.get(Plan, row.refund_id)
        baseline = db.get(Baseline, settlement_id)
        if (plan is None or baseline is None or not isinstance(row.created_at, datetime)
                or value['refund_id'] != row.refund_id or value['settlement_id'] != row.settlement_id
                or value['plan_hash'] != row.plan_hash or row.plan_hash != plan.payload_hash
                or digest(plan.payload_json) != plan.payload_hash or value['baseline_hash'] != baseline.snapshot_hash
                or parsed(baseline.snapshot_json, baseline.snapshot_hash).get('funded_refund_plan_hash') != row.plan_hash):
            fail('funded_refund_projection_evidence_mismatch')
        recorded = row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at.astimezone(timezone.utc)
        return dict(value, kind='internal_funding_absorption_reversal', currency=baseline.currency,
            recorded_at=recorded.isoformat(), evidence_hash=row.payload_hash)

    hooks = dict(Plan=Plan, Slice=Slice, Posting=Posting, basis=basis, freeze=freeze, post=post, describe=describe)
    ns['settlement_funded_refund'] = hooks
    return hooks
