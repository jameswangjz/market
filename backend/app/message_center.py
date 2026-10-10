"""Persistent inbox and recipient lifecycle; email and SSE are later stages."""
import secrets
from datetime import timedelta

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column


class BatchAction(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    action: str


def install(ns):
    Base, app = ns["Base"], ns["app"]
    now, db_session, current_user = ns["now"], ns["db_session"], ns["current_user"]
    audit = ns["audit"]

    class Message(Base):
        __tablename__ = "notification_messages"
        id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        event_key: Mapped[str] = mapped_column(String(180), unique=True)
        title: Mapped[str] = mapped_column(String(220))
        body: Mapped[str] = mapped_column(Text, default="")
        category: Mapped[str] = mapped_column(String(40), default="review")
        severity: Mapped[str] = mapped_column(String(20), default="important")
        target_type: Mapped[str] = mapped_column(String(60), default="")
        target_id: Mapped[str] = mapped_column(String(36), default="")
        sender_id: Mapped[str] = mapped_column(String(36), default="system")
        tenant_id: Mapped[str] = mapped_column(String(36), default="")
        status: Mapped[str] = mapped_column(String(20), default="sent")
        sent_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
        expires_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=lambda: now() + timedelta(days=180))

    class Receipt(Base):
        __tablename__ = "notification_recipients"
        __table_args__ = (UniqueConstraint("message_id", "user_id"), Index("ix_notification_inbox", "user_id", "deleted_at", "created_at"))
        id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        message_id: Mapped[str] = mapped_column(ForeignKey("notification_messages.id"))
        user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
        legacy_id: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
        legacy_read: Mapped[str] = mapped_column(String(20), default="")
        delivered_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
        first_read_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
        last_read_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
        acknowledged_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
        archived_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
        deleted_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
        created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)

    def create(db, recipients, title, body, target_type="", target_id="", severity="important", event_key=None):
        key = event_key or secrets.token_hex(24)
        previous = db.scalar(select(Message).where(Message.event_key == key))
        if previous:
            return previous
        message = Message(event_key=key, title=title, body=body, target_type=target_type, target_id=target_id, severity=severity)
        db.add(message)
        db.flush()
        for uid in set(recipients):
            db.add(Receipt(message_id=message.id, user_id=uid, delivered_at=now()))
        return message

    def migrate(db):
        count = 0
        for old in db.scalars(select(ns["PlatformNotification"])).all():
            if db.scalar(select(Receipt.id).where(Receipt.legacy_id == old.id)):
                continue
            if not db.get(ns["User"], old.recipient_user_id):
                continue
            message = create(db, [], old.title, old.content, old.target_type, old.target_id, event_key="legacy:" + old.id)
            message.sent_at = old.created_at
            message.expires_at = old.created_at + timedelta(days=180)
            db.add(Receipt(message_id=message.id, user_id=old.recipient_user_id, legacy_id=old.id, legacy_read=old.status, created_at=old.created_at))
            count += 1
        db.flush()
        return count

    def base_query(uid):
        return select(Receipt, Message).join(Message, Message.id == Receipt.message_id).where(Receipt.user_id == uid, Message.status == "sent", Message.expires_at > now())

    def unread_condition():
        return (Receipt.first_read_at.is_(None)) & (Receipt.legacy_read != "read")

    def summary(db, uid):
        base = base_query(uid).where(Receipt.deleted_at.is_(None), Receipt.archived_at.is_(None))
        unread = db.scalar(select(func.count()).select_from(base.where(unread_condition()).subquery()))
        urgent = db.scalar(select(func.count()).select_from(base.where(Message.severity == "urgent", Receipt.acknowledged_at.is_(None)).subquery()))
        return {"unread": unread, "unacknowledged_urgent": urgent}

    def out(receipt, message):
        return {"id": receipt.id, "message_id": message.id, "title": message.title, "content": message.body,
                "category": message.category, "severity": message.severity, "target_type": message.target_type,
                "target_id": message.target_id, "status": "read" if receipt.first_read_at or receipt.legacy_read == "read" else "unread",
                "created_at": receipt.created_at, "sent_at": message.sent_at, "delivered_at": receipt.delivered_at,
                "first_read_at": receipt.first_read_at, "last_read_at": receipt.last_read_at,
                "acknowledged_at": receipt.acknowledged_at, "deleted_at": receipt.deleted_at, "archived_at": receipt.archived_at}

    def find(db, uid, rid):
        row = db.execute(base_query(uid).where((Receipt.id == rid) | (Receipt.legacy_id == rid))).first()
        if not row:
            raise HTTPException(404, "消息不存在或已过期")
        return row

    def apply_action(db, user, receipt, message, action):
        if action not in {"read", "acknowledge", "archive", "delete", "restore"}:
            raise HTTPException(400, "不支持的消息操作")
        if action in {"delete", "archive"} and message.severity == "urgent" and not receipt.acknowledged_at:
            raise HTTPException(409, "请先确认收到紧急消息")
        if receipt.deleted_at and action not in {"restore", "delete"}:
            raise HTTPException(409, "请先恢复已删除消息")
        before = out(receipt, message)
        timestamp = now()
        if action == "read":
            receipt.first_read_at = receipt.first_read_at or timestamp
            receipt.last_read_at = timestamp
        elif action == "acknowledge":
            if message.severity != "urgent":
                raise HTTPException(409, "仅紧急消息需要确认")
            receipt.acknowledged_at = receipt.acknowledged_at or timestamp
        elif action == "archive":
            receipt.archived_at = receipt.archived_at or timestamp
        elif action == "delete":
            receipt.deleted_at = receipt.deleted_at or timestamp
        else:
            receipt.deleted_at = None
            receipt.archived_at = None
        audit(db, user.email or user.phone or user.id, "notification_" + action, "notification_recipient", receipt.id,
              category="ops", business_domain="notification", before=before, after=out(receipt, message))

    @app.get("/api/notifications")
    def inbox(q: str = "", folder: str = "inbox", category: str = "", severity: str = "", read: str = "",
              page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
              user=Depends(current_user), db=Depends(db_session)):
        query = base_query(user.id)
        if folder == "trash":
            query = query.where(Receipt.deleted_at.is_not(None))
        elif folder == "archived":
            query = query.where(Receipt.deleted_at.is_(None), Receipt.archived_at.is_not(None))
        elif folder in {"inbox", "unread", "urgent"}:
            query = query.where(Receipt.deleted_at.is_(None), Receipt.archived_at.is_(None))
        else:
            raise HTTPException(400, "消息目录无效")
        if folder == "unread" or read == "unread":
            query = query.where(unread_condition())
        if read == "read":
            query = query.where(~unread_condition())
        if folder == "urgent":
            query = query.where(Message.severity == "urgent", Receipt.acknowledged_at.is_(None))
        if category:
            query = query.where(Message.category == category)
        if severity:
            query = query.where(Message.severity == severity)
        if q:
            pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            query = query.where(Message.title.ilike(pattern, escape="\\") | Message.body.ilike(pattern, escape="\\"))
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        rows = db.execute(query.order_by(Receipt.created_at.desc(), Receipt.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
        return {"items": [out(r, m) for r, m in rows], "total": total, "page": page, "page_size": page_size, **summary(db, user.id)}

    @app.get("/api/notifications/summary")
    def counts(user=Depends(current_user), db=Depends(db_session)):
        return summary(db, user.id)

    @app.post("/api/notifications/batch-actions")
    def batch(body: BatchAction, user=Depends(current_user), db=Depends(db_session)):
        rows = [find(db, user.id, rid) for rid in set(body.ids)]
        for receipt, message in rows:
            apply_action(db, user, receipt, message, body.action)
        db.commit()
        return {"processed": len(rows), **summary(db, user.id)}

    @app.get("/api/notifications/{rid}")
    def detail(rid: str, user=Depends(current_user), db=Depends(db_session)):
        return out(*find(db, user.id, rid))

    @app.delete("/api/notifications/{rid}")
    def delete(rid: str, user=Depends(current_user), db=Depends(db_session)):
        receipt, message = find(db, user.id, rid)
        apply_action(db, user, receipt, message, "delete")
        db.commit()
        return out(receipt, message)

    @app.post("/api/notifications/{rid}/{action}")
    def action(rid: str, action: str, user=Depends(current_user), db=Depends(db_session)):
        receipt, message = find(db, user.id, rid)
        apply_action(db, user, receipt, message, action)
        db.commit()
        return out(receipt, message)

    @app.on_event("startup")
    def migrate_history():
        with ns["SessionLocal"]() as db:
            migrate(db)
            db.commit()

    return {"create": create, "migrate": migrate, "Message": Message, "Receipt": Receipt}
