"""Immutable approved per-source retained-cost applications; no buyer cash I/O."""
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import Column, ForeignKey, String, Text, event, select

from .refund_execution import canonical, digest
from .refund_execution_resources import strict_json

SHARES = ('platform_fee', 'provider_share', 'service_share', 'expert_fee', 'channel_fee')
FIELDS = dict(platform='platform_fee', enterprise='provider_share', service='service_share',
              expert='expert_fee', channel='channel_fee')


def install(ns, money):
    if 'settlement_multi_cost_exception' in ns:
        return ns['settlement_multi_cost_exception']
    validate_money = money

    def money(value):
        amount = Decimal(str(value))
        return -validate_money(-amount) if amount < 0 else validate_money(amount)

    class Application(ns['Base']):
        __tablename__ = 'settlement_multi_cost_applications'
        refund_id = Column(ForeignKey('refunds.id'), primary_key=True)
        exception_id = Column(String(36), nullable=False, unique=True)
        plan_json = Column(Text, nullable=False)
        plan_hash = Column(String(64), nullable=False)
        actor = Column(String(180), nullable=False)

    class Segment(ns['Base']):
        __tablename__ = 'settlement_multi_cost_segments'
        refund_id = Column(ForeignKey(Application.refund_id), primary_key=True)
        segment_id = Column(String(120), primary_key=True)
        source_settlement_id = Column(ForeignKey('settlements.id'), nullable=False, index=True)
        source_hash = Column(String(64), nullable=False)
        payload_json = Column(Text, nullable=False)
        payload_hash = Column(String(64), nullable=False)

    class Posting(ns['Base']):
        __tablename__ = 'settlement_multi_cost_postings'
        refund_id = Column(ForeignKey(Application.refund_id), primary_key=True)
        settlement_id = Column(ForeignKey('settlements.id'), nullable=False, unique=True)
        plan_hash = Column(String(64), nullable=False)

    def immutable(*_):
        raise ValueError('multi-source cost exception evidence is immutable')

    for model in (Application, Segment, Posting):
        event.listen(model, 'before_update', immutable)
        event.listen(model, 'before_delete', immutable)

    def fail(reason):
        raise HTTPException(409, reason)

    def apply(db, refund, ordinary, proof, actor='system'):
        if proof is None:
            return ordinary
        if proof.get('status') != 'ready':
            fail(proof.get('reason', 'approved_multi_cost_exception_evidence_incomplete'))
        required = ('id', 'creator_id', 'reviewer_id', 'approved_at', 'reason', 'decision_reason', 'approval_evidence_hash')
        if (any(not proof.get(k) for k in required) or proof['creator_id'] == proof['reviewer_id']
                or proof.get('refund_id') != refund.id or proof.get('order_id') != refund.order_id
                or proof.get('currency') != ordinary['currency'] or money(proof['refund_amount']) != money(refund.amount)):
            fail('approved_multi_cost_exception_evidence_incomplete')
        evidence_hash = digest({k: v for k, v in proof.items() if k != 'approval_evidence_hash'})
        if evidence_hash != proof['approval_evidence_hash']:
            fail('approved_multi_cost_exception_hash_mismatch')
        allocations = proof['source_allocations']
        supplied = {(a['segment_id'], a['baseline_id']): a for a in allocations}
        refs = ordinary['references']
        if len(supplied) != len(allocations) or set(supplied) != {(r['segment_id'], r['reference_settlement_id']) for r in refs}:
            fail('complete_unique_multi_cost_sources_required')
        bearer = proof['bearer']
        field = FIELDS.get(bearer.get('participant_type'))
        order = db.get(ns['Order'], refund.order_id)
        if (field is None or bearer.get('currency') != ordinary['currency']
                or not all(bearer.get(k) for k in ('participant_id', 'participant_name', 'registry_id'))
                or (field == 'provider_share' and bearer['participant_id'] != order.provider_enterprise_id)
                or (field == 'platform_fee' and bearer['participant_id'] != 'platform')):
            fail('approved_multi_cost_bearer_invalid')
        adjusted, pieces, retained_total = [], [], Decimal(0)
        for ref in refs:
            allocation = supplied[(ref['segment_id'], ref['reference_settlement_id'])]
            retained = money(allocation['retained_cost'])
            evidence = allocation.get('evidence_refs')
            if (allocation.get('baseline_hash') != ref['baseline_hash'] or allocation.get('basis') != ref
                    or allocation.get('basis_hash') != digest(ref) or retained < 0 or retained > -money(ref['cost_amount'])
                    or not isinstance(evidence, list) or not evidence
                    or any(not isinstance(e, str) or not e.strip() for e in evidence)):
                fail('multi_cost_source_basis_or_retention_invalid')
            normal_shares = {k: money(ref['shares'][k]) for k in SHARES}
            if (money(ref['gross_amount']) >= 0 or money(ref['cost_amount']) > 0 or money(ref['profit_amount']) > 0
                    or any(v > 0 for v in normal_shares.values())
                    or money(ref['gross_amount']) - money(ref['cost_amount']) != money(ref['profit_amount'])
                    or sum(normal_shares.values()) != money(ref['profit_amount'])):
                fail('multi_cost_ordinary_basis_invalid')
            cost = money(ref['cost_amount']) + retained
            profit = money(ref['gross_amount']) - cost
            actual_shares = dict(normal_shares)
            actual_shares[field] -= retained
            loss = dict(kind='loss', share_kind=field, participant_type=bearer['participant_type'],
                participant_id=bearer['participant_id'], participant_name=bearer['participant_name'], amount=str(retained),
                currency=ordinary['currency'], direction='internal' if field == 'platform_fee' else 'receivable', blocked_reason='') if retained else None
            result = dict(ref, default_basis=ref, cost_amount=str(cost), profit_amount=str(profit),
                shares={k: str(v) for k, v in actual_shares.items()},
                cost_exception=dict(exception_id=proof['id'], retained_cost=str(retained), evidence_refs=evidence, loss=loss))
            piece = dict(segment_id=ref['segment_id'], source_settlement_id=ref['reference_settlement_id'],
                baseline_hash=ref['baseline_hash'], default_basis=ref, result=result, approval=allocation)
            piece_hash = digest(piece)
            result = dict(result, cost_exception_segment_hash=piece_hash)
            pieces.append((piece, piece_hash))
            adjusted.append(result)
            retained_total += retained
        if retained_total <= 0 or retained_total != money(proof['retained_cost']):
            fail('multi_cost_retained_total_mismatch')
        result = dict(ordinary, references=adjusted,
            **{k: str(sum((money(r[k]) for r in adjusted), Decimal(0))) for k in ('gross_amount', 'cost_amount', 'profit_amount')},
            shares={k: str(sum((money(r['shares'][k]) for r in adjusted), Decimal(0))) for k in SHARES},
            cost_exception=dict(exception_id=proof['id'], retained_cost=str(retained_total)))
        plan = dict(version='approved-multi-retained-cost-v1', refund_id=refund.id, order_id=refund.order_id,
                    exception_id=proof['id'], default_basis=ordinary, result=result, evidence=proof,
                    segment_hashes={p['segment_id']: h for p, h in pieces})
        raw, plan_hash = canonical(plan), digest(plan)
        with db.begin_nested():
            old = db.get(Application, refund.id)
            if old:
                if old.plan_json != raw or old.plan_hash != plan_hash or old.exception_id != proof['id']:
                    fail('frozen_multi_cost_application_mismatch')
                stored = db.scalars(select(Segment).where(Segment.refund_id == refund.id)).all()
                expected = {piece['segment_id']: (piece, h) for piece, h in pieces}
                if len(stored) != len(pieces) or any(s.segment_id not in expected
                        or s.payload_json != canonical(expected[s.segment_id][0])
                        or s.payload_hash != expected[s.segment_id][1]
                        or s.source_settlement_id != expected[s.segment_id][0]['source_settlement_id']
                        or s.source_hash != expected[s.segment_id][0]['baseline_hash'] for s in stored):
                    fail('frozen_multi_cost_segments_mismatch')
            else:
                finance = ns['settlement_finance']
                Baseline, Settlement = finance['Baseline'], ns['Settlement']
                if db.scalar(select(Baseline.settlement_id).join(Settlement, Settlement.id == Baseline.settlement_id)
                             .where(Settlement.refund_id == refund.id)):
                    fail('existing_multi_refund_posting_requires_explicit_adjustment')
                db.add(Application(refund_id=refund.id, exception_id=proof['id'], plan_json=raw, plan_hash=plan_hash, actor=str(actor)))
                db.flush()
                for piece, piece_hash in pieces:
                    db.add(Segment(refund_id=refund.id, segment_id=piece['segment_id'],
                        source_settlement_id=piece['source_settlement_id'], source_hash=piece['baseline_hash'],
                        payload_json=canonical(piece), payload_hash=piece_hash))
                ns['audit'](db, str(actor), 'finance_multi_cost_application', 'settlement_finance', refund.id,
                    category='settlement', business_domain='settlement', after=dict(plan_hash=plan_hash,
                        exception_id=proof['id'], retained_cost=str(retained_total), segment_hashes=plan['segment_hashes']))
                db.flush()
        return dict(result, cost_exception_application_hash=plan_hash)

    def validate_capture(db, refund_id, manifest):
        marker = manifest.get('cost_exception_application_hash')
        old = db.get(Application, refund_id)
        if not marker:
            if old is not None:
                fail('multi_cost_application_not_in_manifest')
            return None
        if old is None or old.plan_hash != marker or digest(strict_json(old.plan_json)) != marker:
            fail('frozen_multi_cost_application_mismatch')
        expected = dict(strict_json(old.plan_json)['result'], cost_exception_application_hash=marker)
        actual = {k: v for k, v in manifest.items() if k not in ('status', 'manifest_hash', 'settlement_id')}
        if {k: v for k, v in expected.items() if k != 'status'} != actual:
            fail('multi_cost_manifest_application_mismatch')
        return old

    def post(db, application, settlement_id):
        if application is None:
            return
        old = db.get(Posting, application.refund_id)
        if old:
            if old.settlement_id != settlement_id or old.plan_hash != application.plan_hash:
                fail('multi_cost_already_posted')
        else:
            db.add(Posting(refund_id=application.refund_id, settlement_id=settlement_id, plan_hash=application.plan_hash))
            db.flush()

    hooks = dict(Application=Application, Segment=Segment, Posting=Posting,
                 apply=apply, validate_capture=validate_capture, post=post)
    ns['settlement_multi_cost_exception'] = hooks
    return hooks
