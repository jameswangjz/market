"""Participant configuration and cost-exception evidence; install before create_all.

Caller owns hook transactions. Freeze configuration(db, product_id) into
order.economic_snapshot_json['settlement_parties']; allocations never reads the
current product configuration. Neither hook grants cash authority or posts money.
"""
import hashlib
import json
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP

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

    def owned(db, model, identity, tenant, field, *, lock=True):
        query = select(model).where(model.id == identity)
        row = db.scalar((query.with_for_update() if lock else query)
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
        payload = json.loads(e.payload_json)
        if payload.get('schema_version') == 2:
            payload['references'] = [a['basis'] for a in payload['source_allocations']]
        return dict(payload, id=e.id, refund_id=e.refund_id, order_id=e.order_id,
                    baseline_id=e.baseline_id, creator_id=e.creator_id, creator_side=e.creator_side,
                    version=2 if d else 1, status=d.decision + 'd' if d and d.decision == 'approve'
                    else 'rejected' if d else 'pending', reviewer_id=d.reviewer_id if d else None,
                    decision_reason=d.reason if d else None,
                    approved_at=(d.created_at.replace(tzinfo=timezone.utc).isoformat()
                                 if d and d.decision == 'approve' else None))

    def baseline(db, order, refund=None):
        if refund is not None:
            refund = db.scalar(select(ns['Refund']).where(ns['Refund'].id == refund.id)
                .with_for_update().execution_options(populate_existing=True))
        model = ns.get('settlement_finance', {}).get('Baseline')
        if model is None:
            raise HTTPException(409, 'final_baseline_required')
        query = select(model).where(model.order_id == order.id, model.revenue > 0)
        if ns.get('Settlement') is not None:
            query = query.join(ns['Settlement'], ns['Settlement'].id == model.settlement_id).where(
                ns['Settlement'].status != 'superseded')
        rows = db.scalars(query.order_by(model.settlement_id).with_for_update(of=model)
                          .execution_options(populate_existing=True)).all()
        adjustment = ns.get('VersionChangeAdjustment')
        revised = refund is not None and adjustment is not None and db.scalar(select(adjustment.id).where(
            adjustment.source_order_id == order.id, adjustment.status == 'posted')) is not None
        if revised:
            try:
                if refund.status != 'completed' or refund.order_id != order.id:
                    raise ValueError('completed refund required')
                hooks = ns['refund_settlement']
                link = db.get(hooks['Model'], refund.id)
                if not link or hashlib.sha256(link.evidence_json.encode()).hexdigest() != link.evidence_hash:
                    raise ValueError('exact refund link required')
                negatives = db.scalars(select(ns['Settlement']).where(ns['Settlement'].refund_id == refund.id)).all()
                if (len(negatives) != 1 or negatives[0].id != link.settlement_id
                        or negatives[0].order_id != order.id or not negatives[0].is_refund):
                    raise ValueError('unique refund settlement required')
                negative = negatives[0]
                # Revalidate immutable completion/resource/funding evidence without
                # re-evaluating the exception or creating any financial posting.
                if hooks['negative_settlement'](db, refund).id != negative.id:
                    raise ValueError('completion mismatch')
                assessed = hooks['assessment'](db, negative.id)
                plan = json.loads(link.evidence_json)['economic_plan']
                segments = plan.get('segments', [plan])
                if len(segments) != 1 or segments[0].get('calculation_version') != 'economic-segment-refund-v1':
                    raise ValueError('single exact segment required')
                vc = ns['version_change_settlement']
                ordinary = vc['refund_basis'](db, refund, segments[0], apply_exception=False)
                if ordinary.get('status') != 'ready':
                    raise ValueError('ready ordinary assessment required')
                keys = ('reference_settlement_id', 'baseline_hash', 'segment_id', 'window_hash', 'segment_context_hash')
                if ((assessed.get('status') == 'ready' and any(assessed.get(k) != ordinary.get(k) for k in keys))
                        or negative.reference_settlement_id not in ('', ordinary['reference_settlement_id'])):
                    raise ValueError('assessment target mismatch')
                bindings = db.scalars(select(vc['SegmentBaselineLink']).where(
                    vc['SegmentBaselineLink'].settlement_id == ordinary['reference_settlement_id'])).all()
                if (len(bindings) != 1 or bindings[0].window_id != ordinary['segment_id']
                        or bindings[0].baseline_hash != ordinary['baseline_hash']):
                    raise ValueError('unique target binding required')
                rows = [b for b in rows if b.settlement_id == ordinary['reference_settlement_id']
                        and b.snapshot_hash == ordinary['baseline_hash']]
            except (HTTPException, ValueError, KeyError, TypeError, AttributeError):
                raise HTTPException(409, 'exact_completed_segment_final_baseline_required') from None
        if len(rows) != 1 or rows[0].currency != 'CNY' or rows[0].cost <= 0:
            raise HTTPException(409, 'unique_positive_final_baseline_required')
        return rows[0]

    def ordinary_segment_transfer(db, b):
        """A verified VC transfer is not a supplement; funded ancestry never is."""
        vc = ns.get('version_change_settlement', {})
        context_hook, Binding = vc.get('financial_context'), vc.get('SegmentBaselineLink')
        if not callable(context_hook) or Binding is None:
            return False
        Baseline = ns['settlement_finance']['Baseline']
        Proof = ns['settlement_finance'].get('FundingProof')
        seen = set()
        try:
            while b is not None:
                if b.settlement_id in seen or Proof is not None and db.get(Proof, b.settlement_id):
                    return False
                seen.add(b.settlement_id)
                snapshot = json.loads(b.snapshot_json)
                if hashlib.sha256(b.snapshot_json.encode()).hexdigest() != b.snapshot_hash:
                    return False
                bindings = db.scalars(select(Binding).where(Binding.settlement_id == b.settlement_id)).all()
                if not bindings:
                    receipts = snapshot.get('actual_receipts')
                    return len(seen) > 1 and receipts is not None and number(receipts) == number(b.revenue)
                if len(bindings) != 1:
                    return False
                binding = bindings[0]
                context = context_hook(db, db.get(ns['Settlement'], b.settlement_id))
                if (context.get('status') != 'ready' or context.get('side') != 'target'
                        or context.get('context_hash') != snapshot.get('segment_context_hash')
                        or binding.context_hash != context['context_hash'] or binding.baseline_hash != b.snapshot_hash
                        or binding.source_settlement_id != context['source_settlement_id']):
                    return False
                projection = context.get('version_funding_projection')
                if projection:
                    FundingLink = ns['settlement_finance'].get('SegmentFundingLink')
                    describe = ns.get('settlement_version_funding', {}).get('describe')
                    if FundingLink is None or not callable(describe):
                        return False
                    links = db.scalars(select(FundingLink).where(FundingLink.target_settlement_id == b.settlement_id)).all()
                    if len(links) != 1 or links[0].context_hash != context['context_hash']:
                        return False
                    old_id = links[0].source_negative_id
                    old = db.get(ns['Settlement'], old_id)
                    evidence = describe(db, old_id)
                    # v2 returns unused supplement on the old side. This exact
                    # immutable bridge ends funding ancestry, not its cash claim.
                    return bool(old is not None and old.status in ('locked', 'paid')
                        and evidence is not None and old_id == projection['settlement_id']
                        and projection['source_settlement_id'] == context['source_settlement_id']
                        and projection['source_hash'] == context['source_baseline_hash']
                        and projection['window_id'] == context['window_id']
                        and projection['change_id'] == context['change_id']
                        and all(evidence.get(k) == v for k, v in projection.items()))
                b = db.get(Baseline, context['source_settlement_id'])
                if b is None or b.snapshot_hash != context['source_baseline_hash']:
                    return False
        except (HTTPException, ValueError, KeyError, TypeError, AttributeError):
            return False
        return False

    def validate_exception(db, e, order):
        unposted(db, e.refund_id)
        payload = json.loads(e.payload_json)
        if payload.get('schema_version') == 2:
            validate_multi(db, e, order, payload)
            return
        b = baseline(db, order, db.get(ns['Refund'], e.refund_id))
        unposted(db, e.refund_id)
        if b.settlement_id != e.baseline_id:
            raise HTTPException(409, 'baseline_changed')
        payload = json.loads(e.payload_json)
        if payload.get('baseline_hash') != getattr(b, 'snapshot_hash', None):
            raise HTTPException(409, 'baseline_changed')
        hooks = ns.get('refund_settlement', {})
        link_model = hooks.get('Model')
        link = db.get(link_model, e.refund_id) if link_model is not None else None
        if link:
            plan = json.loads(link.evidence_json)['economic_plan']
            segments = plan.get('segments', [plan])
            if len(segments) == 1 and segments[0].get('calculation_version') == 'economic-segment-refund-v1':
                ordinary = ns['version_change_settlement']['refund_basis'](
                    db, db.get(ns['Refund'], e.refund_id), segments[0], apply_exception=False)
                if ordinary.get('status') != 'ready' or number(payload['retained_cost']) > -number(ordinary['cost_amount']):
                    raise HTTPException(409, 'retained_cost_exceeds_refund_cost')
        if number(payload['retained_cost']) + approved_retained(db, b.settlement_id, e.id) > b.cost:
            raise HTTPException(409, 'retained_cost_exceeds_baseline')
        model = ns['settlement_finance']['Baseline']
        if db.scalar(select(model.settlement_id).where(model.reference_settlement_id == b.settlement_id)):
            raise HTTPException(409, 'existing_reversal_requires_explicit_adjustment')

    def approved_retained(db, baseline_id, exclude, *, pending=False):
        rows = db.execute(select(CostException, Decision.decision).outerjoin(
            Decision, Decision.exception_id == CostException.id).where(CostException.id != exclude)).all()
        reservations = []
        for row, decision in rows:
            payload = json.loads(row.payload_json)
            if decision == 'reject':
                continue
            if payload.get('schema_version') == 2:
                allocations = [a for a in payload['source_allocations'] if a['baseline_id'] == baseline_id]
                if allocations:
                    reservations.append((decision, payload.get('basis_kind'),
                        sum((number(a['retained_cost']) for a in allocations), Decimal(0))))
            elif row.baseline_id == baseline_id:
                reservations.append((decision, None, number(payload['retained_cost'])))
        # Preserve legacy-only proposal semantics, but once a funded source is
        # involved, every pending single/multi reservation shares its budget.
        count_pending = pending or any(kind == 'funded' for _, kind, _ in reservations)
        return sum((amount for decision, _, amount in reservations
                    if decision == 'approve' or count_pending), Decimal(0))

    def unposted(db, refund_id):
        finance = ns.get('settlement_finance', {})
        posting = finance.get('CostExceptionPosting')
        if posting is not None and db.get(posting, refund_id) is not None:
            raise HTTPException(409, 'existing_reversal_requires_explicit_adjustment')
        settlement, model = ns.get('Settlement'), finance.get('Baseline')
        if settlement is not None and model is not None and db.scalar(select(model.settlement_id).join(
                settlement, settlement.id == model.settlement_id).where(settlement.refund_id == refund_id)):
            raise HTTPException(409, 'existing_reversal_requires_explicit_adjustment')
        if settlement is not None and db.scalar(select(settlement.id).where(
                settlement.refund_id == refund_id, settlement.status.in_(('locked', 'paid')))):
            raise HTTPException(409, 'locked_refund_requires_explicit_adjustment')
        plan = ns.get('settlement_funded_refund', {}).get('Plan')
        if plan is not None and db.get(plan, refund_id) is not None:
            raise HTTPException(409, 'frozen_funded_refund_requires_explicit_adjustment')

    def raw_refs(db, refund, *, funded=False):
        hook = ((ns.get('settlement_funded_refund', {}).get('raw_funded_basis') or
                 ns.get('settlement_funded_cost_exception', {}).get('raw_funded_basis')) if funded
                else ns.get('refund_settlement', {}).get('raw_multi_basis'))
        if not callable(hook):
            raise HTTPException(409, 'raw_funded_refund_basis_unavailable' if funded else 'raw_multi_refund_basis_unavailable')
        result = hook(db, refund)
        refs = result.get('references') if isinstance(result, dict) and result.get('status') == 'ready' else None
        if not isinstance(refs, list) or len(refs) < (1 if funded else 2):
            raise HTTPException(409, 'ready_multi_refund_basis_required')
        if funded and (result.get('kind') != 'confirmed_funded_refund'
                       or number(result.get('buyer_cash_amount'), cents=True) != number(refund.amount, cents=True)):
            raise HTTPException(409, 'funded_refund_buyer_cash_basis_mismatch')
        keys = [(r.get('segment_id'), r.get('reference_settlement_id')) for r in refs if isinstance(r, dict)]
        if (len(keys) != len(refs) or any(not all(isinstance(v, str) and v for v in k) for k in keys)
                or len({k[0] for k in keys}) != len(keys) or len({k[1] for k in keys}) != len(keys)):
            raise HTTPException(409, 'unique_multi_refund_sources_required')
        return sorted(refs, key=lambda r: (r['reference_settlement_id'], r['segment_id']))

    def freeze_sources(db, refund, order, allocations, *, funded=False):
        refs = raw_refs(db, refund, funded=funded)
        if not isinstance(allocations, list) or len(allocations) != len(refs):
            raise HTTPException(422, 'all_source_allocations_required')
        supplied = {}
        for row in allocations:
            if not isinstance(row, dict):
                raise HTTPException(422, 'invalid_source_allocation')
            key = (required(row, 'segment_id'), required(row, 'baseline_id'))
            if key in supplied:
                raise HTTPException(422, 'duplicate_source_allocation')
            supplied[key] = row
        model = ns['settlement_finance']['Baseline']
        ids = sorted(r['reference_settlement_id'] for r in refs)
        # Shared ordered source locks serialize single and multi approval budgets.
        sources = {b.settlement_id: b for b in db.scalars(select(model).where(
            model.settlement_id.in_(ids)).order_by(model.settlement_id).with_for_update(of=model)
            .execution_options(populate_existing=True))}
        frozen = []
        for ref in refs:
            identity = ref['reference_settlement_id']
            row = supplied.get((ref['segment_id'], identity))
            b = sources.get(identity)
            if (row is None or b is None or b.order_id != order.id or b.currency != 'CNY' or b.revenue <= 0 or b.cost <= 0
                    or ref.get('source_order_id') != order.id or ref.get('currency') != b.currency
                    or not ref.get('baseline_hash') or ref['baseline_hash'] != getattr(b, 'snapshot_hash', None)
                    or getattr(b, 'provider_enterprise_id', None) != order.provider_enterprise_id):
                raise HTTPException(409, 'multi_refund_source_identity_mismatch')
            settlement = ns.get('Settlement')
            if settlement is not None:
                source_settlement = db.get(settlement, identity)
                if source_settlement is None or source_settlement.status not in ('locked', 'paid'):
                    raise HTTPException(409, 'multi_refund_source_not_final')
            amount = number(row.get('retained_cost'), cents=True)
            evidence = row.get('evidence_refs')
            if not isinstance(evidence, list) or not evidence or any(not isinstance(x, str) or not x.strip() for x in evidence):
                raise HTTPException(422, 'source_evidence_refs_required')
            cap = -number(ref.get('cost_amount'), cents=True)
            if amount < 0 or cap < 0 or amount > cap:
                raise HTTPException(409, 'retained_cost_exceeds_refund_cost')
            frozen.append(dict(segment_id=ref['segment_id'], baseline_id=identity, retained_cost=str(amount),
                evidence_refs=evidence, baseline_hash=b.snapshot_hash, basis=ref,
                basis_hash=hashlib.sha256(canonical(ref).encode()).hexdigest()))
        return frozen, sources

    def validate_multi(db, e, order, payload):
        unposted(db, e.refund_id)
        refund = db.get(ns['Refund'], e.refund_id)
        funded = payload.get('basis_kind') == 'funded'
        rows, sources = freeze_sources(db, refund, order, payload['source_allocations'], funded=funded)
        unposted(db, e.refund_id)
        if rows != payload['source_allocations'] or number(payload['refund_amount']) != number(refund.amount):
            raise HTTPException(409, 'multi_refund_basis_changed')
        if sum((number(a['retained_cost']) for a in rows), Decimal(0)) != number(payload['retained_cost']):
            raise HTTPException(409, 'retained_cost_total_mismatch')
        for row in rows:
            if number(row['retained_cost']) + approved_retained(db, row['baseline_id'], e.id, pending=funded) > sources[row['baseline_id']].cost:
                raise HTTPException(409, 'retained_cost_exceeds_baseline')

    def create_exception(db, user, body, request=None):
        tenant = scope(db, user, request)
        refund = owned(db, ns['Refund'], required(body, 'refund_id'), '', 'order_id')
        order = owned(db, ns['Order'], refund.order_id, tenant, 'provider_enterprise_id')

        def operation():
            if refund.status not in ('approved', 'completed'):
                raise HTTPException(409, 'approved_or_completed_refund_required')
            multi = 'source_allocations' in body
            funded = body.get('basis_kind') == 'funded'
            if body.get('basis_kind') not in (None, 'funded') or funded and not multi:
                raise HTTPException(422, 'funded_source_allocations_required')
            if multi and (body.get('baseline_id') not in (None, '') or body.get('schema_version', 2) != 2):
                raise HTTPException(422, 'multi_cost_exception_schema_required')
            unposted(db, refund.id)
            source_rows, sources = freeze_sources(db, refund, order, body['source_allocations'], funded=funded) if multi else (None, None)
            b = None if multi else baseline(db, order, refund)
            if b is not None and hasattr(b, 'snapshot_json'):
                receipts = json.loads(b.snapshot_json).get('actual_receipts')
                if (receipts is not None and number(receipts) != number(b.revenue)
                        and not ordinary_segment_transfer(db, b)):
                    raise HTTPException(422, 'funded_source_allocations_required')
            currency = 'CNY' if multi else b.currency
            retained = number(body.get('retained_cost'), cents=True, positive=True)
            if multi and sum((number(a['retained_cost']) for a in source_rows), Decimal(0)) != retained:
                raise HTTPException(422, 'retained_cost_total_mismatch')
            evidence = body.get('evidence_refs', sorted({ref for a in source_rows for ref in a['evidence_refs']}) if multi else None)
            if not isinstance(evidence, list) or not evidence or any(not isinstance(x, str) or not x.strip() for x in evidence):
                raise HTTPException(422, 'evidence_refs_required')
            bearer = db.get(Participant, required(body, 'bearer_participant_id'))
            if not bearer or bearer.currency != currency or bearer.owner_enterprise_id not in ('', order.provider_enterprise_id):
                raise HTTPException(422, 'invalid_bearer')
            bearer_subject = dict(participant_type=bearer.kind, participant_id=bearer.id,
                participant_name=bearer.name, registry_id=bearer.id, currency=bearer.currency,
                enterprise_id=bearer.enterprise_id, user_id=bearer.user_id)
            if bearer.kind == 'platform':
                bearer_subject.update(participant_id='platform', internal=True)
            elif bearer.kind == 'provider':
                if bearer.enterprise_id != order.provider_enterprise_id:
                    raise HTTPException(422, 'provider_bearer_must_match_order_provider')
                bearer_subject.update(participant_type='enterprise', participant_id=bearer.enterprise_id)
            e = CostException(refund_id=refund.id, order_id=order.id,
                provider_enterprise_id=order.provider_enterprise_id, baseline_id='' if multi else b.settlement_id,
                creator_id=user.id, creator_side='provider' if tenant else 'platform',
                payload_json=canonical(dict(retained_cost=str(retained), currency=currency,
                    reason=required(body, 'reason', 4000), evidence_refs=evidence,
                    bearer_participant_id=bearer.id, bearer_participant_name=bearer.name,
                    baseline_hash=getattr(b, 'snapshot_hash', None), refund_amount=str(number(refund.amount, cents=True)),
                    bearer=bearer_subject, **(dict(schema_version=2, source_allocations=source_rows,
                        **(dict(basis_kind='funded') if funded else {})) if multi else {}))))
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
            if decision == 'approve':
                hooks = ns.get('refund_settlement', {})
                link_model = hooks.get('Model')
                link = db.get(link_model, e.refund_id) if link_model is not None else None
                if link:
                    negative = db.get(ns['Settlement'], link.settlement_id)
                    previous = hooks['assessment'](db, negative.id)
                    if previous.get('status') == 'ready' and (
                            previous.get('segment_id') or previous.get('kind') in ('multi_source_refund', 'confirmed_funded_refund')):
                        assessment_model = hooks['FinancialAssessment']
                        latest = db.scalar(select(assessment_model).where(
                            assessment_model.settlement_id == negative.id).order_by(assessment_model.version.desc()))
                        db.add(assessment_model(settlement_id=negative.id, version=latest.version + 1,
                            values_json=canonical(dict(status='waiting',
                                reason='approved_cost_exception_requires_final_basis_refresh', exception_id=e.id)),
                            actor=str(user.id)))
                        ns['audit'](db, user.id, 'approve_segment_cost_exception_refresh', 'settlement',
                            negative.id, category='settlement', business_domain='settlement',
                            before=previous, after=dict(exception_id=e.id, reason=reason))
                        db.flush()
            return exception_dict(db, e)
        return write(db, user, body, 'decide_settlement_cost_exception:' + identity, operation)

    def cost_exception(db, refund_id):
        e = db.scalar(select(CostException).join(Decision, Decision.exception_id == CostException.id).where(
            CostException.refund_id == refund_id, Decision.decision == 'approve'))
        return exception_dict(db, e) if e else None

    def cost_exception_basis(db, refund, final_baseline):
        """Approved frozen evidence, or a waiting result; never supplies cash."""
        result = cost_exception(db, refund.id)
        if result is None:
            return None
        required_fields = ('baseline_hash', 'refund_amount', 'bearer', 'evidence_refs',
                           'reviewer_id', 'approved_at', 'reason', 'decision_reason')
        if (any(not result.get(key) for key in required_fields)
                or result['baseline_id'] != final_baseline.settlement_id
                or result['baseline_hash'] != final_baseline.snapshot_hash
                or result['order_id'] != refund.order_id
                or result['currency'] != final_baseline.currency
                or number(result['refund_amount'], cents=True) != number(refund.amount, cents=True)
                or result['creator_id'] == result['reviewer_id']):
            return dict(status='waiting', reason='approved_cost_exception_evidence_incomplete')
        bearer = result['bearer']
        if (not isinstance(bearer, dict) or bearer.get('currency') != result['currency']
                or not all(bearer.get(k) for k in ('participant_type', 'participant_id', 'participant_name', 'registry_id'))):
            return dict(status='waiting', reason='approved_cost_exception_bearer_incomplete')
        result['status'] = 'ready'
        result['approval_evidence_hash'] = hashlib.sha256(canonical(result).encode()).hexdigest()
        return result

    def source_cost_basis(db, refund, refs, *, funded=False):
        """Read-only approved proof; raw refs must exclude exception application."""
        try:
            result = cost_exception(db, refund.id)
            if result is None:
                return None
            if (result.get('schema_version') != 2 or (result.get('basis_kind') == 'funded') != funded or result['baseline_id'] != ''
                    or result['order_id'] != refund.order_id or number(result['refund_amount']) != number(refund.amount)
                    or result['creator_id'] == result['reviewer_id'] or not result.get('approved_at')
                    or not result.get('bearer') or not result.get('decision_reason')):
                raise ValueError()
            frozen = result['source_allocations']
            expected = sorted(refs, key=lambda r: (r['reference_settlement_id'], r['segment_id']))
            if len(frozen) < (1 if funded else 2) or [a['basis'] for a in frozen] != expected or any(
                    a['basis_hash'] != hashlib.sha256(canonical(a['basis']).encode()).hexdigest() for a in frozen):
                raise ValueError()
            order = db.get(ns['Order'], refund.order_id)
            bearer = result['bearer']
            if (order is None or result['currency'] != 'CNY' or bearer.get('currency') != result['currency']
                    or not all(bearer.get(k) for k in ('participant_type', 'participant_id', 'participant_name', 'registry_id'))
                    or sum((number(a['retained_cost']) for a in frozen), Decimal(0)) != number(result['retained_cost'])):
                raise ValueError()
            model = ns['settlement_finance']['Baseline']
            for a in frozen:
                b = db.get(model, a['baseline_id'])
                if (b is None or b.order_id != order.id or b.provider_enterprise_id != order.provider_enterprise_id
                        or b.currency != result['currency'] or b.snapshot_hash != a['baseline_hash']
                        or a['basis']['baseline_hash'] != a['baseline_hash']
                        or a['basis']['segment_id'] != a['segment_id']
                        or a['basis']['reference_settlement_id'] != a['baseline_id']
                        or not a['evidence_refs'] or not 0 <= number(a['retained_cost']) <= -number(a['basis']['cost_amount'])):
                    raise ValueError()
            result['status'] = 'ready'
            result['approval_evidence_hash'] = hashlib.sha256(canonical(result).encode()).hexdigest()
            return result
        except (KeyError, TypeError, ValueError, AttributeError, HTTPException):
            return dict(status='waiting', reason=('approved_funded_cost_exception_evidence_incomplete' if funded
                                                  else 'approved_multi_cost_exception_evidence_incomplete'))

    def cost_exception_multi_basis(db, refund, refs):
        return source_cost_basis(db, refund, refs)

    def cost_exception_funded_basis(db, refund, refs):
        return source_cost_basis(db, refund, refs, funded=True)

    def proposal_basis(db, user, refund_id, request=None):
        """Read-only raw cost caps. Never calls applied assessment or create hooks."""
        tenant = scope(db, user, request)
        refund_id = required(dict(refund_id=refund_id), 'refund_id')
        with db.no_autoflush:
            refund = owned(db, ns['Refund'], refund_id, '', 'order_id', lock=False)
            order = owned(db, ns['Order'], refund.order_id, '', 'provider_enterprise_id', lock=False)
            if tenant and order.provider_enterprise_id != tenant:
                raise HTTPException(403, 'settlement_cost_exception_order_scope_denied')
            result = dict(refund_id=refund.id, order_id=order.id, status='waiting', kind='',
                basis_kind='', basis_origin='ordinary', proposal_allowed=False, reason='', references=[])
            try:
                if refund.status not in ('approved', 'completed'):
                    raise HTTPException(409, 'approved_or_completed_refund_required')
                funded_hook = (ns.get('settlement_funded_refund', {}).get('raw_funded_basis') or
                               ns.get('settlement_funded_cost_exception', {}).get('raw_funded_basis'))
                # The raw funded builder validates confirmed batches before it
                # reports non-funded sources. Classify first so ordinary locked
                # sources are not subjected to funded-only prerequisites.
                classifier = ns.get('settlement_funded_refund_capture', {}).get('segment_basis')
                classified = classifier(db, refund, apply_exception=False) if callable(classifier) else True
                raw = funded_hook(db, refund) if classified is not None and callable(funded_hook) else None
                if isinstance(raw, dict) and raw.get('status') == 'ready':
                    if raw.get('kind') != 'confirmed_funded_refund':
                        raise HTTPException(409, 'raw_funded_refund_basis_invalid')
                    result.update(basis_kind='funded', basis_origin='raw_funded_basis', kind=raw['kind'])
                    refs = raw['references']
                else:
                    # Missing funded evidence is not permission to fall back to
                    # ordinary cost or an already adjusted assessment.
                    if isinstance(raw, dict) and raw.get('reason') != 'funded_refund_not_a_funded_source':
                        raise HTTPException(409, raw.get('reason', 'raw_funded_refund_basis_unavailable'))
                    hooks = ns.get('refund_settlement', {})
                    completed = ns.get('settlement_funded_refund_evidence', {}).get('completed_evidence')
                    if not callable(completed):
                        raise HTTPException(409, 'completed_refund_basis_evidence_required')
                    _, plan, _, _ = completed(db, refund)
                    segments = plan.get('segments', [plan])
                    if len(segments) > 1:
                        result.update(basis_kind='multi', basis_origin='raw_multi_basis')
                        hook = hooks.get('raw_multi_basis')
                        if not callable(hook):
                            raise HTTPException(409, 'raw_multi_refund_basis_unavailable')
                        raw = hook(db, refund, plan, apply_exception=False)
                        result['kind'] = 'multi_source_refund'
                        refs = raw.get('references')
                    elif len(segments) == 1:
                        result.update(basis_kind='single', basis_origin='raw_single_basis')
                        segment = segments[0]
                        result['kind'] = 'ordinary_single_refund'
                        if segment.get('calculation_version') == 'economic-segment-refund-v1':
                            hook = ns.get('version_change_settlement', {}).get('refund_basis')
                            if not callable(hook):
                                raise HTTPException(409, 'raw_segment_refund_basis_unavailable')
                            raw = hook(db, refund, segment, apply_exception=False)
                            refs = [dict(raw, segment_id=segment['segment_id'], source_order_id=order.id)]
                        else:
                            if segment.get('calculation_version') not in ('ordinary-refund-plan-v1', 'ordinary-file-refund-plan-v1'):
                                raise HTTPException(409, 'exact_segment_final_baseline_required')
                            Baseline, Settlement = ns['settlement_finance']['Baseline'], ns['Settlement']
                            sources = db.scalars(select(Baseline).join(Settlement, Settlement.id == Baseline.settlement_id).where(
                                Baseline.order_id == order.id, Baseline.revenue > 0,
                                Settlement.status.in_(('locked', 'paid')))).all()
                            if len(sources) != 1:
                                raise HTTPException(409, 'unique_positive_final_baseline_required')
                            source = sources[0]
                            total = number(segment['original_amount'], cents=True, positive=True)
                            cumulative = number(segment['cumulative_refund_amount'], cents=True, positive=True)
                            amount = number(refund.amount, cents=True, positive=True)
                            if source.revenue != total or not amount <= cumulative <= total:
                                raise HTTPException(409, 'raw_funded_refund_basis_required')
                            adjustment = ns.get('VersionChangeAdjustment')
                            if adjustment is not None and db.scalar(select(adjustment.id).where(
                                    adjustment.source_order_id == order.id, adjustment.status == 'posted')):
                                raise HTTPException(409, 'version_change_requires_segment_baselines')
                            current = (source.cost * cumulative / total).quantize(CENT, rounding=ROUND_HALF_UP)
                            prior = (source.cost * (cumulative - amount) / total).quantize(CENT, rounding=ROUND_HALF_UP)
                            raw = dict(status='ready')
                            refs = [dict(segment_id=segment['segment_id'], source_order_id=order.id,
                                reference_settlement_id=source.settlement_id, baseline_hash=source.snapshot_hash,
                                currency=source.currency, cost_amount=str(prior - current))]
                    else:
                        raise HTTPException(409, 'complete_refund_segments_required')
                if raw.get('status') != 'ready' or not isinstance(refs, list) or not refs:
                    raise HTTPException(409, raw.get('reason', 'raw_refund_basis_unavailable'))
                identities, references = set(), []
                Baseline, Settlement = ns['settlement_finance']['Baseline'], ns['Settlement']
                for ref in refs:
                    identity = ref['reference_settlement_id']
                    source, settlement = db.get(Baseline, identity), db.get(Settlement, identity)
                    if (identity in identities or source is None or settlement is None
                            or source.order_id != order.id or source.provider_enterprise_id != order.provider_enterprise_id
                            or ref.get('source_order_id', order.id) != order.id or source.currency != ref['currency']
                            or source.snapshot_hash != ref['baseline_hash']
                            or hashlib.sha256(source.snapshot_json.encode()).hexdigest() != source.snapshot_hash
                            or settlement.status not in ('locked', 'paid') or number(ref['cost_amount'], cents=True) > 0):
                        raise HTTPException(409, 'raw_refund_source_identity_mismatch')
                    identities.add(identity)
                    batches = []
                    Batch, Line = ns.get('SettlementBatch'), ns.get('SettlementLine')
                    if Batch is not None and Line is not None:
                        batches = sorted(set(db.scalars(select(Batch.batch_no).join(Line, Line.batch_id == Batch.id).where(
                            Line.settlement_id == identity, Line.status != 'superseded', Batch.status != 'superseded'))))
                    references.append(dict(segment_id=ref['segment_id'], source_order_id=order.id,
                        reference_settlement_id=identity, baseline_hash=source.snapshot_hash, currency=source.currency,
                        cost_amount=str(number(ref['cost_amount'], cents=True)), order_no=getattr(order, 'order_no', ''),
                        settlement_no=getattr(settlement, 'settlement_no', ''), batch_nos=batches,
                        batch_no=batches[0] if len(batches) == 1 else ''))
                result.update(status='ready', references=references, proposal_allowed=True)
                unposted(db, refund.id)
                for module, key in (('settlement_finance', 'CostExceptionApplication'),
                                    ('settlement_funded_cost_exception', 'Application'),
                                    ('settlement_multi_cost_exception', 'Application')):
                    model = ns.get(module, {}).get(key)
                    if model is not None and db.get(model, refund.id) is not None:
                        raise HTTPException(409, 'frozen_cost_exception_requires_explicit_adjustment')
                manifest = ns.get('refund_settlement', {}).get('MultiSourceManifest')
                if manifest is not None and db.scalar(select(manifest.settlement_id).where(manifest.refund_id == refund.id)):
                    raise HTTPException(409, 'frozen_refund_basis_requires_explicit_adjustment')
                existing = db.scalar(select(CostException).where(CostException.refund_id == refund.id))
                if existing:
                    decision = db.get(Decision, existing.id)
                    result.update(proposal_allowed=False, reason=('cost_exception_pending' if decision is None else
                        'cost_exception_approved' if decision.decision == 'approve' else 'cost_exception_reproposal_not_supported'))
            except (HTTPException, ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
                reason = exc.detail if isinstance(exc, HTTPException) else 'raw_refund_basis_evidence_unavailable'
                result.update(proposal_allowed=False, reason=reason)
            return result

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

        @app.get('/api/settlement-cost-exceptions/basis')
        def exception_basis_endpoint(refund_id: str, request: Request,
                                     user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
            return proposal_basis(db, user, refund_id, request)

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
                 allocations=allocations, cost_exception=cost_exception, cost_exception_basis=cost_exception_basis,
                 cost_exception_multi_basis=cost_exception_multi_basis,
                 cost_exception_funded_basis=cost_exception_funded_basis,
                 proposal_basis=proposal_basis,
                 create_participant=create_participant,
                 configure=configure, create_exception=create_exception, decide=decide)
    ns['settlement_parties'] = hooks
    from .settlement_funded_cost_exception import install as install_funded_cost
    install_funded_cost(ns)
    return hooks
