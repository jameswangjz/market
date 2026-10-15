"""Pure SLA aggregation over already scoped, deduplicated source facts.

No collection, persistence, attribution, or clock integration occurs here.
Target bounds are inclusive; warning bounds are inclusive on the risk side.
Insufficient samples retain measured values but never claim period compliance.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Iterable, Literal


Status = Literal["met", "warning", "breached", "insufficient_data"]
Tier = Literal["standard", "enterprise", "strategic"]
Direction = Literal["lower", "upper"]
BEIJING = timezone(timedelta(hours=8))
ONLINE = frozenset({"file", "api", "model_api", "tenant_access"})
OFFLINE = frozenset({"training", "consulting", "custom"})


def _count(value: int, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _number(value: float, name: str, maximum: float | None = None) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not isfinite(value) or value < 0
            or (maximum is not None and value > maximum)):
        raise ValueError(f"{name} must be finite and nonnegative within its range")


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


@dataclass(frozen=True)
class Threshold:
    target: float
    warning: float
    direction: Direction

    def __post_init__(self) -> None:
        _number(self.target, "target")
        _number(self.warning, "warning")
        if self.direction not in {"lower", "upper"}:
            raise ValueError("direction must be lower or upper")
        if ((self.direction == "lower" and self.warning < self.target)
                or (self.direction == "upper" and self.warning > self.target)):
            raise ValueError("warning must precede breach in the risk direction")

    def classify(self, value: float | None, *, sufficient: bool) -> Status:
        if type(sufficient) is not bool:
            raise ValueError("sufficient must be bool")
        if value is not None:
            _number(value, "value")
        if value is None or not sufficient:
            return "insufficient_data"
        if self.direction == "lower":
            if value < self.target:
                return "breached"
            return "warning" if value <= self.warning else "met"
        if value > self.target:
            return "breached"
        return "warning" if value >= self.warning else "met"


def threshold(metric: Literal["availability", "errors", "latency", "delivery"],
              tier: Tier = "standard") -> Threshold:
    if tier not in ("standard", "enterprise", "strategic"):
        raise ValueError("unknown SLA tier")
    index = ("standard", "enterprise", "strategic").index(tier)
    if metric in {"availability", "delivery"}:
        return Threshold((90, 95, 98)[index], (92, 96, 98.4)[index], "lower")
    if metric == "errors":
        return Threshold((10, 5, 3)[index], (8, 4, 2.4)[index], "upper")
    if metric == "latency":
        return Threshold((5000, 4000, 3000)[index], (4000, 3200, 2400)[index], "upper")
    raise ValueError("unknown SLA metric")


@dataclass(frozen=True)
class MetricResult:
    value: float | None
    status: Status
    sample_count: int
    coverage_percent: float | None = None
    completeness_percent: float | None = None


def _ratio(numerator: int, denominator: int) -> float | None:
    _count(numerator, "numerator")
    _count(denominator, "denominator")
    if numerator > denominator:
        raise ValueError("numerator cannot exceed denominator")
    return numerator / denominator * 100 if denominator else None


def availability(successes: int, valid_probes: int, expected_probes: int, *,
                 tier: Tier = "standard", min_samples: int = 100,
                 min_coverage: float = 90) -> MetricResult:
    """Expected probes cover only the service's actual eligible period."""
    _count(min_samples, "min_samples", 1)
    _number(min_coverage, "min_coverage", 100)
    value = _ratio(successes, valid_probes)
    coverage = _ratio(valid_probes, expected_probes)
    sufficient = valid_probes >= min_samples and coverage is not None and coverage >= min_coverage
    return MetricResult(value, threshold("availability", tier).classify(value, sufficient=sufficient),
                        valid_probes, coverage_percent=coverage)


