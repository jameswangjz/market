"""TRD-BE-008 snapshot offline fulfillment hooks.

Install after order_economics/order_workflow with install(globals()). Main must
lock and refresh Order (FOR UPDATE, populate_existing) before calling hooks.
Call handle_transition(db, user, order, body) BEFORE legacy transition lookup,
authorization and side effects; True means main commits and returns order_out.
False delegates to existing logic. Hooks never commit; the caller must roll back
on failure so state, logs, audit, metering and notifications remain atomic.

After setting Payment/Order paid and calling create_delivery_task, call
after_payment(db, order, user) to normalize the existing manual task to
awaiting_start. The payment caller records payment events and commits.
Paid retries must be short-circuited by order_workflow before this hook.

The legacy /delivery-tasks/{id}/process caller must lock and refresh the parent
Order and call require_task_result(db, user, order, task) before task mutation.
Upload must call require_attachment_upload(db, user, order, task) with locked,
refreshed rows before storing evidence. Upload/list/download remain the API: this module
never replaces tasks or deletes attachments. No new HTTP routes are installed.
"""

import secrets

from fastapi import HTTPException
from sqlalchemy import select

from .order_economics import money
from .order_workflow import OFFLINE


PARTY_ACTIONS = {"start_delivery", "submit_delivery", "accept_delivery", "reject_delivery"}
LEGACY_ACTIONS = {"create_task", "confirm_order", "mark_exception", "retry_delivery",
                  "delivery_succeeded", "delivery_failed"}


