"""Transactional clock projections. Caller must hold Order then Ticket locks.

No commit or background I/O. Calendar resolution may append audited annual
bindings; frozen ticket/rule/base-calendar JSON is never rewritten.
"""
from copy import deepcopy
from datetime import timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select

from .sla_clock import CalendarNotReady, elapsed_seconds, ticket_measurements
from .sla_milestones import MilestoneEvent, reduce_milestones


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def install(ns):
    Clock, Event = ns['TicketSlaClock'], ns['AfterSalesEvent']

    def ordered_events(db, ticket):
        events = list(db.scalars(select(Event).where(Event.ticket_id == ticket.id)))

        def version(event):
            try:
                value = json.loads(event.after_json).get('version')
                return value if type(value) is int and value > 0 else 0
            except (ValueError, TypeError, AttributeError):
                return 0

        events.sort(key=lambda event: (utc(event.occurred_at), version(event), event.id))
        complete = (ticket.evidence_status == 'recorded' and bool(events)
            and events[0].event_type == 'create'
            and events[0].to_status == 'awaiting_response'
            and utc(events[0].occurred_at) >= utc(ticket.created_at)
            and (ticket.closed_at is None or utc(events[-1].occurred_at) <= utc(ticket.closed_at))
            and [version(event) for event in events] == list(range(1, ticket.version + 1)))
        return events, complete

    def require_first_intervention_response(db, ticket):
        events, _ = ordered_events(db, ticket)
        start = None
        responded = False
        for event in events:
            if event.event_type == 'request_intervention':
                start, responded = event, False
            elif event.event_type == 'review_intervention':
                start = None
            elif event.event_type == 'respond_intervention' and start is not None:
                responded = True
        if start is None:
            raise HTTPException(409, 'Intervention request evidence is missing')
        if responded:
            raise HTTPException(409, 'This intervention round already has a first response')

    def output(row):
        rule = json.loads(row.rule_snapshot_json)
        return {'id': row.id, 'metric': row.metric, 'round': row.round, 'responsibility': row.party,
            'elapsed_seconds': float(row.elapsed_seconds) if row.elapsed_seconds is not None else None,
            'status': row.status, 'lifecycle': row.lifecycle, 'reason': row.reason or None,
            'version': row.version, 'source_ticket_version': row.source_ticket_version,
            'source_event_ids': json.loads(row.source_event_ids_json),
            'target_seconds': rule.get('target_seconds'), 'warning_seconds': rule.get('warning_seconds'),
            'clock': rule.get('clock'), 'rule_snapshot': rule,
            'rule_version': row.rule_version,
            'calendar_snapshot': json.loads(row.calendar_snapshot_json),
            'calendar_bindings': json.loads(row.calendar_bindings_json),
            'started_at': row.started_at, 'ended_at': row.ended_at, 'as_of': row.measured_at}

    def refresh(db, ticket, at):
        db.flush()  # Publish the new event's ID/version in the same transaction.
        snapshot = json.loads(ticket.sla_snapshot_json or '{}')
        if ticket.evidence_status != 'recorded' or not snapshot.get('metrics'):
            return {'status': 'insufficient_data', 'reason': 'historical_evidence_unknown',
                    'items': [], 'evidence_status': ticket.evidence_status}
        at = utc(at)
        end = min(at, utc(ticket.closed_at)) if ticket.closed_at else at
        events, complete = ordered_events(db, ticket)
        if not complete:
            return {'status': 'insufficient_data', 'reason': 'incomplete_history',
                    'items': [], 'evidence_status': ticket.evidence_status}
        events = [event for event in events if utc(event.occurred_at) <= end]
        rows = list(db.scalars(select(Clock).where(Clock.ticket_id == ticket.id)
                               .execution_options(populate_existing=True)))
        existing = {(row.metric, row.round, row.party): row for row in rows}
        rules = deepcopy(snapshot['metrics'])
        for row in rows:
            rules[row.metric] = json.loads(row.rule_snapshot_json)
        base_calendar = deepcopy(snapshot.get('calendar'))
        calendar = base_calendar
        resolve = ns.get('resolve_ticket_calendar')
        if resolve:
            calendar = resolve(db, ticket, end)
        resolved_snapshot = {**snapshot, 'metrics': rules, 'calendar': calendar}
        response_event = next((event for event in events if event.event_type == 'respond'), None)
        facts = SimpleNamespace(priority=ticket.priority, status=ticket.status,
            evidence_status=ticket.evidence_status, created_at=utc(ticket.created_at),
            first_response_at=utc(response_event.occurred_at) if response_event and complete else None,
            closed_at=utc(ticket.closed_at))
        event_facts = [MilestoneEvent(event.id, event.event_type, utc(event.occurred_at), event.to_status)
                       for event in events]
        calendar_status = 'ready'
        try:
            measured = ticket_measurements(facts, event_facts, end, calendar) if complete else {}
        except CalendarNotReady:
            measured, calendar_status = {}, 'not_ready'
            if ticket.priority == 'urgent':
                response_end = min(facts.first_response_at or end, end)
                measured['first_response_seconds'] = elapsed_seconds(facts.created_at, response_end, mode='natural')
        pending = []
        urgent = ticket.priority == 'urgent'
        closed = ticket.status in {'completed', 'withdrawn', 'force_closed', 'resolved', 'closed'}
        for name, source, done in (
            ('urgent_first_response' if urgent else 'first_response', 'first_response_seconds',
             bool(facts.first_response_at and facts.first_response_at <= end)),
            ('urgent_resolution' if urgent else 'resolution', 'resolution_seconds', ticket.status == 'completed')):
            rule = rules.get(name)
            if not rule:
                continue
            value = measured.get(source)
            status = 'insufficient_data' if value is None else (
                'breached' if value > rule['target_seconds'] else
                'closed_without_confirmation' if closed and not done else
                'warning' if value >= rule['warning_seconds'] else 'met' if done else 'running')
            sources = [event.id for event in events if name.endswith('resolution') or
                       event.event_type in {'create', 'respond'}]
            pending.append({'metric': name, 'round': 1, 'party': rule['responsibility'], 'rule': rule,
                'elapsed_seconds': value, 'status': status,
                'lifecycle': 'completed' if done else 'closed_incomplete' if closed else 'open',
                'reason': '' if value is not None else 'incomplete_history' if not complete else 'calendar_not_ready',
                'sources': sources, 'started_at': facts.created_at if complete else None,
                'ended_at': (facts.first_response_at if source == 'first_response_seconds' and done
                             else facts.closed_at if closed else None)})
        positions = {event.id: index for index, event in enumerate(events)}
        for item in reduce_milestones(event_facts, resolved_snapshot, as_of=end,
                priority='urgent' if urgent else 'normal', history_complete=complete):
            if item.reason == 'calendar_not_ready':
                calendar_status = 'not_ready'
            pending.append({'metric': item.metric_key, 'round': item.round, 'party': item.responsibility,
                'rule': item.frozen_target or {}, 'elapsed_seconds': item.elapsed_seconds,
                'status': item.status, 'lifecycle': item.lifecycle, 'reason': item.reason or '',
                'sources': [event.id for event in events[
                    positions[item.source_event_ids[0]]:
                    positions[item.source_event_ids[-1]] + 1 if item.ended_at else len(events)]],
                'started_at': item.started_at, 'ended_at': item.ended_at})
        changed = []
        result = []
        for item in pending:
            key = item['metric'], item['round'], item['party']
            row = existing.get(key)
            if row is None:
                row = Clock(ticket_id=ticket.id, metric=key[0], round=key[1], party=key[2], version=1,
                    rule_snapshot_json=canonical(item['rule']), calendar_snapshot_json=canonical(base_calendar),
                    rule_version=snapshot.get('contract_rule_version') or snapshot.get('rule_version') or '',
                    calendar_bindings_json='[]')
                db.add(row)
            bindings = json.loads(row.calendar_bindings_json)
            by_year = {entry['year']: entry for entry in bindings}
            for entry in (calendar or {}).get('ticket_calendar_bindings', []):
                # Finished rounds must not acquire bindings for years after their endpoint.
                endpoint = utc(item['ended_at'] or end)
                if item['started_at'] is not None and endpoint > item['started_at']:
                    endpoint -= timedelta(microseconds=1)
                last_year = endpoint.astimezone(ZoneInfo('Asia/Shanghai')).year
                if entry['year'] > last_year:
                    continue
                previous = by_year.get(entry['year'])
                if previous and previous['calendar_version_id'] != entry['calendar_version_id']:
                    raise HTTPException(409, 'An immutable SLA calendar binding changed')
                by_year.setdefault(entry['year'], entry)
            values = {'source_event_ids_json': canonical(item['sources']),
                'source_ticket_version': ticket.version,
                'calendar_bindings_json': canonical(sorted(by_year.values(), key=lambda entry: entry['year'])),
                'elapsed_seconds': Decimal(str(item['elapsed_seconds'])) if item['elapsed_seconds'] is not None else None,
                'status': item['status'], 'lifecycle': item['lifecycle'], 'reason': item['reason'],
                'started_at': item['started_at'], 'ended_at': item['ended_at']}
            dirty = row.id is None or any(
                (utc(getattr(row, key)) if key.endswith('_at') else getattr(row, key)) != value
                for key, value in values.items())
            # Open projections advance at an explicit evaluation instant, never backwards.
            if row.measured_at is not None and utc(row.measured_at) > end:
                raise HTTPException(409, 'Clock evaluation cannot move backwards')
            if dirty or row.lifecycle == 'open' and (row.measured_at is None or utc(row.measured_at) != end):
                if row.id is not None:
                    row.version += 1
                for field, value in values.items():
                    setattr(row, field, value)
                row.measured_at = end
                changed.append(row)
            result.append(row)
        db.flush()
        if changed:
            ns['audit'](db, 'system', 'materialize_ticket_sla_clocks', 'after_sales_ticket', ticket.id,
                'Project recorded events without changing frozen promises', category='sla', business_domain='sla',
                order_id=ticket.order_id, after={'ticket_version': ticket.version,
                    'clocks': [{'id': row.id, 'metric': row.metric, 'round': row.round,
                                'status': row.status, 'version': row.version} for row in changed]})
        db.flush()
        return {'status': 'insufficient_data' if not result else 'partial' if calendar_status != 'ready' else 'measured',
            'reason': 'missing_time_rules' if not result else None,
            'calendar_status': calendar_status, 'calendar_version': (calendar or {}).get('version'),
            'evidence_status': ticket.evidence_status, 'as_of': at, 'items': [output(row) for row in result],
            'scope': 'first_response_resolution_rectification_intervention', 'materialized': True}

    def on_event(db, ticket, event):
        return refresh(db, ticket, utc(event.occurred_at))

    hooks = {'refresh': refresh, 'ordered_events': ordered_events,
             'require_first_intervention_response': require_first_intervention_response}
    ns['ticket_sla_event'] = on_event
    ns['ticket_sla_clocks'] = hooks
    return hooks
