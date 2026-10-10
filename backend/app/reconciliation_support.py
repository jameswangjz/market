"""Independent imported-ledger comparison; not a bank/payment integration."""
import hashlib
import json
import secrets
from decimal import Decimal

from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, Numeric, String, Text, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column


class EntryBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entry_no: str = Field(min_length=1, max_length=120)
    order_no: str = Field(min_length=1, max_length=80)
    amount: Decimal = Field(max_digits=18, decimal_places=2)
    currency: str = Field(default='CNY', max_length=3)


class ImportBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ledger_type: str = Field(pattern='^(platform|payment|bank|distribution)$')
    source_kind: str = Field(default='simulation', pattern='^(simulation|manual_import)$')
    source_name: str = Field(min_length=1, max_length=180)
    event_key: str = Field(min_length=1, max_length=180)
    entries: list[EntryBody] = Field(min_length=1, max_length=5000)


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reason: str = Field(min_length=3, max_length=1000)
    evidence_ref: str = Field(min_length=3, max_length=500)


def install(ns):
    Base, app = ns['Base'], ns['app']

    class LedgerImport(Base):
        __tablename__ = 'settlement_ledger_imports'
        id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        batch_id: Mapped[str] = mapped_column(ForeignKey('settlement_batches.id'), index=True)
        event_key: Mapped[str] = mapped_column(String(180), unique=True)
        digest: Mapped[str] = mapped_column(String(64))
        ledger_type: Mapped[str] = mapped_column(String(30))
        source_kind: Mapped[str] = mapped_column(String(30))
        source_name: Mapped[str] = mapped_column(String(180))
        created_by: Mapped[str] = mapped_column(String(180))
        entries_json: Mapped[str] = mapped_column(Text)

    class LedgerReport(Base):
        __tablename__ = 'settlement_ledger_reports'
        id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        import_id: Mapped[str] = mapped_column(ForeignKey('settlement_ledger_imports.id'), unique=True)
        batch_id: Mapped[str] = mapped_column(ForeignKey('settlement_batches.id'), index=True)
        status: Mapped[str] = mapped_column(String(30), default='difference')
        report_json: Mapped[str] = mapped_column(Text)
        resolution: Mapped[str] = mapped_column(Text, default='')
        evidence_ref: Mapped[str] = mapped_column(String(500), default='')
        resolved_by: Mapped[str] = mapped_column(String(180), default='')

    def require_batch(db, batch_id, user):
        ns['require_settlement_operator'](user)
        batch = db.get(ns['SettlementBatch'], batch_id)
        if not batch:
            raise HTTPException(404, '清算批次不存在')
        return batch

    @app.post('/api/settlement-batches/{batch_id}/ledger-import')
    def import_ledger(batch_id: str, body: ImportBody, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        batch = require_batch(db, batch_id, user)
        if not body.event_key.strip() or not body.source_name.strip() or any(e.currency != 'CNY' or not e.entry_no.strip() or not e.order_no.strip() for e in body.entries):
            raise HTTPException(422, '账单流水号及订单号不能为空，当前仅支持CNY')
        payload = body.model_dump(mode='json')
        for entry in payload['entries']:
            entry['amount'] = str(Decimal(entry['amount']).quantize(Decimal('.01')))
        digest = hashlib.sha256(json.dumps({'batch': batch_id, **payload}, sort_keys=True,
                                          ensure_ascii=False).encode()).hexdigest()
        previous = db.scalar(select(LedgerImport).where(LedgerImport.event_key == body.event_key))
        if previous:
            if previous.digest != digest:
                raise HTTPException(409, '导入幂等键已用于不同的账单内容')
            return {'id': previous.id, 'batch_no': batch.batch_no, 'reused': True,
                    'source_kind': previous.source_kind, 'real_channel_verified': False}
        item = LedgerImport(batch_id=batch_id, event_key=body.event_key, digest=digest,
                            ledger_type=body.ledger_type, source_kind=body.source_kind,
                            source_name=body.source_name, created_by=user.email or user.id,
                            entries_json=json.dumps(payload['entries'], ensure_ascii=False))
        connection = db.connection()
        if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql('BEGIN')
        try:
            with db.begin_nested():
                db.add(item)
                db.flush()
        except IntegrityError:
            previous = db.scalar(select(LedgerImport).where(LedgerImport.event_key == body.event_key))
            if not previous or previous.digest != digest:
                raise HTTPException(409, '账单导入幂等键冲突')
            return {'id': previous.id, 'batch_no': batch.batch_no, 'reused': True,
                    'source_kind': previous.source_kind, 'real_channel_verified': False}
        ns['audit'](db, user.email or user.id, 'import_settlement_ledger', 'ledger_import', item.id,
                    category='reconciliation', business_domain='settlement', batch_no=batch.batch_no,
                    after={'entries': len(body.entries), 'ledger_type': body.ledger_type,
                           'source_kind': body.source_kind, 'real_channel_verified': False})
        db.commit()
        return {'id': item.id, 'batch_no': batch.batch_no, 'reused': False,
                'source_kind': item.source_kind, 'real_channel_verified': False}

    @app.post('/api/settlement-batches/{batch_id}/ledger-imports/{import_id}/compare')
    def compare(batch_id: str, import_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        batch = require_batch(db, batch_id, user)
        imported = db.scalar(select(LedgerImport).where(LedgerImport.id == import_id).with_for_update())
        if not imported or imported.batch_id != batch_id:
            raise HTTPException(404, '本批次账单不存在')
        existing = db.scalar(select(LedgerReport).where(LedgerReport.import_id == import_id))
        if existing:
            return {'id': existing.id, 'status': existing.status, **json.loads(existing.report_json)}
        S, L, O = ns['Settlement'], ns['SettlementLine'], ns['Order']
        ids = select(L.settlement_id).where(L.batch_id == batch_id, L.status != 'superseded')
        rows = db.scalars(select(S).where(S.id.in_(ids), S.status != 'superseded')).all()
        if not rows:
            raise HTTPException(409, '该批次没有有效清算单')
        expected = {}
        for settlement in rows:
            order = db.get(O, settlement.order_id)
            if not order:
                raise HTTPException(409, '清算单关联订单缺失')
            value = (sum((Decimal(str(getattr(settlement, field) or 0)) for field in
                          ('platform_fee', 'provider_share', 'service_share', 'expert_fee', 'channel_fee')),
                         Decimal(0)) if imported.ledger_type == 'distribution'
                     else Decimal(str(settlement.net_amount or 0)))
            expected[order.order_no] = expected.get(order.order_no, Decimal(0)) + value
        actual, duplicates, seen = {}, [], set()
        for entry in json.loads(imported.entries_json):
            if entry['entry_no'] in seen:
                duplicates.append(entry['entry_no'])
                continue
            seen.add(entry['entry_no'])
            key = entry['order_no']
            actual[key] = actual.get(key, Decimal(0)) + Decimal(entry['amount'])
        details = []
        for key in sorted(set(expected) | set(actual)):
            e, a = expected.get(key, Decimal(0)), actual.get(key, Decimal(0))
            status = ('unexpected' if key not in expected else 'missing' if key not in actual
                      else 'matched' if e == a else 'difference')
            details.append({'order_no': key, 'expected_amount': str(e.quantize(Decimal('.01'))),
                            'actual_amount': str(a.quantize(Decimal('.01'))),
                            'difference_amount': str((a-e).quantize(Decimal('.01'))), 'status': status})
        report = {'batch_no': batch.batch_no, 'ledger_type': imported.ledger_type,
                  'compared_at': ns['now']().isoformat(), 'basis': 'immutable_settlement_snapshot_at_comparison',
                  'source_kind': imported.source_kind, 'source_name': imported.source_name,
                  'real_channel_verified': False, 'duplicates': duplicates, 'items': details,
                  'amount_basis': 'allocated_profit' if imported.ledger_type == 'distribution' else 'net_order_receipts'}
        status = 'matched' if not duplicates and all(d['status'] == 'matched' for d in details) else 'difference'
        item = LedgerReport(import_id=import_id, batch_id=batch_id, report_json=json.dumps(report), status=status)
        db.add(item)
        db.flush()
        ns['audit'](db, user.email or user.id, 'compare_settlement_ledger', 'ledger_report', item.id,
                    category='reconciliation', business_domain='settlement', batch_no=batch.batch_no,
                    after={'status': status, 'rows': len(details), 'duplicates': len(duplicates)})
        db.commit()
        return {'id': item.id, 'status': item.status, **report}

    @app.get('/api/settlement-batches/{batch_id}/ledger-reports')
    def reports(batch_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        require_batch(db, batch_id, user)
        items = db.scalars(select(LedgerReport).where(LedgerReport.batch_id == batch_id)).all()
        return {'items': [{'id': x.id, 'status': x.status, 'resolution': x.resolution,
                           'evidence_ref': x.evidence_ref, 'resolved_by': x.resolved_by,
                           **json.loads(x.report_json)} for x in items]}

    @app.post('/api/settlement-batches/{batch_id}/ledger-reports/{report_id}/resolve')
    def resolve(batch_id: str, report_id: str, body: ResolveBody,
                user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        batch = require_batch(db, batch_id, user)
        item = db.get(LedgerReport, report_id)
        if not item or item.batch_id != batch_id:
            raise HTTPException(404, '本批次对账报告不存在')
        if not body.reason.strip() or not body.evidence_ref.strip():
            raise HTTPException(422, '必须填写处理说明及依据')
        if item.status != 'difference':
            raise HTTPException(409, '仅差异报告可以处理结案')
        item.status, item.resolution = 'resolved', body.reason
        item.evidence_ref, item.resolved_by = body.evidence_ref, user.email or user.id
        ns['audit'](db, user.email or user.id, 'resolve_settlement_ledger', 'ledger_report', item.id,
                    body.reason, category='reconciliation', business_domain='settlement',
                    batch_no=batch.batch_no, before={'status': 'difference'},
                    after={'status': 'resolved', 'evidence_ref': body.evidence_ref})
        db.commit()
        return {'id': item.id, 'status': item.status, 'real_channel_verified': False}

    @app.get('/api/settlement-batches/{batch_id}/ledger-reports/{report_id}/download')
    def download(batch_id: str, report_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        batch = require_batch(db, batch_id, user)
        item = db.get(LedgerReport, report_id)
        if not item or item.batch_id != batch_id:
            raise HTTPException(404, '本批次对账报告不存在')
        payload = {'id': item.id, 'status': item.status, 'resolution': item.resolution,
                   'evidence_ref': item.evidence_ref, 'resolved_by': item.resolved_by, **json.loads(item.report_json)}
        ns['audit'](db, user.email or user.id, 'download_settlement_ledger_report', 'ledger_report', item.id,
                    category='reconciliation', business_domain='settlement', batch_no=batch.batch_no)
        db.commit()
        return Response(json.dumps(payload, ensure_ascii=False, indent=2), media_type='application/json',
                        headers={'Content-Disposition': f'attachment; filename="ledger-report-{item.id}.json"',
                                 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    return {'LedgerImport': LedgerImport, 'LedgerReport': LedgerReport}
