"""Initial shared API credential issuance and buyer secret downloads."""
import hashlib
import hmac
import os
import secrets
from datetime import timezone
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select


def install(ns):
    Credential = ns['ApiCredential']

    def cipher():
        key = os.getenv('API_CREDENTIAL_ENCRYPTION_KEY', '')
        if not key:
            raise HTTPException(503, 'API凭据加密密钥尚未配置')
        try:
            return Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise HTTPException(503, 'API凭据加密密钥配置无效') from exc

    def encrypt(secret):
        if not isinstance(secret, str) or not secret:
            raise HTTPException(400, 'API凭据密钥不能为空')
        return cipher().encrypt(secret.encode()).decode()

    def secret_download_available(credential):
        return bool(credential and credential.status == 'active'
                    and getattr(credential, 'secret_encrypted', None))

    def public_gateway_origin():
        origin = os.getenv('API_GATEWAY_PUBLIC_URL', '').strip()
        try:
            parsed = urlsplit(origin)
            port = parsed.port
            valid = (parsed.scheme in {'http', 'https'} and bool(parsed.hostname)
                and not parsed.username and not parsed.password and port != 0
                and parsed.path in {'', '/'} and not parsed.query and not parsed.fragment
                and '\\' not in origin
                and not any(char.isspace() or ord(char) < 32 or ord(char) == 127
                            for char in origin))
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(409, '公共API网关地址未配置或无效，无法生成可用凭据下载文件')
        return origin.rstrip('/')

    def ensure_initial(db, order, route, user):
        terms = ns['subscription_terms']
        terms['lock_combination'](db, order.buyer_enterprise_id, order.product_id)
        rows = db.scalars(select(Credential).where(
            Credential.route_id == route.id,
            Credential.enterprise_id == order.buyer_enterprise_id,
            Credential.order_id == '',
        ).order_by(Credential.created_at.desc(), Credential.id).with_for_update()).all()
        if rows:
            active = next((row for row in rows if row.status == 'active'), None)
            return active

        raw_key = 'mk_' + secrets.token_urlsafe(36)
        encrypted = encrypt(raw_key)
        policy = ns['subscription_gateway']['policy'](
            db, order.buyer_enterprise_id, route)
        if not policy or policy['rate_limit_per_minute'] <= 0:
            raise HTTPException(409, '当前API订阅没有有效的凭据策略')
        credential = Credential(
            route_id=route.id,
            enterprise_id=order.buyer_enterprise_id,
            id=secrets.token_hex(16),
            order_id='',
            product_version_id='',
            name=f'{order.product_name} 企业共享 API 凭据',
            key_prefix=raw_key[:12],
            key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
            apisix_consumer_name='',
            status='active',
            rate_limit_per_minute=policy['rate_limit_per_minute'],
            daily_quota=policy['daily_quota'],
            monthly_quota=policy['monthly_quota'],
            total_quota=0,
            created_by=user.email or user.phone or user.id,
            secret_encrypted=encrypted,
        )
        credential.apisix_consumer_name = f'market-consumer-{credential.id}'
        db.add(credential)
        db.flush()

        # The adapter registers root-transaction compensation before this PUT.
        ns['_track_api_consumer_compensation'](db, credential.apisix_consumer_name)
        try:
            ns['sync_apisix_consumer'](credential, raw_key, route, db)
            consumer = ns['apisix_admin_request'](
                'GET', f'/consumers/{credential.apisix_consumer_name}')
            while isinstance(consumer, dict) and isinstance(consumer.get('node'), dict):
                consumer = consumer['node']
            while isinstance(consumer, dict) and isinstance(consumer.get('value'), dict):
                consumer = consumer['value']
            plugins = consumer.get('plugins', {}) if isinstance(consumer, dict) else {}
            key_auth = plugins.get('key-auth', {}) if isinstance(plugins, dict) else {}
            quota = plugins.get('market-gateway-quota', {}) if isinstance(plugins, dict) else {}
            raw_consumer_key = key_auth.get('key') if isinstance(key_auth, dict) else None
            if (not isinstance(consumer, dict)
                    or consumer.get('username') != credential.apisix_consumer_name
                    or consumer.get('status') in (False, 0, '0', 'disabled', 'Disabled', 'inactive')
                    or consumer.get('enabled') is False or consumer.get('enable') is False
                    or not isinstance(raw_consumer_key, str)
                    or not hmac.compare_digest(hashlib.sha256(raw_consumer_key.encode()).hexdigest(),
                                               credential.key_hash)
                    or not raw_consumer_key.startswith(credential.key_prefix)
                    or key_auth.get('anonymous') or key_auth.get('disable') is True
                    or (isinstance(key_auth.get('_meta'), dict)
                        and key_auth['_meta'].get('disable') is True)
                    or not isinstance(quota, dict) or quota.get('route_key') != route.route_key
                    or quota.get('disable') is True or quota.get('disabled') is True
                    or (isinstance(quota.get('_meta'), dict)
                        and quota['_meta'].get('disable') is True)):
                raise HTTPException(409, 'API凭据未能通过APISIX消费者核验')
            expected_policy = ns['subscription_gateway']['metadata'](db, credential, route)
            redis = ns['apisix_policy_redis']()
            actual_policy = redis.hgetall(
                'market:apisix:credential:' + credential.apisix_consumer_name)
            if (not expected_policy or set(actual_policy) != set(expected_policy)
                    or any(str(actual_policy[key]) != str(value)
                           for key, value in expected_policy.items())):
                raise HTTPException(409, 'API凭据订阅策略未能通过Redis核验')
            ns['audit'](db, credential.created_by, 'issue_initial_api_credential',
                'api_credential', credential.id, '自动签发企业共享API凭据；不包含密钥',
                tenant_id=credential.enterprise_id, order_id=order.id)
        except Exception:
            ns['_cleanup_api_consumer_compensation'](db, credential.apisix_consumer_name)
            raise
        return credential

    def download(order_id, credential_id, user=Depends(ns['current_user']),
                 db=Depends(ns['db_session'])):
        order = db.get(ns['Order'], order_id)
        if not order:
            raise HTTPException(404, '订单不存在')
        if user.platform_role:
            raise HTTPException(403, '仅买方企业已认证管理员可以下载API凭据')
        ns['trading_policy']['require_buyer'](db, user, order.buyer_enterprise_id)
        order, product, route, enterprise_id = ns['api_order_context'](order_id, user, db)
        if user.verified_status != 'verified':
            raise HTTPException(403, '仅买方企业已认证管理员可以下载API凭据')
        membership = ns['require_enterprise_admin'](db, user, enterprise_id)
        if membership.role not in {'super_admin', 'enterprise_admin'}:
            raise HTTPException(403, '仅买方企业超级管理员或企业管理员可以下载API凭据')
        credential = db.scalar(select(Credential).where(
            Credential.id == credential_id,
            Credential.route_id == route.id,
            Credential.enterprise_id == enterprise_id,
            Credential.order_id == '',
        ))
        entitlement = ns['subscription_terms']['current_entitlement'](
            db, enterprise_id, order.product_id)
        now = ns['now']()
        expiry = credential.expires_at if credential else None
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if (not credential or not entitlement
                or entitlement.get('delivery_method') not in {'api', 'model_api'}
                or entitlement['version_code'] != route.version
                or credential.status != 'active'
                or (expiry and expiry <= now)):
            raise HTTPException(404, '有效共享API凭据不存在')
        encrypted = getattr(credential, 'secret_encrypted', None)
        if not encrypted:
            raise HTTPException(409, '该历史凭据仅保存不可恢复的密钥摘要；请使用受控凭据轮换流程')
        try:
            secret = cipher().decrypt(encrypted.encode()).decode()
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(503, 'API凭据无法解密；请检查持久化加密密钥配置') from exc
        if not hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(), credential.key_hash):
            raise HTTPException(503, 'API凭据加密内容与摘要不匹配')
        public_origin = public_gateway_origin()
        ns['audit'](db, user.email or user.phone or user.id,
            'download_api_credential', 'api_credential', credential.id,
            '下载企业共享API凭据；审计记录不包含密钥',
            tenant_id=enterprise_id, order_id=order.id)
        db.commit()
        return JSONResponse(
            content={'credential_id': credential.id, 'key_id': credential.id,
                     'name': credential.name,
                     'api_key': secret, 'route_key': route.route_key,
                     'gateway_base_path': f'/gateway/{route.route_key}',
                     'gateway_url': public_origin + f'/gateway/{route.route_key}',
                     'rate_limit_per_minute': credential.rate_limit_per_minute
                         if credential.rate_limit_per_minute is not None
                         else route.rate_limit_per_minute,
                     'daily_quota': credential.daily_quota
                         if credential.daily_quota is not None else route.daily_quota,
                     'monthly_quota': credential.monthly_quota
                         if credential.monthly_quota is not None else route.monthly_quota,
                     'total_quota': credential.total_quota or 0,
                     'quota': {
                         'rate_limit_per_minute': credential.rate_limit_per_minute
                             if credential.rate_limit_per_minute is not None
                             else route.rate_limit_per_minute,
                         'daily_quota': credential.daily_quota
                             if credential.daily_quota is not None else route.daily_quota,
                         'monthly_quota': credential.monthly_quota
                             if credential.monthly_quota is not None else route.monthly_quota,
                         'total_quota': credential.total_quota or 0,
                     }},
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate',
                'Pragma': 'no-cache',
                'Content-Disposition': f'attachment; filename="api-credential-{credential.id}.json"',
            },
        )

    ns['app'].add_api_route(
        '/api/orders/{order_id}/api-credentials/{credential_id}/download',
        download, methods=['GET'], name='download_buyer_api_credential')
    return {'ensure_initial': ensure_initial, 'download': download, 'cipher': cipher,
            'encrypt': encrypt, 'secret_download_available': secret_download_available}
