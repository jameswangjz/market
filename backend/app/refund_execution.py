"""Ordinary refunds: durable resource-first execution, simulated funds only.

Completion records local evidence, never BE013 settlement. Resource calls run in
the worker, outside the mutation transaction; payment legs never call a bank.
"""

from datetime import datetime, timezone
import hashlib
import json
import secrets

from fastapi import Depends, HTTPException
from sqlalchemy import ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, event, inspect, select
from sqlalchemy.orm import mapped_column

from .refund_funding import _money
from .refund_funding_store import cents
from .refund_schema import RefundExecutionBody
from .subscription_terms import UTCDateTime


RUNNABLE = ("prepared", "resources_pending", "resources_applied", "funds_pending")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def fail(reason):
    raise HTTPException(409, reason)


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        fail("execution_timestamp_unavailable")
    return value.astimezone(timezone.utc)


def install(ns):
    if "refund_execution" in ns:
        return ns["refund_execution"]
    Base, Refund, Payment = (ns[k] for k in ("Base", "Refund", "Payment"))
    workflow = ns["refund_workflow"]
    Case = workflow["Case"]

    class Execution(Base):
        __tablename__ = "ordinary_refund_executions"
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        refund_id = mapped_column(ForeignKey("refunds.id"), unique=True, nullable=False)
        status = mapped_column(String(30), nullable=False, default="prepared", index=True)
        resume_status = mapped_column(String(30), nullable=False, default="prepared")
        plan_hash = mapped_column(String(64), nullable=False)
        resource_plan_json = mapped_column(Text, nullable=False)
        resource_plan_hash = mapped_column(String(64), nullable=False)
        proof_json = mapped_column(Text, nullable=True)
        funding_json = mapped_column(Text, nullable=False)
        attempts = mapped_column(Integer, nullable=False, default=0)
        error_code = mapped_column(String(80), nullable=False, default="")
        lease_token = mapped_column(String(64), nullable=True)
        lease_expires_at = mapped_column(UTCDateTime(), nullable=True)
        requested_by = mapped_column(String(36), nullable=False)
        created_at = mapped_column(UTCDateTime(), nullable=False, default=ns["now"])
        updated_at = mapped_column(UTCDateTime(), nullable=False, default=ns["now"])
        completed_at = mapped_column(UTCDateTime(), nullable=True)

    class PaymentLeg(Base):
        __tablename__ = "ordinary_refund_payment_legs"
        __table_args__ = (UniqueConstraint("refund_id", "payment_id"),)
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        refund_id = mapped_column(ForeignKey("refunds.id"), nullable=False, index=True)
        payment_id = mapped_column(ForeignKey("payments.id"), nullable=False)
        source_order_id = mapped_column(ForeignKey("orders.id"), nullable=False)
        amount = mapped_column(Numeric(14, 2), nullable=False)
        currency = mapped_column(String(3), nullable=False)
        status = mapped_column(String(20), nullable=False, default="prepared")
        receipt = mapped_column(String(160), nullable=True)
        completed_at = mapped_column(UTCDateTime(), nullable=True)

    class Completion(Base):
        __tablename__ = "ordinary_refund_local_completions"
        refund_id = mapped_column(ForeignKey("refunds.id"), primary_key=True)
        execution_id = mapped_column(ForeignKey("ordinary_refund_executions.id"), unique=True, nullable=False)
        plan_hash = mapped_column(String(64), nullable=False)
        proof_hash = mapped_column(String(64), nullable=False)
        funding_hash = mapped_column(String(64), nullable=False)
        economic_plan_json = mapped_column(Text, nullable=False)
        amount = mapped_column(Numeric(14, 2), nullable=False)
        settlement_status = mapped_column(String(30), nullable=False, default="pending_be013")
        completed_at = mapped_column(UTCDateTime(), nullable=False)

    @event.listens_for(Completion, "before_update")
    @event.listens_for(Completion, "before_delete")
    def immutable(*args):
        raise ValueError("local refund completion evidence is immutable")

    @event.listens_for(PaymentLeg, "before_update")
    def immutable_leg(mapper, connection, target):
        state = inspect(target)
        if any(
            state.attrs[key].history.has_changes()
            for key in ("refund_id", "payment_id", "source_order_id", "amount", "currency")
        ):
            raise ValueError("refund payment leg identity is immutable")
        history = state.attrs.status.history
        if target.status == "completed" and not history.has_changes():
            raise ValueError("successful simulated refund receipt is immutable")
        if history.deleted and history.deleted[0] == "completed":
            raise ValueError("successful simulated refund receipt is immutable")

    @event.listens_for(PaymentLeg, "before_delete")
    def keep_leg(*args):
        raise ValueError("refund payment legs are immutable evidence")

    @event.listens_for(Execution, "before_update")
    def keep_execution_identity(mapper, connection, target):
        state = inspect(target)
        if any(
            state.attrs[key].history.has_changes()
            for key in ("refund_id", "plan_hash", "resource_plan_json", "resource_plan_hash", "funding_json")
        ):
            raise ValueError("frozen execution instructions are immutable")
        proof = state.attrs.proof_json.history
        if proof.deleted and proof.deleted[0] is not None:
            raise ValueError("resource recovery proof cannot be replaced or cleared")

    def adapter():
        hooks = ns.get("refund_execution_resources", {})
        if not callable(hooks.get("prepare")) or not callable(hooks.get("apply")):
            fail("refund_resources_unavailable")
        return hooks

    def envelope(db, order, refund, *, fresh=True):
        case = db.get(Case, refund.id)
        if (
            case is None
            or case.source != "ordinary"
            or workflow["controlled_source"](db, refund, case) != "ordinary"
        ):
            fail("ordinary_refund_execution_required")
        try:
            if not case.plan_json or hashlib.sha256(case.plan_json.encode()).hexdigest() != case.plan_hash:
                raise ValueError
            document = json.loads(case.plan_json)
            if (
                document["refund_id"] != refund.id
                or document["order_id"] != order.id
                or document["payment_id"] != refund.payment_id
                or cents(document["amount"]) != cents(refund.amount)
                or document["basis"] not in {"calls", "time"}
            ):
                raise ValueError
            if utc(document["as_of"]) > utc(ns["now"]()):
                fail("approved_plan_timestamp_invalid")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HTTPException(409, "frozen_refund_plan_invalid") from exc
        return case, document

    def funding(db, order, refund, document, *, fresh=True):
        store = ns["refund_funding_store"]
        rows = store["frozen"](db, refund)
        if rows:
            if list(rows) != document.get("funding_allocations"):
                fail("frozen_funding_changed")
            if fresh:
                store["freeze"](db, order, refund)
        else:
            payment = db.get(Payment, refund.payment_id)
            if payment is None or payment.order_id != order.id:
                fail("original_payment_unavailable")
            rows = (
                dict(
                    payment_id=payment.id,
                    source_order_id=order.id,
                    amount=format(_money(cents(refund.amount)), ".2f"),
                    currency=document["economic_plan"]["economic_snapshot"]["currency"],
                ),
            )
        if (
            not rows
            or len({r["payment_id"] for r in rows}) != len(rows)
            or sum(cents(r["amount"]) for r in rows) != cents(refund.amount)
        ):
            fail("execution_funding_invalid")
        return list(rows)

    def recheck(db, order, refund, *, fresh=True):
        case, document = envelope(db, order, refund, fresh=fresh)
        if fresh:
            current = ns["prepare_refund_plan"](db, order, refund)
            previous = document["economic_plan"]
            # Ignore observational timestamps; every money/usage/recovery and
            # version identity in the frozen economic instruction must match.
            for key in (
                "segment_id",
                "change_id",
                "window_hash",
                "recovery_delta",
                "cumulative_recovery",
                "effective_ends_at",
                "economic_snapshot",
                "delivery_snapshot",
            ):
                if previous.get(key) != current.economic_plan.get(key):
                    fail("approved_usage_or_version_changed")
        return case, document, funding(db, order, refund, document, fresh=fresh)

    def prepare(db, order, refund):
        case, document, rows = recheck(db, order, refund)
        plan = adapter()["prepare"](db, order, refund, document)
        try:
            if (
                not isinstance(plan, dict)
                or plan["refund_id"] != refund.id
                or plan["frozen_envelope_hash"] != digest(document)
                or plan["plan_hash"] != digest({k: v for k, v in plan.items() if k != "plan_hash"})
            ):
                raise ValueError
            canonical(plan)
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(409, "refund_resource_plan_invalid") from exc
        return case, plan, rows

    def eligibility(db, order, refund, user):
        if user.platform_role not in {"super_admin", "platform_operator"}:
            return False
        if refund.status not in {"approved", "failed"}:
            return False
        try:
            adapter()
            envelope(db, order, refund, fresh=False)
            if order.delivery_method_snapshot not in {"api", "model_api", "tenant_access"}:
                return False
            execution = db.scalar(select(Execution).where(Execution.refund_id == refund.id))
            if execution:
                return execution.status == "failed"
            if order.delivery_method_snapshot in {"api", "model_api"}:
                Credential, Route = ns["ApiCredential"], ns["ApiGatewayRoute"]
                credentials = list(
                    db.scalars(
                        select(Credential.id)
                        .join(Route, Route.id == Credential.route_id)
                        .where(
                            Credential.enterprise_id == order.buyer_enterprise_id,
                            Route.product_id == order.product_id,
                            Credential.status == "active",
                        )
                    )
                )
                Cursor = ns.get("api_charge_ledger", {}).get("Cursor")
                if (
                    len(credentials) != 1
                    or Cursor is None
                    or db.get(Cursor, "market:apisix:charges:v1") is None
                ):
                    return False
            else:
                Sub, Config = ns["SaaSSubscription"], ns["SaaSIntegrationConfig"]
                if (
                    len(
                        list(
                            db.scalars(
                                select(Sub.id).where(
                                    Sub.enterprise_id == order.buyer_enterprise_id,
                                    Sub.product_id == order.product_id,
                                    Sub.status == "active",
                                )
                            )
                        )
                    )
                    != 1
                    or len(
                        list(
                            db.scalars(
                                select(Config.id).where(
                                    Config.product_id == order.product_id,
                                    Config.status == "active",
                                    Config.auth_mode == "oauth2",
                                )
                            )
                        )
                    )
                    != 1
                ):
                    return False
            return refund.status == "approved"
        except (HTTPException, ValueError, TypeError, KeyError, ArithmeticError):
            return False

    def mutate(db, user, refund_id, body, operation):
        workflow["platform"](user)
        if user.platform_role not in {"super_admin", "platform_operator"}:
            raise HTTPException(403, "Refund execution role required")
        peek = db.get(Refund, refund_id)
        if peek is None:
            raise HTTPException(404, "Refund not found")
        prior, key, payload_hash = workflow["replay"](db, user, body, operation + ":" + refund_id)
        if prior:
            return peek
        order = workflow["lock_order"](db, peek.order_id)
        refund = db.scalar(
            select(Refund)
            .where(Refund.id == refund_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        prior, key, payload_hash = workflow["replay"](db, user, body, operation + ":" + refund_id)
        if prior:
            return refund
        case = db.get(Case, refund.id, populate_existing=True)
        if case is None or case.version != body.expected_version:
            fail("refund_execution_version_conflict")
        execution = db.scalar(select(Execution).where(Execution.refund_id == refund.id).with_for_update())
        before = dict(status=refund.status, version=case.version)
        if operation == "execute":
            if refund.status != "approved" or execution is not None:
                fail("refund_not_approved_for_execution")
            case, plan, rows = prepare(db, order, refund)
            execution = Execution(
                refund_id=refund.id,
                status="prepared",
                resume_status="prepared",
                plan_hash=case.plan_hash,
                resource_plan_json=canonical(plan),
                resource_plan_hash=digest(plan),
                funding_json=canonical(rows),
                requested_by=user.id,
            )
            db.add(execution)
            for row in rows:
                db.add(
                    PaymentLeg(
                        refund_id=refund.id,
                        payment_id=row["payment_id"],
                        source_order_id=row["source_order_id"],
                        amount=_money(cents(row["amount"])),
                        currency=row["currency"],
                    )
                )
        else:
            if refund.status != "failed" or execution is None or execution.status != "failed":
                fail("refund_execution_not_retryable")
            envelope(db, order, refund, fresh=execution.proof_json is None)
            if execution.plan_hash != case.plan_hash:
                fail("execution_plan_changed")
            execution.status = execution.resume_status
            execution.error_code = ""
            execution.lease_token = None
            execution.lease_expires_at = None
        refund.status = "executing"
        case.version += 1
        workflow["record"](db, user, order, refund, case, body, operation, before, key, payload_hash)
        db.flush()
        return refund

    def publish(db, execution, refund, case, action, before):
        case.version += 1
        after = dict(
            status=refund.status,
            execution_status=execution.status,
            version=case.version,
            funds_mode="simulated",
            settlement_status="pending_be013",
        )
        key = "refund-execution:" + execution.id + ":" + action + ":" + str(case.version)
        Event = workflow["Event"]
        db.add(
            Event(
                refund_id=refund.id,
                operation_key=digest(key),
                payload_hash=digest(after),
                action=action,
                actor_id="refund-worker",
                actor_role="system",
                actor_enterprise_id="",
                reason="Ordinary refund execution",
                request_id="",
                before_json=canonical(before),
                after_json=canonical(after),
            )
        )
        ns["audit"](
            db,
            "refund-worker",
            "ordinary_refund_" + action,
            "refund",
            refund.id,
            "Resource-first simulated refund",
            category="refund",
            business_domain="refund",
            order_id=refund.order_id,
            before=before,
            after=after,
        )
        order = db.get(ns["Order"], refund.order_id)
        ns["notify_business_event"](
            db,
            "Refund execution updated",
            refund.refund_no,
            "refund",
            refund.id,
            event_key=key,
            category="refund",
            enterprise_ids=[order.buyer_enterprise_id, order.provider_enterprise_id],
            platform_roles=["super_admin", "platform_operator"],
        )

    def safe_output(db, execution):
        if execution is None:
            return None
        legs = list(
            db.scalars(
                select(PaymentLeg).where(PaymentLeg.refund_id == execution.refund_id).order_by(PaymentLeg.id)
            )
        )
        resource_status = (
            "applied" if execution.proof_json else ("failed" if execution.status == "failed" else "pending")
        )
        completion = db.get(Completion, execution.refund_id)
        ledger_status = "blocked_guard" if execution.status == "completed" and completion is None else "pending_be013"
        if completion is not None:
            describe_ledger = ns.get("refund_settlement", {}).get("ledger_status")
            ledger_status = describe_ledger(db, execution.refund_id) if callable(describe_ledger) else "pending_monthly"
            if ledger_status not in {"pending_monthly", "settlement_generated", "in_batch", "paid"}:
                ledger_status = "blocked_guard"
        return dict(
            status=execution.status,
            resource_status=resource_status,
            ledger_status=ledger_status,
            payment_legs=[
                dict(
                    amount=format(_money(cents(leg.amount)), ".2f"),
                    status=leg.status if leg.status in ("prepared", "completed", "failed") else "failed",
                    error_code="payment_leg_state_invalid"
                    if leg.status not in ("prepared", "completed")
                    else None,
                )
                for leg in legs
            ],
            error_code=execution.error_code or None,
            funds_mode="simulated",
            retryable=execution.status == "failed" and execution.resume_status in RUNNABLE,
        )

    for operation in ("execute", "retry"):

        def route(operation):
            def handle(
                refund_id: str,
                body: RefundExecutionBody,
                user=Depends(ns["current_user"]),
                db=Depends(ns["db_session"]),
            ):
                with workflow["transaction"](db, user, refund_id, operation):
                    refund = mutate(db, user, refund_id, body, operation)
                    db.commit()
                    return workflow["output"](db, user, refund)

            return handle

        ns["app"].post("/api/refunds/{refund_id}/" + operation)(route(operation))

    hooks = dict(
        Execution=Execution,
        PaymentLeg=PaymentLeg,
        Completion=Completion,
        eligibility=eligibility,
        recheck=recheck,
        envelope=envelope,
        funding=funding,
        publish=publish,
        adapter=adapter,
        mutate=mutate,
        safe_output=safe_output,
    )
    ns["refund_execution"] = hooks
    return hooks
