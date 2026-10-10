"""Pure subscription calendar rules; no persistence or entitlement decisions.

Callers freeze the delivery-success origin once and retain it on renewal.
An expired-service restart needs a separate anchor/generation. Period indexes
are zero based and only identify quota buckets within that generation.
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
from zoneinfo import ZoneInfo


BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
_RATIO_CONTEXT = Context(prec=50)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("expected an aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone aware")
    try:
        return value.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError("datetime is outside the supported UTC range") from exc


def _nonnegative_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value


def validate_months(months: int) -> int:
    """Validate one purchase, not the accumulated lifetime of a subscription."""
    _nonnegative_integer(months, "months")
    if not 1 <= months <= 36:
        raise ValueError("months must be in 1..36")
    return months


def _seconds(delta: timedelta) -> Decimal:
    # Avoid total_seconds(), which converts to float and loses microseconds.
    microseconds = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    with localcontext(_RATIO_CONTEXT):
        return Decimal(microseconds) / Decimal(1000000)


@dataclass(frozen=True)
class TimeRange:
    """Immutable UTC [start, end), usable for an original order or a basis.

Keep the original source-order range for target-price prorating; quota periods
and the combined lifetime after renewals are not that pricing denominator.
"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _utc(self.start))
        object.__setattr__(self, "end", _utc(self.end))
        if self.end <= self.start:
            raise ValueError("end must be after start")

    def contains(self, at: datetime) -> bool:
        return self.start <= _utc(at) < self.end

    @property
    def duration_seconds(self) -> Decimal:
        return _seconds(self.end - self.start)

    def remaining_seconds(self, at: datetime) -> Decimal:
        at = _utc(at)
        if at >= self.end:
            return Decimal(0)
        return _seconds(self.end - max(at, self.start))

    def remaining_ratio(self, at: datetime) -> Decimal:
        """Clamp to [0, 1]; divide Decimal seconds at fixed 50-digit precision.

        Repeating fractions are rounded here; currency rounding belongs to
        the pricing caller. The caller's Decimal context does not affect this.
        """
        with localcontext(_RATIO_CONTEXT):
            return self.remaining_seconds(at) / self.duration_seconds


@dataclass(frozen=True)
class SubscriptionPeriod(TimeRange):
    """One quota month; identity also requires caller-owned combination/generation."""

    index: int

    def __post_init__(self) -> None:
        super().__post_init__()
        _nonnegative_integer(self.index, "index")


@dataclass(frozen=True)
class CalendarAnchor:
    """Original delivery timestamp; every boundary derives directly from it."""

    origin: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", _utc(self.origin))
        self._local_origin()

    def _local_origin(self) -> datetime:
        try:
            return self.origin.astimezone(BUSINESS_TIMEZONE)
        except OverflowError as exc:
            raise ValueError("origin is outside the supported business-time range") from exc

    def boundary(self, month_offset: int) -> datetime:
        """Return boundary(k) in UTC; offsets can exceed 36 after renewals."""
        _nonnegative_integer(month_offset, "month_offset")
        origin = self._local_origin()
        year, month = divmod(origin.year * 12 + origin.month - 1 + month_offset, 12)
        month += 1
        if not 1 <= year <= 9999:
            raise ValueError("month_offset exceeds the supported calendar range")
        day = min(origin.day, monthrange(year, month)[1])
        return _utc(origin.replace(year=year, month=month, day=day))

    def term(self, months: int, *, start_month: int = 0) -> TimeRange:
        """One source order's full range, including a future renewal order."""
        validate_months(months)
        _nonnegative_integer(start_month, "start_month")
        return TimeRange(self.boundary(start_month), self.boundary(start_month + months))

    def period(self, index: int) -> SubscriptionPeriod:
        _nonnegative_integer(index, "index")
        return SubscriptionPeriod(self.boundary(index), self.boundary(index + 1), index)

    def month_index(self, at: datetime) -> int | None:
        """Locate a quota month, or None before origin.

        This calendar has no expiry: callers must separately check an active
        paid term. An exact boundary belongs to the following month.
        """
        at = _utc(at)
        if at < self.origin:
            return None
        try:
            local = at.astimezone(BUSINESS_TIMEZONE)
        except OverflowError as exc:
            raise ValueError("at is outside the supported business-time range") from exc
        origin = self._local_origin()
        index = (local.year - origin.year) * 12 + local.month - origin.month
        if at < self.boundary(index):
            index -= 1
        return index

    def period_at(self, at: datetime) -> SubscriptionPeriod | None:
        """Locate a quota range, without granting or checking entitlement."""
        index = self.month_index(at)
        return None if index is None else self.period(index)
