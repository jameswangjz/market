"""Monthly settlement acceptance for posted version-change ledger rows."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy import Column, MetaData, String, Table, column, insert

import test_version_change_quotes as fixture


class VersionChangeSettlementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.VersionChangeQuoteTests.setUpClass.__func__(cls)

    tearDownClass = classmethod(fixture.VersionChangeQuoteTests.tearDownClass.__func__)
    setUp = fixture.VersionChangeQuoteTests.setUp
    tearDown = fixture.VersionChangeQuoteTests.tearDown
    request = fixture.VersionChangeQuoteTests.request
    create = fixture.VersionChangeQuoteTests.create
    deliver = fixture.VersionChangeQuoteTests.deliver
    prepare = fixture.VersionChangeQuoteTests.prepare

    def rows(self, model, **filters):
        with self.m.SessionLocal() as db:
            return db.scalars(self.select(model).filter_by(**filters)).all()

    def posted_adjustments(self, change_id="settle-change", posted_at=None):
        clock = posted_at or datetime(2026, 9, 15, tzinfo=timezone.utc)
        oid = self.create()
        self.deliver(oid)
        with self.m.SessionLocal() as db:
            Term = self.m.subscription_terms["Term"]
            term = db.scalar(self.select(Term).where(Term.order_id == oid))
            combo_id = term.combination_id
            rule = json.dumps({"mode": "global", "id": "frozen-rule", "version": "frozen-v1",
                "rates": {"platform_rate": "10", "provider_rate": "60", "service_rate": "20",
                          "expert_rate": "5", "channel_rate": "5"}}, sort_keys=True)
            for side, revenue, cost, profit in (("old", "-10.01", "-2.00", "-8.01"),
                                                 ("target", "15.02", "4.00", "11.02")):
                db.add(self.m.VersionChangeAdjustment(change_id=change_id, request_change_id=change_id,
                    source_order_id=oid, combination_id=combo_id, side=side, revenue=Decimal(revenue),
                    cost=Decimal(cost), profit=Decimal(profit), currency="CNY", original_version_id="v1",
                    target_version_id="v2", rule_json=rule, effective_at=term.starts_at,
                    status="posted", payload_fingerprint="b" * 64, posted_at=clock))
            db.commit()
        return oid

    def monthly(self, key, expected=200, **values):
        return self.request("POST", "/api/settlement-batches", actor="finance", json={
            "cycle": "monthly", "period_start": "2026-09-01T00:00:00+00:00",
            "period_end": "2026-10-01T00:00:00+00:00", "idempotency_key": key} | values,
            expected=expected)

    def fake_fund_table(self):
        table = Table("test_version_change_funds", MetaData(),
            Column("id", String, primary_key=True), Column("change_id", String),
            Column("kind", String), Column("refund_id", String), Column("payment_id", String))
        table.create(self.m.engine)
        previous = self.m.__dict__.get("VersionChangeFund")
        self.m.VersionChangeFund = SimpleNamespace(refund_id=table.c.refund_id,
                                                   payment_id=table.c.payment_id)
        return table, previous

    def test_signed_cents_frozen_rates_and_five_lines_per_side(self):
        self.posted_adjustments()
        with self.m.SessionLocal() as db:
            rows = db.query(self.m.VersionChangeAdjustment).all()
            snapshots = {row.id: row.rule_json for row in rows}
            rule = db.query(self.m.SettlementRule).filter_by(status="active").first()
            rule.platform_rate, rule.provider_rate, rule.service_rate = 99, 1, 0
            db.commit()
        batch = self.monthly("version-change-signed")
        self.assertEqual(batch["version_change_count"], 2)
        self.assertEqual(Decimal(str(batch["total_amount"])), Decimal("5.01"))
        self.assertEqual(Decimal(str(batch["total_profit"])), Decimal("3.01"))
        settlements = self.rows(self.m.Settlement)
        linked = self.rows(self.m.VersionChangeSettlementLink)
        self.assertEqual(len(linked), 2)
        self.assertEqual(sum((Decimal(str(s.net_amount)) for s in settlements), Decimal(0)), Decimal("5.01"))
        self.assertEqual(sum((Decimal(str(s.cost_amount)) for s in settlements), Decimal(0)), Decimal("2.00"))
        self.assertEqual(sum((Decimal(str(s.profit_amount)) for s in settlements), Decimal(0)), Decimal("3.01"))
        lines = self.rows(self.m.SettlementLine, batch_id=batch["id"])
        self.assertEqual(len(lines), 10)
        for settlement in settlements:
            own = [line for line in lines if line.settlement_id == settlement.id]
            self.assertEqual(len(own), 5)
            self.assertEqual(sum((Decimal(str(line.amount)) for line in own), Decimal(0)),
                             Decimal(str(settlement.profit_amount)))
            self.assertTrue(all((line.amount < 0) == (settlement.profit_amount < 0) or line.amount == 0 for line in own))
        by_adjustment = {row.id: row for row in self.rows(self.m.VersionChangeAdjustment)}
        for link in linked:
            adjustment, settlement = by_adjustment[link.adjustment_id], next(s for s in settlements if s.id == link.settlement_id)
            self.assertEqual(settlement.net_amount, adjustment.revenue)
            frozen = json.loads(snapshots[adjustment.id])["rates"]
            platform = next(line.amount for line in lines if line.settlement_id == settlement.id and line.participant_type == "platform")
            self.assertEqual(platform, (adjustment.profit * Decimal(frozen["platform_rate"]) / 100).quantize(Decimal("0.01")))

    def test_posted_at_uses_half_open_month_boundary(self):
        self.posted_adjustments("at-start", datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.posted_adjustments("at-end", datetime(2026, 10, 1, tzinfo=timezone.utc))
        batch = self.monthly("version-change-boundary")
        self.assertEqual(batch["version_change_count"], 2)
        selected = {link.adjustment_id for link in self.rows(self.m.VersionChangeSettlementLink)}
        rows = self.rows(self.m.VersionChangeAdjustment)
        self.assertEqual(selected, {row.id for row in rows if row.change_id == "at-start"})

    def test_pending_rebuild_reuses_financial_identity_and_finalized_batch_is_immutable(self):
        self.posted_adjustments()
        first = self.monthly("version-change-rebuild-a")
        prior_links = {link.adjustment_id: link.settlement_id for link in self.rows(self.m.VersionChangeSettlementLink)}
        second = self.monthly("version-change-rebuild-b", rebuild=True)
        self.assertEqual(prior_links, {link.adjustment_id: link.settlement_id for link in self.rows(self.m.VersionChangeSettlementLink)})
        self.assertTrue(all(line.status == "superseded" for line in self.rows(self.m.SettlementLine, batch_id=first["id"])))
        for settlement_id in {line.settlement_id for line in self.rows(self.m.SettlementLine, batch_id=second["id"])}:
            self.request("POST", f"/api/settlements/{settlement_id}/lock", actor="finance")
        self.request("POST", f"/api/settlement-batches/{second['id']}/confirm", actor="finance", json={})
        before = len(self.rows(self.m.Settlement))
        self.monthly("version-change-rebuild-c", rebuild=True, expected=409)
        self.assertEqual(len(self.rows(self.m.Settlement)), before)

    def test_rebuilt_report_counts_active_lines_once_and_uses_batch_financial_month(self):
        self.posted_adjustments("report-rebuild")
        self.monthly("version-change-report-first")
        rebuilt = self.monthly("version-change-report-rebuild", rebuild=True)

        report = self.request("GET", "/api/settlement-reports", actor="finance", params={
            "start": "2026-09-01T00:00:00+00:00", "end": "2026-09-30T00:00:00+00:00"})
        self.assertEqual(report["summary"]["count"], 2)
        self.assertEqual(report["summary"]["profit_amount"], 3.01)
        self.assertAlmostEqual(sum(item["amount"] for item in report["summary"]["participants"]), 3.01)
        self.assertEqual([item["label"] for item in report["monthly"]], ["2026-09"])
        self.assertEqual(sum(row["participant_total"] for row in report["items"]), 3.01)
        self.assertTrue(all(row["created_at"] for row in report["items"]))

        next_month = self.request("GET", "/api/settlement-reports", actor="finance", params={
            "start": "2026-10-01T00:00:00+00:00", "end": "2026-10-31T00:00:00+00:00"})
        self.assertEqual(next_month["summary"]["count"], 0, next_month)
        self.assertEqual(next_month["items"], [])
        self.assertEqual(rebuilt["version_change_count"], 2)

    def test_overlapping_active_batch_is_rejected_and_transaction_rolls_back(self):
        self.posted_adjustments()
        first = self.monthly("version-change-overlap-first")
        batches_before = len(self.rows(self.m.SettlementBatch))
        self.monthly("version-change-overlap-second", expected=409,
            period_start="2026-09-10T00:00:00+00:00", period_end="2026-10-10T00:00:00+00:00")
        self.assertEqual(len(self.rows(self.m.SettlementBatch)), batches_before)
        self.assertEqual(self.rows(self.m.SettlementBatch, id=first["id"])[0].status, "generated")

    def test_funding_orders_and_saga_refunds_are_excluded_when_fund_model_exists(self):
        self.posted_adjustments()
        with self.m.SessionLocal() as db:
            hook = self.m.version_change_settlement
            ordinary = db.get(self.m.Order, db.query(self.m.VersionChangeAdjustment).first().source_order_id)
            ordinary.business_type = "version_change_payment"
            db.commit()
            self.assertTrue(hook["is_funding_order"](ordinary))
            self.assertFalse(hook["is_change_refund"](db, None))
            prior_fund_model = self.m.__dict__.get("VersionChangeFund")
            self.m.VersionChangeFund = SimpleNamespace(refund_id=column("refund_id"), payment_id=column("payment_id"))
            fake_db = Mock()
            fake_db.scalar.return_value = "fund-refund"
            self.assertTrue(hook["is_change_refund"](fake_db, SimpleNamespace(id="refund", payment_id="payment")))
            if prior_fund_model is None:
                self.m.__dict__.pop("VersionChangeFund", None)
            else:
                self.m.VersionChangeFund = prior_fund_model
        batch = self.monthly("version-change-funding-filter")
        self.assertEqual(batch["version_change_count"], 2)

    def test_only_exact_fund_refund_is_excluded_not_later_refund_on_same_payment(self):
        table, previous = self.fake_fund_table()
        try:
            oid = self.create()
            with self.m.SessionLocal() as db:
                payment = db.scalar(self.select(self.m.Payment).where(self.m.Payment.order_id == oid))
                db.add_all([
                    self.m.Refund(id="ordinary-later-refund", refund_no="REF-ORDINARY-LATER", order_id=oid,
                        payment_id=payment.id, amount=Decimal("10.00"), status="completed",
                        completed_at=datetime(2026, 9, 15, tzinfo=timezone.utc)),
                    self.m.Refund(id="exact-fund-refund", refund_no="REF-EXACT-FUND", order_id=oid,
                        payment_id=payment.id, amount=Decimal("12.00"), status="completed",
                        completed_at=datetime(2026, 9, 16, tzinfo=timezone.utc)),
                ])
                db.flush()
                db.execute(insert(table).values(id="fund-link", change_id="change", kind="refund",
                    refund_id="exact-fund-refund", payment_id=payment.id))
                db.commit()
            batch = self.monthly("version-change-exact-refund-filter")
            settlements = [row for row in self.rows(self.m.Settlement) if row.is_refund]
            self.assertEqual([row.refund_id for row in settlements], ["ordinary-later-refund"])
            self.assertEqual(len(self.rows(self.m.SettlementLine, batch_id=batch["id"])), 5)
        finally:
            if previous is None:
                self.m.__dict__.pop("VersionChangeFund", None)
            else:
                self.m.VersionChangeFund = previous
            table.drop(self.m.engine)

    def test_adjustment_audit_records_creation_and_reuse_provenance(self):
        self.posted_adjustments("audit-change")
        first = self.monthly("version-change-audit-first")
        second = self.monthly("version-change-audit-rebuild", rebuild=True)
        with self.m.SessionLocal() as db:
            audits = db.scalars(self.select(self.m.AuditLog).where(
                self.m.AuditLog.action == "settle_version_change_adjustment").order_by(
                    self.m.AuditLog.created_at, self.m.AuditLog.id)).all()
            self.assertEqual(len(audits), 4)
            events = [(row, json.loads(row.after_json)) for row in audits]
            created = [(row, after) for row, after in events if after["created_or_reused"] == "created"]
            reused = [(row, after) for row, after in events if after["created_or_reused"] == "reused"]
            self.assertTrue(all(row.actor == "finance@example.invalid" for row in audits))
            self.assertEqual(len(created), 2)
            self.assertEqual(len(reused), 2)
            self.assertTrue(all(row.batch_no == first["batch_no"] for row, _ in created))
            self.assertTrue(all(row.batch_no == second["batch_no"] for row, _ in reused))
            self.assertEqual({item["side"] for _, item in created}, {"old", "target"})
            self.assertTrue(all(item["change_id"] == "audit-change" and item["adjustment_id"] and
                item["rule_version"] == "frozen-v1" and item["net_amount"] in {"-10.01", "15.02"}
                for _, item in events))

    def test_monthly_order_subset_limits_adjustments_by_source_order(self):
        selected_source = self.posted_adjustments("subset-selected")
        other_source = self.posted_adjustments("subset-other")
        batch = self.monthly("version-change-order-subset", order_ids=[selected_source])
        self.assertEqual(batch["version_change_count"], 2)
        selected_adjustment_ids = {row.id for row in self.rows(self.m.VersionChangeAdjustment,
            source_order_id=selected_source)}
        linked_ids = {row.adjustment_id for row in self.rows(self.m.VersionChangeSettlementLink)}
        self.assertEqual(linked_ids, selected_adjustment_ids)
        self.assertNotEqual(selected_source, other_source)

    def test_adjustment_linked_settlements_are_not_rewritten_as_source_order_settlements(self):
        source = self.posted_adjustments()
        first = self.monthly("version-change-source-first-month")
        link = self.rows(self.m.VersionChangeSettlementLink)[0]
        settlement_id = link.settlement_id
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, source)
            payment = db.scalar(self.select(self.m.Payment).where(self.m.Payment.order_id == source))
            order.payment_status, order.paid_amount = "paid", order.amount
            payment.status = "paid"
            payment.paid_at = datetime(2026, 10, 15, tzinfo=timezone.utc)
            db.commit()
        later = self.monthly("version-change-source-later-month", period_start="2026-10-01T00:00:00+00:00",
                             period_end="2026-11-01T00:00:00+00:00")
        self.assertEqual(later["version_change_count"], 0)
        self.assertEqual(self.rows(self.m.Settlement, id=settlement_id)[0].status, "pending")
        self.assertEqual(self.rows(self.m.SettlementLine, settlement_id=settlement_id)[0].batch_id, first["id"])

    def test_describe_returns_side_specific_public_provenance(self):
        self.posted_adjustments("describe-change")
        batch = self.monthly("version-change-describe")
        with self.m.SessionLocal() as db:
            old_adjustment = db.scalar(self.select(self.m.VersionChangeAdjustment).where(
                self.m.VersionChangeAdjustment.change_id == "describe-change",
                self.m.VersionChangeAdjustment.side == "old"))
            link = db.scalar(self.select(self.m.VersionChangeSettlementLink).where(
                self.m.VersionChangeSettlementLink.adjustment_id == old_adjustment.id))
            detail = self.m.version_change_settlement["describe"](db, link.settlement_id)
            self.assertEqual(detail, {
                "kind": "version_change", "change_id": "describe-change",
                "change_no": "VC-DESCRIBE-CHA", "adjustment_id": old_adjustment.id,
                "side": "old", "version_id": "v1", "rule_version": "frozen-v1",
                "effective_at": old_adjustment.effective_at, "posted_at": old_adjustment.posted_at,
            })
            ordinary = db.scalar(self.select(self.m.Settlement).where(
                self.m.Settlement.id.not_in(self.select(self.m.VersionChangeSettlementLink.settlement_id))))
            if ordinary is not None:
                self.assertIsNone(self.m.version_change_settlement["describe"](db, ordinary.id))

    def test_detail_exposes_frozen_old_side_provenance_after_live_rule_changes(self):
        self.posted_adjustments("detail-provenance")
        with self.m.SessionLocal() as db:
            rule = db.scalar(self.select(self.m.SettlementRule).where(
                self.m.SettlementRule.status == "active"))
            rule.platform_rate, rule.provider_rate = 99, 1
            old = db.scalar(self.select(self.m.VersionChangeAdjustment).where(
                self.m.VersionChangeAdjustment.change_id == "detail-provenance",
                self.m.VersionChangeAdjustment.side == "old"))
            adjustment_id = old.id
            effective_at, posted_at = old.effective_at, old.posted_at
            db.commit()
        self.monthly("version-change-detail-provenance")
        with self.m.SessionLocal() as db:
            link = db.scalar(self.select(self.m.VersionChangeSettlementLink).where(
                self.m.VersionChangeSettlementLink.adjustment_id == adjustment_id))
            settlement_id = link.settlement_id
        detail = self.request("GET", f"/api/settlements/{settlement_id}/detail", actor="finance")
        self.assertEqual(detail["version_change"], {
            "kind": "version_change", "change_id": "detail-provenance",
            "change_no": "VC-DETAIL-PROVE", "adjustment_id": adjustment_id,
            "side": "old", "version_id": "v1", "rule_version": "frozen-v1",
            "effective_at": effective_at.isoformat(),
            "posted_at": posted_at.isoformat(),
        })

    def test_legacy_funding_settlement_generation_rejected_and_simulation_skips(self):
        order_id = self.create()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, order_id)
            order.business_type = "version_change_payment"
            db.commit()
        self.request("POST", f"/api/settlements/generate/{order_id}", actor="finance", expected=409)
        result = self.request("POST", "/api/settlement-rules/trading_rule/simulate",
            actor="finance", json={"cycle": "manual", "order_ids": [order_id]})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)

    def test_monthly_batch_with_no_posted_adjustments_reports_zero(self):
        batch = self.monthly("version-change-empty")
        self.assertEqual(batch["version_change_count"], 0)
        self.assertEqual(len(self.rows(self.m.VersionChangeSettlementLink)), 0)


if __name__ == "__main__":
    unittest.main()
