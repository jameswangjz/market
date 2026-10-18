"""Read-only settlement reporting and immutable, explicitly draft annual snapshots.

Call install(globals()) after the host models and authentication dependencies exist,
before its normal metadata migration/create step. No startup jobs or data repair.
"""
import csv
import hashlib
import io
import json
import re
import secrets
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import Column, ForeignKey, Integer, String, Text, UniqueConstraint, event, func, select, text as sql_text
from sqlalchemy.exc import IntegrityError

SHANGHAI = ZoneInfo("Asia/Shanghai")
EXPORT_LIMIT = 500 * 1024 * 1024
MONEY = ("gross_amount", "cost_amount", "profit_amount", "platform_fee",
         "provider_share", "service_share", "expert_fee", "channel_fee",
         "provider_total_receivable", "net_amount", "refund_amount")
ROLES = dict(platform="platform_fee", provider="provider_share", service="service_share",
             expert="expert_fee", channel="channel_fee")
READ_ROLES = {"super_admin", "platform_operator", "finance_settlement", "security_compliance"}
WRITE_ROLES = READ_ROLES - {"security_compliance"}
FUNDS = ("payable_amount", "paid_amount", "payable_offset_amount", "unpaid_amount",
         "receivable_amount", "recovered_amount", "receivable_offset_amount", "unrecovered_amount", "internal_profit")
BRIDGE = ("opening_payable", "payable_added", "paid", "payable_offset", "closing_payable",
          "opening_receivable", "receivable_added", "recovered", "receivable_offset", "closing_receivable",
          "opening_internal_profit", "internal_profit_added", "internal_absorption", "closing_internal_profit")
HEADERS = dict(zip(MONEY, ("清算收入", "供货成本", "可分配利润", "平台分成", "提供方分成",
    "服务方分成", "专家分成", "渠道分成", "提供方合计应收", "净清算额", "退款冲回")))
HEADERS.update(id="内部关联ID", settlement_no="清算单号", order_no="订单号", order_id="订单关联ID",
    product_id="产品关联ID", product_name="产品名称", provider_id="提供方关联ID", provider_name="提供方名称",
    product_version_id="版本关联ID", product_version_name="版本名称", month="财务月份",
    status="清算状态", source_kind="经济记录类型", refund_id="退款关联ID", change_id="变更关联ID",
    batch_id="批次关联ID", batch_no="批次号", created_at="生成时间", financial_at="归属时间",
    participant_type="分成类别", participant_id="收益主体关联ID", participant_name="收益主体名称",
    subject_unassigned="主体未配置", amount="分成金额", income_order_count="收入订单数",
    refund_count="退款笔数", adjustment_count="调整笔数", settlement_count="清算记录数",
    order_count="关联订单数", metric="排名指标", rank="名次", label="名称", basis="排名口径")
HEADERS.update(profit_share="利润分成", total_receivable="成本加分成应收", provisional="暂估未冻结",
               subject_type="主体类型", subject_source="主体依据", baseline_hash="最终基线校验值")
HEADERS.update(position_id="资金权益关联ID", position_count="资金权益数", direction="资金方向",
               currency="币种", funds_known="资金余额已核实", blocked_reason="暂挂原因")
HEADERS.update(loss_amount="承担损失金额", position_kind="权益类型")
HEADERS.update(cost_exception_id="成本例外关联ID", liability_source="损失依据类型", loss_label="损失说明")
HEADERS.update(buyer_enterprise_id="买方企业关联ID", buyer_name="买方名称", seller_enterprise_id="卖方企业关联ID",
    seller_name="卖方名称", segment_id="经济段关联ID", segment_basis="经济段依据", segment_start="经济段起点",
    segment_end="经济段终点", segment_side="经济段方向", baseline_settlement_id="最终基线关联ID",
    baseline_created_at="最终基线冻结时间", baseline_version="最终基线版本摘要", reference_settlement_id="原清算关联ID",
    reference_settlement_no="原清算单号", reference_baseline_hash="原最终基线摘要", rate_basis="五方比例依据",
    rule_version="冻结规则版本", rule_id="冻结规则关联ID")
HEADERS.update({role + "_rate": label for role, label in zip(ROLES, ("平台比例", "提供方比例", "服务方比例", "专家比例", "渠道比例"))})
HEADERS.update(dict(zip(BRIDGE, ("期初未付", "本期新增应付", "本期付款登记", "本期应付抵扣", "期末未付",
    "期初待追回", "本期新增应追回", "本期回款登记", "本期应追回抵扣", "期末待追回",
    "期初平台内部收益", "本期平台内部收益", "本期平台补差吸收", "期末平台内部收益"))))
HEADERS.update(funding_absorption_loss="平台补差吸收损失", baseline_total_receivable="基线应收合计",
               net_total_receivable="补差后净应收", supplementary_funding_json="独立补差来源承担及证据")
HEADERS.update(dict(zip(FUNDS, ("已知应付", "已确认付款登记", "应付已抵扣", "未付余额", "已知应追回",
    "已确认回款登记", "应追回已抵扣", "待追回余额", "平台内部收益"))))


class AnnualBody(BaseModel):
    year: int = Field(ge=1900, le=9998)
    request_id: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=4000)


