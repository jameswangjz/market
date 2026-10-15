"""Pure funding allocation over trusted, server-scoped payment facts.

Callers must establish enterprise ownership, source completeness and economic
refund eligibility before calling. These records are not HTTP input contracts;
allocation neither proves usage nor changes economic segments, costs or splits.
No payment execution or reservation persistence happens here.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable


class FundingUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class FundingSource:
    payment_id: str
    source_order_id: str
    paid_at: datetime
    paid_amount: Decimal
    refunded: Decimal
    reserved: Decimal
    currency: str


@dataclass(frozen=True)
class FundingAllocation:
    payment_id: str
    source_order_id: str
    amount: Decimal
    currency: str


def _cents(value):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise FundingUnavailable('invalid_money')
    numerator, denominator = value.as_integer_ratio()
    result, residue = divmod(numerator * 100, denominator)
    if residue:
        raise FundingUnavailable('invalid_money')
    return result


def _money(value):
    return Decimal((0, tuple(int(digit) for digit in str(value)), -2))


def _currency(value):
    if not isinstance(value, str) or len(value) != 3 or not all('A' <= ch <= 'Z' for ch in value):
        raise FundingUnavailable('invalid_currency')
    return value


def allocate_refund(amount: Decimal, sources: Iterable[FundingSource], *, currency: str) -> tuple[FundingAllocation, ...]:
    """Newest payment first, ascending payment_id for equal instants.

    Validate every source, including unused ones. Unknown money or absent
    sources never become a zero balance. Integer cents avoid Decimal context
    rounding, and insufficient funds reject the entire request.
    """
    requested = _cents(amount)
    if requested == 0:
        raise FundingUnavailable('positive_refund_required')
    currency = _currency(currency)
    if sources is None:
        raise FundingUnavailable('funding_sources_missing')
    rows, seen = [], set()
    for source in sources:
        if not isinstance(source, FundingSource):
            raise FundingUnavailable('invalid_funding_source')
        if any(not isinstance(value, str) or not value.strip()
               for value in (source.payment_id, source.source_order_id)):
            raise FundingUnavailable('funding_identity_missing')
        if source.payment_id in seen:
            raise FundingUnavailable('duplicate_payment_id')
        seen.add(source.payment_id)
        if _currency(source.currency) != currency:
            raise FundingUnavailable('funding_currency_mismatch')
        if (not isinstance(source.paid_at, datetime) or source.paid_at.tzinfo is None
                or source.paid_at.utcoffset() is None):
            raise FundingUnavailable('aware_paid_at_required')
        paid, refunded, reserved = map(_cents, (source.paid_amount, source.refunded, source.reserved))
        if refunded + reserved > paid:
            raise FundingUnavailable('funding_balance_inconsistent')
        rows.append((source, source.paid_at.astimezone(timezone.utc), paid - refunded - reserved))
    if not rows:
        raise FundingUnavailable('funding_sources_missing')
    if sum(row[2] for row in rows) < requested:
        raise FundingUnavailable('insufficient_refundable_funding')
    rows.sort(key=lambda row: row[0].payment_id)
    rows.sort(key=lambda row: row[1], reverse=True)
    allocations = []
    remaining = requested
    for source, _, available in rows:
        allocated = min(available, remaining)
        if allocated:
            allocations.append(FundingAllocation(source.payment_id, source.source_order_id,
                                                 _money(allocated), currency))
            remaining -= allocated
        if remaining == 0:
            break
    return tuple(allocations)
