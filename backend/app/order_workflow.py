"""Provider review and payment guards for TRD-BE-006/007.

Install after order_economics. The transition caller must lock and refresh Order,
call authorize_transition before metering authorization, and return immediately
for an already-paid confirmation, before any logs, tasks, or payment side effects.
These hooks never commit or change payment state.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from .order_economics import money


OFFLINE = {"training", "consulting", "custom"}


class ProviderReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    amount: Decimal | None = None
    cost: Decimal | None = None
    reason: str = Field(default="", max_length=2000)
    expected_updated_at: datetime | None = None

    @field_validator("amount", "cost", mode="before")
    @classmethod
    def valid_money(cls, value):
        if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError("金额必须为有效的非负数值")
        try:
            return money(value)
        except HTTPException as exc:
            raise ValueError("价格或成本无效") from exc


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def install(ns):
    Order, Payment, Membership = (ns[key] for key in ("Order", "Payment", "Membership"))

    def active(user):
        if not user.is_active or user.activation_status != "active":
            raise HTTPException(403, "账号未激活或已停用")

    def member(db, user, enterprise_id, roles):
        return bool(enterprise_id) and db.scalar(select(Membership.id).where(
            Membership.user_id == user.id, Membership.enterprise_id == enterprise_id,
            Membership.status == "active", Membership.role.in_(roles))) is not None

    def require_provider(db, user, order):
        active(user)
        if user.platform_role or not member(db, user, order.provider_enterprise_id,
                                           ("super_admin", "enterprise_admin")):
            raise HTTPException(403, "仅提供方企业的活跃超级管理员或企业管理员可操作")

    def offline(order):
        return order.snapshot_version == 1 and order.delivery_method_snapshot in OFFLINE

    def initialize_order(db, order, user):
        if order.snapshot_version == 1:
            order.main_status = "pending_provider_review" if offline(order) else "pending_payment"

    def payments(db, order):
        return db.scalars(select(Payment).where(Payment.order_id == order.id)
                          .order_by(Payment.created_at.desc(), Payment.id)
                          .with_for_update().execution_options(populate_existing=True)).all()

    def payment_repeat(db, order, action):
        """Call only after authorization, with the Order row locked by the caller."""
        if action != "confirm_payment" or order.payment_status != "paid":
            return False
        rows = payments(db, order)
        if not rows or rows[0].status != "paid" or money(rows[0].amount) != money(order.amount) or money(order.paid_amount) != money(order.amount):
            raise HTTPException(409, "订单与支付单的已支付金额或状态不一致")
        return True

    def authorize_transition(db, user, order, action):
        """True bypasses metering's role check; it is not an idempotency signal."""
        if order.snapshot_version == 1 and action in {"submit_review", "approve", "reject"}:
            ns["settlement_metering"]["require_transition"](db, user, order, action)
            raise HTTPException(409, "新版订单不支持旧审核动作；线下订单须通过提供方审核接口审核，线上订单直接支付")
        if action not in {"start_payment", "confirm_payment"}:
            return False
        active(user)
        buyer = not user.platform_role and member(db, user, order.buyer_enterprise_id, ("super_admin",))
        finance = action == "confirm_payment" and user.platform_role in {"super_admin", "finance_settlement"}
        if not (buyer or finance):
            raise HTTPException(403, "仅买方企业超级管理员可发起支付；平台超级管理员或财务清算人员可确认到账")
        if buyer and order.snapshot_version == 1:
            ns["trading_policy"]["require_buyer"](db, user, order.buyer_enterprise_id)
        if order.main_status in {"rejected", "cancelled", "closed"}:
            raise HTTPException(409, "当前订单状态不允许支付")
        if offline(order) and order.main_status not in {"pending_payment", "pending_fulfillment", "fulfilling", "pending_confirmation", "awaiting_start", "in_delivery", "pending_acceptance", "rectifying", "completed"}:
            raise HTTPException(409, "线下订单须经提供方审核通过后才能支付")
        if offline(order) and order.payment_status != "paid" and order.main_status != "pending_payment":
            raise HTTPException(409, "线下订单须经提供方审核通过后才能支付")
        if order.snapshot_version == 1:
            ns["order_economics"]["economic_snapshot"](order)
            ns["order_economics"]["delivery_snapshot"](order)
        rows = payments(db, order)
        if not rows:
            raise HTTPException(409, "订单支付单不存在")
        if money(rows[0].amount) != money(order.amount):
            raise HTTPException(409, "支付单金额与订单冻结报价不一致")
        if action == "start_payment":
            if order.payment_status != "unpaid" or rows[0].status != "unpaid":
                raise HTTPException(409, "订单已发起支付，不能重复发起")
        elif (order.payment_status not in {"unpaid", "paying", "paid"}
              or rows[0].status not in {"unpaid", "paying", "paid"}
              or (order.payment_status == "paid") != (rows[0].status == "paid")):
            raise HTTPException(409, "订单与支付单的支付状态不一致")
        return True

    def locked_order(db, order_id):
        order = db.scalar(select(Order).where(Order.id == order_id).with_for_update()
                          .execution_options(populate_existing=True))
        if not order:
            raise HTTPException(404, "订单不存在")
        return order

    def editable(order):
        if not offline(order) or order.main_status != "pending_provider_review" or order.payment_status != "unpaid" or order.paid_amount or order.refunded_amount:
            raise HTTPException(409, "订单不处于待提供方审核状态，报价已冻结，不能修改")

    def provider_review(order_id: str, body: ProviderReviewBody,
                        user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        order = locked_order(db, order_id)
        require_provider(db, user, order)
        editable(order)
        if body.expected_updated_at is not None and utc(body.expected_updated_at) != utc(order.updated_at):
            raise HTTPException(409, "订单已变更，请刷新报价后重试")
        previous = ns["order_economics"]["economic_snapshot"](order)
        if previous["billing_unit"] != "order" or order.subscription_months != 1:
            raise HTTPException(409, "线下订单须采用一次性计费，购买月数必须为一")
        amount = body.amount if body.amount is not None else money(order.amount)
        cost = body.cost if body.cost is not None else money(order.total_cost_snapshot)
        changed = amount != money(order.amount) or cost != money(order.total_cost_snapshot)
        reason = body.reason.strip()
        if (changed or body.decision == "reject") and not reason:
            raise HTTPException(400, "调整报价或成本、拒绝订单时必须填写原因")
        if amount < cost:
            raise HTTPException(400, "订单售价不得低于成本")
        if body.decision == "reject" and changed:
            raise HTTPException(400, "拒绝订单时不能同时调整报价或成本")
        rows = payments(db, order)
        if not rows or any(row.status != "unpaid" or row.paid_at is not None for row in rows):
            raise HTTPException(409, "订单已发起支付，报价已冻结，不能修改")
        before = {"main_status": order.main_status, "economic_snapshot": previous}
        updated = {**previous, "unit_price": str(amount), "unit_cost": str(cost),
                   "amount": str(amount), "total_cost": str(cost)}
        order.amount = order.unit_price_snapshot = amount
        order.unit_cost_snapshot = order.total_cost_snapshot = cost
        order.economic_snapshot_json = json.dumps(updated, sort_keys=True)
        order.main_status = "pending_payment" if body.decision == "approve" else "rejected"
        order.updated_at = ns["now"]()
        for row in rows:
            row.amount = amount
        ns["order_economics"]["economic_snapshot"](order)
        action = "provider_review_" + body.decision
        # Free text may contain costs. Keep it in the internal audit, not buyer-visible logs/messages.
        db.add(ns["OrderStateLog"](order_id=order.id, domain="main", from_status=before["main_status"],
                                  to_status=order.main_status, action=action, operator=user.name,
                                  reason="提供方审核通过" if body.decision == "approve" else "提供方审核拒绝"))
        ns["audit"](db, user.email or user.phone or user.id, action, "order", order.id,
                    reason, category="order", business_domain="trading",
                    tenant_id=order.provider_enterprise_id, order_id=order.id,
                    before=before, after={"main_status": order.main_status, "economic_snapshot": updated,
                                          "decision": body.decision, "reason": reason})
        outcome = "审核通过，待支付" if body.decision == "approve" else "审核拒绝"
        ns["notify_business_event"](db, "订单提供方审核结果",
            f"订单 {order.order_no}：{outcome}，订单金额 {amount}，请查看业务详情。",
            "order", order.id, event_key=f"business:order:{order.id}:{action}", category="order",
            tenant_id=order.buyer_enterprise_id,
            recipient_ids=[order.buyer_user_id] if order.buyer_user_id else [],
            enterprise_ids=[order.buyer_enterprise_id])
        db.commit()
        return ns["order_out"](order)

    def provider_quote(order_id: str, user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        order = db.get(Order, order_id)
        if not order:
            raise HTTPException(404, "订单不存在")
        require_provider(db, user, order)
        if not offline(order):
            raise HTTPException(409, "仅新建线下订单支持提供方报价编辑")
        snapshot = ns["order_economics"]["economic_snapshot"](order)
        return {"order_id": order.id, "amount": snapshot["amount"], "cost": snapshot["total_cost"],
                "unit_price": snapshot["unit_price"], "unit_cost": snapshot["unit_cost"],
                "main_status": order.main_status, "payment_status": order.payment_status,
                "expected_updated_at": order.updated_at}

    ns["app"].post("/api/orders/{order_id}/provider-review")(provider_review)
    ns["app"].get("/api/orders/{order_id}/provider-quote")(provider_quote)
    return {"initialize_order": initialize_order, "authorize_transition": authorize_transition,
            "payment_repeat": payment_repeat}
