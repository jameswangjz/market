"""Audit querying, bounded CSV exports and object-scoped event timelines."""
import csv
import json
from datetime import datetime, timezone
from io import StringIO

from fastapi import Depends, HTTPException, Query, Response
from sqlalchemy import Index, func, or_, select
from sqlalchemy.schema import CreateIndex


def install(ns):
    app, Audit, Order = ns['app'], ns['AuditLog'], ns['Order']
    current_user, session = ns['current_user'], ns['db_session']
    indexes = [Index('ix_audit_created_id', Audit.created_at, Audit.id),
               Index('ix_audit_category_created', Audit.category, Audit.created_at),
               Index('ix_audit_order_created', Audit.order_id, Audit.created_at),
               Index('ix_audit_batch_created', Audit.batch_no, Audit.created_at)]

    @app.on_event('startup')
    def ensure_indexes():
        with ns['engine'].begin() as connection:
            for index in indexes:
                connection.execute(CreateIndex(index, if_not_exists=True))

    def date(value, name):
        if not value or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError as exc:
            raise HTTPException(422, f'{name}必须是有效的ISO日期时间') from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def query(db, category=None, q=None, actor=None, order_id=None, batch_no=None,
              rule_version=None, risk_level=None, start=None, end=None):
        start_at, end_at = date(start, 'start'), date(end, 'end')
        if start_at and end_at and start_at > end_at:
            raise HTTPException(422, '开始时间不能晚于结束时间')
        conditions = []
        if category == 'settlement_all':
            conditions.append(or_(Audit.business_domain == 'settlement', Audit.category.in_(
                ['settlement', 'settlement_rule', 'settlement_payment', 'settlement_adjustment',
                 'settlement_report', 'measurement', 'reconciliation'])))
        elif category:
            conditions.append(Audit.category == category)
        def contains(column, value):
            escaped = value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            return column.ilike('%' + escaped + '%', escape='\\')
        if q:
            conditions.append(or_(*(contains(column, q) for column in
                                    (Audit.action, Audit.target_type, Audit.target_id, Audit.detail))))
        if actor:
            conditions.append(contains(Audit.actor, actor))
        if order_id:
            ids = select(Order.id).where(or_(Order.id == order_id, Order.order_no == order_id))
            conditions.append(or_(Audit.order_id == order_id, Audit.order_id.in_(ids)))
        for column, value in ((Audit.batch_no, batch_no), (Audit.rule_version, rule_version),
                              (Audit.risk_level, risk_level)):
            if value:
                conditions.append(column == value)
        if start_at:
            conditions.append(Audit.created_at >= start_at)
        if end_at:
            conditions.append(Audit.created_at <= end_at)
        return select(Audit).where(*conditions).order_by(Audit.created_at.desc(), Audit.id.desc())

    def objects(db, rows):
        ids = {row.order_id for row in rows if row.order_id}
        orders = {o.id: o.order_no for o in db.scalars(select(Order).where(Order.id.in_(ids)))} if ids else {}
        def value(raw):
            try:
                return json.loads(raw or '{}')
            except (TypeError, ValueError):
                return {'legacy_summary': raw}
        return [{key: getattr(row, key) for key in
                 ('id', 'actor', 'action', 'target_type', 'target_id', 'result', 'detail', 'category',
                  'business_domain', 'tenant_id', 'order_id', 'batch_no', 'rule_version', 'request_id',
                  'risk_level', 'created_at')} | {'order_no': orders.get(row.order_id, ''),
                                                'before': value(row.before_json),
                                                'after': value(row.after_json)} for row in rows]

    @app.get('/api/audit-logs/export')
    def export(category: str | None = None, q: str | None = None, actor: str | None = None,
               order_id: str | None = None, batch_no: str | None = None, rule_version: str | None = None,
               risk_level: str | None = None, start: str | None = None, end: str | None = None,
               user=Depends(current_user), db=Depends(session)):
        ns['require_audit_viewer'](user)
        stmt = query(db, category, q, actor, order_id, batch_no, rule_version, risk_level, start, end)
        count = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        if count > 10000:
            raise HTTPException(413, '导出超过10000条，请缩小时间范围或增加筛选条件')
        rows = objects(db, db.scalars(stmt).all())
        buffer = StringIO(newline='')
        fields = ['created_at', 'actor', 'category', 'action', 'target_type', 'target_id', 'result',
                  'order_no', 'order_id', 'batch_no', 'rule_version', 'request_id', 'risk_level',
                  'detail', 'before', 'after']
        writer = csv.writer(buffer)
        writer.writerow(fields)
        for row in rows:
            cells = []
            for field in fields:
                value = row[field]
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False, default=str)
                text = str(value or '')
                if text.lstrip().startswith(('=', '+', '-', '@')):
                    text = "'" + text
                cells.append(text)
            writer.writerow(cells)
        ns['audit'](db, user.email or user.id, 'export_audit_logs', 'audit_export',
                    detail=f'rows={count}', category='security',
                    after={'rows': count, 'filters': {'category': category, 'order_id': order_id,
                            'batch_no': batch_no, 'rule_version': rule_version, 'start': start, 'end': end}})
        db.commit()
        return Response('\ufeff' + buffer.getvalue(), media_type='text/csv',
                        headers={'Content-Disposition': 'attachment; filename="audit-logs.csv"',
                                 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.get('/api/audit-logs/{event_id}/timeline')
    def timeline(event_id: str, page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=200),
                 user=Depends(current_user), db=Depends(session)):
        ns['require_audit_viewer'](user)
        event = db.get(Audit, event_id)
        if not event:
            raise HTTPException(404, '审计事件不存在')
        # Order-specific timelines never expand to unrelated orders in the same batch.
        if event.order_id:
            condition = Audit.order_id == event.order_id
        elif event.target_id:
            condition = (Audit.target_type == event.target_type) & (Audit.target_id == event.target_id)
        elif event.batch_no:
            condition = (Audit.batch_no == event.batch_no) & (Audit.order_id == '')
        else:
            condition = Audit.id == event.id
        stmt = select(Audit).where(condition).order_by(Audit.created_at, Audit.id)
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = objects(db, db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all())
        current = objects(db, [event])[0]
        ns['audit'](db, user.email or user.id, 'view_audit_timeline', 'audit_event', event.id,
                    category='security', after={'event_id': event.id, 'rows': len(rows)})
        db.commit()
        return {'event': current, 'items': rows, 'total': total, 'page': page, 'page_size': page_size}

    return {'query': query, 'objects': objects}
