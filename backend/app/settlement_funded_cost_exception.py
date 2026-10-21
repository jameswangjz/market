"""Frozen approved retained costs over raw funded refunds; never changes cash legs.

The funded-plan owner calls apply before freezing its Plan. Capture validates the
result and records post in the same transaction as its immutable negative ledger.
All hooks are transaction-neutral: no commits and no buyer execution mutations.
"""
import hashlib
import json
from collections import Counter
from decimal import Decimal, ROUND_DOWN

from fastapi import HTTPException
from sqlalchemy import Column, ForeignKey, String, Text, event, select

from .settlement_parties import canonical, number

SHARES = ('platform_fee', 'provider_share', 'service_share', 'expert_fee', 'channel_fee')
FIELDS = dict(platform='platform_fee', enterprise='provider_share', service='service_share',
              expert='expert_fee', channel='channel_fee')


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def money(value):
    return number(value, cents=True)


def fail(reason):
    raise HTTPException(409, reason)


def install(ns):
    if 'settlement_funded_cost_exception' in ns:
        return ns['settlement_funded_cost_exception']
    Base = ns['Base']

    class Application(Base):
        __tablename__ = 'settlement_funded_cost_applications'
        refund_id = Column(ForeignKey('refunds.id'), primary_key=True)
        exception_id = Column(String(36), nullable=False, unique=True)
        plan_json = Column(Text, nullable=False)
        plan_hash = Column(String(64), nullable=False)
        actor = Column(String(180), nullable=False)

    class Posting(Base):
        __tablename__ = 'settlement_funded_cost_postings'
        refund_id = Column(ForeignKey(Application.refund_id), primary_key=True)
        settlement_id = Column(String(36), nullable=False, unique=True)
        plan_hash = Column(String(64), nullable=False)

    def immutable(*_):
        raise ValueError('funded cost exception evidence is immutable')

    for model in (Application, Posting):
        event.listen(model, 'before_update', immutable)
        event.listen(model, 'before_delete', immutable)

    def load(row):
        plan = json.loads(row.plan_json)
        if digest(plan) != row.plan_hash:
            fail('funded_cost_application_hash_mismatch')
        return plan

    def raw_funded_basis(db, refund):
        """Map exact sources via the capture owner; never evaluates exceptions."""
        try:
            evidence = ns.get('settlement_funded_refund_evidence', {}).get('completed_evidence')
            mapper = ns.get('settlement_funded_refund_capture', {}).get('bindings')
            basis = ns.get('settlement_funded_refund', {}).get('basis')
            if not all(callable(hook) for hook in (evidence, mapper, basis)):
                fail('raw_funded_refund_contract_required')
            _, plan, _, _ = evidence(db, refund)
            segments = plan.get('segments', [plan])
            source = None
            if len(segments) == 1 and segments[0]['calculation_version'] in (
                    'ordinary-refund-plan-v1', 'ordinary-file-refund-plan-v1'):
                Baseline, Settlement = ns['settlement_finance']['Baseline'], ns['Settlement']
                rows = db.scalars(select(Baseline).join(Settlement, Settlement.id == Baseline.settlement_id).where(
                    Baseline.order_id == refund.order_id, Baseline.revenue > 0,
                    Settlement.status.in_(('locked', 'paid'))).order_by(Baseline.settlement_id)).all()
                if len(rows) != 1:
                    fail('raw_funded_refund_unique_ordinary_source_required')
                source = rows[0]
            bindings = mapper(db, refund, source)
            return basis(db, refund, bindings, apply_exception=False)
        except HTTPException as exc:
            return dict(status='waiting', reason=exc.detail)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            return dict(status='waiting', reason='raw_funded_refund_evidence_incomplete')

    def entries_after_retention(ref, retained, loss):
        entries = [dict(e) for e in ref['entries']]
        if any(e['kind'] not in ('cost', 'profit') or money(e['amount']) < 0 for e in entries):
            fail('funded_cost_normal_entries_invalid')
        indexes = sorted((i for i, e in enumerate(entries) if e['kind'] == 'cost'), key=lambda i: canonical(entries[i]))
        cost = sum((money(entries[i]['amount']) for i in indexes), Decimal(0))
        if cost != -money(ref['cost_amount']) or sum((money(e['amount']) for e in entries), Decimal(0)) != -money(ref['gross_amount']):
            fail('funded_cost_normal_entries_not_conserved')
        for field in SHARES:
            if sum((money(e['amount']) for e in entries if e['kind'] == 'profit' and e['share_kind'] == field), Decimal(0)) != -money(ref['shares'][field]):
                fail('funded_cost_normal_profit_entries_not_conserved')
        if retained:
            raw = [money(entries[i]['amount']) * (cost - retained) / cost for i in indexes]
            amounts = [value.quantize(Decimal('.01'), rounding=ROUND_DOWN) for value in raw]
            remainder = int((cost - retained - sum(amounts, Decimal(0))) * 100)
            for j in sorted(range(len(indexes)), key=lambda j: (-(raw[j] - amounts[j]), canonical(entries[indexes[j]])))[:remainder]:
                amounts[j] += Decimal('.01')
            for i, amount in zip(indexes, amounts):
                entries[i]['amount'] = str(amount)
            entries.append({k: v for k, v in loss.items() if k not in ('currency', 'direction')})
        entries = [e for e in entries if money(e['amount'])]
        if sum((money(e['amount']) for e in entries), Decimal(0)) != -money(ref['gross_amount']):
            fail('funded_cost_adjusted_entries_not_conserved')
        return entries

    def apply(db, refund, normal_basis, proof=None, actor='system'):
        if normal_basis.get('status', 'ready') != 'ready':
            fail('funded_cost_normal_basis_invalid')
        # Adapter convenience fields duplicate exact references. Keep the same
        # application hash whether called from raw HTTP basis or plan freeze.
        adapter_fields = {'status', 'reference_settlement_id', 'baseline_hash', 'segment_id',
                          'window_hash', 'segment_context_hash', 'cumulative_refund_amount', 'original_amount'}
        normal_basis = {k: v for k, v in normal_basis.items() if k not in adapter_fields}
        hook = ns.get('settlement_parties', {}).get('cost_exception_funded_basis')
        verified = hook(db, refund, normal_basis['references']) if callable(hook) else None
        if proof is not None and proof != verified:
            fail('funded_cost_approval_not_verified')
        if proof is None:
            proof = verified
            if proof is None:
                approved = ns.get('settlement_parties', {}).get('cost_exception')
                if callable(approved) and approved(db, refund.id):
                    fail('approved_funded_cost_proof_required')
                return dict(normal_basis, status='ready')
        if (proof.get('status') != 'ready' or proof.get('basis_kind') != 'funded'
                or proof.get('schema_version') != 2 or proof.get('baseline_id') != ''
                or any(not proof.get(k) for k in ('id', 'creator_id', 'reviewer_id', 'approved_at', 'reason', 'decision_reason'))
                or proof['creator_id'] == proof['reviewer_id'] or proof.get('refund_id') != refund.id
                or proof.get('order_id') != refund.order_id or proof.get('currency') != normal_basis['currency']
                or money(proof['refund_amount']) != money(refund.amount)
                or proof.get('approval_evidence_hash') != digest({k: v for k, v in proof.items() if k != 'approval_evidence_hash'})):
            fail('approved_funded_cost_proof_invalid')
        if (normal_basis.get('kind') != 'confirmed_funded_refund'
                or normal_basis.get('refund_id') != refund.id or normal_basis.get('order_id') != refund.order_id
                or money(normal_basis['buyer_cash_amount']) != money(refund.amount)):
            fail('funded_cost_normal_basis_invalid')
        refs = normal_basis['references']
        allocations = proof['source_allocations']
        supplied = {(a['segment_id'], a['baseline_id']): a for a in allocations}
        keys = {(r['segment_id'], r['reference_settlement_id']) for r in refs}
        if not refs or len(keys) != len(refs) or len(supplied) != len(allocations) or set(supplied) != keys:
            fail('funded_cost_complete_unique_sources_required')
        bearer = proof['bearer']
        field = FIELDS.get(bearer.get('participant_type'))
        order = db.get(ns['Order'], refund.order_id)
        if (field is None or order is None or bearer.get('currency') != normal_basis['currency']
                or any(not bearer.get(k) for k in ('participant_id', 'participant_name', 'registry_id'))
                or field == 'platform_fee' and bearer['participant_id'] != 'platform'
                or field == 'provider_share' and bearer['participant_id'] != order.provider_enterprise_id):
            fail('funded_cost_bearer_invalid')
        adjusted, retained_total = [], Decimal(0)
        for ref in refs:
            allocation = supplied[(ref['segment_id'], ref['reference_settlement_id'])]
            retained, evidence = money(allocation['retained_cost']), allocation.get('evidence_refs')
            if (allocation.get('baseline_hash') != ref['baseline_hash'] or allocation.get('basis') != ref
                    or allocation.get('basis_hash') != digest(ref) or not 0 <= retained <= -money(ref['cost_amount'])
                    or not isinstance(evidence, list) or not evidence
                    or any(not isinstance(e, str) or not e.strip() for e in evidence)):
                fail('funded_cost_source_proof_invalid')
            shares = {k: money(ref['shares'][k]) for k in SHARES}
            if (money(ref['gross_amount']) >= 0 or money(ref['cost_amount']) > 0 or any(v > 0 for v in shares.values())
                    or money(ref['gross_amount']) - money(ref['cost_amount']) != money(ref['profit_amount'])
                    or sum(shares.values(), Decimal(0)) != money(ref['profit_amount'])):
                fail('funded_cost_normal_conservation_failed')
            cost = money(ref['cost_amount']) + retained
            shares[field] -= retained
            loss = dict(kind='loss', share_kind=field, participant_type=bearer['participant_type'],
                participant_id=bearer['participant_id'], participant_name=bearer['participant_name'],
                amount=str(retained), currency=ref['currency'], blocked_reason='',
                direction='internal' if field == 'platform_fee' else 'receivable') if retained else None
            adjusted.append(dict(ref, default_basis=ref, cost_amount=str(cost),
                profit_amount=str(money(ref['gross_amount']) - cost), shares={k: str(v) for k, v in shares.items()},
                entries=entries_after_retention(ref, retained, loss),
                cost_exception=dict(exception_id=proof['id'], retained_cost=str(retained), evidence_refs=evidence, loss=loss)))
            retained_total += retained
        if retained_total <= 0 or retained_total != money(proof['retained_cost']):
            fail('funded_cost_retained_total_mismatch')
        result = dict(normal_basis, references=adjusted,
            **{k: str(sum((money(r[k]) for r in adjusted), Decimal(0))) for k in ('gross_amount', 'cost_amount', 'profit_amount')},
            shares={k: str(sum((money(r['shares'][k]) for r in adjusted), Decimal(0))) for k in SHARES},
            cost_exception=dict(exception_id=proof['id'], retained_cost=str(retained_total)))
        if money(result['gross_amount']) != money(normal_basis['gross_amount']):
            fail('funded_cost_gross_changed')
        plan = dict(version='approved-funded-retained-cost-v1', refund_id=refund.id,
                    exception_id=proof['id'], default_basis=normal_basis, result=result, approval=proof)
        plan_hash = digest(plan)
        with db.begin_nested():
            old = db.get(Application, refund.id)
            if old:
                if old.plan_hash != plan_hash or load(old) != plan:
                    fail('frozen_funded_cost_application_changed')
            else:
                settlement, baseline = ns.get('Settlement'), ns.get('settlement_finance', {}).get('Baseline')
                if settlement is not None and baseline is not None and db.scalar(select(baseline.settlement_id).join(
                        settlement, settlement.id == baseline.settlement_id).where(settlement.refund_id == refund.id)):
                    fail('posted_funded_refund_requires_explicit_adjustment')
                db.add(Application(refund_id=refund.id, exception_id=proof['id'], plan_json=canonical(plan),
                                   plan_hash=plan_hash, actor=str(actor)))
                ns['audit'](db, str(actor), 'finance_funded_cost_application', 'settlement_finance', refund.id,
                    category='settlement', business_domain='settlement', after=dict(plan_hash=plan_hash,
                        exception_id=proof['id'], retained_cost=str(retained_total)))
                db.flush()
        return dict(result, status='ready', funded_cost_exception_application_hash=plan_hash)

    def validate_capture(db, refund_id, basis):
        old = db.get(Application, refund_id)
        marker = basis.get('funded_cost_exception_application_hash')
        if old is None and marker is None:
            return None
        if old is None or old.plan_hash != marker:
            fail('frozen_funded_cost_application_mismatch')
        expected = load(old)['result']
        if any(basis.get(k) != v for k, v in expected.items()):
            fail('funded_cost_capture_basis_mismatch')
        return old

    def post(db, application, settlement_id):
        if application is None:
            return
        expected = load(application)['result']
        Baseline, Settlement = ns['settlement_finance']['Baseline'], ns['Settlement']
        baseline, settlement = db.get(Baseline, settlement_id), db.get(Settlement, settlement_id)
        if (baseline is None or settlement is None or settlement.refund_id != application.refund_id
                or settlement.status not in ('locked', 'paid')
                or baseline.order_id != expected['order_id'] or baseline.currency != expected['currency']
                or any(money(getattr(baseline, field)) != money(expected[key]) for field, key in
                       (('revenue', 'gross_amount'), ('cost', 'cost_amount'), ('profit', 'profit_amount')))):
            fail('funded_cost_captured_baseline_required')
        snapshot = json.loads(baseline.snapshot_json)
        if (hashlib.sha256(baseline.snapshot_json.encode()).hexdigest() != baseline.snapshot_hash
                or snapshot.get('funded_cost_exception_application_hash') != application.plan_hash
                or snapshot.get('shares') != expected['shares'] or snapshot.get('references') != expected['references']
                or money(snapshot.get('buyer_cash_amount')) != money(expected['buyer_cash_amount'])
                or money(snapshot.get('absorption_reversal', 0)) != money(expected.get('absorption_reversal', 0))
                or money(snapshot.get('external_funding_return', 0)) != money(expected.get('external_funding_return', 0))):
            fail('funded_cost_captured_evidence_mismatch')
        fields = ('kind', 'share_kind', 'participant_type', 'participant_id', 'participant_name')
        def key(e):
            return tuple(e[k] for k in fields) + (money(e['amount']),)
        expected_entries = Counter(key(e) for r in expected['references']
            for e in r['entries'] if money(e['amount']))
        for ref in expected['references']:
            returned = Decimal(0)
            for entitlement in ref.get('funding_returns', []):
                amount = money(entitlement['amount'])
                bearer = entitlement.get('bearer', {})
                if (amount < 0 or entitlement.get('currency') != expected['currency']
                        or entitlement.get('source_settlement_id') != ref['reference_settlement_id']
                        or entitlement.get('status') != 'unpaid'
                        or ref['funding'].get('source') != 'external_receipt'
                        or bearer != ref['funding'].get('bearer')
                        or not all(bearer.get(k) for k in ('participant_type', 'participant_id', 'participant_name'))):
                    fail('funded_cost_return_evidence_mismatch')
                if amount:
                    entry = dict(kind='funding_return', share_kind='', amount=str(amount),
                        **{k: bearer[k] for k in ('participant_type', 'participant_id', 'participant_name')})
                    expected_entries.update([key(entry)])
                returned += amount
            if returned != money(ref.get('external_funding_return', 0)):
                fail('funded_cost_return_amount_mismatch')
        Position = ns['settlement_finance']['Position']
        positions = db.scalars(select(Position).where(Position.settlement_id == settlement_id)).all()
        if (Counter(key(e) for e in snapshot['entries'] if money(e['amount'])) != expected_entries
                or Counter(key(dict((k, getattr(p, k)) for k in (*fields, 'amount'))) for p in positions if p.amount) != expected_entries
                or any(p.order_id != expected['order_id'] or p.currency != expected['currency']
                    or p.direction != ('payable' if p.kind == 'funding_return' else
                                       'internal' if p.share_kind == 'platform_fee' else 'receivable') for p in positions)):
            fail('funded_cost_captured_positions_mismatch')
        old = db.get(Posting, application.refund_id)
        if old:
            if old.settlement_id != settlement_id or old.plan_hash != application.plan_hash:
                fail('funded_cost_already_posted')
        else:
            db.add(Posting(refund_id=application.refund_id, settlement_id=settlement_id, plan_hash=application.plan_hash))
            db.flush()

    hooks = dict(Application=Application, Posting=Posting, raw_funded_basis=raw_funded_basis,
                 apply=apply, validate_capture=validate_capture, post=post)
    ns['settlement_funded_cost_exception'] = hooks
    return hooks
