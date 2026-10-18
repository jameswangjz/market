"""BE013 ordinary refund adapter. Caller owns the transaction and commit."""
import hashlib
import secrets
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import Column, ForeignKey, Integer, String, Text, event, select
from sqlalchemy.exc import IntegrityError

from .refund_execution import canonical, digest
from .refund_execution_resources import strict_json
from .refund_multi_source import install as install_multi_source


CENT = Decimal('0.01')
SHARES = dict(platform_fee='platform_rate', provider_share='provider_rate',
              service_share='service_rate', expert_fee='expert_rate', channel_fee='channel_rate')


def completed_utc(value):
    if not isinstance(value, datetime):
        raise ValueError('completion timestamp required')
    # SQLite drops tzinfo on legacy DateTime columns; those DB values are UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if value.utcoffset() is None:
        raise ValueError('invalid completion timestamp')
    return value.astimezone(timezone.utc)


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or result > Decimal('999999999999.99') or result != result.quantize(CENT):
        raise ValueError('invalid cents')
    return result


def segment_values(plan):
    snapshot = plan['economic_snapshot']
    version = plan['calculation_version']
    if version not in ('ordinary-refund-plan-v1', 'economic-segment-refund-v1', 'ordinary-file-refund-plan-v1'):
        raise ValueError('unsupported economic plan')
    revised = version == 'economic-segment-refund-v1'
    revenue = money(plan['refund_amount'])
    original = money(plan['original_amount'])
    cost = money(plan['cost'] if revised else snapshot['total_cost'])
    if not revised and ('cost' in plan and money(plan['cost']) != cost):
        raise ValueError('original cost mismatch')
    if revised and money(plan['revenue']) != original:
        raise ValueError('window revenue mismatch')
    cumulative = money(plan['cumulative_refund_amount'])
    valid_basis = ('manual',) if version == 'ordinary-file-refund-plan-v1' else ('calls', 'time')
    if (not plan['segment_id'] or plan['basis'] not in valid_basis
            or original <= 0 or revenue <= 0 or cost > original
            or not revenue <= cumulative <= original):
        raise ValueError('invalid segment')
    prior_cost = (cost * (cumulative - revenue) / original).quantize(CENT, rounding=ROUND_HALF_UP)
    cumulative_cost = (cost * cumulative / original).quantize(CENT, rounding=ROUND_HALF_UP)
    reversal = cumulative_cost - prior_cost
    for key, expected in (('cost_reversal_delta', reversal), ('prior_cost_reversal', prior_cost),
                          ('cumulative_cost_reversal', cumulative_cost), ('revenue_reversal_delta', revenue)):
        if (revised or key in plan) and money(plan[key]) != expected:
            raise ValueError('cost reversal mismatch')
    rule = snapshot['settlement_rule']
    if (not rule['version'] or ('mode' in rule and rule['mode'] not in ('global', 'custom'))
            or not snapshot['currency']):
        raise ValueError('missing frozen rule')
    rates = {key: Decimal(str(rule['rates'][key])) for key in SHARES.values()}
    if any(not v.is_finite() or not 0 <= v <= 100 for v in rates.values()) or sum(rates.values()) > 100:
        raise ValueError('invalid rates')
    profit = -revenue + reversal
    def cumulative_shares(amount, reversed_cost):
        total = -amount + reversed_cost
        result = {key: (total * rates[rate] / 100).quantize(
                      CENT, rounding=ROUND_HALF_UP if revised else ROUND_HALF_EVEN)
                  for key, rate in SHARES.items()}
        result['provider_share'] += total - sum(result.values())
        return result
    current = cumulative_shares(cumulative, cumulative_cost)
    prior = cumulative_shares(cumulative - revenue, prior_cost)
    shares = {key: current[key] - prior[key] for key in SHARES}
    return revenue, reversal, profit, shares


