"""Verified automatic file fulfillment; API/SaaS remain separate adapters.

Order locks precede task locks. Verification does not download whole archives.
Hooks flush only; process_once owns its worker transaction and never seeds data.
"""
from datetime import timedelta
import json
import os
import re
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from .order_economics import money
from .oidc_provider import NO_STORE
from .saas_auto_delivery import install as install_saas_delivery
from .api_auto_delivery import install as install_api_delivery


def install(ns):
    Order, Task, File = ns['Order'], ns['DeliveryTask'], ns['FileObject']
    saas = install_saas_delivery(ns)
    api = install_api_delivery(ns)

    def handles(order):
        return (order.snapshot_version == 1
                and order.delivery_method_snapshot in {'file', 'tenant_access', 'api', 'model_api'})

    def method(order):
        return order.delivery_method_snapshot

    def eligible(order):
        if (order.payment_status != 'paid' or money(order.paid_amount) != money(order.amount)
                or money(order.refunded_amount) != 0 or order.main_status in {'cancelled', 'closed', 'refunded'}):
            raise HTTPException(409, '交付须使用全额支付且未退款的有效订单')
        ns['order_economics']['economic_snapshot'](order)
        ns['order_economics']['delivery_snapshot'](order)

    def files(db, order):
        product = db.get(ns['Product'], order.product_id)
        if not product or product.status == 'security_unpublished':
            raise HTTPException(409, '产品已安全下架或不存在，不能交付或下载')
        frozen = ns['order_economics']['delivery_snapshot'](order).get('files')
        if (not isinstance(frozen, list) or not frozen
                or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in frozen)
                or len({item['id'] for item in frozen}) != len(frozen)):
            raise HTTPException(409, '订单未冻结有效交付文件')
        rows = []
        for reference in frozen:
            item = db.get(File, reference['id'])
            if (not item or item.status not in {'draft', 'active'} or item.file_role != 'product_data'
                    or item.product_id != order.product_id or item.version_id != order.product_version_id
                    or not item.checksum or item.checksum != reference.get('checksum') or item.size <= 0
                    or item.scan_status != 'clean'):
                raise HTTPException(409, '订单版本文件已失效或病毒扫描未通过')
            try:
                report = json.loads(item.scan_report or '{}')
                if report.get('presidio_status') != 'available':
                    raise ValueError('Presidio not available')
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(409, '交付文件尚无有效Presidio扫描报告') from None
            rows.append(item)
        return rows

    def verify(db, order):
        eligible(order)
        rows = files(db, order)
        if not ns['MINIO_ENDPOINT']:
            raise HTTPException(503, '文件存储服务未配置')
        client = ns['Minio'](ns['MINIO_ENDPOINT'], access_key=ns['MINIO_ACCESS_KEY'],
            secret_key=ns['MINIO_SECRET_KEY'], secure=False)
        for item in rows:
            response = None
            try:
                stat = client.stat_object(ns['MINIO_BUCKET'], item.object_name)
                if stat.size != item.size:
                    raise HTTPException(409, '持久化文件大小与登记快照不一致')
                response = client.get_object(ns['MINIO_BUCKET'], item.object_name, offset=0, length=1)
                if not response.read(1):
                    raise HTTPException(409, '持久化文件不可读取')
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(503, '持久化文件暂不可读取') from None
            finally:
                if response is not None:
                    response.close()
                    response.release_conn()
        return rows

    def state(order, task):
        return {'main_status': order.main_status, 'delivery_status': order.delivery_status,
                'task_status': task.status, 'retry_count': task.retry_count}

    def process(db, order, task, user, force_failure=False, automatic=False):
        if not handles(order):
            raise HTTPException(409, '此自动交付适配器只支持新版线上订单')
        if (task.order_id != order.id or task.method != method(order) or task.delivery_mode != 'automatic'):
            raise HTTPException(409, '交付任务与订单快照不一致')
        if task.status == 'completed' and order.main_status in {'completed', 'fulfilling', 'pending_fulfillment'} and order.delivery_status in {'accepted', 'awaiting_effective'}:
            return True
        if task.status not in {'in_delivery', 'retrying'}:
            raise HTTPException(409, '当前任务不能自动处理，请先人工重试')
        if task.retry_count >= task.max_retries:
            raise HTTPException(409, '自动重试已达到三次上限，请人工处理')
        before = state(order, task)
        error = None
        pending = False
        try:
            if force_failure:
                raise HTTPException(503, '交付人员标记本次尝试失败')
            if method(order) == 'tenant_access':
                with db.begin_nested():
                    pending = saas['process'](db, order, task, user, automatic=automatic) is None
            elif method(order) in {'api', 'model_api'}:
                with db.begin_nested():
                    pending = api['process'](db, order, task, user) is None
            else:
                verify(db, order)
        except HTTPException as exc:
            error = str(exc.detail)
        except SQLAlchemyError:
            raise
        except Exception:
            if method(order) not in {'tenant_access', 'api', 'model_api'}:
                raise
            error = ('API 自动交付或结果校验失败'
                if method(order) in {'api', 'model_api'} else 'SaaS 自动开通或结果校验失败')
        if pending:
            task.next_retry_at = ns['now']() + timedelta(seconds=2)
            task.note = ('API 自动交付阶段已持久化，等待后台继续处理'
                if method(order) in {'api', 'model_api'} else 'SaaS 自动开通阶段已持久化，等待后台继续处理')
            order.delivery_status = 'in_delivery'
            order.updated_at = ns['now']()
            db.flush()
            return None
        task.next_retry_at = None
        if error:
            task.retry_count += 1
            task.last_error = error
            task.status = 'retrying' if task.retry_count < task.max_retries else 'exception'
            task.next_retry_at = ns['now']() + timedelta(seconds=10) if task.status == 'retrying' else None
            task.note = '自动检查失败，等待重试' if task.status == 'retrying' else '本轮三次自动尝试失败，转交付人员处理'
            order.delivery_status = 'in_delivery' if task.status == 'retrying' else 'exception'
            action = 'delivery_failed'
        else:
            task.last_error = ''
            task.status = 'completed'
            task.note = ('API 路由、上游健康与订阅策略已核验，订单已履约；企业共享凭据状态已保留，买方企业管理员可在订单中查看和管理凭据'
                if method(order) in {'api', 'model_api'} else
                'SaaS 租户、管理员、OIDC 与订阅期限已核验，自动履约完成'
                if method(order) == 'tenant_access' else
                '文件扫描、快照和持久化可读性检查通过，自动履约完成')
            order.delivery_status = 'accepted'
            order.main_status = 'completed'
            action = 'delivery_succeeded'
            lifecycle = ns.get('order_service_lifecycle')
            if lifecycle:
                lifecycle['delivered'](db, order, task, user)
            if order.delivery_status == 'awaiting_effective':
                action = 'delivery_prepared'
            if order.delivery_status == 'accepted':
                ns['order_sla_contracts']['record_verified_delivery'](db, order, task)
        for domain in ('main', 'delivery'):
            old = before[domain+'_status']
            new = getattr(order, domain+'_status')
            if old != new:
                ns['log_state'](db, order, domain, old, new, action, user, task.note)
        order.updated_at = ns['now']()
        db.flush()
        version = f'{task.id}:{task.status}:{task.retry_count}'
        ns['audit'](db, user.email or user.phone or user.id, action, 'delivery_task', task.id,
            task.note, category='delivery', business_domain='trading', order_id=order.id,
            tenant_id=order.buyer_enterprise_id, before=before, after=state(order, task),
            risk_level='high' if task.status == 'exception' else 'warning' if error else 'normal')
        ns['settlement_metering']['record_order_event'](db, order, action, version, user.id, task.id)
        ns['notify_order_event'](db, order, action, version, target_type='delivery_task', target_id=task.id)
        return error is None

    def require_buyer(db, user, order):
        ns['trading_policy']['require_buyer'](db, user, order.buyer_enterprise_id)

    def retry(db, user, order, reason):
        ns['require_delivery_operator'](user)
        lifecycle = ns.get('order_service_lifecycle')
        if lifecycle and db.get(lifecycle['State'], order.id):
            raise HTTPException(409, '已配置的订阅请使用订单中的重试服务生效，不重复开通或续费')
        if order.delivery_status != 'exception':
            raise HTTPException(409, '仅交付异常订单可以开始新一轮重试')
        tasks = db.scalars(select(Task).where(Task.order_id == order.id).with_for_update()).all()
        if len(tasks) != 1 or tasks[0].status != 'exception':
            raise HTTPException(409, '交付任务状态异常')
        eligible(order)
        task = tasks[0]
        before = {**state(order, task), 'max_retries': task.max_retries}
        # Keep the cumulative attempt counter so event idempotency survives manual retries.
        task.max_retries = task.retry_count + 3
        task.status, task.last_error, task.next_retry_at = 'retrying', '', ns['now']()
        order.delivery_status, order.updated_at = 'in_delivery', ns['now']()
        ns['log_state'](db, order, 'delivery', 'exception', 'in_delivery', 'retry_delivery', user, reason)
        ns['audit'](db, user.email or user.phone or user.id, 'retry_delivery', 'delivery_task', task.id,
            reason, category='delivery', order_id=order.id, before=before,
            after={**state(order, task), 'max_retries': task.max_retries})
        ns['notify_order_event'](db, order, 'retry_delivery', f'{task.id}:{task.max_retries}')
        db.flush()

    def require_download(db, user, order, item):
        require_buyer(db, user, order)
        ns['refund_file_resources']['require_download'](db, order)
        eligible(order)
        if order.delivery_status != 'accepted' or order.main_status != 'completed':
            raise HTTPException(409, '文件交付尚未完成')
        if item.id not in {row.id for row in files(db, order)}:
            raise HTTPException(403, '文件不属于订单冻结的交付版本')

    def access_order(db, order):
        ns['refund_file_resources']['require_download'](db, order)
        eligible(order)
        allowed = {'fulfilling'} if method(order) in {'api', 'model_api', 'tenant_access'} and order.service_starts_at else {'completed'}
        if order.main_status not in allowed or order.delivery_status != 'accepted':
            raise HTTPException(409, '订单交付尚未完成')

    def api_access(db, order):
        product = db.get(ns['Product'], order.product_id)
        if not product or product.status not in {'published', 'draft'}:
            raise HTTPException(409, '产品已安全下架或不存在，不能访问')
        entitlement = ns['subscription_terms']['current_entitlement'](
            db, order.buyer_enterprise_id, order.product_id)
        if (not entitlement or entitlement['delivery_method'] != order.delivery_method_snapshot
                or entitlement['delivery_method'] not in {'api', 'model_api'}):
            raise HTTPException(409, 'API订阅尚未成功交付或已到期')
        route = ns['product_gateway_route'](db, product.id, entitlement['version_code'], active=True)
        if not route:
            raise HTTPException(409, '当前订阅版本的API网关路由尚未启用')
        now = ns['now']()
        credential = db.scalar(select(ns['ApiCredential']).where(
            ns['ApiCredential'].route_id == route.id,
            ns['ApiCredential'].enterprise_id == order.buyer_enterprise_id,
            ns['ApiCredential'].order_id == '',
            ns['ApiCredential'].status == 'active',
            ns['ApiCredential'].apisix_consumer_name != '',
            (ns['ApiCredential'].expires_at.is_(None)) | (ns['ApiCredential'].expires_at > now),
        ).order_by(ns['ApiCredential'].created_at.desc(), ns['ApiCredential'].id))
        limits = entitlement['limits']
        expires_at = entitlement['expires_at']
        public_origin = os.getenv('API_GATEWAY_PUBLIC_URL', '').strip()
        valid_origin = False
        try:
            parsed_origin = urlsplit(public_origin)
            port = parsed_origin.port
            valid_origin = (parsed_origin.scheme in {'http', 'https'} and bool(parsed_origin.hostname)
                and not parsed_origin.username and not parsed_origin.password and port != 0
                and parsed_origin.path in {'', '/'} and not parsed_origin.query and not parsed_origin.fragment
                and '\\' not in public_origin
                and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in public_origin))
        except ValueError:
            pass
        valid_route_key = bool(re.fullmatch(r'[A-Za-z0-9_-]{1,100}', route.route_key or ''))
        ready = credential is not None and valid_origin and valid_route_key
        reason = ''
        if not valid_origin:
            reason = '公共API网关地址未配置或无效'
        elif not valid_route_key:
            reason = '当前API网关路由标识无效'
        elif credential is None:
            reason = '尚无有效共享API凭据，请先配置凭据'
        gateway_path = f'/gateway/{route.route_key}' if valid_route_key else ''
        return {'ready': ready, 'reason': reason, 'api': {
            'gateway_base_path': gateway_path,
            'gateway_url': (public_origin.rstrip('/') + gateway_path) if valid_origin and valid_route_key else '',
            'credentials_path': f'/api/orders/{order.id}/api-credentials',
            'shared_scope': 'enterprise', 'version_code': entitlement['version_code'],
            'expires_at': expires_at.isoformat() if expires_at else None,
            'limits': {key: limits.get(key, 0) for key in (
                'rate_limit_per_minute', 'daily_quota', 'monthly_quota', 'quota_amount')},
        }}

    def saas_access(db, user, order):
        subscription = db.get(ns['SaaSSubscription'], order.subscription_id) if order.subscription_id else None
        if (not subscription or subscription.enterprise_id != order.buyer_enterprise_id
                or subscription.product_id != order.product_id):
            raise HTTPException(409, '订单未关联有效的买方SaaS订阅')
        _, product = ns['applications_access'](db, user, subscription.id,
            enterprise_id=order.buyer_enterprise_id, product_id=order.product_id)
        return {'ready': True, 'reason': '', 'saas': {
            'subscription_id': subscription.id,
            'launch_path': f'/api/orders/{order.id}/delivery-launch',
            'application_url': product.application_url,
            'expires_at': subscription.expires_at.isoformat() if subscription.expires_at else None,
        }}

    @ns['app'].get('/api/orders/{order_id}/delivery-access')
    def access(order_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        order = db.scalar(select(Order).where(Order.id == order_id).with_for_update()
                          .execution_options(populate_existing=True))
        if not order:
            return JSONResponse({'detail': '订单不存在'}, status_code=404, headers=NO_STORE)
        try:
            require_buyer(db, user, order)
        except HTTPException as exc:
            return JSONResponse({'detail': exc.detail}, status_code=exc.status_code, headers=NO_STORE)
        result = {'order_id': order.id, 'method': order.delivery_method_snapshot, 'ready': False,
                  'reason': '', 'files': [], 'api': None, 'saas': None}
        if order.snapshot_version != 1 or order.delivery_method_snapshot not in {'file', 'api', 'model_api', 'tenant_access'}:
            return JSONResponse({**result, 'reason': '历史或线下订单不支持统一线上访问'}, headers=NO_STORE)
        try:
            access_order(db, order)
            if order.delivery_method_snapshot == 'file':
                rows = files(db, order)
            elif order.delivery_method_snapshot in {'api', 'model_api'}:
                return JSONResponse({**result, **api_access(db, order)}, headers=NO_STORE)
            else:
                return JSONResponse({**result, **saas_access(db, user, order)}, headers=NO_STORE)
        except HTTPException as exc:
            return JSONResponse({**result, 'reason': str(exc.detail)}, headers=NO_STORE)
        used = db.scalar(select(func.count(ns['FileDownloadLog'].id)).where(
            ns['FileDownloadLog'].order_id == order.id, ns['FileDownloadLog'].success.is_(True))) or 0
        limit = ns['order_economics']['delivery_snapshot'](order)['download_limit']
        remaining = max(0, limit-used) if limit else None
        available = remaining is None or remaining > 0
        return JSONResponse({**result, 'ready': available, 'reason': '' if available else '该订单的下载次数已用尽',
                'files': [{'id': item.id, 'name': item.original_name, 'url': f'/api/files/{item.id}/download?order_id={order.id}' if available else '',
                           'download_limit': limit, 'used_downloads': used, 'remaining_downloads': remaining} for item in rows]}, headers=NO_STORE)

    @ns['app'].post('/api/orders/{order_id}/delivery-launch')
    def launch(order_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        order = db.get(Order, order_id)
        if not order:
            raise HTTPException(404, '订单不存在')
        require_buyer(db, user, order)
        if order.snapshot_version != 1 or order.delivery_method_snapshot != 'tenant_access':
            raise HTTPException(409, '仅新版SaaS订单支持在线启动')
        access_order(db, order)
        data = saas_access(db, user, order)
        launch_url = ns['applications_launch'](db, user, data['saas']['subscription_id'])
        return JSONResponse({'order_id': order.id, 'launch_url': launch_url}, headers=NO_STORE)

    def process_once():
        with ns['SessionLocal']() as db:
            actor = db.scalar(select(ns['User']).where(ns['User'].email == 'jfsh@market.local',
                ns['User'].platform_role == 'delivery_monitor', ns['User'].is_active.is_(True),
                ns['User'].activation_status == 'active'))
            if not actor:
                raise RuntimeError('Delivery monitor account unavailable')
            lifecycle = ns.get('order_service_lifecycle')
            if lifecycle and lifecycle['process_due'](db, actor):
                db.commit()
                return True
            order = db.scalar(select(Order).join(Task, Task.order_id == Order.id).where(
                Order.snapshot_version == 1, Order.delivery_method_snapshot.in_(['file', 'tenant_access', 'api', 'model_api']),
                Task.delivery_mode == 'automatic', Task.method == Order.delivery_method_snapshot,
                Task.status.in_(['in_delivery', 'retrying']), Task.next_retry_at <= ns['now']())
                .order_by(Task.next_retry_at, Task.id).with_for_update(skip_locked=True, of=Order)
                .execution_options(populate_existing=True))
            if not order:
                return False
            tasks = db.scalars(select(Task).where(Task.order_id == order.id).with_for_update()
                .execution_options(populate_existing=True)).all()
            if len(tasks) != 1:
                raise RuntimeError('Order has duplicate delivery tasks')
            process(db, order, tasks[0], actor, automatic=True)
            db.commit()
            return True

    hooks = dict(handles=handles, process=process, process_once=process_once,
        require_download=require_download, retry=retry)
    ns['online_delivery'] = hooks
    return hooks
