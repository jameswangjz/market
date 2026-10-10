"""Transactional inbox, isolated delivery jobs and authenticated realtime hints."""
import asyncio
import html
import json
import logging
import os
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, delete, func, select, text, update
from sqlalchemy.orm import Mapped, mapped_column


class BatchAction(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    action: str


class SendMessage(BaseModel):
    title: str = Field(min_length=1, max_length=220)
    content: str = Field(min_length=1, max_length=20000)
    severity: str = "normal"
    category: str = "announcement"
    tenant_id: str = ""
    recipient_ids: list[str] = Field(default_factory=list, max_length=500)
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)
    draft: bool = False
    event_key: str = Field(default="", max_length=140)


class PreferencesBody(BaseModel):
    email_enabled: bool = True


class SettingsBody(BaseModel):
    retention_days: int = Field(180, ge=1, le=3650)
    attachment_max_mb: int = Field(20, ge=1, le=100)
    email_enabled: bool = True


class ReadAllBody(BaseModel):
    cutoff: datetime


class RecallBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def install(ns):
    Base, app = ns["Base"], ns["app"]
    now, db_session, current_user = ns["now"], ns["db_session"], ns["current_user"]
    audit = ns["audit"]

    class Message(Base):
        __tablename__ = "notification_messages"
        __table_args__ = (Index("ix_notification_status_sent", "status", "sent_at"), Index("ix_notification_tenant_sent", "tenant_id", "sent_at"))
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

    class Delivery(Base):
        __tablename__ = "notification_deliveries"
        __table_args__ = (UniqueConstraint("message_id", "user_id", "channel"), Index("ix_notification_delivery_ready", "status", "next_attempt_at"))
        id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        message_id: Mapped[str] = mapped_column(ForeignKey("notification_messages.id"))
        user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
        channel: Mapped[str] = mapped_column(String(20))
        status: Mapped[str] = mapped_column(String(20), default="pending")
        attempts: Mapped[int] = mapped_column(Integer, default=0)
        next_attempt_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
        lease_until: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
        lease_token: Mapped[str] = mapped_column(String(64), default="")
        result: Mapped[str] = mapped_column(String(240), default="")
        completed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)

    class Preference(Base):
        __tablename__ = "notification_preferences"
        user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
        email_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    class Attachment(Base):
        __tablename__ = "notification_attachments"
        id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
        name: Mapped[str] = mapped_column(String(240))
        object_name: Mapped[str] = mapped_column(String(300), unique=True)
        size: Mapped[int] = mapped_column(Integer)
        content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
        scan_result: Mapped[str] = mapped_column(String(20), default="clean")
        created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)

    class MessageAttachment(Base):
        __tablename__ = "notification_message_attachments"
        message_id: Mapped[str] = mapped_column(ForeignKey("notification_messages.id"), primary_key=True)
        attachment_id: Mapped[str] = mapped_column(ForeignKey("notification_attachments.id"), primary_key=True)

    class DeliveryAttempt(Base):
        __tablename__ = "notification_delivery_attempts"
        __table_args__ = (UniqueConstraint("delivery_id", "lease_token"),)
        id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        delivery_id: Mapped[str] = mapped_column(ForeignKey("notification_deliveries.id"), index=True)
        lease_token: Mapped[str] = mapped_column(String(64))
        attempt_no: Mapped[int] = mapped_column(Integer)
        started_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=now)
        finished_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
        result: Mapped[str] = mapped_column(String(240), default="processing")

    def settings(db):
        result = SettingsBody().model_dump()
        if "SystemSetting" in ns:
            for row in db.scalars(select(ns["SystemSetting"]).where(ns["SystemSetting"].setting_key.like("notification_%"))):
                key = row.setting_key.removeprefix("notification_")
                if key in result:
                    result[key] = json.loads(row.setting_value)
        return result

    def queue(db, message, uid, channel):
        if not db.scalar(select(Delivery.id).where(Delivery.message_id == message.id, Delivery.user_id == uid, Delivery.channel == channel)):
            db.add(Delivery(message_id=message.id, user_id=uid, channel=channel))

    def enqueue(db, message):
        for receipt in db.scalars(select(Receipt).where(Receipt.message_id == message.id)):
            queue(db, message, receipt.user_id, "realtime")
            queue(db, message, receipt.user_id, "email")

    def create(db, recipients, title, body, target_type="", target_id="", severity="important", event_key=None,
               category="review", sender_id="system", tenant_id="", status="sent", delivery=True):
        if db.bind.dialect.name == "sqlite":
            connection = db.connection()
            if not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
        key = event_key or secrets.token_hex(24)
        previous = db.scalar(select(Message).where(Message.event_key == key))
        if previous:
            return previous
        # The unique event key resolves racing producers without aborting their
        # outer business transaction. SQLite also exercises this constraint.
        from sqlalchemy.exc import IntegrityError
        try:
            with db.begin_nested():
                message = Message(event_key=key, title=title, body=body, target_type=target_type, target_id=target_id,
                                  severity=severity, category=category, sender_id=sender_id, tenant_id=tenant_id,
                                  status=status, expires_at=now() + timedelta(days=settings(db)["retention_days"]))
                db.add(message)
                db.flush()
        except IntegrityError:
            previous = db.scalar(select(Message).where(Message.event_key == key))
            if previous:
                return previous
            raise
        for uid in set(recipients):
            db.add(Receipt(message_id=message.id, user_id=uid, delivered_at=now() if status == "sent" else None))
        db.flush()
        if status == "sent" and delivery:
            enqueue(db, message)
        message._notification_created = True
        return message

    def migrate(db):
        count = 0
        for old in db.scalars(select(ns["PlatformNotification"])).all():
            if db.scalar(select(Receipt.id).where(Receipt.legacy_id == old.id)):
                continue
            if not db.get(ns["User"], old.recipient_user_id):
                continue
            if (old.created_at + timedelta(days=180)).replace(tzinfo=timezone.utc) <= now():
                continue
            message = create(db, [], old.title, old.content, old.target_type, old.target_id, event_key="legacy:" + old.id, delivery=False)
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
        recent = db.execute(base.order_by(Receipt.created_at.desc(), Receipt.id.desc()).limit(5)).all()
        return {"unread": unread, "unacknowledged_urgent": urgent, "recent": [out(r, m) for r, m in recent], "server_time": now()}

    def out(receipt, message):
        return {"id": receipt.id, "message_id": message.id, "title": message.title, "content": message.body,
                "category": message.category, "severity": message.severity, "target_type": message.target_type,
                "target_id": message.target_id, "status": "read" if receipt.first_read_at or receipt.legacy_read == "read" else "unread",
                "created_at": receipt.created_at, "sent_at": message.sent_at, "delivered_at": receipt.delivered_at,
                "first_read_at": receipt.first_read_at, "last_read_at": receipt.last_read_at,
                "acknowledged_at": receipt.acknowledged_at, "deleted_at": receipt.deleted_at, "archived_at": receipt.archived_at}

    def find(db, uid, rid):
        row = db.execute(base_query(uid).where((Receipt.id == rid) | (Receipt.legacy_id == rid)).with_for_update(of=Receipt)).first()
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
        # Coalesce state-change hints. PG remains authoritative if Redis drops
        # a publication; clients always re-query instead of applying deltas.
        wake = db.scalar(select(Delivery).where(Delivery.message_id == message.id, Delivery.user_id == user.id, Delivery.channel == "realtime"))
        if wake:
            wake.status = "pending"
            wake.next_attempt_at = now()
            wake.lease_token = secrets.token_hex(16)
        else:
            queue(db, message, user.id, "realtime")

    @app.get("/api/notifications")
    def inbox(q: str = "", folder: str = "inbox", category: str = "", severity: str = "", read: str = "",
              start: str = "", end: str = "",
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
        for raw, lower in ((start, True), (end, False)):
            if raw:
                try:
                    timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    raise HTTPException(422, "时间格式无效")
                query = query.where(Receipt.created_at >= timestamp if lower else Receipt.created_at < timestamp)
        if q:
            pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            query = query.where(Message.title.ilike(pattern, escape="\\") | Message.body.ilike(pattern, escape="\\"))
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        rows = db.execute(query.order_by(Receipt.created_at.desc(), Receipt.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
        return {"items": [out(r, m) for r, m in rows], "total": total, "page": page, "page_size": page_size, **summary(db, user.id)}

    @app.get("/api/notifications/summary")
    def counts(user=Depends(current_user), db=Depends(db_session)):
        return summary(db, user.id)

    @app.post("/api/notifications/read-all")
    def read_all(body: ReadAllBody, user=Depends(current_user), db=Depends(db_session)):
        if body.cutoff.tzinfo is None or body.cutoff > now():
            raise HTTPException(422, "截止时间必须包含时区且不能晚于当前时间")
        rows = db.execute(base_query(user.id).where(Receipt.deleted_at.is_(None), Receipt.archived_at.is_(None),
                                                  Receipt.created_at <= body.cutoff, unread_condition()).with_for_update(of=Receipt)).all()
        for receipt, message in rows:
            apply_action(db, user, receipt, message, "read")
        db.commit()
        return {"processed": len(rows), **summary(db, user.id)}

    def platform_admin(user):
        return getattr(user, "platform_role", "") in {"super_admin", "platform_operator"}

    def can_send(db, user, tenant_id=""):
        if platform_admin(user):
            return
        if not tenant_id or "Membership" not in ns:
            raise HTTPException(403, "没有消息发送权限")
        membership = db.scalar(select(ns["Membership"]).where(ns["Membership"].user_id == user.id,
                              ns["Membership"].enterprise_id == tenant_id, ns["Membership"].status == "active",
                              ns["Membership"].role.in_(["super_admin", "enterprise_admin"])))
        if not membership:
            raise HTTPException(403, "只能向本企业发送消息")

    def validate_recipients(db, user, tenant_id, ids):
        can_send(db, user, tenant_id)
        users = db.scalars(select(ns["User"]).where(ns["User"].id.in_(set(ids)))).all()
        if len(users) != len(set(ids)) or any(not getattr(u, "is_active", True) or getattr(u, "activation_status", "active") == "deleted" for u in users):
            raise HTTPException(400, "收件人不存在或不可用")
        if tenant_id:
            if "Membership" not in ns:
                raise HTTPException(403, "企业范围不可用")
            members = set(db.scalars(select(ns["Membership"].user_id).where(ns["Membership"].enterprise_id == tenant_id,
                                ns["Membership"].status == "active")).all())
            if set(ids) - members or any(getattr(u, "platform_role", "") for u in users):
                raise HTTPException(403, "收件人超出本企业范围")
        return users

    def sender_out(db, message):
        return {"id": message.id, "title": message.title, "content": message.body, "status": message.status,
                "severity": message.severity, "category": message.category, "tenant_id": message.tenant_id,
                "sent_at": message.sent_at, "recipients": db.scalar(select(func.count()).select_from(Receipt).where(Receipt.message_id == message.id)),
                "recipient_ids": list(db.scalars(select(Receipt.user_id).where(Receipt.message_id == message.id))),
                "attachments": attachment_list(db, message.id),
                "delivery": [{"channel": channel, "status": state, "count": count} for channel, state, count in
                             db.execute(select(Delivery.channel, Delivery.status, func.count()).where(Delivery.message_id == message.id).group_by(Delivery.channel, Delivery.status))]}

    def attachment_list(db, mid):
        return [{"id": a.id, "name": a.name, "size": a.size} for a in db.scalars(select(Attachment).join(MessageAttachment,
                                        MessageAttachment.attachment_id == Attachment.id).where(MessageAttachment.message_id == mid))]

    @app.get("/api/notifications/sender-context")
    def sender_context(user=Depends(current_user), db=Depends(db_session)):
        enterprises = []
        user_query = select(ns["User"])
        if "Membership" in ns:
            ids = list(db.scalars(select(ns["Membership"].enterprise_id).where(ns["Membership"].user_id == user.id,
                        ns["Membership"].status == "active", ns["Membership"].role.in_(["super_admin", "enterprise_admin"]))))
            if "Enterprise" in ns:
                enterprises = [{"id": e.id, "name": e.name} for e in db.scalars(select(ns["Enterprise"]).where(ns["Enterprise"].id.in_(ids)))]
            if not platform_admin(user):
                user_query = user_query.where(ns["User"].id.in_(select(ns["Membership"].user_id).where(ns["Membership"].enterprise_id.in_(ids), ns["Membership"].status == "active")))
        elif not platform_admin(user):
            user_query = user_query.where(ns["User"].id == user.id)
        users = [{"id": u.id, "name": getattr(u, "name", u.id), "email": getattr(u, "email", ""),
                  "enterprise_ids": list(db.scalars(select(ns["Membership"].enterprise_id).where(ns["Membership"].user_id == u.id, ns["Membership"].status == "active"))) if "Membership" in ns else []}
                 for u in db.scalars(user_query).all() if getattr(u, "is_active", True)]
        return {"platform_admin": platform_admin(user), "enterprises": enterprises, "users": users, "settings": settings(db)}

    @app.post("/api/notifications/send")
    def send(body: SendMessage, user=Depends(current_user), db=Depends(db_session)):
        if not body.title.strip() or not body.content.strip() or body.severity not in {"normal", "important", "urgent"}:
            raise HTTPException(422, "消息内容或等级无效")
        if body.category not in {"announcement", "review", "order", "delivery", "settlement", "security", "sla", "identity", "system"}:
            raise HTTPException(422, "消息分类无效")
        if not body.draft and not body.recipient_ids:
            raise HTTPException(422, "请选择收件人")
        validate_recipients(db, user, body.tenant_id, body.recipient_ids)
        attachments = [db.get(Attachment, aid) for aid in set(body.attachment_ids)]
        if any(not a or a.owner_id != user.id or a.scan_result != "clean" for a in attachments):
            raise HTTPException(403, "附件不可用或无权使用")
        message = create(db, body.recipient_ids, body.title.strip(), body.content.strip(), severity=body.severity,
                         event_key=f"manual:{user.id}:{body.event_key}" if body.event_key else None,
                         sender_id=user.id, category=body.category, tenant_id=body.tenant_id, status="draft" if body.draft else "sent")
        if not getattr(message, "_notification_created", False):
            old_recipients = set(db.scalars(select(Receipt.user_id).where(Receipt.message_id == message.id)))
            old_attachments = set(db.scalars(select(MessageAttachment.attachment_id).where(MessageAttachment.message_id == message.id)))
            if (message.title != body.title.strip() or message.body != body.content.strip() or message.severity != body.severity or
                    message.category != body.category or message.tenant_id != body.tenant_id or
                    message.status != ("draft" if body.draft else "sent") or old_recipients != set(body.recipient_ids) or old_attachments != set(body.attachment_ids)):
                raise HTTPException(409, "相同幂等键不能提交不同消息")
            return sender_out(db, message)
        for attachment in attachments:
            if not db.get(MessageAttachment, (message.id, attachment.id)):
                db.add(MessageAttachment(message_id=message.id, attachment_id=attachment.id))
        audit(db, user.email or user.id, "notification_draft" if body.draft else "notification_send", "notification", message.id,
              category="ops", tenant_id=body.tenant_id, after={"status": message.status, "recipient_count": len(set(body.recipient_ids))})
        db.commit()
        return sender_out(db, message)

    @app.get("/api/notifications/sent")
    def sent(folder: str = "sent", q: str = "", category: str = "", severity: str = "", start: str = "", end: str = "",
             page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user=Depends(current_user), db=Depends(db_session)):
        query = select(Message).where(Message.sender_id == user.id)
        if folder == "drafts":
            query = query.where(Message.status == "draft")
        elif folder == "sent":
            query = query.where(Message.status != "draft")
        elif folder != "all":
            raise HTTPException(422, "发件目录无效")
        if q:
            pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            query = query.where(Message.title.ilike(pattern, escape="\\") | Message.body.ilike(pattern, escape="\\"))
        if category:
            query = query.where(Message.category == category)
        if severity:
            query = query.where(Message.severity == severity)
        for raw, lower in ((start, True), (end, False)):
            if raw:
                try:
                    timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    raise HTTPException(422, "时间格式无效")
                query = query.where(Message.sent_at >= timestamp if lower else Message.sent_at < timestamp)
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        return {"items": [sender_out(db, m) for m in db.scalars(query.order_by(Message.sent_at.desc()).offset((page - 1) * page_size).limit(page_size))], "total": total}

    @app.put("/api/notifications/sent/{mid}")
    def update_draft(mid: str, body: SendMessage, user=Depends(current_user), db=Depends(db_session)):
        message = db.scalar(select(Message).where(Message.id == mid, Message.sender_id == user.id).with_for_update())
        if not message:
            raise HTTPException(404, "草稿不存在")
        if message.status != "draft":
            raise HTTPException(409, "只有草稿可以编辑")
        if not body.title.strip() or not body.content.strip() or body.severity not in {"normal", "important", "urgent"}:
            raise HTTPException(422, "内容或等级无效")
        validate_recipients(db, user, body.tenant_id, body.recipient_ids)
        attachments = [db.get(Attachment, aid) for aid in set(body.attachment_ids)]
        if any(not a or a.owner_id != user.id or a.scan_result != "clean" for a in attachments):
            raise HTTPException(403, "附件不可用")
        before = {"title": message.title, "status": message.status}
        db.execute(delete(Receipt).where(Receipt.message_id == mid))
        db.execute(delete(MessageAttachment).where(MessageAttachment.message_id == mid))
        message.title, message.body = body.title.strip(), body.content.strip()
        message.category, message.severity, message.tenant_id = body.category, body.severity, body.tenant_id
        for uid in set(body.recipient_ids):
            db.add(Receipt(message_id=mid, user_id=uid))
        for attachment in attachments:
            db.add(MessageAttachment(message_id=mid, attachment_id=attachment.id))
        audit(db, user.email or user.id, "notification_update_draft", "notification", mid, category="ops", before=before, after={"title": message.title, "status": "draft"})
        db.commit()
        return sender_out(db, message)

    @app.post("/api/notifications/sent/{mid}/{operation}")
    def sender_action(mid: str, operation: str, body: RecallBody | None = None, user=Depends(current_user), db=Depends(db_session)):
        message = db.scalar(select(Message).where(Message.id == mid, Message.sender_id == user.id).with_for_update())
        if not message:
            raise HTTPException(404, "发件不存在")
        can_send(db, user, message.tenant_id)
        before = message.status
        if operation == "publish":
            if message.status != "draft":
                raise HTTPException(409, "仅草稿可以发送")
            ids = list(db.scalars(select(Receipt.user_id).where(Receipt.message_id == mid)))
            if not ids:
                raise HTTPException(422, "草稿没有收件人")
            validate_recipients(db, user, message.tenant_id, ids)
            message.status = "sent"
            message.sent_at = now()
            message.expires_at = now() + timedelta(days=settings(db)["retention_days"])
            for receipt in db.scalars(select(Receipt).where(Receipt.message_id == mid)):
                receipt.delivered_at = now()
            enqueue(db, message)
        elif operation == "recall":
            if not body or not body.reason.strip():
                raise HTTPException(422, "撤回原因必填")
            if message.status != "sent":
                raise HTTPException(409, "仅已发送消息可以撤回")
            message.status = "recalled"
            for job in db.scalars(select(Delivery).where(Delivery.message_id == mid, Delivery.status.in_(["pending", "processing"]))):
                job.status = "cancelled"
                job.result = "message_recalled"
                job.lease_token = ""
        else:
            raise HTTPException(400, "无效发件操作")
        audit(db, user.email or user.id, "notification_" + operation, "notification", mid, before={"status": before},
              after={"status": message.status, "reason": body.reason.strip() if body else "", "email_recall": "SMTP已接受的邮件无法撤回"}, category="ops")
        db.commit()
        return sender_out(db, message)

    @app.get("/api/notifications/preferences")
    def get_preferences(user=Depends(current_user), db=Depends(db_session)):
        row = db.get(Preference, user.id)
        return {"email_enabled": row.email_enabled if row else True}

    @app.put("/api/notifications/preferences")
    def put_preferences(body: PreferencesBody, user=Depends(current_user), db=Depends(db_session)):
        row = db.get(Preference, user.id)
        before = row.email_enabled if row else True
        if not row:
            row = Preference(user_id=user.id)
            db.add(row)
        row.email_enabled = body.email_enabled
        audit(db, user.email or user.id, "notification_preferences", "user", user.id, category="ops",
              before={"email_enabled": before}, after=body.model_dump())
        db.commit()
        return body

    @app.get("/api/notifications/settings")
    def get_settings(user=Depends(current_user), db=Depends(db_session)):
        if not platform_admin(user):
            raise HTTPException(403, "只有平台管理员可以配置消息")
        return settings(db)

    @app.put("/api/notifications/settings")
    def put_settings(body: SettingsBody, user=Depends(current_user), db=Depends(db_session)):
        if not platform_admin(user) or "SystemSetting" not in ns:
            raise HTTPException(403, "只有平台管理员可以配置消息")
        before = settings(db)
        for key, value in body.model_dump().items():
            row = db.scalar(select(ns["SystemSetting"]).where(ns["SystemSetting"].setting_key == "notification_" + key))
            if not row:
                row = ns["SystemSetting"](setting_key="notification_" + key)
                db.add(row)
            row.setting_value = json.dumps(value)
            row.updated_by = user.email or user.id
        audit(db, user.email or user.id, "notification_settings", "system_setting", category="ops", before=before, after=body.model_dump())
        db.commit()
        return body

    def object_store():
        if not ns.get("MINIO_ENDPOINT"):
            raise HTTPException(503, "附件存储未配置")
        return ns["Minio"](ns["MINIO_ENDPOINT"], access_key=ns["MINIO_ACCESS_KEY"], secret_key=ns["MINIO_SECRET_KEY"], secure=False)

    @app.post("/api/notifications/attachments")
    def upload_attachment(upload: UploadFile = File(...), user=Depends(current_user), db=Depends(db_session)):
        if not platform_admin(user):
            if "Membership" not in ns or not db.scalar(select(ns["Membership"].id).where(ns["Membership"].user_id == user.id,
                   ns["Membership"].status == "active", ns["Membership"].role.in_(["super_admin", "enterprise_admin"]))):
                raise HTTPException(403, "没有附件上传权限")
        filename = Path((upload.filename or "attachment").replace("\\", "/")).name
        if len(filename) > 240 or Path(filename).suffix.lower() not in {".pdf", ".txt", ".csv", ".json", ".docx", ".xlsx", ".png", ".jpg", ".jpeg", ".zip"}:
            raise HTTPException(422, "附件类型不允许")
        upload.file.seek(0, 2)
        size = upload.file.tell()
        upload.file.seek(0)
        if not size or size > settings(db)["attachment_max_mb"] * 1024 * 1024:
            raise HTTPException(413, "附件为空或超过大小限制")
        if "clamav_scan_stream" not in ns:
            raise HTTPException(503, "病毒扫描服务未配置")
        state, _ = ns["clamav_scan_stream"](upload.file, size)
        if state != "clean":
            raise HTTPException(422 if state == "infected" else 503, "附件病毒扫描未通过")
        upload.file.seek(0)
        key = "notifications/" + user.id + "/" + secrets.token_hex(16)
        client = object_store()
        bucket = ns["MINIO_BUCKET"]
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        client.put_object(bucket, key, upload.file, length=size, content_type="application/octet-stream")
        row = Attachment(owner_id=user.id, name=filename, object_name=key, size=size, content_type="application/octet-stream")
        try:
            db.add(row)
            db.flush()
            audit(db, user.email or user.id, "notification_attachment_upload", "notification_attachment", row.id, category="security", after={"name": filename, "size": size, "scan_result": state})
            db.commit()
        except Exception:
            client.remove_object(bucket, key)
            raise
        return {"id": row.id, "name": row.name, "size": row.size}

    @app.get("/api/notifications/attachments/{aid}/download")
    def download_attachment(aid: str, user=Depends(current_user), db=Depends(db_session)):
        attachment = db.get(Attachment, aid)
        if not attachment:
            raise HTTPException(404, "附件不存在")
        authorized = attachment.owner_id == user.id
        if not authorized:
            authorized = db.scalar(select(Receipt.id).join(Message, Message.id == Receipt.message_id).join(MessageAttachment,
                    MessageAttachment.message_id == Message.id).where(MessageAttachment.attachment_id == aid,
                    Receipt.user_id == user.id, Receipt.deleted_at.is_(None), Message.status == "sent", Message.expires_at > now()))
        if not authorized:
            raise HTTPException(404, "附件不存在或没有访问权限")
        try:
            response = object_store().get_object(ns["MINIO_BUCKET"], attachment.object_name)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "附件存储暂不可用")
        audit(db, user.email or user.id, "notification_attachment_download", "notification_attachment", aid, category="security")
        db.commit()
        def chunks():
            try:
                yield from response.stream(1024 * 1024)
            finally:
                response.close()
                response.release_conn()
        return StreamingResponse(chunks(), media_type="application/octet-stream", headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(attachment.name), "X-Content-Type-Options": "nosniff"})

    def redis_client():
        import redis
        return redis.Redis.from_url(os.getenv("REDIS_URL", "redis://market-redis:6379/0"), socket_connect_timeout=2, socket_timeout=3, decode_responses=True)

    @app.get("/api/notifications/stream")
    async def stream(request: Request, user=Depends(current_user), db=Depends(db_session)):
        uid = user.id
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        db.rollback()
        async def events():
            client = None
            subscription = None
            previous = None
            last_check = 0
            try:
                try:
                    client = redis_client()
                    subscription = client.pubsub()
                    await asyncio.to_thread(subscription.subscribe, "market:notifications:" + uid)
                except Exception:
                    if subscription:
                        subscription.close()
                    subscription = None
                yield "event: connected\ndata: {}\n\n"
                while not await request.is_disconnected():
                    hint = None
                    if subscription:
                        try:
                            hint = await asyncio.to_thread(subscription.get_message, ignore_subscribe_messages=True, timeout=2)
                        except Exception:
                            subscription.close()
                            subscription = None
                    else:
                        await asyncio.sleep(2)
                    tick = asyncio.get_running_loop().time()
                    if hint or tick - last_check >= 15:
                        def snapshot():
                            if "jwt" in ns:
                                try:
                                    ns["jwt"].decode(token, ns["JWT_SECRET"], algorithms=["HS256"])
                                except Exception:
                                    return None
                            with ns["SessionLocal"]() as session:
                                account = session.get(ns["User"], uid)
                                if not account or not getattr(account, "is_active", True) or getattr(account, "activation_status", "") == "deleted":
                                    return None
                                return summary(session, uid)
                        state = await asyncio.to_thread(snapshot)
                        if state is None:
                            yield "event: auth_expired\ndata: {}\n\n"
                            break
                        if hint or state != previous:
                            yield "event: notification_changed\ndata: " + json.dumps(state, default=str) + "\n\n"
                            previous = state
                        else:
                            yield ": heartbeat\n\n"
                        last_check = tick
            finally:
                if subscription:
                    subscription.close()
                if client:
                    client.close()
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def send_email(db, account, message):
        config = settings(db)
        preference = db.get(Preference, account.id)
        if not config["email_enabled"] or (preference and not preference.email_enabled):
            return "skipped", "email_disabled"
        address = getattr(account, "email", "") or ""
        if not address or not getattr(account, "email_verified", False) or address.endswith("@market.local"):
            return "skipped", "email_missing_or_unverified"
        values = {r.setting_key: r.setting_value for r in db.scalars(select(ns["SystemSetting"]))} if "SystemSetting" in ns else {}
        test_host = os.getenv("NOTIFICATION_SMTP_HOST", "")
        host = test_host or values.get("smtp_host", "")
        if not host:
            return "skipped", "smtp_not_configured"
        username = values.get("smtp_username", "")
        password = values.get("smtp_password", "")
        mail = EmailMessage()
        mail["Subject"] = message.title
        mail["From"] = os.getenv("NOTIFICATION_SMTP_FROM", "notifications@market.local") if test_host else username
        mail["To"] = address
        mail["Message-ID"] = f"<market-{message.id}-{account.id}@market.local>"
        mail.set_content(message.body + "\n\n请登录平台消息中心查看详情。")
        mail.add_alternative("<html><body><h2>" + html.escape(message.title) + "</h2><p style='white-space:pre-wrap'>" +
                             html.escape(message.body) + "</p><p>请登录平台消息中心查看详情。</p></body></html>", subtype="html")
        use_ssl = not test_host and values.get("smtp_ssl", "false") == "true"
        port = int(os.getenv("NOTIFICATION_SMTP_PORT", "1025")) if test_host else int(values.get("smtp_port", "587"))
        transport = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        kwargs = {"context": ssl.create_default_context()} if use_ssl else {}
        with transport(host, port, timeout=15, **kwargs) as server:
            if not test_host and not use_ssl and values.get("smtp_starttls", "true") == "true":
                server.starttls(context=ssl.create_default_context())
            if not test_host and username:
                server.login(username, password)
            refused = server.send_message(mail)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        return "accepted", "smtp_accepted_not_read_receipt"

    def process_once():
        # Claim and commit a short lease before external I/O. A crashed worker
        # becomes reclaimable, and fencing tokens prevent stale completion.
        with ns["SessionLocal"]() as db:
            job = db.scalar(select(Delivery).where(((Delivery.status == "pending") & (Delivery.next_attempt_at <= now())) |
                   ((Delivery.status == "processing") & (Delivery.lease_until <= now()))).order_by(Delivery.next_attempt_at).with_for_update(skip_locked=True).limit(1))
            if not job:
                return False
            message = db.get(Message, job.message_id)
            if not message or message.status != "sent" or message.expires_at.replace(tzinfo=timezone.utc) <= now():
                job.status = "cancelled"
                job.result = "message_not_active"
                db.commit()
                return True
            for unfinished in db.scalars(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == job.id, DeliveryAttempt.finished_at.is_(None))):
                unfinished.finished_at, unfinished.result = now(), "lease_expired_delivery_unknown"
            job.status = "processing"
            job.attempts += 1
            job.lease_until = now() + timedelta(seconds=90)
            job.lease_token = secrets.token_hex(16)
            jid, fence, channel, uid = job.id, job.lease_token, job.channel, job.user_id
            db.add(DeliveryAttempt(delivery_id=jid, lease_token=fence, attempt_no=job.attempts))
            db.commit()
        try:
            with ns["SessionLocal"]() as db:
                job = db.get(Delivery, jid)
                message = db.get(Message, job.message_id)
                account = db.get(ns["User"], uid)
                if message.status != "sent" or not account or not getattr(account, "is_active", True):
                    state, result = "cancelled", "recipient_or_message_inactive"
                elif channel == "email":
                    state, result = send_email(db, account, message)
                else:
                    client = redis_client()
                    try:
                        client.publish("market:notifications:" + uid, json.dumps({"message_id": message.id}))
                    finally:
                        client.close()
                    state, result = "accepted", "redis_hint_published"
        except Exception as exc:
            state = "error"
            # Never persist raw SMTP exceptions which may include credentials
            # or personal addresses. The type is sufficient for operator triage.
            result = type(exc).__name__
        with ns["SessionLocal"]() as db:
            job = db.scalar(select(Delivery).where(Delivery.id == jid, Delivery.lease_token == fence).with_for_update())
            if not job or job.status != "processing":
                return True
            job.result = result
            attempt = db.scalar(select(DeliveryAttempt).where(DeliveryAttempt.delivery_id == jid, DeliveryAttempt.lease_token == fence))
            if attempt:
                attempt.finished_at, attempt.result = now(), state + ":" + result
            if state == "error":
                job.status = "pending" if job.attempts < 4 else "failed"
                job.next_attempt_at = now() + timedelta(seconds=10)
            else:
                job.status = state
                job.completed_at = now()
            job.lease_until = None
            db.commit()
        return True

    @app.get("/api/notifications/delivery-health")
    def delivery_health(user=Depends(current_user), db=Depends(db_session)):
        if not platform_admin(user):
            raise HTTPException(403, "只有平台管理员可以查看发送任务")
        return {"counts": [{"channel": c, "status": s, "count": n} for c, s, n in db.execute(
                select(Delivery.channel, Delivery.status, func.count()).group_by(Delivery.channel, Delivery.status))],
                "failures": [{"id": j.id, "channel": j.channel, "attempts": j.attempts, "result": j.result} for j in db.scalars(
                    select(Delivery).where(Delivery.status == "failed").limit(100))]}

    @app.get("/metrics/notifications", include_in_schema=False)
    def metrics(db=Depends(db_session)):
        lines = ["# TYPE market_notification_jobs gauge"]
        for channel, state, count in db.execute(select(Delivery.channel, Delivery.status, func.count()).group_by(Delivery.channel, Delivery.status)):
            lines.append(f'market_notification_jobs{{channel="{channel}",status="{state}"}} {count}')
        oldest = db.scalar(select(func.min(Delivery.next_attempt_at)).where(Delivery.status == "pending"))
        delay = max(0, (now() - oldest.replace(tzinfo=timezone.utc)).total_seconds()) if oldest else 0
        lines.append(f"market_notification_oldest_pending_seconds {delay}")
        urgent = db.scalar(select(func.count()).select_from(Receipt).join(Message).where(Message.severity == "urgent",
                             Message.expires_at <= now(), Receipt.acknowledged_at.is_(None)))
        lines.append(f"market_notification_expired_unacknowledged_urgent {urgent}")
        return PlainTextResponse("\n".join(lines) + "\n")

    @app.post("/api/notifications/deliveries/{jid}/retry")
    def retry_delivery(jid: str, user=Depends(current_user), db=Depends(db_session)):
        if not platform_admin(user):
            raise HTTPException(403, "只有平台管理员可以恢复失败任务")
        job = db.scalar(select(Delivery).where(Delivery.id == jid).with_for_update())
        if not job or job.status != "failed":
            raise HTTPException(409, "仅失败任务可以恢复")
        message = db.get(Message, job.message_id)
        if message.status != "sent" or message.expires_at.replace(tzinfo=timezone.utc) <= now():
            raise HTTPException(409, "消息已撤回或过期")
        job.status = "pending"
        job.attempts = 0
        job.next_attempt_at = now()
        audit(db, user.email or user.id, "notification_retry", "notification_delivery", jid, before={"status": "failed"}, after={"status": "pending"})
        db.commit()
        return {"status": job.status}

    def cleanup():
        with ns["SessionLocal"]() as db:
            ids = list(db.scalars(select(Message.id).where(Message.expires_at <= now()).limit(500)))
            if ids:
                db.execute(delete(DeliveryAttempt).where(DeliveryAttempt.delivery_id.in_(select(Delivery.id).where(Delivery.message_id.in_(ids)))))
                db.execute(delete(Delivery).where(Delivery.message_id.in_(ids)))
                db.execute(delete(Receipt).where(Receipt.message_id.in_(ids)))
                db.execute(delete(MessageAttachment).where(MessageAttachment.message_id.in_(ids)))
                db.execute(delete(Message).where(Message.id.in_(ids)))
            orphan = list(db.scalars(select(Attachment).where(Attachment.created_at < now() - timedelta(days=1),
                         ~Attachment.id.in_(select(MessageAttachment.attachment_id))).limit(100)))
            for row in orphan:
                object_store().remove_object(ns["MINIO_BUCKET"], row.object_name)
                db.delete(row)
            if ids or orphan:
                audit(db, "notification-cleanup", "notification_retention_cleanup", "notification", category="ops",
                      after={"messages_removed": len(ids), "attachments_removed": len(orphan)})
            db.commit()
            return {"messages_removed": len(ids), "attachments_removed": len(orphan)}

    def scheduled_scan():
        hook = ns.get("notification_scheduled_events")
        if not hook:
            return {"status": "not_configured"}
        with ns["SessionLocal"]() as db:
            if db.bind.dialect.name == "postgresql" and not db.scalar(text("SELECT pg_try_advisory_xact_lock(72810421)")):
                return {"status": "another_worker_scanning"}
            hook(db)
            db.commit()
            return {"status": "scanned"}

    def rollback_history():
        with ns["SessionLocal"]() as db:
            ids = list(db.scalars(select(Message.id).where(Message.event_key.like("legacy:%"))))
            db.execute(delete(DeliveryAttempt).where(DeliveryAttempt.delivery_id.in_(select(Delivery.id).where(Delivery.message_id.in_(ids)))))
            db.execute(delete(Delivery).where(Delivery.message_id.in_(ids)))
            db.execute(delete(Receipt).where(Receipt.message_id.in_(ids)))
            db.execute(delete(MessageAttachment).where(MessageAttachment.message_id.in_(ids)))
            db.execute(delete(Message).where(Message.id.in_(ids)))
            db.commit()
            return len(ids)

    @app.post("/api/notifications/batch-actions")
    def batch(body: BatchAction, user=Depends(current_user), db=Depends(db_session)):
        rows = [find(db, user.id, rid) for rid in set(body.ids)]
        for receipt, message in rows:
            apply_action(db, user, receipt, message, body.action)
        db.commit()
        return {"processed": len(rows), **summary(db, user.id)}

    @app.get("/api/notifications/{rid}")
    def detail(rid: str, user=Depends(current_user), db=Depends(db_session)):
        receipt, message = find(db, user.id, rid)
        return {**out(receipt, message), "attachments": attachment_list(db, message.id)}

    @app.delete("/api/notifications/{rid}")
    def delete_message(rid: str, user=Depends(current_user), db=Depends(db_session)):
        receipt, message = find(db, user.id, rid)
        apply_action(db, user, receipt, message, "delete")
        db.commit()
        return {**out(receipt, message), "attachments": attachment_list(db, message.id)}

    @app.post("/api/notifications/{rid}/{action}")
    def action(rid: str, action: str, user=Depends(current_user), db=Depends(db_session)):
        receipt, message = find(db, user.id, rid)
        apply_action(db, user, receipt, message, action)
        db.commit()
        return {**out(receipt, message), "attachments": attachment_list(db, message.id)}

    @app.on_event("startup")
    def migrate_history():
        with ns["SessionLocal"]() as db:
            for model in (Message, Receipt, Delivery):
                for index in model.__table__.indexes:
                    index.create(db.connection(), checkfirst=True)
            migrate(db)
            db.commit()

    return {"create": create, "migrate": migrate, "Message": Message, "Receipt": Receipt, "Delivery": Delivery,
            "Outbox": Delivery, "Attachment": Attachment, "Preference": Preference, "process_once": process_once,
            "cleanup": cleanup, "rollback_history": rollback_history, "send_email": send_email, "redis_client": redis_client,
            "DeliveryAttempt": DeliveryAttempt, "scheduled_scan": scheduled_scan}
