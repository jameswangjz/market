"""Audited after-sales state actions; caller owns the transaction."""
import hashlib
import json
import secrets

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

TERMINAL = frozenset({"completed", "withdrawn", "force_closed"})
ENDED = TERMINAL | {"resolved", "closed"}
STATES = frozenset({"awaiting_response", "processing", "awaiting_buyer_info",
                    "awaiting_buyer_confirmation", "platform_intervention"}) | TERMINAL
PLATFORM = frozenset({"super_admin", "platform_operator", "delivery_monitor"})
TRANSITIONS = {
    "respond": ("provider", {"awaiting_response"}, "processing"),
    "request_info": ("provider", {"processing"}, "awaiting_buyer_info"),
    "supplement": ("buyer", {"awaiting_buyer_info"}, "processing"),
    "submit_resolution": ("provider", {"processing"}, "awaiting_buyer_confirmation"),
    "confirm": ("buyer", {"awaiting_buyer_confirmation"}, "completed"),
    "reject_resolution": ("buyer", {"awaiting_buyer_confirmation"}, "processing"),
    "withdraw": ("buyer", STATES - TERMINAL, "withdrawn"),
    "request_intervention": ("party", STATES - TERMINAL - {"platform_intervention"}, "platform_intervention"),
    "respond_intervention": ("platform", {"platform_intervention"}, "platform_intervention"),
    "review_intervention": ("platform", {"platform_intervention"}, None),
    "force_close": ("closer", STATES - TERMINAL, "force_closed"),
}


class TicketCreate(BaseModel):
    order_id: str = Field(min_length=1, max_length=36)
    type: str = Field(default="质量异议", min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=10000)
    priority: str = Field(default="normal", pattern="^(normal|important|urgent)$")
    idempotency_key: str = Field(min_length=8, max_length=100)


class TicketAction(BaseModel):
    action: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=1, max_length=10000)
    expected_version: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=8, max_length=100)
    attachments: list[str] = Field(default_factory=list, max_length=10)


