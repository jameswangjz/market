"""Immutable ticket comments/evidence, separate from the SLA business sequence."""
from contextlib import contextmanager
import hashlib
import json
import logging
import secrets
from urllib.parse import quote

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import mapped_column
from urllib3 import PoolManager, Timeout

from .after_sales_workflow import ENDED


class CommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=10000)
    attachments: list[str] = Field(default_factory=list, max_length=10)
    expected_version: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=8, max_length=100)


def install(ns, workflow):
    Base, Ticket, Order, FileObject = (ns[key] for key in
                                     ('Base', 'AfterSalesTicket', 'Order', 'FileObject'))

    class Comment(Base):
        __tablename__ = 'after_sales_comments'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        ticket_id = mapped_column(ForeignKey('after_sales_tickets.id'), nullable=False, index=True)
        author_id = mapped_column(String(36), nullable=False)
        author_enterprise_id = mapped_column(String(36), nullable=False, default='')
        author_role = mapped_column(String(40), nullable=False)
        content = mapped_column(Text, nullable=False)
        attachments_json = mapped_column(Text, nullable=False, default='[]')
        operation_key = mapped_column(String(64), nullable=False, unique=True)
        payload_hash = mapped_column(String(64), nullable=False)
        created_at = mapped_column(DateTime(timezone=True), nullable=False, default=ns['now'])

    class Attachment(Base):
        __tablename__ = 'after_sales_attachments'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        ticket_id = mapped_column(ForeignKey('after_sales_tickets.id'), nullable=False, index=True)
        file_id = mapped_column(ForeignKey('file_objects.id'), nullable=False, unique=True)
        author_id = mapped_column(String(36), nullable=False)
        author_enterprise_id = mapped_column(String(36), nullable=False, default='')
        author_role = mapped_column(String(40), nullable=False)
        purpose = mapped_column(String(30), nullable=False)
        description = mapped_column(Text, nullable=False, default='')
        operation_key = mapped_column(String(64), nullable=False, unique=True)
        payload_hash = mapped_column(String(64), nullable=False)
        created_at = mapped_column(DateTime(timezone=True), nullable=False, default=ns['now'])

    class EventAttachment(Base):
        __tablename__ = 'after_sales_event_attachments'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        event_id = mapped_column(ForeignKey('after_sales_events.id'), nullable=False, index=True)
        attachment_id = mapped_column(ForeignKey('after_sales_attachments.id'), nullable=False)
        __table_args__ = (UniqueConstraint('event_id', 'attachment_id', name='uq_after_sales_event_attachment'),)

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def key(user, value):
        return hashlib.sha256((user.id + ':' + value).encode()).hexdigest()

    @contextmanager
    def guarded(db, user, target_id, operation):
        try:
            yield
        except HTTPException as exc:
            db.rollback()
            ns['audit'](db, user.email or user.id, 'after_sales_denied', 'after_sales_ticket',
                target_id[:36], 'Rejected after-sales request', category='security', business_domain='after_sales',
                result='denied', after={'operation': operation, 'status_code': exc.status_code})
            db.commit()
            raise
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, 'Evidence operation already processed; refresh and retry') from exc

    def access(db, user, ticket_id, request, *, lock=False):
        ticket = db.get(Ticket, ticket_id)
        order = db.get(Order, ticket.order_id) if ticket else None
        if not order:
            raise HTTPException(404, 'After-sales ticket not found')
        workflow['require_view'](db, user, order)
        workflow['require_scope'](request, db, user, order)
        if lock:
            order = db.scalar(select(Order).where(Order.id == order.id).with_for_update()
                              .execution_options(populate_existing=True))
            ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update()
                               .execution_options(populate_existing=True))
            if order is None or ticket is None:
                raise HTTPException(404, 'After-sales ticket not found')
            workflow['require_view'](db, user, order)
        return ticket, order

    def actor(db, user, order):
        return workflow['authorize'](db, user, order,
            'platform' if user.platform_role else 'party')

    def attachments(db, ticket, user, ids):
        if len(ids) != len(set(ids)):
            raise HTTPException(422, 'Duplicate attachment')
        for attachment_id in ids:
            row = db.get(Attachment, attachment_id)
            item = db.get(FileObject, row.file_id) if row else None
            if (not row or row.ticket_id != ticket.id or row.author_id != user.id or not item
                    or item.file_role != 'after_sales_attachment' or item.status != 'active'
                    or item.scan_status != 'clean' or ticket.status in ENDED and row.purpose != 'audit_comment'):
                raise HTTPException(404, 'Usable attachment not found')

    def attach_event(db, ticket, user, event, ids):
        attachments(db, ticket, user, ids)
        db.flush()
        for attachment_id in ids:
            db.add(EventAttachment(event_id=event.id, attachment_id=attachment_id))

    def attachment_out(db, row, names=None, files=None):
        item = files.get(row.file_id) if files is not None else db.get(FileObject, row.file_id)
        names = names if names is not None else workflow['display_names'](db, [row.author_id])
        return {'id': row.id, 'ticket_id': row.ticket_id, 'file_id': row.file_id,
            'name': item.original_name, 'content_type': item.content_type, 'size': item.size,
            'scan_status': item.scan_status, 'available': item.status == 'active' and item.scan_status == 'clean',
            'purpose': row.purpose, 'description': row.description, 'author_id': row.author_id,
            'author_name': names.get(row.author_id) or '',
            'author_enterprise_id': row.author_enterprise_id, 'author_role': row.author_role,
            'created_at': row.created_at}

    def event_attachments(db, event_ids):
        if not event_ids:
            return {}
        rows = db.execute(select(EventAttachment.event_id, Attachment).join(Attachment,
            EventAttachment.attachment_id == Attachment.id).where(EventAttachment.event_id.in_(event_ids))
            .order_by(Attachment.created_at, Attachment.id)).all()
        names = workflow['display_names'](db, [row.author_id for _, row in rows])
        files = {item.id: item for item in db.scalars(select(FileObject).where(
            FileObject.id.in_({row.file_id for _, row in rows})))} if rows else {}
        result = {}
        for event_id, row in rows:
            result.setdefault(event_id, []).append(attachment_out(db, row, names, files))
        return result

    def storage_client():
        return ns['Minio'](ns['MINIO_ENDPOINT'], access_key=ns['MINIO_ACCESS_KEY'],
            secret_key=ns['MINIO_SECRET_KEY'], secure=False,
            http_client=PoolManager(timeout=Timeout(connect=3, read=10, total=20), retries=False))

    def comment_out(db, row, names=None):
        names = names if names is not None else workflow['display_names'](db, [row.author_id])
        return {'id': row.id, 'ticket_id': row.ticket_id, 'content': row.content,
            'attachments': json.loads(row.attachments_json), 'author_id': row.author_id,
            'author_name': names.get(row.author_id) or '',
            'author_enterprise_id': row.author_enterprise_id, 'author_role': row.author_role,
            'created_at': row.created_at}

    def record(db, user, order, ticket, row, operation):
        ns['audit'](db, user.email or user.id, 'after_sales_' + operation, 'after_sales_ticket', ticket.id,
            'Append immutable ticket evidence', category='after_sales', business_domain='after_sales',
            order_id=order.id, after={'evidence_id': row.id, 'actor_id': user.id,
                                    'actor_role': row.author_role, 'actor_enterprise_id': row.author_enterprise_id})
        ns['notify_business_event'](db, '售后工单更新', ticket.ticket_no + '：新增沟通记录',
            'after_sales', ticket.id, event_key='after-sales-evidence:' + operation + ':' + row.operation_key,
            category='after_sales', enterprise_ids=[order.buyer_enterprise_id, order.provider_enterprise_id],
            platform_roles=['super_admin', 'platform_operator', 'delivery_monitor'])

    def replay(db, Model, user, operation_key, payload_hash, ticket):
        row = db.scalar(select(Model).where(Model.operation_key == operation_key))
        if row is not None and (row.ticket_id != ticket.id or row.payload_hash != payload_hash):
            raise HTTPException(409, 'Idempotency key already used for a different payload')
        return row

    def version(ticket, expected):
        if ticket.version != expected:
            raise HTTPException(409, 'Ticket changed; refresh and retry')

    def upload_purpose(ticket, purpose):
        if ticket.status in ENDED and purpose != 'audit_comment':
            raise HTTPException(409, 'Closed tickets only accept audit-comment attachments')

    app = ns['app']

    @app.get('/api/after-sales/{ticket_id}/comments')
    def list_comments(ticket_id: str, request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        with guarded(db, user, ticket_id, 'list_comments'):
            access(db, user, ticket_id, request)
            rows = list(db.scalars(select(Comment).where(
                Comment.ticket_id == ticket_id).order_by(Comment.created_at, Comment.id)))
            names = workflow['display_names'](db, [row.author_id for row in rows])
            return {'items': [comment_out(db, row, names) for row in rows]}

    @app.post('/api/after-sales/{ticket_id}/comments')
    def add_comment(ticket_id: str, body: CommentCreate, request: Request,
                    user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        with guarded(db, user, ticket_id, 'comment'):
            ticket, order = access(db, user, ticket_id, request, lock=True)
            role, tenant = actor(db, user, order)
            operation_key, payload_hash = key(user, body.idempotency_key), digest(body.model_dump())
            prior = replay(db, Comment, user, operation_key, payload_hash, ticket)
            if prior:
                return comment_out(db, prior)
            version(ticket, body.expected_version)
            if not body.content.strip():
                raise HTTPException(422, 'Comment cannot be blank')
            attachments(db, ticket, user, body.attachments)
            row = Comment(ticket_id=ticket.id, author_id=user.id, author_role=role, author_enterprise_id=tenant,
                content=body.content.strip(), attachments_json=json.dumps(body.attachments),
                operation_key=operation_key, payload_hash=payload_hash)
            db.add(row)
            db.flush()
            record(db, user, order, ticket, row, 'comment')
            db.commit()
            return comment_out(db, row)

    @app.get('/api/after-sales/{ticket_id}/attachments')
    def list_attachments(ticket_id: str, request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        with guarded(db, user, ticket_id, 'list_attachments'):
            access(db, user, ticket_id, request)
            rows = list(db.scalars(select(Attachment).where(
                Attachment.ticket_id == ticket_id).order_by(Attachment.created_at, Attachment.id)))
            names = workflow['display_names'](db, [row.author_id for row in rows])
            files = {item.id: item for item in db.scalars(select(FileObject).where(
                FileObject.id.in_({row.file_id for row in rows})))} if rows else {}
            return {'items': [attachment_out(db, row, names, files) for row in rows]}

    @app.post('/api/after-sales/{ticket_id}/attachments')
    def upload_attachment(ticket_id: str, request: Request, upload: UploadFile = File(...),
            expected_version: int = Form(ge=1), idempotency_key: str = Form(min_length=8, max_length=100),
            purpose: str = Form(default='evidence', pattern='^(evidence|audit_comment)$'),
            description: str = Form(default='', max_length=2000),
            user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        stored = None
        try:
            with guarded(db, user, ticket_id, 'upload_attachment'):
                ticket, order = access(db, user, ticket_id, request)
                role, tenant = actor(db, user, order)
                upload.file.seek(0, 2)
                size = upload.file.tell()
                upload.file.seek(0)
                if not 0 < size <= 20 * 1024 * 1024:
                    raise HTTPException(413, 'Attachment must be between 1 byte and 20MB')
                hasher = hashlib.sha256()
                while chunk := upload.file.read(1024 * 1024):
                    hasher.update(chunk)
                upload.file.seek(0)
                filename = (upload.filename or 'attachment').replace('\\', '/').rsplit('/', 1)[-1]
                if not filename or len(filename) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in filename):
                    raise HTTPException(422, 'Invalid attachment name')
                mime = upload.content_type or 'application/octet-stream'
                if len(mime) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in mime):
                    raise HTTPException(422, 'Invalid content type')
                operation_key = key(user, idempotency_key)
                payload_hash = digest([ticket_id, expected_version, filename, mime, size,
                                       hasher.hexdigest(), purpose, description])
                prior = replay(db, Attachment, user, operation_key, payload_hash, ticket)
                if prior:
                    return attachment_out(db, prior)
                version(ticket, expected_version)
                upload_purpose(ticket, purpose)
                # Scan/storage are outside business locks and DB transactions.
                db.rollback()
                scan, report = ns['clamav_scan_stream'](upload.file, size, timeout_seconds=15)
                if scan != 'clean':
                    raise HTTPException(400 if scan == 'infected' else 503, 'Attachment requires a clean virus scan')
                if not ns['MINIO_ENDPOINT']:
                    raise HTTPException(503, 'Object storage unavailable')
                client = storage_client()
                object_name = 'after-sales/' + ticket_id + '/' + secrets.token_hex(16)
                upload.file.seek(0)
                stored = client, object_name
                client.put_object(ns['MINIO_BUCKET'], object_name, upload.file, length=size, content_type=mime)
                ticket, order = access(db, user, ticket_id, request, lock=True)
                role, tenant = actor(db, user, order)
                prior = replay(db, Attachment, user, operation_key, payload_hash, ticket)
                if prior:
                    return attachment_out(db, prior)
                version(ticket, expected_version)
                upload_purpose(ticket, purpose)
                item = FileObject(owner_id=user.id, object_name=object_name, original_name=filename,
                    content_type=mime, size=size, checksum=hasher.hexdigest(), file_role='after_sales_attachment',
                    version='', status='active', scan_status='clean', scan_report=report, scanned_at=ns['now']())
                db.add(item)
                db.flush()
                row = Attachment(ticket_id=ticket.id, file_id=item.id, author_id=user.id,
                    author_role=role, author_enterprise_id=tenant, purpose=purpose, description=description,
                    operation_key=operation_key, payload_hash=payload_hash)
                db.add(row)
                db.flush()
                record(db, user, order, ticket, row, 'upload_attachment')
                db.commit()
                stored = None
                return attachment_out(db, row)
        finally:
            if stored is not None:
                db.rollback()
                try:
                    stored[0].remove_object(ns['MINIO_BUCKET'], stored[1])
                except Exception:
                    logging.getLogger(__name__).warning(
                        'After-sales evidence object cleanup requires retry')

    def download_file(db, user, file_id, request, ticket_id=None):
        row = db.scalar(select(Attachment).where(Attachment.file_id == file_id))
        if row is None or ticket_id is not None and row.ticket_id != ticket_id:
            raise HTTPException(404, 'Attachment not found')
        ticket, order = access(db, user, row.ticket_id, request)
        item = db.get(FileObject, row.file_id)
        if not item or item.status != 'active' or item.scan_status != 'clean':
            raise HTTPException(404, 'Usable attachment not found')
        if not ns['MINIO_ENDPOINT']:
            raise HTTPException(503, 'Object storage unavailable')
        client = storage_client()
        response = None
        try:
            response = client.get_object(ns['MINIO_BUCKET'], item.object_name)
            content = response.read(20 * 1024 * 1024 + 1)
            if len(content) > 20 * 1024 * 1024:
                raise HTTPException(413, 'Stored attachment exceeds limit')
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(404, 'Attachment content not found') from exc
        finally:
            if response is not None:
                response.close()
                response.release_conn()
        ns['audit'](db, user.email or user.id, 'after_sales_download_attachment', 'after_sales_ticket', ticket.id,
            'Download controlled evidence', category='after_sales', business_domain='after_sales',
            order_id=order.id, after={'attachment_id': row.id})
        db.commit()
        return Response(content=content, media_type='application/octet-stream', headers={
            'Content-Disposition': "attachment; filename=attachment; filename*=UTF-8''" + quote(item.original_name, safe=''),
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.get('/api/after-sales/{ticket_id}/attachments/{attachment_id}/download')
    def download(ticket_id: str, attachment_id: str, request: Request,
                 user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        with guarded(db, user, ticket_id, 'download_attachment'):
            row = db.get(Attachment, attachment_id)
            if row is None or row.ticket_id != ticket_id:
                raise HTTPException(404, 'Attachment not found')
            return download_file(db, user, row.file_id, request, ticket_id)

    return dict(Comment=Comment, Attachment=Attachment, EventAttachment=EventAttachment,
                validate_attachments=attachments, attach_event=attach_event, download_file=download_file,
                guarded=guarded, event_attachments=event_attachments)
