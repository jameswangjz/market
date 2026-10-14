"""Limited provider participation in the existing settlement workflow."""
import json
from decimal import Decimal, ROUND_DOWN

from fastapi import HTTPException
from sqlalchemy import or_, select

from .console_business_scope import resolve_business_scope


def settlement_access(ns, request, db, user, settlement_id, *, write=False):
    scope = resolve_business_scope(request, db, user, ns["Membership"])
    if scope is None:
        ns["require_settlement_operator" if write else "require_settlement_viewer"](user)
    elif (user.verified_status != "verified" or scope["mode"] == "buyer" or
            scope["membership"].role not in {"super_admin", "enterprise_admin"}):
        raise HTTPException(403, "只有已验证的提供方企业管理员可以参与清算")
    settlement, order = ns["Settlement"], ns["Order"]
    query = select(settlement).join(order, order.id == settlement.order_id).where(
        or_(settlement.id == settlement_id, settlement.settlement_no == settlement_id))
    if scope is not None:
        query = query.where(order.provider_enterprise_id == scope["enterprise_id"], settlement.status != "superseded")
    item = db.scalar(query)
    if item is None:
        raise HTTPException(404, "清算单不存在")
    if write:
        line, batch = ns["SettlementLine"], ns["SettlementBatch"]
        batch_ids = set(db.scalars(select(line.batch_id).where(line.settlement_id == item.id, line.status != "superseded")))
        # Batch before settlement is also the lock order for rebuild/confirm.
        if batch_ids:
            db.scalars(select(batch).where(batch.id.in_(batch_ids)).order_by(batch.id).with_for_update()
                .execution_options(populate_existing=True)).all()
        item = db.scalar(query.with_for_update(of=settlement).execution_options(populate_existing=True))
        if item is None:
            raise HTTPException(404, "清算单不存在")
        current_batches = set(db.scalars(select(line.batch_id).where(line.settlement_id == item.id, line.status != "superseded")))
        if current_batches != batch_ids:
            raise HTTPException(409, "清算批次发生变化，请刷新重试")
        if batch_ids and db.scalar(select(batch.id).where(batch.id.in_(batch_ids),
                batch.status.notin_(["generated", "pending_confirm"]))):
            raise HTTPException(409, "清算批次已确认、回滚或作废")
    return item, scope


def proposal_identity(user, scope):
    return {"side": "provider" if scope is not None else "platform",
            "enterprise_id": scope["enterprise_id"] if scope is not None else "",
            "user_id": user.id}


def require_counterparty(ns, db, proposal, user, scope):
    values = json.loads(proposal.values_json or "{}")
    identity = values.get("_proposer")
    if identity is None:
        # Historical proposals were platform-only. Keep their original
        # different-operator review rule; never infer provider authority.
        if scope is not None or proposal.proposed_by == (user.email or user.name):
            raise HTTPException(409, "历史提案须由另一名平台清算人员审核")
        return
    if identity["user_id"] == user.id or identity["side"] == proposal_identity(user, scope)["side"]:
        raise HTTPException(409, "调整提案必须由平台与原提供方对方确认")


def proposal_amounts(profit, values):
    kinds = ("platform", "provider", "service", "expert", "channel")
    rates = {kind: Decimal(str(values[kind + "_rate"])) for kind in kinds}
    total_rate = sum(rates.values())
    if total_rate <= 0:
        raise HTTPException(409, "提案分成比例无效")
    raw = {kind: profit * rates[kind] / total_rate for kind in kinds}
    amounts = {kind: amount.quantize(Decimal(".01"), rounding=ROUND_DOWN) for kind, amount in raw.items()}
    remainder = int((profit - sum(amounts.values())) * 100)
    for kind in sorted(kinds, key=lambda kind: raw[kind] - amounts[kind], reverse=True)[:remainder]:
        amounts[kind] += Decimal(".01")
    return amounts


def workflow_actions(ns, db, user, scope, settlement, batch, proposals):
    operator = scope is not None or user.platform_role in {"super_admin", "platform_operator", "finance_settlement"}
    mutable = operator and settlement.status not in {"locked", "paid", "superseded"} and (
        batch is None or batch.status in {"generated", "pending_confirm"})
    pending = any(p.status == "pending" for p in proposals)
    latest = proposals[0] if proposals else None
    accepted = latest is None or "_proposer" not in json.loads(latest.values_json or "{}") or latest.status == "accepted"
    actions = {"can_propose": bool(mutable and not pending),
        "can_lock": bool(mutable and settlement.status != "disputed" and not pending and accepted)}
    decisions = {}
    for proposal in proposals:
        allowed = mutable and settlement.status == "disputed" and proposal.status == "pending"
        if allowed:
            try:
                require_counterparty(ns, db, proposal, user, scope)
            except HTTPException:
                allowed = False
            original = json.loads(proposal.values_json or "{}").get("_provider_enterprise_id")
            if original and db.get(ns["Order"], settlement.order_id).provider_enterprise_id != original:
                allowed = False
        decisions[proposal.id] = {"can_decide": bool(allowed)}
    return actions, decisions
