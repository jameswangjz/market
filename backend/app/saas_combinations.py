"""Reserve one SaaS tenant per enterprise/product without provisioning or payment.

Lock order: product (caller), enterprise, binding, SaaS subscription. Hooks flush
only; their caller owns the transaction. Historical tenants are never merged.
"""
import secrets

from fastapi import Depends, HTTPException
from sqlalchemy import Column, ForeignKey, String, UniqueConstraint, select


def install(ns):
    Base, Subscription = ns['Base'], ns['SaaSSubscription']

    class SaaSCombinationBinding(Base):
        __tablename__ = 'saas_combination_bindings'
        __table_args__ = (UniqueConstraint('enterprise_id', 'product_id', name='uq_saas_binding_enterprise_product'),)
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        enterprise_id = Column(ForeignKey('enterprises.id'), nullable=False)
        product_id = Column(ForeignKey('products.id'), nullable=False)
        subscription_id = Column(ForeignKey('saas_subscriptions.id'), nullable=False, unique=True)

    Binding = SaaSCombinationBinding

    def lock_binding(db, enterprise_id, product_id):
        ns['subscription_terms']['lock_combination'](db, enterprise_id, product_id)
        return db.scalar(select(Binding).where(Binding.enterprise_id == enterprise_id,
            Binding.product_id == product_id).with_for_update().execution_options(populate_existing=True))

    def reserve(db, user, quote):
        product = quote['product']
        if product.product_type != 'saas' or product.delivery_method != 'tenant_access':
            return None
        enterprise_id = quote['buyer_enterprise_id']
        ns['trading_policy']['require_buyer'](db, user, enterprise_id)
        binding = lock_binding(db, enterprise_id, product.id)
        target_id = quote.get('saas_subscription_id', '')
        rows = db.scalars(select(Subscription).where(Subscription.enterprise_id == enterprise_id,
            Subscription.product_id == product.id).order_by(Subscription.id).with_for_update()
            .execution_options(populate_existing=True)).all()
        if binding:
            target_id = target_id or binding.subscription_id
            if target_id != binding.subscription_id:
                raise HTTPException(409, '本企业已绑定该SaaS产品的订阅组合，不可新增或切换独立租户')
        elif not target_id and len(rows) > 1:
            raise HTTPException(409, '存在多个历史SaaS租户，请明确选择续费目标，不会自动合并')
        elif not target_id and rows:
            target_id = rows[0].id
        target = next((row for row in rows if row.id == target_id), None)
        if target_id and not target:
            raise HTTPException(409, '续费目标不属于本企业和产品')
        if target:
            if target.version_id != quote['version'].id:
                raise HTTPException(409, '已有SaaS订阅使用其他版本，请使用版本变更流程')
            if target.status not in {'active', 'provisioning', 'failed'}:
                raise HTTPException(409, 'SaaS租户已关闭或不可续费，请先完成恢复处理')
        else:
            target = Subscription(enterprise_id=enterprise_id, product_id=product.id,
                version_id=quote['version'].id, billing_cycle='monthly', status='provisioning',
                created_by=user.id)
            db.add(target)
            db.flush()
        if binding is None:
            binding = Binding(enterprise_id=enterprise_id, product_id=product.id, subscription_id=target.id)
            db.add(binding)
            db.flush()
            ns['audit'](db, user.email or user.phone or user.id, 'bind_saas_combination',
                'saas_combination', binding.id, '预留或选择SaaS订阅组合；尚未付款或开通',
                tenant_id=enterprise_id, category='delivery', business_domain='trading',
                before={}, after={'subscription_id': target.id, 'product_id': product.id})
        return target

    def guard_legacy_create(db, enterprise_id, product_id):
        binding = lock_binding(db, enterprise_id, product_id)
        if binding or db.scalar(select(Subscription.id).where(Subscription.enterprise_id == enterprise_id,
                Subscription.product_id == product_id).limit(1)):
            raise HTTPException(409, '该企业已有SaaS租户，请续费现有组合，不允许创建独立租户')

    def bind_legacy(db, subscription):
        db.add(Binding(enterprise_id=subscription.enterprise_id, product_id=subscription.product_id,
                       subscription_id=subscription.id))
        db.flush()

    @ns['app'].get('/api/products/{product_id}/saas-subscription-targets')
    def targets(product_id: str, enterprise_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        ns['trading_policy']['require_buyer'](db, user, enterprise_id)
        ns['saas_product'](product_id, db)
        binding = db.scalar(select(Binding).where(Binding.enterprise_id == enterprise_id, Binding.product_id == product_id))
        rows = db.scalars(select(Subscription).where(Subscription.enterprise_id == enterprise_id,
            Subscription.product_id == product_id).order_by(Subscription.created_at, Subscription.id)).all()
        return {'bound_subscription_id': binding.subscription_id if binding else '',
                'selection_required': not binding and len(rows) > 1,
                'items': [{'id': row.id, 'version_id': row.version_id, 'status': row.status,
                           'expires_at': row.expires_at, 'bound': bool(binding and binding.subscription_id == row.id)} for row in rows]}

    ns['SaaSCombinationBinding'] = Binding
    hooks = dict(reserve=reserve, guard_legacy_create=guard_legacy_create, bind_legacy=bind_legacy, Binding=Binding)
    ns['saas_combinations'] = hooks
    return hooks
