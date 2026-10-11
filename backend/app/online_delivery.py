"""Verified automatic file fulfillment; API/SaaS remain separate adapters.

Order locks precede task locks. Verification does not download whole archives.
Hooks flush only; process_once owns its worker transaction and never seeds data.
"""
from datetime import timedelta
import json

from fastapi import Depends, HTTPException
from sqlalchemy import func, select

from .order_economics import money


def install(ns):
    Order, Task, File = ns['Order'], ns['DeliveryTask'], ns['FileObject']

    def handles(order):
        return order.snapshot_version == 1 and order.delivery_method_snapshot == 'file'

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

    def process(db, order, task, user, force_failure=False):
        if not handles(order):
            raise HTTPException(409, '此自动交付适配器只支持新版文件订单')
        if (task.order_id != order.id or task.method != 'file' or task.delivery_mode != 'automatic'):
            raise HTTPException(409, '交付任务与订单快照不一致')
        if task.status == 'completed' and order.main_status == 'completed' and order.delivery_status == 'accepted':
            return True
        if task.status not in {'in_delivery', 'retrying'}:
            raise HTTPException(409, '当前任务不能自动处理，请先人工重试')
        if task.retry_count >= task.max_retries:
            raise HTTPException(409, '自动重试已达到三次上限，请人工处理')
        before = state(order, task)
        error = None
        try:
            if force_failure:
                raise HTTPException(503, '交付人员标记本次尝试失败')
            verify(db, order)
        except HTTPException as exc:
            error = str(exc.detail)
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
            task.note = '文件扫描、快照和持久化可读性检查通过，自动履约完成'
            order.delivery_status = 'accepted'
            order.main_status = 'completed'
            action = 'delivery_succeeded'
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
        eligible(order)
        if order.delivery_status != 'accepted' or order.main_status != 'completed':
            raise HTTPException(409, '文件交付尚未完成')
        if item.id not in {row.id for row in files(db, order)}:
            raise HTTPException(403, '文件不属于订单冻结的交付版本')

    @ns['app'].get('/api/orders/{order_id}/delivery-access')
    def access(order_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        order = db.get(Order, order_id)
        if not order:
            raise HTTPException(404, '订单不存在')
        require_buyer(db, user, order)
        result = {'order_id': order.id, 'method': order.delivery_method_snapshot, 'ready': False, 'files': []}
        if not handles(order):
            return {**result, 'reason': '该交付类型的统一访问入口尚未完成'}
        try:
            eligible(order)
            if order.main_status != 'completed' or order.delivery_status != 'accepted':
                raise HTTPException(409, '文件交付尚未完成')
            rows = files(db, order)
        except HTTPException as exc:
            return {**result, 'reason': str(exc.detail)}
        used = db.scalar(select(func.count(ns['FileDownloadLog'].id)).where(
            ns['FileDownloadLog'].order_id == order.id, ns['FileDownloadLog'].success.is_(True))) or 0
        limit = ns['order_economics']['delivery_snapshot'](order)['download_limit']
        remaining = max(0, limit-used) if limit else None
        available = remaining is None or remaining > 0
        return {**result, 'ready': available, 'reason': '' if available else '该订单的下载次数已用尽',
                'files': [{'id': item.id, 'name': item.original_name, 'url': f'/api/files/{item.id}/download?order_id={order.id}' if available else '',
                           'download_limit': limit, 'used_downloads': used, 'remaining_downloads': remaining} for item in rows]}

    def process_once():
        with ns['SessionLocal']() as db:
            actor = db.scalar(select(ns['User']).where(ns['User'].email == 'jfsh@market.local',
                ns['User'].platform_role == 'delivery_monitor', ns['User'].is_active.is_(True),
                ns['User'].activation_status == 'active'))
            if not actor:
                raise RuntimeError('Delivery monitor account unavailable')
            order = db.scalar(select(Order).join(Task, Task.order_id == Order.id).where(
                Order.snapshot_version == 1, Order.delivery_method_snapshot == 'file',
                Task.delivery_mode == 'automatic', Task.method == 'file',
                Task.status.in_(['in_delivery', 'retrying']), Task.next_retry_at <= ns['now']())
                .order_by(Task.next_retry_at, Task.id).with_for_update(skip_locked=True, of=Order)
                .execution_options(populate_existing=True))
            if not order:
                return False
            tasks = db.scalars(select(Task).where(Task.order_id == order.id).with_for_update()
                .execution_options(populate_existing=True)).all()
            if len(tasks) != 1:
                raise RuntimeError('Order has duplicate delivery tasks')
            process(db, order, tasks[0], actor)
            db.commit()
            return True

    hooks = dict(handles=handles, process=process, process_once=process_once, require_download=require_download, retry=retry)
    ns['online_delivery'] = hooks
    return hooks
