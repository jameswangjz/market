"""Short-transaction ordinary refund worker; simulated funds, no bank adapter."""

from datetime import timedelta
import importlib
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import threading

from fastapi import HTTPException
from sqlalchemy import or_, select

from .refund_execution import RUNNABLE, canonical, digest, fail, utc
from .refund_funding import _money
from .refund_funding_store import cents


logger = logging.getLogger(__name__)
HEARTBEAT = Path("/tmp/refund-execution-heartbeat")


def process_once(SessionLocal=None, *, ns=None, now=None):
    if ns is None:
        ns = vars(importlib.import_module("app.main"))
    sessions = SessionLocal or ns["SessionLocal"]
    hooks = ns["refund_execution"]
    Execution, Leg, Completion = (hooks[k] for k in ("Execution", "PaymentLeg", "Completion"))
    instant = utc(now() if now else ns["now"]())
    token = secrets.token_hex(24)
    with sessions() as db:
        row = db.scalar(
            select(Execution)
            .where(
                Execution.status.in_(RUNNABLE),
                or_(Execution.lease_expires_at.is_(None), Execution.lease_expires_at <= instant),
            )
            .order_by(Execution.created_at, Execution.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return False
        row.lease_token, row.lease_expires_at = token, instant + timedelta(seconds=60)
        row.attempts += 1
        execution_id, refund_id, phase = row.id, row.refund_id, row.status
        db.commit()

    def locked(db):
        peek = db.get(ns["Refund"], refund_id)
        order = ns["refund_workflow"]["lock_order"](db, peek.order_id)
        refund = db.scalar(
            select(ns["Refund"])
            .where(ns["Refund"].id == refund_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        execution = db.scalar(
            select(Execution)
            .where(Execution.id == execution_id, Execution.lease_token == token)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if execution is None or execution.status != phase:
            fail("execution_lease_lost")
        case, document = hooks["envelope"](db, order, refund, fresh=False)
        resource_plan = json.loads(execution.resource_plan_json)
        if (
            case.plan_hash != execution.plan_hash
            or digest(resource_plan) != execution.resource_plan_hash
            or digest({k: v for k, v in resource_plan.items() if k != "plan_hash"})
            != resource_plan["plan_hash"]
        ):
            fail("execution_plan_changed")
        return order, refund, case, execution

    def finish_phase(db, execution, refund, case, status):
        before = dict(status=refund.status, execution_status=execution.status)
        execution.status = execution.resume_status = status
        execution.updated_at = instant
        execution.lease_token = execution.lease_expires_at = None
        hooks["publish"](db, execution, refund, case, status, before)
        db.commit()

    def verify_proof(execution, proof):
        if (not isinstance(proof, dict) or proof.get('refund_id') != refund_id
                or proof.get('resource_plan_hash') != execution.resource_plan_hash
                or not isinstance(proof.get('evidence_fingerprint'), str)
                or len(proof['evidence_fingerprint']) != 64
                or any(c not in '0123456789abcdef' for c in proof['evidence_fingerprint'])):
            fail('refund_resource_proof_invalid')

    try:
        if phase == "prepared":
            with sessions() as db:
                order, refund, case, execution = locked(db)
                hooks["recheck"](db, order, refund)
                finish_phase(db, execution, refund, case, "resources_pending")
        elif phase == "resources_pending":
            with sessions() as db:
                order, refund, case, execution = locked(db)
                hooks["recheck"](db, order, refund)
                db.expunge(execution)
                db.commit()
            # A's immutable Recovery is a durable external-effect receipt, not
            # a funds authorization. It can precede our phase/proof commit; a
            # crash replays the same remote receipt and Recovery. AUTOCOMMIT
            # prevents SELECT/Term locks from spanning the network call.
            with sessions(bind=ns["engine"].execution_options(isolation_level="AUTOCOMMIT")) as reads:
                proof = hooks["adapter"]()["apply"](reads, execution)
            verify_proof(execution, proof)
            with sessions() as db:
                order, refund, case, execution = locked(db)
                persist = hooks["adapter"]().get("persist")
                if persist:
                    persist(db, execution, proof)
                execution.proof_json = canonical(proof)
                finish_phase(db, execution, refund, case, "resources_applied")
        elif phase == "resources_applied":
            with sessions() as db:
                order, refund, case, execution = locked(db)
                if not execution.proof_json:
                    fail("refund_resource_proof_missing")
                verify_proof(execution, json.loads(execution.proof_json))
                finish_phase(db, execution, refund, case, "funds_pending")
        elif phase == "funds_pending":
            with sessions() as db:
                order, refund, case, execution = locked(db)
                if not execution.proof_json:
                    fail("refund_resource_proof_missing")
                verify_proof(execution, json.loads(execution.proof_json))
                rows = list(
                    db.scalars(select(Leg).where(Leg.refund_id == refund_id).order_by(Leg.payment_id))
                )
                expected = json.loads(execution.funding_json)
                current = hooks["funding"](db, order, refund, json.loads(case.plan_json))
                if current != expected:
                    fail("execution_funding_changed")
                if (
                    len(rows) != len(expected)
                    or sum(cents(row.amount) for row in rows) != cents(refund.amount)
                    or {(r.payment_id, r.source_order_id, cents(r.amount), r.currency) for r in rows}
                    != {
                        (r["payment_id"], r["source_order_id"], cents(r["amount"]), r["currency"])
                        for r in expected
                    }
                ):
                    fail("execution_funding_changed")
                payments = list(
                    db.scalars(
                        select(ns["Payment"])
                        .where(ns["Payment"].id.in_([row.payment_id for row in rows]))
                        .order_by(ns["Payment"].id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                )
                if len(payments) != len(rows):
                    fail("execution_payment_missing")
                for payment in payments:
                    leg = next(row for row in rows if row.payment_id == payment.id)
                    source = db.get(ns["Order"], payment.order_id)
                    source_currency = json.loads(source.economic_snapshot_json).get("currency") if source else None
                    total = ns["refund_funding_store"]["payment_reserved_total"](db, payment.id)
                    currency = json.loads(case.plan_json)["economic_plan"]["economic_snapshot"]["currency"]
                    if (
                        cents(total) > cents(payment.amount)
                        or payment.status not in ("paid", "refunding", "refunded")
                        or not payment.paid_at
                        or source is None
                        or payment.order_id != leg.source_order_id
                        or source.buyer_enterprise_id != order.buyer_enterprise_id
                        or source.product_id != order.product_id
                        or source.provider_enterprise_id != order.provider_enterprise_id
                        or cents(source.paid_amount) != cents(payment.amount)
                        or leg.currency != currency
                        or source_currency not in (None, currency)
                        or (source_currency is None and source.business_type != "version_change_payment")
                        or (payment.status == "refunded" and leg.status == "prepared")
                    ):
                        fail("execution_payment_balance_or_identity_invalid")
                for leg in rows:
                    if leg.status not in ("prepared", "completed"):
                        fail("execution_payment_leg_state_unknown")
                    if leg.status == "completed" and (
                        not leg.completed_at or leg.receipt != "simulated:" + refund_id + ":" + leg.payment_id
                    ):
                        fail("execution_payment_receipt_invalid")
                pending = next((row for row in rows if row.status == "prepared"), None)
                if pending:
                    # The durable unique leg is itself the simulated provider's
                    # idempotency receipt. No external money movement exists.
                    pending.status = "completed"
                    pending.receipt = "simulated:" + refund_id + ":" + pending.payment_id
                    pending.completed_at = instant
                    finish_phase(db, execution, refund, case, "funds_pending")
                else:
                    if db.get(Completion, refund_id) is not None:
                        fail("refund_completion_conflict")
                    refund.status, refund.completed_at = "completed", instant
                    refund.completed_by = "refund-execution-worker:simulated"
                    order.refunded_amount = _money(cents(order.refunded_amount) + cents(refund.amount))
                    execution.completed_at = instant
                    db.add(
                        Completion(
                            refund_id=refund_id,
                            execution_id=execution.id,
                            plan_hash=execution.plan_hash,
                            proof_hash=digest(json.loads(execution.proof_json)),
                            funding_hash=digest(expected),
                            economic_plan_json=canonical(json.loads(case.plan_json)["economic_plan"]),
                            amount=_money(cents(refund.amount)),
                            completed_at=instant,
                        )
                    )
                    finish_phase(db, execution, refund, case, "completed")
        return True
    except Exception as exc:
        # Never persist exception messages, provider replies, or secrets.
        logger.warning("Ordinary refund phase failed: %s", type(exc).__name__)
        with sessions() as db:
            peek = db.get(ns["Refund"], refund_id)
            ns["refund_workflow"]["lock_order"](db, peek.order_id)
            execution = db.scalar(
                select(Execution)
                .where(Execution.id == execution_id, Execution.lease_token == token)
                .with_for_update()
            )
            if execution is None:
                return True
            refund = db.get(ns["Refund"], refund_id)
            case = db.get(ns["refund_workflow"]["Case"], refund_id)
            before = dict(status=refund.status, execution_status=execution.status)
            execution.resume_status = phase
            execution.status, refund.status = "failed", "failed"
            execution.error_code = (
                "execution_revalidation_failed"
                if isinstance(exc, HTTPException)
                else "execution_phase_failed"
            )
            execution.lease_token = execution.lease_expires_at = None
            hooks["publish"](db, execution, refund, case, "failed", before)
            db.commit()
        return True


def main():
    interval = float(os.environ.get("REFUND_WORKER_INTERVAL", "2"))
    if not 0.1 <= interval <= 60:
        raise ValueError("Invalid refund worker interval")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            process_once()
            HEARTBEAT.touch()
        except Exception:
            logger.warning("Refund worker iteration failed")
        stop.wait(interval)


if __name__ == "__main__":
    main()