class FinalizeBody(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    snapshot_digest: str = Field(min_length=64, max_length=64)
    reconciliation_reference: str = Field(min_length=1, max_length=240)
    note: str = Field(min_length=1, max_length=4000)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decimal(value):
    return Decimal(str(value or 0))


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def aggregate(rows):
    result = {key: format(sum((decimal(row[key]) for row in rows), Decimal(0)), ".2f") for key in MONEY}
    result["funding_absorption_loss"] = format(sum((decimal(r.get("funding_absorption_loss")) for r in rows), Decimal(0)), ".2f")
    for suffix, predicate in (("positive", lambda n: n > 0), ("reversal", lambda n: n < 0)):
        result[suffix] = {key: format(sum((decimal(r[key]) for r in rows if predicate(decimal(r[key]))), Decimal(0)), ".2f") for key in MONEY}
    result.update(income_order_count=len({r["order_id"] for r in rows if r["source_kind"] == "original" and decimal(r["gross_amount"]) > 0}),
        refund_count=len({r["refund_id"] for r in rows if r["source_kind"] == "refund" and r["refund_id"]}),
        adjustment_count=len({r["change_id"] for r in rows if r["source_kind"] == "version_change" and r["change_id"]}),
        settlement_count=len(rows), order_count=len({r["order_id"] for r in rows}))
    return result


def fund_totals(rows):
    known_totals = {key: format(sum((decimal(r["funds"]["known_totals"][key]) for r in rows), Decimal(0)), ".2f") for key in FUNDS}
    unknown = sum(not r["funds"]["known"] for r in rows)
    return dict(known=not bool(unknown), unknown_settlement_count=unknown, known_totals=known_totals,
                **{key: None if unknown else value for key, value in known_totals.items()})


def data_digest(report):
    # Cutoff is metadata; every economic row and its evidence remain in this digest.
    scope = {key: value for key, value in report["scope"].items() if key != "cutoff"}
    stable = dict(report, scope=scope)
    if "balance_bridge" in stable:
        stable["balance_bridge"] = {k: v for k, v in stable["balance_bridge"].items() if k != "calculated_through"}
    return hashlib.sha256(canonical(stable).encode()).hexdigest()


def unresolved(report):
    reasons = []
    if not report["items"]:
        reasons.append("no_settlements")
    if report["scope"]["includes_unconfirmed"]:
        reasons.append("unconfirmed_settlements")
    if report["scope"].get("includes_provisional", True):
        reasons.append("final_baseline_missing")
    if not report["summary"].get("funds", {}).get("known", False):
        reasons.append("fund_balances_unknown")
    if any(p["subject_unassigned"] and (decimal(p.get("cost_amount")) or decimal(p.get("profit_share", p.get("amount"))) or decimal(p.get("loss_amount"))) for p in report["participants"]):
        reasons.append("subjects_unknown")
    if any(decimal(r.get("funds", {}).get("known_totals", {}).get(key)) for r in report["items"] for key in ("unpaid_amount", "unrecovered_amount")):
        reasons.append("open_fund_balances")
    if any(r.get("funds", {}).get("issues", ["finance_evidence_missing"]) for r in report["items"]):
        reasons.append("fund_evidence_incomplete")
    bridge = report.get("balance_bridge", {})
    if not bridge.get("known", False):
        reasons.append("historical_balance_unknown")
    elif any(decimal(bridge[key]) for key in ("closing_payable", "closing_receivable")):
        reasons.append("historical_balances_open")
    if not bridge.get("period_complete", False):
        reasons.append("annual_period_not_complete")
    return reasons


def csv_response(report, kind):
    if kind not in {"summary", "details", "products", "providers", "participants", "refunds", "rankings"}:
        raise HTTPException(422, "invalid_export_kind")
    summary = dict(report["summary"], **report["summary"].get("funds", {}))
    summary.update({key: report["balance_bridge"][key] for key in BRIDGE} if "balance_bridge" in report else {})
    rows = ([summary] if kind == "summary" else
            [r for r in report["items"] if r["source_kind"] == "refund"] if kind == "refunds" else
            report["items" if kind == "details" else kind])
    columns = list(dict.fromkeys(key for row in rows for key in row if not isinstance(row[key], (dict, list))))
    if not columns:
        columns = ["settlement_no"] if kind in {"details", "refunds"} else ["label"]
    output = io.BytesIO()
    output.write(b"\xef\xbb\xbf")
    def write(values):
        line = io.StringIO(newline="")
        csv.writer(line).writerow(values)
        encoded = line.getvalue().encode("utf-8")
        if output.tell() + len(encoded) > EXPORT_LIMIT:
            raise HTTPException(413, "export_exceeds_500mb")
        output.write(encoded)
    def safe(value, key):
        if value is None:
            return "未知"
        text = str(value)
        # Decimal amounts are server-generated numeric literals, never user text.
        numeric = key in MONEY or key in FUNDS or key in BRIDGE or key in {"amount", "profit_share", "total_receivable", "loss_amount", "funding_absorption_loss", "baseline_total_receivable", "net_total_receivable"}
        if not numeric and (text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n"))):
            return "'" + text
        return text
    write([HEADERS.get(key, "业务字段：" + key) for key in columns])
    for row in rows:
        write([safe(row.get(key, ""), key) for key in columns])
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="settlement-{kind}.csv"'})


def install(ns):
    if "settlement_reporting" in ns:
        return ns["settlement_reporting"]
    from .console_business_scope import resolve_business_scope

    class AnnualReport(ns["Base"]):
        __tablename__ = "settlement_reporting_annual_reports"
        __table_args__ = (UniqueConstraint("scope_key", "request_id"),
                          UniqueConstraint("scope_key", "year", "version"))
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        year = Column(Integer, nullable=False)
        version = Column(Integer, nullable=False)
        scope_key = Column(String(100), nullable=False)
        request_id = Column(String(120), nullable=False)
        actor_id = Column(String(36), nullable=False)
        cutoff = Column(String(40), nullable=False)
        status = Column(String(30), nullable=False, default="draft")
        note = Column(Text, nullable=False)
        report_json = Column(Text, nullable=False)
        digest = Column(String(64), nullable=False)

    class Finalization(ns["Base"]):
        __tablename__ = "settlement_reporting_finalizations"
        report_id = Column(ForeignKey(AnnualReport.id), primary_key=True)
        request_id = Column(String(120), nullable=False)
        reviewer_id = Column(String(36), nullable=False)
        created_at = Column(String(40), nullable=False)
        snapshot_digest = Column(String(64), nullable=False)
        evidence_json = Column(Text, nullable=False)
        digest = Column(String(64), nullable=False)

    def immutable(*args):
        raise ValueError("annual_report_is_immutable")
    event.listen(AnnualReport, "before_update", immutable)
    event.listen(AnnualReport, "before_delete", immutable)
    event.listen(Finalization, "before_update", immutable)
    event.listen(Finalization, "before_delete", immutable)
    S, O, E, L, B = (ns[key] for key in ("Settlement", "Order", "Enterprise", "SettlementLine", "SettlementBatch"))

    def funding_projection(db, baseline, proof):
        """Validate independent funding facts without editing frozen entitlements."""
        if proof is None:
            return dict(valid=False, status="uncovered", kind=None, amount="0.00")
        finance = ns["settlement_finance"]
        try:
            def decoded(record, raw_field, hash_field):
                raw = getattr(record, raw_field)
                if hashlib.sha256(raw.encode()).hexdigest() != getattr(record, hash_field):
                    raise ValueError()
                return json.loads(raw)
            snapshot = decoded(baseline, "snapshot_json", "snapshot_hash")
            evidence = decoded(proof, "evidence_json", "evidence_hash")
            request = db.get(finance["FundingRequest"], proof.request_id)
            decision = db.get(finance["FundingDecision"], proof.request_id)
            payload = decoded(request, "payload_json", "payload_hash")
            amount = decimal(payload["amount"])
            if (request.settlement_id != baseline.settlement_id or proof.settlement_id != baseline.settlement_id
                    or payload["baseline_hash"] != baseline.snapshot_hash or payload["currency"] != baseline.currency
                    or payload["settlement_id"] != baseline.settlement_id or decimal(payload["actual_receipts"]) != decimal(snapshot["actual_receipts"])
                    or amount <= 0 or amount != baseline.revenue - decimal(snapshot["actual_receipts"])
                    or decision.decision != "approved" or decision.reviewer_id == request.creator_id
                    or proof.verifier_id == request.creator_id or evidence["request_id"] != request.id
                    or evidence["reviewer_id"] != decision.reviewer_id or evidence["verifier_id"] != proof.verifier_id
                    or any(evidence.get(key) != value for key, value in payload.items())):
                raise ValueError()
            result = dict(valid=True, status="covered", kind=payload["kind"], amount=format(amount, ".2f"), currency=baseline.currency,
                request_id=request.id, bearer=payload["bearer"], evidence_hash=proof.evidence_hash,
                source=payload["kind"], source_key=proof.source_key,
                evidence=dict(request_hash=request.payload_hash, proof_hash=proof.evidence_hash, baseline_hash=baseline.snapshot_hash),
                bank_receipt_verified=False, effective_at=None, absorption_allocations={})
            if payload["kind"] == "platform_absorption":
                if (payload["bearer"]["participant_type"], payload["bearer"]["participant_id"]) != ("platform", "platform"):
                    raise ValueError()
                absorption = db.get(finance["FundingAbsorption"], request.id)
                fact = decoded(absorption, "evidence_json", "evidence_hash")
                tx = db.get(finance["Transaction"], fact["transaction_id"])
                confirmation = db.get(finance["Confirmation"], tx.id)
                if (absorption.settlement_id != baseline.settlement_id or absorption.amount != amount
                        or absorption.currency != baseline.currency or fact["baseline_hash"] != baseline.snapshot_hash
                        or fact["request_id"] != request.id or fact["settlement_id"] != baseline.settlement_id
                        or decimal(fact["amount"]) != amount or fact["currency"] != baseline.currency or decimal(fact["cash_amount"]) != 0
                        or (fact["participant_type"], fact["participant_id"]) != ("platform", "platform")
                        or fact["creator_id"] != request.creator_id or fact["reviewer_id"] != decision.reviewer_id
                        or fact["evidence_refs"] != payload["evidence_refs"]
                        or fact["verifier_id"] != proof.verifier_id or evidence["absorption_request_id"] != request.id
                        or proof.source_key != "platform:" + baseline.settlement_id
                        or evidence["transaction_id"] != tx.id or tx.kind != "internal_absorption" or tx.mode != "manual"
                        or tx.reference != "funding:" + request.id or tx.creator_id != request.creator_id
                        or confirmation.reviewer_id != proof.verifier_id or not isinstance(confirmation.created_at, datetime)
                        or not isinstance(tx.created_at, datetime) or not isinstance(baseline.created_at, datetime)
                        or utc(confirmation.created_at) < max(utc(tx.created_at), utc(baseline.created_at))):
                    raise ValueError()
                declared = {a["position_id"]: decimal(a["amount"]) for a in fact["allocations"]}
                tx_allocations = json.loads(tx.payload_json)["allocations"]
                actual = db.scalars(select(finance["Allocation"]).where(finance["Allocation"].transaction_id == tx.id)).all()
                if (not declared or len(declared) != len(fact["allocations"]) or len(tx_allocations) != len(declared) or sum(declared.values()) != amount
                        or {a["position_id"]: decimal(a["amount"]) for a in tx_allocations} != declared
                        or {a.position_id: decimal(a.amount) for a in actual} != declared or len(actual) != len(declared)):
                    raise ValueError()
                for position_id, value in declared.items():
                    p = db.get(finance["Position"], position_id)
                    if (p is None or p.settlement_id != baseline.settlement_id or value <= 0 or value > p.amount
                            or (p.kind, p.share_kind, p.direction, p.participant_type, p.participant_id, p.currency) !=
                            ("profit", "platform_fee", "internal", "platform", "platform", baseline.currency)
                            or not any(e["kind"] == p.kind and e["share_kind"] == p.share_kind and
                                e["participant_type"] == p.participant_type and e["participant_id"] == p.participant_id and
                                decimal(e["amount"]) == p.amount for e in snapshot["entries"])):
                        raise ValueError()
                result.update(effective_at=utc(confirmation.created_at).isoformat(), transaction_id=tx.id,
                    absorption_allocations=declared, absorption_evidence_hash=absorption.evidence_hash)
            elif payload["kind"] == "external_receipt":
                receipt = evidence["receipt"]
                if (receipt["status"] != "verified" or decimal(receipt["amount"]) != amount or receipt["currency"] != baseline.currency
                        or receipt["payer_type"] != payload["bearer"]["participant_type"] or receipt["payer_id"] != payload["bearer"]["participant_id"]
                        or proof.source_key != "cash:" + receipt["source_key"] or len(receipt["evidence_hash"]) != 64):
                    raise ValueError()
            else:
                raise ValueError()
            return result
        except (AttributeError, KeyError, ValueError, TypeError, ArithmeticError):
            return dict(valid=False, status="unproven", kind=None, amount=None, reason="funding_evidence_inconsistent")

    def cash_block(position, funding, *, as_of=None):
        if position.blocked_reason != "unfunded_settlement_adjustment":
            return position.blocked_reason
        if not funding.get("valid"):
            return position.blocked_reason
        if funding.get("effective_at") and datetime.fromisoformat(funding["effective_at"]) >= datetime.now(timezone.utc):
            return "funding_effective_time_unproven"
        if as_of is not None:
            effective = funding.get("effective_at")
            if not effective or datetime.fromisoformat(effective) >= as_of:
                return "funding_effective_time_unproven"
        return ""

    def scoped_funding(db, ids):
        finance = ns.get("settlement_finance", {})
        Proof, Baseline = finance.get("FundingProof"), finance.get("Baseline")
        if Proof is None or Baseline is None:
            return {}
        return {b.settlement_id: funding_projection(db, b, proof) for b, proof in db.execute(
            select(Baseline, Proof).join(Proof, Proof.settlement_id == Baseline.settlement_id).where(Baseline.settlement_id.in_(ids)))}

    def subject_rows(db, row, order, linked, batch, baseline, positions):
        provisional = baseline is None
        source = "finance_baseline" if baseline is not None else "provisional"
        result = {}

        def add(role, entry, amount, *, cost=False, loss=False):
            pid = entry.get("participant_id") or ""
            subject_type = entry.get("subject_type") or entry.get("participant_type") or role
            missing = (not pid or pid == "unassigned" or entry.get("unassigned") or
                       entry.get("blocked_reason") in {"missing_subject_mapping", "participant_unassigned"})
            if missing:
                pid = ""
            key = (role, subject_type, pid)
            target = result.setdefault(key, dict(participant_type=role, subject_type=subject_type,
                participant_id=pid, participant_name=entry.get("participant_name", "") if not missing else "未分配主体",
                subject_unassigned=bool(missing), cost_amount="0.00", profit_share="0.00", loss_amount="0.00",
                total_receivable="0.00", amount="0.00", provisional=provisional, subject_source=source))
            field = "cost_amount" if cost else "loss_amount" if loss else "profit_share"
            target[field] = format(decimal(target[field]) + amount, ".2f")
            net_share = decimal(target["profit_share"]) - decimal(target["loss_amount"])
            target["total_receivable"] = format(decimal(target["cost_amount"]) + net_share, ".2f")
            # Legacy amount is net role allocation; profit_share excludes separate losses.
            target["amount"] = format(net_share, ".2f")

        if baseline is not None:
            try:
                if hashlib.sha256(baseline.snapshot_json.encode()).hexdigest() != baseline.snapshot_hash:
                    raise ValueError()
                snapshot = json.loads(baseline.snapshot_json)
                entries = [dict(kind=p.kind, share_kind=p.share_kind, participant_type=p.participant_type,
                    participant_id=p.participant_id, participant_name=p.participant_name,
                    blocked_reason=p.blocked_reason, amount=str(p.amount)) for p in positions]
                if not entries:
                    entries = snapshot["entries"]
                fields = {field: role for role, field in ROLES.items()}
                default_shares = (snapshot.get("cost_exception_application") or {}).get("default_basis", {}).get("shares")
                for entry in entries:
                    cost = entry["kind"] == "cost"
                    loss = entry["kind"] == "loss"
                    if entry["kind"] not in {"cost", "profit", "loss"}:
                        raise ValueError()
                    role = "provider" if cost else fields[entry["share_kind"]]
                    value = decimal(entry["amount"])
                    signed_total = decimal(row["cost_amount"] if cost else
                        default_shares[ROLES[role]] if default_shares else row[ROLES[role]])
                    add(role, entry, abs(value) if loss else -abs(value) if signed_total < 0 else abs(value), cost=cost, loss=loss)
                if sum((decimal(p["cost_amount"]) for p in result.values()), Decimal(0)) != decimal(row["cost_amount"]):
                    raise ValueError()
                for role, field in ROLES.items():
                    role_rows = [p for p in result.values() if p["participant_type"] == role]
                    ordinary = sum((decimal(p["profit_share"]) for p in role_rows), Decimal(0))
                    losses = sum((decimal(p["loss_amount"]) for p in role_rows), Decimal(0))
                    if ordinary - losses != decimal(row[field]) or default_shares and ordinary != decimal(default_shares[field]):
                        raise ValueError()
            except (ValueError, KeyError, TypeError, ArithmeticError):
                raise HTTPException(409, "finance_baseline_subjects_inconsistent") from None
            return list(result.values())

        try:
            economic = json.loads(order.economic_snapshot_json or "{}")
        except (ValueError, TypeError):
            raise HTTPException(409, "invalid_economic_snapshot") from None
        allocations = ns.get("settlement_parties", {}).get("allocations")
        frozen = economic.get("settlement_parties")
        source = "parties_frozen_configuration" if frozen and callable(allocations) else "provisional"
        for role, field in ROLES.items():
            expected = decimal(row[field])
            if role == "provider":
                entries = [dict(participant_id=order.provider_enterprise_id, participant_name=row["provider_name"],
                                participant_type="enterprise", amount=str(expected))]
            elif frozen and callable(allocations):
                entries = allocations(db, order, role, expected)
            else:
                # Only explicit legacy identities are usable; generic role labels are not subjects.
                entries = [dict(participant_id=l.participant_id, participant_name=l.participant_name,
                                participant_type=role, amount=str(l.amount)) for l, b in linked
                           if l.participant_type == role and l.participant_id and
                           l.participant_id != "unassigned" and (batch is None or b.id == batch.id)]
                if not entries and callable(allocations):
                    entries = allocations(db, order, role, expected)
            allocated = Decimal(0)
            for entry in entries:
                value = decimal(entry["amount"])
                add(role, entry, value)
                allocated += value
            if allocated != expected or not entries:
                add(role, dict(participant_type=role), expected - allocated)
        add("provider", dict(participant_id=order.provider_enterprise_id, participant_name=row["provider_name"],
                             participant_type="enterprise"), decimal(row["cost_amount"]), cost=True)
        return list(result.values())

    def detail_fields(db, order, settlement, baseline, snapshot, adjustment=None):
        frozen = snapshot.get("economic_snapshot", {}) if baseline else json.loads(order.economic_snapshot_json or "{}")
        rule = frozen.get("settlement_rule", {})
        profit = decimal(snapshot.get("profit", settlement.profit_amount))
        shares = snapshot.get("shares", {field: str(getattr(settlement, field)) for field in ROLES.values()})
        def rates(values, base):
            return {role: format(decimal(values[field]) / base * 100, ".4f") if base else None for role, field in ROLES.items()}
        final_rates = rates(shares, profit)
        ordinary = (snapshot.get("cost_exception_application") or {}).get("default_basis")
        ordinary_rates = rates(ordinary["shares"], decimal(ordinary["profit_amount"])) if ordinary else final_rates
        context = snapshot.get("segment_context") or {}
        reference = snapshot.get("reference_settlement_id") or settlement.reference_settlement_id or context.get("source_settlement_id", "")
        source, source_baseline = None, None
        if reference:
            source = db.get(S, reference)
            model = ns.get("settlement_finance", {}).get("Baseline")
            source_baseline = db.get(model, reference) if model is not None else None
            if source is not None:
                source_order = db.get(O, source.order_id)
                if source_order is None or source_order.provider_enterprise_id != order.provider_enterprise_id:
                    raise HTTPException(409, "baseline_reference_scope_mismatch")
        buyer = db.get(E, order.buyer_enterprise_id) if order.buyer_enterprise_id else None
        seller = db.get(E, order.provider_enterprise_id)
        result = dict(buyer_enterprise_id=order.buyer_enterprise_id, buyer_name=buyer.name if buyer else order.buyer_name,
            seller_enterprise_id=order.provider_enterprise_id, seller_name=seller.name if seller else "",
            baseline_settlement_id=baseline.settlement_id if baseline else None,
            baseline_created_at=utc(baseline.created_at).isoformat() if baseline and baseline.created_at else None,
            baseline_version=baseline.snapshot_hash if baseline else None, reference_settlement_id=reference,
            reference_settlement_no=source.settlement_no if source else None,
            reference_baseline_hash=source_baseline.snapshot_hash if source_baseline else None,
            share_rates=final_rates, ordinary_share_rates=ordinary_rates,
            configured_share_rates={role: str(rule.get("rates", {})[role + "_rate"]) if role + "_rate" in rule.get("rates", {}) else None for role in ROLES},
            rate_basis="final_baseline_amount_ratio" if baseline else "provisional_amount_ratio",
            rule_id=rule.get("id"), rule_version=rule.get("version"),
            segment_id=None, segment_start=None, segment_end=None, segment_side=None, segment_basis="unknown")
        result.update({role + "_rate": value for role, value in final_rates.items()})
        if context:
            result.update(segment_id=context.get("window_id"), segment_side=context.get("side"), segment_basis="final_baseline_segment_context")
            Window = ns.get("refund_segment_store", {}).get("Window")
            window = db.get(Window, context["window_id"]) if Window is not None and context.get("window_id") else None
            if window is not None:
                result.update(segment_start=utc(window.starts_at).isoformat(), segment_end=utc(window.ends_at).isoformat())
        elif settlement.is_refund or decimal(settlement.gross_amount) < 0:
            Manifest = ns.get("refund_settlement", {}).get("MultiSourceManifest")
            manifest = db.get(Manifest, settlement.id) if Manifest is not None else None
            result["economic_segments"] = []
            if manifest is not None:
                if hashlib.sha256(manifest.payload_json.encode()).hexdigest() != manifest.payload_hash:
                    raise HTTPException(409, "refund_manifest_digest_mismatch")
                payload = json.loads(manifest.payload_json)
                if payload.get("settlement_id") != settlement.id or payload.get("order_id") != order.id:
                    raise HTTPException(409, "refund_manifest_scope_mismatch")
                for entry in payload.get("references", []):
                    original = db.get(ns["settlement_finance"]["Baseline"], entry["reference_settlement_id"])
                    if (original is None or original.provider_enterprise_id != order.provider_enterprise_id
                            or original.order_id != order.id or original.snapshot_hash != entry.get("baseline_hash")):
                        raise HTTPException(409, "refund_manifest_baseline_mismatch")
                    original_order = db.get(O, original.order_id)
                    original_settlement = db.get(S, original.settlement_id)
                    if (original_order is None or original_order.provider_enterprise_id != order.provider_enterprise_id
                            or original_settlement is None or original_settlement.order_id != original.order_id
                            or entry.get("source_order_id") not in (None, original.order_id)
                            or hashlib.sha256(original.snapshot_json.encode()).hexdigest() != original.snapshot_hash):
                        raise HTTPException(409, "refund_manifest_baseline_mismatch")
                    original_snapshot = json.loads(original.snapshot_json)
                    batch_nos = sorted(set(db.scalars(select(B.batch_no).join(L, L.batch_id == B.id).where(
                        L.settlement_id == original.settlement_id, L.status != "superseded", B.status != "superseded"))))
                    segment = {key: entry.get(key) for key in (
                        "segment_id", "source_order_id", "reference_settlement_id", "baseline_hash", "term_id", "revision_id",
                        "change_id", "receipt_id", "receipt_hash", "gross_amount", "cost_amount", "profit_amount", "shares")}
                    segment.update(source_order_id=original.order_id, source_order_no=original_order.order_no,
                        reference_settlement_no=original_settlement.settlement_no,
                        reference_batch_no=batch_nos[0] if len(batch_nos) == 1 else None, reference_batch_nos=batch_nos,
                        share_rates=rates(original_snapshot["shares"], decimal(original_snapshot["profit"])),
                        baseline_created_at=utc(original.created_at).isoformat() if original.created_at else None)
                    result["economic_segments"].append(segment)
                result.update(segment_basis="immutable_multi_source_manifest", segment_manifest_hash=manifest.payload_hash)
                result["economic_segments_json"] = canonical(result["economic_segments"])
            elif reference:
                result.update(segment_basis="original_baseline_reference_only")
        elif adjustment is not None:
            result.update(segment_id=adjustment.id, segment_side=adjustment.side, segment_basis="posted_version_adjustment",
                          segment_start=utc(adjustment.effective_at).isoformat() if adjustment.effective_at else None)
        else:
            Term = ns.get("subscription_terms", {}).get("Term")
            term = db.scalar(select(Term).where(Term.order_id == order.id)) if Term is not None else None
            if term is not None:
                result.update(segment_id=term.id, segment_start=utc(term.starts_at).isoformat(),
                              segment_end=utc(term.ends_at).isoformat(), segment_basis="subscription_term")
            elif order.delivery_method_snapshot in {"file", "training", "consulting", "custom"}:
                result.update(segment_id=order.id, segment_basis="one_time_order")
        return result

    def balance_bridge(db, scope, f):
        """Reconstruct ledger balances, never treating current balance as history."""
        finance = ns.get("settlement_finance", {})
        start = datetime.combine(date.fromisoformat(f["start"]), time.min, SHANGHAI).astimezone(timezone.utc)
        end = datetime.combine(date.fromisoformat(f["end"]) + timedelta(days=1), time.min, SHANGHAI).astimezone(timezone.utc)
        cutoff = datetime.now(timezone.utc)
        through = min(end, cutoff)
        totals = {key: Decimal(0) for key in BRIDGE}
        issues, facts = set(), []
        result = dict(business_period=dict(start=f["start"], end=f["end"]), funds_period=dict(
            start_inclusive=start.isoformat(), end_exclusive=end.isoformat(), timezone="Asia/Shanghai"),
            calculated_through=through.isoformat(), period_complete=end <= cutoff,
            recognition_basis="immutable_baseline_created_at", movement_basis="immutable_confirmation_created_at",
            bank_occurrence_time_known=False, currency="CNY", scope_basis="authorized_current_business_filters_all_history")
        result["participant_type_basis"] = "settlement_selection_full_five_parties_and_provider_cost" if scope["kind"] != "beneficiary" else "selected_registry_identity_positions_only"
        if start >= cutoff:
            return dict(result, known=False, period_status="not_started", unknown_reasons=["funds_period_not_started"],
                        **{key: None for key in BRIDGE})
        result["period_status"] = "complete" if end <= cutoff else "in_progress"
        if not all(finance.get(key) is not None for key in ("Baseline", "Position", "Allocation", "Confirmation", "Transaction")):
            return dict(result, known=False, unknown_reasons=["finance_history_unavailable"], **{key: None for key in BRIDGE})
        Baseline, P, A, C, T = (finance[key] for key in ("Baseline", "Position", "Allocation", "Confirmation", "Transaction"))
        query = select(S, O, Baseline, E.name).join(O, O.id == S.order_id).outerjoin(
            Baseline, Baseline.settlement_id == S.id).outerjoin(E, E.id == O.provider_enterprise_id)
        provider = scope.get("provider_id") or f["provider_id"]
        if provider:
            query = query.where(O.provider_enterprise_id == provider)
        if f["product_id"]:
            query = query.where(O.product_id == f["product_id"])
        if f["status"]:
            query = query.where(S.status == f["status"])
        else:
            query = query.where((S.status != "superseded") | (Baseline.settlement_id.is_not(None)))
        owned_positions = select(P)
        if scope["kind"] == "beneficiary":
            owned_positions = owned_positions.where(P.participant_id == scope["participant_id"], P.participant_type == scope["participant_type"])
            query = query.where(S.id.in_(owned_positions.with_only_columns(P.settlement_id)))
        ids = query.with_only_columns(S.id)
        owned_positions = owned_positions.where(P.settlement_id.in_(ids))
        positions, batches, events = defaultdict(list), defaultdict(list), defaultdict(list)
        for p in db.scalars(owned_positions.order_by(P.id)):
            positions[p.settlement_id].append(p)
        for sid, batch in db.execute(select(L.settlement_id, B).join(B, B.id == L.batch_id).where(
                L.settlement_id.in_(ids), L.status != "superseded", B.status != "superseded").order_by(B.created_at, B.id)):
            if batch.id not in {b.id for b in batches[sid]}:
                batches[sid].append(batch)
        for a, confirmation, tx in db.execute(select(A, C, T).outerjoin(C, C.transaction_id == A.transaction_id).outerjoin(
                T, T.id == A.transaction_id).where(A.position_id.in_(owned_positions.with_only_columns(P.id))).order_by(A.id)):
            events[a.position_id].append((a, confirmation, tx))
        Link, Adjustment = ns.get("version_change_settlement", {}).get("Model"), ns.get("VersionChangeAdjustment")
        changes = dict(db.execute(select(Link.settlement_id, Adjustment.change_id).join(Adjustment,
            Adjustment.id == Link.adjustment_id).where(Link.settlement_id.in_(ids))).all()) if Link is not None and Adjustment is not None else {}
        funding_by_id = scoped_funding(db, ids)
        for settlement, order, baseline, provider_name in db.execute(query):
            linked = batches[settlement.id]
            if f["batch_id"] and not any(f["batch_id"] in (b.id, b.batch_no) for b in linked):
                continue
            source = "version_change" if settlement.id in changes else "refund" if settlement.is_refund or settlement.refund_id else "original"
            if f["source_kind"] and source != f["source_kind"] or source == "original" and order.business_type == "version_change_payment":
                continue
            if f["q"] and f["q"].casefold() not in " ".join((settlement.settlement_no, order.order_no if scope["kind"] != "beneficiary" else "",
                    order.product_name, order.product_version_name if scope["kind"] != "beneficiary" else "", provider_name or "",
                    " ".join(b.batch_no for b in linked), scope.get("participant_name", ""))).casefold():
                continue
            rows = positions[settlement.id]
            if f["participant_type"] and not any(p.share_kind == ROLES[f["participant_type"]] or
                    f["participant_type"] == "provider" and p.kind == "cost" for p in rows):
                continue
            if baseline is None:
                if not settlement.created_at or utc(settlement.created_at) < through:
                    issues.add("unfrozen_settlement_history")
                continue
            recognized = utc(baseline.created_at) if isinstance(baseline.created_at, datetime) else None
            if recognized is None:
                issues.add("baseline_confirmation_time_missing")
                continue
            if recognized >= through:
                continue
            if settlement.status == "superseded":
                issues.add("superseded_baseline_history_unproven")
            if baseline.currency != "CNY":
                issues.add("unsupported_currency")
                continue
            try:
                if hashlib.sha256(baseline.snapshot_json.encode()).hexdigest() != baseline.snapshot_hash:
                    raise ValueError()
                snapshot = json.loads(baseline.snapshot_json)
                entries = snapshot["entries"]
                if scope["kind"] == "beneficiary":
                    entries = [e for e in entries if e.get("participant_id") == scope["participant_id"] and e.get("participant_type") == scope["participant_type"]]
                def identity(e):
                    return (e["kind"], e["share_kind"], e["participant_type"], e["participant_id"], format(decimal(e["amount"]), ".2f"))
                if Counter(identity(e) for e in entries) != Counter(identity({k: getattr(p, k) for k in
                        ("kind", "share_kind", "participant_type", "participant_id", "amount")}) for p in rows):
                    issues.add("positions_do_not_match_frozen_baseline")
            except (KeyError, ValueError, TypeError, ArithmeticError):
                issues.add("baseline_history_evidence_invalid")
            facts.append(dict(baseline_id=baseline.settlement_id, digest=baseline.snapshot_hash, recognized_at=recognized.isoformat()))
            funding = funding_by_id.get(settlement.id, {})
            if funding and not funding.get("valid"):
                issues.add("funding_evidence_inconsistent")
            if funding:
                facts.append(dict(funding_request_id=funding.get("request_id"), evidence_hash=funding.get("evidence_hash"),
                    absorption_evidence_hash=funding.get("absorption_evidence_hash"), effective_at=funding.get("effective_at")))
            for p in rows:
                if p.direction == "internal":
                    signed = decimal(p.amount) * (-1 if baseline.revenue < 0 or p.kind == "loss" else 1)
                    totals["opening_internal_profit" if recognized < start else "internal_profit_added"] += signed
                    for a, confirmation, tx in events[p.id]:
                        if confirmation is None or not isinstance(confirmation.created_at, datetime):
                            issues.add("internal_allocation_time_missing")
                        elif utc(confirmation.created_at) < through:
                            valid = (funding.get("valid") and funding.get("kind") == "platform_absorption"
                                and tx is not None and tx.id == funding.get("transaction_id")
                                and funding.get("absorption_allocations", {}).get(p.id) == decimal(a.amount))
                            if valid:
                                if utc(confirmation.created_at) < start:
                                    totals["opening_internal_profit"] -= decimal(a.amount)
                                else:
                                    totals["internal_absorption"] += decimal(a.amount)
                            else:
                                issues.add("unsupported_internal_funding_transaction:" + (tx.kind if tx else "unknown"))
                            facts.append(dict(allocation_id=a.id, kind=tx.kind if tx else "unknown",
                                confirmed_at=utc(confirmation.created_at).isoformat(), amount=str(a.amount)))
                    continue
                blocked = cash_block(p, funding, as_of=through)
                if blocked or not p.participant_id:
                    issues.add(blocked or "unknown_beneficiary")
                if p.direction not in {"payable", "receivable"} or p.currency != "CNY":
                    issues.add("position_history_invalid")
                    continue
                direction = p.direction
                amount = decimal(p.amount)
                if recognized < start:
                    totals["opening_" + direction] += amount
                else:
                    totals[direction + "_added"] += amount
                facts.append(dict(position_id=p.id, direction=direction, amount=str(amount)))
                applied = Decimal(0)
                for a, confirmation, tx in events[p.id]:
                    if confirmation is None or not isinstance(confirmation.created_at, datetime):
                        issues.add("allocation_confirmation_time_missing")
                        continue
                    posted = utc(confirmation.created_at)
                    if posted >= through:
                        continue
                    if tx is None or not isinstance(tx.created_at, datetime):
                        issues.add("transaction_time_missing")
                        continue
                    if (posted < recognized or posted < utc(tx.created_at) or not tx.reference.strip() or not confirmation.reviewer_id
                            or tx.mode not in {"manual", "simulated"}):
                        issues.add("allocation_history_evidence_invalid")
                    value = decimal(a.amount)
                    applied += value
                    if tx.kind == "offset":
                        field = direction + "_offset"
                    elif tx.kind == "payout" and direction == "payable":
                        field = "paid"
                    elif tx.kind == "recovery" and direction == "receivable":
                        field = "recovered"
                    else:
                        issues.add("historical_allocation_direction_invalid:" + tx.kind)
                        continue
                    if posted < start:
                        totals["opening_" + direction] -= value
                    else:
                        totals[field] += value
                    facts.append(dict(allocation_id=a.id, position_id=p.id, transaction_id=tx.id,
                        confirmed_at=posted.isoformat(), amount=str(value), kind=tx.kind))
                if applied > amount:
                    issues.add("historical_position_overallocated")
        totals["closing_payable"] = totals["opening_payable"] + totals["payable_added"] - totals["paid"] - totals["payable_offset"]
        totals["closing_receivable"] = totals["opening_receivable"] + totals["receivable_added"] - totals["recovered"] - totals["receivable_offset"]
        totals["closing_internal_profit"] = totals["opening_internal_profit"] + totals["internal_profit_added"] - totals["internal_absorption"]
        if any(value < 0 for key, value in totals.items() if "internal" not in key):
            issues.add("negative_historical_balance")
        values = {key: format(value, ".2f") for key, value in totals.items()}
        return dict(result, known=not issues, unknown_reasons=sorted(issues), observed_totals=values,
            evidence_digest=hashlib.sha256(canonical(sorted(facts, key=canonical)).encode()).hexdigest(),
            **{key: None if issues else value for key, value in values.items()})

    def scope_for(request, db, user):
        if request.query_params.get("business_view") == "beneficiary":
            participant_id = request.query_params.get("participant_id", "")
            participant = db.get(ns["settlement_parties"]["Participant"], participant_id) if participant_id else None
            if participant is None or not beneficiary_allowed(request, db, user, participant):
                raise HTTPException(403, "beneficiary_identity_forbidden")
            return dict(kind="beneficiary", provider_id=None, key="beneficiary:" + participant.id,
                participant_id=participant.id, participant_type=participant.kind,
                participant_name=participant.name, currency=participant.currency)
        if user.platform_role:
            if user.platform_role not in READ_ROLES:
                raise HTTPException(403, "reporting_role_forbidden")
            return {"kind": "platform", "provider_id": None, "key": "platform"}
        scope = resolve_business_scope(request, db, user, ns["Membership"])
        enterprise = db.get(E, scope["enterprise_id"])
        if (user.verified_status != "verified" or scope["mode"] != "provider" or
                enterprise is None or enterprise.verification_status != "verified" or
                "provider" not in (scope["membership"].business_roles or "").split(",") or
                scope["membership"].status != "active" or
                scope["membership"].role not in {"super_admin", "enterprise_admin"}):
            raise HTTPException(403, "verified_provider_admin_scope_required")
        return {"kind": "provider", "provider_id": scope["enterprise_id"], "key": "provider:" + scope["enterprise_id"]}

    def beneficiary_allowed(request, db, user, participant):
        if (user.verified_status != "verified" or not user.is_active or
                user.activation_status != "active" or participant.kind not in {"service", "expert", "channel"}):
            return False
        header = request.headers.get("X-Enterprise-Id", "")
        selected = request.query_params.get("enterprise_id", "")
        if header and selected and header != selected:
            raise HTTPException(400, "enterprise_scope_mismatch")
        selected = header or selected
        # owner_enterprise_id identifies the configuring provider, never the beneficiary.
        if participant.user_id and participant.user_id != user.id:
            return False
        enterprise_id = participant.enterprise_id or ""
        if not enterprise_id:
            # The console always sends its current enterprise. Personal identities
            # are user-bound and independent of that navigation context.
            return bool(participant.user_id)
        if selected and selected != enterprise_id:
            return False
        if enterprise_id:
            M = ns["Membership"]
            membership = db.scalar(select(M).where(M.user_id == user.id, M.enterprise_id == enterprise_id, M.status == "active"))
            enterprise = db.get(E, enterprise_id)
            if membership is None or enterprise is None or enterprise.verification_status != "verified":
                return False
            if not participant.user_id and (not selected or membership.role not in {"super_admin", "enterprise_admin"}):
                return False
        return bool(participant.user_id or enterprise_id)

    def beneficiary_build(db, scope, f, *, complete=False):
        if f["participant_type"] and f["participant_type"] != scope["participant_type"]:
            raise HTTPException(403, "beneficiary_role_filter_forbidden")
        finance = ns["settlement_finance"]
        P, Baseline = finance["Position"], finance["Baseline"]
        # Include liabilities for this identity as well as profit; unknown kinds fail closed.
        stmt = select(P, S, O.product_id, O.product_name, O.provider_enterprise_id, E.name, Baseline.snapshot_json, Baseline.snapshot_hash).join(
            S, S.id == P.settlement_id).join(O, O.id == S.order_id).outerjoin(E, E.id == O.provider_enterprise_id).join(
            Baseline, Baseline.settlement_id == P.settlement_id).where(
                P.participant_id == scope["participant_id"],
                P.participant_type == scope["participant_type"], S.status != "superseded")
        if f["provider_id"]:
            stmt = stmt.where(O.provider_enterprise_id == f["provider_id"])
        if f["product_id"]:
            stmt = stmt.where(O.product_id == f["product_id"])
        if f["status"]:
            stmt = stmt.where(S.status == f["status"])
        settlement_ids = stmt.with_only_columns(S.id)
        funding_by_id = scoped_funding(db, settlement_ids)
        position_ids = stmt.with_only_columns(P.id)
        batches = defaultdict(dict)
        for sid, batch in db.execute(select(L.settlement_id, B).join(B, B.id == L.batch_id).where(
                L.settlement_id.in_(settlement_ids), L.status != "superseded", B.status != "superseded").order_by(B.created_at, B.id)):
            batches[sid][batch.id] = batch
        changes = {}
        Link = ns.get("version_change_settlement", {}).get("Model")
        Adjustment = ns.get("VersionChangeAdjustment")
        if Link is not None and Adjustment is not None:
            changes = dict(db.execute(select(Link.settlement_id, Adjustment.change_id).join(
                Adjustment, Adjustment.id == Link.adjustment_id).where(Link.settlement_id.in_(settlement_ids))).all())
        A, C, T = (finance[key] for key in ("Allocation", "Confirmation", "Transaction"))
        applied = defaultdict(list)
        for allocation, confirmation, transaction in db.execute(select(A, C, T).join(C, C.transaction_id == A.transaction_id).join(
                T, T.id == C.transaction_id).where(A.position_id.in_(position_ids)).order_by(A.id)):
            applied[allocation.position_id].append((allocation, confirmation, transaction))
        lo = datetime.combine(date.fromisoformat(f["start"]), time.min, SHANGHAI).astimezone(timezone.utc)
        hi = datetime.combine(date.fromisoformat(f["end"]) + timedelta(days=1), time.min, SHANGHAI).astimezone(timezone.utc)
        rows = []
        for position, settlement, product_id, product_name, provider_id, provider_name, snapshot_json, snapshot_hash in db.execute(stmt):
            linked = list(batches[settlement.id].values())
            monthly = [b for b in linked if b.cycle == "monthly" and b.period_start]
            if len({utc(b.period_start) for b in monthly}) > 1:
                raise HTTPException(409, "ambiguous_settlement_financial_period")
            batch = monthly[-1] if monthly else linked[-1] if linked else None
            financial_at = utc(batch.period_start if monthly else settlement.created_at)
            if not lo <= financial_at < hi or f["year"] and financial_at.astimezone(SHANGHAI).year != f["year"]:
                continue
            if f["batch_id"] and not any(f["batch_id"] in (b.id, b.batch_no) for b in linked):
                continue
            change = changes.get(settlement.id, "")
            source = "version_change" if change else "refund" if settlement.is_refund or settlement.refund_id else "original"
            if f["source_kind"] and source != f["source_kind"]:
                continue
            if f["q"] and f["q"].casefold() not in " ".join((settlement.settlement_no, product_name or "", provider_name or "", batch.batch_no if batch else "", scope["participant_name"])).casefold():
                continue
            if position.currency != scope["currency"]:
                raise HTTPException(409, "beneficiary_currency_mismatch")
            if (position.kind not in {"profit", "loss"} or position.share_kind != ROLES[scope["participant_type"]] or
                    position.kind == "loss" and position.direction != "receivable"):
                raise HTTPException(409, "beneficiary_position_kind_unsupported")
            totals = {key: Decimal(0) for key in FUNDS}
            issues = []
            paid, recovered, offset = Decimal(0), Decimal(0), Decimal(0)
            for allocation, confirmation, transaction in applied[position.id]:
                value = decimal(allocation.amount)
                if transaction.kind not in {"payout", "recovery", "offset"}:
                    issues.append("unsupported_transaction_kind:" + transaction.kind)
                if not isinstance(confirmation.created_at, datetime):
                    issues.append("allocation_confirmation_time_missing")
                if not transaction.reference.strip() or not confirmation.reviewer_id or transaction.mode not in {"manual", "simulated"}:
                    issues.append("unverified_transaction_evidence")
                if transaction.kind == "payout" and position.direction == "payable":
                    paid += value
                elif transaction.kind == "recovery" and position.direction == "receivable":
                    recovered += value
                elif transaction.kind == "offset" and position.direction in {"payable", "receivable"}:
                    offset += value
                else:
                    issues.append("allocation_direction_mismatch")
            balance = decimal(position.amount) - paid - recovered - offset
            if balance < 0:
                issues.append("overallocated_position")
            blocked = cash_block(position, funding_by_id.get(settlement.id, {}))
            if blocked:
                issues.append(blocked)
            if position.direction == "payable":
                totals.update(payable_amount=decimal(position.amount), paid_amount=paid, payable_offset_amount=offset, unpaid_amount=balance)
            elif position.direction == "receivable":
                totals.update(receivable_amount=decimal(position.amount), recovered_amount=recovered, receivable_offset_amount=offset, unrecovered_amount=balance)
            else:
                raise HTTPException(409, "beneficiary_position_direction_invalid")
            signed = -decimal(position.amount) if position.direction == "receivable" else decimal(position.amount)
            exception_id, loss_source, loss_label = "", "", ""
            if position.kind == "loss":
                loss_source, loss_label = "finance_loss_position", "损失承担（需核对依据）"
                try:
                    if hashlib.sha256(snapshot_json.encode()).hexdigest() != snapshot_hash:
                        raise ValueError()
                    application = json.loads(snapshot_json).get("cost_exception_application") or {}
                    loss, proof = application.get("loss", {}), application.get("evidence", {})
                    if (loss.get("participant_id") == scope["participant_id"] and loss.get("participant_type") == scope["participant_type"]
                            and decimal(loss.get("amount")) == decimal(position.amount) and proof.get("id")
                            and proof.get("reviewer_id") and proof.get("approved_at")):
                        exception_id = proof["id"]
                        loss_source, loss_label = "approved_cost_exception", "已批准成本例外承担"
                except (ValueError, TypeError, ArithmeticError):
                    raise HTTPException(409, "finance_baseline_invalid") from None
            values = {key: format(value, ".2f") for key, value in totals.items()}
            row = dict(position_id=position.id, settlement_no=settlement.settlement_no, product_id=product_id,
                product_name=product_name, provider_id=provider_id, provider_name=provider_name or "",
                participant_id=scope["participant_id"], participant_type=scope["participant_type"], participant_name=scope["participant_name"],
                status=settlement.status, direction=position.direction, position_kind=position.kind,
                month=financial_at.astimezone(SHANGHAI).strftime("%Y-%m"),
                financial_at=financial_at.isoformat(), source_kind=source, currency=position.currency, provisional=False,
                cost_amount="0.00", profit_share=format(signed if position.kind == "profit" else Decimal(0), ".2f"),
                loss_amount=format(decimal(position.amount) if position.kind == "loss" else Decimal(0), ".2f"),
                total_receivable=format(signed, ".2f"),
                cost_exception_id=exception_id, liability_source=loss_source, loss_label=loss_label,
                funds_known=not issues, blocked_reason=blocked,
                funds=dict(known=not issues, known_totals=values, issues=sorted(set(issues))))
            row.update({key: None if issues else value for key, value in values.items()})
            # Used only for distinct counts, removed before either JSON or CSV serialization.
            row["_counts"] = dict(settlement=settlement.id, order=position.order_id, refund=settlement.refund_id, change=change)
            rows.append(row)
        rows.sort(key=lambda r: (r["financial_at"], r["position_id"]), reverse=True)
        def summarize(bucket):
            result = {key: format(sum((decimal(r[key]) for r in bucket), Decimal(0)), ".2f") for key in ("cost_amount", "profit_share", "loss_amount", "total_receivable")}
            funds = fund_totals(bucket)
            result.update({key: funds[key] for key in FUNDS})
            result.update(funds=funds, position_count=len(bucket), settlement_count=len({r["_counts"]["settlement"] for r in bucket}),
                order_count=len({r["_counts"]["order"] for r in bucket}),
                income_order_count=len({r["_counts"]["order"] for r in bucket if r["source_kind"] == "original" and decimal(r["profit_share"]) > 0}),
                refund_count=len({r["_counts"]["refund"] for r in bucket if r["source_kind"] == "refund" and r["_counts"]["refund"]}),
                adjustment_count=len({r["_counts"]["change"] for r in bucket if r["_counts"]["change"]}))
            return result
        def group(keys):
            buckets = defaultdict(list)
            for row in rows:
                buckets[tuple(row[key] for key in keys)].append(row)
            return [dict(zip(keys, key)) | summarize(bucket) for key, bucket in sorted(buckets.items())]
        monthly = group(("month",))
        if f["year"]:
            by_month = {r["month"]: r for r in monthly}
            monthly = [by_month.get(f'{f["year"]}-{m:02}', dict(month=f'{f["year"]}-{m:02}') | summarize([])) for m in range(1, 13)]
        result = dict(summary=summarize(rows), monthly=monthly, products=group(("product_id", "product_name")),
            providers=group(("provider_id", "provider_name")), participants=group(("participant_id", "participant_type", "participant_name")),
            rankings=[], total=len(rows), page=f["page"], page_size=f["page_size"], scope=dict(scope, filters=f,
                timezone="Asia/Shanghai", cutoff=datetime.now(timezone.utc).isoformat(), basis="posted_finance_positions_only",
                beneficiary_grants_supported=True, bank_receipts_verified=False, includes_provisional=False))
        public = [{key: value for key, value in row.items() if key != "_counts"} for row in rows]
        offset = (f["page"] - 1) * f["page_size"]
        result["items"] = public if complete else public[offset:offset + f["page_size"]]
        result["balance_bridge"] = balance_bridge(db, scope, f)
        result["scope"]["funds_basis"] = "current_confirmed_positions; historical_balances_in_balance_bridge"
        result["scope"]["participant_type_basis"] = "selected_registry_identity_positions_only"
        return result

    def filters(start: str | None = None, end: str | None = None,
                year: int | None = Query(None, ge=1900, le=9998), q: str = "",
                provider_id: str = "", product_id: str = "", batch_id: str = "",
                status: str = "", source_kind: str = "", participant_type: str = "",
                page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500)):
        if participant_type and participant_type not in ROLES:
            raise HTTPException(422, "invalid_participant_type")
        if source_kind and source_kind not in {"original", "refund", "version_change"}:
            raise HTTPException(422, "invalid_source_kind")
        def parse(value):
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError()
                return date.fromisoformat(value)
            except ValueError:
                raise HTTPException(422, "date_must_be_YYYY_MM_DD") from None
        today = datetime.now(SHANGHAI).date()
        lo = parse(start) if start else date(year, 1, 1) if year else today.replace(day=1)
        hi = parse(end) if end else date(year, 12, 31) if year else (lo.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        if hi < lo or hi == date.max:
            raise HTTPException(422, "invalid_date_range")
        return dict(start=lo.isoformat(), end=hi.isoformat(), year=year, q=q, provider_id=provider_id,
            product_id=product_id, batch_id=batch_id, status=status, source_kind=source_kind,
            participant_type=participant_type, page=page, page_size=page_size)

    def build(db, scope, f, *, complete=False):
        if scope["kind"] == "beneficiary":
            return beneficiary_build(db, scope, f, complete=complete)
        if scope["provider_id"] and f["provider_id"] and scope["provider_id"] != f["provider_id"]:
            raise HTTPException(403, "cross_provider_filter_forbidden")
        stmt = select(S, O, E.name).join(O, O.id == S.order_id).outerjoin(E, E.id == O.provider_enterprise_id).where(S.status != "superseded")
        provider = scope["provider_id"] or f["provider_id"]
        if provider:
            stmt = stmt.where(O.provider_enterprise_id == provider)
        if f["product_id"]:
            stmt = stmt.where(O.product_id == f["product_id"])
        if f["status"]:
            stmt = stmt.where(S.status == f["status"])
        # Restrict related records with a subquery, avoiding a 2000-row or SQL IN cap.
        ids = stmt.with_only_columns(S.id)
        funding_by_id = scoped_funding(db, ids)
        receipts = defaultdict(list)
        Payment = ns.get("Payment")
        if Payment is not None:
            for payment in db.scalars(select(Payment).where(Payment.order_id.in_(stmt.with_only_columns(O.id))).order_by(Payment.id)):
                receipts[payment.order_id].append({column.name: str(getattr(payment, column.name)) for column in Payment.__table__.columns})
        finance = ns.get("settlement_finance", {})
        baselines, positions, applied = {}, defaultdict(list), defaultdict(list)
        if finance.get("Baseline") is not None:
            Baseline = finance["Baseline"]
            baselines = {b.settlement_id: b for b in db.scalars(select(Baseline).where(Baseline.settlement_id.in_(ids)))}
            Position = finance.get("Position")
            if Position is not None:
                for position in db.scalars(select(Position).where(Position.settlement_id.in_(ids)).order_by(Position.id)):
                    positions[position.settlement_id].append(position)
                if all(finance.get(key) is not None for key in ("Allocation", "Confirmation", "Transaction")):
                    A, C, T = (finance[key] for key in ("Allocation", "Confirmation", "Transaction"))
                    for allocation, confirmation, transaction in db.execute(select(A, C, T).join(
                            C, C.transaction_id == A.transaction_id).join(T, T.id == C.transaction_id).where(
                            A.position_id.in_(select(Position.id).where(Position.settlement_id.in_(ids)))).order_by(A.id)):
                        applied[allocation.position_id].append((allocation, confirmation, transaction))
        related = db.execute(select(L, B).join(B, B.id == L.batch_id).where(
            L.settlement_id.in_(ids), L.status != "superseded", B.status != "superseded").order_by(B.created_at, B.id, L.id)).all()
        lines = defaultdict(list)
        for line, batch in related:
            lines[line.settlement_id].append((line, batch))
        changes, adjustments = {}, {}
        hooks = ns.get("version_change_settlement", {})
        Link, Adjustment = hooks.get("Model"), ns.get("VersionChangeAdjustment")
        if Link is not None and Adjustment is not None:
            adjustments = dict(db.execute(select(Link.settlement_id, Adjustment).join(
                Adjustment, Adjustment.id == Link.adjustment_id).where(Link.settlement_id.in_(ids))).all())
            changes = {sid: adjustment.change_id for sid, adjustment in adjustments.items()}
        lo = datetime.combine(date.fromisoformat(f["start"]), time.min, SHANGHAI).astimezone(timezone.utc)
        hi = datetime.combine(date.fromisoformat(f["end"]) + timedelta(days=1), time.min, SHANGHAI).astimezone(timezone.utc)
        rows = []
        for settlement, order, provider_name in db.execute(stmt):
            linked = lines[settlement.id]
            batches = {b.id: b for _, b in linked}
            monthly = [b for b in batches.values() if b.cycle == "monthly" and b.period_start]
            if len({utc(b.period_start) for b in monthly}) > 1:
                raise HTTPException(409, "ambiguous_settlement_financial_period")
            batch = monthly[-1] if monthly else list(batches.values())[-1] if batches else None
            financial_at = utc(batch.period_start if monthly else settlement.created_at)
            if not lo <= financial_at < hi:
                continue
            if f["year"] and financial_at.astimezone(SHANGHAI).year != f["year"]:
                continue
            if f["batch_id"] and not any(f["batch_id"] in (b.id, b.batch_no) for b in batches.values()):
                continue
            change_id = changes.get(settlement.id, "")
            source = "version_change" if change_id else "refund" if settlement.is_refund or settlement.refund_id else "original"
            if order.business_type == "version_change_payment" and source == "original":
                continue
            if f["source_kind"] and source != f["source_kind"]:
                continue
            row = dict(id=settlement.id, settlement_no=settlement.settlement_no, order_id=order.id,
                order_no=order.order_no, product_id=order.product_id, product_name=order.product_name,
                product_version_id=order.product_version_id, product_version_name=order.product_version_name,
                provider_id=order.provider_enterprise_id, provider_name=provider_name or "",
                status=settlement.status, source_kind=source, refund_id=settlement.refund_id or "",
                change_id=change_id, batch_id=batch.id if batch else "", batch_no=batch.batch_no if batch else "",
                batch_status=batch.status if batch else "",
                month=financial_at.astimezone(SHANGHAI).strftime("%Y-%m"), financial_at=financial_at.isoformat(),
                created_at=utc(settlement.created_at).isoformat())
            version_evidence = dict(settlement={column.name: str(getattr(settlement, column.name)) for column in S.__table__.columns},
                order={key: str(getattr(order, key)) for key in ("id", "economic_snapshot_json", "updated_at", "paid_amount", "payment_status", "product_version_id")},
                lines=[dict(id=l.id, batch_id=b.id, status=l.status, participant_type=l.participant_type,
                            participant_id=l.participant_id, participant_name=l.participant_name, amount=str(l.amount)) for l, b in linked],
                receipts=receipts[order.id])
            row["source_version_digest"] = hashlib.sha256(canonical(version_evidence).encode()).hexdigest()
            for key in MONEY:
                amount = decimal(settlement.cost_amount) + decimal(settlement.provider_share) if key == "provider_total_receivable" else decimal(getattr(settlement, key))
                row[key] = format(amount, ".2f")
            baseline = baselines.get(settlement.id)
            snapshot = {}
            row["provisional"] = baseline is None
            row["baseline_hash"] = baseline.snapshot_hash if baseline else ""
            if baseline is not None:
                if baseline.order_id != order.id or baseline.provider_enterprise_id != order.provider_enterprise_id or baseline.currency != "CNY":
                    raise HTTPException(409, "finance_baseline_scope_or_currency_mismatch")
                try:
                    snapshot = json.loads(baseline.snapshot_json)
                    for key, value in dict(gross_amount=baseline.revenue, cost_amount=baseline.cost,
                                           profit_amount=baseline.profit, **snapshot["shares"]).items():
                        row[key] = format(decimal(value), ".2f")
                except (ValueError, TypeError, KeyError, ArithmeticError):
                    raise HTTPException(409, "finance_baseline_invalid") from None
                row["net_amount"] = row["gross_amount"]
                row["provider_total_receivable"] = format(decimal(row["cost_amount"]) + decimal(row["provider_share"]), ".2f")
            subjects = subject_rows(db, row, order, linked, batch, baseline, positions[settlement.id])
            funding = funding_by_id.get(settlement.id, {})
            absorption = decimal(funding["amount"]) if funding.get("valid") and funding.get("kind") == "platform_absorption" else Decimal(0)
            row["supplementary_funding"] = {key: value for key, value in funding.items() if key != "absorption_allocations"}
            row["supplementary_funding_json"] = canonical(row["supplementary_funding"])
            row["funding_absorption_loss"] = format(absorption, ".2f")
            for subject in subjects:
                burden = absorption if (subject["participant_type"], subject["participant_id"]) == ("platform", "platform") else Decimal(0)
                subject["baseline_total_receivable"] = subject["total_receivable"]
                subject["funding_absorption_loss"] = format(burden, ".2f")
                subject["net_total_receivable"] = format(decimal(subject["total_receivable"]) - burden, ".2f")
                subject["total_receivable"] = subject["net_total_receivable"]
            row.update(detail_fields(db, order, settlement, baseline, snapshot, adjustments.get(settlement.id)))
            row["participants"] = subjects
            totals = {key: Decimal(0) for key in FUNDS}
            evidence, issues, position_details = [], [], []
            if funding and not funding.get("valid"):
                issues.append("funding_evidence_inconsistent")
            for position in positions[settlement.id]:
                paid, recovered, offset = Decimal(0), Decimal(0), Decimal(0)
                for allocation, confirmation, transaction in applied[position.id]:
                    value = decimal(allocation.amount)
                    if (funding.get("valid") and transaction.id == funding.get("transaction_id")
                            and funding.get("absorption_allocations", {}).get(position.id) == value):
                        continue
                    if transaction.kind not in {"payout", "recovery", "offset"}:
                        issues.append("unsupported_transaction_kind:" + transaction.kind)
                    if not isinstance(confirmation.created_at, datetime):
                        issues.append("allocation_confirmation_time_missing")
                    if (not transaction.reference.strip() or not confirmation.reviewer_id or
                            transaction.mode not in {"manual", "simulated"}):
                        issues.append("unverified_transaction_evidence")
                    if transaction.kind == "payout" and position.direction == "payable":
                        paid += value
                    elif transaction.kind == "recovery" and position.direction == "receivable":
                        recovered += value
                    elif transaction.kind == "offset" and position.direction in {"payable", "receivable"}:
                        offset += value
                    else:
                        issues.append("allocation_direction_mismatch")
                    evidence.append(dict(allocation_id=allocation.id, transaction_id=transaction.id,
                        kind=transaction.kind, mode=transaction.mode, reference=transaction.reference,
                        reviewer_id=confirmation.reviewer_id,
                        confirmed_at=utc(confirmation.created_at).isoformat() if isinstance(confirmation.created_at, datetime) else None, amount=str(value)))
                balance = decimal(position.amount) - paid - recovered - offset
                if balance < 0:
                    issues.append("overallocated_position")
                blocked = cash_block(position, funding)
                if blocked or not position.participant_id:
                    issues.append(blocked or "missing_subject_mapping")
                if position.currency != "CNY":
                    issues.append("unsupported_currency")
                if position.direction == "payable":
                    totals["payable_amount"] += decimal(position.amount)
                    totals["paid_amount"] += paid
                    totals["payable_offset_amount"] += offset
                    totals["unpaid_amount"] += balance
                elif position.direction == "receivable":
                    totals["receivable_amount"] += decimal(position.amount)
                    totals["recovered_amount"] += recovered
                    totals["receivable_offset_amount"] += offset
                    totals["unrecovered_amount"] += balance
                elif position.direction == "internal":
                    totals["internal_profit"] += (-1 if decimal(row["gross_amount"]) < 0 else 1) * decimal(position.amount)
                    totals["internal_profit"] -= funding.get("absorption_allocations", {}).get(position.id, Decimal(0)) if funding.get("valid") else Decimal(0)
                else:
                    issues.append("unknown_position_direction")
                position_details.append(dict(id=position.id, kind=position.kind, share_kind=position.share_kind,
                    participant_id=position.participant_id, participant_type=position.participant_type,
                    direction=position.direction, currency=position.currency, amount=str(position.amount),
                    remaining_amount=format(balance - (funding.get("absorption_allocations", {}).get(position.id, Decimal(0)) if funding.get("valid") else Decimal(0)), ".2f"), blocked_reason=blocked))
            # Missing coverage is unknown, including an all-zero baseline with no evidence.
            covered = baseline is not None and bool(position_details)
            if not covered:
                issues.append("finance_positions_missing")
            known = covered and not issues
            known_totals = {key: format(value, ".2f") for key, value in totals.items()}
            row["funds"] = dict(known=bool(known), known_totals=known_totals,
                **{key: value if known else None for key, value in known_totals.items()},
                issues=sorted(set(issues)), positions=position_details, evidence=evidence)
            if f["participant_type"] and not any(p["participant_type"] == f["participant_type"] and decimal(p["total_receivable"]) for p in subjects):
                continue
            if f["q"] and f["q"].casefold() not in " ".join(str(row[k]) for k in ("settlement_no", "order_no", "product_name", "provider_name", "batch_no", "product_version_name")).casefold():
                continue
            rows.append(row)
        rows.sort(key=lambda r: (r["financial_at"], r["created_at"], r["id"]), reverse=True)
        def group(keys):
            buckets = defaultdict(list)
            for row in rows:
                buckets[tuple(row[k] for k in keys)].append(row)
            return [dict(zip(keys, key)) | aggregate(bucket) for key, bucket in sorted(buckets.items())]
        participants = {}
        for row in rows:
            for p in row["participants"]:
                # Unassigned subjects remain record-local; no fabricated shared identity.
                key = (p["participant_type"], p["subject_type"], p["participant_id"], "" if p["participant_id"] else row["id"])
                target = participants.setdefault(key, dict(p, amount="0.00", cost_amount="0.00", loss_amount="0.00",
                    profit_share="0.00", total_receivable="0.00", funding_absorption_loss="0.00", baseline_total_receivable="0.00", net_total_receivable="0.00", settlement_ids=[]))
                for field in ("amount", "cost_amount", "profit_share", "loss_amount", "total_receivable", "funding_absorption_loss", "baseline_total_receivable", "net_total_receivable"):
                    target[field] = format(decimal(target[field]) + decimal(p[field]), ".2f")
                target["provisional"] = target["provisional"] or p["provisional"]
                if target["subject_source"] != p["subject_source"]:
                    target["subject_source"] = "mixed"
                if row["id"] not in target["settlement_ids"]:
                    target["settlement_ids"].append(row["id"])
        providers = group(("provider_id", "provider_name"))
        rankings = []
        for metric in ("gross_amount", "profit_amount", *ROLES.values()):
            for rank, provider_row in enumerate(sorted(providers, key=lambda p: (-decimal(p[metric]), p["provider_id"])), 1):
                rankings.append(dict(provider_id=provider_row["provider_id"], provider_name=provider_row["provider_name"],
                    metric=metric, amount=provider_row[metric], rank=rank, basis="net"))
        monthly = group(("month",))
        if f["year"]:
            by_month = {m["month"]: m for m in monthly}
            monthly = [by_month.get(f'{f["year"]}-{m:02}', dict(month=f'{f["year"]}-{m:02}') | aggregate([])) for m in range(1, 13)]
        offset = (f["page"] - 1) * f["page_size"]
        summary = aggregate(rows)
        summary["funds"] = fund_totals(rows)
        return dict(summary=summary, monthly=monthly, balance_bridge=balance_bridge(db, scope, f),
            products=group(("product_id", "product_name", "product_version_id", "product_version_name", "provider_id", "month")),
            providers=providers, participants=list(participants.values()), rankings=rankings,
            items=rows if complete else rows[offset:offset + f["page_size"]], total=len(rows), page=f["page"], page_size=f["page_size"],
            scope=dict(scope, filters=f, timezone="Asia/Shanghai", cutoff=datetime.now(timezone.utc).isoformat(),
                currency="CNY", includes_unconfirmed=any(r["status"] not in {"locked", "paid"} or
                    r["batch_status"] and r["batch_status"] not in {"confirmed", "payment_processing", "partial_paid", "paid", "archived"} for r in rows),
                includes_provisional=any(r["provisional"] for r in rows),
                funds_basis="current_confirmed_positions; historical_balances_in_balance_bridge",
                participant_type_basis="settlement_selection_full_five_parties_and_provider_cost",
                participant_receivable_basis="baseline_cost_plus_profit_minus_loss_minus_separate_funding_absorption; legacy_amount_excludes_funding",
                beneficiary_grants_supported=True, balance_status="known" if summary["funds"]["known"] else "unknown", bank_receipts_verified=False))

    app = ns["app"]
    user_dep, db_dep = Depends(ns["current_user"]), Depends(ns["db_session"])
    prefix = "/api/settlement-reporting"

    @app.get(prefix + "/beneficiaries")
    def beneficiaries(request: Request, user=user_dep, db=db_dep):
        Participant = ns["settlement_parties"]["Participant"]
        # This is an identity selector, not a registry administration endpoint.
        selected = request.headers.get("X-Enterprise-Id", "") or request.query_params.get("enterprise_id", "")
        from sqlalchemy import or_
        predicate = Participant.user_id == user.id
        if selected:
            predicate = or_(predicate, (Participant.user_id == "") & (Participant.enterprise_id == selected))
        items = [dict(participant_id=p.id, participant_type=p.kind, participant_name=p.name, currency=p.currency,
                      enterprise_id=p.enterprise_id) for p in db.scalars(select(Participant).where(predicate).order_by(Participant.id))
                 if beneficiary_allowed(request, db, user, p)]
        return dict(items=items, total=len(items))

    def audit(db, user, action, target, scope):
        if "audit" in ns:
            ns["audit"](db, user.email or user.id, action, "settlement_report", target,
                        canonical(scope), category="settlement_report", business_domain="settlement")

    @app.get(prefix + "/report")
    def report(request: Request, f=Depends(filters), user=user_dep, db=db_dep):
        return build(db, scope_for(request, db, user), f)

    @app.get(prefix + "/export")
    def export(request: Request, kind: str = "details", f=Depends(filters), user=user_dep, db=db_dep):
        data = build(db, scope_for(request, db, user), f, complete=True)
        response = csv_response(data, kind)
        audit(db, user, "export_settlement_reporting", kind, data["scope"])
        db.commit()
        return response

    def metadata(item, db):
        result = {key: getattr(item, key) for key in ("id", "year", "version", "scope_key", "actor_id", "cutoff", "status", "note", "digest", "request_id")}
        finalization = db.get(Finalization, item.id)
        if finalization:
            result.update(status="formal", snapshot_status=item.status, finalization_digest=finalization.digest,
                          reviewer_id=finalization.reviewer_id, finalized_at=finalization.created_at)
        return result

    @app.get(prefix + "/annual-reports")
    def annual_list(request: Request, year: int | None = Query(None, ge=1900, le=9998),
                    provider_id: str = "",
                    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500), user=user_dep, db=db_dep):
        scope = scope_for(request, db, user)
        if scope["kind"] == "beneficiary":
            raise HTTPException(403, "beneficiary_annual_snapshots_unavailable")
        stmt = select(AnnualReport)
        if scope["kind"] == "provider":
            if provider_id and provider_id != scope["provider_id"]:
                raise HTTPException(403, "cross_provider_filter_forbidden")
            stmt = stmt.where(AnnualReport.scope_key == scope["key"])
        elif provider_id:
            stmt = stmt.where(AnnualReport.scope_key == "provider:" + provider_id)
        if year:
            stmt = stmt.where(AnnualReport.year == year)
        total = db.scalar(select(func.count()).select_from(stmt.subquery()))
        items = db.scalars(stmt.order_by(AnnualReport.year.desc(), AnnualReport.version.desc(), AnnualReport.id).offset((page - 1) * page_size).limit(page_size)).all()
        return dict(items=[metadata(i, db) for i in items], total=total, page=page, page_size=page_size, scope=scope)

    @app.post(prefix + "/annual-reports")
    def annual_create(body: AnnualBody, request: Request, provider_id: str = "", user=user_dep, db=db_dep):
        scope = scope_for(request, db, user)
        if scope["kind"] == "beneficiary":
            raise HTTPException(403, "beneficiary_annual_snapshots_unavailable")
        if user.platform_role not in WRITE_ROLES:
            raise HTTPException(403, "annual_report_creation_forbidden")
        if provider_id:
            if db.get(E, provider_id) is None:
                raise HTTPException(404, "provider_not_found")
            scope = dict(kind="provider", provider_id=provider_id, key="provider:" + provider_id)
        def existing():
            item = db.scalar(select(AnnualReport).where(AnnualReport.scope_key == scope["key"], AnnualReport.request_id == body.request_id))
            if item and (item.year != body.year or item.note != body.note or item.actor_id != user.id):
                raise HTTPException(409, "annual_request_id_conflict")
            return item
        prior = existing()
        if prior:
            return metadata(prior, db)
        f = dict(start=f"{body.year}-01-01", end=f"{body.year}-12-31", year=body.year,
                 q="", provider_id="", product_id="", batch_id="", status="", source_kind="", participant_type="", page=1, page_size=50)
        data = build(db, scope, f, complete=True)
        version = (db.scalar(select(func.max(AnnualReport.version)).where(AnnualReport.scope_key == scope["key"], AnnualReport.year == body.year)) or 0) + 1
        envelope = dict(report=data, scope=scope, actor_id=user.id, cutoff=data["scope"]["cutoff"],
                        version=version, year=body.year, note=body.note, status="draft",
                        unresolved=unresolved(data), data_digest=data_digest(data))
        payload = canonical(envelope)
        item = AnnualReport(year=body.year, version=version, scope_key=scope["key"], request_id=body.request_id,
            actor_id=user.id, cutoff=envelope["cutoff"], note=body.note, status="draft", report_json=payload,
            digest=hashlib.sha256(payload.encode()).hexdigest())
        try:
            db.add(item)
            db.flush()
            audit(db, user, "create_settlement_annual_report", item.id, scope)
            db.commit()
        except IntegrityError:
            db.rollback()
            prior = existing()
            if prior:
                return metadata(prior, db)
            raise HTTPException(409, "annual_version_conflict_retry") from None
        return metadata(item, db)

    def authorized_snapshot(report_id, request, db, user):
        scope = scope_for(request, db, user)
        if scope["kind"] == "beneficiary":
            raise HTTPException(404, "annual_report_not_found")
        stmt = select(AnnualReport).where(AnnualReport.id == report_id)
        if scope["kind"] == "provider":
            stmt = stmt.where(AnnualReport.scope_key == scope["key"])
        item = db.scalar(stmt)
        if item is None:
            raise HTTPException(404, "annual_report_not_found")
        if hashlib.sha256(item.report_json.encode()).hexdigest() != item.digest:
            raise HTTPException(409, "annual_report_digest_mismatch")
        return item, scope

    @app.get(prefix + "/annual-reports/{report_id}/export")
    def annual_export(report_id: str, request: Request, kind: str = "details", user=user_dep, db=db_dep):
        item, scope = authorized_snapshot(report_id, request, db, user)
        response = csv_response(json.loads(item.report_json)["report"], kind)
        response.headers["X-Report-Digest"] = item.digest
        finalization = db.get(Finalization, report_id)
        response.headers["X-Report-Status"] = "formal" if finalization else item.status
        if finalization:
            response.headers["X-Finalization-Digest"] = finalization.digest
        audit(db, user, "export_settlement_annual_report", item.id, scope)
        db.commit()
        return response

    @app.post(prefix + "/annual-reports/{report_id}/finalize")
    def annual_finalize(report_id: str, request: Request, body: FinalizeBody | None = None, user=user_dep, db=db_dep):
        if user.platform_role not in WRITE_ROLES:
            raise HTTPException(403, "annual_report_finalize_forbidden")
        item, _ = authorized_snapshot(report_id, request, db, user)
        envelope = json.loads(item.report_json)
        reasons = unresolved(envelope["report"])
        if reasons:
            raise HTTPException(409, dict(reason="annual_report_unresolved", unresolved=reasons))
        if body is None or body.snapshot_digest != item.digest or not body.reconciliation_reference.strip() or not body.note.strip():
            raise HTTPException(422, "explicit_snapshot_reconciliation_required")
        if user.id == item.actor_id:
            raise HTTPException(403, "independent_annual_reviewer_required")
        old = db.get(Finalization, report_id)
        if old:
            prior = json.loads(old.evidence_json)
            if prior["request"] != body.model_dump() or old.reviewer_id != user.id:
                raise HTTPException(409, "annual_finalization_conflict")
            return dict(report_id=report_id, status="formal", digest=old.digest, snapshot_digest=item.digest)
        # Prevent economic/evidence writes and phantoms during the final comparison.
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            tables = [S.__table__, O.__table__, E.__table__, L.__table__, B.__table__, AnnualReport.__table__]
            for name in ("Payment", "VersionChangeAdjustment"):
                if ns.get(name) is not None:
                    tables.append(ns[name].__table__)
            for hooks_name in ("settlement_finance", "settlement_parties", "version_change_settlement"):
                for model in ns.get(hooks_name, {}).values():
                    table = getattr(model, "__table__", None)
                    if table is not None:
                        tables.append(table)
            quote = db.get_bind().dialect.identifier_preparer.format_table
            db.execute(sql_text("LOCK TABLE " + ", ".join(sorted({quote(t) for t in tables})) + " IN SHARE MODE"))
        elif dialect == "sqlite":
            connection = db.connection()
            if connection.connection.driver_connection.in_transaction:
                raise HTTPException(409, "annual_finalization_retry_clean_transaction")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            raise HTTPException(409, "annual_finalization_isolation_unsupported")
        db.scalar(select(AnnualReport).where(AnnualReport.id == report_id).with_for_update())
        current = build(db, envelope["scope"], envelope["report"]["scope"]["filters"], complete=True)
        if data_digest(current) != envelope.get("data_digest"):
            raise HTTPException(409, "annual_data_changed_after_cutoff")
        if unresolved(current):
            raise HTTPException(409, "annual_current_data_unresolved")
        evidence = dict(report_id=report_id, snapshot_digest=item.digest, data_digest=envelope["data_digest"],
            reviewer_id=user.id, request=body.model_dump(), status="formal", bank_receipt_verified=False,
            created_at=datetime.now(timezone.utc).isoformat())
        encoded = canonical(evidence)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        finalization = Finalization(report_id=report_id, request_id=body.request_id, reviewer_id=user.id,
            created_at=evidence["created_at"], snapshot_digest=item.digest, evidence_json=encoded, digest=digest)
        try:
            db.add(finalization)
            db.flush()
            audit(db, user, "finalize_settlement_annual_report", report_id, evidence)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "annual_finalization_conflict") from None
        return dict(report_id=report_id, status="formal", digest=digest, snapshot_digest=item.digest)

    hooks = dict(Model=AnnualReport, Finalization=Finalization, build=build, filters=filters)
    ns["settlement_reporting"] = hooks
    return hooks
