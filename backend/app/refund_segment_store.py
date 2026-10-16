"""Internal immutable Redis cutover receipts and verified economic windows.

Install before create_all. All writes flush only; the caller owns the transaction.
"""
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fastapi import HTTPException

from sqlalchemy import Column, String, Text, Numeric, BigInteger, ForeignKey, UniqueConstraint, CheckConstraint, event, select

from . import refund_segment_boundary as boundary
from .subscription_terms import UTCDateTime
from .subscription_calendar import CalendarAnchor
from .order_economics import money
from .quota_cutover import token

STREAM = 'market:apisix:charges:v1'
EPOCH = 'market:apisix:charges:source'


def source_context(ns, db, client):
    ledger = ns.get('api_charge_ledger', {})
    hook = ledger.get('source_context')
    if callable(hook):
        return hook(db, client)
    model = ledger.get('Cursor')
    cursor = db.get(model, STREAM) if model is not None else None
    if cursor and (client.info('server')['run_id'] != cursor.redis_run_id
                   or client.hget(EPOCH, 'run_id') != cursor.redis_run_id):
        raise HTTPException(409, 'Charge source epoch continuity changed')
    return STREAM, cursor


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON number')))


def safe_policy(policy):
    for key in policy:
        if (key.lower() in ('key', 'apikey', 'api_key', 'rawkey', 'access_key')
                or any(word in key.lower() for word in ('secret', 'token', 'password', 'authorization', 'raw_key'))):
            raise ValueError('unsafe policy metadata')
    boundary._policy(policy)
    return {key: str(value) for key, value in policy.items()}


def boundary_periods(policy):
    return [{key: value for key, value in row.items()
             if key in boundary.PERIOD_FIELDS | boundary.PERIOD_IDENTITIES}
            for row in boundary._policy(policy)]


def saas_switch_evidence(change_id, direction, change_result, status_result):
    fields = {'change_id', 'direction', 'effective_at'}
    if direction not in ('target', 'original'):
        raise ValueError('invalid SaaS switch direction')
    if not any(row.get(field) is not None for row in (change_result, status_result) for field in fields):
        return None
    normalized = []
    for row in (change_result, status_result):
        if (not fields <= row.keys() or row['change_id'] != change_id or row['direction'] != direction
                or type(row['effective_at']) is not str):
            raise ValueError('incomplete SaaS switch evidence')
        at = datetime.fromisoformat(row['effective_at'].replace('Z', '+00:00'))
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError('SaaS effective time requires timezone')
        normalized.append(at.astimezone(timezone.utc))
    if normalized[0] != normalized[1]:
        raise ValueError('SaaS effective time mismatch')
    return normalized[0]