def install(ns):
    Task, Membership = ns["DeliveryTask"], ns["Membership"]

    def offline(order):
        return order.snapshot_version == 1 and order.delivery_method_snapshot in OFFLINE

    def require_party(db, user, order, provider):
        tenant = order.provider_enterprise_id if provider else order.buyer_enterprise_id
        if (not user.is_active or user.activation_status != "active" or user.platform_role
                or not tenant or db.scalar(select(Membership.id).where(
                    Membership.user_id == user.id, Membership.enterprise_id == tenant,
                    Membership.status == "active",
                    Membership.role.in_(("super_admin", "enterprise_admin")))) is None):
            raise HTTPException(403, "仅订单对应企业的活跃超级管理员或企业管理员可操作")

    def paid(order):
        if order.payment_status != "paid" or money(order.paid_amount) != money(order.amount):
            raise HTTPException(409, "线下履约须先完成订单全额支付")
        if money(order.refunded_amount) != 0:
            raise HTTPException(409, "已退款订单不能继续线下履约")
        ns["order_economics"]["economic_snapshot"](order)
        ns["order_economics"]["delivery_snapshot"](order)

    def tasks(db, order):
        rows = db.scalars(select(Task).where(Task.order_id == order.id)
                          .order_by(Task.created_at, Task.id).with_for_update()
                          .execution_options(populate_existing=True)).all()
        if len(rows) > 1:
            raise HTTPException(409, "线下订单存在多个履约任务，请核查任务数据")
        return rows

    def state(order, task):
        return {"main_status": order.main_status, "delivery_status": order.delivery_status,
                "task_id": task.id, "task_status": task.status}

    def set_state(db, user, order, task, main, delivery, task_status, action, reason):
        for domain, target in (("main", main), ("delivery", delivery)):
            old = getattr(order, domain + "_status")
            if old != target:
                setattr(order, domain + "_status", target)
                ns["log_state"](db, order, domain, old, target, action, user, reason)
        task.status = task_status
        task.next_retry_at = None
        order.updated_at = ns["now"]()

    def after_payment(db, order, user):
        """Normalize the task created by main; never commit the payment transaction."""
        if not offline(order):
            return False
        paid(order)
        # Main's task factory adds a pending row with autoflush disabled.
        db.flush()
        rows = tasks(db, order)
        if rows and (rows[0].method != order.delivery_method_snapshot or rows[0].delivery_mode != "manual"):
            raise HTTPException(409, "履约任务与订单冻结的线下交付快照不一致")
        if (order.main_status == order.delivery_status == "awaiting_start" and rows
                and rows[0].status == "awaiting_start"):
            return True
        if (order.main_status not in {"pending_payment", "pending_fulfillment"}
                or order.delivery_status != "preparing" or not rows or rows[0].status != "preparing"):
            raise HTTPException(409, "线下支付履约初始化须使用新生成的人工交付任务")
        method, mode, _ = ns["delivery_task_config"](db, order)
        if mode != "manual" or method != order.delivery_method_snapshot:
            raise HTTPException(409, "线下任务配置与订单冻结快照不一致")
        task = rows[0]
        if task.method != method or task.delivery_mode != mode:
            raise HTTPException(409, "履约任务与订单冻结的线下交付快照不一致")
        before = state(order, task)
        task.assignee = order.provider_enterprise_id
        set_state(db, user, order, task, "awaiting_start", "awaiting_start", "awaiting_start",
                  "payment_fulfillment_ready", "")
        ns["audit"](db, user.email or user.phone or user.id, "payment_fulfillment_ready",
                    "order", order.id, category="delivery", business_domain="trading",
                    tenant_id=order.provider_enterprise_id, order_id=order.id,
                    before=before, after=state(order, task))
        # The payment caller emits confirm_payment once, notifying both parties.
        return True

    def guard_delivery_process(db, user, order):
        """Block platform/manual task processing from bypassing party transitions."""
        if offline(order):
            raise HTTPException(409, "新版线下订单须由提供方提交交付、买方验收，不能使用旧任务处理接口")

    def require_task_result(db, user, order, task):
        """Use for every legacy task result/process path, before mutation."""
        guard_delivery_process(db, user, order)

    def require_attachment_upload(db, user, order, task):
        """New offline evidence is writable only by an active provider administrator."""
        if not offline(order):
            return
        require_party(db, user, order, True)
        paid(order)
        if (task.order_id != order.id or task.method != order.delivery_method_snapshot
                or task.delivery_mode != "manual"
                or order.main_status not in {"awaiting_start", "in_delivery", "rectifying"}
                or task.status != order.main_status or order.delivery_status != order.main_status):
            raise HTTPException(409, "当前履约状态不允许修改线下交付凭证")

    def handle_transition(db, user, order, body):
        """True is handled, False delegates; main commits and serializes the order."""
        if not offline(order):
            return False
        action = body.action
        if action not in PARTY_ACTIONS | LEGACY_ACTIONS:
            return False
        provider = action in {"start_delivery", "submit_delivery", "create_task",
                              "mark_exception", "retry_delivery", "delivery_succeeded", "delivery_failed"}
        require_party(db, user, order, provider)
        if action in LEGACY_ACTIONS:
            raise HTTPException(409, "新版线下订单不支持旧履约动作，请使用提供方交付和买方验收流程")
        paid(order)
        reason = (body.reason or "").strip()
        if action == "reject_delivery" and not reason:
            raise HTTPException(400, "拒绝交付时必须填写整改原因")
        rows = tasks(db, order)
        if not rows:
            raise HTTPException(409, "线下履约任务不存在")
        task = rows[0]
        if task.method != order.delivery_method_snapshot or task.delivery_mode != "manual":
            raise HTTPException(409, "履约任务与订单冻结的线下交付快照不一致")
        current = order.main_status
        expected = {"start_delivery": {"awaiting_start"},
                    "submit_delivery": {"in_delivery", "rectifying"},
                    "accept_delivery": {"pending_acceptance"},
                    "reject_delivery": {"pending_acceptance"}}[action]
        if current not in expected or order.delivery_status != current or task.status != current:
            raise HTTPException(409, "当前订单或履约任务状态不允许执行此履约动作")
        target = {"start_delivery": "in_delivery", "submit_delivery": "pending_acceptance",
                  "accept_delivery": "completed", "reject_delivery": "rectifying"}[action]
        before = state(order, task)
        set_state(db, user, order, task, target,
                  "accepted" if action == "accept_delivery" else target,
                  "completed" if action == "accept_delivery" else target, action, reason)
        if action == "reject_delivery":
            task.last_error = reason
        elif action == "submit_delivery":
            task.last_error = ""
        if reason:
            task.note = reason
        version = secrets.token_hex(16)
        db.flush()
        ns["audit"](db, user.email or user.phone or user.id, action, "order", order.id,
                    reason, category="delivery", business_domain="trading",
                    tenant_id=order.provider_enterprise_id if provider else order.buyer_enterprise_id,
                    order_id=order.id, before=before, after={**state(order, task), "reason": reason})
        ns["settlement_metering"]["record_order_event"](db, order, action, version, user.id, task.id)
        ns["notify_order_event"](db, order, action, version)
        return True

    return {"after_payment": after_payment, "handle_transition": handle_transition,
            "guard_delivery_process": guard_delivery_process,
            "require_task_result": require_task_result,
            "require_attachment_upload": require_attachment_upload}