def install(ns):
    Ticket, Event, Order, Member = (ns[n] for n in
                                  ("AfterSalesTicket", "AfterSalesEvent", "Order", "Membership"))
    app = ns["app"]

    def active(user):
        if not user.is_active or user.activation_status != "active" or user.verified_status != "verified":
            raise HTTPException(403, "仅已激活、已实名用户可操作售后")

    def party_role(db, user, order, side):
        if user.platform_role:
            return None
        enterprise_id = getattr(order, side + "_enterprise_id")
        enterprise = db.get(ns["Enterprise"], enterprise_id) if enterprise_id else None
        if not enterprise or enterprise.verification_status != "verified":
            return None
        return db.scalar(select(Member.role).where(Member.enterprise_id == enterprise_id,
            Member.user_id == user.id, Member.status == "active",
            Member.role.in_(("super_admin", "enterprise_admin"))))

    def require_view(db, user, order):
        active(user)
        if user.platform_role in PLATFORM:
            return
        if not any(party_role(db, user, order, side) for side in ("buyer", "provider")):
            raise HTTPException(404, "售后工单不存在")

    def require_scope(request, db, user, order):
        scope = ns["resolve_business_scope"](request, db, user, Member)
        if scope is None:
            return
        tenant, mode = scope["enterprise_id"], scope["mode"]
        visible = (mode != "provider" and order.buyer_enterprise_id == tenant or
                   mode != "buyer" and order.provider_enterprise_id == tenant)
        if not visible:
            raise HTTPException(404, "售后工单不存在")

    def authorize(db, user, order, role):
        active(user)
        if role == "platform" and user.platform_role in PLATFORM:
            return user.platform_role, ""
        if role == "closer" and user.platform_role in {"super_admin", "platform_operator"}:
            return user.platform_role, ""
        for side in (("buyer", "provider") if role == "party" else (role,)):
            if side in {"buyer", "provider"}:
                member_role = party_role(db, user, order, side)
                if member_role:
                    return member_role, getattr(order, side + "_enterprise_id")
        raise HTTPException(403, "当前角色无权执行该售后操作")

    def allowed_actions(db, user, ticket, order):
        current = "completed" if ticket.status == "resolved" else ticket.status
        result = []
        for action, (role, states, _) in TRANSITIONS.items():
            if current not in states:
                continue
            try:
                authorize(db, user, order, role)
                if action == "respond_intervention":
                    ns["ticket_sla_clocks"]["require_first_intervention_response"](db, ticket)
            except HTTPException:
                continue
            result.append(action)
        return result

    def display_names(db, ids):
        User = ns["User"]
        return dict(db.execute(select(User.id, User.name).where(User.id.in_(set(ids)))).all()) if ids else {}

    def output(ticket, db=None, user=None, orders=None):
        result = {key: getattr(ticket, key) for key in ("id", "ticket_no", "order_id", "type",
            "description", "priority", "status", "previous_status", "version", "created_by",
            "buyer_enterprise_id", "provider_enterprise_id", "service_level_code", "response_due_at",
            "first_response_at", "submitted_resolution_at", "closed_at", "close_reason",
            "evidence_status", "created_at")}
        if db is not None and user is not None:
            order = orders.get(ticket.order_id) if orders is not None else db.get(Order, ticket.order_id)
            result["order_no"] = order.order_no if order else ""
            result["allowed_actions"] = allowed_actions(db, user, ticket, order) if order else []
        return result

    def output_list(db, user, tickets):
        orders = {order.id: order for order in db.scalars(select(Order).where(
            Order.id.in_({ticket.order_id for ticket in tickets})))} if tickets else {}
        return [output(ticket, db, user, orders) for ticket in tickets]

    def canonical(body):
        payload = body.model_dump()
        if not payload.get("attachments"):
            payload.pop("attachments", None)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def identity(user, body):
        return hashlib.sha256((user.id + ":" + body.idempotency_key).encode()).hexdigest()

    def repeated(db, user, body, ticket_id=None):
        event = db.scalar(select(Event).where(Event.operation_key == identity(user, body)))
        if event:
            if event.payload_hash != hashlib.sha256(canonical(body).encode()).hexdigest() or (
                    ticket_id is not None and event.ticket_id != ticket_id):
                raise HTTPException(409, "幂等键已用于不同操作")
            return db.get(Ticket, event.ticket_id)

    def record(db, user, order, ticket, body, old, actor_role, actor_enterprise, occurred_at):
        new = {"status": ticket.status, "version": ticket.version}
        attachment_ids = getattr(body, "attachments", [])
        if attachment_ids:
            new["attachments"] = attachment_ids
        event = Event(ticket_id=ticket.id, operation_key=identity(user, body),
            payload_hash=hashlib.sha256(canonical(body).encode()).hexdigest(),
            event_type=getattr(body, "action", "create"), from_status=old.get("status", ""),
            to_status=ticket.status, actor_id=user.id, actor_role=actor_role,
            actor_enterprise_id=actor_enterprise, reason=getattr(body, "reason", body.description if isinstance(body, TicketCreate) else ""),
            request_id=(ns["audit_request_id"].get() or "")[:100],
            before_json=json.dumps(old), after_json=json.dumps(new), occurred_at=occurred_at)
        db.add(event)
        if attachment_ids:
            collaboration["attach_event"](db, ticket, user, event, attachment_ids)
        ns["audit"](db, user.email or user.id, "after_sales_" + event.event_type,
            "after_sales_ticket", ticket.id, event.reason, order_id=order.id,
            category="after_sales", business_domain="after_sales", before=old, after=new)
        ns["notify_business_event"](db, "售后工单更新", ticket.ticket_no + "：" + ticket.status,
            "after_sales", ticket.id, event_key="after-sales:" + event.operation_key,
            category="after_sales", enterprise_ids=[order.buyer_enterprise_id, order.provider_enterprise_id],
            platform_roles=["super_admin", "platform_operator", "delivery_monitor"])
        hook = ns.get("ticket_sla_event")
        if hook:
            hook(db, ticket, event)
        db.flush()

    def create(db, user, body):
        order = db.scalar(select(Order).where(Order.id == body.order_id).with_for_update()
                          .execution_options(populate_existing=True))
        if not order:
            raise HTTPException(404, "订单不存在")
        role, tenant = authorize(db, user, order, "buyer")
        prior = repeated(db, user, body)
        if prior:
            return prior
        if not body.description.strip():
            raise HTTPException(422, "售后说明不能为空")
        category = body.type.strip()
        if not category:
            raise HTTPException(422, "售后类别不能为空")
        if order.payment_status not in {"paid", "refunding", "refunded"} or not order.paid_amount:
            raise HTTPException(409, "仅已支付订单可申请售后")
        existing = db.scalar(select(Ticket.id).where(Ticket.order_id == order.id,
            Ticket.type == category, Ticket.status.notin_(ENDED)))
        if existing:
            raise HTTPException(409, "同订单同类别已有未结束工单")
        freeze = ns.get("freeze_sla_snapshot")
        snapshot = freeze(db, order.buyer_enterprise_id) if freeze else None
        if snapshot is None:
            raise HTTPException(409, "企业SLA承诺尚未配置，请联系平台")
        ticket = Ticket(ticket_no="AS-" + secrets.token_hex(8).upper(), order_id=order.id,
            type=category, description=body.description.strip(), priority=body.priority,
            status="awaiting_response", version=1, buyer_enterprise_id=order.buyer_enterprise_id,
            provider_enterprise_id=order.provider_enterprise_id, created_by=user.id,
            sla_snapshot_json=json.dumps(snapshot, ensure_ascii=False), evidence_status="recorded",
            service_level_code=snapshot["service_level_code"], created_at=ns["now"]())
        db.add(ticket)
        db.flush()
        order.after_sales_status = "processing"
        record(db, user, order, ticket, body, {}, role, tenant, ticket.created_at)
        return ticket

    def act(db, user, ticket_id, body):
        peek = db.get(Ticket, ticket_id)
        if not peek:
            raise HTTPException(404, "售后工单不存在")
        order = db.scalar(select(Order).where(Order.id == peek.order_id).with_for_update()
                          .execution_options(populate_existing=True))
        if not order:
            raise HTTPException(404, "售后工单不存在")
        require_view(db, user, order)
        ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update()
                           .execution_options(populate_existing=True))
        if body.action not in TRANSITIONS:
            raise HTTPException(422, "不支持的售后动作")
        party, allowed, target = TRANSITIONS[body.action]
        role, tenant = authorize(db, user, order, party)
        prior = repeated(db, user, body, ticket.id)
        if prior:
            return prior
        if not body.reason.strip():
            raise HTTPException(422, "处理说明不能为空")
        if ticket.version != body.expected_version:
            raise HTTPException(409, "工单已更新，请刷新后操作")
        collaboration["validate_attachments"](db, ticket, user, body.attachments)
        # Legacy evidence is never backfilled with invented response timestamps.
        current = "completed" if ticket.status == "resolved" else ticket.status
        if current not in allowed:
            raise HTTPException(409, "当前售后状态不能执行该动作")
        if body.action == "respond_intervention":
            ns["ticket_sla_clocks"]["require_first_intervention_response"](db, ticket)
        old = {"status": ticket.status, "version": ticket.version}
        if body.action == "request_intervention":
            ticket.previous_status = current
        if body.action == "review_intervention":
            target = ticket.previous_status
            if target not in STATES - TERMINAL - {"platform_intervention"}:
                raise HTTPException(409, "介入前状态缺失，请核查历史记录")
        ticket.status, ticket.version = target, ticket.version + 1
        at = ns["now"]()
        if body.action == "respond" and ticket.first_response_at is None:
            ticket.first_response_at = at
        if body.action == "submit_resolution":
            ticket.submitted_resolution_at = at
        if target in TERMINAL:
            ticket.closed_at, ticket.close_reason = at, body.reason.strip()
        active_ticket = db.scalar(select(Ticket.id).where(Ticket.order_id == order.id,
            Ticket.id != ticket.id, Ticket.status.notin_(ENDED)))
        order.after_sales_status = "processing" if target not in TERMINAL or active_ticket else "resolved"
        record(db, user, order, ticket, body, old, role, tenant, at)
        return ticket

    @app.post("/api/after-sales")
    def create_ticket(body: TicketCreate, request: Request, user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        with collaboration["guarded"](db, user, body.order_id, "create"):
            order = db.get(Order, body.order_id)
            if not order:
                raise HTTPException(404, "订单不存在")
            require_scope(request, db, user, order)
            ticket = create(db, user, body)
            db.commit()
            return output(ticket, db, user)

    @app.get("/api/after-sales/{ticket_id}")
    def detail(ticket_id: str, request: Request, user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        ticket = db.get(Ticket, ticket_id)
        order = db.get(Order, ticket.order_id) if ticket else None
        if not order:
            raise HTTPException(404, "售后工单不存在")
        require_view(db, user, order)
        require_scope(request, db, user, order)
        return output(ticket, db, user)

    @app.post("/api/after-sales/{ticket_id}/actions")
    def action(ticket_id: str, body: TicketAction, request: Request, user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        with collaboration["guarded"](db, user, ticket_id, body.action):
            ticket = db.get(Ticket, ticket_id)
            order = db.get(Order, ticket.order_id) if ticket else None
            if not order:
                raise HTTPException(404, "售后工单不存在")
            require_view(db, user, order)
            require_scope(request, db, user, order)
            ticket = act(db, user, ticket_id, body)
            db.commit()
            return output(ticket, db, user)

    @app.get("/api/after-sales/{ticket_id}/timeline")
    def timeline(ticket_id: str, request: Request,
            page: int | None = Query(default=None, ge=1),
            page_size: int | None = Query(default=None, ge=1, le=100),
            user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        if page is not None or page_size is not None:
            return ns["sla_evidence"]["timeline_page"](db, user, ticket_id, request, page or 1, page_size or 20)
        ticket = db.get(Ticket, ticket_id)
        order = db.get(Order, ticket.order_id) if ticket else None
        if not order:
            raise HTTPException(404, "售后工单不存在")
        require_view(db, user, order)
        require_scope(request, db, user, order)
        events = db.scalars(select(Event).where(Event.ticket_id == ticket.id)
                           .order_by(Event.occurred_at, Event.id)).all()
        names = display_names(db, [e.actor_id for e in events])
        attachments = collaboration["event_attachments"](db, [e.id for e in events])
        return {"items": [{"id": e.id, "action": e.event_type, "actor_id": e.actor_id,
            "actor_name": names.get(e.actor_id) or "",
            "actor_role": e.actor_role, "actor_enterprise_id": e.actor_enterprise_id,
            "request_id": e.request_id,
            "reason": e.reason, "occurred_at": e.occurred_at,
            "attachments": attachments.get(e.id, []),
            "before": json.loads(e.before_json), "after": json.loads(e.after_json)} for e in events],
            "evidence_status": ticket.evidence_status}

    @app.get("/api/after-sales/{ticket_id}/sla")
    def ticket_sla(ticket_id: str, request: Request, user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        ticket = db.get(Ticket, ticket_id)
        order = db.get(Order, ticket.order_id) if ticket else None
        if not order:
            raise HTTPException(404, "售后工单不存在")
        require_view(db, user, order)
        require_scope(request, db, user, order)
        # Match action lock order before calendar resolution or clock writes.
        order = db.scalar(select(Order).where(Order.id == order.id).with_for_update()
                          .execution_options(populate_existing=True))
        ticket = db.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update()
                           .execution_options(populate_existing=True))
        if ticket is None or order is None:
            raise HTTPException(404, "售后工单不存在")
        result = ns["ticket_sla_clocks"]["refresh"](db, ticket, ns["now"]())
        # GET materializes clocks and may append audited annual bindings.
        db.commit()
        return result

    workflow = {"create": create, "act": act, "output": output, "require_view": require_view,
                "require_scope": require_scope, "authorize": authorize, "active": active,
                "display_names": display_names, "output_list": output_list}
    from .after_sales_collaboration import install as install_collaboration
    collaboration = install_collaboration(ns, workflow)
    workflow["collaboration"] = collaboration
    return workflow
