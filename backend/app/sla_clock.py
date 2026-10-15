"""Deterministic SLA elapsed time over frozen calendars and auditable pauses."""
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite
from zoneinfo import ZoneInfo


class CalendarNotReady(ValueError):
    pass


def aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("SLA timestamps must have a timezone")
    return value.astimezone(timezone.utc)


def merged(intervals, start, end):
    clipped = []
    for left, right in intervals:
        left, right = aware(left), aware(right)
        if right < left:
            raise ValueError("Reversed pause interval")
        left, right = max(left, start), min(right, end)
        if left < right:
            clipped.append((left, right))
    result = []
    for left, right in sorted(clipped):
        if result and left <= result[-1][1]:
            result[-1] = (result[-1][0], max(right, result[-1][1]))
        else:
            result.append((left, right))
    return result


def _segments(start, end, calendar):
    if not isinstance(calendar, dict) or not calendar.get("version"):
        raise CalendarNotReady("Versioned support calendar required")
    zone = ZoneInfo(calendar.get("timezone", "Asia/Shanghai"))
    weekdays = calendar.get("weekdays")
    if not isinstance(weekdays, list) or not weekdays or any(type(d) is not int or d not in range(7) for d in weekdays):
        raise ValueError("Invalid support weekdays")
    years = calendar.get("covered_years", [])
    holidays = {date.fromisoformat(value) for value in calendar.get("holidays", [])}
    workdays = {date.fromisoformat(value) for value in calendar.get("workdays", [])}
    if holidays & workdays:
        raise ValueError("A date cannot be both holiday and working-day override")
    windows = []
    for left, right in calendar.get("windows", []):
        begin = time.fromisoformat(left)
        finish = time.min if right == "24:00" else time.fromisoformat(right)
        if begin.tzinfo or finish.tzinfo:
            raise ValueError("Support window must be local wall-clock time")
        windows.append((begin, finish))
    if not windows:
        raise CalendarNotReady("Explicit support windows required")
    day = start.astimezone(zone).date() - timedelta(days=1)
    last = end.astimezone(zone).date()
    spans = []
    while day <= last:
        candidates = [(datetime.combine(day, left, zone),
                       datetime.combine(day + timedelta(days=right <= left), right, zone))
                      for left, right in windows]
        candidates = [(a, b) for a, b in candidates if a < end and b > start]
        exclude_holidays = calendar.get("exclude_public_holidays", True)
        if candidates and exclude_holidays and day.year not in years:
            raise CalendarNotReady("Support calendar year is not configured")
        if day in workdays or day.weekday() in weekdays and (not exclude_holidays or day not in holidays):
            spans.extend(candidates)
        day += timedelta(days=1)
    return merged(spans, start, end)


def elapsed_seconds(start, end, *, mode, calendar=None, pauses=()):
    start, end = aware(start), aware(end)
    if end < start:
        raise ValueError("SLA end must not precede start")
    if end == start:
        return 0.0
    if mode not in {"natural", "support"}:
        raise ValueError("Unknown SLA clock mode")
    intervals = [(start, end)] if mode == "natural" else _segments(start, end, calendar)
    paused = merged(pauses, start, end)
    return sum((right - left).total_seconds() - sum(
        max(0.0, (min(right, b) - max(left, a)).total_seconds()) for a, b in paused)
        for left, right in intervals)


def minute_budget(value):
    if isinstance(value, bool):
        raise ValueError("SLA minute budget must be positive")
    value = float(value)
    if not isfinite(value) or value <= 0:
        raise ValueError("SLA minute budget must be positive")
    return value * 60


def ticket_measurements(ticket, events, as_of, calendar=None):
    """Intervention never pauses clocks; only explicit buyer waits pause resolution."""
    as_of = aware(as_of)
    created = aware(ticket.created_at)
    if as_of < created:
        raise ValueError("Cannot evaluate before ticket creation")
    first_response, closed = ticket.first_response_at, ticket.closed_at
    prior = created
    paused_at = None
    pauses = []
    interventions = []
    intervention_start = None
    for event in events:
        at = aware(event.occurred_at)
        if at < prior:
            raise ValueError("SLA events must be monotonic")
        prior = at
        if at > as_of:
            break
        # Entering platform review resumes resolution even if the previous state waited on the buyer.
        waiting = event.to_status in {"awaiting_buyer_info", "awaiting_buyer_confirmation"}
        if paused_at is not None and not waiting:
            pauses.append((paused_at, at))
            paused_at = None
        if paused_at is None and waiting:
            paused_at = at
        if event.event_type == "request_intervention":
            intervention_start = at
        if event.event_type == "review_intervention" and intervention_start is not None:
            interventions.append((intervention_start, at))
            intervention_start = None
    end = min(aware(closed), as_of) if closed else as_of
    if paused_at is not None:
        pauses.append((paused_at, end))
    if intervention_start is not None:
        interventions.append((intervention_start, end))
    mode = "natural" if ticket.priority == "urgent" else "support"
    response_end = min(aware(first_response), as_of) if first_response else end
    return {
        "first_response_seconds": elapsed_seconds(created, response_end, mode=mode, calendar=calendar),
        "resolution_seconds": elapsed_seconds(created, end, mode="support", calendar=calendar, pauses=pauses),
        "intervention_seconds": sum(elapsed_seconds(a, b, mode="support", calendar=calendar)
                                    for a, b in interventions),
        "response_completed": bool(first_response and aware(first_response) <= as_of),
        "resolution_outcome": ticket.status if closed and aware(closed) <= as_of else "open",
        "historical_evidence": ticket.evidence_status,
    }
