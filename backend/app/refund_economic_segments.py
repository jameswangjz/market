"""Pure economic-segment quotes; no ledger, transaction or resource execution.

The caller freezes these inputs at the actual resource switch. used_calls must
be complete usage attributed only to this segment, starting at starts_at.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import math
from types import MappingProxyType
from collections.abc import Mapping

from .refund_planning import PlanUnavailable, count, utc


def _integer_cents(value):
    numerator, denominator = value.as_integer_ratio()
    quotient, remainder = divmod(numerator * 100, denominator)
    if remainder:
        raise PlanUnavailable('invalid_money')
    return quotient


def _money(value):
    """Validate exact cents in Numeric(14, 2), independent of Decimal context."""
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal('999999999999.99'):
            raise ValueError
        _integer_cents(result)
        return result
    except (ValueError, ArithmeticError) as exc:
        raise PlanUnavailable('invalid_money') from exc


def _identity(value):
    if type(value) is not str or not value.strip():
        raise PlanUnavailable('identity_required')
    return value


def _freeze(value):
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise PlanUnavailable('invalid_snapshot')
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) in (list, tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise PlanUnavailable('invalid_snapshot')


def _detach(value):
    if isinstance(value, Mapping):
        return {key: _detach(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_detach(item) for item in value]
    return value


@dataclass(frozen=True)
class EconomicSegment:
    segment_id: str
    source_order_id: str
    change_id: str
    revenue: Decimal
    cost: Decimal
    method: str
    available_quota: int | None
    starts_at: datetime
    ends_at: datetime
    economic_snapshot: dict
    delivery_snapshot: dict
    evidence_ids: tuple[str, ...]

    def __post_init__(self):
        for value in (self.segment_id, self.source_order_id, self.change_id):
            _identity(value)
        if type(self.method) is not str or self.method not in {'api', 'model_api', 'tenant_access'}:
            raise PlanUnavailable('manual_assessment_required')
        revenue, cost = _money(self.revenue), _money(self.cost)
        start, end = utc(self.starts_at), utc(self.ends_at)
        if revenue <= 0 or end <= start:
            raise PlanUnavailable('invalid_economic_segment')
        if self.available_quota is not None:
            count(self.available_quota)
        if type(self.evidence_ids) not in (tuple, list) or not self.evidence_ids:
            raise PlanUnavailable('evidence_required')
        evidence = tuple(_identity(value) for value in self.evidence_ids)
        for name in ('economic_snapshot', 'delivery_snapshot'):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise PlanUnavailable('invalid_snapshot')
            try:
                object.__setattr__(self, name, _freeze(value))
            except RecursionError as exc:
                raise PlanUnavailable('invalid_snapshot') from exc
        for name, value in (('revenue', revenue), ('cost', cost), ('starts_at', start),
                            ('ends_at', end), ('evidence_ids', evidence)):
            object.__setattr__(self, name, value)


def _micros(duration):
    return (duration.days * 86400 + duration.seconds) * 1000000 + duration.microseconds


def plan_economic_segment(segment, amount, *, at, used_calls=None,
                          prior_amount='0', prior_recovery=0, other_pending='0'):
    """Plan using frozen remaining_quota_at_open, never monthly quota * months.

    Prior recovery reconciliation deliberately matches plan_segment, including
    rejection when an earlier final-cent adjustment differs from plain FLOOR.
    Pending holds reduce money availability but do not reserve extra recovery.
    """
    if type(segment) is not EconomicSegment:
        raise PlanUnavailable('economic_segment_required')
    request, prior, pending = map(_money, (amount, prior_amount, other_pending))
    at, recovered = utc(at), count(prior_recovery)
    price, cost = segment.revenue, segment.cost
    if request <= 0 or prior > price:
        raise PlanUnavailable('invalid_economic_segment')
    finite = segment.method in {'api', 'model_api'} and segment.available_quota is not None
    if used_calls is not None:
        count(used_calls)
        if at < segment.starts_at and used_calls:
            raise PlanUnavailable('charge_ledger_incomplete_or_unallocated')
    if finite:
        if used_calls is None:
            raise PlanUnavailable('charge_ledger_unavailable')
        base = segment.available_quota
        if used_calls > base:
            raise PlanUnavailable('usage_exceeds_original_allocation')
        if base == 0:
            raise PlanUnavailable('no_remaining_entitlement')
        remaining = base - used_calls
    else:
        base = _micros(segment.ends_at - segment.starts_at)
        remaining = _micros(segment.ends_at - min(max(at, segment.starts_at), segment.ends_at))
    # Integer ratios avoid Decimal context drift at cent and FLOOR boundaries.
    price_cents, prior_cents, request_cents, cost_cents, pending_cents = map(
        _integer_cents, (price, prior, request, cost, pending))
    def half_up(numerator, denominator):
        return (2 * numerator + denominator) // (2 * denominator)
    limit_cents = half_up(price_cents * remaining, base)
    cumulative_cents = prior_cents + request_cents
    if cumulative_cents + pending_cents > limit_cents:
        raise PlanUnavailable('refund_exceeds_unconsumed_balance_with_reservations')
    if recovered != base * prior_cents // price_cents:
        raise PlanUnavailable('prior_recovery_reconciliation_required')
    recovery = base * cumulative_cents // price_cents
    if cumulative_cents == limit_cents:
        recovery = remaining
    if recovered > remaining or recovery < recovered or recovery > remaining:
        raise PlanUnavailable('prior_recovery_reconciliation_required')
    def money(value):
        return f'{value // 100}.{value % 100:02d}'
    prior_cost = half_up(cost_cents * prior_cents, price_cents)
    cumulative_cost = half_up(cost_cents * cumulative_cents, price_cents)
    result = dict(schema_version=1, calculation_version='economic-segment-refund-v1',
        segment_id=segment.segment_id, source_order_id=segment.source_order_id,
        change_id=segment.change_id, evidence_ids=list(segment.evidence_ids),
        method=segment.method, basis='calls' if finite else 'time',
        original_amount=money(price_cents), revenue=money(price_cents), cost=money(cost_cents),
        refund_amount=money(request_cents), revenue_reversal_delta=money(request_cents),
        cumulative_refund_amount=money(cumulative_cents),
        cumulative_revenue_reversal=money(cumulative_cents),
        prior_cost_reversal=money(prior_cost), cumulative_cost_reversal=money(cumulative_cost),
        cost_reversal_delta=money(cumulative_cost - prior_cost),
        refundable_balance_before=money(limit_cents - prior_cents - pending_cents),
        other_pending_amount=money(pending_cents), prior_recovery=recovered,
        cumulative_recovery=recovery, recovery_delta=recovery - recovered,
        recovery_unit='calls' if finite else 'microseconds', available_quota=segment.available_quota,
        remaining_quota_at_open=segment.available_quota,
        starts_at=segment.starts_at.isoformat(), ends_at=segment.ends_at.isoformat(),
        original_ends_at=segment.ends_at.isoformat(), as_of=at.isoformat(),
        economic_snapshot=_detach(segment.economic_snapshot),
        delivery_snapshot=_detach(segment.delivery_snapshot))
    if finite:
        result.update(original_quota=base, charged_calls=used_calls, remaining_total_quota=base - recovery)
    else:
        result.update(original_duration_microseconds=base,
                      effective_ends_at=(segment.ends_at - timedelta(microseconds=recovery)).isoformat())
    return result
