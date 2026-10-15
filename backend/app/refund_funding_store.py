"""Transactional ordinary-refund funding holds; no economic approval or execution.

Caller locks Order first and owns commit/rollback. All writers sharing payments
must take the same ascending Payment locks before reading available balances.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from fastapi import HTTPException
from sqlalchemy import ForeignKey, Integer, Numeric, String, UniqueConstraint, event, select
from sqlalchemy.orm import mapped_column

from .refund_funding import FundingSource, FundingUnavailable, _cents, _money, allocate_refund


ACTIVE = ("pending", "requested", "approved", "executing", "failed", "completed")
CONFIRMED = ("paid", "refunding", "refunded")


def fail(reason):
    raise HTTPException(409, reason)


def cents(value):
    try:
        result = _cents(Decimal(str(value)))
        if result > 99999999999999:
            raise ValueError
        return result
    except (ValueError, ArithmeticError):
        fail("funding_invalid_money")


def install(ns):
    if "refund_funding_store" in ns:
        return ns["refund_funding_store"]
    Order, Payment, Refund = (ns[key] for key in ("Order", "Payment", "Refund"))
    terms, saga = ns["subscription_terms"], ns["version_change_saga"]
    Term, Revision = terms["Term"], terms["Revision"]
    Change, Fund = saga["VersionChangeRequest"], saga["VersionChangeFund"]

    class FundingAllocation(ns["Base"]):
        __tablename__ = "refund_funding_allocations"
        __table_args__ = (UniqueConstraint("refund_id", "payment_id", name="uq_refund_funding_payment"),)
        rowid = mapped_column(Integer, primary_key=True, autoincrement=True)
        refund_id = mapped_column(ForeignKey("refunds.id"), nullable=False, index=True)
        payment_id = mapped_column(ForeignKey("payments.id"), nullable=False, index=True)
        source_order_id = mapped_column(ForeignKey("orders.id"), nullable=False)
        amount = mapped_column(Numeric(14, 2), nullable=False)
        currency = mapped_column(String(3), nullable=False)
        window_id = mapped_column(ForeignKey("refund_segment_windows.id"), nullable=False)
        window_hash = mapped_column(String(64), nullable=False)

    @event.listens_for(FundingAllocation, "before_update")
    @event.listens_for(FundingAllocation, "before_delete")
    def immutable(mapper, connection, target):
        raise ValueError("funding allocations are immutable")

    def context(db, order):
        combo = terms["lock_combination"](db, order.buyer_enterprise_id, order.product_id)
        if combo and db.scalar(
            select(Change.id)
            .where(Change.combination_id == combo.id, Change.status.in_(saga["OPEN_STATUSES"]))
            .limit(1)
        ):
            fail("version_change_in_progress")
        latest = ns.get("refund_segment_store", {}).get("latest")
        window = latest(db, order) if callable(latest) else None
        if window is None:
            return None
        rows = list(db.scalars(select(Term).where(Term.order_id == order.id)))
        if len(rows) != 1 or combo is None:
            fail("funding_term_missing")
        term = rows[0]
        if (
            term.combination_id != combo.id
            or term.generation != combo.generation
            or combo.enterprise_id != order.buyer_enterprise_id
            or combo.product_id != order.product_id
        ):
            fail("funding_generation_mismatch")
        if not db.scalar(select(Revision.id).where(Revision.term_id == term.id).limit(1)):
            return None
        if not window.id or not isinstance(window.payload_hash, str) or len(window.payload_hash) != 64:
            fail("funding_window_invalid")
        return window, term

    def eligible(db, order):
        ctx = context(db, order)
        if ctx is None:
            return False
        sources(db, order, ctx[1], None)
        return True

    def currency(order):
        try:
            value = json.loads(order.economic_snapshot_json)["currency"]
            if len(value) != 3 or not all("A" <= c <= "Z" for c in value):
                raise ValueError
            return value
        except (ValueError, TypeError, KeyError):
            fail("funding_currency_missing")

    def allocations(db, refund):
        return list(
            db.scalars(
                select(FundingAllocation)
                .where(FundingAllocation.refund_id == refund.id)
                .order_by(FundingAllocation.rowid)
            )
        )

    def payment_reserved(db, payment_id, exclude=None):
        """Allocation-only occupied Decimal amount; caller serializes Payment."""
        rows = db.execute(
            select(FundingAllocation, Refund)
            .join(Refund, Refund.id == FundingAllocation.refund_id)
            .where(
                FundingAllocation.payment_id == payment_id,
                Refund.status.in_(ACTIVE),
                Refund.id != (getattr(exclude, "id", exclude) or ""),
            )
        ).all()
        for row, refund in rows:
            own = allocations(db, refund)
            if sum(cents(item.amount) for item in own) != cents(refund.amount) or len(
                {item.payment_id for item in own}
            ) != len(own):
                fail("funding_hold_evidence_invalid")
        return _money(sum(cents(row.amount) for row, refund in rows))

    def spent(db, payment, exclude):
        excluded = getattr(exclude, "id", exclude) or ""
        unknown = db.scalar(
            select(Refund.id)
            .where(Refund.payment_id == payment.id, Refund.status.not_in((*ACTIVE, "rejected")))
            .limit(1)
        )
        unknown_allocation = db.scalar(
            select(FundingAllocation.rowid)
            .join(Refund, Refund.id == FundingAllocation.refund_id)
            .where(FundingAllocation.payment_id == payment.id, Refund.status.not_in((*ACTIVE, "rejected")))
            .limit(1)
        )
        if unknown or unknown_allocation:
            fail("funding_refund_state_unknown")
        total = cents(payment_reserved(db, payment.id, excluded))
        for refund in db.scalars(
            select(Refund).where(
                Refund.payment_id == payment.id, Refund.status.in_(ACTIVE), Refund.id != excluded
            )
        ):
            if not db.scalar(
                select(FundingAllocation.rowid).where(FundingAllocation.refund_id == refund.id).limit(1)
            ):
                total += cents(refund.amount)
        for fund in db.scalars(select(Fund).where(Fund.payment_id == payment.id, Fund.kind == "refund")):
            change = db.get(Change, fund.change_id)
            if change is None:
                fail("funding_refund_evidence_missing")
            if fund.refund_id:
                refund = db.get(Refund, fund.refund_id)
                if (
                    refund is None
                    or refund.payment_id != payment.id
                    or cents(refund.amount) != cents(fund.amount)
                    or (change.status == "completed" and refund.status != "completed")
                ):
                    fail("funding_refund_link_invalid")
            elif change.status in saga["OPEN_STATUSES"]:
                total += cents(fund.amount)
            elif change.status == "completed":
                fail("funding_completed_refund_link_missing")
            elif change.status != "compensated":
                fail("funding_refund_state_unknown")
        return total

    def verify_upgrade(db, change, order, term, revision):
        ledger = ns["version_change_ledger"]["Model"]
        origin = list(
            db.scalars(
                select(Term).where(
                    Term.order_id == change.source_order_id,
                    Term.combination_id == term.combination_id,
                    Term.generation == term.generation,
                )
            )
        )
        origin_order = db.get(Order, change.source_order_id)
        target = list(
            db.scalars(
                select(ledger).where(
                    ledger.change_id == change.id, ledger.source_order_id == order.id, ledger.side == "target"
                )
            )
        )
        if (
            len(origin) != 1
            or origin_order is None
            or origin_order.buyer_enterprise_id != order.buyer_enterprise_id
            or origin_order.product_id != order.product_id
            or not change.resource_applied
            or change.enterprise_id != order.buyer_enterprise_id
            or len(target) != 1
            or target[0].combination_id != term.combination_id
            or target[0].status != "posted"
        ):
            fail("funding_upgrade_evidence_invalid")
        try:
            snapshots = json.loads(change.target_snapshots_json)
            frozen_target = snapshots[term.id]
            economic = json.loads(revision.economic_snapshot_json)
            delivery = json.loads(revision.delivery_snapshot_json)
            if (
                change.target_version_id != revision.version_id
                or change.target_version_code != revision.version_code
                or not isinstance(economic, dict)
                or not isinstance(delivery, dict)
                or economic.get("currency") != currency(order)
                or not isinstance(frozen_target, dict)
                or frozen_target.get("economic") != economic
                or frozen_target.get("delivery") != delivery
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            fail("funding_request_revision_mismatch")
        verifier = terms["verified_revision"]
        with localcontext() as ctx:
            ctx.prec = 40
            try:
                verified = verifier(db, revision, order, term)
            except (ValueError, TypeError, KeyError, ArithmeticError):
                fail("funding_revision_unverified")
        if not verified:
            fail("funding_revision_unverified")

    def sources(db, order, term, exclude):
        pool_terms = list(
            db.scalars(
                select(Term).where(
                    Term.combination_id == term.combination_id, Term.generation == term.generation
                )
            )
        )
        roots, expected = {}, {}
        for member in pool_terms:
            root = db.get(Order, member.order_id)
            if (
                root is None
                or root.id in roots
                or root.buyer_enterprise_id != order.buyer_enterprise_id
                or root.product_id != order.product_id
                or root.provider_enterprise_id != order.provider_enterprise_id
                or root.delivery_status not in {"pending_acceptance", "accepted"}
                or currency(root) != currency(order)
                or root.business_type == "version_change_payment"
            ):
                fail("funding_pool_root_invalid")
            roots[root.id] = (root, member)
            ids = list(
                db.scalars(
                    select(Payment.id).where(Payment.order_id == root.id, Payment.status.in_(CONFIRMED))
                )
            )
            if len(ids) != 1 or ids[0] in expected:
                fail("funding_anchor_ambiguous")
            expected[ids[0]] = None
        if order.id not in roots:
            fail("funding_pool_anchor_missing")
        for change in db.scalars(
            select(Change).where(
                Change.combination_id == term.combination_id,
                Change.kind == "upgrade",
                Change.status == "completed",
            )
        ):
            try:
                snapshots = json.loads(change.target_snapshots_json)
                if not isinstance(snapshots, dict):
                    raise ValueError
            except (ValueError, TypeError):
                fail("funding_upgrade_target_missing")
            applicable = []
            Ledger = ns["version_change_ledger"]["Model"]
            for root, member in roots.values():
                revisions = list(
                    db.scalars(
                        select(Revision).where(Revision.change_id == change.id, Revision.term_id == member.id)
                    )
                )
                target_exists = db.scalar(
                    select(Ledger.id)
                    .where(
                        Ledger.change_id == change.id,
                        Ledger.source_order_id == root.id,
                        Ledger.side == "target",
                    )
                    .limit(1)
                )
                if revisions or member.id in snapshots or target_exists:
                    if len(revisions) != 1:
                        fail("funding_revision_missing")
                    verify_upgrade(db, change, root, member, revisions[0])
                    applicable.append(member.id)
            if not applicable:
                if change.source_order_id in roots:
                    fail("funding_revision_missing")
                continue
            funds = list(db.scalars(select(Fund).where(Fund.change_id == change.id, Fund.kind == "payment")))
            if len(funds) != 1 or not funds[0].payment_id or funds[0].payment_id in expected:
                fail("funding_payment_evidence_invalid")
            expected[funds[0].payment_id] = (change, funds[0])
        payments = list(
            db.scalars(
                select(Payment)
                .where(Payment.id.in_(expected))
                .order_by(Payment.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(payments) != len(expected):
            fail("funding_payment_missing")
        result = []
        for payment in payments:
            source = db.get(Order, payment.order_id)
            proof = expected[payment.id]
            if (
                source is None
                or source.buyer_enterprise_id != order.buyer_enterprise_id
                or source.product_id != order.product_id
                or source.provider_enterprise_id != order.provider_enterprise_id
                or payment.status not in CONFIRMED
                or source.payment_status not in CONFIRMED
                or not isinstance(payment.paid_at, datetime)
                or cents(payment.amount) != cents(source.paid_amount)
            ):
                fail("funding_payment_invalid")
            paid_at = payment.paid_at
            # SQLite drops timezone on Payment's DateTime; production PostgreSQL
            # preserves it. Test storage values are UTC, like the main model.
            if paid_at.tzinfo is None and db.bind.dialect.name == "sqlite":
                paid_at = paid_at.replace(tzinfo=timezone.utc)
            if paid_at.tzinfo is None or paid_at > ns["now"]():
                fail("funding_payment_date_invalid")
            if proof and (
                proof[0].funding_order_id != source.id
                or source.business_type != "version_change_payment"
                or source.related_order_id != proof[0].source_order_id
                or cents(proof[1].amount) != cents(payment.amount)
                or cents(source.amount) != cents(payment.amount)
            ):
                fail("funding_fund_mismatch")
            if proof:
                try:
                    quote = json.loads(proof[0].quote_json)
                    snapshot = json.loads(source.economic_snapshot_json)
                    if (
                        not isinstance(snapshot, dict)
                        or quote["currency"] != currency(order)
                        or cents(quote["payment_amount"]) != cents(payment.amount)
                        or ("currency" in snapshot and snapshot["currency"] != currency(order))
                    ):
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    fail("funding_currency_mismatch")
            elif currency(source) != currency(order):
                fail("funding_currency_mismatch")
            reconcile(db, source)
            used = spent(db, payment, exclude)
            if used > cents(payment.amount):
                fail("funding_balance_inconsistent")
            if "refunded" in (payment.status, source.payment_status) and completed_spent(
                db, payment
            ) != cents(payment.amount):
                fail("funding_refunded_payment_evidence_missing")
            result.append(
                FundingSource(
                    payment.id,
                    source.id,
                    paid_at,
                    _money(cents(payment.amount)),
                    Decimal("0"),
                    _money(used),
                    currency(order),
                )
            )
        return result

    def completed_spent(db, payment):
        allocated = sum(
            cents(row.amount)
            for row in db.scalars(
                select(FundingAllocation)
                .join(Refund, Refund.id == FundingAllocation.refund_id)
                .where(FundingAllocation.payment_id == payment.id, Refund.status == "completed")
            )
        )
        legacy = sum(
            cents(row.amount)
            for row in db.scalars(
                select(Refund).where(Refund.payment_id == payment.id, Refund.status == "completed")
            )
            if not allocations(db, row)
        )
        return allocated + legacy

    def payment_reserved_total(db, payment_id, exclude=None):
        """All evidenced occupied funds, including completed; caller locks Payment.

        This replaces, rather than supplements, Saga's legacy refund subtraction.
        """
        payment = db.get(Payment, payment_id)
        if payment is None:
            fail("funding_payment_missing")
        total = spent(db, payment, exclude)
        if total > cents(payment.amount):
            fail("funding_balance_inconsistent")
        return _money(total)

    def reconcile(db, source):
        # The original order can retain the full compatibility-anchor refund,
        # while funding orders record their actual allocated share. Both forms
        # require completed Refund evidence; neither affects payment accounting.
        completed = list(
            db.scalars(select(Refund).where(Refund.order_id == source.id, Refund.status == "completed"))
        )
        anchored = sum(cents(row.amount) for row in completed)
        legacy = sum(cents(row.amount) for row in completed if not allocations(db, row))
        allocated = sum(
            cents(row.amount)
            for row in db.scalars(
                select(FundingAllocation)
                .join(Refund, Refund.id == FundingAllocation.refund_id)
                .where(FundingAllocation.source_order_id == source.id, Refund.status == "completed")
            )
        )
        if cents(source.refunded_amount) > max(anchored, allocated + legacy):
            fail("funding_historical_refund_evidence_missing")

    def checked(db, order, payment, amount, exclude=None, *, allocate=True):
        ctx = context(db, order)
        if ctx is None:
            return None
        if payment is None or payment.order_id != order.id:
            fail("funding_anchor_invalid")
        rows = sources(db, order, ctx[1], exclude)
        if payment.id not in {row.payment_id for row in rows if row.source_order_id == order.id}:
            fail("funding_anchor_invalid")
        try:
            plan = (
                allocate_refund(_money(cents(amount)), rows, currency=currency(order)) if allocate else None
            )
        except FundingUnavailable as exc:
            fail(str(exc))
        return ctx[0], rows, plan

    def validate(db, order, payment, amount, exclude=None):
        result = checked(db, order, payment, amount, exclude)
        return True if result else None

    def frozen(db, refund):
        return tuple(
            dict(
                payment_id=row.payment_id,
                source_order_id=row.source_order_id,
                amount=format(row.amount, ".2f"),
                currency=row.currency,
                window_id=row.window_id,
                window_hash=row.window_hash,
            )
            for row in allocations(db, refund)
        )

    def verify_existing(db, order, refund, result):
        window, sources_, plan = result
        rows = allocations(db, refund)
        available = {row.payment_id: row for row in sources_}
        if not rows or sum(cents(row.amount) for row in rows) != cents(refund.amount):
            fail("funding_allocation_amount_mismatch")
        for row in rows:
            source = available.get(row.payment_id)
            if (
                source is None
                or row.source_order_id != source.source_order_id
                or row.currency != source.currency
                or row.window_id != window.id
                or row.window_hash != window.payload_hash
                or cents(row.amount) == 0
                or cents(row.amount) + cents(source.reserved) > cents(source.paid_amount)
            ):
                fail("funding_allocation_identity_mismatch")
        return frozen(db, refund)

    def reserve(db, order, refund):
        if refund.order_id != order.id:
            fail("funding_refund_order_mismatch")
        existing = allocations(db, refund)
        result = checked(
            db, order, db.get(Payment, refund.payment_id), refund.amount, refund.id, allocate=not existing
        )
        if result is None:
            if allocations(db, refund):
                fail("funding_window_missing")
            return None
        if existing:
            return verify_existing(db, order, refund, result)
        window, _, plan = result
        for row in plan:
            db.add(
                FundingAllocation(
                    refund_id=refund.id,
                    payment_id=row.payment_id,
                    source_order_id=row.source_order_id,
                    amount=row.amount,
                    currency=row.currency,
                    window_id=window.id,
                    window_hash=window.payload_hash,
                )
            )
        db.flush()
        return frozen(db, refund)

    def freeze(db, order, refund):
        if refund.order_id != order.id:
            fail("funding_refund_order_mismatch")
        result = checked(
            db, order, db.get(Payment, refund.payment_id), refund.amount, refund.id, allocate=False
        )
        if result is None:
            if allocations(db, refund):
                fail("funding_window_missing")
            return None
        return verify_existing(db, order, refund, result)

    hooks = dict(
        FundingAllocation=FundingAllocation,
        eligible=eligible,
        validate=validate,
        reserve=reserve,
        frozen=frozen,
        freeze=freeze,
        payment_reserved=payment_reserved,
        payment_reserved_total=payment_reserved_total,
    )
    ns["refund_funding_store"] = hooks
    return hooks