def install(ns):
    if 'refund_segment_store' in ns:
        return ns['refund_segment_store']
    Base = ns['Base']

    class Receipt(Base):
        __tablename__ = 'refund_segment_receipts'
        __table_args__ = (UniqueConstraint('change_id', 'credential_id', 'direction'),
            UniqueConstraint('change_id', 'subscription_id', 'direction'),
            CheckConstraint("(kind = 'api' AND credential_id IS NOT NULL AND subscription_id IS NULL "
                "AND source_run_id IS NOT NULL AND boundary_id IS NOT NULL AND target_fingerprint IS NOT NULL) OR "
                "(kind = 'saas' AND subscription_id IS NOT NULL AND credential_id IS NULL "
                "AND source_run_id IS NULL AND boundary_id IS NULL AND target_fingerprint IS NULL)"))
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        change_id = Column(ForeignKey('version_change_requests.id'), nullable=False, index=True)
        kind = Column(String(12), nullable=False, default='api')
        credential_id = Column(ForeignKey('api_credentials.id'), nullable=True)
        subscription_id = Column(ForeignKey('saas_subscriptions.id'), nullable=True)
        direction = Column(String(12), nullable=False)
        receipt_json = Column(Text, nullable=False)
        receipt_hash = Column(String(64), nullable=False)
        cutover_at = Column(UTCDateTime(), nullable=False)
        source_run_id = Column(String(40), nullable=True)
        boundary_id = Column(String(48), nullable=True)
        target_fingerprint = Column(String(64), nullable=True)

    class Window(Base):
        __tablename__ = 'refund_segment_windows'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        order_id = Column(ForeignKey('orders.id'), nullable=False, index=True)
        term_id = Column(ForeignKey('subscription_terms.id'), nullable=False)
        change_id = Column(ForeignKey('version_change_requests.id'), nullable=False)
        receipt_id = Column(ForeignKey('refund_segment_receipts.id'), nullable=False)
        revision_id = Column(ForeignKey('subscription_term_revisions.id'), nullable=False, unique=True)
        revenue = Column(Numeric(18, 2), nullable=False)
        cost = Column(Numeric(18, 2), nullable=False)
        currency = Column(String(3), nullable=False)
        starts_at = Column(UTCDateTime(), nullable=False)
        ends_at = Column(UTCDateTime(), nullable=False)
        available_quota = Column(BigInteger, nullable=True)
        economic_snapshot_json = Column(Text, nullable=False)
        delivery_snapshot_json = Column(Text, nullable=False)
        evidence_json = Column(Text, nullable=False)
        payload_hash = Column(String(64), nullable=False)

    def immutable(*_args):
        raise ValueError('refund segment evidence is immutable')
    for model in (Receipt, Window):
        event.listen(model, 'before_update', immutable)
        event.listen(model, 'before_delete', immutable)

    def descriptor(db, change, plan, client):
        legacy = {'mode': 'legacy', 'reason': 'continuous_attributed_charge_evidence_unavailable'}
        active = [item for item in plan['credentials'] if item['status'] == 'active']
        if len(active) != 1:
            return legacy
        item = active[0]
        key = 'market:apisix:credential:' + item['consumer']
        stream, cursor = source_context(ns, db, client)
        try:
            original = safe_policy(client.hgetall(key))
            rows = boundary._policy(original)
            stream, cursor = source_context(ns, db, client)
            now = ns['now']()
            if (not cursor or cursor.activated_at >= now
                    or client.hget(EPOCH, 'run_id') != cursor.redis_run_id
                    or original['quota_scope'] != change.combination_id
                    or client.pttl(key) != -1):
                return legacy
            expected = plan['periods']['original']
            if len(rows) != len(expected):
                return legacy
            for row, frozen in zip(rows, expected):
                if (stream != STREAM and row['start_at'] < cursor.activated_at.timestamp()
                        and row['end_at'] > now.timestamp()):
                    raise HTTPException(409, 'Charge period predates source activation')
                if (any(row.get(k) != frozen.get(k) for k in frozen)
                        or not row.get('order_id') or not row.get('segment_id')):
                    return legacy
                if row['end_at'] > now.timestamp():
                    coverage = ('market:apisix:charges:coverage:' + token(change.combination_id)
                                + ':' + token(row['period_key']))
                    if client.hget(coverage, 'baseline') != '0' or client.pttl(coverage) != -1:
                        return legacy
            current = next(p for p in plan['periods']['target']
                           if p['start_at'] <= now.timestamp() < p['end_at'])
            target = dict(original, subscription_periods=json.dumps(plan['periods']['target'], separators=(',', ':')),
                          daily_quota=str(current['daily_quota']), monthly_quota=str(current['monthly_quota']),
                          total_quota='0')
            return {'mode': 'strict', 'credential_id': item['id'], 'original': original,
                    'source_stream': stream, 'source_run_id': cursor.redis_run_id,
                    'target': safe_policy(target)}
        except (ValueError, TypeError, KeyError, StopIteration):
            return legacy

    def saas_raw(db, change, subscription, direction, effective_at):
        plan = strict_json(change.resource_plan_json)
        combo = db.get(ns['subscription_terms']['Combination'], change.combination_id)
        config = db.get(ns['SaaSIntegrationConfig'], plan['config_id'])
        config_fingerprint = digest({key: getattr(config, key, None) for key in (
            'id', 'base_url', 'operation_path', 'token_url', 'client_id', 'scope',
            'auth_mode', 'timeout_ms', 'status')})
        created_at, now = change.created_at, ns['now']()
        if any(not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
               for value in (created_at, now, effective_at)):
            raise ValueError('SaaS evidence requires aware time')
        if (not combo or combo.delivery_method != 'tenant_access' or plan.get('kind') != 'saas'
                or not config or config_fingerprint != plan['config_fingerprint']
                or combo.enterprise_id != change.enterprise_id
                or subscription.id != plan['subscription_id'] or subscription.status != 'active'
                or subscription.enterprise_id != combo.enterprise_id or subscription.product_id != combo.product_id
                or subscription.external_tenant_id != plan['tenant_id']
                or plan['product_id'] != combo.product_id or plan['enterprise_id'] != combo.enterprise_id
                or not created_at <= effective_at <= now):
            raise ValueError('SaaS receipt ownership or time mismatch')
        return dict(schema='saas_version_switch', version=1, kind='saas', change_id=change.id,
            direction=direction, scope=combo.id, subscription_id=subscription.id,
            tenant_id=subscription.external_tenant_id, product_id=combo.product_id, enterprise_id=combo.enterprise_id,
            config_id=plan['config_id'], config_fingerprint=plan['config_fingerprint'],
            version_id=change.target_version_id if direction == 'target' else change.old_version_id,
            version_code=change.target_version_code if direction == 'target' else change.old_version_code,
            effective_at=effective_at.isoformat())

    def record_saas(db, change, subscription, direction, receipt):
        try:
            existing = db.scalar(select(Receipt).where(Receipt.change_id == change.id,
                Receipt.subscription_id == subscription.id, Receipt.direction == direction))
            at = saas_switch_evidence(change.id, direction, receipt['change'], receipt['status'])
            if at is None:
                if existing:
                    raise ValueError('SaaS receipt cannot become legacy')
                return None
            raw = saas_raw(db, change, subscription, direction, at)
            status = receipt['status']
            changed = receipt['change']
            if (receipt['change'].get('ret') != 0 or status.get('ret') != 0
                    or any(status.get(key) != raw[key] for key in
                           ('subscription_id', 'tenant_id', 'product_id', 'enterprise_id'))
                    or str(status.get('version')) != raw['version_code'] or status.get('status') != 'active'
                    or ('version' in changed and str(changed['version']) != raw['version_code'])
                    or ('tenant_id' in changed and changed['tenant_id'] != raw['tenant_id'])
                    or ('status' in changed and changed['status'] != 'active')):
                raise ValueError('SaaS STATUS receipt identity mismatch')
            encoded = canonical(raw)
            if existing:
                if existing.receipt_json != encoded:
                    raise ValueError('SaaS first effective time is immutable')
                return existing
            result = Receipt(kind='saas', change_id=change.id, subscription_id=subscription.id,
                direction=direction, receipt_json=encoded, receipt_hash=hashlib.sha256(encoded.encode()).hexdigest(),
                cutover_at=at)
            db.add(result)
            db.flush()
            return result
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise HTTPException(409, 'SaaS switch receipt is incomplete or conflicting') from exc

    def record_receipt(db, change, credential, direction, receipt, *, client=None, subscription=None):
        if subscription is not None:
            if credential is not None:
                raise HTTPException(409, 'Ambiguous receipt binding')
            return record_saas(db, change, subscription, direction, receipt)
        plan = strict_json(change.resource_plan_json)
        desc = plan.get('charge_boundary', {})
        if desc.get('mode') != 'strict' or direction not in ('target', 'original'):
            raise ValueError('no strict boundary descriptor')
        combo = db.get(ns['subscription_terms']['Combination'], change.combination_id)
        route = db.get(ns['ApiGatewayRoute'], credential.route_id)
        frozen_credentials = [item for item in plan.get('credentials', []) if item['id'] == credential.id]
        if (not combo or combo.delivery_method not in ('api', 'model_api')
                or combo.enterprise_id != change.enterprise_id
                or credential.enterprise_id != change.enterprise_id or credential.status != 'active'
                or credential.id != desc['credential_id'] or not route
                or route.product_id != combo.product_id
                or route.id != plan[direction == 'target' and 'target_route_id' or 'old_route_id']):
            raise ValueError('receipt credential ownership mismatch')
        if (len(frozen_credentials) != 1 or frozen_credentials[0]['consumer'] != credential.apisix_consumer_name
                or frozen_credentials[0]['status'] != 'active'
                or ('generation' in desc['target'] and desc['target']['generation'] != str(combo.generation))):
            raise ValueError('receipt frozen credential mismatch')
        key = 'market:apisix:credential:' + credential.apisix_consumer_name
        raw = client.get(f'{boundary.NAMESPACE}{change.id}:{direction}:' + hashlib.sha256(key.encode()).hexdigest())
        parsed = strict_json(raw)
        if parsed != receipt or canonical(parsed) != canonical(receipt):
            raise ValueError('receipt differs from Redis raw intent')
        required = {'schema', 'version', 'change_id', 'direction', 'scope', 'source_run_id',
                    'boundary_id', 'target_fingerprint', 'payload_fingerprint', 'cutover_us', 'periods'}
        if (type(parsed) is not dict or set(parsed) != required
                or parsed['schema'] != 'refund_segment_boundary' or type(parsed['version']) is not int
                or parsed['version'] != 1 or parsed['change_id'] != change.id or parsed['direction'] != direction
                or parsed['scope'] != combo.id
                or not re.fullmatch(r'[a-f0-9]{40}', parsed['source_run_id'])
                or not re.fullmatch(r'(0|[1-9]\d*)-(0|[1-9]\d*)', parsed['boundary_id'])):
            raise ValueError('invalid receipt identity')
        boundary._integer(parsed['cutover_us'])
        cutover = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=parsed['cutover_us'])
        try:
            stream, cursor = source_context(ns, db, client)
        except (HTTPException, ValueError) as exc:
            raise ValueError('unregistered source epoch before cutover') from exc
        if (not cursor or cursor.activated_at >= cutover or cursor.redis_run_id != parsed['source_run_id']
                or client.hget(EPOCH, 'run_id') != cursor.redis_run_id):
            raise ValueError('unregistered source epoch before cutover')
        target = desc[direction]
        expected = desc['original' if direction == 'target' else 'target']
        periods = boundary_periods(target)
        if stream != STREAM and any(p['start_at'] < cursor.activated_at.timestamp()
                                   and p['end_at'] * 1000000 > parsed['cutover_us'] for p in periods):
            raise ValueError('receipt period predates source activation')
        payload = dict(change_id=change.id, direction=direction, expected=expected, target=target,
                       periods=periods, credential_key=key)
        payload['period_json'] = [canonical(p) for p in periods]
        payload['target_fingerprint'] = digest(target)
        if (parsed['target_fingerprint'] != digest(target) or parsed['payload_fingerprint'] != digest(payload)
                or client.hgetall(key) != target):
            raise ValueError('target metadata readback mismatch')
        live = [p for p in periods if p['end_at'] * 1000000 > parsed['cutover_us']]
        if type(parsed['periods']) is not list or len(live) != len(parsed['periods']):
            raise ValueError('receipt period set mismatch')
        for p, row in zip(live, parsed['periods']):
            if type(row) is not dict or set(row) != set(p) | {'used_at_switch', 'available_quota'}:
                raise ValueError('invalid receipt period')
            if any(type(row[k]) is not type(v) or row[k] != v for k, v in p.items()):
                raise ValueError('receipt period mismatch')
            boundary._integer(row['used_at_switch'])
            available = None if p['monthly_quota'] == 0 else max(0, p['monthly_quota'] - row['used_at_switch'])
            if (row['available_quota'] != available or
                    (available is not None and type(row['available_quota']) is not int)
                    or (p['start_at'] * 1000000 > parsed['cutover_us'] and row['used_at_switch'] != 0)):
                raise ValueError('invalid remaining quota')
            term = db.get(ns['subscription_terms']['Term'], p.get('term_id', p.get('segment_id')))
            order = db.get(ns['Order'], p.get('order_id'))
            if (not term or not order or term.order_id != order.id or term.combination_id != combo.id
                    or term.generation != combo.generation or order.product_id != combo.product_id
                    or order.buyer_enterprise_id != combo.enterprise_id
                    or (p.get('segment_id') and p['segment_id'] != term.id)
                    or p['start_at'] < term.starts_at.timestamp() or p['end_at'] > term.ends_at.timestamp()):
                raise ValueError('receipt period ownership mismatch')
            anchor = CalendarAnchor(combo.anchor_at)
            if not any(p['period_key'] == f'{combo.id}:{term.generation}:{i}'
                       and p['start_at'] == int(anchor.period(i).start.timestamp())
                       and p['end_at'] == int(anchor.period(i).end.timestamp())
                       for i in range(term.start_month, term.start_month + term.months)):
                raise ValueError('receipt period calendar mismatch')
        existing = db.scalar(select(Receipt).where(Receipt.change_id == change.id,
            Receipt.credential_id == credential.id, Receipt.direction == direction))
        if existing:
            if existing.receipt_json != raw:
                raise ValueError('receipt replay conflict')
            return existing
        result = Receipt(change_id=change.id, credential_id=credential.id, direction=direction,
                         receipt_json=raw, receipt_hash=hashlib.sha256(raw.encode()).hexdigest(),
                         cutover_at=cutover, source_run_id=parsed['source_run_id'],
                         boundary_id=parsed['boundary_id'], target_fingerprint=parsed['target_fingerprint'])
        db.add(result)
        db.flush()
        return result

    def verified(db, change, revision, term, order, proof):
        Revision = ns['subscription_terms']['Revision']
        newest = db.scalar(select(Revision).where(Revision.term_id == term.id)
                           .order_by(Revision.revision_number.desc()).limit(1))
        if newest is None or newest.id != revision.id or revision.change_id != change.id:
            raise ValueError('revision is not latest')
        if (not change.resource_applied or change.status in ('compensated', 'compensating', 'cancelled')
                or proof != strict_json(change.evidence_json)
                or set(proof) != {'request_change_id', 'resource_change_id', 'evidence_fingerprint'}
                or proof['request_change_id'] != change.id or proof['resource_change_id'] != change.id
                or not re.fullmatch(r'[a-f0-9]{64}', proof['evidence_fingerprint'])):
            raise ValueError('resource proof is not verified')
        economic, delivery = strict_json(revision.economic_snapshot_json), strict_json(revision.delivery_snapshot_json)
        frozen = strict_json(change.target_snapshots_json).get(term.id)
        combo = db.get(ns['subscription_terms']['Combination'], term.combination_id)
        if (not combo or combo.id != change.combination_id or combo.enterprise_id != change.enterprise_id
                or combo.product_id != order.product_id or order.buyer_enterprise_id != change.enterprise_id
                or combo.generation != term.generation or term.status != 'active'
                or combo.delivery_method not in ('api', 'model_api', 'tenant_access')
                or frozen is None or frozen.get('economic') != economic or frozen.get('delivery') != delivery
                or revision.version_id != change.target_version_id or revision.version_code != change.target_version_code
                or economic.get('product_version_id') != revision.version_id
                or economic.get('version_code') != revision.version_code
                or economic.get('product_id') != order.product_id or economic.get('subscription_months') != term.months
                or delivery.get('method') != combo.delivery_method):
            raise ValueError('revision snapshot ownership mismatch')
        effective = ns['subscription_terms']['effective_snapshot'](db, order, term)
        if effective['economic'] != economic or effective['delivery'] != delivery:
            raise ValueError('revision is not verified effective snapshot')
        Model = ns['version_change_ledger']['Model']
        entries = db.scalars(select(Model).where(Model.change_id == change.id,
            Model.source_order_id == order.id, Model.side == 'target')).all()
        if len(entries) != 1:
            raise ValueError('requires unique target ledger')
        entry = entries[0]
        if (entry.status not in ('pending', 'posted') or entry.combination_id != combo.id
                or entry.request_change_id != change.id or entry.target_version_id != revision.version_id
                or entry.currency != economic['currency']
                or strict_json(entry.rule_json) != economic['settlement_rule']):
            raise ValueError('target ledger differs from revision')
        duration = term.ends_at - term.starts_at
        ratio = Decimal(max(timedelta(0), min(duration, term.ends_at - entry.effective_at)) // timedelta(microseconds=1)) / Decimal(duration // timedelta(microseconds=1))
        if (money(entry.revenue) != money(money(economic['amount']) * ratio)
                or money(entry.cost) != money(money(economic['total_cost']) * ratio)):
            raise ValueError('target remaining economics mismatch')
        return entry

    def payload(db, change, revision, term, order, proof):
        entry = verified(db, change, revision, term, order, proof)
        receipts = db.scalars(select(Receipt).where(Receipt.change_id == change.id, Receipt.direction == 'target')).all()
        if len(receipts) != 1:
            raise ValueError('requires unique target receipt')
        receipt = receipts[0]
        if hashlib.sha256(receipt.receipt_json.encode()).hexdigest() != receipt.receipt_hash:
            raise ValueError('receipt hash mismatch')
        raw = strict_json(receipt.receipt_json)
        plan = strict_json(change.resource_plan_json)
        if plan.get('kind') == 'saas':
            subscription = db.get(ns['SaaSSubscription'], plan['subscription_id'])
            expected_raw = saas_raw(db, change, subscription, 'target', receipt.cutover_at)
            evidence = dict(subscription_id=subscription.id, tenant_id=plan['tenant_id'],
                            version_id=change.target_version_id, receipt_ids=[receipt.id])
            if (receipt.kind != 'saas' or receipt.subscription_id != subscription.id
                    or raw != expected_raw or receipt.credential_id is not None
                    or any(value is not None for value in (receipt.source_run_id, receipt.boundary_id, receipt.target_fingerprint))
                    or proof['evidence_fingerprint'] != digest(dict(change_id=change.id, direction='target',
                                                                   kind='saas', evidence=evidence))):
                raise ValueError('SaaS proof or receipt mismatch')
            start = max(term.starts_at, receipt.cutover_at)
            frozen_start = max(term.starts_at, entry.effective_at)
            if start >= term.ends_at or start < frozen_start:
                raise ValueError('SaaS cutover outside frozen economic window')
            ratio = Decimal((term.ends_at-start)//timedelta(microseconds=1)) / Decimal((term.ends_at-frozen_start)//timedelta(microseconds=1))
            return window_values(change, revision, term, order, proof, entry, receipt, start, None,
                                 money(entry.revenue * ratio), money(entry.cost * ratio))
        desc = plan['charge_boundary']
        safe_evidence = dict(change_id=change.id, direction='target', kind='api', evidence={
            'route_id': plan['target_route_id'], 'route_key': plan['target_route_key'],
            'credential_ids': [item['id'] for item in plan['credentials'] if item['status'] == 'active'],
            'receipt_ids': [receipt.id]})
        if proof['evidence_fingerprint'] != digest(safe_evidence):
            raise ValueError('resource proof does not bind the receipt')
        if (receipt.kind != 'api' or receipt.subscription_id is not None
                or desc.get('mode') != 'strict' or raw['change_id'] != change.id or raw['direction'] != 'target'
                or raw['scope'] != term.combination_id or raw['source_run_id'] != receipt.source_run_id
                or raw['boundary_id'] != receipt.boundary_id
                or receipt.target_fingerprint != digest(desc['target']) or raw['target_fingerprint'] != receipt.target_fingerprint
                or receipt.credential_id != desc['credential_id']
                or receipt.cutover_at != datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=raw['cutover_us'])):
            raise ValueError('receipt descriptor mismatch')
        rows = [p for p in raw['periods'] if p['order_id'] == order.id
                and p.get('term_id', p.get('segment_id')) == term.id]
        start = max(term.starts_at, receipt.cutover_at)
        if not rows or start >= term.ends_at:
            raise ValueError('no attributed refundable window')
        available = None if any(p['available_quota'] is None for p in rows) else sum(p['available_quota'] for p in rows)
        return window_values(change, revision, term, order, proof, entry, receipt, start, available,
                             money(entry.revenue), money(entry.cost))

    def window_values(change, revision, term, order, proof, entry, receipt, start, available, revenue, cost):
        return dict(order_id=order.id, term_id=term.id, change_id=change.id, receipt_id=receipt.id,
                    revision_id=revision.id, revenue=revenue, cost=cost, currency=entry.currency,
                    starts_at=start, ends_at=term.ends_at, available_quota=available,
                    economic_snapshot_json=revision.economic_snapshot_json,
                    delivery_snapshot_json=revision.delivery_snapshot_json,
                    evidence_json=canonical({'proof': proof, 'receipt_hash': receipt.receipt_hash,
                        'economic_hash': digest(strict_json(revision.economic_snapshot_json)),
                        'delivery_hash': digest(strict_json(revision.delivery_snapshot_json))}))

    def payload_hash(values):
        return digest({k: v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, Decimal) else v
                       for k, v in values.items()})

    def materialize_strict(db, change, proof):
        Revision, Term = (ns['subscription_terms'][key] for key in ('Revision', 'Term'))
        revisions = db.scalars(select(Revision).where(Revision.change_id == change.id)).all()
        if (not revisions or {r.term_id for r in revisions} != set(strict_json(change.target_snapshots_json))
                or len({r.term_id for r in revisions}) != len(revisions)):
            raise ValueError('verified revision required')
        result = []
        for revision in revisions:
            term = db.get(Term, revision.term_id)
            order = db.get(ns['Order'], term.order_id)
            values = payload(db, change, revision, term, order, proof)
            fingerprint = payload_hash(values)
            window = db.scalar(select(Window).where(Window.revision_id == revision.id))
            if window:
                if window.payload_hash != fingerprint or any(getattr(window, k) != v for k, v in values.items()):
                    raise ValueError('window replay conflict')
            else:
                window = Window(**values, payload_hash=fingerprint)
                db.add(window)
            result.append(window)
        db.flush()
        return result

    def materialize(db, change, proof):
        try:
            plan = strict_json(change.resource_plan_json or '{}')
            if plan.get('kind') == 'saas':
                receipts = db.scalars(select(Receipt).where(Receipt.change_id == change.id, Receipt.direction == 'target')).all()
                if not receipts:
                    legacy = dict(change_id=change.id, direction='target', kind='saas', evidence=dict(
                        subscription_id=plan['subscription_id'], tenant_id=plan['tenant_id'], version_id=change.target_version_id))
                    if proof.get('evidence_fingerprint') == digest(legacy) and proof == strict_json(change.evidence_json):
                        return []
                    raise ValueError('SaaS receipt missing from bound proof')
            elif plan.get('charge_boundary', {}).get('mode') != 'strict':
                return []
            return materialize_strict(db, change, proof)
        except (ValueError, TypeError, KeyError, AttributeError, ZeroDivisionError) as exc:
            raise HTTPException(409, 'Strict refund segment evidence is unavailable or conflicting') from exc

    def latest(db, order):
        Revision, Term = (ns['subscription_terms'][key] for key in ('Revision', 'Term'))
        term = db.scalar(select(Term).where(Term.order_id == order.id))
        if term is None:
            return None
        revision = db.scalar(select(Revision).where(Revision.term_id == term.id).order_by(Revision.revision_number.desc()).limit(1))
        if revision is None:
            return None
        Request = ns.get('version_change_saga', {}).get('VersionChangeRequest')
        change = db.get(Request, revision.change_id) if Request is not None else None
        if not change or change.status != 'completed' or not change.resource_applied:
            return None
        window = db.scalar(select(Window).where(Window.revision_id == revision.id))
        if window is None:
            return None
        try:
            proof = strict_json(change.evidence_json)
            values = payload(db, change, revision, term, order, proof)
            entry = verified(db, change, revision, term, order, proof)
            if (entry.status != 'posted' or entry.resource_change_id != change.id
                    or entry.resource_evidence_fingerprint != proof['evidence_fingerprint']
                    or window.payload_hash != payload_hash(values)
                    or any(getattr(window, k) != v for k, v in values.items())):
                return None
        except (ValueError, TypeError, KeyError, AttributeError):
            return None
        return window

    hooks = dict(Receipt=Receipt, Window=Window, record_receipt=record_receipt,
                 materialize=materialize, latest=latest, descriptor=descriptor)
    ns['refund_segment_store'] = hooks
    return hooks
