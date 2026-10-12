"""Crash-safe, staged SaaS provisioning for paid tenant-access orders."""
import json
from datetime import timezone
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select

from .order_economics import money
from .subscription_calendar import CalendarAnchor


class SaaSDeliveryError(Exception):
    pass


def install(ns):
    Operation = ns['SaaSOperation']

    def handles(order):
        return order.snapshot_version == 1 and order.delivery_method_snapshot == 'tenant_access'

    def utc_iso(value):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def preflight(db, order, task):
        economic = ns['order_economics']['economic_snapshot'](order)
        delivery = ns['order_economics']['delivery_snapshot'](order)
        if (not handles(order) or task.order_id != order.id or task.method != 'tenant_access'
                or task.delivery_mode != 'automatic' or delivery.get('method') != 'tenant_access'
                or economic.get('product_id') != order.product_id
                or economic.get('product_version_id') != order.product_version_id
                or economic.get('version_code') != order.product_version_code):
            raise HTTPException(409, 'SaaS 交付任务与订单冻结快照不一致')
        if (order.payment_status != 'paid' or money(order.paid_amount)
                != money(order.amount) or money(order.refunded_amount) != 0
                or order.main_status in {'cancelled', 'closed', 'refunded'}):
            raise HTTPException(409, 'SaaS 自动交付须使用全额支付且未退款的有效订单')
        product = db.get(ns['Product'], order.product_id)
        enterprise = db.get(ns['Enterprise'], order.buyer_enterprise_id)
        if (not product or product.product_type != 'saas' or product.delivery_method != 'tenant_access'
                or product.status == 'security_unpublished' or not enterprise
                or enterprise.verification_status != 'verified'):
            raise HTTPException(409, 'SaaS 产品或买方企业无效')
        ns['subscription_terms']['lock_combination'](db, enterprise.id, product.id)
        binding_model = ns['SaaSCombinationBinding']
        binding = db.scalar(select(binding_model).where(
            binding_model.enterprise_id == enterprise.id, binding_model.product_id == product.id)
            .with_for_update().execution_options(populate_existing=True))
        subscription = db.scalar(select(ns['SaaSSubscription']).where(
            ns['SaaSSubscription'].id == order.subscription_id).with_for_update()
            .execution_options(populate_existing=True)) if order.subscription_id else None
        if (not subscription or subscription.status not in {'provisioning', 'active'}
                or subscription.enterprise_id != enterprise.id or subscription.product_id != product.id):
            raise HTTPException(409, 'SaaS 产品、买方企业或订阅绑定无效')
        if not binding or binding.subscription_id != subscription.id:
            raise HTTPException(409, 'SaaS 企业产品组合绑定与订单不一致')
        version = db.get(ns['SaaSProductVersion'], subscription.version_id)
        if (not version or version.product_id != product.id
                or version.version_code != order.product_version_code):
            raise HTTPException(409, 'SaaS 订阅版本与订单冻结版本不一致')
        config = db.scalar(select(ns['SaaSIntegrationConfig']).where(
            ns['SaaSIntegrationConfig'].product_id == product.id,
            ns['SaaSIntegrationConfig'].status == 'active'))
        required_scopes = {'saas.tenant', 'saas.user'}
        if (not config or config.auth_mode != 'oauth2' or not config.client_id or not config.client_secret
                or not required_scopes.issubset(set((config.scope or '').split()))):
            raise HTTPException(409, 'SaaS OAuth 配置或授权范围无效')
        client_row = db.scalar(select(ns['OIDCClient']).where(
            ns['OIDCClient'].product_id == product.id, ns['OIDCClient'].status == 'active'))
        client = ns['oidc_provider']['client_for'](db, client_row.client_id) if client_row else None
        if not client or client.product_id != product.id:
            raise HTTPException(409, 'SaaS OIDC 客户端未启用')
        try:
            parsed = urlsplit(product.application_url or '')
            port = parsed.port
            callbacks = json.loads(product.oidc_redirect_uris_json or '[]')
            valid_url = (parsed.scheme in {'http', 'https'} and bool(parsed.hostname)
                and not parsed.username and not parsed.password and port != 0
                and '\\' not in product.application_url
                and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in product.application_url))
            valid_callbacks = (isinstance(callbacks, list) and bool(callbacks)
                and all(isinstance(uri, str) and ns['oidc_provider']['valid_redirect'](uri, db=db)
                    for uri in callbacks))
        except (ValueError, TypeError):
            valid_url, valid_callbacks = False, False
        if not valid_url or not valid_callbacks or not product.application_url:
            raise HTTPException(409, 'SaaS 应用地址或 OIDC 回调地址无效')
        members = db.execute(select(ns['Membership'], ns['User']).join(
            ns['User'], ns['User'].id == ns['Membership'].user_id).where(
                ns['Membership'].enterprise_id == enterprise.id,
                ns['Membership'].role == 'super_admin', ns['Membership'].status == 'active',
                ns['User'].is_active.is_(True), ns['User'].activation_status == 'active',
                ns['User'].verified_status == 'verified',
                (ns['User'].platform_role == '') | (ns['User'].platform_role.is_(None)))
            .order_by(ns['User'].id)).all()
        if len(members) != 1:
            raise HTTPException(409, '买方企业须有且仅有一名有效超级管理员')
        membership, admin = members[0]
        return {'product': product, 'enterprise': enterprise, 'subscription': subscription,
            'version': version, 'config': config, 'client': client, 'admin': admin,
            'membership': membership}

    def operation(db, subscription, order, name, payload, suffix=None):
        key = f"delivery:{subscription.id}:{order.id}:{name}"
        if suffix is not None:
            key += f':{suffix}'
        payload = {**payload, 'idempotency_key': key}
        record = db.scalar(select(Operation).where(Operation.idempotency_key == key)
            .with_for_update().execution_options(populate_existing=True))
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
        if record:
            if (record.subscription_id != subscription.id or record.operation != name
                    or record.request_payload != encoded):
                raise HTTPException(409, 'SaaS 操作幂等键与订阅或请求内容冲突')
            return record, False
        record = Operation(subscription_id=subscription.id, operation=name,
            idempotency_key=key, request_payload=encoded, status='prepared', retry_count=0)
        db.add(record)
        db.flush()
        return record, True

    def prepare_or_execute(db, ctx, order, task, name, payload, suffix=None, readonly=False,
                           validate=None):
        record, created = operation(db, ctx['subscription'], order, name, payload, suffix)
        if created:
            return 'pending', None
        if record.status == 'succeeded' and not readonly:
            try:
                return 'done', json.loads(record.response_payload or '{}')
            except (ValueError, TypeError):
                raise HTTPException(409, 'SaaS 成功操作记录无效') from None
        try:
            result = ns['saas_call'](ctx['config'], name, payload_with_key(record))
            if (not isinstance(result, dict) or type(result.get('ret')) is not int
                    or result['ret'] != 0):
                raise SaaSDeliveryError('SaaS operation rejected')
            if validate:
                validate(result)
        except Exception:
            record.status = 'prepared'
            record.retry_count = int(record.retry_count or 0) + 1
            record.error_message = 'SaaS provider call failed'
            raise SaaSDeliveryError('SaaS provider call failed') from None
        record.response_payload = json.dumps(result, ensure_ascii=True, sort_keys=True)
        record.status = 'succeeded'
        record.error_message = ''
        record.completed_at = ns['now']()
        record.retry_count = int(record.retry_count or 0) + 1
        db.flush()
        return 'done', result

    def payload_with_key(record):
        return json.loads(record.request_payload or '{}')

    def require_identity(result, ctx, order, tenant_id=None, status=None):
        sub, product, enterprise, version, admin, client = (ctx[k] for k in
            ('subscription', 'product', 'enterprise', 'version', 'admin', 'client'))
        if (type(result.get('ret')) is not int or result['ret'] != 0
                or result.get('tenant_id') != (tenant_id or sub.external_tenant_id)
                or result.get('product_id') != product.id
                or result.get('enterprise_id') != enterprise.id
                or result.get('subscription_id') != sub.id
                or result.get('version') != version.version_code
                or result.get('admin_user_id') != admin.id
                or result.get('oidc_client_id') != client.client_id
                or result.get('admin_active') is not True
                or result.get('application_ready') is not True
                or status and result.get('status') != status
                or not status and result.get('status') not in {'opened', 'active'}):
            raise SaaSDeliveryError('SaaS response validation failed')

    def ensure_mapping(db, ctx, external_user_id):
        Mapping = ns['SaaSUserMapping']
        mapping = db.scalar(select(Mapping).where(Mapping.subscription_id == ctx['subscription'].id,
            Mapping.user_id == ctx['admin'].id).with_for_update())
        if mapping:
            if mapping.external_user_id != external_user_id or mapping.status != 'active':
                raise HTTPException(409, '现有 SaaS 管理员用户映射与第三方结果不一致')
            return
        db.add(Mapping(subscription_id=ctx['subscription'].id, user_id=ctx['admin'].id,
            department_id=ctx['membership'].department_id or '', external_user_id=external_user_id,
            status='active'))
        db.flush()

    def activation_payload(db, ctx, order, task):
        sub, combination = ctx['subscription'], db.scalar(
            select(ns['SubscriptionCombination']).where(
                ns['SubscriptionCombination'].enterprise_id == order.buyer_enterprise_id,
                ns['SubscriptionCombination'].product_id == order.product_id).with_for_update()
            .execution_options(populate_existing=True))
        delivery_at = ns['now']()
        live = bool(combination and combination.status == 'active'
            and delivery_at < CalendarAnchor(combination.anchor_at).boundary(combination.total_months))
        if live and (combination.version_code != order.product_version_code
                or combination.delivery_method != 'tenant_access'):
            raise HTTPException(409, '有效订阅组合版本不匹配')
        anchor = CalendarAnchor(combination.anchor_at if live else delivery_at)
        start_month = combination.total_months if live else 0
        bounds = anchor.term(order.subscription_months, start_month=start_month)
        return {'tenant_id': sub.external_tenant_id, 'product_id': order.product_id,
            'enterprise_id': order.buyer_enterprise_id, 'subscription_id': sub.id,
            'version': ctx['version'].version_code, 'subscription_months': order.subscription_months,
            'starts_at': utc_iso(bounds.start), 'expires_at': utc_iso(bounds.end),
            'platform_delivery_at': utc_iso(delivery_at), 'admin_user_id': ctx['admin'].id,
            'oidc_client_id': ctx['client'].client_id}

    def process(db, order, task, user, force_failure=False, automatic=False):
        if force_failure:
            raise HTTPException(503, 'SaaS 自动交付尝试失败')
        ctx = preflight(db, order, task)
        sub, admin = ctx['subscription'], ctx['admin']
        try:
            if not sub.external_tenant_id:
                state, response = prepare_or_execute(db, ctx, order, task, 'OPEN', {
                    'product_id': order.product_id, 'enterprise_id': order.buyer_enterprise_id,
                    'subscription_id': sub.id, 'version': ctx['version'].version_code,
                    'admin_user_id': admin.id, 'oidc_client_id': ctx['client'].client_id})
                if state == 'pending':
                    return None
                tenant_id = response.get('tenant_id') if isinstance(response, dict) else None
                if (not isinstance(tenant_id, str) or not tenant_id or len(tenant_id) > 180
                        or response.get('version') != ctx['version'].version_code
                        or response.get('status') != 'opened'
                        or any(response.get(key) != value for key, value in {
                            'product_id': order.product_id, 'enterprise_id': order.buyer_enterprise_id,
                            'subscription_id': sub.id}.items())):
                    raise SaaSDeliveryError('SaaS OPEN response validation failed')
                sub.external_tenant_id = tenant_id
                if not sub.external_app_id and response.get('app_id'):
                    sub.external_app_id = str(response['app_id'])
                if sub.status != 'active':
                    sub.status = 'provisioning'
                db.flush()
                return None

            assign_key = f'assign:{admin.id}'
            assignment = db.scalar(select(Operation).where(
                Operation.idempotency_key == f'delivery:{sub.id}:{order.id}:USER_ASSIGN:{assign_key}'))
            external_user_id = ''
            if assignment and assignment.status == 'succeeded':
                try:
                    assigned = json.loads(assignment.response_payload or '{}')
                    external_user_id = str(assigned.get('external_user_id') or assigned.get('user_id') or '')
                except (TypeError, ValueError):
                    raise HTTPException(409, 'SaaS 管理员分配结果无效') from None
            if not external_user_id:
                def validate_assignment(result):
                    external_id = result.get('external_user_id')
                    if (result.get('tenant_id') != sub.external_tenant_id
                            or result.get('product_id') != order.product_id
                            or result.get('enterprise_id') != order.buyer_enterprise_id
                            or result.get('subscription_id') != sub.id
                            or result.get('platform_user_id') != admin.id
                            or result.get('user_id') != external_id
                            or result.get('status') != 'active'
                            or not isinstance(external_id, str) or not external_id
                            or len(external_id) > 180):
                        raise SaaSDeliveryError('SaaS USER_ASSIGN response validation failed')
                state, response = prepare_or_execute(db, ctx, order, task, 'USER_ASSIGN', {
                    'tenant_id': sub.external_tenant_id, 'product_id': order.product_id,
                    'enterprise_id': order.buyer_enterprise_id, 'subscription_id': sub.id,
                    'user_id': admin.id, 'admin_user_id': admin.id,
                    'department_id': ctx['membership'].department_id or '',
                    'oidc_client_id': ctx['client'].client_id}, suffix=assign_key,
                    validate=validate_assignment)
                if state == 'pending':
                    return None
                external_user_id = response.get('external_user_id')
                if not isinstance(external_user_id, str) or not external_user_id:
                    raise SaaSDeliveryError('SaaS USER_ASSIGN response validation failed')
                ensure_mapping(db, ctx, external_user_id)
                return None
            ensure_mapping(db, ctx, external_user_id)

            prior_status = db.scalar(select(Operation).where(
                Operation.subscription_id == sub.id, Operation.operation == 'STATUS',
                Operation.status == 'succeeded',
                Operation.idempotency_key.like(f'delivery:{sub.id}:{order.id}:STATUS:pre:%'))
                .order_by(Operation.created_at.desc()).with_for_update())
            if not prior_status or prior_status.status != 'succeeded':
                suffix = f'pre:{task.retry_count}'
                state, response = prepare_or_execute(db, ctx, order, task, 'STATUS', {
                    'tenant_id': sub.external_tenant_id, 'product_id': order.product_id,
                    'enterprise_id': order.buyer_enterprise_id, 'subscription_id': sub.id,
                    'version': ctx['version'].version_code, 'admin_user_id': admin.id,
                    'oidc_client_id': ctx['client'].client_id}, suffix=suffix, readonly=True,
                    validate=lambda value: require_identity(value, ctx, order))
                if state == 'pending':
                    return None
                return None

            renew_key = f'delivery:{sub.id}:{order.id}:RENEW'
            renew = db.scalar(select(Operation).where(Operation.idempotency_key == renew_key)
                .with_for_update().execution_options(populate_existing=True))
            if renew is None:
                payload = activation_payload(db, ctx, order, task)
                state, _ = prepare_or_execute(db, ctx, order, task, 'RENEW', payload)
                if state == 'pending':
                    return None
            if renew is not None and renew.status != 'succeeded':
                state, _ = prepare_or_execute(db, ctx, order, task, 'RENEW',
                    json.loads(renew.request_payload or '{}'))
                if state == 'pending':
                    return None
            renew = db.scalar(select(Operation).where(Operation.idempotency_key == renew_key)
                .with_for_update().execution_options(populate_existing=True))
            if not renew or renew.status != 'succeeded':
                return None
            try:
                activation = json.loads(renew.request_payload or '{}')
            except (TypeError, ValueError):
                raise HTTPException(409, 'SaaS 激活请求记录无效') from None
            suffix = f"final:{task.retry_count}"
            status_payload = {key: activation[key] for key in (
                'tenant_id', 'product_id', 'enterprise_id', 'subscription_id', 'version',
                'admin_user_id', 'oidc_client_id')}
            def validate_final(result):
                require_identity(result, ctx, order, status='active')
                if result.get('expires_at') != activation['expires_at']:
                    raise SaaSDeliveryError('SaaS expiry verification failed')
            state, response = prepare_or_execute(db, ctx, order, task, 'STATUS',
                status_payload, suffix=suffix, readonly=True, validate=validate_final)
            if state == 'pending':
                return None
            from datetime import datetime
            granted_at = datetime.fromisoformat(activation['platform_delivery_at'])
            sub.expires_at = datetime.fromisoformat(activation['expires_at'])
            sub.status = 'active'
            sub.last_error = ''
            order.delivery_status = 'pending_acceptance'
            term = ns['subscription_terms']['on_delivery_success'](db, order, user, at=granted_at)
            if not sub.starts_at and term:
                sub.starts_at = term.starts_at
            db.flush()
            return True
        except SaaSDeliveryError:
            raise HTTPException(502, 'SaaS 自动开通或结果校验失败') from None

    return {'handles': handles, 'process': process}
