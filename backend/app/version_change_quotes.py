"""Read-only, tenant-bound version-change quotes; never move funds or resources.

Quotes freeze their time for at most five minutes. A keyed snapshot fingerprint
protects hidden costs and detects renewal/refund/config changes during validation.
The subsequent execution Saga must validate again under its own business locks.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json

from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .order_economics import money
from .subscription_calendar import CalendarAnchor
from .subscription_terms import MONTHLY, utc


AUDIENCE = 'market-version-change-quote'
TTL_SECONDS = 300


class QuoteBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_version_id: str = Field(min_length=1, max_length=36, strict=True)
    compatibility_confirmed: bool = Field(default=False, strict=True)


class ValidateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    quote_token: str = Field(min_length=1, max_length=8192, strict=True)


def microseconds(delta):
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def remaining_ratio(starts_at, ends_at, at):
    start, end, instant = utc(starts_at), utc(ends_at), utc(at)
    duration = microseconds(end-start)
    if duration <= 0:
        raise HTTPException(409, '订阅期限无效，不能报价')
    return Decimal(max(0, min(duration, microseconds(end-max(start, instant))))) / Decimal(duration)


def iso(value):
    if value is None:
        return None
    return utc(value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()


def install(ns):
    Order, Product = ns['Order'], ns['Product']
    terms = ns['subscription_terms']
    Combo, Term = terms['Combination'], terms['Term']

    def subject(db, user, order_id, at):
        order = db.scalar(select(Order).where(Order.id == order_id).execution_options(populate_existing=True))
        if not order:
            raise HTTPException(404, '订单不存在')
        ns['trading_policy']['require_buyer'](db, user, order.buyer_enterprise_id)
        if order.snapshot_version != 1 or order.delivery_method_snapshot not in MONTHLY:
            raise HTTPException(409, '仅新版API、模型API和SaaS订阅支持版本变更报价')
        terms_snapshot(order)
        combo = db.scalar(select(Combo).where(Combo.enterprise_id == order.buyer_enterprise_id,
            Combo.product_id == order.product_id).execution_options(populate_existing=True))
        source_term = db.scalar(select(Term).where(Term.order_id == order.id, Term.status == 'active')
            .execution_options(populate_existing=True))
        if (not combo or combo.status != 'active' or not source_term
                or source_term.combination_id != combo.id or source_term.generation != combo.generation
                or combo.delivery_method != order.delivery_method_snapshot
                or order.product_version_id != combo.version_id
                or order.product_version_code != combo.version_code
                or not combo.anchor_at <= at < CalendarAnchor(combo.anchor_at).boundary(combo.total_months)):
            raise HTTPException(409, '订单不属于当前有效订阅组合')
        if order.delivery_method_snapshot == 'tenant_access':
            subscription = db.get(ns['SaaSSubscription'], order.subscription_id) if order.subscription_id else None
            binding = db.scalar(select(ns['saas_combinations']['Binding']).where(
                ns['saas_combinations']['Binding'].enterprise_id == order.buyer_enterprise_id,
                ns['saas_combinations']['Binding'].product_id == order.product_id))
            if (not subscription or not binding or binding.subscription_id != subscription.id
                    or subscription.status != 'active' or not subscription.external_tenant_id
                    or subscription.enterprise_id != order.buyer_enterprise_id
                    or subscription.product_id != order.product_id or subscription.version_id != combo.version_id):
                raise HTTPException(409, 'SaaS租户尚未完成有效开通或组合绑定')
        return order, combo

    def terms_snapshot(order):
        if (order.payment_status != 'paid' or money(order.paid_amount) != money(order.amount)
                or money(order.refunded_amount) != 0
                or order.main_status in {'cancelled', 'closed', 'refunded'}
                or order.delivery_status not in {'accepted', 'pending_acceptance'}):
            raise HTTPException(409, '报价须使用全额支付、未退款且已交付的订阅')
        economic = ns['order_economics']['economic_snapshot'](order)
        delivery = ns['order_economics']['delivery_snapshot'](order)
        if (economic['billing_unit'] != 'month' or delivery['method'] not in MONTHLY
                or economic.get('version_code') != order.product_version_code
                or not isinstance(economic.get('currency'), str) or len(economic['currency']) != 3):
            raise HTTPException(409, '月度订阅快照不完整')
        return economic

    def eligible_segments(db, order, combo, at):
        anchor = CalendarAnchor(combo.anchor_at)
        currency = terms_snapshot(order)['currency']
        rows = db.execute(select(Term, Order).join(Order, Order.id == Term.order_id).where(
            Term.combination_id == combo.id, Term.generation == combo.generation,
            Term.status == 'active', Term.ends_at > at).order_by(Term.starts_at, Term.id)
            .execution_options(populate_existing=True)).all()
        previous = None
        for term, original in rows:
            bounds = anchor.term(term.months, start_month=term.start_month)
            if (term.starts_at != bounds.start or term.ends_at != bounds.end
                    or term.version_id != combo.version_id or term.version_code != combo.version_code
                    or original.buyer_enterprise_id != order.buyer_enterprise_id
                    or original.product_id != order.product_id or original.snapshot_version != 1
                    or original.delivery_method_snapshot != combo.delivery_method
                    or original.product_version_id != term.version_id
                    or original.product_version_code != term.version_code
                    or original.subscription_months != term.months
                    or (previous is not None and term.starts_at != previous)):
                raise HTTPException(409, '订阅时段或订单快照不一致，请先核实权益')
            if terms_snapshot(original)['currency'] != currency:
                raise HTTPException(409, '订阅原订单币种不一致，不能直接计算差价')
            previous = term.ends_at
        if (not rows or not rows[0][0].starts_at <= at < rows[0][0].ends_at
                or previous != anchor.boundary(combo.total_months)):
            raise HTTPException(409, '当前及未来订阅时段不连续，不能遗漏付费权益')
        orphan_paid = db.scalar(select(Order.id).where(Order.buyer_enterprise_id == order.buyer_enterprise_id,
            Order.product_id == order.product_id, Order.snapshot_version == 1,
            Order.payment_status == 'paid', Order.refunded_amount == 0,
            Order.delivery_method_snapshot.in_(MONTHLY),
            ~select(Term.id).where(Term.order_id == Order.id).exists()).limit(1))
        if orphan_paid:
            raise HTTPException(409, '存在已支付但尚未完成期限编排的订单，请等待交付完成')
        return rows

    def compatibility(version):
        mode = version.compatibility_mode or 'compatible'
        if mode not in {'compatible', 'breaking'}:
            raise HTTPException(409, '目标版本兼容性配置无效')
        return {'mode': mode, 'notes': version.compatibility_notes or '', 'confirmation_required': mode == 'breaking'}

    def prepare(db, user, order_id, target_id, confirmed, at):
        order, combo = subject(db, user, order_id, at)
        if target_id == combo.version_id:
            raise HTTPException(409, '请选择不同于当前订阅的目标版本')
        body = ns['OrderBody'](product_id=order.product_id, product_version_id=target_id,
            buyer_enterprise_id=order.buyer_enterprise_id, subscription_months=1)
        target = ns['order_economics']['quote'](db, user, body, lock=True)
        combo = terms['lock_combination'](db, order.buyer_enterprise_id, order.product_id)
        # Re-read eligibility after the enterprise lock serializes renewal/delivery.
        order, current_combo = subject(db, user, order_id, at)
        if not combo or current_combo.id != combo.id:
            raise HTTPException(409, '订阅组合已变化，请重新报价')
        if target['delivery']['method'] != combo.delivery_method:
            raise HTTPException(409, '目标交付类型与当前订阅不同')
        currency = terms_snapshot(order)['currency']
        if target['economic']['currency'] != currency:
            raise HTTPException(409, '目标版本与原订单币种不同，不能直接计算差价')
        rows = eligible_segments(db, order, combo, at)
        version = target['version']
        compat = compatibility(version)
        if compat['confirmation_required'] and not confirmed:
            raise HTTPException(409, '目标版本存在不兼容变更，请明确确认兼容性后获取报价')
        segments, internal, state = [], [], []
        for term, original in rows:
            ratio = remaining_ratio(term.starts_at, term.ends_at, at)
            old = terms_snapshot(original)
            old_revenue = money(money(original.amount) * ratio)
            old_cost = money(money(original.total_cost_snapshot) * ratio)
            full_revenue = money(target['unit_price'] * term.months)
            full_cost = money(target['unit_cost'] * term.months)
            new_revenue, new_cost = money(full_revenue * ratio), money(full_cost * ratio)
            delta = new_revenue-old_revenue
            segments.append({'order_id': original.id, 'order_no': original.order_no,
                'version_code': term.version_code, 'subscription_months': term.months,
                'starts_at': iso(term.starts_at), 'ends_at': iso(term.ends_at),
                'remaining_starts_at': iso(max(at, term.starts_at)), 'remaining_ends_at': iso(term.ends_at),
                'remaining_ratio': str(ratio), 'original_amount': float(money(original.amount)),
                'original_remaining_amount': float(old_revenue), 'target_full_amount': float(full_revenue),
                'target_remaining_amount': float(new_revenue), 'net_difference': float(delta)})
            internal.append({'order_id': original.id, 'term_id': term.id,
                'old': {'revenue': str(-old_revenue), 'cost': str(-old_cost),
                        'profit': str(-(old_revenue-old_cost)), 'rule': old['settlement_rule'], 'version_id': term.version_id},
                'target': {'revenue': str(new_revenue), 'cost': str(new_cost),
                           'profit': str(new_revenue-new_cost), 'rule': target['economic']['settlement_rule'], 'version_id': version.id}})
            state.append({'term_id': term.id, 'start_month': term.start_month, 'months': term.months,
                'status': term.status, 'economic': original.economic_snapshot_json,
                'delivery': original.delivery_snapshot_json, 'paid': str(original.paid_amount),
                'refunded': str(original.refunded_amount), 'main_status': original.main_status,
                'delivery_status': original.delivery_status, 'updated_at': iso(original.updated_at)})
        total = sum((Decimal(segment['target']['revenue'])+Decimal(segment['old']['revenue'])
                     for segment in internal), Decimal(0))
        money(abs(total))
        fingerprint = {'source_order_id': order.id, 'combination_id': combo.id,
            'generation': combo.generation, 'anchor_at': iso(combo.anchor_at), 'total_months': combo.total_months,
            'combo_updated_at': iso(combo.updated_at), 'target': target['economic'],
            'target_delivery': target['delivery'], 'target_updated_at': iso(version.updated_at),
            'compatibility': compat, 'confirmed': confirmed, 'at': iso(at), 'state': state, 'internal': internal}
        digest = hmac.new(ns['JWT_SECRET'].encode(), json.dumps(fingerprint, sort_keys=True,
            separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
        result = {'order_id': order.id, 'combination_id': combo.id, 'current_version_code': combo.version_code,
            'target_version_code': version.version_code, 'action': 'upgrade' if total > 0 else 'downgrade' if total < 0 else 'no_payment',
            'net_difference': float(total), 'payment_amount': float(max(total, Decimal(0))),
            'refund_amount': float(max(-total, Decimal(0))), 'currency': currency,
            'compatibility': compat, 'segments': segments, 'execution_ready': False}
        return {'public': result, 'internal': internal, 'digest': digest, 'combo': combo, 'current_end': rows[0][0].ends_at}

    def make_quote(db, user, order_id, body):
        at = utc(ns['now']())
        prepared = prepare(db, user, order_id, body.target_version_id, body.compatibility_confirmed, at)
        expiry = int(min(at+timedelta(seconds=TTL_SECONDS), prepared['current_end']).timestamp())
        if expiry <= at.timestamp():
            raise HTTPException(409, '订阅时段即将结束，请稍后重新报价')
        claims = {'aud': AUDIENCE, 'purpose': 'version_change_quote', 'sub': user.id,
            'order_id': order_id, 'combination_id': prepared['combo'].id, 'target_id': body.target_version_id,
            'confirmed': body.compatibility_confirmed, 'at': iso(at), 'digest': prepared['digest'],
            'iat': int(at.timestamp()), 'exp': expiry}
        token = ns['jwt'].encode(claims, ns['JWT_SECRET'], algorithm='HS256')
        return {**prepared['public'], 'quote_id': prepared['digest'], 'quote_token': token,
                'created_at': iso(at), 'expires_at': datetime.fromtimestamp(expiry, timezone.utc).isoformat()}

    def validate(db, user, order_id, token):
        try:
            claims = ns['jwt'].decode(token, ns['JWT_SECRET'], algorithms=['HS256'], audience=AUDIENCE,
                options={'verify_exp': False, 'verify_iat': False,
                         'require': ['aud', 'purpose', 'sub', 'order_id', 'combination_id', 'target_id', 'confirmed', 'at', 'digest', 'iat', 'exp']})
            at = utc(datetime.fromisoformat(claims['at']))
            if (claims['purpose'] != 'version_change_quote' or type(claims['confirmed']) is not bool
                    or type(claims['exp']) is not int or type(claims['iat']) is not int
                    or not isinstance(claims['digest'], str) or not isinstance(claims['target_id'], str)
                    or claims['iat'] != int(at.timestamp()) or not 0 < claims['exp']-at.timestamp() <= TTL_SECONDS):
                raise ValueError('Invalid quote claims')
        except (ns['jwt'].PyJWTError, TypeError, ValueError, KeyError, OverflowError):
            raise HTTPException(400, '版本变更报价凭据无效') from None
        if claims['sub'] != user.id or claims['order_id'] != order_id:
            raise HTTPException(403, '报价不属于当前用户和订单')
        instant = utc(ns['now']())
        if instant.timestamp() >= claims['exp'] or instant < at:
            raise HTTPException(409, '报价已过期或时间无效，请重新获取')
        prepared = prepare(db, user, order_id, claims['target_id'], claims['confirmed'], at)
        if (prepared['combo'].id != claims['combination_id']
                or not hmac.compare_digest(prepared['digest'], claims['digest'])):
            raise HTTPException(409, '订阅、订单或目标配置已变更，请重新报价')
        return {**prepared['public'], 'quote_id': claims['digest'], 'quote_token': token,
                'created_at': iso(at), 'expires_at': datetime.fromtimestamp(claims['exp'], timezone.utc).isoformat()}

    @ns['app'].get('/api/orders/{order_id}/version-change-options')
    def options(order_id: str, response: Response, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        response.headers['Cache-Control'] = 'no-store'
        at = utc(ns['now']())
        order, combo = subject(db, user, order_id, at)
        eligible_segments(db, order, combo, at)
        product = db.get(Product, order.product_id)
        if not product or product.status != 'published':
            raise HTTPException(409, '产品未发布，不能变更版本')
        model = ns['SaaSProductVersion'] if product.product_type == 'saas' else ns['ProductReleaseVersion']
        available = {row['id'] for row in ns['storefront']['versions_for'](db, product)}
        versions = db.scalars(select(model).where(model.product_id == product.id, model.status == 'active').order_by(model.version_code, model.id)).all()
        return {'order_id': order_id, 'combination_id': combo.id, 'delivery_method': combo.delivery_method,
            'server_time': iso(at),
            'current_version_id': combo.version_id, 'current_version_code': combo.version_code,
            'expires_at': iso(CalendarAnchor(combo.anchor_at).boundary(combo.total_months)),
            'versions': [{'id': version.id, 'version_code': version.version_code,
                'monthly_price': float(money(version.monthly_price if product.product_type == 'saas' else version.price)),
                'compatibility_mode': compatibility(version)['mode'],
                'compatibility_notes': compatibility(version)['notes']} for version in versions
                if version.id in available and version.id != combo.version_id]}

    @ns['app'].post('/api/orders/{order_id}/version-change-quote')
    def quote_route(order_id: str, body: QuoteBody, response: Response, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        response.headers['Cache-Control'] = 'no-store'
        return make_quote(db, user, order_id, body)

    @ns['app'].post('/api/orders/{order_id}/version-change-quote/validate')
    def validate_route(order_id: str, body: ValidateBody, response: Response, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        response.headers['Cache-Control'] = 'no-store'
        return validate(db, user, order_id, body.quote_token)

    hooks = dict(prepare=prepare, quote=make_quote, validate=validate, QuoteBody=QuoteBody)
    ns['version_change_quotes'] = hooks
    return hooks
