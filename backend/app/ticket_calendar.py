"""Append immutable annual holiday choices without rewriting ticket promises."""
from copy import deepcopy
from datetime import timedelta, timezone
import hashlib
import json
import secrets
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import mapped_column

from .holiday_calendar import SOURCE, canonical, normalized_document, validate_year


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _required_years(start, end):
    start, end = _utc(start), _utc(end)
    if end <= start:
        return []
    zone = ZoneInfo('Asia/Shanghai')
    first = start.astimezone(zone).year
    last = (end - timedelta(microseconds=1)).astimezone(zone).year
    return list(range(first, last + 1))


def _checked_document(version, year):
    if (version is None or version.year != year or version.region != 'CN'
            or version.source_url != SOURCE.format(year=year)):
        return None
    try:
        document = normalized_document(json.loads(version.document_json), year)
        if hashlib.sha256(canonical(document).encode()).hexdigest() != version.content_hash:
            return None
        return document
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None


def install(ns):
    if 'ticket_calendar' in ns:
        return ns['ticket_calendar']
    Base, Ticket = ns['Base'], ns['AfterSalesTicket']
    Version, State = (ns['holiday_calendar'][key] for key in ('Version', 'State'))

    class TicketCalendarBinding(Base):
        __tablename__ = 'ticket_calendar_bindings'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        ticket_id = mapped_column(ForeignKey('after_sales_tickets.id'), nullable=False)
        year = mapped_column(Integer, nullable=False)
        calendar_version_id = mapped_column(ForeignKey('holiday_calendar_versions.id'), nullable=False)
        bound_at = mapped_column(DateTime(timezone=True), nullable=False, default=ns['now'])
        __table_args__ = (UniqueConstraint('ticket_id', 'year', name='uq_ticket_calendar_year'),)

    Binding = TicketCalendarBinding

    def resolve_ticket_calendar(db, ticket, as_of):
        # All callers serialize on the ticket, never the order or cache state.
        ticket = db.scalar(select(Ticket).where(Ticket.id == ticket.id).with_for_update()
                           .execution_options(populate_existing=True))
        if ticket is None:
            raise HTTPException(404, 'Ticket not found')
        try:
            snapshot = json.loads(ticket.sla_snapshot_json or '{}')
            base = snapshot.get('calendar') if isinstance(snapshot, dict) else None
        except (ValueError, TypeError):
            base = None
        calendar = deepcopy(base) if isinstance(base, dict) else {}
        if calendar.get('exclude_public_holidays') is False:
            return calendar
        end = min(_utc(as_of), _utc(ticket.closed_at)) if ticket.closed_at else _utc(as_of)
        years = _required_years(ticket.created_at, end)
        original_years = {year for year in calendar.get('covered_years', []) if type(year) is int}
        covered, missing, sources = set(original_years), [], []
        for year in years:
            if year in original_years:
                continue
            try:
                validate_year(year)
            except ValueError:
                missing.append(year)
                continue
            binding = db.scalar(select(Binding).where(Binding.ticket_id == ticket.id, Binding.year == year)
                                .execution_options(populate_existing=True))
            if binding is not None:
                version = db.get(Version, binding.calendar_version_id, populate_existing=True)
            else:
                state = db.get(State, year, populate_existing=True)
                version = db.get(Version, state.current_version_id, populate_existing=True) if (
                    state is not None and state.current_version_id) else None
            document = _checked_document(version, year)
            if document is None:
                missing.append(year)
                continue
            if binding is None:
                binding = Binding(ticket_id=ticket.id, year=year, calendar_version_id=version.id,
                                  bound_at=ns['now']())
                db.add(binding)
                db.flush()
                ns['audit'](db, 'system', 'bind_ticket_calendar_year', 'ticket_calendar_binding', binding.id,
                    'Append annual calendar to an existing ticket', order_id=ticket.order_id,
                    category='sla', business_domain='sla', before={},
                    after={'year': year, 'calendar_version_id': version.id, 'content_hash': version.content_hash,
                           'source_url': version.source_url, 'order_id': ticket.order_id,
                           'bound_at': _utc(binding.bound_at).isoformat()})
            # Only appended years are populated from a validated version. Frozen years stay intact.
            for field, kind in (('holidays', 'public_holiday'), ('workdays', 'transfer_workday')):
                existing = [value for value in calendar.get(field, []) if not value.startswith(f'{year:04d}-')]
                calendar[field] = sorted(set(existing + [entry['date'] for entry in document['dates']
                                                       if entry['type'] == kind]))
            covered.add(year)
            sources.append({'year': year, 'calendar_version_id': version.id,
                            'content_hash': version.content_hash, 'source_url': version.source_url,
                            'bound_at': _utc(binding.bound_at).isoformat()})
        calendar['covered_years'] = sorted(covered)
        calendar['missing_years'] = missing
        calendar['ticket_calendar_bindings'] = sources
        calendar['holiday_calendar_status'] = 'missing_years' if missing else 'bound_years_only'
        if sources:
            version = hashlib.sha256(canonical([base.get('version') if isinstance(base, dict) else None,
                [(source['year'], source['content_hash']) for source in sources]]).encode()).hexdigest()
            calendar['version'] = version
            calendar['holiday_calendar_version'] = version
        db.flush()
        return calendar

    hooks = {'Binding': Binding, 'resolve_ticket_calendar': resolve_ticket_calendar}
    ns['TicketCalendarBinding'] = Binding
    ns['resolve_ticket_calendar'] = resolve_ticket_calendar
    ns['ticket_calendar'] = hooks
    return hooks
