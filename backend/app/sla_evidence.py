"""Immutable bounded assessment evidence and strictly read-only evidence APIs."""
from datetime import datetime, timezone
from bisect import insort
import json
from math import isfinite
import re
import secrets
from typing import Literal

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, literal, select, union_all
from sqlalchemy.orm import mapped_column


METRICS = ('availability', 'request_error_rate', 'latency_p95', 'delivery_timeliness', 'delivery_duration')
Metric = Literal['availability', 'request_error_rate', 'latency_p95', 'delivery_timeliness', 'delivery_duration']
MAX_FACTS = 200


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def safe_fact(value):
    """Never copy model dictionaries, config JSON, headers or raw payloads."""
    result = {}
    if 'source_reason' in value:
        reasons = {'http_success', 'http_failure', 'probe_timeout', 'connection_failure', 'security_withdrawal',
            'business_identity_source_missing', 'business_probe_source_missing', 'invalid_or_unsafe_source',
            'probe_adapter_error', 'outside_service_interval', 'eligibility_source_missing',
            'outside_service_scope', 'target_resolver_missing', 'target_scope_unknown', 'unknown'}
        result['source_reason'] = value['source_reason'] if value['source_reason'] in reasons else 'unknown'
    if value.get('responsibility') in {'platform', 'provider', 'caller', 'unknown'}:
        result['responsibility'] = value['responsibility']
    policy = value.get('policy_version')
    if isinstance(policy, str) and re.fullmatch(r'sla-[A-Za-z0-9_.:-]{1,76}', policy):
        result['policy_version'] = policy
    for key in ('fact_id', 'order_id', 'order_no'):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,180}', item):
            result[key] = item
    for key in ('occurred_at', 'paid_at', 'deadline_at', 'verified_at'):
        if value.get(key) is not None:
            result[key] = iso(value[key])
    for key in ('duration_ms', 'slot', 'status_code'):
        item = value.get(key)
        if type(item) in (int, float) and isfinite(item) and item >= 0:
            result[key] = item
    for key in ('included', 'attributable_failure', 'duration_present', 'on_time'):
        if type(value.get(key)) is bool or value.get(key) is None and key in value:
            result[key] = value[key]
    for key, options in {
        'kind': {'probe', 'gateway', 'delivery', 'missing_contract'},
        'status': {'healthy', 'failed', 'claimed', 'unknown', 'verified', 'pending', 'overdue'},
        'reason': {'valid_success', 'attributable_server_error', 'unresolved_eligibility',
            'unresolved_failure', 'missing_terminal_gateway_fact', 'caller_parameters', 'buyer_credentials',
            'undeclared_path', 'contract_quota', 'client_cancel', 'upstream_failure', 'timeout',
            'connection_failure', 'forward_auth_failure', 'missing_published_route', 'incorrect_rate_limit',
            'valid_input_failure', 'trusted_probe', 'trusted_isolated_test', 'trusted_internal',
            'contract_preapproved_maintenance', 'valid_probe', 'probe_unknown', 'missing_frozen_contract',
            'deadline_cohort', 'deadline_not_elapsed', 'first_verified_delivery', 'delivery_pending', 'delivery_overdue'},
    }.items():
        if value.get(key) in options:
            result[key] = value[key]
    return result


def evidence_sort_key(item):
    failed = (item.get('attributable_failure') is True or item.get('on_time') is False
              or item.get('status') in {'failed', 'overdue'})
    unexplained = (item.get('status') in {'unknown', 'claimed'} or item.get('included') is False
        or item.get('duration_present') is False or item.get('kind') == 'missing_contract'
        or 'attributable_failure' in item and item['attributable_failure'] is None)
    return (0 if failed else 1 if unexplained else 2,
            item.get('occurred_at') or item.get('deadline_at') or '',
            item.get('fact_id') or item.get('order_id') or '', json.dumps(item, sort_keys=True))


def bundle(facts, *, start=None, end=None, status='recorded'):
    captured = FactBuffer(start=start, end=end)
    for fact in facts:
        captured.add(fact)
    captured.data['evidence_status'] = status
    return captured.data


class FactBuffer:
    """Bounded copies from the exact facts visited by a calculation."""
    def __init__(self, *, start=None, end=None):
        self.data = {'evidence_status': 'recorded', 'total': 0, 'retained_count': 0, 'truncated': False,
                     'window_start': iso(start), 'window_end': iso(end), 'items': []}

    def add(self, fact):
        self.data['total'] += 1
        insort(self.data['items'], safe_fact(fact), key=evidence_sort_key)
        if len(self.data['items']) > MAX_FACTS:
            self.data['items'].pop()
        self.data['retained_count'] = len(self.data['items'])
        self.data['truncated'] = self.data['total'] > self.data['retained_count']


