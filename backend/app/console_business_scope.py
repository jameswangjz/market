"""Tenant and business-side query scopes for the limited enterprise console."""

from fastapi import HTTPException
from sqlalchemy import and_, or_, select

from .enterprise_console_scope import resolve_enterprise_membership


def resolve_business_scope(request, db, user, membership_model, mode="", *, allow_member_view=False):
    if user.platform_role:
        return None
    header_id = request.headers.get("X-Enterprise-Id", "") if request is not None else ""
    query_id = request.query_params.get("enterprise_id", "") if request is not None else ""
    if header_id and query_id and header_id != query_id:
        raise HTTPException(400, "企业请求范围不一致")
    membership = resolve_enterprise_membership(db, user, membership_model, header_id or query_id)
    selected_mode = (mode if isinstance(mode, str) else "") or (request.query_params.get("business_view", "") if request is not None else "")
    if selected_mode and selected_mode not in {"provider", "buyer"}:
        raise HTTPException(422, "business_view必须是provider或buyer")
    if selected_mode and not allow_member_view and membership.role not in {"super_admin", "enterprise_admin"}:
        raise HTTPException(403, "普通企业成员不能切换业务视图")
    return {"enterprise_id": membership.enterprise_id, "mode": selected_mode or "both", "membership": membership}


def order_predicate(order_model, scope):
    if scope is None:
        return None
    if scope["mode"] == "provider":
        return order_model.provider_enterprise_id == scope["enterprise_id"]
    if scope["mode"] == "buyer":
        return order_model.buyer_enterprise_id == scope["enterprise_id"]
    return or_(order_model.provider_enterprise_id == scope["enterprise_id"],
               order_model.buyer_enterprise_id == scope["enterprise_id"])


def scoped_order_ids(order_model, scope):
    return select(order_model.id).where(order_predicate(order_model, scope))


def audit_predicate(audit_model, order_model, scope):
    if scope is None:
        return None
    tenant = scope["enterprise_id"]
    order_ids = scoped_order_ids(order_model, scope)
    return or_(and_(audit_model.tenant_id == tenant, or_(audit_model.order_id == "", audit_model.order_id.is_(None))),
               audit_model.order_id.in_(order_ids))


def message_predicate(message, ns, scope):
    """Classify from authoritative business links, never from message text."""
    if scope is None:
        return None
    order = ns["Order"]
    orders = select(order.id).where(order_predicate(order, scope))
    clauses = [and_(message.target_type.in_(["order", "order_state"]),
        message.target_id.in_(orders)),
        and_(message.target_type == "settlement", message.target_id.in_(
            select(ns["Settlement"].settlement_no).where(ns["Settlement"].order_id.in_(orders))))]
    links = {"delivery_task": ("DeliveryTask", "order_id"),
             "refund": ("Refund", "order_id"), "payment": ("Payment", "order_id"),
             "after_sales": ("AfterSalesTicket", "order_id"),
             "settlement_correction": ("SettlementCorrection", "order_id"),
             "settlement": ("Settlement", "order_id")}
    for target, (name, field) in links.items():
        model = ns.get(name)
        if model is not None:
            clauses.append(and_(message.target_type == target, message.target_id.in_(
                select(model.id).where(getattr(model, field).in_(orders)))))
    settlement = ns["Settlement"]
    proposal = ns["SettlementAdjustmentProposal"]
    clauses.append(and_(message.target_type == "settlement_proposal", message.target_id.in_(
        select(proposal.id).where(proposal.settlement_id.in_(
            select(settlement.id).where(settlement.order_id.in_(orders)))))))
    batch, line = ns["SettlementBatch"], ns["SettlementLine"]
    clauses.append(and_(message.target_type == "settlement_batch", message.tenant_id == scope["enterprise_id"],
        message.target_id.in_(select(batch.batch_no).join(line, line.batch_id == batch.id).join(
            settlement, settlement.id == line.settlement_id).where(settlement.order_id.in_(orders)))))
    if scope["mode"] != "buyer":
        product = ns["Product"]
        clauses.append(and_(message.target_type == "product", message.target_id.in_(
            select(product.id).where(product.enterprise_id == scope["enterprise_id"]))))
    subscription = ns["SaaSSubscription"]
    product = ns["Product"]
    subscription_scope = subscription.enterprise_id == scope["enterprise_id"]
    if scope["mode"] == "provider":
        subscription_scope = subscription.product_id.in_(select(product.id).where(product.enterprise_id == scope["enterprise_id"]))
    elif scope["mode"] == "both":
        subscription_scope = or_(subscription_scope, subscription.product_id.in_(
            select(product.id).where(product.enterprise_id == scope["enterprise_id"])))
    clauses.append(and_(message.target_type == "saas_subscription", message.target_id.in_(
        select(subscription.id).where(subscription_scope))))
    credential = ns["ApiCredential"]
    if scope["mode"] != "provider":
        clauses.append(and_(message.target_type == "api_credential", message.target_id.in_(
            select(credential.id).where(credential.enterprise_id == scope["enterprise_id"]))))
    # Unknown legacy targets remain tenant-local shared notices. Known business
    # targets must match their authoritative party; tenant_id is not sufficient.
    clauses.append(and_(message.tenant_id == scope["enterprise_id"],
        message.target_type.notin_(["order", "order_state", "settlement", "settlement_batch",
            "settlement_proposal", "product", "saas_subscription", "api_credential", *links])))
    clauses.append(and_(message.tenant_id == "", message.target_type.in_(["", "user", "identity", "identity_verification", "membership"]),
        message.category.in_(["announcement", "auth", "identity", "enterprise", "review"])))
    clauses.append(and_(message.tenant_id == scope["enterprise_id"], message.target_type.in_(["order", "order_state"]),
        ~message.target_id.in_(select(order.id)), ~message.target_id.in_(select(order.order_no))))
    return or_(*clauses)
