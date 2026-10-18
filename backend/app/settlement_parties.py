"""Participant configuration and cost-exception evidence; install before create_all.

Caller owns hook transactions. Freeze configuration(db, product_id) into
order.economic_snapshot_json['settlement_parties']; allocations never reads the
current product configuration. Neither hook grants cash authority or posts money.
"""
import hashlib
import json
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from fastapi import Depends, HTTPException, Request
from sqlalchemy import Column, String, Text, Integer, DateTime, ForeignKey, UniqueConstraint, event, select
from sqlalchemy.exc import IntegrityError

KINDS = {'platform', 'provider', 'service', 'expert', 'channel'}
OPERATORS = {'super_admin', 'platform_operator', 'finance_settlement'}
CENT = Decimal('.01')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def number(value, *, cents=False, positive=False):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or abs(n) > Decimal('999999999999.99'):
            raise ValueError()
        if cents and n != n.quantize(CENT) or positive and n <= 0:
            raise ValueError()
        return n.quantize(CENT) if cents else n
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(422, 'invalid_decimal') from None


def required(body, key, limit=180):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise HTTPException(422, key + '_required')
    return value.strip()


def install(ns):
    if 'settlement_parties' in ns:
        return ns['settlement_parties']
    Base = ns['Base']

    class Participant(Base):
        __tablename__ = 'settlement_participants'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        name = Column(String(180), nullable=False)
        enterprise_id = Column(String(36), nullable=False, default='')
        user_id = Column(String(36), nullable=False, default='')
        owner_enterprise_id = Column(String(36), nullable=False, index=True)
        kind = Column(String(20), nullable=False)
        currency = Column(String(12), nullable=False)
        creator_id = Column(String(36), nullable=False)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class Configuration(Base):
        __tablename__ = 'settlement_participant_configurations'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        product_id = Column(ForeignKey('products.id'), nullable=False, index=True)
        version = Column(Integer, nullable=False)
        allocations_json = Column(Text, nullable=False)
        creator_id = Column(String(36), nullable=False)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        __table_args__ = (UniqueConstraint('product_id', 'version'),)

    class CostException(Base):
        __tablename__ = 'settlement_cost_exceptions'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        refund_id = Column(ForeignKey('refunds.id'), nullable=False, unique=True)
        order_id = Column(String(36), nullable=False, index=True)
        provider_enterprise_id = Column(String(36), nullable=False, index=True)
        baseline_id = Column(String(36), nullable=False)
        payload_json = Column(Text, nullable=False)
        creator_id = Column(String(36), nullable=False)
        creator_side = Column(String(12), nullable=False)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class Decision(Base):
        __tablename__ = 'settlement_cost_exception_decisions'
        exception_id = Column(ForeignKey(CostException.id), primary_key=True)
        decision = Column(String(12), nullable=False)
        reason = Column(Text, nullable=False)
        reviewer_id = Column(String(36), nullable=False)
        created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class Idempotency(Base):
        __tablename__ = 'settlement_party_requests'
        actor_id = Column(String(36), primary_key=True)
        request_id = Column(String(180), primary_key=True)
        digest = Column(String(64), nullable=False)
        response_json = Column(Text, nullable=False)

    def immutable(*args):
        raise ValueError('settlement party evidence is immutable')

    for model in (Participant, Configuration, CostException, Decision, Idempotency):
        event.listen(model, 'before_update', immutable)
        event.listen(model, 'before_delete', immutable)

    def scope(db, user, request=None):
        if not user.is_active or getattr(user, 'activation_status', 'active') != 'active':
            raise HTTPException(403, 'active_user_required')
        if user.platform_role in OPERATORS:
            return ''
        if user.platform_role or user.verified_status != 'verified':
            raise HTTPException(403, 'settlement_party_scope_denied')
        from .console_business_scope import resolve_business_scope
        s = resolve_business_scope(request, db, user, ns['Membership'])
        m = s['membership']
        enterprise = db.get(ns['Enterprise'], s['enterprise_id'])
        if (s['mode'] == 'buyer' or m.status != 'active' or
                m.role not in {'super_admin', 'enterprise_admin'} or
                'provider' not in (m.business_roles or '').split(',') or
                not enterprise or enterprise.verification_status != 'verified'):
            raise HTTPException(403, 'verified_provider_admin_required')
        return enterprise.id

    def owned(db, model, identity, tenant, field):
        row = db.scalar(select(model).where(model.id == identity).with_for_update()
                        .execution_options(populate_existing=True))
        if not row or tenant and getattr(row, field) != tenant:
            raise HTTPException(404, 'scoped_object_not_found')
        return row

    def write(db, user, body, action, operation):
        key = required(body, 'request_id')
        payload = dict(body, _action=action)
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()

        def replay():
            old = db.get(Idempotency, (user.id, key))
            if old:
                if old.digest != digest:
                    raise HTTPException(409, 'idempotency_payload_mismatch')
                return json.loads(old.response_json)

        old = replay()
        if old is not None:
            return old
        try:
            with db.begin_nested():
                result = operation()
                db.add(Idempotency(actor_id=user.id, request_id=key, digest=digest,
                                   response_json=canonical(result)))
                ns['audit'](db, user.id, action, 'settlement_parties', result['id'],
                            category='settlement', business_domain='settlement',
                            after=dict(result, request_id=key))
                db.flush()
            return result
        except IntegrityError:
            old = replay()
            if old is not None:
                return old
            raise HTTPException(409, 'settlement_party_write_conflict') from None

    def participant_dict(p):
        return {key: getattr(p, key) for key in ('id', 'name', 'enterprise_id', 'user_id',
                'kind', 'currency', 'creator_id', 'owner_enterprise_id')}

    def create_participant(db, user, body, request=None):
        tenant = scope(db, user, request)

        def operation():
            name = required(body, 'name')
            kind = body.get('kind')
            if not isinstance(kind, str) or kind not in KINDS or body.get('currency') != 'CNY':
                raise HTTPException(422, 'invalid_kind_or_currency')
            eid, uid = body.get('enterprise_id') or '', body.get('user_id') or ''
            if not isinstance(eid, str) or not isinstance(uid, str) or len(eid) > 36 or len(uid) > 36:
                raise HTTPException(422, 'invalid_identity')
            if kind == 'platform' and tenant:
                raise HTTPException(403, 'platform_participant_requires_operator')
            if kind != 'platform' and not (eid or uid):
                raise HTTPException(422, 'real_identity_required')
            if eid and not db.get(ns['Enterprise'], eid) or uid and not db.get(ns['User'], uid):
                raise HTTPException(422, 'identity_not_found')
            if tenant and (eid != tenant or uid and not db.scalar(select(ns['Membership'].id).where(
                    ns['Membership'].user_id == uid, ns['Membership'].enterprise_id == tenant,
                    ns['Membership'].status == 'active'))):
                raise HTTPException(403, 'participant_identity_outside_scope')
            p = Participant(name=name, enterprise_id=eid, user_id=uid, kind=kind,
                            currency='CNY', creator_id=user.id, owner_enterprise_id=tenant)
            db.add(p)
            db.flush()
            return participant_dict(p)
        return write(db, user, body, 'create_settlement_participant', operation)

    def configuration(db, product_id):
        c = db.scalar(select(Configuration).where(Configuration.product_id == product_id)
                      .order_by(Configuration.version.desc()))
        return dict(id=c.id, product_id=c.product_id, version=c.version,
                    allocations=json.loads(c.allocations_json), creator_id=c.creator_id) if c else None

    def configure(db, user, product_id, body, request=None):
        tenant = scope(db, user, request)
        owned(db, ns['Product'], product_id, tenant, 'enterprise_id')

        def operation():
            rows = body.get('allocations')
            if not isinstance(rows, list):
                raise HTTPException(422, 'allocations_required')
            result, totals, seen = [], {}, set()
            for a in rows:
                if not isinstance(a, dict):
                    raise HTTPException(422, 'invalid_allocation')
                pid, kind = required(a, 'participant_id'), a.get('participant_type')
                p = db.get(Participant, pid)
                if (not p or not isinstance(kind, str) or kind not in KINDS or kind != p.kind or
                        tenant and p.owner_enterprise_id not in ('', tenant)):
                    raise HTTPException(422, 'invalid_participant_identity_or_kind')
                if (kind, pid) in seen:
                    raise HTTPException(422, 'duplicate_participant')
                seen.add((kind, pid))
                weight = number(a.get('weight'), positive=True)
                totals[kind] = totals.get(kind, Decimal(0)) + weight
                result.append(dict(participant_id=pid, participant_type=kind,
                                   participant_name=p.name, currency=p.currency, weight=str(weight)))
            if any(total != 100 for total in totals.values()):
                raise HTTPException(422, 'category_weights_must_equal_100')
            previous = configuration(db, product_id)
            c = Configuration(product_id=product_id, version=previous['version'] + 1 if previous else 1,
                              allocations_json=canonical(sorted(result, key=lambda a: (a['participant_type'], a['participant_id']))),
                              creator_id=user.id)
            db.add(c)
            db.flush()
            return configuration(db, product_id)
        return write(db, user, body, 'configure_settlement_participants:' + product_id, operation)

    def allocations(db, order, participant_type, amount):
        if participant_type not in KINDS | {'cost'}:
            raise HTTPException(422, 'invalid_participant_type')
        amount = number(amount, cents=True)
        if not amount:
            return []
        try:
            snapshot = json.loads(order.economic_snapshot_json or '{}')
        except (ValueError, TypeError):
            raise HTTPException(409, 'invalid_economic_snapshot') from None
        currency = snapshot.get('currency', 'CNY')
        if currency != 'CNY':
            raise HTTPException(409, 'unsupported_currency')
        if participant_type == 'cost':
            p = db.get(ns['Enterprise'], order.provider_enterprise_id)
            if not p:
                raise HTTPException(409, 'order_provider_required')
            return [dict(participant_id=p.id, participant_name=p.name, amount=str(amount), currency=currency)]
        frozen = snapshot.get('settlement_parties')
        rows = []
        if frozen:
            c = db.get(Configuration, frozen.get('id')) if isinstance(frozen, dict) else None
            if not c or c.product_id != order.product_id or c.version != frozen.get('version'):
                raise HTTPException(409, 'frozen_configuration_mismatch')
            rows = [a for a in json.loads(c.allocations_json) if a['participant_type'] == participant_type]
        if not rows:
            if participant_type == 'provider':
                return allocations(db, order, 'cost', amount)
            if participant_type == 'platform':
                return [dict(participant_id='platform', participant_name='Platform', amount=str(amount),
                             currency=currency, internal=True)]
            return [dict(participant_id='', participant_name='Unassigned', amount=str(amount),
                         currency=currency, unassigned=True, blocked_reason='participant_unassigned')]
        cents = int(abs(amount) * 100)
        raw = [Decimal(cents) * Decimal(a['weight']) / 100 for a in rows]
        units = [int(n.to_integral_value(rounding=ROUND_DOWN)) for n in raw]
        for i in sorted(range(len(rows)), key=lambda i: (-(raw[i] - units[i]), rows[i]['participant_id']))[:cents - sum(units)]:
            units[i] += 1
        sign = 1 if amount >= 0 else -1
        result = [dict(participant_id=a['participant_id'], participant_name=a['participant_name'],
                     amount=str((Decimal(units[i] * sign) / 100).quantize(CENT)), currency=currency)
                for i, a in enumerate(rows)]
        if participant_type == 'platform':
            for row in result:
                row['internal'] = True
        return result

    def exception_dict(db, e):
        d = db.get(Decision, e.id)
        return dict(json.loads(e.payload_json), id=e.id, refund_id=e.refund_id, order_id=e.order_id,
                    baseline_id=e.baseline_id, creator_id=e.creator_id, creator_side=e.creator_side,
                    version=2 if d else 1, status=d.decision + 'd' if d and d.decision == 'approve'
                    else 'rejected' if d else 'pending', reviewer_id=d.reviewer_id if d else None,
                    decision_reason=d.reason if d else None)

    def baseline(db, order):
        model = ns.get('settlement_finance', {}).get('Baseline')
        if model is None:
            raise HTTPException(409, 'final_baseline_required')
        query = select(model).where(model.order_id == order.id, model.revenue > 0)
        if ns.get('Settlement') is not None:
            query = query.join(ns['Settlement'], ns['Settlement'].id == model.settlement_id).where(
                ns['Settlement'].status != 'superseded')
        rows = db.scalars(query.with_for_update(of=model).execution_options(populate_existing=True)).all()
        if len(rows) != 1 or rows[0].currency != 'CNY' or rows[0].cost <= 0:
            raise HTTPException(409, 'unique_positive_final_baseline_required')
        return rows[0]

    def validate_exception(db, e, order):
        b = baseline(db, order)
        if b.settlement_id != e.baseline_id:
            raise HTTPException(409, 'baseline_changed')
        payload = json.loads(e.payload_json)
        approved = db.scalars(select(CostException).join(Decision, Decision.exception_id == CostException.id).where(
            CostException.baseline_id == e.baseline_id, Decision.decision == 'approve', CostException.id != e.id)).all()
        if number(payload['retained_cost']) + sum(number(json.loads(x.payload_json)['retained_cost']) for x in approved) > b.cost:
            raise HTTPException(409, 'retained_cost_exceeds_baseline')
        model = ns['settlement_finance']['Baseline']
        if db.scalar(select(model.settlement_id).where(model.reference_settlement_id == b.settlement_id)):
            raise HTTPException(409, 'existing_reversal_requires_explicit_adjustment')

    def create_exception(db, user, body, request=None):
        tenant = scope(db, user, request)
        refund = owned(db, ns['Refund'], required(body, 'refund_id'), '', 'order_id')
        order = owned(db, ns['Order'], refund.order_id, tenant, 'provider_enterprise_id')

        def operation():
            if refund.status not in ('approved', 'completed'):
                raise HTTPException(409, 'approved_or_completed_refund_required')
            b = baseline(db, order)
            retained = number(body.get('retained_cost'), cents=True, positive=True)
            evidence = body.get('evidence_refs')
            if not isinstance(evidence, list) or not evidence or any(not isinstance(x, str) or not x.strip() for x in evidence):
                raise HTTPException(422, 'evidence_refs_required')
            bearer = db.get(Participant, required(body, 'bearer_participant_id'))
            if not bearer or bearer.currency != b.currency or bearer.owner_enterprise_id not in ('', order.provider_enterprise_id):
                raise HTTPException(422, 'invalid_bearer')
            e = CostException(refund_id=refund.id, order_id=order.id,
                provider_enterprise_id=order.provider_enterprise_id, baseline_id=b.settlement_id,
                creator_id=user.id, creator_side='provider' if tenant else 'platform',
                payload_json=canonical(dict(retained_cost=str(retained), currency=b.currency,
                    reason=required(body, 'reason', 4000), evidence_refs=evidence,
                    bearer_participant_id=bearer.id, bearer_participant_name=bearer.name)))
            validate_exception(db, e, order)
            db.add(e)
            db.flush()
            return exception_dict(db, e)
        return write(db, user, body, 'create_settlement_cost_exception', operation)

    def decide(db, user, identity, body, request=None):
        tenant = scope(db, user, request)
        e = owned(db, CostException, identity, tenant, 'provider_enterprise_id')

        def operation():
            if user.id == e.creator_id or ('provider' if tenant else 'platform') == e.creator_side:
                raise HTTPException(403, 'opposite_party_required')
            if type(body.get('expected_version')) is not int or body['expected_version'] != 1 or db.get(Decision, e.id):
                raise HTTPException(409, 'exception_version_conflict')
            decision = body.get('decision')
            if decision not in ('approve', 'reject'):
                raise HTTPException(422, 'invalid_decision')
            reason = required(body, 'reason', 4000)
            order = owned(db, ns['Order'], e.order_id, tenant, 'provider_enterprise_id')
            if order.provider_enterprise_id != e.provider_enterprise_id:
                raise HTTPException(409, 'provider_changed')
            if decision == 'approve':
                validate_exception(db, e, order)
            db.add(Decision(exception_id=e.id, decision=decision, reason=reason, reviewer_id=user.id))
            db.flush()
            return exception_dict(db, e)
        return write(db, user, body, 'decide_settlement_cost_exception:' + identity, operation)

    def cost_exception(db, refund_id):
        e = db.scalar(select(CostException).join(Decision, Decision.exception_id == CostException.id).where(
            CostException.refund_id == refund_id, Decision.decision == 'approve'))
        return exception_dict(db, e) if e else None

    app = ns.get('app')
    if app is not None:
        @app.get('/api/settlement-parties')
        def list_endpoint(request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            tenant = scope(db, user, request)
            query = select(Participant)
            if tenant:
                query = query.where(Participant.owner_enterprise_id == tenant)
            rows = db.scalars(query.order_by(Participant.id)).all()
            return dict(items=[participant_dict(p) for p in rows], total=len(rows))

        @app.post('/api/settlement-parties')
        def create_endpoint(body: dict, request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            result = create_participant(db, user, body, request)
            db.commit()
            return result

        @app.get('/api/settlement-parties/products/{product_id}/allocations')
        def get_config_endpoint(product_id: str, request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            tenant = scope(db, user, request)
            owned(db, ns['Product'], product_id, tenant, 'enterprise_id')
            return configuration(db, product_id) or dict(product_id=product_id, version=0, allocations=[])

        @app.post('/api/settlement-parties/products/{product_id}/allocations')
        def config_endpoint(product_id: str, body: dict, request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            result = configure(db, user, product_id, body, request)
            db.commit()
            return result

        @app.get('/api/settlement-cost-exceptions')
        def list_exceptions_endpoint(request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            tenant = scope(db, user, request)
            query = select(CostException)
            if tenant:
                query = query.where(CostException.provider_enterprise_id == tenant)
            rows = db.scalars(query.order_by(CostException.id)).all()
            return dict(items=[exception_dict(db, e) for e in rows], total=len(rows))

        @app.post('/api/settlement-cost-exceptions')
        def exception_endpoint(body: dict, request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            result = create_exception(db, user, body, request)
            db.commit()
            return result

        @app.post('/api/settlement-cost-exceptions/{exception_id}/decision')
        def decision_endpoint(exception_id: str, body: dict, request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            result = decide(db, user, exception_id, body, request)
            db.commit()
            return result

    hooks = dict(Participant=Participant, Configuration=Configuration, CostException=CostException,
                 Decision=Decision, Idempotency=Idempotency, configuration=configuration,
                 allocations=allocations, cost_exception=cost_exception, create_participant=create_participant,
                 configure=configure, create_exception=create_exception, decide=decide)
    ns['settlement_parties'] = hooks
    return hooks
