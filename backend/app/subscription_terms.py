"""TRD-BE-009/010 persisted subscription calendar and renewal routes.

Integration contract:
* Call install(globals()) once after create_order is defined, BEFORE startup
  Base.metadata.create_all. It registers models in Base and ns, and HTTP routes.
* After quoting, set quoted['buyer_enterprise_id'] from the validated request and
  call validate_purchase(db, user, quoted) before creating/paying an order.
* Lock/refresh the parent Order, verify actual API/model/SaaS delivery success,
  set delivery_status to pending_acceptance (or accepted), THEN call
  on_delivery_success(db, order, user, at=aware_success_time). A status flag alone
  is not verification; this is a trusted internal hook, never a public action.
* Hooks flush but NEVER commit. Caller rolls back every failure, including later
  audit/provisioning failures. Quote/Product locks precede enterprise/combination
  locks in purchase/renewal; delivery never acquires Product locks afterward.
* current_entitlement returns None without an active paid unrefunded term. Its
  limits and period identity replace additive order quota calculations; future
  terms do not reset buckets. No historical SaaS records are imported/changed.
* Renewal calls ns['create_quoted_order'](db, user, quoted), returning an Order
  without commit/payment/provisioning. Route commits order + request together.
"""

import hashlib
import json
import secrets
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.types import DateTime, TypeDecorator

from .order_economics import money
from .subscription_calendar import CalendarAnchor, validate_months


