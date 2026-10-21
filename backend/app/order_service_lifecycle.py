"""Service periods are separate from payment and resource provisioning."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from fastapi import Depends, HTTPException
from sqlalchemy import Column, ForeignKey, Integer, String, Text, select
from sqlalchemy.exc import SQLAlchemyError

from .saas_entry import probe_saas_entry
from .subscription_terms import UTCDateTime

MONTHLY = frozenset({'api', 'model_api', 'tenant_access'})


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def install(ns):
    if ns.get('order_service_lifecycle'):
        return ns['order_service_lifecycle']
    Order, Task = ns['Order'], ns['DeliveryTask']

    class State(ns['Base']):
        __tablename__ = 'order_service_states'
        order_id = Column(ForeignKey('orders.id'), primary_key=True)
        status = Column(String(30), nullable=False)
        next_check_at = Column(UTCDateTime(), nullable=True, index=True)
        attempts = Column(Integer, nullable=False, default=0)
        last_error = Column(Text, nullable=False, default='')

    def initialize(db, order):
        order.order_type = 'new_purchase'
        if order.delivery_method_snapshot not in MONTHLY:
            return
        combo = ns['subscription_terms']['lock_combination'](db, order.buyer_enterprise_id, order.product_id)
        if combo and combo.status == 'active':
            from .subscription_calendar import CalendarAnchor
            anchor = CalendarAnchor(combo.anchor_at)
            start = anchor.boundary(combo.total_months)
            if utc(ns['now']()) < start:
                order.order_type = 'renewal'
                order.service_starts_at = start
                order.service_ends_at = anchor.boundary(combo.total_months + order.subscription_months)

    def describe(db, order):
        state = db.get(State, order.id)
        result = {'service_activation_status': state.status if state else None,
                  'service_activation_error': state.last_error if state else '',
                  'service_activation_attempts': state.attempts if state else 0}
        if order.order_type == 'new_purchase' and order.business_type in {'renew', 'renewal', 'upgrade', 'downgrade'}:
            result['order_type'] = 'renewal' if order.business_type in {'renew', 'renewal'} else order.business_type
        if order.service_starts_at is None:
            Term = ns['subscription_terms']['Term']
            term = db.scalar(select(Term).where(Term.order_id == order.id))
            if term:
                result.update(service_starts_at=term.starts_at, service_ends_at=term.ends_at,
                              order_type='renewal' if term.start_month > 0 else 'new_purchase')
        if order.related_order_id:
            source = db.get(Order, order.related_order_id)
            result['related_order_no'] = source.order_no if source else None
        Link = ns.get('RefundOrderLink')
        if Link is not None and order.order_type == 'refund':
            link = db.scalar(select(Link).where(Link.order_id == order.id))
            refund = db.get(ns['Refund'], link.refund_id) if link else None
            if refund:
                result.update(refund_id=refund.id, refund_no=refund.refund_no, refund_status=refund.status)
        Request = ns.get('version_change_saga', {}).get('VersionChangeRequest')
        if Request is not None and order.business_type == 'version_change_payment':
            change = db.scalar(select(Request).where(Request.funding_order_id == order.id))
            if change:
                quote = json.loads(change.quote_json)
                result['service_segments'] = [{key: segment[key] for key in (
                    'order_id', 'order_no', 'starts_at', 'ends_at') if key in segment}
                    for segment in quote.get('segments', [])]
                if order.order_type == 'new_purchase':
                    result['order_type'] = 'upgrade' if change.kind in {'upgrade', 'no_payment'} else 'downgrade'
                if order.service_starts_at is None and result['service_segments']:
                    result.update(service_starts_at=order.created_at,
                        service_ends_at=max(segment['ends_at'] for segment in result['service_segments']))
        return result

    def transition(db, order, main, delivery, actor, reason):
        before = {'main_status': order.main_status, 'delivery_status': order.delivery_status}
        order.main_status, order.delivery_status = main, delivery
        after = {'main_status': main, 'delivery_status': delivery}
        if before != after:
            for domain in ('main', 'delivery'):
                if before[domain + '_status'] != after[domain + '_status']:
                    ns['log_state'](db, order, domain, before[domain + '_status'],
                        after[domain + '_status'], 'service_period_transition', actor, reason)
            ns['audit'](db, actor.email or actor.id, 'service_period_transition', 'order', order.id,
                reason, category='delivery', business_domain='trading', order_id=order.id,
                before=before, after=after)

    def sync_gateway(db, order, version_code):
        route = ns['product_gateway_route'](db, order.product_id, version_code, active=True)
        if not route:
            raise HTTPException(409, '有效订阅路由不存在')
        redis, previous = None, {}
        if ns['apisix_enabled']():
            redis = ns['apisix_policy_redis']()
            Credential = ns['ApiCredential']
            credentials = db.scalars(select(Credential).where(
                Credential.enterprise_id == order.buyer_enterprise_id, Credential.route_id == route.id)).all()
            previous = {credential.apisix_consumer_name: redis.hgetall(
                'market:apisix:credential:' + credential.apisix_consumer_name)
                for credential in credentials if credential.apisix_consumer_name}
            ns['_track_api_policy_compensation'](db, redis, previous)
        try:
            ns['subscription_gateway']['refresh'](db, order.buyer_enterprise_id, route)
        except Exception:
            if redis and ns['_restore_api_policies'](redis, previous):
                raise RuntimeError('Service gateway rollback could not safely restore policy') from None
            raise
        return redis, previous

    def delivered(db, order, task, actor):
        if order.delivery_method_snapshot not in MONTHLY:
            if order.delivery_method_snapshot == 'file':
                order.service_starts_at = order.service_starts_at or utc(ns['now']())
                order.service_effective_at = order.service_effective_at or order.service_starts_at
            return False
        Term = ns['subscription_terms']['Term']
        term = db.scalar(select(Term).where(Term.order_id == order.id))
        if term is None:
            raise HTTPException(409, '订阅交付缺少已冻结期限')
        at = utc(ns['now']())
        order.order_type = 'renewal' if term.start_month > 0 else 'new_purchase'
        order.service_starts_at, order.service_ends_at = utc(term.starts_at), utc(term.ends_at)
        state = db.get(State, order.id)
        if state is None:
            state = State(order_id=order.id, status='scheduled', attempts=0, last_error='')
            db.add(state)
        if at < utc(term.starts_at):
            transition(db, order, 'pending_fulfillment', 'awaiting_effective', actor,
                       '续费权益配置完成，等待服务开始时间并重新检查可达性')
            state.status, state.next_check_at = 'scheduled', utc(term.starts_at)
            task.status, task.note = 'awaiting_effective', '权益配置已完成，等待服务开始并重新验证'
        else:
            transition(db, order, 'fulfilling' if at < utc(term.ends_at) else 'completed',
                       'accepted', actor, '订阅资源已验证，按服务期限履约')
            order.service_effective_at = order.service_effective_at or at
            state.status = 'active' if at < utc(term.ends_at) else 'completed'
            state.next_check_at = utc(term.ends_at) if state.status == 'active' else None
        db.flush()
        if order.delivery_method_snapshot in {'api', 'model_api'}:
            sync_gateway(db, order, term.version_code)
        return True

    def change_applied(db, change):
        if not change.resource_applied or change.status not in {'completed', 'refund_pending'}:
            return
        order = db.get(Order, change.funding_order_id) if change.funding_order_id else None
        if not order or not order.service_ends_at:
            return
        actor = SimpleNamespace(id='version-change-worker', email='version-change-worker', name='版本变更工作进程')
        at = utc(ns['now']())
        order.service_effective_at = order.service_effective_at or at
        transition(db, order, 'fulfilling' if at < utc(order.service_ends_at) else 'completed',
                   'accepted', actor, '版本资源切换已验证，继续剩余服务期限')
        state = db.get(State, order.id)
        if state is None:
            state = State(order_id=order.id, attempts=0, last_error='')
            db.add(state)
        state.status = 'active' if at < utc(order.service_ends_at) else 'completed'
        state.next_check_at = utc(order.service_ends_at) if state.status == 'active' else None

    def verify(db, order):
        product = db.get(ns['Product'], order.product_id)
        if not product or product.status == 'security_unpublished':
            raise HTTPException(409, '产品不可用')
        Term = ns['subscription_terms']['Term']
        term = db.scalar(select(Term).where(Term.order_id == order.id))
        if not term or term.status != 'active':
            raise HTTPException(409, '订阅期限不可用')
        effective = ns['subscription_terms']['effective_snapshot'](db, order, term)
        view = SimpleNamespace(**{column.name: getattr(order, column.name) for column in Order.__table__.columns})
        view.product_version_id, view.product_version_code = effective['version_id'], effective['version_code']
        ns['subscription_gateway']['verify_delivery'](db, view)
        if order.delivery_method_snapshot == 'tenant_access':
            probe_saas_entry(ns, db, product)

    def process_due(db, actor):
        at = utc(ns['now']())
        order = db.scalar(select(Order).join(State, State.order_id == Order.id).where(
            State.next_check_at <= at).order_by(State.next_check_at, Order.id)
            .with_for_update(skip_locked=True, of=Order).execution_options(populate_existing=True))
        if order is None:
            return False
        state = db.get(State, order.id, with_for_update=True)
        ns['subscription_terms']['lock_combination'](db, order.buyer_enterprise_id, order.product_id)
        if order.refunded_amount and order.paid_amount and order.refunded_amount >= order.paid_amount:
            transition(db, order, 'refunded', 'revoked', actor, '原订单已全额退款，停止后续服务')
            state.status, state.next_check_at = 'stopped', None
            return True
        if order.main_status in {'cancelled', 'closed', 'refunded'}:
            state.status, state.next_check_at = 'stopped', None
            return True
        if order.refunded_amount:
            Term = ns['subscription_terms']['Term']
            term = db.scalar(select(Term).where(Term.order_id == order.id))
            if term:
                overlay = ns['refund_execution_resources']['entitlement'](db, order, term)
                if overlay is not None and (not overlay.get('payment_compatible') or not overlay.get('periods')):
                    transition(db, order, 'pending_fulfillment', 'exception', actor,
                               '退款权益证据不允许继续服务，等待资源核验')
                    if state.status != 'needs_manual':
                        state.attempts += 1
                        state.status = 'retrying' if state.attempts < 3 else 'needs_manual'
                        state.next_check_at = at + timedelta(seconds=10) if state.attempts < 3 else utc(order.service_ends_at)
                    return True
                if overlay and overlay.get('payment_compatible') and overlay.get('periods'):
                    end = max(datetime.fromisoformat(row['ends_at']) if row.get('ends_at') else
                        datetime.fromtimestamp(row['end_at'], timezone.utc) for row in overlay['periods'])
                    order.service_ends_at = min(utc(order.service_ends_at), utc(end))
        if at >= utc(order.service_ends_at):
            transition(db, order, 'completed', order.delivery_status, actor, '服务期限结束')
            state.status, state.next_check_at = 'completed', None
            return True
        if at < utc(order.service_starts_at):
            state.next_check_at = min(utc(order.service_starts_at), utc(order.service_ends_at))
            return True
        if state.status == 'active':
            state.next_check_at = utc(order.service_ends_at)
            return True
        if state.status == 'needs_manual':
            state.next_check_at = utc(order.service_ends_at)
            return True
        published = None
        try:
            if state.status == 'activation_verified':
                verify(db, order)
                with db.begin_nested():
                    transition(db, order, 'fulfilling', 'accepted', actor, '服务生效检查及授权同步完成')
                    order.service_effective_at = at
                    state.status, state.next_check_at, state.last_error = 'active', utc(order.service_ends_at), ''
                    db.flush()
                    if order.delivery_method_snapshot in {'api', 'model_api'}:
                        entitlement = ns['subscription_terms']['current_entitlement'](db, order.buyer_enterprise_id, order.product_id)
                        if not entitlement:
                            raise HTTPException(409, '有效订阅路由不存在')
                        published = sync_gateway(db, order, entitlement['version_code'])
                    task = db.scalar(select(Task).where(Task.order_id == order.id))
                    if task:
                        task.status, task.last_error = 'completed', ''
                        task.note = '服务开始时间已到，资源和可达性验证通过'
                        ns['order_sla_contracts']['record_verified_delivery'](db, order, task)
                        version = f'service:{order.id}:active'
                        ns['settlement_metering']['record_order_event'](db, order, 'delivery_succeeded',
                            version, actor.id, task.id)
                        ns['notify_order_event'](db, order, 'delivery_succeeded', version,
                            target_type='delivery_task', target_id=task.id)
                return True
            verify(db, order)
        except SQLAlchemyError:
            raise
        except Exception:
            if published and published[0] and ns['_restore_api_policies'](*published):
                raise RuntimeError('Service activation could not roll back gateway policy') from None
            state.attempts += 1
            state.last_error = '服务生效检查未通过，请检查产品接入与授权'
            state.status = 'retrying' if state.attempts < 3 else 'needs_manual'
            state.next_check_at = at + timedelta(seconds=10) if state.attempts < 3 else utc(order.service_ends_at)
            transition(db, order, 'pending_fulfillment', 'exception', actor, state.last_error)
            ns['audit'](db, actor.email or actor.id, 'service_activation_failed', 'order', order.id,
                state.last_error, category='delivery', order_id=order.id,
                after={'attempts': state.attempts, 'status': state.status})
        else:
            # Persist verification intent before publishing gateway rights.
            state.status, state.next_check_at, state.last_error = 'activation_verified', at, ''
        return True

    @ns['app'].post('/api/orders/{order_id}/service-retry')
    def retry(order_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        ns['require_delivery_operator'](user)
        if not user.is_active or user.activation_status != 'active' or user.verified_status != 'verified':
            raise HTTPException(403, '账号未激活或未完成实名认证')
        order = db.get(Order, order_id, with_for_update=True)
        state = db.get(State, order_id, with_for_update=True)
        if not order or not state:
            raise HTTPException(404, '订阅生效任务不存在')
        if state.status not in {'retrying', 'needs_manual'} or utc(ns['now']()) >= utc(order.service_ends_at):
            raise HTTPException(409, '当前状态不能重试服务生效')
        state.status, state.attempts, state.next_check_at = 'scheduled', 0, utc(ns['now']())
        ns['audit'](db, user.email or user.id, 'retry_service_activation', 'order', order.id,
            category='delivery', order_id=order.id, after={'status': 'scheduled'})
        db.commit()
        return {'order_id': order.id, 'status': state.status}

    hooks = dict(State=State, initialize=initialize, delivered=delivered, process_due=process_due,
                 change_applied=change_applied, describe=describe)
    ns['order_service_lifecycle'] = hooks
    return hooks
