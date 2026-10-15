"""Durable orchestration for subscription version changes.

Integration adapters are deliberately explicit: quote preparation must return
validated public/internal snapshots, and resource operations must be replayable.
"""
from datetime import timedelta
from decimal import Decimal
import hashlib
import hmac
import importlib
import json
import re
import secrets

from fastapi import Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, Numeric, String, Text, select, text
from sqlalchemy.exc import IntegrityError

from .order_economics import money
from .subscription_terms import UTCDateTime
from .refund_funding_store import cents
from .refund_funding import _money


OPEN_STATUSES = ('pending_payment', 'resource_pending', 'retrying', 'compensating', 'refund_pending', 'exception')
RETRY_DELAY = 10
MAX_ATTEMPTS = 3


def quote_token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def quote_claims_match_owner(claims, user_id, order_id):
    return (isinstance(claims, dict) and claims.get('purpose') == 'version_change_quote'
            and claims.get('sub') == user_id and claims.get('order_id') == order_id)


class CreateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    quote_token: str = Field(min_length=1, max_length=8192, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r'^\S+$')


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    payment_id: str = Field(min_length=1, max_length=36, strict=True)
    proof: str = Field(default='', max_length=1000, strict=True)


def install(ns):
    if 'version_change_saga' in ns:
        return ns['version_change_saga']
    Base, Order = ns['Base'], ns['Order']

    class VersionChangeRequest(Base):
        __tablename__ = 'version_change_requests'
        __table_args__ = (
            CheckConstraint("kind IN ('upgrade','downgrade','no_payment')", name='ck_version_change_kind'),
            Index('uq_version_change_open_combination', 'combination_id', unique=True,
                  sqlite_where=text("status IN ('pending_payment','resource_pending','retrying','compensating','refund_pending','exception')"),
                  postgresql_where=text("status IN ('pending_payment','resource_pending','retrying','compensating','refund_pending','exception')")),
        )
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        source_order_id = Column(ForeignKey('orders.id'), nullable=False, index=True)
        enterprise_id = Column(ForeignKey('enterprises.id'), nullable=False, index=True)
        combination_id = Column(ForeignKey('subscription_combinations.id'), nullable=False, index=True)
        target_version_id = Column(String(36), nullable=False)
        target_version_code = Column(String(60), nullable=False)
        old_version_id = Column(String(36), nullable=False)
        old_version_code = Column(String(60), nullable=False)
        kind = Column(String(20), nullable=False)
        status = Column(String(30), nullable=False, index=True)
        idempotency_key = Column(String(128), nullable=False)
        request_hash = Column(String(64), nullable=False)
        quote_json = Column(Text, nullable=False)
        prepared_json = Column(Text, nullable=False)
        target_snapshots_json = Column(Text, nullable=False)
        resource_plan_json = Column(Text, nullable=False, default='{}')
        evidence_json = Column(Text, nullable=False, default='{}')
        funding_order_id = Column(ForeignKey('orders.id'), nullable=True)
        resource_applied = Column(Boolean, nullable=False, default=False)
        compensation_started = Column(Boolean, nullable=False, default=False)
        compensation_retries = Column(Integer, nullable=False, default=0)
        retries = Column(Integer, nullable=False, default=0)
        next_retry_at = Column(UTCDateTime(), nullable=True)
        last_error = Column(String(80), nullable=False, default='')
        created_by = Column(String(180), nullable=False)
        created_at = Column(UTCDateTime(), nullable=False, default=ns['now'])
        updated_at = Column(UTCDateTime(), nullable=False, default=ns['now'])
        completed_at = Column(UTCDateTime(), nullable=True)
        lease_owner = Column(String(120), nullable=False, default='')
        lease_expires_at = Column(UTCDateTime(), nullable=True)

    class VersionChangeFund(Base):
        __tablename__ = 'version_change_funds'
        __table_args__ = (CheckConstraint("kind IN ('payment','refund')", name='ck_version_change_fund_kind'),)
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        change_id = Column(ForeignKey('version_change_requests.id'), nullable=False, index=True)
        kind = Column(String(20), nullable=False)
        payment_id = Column(ForeignKey('payments.id'), nullable=True, index=True)
        refund_id = Column(ForeignKey('refunds.id'), nullable=True, index=True)
        amount = Column(Numeric(14, 2), nullable=False)
        idempotency_key = Column(String(160), nullable=False, unique=True)

    class Idempotency(Base):
        __tablename__ = 'version_change_idempotency'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        combination_id = Column(String(36), nullable=False, index=True)
        key = Column(String(128), nullable=False)
        request_hash = Column(String(64), nullable=False)
        change_id = Column(ForeignKey('version_change_requests.id'), nullable=False)
        __table_args__ = (Index('uq_version_change_idempotency', 'combination_id', 'key', unique=True),)

    ns.update(VersionChangeRequest=VersionChangeRequest, VersionChangeFund=VersionChangeFund)

    def fail(message, code=409):
        raise HTTPException(code, message)

    def adapter(name, method):
        group = ns.get(name)
        hook = group.get(method) if isinstance(group, dict) else None
        if not callable(hook):
            raise RuntimeError(f'missing required {name}.{method} adapter')
        return hook

    def public(change, db=None):
        quote = json.loads(change.quote_json)
        funding = db.get(Order, change.funding_order_id) if db is not None and change.funding_order_id else None
        return {'id': change.id, 'change_no': 'VC-' + change.id[:12].upper(),
                'source_order_id': change.source_order_id, 'combination_id': change.combination_id,
                'enterprise_id': change.enterprise_id, 'kind': change.kind, 'status': change.status,
                'current_version_code': change.old_version_code,
                'target_version_code': change.target_version_code,
                'net_difference': quote.get('net_difference', 0),
                'payment_amount': quote.get('payment_amount', 0),
                'refund_amount': quote.get('refund_amount', 0), 'currency': quote.get('currency', ''),
                'funding_order_id': change.funding_order_id,
                'funding_order_no': getattr(funding, 'order_no', None),
                'payment_status': getattr(funding, 'payment_status', 'unpaid' if change.kind == 'upgrade' else 'not_required'),
                'refund_status': _public_refund_status(change, db),
                'retry_count': change.retries, 'last_error': change.last_error,
                'created_at': change.created_at, 'updated_at': change.updated_at,
                'completed_at': change.completed_at}

    def _public_refund_status(change, db):
        if change.status in {'cancelled'} or (change.kind == 'downgrade' and change.status == 'compensated'):
            return 'not_required'
        if change.kind != 'downgrade' and change.status not in {'compensating','compensated'}:
            return 'not_required'
        if db is None:
            return 'pending' if change.kind == 'downgrade' else 'not_required'
        rows = db.scalars(select(VersionChangeFund).where(VersionChangeFund.change_id == change.id,
            VersionChangeFund.kind == 'refund')).all()
        if not rows:
            return 'pending' if change.kind == 'downgrade' else 'not_required'
        refunds = [db.get(ns['Refund'], row.refund_id) for row in rows if row.refund_id]
        return 'completed' if len(refunds) == len(rows) and all(
            item and item.status == 'completed' for item in refunds) else 'pending'

    def require_buyer_read(db, user, enterprise_id):
        if user.platform_role in {'super_admin', 'finance_settlement'}:
            if not user.is_active or user.activation_status != 'active' or user.verified_status != 'verified':
                fail('账号不可查看', 403)
        else:
            ns['trading_policy']['require_buyer'](db, user, enterprise_id)

    def create(db, user, order_id, body):
        order = db.get(Order, order_id)
        if not order:
            fail('订单不存在', 404)
        ns['trading_policy']['require_buyer'](db, user, order.buyer_enterprise_id)
        db.scalar(select(ns['Product']).where(ns['Product'].id == order.product_id).with_for_update())
        order = db.scalar(select(Order).where(Order.id == order_id).with_for_update()
                          .execution_options(populate_existing=True))
        if not order:
            fail('订单不存在', 404)
        token = body.quote_token
        try:
            claims = ns['jwt'].decode(token, ns['JWT_SECRET'], algorithms=['HS256'],
                audience='market-version-change-quote', options={'verify_exp': False, 'verify_iat': False})
        except (ns['jwt'].PyJWTError, TypeError, ValueError):
            fail('版本变更报价凭据无效', 400)
        if not quote_claims_match_owner(claims, user.id, order_id):
            fail('报价不属于当前用户和订单', 403)
        terms = ns['subscription_terms']
        locked_combo = terms['lock_combination'](db, order.buyer_enterprise_id, order.product_id)
        if not locked_combo:
            fail('订阅组合不存在')
        payload_hash = quote_token_digest(token)
        old = db.scalar(select(Idempotency).where(Idempotency.combination_id == locked_combo.id,
            Idempotency.key == body.idempotency_key).with_for_update())
        if old:
            if not hmac.compare_digest(old.request_hash, payload_hash):
                fail('幂等标识已用于其他报价请求')
            existing = db.get(VersionChangeRequest, old.change_id)
            if not existing or existing.source_order_id != order_id or existing.created_by != user.id:
                fail('幂等请求与当前用户或订单不匹配', 403)
            return public(existing, db)
        assert_no_ordinary_refunds(db, locked_combo)
        quotes = ns['version_change_quotes']
        if not callable(quotes.get('validate')) or not callable(quotes.get('prepare')):
            raise RuntimeError('version quote adapter is incomplete')
        verified = quotes['validate'](db, user, order_id, token)
        prepared = quotes['prepare'](db, user, order_id, claims['target_id'], claims['confirmed'],
                                    __import__('datetime').datetime.fromisoformat(claims['at']))
        combo = prepared.get('combo')
        if not combo:
            fail('订阅组合不存在')
        quote = {**prepared['public'], **{k: verified[k] for k in ('quote_id','created_at','expires_at')},
                 'target_version_id': claims['target_id'], 'execution_ready': False}
        internal = prepared['internal']
        if quote.get('order_id') != order_id or quote.get('combination_id') != combo.id:
            fail('报价与当前订阅不匹配')
        if combo.id != locked_combo.id:
            fail('订阅组合已变化，请重新报价')
        active = db.scalar(select(VersionChangeRequest).where(
            VersionChangeRequest.combination_id == combo.id,
            VersionChangeRequest.status.in_(OPEN_STATUSES)))
        if active:
            fail('该订阅已有进行中的版本变更')
        change_id = secrets.token_hex(16)
        kind = quote.get('action')
        if kind not in {'upgrade', 'downgrade', 'no_payment'}:
            fail('报价变更类型无效')
        target_snapshots = prepared.get('snapshots_by_term')
        if not isinstance(target_snapshots, dict) or not target_snapshots:
            fail('报价缺少冻结的目标订阅快照')
        change = VersionChangeRequest(id=change_id, source_order_id=order_id,
            enterprise_id=order.buyer_enterprise_id, combination_id=combo.id,
            target_version_id=quote['target_version_id'], target_version_code=quote['target_version_code'],
            old_version_id=combo.version_id, old_version_code=combo.version_code, kind=kind,
            status='pending_payment' if kind == 'upgrade' else 'resource_pending',
            idempotency_key=body.idempotency_key, request_hash=payload_hash,
            quote_json=json.dumps({k:v for k,v in quote.items() if k not in {'quote_token','cost','unit_cost'}},
                                  sort_keys=True),
            prepared_json=json.dumps(internal, sort_keys=True),
            target_snapshots_json=json.dumps(target_snapshots, sort_keys=True), created_by=user.id,
            next_retry_at=__import__('datetime').datetime.fromisoformat(quote['expires_at']) if kind == 'upgrade' else None)
        db.add(change)
        db.flush()
        db.add(Idempotency(combination_id=combo.id, key=body.idempotency_key,
                           request_hash=payload_hash, change_id=change_id))
        db.flush()
        if kind == 'downgrade':
            reserve_downgrade_refunds(db, change)
        ns['version_change_ledger']['stage'](db, change_id, prepared, user)
        db.flush()
        return public(change, db)

    def assert_no_ordinary_refunds(db, combo):
        Term, Refund = ns['subscription_terms']['Term'], ns['Refund']
        order_ids = select(Term.order_id).where(Term.combination_id == combo.id, Term.generation == combo.generation)
        statuses = ('pending', 'requested', 'approved', 'executing', 'failed', 'completed')
        Case = ns.get('refund_workflow', {}).get('Case')
        Allocation = ns.get('refund_funding_store', {}).get('FundingAllocation')
        for refund in db.scalars(select(Refund).where(Refund.order_id.in_(order_ids), Refund.status.in_(statuses))):
            case = db.get(Case, refund.id) if Case is not None else None
            allocated = Allocation is not None and db.scalar(select(Allocation.rowid).where(
                Allocation.refund_id == refund.id).limit(1)) is not None
            ordinary = bool(case and case.source == 'ordinary') or allocated
            controlled = db.scalar(select(VersionChangeFund.id).where(
                VersionChangeFund.refund_id == refund.id).limit(1)) is not None
            if ordinary or not controlled and refund.status != 'completed':
                fail('普通退款占用或已完成权益调整需要重核，不能发起版本变更')

    def assert_no_pending(db, enterprise_id, product_id):
        if enterprise_id is None or product_id is None:
            raise RuntimeError('assert_no_pending requires enterprise_id and product_id')
        combo = ns['subscription_terms']['lock_combination'](db, enterprise_id, product_id)
        if combo and db.scalar(select(VersionChangeRequest.id).where(
                VersionChangeRequest.combination_id == combo.id,
                VersionChangeRequest.status.in_(OPEN_STATUSES)).limit(1)):
            fail('订阅有进行中的版本变更，请稍后重试')

    def handle_transition(db, user, order, body):
        if order.business_type != 'version_change_payment':
            return False
        if body.action not in {'start_payment', 'confirm_payment'}:
            fail('版本变更支付订单仅支持支付操作')
        link = db.scalar(select(VersionChangeFund).where(VersionChangeFund.kind == 'payment',
            VersionChangeFund.payment_id == select(ns['Payment'].id).where(ns['Payment'].order_id == order.id).scalar_subquery()))
        if not link:
            fail('版本变更支付资金关联不存在')
        change = db.scalar(select(VersionChangeRequest).where(VersionChangeRequest.id == link.change_id).with_for_update())
        if not change:
            fail('版本变更请求不存在')
        order = db.scalar(select(Order).where(Order.id == order.id).with_for_update()
                          .execution_options(populate_existing=True))
        if (not order or order.business_type != 'version_change_payment'
                or change.funding_order_id != order.id):
            fail('版本变更支付订单已变化，请重试')
        if body.action == 'start_payment':
            if user.platform_role or order.buyer_enterprise_id != change.enterprise_id:
                fail('仅购买企业可发起支付', 403)
            ns['trading_policy']['require_buyer'](db, user, change.enterprise_id)
            membership = db.scalar(select(ns['Membership']).where(
                ns['Membership'].enterprise_id == change.enterprise_id,
                ns['Membership'].user_id == user.id, ns['Membership'].status == 'active'))
            if not membership or membership.role != 'super_admin':
                fail('仅购买企业超级管理员可发起支付', 403)
            if order.payment_status == 'paid':
                payment = db.scalar(select(ns['Payment']).where(ns['Payment'].order_id == order.id)
                                    .with_for_update())
                if payment and payment.status == 'paid':
                    return True
            if change.status != 'pending_payment' or order.payment_status not in {'unpaid', 'paying'}:
                fail('支付状态不允许发起')
            if change.next_retry_at and ns['now']() >= change.next_retry_at:
                fail('版本变更报价已过期，请重新报价')
            if order.payment_status == 'paying':
                return True
            order.payment_status = 'paying'
            payment = db.scalar(select(ns['Payment']).where(ns['Payment'].order_id == order.id).with_for_update())
            if not payment:
                fail('支付记录不存在')
            payment.status = 'paying'
        else:
            if user.platform_role in {'super_admin', 'finance_settlement'}:
                if (not user.is_active or user.activation_status != 'active'
                        or user.verified_status != 'verified'):
                    fail('平台账号未激活或未完成认证', 403)
            else:
                ns['trading_policy']['require_buyer'](db, user, change.enterprise_id)
                membership = db.scalar(select(ns['Membership']).where(
                    ns['Membership'].enterprise_id == change.enterprise_id,
                    ns['Membership'].user_id == user.id, ns['Membership'].status == 'active'))
                if not membership or membership.role != 'super_admin':
                    fail('仅购买企业超级管理员可模拟确认支付', 403)
            payment = db.scalar(select(ns['Payment']).where(ns['Payment'].order_id == order.id).with_for_update())
            if not payment:
                fail('支付记录或状态无效')
            if order.payment_status == 'paid' and payment.status == 'paid':
                return True
            if change.status != 'pending_payment' or order.payment_status not in {'unpaid','paying'}:
                fail('支付记录或状态无效')
            if change.next_retry_at and ns['now']() >= change.next_retry_at:
                fail('版本变更报价已过期，请重新报价')
            if payment.status != 'paid':
                payment.status = 'paid'; payment.paid_at = ns['now'](); payment.confirmed_by = user.name
                order.payment_status = 'paid'; order.paid_amount = order.amount
                db.add(ns['OrderStateLog'](order_id=order.id, domain='payment', from_status='paying',
                    to_status='paid', action='confirm_payment', reason='version change funding', operator=user.name))
                ns['audit'](db, user.email or user.id, 'confirm_version_change_payment', 'version_change',
                    change.id, category='payment', business_domain='version_change', order_id=order.id,
                    after={'payment_id': payment.id, 'amount': str(payment.amount)})
                change.status = 'resource_pending'
                change.next_retry_at = ns['now']()
        return True

    def lock_funding_payments(db, payment_ids):
        Payment = ns['Payment']
        rows = list(db.scalars(select(Payment).where(Payment.id.in_(set(payment_ids)))
            .order_by(Payment.id).with_for_update().execution_options(populate_existing=True)))
        if {row.id for row in rows} != set(payment_ids):
            fail('退款来源支付记录不存在')
        return {row.id: row for row in rows}

    def occupied(db, payment, *, include_funds=False):
        store = ns.get('refund_funding_store')
        if store:
            return cents(store['payment_reserved_total'](db, payment.id))
        Refund = ns['Refund']
        total = sum(cents(row.amount) for row in db.scalars(select(Refund).where(
            Refund.payment_id == payment.id, Refund.status.in_(['pending', 'completed']))))
        if include_funds:
            total += sum(cents(row.amount) for row in db.scalars(select(VersionChangeFund).where(
                VersionChangeFund.payment_id == payment.id, VersionChangeFund.kind == 'refund',
                VersionChangeFund.refund_id.is_(None))))
        return total

    def reserve_downgrade_refunds(db, change):
        requested = cents(json.loads(change.quote_json)['refund_amount'])
        if requested <= 0:
            return
        Term = ns['subscription_terms']['Term']
        combo = db.get(ns['subscription_terms']['Combination'], change.combination_id)
        term_order_ids = select(Term.order_id).where(Term.combination_id == combo.id,
            Term.generation == combo.generation, Term.status == 'active')
        Payment = ns['Payment']
        rows = db.execute(select(Payment, Order).join(Order, Order.id == Payment.order_id)
            .where(Order.id.in_(term_order_ids), Payment.status.in_(['paid','refunding','refunded']))
            .order_by(Payment.created_at, Payment.id)).all()
        sources = {payment.id: (payment, order) for payment, order in rows}
        prior_statement = (select(VersionChangeFund, Order).join(VersionChangeRequest,
            VersionChangeRequest.id == VersionChangeFund.change_id).join(Payment,
            Payment.id == VersionChangeFund.payment_id).join(Order, Order.id == Payment.order_id)
            .where(VersionChangeRequest.combination_id == combo.id, VersionChangeRequest.kind == 'upgrade',
                   VersionChangeFund.kind == 'payment', Payment.status.in_(['paid','refunding','refunded'])))
        if ns.get('refund_funding_store'):
            Revision = ns['subscription_terms']['Revision']
            generation_changes = select(Revision.change_id).join(Term, Term.id == Revision.term_id).where(
                Term.combination_id == combo.id, Term.generation == combo.generation)
            prior_statement = prior_statement.where(VersionChangeRequest.status == 'completed',
                VersionChangeRequest.resource_applied.is_(True), VersionChangeRequest.id.in_(generation_changes))
        prior = db.execute(prior_statement).all()
        for fund, order in prior:
            payment = db.get(Payment, fund.payment_id)
            sources[payment.id] = (payment, order)
        locked = lock_funding_payments(db, sources)
        remaining = requested
        for payment, order in sources.values():
            payment = locked[payment.id]
            reserved = occupied(db, payment, include_funds=True)
            room = max(0, cents(payment.amount) - reserved)
            portion = min(room, remaining)
            if portion <= 0:
                continue
            db.add(VersionChangeFund(change_id=change.id, kind='refund', payment_id=payment.id,
                amount=_money(portion), idempotency_key=f'{change.id}:refund:{payment.id}'))
            remaining -= portion
            if remaining == 0:
                break
        if remaining != 0:
            fail('可退款已支付余额不足，版本变更未启动')

    def create_refund_links(db, change, payment_rows, amount, *, actor='version-change-worker'):
        remaining = cents(amount)
        Refund = ns['Refund']
        locked = lock_funding_payments(db, [payment.id for payment, order in payment_rows])
        for payment, order in payment_rows:
            payment = locked[payment.id]
            if remaining <= 0:
                break
            key = f'{change.id}:refund:{payment.id}'
            prior = db.scalar(select(VersionChangeFund).where(VersionChangeFund.idempotency_key == key))
            if prior:
                remaining -= cents(prior.amount)
                continue
            reserved = occupied(db, payment)
            available = max(0, cents(payment.amount) - reserved)
            allocated = min(remaining, available)
            if allocated <= 0:
                continue
            refund = Refund(refund_no='REF-' + secrets.token_hex(6).upper(), order_id=order.id,
                payment_id=payment.id, amount=_money(allocated), status='completed',
                reason=f'Version change {change.id}', requested_by=actor, completed_by=actor,
                completed_at=ns['now']())
            db.add(refund); db.flush()
            db.add(VersionChangeFund(change_id=change.id, kind='refund', payment_id=payment.id,
                refund_id=refund.id, amount=_money(allocated), idempotency_key=key))
            remaining -= allocated
        if remaining != 0:
            raise ValueError('insufficient paid balance for version change refund')

    def process_downgrade_refunds(db, change):
        reservations = db.scalars(select(VersionChangeFund).where(VersionChangeFund.change_id == change.id,
            VersionChangeFund.kind == 'refund').with_for_update()).all()
        requested = cents(json.loads(change.quote_json)['refund_amount'])
        if requested and not reservations:
            fail('降级退款余额预留记录不存在')
        if sum(cents(item.amount) for item in reservations) != requested:
            fail('降级退款余额预留金额不一致')
        locked = lock_funding_payments(db, [row.payment_id for row in reservations])
        if ns.get('refund_funding_store'):
            for payment in locked.values():
                if occupied(db, payment) > cents(payment.amount):
                    fail('退款占用超过支付金额')
        for reservation in reservations:
            if reservation.refund_id:
                continue
            payment = locked.get(reservation.payment_id)
            order = db.get(Order, payment.order_id) if payment else None
            if not payment or not order:
                fail('退款来源支付记录不存在')
            refund = ns['Refund'](refund_no='REF-' + secrets.token_hex(6).upper(), order_id=order.id,
                payment_id=payment.id, amount=reservation.amount, status='completed',
                reason=f'Version change {change.id}', requested_by='version-change-worker',
                completed_by='version-change-worker', completed_at=ns['now']())
            db.add(refund); db.flush(); reservation.refund_id = refund.id
            db.flush()

    def refund_upgrade_funding(db, change, proof):
        if not isinstance(proof, dict) or proof.get('request_change_id') != change.id:
            fail('资源恢复证明无效')
        funds = db.scalars(select(VersionChangeFund).where(VersionChangeFund.change_id == change.id,
            VersionChangeFund.kind == 'payment')).all()
        if len(funds) != 1:
            fail('升级资金支付关联无效')
        fund = funds[0]
        payment = lock_funding_payments(db, [fund.payment_id]).get(fund.payment_id)
        order = db.get(Order, payment.order_id) if payment else None
        if not payment or not order or payment.status not in {'paid','refunding','refunded'}:
            fail('升级资金尚未支付，不能退款')
        create_refund_links(db, change, [(payment, order)], money(fund.amount))
        db.flush()
        refund_links = db.scalars(select(VersionChangeFund).where(
            VersionChangeFund.change_id == change.id, VersionChangeFund.kind == 'refund',
            VersionChangeFund.payment_id == payment.id, VersionChangeFund.refund_id.is_not(None))).all()
        refunded = 0
        for link in refund_links:
            refund = db.get(ns['Refund'], link.refund_id)
            if (not refund or refund.payment_id != payment.id or refund.status != 'completed'
                    or money(refund.amount) != money(link.amount)):
                fail('升级资金退款凭据无效')
            refunded += cents(refund.amount)
        if refunded != cents(fund.amount):
            fail('升级资金退款金额不匹配')
        order.refunded_amount = _money(refunded)
        order.payment_status = 'refunded'; payment.status = 'refunded'

    def require_order_transition(db, user, order, action):
        if getattr(order, 'business_type', '') == 'version_change_payment':
            if action not in {'start_payment', 'confirm_payment'}:
                fail('版本变更资金订单仅支持支付操作')
            return
        if action not in {'approve_refund', 'complete_refund'}:
            return
        if (not order.buyer_enterprise_id
                or order.delivery_method_snapshot not in {'api', 'model_api', 'tenant_access'}):
            return
        combination = ns['subscription_terms']['lock_combination'](
            db, order.buyer_enterprise_id, order.product_id)
        if combination:
            assert_no_pending(db, order.buyer_enterprise_id, order.product_id)
        Revision = ns['subscription_terms'].get('Revision')
        if Revision is None:
            return
        prior = db.scalar(select(VersionChangeRequest.id).join(Revision,
            Revision.change_id == VersionChangeRequest.id).join(ns['subscription_terms']['Term'],
            ns['subscription_terms']['Term'].id == Revision.term_id)
            .where(ns['subscription_terms']['Term'].order_id == order.id,
                   VersionChangeRequest.status.in_(('completed', 'compensated'))).limit(1))
        if prior:
            fail('该订阅已发生版本变更，普通退款须先完成版本变更资金核对')

    @ns['app'].post('/api/orders/{order_id}/version-changes')
    def create_route(order_id, body: CreateBody, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        try:
            result = create(db, user, order_id, body); db.commit(); return result
        except IntegrityError:
            db.rollback(); fail('版本变更幂等或并发冲突，请查询现有请求')
        except Exception:
            db.rollback(); raise

    @ns['app'].get('/api/orders/{order_id}/version-changes')
    def list_route(order_id, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        order = db.get(Order, order_id)
        if not order: fail('订单不存在', 404)
        require_buyer_read(db, user, order.buyer_enterprise_id)
        Term = ns['subscription_terms']['Term']
        combo_id = db.scalar(select(Term.combination_id).where(Term.order_id == order_id).limit(1))
        stmt = select(VersionChangeRequest).order_by(VersionChangeRequest.created_at.desc())
        stmt = stmt.where(VersionChangeRequest.combination_id == combo_id) if combo_id else stmt.where(
            VersionChangeRequest.source_order_id == order_id)
        return {'items': [public(row, db) for row in db.scalars(stmt).all()]}

    @ns['app'].get('/api/version-changes/{change_id}')
    def detail_route(change_id, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        change = db.get(VersionChangeRequest, change_id)
        if not change: fail('版本变更不存在', 404)
        require_buyer_read(db, user, change.enterprise_id)
        return public(change, db)

    class ReasonBody(BaseModel):
        model_config = ConfigDict(extra='forbid')
        reason: str = Field(default='', max_length=1000, strict=True)

    @ns['app'].post('/api/version-changes/{change_id}/start-payment')
    def start_payment(change_id, body: ReasonBody = Body(default=ReasonBody()), user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        if user.platform_role or not user.is_active: fail('仅购买企业超级管理员可发起支付', 403)
        change = db.scalar(select(VersionChangeRequest).where(VersionChangeRequest.id == change_id).with_for_update())
        if not change: fail('版本变更不存在', 404)
        if change.kind != 'upgrade': fail('此版本变更无需发起支付')
        ns['trading_policy']['require_buyer'](db, user, change.enterprise_id)
        membership = db.scalar(select(ns['Membership']).where(ns['Membership'].enterprise_id == change.enterprise_id,
            ns['Membership'].user_id == user.id, ns['Membership'].status == 'active'))
        if not membership or membership.role != 'super_admin':
            fail('仅购买企业超级管理员可发起支付', 403)
        if change.status != 'pending_payment':
            return public(change, db)
        if change.next_retry_at and ns['now']() >= change.next_retry_at:
            fail('版本变更报价已过期，请重新报价')
        funding = db.get(Order, change.funding_order_id) if change.funding_order_id else None
        if funding is None:
            source = db.get(Order, change.source_order_id)
            product = db.get(ns['Product'], source.product_id)
            version_model = ns['SaaSProductVersion'] if product.product_type == 'saas' else ns['ProductReleaseVersion']
            version = db.get(version_model, change.target_version_id)
            if not product or not version: fail('目标版本不存在')
            amount = money(Decimal(str(json.loads(change.quote_json)['payment_amount'])))
            if amount <= 0: fail('支付金额必须大于零')
            funding = Order(order_no=ns['make_order_no'](), buyer_user_id=user.id,
                buyer_enterprise_id=change.enterprise_id, provider_enterprise_id=source.provider_enterprise_id,
                product_id=source.product_id, main_status='pending_payment', payment_status='unpaid',
                delivery_status='not_started', amount=amount, paid_amount=0, refunded_amount=0,
                buyer_name=source.buyer_name, product_name=source.product_name,
                product_version_id=change.target_version_id, product_version_code=change.target_version_code,
                product_version_name=getattr(version, 'name', change.target_version_code),
                billing_cycle='version_change', business_type='version_change_payment',
                related_order_id=source.id, snapshot_version=0, subscription_months=1,
                delivery_method_snapshot='',
                economic_snapshot_json='{}', delivery_snapshot_json='{}')
            db.add(funding); db.flush()
            payment = ns['Payment'](order_id=funding.id, payment_no='PAY-' + secrets.token_hex(6).upper(),
                                    amount=amount, status='unpaid')
            db.add(payment); db.flush()
            db.add(VersionChangeFund(change_id=change.id, kind='payment', payment_id=payment.id,
                amount=amount, idempotency_key=f'{change.id}:upgrade-payment'))
            change.funding_order_id = funding.id
        if funding.payment_status == 'unpaid':
            funding.payment_status = 'paying'
            payment = db.scalar(select(ns['Payment']).where(ns['Payment'].order_id == funding.id))
            payment.status = 'paying'
        ns['audit'](db, user.email or user.id, 'start_version_change_payment', 'version_change', change.id,
            category='payment', business_domain='version_change', order_id=funding.id,
            after={'funding_order_id': funding.id, 'reason': body.reason[:200]})
        db.commit(); return public(change, db)

    @ns['app'].post('/api/version-changes/{change_id}/confirm-payment')
    def confirm_payment(change_id, body: ReasonBody = Body(default=ReasonBody()), user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        change = db.scalar(select(VersionChangeRequest).where(VersionChangeRequest.id == change_id).with_for_update())
        if not change: fail('版本变更不存在', 404)
        if user.platform_role in {'super_admin','finance_settlement'}:
            if not user.is_active or user.activation_status != 'active' or user.verified_status != 'verified':
                fail('平台账号未激活或未完成认证', 403)
        else:
            ns['trading_policy']['require_buyer'](db, user, change.enterprise_id)
            membership = db.scalar(select(ns['Membership']).where(ns['Membership'].enterprise_id == change.enterprise_id,
                ns['Membership'].user_id == user.id, ns['Membership'].status == 'active'))
            if not membership or membership.role != 'super_admin':
                fail('仅购买企业超级管理员可模拟确认支付', 403)
        payment_order = db.get(Order, change.funding_order_id) if change.funding_order_id else None
        if not payment_order: fail('版本变更支付订单不存在')
        if payment_order.payment_status == 'paid':
            return public(change, db)
        if change.status != 'pending_payment':
            fail('当前版本变更阶段不能确认支付')
        class Action:
            action, reason = 'confirm_payment', body.reason
        if not handle_transition(db, user, payment_order, Action()): fail('支付确认失败')
        db.commit(); return public(change, db)

    @ns['app'].post('/api/version-changes/{change_id}/retry')
    def retry(change_id, body: ReasonBody = Body(default=ReasonBody()), user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        if user.platform_role not in {'super_admin','finance_settlement','platform_operator'}:
            fail('仅平台管理员或财务可重试版本变更', 403)
        if (not user.is_active or user.activation_status != 'active' or user.verified_status != 'verified'):
            fail('平台账号未激活或未完成认证', 403)
        change = db.scalar(select(VersionChangeRequest).where(VersionChangeRequest.id == change_id).with_for_update())
        if not change: fail('版本变更不存在', 404)
        if change.status not in {'exception','compensating','refund_pending'}:
            fail('当前阶段不可手动重试')
        if change.compensation_started:
            change.status = 'compensating'
            change.compensation_retries = 0
        elif change.kind == 'downgrade' and change.resource_applied:
            change.status = 'refund_pending'
        elif change.resource_plan_json != '{}' and change.retries >= MAX_ATTEMPTS:
            change.status = 'compensating'
            change.compensation_started = True
            change.compensation_retries = 0
        else:
            change.status = 'resource_pending'
        change.retries = 0; change.next_retry_at = ns['now'](); change.last_error = ''
        ns['audit'](db, user.email or user.id, 'retry_version_change', 'version_change', change.id,
            category='version_change', business_domain='version_change', order_id=change.source_order_id,
            after={'status': change.status, 'reason': body.reason[:200]})
        db.commit(); return public(change, db)

    def process_once(*args, **kwargs):
        worker = importlib.import_module(__package__ + '.version_change_worker')
        return worker.process_once(*args, **kwargs)

    hooks = {'VersionChangeRequest': VersionChangeRequest, 'VersionChangeFund': VersionChangeFund,
             'create': create, 'handle_transition': handle_transition,
             'assert_no_pending': assert_no_pending, 'process_once': process_once,
             'public': public, 'OPEN_STATUSES': OPEN_STATUSES,
             'process_downgrade_refunds': process_downgrade_refunds,
             'refund_upgrade_funding': refund_upgrade_funding,
             'require_order_transition': require_order_transition}
    ns['version_change_saga'] = hooks
    return hooks
