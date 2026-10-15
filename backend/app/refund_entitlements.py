"""Pure ordinary-refund planning; callers must lock and persist the source segment."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR


def _money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result != result.quantize(Decimal('0.01')):
            raise ValueError('Amounts must be nonnegative cents')
        return result
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('Invalid amount') from exc


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f'Invalid {name}')
    return value


def _micros(delta):
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Timezone-aware timestamps required')
    return value.astimezone(timezone.utc)


def plan_refund(*, method, original_amount, requested_amount, refunded_amount='0',
                monthly_quota=0, months=1, used_calls=0, starts_at=None, ends_at=None,
                at=None):
    """Return cumulative and incremental recovery, without mutating funds or resources.

    Inputs describe one immutable economic segment, not the entire shared credential.
    refunded_amount must include prior completed/reserved refunds while holding a lock.
    For time plans, ends_at is the original end, never an already shortened end.
    """
    if method not in {'api', 'model_api', 'tenant_access'}:
        raise ValueError('Unsupported delivery method')
    price, request, prior = map(_money, (original_amount, requested_amount, refunded_amount))
    quota = _integer(monthly_quota, 'monthly quota')
    count = _integer(months, 'months')
    used = _integer(used_calls, 'used calls')
    if price <= 0 or request <= 0 or not 1 <= count <= 36 or prior > price:
        raise ValueError('Invalid original price, refund or duration')
    finite = method in {'api', 'model_api'} and quota > 0
    total = quota * count if finite else None
    if finite:
        if used > total:
            raise ValueError('Usage exceeds purchased allocation')
        unconsumed = Decimal(total - used) / Decimal(total)
    else:
        start, end, current = map(_utc, (starts_at, ends_at, at))
        duration = _micros(end - start)
        if duration <= 0:
            raise ValueError('Invalid original interval')
        remaining = _micros(end - min(max(current, start), end))
        unconsumed = Decimal(remaining) / Decimal(duration)
    refundable = (price * unconsumed).quantize(Decimal('0.01'), rounding=ROUND_FLOOR)
    if prior + request > refundable:
        raise ValueError('Refund exceeds unconsumed balance')
    cumulative = prior + request
    base = total if finite else duration
    previous_recovery = int((Decimal(base) * prior / price).to_integral_value(rounding=ROUND_FLOOR))
    recovery = int((Decimal(base) * cumulative / price).to_integral_value(rounding=ROUND_FLOOR))
    # A complete refund of the cent-rounded balance must not leave rounding residue.
    if cumulative == refundable:
        recovery = total - used if finite else remaining
    result = {'basis': 'calls' if finite else 'time', 'refund_amount': str(request),
              'cumulative_refund_amount': str(cumulative),
              'refundable_balance_before': str(refundable - prior),
              'recovery_delta': recovery - previous_recovery,
              'cumulative_recovery': recovery}
    if finite:
        result.update(original_quota=total, remaining_total_quota=total - recovery,
                      remaining_unused_calls=total - recovery - used)
    else:
        result.update(original_duration_microseconds=duration,
                      effective_ends_at=end - timedelta(microseconds=recovery))
    return result
