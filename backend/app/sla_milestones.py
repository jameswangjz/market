"""Pure, non-persistent milestone reduction over explicit workflow evidence.

Callers must explicitly attest complete history. No ORM objects, live rules, or
wall clock are consulted; callers serialize the returned UTC timestamps.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite
from typing import Any, Literal, Mapping, Sequence

from .sla_clock import CalendarNotReady, aware, elapsed_seconds


TERMINAL = frozenset({'completed', 'withdrawn', 'force_closed', 'resolved', 'closed'})
Kind = Literal['rectification', 'intervention_review', 'intervention_response']


@dataclass(frozen=True)
class MilestoneEvent:
    id: str
    event_type: str
    occurred_at: datetime
    to_status: str


@dataclass(frozen=True)
class Milestone:
    kind: Kind
    round: int
    source_event_ids: tuple[str, ...]
    responsibility: str
    metric_key: str
    frozen_target: dict[str, Any] | None
    started_at: datetime | None
    ended_at: datetime | None
    lifecycle: str
    elapsed_seconds: float | None
    status: str
    reason: str | None


def reduce_milestones(
    events: Sequence[MilestoneEvent], snapshot: Mapping[str, Any], *,
    as_of: datetime, priority: Literal['normal', 'urgent'] = 'normal',
    history_complete: bool = False,
) -> tuple[Milestone, ...]:
    """Reduce rounds, preserving input event order (including timestamp ties).

    Missing starts/targets/calendar/history produce insufficient_data. A missing
    intervention response always does too, even after review or terminal close.
    Complete evidence is an explicit caller assertion, never inferred from age.
    """
    cutoff = aware(as_of)
    if priority not in {'normal', 'urgent'} or type(history_complete) is not bool:
        raise ValueError('Invalid priority or history completeness')
    prior = None
    seen = set()
    visible = []
    for event in events:
        at = aware(event.occurred_at)
        if not isinstance(event.id, str) or not event.id.strip() or event.id in seen:
            raise ValueError('Unique nonblank source event IDs required')
        if not event.event_type or not event.to_status:
            raise ValueError('Event type and destination status required')
        if prior is not None and at < prior:
            raise ValueError('Events must be chronologically ordered')
        seen.add(event.id)
        prior = at
        if at <= cutoff:
            visible.append(event)

    metrics = snapshot.get('metrics', {})
    calendar = snapshot.get('calendar')
    rounds: dict[str, int] = {}
    active: dict[str, MilestoneEvent] = {}
    results = []
    pauses = []
    waiting = None
    terminal = False

    def finish(kind: Kind, end: MilestoneEvent | None, lifecycle: str, *, missing_start=False):
        start = None if missing_start else active.pop(kind, None)
        rounds[kind] = rounds.get(kind, 0) + 1
        key = ('urgent_intervention_response' if priority == 'urgent' else 'intervention_response') \
            if kind == 'intervention_response' else kind
        party = 'provider' if kind == 'rectification' else 'platform'
        target = deepcopy(metrics.get(key)) if isinstance(metrics, Mapping) else None
        begin = aware(start.occurred_at) if start else None
        stop = aware(end.occurred_at) if end else cutoff
        ids = tuple(event.id for event in (start, end) if event is not None)
        reason = None
        elapsed = None
        status = 'insufficient_data'
        if not history_complete:
            reason = 'incomplete_history'
        elif begin is None:
            reason = 'missing_start_event'
        elif kind == 'intervention_response' and lifecycle != 'completed':
            reason = 'missing_response_event'
        elif not isinstance(target, dict):
            reason = 'missing_frozen_target'
        else:
            budget, warning = target.get('target_seconds'), target.get('warning_seconds')
            mode = target.get('clock')
            if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                    or not isfinite(value) or value <= 0 for value in (budget, warning))
                    or warning > budget or mode not in {'support', 'natural'}
                    or target.get('responsibility') != party or target.get('direction') != 'upper'):
                reason = 'invalid_frozen_target'
            else:
                effective_pauses = list(pauses) if kind == 'rectification' else []
                if kind == 'rectification' and waiting is not None:
                    effective_pauses.append((waiting, stop))
                try:
                    elapsed = elapsed_seconds(begin, stop, mode=mode, calendar=calendar,
                                              pauses=effective_pauses)
                except CalendarNotReady:
                    reason = 'calendar_not_ready'
                else:
                    status = ('breached' if elapsed > budget else
                              'closed_without_confirmation' if lifecycle == 'closed_incomplete' else
                              'warning' if elapsed >= warning else 'within_target')
        results.append(Milestone(kind, rounds[kind], ids, party, key, target, begin,
            stop if end else None, lifecycle, elapsed, status, reason))

    def start(kind: Kind, event: MilestoneEvent):
        if kind in active:
            # A second start is evidence of a gap, not completion of the first round.
            finish(kind, event, 'interrupted')
            results[-1] = replace(results[-1], elapsed_seconds=None,
                status='insufficient_data', reason='overlapping_start')
        active[kind] = event

    for event in visible:
        if terminal:
            raise ValueError('Events after a terminal state are not a valid workflow history')
        at = aware(event.occurred_at)
        # Only buyer-material waits pause rectification; intervention resumes it.
        if waiting is not None and event.to_status != 'awaiting_buyer_info':
            pauses.append((waiting, at))
            waiting = None
        if waiting is None and event.to_status == 'awaiting_buyer_info':
            waiting = at
        action = event.event_type
        if action == 'reject_resolution':
            start('rectification', event)
        elif action == 'submit_resolution' and 'rectification' in active:
            finish('rectification', event, 'completed')
        elif action == 'request_intervention':
            start('intervention_review', event)
            start('intervention_response', event)
        elif action == 'respond_intervention':
            finish('intervention_response', event, 'completed',
                   missing_start='intervention_response' not in active)
        elif action == 'review_intervention':
            finish('intervention_review', event, 'completed',
                   missing_start='intervention_review' not in active)
            if 'intervention_response' in active:
                finish('intervention_response', event, 'closed_incomplete')
        if event.to_status in TERMINAL:
            for kind in tuple(active):
                finish(kind, event, 'closed_incomplete')
            terminal = True
    for kind in tuple(active):
        finish(kind, None, 'open')
    return tuple(results)
