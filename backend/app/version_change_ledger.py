"""Internal, non-payment ledger foundation for version-change compensation."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import secrets

from sqlalchemy import CheckConstraint, Column, ForeignKey, Numeric, String, Text, UniqueConstraint, select
from fastapi import HTTPException

from .subscription_terms import UTCDateTime
from .order_economics import RATE_KEYS, money
from .version_change_quotes import remaining_ratio


SIDES = ('old', 'target')
STATUSES = ('pending', 'posted', 'void')
MONEY = Decimal('0.01')


def install(ns):
    if 'version_change_ledger' in ns:
        return ns['version_change_ledger']
    if not callable(ns.get('audit')):
        raise RuntimeError('version change ledger requires the transactional audit hook')
    Base, Order = ns['Base'], ns['Order']
    Combo = ns['subscription_terms']['Combination']
    Term = ns['subscription_terms']['Term']

    class VersionChangeAdjustment(Base):
        __tablename__ = 'version_change_adjustments'
        __table_args__ = (
            UniqueConstraint('change_id', 'source_order_id', 'side', name='uq_version_change_adjustment_side'),
            CheckConstraint("side IN ('old', 'target')", name='ck_version_change_adjustment_side'),
            CheckConstraint("status IN ('pending', 'posted', 'void')", name='ck_version_change_adjustment_status'),
        )
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        change_id = Column(String(36), nullable=False, index=True)
        request_change_id = Column(String(36), nullable=True, index=True)
        source_order_id = Column(ForeignKey('orders.id'), nullable=False, index=True)
        combination_id = Column(ForeignKey('subscription_combinations.id'), nullable=False, index=True)
        side = Column(String(10), nullable=False)
        revenue = Column(Numeric(18, 2), nullable=False)
        cost = Column(Numeric(18, 2), nullable=False)
        profit = Column(Numeric(18, 2), nullable=False)
        currency = Column(String(3), nullable=False)
        original_version_id = Column(String(36), nullable=False)
        target_version_id = Column(String(36), nullable=False)
        rule_json = Column(Text, nullable=False)
        effective_at = Column(UTCDateTime(), nullable=False)
        status = Column(String(12), nullable=False, default='pending')
        payload_fingerprint = Column(String(64), nullable=False)
        resource_change_id = Column(String(36), nullable=True)
        resource_evidence_fingerprint = Column(String(64), nullable=True)
        created_at = Column(UTCDateTime(), nullable=False, default=ns['now'])
        posted_at = Column(UTCDateTime(), nullable=True)

    def fail(message):
        raise HTTPException(409, message)

    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)

    def amount(value):
        try:
            result = Decimal(str(value))
            if not result.is_finite() or result.quantize(MONEY) != result:
                raise ValueError
            if abs(result) > Decimal('999999999999.99'):
                raise ValueError
            return result
        except (InvalidOperation, TypeError, ValueError):
            fail('版本变更账本金额无效')

    def valid_rule(rule):
        if not isinstance(rule, dict) or set(rule) != {'mode', 'id', 'version', 'rates'}:
            fail('版本变更清算规则快照无效')
        if rule['mode'] not in {'custom', 'global'} or not isinstance(rule['id'], str) or not isinstance(rule['version'], str) or not rule['version']:
            fail('版本变更清算规则快照无效')
        rates = rule['rates']
        keys = {'platform_rate', 'provider_rate', 'service_rate', 'expert_rate', 'channel_rate'}
        if not isinstance(rates, dict) or set(rates) != keys:
            fail('版本变更清算比例无效')
        values = []
        for raw in rates.values():
            try:
                rate = Decimal(str(raw))
            except (InvalidOperation, TypeError, ValueError):
                fail('版本变更清算比例无效')
            if not rate.is_finite() or rate < 0 or rate > 100:
                fail('版本变更清算比例无效')
            values.append(rate)
        if sum(values) > 100 or rule['mode'] == 'global' and not rule['id']:
            fail('版本变更清算比例无效')
        return rule

    def actor_name(actor):
        if isinstance(actor, str) and actor:
            return actor
        return str(getattr(actor, 'email', None) or getattr(actor, 'id', None) or '')

    def target_rule_snapshot(db, product):
        if product.settlement_rule_mode == 'custom':
            try:
                raw = json.loads(product.settlement_rule_json or '{}')
            except (TypeError, ValueError):
                fail('目标版本清算规则快照无效')
            rule_id, version = '', f'product:{product.id}'
        elif product.settlement_rule_mode in {'global', 'platform'}:
            Rule = ns['SettlementRule']
            stmt = select(Rule)
            if product.settlement_rule_id:
                stmt = stmt.where(Rule.id == product.settlement_rule_id, Rule.status.in_(['active', 'disabled']))
            else:
                stmt = stmt.where(Rule.status == 'active')
            rule = db.scalar(stmt.order_by(Rule.created_at.desc(), Rule.id.desc()))
            if not rule:
                fail('目标版本清算规则不存在')
            raw = {key: getattr(rule, key) for key in RATE_KEYS}
            rule_id, version = rule.id, f'global:{rule.version}'
        else:
            fail('目标版本清算规则模式无效')
        try:
            rates = {key: str(Decimal(str(raw.get(key, 0)))) for key in RATE_KEYS}
        except (AttributeError, InvalidOperation, TypeError, ValueError):
            fail('目标版本清算比例无效')
        result = {'mode': product.settlement_rule_mode, 'id': rule_id,
                  'version': version, 'rates': rates}
        valid_rule(result)
        return result

    def stage(db, change_id, prepared, actor):
        if not isinstance(change_id, str) or not 1 <= len(change_id) <= 36:
            fail('版本变更请求标识无效')
        try:
            public, internal, combo = prepared['public'], prepared['internal'], prepared['combo']
        except (KeyError, TypeError):
            fail('版本变更报价结构无效')
        public_segments = public.get('segments')
        try:
            effective_at = datetime.fromisoformat(public_segments[0]['remaining_starts_at'])
        except (TypeError, IndexError, KeyError, ValueError):
            fail('版本变更报价缺少分段生效时间')
        if not isinstance(effective_at, datetime) or effective_at.tzinfo is None or effective_at.utcoffset() is None:
            fail('账本生效时间必须包含时区')
        effective_at = effective_at.astimezone(timezone.utc)
        source_id, combo_id = public.get('order_id'), public.get('combination_id')
        currency = public.get('currency')
        if not isinstance(source_id, str) or not source_id or not isinstance(combo_id, str) or not combo_id:
            fail('版本变更来源或组合无效')
        if not isinstance(currency, str) or len(currency) != 3 or currency != currency.upper():
            fail('版本变更币种无效')
        if not isinstance(internal, list) or not internal or not isinstance(public_segments, list):
            fail('版本变更报价分段为空')
        if {item.get('order_id') for item in public_segments if isinstance(item, dict)} != {
                item.get('order_id') for item in internal if isinstance(item, dict)} or len(public_segments) != len(internal):
            fail('版本变更报价公开与内部的分段不一致')
        segment_ids, rows = set(), []
        public_by_order = {item['order_id']: item for item in public_segments if isinstance(item, dict)}
        if len(public_by_order) != len(public_segments):
            fail('版本变更公开分段重复或无效')
        for segment in internal:
            if not isinstance(segment, dict) or set(segment) != {'order_id', 'term_id', 'old', 'target'}:
                fail('版本变更报价分段无效')
            order_id = segment['order_id']
            if not isinstance(order_id, str) or not order_id or order_id in segment_ids:
                fail('版本变更报价分段重复或来源无效')
            segment_ids.add(order_id)
            for side in SIDES:
                item = segment[side]
                if not isinstance(item, dict) or set(item) != {'revenue', 'cost', 'profit', 'rule', 'version_id'}:
                    fail('版本变更账本分录无效')
                revenue, cost, profit = (amount(item[key]) for key in ('revenue', 'cost', 'profit'))
                sign_ok = revenue <= 0 and cost <= 0 and profit <= 0 if side == 'old' else revenue >= 0 and cost >= 0 and profit >= 0
                if not sign_ok or profit != revenue - cost:
                    fail('版本变更账本金额符号或利润不匹配')
                public_segment = public_by_order.get(order_id)
                expected_revenue = ('original_remaining_amount' if side == 'old' else 'target_remaining_amount')
                if public_segment is None or (amount(public_segment.get(expected_revenue)) *
                        (Decimal(-1) if side == 'old' else Decimal(1))) != revenue:
                    fail('版本变更账本收入与报价不匹配')
                valid_rule(item['rule'])
                if not isinstance(item['version_id'], str) or not item['version_id']:
                    fail('版本变更版本标识无效')
                rows.append((order_id, side, revenue, cost, profit, item))
        versions = {item['version_id'] for _, side, _, _, _, item in rows if side == 'old'}
        targets = {item['version_id'] for _, side, _, _, _, item in rows if side == 'target'}
        if len(versions) != 1 or len(targets) != 1:
            fail('版本变更各分段的原版本或目标版本不一致')
        payload = {'change_id': change_id, 'quote_digest': prepared.get('digest'),
                   'source_order_id': source_id, 'combination_id': combo_id,
                   'currency': currency, 'effective_at': effective_at.isoformat(),
                   'original_version_id': next(iter(versions)), 'target_version_id': next(iter(targets)),
                   'segments': [{'order_id': oid, 'side': side, 'revenue': str(rev), 'cost': str(cost),
                                 'profit': str(profit), 'version_id': item['version_id'], 'rule': item['rule']}
                                for oid, side, rev, cost, profit, item in sorted(rows)]}
        fingerprint = hashlib.sha256(canonical(payload).encode()).hexdigest()
        prior = db.scalars(select(VersionChangeAdjustment).where(VersionChangeAdjustment.change_id == change_id).order_by(
            VersionChangeAdjustment.source_order_id, VersionChangeAdjustment.side)).all()
        if prior:
            if len(prior) != len(rows) or any(row.payload_fingerprint != fingerprint for row in prior):
                fail('同一版本变更标识已用于不同报价数据')
            return prior
        order_ids = set(db.scalars(select(Order.id).where(Order.id.in_(segment_ids))).all())
        if order_ids != segment_ids or source_id not in segment_ids:
            fail('版本变更包含未知来源订单')
        combo = db.scalar(select(Combo).where(Combo.id == combo_id).with_for_update()
                          .execution_options(populate_existing=True))
        if not combo or combo.status != 'active':
            fail('版本变更订阅组合不存在')
        eligible_terms = db.scalars(select(Term).where(Term.combination_id == combo_id,
            Term.generation == combo.generation, Term.status == 'active', Term.ends_at > effective_at)).all()
        if {term.order_id for term in eligible_terms} != segment_ids:
            fail('版本变更报价缺少当前或未来期限分段')
        for order_id in segment_ids:
            order = db.get(Order, order_id)
            if (order.buyer_enterprise_id != combo.enterprise_id or order.product_id != combo.product_id
                    or order.delivery_method_snapshot != combo.delivery_method):
                fail('版本变更来源订单与组合不匹配')
            if (order.payment_status != 'paid' or order.delivery_status not in {'accepted', 'pending_acceptance'}
                    or order.main_status in {'cancelled', 'closed', 'refunded'}
                    or money(order.paid_amount) != money(order.amount) or money(order.refunded_amount) != 0):
                fail('版本变更来源订单未满足已支付且未退款条件')
        if next(iter(versions)) != getattr(combo, 'version_id', None):
            fail('版本变更各分段的原版本或目标版本不一致')
        target_version_id = next(iter(targets))
        product = db.get(ns['Product'], getattr(combo, 'product_id', None))
        version_model = ns['SaaSProductVersion'] if product and product.product_type == 'saas' else ns['ProductReleaseVersion']
        target_version = db.get(version_model, target_version_id)
        if (not product or not target_version or target_version.product_id != product.id
                or target_version.status != 'active' or currency != product.currency):
            fail('版本变更目标版本不存在')
        expected_target_rule = target_rule_snapshot(db, product)
        for segment in internal:
            order = db.get(Order, segment['order_id'])
            term = db.get(Term, segment['term_id'])
            public_segment = public_by_order[segment['order_id']]
            if (not term or term.order_id != order.id or term.combination_id != combo_id
                    or term.generation != combo.generation or term.status != 'active'
                    or term.version_id != combo.version_id or term.version_code != combo.version_code):
                fail('版本变更期限与来源订单不匹配')
            try:
                ratio = remaining_ratio(term.starts_at, term.ends_at, effective_at)
                old_snapshot = ns['order_economics']['economic_snapshot'](order)
                expected_old_revenue = money(order.amount * ratio)
                expected_old_cost = money(order.total_cost_snapshot * ratio)
                unit_price = money(
                    target_version.monthly_price if version_model is ns['SaaSProductVersion'] else target_version.price)
                unit_cost = money(target_version.cost)
                expected_target_revenue = money(unit_price * term.months * ratio)
                expected_target_cost = money(unit_cost * term.months * ratio)
                public_ratio = Decimal(str(public_segment['remaining_ratio']))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                fail('版本变更金额无法根据原订单和期限复核')
            old_entry, target_entry = segment['old'], segment['target']
            if (ratio != public_ratio or old_snapshot['currency'] != currency
                    or old_entry['version_id'] != order.product_version_id
                    or old_entry['rule'] != old_snapshot['settlement_rule']
                    or target_entry['rule'] != expected_target_rule
                    or amount(old_entry['revenue']) != -expected_old_revenue
                    or amount(old_entry['cost']) != -expected_old_cost
                    or amount(target_entry['revenue']) != expected_target_revenue
                    or amount(target_entry['cost']) != expected_target_cost):
                fail('版本变更金额或历史规则与原订单、期限不匹配')
        created = []
        for order_id, side, revenue, cost, profit, item in rows:
            created.append(VersionChangeAdjustment(change_id=change_id, request_change_id=change_id,
                source_order_id=order_id,
                combination_id=combo_id, side=side, revenue=revenue, cost=cost, profit=profit,
                currency=currency, original_version_id=next(iter(versions)), target_version_id=next(iter(targets)),
                rule_json=canonical(item['rule']), effective_at=effective_at, status='pending',
                payload_fingerprint=fingerprint))
        db.add_all(created)
        db.flush()
        audit = ns['audit']
        audit(db, actor_name(actor), 'stage_version_change_adjustment', 'version_change', change_id,
              category='settlement_adjustment', business_domain='settlement', order_id=source_id,
              before={'status': 'none'}, after={'status': 'pending', 'segments': len(internal), 'fingerprint': fingerprint})
        db.flush()
        return created

    def finalize(db, change_id, resource_change_proof, actor):
        proof_keys = {'request_change_id', 'resource_change_id', 'evidence_fingerprint'}
        if (not isinstance(resource_change_proof, dict) or set(resource_change_proof) != proof_keys
                or any(not isinstance(resource_change_proof[key], str) or not resource_change_proof[key]
                       for key in proof_keys)
                or len(resource_change_proof['resource_change_id']) > 36
                or len(resource_change_proof['evidence_fingerprint']) != 64
                or any(char not in '0123456789abcdef' for char in resource_change_proof['evidence_fingerprint'])):
            fail('资源变更证明格式无效')
        rows = db.scalars(select(VersionChangeAdjustment).where(VersionChangeAdjustment.change_id == change_id)
                          .with_for_update()).all()
        if not rows:
            fail('版本变更待处理账本不存在')
        if all(row.status == 'posted' for row in rows):
            if any(row.request_change_id != resource_change_proof['request_change_id']
                   or row.resource_change_id != resource_change_proof['resource_change_id']
                   or row.resource_evidence_fingerprint != resource_change_proof['evidence_fingerprint']
                   for row in rows):
                fail('资源变更证明与已入账证据不一致')
            return rows
        if any(row.status != 'pending' for row in rows):
            fail('版本变更账本状态不允许入账')
        if any(row.request_change_id != resource_change_proof['request_change_id'] for row in rows):
            fail('资源变更证明与账本请求标识不匹配')
        before = {'status': 'pending', 'rows': len(rows)}
        posted_at = ns['now']()
        for row in rows:
            row.status, row.posted_at = 'posted', posted_at
            row.resource_change_id = resource_change_proof['resource_change_id']
            row.resource_evidence_fingerprint = resource_change_proof['evidence_fingerprint']
        db.flush()
        audit = ns['audit']
        audit(db, actor_name(actor), 'post_version_change_adjustment', 'version_change', change_id,
              category='settlement_adjustment', business_domain='settlement', order_id=rows[0].source_order_id,
              before=before, after={'status': 'posted', 'rows': len(rows),
                                    'resource_change_id': resource_change_proof['resource_change_id'],
                                    'evidence_fingerprint': resource_change_proof['evidence_fingerprint']})
        db.flush()
        return rows

    def void(db, change_id, reason, actor):
        if not isinstance(reason, str) or not reason.strip():
            fail('作废待处理账本必须提供原因')
        rows = db.scalars(select(VersionChangeAdjustment).where(VersionChangeAdjustment.change_id == change_id)
                          .with_for_update()).all()
        if not rows:
            fail('版本变更待处理账本不存在')
        if any(row.status != 'pending' for row in rows):
            fail('已入账的财务证据不可作废')
        before = {'status': 'pending', 'rows': len(rows)}
        for row in rows:
            row.status = 'void'
        db.flush()
        audit = ns['audit']
        audit(db, actor_name(actor), 'void_version_change_adjustment', 'version_change', change_id,
              category='settlement_adjustment', business_domain='settlement', order_id=rows[0].source_order_id,
              before=before, after={'status': 'void', 'rows': len(rows), 'reason': reason})
        db.flush()
        return rows

    hooks = {'Model': VersionChangeAdjustment, 'stage': stage, 'finalize': finalize, 'void': void}
    ns['VersionChangeAdjustment'] = VersionChangeAdjustment
    ns['version_change_ledger'] = hooks
    return hooks