MONTHLY = frozenset({'api', 'model_api', 'tenant_access'})
LIMIT_KEYS = ('rate_limit_per_minute', 'daily_quota', 'monthly_quota', 'quota_amount')


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('订阅时间必须包含时区')
    return value.astimezone(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLite drops timezone metadata; restore the UTC value we always store."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else utc(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else utc(value)


class RenewalBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    subscription_months: int = Field(ge=1, le=36, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r'^\S+$')


def install(ns):
    if 'subscription_terms' in ns:
        return ns['subscription_terms']
    Base, Order, Enterprise = ns['Base'], ns['Order'], ns['Enterprise']

    class SubscriptionCombination(Base):
        __tablename__ = 'subscription_combinations'
        __table_args__ = (
            UniqueConstraint('enterprise_id', 'product_id', name='uq_subscription_enterprise_product'),
            CheckConstraint('generation >= 1 AND total_months >= 1', name='ck_subscription_combination_calendar'),
            CheckConstraint("status IN ('active', 'revoked')", name='ck_subscription_combination_status'),
        )
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        enterprise_id = Column(ForeignKey('enterprises.id'), nullable=False, index=True)
        product_id = Column(ForeignKey('products.id'), nullable=False, index=True)
        delivery_method = Column(String(40), nullable=False)
        version_id = Column(String(36), nullable=False)
        version_code = Column(String(60), nullable=False)
        anchor_at = Column(UTCDateTime(), nullable=False)
        generation = Column(Integer, nullable=False, default=1)
        total_months = Column(Integer, nullable=False)
        status = Column(String(20), nullable=False, default='active')
        updated_at = Column(UTCDateTime(), nullable=False, default=ns['now'])

    class SubscriptionTerm(Base):
        __tablename__ = 'subscription_terms'
        __table_args__ = (
            UniqueConstraint('order_id', name='uq_subscription_term_order'),
            CheckConstraint('generation >= 1 AND start_month >= 0 AND months BETWEEN 1 AND 36',
                            name='ck_subscription_term_calendar'),
            CheckConstraint("status IN ('active', 'revoked')", name='ck_subscription_term_status'),
        )
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        order_id = Column(ForeignKey('orders.id'), nullable=False)
        combination_id = Column(ForeignKey('subscription_combinations.id'), nullable=False, index=True)
        generation = Column(Integer, nullable=False)
        version_id = Column(String(36), nullable=False)
        version_code = Column(String(60), nullable=False)
        start_month = Column(Integer, nullable=False)
        months = Column(Integer, nullable=False)
        starts_at = Column(UTCDateTime(), nullable=False)
        ends_at = Column(UTCDateTime(), nullable=False)
        status = Column(String(20), nullable=False, default='active')
        created_at = Column(UTCDateTime(), nullable=False, default=ns['now'])

    class SubscriptionRenewal(Base):
        """Durable request identity, committed atomically with the unpaid order."""
        __tablename__ = 'subscription_renewals'
        __table_args__ = (UniqueConstraint('combination_id', 'idempotency_key', name='uq_subscription_renewal_key'),)
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        enterprise_id = Column(ForeignKey('enterprises.id'), nullable=False)
        combination_id = Column(ForeignKey('subscription_combinations.id'), nullable=False)
        idempotency_key = Column(String(128), nullable=False)
        request_hash = Column(String(64), nullable=False)
        source_order_id = Column(ForeignKey('orders.id'), nullable=False)
        months = Column(Integer, nullable=False)
        order_id = Column(ForeignKey('orders.id'), nullable=False, unique=True)

    Combination, Term, Renewal = SubscriptionCombination, SubscriptionTerm, SubscriptionRenewal

    def lock_combination(db, enterprise_id, product_id):
        # A preexisting enterprise row serializes even the first combination
        # insert on PostgreSQL; the unique constraint is the final backstop.
        enterprise = db.scalar(select(Enterprise).where(Enterprise.id == enterprise_id)
                               .with_for_update().execution_options(populate_existing=True))
        if enterprise is None:
            raise HTTPException(409, '订阅所属企业不存在')
        return db.scalar(select(Combination).where(Combination.enterprise_id == enterprise_id,
                         Combination.product_id == product_id).with_for_update()
                         .execution_options(populate_existing=True))

    def snapshot(order):
        economic = ns['order_economics']['economic_snapshot'](order)
        delivery = ns['order_economics']['delivery_snapshot'](order)
        if (economic.get('billing_unit') != 'month'
                or economic.get('version_code') != order.product_version_code
                or not order.product_version_id or not order.product_version_code):
            raise HTTPException(409, '订阅版本与月度经济快照不一致')
        quota = delivery.get('quota')
        if not isinstance(quota, dict) or any(type(quota.get(key)) is not int or quota[key] < 0 for key in LIMIT_KEYS):
            raise HTTPException(409, '订阅冻结额度配置无效')
        validate_months(order.subscription_months)
        return delivery

    def paid_snapshot(order):
        if (order.snapshot_version != 1 or order.delivery_method_snapshot not in MONTHLY
                or order.payment_status != 'paid' or money(order.paid_amount) != money(order.amount)
                or money(order.refunded_amount) != 0
                or order.main_status in {'cancelled', 'closed', 'refunded'}
                or order.delivery_status not in {'pending_acceptance', 'accepted'}):
            raise HTTPException(409, '订阅须使用已全额支付、未退款且交付成功的新版快照订单')
        return snapshot(order)

    def expires(combination):
        return CalendarAnchor(combination.anchor_at).boundary(combination.total_months)

    def validate_purchase(db, user, quoted):
        """quoted must include validated buyer_enterprise_id; retains locks to commit."""
        enterprise_id = quoted.get('buyer_enterprise_id')
        if not enterprise_id:
            raise HTTPException(400, '报价必须包含购买企业标识')
        ns['trading_policy']['require_buyer'](db, user, enterprise_id)
        public = quoted['public']
        if public['delivery_method'] not in MONTHLY:
            return None
        combination = lock_combination(db, enterprise_id, public['product_id'])
        if combination and combination.status == 'active' and utc(ns['now']()) < expires(combination):
            if (combination.version_id != public['product_version_id']
                    or combination.version_code != public['product_version_code']
                    or combination.delivery_method != public['delivery_method']):
                raise HTTPException(409, '有效订阅使用其他版本，请使用升级流程')
        return combination

    def on_delivery_success(db, order, user, at=None):
        """Trusted verified-success caller only; parent Order must be locked/refreshed."""
        if order.snapshot_version != 1 or order.delivery_method_snapshot not in MONTHLY:
            return None
        success_at = utc(at if at is not None else ns['now']())
        paid_snapshot(order)
        combination = lock_combination(db, order.buyer_enterprise_id, order.product_id)
        existing = db.scalar(select(Term).where(Term.order_id == order.id).with_for_update()
                             .execution_options(populate_existing=True))
        if existing is not None:
            return existing
        if order.delivery_status not in {'pending_acceptance', 'accepted'}:
            raise HTTPException(409, '实际交付成功后才能创建订阅期限')
        live = combination is not None and combination.status == 'active' and success_at < expires(combination)
        if live and (combination.version_id != order.product_version_id
                     or combination.version_code != order.product_version_code
                     or combination.delivery_method != order.delivery_method_snapshot):
            raise HTTPException(409, '有效订阅使用其他版本，请使用升级流程')
        if live and success_at < combination.anchor_at:
            raise HTTPException(409, '交付成功时间不能早于订阅起点')
        before = {} if combination is None else {
            'anchor_at': combination.anchor_at.isoformat(), 'generation': combination.generation,
            'version_id': combination.version_id, 'version_code': combination.version_code,
            'total_months': combination.total_months, 'expires_at': expires(combination).isoformat(),
            'status': combination.status}
        start_month = combination.total_months if live else 0
        anchor = CalendarAnchor(combination.anchor_at if live else success_at)
        bounds = anchor.term(order.subscription_months, start_month=start_month)
        if combination is None:
            combination = Combination(enterprise_id=order.buyer_enterprise_id, product_id=order.product_id,
                delivery_method=order.delivery_method_snapshot, version_id=order.product_version_id,
                version_code=order.product_version_code, anchor_at=success_at, generation=1,
                total_months=order.subscription_months, status='active', updated_at=success_at)
            db.add(combination)
            db.flush()
        else:
            if not live:
                combination.generation += 1
                combination.anchor_at = success_at
                combination.version_id = order.product_version_id
                combination.version_code = order.product_version_code
                combination.delivery_method = order.delivery_method_snapshot
            combination.total_months = start_month + order.subscription_months
            combination.status = 'active'
            combination.updated_at = success_at
        term = Term(order_id=order.id, combination_id=combination.id, generation=combination.generation,
                    version_id=order.product_version_id, version_code=order.product_version_code,
                    start_month=start_month, months=order.subscription_months,
                    starts_at=bounds.start, ends_at=bounds.end, status='active', created_at=success_at)
        db.add(term)
        db.flush()
        event_version = term.id
        after = {'combination_id': combination.id, 'term_id': term.id, 'order_id': order.id,
            'anchor_at': combination.anchor_at.isoformat(), 'generation': combination.generation,
            'version_id': combination.version_id, 'version_code': combination.version_code,
            'total_months': combination.total_months, 'expires_at': expires(combination).isoformat(),
            'starts_at': term.starts_at.isoformat(), 'ends_at': term.ends_at.isoformat(),
            'months': term.months, 'status': combination.status, 'event_version': event_version}
        ns['audit'](db, user.email or user.phone or user.id, 'subscription_term_granted',
                    'subscription_term', term.id, '交付成功后登记订阅期限', category='delivery',
                    business_domain='trading', tenant_id=order.buyer_enterprise_id, order_id=order.id,
                    before=before, after=after)
        ns['settlement_metering']['record'](db, order, 'subscription_term', term.months, 'month',
                    'subscription_term', event_version, source_id=term.id, actor_id=user.id,
                    period_start=term.starts_at, period_end=term.ends_at,
                    evidence={'action': 'subscription_term_granted', 'before': before, 'after': after,
                              'billing_effect': 'none', 'quota_effect': 'calendar_only'})
        ns['notify_business_event'](db, '订阅期限已登记',
                    f'订单 {order.order_no}：订阅版本 {term.version_code}，期限已登记，请查看订单详情。',
                    'order', order.id, event_key=f'subscription_term_granted:{event_version}', category='order',
                    tenant_id=order.buyer_enterprise_id,
                    recipient_ids=[order.buyer_user_id] if order.buyer_user_id else [],
                    enterprise_ids=[order.buyer_enterprise_id, order.provider_enterprise_id])
        return term

    def current_entitlement(db, enterprise_id, product_id, at=None):
        instant = utc(at if at is not None else ns['now']())
        combination = db.scalar(select(Combination).where(Combination.enterprise_id == enterprise_id,
                                Combination.product_id == product_id).execution_options(populate_existing=True))
        if combination is None or combination.status != 'active':
            return None
        rows = db.execute(select(Term, Order).join(Order, Term.order_id == Order.id).where(
            Term.combination_id == combination.id, Term.generation == combination.generation,
            Term.status == 'active', Term.version_id == combination.version_id,
            Term.version_code == combination.version_code, Term.starts_at <= instant, Term.ends_at > instant)
            .order_by(Term.start_month).execution_options(populate_existing=True)).all()
        anchor = CalendarAnchor(combination.anchor_at)
        for term, order in rows:
            if (order.buyer_enterprise_id != enterprise_id or order.product_id != product_id
                    or order.product_version_id != term.version_id or order.product_version_code != term.version_code
                    or order.delivery_method_snapshot != combination.delivery_method
                    or order.subscription_months != term.months):
                continue
            expected = anchor.term(term.months, start_month=term.start_month)
            if term.starts_at != expected.start or term.ends_at != expected.end:
                continue
            try:
                delivery = paid_snapshot(order)
            except HTTPException:
                continue
            period = anchor.period_at(instant)
            if period is None or instant >= expires(combination):
                continue
            return {'combination_id': combination.id, 'generation': combination.generation,
                    'delivery_method': combination.delivery_method, 'anchor_at': combination.anchor_at,
                    'version_id': combination.version_id, 'version_code': combination.version_code,
                    'expires_at': expires(combination), 'term_id': term.id, 'order_id': order.id,
                    'limits': {key: delivery['quota'][key] for key in LIMIT_KEYS},
                    'period_key': f'{combination.id}:{combination.generation}:{period.index}',
                    'period_index': period.index, 'period_starts_at': period.start, 'period_ends_at': period.end}
        return None

    def require_read(db, user, order):
        if user.platform_role in {'super_admin', 'finance_settlement'}:
            if not user.is_active or user.activation_status != 'active':
                raise HTTPException(403, '仅活跃的平台账号可查看')
            return
        ns['trading_policy']['require_buyer'](db, user, order.buyer_enterprise_id)

    def quota_periods(db, enterprise_id, product_id):
        """Granted current-generation monthly buckets, including future paid terms.

        monthly_quota is the configured integer, with zero meaning unlimited.
        quota_amount has separate units and is never converted or added here.
        """
        combination = db.scalar(select(Combination).where(Combination.enterprise_id == enterprise_id,
                                Combination.product_id == product_id).execution_options(populate_existing=True))
        if combination is None or combination.status != 'active':
            return []
        anchor = CalendarAnchor(combination.anchor_at)
        result = []
        rows = db.execute(select(Term, Order).join(Order, Term.order_id == Order.id).where(
            Term.combination_id == combination.id, Term.generation == combination.generation,
            Term.status == 'active', Term.version_id == combination.version_id,
            Term.version_code == combination.version_code).order_by(Term.start_month)
            .execution_options(populate_existing=True)).all()
        for term, order in rows:
            if (order.buyer_enterprise_id != enterprise_id or order.product_id != product_id
                    or order.product_version_id != term.version_id or order.product_version_code != term.version_code
                    or order.delivery_method_snapshot != combination.delivery_method
                    or order.subscription_months != term.months):
                continue
            bounds = anchor.term(term.months, start_month=term.start_month)
            if term.starts_at != bounds.start or term.ends_at != bounds.end:
                continue
            try:
                delivery = paid_snapshot(order)
            except HTTPException:
                continue
            for index in range(term.start_month, term.start_month + term.months):
                if index >= combination.total_months:
                    break
                period = anchor.period(index)
                result.append({'start_at': int(period.start.timestamp()), 'end_at': int(period.end.timestamp()),
                    'period_key': f'{combination.id}:{combination.generation}:{index}',
                    'version_id': term.version_id, 'version_code': term.version_code,
                    **{key: delivery['quota'][key] for key in LIMIT_KEYS[:3]}})
        return result

    @ns['app'].get('/api/orders/{order_id}/subscription')
    def get_subscription(order_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        order = db.get(Order, order_id)
        if order is None:
            raise HTTPException(404, '订单不存在')
        require_read(db, user, order)
        combination = db.scalar(select(Combination).where(Combination.enterprise_id == order.buyer_enterprise_id,
                                Combination.product_id == order.product_id))
        if combination is None or order.snapshot_version != 1 or order.delivery_method_snapshot not in MONTHLY:
            return {'item': {'available': False, 'id': None, 'product_id': order.product_id,
                    'enterprise_id': order.buyer_enterprise_id, 'version_id': None, 'version_code': None,
                    'anchor_at': None, 'generation': None, 'expires_at': None, 'status': 'unavailable',
                    'terms': [], 'current_period': None, 'limits': None, 'can_renew': False}}
        terms = db.execute(select(Term, Order.order_no).join(Order, Term.order_id == Order.id)
                           .where(Term.combination_id == combination.id)
                           .order_by(Term.generation, Term.start_month, Term.id)).all()
        entitlement = current_entitlement(db, order.buyer_enterprise_id, order.product_id)
        source_term = db.scalar(select(Term).where(Term.order_id == order.id, Term.status == 'active'))
        can_renew = (not user.platform_role and combination.status == 'active' and source_term is not None
                     and source_term.generation == combination.generation
                     and order.product_version_id == combination.version_id
                     and order.product_version_code == combination.version_code
                     and order.delivery_method_snapshot == combination.delivery_method)
        if can_renew:
            try:
                paid_snapshot(order)
            except HTTPException:
                can_renew = False
        return {'item': {'available': entitlement is not None, 'id': combination.id,
                'product_id': combination.product_id, 'enterprise_id': combination.enterprise_id,
                'generation': combination.generation, 'anchor_at': combination.anchor_at,
                'version_id': combination.version_id, 'version_code': combination.version_code,
                'expires_at': expires(combination), 'status': combination.status,
                'current_period': None if entitlement is None else {
                    'starts_at': entitlement['period_starts_at'], 'ends_at': entitlement['period_ends_at'],
                    'index': entitlement['period_index']},
                'limits': None if entitlement is None else entitlement['limits'],
                'can_renew': can_renew,
                'terms': [{'order_id': term.order_id, 'order_no': number, 'version_code': term.version_code,
                    'starts_at': term.starts_at, 'ends_at': term.ends_at,
                    'status': term.status, 'months': term.months}
                    for term, number in terms]}}

    @ns['app'].post('/api/orders/{order_id}/renewal')
    def renew(order_id: str, body: RenewalBody, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        try:
            source = db.get(Order, order_id)
            if source is None:
                raise HTTPException(404, '订单不存在')
            ns['trading_policy']['require_buyer'](db, user, source.buyer_enterprise_id)
            # Lock Product before the combination, but let retries return even
            # when the catalog version is no longer available for new quotes.
            db.scalar(select(ns['Product']).where(ns['Product'].id == source.product_id).with_for_update())
            combination = db.scalar(select(Combination).where(
                Combination.enterprise_id == source.buyer_enterprise_id, Combination.product_id == source.product_id))
            request_hash = hashlib.sha256(json.dumps({'source_order_id': source.id,
                    'months': body.subscription_months}, sort_keys=True).encode()).hexdigest()
            previous = db.scalar(select(Renewal).where(Renewal.combination_id == (combination.id if combination else ''),
                                 Renewal.idempotency_key == body.idempotency_key))
            if previous:
                if previous.request_hash != request_hash:
                    raise HTTPException(409, '幂等标识已用于其他续费请求')
                return ns['order_out'](db.get(Order, previous.order_id))
            if source.snapshot_version != 1 or source.delivery_method_snapshot not in MONTHLY:
                raise HTTPException(409, '仅新版快照订阅订单支持续费')
            order_body = ns['OrderBody'](product_id=source.product_id, product_version_id=source.product_version_id,
                    buyer_enterprise_id=source.buyer_enterprise_id, subscription_months=body.subscription_months)
            quoted = ns['order_economics']['quote'](db, user, order_body, lock=True)
            quoted['buyer_enterprise_id'] = source.buyer_enterprise_id
            combination = validate_purchase(db, user, quoted)
            term = db.scalar(select(Term).where(Term.order_id == source.id, Term.status == 'active'))
            paid_snapshot(source)
            if (combination is None or combination.status != 'active' or term is None
                    or term.generation != combination.generation
                    or source.product_version_id != combination.version_id
                    or source.product_version_code != combination.version_code
                    or source.delivery_method_snapshot != combination.delivery_method):
                raise HTTPException(409, '续费须使用当前订阅版本的有效源订单')
            if (quoted['public']['product_version_code'] != combination.version_code
                    or quoted['public']['delivery_method'] != combination.delivery_method):
                raise HTTPException(409, '续费版本配置已变更')
            created = ns['create_quoted_order'](db, user, quoted)
            db.flush()
            if (created is None or created.payment_status != 'unpaid' or money(created.paid_amount) != 0
                    or created.delivery_status != 'not_started'
                    or created.buyer_enterprise_id != source.buyer_enterprise_id
                    or created.product_id != source.product_id or created.product_version_id != combination.version_id
                    or created.product_version_code != combination.version_code
                    or created.delivery_method_snapshot != combination.delivery_method
                    or created.subscription_months != body.subscription_months or created.snapshot_version != 1):
                raise HTTPException(409, '续费须生成版本一致、未支付且未开通的新订单')
            snapshot(created)
            created.business_type = 'renewal'
            created.related_order_id = source.id
            db.add(Renewal(enterprise_id=source.buyer_enterprise_id, idempotency_key=body.idempotency_key,
                          combination_id=combination.id, request_hash=request_hash,
                          source_order_id=source.id, months=body.subscription_months, order_id=created.id))
            db.commit()
            return ns['order_out'](created)
        except IntegrityError as exc:
            db.rollback()
            constraint = getattr(getattr(exc.orig, 'diag', None), 'constraint_name', None)
            if constraint == 'uq_subscription_renewal_key' or (
                    'subscription_renewals.combination_id, subscription_renewals.idempotency_key' in str(exc.orig)):
                raise HTTPException(409, '幂等标识已用于其他续费请求，请查询原续费订单') from exc
            raise
        except Exception:
            db.rollback()
            raise

    hooks = {'SubscriptionCombination': Combination, 'SubscriptionTerm': Term, 'SubscriptionRenewal': Renewal,
             'Combination': Combination, 'Term': Term,
             'validate_purchase': validate_purchase, 'on_delivery_success': on_delivery_success,
             'current_entitlement': current_entitlement, 'lock_combination': lock_combination,
             'quota_periods': quota_periods}
    ns.update({key: hooks[key] for key in ('SubscriptionCombination', 'SubscriptionTerm', 'SubscriptionRenewal',
                                         'Combination', 'Term')})
    ns['subscription_terms'] = hooks
    return hooks
