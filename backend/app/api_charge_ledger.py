"""Financial charge facts, distinct from best-effort online SLA request logs."""
from datetime import datetime, timedelta, timezone
import json
import hashlib
import re

from sqlalchemy import BigInteger, ForeignKey, Index, Numeric, String, cast, func, select, text, tuple_
from sqlalchemy.orm import mapped_column

from .refund_planning import ChargeSummary, PlanUnavailable
from .subscription_terms import UTCDateTime


STREAM = 'market:apisix:charges:v1'
EPOCH = 'market:apisix:charges:source'
POLICY = 'gateway_actual_charge_v1'


def stream_position(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d{0,19})-(0|[1-9]\d{0,19})', value):
        raise ValueError('Invalid stream watermark')
    parts = tuple(map(int, value.split('-')))
    if any(part > 18446744073709551615 for part in parts):
        raise ValueError('Invalid stream watermark')
    return parts


def stream_position_sql(column, dialect):
    if dialect == 'postgresql':
        parts = (func.split_part(column, '-', 1), func.split_part(column, '-', 2))
    elif dialect == 'sqlite':
        separator = func.instr(column, '-')
        parts = (func.substr(column, 1, separator - 1), func.substr(column, separator + 1))
    else:
        raise ValueError('Unsupported charge ledger dialect')
    return tuple_(*(cast(part, Numeric(20, 0)) for part in parts))


def bounded_client(ns):
    client = ns['apisix_policy_redis']()
    options = client.connection_pool.connection_kwargs
    if isinstance(options, dict):
        for key in ('socket_timeout', 'socket_connect_timeout'):
            if options.get(key) is None:
                options[key] = 3
    return client


def token(value):
    return value.replace('%', '%25').replace(':', '%3A')


def microseconds(value):
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def period_keys(scope, period):
    suffix = token(scope) + ':' + token(period)
    return ('market:apisix:quota:subscription:month:' + suffix,
            'market:apisix:charges:coverage:' + suffix)


def initialize_coverage(client, metadata):
    """Capture a nonzero pre-ledger baseline as unknown, never overwrite it."""
    periods = json.loads(metadata['subscription_periods'])
    script = """
local value = redis.call('GET', KEYS[1]) or '0'
if not value:match('^%d+$') then return redis.error_reply('Invalid quota counter') end
return redis.call('HSETNX', KEYS[2], 'baseline', value)
"""
    for period in periods:
        if period.get('order_id') and period.get('segment_id'):
            client.eval(script, 2, *period_keys(metadata['quota_scope'], period['period_key']))


def event_values(event_id, values):
    required = {'order_id', 'segment_id', 'scope', 'period_key', 'route_key',
                'charged_at_us', 'quantity', 'policy', 'source_run_id'}
    if (not re.fullmatch(r'\d+-\d+', event_id) or set(values) != required
            or values['quantity'] != '1' or values['policy'] != POLICY):
        raise ValueError('Invalid financial charge event')
    for field in ('order_id', 'segment_id'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,36}', values[field]):
            raise ValueError('Invalid charge owner')
    if any(not values[key] or len(values[key]) > 256 for key in ('scope', 'period_key', 'route_key')):
        raise ValueError('Invalid charge scope')
    if not re.fullmatch(r'[1-9]\d{0,15}', values['charged_at_us']):
        raise ValueError('Invalid charge timestamp')
    if not re.fullmatch(r'[a-f0-9]{40}', values['source_run_id']):
        raise ValueError('Invalid charge source epoch')
    return {**values, 'quantity': 1, 'charged_at_us': int(values['charged_at_us'])}