def install(ns):
    if 'refund_settlement' in ns:
        return ns['refund_settlement']

    class RefundSettlementLink(ns['Base']):
        __tablename__ = 'ordinary_refund_settlement_links'
        refund_id = Column(ForeignKey('refunds.id'), primary_key=True)
        settlement_id = Column(ForeignKey('settlements.id'), nullable=False, unique=True)
        evidence_json = Column(Text, nullable=False)
        evidence_hash = Column(String(64), nullable=False)

    class FinancialAssessment(ns['Base']):
        __tablename__ = 'ordinary_refund_financial_assessments'
        settlement_id = Column(ForeignKey('settlements.id'), primary_key=True)
        version = Column(Integer, primary_key=True)
        values_json = Column(Text, nullable=False)
        actor = Column(String(180), nullable=False)

    def immutable(*args):
        raise ValueError('refund settlement mapping is immutable')

    event.listen(RefundSettlementLink, 'before_update', immutable)
    event.listen(RefundSettlementLink, 'before_delete', immutable)
    event.listen(FinancialAssessment, 'before_update', immutable)
    event.listen(FinancialAssessment, 'before_delete', immutable)

    def financial_basis(db, refund, plan):
        """Financial evaluation only; resource/funding evidence is never changed."""
        segments = plan.get('segments', [plan])
        if len(segments) > 1:
            return multi['financial_basis'](db, refund, plan)
        if (len(segments) != 1 or segments[0].get('calculation_version') not in
                ('ordinary-refund-plan-v1', 'ordinary-file-refund-plan-v1', 'economic-segment-refund-v1')):
            return dict(status='unsupported', reason='exact_segment_final_baseline_required')
        revised = segments[0].get('calculation_version') == 'economic-segment-refund-v1'
        adjustment = ns.get('VersionChangeAdjustment')
        if not revised and adjustment is not None and db.scalar(select(adjustment.id).where(
                adjustment.source_order_id == refund.order_id, adjustment.status == 'posted')):
            return dict(status='unsupported', reason='version_change_requires_segment_baselines')
        hook = ns.get('version_change_settlement' if revised else 'settlement_finance', {}).get('refund_basis')
        if not callable(hook):
            return dict(status='waiting', reason='settlement_finance_unavailable')
        try:
            segment = segments[0]
            result = hook(db, refund, segment if revised else dict(original_amount=segment['original_amount'],
                          cumulative_refund_amount=segment['cumulative_refund_amount']))
            if not isinstance(result, dict) or result.get('status') not in ('ready', 'waiting', 'unsupported'):
                raise ValueError('invalid financial basis response')
            if result['status'] != 'ready':
                return dict(status=result['status'], reason=result.get('reason', 'final_baseline_not_ready'))
            revenue = -money(-Decimal(str(result['gross_amount'])))
            cost = -money(-Decimal(str(result['cost_amount'])))
            profit = -money(-Decimal(str(result['profit_amount'])))
            shares = {k: -money(-Decimal(str(result['shares'][k]))) for k in SHARES}
            if (revenue != -money(refund.amount) or revenue - cost != profit or
                    sum(shares.values()) != profit or not result.get('reference_settlement_id') or
                    not result.get('baseline_hash') or result['currency'] != segment['economic_snapshot']['currency']):
                raise ValueError('financial basis mismatch')
            values = dict(status='ready', reference_settlement_id=result['reference_settlement_id'],
                        baseline_hash=result['baseline_hash'], currency=result['currency'],
                        gross_amount=str(revenue), cost_amount=str(cost), profit_amount=str(profit),
                        shares={k: str(v) for k, v in shares.items()})
            if revised:
                values.update({k: result[k] for k in ('segment_id', 'window_hash', 'segment_context_hash',
                                                      'cumulative_refund_amount', 'original_amount')})
            return values
        except (HTTPException, ValueError, TypeError, KeyError, ArithmeticError):
            return dict(status='waiting', reason='final_baseline_evidence_unavailable')

    def assessment(db, settlement_id):
        row = db.scalar(select(FinancialAssessment).where(FinancialAssessment.settlement_id == settlement_id)
                        .order_by(FinancialAssessment.version.desc()))
        return strict_json(row.values_json) if row else dict(status='waiting', reason='final_baseline_not_assessed')

    def record_assessment(db, settlement, result, actor):
        previous = db.scalar(select(FinancialAssessment).where(FinancialAssessment.settlement_id == settlement.id)
                             .order_by(FinancialAssessment.version.desc()))
        if previous and strict_json(previous.values_json) == result:
            return
        db.add(FinancialAssessment(settlement_id=settlement.id, version=previous.version + 1 if previous else 1,
                                  values_json=canonical(result), actor=str(actor)))
        db.flush()

    def apply_basis(settlement, result):
        if result['status'] != 'ready':
            return
        settlement.reference_settlement_id = result['reference_settlement_id']
        settlement.gross_amount = Decimal(result['gross_amount'])
        settlement.net_amount = Decimal(result['gross_amount'])
        settlement.cost_amount = Decimal(result['cost_amount'])
        settlement.profit_amount = Decimal(result['profit_amount'])
        for k, v in result['shares'].items():
            setattr(settlement, k, Decimal(v))

    def prepare_lock(db, settlement, actor='system'):
        if not settlement.refund_id:
            return
        link = db.get(RefundSettlementLink, settlement.refund_id)
        if not link:
            raise HTTPException(409, 'legacy_refund_requires_final_baseline_review')
        refund = db.get(ns['Refund'], settlement.refund_id)
        create(db, refund, actor)  # Revalidate the independent completion proof.
        result = financial_basis(db, refund, strict_json(link.evidence_json)['economic_plan'])
        if result['status'] != 'ready':
            raise HTTPException(409, dict(code='refund_final_baseline_not_ready', **result))
        if result.get('kind') == 'multi_source_refund':
            if not callable(ns.get('settlement_finance', {}).get('capture_multi_refund')):
                raise HTTPException(409, 'multi_source_refund_capture_contract_required')
        before = dict(cost_amount=str(settlement.cost_amount), profit_amount=str(settlement.profit_amount),
                      reference_settlement_id=settlement.reference_settlement_id)
        previous = assessment(db, settlement.id)
        if previous.get('status') == 'ready' and previous != result:
            raise HTTPException(409, 'frozen_refund_basis_changed_requires_explicit_adjustment')
        apply_basis(settlement, result)
        Line = ns['SettlementLine']
        fields = dict(platform='platform_fee', provider='provider_share', service='service_share',
                      expert='expert_fee', channel='channel_fee')
        lines = db.scalars(select(Line).where(Line.settlement_id == settlement.id, Line.status != 'superseded')).all()
        # Legacy lines are one per category; never replicate an amount across recipients.
        if len({line.participant_type for line in lines}) != len(lines):
            raise HTTPException(409, 'multi_subject_refund_line_refresh_requires_explicit_plan')
        for line in lines:
            line.amount = getattr(settlement, fields[line.participant_type])
        for batch_id in {line.batch_id for line in lines}:
            batch = db.get(ns['SettlementBatch'], batch_id)
            batch.total_profit = Decimal(str(batch.total_profit)) + settlement.profit_amount - Decimal(before['profit_amount'])
        record_assessment(db, settlement, result, actor)
        if result.get('kind') == 'multi_source_refund':
            multi['freeze'](db, settlement, refund, result, actor)
        ns['audit'](db, actor, 'refund_final_baseline_review', 'settlement', settlement.settlement_no,
                    category='settlement', business_domain='settlement', order_id=settlement.order_id,
                    before=before, after=result)
        db.flush()

    def completed_evidence(db, refund):
        Refund, Settlement = ns['Refund'], ns['Settlement']
        # Serialize on the durable refund, including the first mapping insertion.
        refund_id = getattr(refund, 'id', None)
        refund = db.scalar(select(Refund).where(Refund.id == refund_id).with_for_update()
                           .execution_options(populate_existing=True))
        try:
            case = db.get(ns['refund_workflow']['Case'], refund.id)
            if (case is None or case.source != 'ordinary' or refund.status != 'completed'
                    or ns['refund_workflow']['controlled_source'](db, refund, case) != 'ordinary'):
                raise ValueError('ordinary completed refund required')
            hook = ns.get('version_change_settlement', {}).get('is_change_refund')
            if callable(hook) and hook(db, refund):
                raise ValueError('downgrade refund excluded')
            execution_hooks = ns['refund_execution']
            completion = db.get(execution_hooks['Completion'], refund.id)
            execution = db.get(execution_hooks['Execution'], completion.execution_id) if completion else None
            envelope = strict_json(case.plan_json)
            plan = strict_json(completion.economic_plan_json) if completion else None
            resource = strict_json(execution.resource_plan_json) if execution else None
            proof = strict_json(execution.proof_json) if execution else None
            funding = strict_json(execution.funding_json) if execution else None
            if (not completion or not execution or execution.status != 'completed'
                    or execution.refund_id != refund.id or not execution.completed_at or not completion.completed_at
                    or completed_utc(refund.completed_at) != completed_utc(completion.completed_at)
                    or completed_utc(execution.completed_at) != completed_utc(completion.completed_at)
                    or hashlib.sha256(case.plan_json.encode()).hexdigest() != case.plan_hash
                    or completion.plan_hash != case.plan_hash or execution.plan_hash != case.plan_hash
                    or envelope['refund_id'] != refund.id or envelope['order_id'] != refund.order_id
                    or envelope['payment_id'] != refund.payment_id
                    or envelope['economic_plan'] != plan or money(envelope['amount']) != money(refund.amount)
                    or money(completion.amount) != money(refund.amount)
                    or completion.proof_hash != digest(proof) or completion.funding_hash != digest(funding)
                    or execution.resource_plan_hash != digest(resource)
                    or resource['refund_id'] != refund.id or resource['order_id'] != refund.order_id
                    or resource['frozen_envelope_hash'] != digest(envelope)
                    or resource['plan_hash'] != digest({k: v for k, v in resource.items() if k != 'plan_hash'})
                    or proof['refund_id'] != refund.id or proof['resource_plan_hash'] != digest(resource)):
                raise ValueError('completion mismatch')
            Recovery = (ns['refund_file_resources']['Recovery'] if resource.get('kind') == 'file'
                        else ns['refund_execution_resources']['Recovery'])
            recovery = db.scalar(select(Recovery).where(Recovery.refund_id == refund.id))
            if (not recovery or recovery.execution_id != execution.id or recovery.order_id != refund.order_id
                    or recovery.stage != 'resources_applied' or strict_json(recovery.plan_json) != resource
                    or strict_json(recovery.proof_json) != proof
                    or recovery.payload_hash != digest(dict(plan=resource, proof=proof))):
                raise ValueError('recovery mismatch')
            if resource.get('kind') == 'file':
                order = db.get(ns['Order'], refund.order_id)
                ns['refund_file_resources']['validate_economic'](db, order, refund, envelope)
            Leg = execution_hooks['PaymentLeg']
            legs = db.scalars(select(Leg).where(Leg.refund_id == refund.id)).all()
            allocations = {r['payment_id']: r for r in funding}
            if not legs or len(legs) != len(funding) or len(allocations) != len(funding):
                raise ValueError('funding incomplete')
            for leg in legs:
                allocation = allocations[leg.payment_id]
                payment = db.get(ns['Payment'], leg.payment_id)
                if (leg.status != 'completed' or not leg.completed_at
                        or leg.receipt != 'simulated:' + refund.id + ':' + leg.payment_id
                        or leg.source_order_id != allocation['source_order_id']
                        or money(leg.amount) != money(allocation['amount'])
                        or leg.currency != allocation['currency'] or not payment
                        or payment.order_id != leg.source_order_id):
                    raise ValueError('payment receipt mismatch')
            if sum(money(leg.amount) for leg in legs) != money(refund.amount):
                raise ValueError('funding total mismatch')
            segments = plan.get('segments', [plan])
            if not segments or len({p['segment_id'] for p in segments}) != len(segments):
                raise ValueError('segments incomplete')
            values = []
            for segment in segments:
                if (segment['source_order_id'] != refund.order_id
                        or any(leg.currency != segment['economic_snapshot']['currency'] for leg in legs)):
                    raise ValueError('segment ownership mismatch')
                values.append(segment_values(segment))
            amount = sum(v[0] for v in values)
            cost = sum(v[1] for v in values)
            profit = sum(v[2] for v in values)
            shares = {key: sum(v[3][key] for v in values) for key in SHARES}
            if amount != money(refund.amount):
                raise ValueError('economic total mismatch')
            evidence = canonical(dict(completion_execution_id=execution.id, plan_hash=completion.plan_hash,
                proof_hash=completion.proof_hash, funding_hash=completion.funding_hash,
                recovery_id=recovery.id, economic_plan=plan,
                segments=[dict(segment_id=p['segment_id'], revenue=str(-v[0]), cost=str(-v[1]),
                               profit=str(v[2]), shares={k: str(n) for k, n in v[3].items()})
                          for p, v in zip(segments, values)]))
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
            raise HTTPException(409, 'ordinary_refund_settlement_evidence_invalid') from exc
        return refund, plan, evidence, (amount, cost, profit, shares)

    def create(db, refund, actor='system'):
        Settlement = ns['Settlement']
        refund, plan, evidence, (amount, cost, profit, shares) = completed_evidence(db, refund)
        proven = strict_json(evidence)
        try:
            link = db.get(RefundSettlementLink, refund.id, populate_existing=True)
            existing = db.scalars(select(Settlement).where(Settlement.refund_id == refund.id)).all()
            if link:
                if (len(existing) != 1 or existing[0].id != link.settlement_id
                        or link.evidence_json != evidence or link.evidence_hash != hashlib.sha256(evidence.encode()).hexdigest()):
                    raise ValueError('existing mapping mismatch')
                return existing[0]
            if existing:
                raise ValueError('unmapped settlement already exists')
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
            raise HTTPException(409, 'ordinary_refund_settlement_evidence_invalid') from exc
        with db.begin_nested():
            basis = financial_basis(db, refund, plan)
            settlement = Settlement(settlement_no='REF-SET-' + secrets.token_hex(6).upper(),
                order_id=refund.order_id, refund_id=refund.id, is_refund=True,
                gross_amount=-amount, refund_amount=-amount, net_amount=-amount,
                cost_amount=-cost, profit_amount=profit, refund_recovery=-amount,
                tax_amount=Decimal('0.00'), status='pending', **shares)
            apply_basis(settlement, basis)
            db.add(settlement)
            db.flush()
            record_assessment(db, settlement, basis, actor)
            db.add(RefundSettlementLink(refund_id=refund.id, settlement_id=settlement.id,
                evidence_json=evidence, evidence_hash=hashlib.sha256(evidence.encode()).hexdigest()))
            ns['audit'](db, actor, 'ordinary_refund_negative_settlement', 'settlement',
                settlement.settlement_no, 'Completed ordinary refund settlement',
                category='settlement_adjustment', business_domain='settlement',
                order_id=refund.order_id, after=dict(status=settlement.status,
                    refund_id=refund.id, execution_id=proven['completion_execution_id'], plan_hash=proven['plan_hash'],
                    evidence_hash=hashlib.sha256(evidence.encode()).hexdigest(),
                    net_amount=str(settlement.net_amount), cost_amount=str(settlement.cost_amount),
                    profit_amount=str(settlement.profit_amount), financial_basis_status=basis['status'],
                    **{key: str(getattr(settlement, key)) for key in shares}))
            db.flush()
        return settlement

    def negative_settlement(db, refund, actor='system'):
        try:
            return create(db, refund, actor)
        except IntegrityError:
            # The savepoint has rolled back only this insertion. Revalidate a
            # concurrent winner; never expose a database constraint exception.
            link = db.get(RefundSettlementLink, refund.id, populate_existing=True)
            if link is not None:
                try:
                    return create(db, refund, actor)
                except IntegrityError:
                    pass
            raise HTTPException(409, 'ordinary_refund_settlement_conflict') from None

    def describe(db, settlement_id):
        link = db.scalar(select(RefundSettlementLink).where(
            RefundSettlementLink.settlement_id == settlement_id))
        if link is None:
            settlement = db.get(ns['Settlement'], settlement_id)
            if settlement and settlement.is_refund:
                return dict(refund_id=settlement.refund_id,
                            financial_basis=dict(status='unsupported', reason='legacy_refund_requires_final_baseline_review'))
            return None
        refund = db.get(ns['Refund'], link.refund_id)
        completion = db.get(ns['refund_execution']['Completion'], link.refund_id)
        detail = strict_json(link.evidence_json)
        segments = detail['economic_plan'].get('segments', [detail['economic_plan']])
        basis = assessment(db, settlement_id)
        order = db.get(ns['Order'], refund.order_id)
        baseline_model = ns.get('settlement_finance', {}).get('Baseline')
        display_refs = []
        for stored_ref in basis.get('references', []):
            ref = dict(stored_ref)
            try:
                source = db.get(baseline_model, ref['reference_settlement_id']) if baseline_model is not None else None
                original = db.get(ns['Settlement'], ref['reference_settlement_id'])
                if (not order or not source or not original or ref.get('source_order_id') != order.id
                        or source.order_id != order.id or original.order_id != order.id
                        or source.provider_enterprise_id != order.provider_enterprise_id
                        or source.snapshot_hash != ref.get('baseline_hash')
                        or hashlib.sha256(source.snapshot_json.encode()).hexdigest() != source.snapshot_hash
                        or source.currency != ref.get('currency') or source.revenue <= 0
                        or original.status not in ('locked', 'paid')):
                    raise ValueError('source identity mismatch')
                snapshot = strict_json(source.snapshot_json)
                source_shares = {k: money(snapshot['shares'][k]) for k in SHARES}
                if sum(source_shares.values()) != source.profit or source.profit < 0:
                    raise ValueError('source share mismatch')
                rates = {role: str((source_shares[field] * 100 / source.profit).quantize(
                    Decimal('.0001'), rounding=ROUND_HALF_UP)) if source.profit else None
                    for role, field in zip(('platform', 'provider', 'service', 'expert', 'channel'), SHARES)}
                Batch, Line = ns['SettlementBatch'], ns['SettlementLine']
                batches = db.scalars(select(Batch).join(Line, Line.batch_id == Batch.id).where(
                    Line.settlement_id == source.settlement_id, Line.status != 'superseded',
                    Batch.status != 'rolled_back').distinct()).all()
                ref.update(source_order_no=order.order_no, reference_settlement_no=original.settlement_no,
                    reference_batch_no=batches[0].batch_no if len(batches) == 1 else None,
                    share_rates=rates, baseline_created_at=source.created_at, display_status='verified')
            except (HTTPException, ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
                ref.update(display_status='unavailable', display_reason='source_identity_or_baseline_unverified')
            display_refs.append(ref)
        if 'references' in basis:
            basis = dict(basis, references=display_refs)
        return dict(refund_id=link.refund_id, refund_no=refund.refund_no,
                    execution_id=completion.execution_id, plan_hash=completion.plan_hash,
                    source_order_ids=sorted({p['source_order_id'] for p in segments}),
                    rule_version=','.join(sorted({p['economic_snapshot']['settlement_rule']['version'] for p in segments})),
                    completed_at=completion.completed_at, financial_basis=basis)

    def ledger_status(db, refund_id):
        link = db.get(RefundSettlementLink, refund_id)
        if link is None:
            return 'pending_monthly'
        settlement = db.get(ns['Settlement'], link.settlement_id)
        finance = ns.get('settlement_finance', {}).get('describe')
        financial = finance(db, settlement.id) if settlement and callable(finance) else None
        if financial:
            return 'funds_settled' if financial['funds_settled'] else 'recovery_pending'
        Line, Batch = ns['SettlementLine'], ns['SettlementBatch']
        batches = db.scalars(select(Batch).join(Line, Line.batch_id == Batch.id).where(
            Line.settlement_id == link.settlement_id, Line.status != 'superseded')).all()
        if any(b.status in ('generated', 'pending_confirm', 'confirmed', 'payment_processing',
                            'partial_paid', 'paid', 'archived') for b in batches):
            return 'in_batch'
        return 'settlement_generated'

    multi = install_multi_source(ns, completed_evidence, money, segment_values, SHARES)
    hooks = dict(Model=RefundSettlementLink, FinancialAssessment=FinancialAssessment,
                 negative_settlement=negative_settlement, describe=describe, ledger_status=ledger_status,
                 financial_basis=financial_basis, assessment=assessment, prepare_lock=prepare_lock,
                 MultiSourceManifest=multi['Model'], capture_manifest=multi['capture_manifest'])
    ns['refund_settlement'] = hooks
    return hooks
