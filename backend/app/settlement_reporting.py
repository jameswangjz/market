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
from collections import defaultdict
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
    return hashlib.sha256(canonical(dict(report, scope=scope)).encode()).hexdigest()


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
    return reasons


def csv_response(report, kind):
    if kind not in {"summary", "details", "products", "providers", "participants", "refunds", "rankings"}:
        raise HTTPException(422, "invalid_export_kind")
    summary = dict(report["summary"], **report["summary"].get("funds", {}))
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
        numeric = key in MONEY or key in FUNDS or key in {"amount", "profit_share", "total_receivable", "loss_amount"}
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
            if position.blocked_reason:
                issues.append(position.blocked_reason)
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
                funds_known=not issues, blocked_reason=position.blocked_reason,
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
        changes = {}
        hooks = ns.get("version_change_settlement", {})
        Link, Adjustment = hooks.get("Model"), ns.get("VersionChangeAdjustment")
        if Link is not None and Adjustment is not None:
            changes = dict(db.execute(select(Link.settlement_id, Adjustment.change_id).join(
                Adjustment, Adjustment.id == Link.adjustment_id).where(Link.settlement_id.in_(ids))).all())
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
            row["participants"] = subjects
            totals = {key: Decimal(0) for key in FUNDS}
            evidence, issues, position_details = [], [], []
            for position in positions[settlement.id]:
                paid, recovered, offset = Decimal(0), Decimal(0), Decimal(0)
                for allocation, confirmation, transaction in applied[position.id]:
                    value = decimal(allocation.amount)
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
                        reviewer_id=confirmation.reviewer_id, confirmed_at=utc(confirmation.created_at).isoformat(), amount=str(value)))
                balance = decimal(position.amount) - paid - recovered - offset
                if balance < 0:
                    issues.append("overallocated_position")
                if position.blocked_reason or not position.participant_id:
                    issues.append(position.blocked_reason or "missing_subject_mapping")
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
                else:
                    issues.append("unknown_position_direction")
                position_details.append(dict(id=position.id, kind=position.kind, share_kind=position.share_kind,
                    participant_id=position.participant_id, participant_type=position.participant_type,
                    direction=position.direction, currency=position.currency, amount=str(position.amount),
                    remaining_amount=format(balance, ".2f"), blocked_reason=position.blocked_reason))
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
                    profit_share="0.00", total_receivable="0.00", settlement_ids=[]))
                for field in ("amount", "cost_amount", "profit_share", "loss_amount", "total_receivable"):
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
        return dict(summary=summary, monthly=monthly,
            products=group(("product_id", "product_name", "product_version_id", "product_version_name", "provider_id", "month")),
            providers=providers, participants=list(participants.values()), rankings=rankings,
            items=rows if complete else rows[offset:offset + f["page_size"]], total=len(rows), page=f["page"], page_size=f["page_size"],
            scope=dict(scope, filters=f, timezone="Asia/Shanghai", cutoff=datetime.now(timezone.utc).isoformat(),
                currency="CNY", includes_unconfirmed=any(r["status"] not in {"locked", "paid"} or
                    r["batch_status"] and r["batch_status"] not in {"confirmed", "payment_processing", "partial_paid", "paid", "archived"} for r in rows),
                includes_provisional=any(r["provisional"] for r in rows),
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