def install(ns):
    Term, Combo = (ns['subscription_terms'][key] for key in ('Term', 'Combination'))

    class Charge(ns['Base']):
        __tablename__ = 'api_financial_charges'
        __table_args__ = (Index('ix_charge_scope_period', 'scope', 'period_key'),
                          Index('ix_charge_segment_time', 'segment_id', 'charged_at_us'))
        id = mapped_column(String(48), primary_key=True)
        event_id = mapped_column(String(48), nullable=True)
        order_id = mapped_column(ForeignKey('orders.id'), nullable=False, index=True)
        segment_id = mapped_column(ForeignKey('subscription_terms.id'), nullable=False, index=True)
        scope = mapped_column(String(256), nullable=False, index=True)
        period_key = mapped_column(String(256), nullable=False, index=True)
        route_key = mapped_column(String(256), nullable=False)
        charged_at_us = mapped_column(BigInteger, nullable=False, index=True)
        quantity = mapped_column(BigInteger, nullable=False)
        policy = mapped_column(String(64), nullable=False)
        source_run_id = mapped_column(String(64), nullable=False)

    class Cursor(ns['Base']):
        __tablename__ = 'api_financial_charge_cursors'
        stream = mapped_column(String(128), primary_key=True)
        last_id = mapped_column(String(48), nullable=False, default='0-0')
        redis_run_id = mapped_column(String(64), nullable=False)
        activated_at = mapped_column(UTCDateTime(), nullable=False)

    def run_id(client):
        value = client.info('server').get('run_id')
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{40}', value):
            raise ValueError('Redis charge source identity unavailable')
        return value

    def calendar(db, term):
        from .subscription_calendar import CalendarAnchor
        anchors = list(db.scalars(select(Term.starts_at).where(
            Term.combination_id == term.combination_id, Term.generation == term.generation,
            Term.start_month == 0)))
        if len(anchors) != 1:
            raise PlanUnavailable('original_generation_anchor_unavailable')
        return CalendarAnchor(anchors[0])

    def ingest(db, client, limit=500, _sealed=False):
        if type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError('Bounded ingest batch required')
        if db.bind.dialect.name == 'postgresql':
            db.execute(text('SELECT pg_advisory_xact_lock(723401830031)'))
        managed = sources['source_context'](db, client, allow_paused=_sealed)
        active_stream = managed[0] if managed else STREAM
        cursor = managed[1] if managed else db.scalar(select(Cursor).where(Cursor.stream == STREAM).with_for_update())
        identity = run_id(client)
        if cursor is None:
            client.hsetnx(EPOCH, 'run_id', identity)
            if client.hget(EPOCH, 'run_id') != identity:
                raise ValueError('Existing Redis charge source is not continuous')
            cursor = Cursor(stream=STREAM, last_id='0-0', redis_run_id=identity, activated_at=ns['now']())
            db.add(cursor)
            db.flush()
        elif not managed and (cursor.redis_run_id != identity or client.hget(EPOCH, 'run_id') != identity):
            raise ValueError('Redis charge source restarted; explicit continuity review required')
        rows = client.xread({active_stream: cursor.last_id}, count=limit)
        inserted = 0
        read = 0
        for stream, events in rows:
            if stream != active_stream:
                raise ValueError('Unexpected charge stream')
            for event_id, raw in events:
                read += 1
                values = event_values(event_id, raw)
                if values['source_run_id'] != cursor.redis_run_id:
                    raise ValueError('Charge event does not belong to the registered source epoch')
                term = db.get(Term, values['segment_id'])
                if (term is None or term.order_id != values['order_id']
                        or term.combination_id != values['scope']):
                    raise ValueError('Charge cannot be allocated to a unique order segment')
                anchor = calendar(db, term)
                at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=values['charged_at_us'])
                if managed and at < cursor.activated_at:
                    raise ValueError('Charge precedes managed source cutover')
                matches = [index for index in range(term.start_month, term.start_month + term.months)
                    if values['period_key'] == f'{term.combination_id}:{term.generation}:{index}'
                    and anchor.period(index).start <= at < anchor.period(index).end
                    and term.starts_at <= at < term.ends_at]
                if len(matches) != 1:
                    raise ValueError('Charge is outside its attributed subscription period')
                charge_id = hashlib.sha256((cursor.redis_run_id + '|' + event_id).encode()).hexdigest()[:40] if managed else event_id
                if managed:
                    values['event_id'] = event_id
                existing = db.get(Charge, charge_id)
                if existing is None:
                    db.add(Charge(id=charge_id, **values))
                    inserted += 1
                elif any(getattr(existing, key) != value for key, value in values.items()):
                    raise ValueError('Financial charge identity cannot be rewritten')
                cursor.last_id = event_id
        # Caller commits charge facts and cursor together; no Redis deletion/ack.
        db.flush()
        return {'inserted': inserted, 'last_id': cursor.last_id, 'read': read}

    def summary(db, *, order_id, segment_id, start, end, as_of):
        def unavailable():
            raise PlanUnavailable('charge_ledger_incomplete_or_unallocated')
        term = db.get(Term, segment_id)
        combo = db.get(Combo, term.combination_id) if term else None
        if not combo or term.order_id != order_id:
            unavailable()
        if any(not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
               for value in (start, end, as_of)):
            unavailable()
        future = end == start and as_of < start
        if end < start or (end > as_of and not future) or start != term.starts_at:
            unavailable()
        cursor = db.get(Cursor, STREAM)
        anchor = calendar(db, term)
        evidence = []
        client = bounded_client(ns)
        try:
            managed = sources['source_context'](db, client)
            original_source = (managed[0], managed[1].redis_run_id) if managed else None
            if managed:
                cursor = managed[1]
            if cursor is None or start < cursor.activated_at:
                unavailable()
            if not managed and (run_id(client) != cursor.redis_run_id or client.hget(EPOCH, 'run_id') != cursor.redis_run_id):
                unavailable()
            for index in range(term.start_month, term.start_month + term.months):
                period = anchor.period(index)
                if not future and (period.start >= end or period.end <= start):
                    continue
                period_key = f'{combo.id}:{term.generation}:{index}'
                count_key, coverage_key = period_keys(combo.id, period_key)
                # A race can make this check conservatively unavailable, not falsely complete.
                baseline = client.hget(coverage_key, 'baseline')
                current = client.get(count_key)
                if baseline != '0' or (current is not None and not re.fullmatch(r'\d+', current)):
                    unavailable()
                stored = db.scalar(select(func.coalesce(func.sum(Charge.quantity), 0)).where(
                    Charge.scope == combo.id, Charge.period_key == period_key,
                    Charge.source_run_id == cursor.redis_run_id))
                if stored != int(current or 0) or (future and stored != 0):
                    unavailable()
                evidence.append('charge-period:' + period_key + ':' + str(stored))
            if managed:
                current = sources['source_context'](db, client)
                if current is None or (current[0], current[1].redis_run_id) != original_source:
                    unavailable()
            elif run_id(client) != cursor.redis_run_id or client.hget(EPOCH, 'run_id') != cursor.redis_run_id:
                unavailable()
        except PlanUnavailable:
            raise
        except Exception as exc:
            raise PlanUnavailable('charge_ledger_incomplete_or_unallocated') from exc
        finally:
            client.close()
        if not evidence:
            unavailable()
        low, high = microseconds(start), microseconds(end)
        used = db.scalar(select(func.coalesce(func.sum(Charge.quantity), 0)).where(
            Charge.order_id == order_id, Charge.segment_id == segment_id,
            Charge.source_run_id == cursor.redis_run_id,
            Charge.charged_at_us >= low, Charge.charged_at_us < high))
        return ChargeSummary(order_id, segment_id, start, end, as_of, int(used), True,
                             POLICY, tuple(evidence))

    def window_summary(db, *, order_id, segment_id, start, end, as_of,
                       source_run_id, boundary_id, scope, periods, receipt_evidence):
        """Receipt arguments must be DB-verified by the caller; no execution occurs.

        periods are the complete receipt period mappings (start_at/end_at Unix
        seconds, period_key, order_id, segment_id, used_at_switch). receipt_evidence
        contains durable receipt ID/hash evidence strings. end is the observation
        endpoint, not a historical timestamp filter on the stream facts.
        """
        def unavailable():
            raise PlanUnavailable('charge_window_incomplete_or_unallocated')

        client = None
        try:
            boundary = stream_position(boundary_id)
            if not re.fullmatch(r'[a-f0-9]{40}', source_run_id):
                unavailable()
            term = db.get(Term, segment_id)
            if term is None or term.order_id != order_id or term.combination_id != scope:
                unavailable()
            if any(not isinstance(v, datetime) or v.tzinfo is None or v.utcoffset() is None
                   for v in (start, end, as_of)):
                unavailable()
            future = start == end and as_of < start
            if (start < term.starts_at or start >= term.ends_at or end < start
                    or end > term.ends_at or (not future and end != min(as_of, term.ends_at))):
                unavailable()
            if (not isinstance(receipt_evidence, (tuple, list)) or not receipt_evidence
                    or any(not isinstance(v, str) or not v for v in receipt_evidence)):
                unavailable()
            client = bounded_client(ns)
            managed = sources['source_context'](db, client)
            original_source = (managed[0], managed[1].redis_run_id) if managed else None
            active_stream = managed[0] if managed else STREAM
            cursor = managed[1] if managed else db.get(Cursor, STREAM)
            if cursor is None or cursor.redis_run_id != source_run_id or cursor.activated_at > start:
                unavailable()
            watermark = stream_position(cursor.last_id)
            if watermark < boundary:
                unavailable()
            anchor = calendar(db, term)
            expected = {f'{scope}:{term.generation}:{i}': anchor.period(i)
                        for i in range(term.start_month, term.start_month + term.months)}
            if not isinstance(periods, (list, tuple)) or not periods:
                unavailable()
            selected = {}
            for row in periods:
                key = row['period_key']
                owner = db.get(Term, row['segment_id'])
                if (owner is None or owner.order_id != row['order_id']
                        or owner.combination_id != scope or key in selected):
                    unavailable()
                owner_anchor = calendar(db, owner)
                owner_periods = {f'{scope}:{owner.generation}:{i}': owner_anchor.period(i)
                                 for i in range(owner.start_month, owner.start_month + owner.months)}
                if key not in owner_periods:
                    unavailable()
                period = owner_periods[key]
                if (row['start_at'] != int(period.start.timestamp())
                        or row['end_at'] != int(period.end.timestamp())
                        or type(row['used_at_switch']) is not int or row['used_at_switch'] < 0):
                    unavailable()
                selected[key] = row
            required = {key for key, p in expected.items() if p.end > start}
            if not required.issubset(selected) or not selected:
                unavailable()

            def source_tail():
                if managed:
                    current = sources['source_context'](db, client)
                    if current is None or (current[0], current[1].redis_run_id) != original_source:
                        unavailable()
                elif run_id(client) != source_run_id or client.hget(EPOCH, 'run_id') != source_run_id:
                    unavailable()
                rows = client.xrevrange(active_stream, count=1)
                if not isinstance(rows, list):
                    unavailable()
                tail = rows[0][0] if rows else '0-0'
                if stream_position(tail) != watermark:
                    unavailable()
                if not rows and (boundary != (0, 0) or watermark != (0, 0)):
                    unavailable()
                return tail

            tail = source_tail()
            position = stream_position_sql(func.coalesce(Charge.event_id, Charge.id), db.bind.dialect.name)
            evidence = list(receipt_evidence) + ['charge-boundary:' + boundary_id,
                'charge-cursor:' + cursor.last_id, 'charge-source:' + source_run_id]
            calls = 0
            for key, row in selected.items():
                bucket = (Charge.scope == scope, Charge.period_key == key,
                          Charge.source_run_id == source_run_id)
                def total(*conditions):
                    return int(db.scalar(select(func.coalesce(func.sum(Charge.quantity), 0)).where(
                        *bucket, *conditions)))
                owned = (Charge.order_id == row['order_id'], Charge.segment_id == row['segment_id'],
                         Charge.source_run_id == source_run_id, Charge.policy == POLICY)
                stored = total()
                if stored != total(*owned) or total(position > watermark):
                    unavailable()
                before = total(*owned, position <= boundary)
                if before != row['used_at_switch']:
                    unavailable()
                counter, coverage = period_keys(scope, key)
                current = client.get(counter)
                if (client.hget(coverage, 'baseline') != '0'
                        or (current is not None and (not isinstance(current, str)
                            or not re.fullmatch(r'\d+', current)))
                        or stored != int(current or 0)):
                    unavailable()
                after = total(*owned, position > boundary)
                if row['segment_id'] == segment_id and future and stored:
                    unavailable()
                if row['segment_id'] == segment_id:
                    calls += after
                evidence.append(f'charge-period:{key}:{stored}')
            if source_tail() != tail:
                unavailable()
            return ChargeSummary(order_id, segment_id, start, end, as_of, calls, True,
                                 POLICY, tuple(evidence))
        except PlanUnavailable:
            raise
        except Exception as exc:
            raise PlanUnavailable('charge_window_incomplete_or_unallocated') from exc
        finally:
            if client is not None:
                client.close()

    ns['refund_charge_summary'] = summary
    hooks = dict(Charge=Charge, Cursor=Cursor, ingest=ingest, summary=summary,
                 window_summary=window_summary)
    from .api_charge_sources import install as install_sources
    sources = install_sources(ns, hooks, run_id, STREAM, EPOCH, microseconds)
    hooks.update(sources)
    def source_context(db, client):
        managed = sources['source_context'](db, client)
        if managed:
            return managed
        cursor = db.get(Cursor, STREAM)
        if cursor is None:
            return STREAM, None
        identity = run_id(client)
        if cursor.redis_run_id != identity or client.hget(EPOCH, 'run_id') != identity:
            raise ValueError('Redis source continuity unavailable')
        return STREAM, cursor
    hooks['source_context'] = source_context
    ns['api_charge_ledger'] = hooks
    return hooks