def safe_metric(value, metric=None):
    result = {}
    for key in ('metric', 'status', 'reason', 'frozen_tier', 'rule_version', 'contract_rule_version'):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9_:-]{1,128}', item):
            result[key] = item
    units = {'%': '%', 'percent': '%', 'ms': 'ms', 'milliseconds': 'ms', 'seconds': 'seconds', 'minutes': 'minutes'}
    unit = value.get('unit')
    if unit is None:
        unit = {'availability': '%', 'request_error_rate': '%', 'latency_p95': 'ms',
                'delivery_timeliness': '%', 'delivery_duration': 'seconds'}.get(metric)
    if isinstance(unit, str) and unit in units:
        result['unit'] = units[unit]
    for key in ('value', 'sample_count', 'target', 'warning', 'coverage_percent', 'completeness_percent'):
        item = value.get(key)
        if item is None or type(item) in (int, float) and isfinite(item):
            result[key] = item
    for key in ('paid_at', 'deadline_at', 'verified_at'):
        if value.get(key) is not None:
            result[key] = iso(value[key])
    return result


def install(ns):
    Assessment = ns['sla_online_assessments']['Assessment']
    Ticket, Clock, Event, Order = (ns[key] for key in ('AfterSalesTicket', 'TicketSlaClock', 'AfterSalesEvent', 'Order'))

    class AssessmentEvidence(ns['Base']):
        __tablename__ = 'sla_assessment_evidence'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        assessment_id = mapped_column(ForeignKey('sla_online_assessments.id', ondelete='CASCADE'), nullable=False, index=True)
        order_id = mapped_column(ForeignKey('orders.id'), nullable=False, index=True)
        version = mapped_column(Integer, nullable=False)
        input_hash = mapped_column(String(64), nullable=False)
        calculated_at = mapped_column(DateTime(timezone=True), nullable=False)
        context_json = mapped_column(Text, nullable=False)
        metrics_json = mapped_column(Text, nullable=False)
        sources_json = mapped_column(Text, nullable=False)
        __table_args__ = (UniqueConstraint('assessment_id', 'version', name='uq_sla_assessment_evidence_version'),)

    def save(db, assessment, sources):
        # Called only for a newly evaluated version, never on a historical GET
        # or an unchanged evaluation. Caller holds Order lock and owns commit.
        if db.scalar(select(AssessmentEvidence.id).where(AssessmentEvidence.assessment_id == assessment.id,
                                                        AssessmentEvidence.version == assessment.version)):
            raise RuntimeError('Assessment evidence version is immutable')
        cleaned = {}
        for metric in METRICS:
            source = sources.get(metric)
            if source is not None:
                cleaned[metric] = bundle(source.get('items', []), start=source.get('window_start'),
                    end=source.get('window_end'), status=source.get('evidence_status', 'recorded'))
                # Preserve the full visited count, not just retained records.
                cleaned[metric]['total'] = source['total']
                cleaned[metric]['truncated'] = source['total'] > cleaned[metric]['retained_count']
                if source.get('fact_identity') == 'terminal_request_id':
                    cleaned[metric]['fact_identity'] = 'terminal_request_id'
        metrics = {key: safe_metric(value, key) for key, value in json.loads(assessment.metrics_json).items() if key in METRICS}
        for metric, value in metrics.items():
            if metric not in cleaned and value.get('status') in {'not_applicable', 'not_started'}:
                cleaned[metric] = bundle([], status=value['status'])
        source_summary = json.loads(assessment.source_json)
        raw_cutoff = source_summary.get('request_source_cutoff')
        try:
            cutoff = iso(raw_cutoff)
        except (ValueError, TypeError, AttributeError):
            cutoff = None
        as_of = min(aware(assessment.calculated_at), aware(assessment.period_end))
        lag = (as_of - datetime.fromisoformat(cutoff)).total_seconds() if cutoff else None
        if lag is not None and lag < 0:
            cutoff, lag = None, None
        db.add(AssessmentEvidence(assessment_id=assessment.id, order_id=assessment.order_id,
            version=assessment.version, input_hash=assessment.input_hash, calculated_at=assessment.calculated_at,
            context_json=json.dumps({'as_of': iso(as_of),
                'period_start': iso(assessment.period_start), 'period_end': iso(assessment.period_end),
                'request_source_cutoff': cutoff,
                'request_source_lag_seconds': lag,
                'request_window_policy': 'closed_5_minute_windows',
                'source_available': {metric: ('delivery_duration' in sources if metric == 'delivery_duration'
                    else source_summary.get('source_available', {}).get(metric) is True)
                                     for metric in METRICS}}),
            metrics_json=json.dumps(metrics), sources_json=json.dumps(cleaned)))
        db.flush()

    def ticket_access(db, user, ticket_id, request):
        ticket = db.get(Ticket, ticket_id)
        order = db.get(Order, ticket.order_id) if ticket else None
        if not order:
            raise HTTPException(404, 'After-sales ticket not found')
        workflow = ns['after_sales_workflow']
        workflow['require_view'](db, user, order)
        workflow['require_scope'](request, db, user, order)
        return ticket, order

    def page_result(items, total, page, page_size):
        return {'items': items, 'total': total, 'page': page, 'page_size': page_size}

    def timeline_page(db, user, ticket_id, request, page, page_size):
        ticket, _ = ticket_access(db, user, ticket_id, request)
        where = Event.ticket_id == ticket.id
        total = db.scalar(select(func.count()).select_from(Event).where(where))
        events = list(db.scalars(select(Event).where(where).order_by(Event.occurred_at, Event.id)
            .offset((page - 1) * page_size).limit(page_size)))
        workflow = ns['after_sales_workflow']
        names = workflow['display_names'](db, [e.actor_id for e in events])
        attachments = workflow['collaboration']['event_attachments'](db, [e.id for e in events])
        return {**page_result([{'id': e.id, 'action': e.event_type, 'actor_id': e.actor_id,
            'actor_name': names.get(e.actor_id) or '', 'actor_role': e.actor_role,
            'actor_enterprise_id': e.actor_enterprise_id, 'request_id': e.request_id,
            'reason': e.reason, 'occurred_at': e.occurred_at, 'attachments': attachments.get(e.id, []),
            'before': {key: json.loads(e.before_json).get(key) for key in ('status', 'version')},
            'after': {key: json.loads(e.after_json).get(key) for key in ('status', 'version')}} for e in events],
            total, page, page_size), 'evidence_status': ticket.evidence_status}

    @ns['app'].get('/api/after-sales/{ticket_id}/sla/evidence')
    def ticket_evidence(ticket_id: str, request: Request, page: int = Query(default=1, ge=1),
            page_size: int = Query(default=20, ge=1, le=100), responsibility: str | None = Query(default=None,
                pattern='^(buyer|provider|platform)$'),
            user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        ticket, order = ticket_access(db, user, ticket_id, request)
        conditions = [Clock.ticket_id == ticket.id]
        if responsibility:
            conditions.append(Clock.party == responsibility)
        total = db.scalar(select(func.count()).select_from(Clock).where(*conditions))
        oldest_version, measured_at = db.execute(select(func.min(Clock.source_ticket_version), func.min(Clock.measured_at))
            .where(*conditions)).one()
        rows = list(db.scalars(select(Clock).where(*conditions).order_by(Clock.metric, Clock.round, Clock.party, Clock.id)
            .offset((page - 1) * page_size).limit(page_size)))
        items = []
        for row in rows:
            rule = json.loads(row.rule_snapshot_json)
            calendar = json.loads(row.calendar_snapshot_json)
            items.append({'id': row.id, 'metric': row.metric, 'round': row.round, 'responsibility': row.party,
                'elapsed_seconds': float(row.elapsed_seconds) if row.elapsed_seconds is not None else None,
                'status': row.status, 'lifecycle': row.lifecycle, 'reason': row.reason or None,
                'version': row.version, 'source_ticket_version': row.source_ticket_version,
                'source_event_ids': json.loads(row.source_event_ids_json),
                'target_seconds': rule.get('target_seconds'), 'warning_seconds': rule.get('warning_seconds'),
                'clock': rule.get('clock'), 'rule_version': row.rule_version,
                'calendar_version': calendar.get('version'),
                'calendar_bindings': [{key: binding.get(key) for key in ('year', 'calendar_version_id', 'content_hash')}
                    for binding in json.loads(row.calendar_bindings_json)],
                'started_at': row.started_at, 'ended_at': row.ended_at, 'as_of': row.measured_at,
                'stale': row.source_ticket_version != ticket.version})
        # Buyer waiting time is cooperation, not a fabricated lower/upper SLA.
        events, complete = ns['ticket_sla_clocks']['ordered_events'](db, ticket)
        waiting, opened = [], None
        for event in events:
            if event.event_type in {'request_intervention', 'respond_intervention', 'review_intervention'}:
                continue
            if event.to_status in {'awaiting_buyer_info', 'awaiting_buyer_confirmation'}:
                if opened is None:
                    opened = event
            elif opened is not None:
                waiting.append({'responsibility': 'buyer', 'start_event_id': opened.id, 'end_event_id': event.id,
                    'started_at': iso(opened.occurred_at), 'ended_at': iso(event.occurred_at), 'status': 'observed'})
                opened = None
        if opened is not None:
            waiting.append({'responsibility': 'buyer', 'start_event_id': opened.id, 'end_event_id': None,
                'started_at': iso(opened.occurred_at), 'ended_at': None, 'status': 'open'})
        return {**page_result(items, total, page, page_size), 'ticket_id': ticket.id, 'order_no': order.order_no,
            'ticket_version': ticket.version, 'evidence_status': ticket.evidence_status,
            'materialized': False, 'as_of': measured_at,
            'stale': oldest_version is not None and oldest_version != ticket.version,
            'reason': None if total else 'no_persisted_clocks',
            'buyer_cooperation': {'status': 'observed_intervals' if complete else 'unknown',
                                  'items': waiting[:MAX_FACTS] if complete else [],
                                  'truncated': len(waiting) > MAX_FACTS}}

    @ns['app'].get('/api/sla/results/{assessment_id}/sources')
    def sources(assessment_id: str, metric: Metric, version: int | None = Query(default=None, ge=1),
            page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
            user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        ns['require_platform_admin'](user)
        assessment = db.get(Assessment, assessment_id)
        if assessment is None:
            raise HTTPException(404, 'Assessment not found')
        version = assessment.version if version is None else version
        frozen = db.scalar(select(AssessmentEvidence).where(AssessmentEvidence.assessment_id == assessment_id,
                                                           AssessmentEvidence.version == version))
        result = {**page_result([], 0, page, page_size), 'assessment_id': assessment_id, 'version': version,
                  'metric': metric, 'evidence_status': 'unknown', 'reason': 'historical_source_links_missing',
                  'retained_count': 0, 'source_total': None, 'truncated': False, 'metric_result': None}
        if frozen is None:
            return result
        result.update(json.loads(frozen.context_json))
        source = json.loads(frozen.sources_json).get(metric)
        result.update(metric_result=json.loads(frozen.metrics_json).get(metric), calculated_at=frozen.calculated_at,
                      input_hash=frozen.input_hash)
        if source is None:
            result['reason'] = 'source_not_captured'
            return result
        items = source['items']
        result.update(source)
        result.update(items=items[(page - 1) * page_size:page * page_size], reason=None,
                      total=len(items), source_total=source['total'])
        return result

    def results_page(db, page, page_size):
        Legacy = ns['SLAResult']
        combined = union_all(select(Assessment.id.label('id'), Assessment.calculated_at.label('at'), literal('current').label('kind')),
            select(Legacy.id.label('id'), Legacy.calculated_at.label('at'), literal('legacy').label('kind'))).subquery()
        total = db.scalar(select(func.count()).select_from(combined))
        selected = db.execute(select(combined).order_by(combined.c.at.desc(), combined.c.id.desc(), combined.c.kind)
            .offset((page - 1) * page_size).limit(page_size)).mappings()
        items = []
        for item in selected:
            if item['kind'] == 'current':
                items.append(ns['sla_online_assessments']['output'](db, db.get(Assessment, item['id'])))
            else:
                row = db.get(Legacy, item['id'])
                items.append(ns['sla_result_out'](row, db.get(ns['SLAProfile'], row.profile_id)))
        return page_result(items, total, page, page_size)

    def has_version(db, assessment):
        return db.scalar(select(AssessmentEvidence.id).where(AssessmentEvidence.assessment_id == assessment.id,
            AssessmentEvidence.version == assessment.version)) is not None

    hooks = dict(Version=AssessmentEvidence, save=save, has_version=has_version,
                 results_page=results_page, timeline_page=timeline_page)
    ns['sla_evidence'] = hooks
    return hooks
