"""Shared, permission-neutral filters for the after-sales and refund lists."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func, or_, select


def contains(column, value):
    value = (value or '').strip()
    if len(value) > 180:
        raise HTTPException(422, '检索条件不能超过180个字符')
    return column.ilike('%' + value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%', escape='\\')


def dates(statement, column, start=None, end=None):
    if start and end and start > end:
        raise HTTPException(422, '开始日期不能晚于结束日期')
    zone = ZoneInfo('Asia/Shanghai')
    if start:
        statement = statement.where(column >= datetime.combine(start, time.min, zone).astimezone(timezone.utc))
    if end:
        if end == date.max:
            raise HTTPException(422, '结束日期超出支持范围')
        statement = statement.where(column < datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(timezone.utc))
    return statement


def party_filters(statement, Order, Enterprise, *, product='', provider='', buyer='', order_id=''):
    if order_id:
        statement = statement.where(or_(Order.id == order_id, contains(Order.order_no, order_id)))
    if product:
        statement = statement.where(contains(Order.product_name, product))
    if provider:
        statement = statement.where(Order.provider_enterprise_id.in_(select(Enterprise.id).where(contains(Enterprise.name, provider))))
    if buyer:
        statement = statement.where(or_(contains(Order.buyer_name, buyer), Order.buyer_enterprise_id.in_(
            select(Enterprise.id).where(contains(Enterprise.name, buyer)))))
    return statement


def paged(db, statement, page, size, *sort):
    # Refund integrations already accept any size 1..100; keep that contract.
    if not 1 <= size <= 100 or page < 1 or page > 1000000:
        raise HTTPException(422, '分页参数不合法')
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    page = min(page, max(1, (total + size - 1) // size))
    rows = list(db.scalars(statement.order_by(*sort).offset((page - 1) * size).limit(size)))
    return rows, {'total': total, 'page': page, 'page_size': size}


def parties(db, orders, Enterprise):
    ids = {key for order in orders for key in (order.provider_enterprise_id, order.buyer_enterprise_id) if key}
    return dict(db.execute(select(Enterprise.id, Enterprise.name).where(Enterprise.id.in_(ids))).all()) if ids else {}


def order_labels(order, names):
    return {'order_no': order.order_no, 'product_name': order.product_name,
            'provider_name': names.get(order.provider_enterprise_id, ''),
            'buyer_name': order.buyer_name or names.get(order.buyer_enterprise_id, '')}
