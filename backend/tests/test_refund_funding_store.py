"""Isolated SQLite store integration, real source graph; no PostgreSQL concurrency proof."""

from datetime import timedelta
from decimal import Decimal, localcontext
import importlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

import test_refund_planning as planning


class RefundFundingStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        planning.RefundPlanningHTTPTests.setUpClass.__func__(cls)
        module = importlib.import_module(cls.m.__package__ + ".refund_funding_store")
        cls.h = module.install(vars(cls.m))

    tearDownClass = classmethod(planning.RefundPlanningHTTPTests.tearDownClass.__func__)
    tearDown = planning.RefundPlanningHTTPTests.tearDown
    request = planning.RefundPlanningHTTPTests.request
    create_order = planning.RefundPlanningHTTPTests.create_order
    monthly = planning.RefundPlanningHTTPTests.monthly

    def setUp(self):
        planning.RefundPlanningHTTPTests.setUp(self)
        self.monthly()
        self.A = self.h["FundingAllocation"]
        self.A.__table__.create(self.m.engine, checkfirst=True)
        self.window = SimpleNamespace(id="window-1", payload_hash="a" * 64)
        self.segment_store = self.m.refund_segment_store
        stub = patch.dict(vars(self.m), refund_segment_store={"latest": lambda db, order: self.window})
        stub.start()
        self.addCleanup(stub.stop)
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            order.paid_amount = 100
            order.delivery_status = "accepted"
            base = db.get(self.m.Payment, "paid-source")
            base.paid_at = self.at - timedelta(days=10)
            economic = json.loads(order.economic_snapshot_json)
            economic.update(amount="150.00", unit_price="150.00", product_version_id="v2", version_code="v2")
            delivery = json.loads(order.delivery_snapshot_json)
            source = self.m.Order(
                id="upgrade-order",
                order_no="UPGRADE",
                buyer_user_id="buyer",
                buyer_enterprise_id=order.buyer_enterprise_id,
                provider_enterprise_id=order.provider_enterprise_id,
                product_id=order.product_id,
                buyer_name="Buyer",
                product_name="Product",
                payment_status="paid",
                business_type="version_change_payment",
                related_order_id=order.id,
                amount=50,
                paid_amount=50,
                snapshot_version=0,
                economic_snapshot_json="{}",
            )
            db.add(source)
            db.flush()
            db.add(
                self.m.Payment(
                    id="upgrade-payment",
                    order_id=source.id,
                    payment_no="UPGRADE-PAY",
                    amount=50,
                    status="paid",
                    paid_at=self.at - timedelta(days=1),
                )
            )
            self.Change = self.m.version_change_saga["VersionChangeRequest"]
            self.Fund = self.m.version_change_saga["VersionChangeFund"]
            db.add(
                self.Change(
                    id="upgrade",
                    source_order_id=order.id,
                    enterprise_id=order.buyer_enterprise_id,
                    combination_id="refund-combo",
                    target_version_id="v2",
                    target_version_code="v2",
                    old_version_id="version",
                    old_version_code="v1",
                    kind="upgrade",
                    status="completed",
                    resource_applied=True,
                    funding_order_id=source.id,
                    idempotency_key="upgrade",
                    request_hash="a" * 64,
                    quote_json=json.dumps({"currency": economic["currency"], "payment_amount": "50.00"}),
                    prepared_json="{}",
                    target_snapshots_json=json.dumps(
                        {"refund-segment": {"economic": economic, "delivery": delivery, "integration": {}}}
                    ),
                    created_by="buyer",
                )
            )
            db.flush()
            db.add(
                self.Fund(
                    change_id="upgrade",
                    kind="payment",
                    payment_id="upgrade-payment",
                    amount=50,
                    idempotency_key="upgrade-payment",
                )
            )
            Revision = self.m.subscription_terms["Revision"]
            db.add(
                Revision(
                    change_id="upgrade",
                    term_id="refund-segment",
                    revision_number=1,
                    economic_snapshot_json=json.dumps(economic),
                    delivery_snapshot_json=json.dumps(delivery),
                    version_id="v2",
                    version_code="v2",
                )
            )
            Ledger = self.m.version_change_ledger["Model"]
            db.add(
                Ledger(
                    change_id="upgrade",
                    request_change_id="upgrade",
                    source_order_id=order.id,
                    combination_id="refund-combo",
                    side="target",
                    revenue=150,
                    cost=economic["total_cost"],
                    profit=0,
                    currency=economic["currency"],
                    original_version_id="version",
                    target_version_id="v2",
                    rule_json=json.dumps(
                        economic["settlement_rule"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ),
                    effective_at=self.at - timedelta(days=14),
                    status="posted",
                    payload_fingerprint="b" * 64,
                )
            )
            db.flush()
            db.add(
                self.m.ApiGatewayRoute(
                    id="funding-route",
                    product_id=order.product_id,
                    route_key="funding-route",
                    upstream_url="https://example.invalid",
                )
            )
            db.flush()
            db.add(
                self.m.ApiCredential(
                    id="funding-credential",
                    route_id="funding-route",
                    enterprise_id=order.buyer_enterprise_id,
                    order_id=order.id,
                    key_hash="f" * 64,
                )
            )
            db.flush()
            db.add(
                self.segment_store["Receipt"](
                    id="funding-receipt",
                    change_id="upgrade",
                    credential_id="funding-credential",
                    direction="target",
                    receipt_json="{}",
                    receipt_hash="c" * 64,
                    cutover_at=self.at,
                    source_run_id="d" * 40,
                    boundary_id="1-0",
                    target_fingerprint="e" * 64,
                )
            )
            db.flush()
            revision = db.scalar(select(Revision))
            db.add(
                self.segment_store["Window"](
                    id="window-1",
                    order_id=order.id,
                    term_id="refund-segment",
                    change_id="upgrade",
                    receipt_id="funding-receipt",
                    revision_id=revision.id,
                    revenue=150,
                    cost=10,
                    currency="CNY",
                    starts_at=self.at,
                    ends_at=self.at + timedelta(days=14),
                    available_quota=100,
                    economic_snapshot_json=json.dumps(economic),
                    delivery_snapshot_json=json.dumps(delivery),
                    evidence_json="{}",
                    payload_hash="a" * 64,
                )
            )
            db.commit()

    def refund(
        self, db, amount="120", key="refund", order_id=None, payment_id="paid-source", status="requested"
    ):
        row = self.m.Refund(
            id=key,
            refund_no=key,
            order_id=order_id or self.order_id,
            payment_id=payment_id,
            amount=Decimal(amount),
            status=status,
        )
        db.add(row)
        db.flush()
        return row

    def order(self, db):
        return db.scalar(select(self.m.Order).where(self.m.Order.id == self.order_id).with_for_update())

    def test_lifo_replay_freeze_and_window_binding(self):
        with self.m.SessionLocal() as db:
            order = self.order(db)
            refund = self.refund(db)
            result = self.h["reserve"](db, order, refund)
            self.assertEqual(
                [(r["payment_id"], r["amount"]) for r in result],
                [("upgrade-payment", "50.00"), ("paid-source", "70.00")],
            )
            self.assertEqual(result, self.h["reserve"](db, order, refund))
            self.assertEqual(result, self.h["freeze"](db, order, refund))
            self.assertEqual(self.h["payment_reserved"](db, "paid-source"), Decimal("70"))
            self.window = SimpleNamespace(id="window-2", payload_hash="b" * 64)
            with self.assertRaises(HTTPException):
                self.h["freeze"](db, order, refund)

    def test_cross_order_holds_rejection_release_and_no_anchor_double_count(self):
        with self.m.SessionLocal() as db:
            order = self.order(db)
            first = self.refund(db, "120")
            self.h["reserve"](db, order, first)
            self.refund(db, "31", "foreign", "upgrade-order", "paid-source")
            with self.assertRaises(HTTPException):
                self.h["validate"](db, order, db.get(self.m.Payment, "paid-source"), Decimal("1"))
            first.status = "rejected"
            db.flush()
            self.assertEqual(self.h["payment_reserved"](db, "paid-source"), 0)
            self.assertTrue(
                self.h["validate"](db, order, db.get(self.m.Payment, "paid-source"), Decimal("119"))
            )

    def test_caller_rollback_releases_reservation_after_audit_failure(self):
        with self.m.SessionLocal() as db:
            try:
                self.h["reserve"](db, self.order(db), self.refund(db))
                raise RuntimeError("audit failed")
            except RuntimeError:
                db.rollback()
        with self.m.SessionLocal() as db:
            self.assertEqual(self.h["payment_reserved"](db, "paid-source"), 0)
            self.assertEqual(list(db.scalars(select(self.A))), [])

    def test_completed_downgrade_counted_once_and_unlinked_rejected(self):
        with self.m.SessionLocal() as db:
            order = self.order(db)
            self.refund(db, "20", "down-refund", status="completed")
            # Link to a real completed request; accounting is by actual Refund.
            fund = self.Fund(
                change_id="upgrade",
                kind="refund",
                payment_id="paid-source",
                refund_id="down-refund",
                amount=20,
                idempotency_key="down-refund",
            )
            db.add(fund)
            db.flush()
            self.h["reserve"](db, order, self.refund(db, "130", "remaining"))
            fund.refund_id = None
            db.flush()
            with self.assertRaises(HTTPException):
                self.h["freeze"](db, order, db.get(self.m.Refund, "remaining"))

    def test_source_tampering_rejected(self):
        for field, value in [
            ("buyer_enterprise_id", "provider_tenant"),
            ("economic_snapshot_json", '{"currency":"USD"}'),
            ("paid_amount", 49),
            ("amount", 49),
        ]:
            with self.subTest(field=field), self.m.SessionLocal() as db:
                setattr(db.get(self.m.Order, "upgrade-order"), field, value)
                db.flush()
                with self.assertRaises(HTTPException):
                    self.h["reserve"](db, self.order(db), self.refund(db))
                db.rollback()
        with self.m.SessionLocal() as db:
            db.get(self.m.Payment, "upgrade-payment").paid_at = None
            db.flush()
            with self.assertRaises(HTTPException):
                self.h["reserve"](db, self.order(db), self.refund(db))
        with self.m.SessionLocal() as db:
            db.get(self.m.Payment, "upgrade-payment").paid_at = self.at + timedelta(days=1)
            db.flush()
            with self.assertRaises(HTTPException):
                self.h["reserve"](db, self.order(db), self.refund(db))
        with self.m.SessionLocal() as db:
            Ledger = self.m.version_change_ledger["Model"]
            db.delete(db.scalar(select(Ledger).where(Ledger.change_id == "upgrade")))
            db.flush()
            with self.assertRaises(HTTPException):
                self.h["reserve"](db, self.order(db), self.refund(db))

    def test_sorted_payment_lock_sql_and_exact_decimal(self):
        statements = []
        from sqlalchemy.orm import Session

        original = Session.scalars

        def capture(db, statement, *args, **kwargs):
            sql = str(statement.compile(dialect=postgresql.dialect()))
            if "FOR UPDATE" in sql and "payments" in sql:
                statements.append(sql)
            return original(db, statement, *args, **kwargs)

        with patch.object(Session, "scalars", capture), self.m.SessionLocal() as db:
            with localcontext() as ctx:
                ctx.prec = 4
                self.h["reserve"](db, self.order(db), self.refund(db))
        self.assertTrue(statements)
        self.assertTrue(all("ORDER BY payments.id FOR UPDATE" in sql for sql in statements))

    def test_missing_window_falls_back(self):
        self.window = None
        with self.m.SessionLocal() as db:
            order = self.order(db)
            self.assertFalse(self.h["eligible"](db, order))
            self.assertIsNone(self.h["reserve"](db, order, self.refund(db, "20")))

    def test_historical_gap_on_anchor_or_funding_order_rejected(self):
        for order_id in (self.order_id, "upgrade-order"):
            with self.subTest(order_id=order_id), self.m.SessionLocal() as db:
                db.get(self.m.Order, order_id).refunded_amount = 1
                db.flush()
                with self.assertRaises(HTTPException):
                    self.h["reserve"](db, self.order(db), self.refund(db))

    def test_completed_allocations_reconcile_compatibility_anchor_and_actual_shares(self):
        with self.m.SessionLocal() as db:
            order = self.order(db)
            refund = self.refund(db)
            self.h["reserve"](db, order, refund)
            refund.status = "completed"
            order.refunded_amount = 120
            db.get(self.m.Order, "upgrade-order").refunded_amount = 50
            db.get(self.m.Payment, "upgrade-payment").status = "refunded"
            db.flush()
            self.assertTrue(
                self.h["validate"](db, order, db.get(self.m.Payment, "paid-source"), Decimal("30"))
            )
            self.assertEqual(self.h["payment_reserved_total"](db, "paid-source"), Decimal("70"))
            self.assertEqual(self.h["payment_reserved_total"](db, "upgrade-payment"), Decimal("50"))

    def test_total_hook_includes_legacy_and_open_unlinked_fund_once(self):
        with self.m.SessionLocal() as db:
            order = self.order(db)
            self.h["reserve"](db, order, self.refund(db, "60"))
            self.refund(db, "5", "legacy")
            change = db.get(self.Change, "upgrade")
            change.status = "refund_pending"
            db.add(
                self.Fund(
                    change_id="upgrade",
                    kind="refund",
                    payment_id="paid-source",
                    amount=7,
                    idempotency_key="unlinked-open",
                )
            )
            db.flush()
            self.assertEqual(self.h["payment_reserved"](db, "paid-source"), Decimal("10"))
            self.assertEqual(self.h["payment_reserved_total"](db, "paid-source"), Decimal("22"))
            self.assertEqual(self.h["payment_reserved_total"](db, "paid-source", "refund"), Decimal("12"))
            with self.assertRaises(HTTPException):
                self.h["eligible"](db, order)

    def test_refunding_paid_source_and_refunded_without_completed_evidence(self):
        with self.m.SessionLocal() as db:
            payment = db.get(self.m.Payment, "paid-source")
            payment.status = "refunding"
            db.flush()
            self.h["reserve"](db, self.order(db), self.refund(db))
            payment.status = "refunded"
            db.flush()
            with self.assertRaises(HTTPException):
                self.h["freeze"](db, self.order(db), db.get(self.m.Refund, "refund"))

    def test_exact_max_money_and_invalid_none(self):
        module = importlib.import_module(self.m.__package__ + ".refund_funding_store")
        with localcontext() as ctx:
            ctx.prec = 3
            self.assertEqual(module.cents("999999999999.99"), 99999999999999)
            for value in (None, "1000000000000.00", "0.001", "NaN", "-1"):
                with self.subTest(value=value), self.assertRaises(HTTPException):
                    module.cents(value)

    def test_replay_amount_and_immutable_allocation(self):
        with self.m.SessionLocal() as db:
            order = self.order(db)
            refund = self.refund(db)
            first = self.h["reserve"](db, order, refund)
            # Approval must not call the allocator again, even to compute LIFO.
            module = importlib.import_module(self.m.__package__ + ".refund_funding_store")
            with patch.object(module, "allocate_refund", side_effect=AssertionError("reallocated")):
                self.assertEqual(self.h["freeze"](db, order, refund), first)
                self.assertEqual(self.h["reserve"](db, order, refund), first)
            refund.amount = Decimal("119")
            db.flush()
            with self.assertRaises(HTTPException):
                self.h["freeze"](db, order, refund)
            allocation = db.scalar(select(self.A))
            allocation.amount = Decimal("1")
            with self.assertRaises(ValueError):
                db.flush()

    def test_currency_fund_generation_and_target_proof_fail_closed(self):
        for kind in (
            "quote_currency",
            "quote_amount",
            "fund_amount",
            "generation",
            "target_order",
            "related_order",
            "source_kind",
            "request_target",
            "request_target_code",
            "request_snapshots",
        ):
            with self.subTest(kind=kind), self.m.SessionLocal() as db:
                if kind == "quote_currency":
                    db.get(self.Change, "upgrade").quote_json = '{"currency":"USD","payment_amount":"50.00"}'
                elif kind == "quote_amount":
                    db.get(self.Change, "upgrade").quote_json = '{"currency":"CNY","payment_amount":"49.00"}'
                elif kind == "fund_amount":
                    db.scalar(select(self.Fund).where(self.Fund.kind == "payment")).amount = 49
                elif kind == "generation":
                    db.get(self.m.subscription_terms["Term"], "refund-segment").generation = 2
                elif kind == "related_order":
                    db.get(self.m.Order, "upgrade-order").related_order_id = "foreign"
                elif kind == "source_kind":
                    db.get(self.m.Order, "upgrade-order").business_type = "purchase"
                elif kind == "request_target":
                    db.get(self.Change, "upgrade").target_version_id = "foreign"
                elif kind == "request_target_code":
                    db.get(self.Change, "upgrade").target_version_code = "foreign"
                elif kind == "request_snapshots":
                    db.get(self.Change, "upgrade").target_snapshots_json = "{}"
                else:
                    Ledger = self.m.version_change_ledger["Model"]
                    db.scalar(select(Ledger).where(Ledger.side == "target")).source_order_id = "upgrade-order"
                db.flush()
                with self.assertRaises(HTTPException):
                    self.h["reserve"](db, self.order(db), self.refund(db))

    def test_shared_upgrade_payment_is_isolated_across_two_target_orders(self):
        with self.m.SessionLocal() as db:
            order = self.order(db)

            def clone(model, row, **changes):
                values = {column.name: getattr(row, column.name) for column in model.__table__.columns}
                values.update(changes)
                return model(**values)

            second = clone(self.m.Order, order, id="second-order", order_no="SECOND")
            db.add(second)
            db.flush()
            db.add(
                self.m.Payment(
                    id="second-payment",
                    order_id=second.id,
                    payment_no="SECOND-PAY",
                    amount=100,
                    status="paid",
                    paid_at=self.at - timedelta(days=5),
                )
            )
            Term, Revision = (self.m.subscription_terms[k] for k in ("Term", "Revision"))
            db.add(clone(Term, db.get(Term, "refund-segment"), id="second-term", order_id=second.id))
            db.flush()
            db.add(clone(Revision, db.scalar(select(Revision)), id="second-revision", term_id="second-term"))
            Ledger = self.m.version_change_ledger["Model"]
            db.add(clone(Ledger, db.scalar(select(Ledger)), id="second-target", source_order_id=second.id))
            change = db.get(self.Change, "upgrade")
            snapshots = json.loads(change.target_snapshots_json)
            snapshots["second-term"] = snapshots["refund-segment"]
            change.target_snapshots_json = json.dumps(snapshots)
            change.quote_json = '{"currency":"CNY","payment_amount":"100.00"}'
            db.scalar(select(self.Fund).where(self.Fund.kind == "payment")).amount = 100
            db.get(self.m.Payment, "upgrade-payment").amount = 100
            funding_order = db.get(self.m.Order, "upgrade-order")
            funding_order.amount = funding_order.paid_amount = 100
            db.flush()
            first = self.h["reserve"](db, order, self.refund(db, "140"))
            self.assertEqual(
                [(r["payment_id"], r["amount"]) for r in first],
                [("upgrade-payment", "100.00"), ("second-payment", "40.00")],
            )
            competing = self.refund(db, "140", "second-refund", second.id, "second-payment")
            allocation = self.h["reserve"](db, second, competing)
            self.assertEqual(
                [(r["payment_id"], r["amount"]) for r in allocation],
                [("second-payment", "60.00"), ("paid-source", "80.00")],
            )
            with self.assertRaises(HTTPException):
                with db.begin_nested():
                    self.h["reserve"](db, order, self.refund(db, "21", "third-refund"))
            self.assertEqual(
                sum(
                    self.h["payment_reserved_total"](db, pid)
                    for pid in ("paid-source", "second-payment", "upgrade-payment")
                ),
                Decimal("280"),
            )

    def test_install_reuses_main_model_and_hooks(self):
        module = importlib.import_module(self.m.__package__ + ".refund_funding_store")
        self.assertIs(module.install(vars(self.m)), self.h)
        self.assertIs(module.install(vars(self.m))["FundingAllocation"], self.A)

    def test_other_generation_original_payment_is_not_in_pool(self):
        with self.m.SessionLocal() as db:
            order = self.order(db)
            values = {column.name: getattr(order, column.name) for column in self.m.Order.__table__.columns}
            values.update(id="other-generation", order_no="OTHER-GEN", amount=1000, paid_amount=1000)
            db.add(self.m.Order(**values))
            db.flush()
            Term = self.m.subscription_terms["Term"]
            member = db.get(Term, "refund-segment")
            values = {column.name: getattr(member, column.name) for column in Term.__table__.columns}
            values.update(id="other-generation-term", order_id="other-generation", generation=2)
            db.add(Term(**values))
            db.add(
                self.m.Payment(
                    id="other-generation-payment",
                    order_id="other-generation",
                    payment_no="OTHER-GEN-PAY",
                    amount=1000,
                    status="paid",
                    paid_at=self.at - timedelta(days=1),
                )
            )
            db.flush()
            with self.assertRaises(HTTPException) as error:
                self.h["reserve"](db, order, self.refund(db, "151"))
            self.assertEqual(error.exception.detail, "insufficient_refundable_funding")


if __name__ == "__main__":
    unittest.main()
