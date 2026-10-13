"""Request-scoped enterprise membership resolution for console endpoints."""

from fastapi import HTTPException
from sqlalchemy import select


def resolve_enterprise_membership(db, user, membership_model, enterprise_id=None):
    """Select an active membership, rejecting explicit scopes the user lacks."""
    if enterprise_id:
        membership = db.scalar(select(membership_model).where(
            membership_model.user_id == user.id,
            membership_model.enterprise_id == enterprise_id,
            membership_model.status == "active",
        ))
        if not membership:
            raise HTTPException(403, "用户尚未加入所选企业")
        return membership

    membership = db.scalar(select(membership_model).where(
        membership_model.user_id == user.id,
        membership_model.status == "active",
    ).order_by(membership_model.created_at))
    if not membership:
        raise HTTPException(403, "用户尚未加入有效企业")
    return membership


def require_enterprise_super_admin(membership):
    if membership.role != "super_admin":
        raise HTTPException(403, "只有企业超级管理员可以查看企业运营总览")


def require_enterprise_editor(db, user, membership_model, enterprise_id):
    if user.platform_role in {
        "super_admin", "platform_operator", "product_manager", "business_reviewer",
        "quality_reviewer", "security_compliance",
    }:
        return
    if user.platform_role:
        raise HTTPException(403, "当前平台角色无权创建或编辑产品")
    membership = resolve_enterprise_membership(db, user, membership_model, enterprise_id)
    if user.verified_status != "verified" or membership.role not in {"super_admin", "enterprise_admin"}:
        raise HTTPException(403, "普通企业成员无权创建或编辑产品")
    return membership
