"""SQLite HTTP integration with real funding graph/holds and isolated economics.

The Window adapter and economic approval proof are explicit test seams. Payment
discovery, source verification, allocation, balances, audit and outbox are real.
This is not PostgreSQL concurrency or external payment execution evidence.
"""

from decimal import Decimal
import hashlib
import importlib
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select

import test_refund_funding_store as store_fixture
import test_refund_planning as planning


class RefundFundingHTTPIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(store_fixture.RefundFundingStoreTests.setUpClass.__func__)
    tearDownClass = classmethod(store_fixture.RefundFundingStoreTests.tearDownClass.__func__)
    tearDown = store_fixture.RefundFundingStoreTests.tearDown
    request = store_fixture.RefundFundingStoreTests.request
    create_order = store_fixture.RefundFundingStoreTests.create_order
    monthly = store_fixture.RefundFundingStoreTests.monthly
    order = store_fixture.RefundFundingStoreTests.order
    create = planning.RefundPlanningHTTPTests.create
    review = planning.RefundPlanningHTTPTests.review
    manual = planning.RefundPlanningHTTPTests.manual

    def setUp(self):
        store_fixture.RefundFundingStoreTests.setUp(self)
        Window = self.segment_store["Window"]
        self.no_window = False
        adapters = patch.dict(
            vars(self.m),
            refund_segment_store={
                **self.segment_store,
                "latest": lambda db, order: None if self.no_window else db.get(Window, "window-1"),
            },
            prepare_refund_plan=self.economic_proof,
            refund_planning_status=lambda db, order, refund: {"status": "available"},
        )
        adapters.start()
        self.addCleanup(adapters.stop)

    def economic_proof(self, db, order, refund):
        Plan = importlib.import_module(self.m.__package__ + ".refund_schema").PreparedRefundPlan
        return Plan(
            refund_id=refund.id,
            order_id=order.id,
            payment_id=refund.payment_id,
            amount=Decimal(str(refund.amount)),
            calculation_version="funding-integration-v1",
            as_of=self.m.now(),
            evidence_ids=("isolated-economic-approval-proof",),
            basis="manual",
            economic_plan={
                "source_order_id": order.id,
                "basis": "manual",
                "funding_allocations": list(self.h["frozen"](db, refund)),
                "cost": "PRIVATE_COST",
            },
        )

    def test_http_request_approval_replay_and_audit_share_immutable_allocations(self):
        requested = self.create(amount="120")
        self.assertEqual(self.create(amount="120")["id"], requested["id"])
        approved = self.review(requested, decision="approve")
        self.assertEqual(approved["status"], "approved")
        self.assertFalse(approved["execution_available"])
        self.assertNotIn("PRIVATE_COST", json.dumps(approved))
        self.assertEqual(self.review(requested, decision="approve")["id"], approved["id"])
        with self.m.SessionLocal() as db:
            refund = db.get(self.m.Refund, approved["id"])
            expected = list(self.h["frozen"](db, refund))
            self.assertEqual([item["amount"] for item in expected], ["50.00", "70.00"])
            case = db.get(self.Case, refund.id)
            self.assertEqual(hashlib.sha256(case.plan_json.encode()).hexdigest(), case.plan_hash)
            envelope = json.loads(case.plan_json)
            self.assertEqual(envelope["funding_allocations"], expected)
            self.assertEqual(envelope["economic_plan"]["funding_allocations"], expected)
            self.assertEqual(expected[0]["window_hash"], "a" * 64)
            for audit in db.scalars(
                select(self.m.AuditLog).where(
                    self.m.AuditLog.target_id == refund.id,
                    self.m.AuditLog.action.in_(("ordinary_refund_request", "ordinary_refund_approve")),
                )
            ):
                after = json.loads(audit.after_json)
                self.assertEqual(after["funding_state"], "held")
                self.assertEqual(after["funding_allocations"], expected)
                self.assertEqual(
                    after["funding_hash"],
                    hashlib.sha256(
                        importlib.import_module(self.m.__package__ + ".refund_workflow")
                        .canonical(expected)
                        .encode()
                    ).hexdigest(),
                )
            self.assertEqual(len(list(db.scalars(select(self.A)))), 2)
            self.assertEqual(len(list(db.scalars(select(self.Event)))), 2)

    def test_rejection_releases_holds_and_preserves_audit_and_rows(self):
        row = self.create(amount="120")
        self.review(row, decision="reject")
        with self.m.SessionLocal() as db:
            self.assertEqual(self.h["payment_reserved_total"](db, "paid-source"), Decimal("0"))
            self.assertEqual(len(list(db.scalars(select(self.A)))), 2)
            audit = db.scalar(
                select(self.m.AuditLog).where(self.m.AuditLog.action == "ordinary_refund_reject")
            )
            self.assertEqual(json.loads(audit.after_json)["funding_state"], "released")
        self.create(amount="120", idempotency_key="after-rejection-new")

    def test_request_audit_or_outbox_failure_rolls_back_new_refund_and_holds(self):
        with self.m.SessionLocal() as db:
            outbox_before = len(list(db.scalars(select(self.Outbox))))
        for name in ("audit", "notify_business_event"):
            with self.subTest(name=name):
                with patch.object(self.m, name, side_effect=RuntimeError("isolated failure")):
                    self.create(amount="120", expected=500)
                with self.m.SessionLocal() as db:
                    self.assertEqual(list(db.scalars(select(self.A))), [])
                    self.assertEqual(list(db.scalars(select(self.m.Refund))), [])
                    self.assertEqual(list(db.scalars(select(self.Case))), [])
                    self.assertEqual(list(db.scalars(select(self.Event))), [])
                    self.assertEqual(len(list(db.scalars(select(self.Outbox)))), outbox_before)

    def test_legacy_cap_and_modern_shared_holds_use_same_payment_balance(self):
        self.no_window = True
        self.create(amount="101", expected=409)
        self.create(amount="20")
        with self.m.SessionLocal() as db:
            self.assertEqual(list(db.scalars(select(self.A))), [])
        self.no_window = False
        row = self.create(amount="130", idempotency_key="modern-after-legacy")
        with self.m.SessionLocal() as db:
            self.assertEqual(self.h["payment_reserved_total"](db, "paid-source"), Decimal("100"))
        self.create(amount="1", idempotency_key="over-shared-balance", expected=409)
        self.review(row, decision="approve")

    def test_existing_allocation_without_window_cannot_fall_back_on_approval(self):
        row = self.create(amount="20")
        self.no_window = True
        self.review(row, decision="approve", expected=409)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Refund, row["id"]).status, "requested")
            self.assertIsNone(db.get(self.Case, row["id"]).plan_hash)

    def test_open_version_change_blocks_request_review_and_manual(self):
        row = self.create(amount="20")
        with self.m.SessionLocal() as db:
            db.get(self.Change, "upgrade").status = "resource_pending"
            db.commit()
        self.create(amount="1", idempotency_key="during-version-change", expected=409)
        self.review(row, decision="approve", expected=409)
        self.manual(row, expected=409)
        # Recorded replay succeeds without attempting another reservation.
        self.assertEqual(self.create(amount="20")["id"], row["id"])

    def test_ordinary_refund_blocks_new_version_change_before_quote_validation(self):
        row = self.create(amount="20")
        claims = {
            "purpose": "version_change_quote",
            "sub": "buyer",
            "order_id": self.order_id,
            "aud": "market-version-change-quote",
        }
        token = self.m.jwt.encode(claims, self.m.JWT_SECRET, algorithm="HS256")
        for status in ("pending", "requested", "approved", "executing", "failed", "completed"):
            with self.subTest(status=status):
                with self.m.SessionLocal() as db:
                    db.get(self.m.Refund, row["id"]).status = status
                    db.commit()
                response = self.request(
                    "POST",
                    f"/api/orders/{self.order_id}/version-changes",
                    expected=409,
                    json={"quote_token": token, "idempotency_key": "blocked-by-ordinary"},
                )
                self.assertIn("普通退款", response["detail"])

    def test_saga_downgrade_uses_its_existing_hold_once_and_rejects_overbook(self):
        self.create(amount="120")
        for amount in ("31", "30"):
            with self.subTest(amount=amount), self.m.SessionLocal() as db:
                source = db.get(self.Change, "upgrade")
                values = {
                    column.name: getattr(source, column.name) for column in self.Change.__table__.columns
                }
                values.update(
                    id="down",
                    kind="downgrade",
                    status="refund_pending",
                    funding_order_id=None,
                    idempotency_key="down",
                    quote_json=json.dumps({"refund_amount": amount}),
                )
                db.add(self.Change(**values))
                db.flush()
                reservation = self.Fund(
                    change_id="down",
                    kind="refund",
                    payment_id="paid-source",
                    amount=Decimal(amount),
                    idempotency_key="down-refund",
                )
                db.add(reservation)
                db.flush()
                change = db.get(self.Change, "down")
                if amount == "31":
                    with self.assertRaises(HTTPException):
                        self.m.version_change_saga["process_downgrade_refunds"](db, change)
                    self.assertIsNone(reservation.refund_id)
                else:
                    self.m.version_change_saga["process_downgrade_refunds"](db, change)
                    self.assertIsNotNone(reservation.refund_id)
                    self.assertEqual(self.h["payment_reserved_total"](db, "paid-source"), Decimal("100"))
                    self.m.version_change_saga["process_downgrade_refunds"](db, change)
                    self.assertEqual(self.h["payment_reserved_total"](db, "paid-source"), Decimal("100"))


if __name__ == "__main__":
    unittest.main()