def request_error_rate(failures: int, valid_requests: int, *, tier: Tier = "standard",
                       min_samples: int = 100) -> MetricResult:
    """Caller supplies attributable failures and eligible terminal requests."""
    _count(min_samples, "min_samples", 1)
    value = _ratio(failures, valid_requests)
    return MetricResult(value, threshold("errors", tier).classify(
        value, sufficient=valid_requests >= min_samples), valid_requests)


def latency_p95(durations_ms: Iterable[float | None], *, tier: Tier = "standard",
                min_samples: int = 100, min_completeness: float = 95) -> MetricResult:
    """One entry per eligible request, including failures; None means missing."""
    _count(min_samples, "min_samples", 1)
    _number(min_completeness, "min_completeness", 100)
    durations = []
    total = 0
    for duration in durations_ms:
        total += 1
        if duration is not None:
            _number(duration, "duration_ms")
            durations.append(duration)
    durations.sort()
    count = len(durations)
    # Integer arithmetic preserves the exact nearest-rank boundary.
    value = durations[(95 * count + 99) // 100 - 1] if count else None
    completeness = _ratio(count, total)
    sufficient = count >= min_samples and completeness is not None and completeness >= min_completeness
    return MetricResult(value, threshold("latency", tier).classify(value, sufficient=sufficient),
                        count, completeness_percent=completeness)


@dataclass(frozen=True)
class DeliveryFact:
    order_id: str
    method: str
    paid_at: datetime
    deadline: datetime
    first_verified_success_at: datetime | None = None
    paid: bool = True
    cancelled_before_delivery: bool = False
    isolated_test: bool = False
    requires_delivery: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise ValueError("order_id is required")
        if self.method not in ONLINE | OFFLINE:
            raise ValueError("unknown delivery method")
        for name in ("paid", "cancelled_before_delivery", "isolated_test", "requires_delivery"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be bool")
        _aware(self.paid_at, "paid_at")
        _aware(self.deadline, "deadline")
        if self.deadline < self.paid_at:
            raise ValueError("deadline cannot precede payment")
        if self.first_verified_success_at is not None:
            _aware(self.first_verified_success_at, "first_verified_success_at")
            if self.first_verified_success_at < self.paid_at:
                raise ValueError("verified success cannot precede payment")


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """Beijing calendar month as a half-open interval, expressed in UTC."""
    _count(year, "year", 1)
    _count(month, "month", 1)
    if year >= 9999 or month > 12:
        raise ValueError("unsupported calendar month")
    start = datetime(year, month, 1, tzinfo=BEIJING)
    end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=BEIJING)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def delivery_timeliness(facts: Iterable[DeliveryFact], *, year: int, month: int,
                        as_of: datetime, tier: Tier = "standard",
                        min_samples: int = 10) -> MetricResult:
    """Deadline assigns the month; overdue failures stay in the denominator.

    Facts carry frozen deadlines, not recalculated current SLA targets. Exact
    duplicate order facts collapse; conflicting duplicates require reconciliation.
    Refund state deliberately does not erase an established delivery failure.
    """
    _aware(as_of, "as_of")
    _count(min_samples, "min_samples", 1)
    start, end = month_bounds(year, month)
    orders: dict[str, DeliveryFact] = {}
    for fact in facts:
        if not isinstance(fact, DeliveryFact):
            raise ValueError("expected DeliveryFact")
        if fact.order_id in orders and orders[fact.order_id] != fact:
            raise ValueError("conflicting delivery facts for the same order")
        orders[fact.order_id] = fact
    eligible = [fact for fact in orders.values()
                if fact.method in ONLINE and fact.paid and not fact.cancelled_before_delivery
                and not fact.isolated_test and fact.requires_delivery
                and start <= fact.deadline < end and fact.deadline <= as_of]
    on_time = sum(fact.first_verified_success_at is not None
                  and fact.first_verified_success_at <= fact.deadline for fact in eligible)
    value = _ratio(on_time, len(eligible))
    return MetricResult(value, threshold("delivery", tier).classify(
        value, sufficient=len(eligible) >= min_samples), len(eligible))
