"""Buyer-scoped SaaS access, independent of provider administration."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import select

from .oidc_provider import NO_STORE


class GrantBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    user_id: str
    granted: StrictBool


def install(ns):
    hooks = ns['oidc_provider']
    Subscription, Member, Grant = ns['SaaSSubscription'], ns['Membership'], ns['ApplicationAccessGrant']

    def member_for(db, user, enterprise_id, admin=False):
        if not user.is_active or user.activation_status != 'active' or user.verified_status != 'verified' or user.platform_role:
            raise HTTPException(403, '仅已实名企业成员可访问应用')
        member = db.scalar(select(Member).where(Member.user_id == user.id,
            Member.enterprise_id == enterprise_id, Member.status == 'active'))
        enterprise = db.get(ns['Enterprise'], enterprise_id)
        if not member or not enterprise or enterprise.verification_status != 'verified':
            raise HTTPException(403, '企业成员身份无效')
        if admin and member.role not in {'super_admin', 'enterprise_admin'}:
            raise HTTPException(403, '仅购买企业管理员可管理访问权限')
        return member

    def context(db, user, sid, admin=False):
        subscription = db.get(Subscription, sid)
        if not subscription:
            raise HTTPException(404, '应用不存在')
        member = member_for(db, user, subscription.enterprise_id, admin)
        product = db.get(ns['Product'], subscription.product_id)
        if not product or product.product_type != 'saas' or product.delivery_method != 'tenant_access':
            raise HTTPException(404, 'SaaS应用不存在')
        return subscription, product, member

    def client_for_product(db, product):
        client = db.scalar(select(hooks['Client']).where(hooks['Client'].product_id == product.id))
        return hooks['client_for'](db, client.client_id) if client else None

    def validated_application_address(product):
        raw_address = product.application_url or ''
        try:
            address = urlsplit(raw_address)
            port = address.port
        except ValueError as exc:
            raise HTTPException(409, '应用访问地址无效') from exc
        if (address.scheme not in {'http', 'https'} or not address.hostname or address.username or address.password
                or port == 0 or '\\' in raw_address
                or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw_address)):
            raise HTTPException(409, '应用访问地址无效')
        return address

    def access_for_subscription(db, user, sid, enterprise_id=None, product_id=None):
        subscription, product, _ = context(db, user, sid)
        if ((enterprise_id and subscription.enterprise_id != enterprise_id)
                or (product_id and subscription.product_id != product_id)):
            raise HTTPException(409, '应用订阅与订单不匹配')
        client = client_for_product(db, product)
        if not client:
            raise HTTPException(409, '应用尚未配置单点登录')
        hooks['eligible'](db, user, client, subscription.enterprise_id, subscription.id)
        if not product.application_url:
            raise HTTPException(409, '应用访问地址未配置')
        validated_application_address(product)
        return subscription, product

    def launch_subscription(db, user, sid):
        subscription, product = access_for_subscription(db, user, sid)
        address = validated_application_address(product)
        query = [(key, value) for key, value in parse_qsl(address.query, keep_blank_values=True) if key != 'enterprise_id']
        query.append(('enterprise_id', subscription.enterprise_id))
        ns['audit'](db, user.email or user.id, 'launch_saas_application', 'saas_subscription', subscription.id,
            '启动已授权应用；不传递登录令牌或凭据', tenant_id=subscription.enterprise_id)
        db.commit()
        return urlunsplit(address._replace(query=urlencode(query)))

    ns['applications_access'] = access_for_subscription
    ns['applications_launch'] = launch_subscription

    @ns['app'].get('/api/applications')
    def listing(user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        if not user.is_active or user.activation_status != 'active' or user.verified_status != 'verified' or user.platform_role:
            raise HTTPException(403, '仅已实名企业成员可访问应用')
        memberships = db.scalars(select(Member).where(Member.user_id == user.id, Member.status == 'active')).all()
        items = []
        for member in memberships:
            enterprise = db.get(ns['Enterprise'], member.enterprise_id)
            if not enterprise or enterprise.verification_status != 'verified':
                continue
            for subscription in db.scalars(select(Subscription).where(Subscription.enterprise_id == enterprise.id)).all():
                product = db.get(ns['Product'], subscription.product_id)
                if not product or product.product_type != 'saas' or product.delivery_method != 'tenant_access':
                    continue
                admin = member.role in {'super_admin', 'enterprise_admin'}
                if not admin and not db.scalar(select(Grant.id).where(Grant.enterprise_id == enterprise.id,
                        Grant.product_id == product.id, Grant.user_id == user.id, Grant.status == 'active')):
                    continue
                client = client_for_product(db, product)
                blocked = ''
                try:
                    if not client:
                        raise HTTPException(409, '应用尚未配置单点登录')
                    hooks['eligible'](db, user, client, enterprise.id, subscription.id)
                    if not product.application_url:
                        raise HTTPException(409, '应用访问地址未配置')
                except HTTPException as exc:
                    blocked = str(exc.detail)
                version = db.get(ns['SaaSProductVersion'], subscription.version_id)
                items.append({'subscription_id': subscription.id, 'enterprise_id': enterprise.id,
                    'enterprise_name': enterprise.name, 'product_id': product.id, 'product_name': product.name,
                    'provider_name': product.provider_name, 'version_code': version.version_code if version else '',
                    'expires_at': subscription.expires_at.isoformat() if subscription.expires_at else None,
                    'available': not blocked, 'blocked_reason': blocked, 'is_enterprise_admin': admin,
                    'logo_url': f'/api/storefront/products/{product.id}/logo'
                        if product.logo_thumbnail_file_id and product.status == 'published' else ''})
        return JSONResponse({'items': items}, headers=NO_STORE)

    @ns['app'].post('/api/applications/{subscription_id}/launch')
    def launch(subscription_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        return JSONResponse({'launch_url': launch_subscription(db, user, subscription_id)}, headers=NO_STORE)

    @ns['app'].get('/api/applications/{subscription_id}/members')
    def members(subscription_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        subscription, product, member = context(db, user, subscription_id, admin=True)
        items = []
        for membership in db.scalars(select(Member).where(Member.enterprise_id == subscription.enterprise_id,
                Member.status == 'active')).all():
            target = db.get(ns['User'], membership.user_id)
            if not target or not target.is_active or target.activation_status != 'active' or target.verified_status != 'verified' or target.platform_role:
                continue
            grant = db.scalar(select(Grant).where(Grant.enterprise_id == subscription.enterprise_id,
                Grant.product_id == product.id, Grant.user_id == target.id, Grant.status == 'active'))
            items.append({'user_id': target.id, 'name': target.name, 'login_name': target.username or target.email or target.phone,
                'email': target.email, 'role': membership.role, 'granted': bool(grant) or membership.role in {'super_admin', 'enterprise_admin'}})
        return JSONResponse({'items': items}, headers=NO_STORE)

    @ns['app'].post('/api/applications/{subscription_id}/grants')
    def change_grant(subscription_id: str, body: GrantBody, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        subscription, product, member = context(db, user, subscription_id, admin=True)
        # Grants are shared across subscriptions; use the same enterprise lock as legacy writers.
        db.scalar(select(ns['Enterprise']).where(ns['Enterprise'].id == subscription.enterprise_id).with_for_update())
        target = db.get(ns['User'], body.user_id)
        if not target:
            raise HTTPException(404, '用户不存在')
        if body.granted:
            target_member = member_for(db, target, subscription.enterprise_id)
        else:
            target_member = db.scalar(select(Member).where(Member.user_id == target.id,
                Member.enterprise_id == subscription.enterprise_id))
            existing_grant = db.scalar(select(Grant.id).where(Grant.enterprise_id == subscription.enterprise_id,
                Grant.product_id == product.id, Grant.user_id == target.id))
            if (not target_member and not existing_grant) or target.platform_role:
                raise HTTPException(403, '用户不属于购买企业')
        if target_member and target_member.status == 'active' and target_member.role in {'super_admin', 'enterprise_admin'}:
            raise HTTPException(409, '企业管理员按订阅权益访问，无需单独授予或撤销')
        grants = db.scalars(select(Grant).where(Grant.enterprise_id == subscription.enterprise_id,
            Grant.product_id == product.id, Grant.user_id == target.id).with_for_update()).all()
        before = any(grant.status == 'active' for grant in grants)
        if body.granted and not grants:
            db.add(Grant(enterprise_id=subscription.enterprise_id, product_id=product.id,
                user_id=target.id, granted_by=user.id, status='active'))
        else:
            for index, grant in enumerate(grants):
                grant.status = 'active' if body.granted and index == 0 else 'revoked'
                grant.granted_by = user.id
        ns['audit'](db, user.email or user.id, 'change_application_access', 'saas_subscription', subscription.id,
            f'user={target.id}', tenant_id=subscription.enterprise_id,
            before={'user_id': target.id, 'granted': before},
            after={'user_id': target.id, 'granted': body.granted})
        db.commit()
        return JSONResponse({'user_id': target.id, 'granted': body.granted}, headers=NO_STORE)
