"""Bounded ordinary-refund planning. No funds or entitlement execution.

Only an unrevised original subscription segment is auto-planned. A missing or
unallocated charge ledger is unavailable, never zero. Manual determinations are
audited human decisions, not a substitute automatic usage ledger.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
import hashlib
import json
import secrets

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text, select
from sqlalchemy.orm import mapped_column

from .refund_schema import PreparedRefundPlan, RefundRequestBody


VERSION = 'ordinary-refund-plan-v1'
POLICY = 'gateway_actual_charge_including_upstream_failure'


class PlanUnavailable(ValueError):
    pass


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PlanUnavailable('aware_segment_timestamps_required')
    return value.astimezone(timezone.utc)


def cents(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result != result.quantize(Decimal('0.01')):
            raise ValueError
        return result
    except (ValueError, ArithmeticError) as exc:
        raise PlanUnavailable('invalid_money') from exc


def count(value):
    if type(value) is not int or value < 0:
        raise PlanUnavailable('invalid_quantity')
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class ChargeSummary:
    order_id: str
    segment_id: str
    start: datetime
    end: datetime
    as_of: datetime
    charged_calls: int | None
    complete: bool
    policy_version: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class OriginalSegment:
    segment_id: str
    order_id: str
    original_amount: Decimal
    method: str
    monthly_quota: int
    months: int
    starts_at: datetime
    ends_at: datetime
    economic_snapshot: dict
    delivery_snapshot: dict


def plan_segment(segment, amount, *, at, used_calls=None, prior_amount='0', prior_recovery=0, other_pending='0'):
    """Cumulative recovery uses the original denominator, never a shortened tail.

    prior_recovery is persisted recovery already reserved by approved plans, not
    recomputed from a previous amount (which would lose final-cent residue).
    """
    price, request, prior, pending = map(cents, (segment.original_amount, amount, prior_amount, other_pending))
    start, end, at = map(utc, (segment.starts_at, segment.ends_at, at))
    quota, months, recovered = map(count, (segment.monthly_quota, segment.months, prior_recovery))
    if price <= 0 or request <= 0 or not 1 <= months <= 36 or end <= start or prior > price:
        raise PlanUnavailable('invalid_original_segment')
    if segment.method not in {'api', 'model_api', 'tenant_access'}:
        raise PlanUnavailable('manual_assessment_required')
    if at < start:
        raise PlanUnavailable('future_renewal_segment_not_supported')
    finite = segment.method in {'api', 'model_api'} and quota > 0
    if finite:
        if used_calls is None:
            raise PlanUnavailable('charge_ledger_unavailable')
        base, used = quota * months, count(used_calls)
        if used > base:
            raise PlanUnavailable('usage_exceeds_original_allocation')
        remaining = base - used
        basis = 'calls'
    else:
        duration = end - start
        base = (duration.days * 86400 + duration.seconds) * 1000000 + duration.microseconds
        tail = end - min(max(at, start), end)
        remaining = (tail.days * 86400 + tail.seconds) * 1000000 + tail.microseconds
        basis = 'time'
    limit = (price * Decimal(remaining) / Decimal(base)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if prior + request + pending > limit:
        raise PlanUnavailable('refund_exceeds_unconsumed_balance_with_reservations')
    prior_expected = int((Decimal(base) * prior / price).to_integral_value(rounding=ROUND_FLOOR))
    if recovered != prior_expected:
        raise PlanUnavailable('prior_recovery_reconciliation_required')
    cumulative = prior + request
    recovery = int((Decimal(base) * cumulative / price).to_integral_value(rounding=ROUND_FLOOR))
    if cumulative == limit:
        recovery = remaining
    delta = recovery - recovered
    if recovered > remaining or delta < 0 or recovery > remaining:
        raise PlanUnavailable('prior_recovery_reconciliation_required')
    result = {'schema_version': 1, 'calculation_version': VERSION, 'source_order_id': segment.order_id,
        'segment_id': segment.segment_id, 'basis': basis, 'original_amount': format(price, '.2f'),
        'refund_amount': format(request, '.2f'), 'cumulative_refund_amount': format(cumulative, '.2f'),
        'refundable_balance_before': format(limit - prior - pending, '.2f'),
        'other_pending_amount': format(pending, '.2f'), 'prior_recovery': recovered,
        'cumulative_recovery': recovery, 'recovery_delta': delta,
        'recovery_unit': 'calls' if finite else 'microseconds', 'as_of': at.isoformat(),
        'starts_at': start.isoformat(), 'original_ends_at': end.isoformat(),
        'economic_snapshot': json.loads(canonical(segment.economic_snapshot)),
        'delivery_snapshot': json.loads(canonical(segment.delivery_snapshot))}
    if finite:
        result.update(original_quota=base, charged_calls=used, remaining_total_quota=base - recovery)
    else:
        result.update(original_duration_microseconds=base,
                      effective_ends_at=(end - timedelta(microseconds=recovery)).isoformat())
    return result


class QuoteBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)


class ManualBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=10000, strict=True)
    idempotency_key: str = Field(min_length=8, max_length=100, strict=True)
    expected_version: int = Field(ge=1, strict=True)
    refundable_limit: Decimal = Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)
    used_calls: int = Field(ge=0, le=9223372036854775807, strict=True)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)

    @field_validator('reason')
    @classmethod
    def reason_not_blank(cls, value):
        return RefundRequestBody.reason_not_blank(value)


def install(ns):
    workflow = ns['refund_workflow']
    Case, Refund, Order = workflow['Case'], ns['Refund'], ns['Order']
    Term, Revision = (ns['subscription_terms'][key] for key in ('Term', 'Revision'))

    class ManualAssessment(ns['Base']):
        __tablename__ = 'refund_manual_assessments'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        refund_id = mapped_column(ForeignKey('refunds.id'), nullable=False, index=True)
        case_version = mapped_column(Integer, nullable=False)
        amount = mapped_column(Numeric(14, 2), nullable=False)
        refundable_limit = mapped_column(Numeric(14, 2), nullable=False)
        used_calls = mapped_column(BigInteger, nullable=False)
        reason = mapped_column(Text, nullable=False)
        evidence_refs_json = mapped_column(Text, nullable=False)
        assessed_by = mapped_column(String(36), nullable=False)
        assessed_at = mapped_column(DateTime(timezone=True), nullable=False, default=ns['now'])

    def latest_manual(db, refund, case):
        return db.scalar(select(ManualAssessment).where(ManualAssessment.refund_id == refund.id,
            ManualAssessment.case_version == case.version).order_by(ManualAssessment.assessed_at.desc(),
                ManualAssessment.id.desc()).limit(1)) if case else None

    def original(db, order):
        if order.snapshot_version != 1:
            raise PlanUnavailable('manual_assessment_required')
        if order.delivery_method_snapshot not in {'api', 'model_api', 'tenant_access'}:
            raise PlanUnavailable('manual_assessment_required')
        economic = ns['order_economics']['economic_snapshot'](order)
        delivery = ns['order_economics']['delivery_snapshot'](order)
        terms = list(db.scalars(select(Term).where(Term.order_id == order.id)))
        if len(terms) != 1 or terms[0].status != 'active':
            raise PlanUnavailable('original_subscription_segment_unavailable')
        term = terms[0]
        Change = ns['version_change_saga']['VersionChangeRequest']
        if db.scalar(select(Change.id).where(Change.combination_id == term.combination_id,
                Change.status.in_(ns['version_change_saga']['OPEN_STATUSES'])).limit(1)):
            raise PlanUnavailable('version_change_in_progress')
        if db.scalar(select(Revision.id).where(Revision.term_id == term.id).limit(1)):
            raise PlanUnavailable('revised_economic_segments_require_reconciliation')
        if (economic.get('billing_unit') != 'month' or term.version_id != order.product_version_id
                or term.version_code != order.product_version_code or term.months != order.subscription_months):
            raise PlanUnavailable('original_segment_snapshot_mismatch')
        quota = delivery.get('quota', {}).get('monthly_quota')
        count(quota)
        if order.delivery_method_snapshot in {'api', 'model_api'} and quota > 0 and not callable(ns.get('refund_charge_summary')):
            raise PlanUnavailable('api_charge_ledger_unavailable')
        return OriginalSegment(term.id, order.id, cents(economic['amount']), order.delivery_method_snapshot,
                               quota, term.months, term.starts_at, term.ends_at, economic, delivery)

    def reservations(db, order, refund_id=None, segment=None):
        prior, pending, recovery, evidence = Decimal('0'), Decimal('0'), 0, []
        rows = list(db.scalars(select(Refund).where(Refund.order_id == order.id, Refund.id != (refund_id or ''))))
        completed = sum((cents(row.amount) for row in rows if row.status == 'completed'), Decimal('0'))
        if cents(order.refunded_amount) > completed:
            raise PlanUnavailable('historical_refund_allocation_missing')
        for row in rows:
            if row.status == 'requested':
                pending += cents(row.amount)
            elif row.status in {'pending', 'approved', 'executing', 'failed', 'completed'}:
                case = db.get(Case, row.id)
                if case is None or not case.plan_json or not case.plan_hash:
                    raise PlanUnavailable('prior_refund_plan_missing')
                if hashlib.sha256(case.plan_json.encode()).hexdigest() != case.plan_hash:
                    raise PlanUnavailable('prior_refund_plan_invalid')
                envelope = json.loads(case.plan_json)
                if not isinstance(envelope, dict) or not isinstance(envelope.get('economic_plan'), dict):
                    raise PlanUnavailable('prior_refund_plan_invalid')
                plan = envelope['economic_plan']
                if (plan.get('basis') not in {'time', 'calls'} or plan.get('segment_id') != segment.segment_id
                        or envelope.get('refund_id') != row.id or envelope.get('order_id') != order.id
                        or envelope.get('payment_id') != row.payment_id
                        or cents(envelope.get('amount')) != cents(row.amount)
                        or cents(plan.get('refund_amount')) != cents(row.amount)
                        or plan.get('economic_snapshot') != segment.economic_snapshot
                        or plan.get('delivery_snapshot') != segment.delivery_snapshot
                        or plan.get('starts_at') != utc(segment.starts_at).isoformat()
                        or plan.get('original_ends_at') != utc(segment.ends_at).isoformat()):
                    raise PlanUnavailable('prior_refund_recovery_unavailable')
                prior += cents(row.amount)
                recovery += count(plan['recovery_delta'])
                evidence.append(row.id)
        return prior, pending, recovery, evidence

    def automatic(db, order, amount, refund_id=None):
        segment = original(db, order)
        at = ns['now']()
        if utc(at) < utc(segment.starts_at):
            raise PlanUnavailable('future_renewal_segment_not_supported')
        prior, pending, recovered, prior_ids = reservations(db, order, refund_id, segment)
        used, evidence_ids, charge_evidence = None, [segment.segment_id, *prior_ids], None
        if segment.method in {'api', 'model_api'} and segment.monthly_quota > 0:
            adapter = ns.get('refund_charge_summary')
            if not callable(adapter):
                raise PlanUnavailable('charge_ledger_unavailable')
            end = min(at, segment.ends_at)
            if end <= segment.starts_at:
                raise PlanUnavailable('charge_window_not_started')
            try:
                summary = adapter(db, order_id=order.id, segment_id=segment.segment_id,
                    start=segment.starts_at, end=end, as_of=at)
            except Exception as exc:
                raise PlanUnavailable('charge_ledger_adapter_unavailable') from exc
            if (not isinstance(summary, ChargeSummary) or summary.complete is not True
                    or summary.order_id != order.id or summary.segment_id != segment.segment_id
                    or utc(summary.start) != utc(segment.starts_at) or utc(summary.end) != utc(end)
                    or utc(summary.as_of) != utc(at) or not summary.policy_version
                    or not summary.evidence_ids or summary.charged_calls is None):
                raise PlanUnavailable('charge_ledger_incomplete_or_unallocated')
            used = count(summary.charged_calls)
            if any(not isinstance(value, str) or not value for value in summary.evidence_ids):
                raise PlanUnavailable('charge_evidence_missing')
            evidence_ids.extend(summary.evidence_ids)
            charge_evidence = {'order_id': summary.order_id, 'segment_id': summary.segment_id,
                'start': utc(summary.start).isoformat(), 'end': utc(summary.end).isoformat(),
                'as_of': utc(summary.as_of).isoformat(), 'charged_calls': used, 'complete': True,
                'policy_version': summary.policy_version, 'evidence_ids': list(summary.evidence_ids)}
        result = plan_segment(segment, amount, at=at, used_calls=used,
            prior_amount=prior, prior_recovery=recovered, other_pending=pending)
        result['charge_policy'] = POLICY if result['basis'] == 'calls' else None
        result['charge_ledger'] = charge_evidence
        return result, tuple(evidence_ids)

    def public(plan):
        keys = ('basis', 'refund_amount', 'refundable_balance_before', 'cumulative_refund_amount',
            'cumulative_recovery', 'recovery_delta', 'recovery_unit', 'original_quota', 'charged_calls',
            'remaining_total_quota', 'effective_ends_at', 'as_of', 'charge_policy')
        return {key: plan[key] for key in keys if key in plan}

    def prepare(db, order, refund):
        case = db.get(Case, refund.id)
        manual = latest_manual(db, refund, case)
        if manual is not None:
            if cents(manual.amount) != cents(refund.amount):
                raise HTTPException(409, 'Manual assessment no longer matches the requested amount')
            check_manual_limit(db, order, refund, cents(manual.refundable_limit))
            # An explicit human monetary determination does not invent an
            # automatic entitlement recovery plan or historical economic costs.
            plan = {'schema_version': 1, 'basis': 'manual', 'refund_amount': str(manual.amount),
                'refundable_limit': str(manual.refundable_limit), 'used_calls': manual.used_calls,
                'manual_assessment_id': manual.id, 'assessed_by': manual.assessed_by,
                'assessed_at': manual.assessed_at.isoformat(), 'reason': manual.reason,
                'evidence_refs': json.loads(manual.evidence_refs_json),
                'source_trust': 'manual_determination', 'automatic_usage_trusted': False,
                'resource_recovery_status': 'unavailable', 'cost_basis_status': 'unavailable'}
            return PreparedRefundPlan(refund.id, order.id, refund.payment_id, cents(refund.amount), VERSION,
                ns['now'](), (manual.id,), plan, 'manual')
        try:
            plan, evidence_ids = automatic(db, order, refund.amount, refund.id)
        except (PlanUnavailable, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(409, 'Refund planning unavailable: ' + str(exc)) from exc
        return PreparedRefundPlan(refund.id, order.id, refund.payment_id, cents(refund.amount), VERSION,
            datetime.fromisoformat(plan['as_of']), evidence_ids, plan, plan['basis'])

    def readiness(db, order, refund):
        case = db.get(Case, refund.id)
        if latest_manual(db, refund, case):
            return {'status': 'manual_ready', 'reason': None, 'source_trust': 'manual_determination',
                    'automatic_quote_available': False, 'manual_assessment_allowed': True}
        try:
            plan, _ = automatic(db, order, refund.amount, refund.id)
            return {'status': 'available', 'reason': None, 'automatic_quote_available': True,
                    'manual_assessment_allowed': True, 'preview': public(plan), 'source_trust': 'automatic'}
        except (PlanUnavailable, HTTPException, KeyError, TypeError, ValueError) as exc:
            return {'status': 'unavailable', 'reason': str(exc) if isinstance(exc, PlanUnavailable)
                    else 'source_reconciliation_required', 'automatic_quote_available': False,
                    'manual_assessment_allowed': True, 'preview': None}

    def check_manual_limit(db, order, refund, limit):
        rows = list(db.scalars(select(Refund).where(Refund.order_id == order.id, Refund.id != refund.id,
            Refund.status.in_(('pending', 'requested', 'approved', 'executing', 'failed', 'completed')))))
        completed = sum((cents(row.amount) for row in rows if row.status == 'completed'), Decimal('0'))
        held = sum((cents(row.amount) for row in rows if row.status != 'completed'), Decimal('0'))
        if max(cents(order.refunded_amount), completed) + held + cents(refund.amount) > limit:
            raise HTTPException(409, 'Manual limit cannot cover prior/in-flight refund allocations')

    @ns['app'].post('/api/orders/{order_id}/refund-quote')
    def quote(order_id: str, body: QuoteBody, request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        order = db.get(Order, order_id)
        if order is None:
            raise HTTPException(404, 'Order not found')
        workflow['scope'](db, user, request, order)
        try:
            plan, _ = automatic(db, order, body.amount)
            return {'status': 'available', 'source_trust': 'automatic', 'item': public(plan),
                    'binding': False, 'execution_available': False}
        except (PlanUnavailable, HTTPException, KeyError, TypeError, ValueError) as exc:
            return {'status': 'unavailable', 'reason': str(exc) if isinstance(exc, PlanUnavailable)
                    else 'source_reconciliation_required', 'item': None, 'execution_available': False}

    @ns['app'].post('/api/refunds/{refund_id}/manual-assessments')
    def manual(refund_id: str, body: ManualBody, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        with workflow['transaction'](db, user, refund_id, 'manual_assessment'):
            workflow['platform'](user)
            peek = db.get(Refund, refund_id)
            if peek is None:
                raise HTTPException(404, 'Refund not found')
            order = workflow['lock_order'](db, peek.order_id)
            refund = db.scalar(select(Refund).where(Refund.id == refund_id).with_for_update()
                               .execution_options(populate_existing=True))
            case = db.get(Case, refund_id, populate_existing=True)
            if case is None or workflow['controlled_source'](db, refund, case) != 'ordinary':
                raise HTTPException(409, 'Only an ordinary request can receive manual assessment')
            prior, key, payload_hash = workflow['replay'](db, user, body, 'manual:' + refund_id)
            if prior:
                return workflow['output'](db, user, refund)
            if refund.status != 'requested' or case.version != body.expected_version:
                raise HTTPException(409, 'Refund changed; refresh before assessment')
            if cents(body.amount) != cents(refund.amount) or body.refundable_limit > order.paid_amount:
                raise HTTPException(409, 'Manual amount/limit does not match the paid refund request')
            if any(not isinstance(value, str) or not value.strip() or len(value) > 500 for value in body.evidence_refs):
                raise HTTPException(422, 'Nonempty documentary basis references required')
            payment = workflow['payment_for'](db, order, refund.payment_id)
            workflow['cap'](db, order, payment, cents(body.amount), exclude=refund.id)
            check_manual_limit(db, order, refund, cents(body.refundable_limit))
            before = {'status': refund.status, 'version': case.version}
            case.version += 1
            row = ManualAssessment(refund_id=refund.id, case_version=case.version, amount=body.amount,
                refundable_limit=body.refundable_limit, used_calls=body.used_calls, reason=body.reason,
                evidence_refs_json=canonical(body.evidence_refs), assessed_by=user.id, assessed_at=ns['now']())
            db.add(row)
            db.flush()
            workflow['record'](db, user, order, refund, case, body, 'manual_assessment', before, key, payload_hash)
            ns['audit'](db, user.email or user.id, 'manual_refund_determination', 'refund', refund.id,
                'Explicit manual monetary/usage determination, not automatic ledger evidence', category='refund',
                business_domain='refund', order_id=order.id, after={'assessment_id': row.id,
                    'amount': str(body.amount), 'refundable_limit': str(body.refundable_limit),
                    'used_calls': body.used_calls, 'reason': body.reason, 'evidence_refs': body.evidence_refs,
                    'source_trust': 'manual_determination'})
            db.commit()
            return workflow['output'](db, user, refund)

    hooks = dict(ManualAssessment=ManualAssessment, ChargeSummary=ChargeSummary,
                 automatic=automatic, prepare=prepare, readiness=readiness)
    ns['refund_planning'] = hooks
    ns['prepare_refund_plan'] = prepare
    ns['refund_planning_status'] = readiness
    return hooks
