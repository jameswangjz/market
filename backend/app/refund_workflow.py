"""Ordinary REF application/review only; caller owns the transaction.

Existing Refund/Order/Payment rows are reused. Additive companion tables avoid
reinterpreting historical pending rows or changing the version-change Saga.
No executor, entitlement recovery, external payment, or settlement is invoked.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
import secrets

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import mapped_column

from .refund_schema import PreparedRefundPlan, RefundRequestBody, RefundReviewBody


RESERVED = ('pending', 'requested', 'approved', 'executing', 'failed')
FUNDING = ('upgrade', 'downgrade', 'version_change_payment')


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or result != result.quantize(Decimal('0.01')):
        raise HTTPException(409, 'Invalid recorded payment amount')
    return result


def install(ns):
    Base, Refund, Order, Payment, Member = (ns[name] for name in ('Base', 'Refund', 'Order', 'Payment', 'Membership'))

    class RefundCase(Base):
        __tablename__ = 'ordinary_refund_cases'
        refund_id = mapped_column(ForeignKey('refunds.id'), primary_key=True)
        ticket_id = mapped_column(ForeignKey('after_sales_tickets.id'), nullable=True)
        version = mapped_column(Integer, nullable=False, default=1)
        source = mapped_column(String(30), nullable=False, default='ordinary')
        requested_by_id = mapped_column(String(36), nullable=False)
        reviewed_by_id = mapped_column(String(36), nullable=True)
        review_reason = mapped_column(Text, nullable=False, default='')
        reviewed_at = mapped_column(DateTime(timezone=True), nullable=True)
        plan_json = mapped_column(Text, nullable=True)
        plan_hash = mapped_column(String(64), nullable=True)

    class RefundEvent(Base):
        __tablename__ = 'ordinary_refund_events'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        refund_id = mapped_column(ForeignKey('refunds.id'), nullable=False, index=True)
        operation_key = mapped_column(String(64), nullable=False, unique=True)
        payload_hash = mapped_column(String(64), nullable=False)
        action = mapped_column(String(30), nullable=False)
        actor_id = mapped_column(String(36), nullable=False)
        actor_role = mapped_column(String(40), nullable=False)
        actor_enterprise_id = mapped_column(String(36), nullable=False, default='')
        reason = mapped_column(Text, nullable=False)
        request_id = mapped_column(String(100), nullable=False, default='')
        before_json = mapped_column(Text, nullable=False)
        after_json = mapped_column(Text, nullable=False)
        occurred_at = mapped_column(DateTime(timezone=True), nullable=False, default=ns['now'])

    def active(user):
        if not user.is_active or user.activation_status != 'active' or user.verified_status != 'verified':
            raise HTTPException(403, 'Activated verified account required')

    def member_role(db, user, enterprise_id):
        if user.platform_role or not enterprise_id:
            return None
        enterprise = db.get(ns['Enterprise'], enterprise_id)
        if enterprise is None or enterprise.verification_status != 'verified':
            return None
        return db.scalar(select(Member.role).where(Member.enterprise_id == enterprise_id,
            Member.user_id == user.id, Member.status == 'active', Member.role.in_(('super_admin', 'enterprise_admin'))))

    def platform(user):
        active(user)
        ns['require_platform_admin'](user)

    def scope(db, user, request, order=None, *, buyer=False):
        active(user)
        if user.platform_role:
            if buyer:
                raise HTTPException(403, 'Only buyer enterprise administrators may request refunds')
            platform(user)
            return None
        selected = ns['resolve_business_scope'](request, db, user, Member)
        enterprise_id = selected['enterprise_id']
        role = member_role(db, user, enterprise_id)
        if not role:
            raise HTTPException(403, 'Active verified enterprise administrator required')
        if order is not None:
            visible = (selected['mode'] != 'provider' and order.buyer_enterprise_id == enterprise_id or
                       not buyer and selected['mode'] != 'buyer' and order.provider_enterprise_id == enterprise_id)
            if not visible:
                raise HTTPException(404, 'Refund order not found')
        elif buyer:
            raise HTTPException(404, 'Refund order not found')
        return selected

    def lock_order(db, order_id):
        order = db.scalar(select(Order).where(Order.id == order_id).with_for_update()
                          .execution_options(populate_existing=True))
        if order is None:
            raise HTTPException(404, 'Order not found')
        return order

    def replay(db, user, body, target):
        operation_key = hashlib.sha256((user.id + ':' + body.idempotency_key).encode()).hexdigest()
        payload = body.model_dump(mode='json')
        if 'amount' in payload:
            payload['amount'] = format(money(body.amount), '.2f')
        payload_hash = hashlib.sha256(canonical([target, payload]).encode()).hexdigest()
        event = db.scalar(select(RefundEvent).where(RefundEvent.operation_key == operation_key))
        if event is not None and event.payload_hash != payload_hash:
            raise HTTPException(409, 'Idempotency key already used for a different operation')
        return event, operation_key, payload_hash

    def payment_for(db, order, payment_id):
        if order.payment_status not in {'paid', 'refunding', 'refunded'} or money(order.paid_amount) <= 0:
            raise HTTPException(409, 'A recorded paid order is required')
        statement = select(Payment).where(Payment.order_id == order.id, Payment.paid_at.is_not(None),
                                          Payment.status.in_(('paid', 'refunding', 'refunded')), Payment.amount > 0)
        if payment_id:
            statement = statement.where(Payment.id == payment_id)
        rows = list(db.scalars(statement.order_by(Payment.paid_at, Payment.id)))
        if len(rows) != 1:
            raise HTTPException(409, 'Payment source unavailable or ambiguous; select a recorded payment')
        return rows[0]

    def cap(db, order, payment, amount, exclude=None):
        rows = list(db.scalars(select(Refund).where(Refund.order_id == order.id)))
        completed = sum((money(row.amount) for row in rows if row.status == 'completed'), Decimal('0.00'))
        recorded = max(money(order.refunded_amount), completed)
        held = sum((money(row.amount) for row in rows if row.id != exclude and row.status in RESERVED), Decimal('0.00'))
        payment_held = sum((money(row.amount) for row in rows if row.payment_id == payment.id
            and row.id != exclude and row.status in (*RESERVED, 'completed')), Decimal('0.00'))
        if amount + recorded + held > money(order.paid_amount) or amount + payment_held > money(payment.amount):
            raise HTTPException(409, 'Requested amount exceeds paid balance including in-flight reservations')

    def planning(order, case=None):
        if case is not None and case.plan_hash:
            try:
                if hashlib.sha256(case.plan_json.encode()).hexdigest() != case.plan_hash:
                    raise ValueError('Plan hash mismatch')
                basis = json.loads(case.plan_json)['basis']
                if basis not in {'manual', 'calls', 'time'}:
                    raise ValueError('Unknown plan basis')
            except (ValueError, TypeError, KeyError, AttributeError):
                return {'status': 'unavailable', 'reason': 'frozen_plan_invalid',
                        'automatic_quote_available': False, 'resource_plan_status': 'unavailable'}
            return {'status': 'frozen', 'reason': None, 'basis': basis,
                    'source_trust': 'manual_determination' if basis == 'manual' else 'automatic',
                    'automatic_usage_trusted': basis == 'calls', 'automatic_quote_available': False,
                    'resource_plan_status': 'unavailable'}
        if order.snapshot_version != 1:
            reason = 'manual_assessment_required'
        elif order.delivery_method_snapshot in {'api', 'model_api'}:
            reason = 'api_charge_ledger_unavailable'
        else:
            reason = 'economic_plan_not_available'
        return {'status': 'unavailable', 'reason': reason,
                'manual_assessment_allowed': True, 'automatic_quote_available': False}

    def controlled_source(db, refund, case):
        Fund = ns['version_change_saga']['VersionChangeFund']
        if db.scalar(select(Fund.id).where(Fund.refund_id == refund.id).limit(1)):
            return 'version_change'
        return 'ordinary' if case is not None else 'legacy'

    def output(db, user, refund):
        order, case = db.get(Order, refund.order_id), db.get(RefundCase, refund.id)
        source = controlled_source(db, refund, case)
        allowed = ['reject'] if source == 'ordinary' and refund.status == 'requested' and user.platform_role in {
            'super_admin', 'platform_operator'} else []
        # BE011 has not supplied a trusted planning adapter yet. Do not advertise
        # approve based on a nominal paid balance or a caller-provided quote.
        if allowed and callable(ns.get('prepare_refund_plan')):
            allowed.insert(0, 'approve')
        planning_result = planning(order, case)
        readiness = ns.get('refund_planning_status')
        if readiness and source == 'ordinary' and not (case and case.plan_hash):
            planning_result = readiness(db, order, refund)
            if planning_result['status'] not in {'available', 'manual_ready'}:
                allowed = [action for action in allowed if action != 'approve']
        return {'id': refund.id, 'refund_no': refund.refund_no, 'order_id': refund.order_id,
            'order_no': order.order_no, 'payment_id': refund.payment_id, 'ticket_id': case.ticket_id if case else None,
            'amount': format(money(refund.amount), '.2f'), 'currency': 'CNY', 'status': refund.status,
            'source': source, 'version': case.version if case else None, 'reason': refund.reason,
            'requested_by': refund.requested_by, 'reviewed_by_id': case.reviewed_by_id if case else None,
            'review_reason': case.review_reason if case else None, 'reviewed_at': case.reviewed_at if case else None,
            'created_at': refund.created_at, 'completed_at': refund.completed_at,
            'planning': planning_result, 'allowed_actions': allowed,
            'execution_available': False, 'execution_mode': 'unavailable'}

    def record(db, user, order, refund, case, body, action, before, key, payload_hash):
        after = {'status': refund.status, 'version': case.version, 'amount': format(money(refund.amount), '.2f')}
        role = user.platform_role or member_role(db, user, order.buyer_enterprise_id)
        db.add(RefundEvent(refund_id=refund.id, operation_key=key, payload_hash=payload_hash, action=action,
            actor_id=user.id, actor_role=role, actor_enterprise_id='' if user.platform_role else order.buyer_enterprise_id,
            reason=body.reason, request_id=(ns['audit_request_id'].get() or '')[:100],
            before_json=canonical(before), after_json=canonical(after)))
        ns['audit'](db, user.email or user.id, 'ordinary_refund_' + action, 'refund', refund.id,
            'Ordinary refund ' + action, category='refund', business_domain='refund', order_id=order.id,
            before=before, after={**after, 'actor_id': user.id, 'actor_role': role, 'reason': body.reason})
        ns['notify_business_event'](db, '退款申请更新', refund.refund_no + '：' + refund.status,
            'refund', refund.id, event_key='ordinary-refund:' + key, category='refund',
            enterprise_ids=[order.buyer_enterprise_id, order.provider_enterprise_id],
            platform_roles=['super_admin', 'platform_operator'])
        db.flush()

    def create(db, user, order_id, body, request):
        peek = db.get(Order, order_id)
        if peek is None:
            raise HTTPException(404, 'Order not found')
        scope(db, user, request, peek, buyer=True)
        order = lock_order(db, order_id)
        scope(db, user, request, order, buyer=True)
        if order.business_type in FUNDING:
            raise HTTPException(409, 'Version-change funds use the internal controlled workflow')
        prior, key, payload_hash = replay(db, user, body, 'create:' + order.id)
        if prior:
            return db.get(Refund, prior.refund_id)
        payment = payment_for(db, order, body.payment_id)
        if body.ticket_id:
            ticket = db.get(ns['AfterSalesTicket'], body.ticket_id)
            if ticket is None or ticket.order_id != order.id:
                raise HTTPException(404, 'Linked after-sales ticket not found')
        amount = money(body.amount)
        cap(db, order, payment, amount)
        refund = Refund(refund_no='REF-' + secrets.token_hex(8).upper(), order_id=order.id,
            payment_id=payment.id, amount=amount, status='requested', reason=body.reason,
            requested_by=user.name, created_at=ns['now']())
        db.add(refund)
        db.flush()
        case = RefundCase(refund_id=refund.id, ticket_id=body.ticket_id, requested_by_id=user.id, source='ordinary', version=1)
        db.add(case)
        record(db, user, order, refund, case, body, 'request', {}, key, payload_hash)
        return refund

    def freeze_plan(db, order, refund):
        prepare = ns.get('prepare_refund_plan')
        if not callable(prepare):
            raise HTTPException(409, 'Trusted frozen economic plan unavailable; approval is disabled')
        plan = prepare(db, order, refund)
        if (not isinstance(plan, PreparedRefundPlan) or plan.refund_id != refund.id or plan.order_id != order.id
                or plan.payment_id != refund.payment_id or money(plan.amount) != money(refund.amount)
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', plan.calculation_version or '')
                or not plan.evidence_ids or any(not isinstance(i, str) or not i for i in plan.evidence_ids)
                or plan.basis not in {'calls', 'time', 'manual'}
                or order.snapshot_version != 1 and plan.basis != 'manual'
                or not isinstance(plan.as_of, datetime) or plan.as_of.tzinfo is None
                or plan.as_of.utcoffset() is None or plan.as_of > ns['now']()
                or not isinstance(plan.economic_plan, dict) or not plan.economic_plan):
            raise HTTPException(409, 'Trusted frozen economic plan is invalid')
        if ((order.delivery_method_snapshot not in {'api', 'model_api', 'tenant_access'} and plan.basis != 'manual')
                or order.delivery_method_snapshot == 'tenant_access' and plan.basis not in {'time', 'manual'}):
            raise HTTPException(409, 'Frozen plan basis does not match the delivery method')
        try:
            document = canonical({'schema_version': 1, 'refund_id': refund.id, 'order_id': order.id,
                'payment_id': refund.payment_id, 'amount': format(money(refund.amount), '.2f'),
                'calculation_version': plan.calculation_version, 'as_of': plan.as_of.astimezone(timezone.utc).isoformat(),
                'basis': plan.basis,
                'evidence_ids': list(plan.evidence_ids), 'economic_plan': plan.economic_plan})
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, 'Frozen economic plan must be finite JSON') from exc
        return document, hashlib.sha256(document.encode()).hexdigest()

    def review(db, user, refund_id, body):
        platform(user)
        peek = db.get(Refund, refund_id)
        if peek is None:
            raise HTTPException(404, 'Refund not found')
        order = lock_order(db, peek.order_id)
        refund = db.scalar(select(Refund).where(Refund.id == refund_id).with_for_update()
                           .execution_options(populate_existing=True))
        case = db.get(RefundCase, refund_id, populate_existing=True)
        if case is None or controlled_source(db, refund, case) != 'ordinary':
            raise HTTPException(409, 'Legacy and version-change refunds cannot use ordinary review')
        prior, key, payload_hash = replay(db, user, body, 'review:' + refund_id)
        if prior:
            return refund
        if case.version != body.expected_version or refund.status != 'requested':
            raise HTTPException(409, 'Refund changed or already reviewed; refresh and retry')
        before = {'status': refund.status, 'version': case.version}
        if body.decision == 'approve':
            payment = payment_for(db, order, refund.payment_id)
            cap(db, order, payment, money(refund.amount), exclude=refund.id)
            case.plan_json, case.plan_hash = freeze_plan(db, order, refund)
        refund.status = 'approved' if body.decision == 'approve' else 'rejected'
        case.version += 1
        case.reviewed_by_id, case.review_reason, case.reviewed_at = user.id, body.reason, ns['now']()
        record(db, user, order, refund, case, body, body.decision, before, key, payload_hash)
        return refund

    @contextmanager
    def transaction(db, user, target_id, operation):
        try:
            yield
        except HTTPException as exc:
            db.rollback()
            ns['audit'](db, user.email or user.id, 'ordinary_refund_denied', 'refund', target_id[:36],
                'Rejected ordinary refund operation', category='security', business_domain='refund', result='denied',
                after={'operation': operation, 'status_code': exc.status_code})
            db.commit()
            raise
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, 'Refund operation conflicts with another request') from exc

    @ns['app'].post('/api/orders/{order_id}/refund-requests')
    def request_route(order_id: str, body: RefundRequestBody, request: Request,
                      user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        with transaction(db, user, order_id, 'request'):
            refund = create(db, user, order_id, body, request)
            db.commit()
            return output(db, user, refund)

    @ns['app'].post('/api/refunds/{refund_id}/review')
    def review_route(refund_id: str, body: RefundReviewBody,
                     user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        with transaction(db, user, refund_id, 'review'):
            refund = review(db, user, refund_id, body)
            db.commit()
            return output(db, user, refund)

    @ns['app'].get('/api/refunds')
    def list_route(request: Request, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
                   order_id: str | None = None, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        selected = scope(db, user, request)
        conditions = []
        if selected is not None:
            conditions.append(ns['order_predicate'](Order, selected))
        if order_id:
            conditions.append(Order.id == order_id)
        statement = select(Refund).join(Order, Order.id == Refund.order_id).where(*conditions)
        total = db.scalar(select(func.count()).select_from(statement.subquery()))
        rows = db.scalars(statement.order_by(Refund.created_at.desc(), Refund.id.desc())
                          .offset((page - 1) * page_size).limit(page_size))
        return {'items': [output(db, user, refund) for refund in rows], 'total': total, 'page': page, 'page_size': page_size}

    @ns['app'].get('/api/refunds/{refund_id}')
    def detail_route(refund_id: str, request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        refund = db.get(Refund, refund_id)
        order = db.get(Order, refund.order_id) if refund else None
        if order is None:
            raise HTTPException(404, 'Refund not found')
        scope(db, user, request, order)
        return output(db, user, refund)

    hooks = dict(Case=RefundCase, Event=RefundEvent, create=create, review=review, output=output,
                 platform=platform, scope=scope, cap=cap, payment_for=payment_for, lock_order=lock_order,
                 record=record, replay=replay, transaction=transaction, controlled_source=controlled_source)
    ns['refund_workflow'] = hooks
    return hooks
