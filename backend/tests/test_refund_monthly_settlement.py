"""BE013 HTTP acceptance on disposable SQLite and real isolated Redis.

Reuses BE012 execution (simulated funds, fixture charge-summary evidence).
Downgrade history is seeded persisted evidence, not a saga execution proof.
No deployment connections or PostgreSQL concurrency claims.
"""
import csv
from datetime import timedelta
from decimal import Decimal
import io
import json
import unittest

from sqlalchemy import select

import test_refund_execution as fixture


class RefundMonthlySettlementTests(unittest.TestCase):
    setUpClass = classmethod(fixture.RefundExecutionTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.RefundExecutionTests.tearDownClass.__func__)
    setUp = fixture.RefundExecutionTests.setUp
    tearDown = fixture.RefundExecutionTests.tearDown
    request = fixture.RefundExecutionTests.request
    create_order = fixture.RefundExecutionTests.create_order
    monthly = fixture.RefundExecutionTests.monthly
    create = fixture.RefundExecutionTests.create
    review = fixture.RefundExecutionTests.review
    ledger = fixture.RefundExecutionTests.ledger
    approve = fixture.RefundExecutionTests.approve
    execute = fixture.RefundExecutionTests.execute
    step = fixture.RefundExecutionTests.step

    def rows(self, model, **filters):
        with self.m.SessionLocal() as db:
            return list(db.scalars(select(model).filter_by(**filters)))

    def period(self, offset=0):
        start = self.at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if offset == -1:
            start = (start - timedelta(days=1)).replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
        return dict(cycle="monthly", period_start=start.isoformat(), period_end=end.isoformat())

    def batch(self, key, expected=200, offset=0, **changes):
        return self.request("POST", "/api/settlement-batches", actor="finance", expected=expected,
                            json=self.period(offset) | dict(idempotency_key=key) | changes)

    def complete(self):
        approved = self.approve()
        self.execute(approved)
        for _ in range(8):
            if not self.step():
                break
        else:
            self.fail("Refund worker did not quiesce in eight steps")
        row = self.request("GET", "/api/refunds/" + approved["id"], actor="ops")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(len(self.rows(self.Completion, refund_id=row["id"])), 1)
        return row

    def negative(self, refund):
        rows = self.rows(self.m.Settlement, refund_id=refund["id"])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].is_refund)
        return rows[0]

    def economics(self, row):
        return tuple(getattr(row, key) for key in (
            "gross_amount", "net_amount", "cost_amount", "profit_amount", "platform_fee",
            "provider_share", "service_share", "expert_fee", "channel_fee"))

    def assert_shares(self, batch, settlement):
        lines = self.rows(self.m.SettlementLine, batch_id=batch["id"], settlement_id=settlement.id)
        self.assertEqual({line.participant_type for line in lines},
                         {"platform", "provider", "service", "expert", "channel"})
        self.assertEqual(len(lines), 5)
        self.assertEqual(sum((line.amount for line in lines), Decimal(0)), settlement.profit_amount)
        self.assertTrue(all(line.amount <= 0 for line in lines))

    def test_completion_month_reports_csv_and_five_signed_shares(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.Payment, "paid-source").paid_at = self.at.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
            db.commit()
        refund = self.complete()
        old = self.batch("ordinary-original-month", offset=-1)
        self.assertFalse(any(s.is_refund for s in self.rows(self.m.Settlement)))
        original = self.rows(self.m.Settlement, order_id=self.order_id)[0]
        before = self.economics(original)
        batch = self.batch("ordinary-completion-month")
        negative = self.negative(refund)
        self.assertEqual(negative.net_amount, Decimal("-20.00"))
        self.assertEqual(negative.cost_amount, Decimal("-2.00"))
        self.assertEqual(negative.profit_amount, Decimal("-18.00"))
        detail = self.request("GET", f"/api/settlements/{negative.id}/detail", actor="finance")
        self.assertEqual(detail["refund_adjustment"]["refund_id"], refund["id"])
        self.assertEqual(detail["refund_adjustment"]["source_order_ids"], [self.order_id])
        self.assert_shares(batch, negative)
        self.assertEqual(self.economics(self.rows(self.m.Settlement, id=original.id)[0]), before)
        self.assertEqual(len(self.rows(self.m.SettlementLine, batch_id=old["id"])), 5)
        params = {"start": self.period()["period_start"],
                  "end": self.period()["period_end"]}
        report = self.request("GET", "/api/settlement-reports", actor="finance", params=params)
        self.assertEqual([r["id"] for r in report["items"]], [negative.id])
        report_refund = report["items"][0]
        self.assertEqual(report_refund["source_kind"], "ordinary_refund")
        self.assertEqual(report_refund["refund_id"], refund["id"])
        self.assertEqual(report_refund["refund_no"], refund["refund_no"])
        self.assertTrue(report_refund["is_refund"])
        self.assertEqual(Decimal(str(report["summary"]["net_amount"])), Decimal("-20"))
        self.assertEqual(Decimal(str(report["summary"]["profit_amount"])), Decimal("-18"))
        self.assertEqual(report["monthly"][0]["label"], self.at.strftime("%Y-%m"))
        self.assertEqual(sum((Decimal(str(p["amount"])) for p in
                              report["summary"]["participants"]), Decimal(0)), Decimal("-18"))
        response = self.client.get("/api/settlement-reports/export", params=params,
                                   headers={"Authorization": "Bearer " + self.tokens["finance"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/csv", response.headers["content-type"])
        exported = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["settlement_no"], negative.settlement_no)
        self.assertEqual(exported[0]["source_kind"], "ordinary_refund")
        self.assertEqual(exported[0]["refund_id"], refund["id"])
        self.assertEqual(exported[0]["refund_no"], refund["refund_no"])
        self.assertTrue(exported[0]["refund_no"].startswith("REF"))
        for field, amount in (("net_amount", "-20"), ("cost_amount", "-2"), ("profit_amount", "-18")):
            self.assertEqual(Decimal(exported[0][field]), Decimal(amount))
        share_headers = ("\u5e73\u53f0\u8fd0\u8425\u65b9", "\u6570\u636e/\u670d\u52a1\u63d0\u4f9b\u65b9",
                         "\u6570\u636e\u670d\u52a1\u65b9", "\u4e13\u5bb6", "\u6e20\u9053")
        self.assertEqual(sum((Decimal(exported[0][field]) for field in share_headers),
                             Decimal(0)), Decimal("-18"))

    def test_frozen_cost_and_rule_survive_catalog_and_global_edits(self):
        refund = self.complete()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            frozen = json.loads(order.economic_snapshot_json)
            product = db.get(self.m.Product, "product")
            product.settlement_rule_mode = "custom"
            product.settlement_rule_json = json.dumps(dict(platform_rate=100, provider_rate=0,
                service_rate=0, expert_rate=0, channel_rate=0))
            db.get(self.m.ProductReleaseVersion, "version").cost = 99
            rule = db.get(self.m.SettlementRule, "trading_rule")
            rule.platform_rate, rule.provider_rate, rule.service_rate = 100, 0, 0
            rule.expert_rate, rule.channel_rate = 0, 0
            db.commit()
        batch = self.batch("frozen-refund")
        negative = self.negative(refund)
        self.assertEqual(negative.cost_amount, Decimal("-2.00"))
        self.assertEqual(negative.profit_amount, Decimal("-18.00"))
        rates = frozen["settlement_rule"]["rates"]
        for field, rate in (("platform_fee", "platform_rate"), ("service_share", "service_rate"),
                            ("expert_fee", "expert_rate"), ("channel_fee", "channel_rate")):
            self.assertEqual(getattr(negative, field),
                             (Decimal("-18") * Decimal(str(rates[rate])) / 100).quantize(Decimal(".01")))
        self.assert_shares(batch, negative)

    def test_unlocked_rebuild_reuses_negative_and_reports_once(self):
        refund = self.complete()
        first = self.batch("refund-first")
        negative = self.negative(refund)
        before = self.economics(negative)
        second = self.batch("refund-rebuilt", rebuild=True)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.negative(refund).id, negative.id)
        self.assertEqual(self.economics(self.negative(refund)), before)
        self.assertTrue(all(line.status == "superseded" for line in
                            self.rows(self.m.SettlementLine, batch_id=first["id"])))
        self.assert_shares(second, self.negative(refund))
        report = self.request("GET", "/api/settlement-reports", actor="finance")
        matching = [r for r in report["items"] if r["id"] == negative.id]
        self.assertEqual(len(matching), 1)
        self.assertEqual(Decimal(str(matching[0]["participant_total"])), Decimal("-18"))
        self.assertEqual(len(report["items"]), 2)
        self.assertEqual({row["source_kind"] for row in report["items"]}, {"order", "ordinary_refund"})
        self.assertEqual({row["order_id"] for row in report["items"]}, {self.order_id})
        self.assertEqual(len(report["monthly"]), 1)
        self.assertEqual(len(report["products"]), 1)
        for breakdown in (report["monthly"][0], report["products"][0]):
            self.assertEqual(breakdown["order_count"], 1)
            self.assertEqual(Decimal(str(breakdown["cost_amount"])), Decimal("8"))
            self.assertEqual(Decimal(str(breakdown["profit_amount"])), Decimal("72"))
        self.assertEqual(Decimal(str(report["summary"]["net_amount"])), Decimal("80"))
        self.assertEqual(Decimal(str(report["summary"]["profit_amount"])), Decimal("72"))

    def test_partially_locked_unconfirmed_batch_allows_rebuild(self):
        refund = self.complete()
        first = self.batch("refund-partial-lock-first")
        negative = self.negative(refund)
        before = self.economics(negative)
        old_lines = self.rows(self.m.SettlementLine, batch_id=first["id"])
        old_ids = {line.settlement_id for line in old_lines}
        self.assertEqual(len(old_ids), 2)
        self.assertEqual(len(old_lines), 10)
        positive_id = next(sid for sid in old_ids if sid != negative.id)
        self.request("POST", f"/api/settlements/{positive_id}/lock", actor="finance")
        self.assertIn(self.rows(self.m.SettlementBatch, id=first["id"])[0].status,
                      {"generated", "pending_confirm"})
        children = [self.rows(self.m.Settlement, id=sid)[0] for sid in old_ids]
        self.assertEqual(sum(child.status == "locked" for child in children), 1)
        self.assertFalse(any(child.status == "paid" for child in children))
        self.assertFalse(any(line.status == "paid" or line.payment_no for line in
                             self.rows(self.m.SettlementLine, batch_id=first["id"])))

        rebuilt = self.batch("refund-partial-lock-rebuilt", rebuild=True)
        self.assertNotEqual(rebuilt["id"], first["id"])
        self.assertEqual(self.rows(self.m.SettlementBatch, id=first["id"])[0].status, "superseded")
        self.assertTrue(all(line.status == "superseded" for line in
                            self.rows(self.m.SettlementLine, batch_id=first["id"])))
        current = self.negative(refund)
        self.assertEqual(current.id, negative.id)
        self.assertEqual(self.economics(current), before)
        self.assert_shares(rebuilt, current)
        new_lines = self.rows(self.m.SettlementLine, batch_id=rebuilt["id"])
        new_ids = {line.settlement_id for line in new_lines}
        self.assertEqual(len(new_lines), 10)
        self.assertEqual(len(new_ids), 2)
        self.assertTrue(all(line.status != "superseded" and not line.payment_no for line in new_lines))
        settlements = [self.rows(self.m.Settlement, id=sid)[0] for sid in new_ids]
        self.assertEqual(sorted(row.net_amount for row in settlements),
                         [Decimal("-20"), Decimal("100")])
        self.assertEqual(Decimal(str(rebuilt["total_amount"])), Decimal("80"))
        self.assertEqual(Decimal(str(rebuilt["total_profit"])), Decimal("72"))
        for settlement in settlements:
            own = [line for line in new_lines if line.settlement_id == settlement.id]
            self.assertEqual(len(own), 5)
            self.assertEqual(sum((line.amount for line in own), Decimal(0)), settlement.profit_amount)

    def test_confirmed_and_paid_batches_block_rebuild_without_rewrite(self):
        refund = self.complete()
        batch = self.batch("refund-finalize")
        children = [self.rows(self.m.Settlement, id=sid)[0] for sid in
                    {line.settlement_id for line in self.rows(self.m.SettlementLine, batch_id=batch['id'])}]
        for child in sorted(children, key=lambda row: row.is_refund):
            if not child.is_refund:
                # Freeze a bilateral final agreement with explicit provider
                # and internal-platform recipients; never backfill old orders.
                proposal = self.request('POST', f'/api/settlements/{child.id}/proposals', actor='finance', json=dict(
                    gross_amount=100, cost_amount=10, profit_amount=90, platform_rate=20,
                    provider_rate=80, service_rate=0, expert_rate=0, channel_rate=0,
                    reason='Documented final allocation for fixture'))
                self.request('POST', f'/api/settlement-proposals/{proposal["id"]}/decision', actor='provider',
                             json=dict(decision='approve', comment='Agreed'))
            self.request('POST', f'/api/settlements/{child.id}/lock', actor='finance')
        self.request("POST", f"/api/settlement-batches/{batch['id']}/confirm", actor="finance", json={})
        for state in ("confirmed", "paid"):
            if state == "paid":
                with self.m.SessionLocal() as db:
                    h = self.m.settlement_finance
                    positions = db.scalars(select(h['Position']).where(h['Position'].settlement_id.in_(
                        [child.id for child in children]), h['Position'].direction == 'payable')).all()
                    allocations = [dict(position_id=p.id, amount=str(h['balance'](db, p)))
                                   for p in positions if h['balance'](db, p) > 0]
                tx = self.request('POST', '/api/settlement-finance/transactions', actor='finance', json=dict(
                    request_id='monthly-payout', kind='payout', mode='simulated', reference='Isolated simulated voucher',
                    allocations=allocations))
                self.request('POST', f'/api/settlement-finance/transactions/{tx["id"]}/confirm', actor='ops',
                             json=dict(request_id='monthly-payout-review', expected_version=1, note='Independent check'))
                self.request("POST", f"/api/settlement-batches/{batch['id']}/pay", actor="finance", json={})
            before = self.economics(self.negative(refund))
            lines = [(r.id, r.amount, r.status, r.payment_no) for r in
                     self.rows(self.m.SettlementLine, batch_id=batch["id"])]
            count = len(self.rows(self.m.SettlementBatch))
            self.batch("refund-blocked-" + state, expected=409, rebuild=True)
            self.assertEqual(self.economics(self.negative(refund)), before)
            self.assertEqual([(r.id, r.amount, r.status, r.payment_no) for r in
                self.rows(self.m.SettlementLine, batch_id=batch["id"])], lines)
            self.assertEqual(len(self.rows(self.m.SettlementBatch)), count)
            self.assertEqual(self.rows(self.m.SettlementBatch, id=batch["id"])[0].status, state)

    def test_explicit_order_filter_excludes_completed_refund_then_includes_it(self):
        refund = self.complete()
        batch = self.batch("refund-outside-scope", order_ids=["not-an-order"])
        self.assertEqual(self.rows(self.m.SettlementLine, batch_id=batch["id"]), [])
        self.assertEqual(self.rows(self.m.Settlement, refund_id=refund["id"]), [])
        included = self.batch("refund-inside-scope", order_ids=[self.order_id], rebuild=True)
        self.assert_shares(included, self.negative(refund))

    def test_existing_downgrade_negative_is_not_duplicated_as_refund(self):
        # Persist the same real model links used by completed downgrade history.
        # Ordinary execution still runs separately on the shared source payment.
        refund = self.complete()
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            rule = json.loads(order.economic_snapshot_json)["settlement_rule"]
            db.add(self.m.VersionChangeRequest(id="settled-downgrade", source_order_id=order.id,
                enterprise_id="buyer_tenant", combination_id="refund-combo",
                target_version_id="version", target_version_code="v1", old_version_id="version",
                old_version_code="v1", kind="downgrade", status="completed",
                idempotency_key="seed-downgrade", request_hash="a" * 64, quote_json="{}",
                prepared_json="{}", target_snapshots_json="{}", created_by="ops"))
            db.add(self.m.Refund(id="downgrade-refund", refund_no="REF-DOWNGRADE-ISOLATED",
                order_id=order.id, payment_id="paid-source", amount=10, status="completed",
                completed_at=self.at))
            db.flush()
            db.add(self.m.VersionChangeFund(change_id="settled-downgrade", kind="refund",
                payment_id="paid-source", refund_id="downgrade-refund", amount=10,
                idempotency_key="seed-downgrade-fund"))
            db.add(self.m.VersionChangeAdjustment(change_id="settled-downgrade",
                request_change_id="settled-downgrade", source_order_id=order.id,
                combination_id="refund-combo", side="old", revenue=Decimal("-10"),
                cost=Decimal("-1"), profit=Decimal("-9"), currency="CNY",
                original_version_id="version", target_version_id="version", rule_json=json.dumps(rule),
                effective_at=self.at, status="posted", payload_fingerprint="b" * 64, posted_at=self.at))
            db.commit()
        batch = self.batch("ordinary-with-downgrade")
        self.assertEqual(batch["version_change_count"], 1)
        self.assertEqual(self.rows(self.m.Settlement, refund_id="downgrade-refund"), [])
        self.assert_shares(batch, self.negative(refund))
        links = self.rows(self.m.VersionChangeSettlementLink)
        self.assertEqual(len(links), 1)
        negative = self.rows(self.m.Settlement, id=links[0].settlement_id)[0]
        self.assertEqual(negative.net_amount, Decimal("-10"))
        self.assert_shares(batch, negative)
        self.batch("ordinary-with-downgrade-rebuilt", rebuild=True)
        self.assertEqual(len(self.rows(self.m.VersionChangeSettlementLink)), 1)
        self.assertEqual(len(self.rows(self.m.Settlement, refund_id=refund["id"])), 1)


if __name__ == "__main__":
    unittest.main()
