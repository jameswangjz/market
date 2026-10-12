"""Monthly settlement integration for posted version-change adjustments."""
import json
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import Column, ForeignKey, String, UniqueConstraint, select


CENT = Decimal("0.01")
ACTIVE_BATCH = {"generated", "pending_confirm", "confirmed", "payment_processing", "partial_paid", "paid", "archived"}
FINAL_BATCH = {"confirmed", "payment_processing", "partial_paid", "paid", "archived"}


def install(ns):
    if "version_change_settlement" in ns:
        return ns["version_change_settlement"]
    Base = ns["Base"]

    class VersionChangeSettlementLink(Base):
        __tablename__ = "version_change_settlement_links"
        __table_args__ = (UniqueConstraint("adjustment_id", name="uq_vc_settlement_adjustment"),
                          UniqueConstraint("settlement_id", name="uq_vc_settlement_settlement"))
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        adjustment_id = Column(ForeignKey("version_change_adjustments.id"), nullable=False)
        settlement_id = Column(ForeignKey("settlements.id"), nullable=False)

    def fail(message):
        raise HTTPException(409, message)

    def is_funding_order(order):
        return bool(order and getattr(order, "business_type", "") == "version_change_payment")

    def is_change_refund(db, refund):
        Fund = ns.get("VersionChangeFund")
        if Fund is None or refund is None:
            return False
        return db.scalar(select(Fund.refund_id).where(
            Fund.refund_id == refund.id).limit(1)) is not None

    def describe(db, settlement_id):
        link = db.scalar(select(VersionChangeSettlementLink).where(
            VersionChangeSettlementLink.settlement_id == settlement_id))
        if link is None:
            return None
        Adjustment = ns.get("VersionChangeAdjustment")
        if Adjustment is None:
            return None
        adjustment = db.get(Adjustment, link.adjustment_id)
        if adjustment is None:
            return None
        try:
            rule_version = json.loads(adjustment.rule_json or "{}").get("version", "")
        except (TypeError, ValueError):
            rule_version = ""
        version_id = (adjustment.original_version_id if adjustment.side == "old"
                      else adjustment.target_version_id)
        return {
            "kind": "version_change",
            "change_id": adjustment.change_id,
            "change_no": "VC-" + adjustment.change_id[:12].upper(),
            "adjustment_id": adjustment.id,
            "side": adjustment.side,
            "version_id": version_id,
            "rule_version": rule_version,
            "effective_at": adjustment.effective_at,
            "posted_at": adjustment.posted_at,
        }

    def include_into_batch(db, batch, body, actor):
        Adjustment = ns.get("VersionChangeAdjustment")
        if Adjustment is None or body.cycle != "monthly":
            return {"amount": Decimal("0.00"), "profit": Decimal("0.00"), "count": 0}
        stmt = select(Adjustment).where(Adjustment.status == "posted", Adjustment.posted_at.is_not(None))
        if body.period_start:
            stmt = stmt.where(Adjustment.posted_at >= body.period_start)
        if body.period_end:
            stmt = stmt.where(Adjustment.posted_at < body.period_end)
        if body.order_ids:
            stmt = stmt.where(Adjustment.source_order_id.in_(body.order_ids))
        rows = db.scalars(stmt.order_by(Adjustment.posted_at, Adjustment.change_id,
                                        Adjustment.source_order_id, Adjustment.side).with_for_update()).all()
        amount_total = Decimal("0.00")
        profit_total = Decimal("0.00")
        count = 0
        names = {"platform": "平台运营方", "provider": "数据/服务提供方",
                 "service": "数据服务方", "expert": "专家", "channel": "渠道"}
        for row in rows:
            link = db.scalar(select(VersionChangeSettlementLink).where(
                VersionChangeSettlementLink.adjustment_id == row.id))
            settlement = db.get(ns["Settlement"], link.settlement_id) if link else None
            reused = settlement is not None
            previous_status = settlement.status if settlement else "none"
            existing_lines = db.scalars(select(ns["SettlementLine"]).where(
                ns["SettlementLine"].settlement_id == settlement.id,
                ns["SettlementLine"].status != "superseded")).all() if settlement else []
            for line in existing_lines:
                other_batch = db.get(ns["SettlementBatch"], line.batch_id)
                if other_batch and other_batch.id != batch.id and other_batch.status in ACTIVE_BATCH:
                    fail("版本变更调整已属于其他有效清算批次")
                if other_batch and other_batch.status in FINAL_BATCH:
                    fail("已确认或付款的版本变更清算不可重写")

            try:
                snapshot = json.loads(row.rule_json)
                rates = snapshot["rates"]
                values = {key: Decimal(str(rates[key])) for key in (
                    "platform_rate", "provider_rate", "service_rate", "expert_rate", "channel_rate")}
                revenue, cost, profit = (Decimal(str(value)) for value in (row.revenue, row.cost, row.profit))
                if (any(not value.is_finite() or value.quantize(CENT) != value for value in (revenue, cost, profit))
                        or any(not rate.is_finite() or rate < 0 or rate > 100 for rate in values.values())
                        or sum(values.values()) > 100 or revenue - cost != profit):
                    raise ValueError("profit mismatch")
                shares = {}
                for participant, rate_key in (("platform", "platform_rate"), ("provider", "provider_rate"),
                                               ("service", "service_rate"), ("expert", "expert_rate"),
                                               ("channel", "channel_rate")):
                    shares[participant] = (profit * values[rate_key] / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
                shares["provider"] += profit - sum(shares.values())
            except (KeyError, TypeError, ValueError, InvalidOperation):
                fail("版本变更冻结清算规则或金额无效")

            if settlement is None:
                settlement = ns["Settlement"](
                    settlement_no="SET-VC-" + secrets.token_hex(6).upper(), order_id=row.source_order_id,
                    gross_amount=revenue, refund_amount=Decimal("0.00"), net_amount=revenue,
                    cost_amount=cost, profit_amount=profit, refund_recovery=Decimal("0.00"),
                    platform_fee=shares["platform"], provider_share=shares["provider"],
                    service_share=shares["service"], expert_fee=shares["expert"],
                    channel_fee=shares["channel"], tax_amount=Decimal("0.00"), status="pending")
                db.add(settlement)
                db.flush()
                db.add(VersionChangeSettlementLink(adjustment_id=row.id, settlement_id=settlement.id))
            else:
                settlement.gross_amount, settlement.net_amount = revenue, revenue
                settlement.cost_amount, settlement.profit_amount = cost, profit
                settlement.platform_fee, settlement.provider_share = shares["platform"], shares["provider"]
                settlement.service_share, settlement.expert_fee = shares["service"], shares["expert"]
                settlement.channel_fee, settlement.status = shares["channel"], "pending"

            for line in db.scalars(select(ns["SettlementLine"]).where(
                    ns["SettlementLine"].settlement_id == settlement.id,
                    ns["SettlementLine"].status != "superseded")).all():
                line.status = "superseded"
            order = db.get(ns["Order"], row.source_order_id)
            db.add_all(ns["SettlementLine"](batch_id=batch.id, settlement_id=settlement.id,
                participant_type=participant, participant_id=(order.provider_enterprise_id or "") if participant == "provider" and order else "",
                participant_name=names[participant], amount=shares[participant])
                for participant in names)
            ns["audit"](db, actor, "settle_version_change_adjustment", "settlement",
                settlement.settlement_no, "版本变更调整纳入月度清算", category="settlement_adjustment",
                business_domain="settlement", order_id=row.source_order_id, batch_no=batch.batch_no,
                rule_version=str(snapshot.get("version", "")),
                before={"status": previous_status},
                after={"status": settlement.status, "created_or_reused": "reused" if reused else "created",
                    "change_id": row.change_id, "adjustment_id": row.id, "side": row.side,
                    "rule_id": snapshot.get("id", ""), "rule_version": snapshot.get("version", ""),
                    "rule_mode": snapshot.get("mode", ""), "gross_amount": str(revenue),
                    "cost_amount": str(cost), "net_amount": str(revenue), "profit_amount": str(profit),
                    "platform_fee": str(shares["platform"]), "provider_share": str(shares["provider"]),
                    "service_share": str(shares["service"]), "expert_fee": str(shares["expert"]),
                    "channel_fee": str(shares["channel"])})
            amount_total += revenue
            profit_total += profit
            count += 1
        db.flush()
        return {"amount": amount_total, "profit": profit_total, "count": count}

    hooks = {"Model": VersionChangeSettlementLink, "include_into_batch": include_into_batch,
             "describe": describe,
             "is_funding_order": is_funding_order, "is_change_refund": is_change_refund}
    ns["VersionChangeSettlementLink"] = VersionChangeSettlementLink
    ns["version_change_settlement"] = hooks
    return hooks
