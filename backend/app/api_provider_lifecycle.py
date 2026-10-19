"""API management protocol v1. Install before create_all; hooks never commit.

prepare(db, payload, management_url) -> (immutable Plan, created), no network.
Caller commits staged term/combination/plan before execute(db, plan). STATUS
requests replace type with STATUS and add target_type; replies are the actual
persisted ROOT with original type, ret=0, status, and first stable effective_at.
Version is {id, code}; periods contain full target rights, not financial data.
Never reconstruct a saved plan or fill its tenant_id after an OPEN rollback.
management_url MUST come from the frozen delivery snapshot. Empty means explicit
skipped_unmanaged even if live product registration now has a management URL.
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import Column, ForeignKey, Integer, String, Text, UniqueConstraint, event, select
from sqlalchemy.orm import Session, object_session

from .subscription_terms import UTCDateTime

EVENTS = frozenset({'OPEN', 'RENEW', 'CHANGE', 'CLOSE', 'RESTORE', 'ADJUST', 'STATUS'})
QUOTAS = ('rate_limit_per_minute', 'daily_quota', 'monthly_quota', 'quota_amount')
ROOT = frozenset({'schema_version', 'type', 'event_id', 'idempotency_key', 'product_id',
    'enterprise_id', 'combination_id', 'generation', 'tenant_id', 'order_id', 'order_no',
    'version', 'subscription_months', 'starts_at', 'expires_at', 'periods'})
OPTIONAL = frozenset({'change_id', 'refund_id', 'direction'})
PERIOD = frozenset({'order_id', 'order_no', 'term_id', 'starts_at', 'expires_at', 'quotas'})
SKIPPED = {'status': 'skipped_unmanaged', 'provider_synced': False}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fail(message='Provider lifecycle evidence rejected'):
    raise HTTPException(409, message)


def iso(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('Lifecycle dates require a timezone')
    return value.astimezone(timezone.utc).isoformat()


def valid_text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200


def validate_payload(payload):
    """Allowlist identity and rights; never send credentials, prices, or costs."""
    try:
        body = json.loads(canonical(payload))
        if not isinstance(body, dict) or not ROOT <= body.keys() or body.keys() - ROOT - OPTIONAL:
            fail('Invalid provider payload fields')
        if type(body['schema_version']) is not int or body['schema_version'] != 1 or body['type'] not in EVENTS:
            fail('Unsupported provider protocol')
        for key in ('event_id', 'idempotency_key', 'product_id', 'enterprise_id', 'combination_id', 'order_id', 'order_no'):
            if not valid_text(body[key]):
                fail('Invalid provider identity')
        if len(body['idempotency_key']) > 128:
            fail('Invalid provider key')
        for key in ('generation', 'subscription_months'):
            if type(body[key]) is not int or body[key] < 1:
                fail('Invalid provider calendar')
        if body['tenant_id'] is not None and not valid_text(body['tenant_id']):
            fail('Invalid provider tenant')
        if (not isinstance(body['version'], dict) or set(body['version']) != {'id', 'code'}
                or not all(valid_text(v) for v in body['version'].values())):
            fail('Invalid frozen version')
        for key in ('change_id', 'refund_id'):
            if key in body and not valid_text(body[key]):
                fail('Invalid operation identity')
        if 'direction' in body and body['direction'] not in {'upgrade', 'downgrade', 'restore'}:
            fail('Invalid change direction')
        body['starts_at'], body['expires_at'] = iso(body['starts_at']), iso(body['expires_at'])
        start, end = body['starts_at'], body['expires_at']
        if datetime.fromisoformat(start) >= datetime.fromisoformat(end):
            fail('Invalid aggregate dates')
        if not isinstance(body['periods'], list) or len(body['periods']) > 10000:
            fail('Invalid provider periods')
        if not body['periods'] and body['type'] not in {'CLOSE', 'ADJUST', 'STATUS'}:
            fail('Active rights require periods')
        seen = set()
        for row in body['periods']:
            if not isinstance(row, dict) or set(row) != PERIOD:
                fail('Invalid period fields')
            if not all(valid_text(row[k]) for k in ('order_id', 'order_no', 'term_id')):
                fail('Invalid period identity')
            row['starts_at'], row['expires_at'] = iso(row['starts_at']), iso(row['expires_at'])
            lower, upper = row['starts_at'], row['expires_at']
            if not (datetime.fromisoformat(start) <= datetime.fromisoformat(lower)
                    < datetime.fromisoformat(upper) <= datetime.fromisoformat(end)):
                fail('Invalid period dates')
            identity = (row['order_id'], row['term_id'], lower, upper)
            if identity in seen:
                fail('Duplicate provider period')
            seen.add(identity)
            quotas = row['quotas']
            if (not isinstance(quotas, dict) or set(quotas) != set(QUOTAS)
                    or any(type(v) is not int or v < 0 for v in quotas.values())):
                fail('Invalid frozen quotas')
        return body
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise HTTPException(409, 'Invalid provider frozen payload') from exc


def period_snapshot(order, term, starts_at, expires_at, quotas):
    """Project an already verified quota row; callers enumerate ALL target rows."""
    if term.order_id != order.id:
        fail('Provider period order mismatch')
    return dict(order_id=order.id, order_no=order.order_no, term_id=term.id,
        starts_at=iso(starts_at), expires_at=iso(expires_at), quotas={k: quotas[k] for k in QUOTAS})


def frozen_snapshot(order, term, combination, *, event_type, operation_key, periods,
                    starts_at, expires_at, tenant_id=None, version=None, **optional):
    if (order.product_id != combination.product_id or order.buyer_enterprise_id != combination.enterprise_id
            or term.order_id != order.id or term.combination_id != combination.id
            or term.generation != combination.generation):
        fail('Provider snapshot identity mismatch')
    identity = [order.product_id, order.buyer_enterprise_id, operation_key]
    return validate_payload(dict(schema_version=1, type=event_type,
        event_id=hashlib.sha256(canonical(identity).encode()).hexdigest(),
        idempotency_key=operation_key, product_id=order.product_id,
        enterprise_id=order.buyer_enterprise_id, combination_id=combination.id,
        generation=term.generation, tenant_id=tenant_id, order_id=order.id, order_no=order.order_no,
        version=version or {'id': term.version_id, 'code': term.version_code},
        subscription_months=term.months, starts_at=iso(starts_at), expires_at=iso(expires_at),
        periods=periods, **optional))


def install(ns):
    if 'api_provider_lifecycle' in ns:
        return ns['api_provider_lifecycle']
    Base = ns['Base']

    class APIProviderPlan(Base):
        __tablename__ = 'api_provider_plans'
        id = Column(String(64), primary_key=True)
        event_id = Column(String(200), nullable=False, unique=True)
        operation_key = Column(String(128), nullable=False)
        enterprise_id = Column(ForeignKey('enterprises.id'), nullable=False)
        product_id = Column(ForeignKey('products.id'), nullable=False)
        endpoint = Column(String(500), nullable=False)
        body_json = Column(Text, nullable=False)
        __table_args__ = (UniqueConstraint('enterprise_id', 'product_id', 'operation_key'),)

        @property
        def management_url(self):
            return self.endpoint

        @property
        def payload(self):
            return json.loads(self.body_json)

        @property
        def response(self):
            session = object_session(self)
            receipt = session.get(APIProviderReceipt, self.id) if session else None
            return json.loads(receipt.evidence_json) if receipt else None

        @property
        def status(self):
            return 'verified' if self.response is not None else 'prepared'

    class APIProviderTenant(Base):
        __tablename__ = 'api_provider_tenants'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        enterprise_id = Column(ForeignKey('enterprises.id'), nullable=False)
        product_id = Column(ForeignKey('products.id'), nullable=False)
        combination_id = Column(String(36), nullable=False)
        tenant_id = Column(String(200), nullable=True)
        __table_args__ = (UniqueConstraint('enterprise_id', 'product_id'),)

    class APIProviderReceipt(Base):
        __tablename__ = 'api_provider_receipts'
        plan_id = Column(ForeignKey('api_provider_plans.id'), primary_key=True)
        evidence_json = Column(Text, nullable=False)
        recorded_at = Column(UTCDateTime(), nullable=False, default=lambda: datetime.now(timezone.utc))

    class APIProviderExpiryAttempt(Base):
        __tablename__ = 'api_provider_expiry_attempts'
        plan_id = Column(ForeignKey('api_provider_plans.id'), primary_key=True)
        status = Column(String(24), nullable=False, default='prepared')
        attempts = Column(Integer, nullable=False, default=0)
        next_retry_at = Column(UTCDateTime(), nullable=True)
        last_error = Column(String(100), nullable=False, default='')

    def immutable(mapper, connection, target):
        raise ValueError('Provider operation plans and receipts are immutable')

    for model in (APIProviderPlan, APIProviderReceipt):
        event.listen(model, 'before_update', immutable)
        event.listen(model, 'before_delete', immutable)

    def target(db, endpoint):
        try:
            ns['application_address_policy']['validate_url'](endpoint, db=db)
            ns['validate_upstream_url'](endpoint)
            parsed = urlsplit(endpoint)
            if (not parsed.hostname or parsed.username or parsed.password or parsed.query
                    or parsed.fragment or parsed.port == 0):
                fail('Invalid provider management endpoint')
        except (ValueError, KeyError) as exc:
            raise HTTPException(409, 'Invalid provider management endpoint') from exc

    def binding(db, body):
        row = db.scalar(select(APIProviderTenant).where(
            APIProviderTenant.enterprise_id == body['enterprise_id'],
            APIProviderTenant.product_id == body['product_id']).with_for_update())
        if row and row.combination_id != body['combination_id']:
            fail('Provider tenant combination mismatch')
        return row

    def product_target(db, body, endpoint):
        product = db.get(ns['Product'], body['product_id'])
        if not product or product.delivery_method not in {'api', 'model_api'}:
            fail('Invalid API provider product')
        # The order's delivery snapshot owns the endpoint. Live registration must
        # never turn a legacy unmanaged purchase into a managed purchase.
        if not endpoint:
            return None
        if not isinstance(endpoint, str) or endpoint != endpoint.strip():
            fail('Invalid frozen provider endpoint')
        target(db, endpoint)
        return product

    def prepare(db, payload, management_url):
        body = validate_payload(payload)
        if product_target(db, body, management_url) is None:
            return dict(SKIPPED), False
        row = binding(db, body)
        if row and body['tenant_id'] is not None and row.tenant_id != body['tenant_id']:
            fail('Provider tenant mismatch')
        plan_id = hashlib.sha256(canonical([body['product_id'], body['enterprise_id'], body['idempotency_key']]).encode()).hexdigest()
        old = db.get(APIProviderPlan, plan_id)
        if old:
            if old.body_json != canonical(body) or old.endpoint != management_url:
                fail('Provider operation key has a different immutable plan')
            return old, False
        collision = db.scalar(select(APIProviderPlan).where(APIProviderPlan.event_id == body['event_id']))
        if collision:
            fail('Provider event ID already belongs to another plan')
        plan = APIProviderPlan(id=plan_id, event_id=body['event_id'], operation_key=body['idempotency_key'],
            enterprise_id=body['enterprise_id'], product_id=body['product_id'],
            endpoint=management_url, body_json=canonical(body))
        db.add(plan)
        db.flush()
        return plan, True

    def verify(result, body):
        if not isinstance(result, dict) or type(result.get('ret')) is not int or result['ret'] != 0:
            fail()
        for key in (ROOT - {'tenant_id', 'starts_at', 'expires_at', 'periods'}) | OPTIONAL.intersection(body):
            if canonical(result.get(key)) != canonical(body[key]):
                fail()
        for key in ('starts_at', 'expires_at'):
            if iso(result.get(key)) != body[key]:
                fail()
        remote_id = result.get('tenant_id')
        if remote_id is not None and not valid_text(remote_id):
            fail()
        if body['tenant_id'] is not None and body['tenant_id'] != remote_id:
            fail()
        actual = validate_payload(dict(body, periods=result.get('periods')))
        if canonical(actual['periods']) != canonical(body['periods']):
            fail()
        effective_at = iso(result.get('effective_at'))
        expected = 'closed' if body['type'] == 'CLOSE' else 'active'
        if body['type'] == 'STATUS':
            if result.get('status') not in {'closed', 'active'}:
                fail()
        elif result.get('status') != expected:
            fail()
        return dict(body, ret=0, tenant_id=remote_id, status=result['status'], effective_at=effective_at)

    def execute(db, record, management_url=None, oauth_adapter=None):
        """Execute committed Plan, or durable payload with explicit endpoint."""
        if isinstance(record, dict):
            if record == SKIPPED:
                return dict(SKIPPED)
            record, _ = prepare(db, record, management_url)
            if isinstance(record, dict):
                return record
        with Session(db.get_bind()) as journal:
            if (journal.connection().connection.driver_connection
                    is db.connection().connection.driver_connection):
                fail('Provider plan verification requires an independent database connection')
            durable = journal.get(APIProviderPlan, record.id)
            if not durable or durable.body_json != record.body_json or durable.endpoint != record.endpoint:
                fail('Commit provider operation plan before execute')
        body = json.loads(record.body_json)
        product_target(db, body, record.endpoint)
        tenant = binding(db, body)
        client = db.scalar(select(ns['OAuthClient']).where(
            ns['OAuthClient'].product_id == record.product_id, ns['OAuthClient'].status == 'active'))
        if (not client or 'resource.manage' not in (client.scope or '').split()
                or not client.client_id or not client.client_secret):
            fail('Active product machine OAuth resource.manage is required')
        config = SimpleNamespace(id='api-provider:' + client.id, client_id=client.client_id,
            client_secret=client.client_secret, scope='resource.manage', timeout_ms=10000)
        try:
            token = (oauth_adapter or ns['saas_access_token'])(config)
            def call(request_body, key):
                response = ns['validated_http_request']('POST', record.endpoint, timeout=10,
                    follow_redirects=False, headers={'Authorization': 'Bearer ' + token,
                        'Content-Type': 'application/json', 'Idempotency-Key': key},
                    content=canonical(request_body).encode())
                if response.status_code == 401:
                    evict = ns.get('_evict_saas_token')
                    if evict is not None:
                        evict(config.id, token)
                if not 200 <= response.status_code < 300:
                    fail()
                return verify(response.json(), body)
            applied = call(body, body['idempotency_key'])
            actual = applied if body['type'] == 'STATUS' else call(
                dict(body, type='STATUS', target_type=body['type']), body['idempotency_key'] + ':status')
            if canonical(applied) != canonical(actual):
                fail()
            if tenant and tenant.tenant_id and tenant.tenant_id != actual['tenant_id']:
                fail()
            receipt = db.get(APIProviderReceipt, record.id)
            if receipt and receipt.evidence_json != canonical(actual):
                fail('Provider receipt changed on replay')
        except Exception as exc:
            raise HTTPException(502, 'Provider lifecycle operation failed verification') from exc
        if tenant is None:
            db.add(APIProviderTenant(enterprise_id=record.enterprise_id, product_id=record.product_id,
                combination_id=body['combination_id'], tenant_id=actual['tenant_id']))
        elif actual['tenant_id']:
            tenant.tenant_id = actual['tenant_id']
        if receipt is None:
            db.add(APIProviderReceipt(plan_id=record.id, evidence_json=canonical(actual)))
            audit = ns.get('audit')
            if callable(audit):
                evidence = {key: actual[key] for key in (
                    'type', 'event_id', 'product_id', 'enterprise_id', 'combination_id',
                    'generation', 'tenant_id', 'order_id', 'order_no', 'version',
                    'starts_at', 'expires_at', 'periods', 'status', 'effective_at')}
                audit(db, 'api-provider-worker', 'api_provider_verified',
                    'api_provider_receipt', record.id, category='delivery',
                    business_domain='trading', tenant_id=body['enterprise_id'],
                    order_id=body['order_id'], after=evidence)
        db.flush()
        return actual

    def delivery(db, order, term, combination):
        key = 'delivery:' + order.id
        plan_id = hashlib.sha256(canonical([order.product_id, order.buyer_enterprise_id, key]).encode()).hexdigest()
        plan = db.get(APIProviderPlan, plan_id)
        if plan:
            body = json.loads(plan.body_json)
            if (body['order_id'] != order.id or body['combination_id'] != combination.id
                    or body['generation'] != term.generation or term.order_id != order.id
                    or combination.enterprise_id != order.buyer_enterprise_id
                    or combination.product_id != order.product_id):
                fail('Provider delivery plan identity mismatch')
            return execute(db, plan)
        delivery_snapshot = ns['order_economics']['delivery_snapshot'](order)
        if not (delivery_snapshot.get('integration_api_url') or '').strip():
            return dict(SKIPPED)
        fail('Stage provider delivery plan before delivery')

    def prepare_change(db, order, term, combination, direction, frozen_context):
        context = dict(frozen_context)
        key = context.pop('operation_key')
        body = frozen_snapshot(order, term, combination, event_type='CHANGE',
            operation_key=key, direction=direction, **context)
        endpoint = ns['order_economics']['delivery_snapshot'](order).get('integration_api_url') or ''
        return prepare(db, body, endpoint)

    def prepare_adjustment(db, order, term, combination, event_type, frozen_context):
        if event_type not in {'ADJUST', 'CLOSE', 'RESTORE'}:
            fail('Invalid refund adjustment event')
        context = dict(frozen_context)
        key = context.pop('operation_key')
        body = frozen_snapshot(order, term, combination, event_type=event_type, operation_key=key, **context)
        endpoint = ns['order_economics']['delivery_snapshot'](order).get('integration_api_url') or ''
        return prepare(db, body, endpoint)

    def get_operation(db, event_id):
        return db.scalar(select(APIProviderPlan).where(APIProviderPlan.event_id == event_id))

    def pending_operations(db, enterprise_id, product_id, combination_id=None):
        plans = db.scalars(select(APIProviderPlan).outerjoin(APIProviderReceipt,
            APIProviderReceipt.plan_id == APIProviderPlan.id).where(
                APIProviderPlan.enterprise_id == enterprise_id,
                APIProviderPlan.product_id == product_id,
                APIProviderReceipt.plan_id.is_(None))).all()
        return [p for p in plans if combination_id is None or p.payload['combination_id'] == combination_id]

    def expire_once(db, *, blocked, at=None):
        """One worker tick; caller MUST commit returned state, including failures.

        blocked(db, frozen_body) must return literal False to authorize closure;
        main checks pending delivery/refund/version work under combination locks.
        Ticks stage, reserve an attempt, then dispatch. A five-minute reservation
        lease bounds crash recovery; expired reservations count as failed attempts.
        Normal failures retry after ten seconds. The blocker owns active-work
        checks: unused compensation plans are not evidence of an active saga.
        No internal commits and no product-wide or financial changes.
        """
        at = datetime.fromisoformat(iso(at or datetime.now(timezone.utc)))
        results = []
        tenants = db.scalars(select(APIProviderTenant).with_for_update()).all()
        for tenant in tenants:
            receipts = db.scalars(select(APIProviderReceipt).join(APIProviderPlan,
                APIProviderPlan.id == APIProviderReceipt.plan_id).where(
                    APIProviderPlan.enterprise_id == tenant.enterprise_id,
                    APIProviderPlan.product_id == tenant.product_id).order_by(
                        APIProviderReceipt.recorded_at.desc())).all()
            receipts = [r for r in receipts if json.loads(r.evidence_json)['combination_id'] == tenant.combination_id]
            if not receipts:
                continue
            if len(receipts) > 1 and receipts[0].recorded_at == receipts[1].recorded_at:
                results.append({'status': 'needs_manual', 'reason': 'ambiguous_latest_receipt',
                                'combination_id': tenant.combination_id})
                continue
            actual = json.loads(receipts[0].evidence_json)
            if actual['status'] == 'closed' or datetime.fromisoformat(actual['expires_at']) > at:
                continue
            source = db.get(APIProviderPlan, receipts[0].plan_id)
            key = 'expiry:' + hashlib.sha256(canonical([actual['combination_id'],
                actual['generation'], actual['event_id'], actual['expires_at']]).encode()).hexdigest()
            body = {k: actual[k] for k in ROOT}
            body.update(type='CLOSE', event_id=key, idempotency_key=key, periods=[])
            if blocked(db, body) is not False:
                results.append({'status': 'blocked', 'combination_id': tenant.combination_id})
                continue
            plan, created = prepare(db, body, source.endpoint)
            attempt = db.get(APIProviderExpiryAttempt, plan.id)
            if attempt is None:
                attempt = APIProviderExpiryAttempt(plan_id=plan.id, status='prepared', attempts=0)
                db.add(attempt)
                db.flush()
                results.append({'status': 'prepared', 'event_id': key})
                continue
            if attempt.status in {'needs_manual', 'verified'}:
                results.append({'status': attempt.status, 'event_id': key})
                continue
            if attempt.status == 'reserved' and attempt.next_retry_at <= at:
                attempt.status = 'needs_manual' if attempt.attempts >= 3 else 'prepared'
                attempt.last_error = 'attempt_lease_expired'
                attempt.next_retry_at = at + timedelta(seconds=10)
                results.append({'status': attempt.status, 'event_id': key})
                continue
            if attempt.status == 'prepared':
                if attempt.next_retry_at and attempt.next_retry_at > at:
                    continue
                if attempt.attempts >= 3:
                    attempt.status = 'needs_manual'
                else:
                    attempt.attempts += 1
                    attempt.status = 'reserved'
                    attempt.next_retry_at = at + timedelta(minutes=5)
                results.append({'status': attempt.status, 'event_id': key})
                continue
            # A reservation, like a plan, must survive rollback before dispatch.
            with Session(db.get_bind()) as journal:
                saved = journal.get(APIProviderExpiryAttempt, plan.id)
                if not saved or saved.status != 'reserved' or saved.attempts != attempt.attempts:
                    fail('Commit expiry attempt reservation before dispatch')
            try:
                execute(db, plan)
                attempt.status, attempt.last_error = 'verified', ''
            except HTTPException:
                attempt.status = 'needs_manual' if attempt.attempts >= 3 else 'prepared'
                attempt.last_error = 'provider_verification_failed'
                attempt.next_retry_at = at + timedelta(seconds=10)
            results.append({'status': attempt.status, 'event_id': key, 'attempts': attempt.attempts})
        db.flush()
        return results

    result = dict(Plan=APIProviderPlan, Tenant=APIProviderTenant, Receipt=APIProviderReceipt,
        prepare=prepare, execute=execute, frozen_snapshot=frozen_snapshot, period_snapshot=period_snapshot,
        delivery=delivery, prepare_change=prepare_change, prepare_adjustment=prepare_adjustment)
    result.update(get_operation=get_operation, pending_operations=pending_operations,
                  expire_once=expire_once, ExpiryAttempt=APIProviderExpiryAttempt)
    ns['api_provider_lifecycle'] = result
    return result
