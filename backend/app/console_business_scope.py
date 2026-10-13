"""Tenant and business-side query scopes for the limited enterprise console."""

from fastapi import HTTPException
from sqlalchemy import or_, select

from .enterprise_console_scope import resolve_enterprise_membership


def resolve_business_scope(request, db, user, membership_model, mode=""):
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
    if selected_mode and membership.role not in {"super_admin", "enterprise_admin"}:
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
    return or_(audit_model.tenant_id == tenant, audit_model.order_id.in_(order_ids))
