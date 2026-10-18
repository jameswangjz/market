"""Reporting contract against isolated host models; no startup or business data."""
import csv
import importlib
import io
import json
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import test_message_business_events as fixtures


class SettlementReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = fixtures.load_isolated_main()
        cls.reporting = importlib.import_module(cls.m.__package__ + ".settlement_reporting")
        cls.hooks = cls.reporting.install(vars(cls.m))

    tearDownClass = classmethod(fixtures.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixtures.BusinessRoutesHTTPTests.setUp
    tearDown = fixtures.BusinessRoutesHTTPTests.tearDown
    request = fixtures.BusinessRoutesHTTPTests.request
    prefix = "/api/settlement-reporting"

    def seed(self, count=1, provider="provider_tenant", amount="150.01", **values):
        m = self.m
        with m.SessionLocal() as db:
            order = m.Order(order_no="ORD-" + str(db.query(m.Order).count()), buyer_user_id="buyer",
                buyer_enterprise_id="buyer_tenant", provider_enterprise_id=provider, product_id="product",
                product_version_id="version", product_name="Dataset", buyer_name="Buyer", payment_status="paid",
                amount=max(Decimal(amount), Decimal(0)), paid_amount=max(Decimal(amount), Decimal(0)),
                business_type=values.pop("business_type", ""))
            db.add(order)
            db.flush()
            settlements = []
            for i in range(count):
                row = m.Settlement(settlement_no="SET-" + order.id + str(i), order_id=order.id,
                    gross_amount=Decimal(amount), net_amount=Decimal(amount), cost_amount=Decimal("100.00"),
                    profit_amount=Decimal(amount) - 100, platform_fee=Decimal("10.00"),
                    provider_share=Decimal(amount) - 110, created_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
                    **values)
                db.add(row)
                settlements.append(row)
            db.commit()
            return order.id, [r.id for r in settlements]

    def report(self, actor="finance", **params):
        return self.request("GET", self.prefix + "/report", actor=actor, params={"year": 2026, **params})

    def download(self, path="/export", actor="finance", **params):
        return self.client.get(self.prefix + path, headers={"Authorization": "Bearer " + self.tokens[actor]},
                               params={"year": 2026, **params})

    def test_more_than_2000_exact_totals_and_stable_pages_complete_exports(self):
        self.seed(2007)
        data = self.report(page_size=2)
        self.assertEqual(data["total"], 2007)
        self.assertEqual(data["summary"]["gross_amount"], "301070.07")
        self.assertEqual(data["summary"]["provider_total_receivable"], "281000.07")
        self.assertEqual(data["summary"]["income_order_count"], 1)
        self.assertEqual(data["summary"]["settlement_count"], 2007)
        self.assertEqual(len(data["monthly"]), 12)
        self.assertEqual(data["monthly"][8]["gross_amount"], "301070.07")
        self.assertEqual(self.report(page_size=2)["items"], data["items"])
        self.assertTrue(set(r["id"] for r in data["items"]).isdisjoint(r["id"] for r in self.report(page=2, page_size=2)["items"]))
        response = self.download(page_size=1, page=2)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(len(list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))), 2008)

    def test_provider_isolation_all_aggregates_exports_and_cross_scope(self):
        self.seed()
        self.seed(provider="other_tenant", amount="999.99")
        data = self.report(actor="provider", business_view="provider", enterprise_id="provider_tenant")
        self.assertEqual(data["summary"]["gross_amount"], "150.01")
        self.assertEqual(data["providers"][0]["provider_name"], "provider_tenant")
        self.assertTrue(all(r["provider_id"] == "provider_tenant" for r in data["rankings"]))
        for kind in ("details", "summary", "products", "providers", "participants", "refunds", "rankings"):
            response = self.download(actor="provider", business_view="provider", kind=kind)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("other_tenant", response.text)
            self.assertNotIn("999.99", response.text)
        self.request("GET", self.prefix + "/report", actor="provider", expected=403,
                     params={"year": 2026, "business_view": "provider", "provider_id": "other_tenant"})
        self.request("GET", self.prefix + "/export", actor="provider", expected=403,
                     params={"business_view": "provider", "enterprise_id": "other_tenant"})

    def test_forbidden_roles_buyer_member_unverified_inactive(self):
        for actor in ("buyer", "buyer_admin", "reviewer", "quality", "provider"):
            self.request("GET", self.prefix + "/report", actor=actor, expected=403)
        self.request("GET", self.prefix + "/report", actor="provider", expected=403, params={"business_view": "buyer"})
        for field, value in (("role", "member"), ("status", "disabled"), ("business_roles", "user")):
            with self.m.SessionLocal() as db:
                member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "provider"))
                old = getattr(member, field)
                setattr(member, field, value)
                db.commit()
            self.request("GET", self.prefix + "/export", actor="provider", expected=403, params={"business_view": "provider"})
            with self.m.SessionLocal() as db:
                member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "provider"))
                setattr(member, field, old)
                db.commit()
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "provider").verified_status = "pending_review"
            db.commit()
        self.request("GET", self.prefix + "/report", actor="provider", expected=403, params={"business_view": "provider"})

    def test_shanghai_end_day_half_open_and_monthly_period_precedence(self):
        _, ids = self.seed(3)
        m = self.m
        with m.SessionLocal() as db:
            for sid, clock in zip(ids, (datetime(2026, 9, 30, 15, 59, 59), datetime(2026, 9, 30, 16), datetime(2026, 10, 15))):
                db.get(m.Settlement, sid).created_at = clock
            batch = m.SettlementBatch(batch_no="BATCH-SEP", cycle="monthly", period_start=datetime(2026, 8, 31, 16), idempotency_key="sep")
            db.add(batch)
            db.flush()
            db.add(m.SettlementLine(batch_id=batch.id, settlement_id=ids[2], participant_type="provider", amount=Decimal("40.01")))
            db.commit()
        data = self.report(start="2026-09-01", end="2026-09-30")
        self.assertEqual({r["id"] for r in data["items"]}, {ids[0], ids[2]})
        self.assertTrue(all(r["month"] == "2026-09" for r in data["items"]))
        self.assertEqual(self.report(start="2026-10-01", end="2026-10-01")["total"], 1)

    def test_refund_counts_superseded_and_funding_exclusion(self):
        self.seed(2)
        self.seed(2, amount="-75.00", is_refund=True, refund_id="refund-one")
        self.seed(status="superseded")
        self.seed(business_type="version_change_payment")
        data = self.report()
        self.assertEqual(data["summary"]["income_order_count"], 1)
        self.assertEqual(data["summary"]["refund_count"], 1)
        self.assertEqual(data["summary"]["settlement_count"], 4)
        self.assertEqual(data["summary"]["order_count"], 2)
        self.assertEqual(data["summary"]["positive"]["gross_amount"], "300.02")
        self.assertEqual(data["summary"]["reversal"]["gross_amount"], "-150.00")
        self.assertEqual(self.report(source_kind="refund")["total"], 2)

    def test_version_change_two_legs_count_once_via_real_link(self):
        oid, ids = self.seed(2)
        m = self.m
        with m.SessionLocal() as db:
            combo = m.subscription_terms["Combination"](enterprise_id="buyer_tenant", product_id="product",
                delivery_method="api", version_id="version", version_code="v1", anchor_at=datetime(2026, 9, 1, tzinfo=timezone.utc), total_months=1)
            db.add(combo)
            db.flush()
            for sid, side in zip(ids, ("old", "target")):
                adjustment = m.VersionChangeAdjustment(change_id="one-change", source_order_id=oid,
                    combination_id=combo.id, side=side, revenue=1, cost=0, profit=1, currency="CNY",
                    original_version_id="version", target_version_id="v2", rule_json="{}",
                    effective_at=datetime(2026, 9, 1, tzinfo=timezone.utc), status="posted", payload_fingerprint=side)
                db.add(adjustment)
                db.flush()
                db.add(m.version_change_settlement["Model"](settlement_id=sid, adjustment_id=adjustment.id))
            db.commit()
        data = self.report()
        self.assertEqual(data["summary"]["adjustment_count"], 1)
        self.assertEqual(data["summary"]["income_order_count"], 0)
        self.assertEqual(data["summary"]["settlement_count"], 2)

    def test_participant_same_name_distinct_ids_and_unassigned(self):
        _, ids = self.seed(expert_fee=Decimal("3.00"), service_share=Decimal("2.00"))
        m = self.m
        with m.SessionLocal() as db:
            batch = m.SettlementBatch(batch_no="BATCH-ID", idempotency_key="ids")
            db.add(batch)
            db.flush()
            for pid, role, amount in (("expert1", "expert", "1.00"), ("expert2", "expert", "2.00"), ("expert1", "service", "2.00")):
                db.add(m.SettlementLine(batch_id=batch.id, settlement_id=ids[0], participant_type=role,
                    participant_id=pid, participant_name="Same name", amount=Decimal(amount)))
            db.commit()
        data = self.report(participant_type="expert")
        assigned = [p for p in data["participants"] if p["participant_id"] in {"expert1", "expert2"}]
        self.assertEqual(len(assigned), 3)
        self.assertEqual(sum(Decimal(p["amount"]) for p in assigned), Decimal("5.00"))
        self.assertTrue(data["scope"]["beneficiary_grants_supported"])
        self.seed(expert_fee=Decimal("4.00"))
        missing = [p for p in self.report()["participants"] if p["participant_type"] == "expert" and p["subject_unassigned"]]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["participant_id"], "")

    def test_annual_immutable_idempotent_download_roles_and_finalize_rejection(self):
        _, ids = self.seed()
        body = dict(year=2026, request_id="annual-1", note="review draft")
        first = self.request("POST", self.prefix + "/annual-reports", actor="finance", json=body)
        self.assertEqual(first["status"], "draft")
        before = self.download("/annual-reports/" + first["id"] + "/export")
        self.assertEqual(before.status_code, 200, before.text)
        with self.m.SessionLocal() as db:
            db.get(self.m.Settlement, ids[0]).gross_amount = Decimal("999.99")
            db.commit()
        repeated = self.request("POST", self.prefix + "/annual-reports", actor="finance", json=body)
        self.assertEqual(first, repeated)
        self.assertEqual(before.content, self.download("/annual-reports/" + first["id"] + "/export").content)
        second = self.request("POST", self.prefix + "/annual-reports", actor="finance", json=body | {"request_id": "annual-2"})
        self.assertEqual(second["version"], 2)
        self.assertNotEqual(first["digest"], second["digest"])
        self.request("POST", self.prefix + "/annual-reports", actor="finance", expected=409, json=body | {"note": "changed"})
        self.assertEqual(self.download("/annual-reports/" + first["id"] + "/export", actor="provider", business_view="provider").status_code, 404)
        self.request("POST", self.prefix + "/annual-reports", actor="provider", expected=403, params={"business_view": "provider"}, json=body)
        self.request("POST", self.prefix + "/annual-reports/" + first["id"] + "/finalize", actor="finance", expected=409)
        with self.m.SessionLocal() as db:
            item = db.get(self.hooks["Model"], first["id"])
            envelope = json.loads(item.report_json)
            self.assertEqual(len(envelope["report"]["monthly"]), 12)
            item.note = "overwrite"
            with self.assertRaisesRegex(ValueError, "immutable"):
                db.flush()
            db.rollback()
            db.delete(db.get(self.hooks["Model"], first["id"]))
            with self.assertRaisesRegex(ValueError, "immutable"):
                db.flush()
            db.rollback()

    def test_formula_escape_csv_quoting_and_explicit_size_guard(self):
        self.seed()
        with self.m.SessionLocal() as db:
            order = db.scalar(self.select(self.m.Order))
            order.product_name = '=SUM(1,2),"quoted"\nnext'
            db.commit()
        response = self.download()
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(rows[1][rows[0].index("产品名称")], '\'=SUM(1,2),"quoted"\nnext')
        with patch.object(self.reporting, "EXPORT_LIMIT", 30):
            self.assertEqual(self.download().status_code, 413)
        self.assertEqual(self.download(kind="invalid").status_code, 422)

    def test_provider_annual_download_scope_and_security_read_only(self):
        self.seed()
        self.seed(provider="other_tenant")
        body = dict(year=2026, request_id="provider-annual", note="")
        first = self.request("POST", self.prefix + "/annual-reports", actor="finance",
                             params={"provider_id": "provider_tenant"}, json=body)
        second = self.request("POST", self.prefix + "/annual-reports", actor="finance",
                              params={"provider_id": "other_tenant"}, json=body)
        own = self.download("/annual-reports/" + first["id"] + "/export", actor="provider", business_view="provider")
        self.assertEqual(own.status_code, 200, own.text)
        self.assertNotIn("other_tenant", own.text)
        other = self.download("/annual-reports/" + second["id"] + "/export", actor="provider", business_view="provider")
        self.assertEqual(other.status_code, 404)
        listing = self.request("GET", self.prefix + "/annual-reports", actor="provider", params={"business_view": "provider"})
        self.assertEqual([r["id"] for r in listing["items"]], [first["id"]])
        with self.m.SessionLocal() as db:
            db.get(self.m.User, "quality").platform_role = "security_compliance"
            db.commit()
        self.assertEqual(self.report(actor="quality")["total"], 2)
        self.assertEqual(self.download(actor="quality").status_code, 200)
        self.request("POST", self.prefix + "/annual-reports", actor="quality", expected=403, json=body)
        self.request("POST", self.prefix + "/annual-reports/" + first["id"] + "/finalize", actor="quality", expected=403)

    def test_formula_escape_all_text_prefixes(self):
        for prefix in ("=", "+", "-", "@", "\t", "\r", "\n", "  ="):
            data = dict(summary={}, items=[dict(product_name=prefix + "payload", gross_amount="-1.00")])
            response = self.reporting.csv_response(data, "details")
            rows = list(csv.reader(io.StringIO(response.body.decode("utf-8-sig"))))
            self.assertEqual(rows[1], ["'" + prefix + "payload", "-1.00"])

    def test_validation_and_all_query_filters(self):
        self.seed()
        for params in ({"start": "2026-9-01"}, {"start": "2026-02-30"}, {"start": "2026-10-02", "end": "2026-10-01"},
                       {"page": 0}, {"page_size": 501}, {"year": 10000}, {"participant_type": "buyer"}, {"source_kind": "bogus"}):
            self.request("GET", self.prefix + "/report", actor="finance", expected=422, params=params)
        self.assertEqual(self.report(q="dataset", product_id="product", provider_id="provider_tenant", status="pending")["total"], 1)
        self.assertEqual(self.report(q="absent")["total"], 0)
        self.assertEqual(self.report(batch_id="unknown")["total"], 0)
        self.assertEqual(self.report(page=99)["summary"]["settlement_count"], 1)

    def test_installed_sidecars_share_metadata_and_reinstall_without_duplicates(self):
        m = self.m
        namespace = vars(m)
        tables = dict(m.Base.metadata.tables)
        models = set(m.Base.registry.mappers)
        routes = tuple((r.path, tuple(sorted(r.methods or []))) for r in m.app.routes)
        for name in ("settlement_parties", "settlement_finance", "settlement_reporting"):
            module = importlib.import_module(m.__package__ + "." + name)
            installed = namespace[name]
            for _ in range(2):
                self.assertIs(module.install(namespace), installed)
        self.assertEqual(tables, dict(m.Base.metadata.tables))
        self.assertEqual(models, set(m.Base.registry.mappers))
        self.assertEqual(routes, tuple((r.path, tuple(sorted(r.methods or []))) for r in m.app.routes))
        for model in (m.settlement_parties["Participant"], m.settlement_parties["Configuration"],
                      m.settlement_finance["Baseline"], m.settlement_finance["Position"],
                      self.hooks["Model"], self.hooks["Finalization"]):
            self.assertIs(model.metadata, m.Base.metadata)
            self.assertEqual(sum(mapper.class_ is model for mapper in models), 1)
        reporting_routes = [route for route in routes if route[0].startswith(self.prefix)]
        self.assertEqual(len(reporting_routes), 7)
        self.assertEqual(len(reporting_routes), len(set(reporting_routes)))

    def test_finance_capture_and_reporting_reconcile_without_read_side_effects(self):
        oid, ids = self.seed(status="locked")
        m = self.m
        finance = m.settlement_finance
        with m.SessionLocal() as db:
            order = db.get(m.Order, oid)
            order.economic_snapshot_json = json.dumps(dict(currency="CNY"))
            db.flush()
            baseline = finance["capture"](db, db.get(m.Settlement, ids[0]), actor="finance")
            self.assertEqual(baseline.revenue, Decimal("150.01"))
            db.commit()
            positions = db.scalars(self.select(finance["Position"]).where(finance["Position"].settlement_id == ids[0])).all()
            provider_receivable = sum((p.amount for p in positions if p.participant_id == "provider_tenant"), Decimal(0))
            counts = {key: db.scalar(self.select(self.func.count()).select_from(finance[key]))
                      for key in ("Baseline", "Position", "Transaction", "Confirmation", "Allocation", "Idempotency")}
        report = self.report()
        self.assertEqual(Decimal(report["summary"]["provider_total_receivable"]), provider_receivable)
        self.assertEqual(self.download().status_code, 200)
        with m.SessionLocal() as db:
            self.assertEqual(counts, {key: db.scalar(self.select(self.func.count()).select_from(finance[key])) for key in counts})

    def capture_order(self, amount="150.00", *, expert="0.00", channel="0.00", service="0.00", configuration=None):
        m = self.m
        oid, ids = self.seed(amount=amount, status="locked", expert_fee=Decimal(expert), channel_fee=Decimal(channel), service_share=Decimal(service))
        with m.SessionLocal() as db:
            order = db.get(m.Order, oid)
            economic = dict(currency="CNY")
            if configuration:
                economic["settlement_parties"] = dict(id=configuration[0], version=configuration[1])
            order.economic_snapshot_json = json.dumps(economic)
            db.add(m.Payment(order_id=oid, payment_no="PAY-" + oid, amount=Decimal(amount),
                             status="paid", paid_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
                             proof="Manual receipt fixture", confirmed_by="finance"))
            settlement = db.get(m.Settlement, ids[0])
            settlement.provider_share -= Decimal(expert) + Decimal(channel) + Decimal(service)
            batch = m.SettlementBatch(batch_no="BATCH-" + oid, cycle="monthly", status="confirmed",
                period_start=datetime(2026, 8, 31, 16, tzinfo=timezone.utc), idempotency_key=oid)
            db.add(batch)
            db.flush()
            db.add(m.SettlementLine(batch_id=batch.id, settlement_id=ids[0], participant_type="expert", participant_name="Placeholder", amount=Decimal(expert)))
            db.flush()
            m.settlement_finance["capture"](db, settlement, actor="finance")
            db.commit()
        return oid, ids[0]

    def record_payment(self, sid, *, kind="payout", value=None, key="paid"):
        m = self.m
        finance = m.settlement_finance
        with m.SessionLocal() as db:
            positions = db.scalars(self.select(finance["Position"]).where(
                finance["Position"].settlement_id == sid,
                finance["Position"].direction == ("payable" if kind == "payout" else "receivable"))).all()
            if value is not None:
                positions = [p for p in positions if p.kind == "cost"]
            body = dict(kind=kind, mode="manual", reference="VOUCHER-" + key, request_id=key,
                allocations=[dict(position_id=p.id, amount=str(value if value is not None else p.amount)) for p in positions])
            tx = finance["draft"](db, db.get(m.User, "finance"), body)
            finance["confirm"](db, db.get(m.User, "ops"), tx["id"],
                               dict(request_id=key + "-confirmed", expected_version=1, note="Checked voucher"))
            db.commit()

    def test_participant_cost_split_and_real_fund_balances_then_proportional_refund(self):
        oid, sid = self.capture_order()
        initial = self.report()
        provider = next(p for p in initial["participants"] if p["participant_type"] == "provider")
        platform = next(p for p in initial["participants"] if p["participant_type"] == "platform")
        self.assertEqual((provider["cost_amount"], provider["profit_share"], provider["total_receivable"]), ("100.00", "40.00", "140.00"))
        self.assertEqual((platform["cost_amount"], platform["profit_share"], platform["total_receivable"]), ("0.00", "10.00", "10.00"))
        self.assertFalse(provider["provisional"])
        self.assertEqual(initial["summary"]["funds"]["unpaid_amount"], "140.00")
        export = list(csv.DictReader(io.StringIO(self.download(kind="participants").content.decode("utf-8-sig"))))
        exported_provider = next(p for p in export if p["分成类别"] == "provider")
        self.assertEqual((exported_provider["供货成本"], exported_provider["利润分成"], exported_provider["成本加分成应收"]), ("100.00", "40.00", "140.00"))
        self.record_payment(sid)
        m = self.m
        with m.SessionLocal() as db:
            negative = m.Settlement(settlement_no="SET-REFUND", order_id=oid, reference_settlement_id=sid,
                refund_id="half-refund", is_refund=True, gross_amount=-75, net_amount=-75, refund_amount=-75,
                cost_amount=-50, profit_amount=-25, platform_fee=-5, provider_share=-20, status="locked",
                created_at=datetime(2026, 9, 20, tzinfo=timezone.utc))
            db.add(negative)
            db.flush()
            m.settlement_finance["capture"](db, negative, actor="finance")
            negative_id = negative.id
            db.commit()
        refund = self.report(source_kind="refund")
        provider = next(p for p in refund["participants"] if p["participant_type"] == "provider")
        self.assertEqual((provider["cost_amount"], provider["profit_share"], provider["total_receivable"]), ("-50.00", "-20.00", "-70.00"))
        self.assertEqual(refund["summary"]["funds"]["unrecovered_amount"], "70.00")
        self.record_payment(negative_id, kind="recovery", value=Decimal("20.00"), key="recovered")
        funds = self.report()["summary"]["funds"]
        self.assertTrue(funds["known"])
        self.assertEqual((funds["paid_amount"], funds["unpaid_amount"], funds["recovered_amount"], funds["unrecovered_amount"]), ("140.00", "0.00", "20.00", "50.00"))

    def test_frozen_parties_multi_experts_channels_provisional_and_final(self):
        m = self.m
        parties = m.settlement_parties
        with m.SessionLocal() as db:
            for role, pid, name, enterprise, user in (
                    ("expert", "expert-a", "Same Expert", "provider_tenant", "provider"),
                    ("expert", "expert-b", "Same Expert", "buyer_tenant", "buyer"),
                    ("channel", "channel-a", "Channel A", "provider_tenant", ""),
                    ("channel", "channel-b", "Channel B", "other_tenant", "")):
                db.add(parties["Participant"](id=pid, name=name, enterprise_id=enterprise, user_id=user,
                    owner_enterprise_id="provider_tenant", kind=role, currency="CNY", creator_id="finance"))
            db.flush()
            configuration = parties["Configuration"](product_id="product", version=1, creator_id="finance",
                allocations_json=json.dumps([dict(participant_type=role, participant_id=pid, participant_name=name,
                    currency="CNY", weight=weight) for role, pid, name, weight in (
                        ("expert", "expert-a", "Same Expert", "25"), ("expert", "expert-b", "Same Expert", "75"),
                        ("channel", "channel-a", "Channel A", "50"), ("channel", "channel-b", "Channel B", "50"))]))
            db.add(configuration)
            db.commit()
            frozen = (configuration.id, configuration.version)
        oid, sid = self.capture_order(expert="6.00", channel="4.00", configuration=frozen)
        data = self.report()
        real = {p["participant_id"]: p for p in data["participants"]}
        self.assertEqual({pid: real[pid]["profit_share"] for pid in ("expert-a", "expert-b", "channel-a", "channel-b")},
                         {"expert-a": "1.50", "expert-b": "4.50", "channel-a": "2.00", "channel-b": "2.00"})
        self.assertTrue(all(real[pid]["cost_amount"] == "0.00" for pid in ("expert-a", "expert-b", "channel-a", "channel-b")))
        # Current product configuration and placeholder lines cannot rewrite a final baseline.
        with m.SessionLocal() as db:
            db.get(m.Order, oid).economic_snapshot_json = '{"currency":"CNY"}'
            db.commit()
        self.assertEqual(data["participants"], self.report()["participants"])
        with m.SessionLocal() as db:
            negative = m.Settlement(settlement_no="SET-MULTI-REFUND", order_id=oid, reference_settlement_id=sid,
                refund_id="multi-refund", is_refund=True, gross_amount=-75, net_amount=-75, refund_amount=-75,
                cost_amount=-50, profit_amount=-25, platform_fee=-5, provider_share=-15,
                expert_fee=-3, channel_fee=-2, status="locked", created_at=datetime(2026, 9, 20, tzinfo=timezone.utc))
            db.add(negative)
            db.flush()
            m.settlement_finance["capture"](db, negative, actor="finance")
            db.commit()
        reversals = {p["participant_id"]: p for p in self.report(source_kind="refund")["participants"]}
        self.assertEqual({pid: reversals[pid]["profit_share"] for pid in ("expert-a", "expert-b", "channel-a", "channel-b")},
                         {"expert-a": "-0.75", "expert-b": "-2.25", "channel-a": "-1.00", "channel-b": "-1.00"})
        self.assertEqual(reversals["provider_tenant"]["total_receivable"], "-65.00")
        self.assertTrue(all(not p["provisional"] for p in reversals.values()))
        _, ids = self.seed(amount="150.00", expert_fee=Decimal("6.00"), channel_fee=Decimal("4.00"))
        with m.SessionLocal() as db:
            order = db.get(m.Order, db.get(m.Settlement, ids[0]).order_id)
            order.economic_snapshot_json = json.dumps(dict(currency="CNY", settlement_parties=dict(id=frozen[0], version=frozen[1])))
            db.commit()
            before = db.scalar(self.select(self.func.count()).select_from(m.settlement_finance["Baseline"]))
        combined = self.report()
        experts = {p["participant_id"]: p for p in combined["participants"] if p["participant_type"] == "expert"}
        self.assertEqual((experts["expert-a"]["profit_share"], experts["expert-b"]["profit_share"]), ("2.25", "6.75"))
        self.assertTrue(all(p["provisional"] for p in experts.values()))
        with m.SessionLocal() as db:
            self.assertEqual(before, db.scalar(self.select(self.func.count()).select_from(m.settlement_finance["Baseline"])))

    def test_missing_evidence_stays_unknown_and_draft_unresolved_is_specific(self):
        self.seed()
        data = self.report()
        self.assertFalse(data["summary"]["funds"]["known"])
        self.assertIsNone(data["summary"]["funds"]["unpaid_amount"])
        self.assertTrue(data["scope"]["includes_provisional"])
        snapshot = self.request("POST", self.prefix + "/annual-reports", actor="finance", json=dict(year=2026, request_id="unknown"))
        failure = self.request("POST", self.prefix + "/annual-reports/" + snapshot["id"] + "/finalize", actor="ops", expected=409)
        self.assertIn("fund_balances_unknown", failure["detail"]["unresolved"])

    def finalization_body(self, snapshot):
        return dict(request_id="finalize-" + snapshot["id"], snapshot_digest=snapshot["digest"],
                    reconciliation_reference="MANUAL-RECONCILIATION-2026", note="Reconciled all recorded vouchers")

    def beneficiary_fixture(self):
        m = self.m
        parties = m.settlement_parties
        definitions = (
            ("own-expert", "expert", "buyer_tenant", "buyer", '=Expert,"self"\nName', "25"),
            ("other-expert", "expert", "buyer_tenant", "buyer_admin", "Other Expert Private", "75"),
            ("own-channel", "channel", "buyer_tenant", "", "Corporate Channel", "100"),
            ("other-service", "service", "other_tenant", "", "Other Tenant Service", "100"),
        )
        with m.SessionLocal() as db:
            for pid, role, enterprise, user, name, weight in definitions:
                db.add(parties["Participant"](id=pid, name=name, kind=role, enterprise_id=enterprise,
                    user_id=user, owner_enterprise_id="provider_tenant", currency="CNY", creator_id="finance"))
            configuration = parties["Configuration"](product_id="product", version=1, creator_id="finance",
                allocations_json=json.dumps([dict(participant_id=pid, participant_type=role, participant_name=name,
                    currency="CNY", weight=weight) for pid, role, enterprise, user, name, weight in definitions]))
            db.add(configuration)
            member = db.scalar(self.select(m.Membership).where(m.Membership.user_id == "buyer"))
            member.role = "member"
            db.commit()
            config = configuration.id, configuration.version
        return self.capture_order(expert="6.00", channel="4.00", service="8.00", configuration=config)

    def beneficiary_report(self, actor="buyer", identity="own-expert", **params):
        return self.report(actor=actor, business_view="beneficiary", participant_id=identity, **params)

    def beneficiary_payment(self, sid, identity, amount, key, kind="payout"):
        m = self.m
        finance = m.settlement_finance
        with m.SessionLocal() as db:
            position = db.scalar(self.select(finance["Position"]).where(
                finance["Position"].settlement_id == sid, finance["Position"].participant_id == identity))
            tx = finance["draft"](db, db.get(m.User, "finance"), dict(kind=kind, mode="manual",
                reference="PRIVATE-ACCOUNTING-VOUCHER-" + key, request_id=key,
                allocations=[dict(position_id=position.id, amount=amount)]))
            finance["confirm"](db, db.get(m.User, "ops"), tx["id"],
                dict(request_id=key + "-confirm", expected_version=1, note="Checked"))
            db.commit()

    def test_beneficiary_actual_registry_identity_and_redacted_own_financials(self):
        self.beneficiary_fixture()
        result = self.beneficiary_report()
        self.assertEqual(result["scope"]["kind"], "beneficiary")
        self.assertEqual((result["summary"]["cost_amount"], result["summary"]["profit_share"], result["summary"]["total_receivable"]), ("0.00", "1.50", "1.50"))
        self.assertEqual(result["summary"]["unpaid_amount"], "1.50")
        self.assertEqual(result["rankings"], [])
        denied_keys = {"gross_amount", "profit_amount", "provider_share", "provider_total_receivable", "platform_fee",
            "expert_fee", "channel_fee", "service_share", "order_no", "order_id", "buyer_name", "baseline_hash",
            "evidence", "snapshot_json", "reference", "reviewer_id", "_counts"}
        def check(value):
            if isinstance(value, dict):
                self.assertFalse(denied_keys.intersection(value), value)
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)
        check(result)
        self.assertNotIn("Other Expert Private", json.dumps(result))
        self.assertNotIn("other-expert", json.dumps(result))
        own_other = self.beneficiary_report(actor="buyer_admin", identity="other-expert")
        self.assertEqual(own_other["summary"]["profit_share"], "4.50")
        channel = self.beneficiary_report(actor="buyer_admin", identity="own-channel", enterprise_id="buyer_tenant")
        self.assertEqual(channel["summary"]["profit_share"], "4.00")
        service = self.beneficiary_report(actor="outsider", identity="other-service", enterprise_id="other_tenant")
        self.assertEqual(service["summary"]["profit_share"], "8.00")
        self.assertEqual(self.report()["summary"]["provider_total_receivable"], "122.00")
        self.assertEqual(self.report(actor="provider", business_view="provider")["summary"]["provider_total_receivable"], "122.00")

    def test_beneficiary_cross_identity_owner_enterprise_and_filter_manipulation_denied(self):
        self.beneficiary_fixture()
        for actor, identity, params in (
                ("buyer", "other-expert", {}), ("buyer_admin", "own-expert", {"enterprise_id": "buyer_tenant"}),
                ("provider", "own-expert", {"enterprise_id": "provider_tenant"}),
                ("buyer", "other-service", {"enterprise_id": "other_tenant"}),
                ("buyer", "own-channel", {"enterprise_id": "buyer_tenant"}),
                ("buyer_admin", "own-channel", {}), ("buyer", "own-expert", {"enterprise_id": "other_tenant"}),
                ("buyer", "unknown-id", {})):
            for path in ("/report", "/export"):
                self.request("GET", self.prefix + path, actor=actor, expected=403,
                    params=dict(year=2026, business_view="beneficiary", participant_id=identity, **params))
        self.request("GET", self.prefix + "/report", expected=403,
            params=dict(year=2026, business_view="beneficiary", participant_id="own-expert", participant_type="provider"))
        self.assertEqual(self.beneficiary_report(provider_id="other_tenant")["total"], 0)
        self.assertEqual(self.beneficiary_report(product_id="unknown")["total"], 0)
        self.assertEqual(self.beneficiary_report(user_id="buyer_admin", subject_id="other-expert")["summary"]["profit_share"], "1.50")
        self.request("GET", self.prefix + "/report", actor="buyer", expected=403)

    def test_beneficiary_selector_and_membership_verification_rechecked(self):
        self.beneficiary_fixture()
        listed = self.request("GET", self.prefix + "/beneficiaries", actor="buyer")
        self.assertEqual([p["participant_id"] for p in listed["items"]], ["own-expert"])
        listed = self.request("GET", self.prefix + "/beneficiaries", actor="buyer_admin", params={"enterprise_id": "buyer_tenant"})
        self.assertEqual({p["participant_id"] for p in listed["items"]}, {"other-expert", "own-channel"})
        with self.m.SessionLocal() as db:
            member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "buyer"))
            member.status = "disabled"
            db.commit()
        for path in ("/report", "/export"):
            self.request("GET", self.prefix + path, expected=403, params=dict(year=2026, business_view="beneficiary", participant_id="own-expert"))
        self.assertEqual(self.request("GET", self.prefix + "/beneficiaries")["items"], [])
        with self.m.SessionLocal() as db:
            member = db.scalar(self.select(self.m.Membership).where(self.m.Membership.user_id == "buyer"))
            member.status = "active"
            db.get(self.m.User, "buyer").verified_status = "pending_review"
            db.commit()
        self.request("GET", self.prefix + "/report", expected=403, params=dict(year=2026, business_view="beneficiary", participant_id="own-expert"))

    def test_beneficiary_payments_refunds_offsets_recovery_and_complete_csv(self):
        oid, sid = self.beneficiary_fixture()
        self.beneficiary_payment(sid, "own-expert", "1.00", "self-paid")
        self.beneficiary_payment(sid, "other-expert", "3.00", "other-paid")
        m = self.m
        with m.SessionLocal() as db:
            negative = m.Settlement(settlement_no="SET-OWN-REFUND", order_id=oid, reference_settlement_id=sid,
                refund_id="own-half-refund", is_refund=True, status="locked", gross_amount=-75, net_amount=-75,
                cost_amount=-50, profit_amount=-25, platform_fee=-5, provider_share=-11, expert_fee=-3,
                channel_fee=-2, service_share=-4, created_at=datetime(2026, 9, 20, tzinfo=timezone.utc))
            db.add(negative)
            db.flush()
            m.settlement_finance["capture"](db, negative, actor="finance")
            negative_id = negative.id
            batch = m.SettlementBatch(batch_no="BATCH-OWN-REFUND", cycle="manual", status="generated",
                idempotency_key="own-refund-batch")
            db.add(batch)
            db.flush()
            db.add(m.SettlementLine(batch_id=batch.id, settlement_id=negative.id, participant_type="expert",
                participant_name="Placeholder", amount=Decimal("-3.00")))
            db.flush()
            waiting = m.settlement_finance["reconcile_offsets"](db, batch.id, actor="finance")
            self.assertEqual(waiting["status"], "waiting")
            batch.status = "confirmed"
            db.flush()
            reconciled = m.settlement_finance["reconcile_offsets"](db, batch.id, actor="finance")
            self.assertGreater(reconciled["offset_count"], 0)
            self.assertEqual(m.settlement_finance["reconcile_offsets"](db, batch.id, actor="finance")["offset_count"], 0)
            db.commit()
        self.beneficiary_payment(negative_id, "own-expert", "0.10", "self-recovery", kind="recovery")
        result = self.beneficiary_report(page_size=1)
        self.assertEqual((result["total"], len(result["items"])), (2, 1))
        summary = result["summary"]
        self.assertEqual((summary["profit_share"], summary["paid_amount"], summary["recovered_amount"]), ("0.75", "1.00", "0.10"))
        self.assertEqual((summary["payable_offset_amount"], summary["receivable_offset_amount"], summary["unpaid_amount"], summary["unrecovered_amount"]), ("0.50", "0.50", "0.00", "0.15"))
        self.assertEqual(summary["refund_count"], 1)
        self.assertEqual(self.beneficiary_report(source_kind="refund")["summary"]["profit_share"], "-0.75")
        self.assertEqual(self.beneficiary_report(q="SET-OWN-REFUND")["total"], 1)
        self.assertEqual(self.beneficiary_report(start="2026-09-20", end="2026-09-20")["total"], 1)
        for kind in ("details", "summary", "participants", "products", "providers", "refunds", "rankings"):
            response = self.download(actor="buyer", business_view="beneficiary", participant_id="own-expert", kind=kind, page_size=1)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("Other Expert Private", response.text)
            self.assertNotIn("PRIVATE-ACCOUNTING-VOUCHER", response.text)
            self.assertNotIn("供货成本回收", response.text)
            rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
            if kind == "details":
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(row["收益主体名称"].startswith("'=Expert") for row in rows))
                self.assertEqual({row["供货成本"] for row in rows}, {"0.00"})
        self.assertEqual(self.download(actor="buyer", business_view="beneficiary", participant_id="own-expert", kind="details").content,
                         self.download(actor="buyer", business_view="beneficiary", participant_id="own-expert", kind="details").content)

    def test_beneficiary_cannot_download_full_annual_snapshots_or_write_finance(self):
        self.beneficiary_fixture()
        snapshot = self.request("POST", self.prefix + "/annual-reports", actor="finance", json=dict(year=2026, request_id="beneficiary-denied"))
        params = dict(business_view="beneficiary", participant_id="own-expert")
        self.request("GET", self.prefix + "/annual-reports", expected=403, params=params)
        self.request("GET", self.prefix + "/annual-reports/" + snapshot["id"] + "/export", expected=404, params=params)
        self.request("POST", self.prefix + "/annual-reports", expected=403, params=params, json=dict(year=2026, request_id="deny"))
        self.request("POST", "/api/settlement-finance/transactions", expected=403, json={})

    def test_beneficiary_active_verified_users_enterprises_and_internal_registry_roles(self):
        self.beneficiary_fixture()
        params = dict(year=2026, business_view="beneficiary", participant_id="own-expert")
        for field, value, expected in (("is_active", False, 401), ("activation_status", "pending_activation", 403),
                                       ("verified_status", "pending_review", 403)):
            with self.m.SessionLocal() as db:
                user = db.get(self.m.User, "buyer")
                original = getattr(user, field)
                setattr(user, field, value)
                db.commit()
            for path in ("/report", "/export"):
                self.request("GET", self.prefix + path, expected=expected, params=params)
            with self.m.SessionLocal() as db:
                setattr(db.get(self.m.User, "buyer"), field, original)
                db.commit()
        with self.m.SessionLocal() as db:
            enterprise = db.get(self.m.Enterprise, "buyer_tenant")
            enterprise.verification_status = "pending_review"
            db.commit()
        self.request("GET", self.prefix + "/report", expected=403, params=params)
        with self.m.SessionLocal() as db:
            db.get(self.m.Enterprise, "buyer_tenant").verification_status = "verified"
            Participant = self.m.settlement_parties["Participant"]
            for role in ("platform", "provider"):
                db.add(Participant(id="internal-" + role, name=role, kind=role, enterprise_id="buyer_tenant",
                    user_id="buyer", owner_enterprise_id="provider_tenant", currency="CNY", creator_id="finance"))
            db.commit()
        for role in ("platform", "provider"):
            self.request("GET", self.prefix + "/report", expected=403, params=params | {"participant_id": "internal-" + role})
        self.assertEqual([p["participant_id"] for p in self.request("GET", self.prefix + "/beneficiaries")["items"]], ["own-expert"])
        with self.m.SessionLocal() as db:
            settlement = db.scalar(self.select(self.m.Settlement))
            db.add(self.m.settlement_finance["Position"](id="invalid-internal-benefit", settlement_id=settlement.id,
                order_id=settlement.order_id, kind="profit", share_kind="expert_fee", participant_type="expert",
                participant_id="own-expert", participant_name="Self", direction="internal", currency="CNY", amount=Decimal("0.01")))
            db.commit()
        for path in ("/report", "/export"):
            failure = self.request("GET", self.prefix + path, expected=409, params=params)
            self.assertEqual(failure["detail"], "beneficiary_position_direction_invalid")

    def test_beneficiary_enterprise_admin_role_and_explicit_multi_enterprise_scope(self):
        self.beneficiary_fixture()
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.Membership(user_id="buyer_admin", enterprise_id="other_tenant", role="enterprise_admin", status="active"))
            db.commit()
        self.assertEqual(self.beneficiary_report(actor="buyer_admin", identity="own-channel", enterprise_id="buyer_tenant")["summary"]["profit_share"], "4.00")
        self.assertEqual(self.beneficiary_report(actor="buyer_admin", identity="other-service", enterprise_id="other_tenant")["summary"]["profit_share"], "8.00")
        params = dict(year=2026, business_view="beneficiary", participant_id="other-service", enterprise_id="buyer_tenant")
        self.request("GET", self.prefix + "/report", actor="buyer_admin", expected=403, params=params)
        response = self.client.get(self.prefix + "/report", headers={"Authorization": "Bearer " + self.tokens["buyer_admin"],
            "X-Enterprise-Id": "other_tenant"}, params=params)
        self.assertEqual(response.status_code, 400, response.text)
        with m.SessionLocal() as db:
            member = db.scalar(self.select(m.Membership).where(m.Membership.user_id == "buyer_admin", m.Membership.enterprise_id == "other_tenant"))
            member.role = "member"
            db.commit()
        self.request("GET", self.prefix + "/export", actor="buyer_admin", expected=403,
            params=params | {"enterprise_id": "other_tenant"})

    def test_beneficiary_loss_liability_exposed_and_unknown_kind_fails_closed(self):
        oid, sid = self.beneficiary_fixture()
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.settlement_finance["Position"](id="own-loss", settlement_id=sid, order_id=oid, kind="loss",
                share_kind="expert_fee", participant_type="expert", participant_id="own-expert", participant_name="Self",
                currency="CNY", direction="receivable", amount=Decimal("0.40"), blocked_reason=""))
            db.commit()
        result = self.beneficiary_report()
        self.assertEqual((result["summary"]["profit_share"], result["summary"]["loss_amount"], result["summary"]["total_receivable"]), ("1.50", "0.40", "1.10"))
        self.assertEqual(result["summary"]["unrecovered_amount"], "0.40")
        self.assertEqual(result["summary"]["settlement_count"], 1)
        self.assertEqual(result["total"], 2)
        export = self.download(actor="buyer", business_view="beneficiary", participant_id="own-expert")
        self.assertIn("承担损失金额", export.text)
        self.assertIn("loss", export.text)
        self.assertEqual(self.beneficiary_report(actor="buyer_admin", identity="other-expert")["summary"]["loss_amount"], "0.00")
        with m.SessionLocal() as db:
            db.add(m.settlement_finance["Position"](id="own-unknown", settlement_id=sid, order_id=oid, kind="future_kind",
                share_kind="expert_fee", participant_type="expert", participant_id="own-expert", participant_name="Self",
                currency="CNY", direction="receivable", amount=Decimal("0.01"), blocked_reason=""))
            db.commit()
        for path in ("/report", "/export"):
            error = self.request("GET", self.prefix + path, expected=409,
                params=dict(year=2026, business_view="beneficiary", participant_id="own-expert"))
            self.assertEqual(error["detail"], "beneficiary_position_kind_unsupported")

    def test_approved_cost_exception_separates_ordinary_profit_and_loss_in_all_scopes(self):
        oid, sid = self.beneficiary_fixture()
        m = self.m
        finance = m.settlement_finance
        with m.SessionLocal() as db:
            payment = db.scalar(self.select(m.Payment).where(m.Payment.order_id == oid))
            refund = m.Refund(refund_no="REF-APPROVED-LOSS", order_id=oid, payment_id=payment.id,
                              amount=Decimal("75.00"), status="approved", reason="Partial refund")
            db.add(refund)
            db.commit()
            refund_id = refund.id
        exception = self.request("POST", "/api/settlement-cost-exceptions", actor="finance", json=dict(
            request_id="retained-cost", refund_id=refund_id, retained_cost="10.00", bearer_participant_id="own-expert",
            reason="Approved nonreturnable cost", evidence_refs=["PRIVATE-COST-EVIDENCE"]))
        self.request("POST", "/api/settlement-cost-exceptions/" + exception["id"] + "/decision", actor="provider",
            params=dict(enterprise_id="provider_tenant", business_view="provider"),
            json=dict(request_id="retained-approved", expected_version=1, decision="approve", reason="Checked actual cost"))
        with m.SessionLocal() as db:
            baseline = db.get(finance["Baseline"], sid)
            default = dict(status="ready", reference_settlement_id=sid, baseline_hash=baseline.snapshot_hash,
                currency="CNY", gross_amount="-75.00", cost_amount="-50.00", profit_amount="-25.00",
                shares=dict(platform_fee="-5.00", provider_share="-11.00", expert_fee="-3.00", channel_fee="-2.00", service_share="-4.00"))
            plan = finance["apply_cost_exception"](db, db.get(m.Refund, refund_id), baseline, default, actor="finance")
            self.assertEqual(plan["status"], "ready", plan)
            negative = m.Settlement(settlement_no="SET-APPROVED-LOSS", order_id=oid, reference_settlement_id=sid,
                refund_id=refund_id, is_refund=True, status="locked", gross_amount=plan["gross_amount"],
                net_amount=plan["gross_amount"], cost_amount=plan["cost_amount"], profit_amount=plan["profit_amount"],
                created_at=datetime(2026, 9, 20, tzinfo=timezone.utc), **plan["shares"])
            db.add(negative)
            db.flush()
            captured = finance["capture"](db, negative, actor="finance")
            self.assertNotIsInstance(captured, dict)
            db.commit()
        for actor, params in (("finance", {}), ("provider", dict(business_view="provider"))):
            report = self.report(actor=actor, source_kind="refund", **params)
            own = next(p for p in report["participants"] if p["participant_id"] == "own-expert")
            self.assertEqual((own["profit_share"], own["loss_amount"], own["amount"], own["total_receivable"]),
                             ("-0.75", "10.00", "-10.75", "-10.75"))
            self.assertEqual(sum(Decimal(p["total_receivable"]) for p in report["participants"]), Decimal("-75.00"))
            response = self.download(actor=actor, kind="participants", source_kind="refund", **params)
            self.assertEqual(response.status_code, 200, response.text)
            exported = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
            entry = next(p for p in exported if p["收益主体关联ID"] == "own-expert")
            self.assertEqual((entry["利润分成"], entry["承担损失金额"]), ("-0.75", "10.00"))
        own = self.beneficiary_report(source_kind="refund")
        self.assertEqual((own["summary"]["profit_share"], own["summary"]["loss_amount"], own["summary"]["total_receivable"]),
                         ("-0.75", "10.00", "-10.75"))
        loss = next(row for row in own["items"] if row["position_kind"] == "loss")
        self.assertEqual((loss["liability_source"], loss["cost_exception_id"]), ("approved_cost_exception", exception["id"]))
        self.assertEqual(loss["loss_label"], "已批准成本例外承担")
        self.assertNotIn("PRIVATE-COST-EVIDENCE", json.dumps(own))
        other = self.beneficiary_report(actor="buyer_admin", identity="other-expert", source_kind="refund")
        self.assertEqual((other["summary"]["profit_share"], other["summary"]["loss_amount"]), ("-2.25", "0.00"))
        self.assertNotIn(exception["id"], json.dumps(other))

    def test_beneficiary_full_aggregation_and_export_over_2000_positions(self):
        from sqlalchemy import insert
        _, sid = self.beneficiary_fixture()
        m = self.m
        with m.SessionLocal() as db:
            finance = m.settlement_finance
            settlement = db.get(m.Settlement, sid)
            baseline = db.get(finance["Baseline"], sid)
            position = db.scalar(self.select(finance["Position"]).where(
                finance["Position"].settlement_id == sid, finance["Position"].participant_id == "own-expert"))
            def values(model, row):
                return {c.name: getattr(row, c.name) for c in model.__table__.columns}
            st, bl, po = values(m.Settlement, settlement), values(finance["Baseline"], baseline), values(finance["Position"], position)
            ids = [f"beneficiary-record-{i}" for i in range(2006)]
            db.execute(insert(m.Settlement), [dict(st, id=key, settlement_no="SET-" + key) for key in ids])
            db.execute(insert(finance["Baseline"]), [dict(bl, settlement_id=key) for key in ids])
            db.execute(insert(finance["Position"]), [dict(po, id="own-position-" + key, settlement_id=key) for key in ids])
            db.commit()
        result = self.beneficiary_report(page_size=2)
        self.assertEqual((result["total"], len(result["items"])), (2007, 2))
        self.assertEqual(result["summary"]["profit_share"], "3010.50")
        self.assertEqual(result["summary"]["unpaid_amount"], "3010.50")
        self.assertEqual(result["summary"]["settlement_count"], 2007)
        self.assertEqual(result["summary"]["income_order_count"], 1)
        self.assertEqual(result["monthly"][8]["profit_share"], "3010.50")
        second = self.beneficiary_report(page_size=2, page=2)
        self.assertTrue({r["position_id"] for r in result["items"]}.isdisjoint(r["position_id"] for r in second["items"]))
        response = self.download(actor="buyer", business_view="beneficiary", participant_id="own-expert", page_size=1, page=2)
        self.assertEqual(response.status_code, 200, response.text)
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2007)
        self.assertEqual(sum(Decimal(r["利润分成"]) for r in rows), Decimal("3010.50"))
        self.assertTrue(all(r["收益主体关联ID"] == "own-expert" for r in rows))

    def personal_beneficiary_fixture(self):
        m = self.m
        parties = m.settlement_parties
        with m.SessionLocal() as db:
            for pid, enterprise in (("personal-expert", ""), ("bound-expert", "buyer_tenant")):
                db.add(parties["Participant"](id=pid, name=pid, kind="expert", enterprise_id=enterprise,
                    user_id="buyer", owner_enterprise_id="provider_tenant", currency="CNY", creator_id="finance"))
            config = parties["Configuration"](product_id="product", version=1, creator_id="finance",
                allocations_json=json.dumps([dict(participant_id="personal-expert", participant_type="expert",
                    participant_name="Personal Expert", currency="CNY", weight="100")]))
            db.add(config)
            member = db.scalar(self.select(m.Membership).where(m.Membership.user_id == "buyer"))
            member.role = "member"
            db.commit()
            frozen = config.id, config.version
        return self.capture_order(expert="6.00", configuration=frozen)

    def test_personal_beneficiary_ignores_current_enterprise_but_bound_identity_does_not(self):
        self.personal_beneficiary_fixture()
        params = dict(year=2026, business_view="beneficiary", participant_id="personal-expert")
        for enterprise in ("buyer_tenant", "other_tenant"):
            headers = {"Authorization": "Bearer " + self.tokens["buyer"], "X-Enterprise-Id": enterprise}
            selector = self.client.get(self.prefix + "/beneficiaries", headers=headers)
            self.assertEqual(selector.status_code, 200, selector.text)
            self.assertIn("personal-expert", [p["participant_id"] for p in selector.json()["items"]])
            report = self.client.get(self.prefix + "/report", headers=headers, params=params)
            self.assertEqual(report.status_code, 200, report.text)
            self.assertEqual(report.json()["summary"]["profit_share"], "6.00")
            export = self.client.get(self.prefix + "/export", headers=headers, params=params)
            self.assertEqual(export.status_code, 200, export.text)
            self.assertIn("personal-expert", export.text)
        wrong = self.client.get(self.prefix + "/report", headers=headers,
                                params=params | {"participant_id": "bound-expert"})
        self.assertEqual(wrong.status_code, 403, wrong.text)
        self.request("GET", self.prefix + "/report", actor="buyer_admin", expected=403, params=params)

    @unittest.skipUnless(os.getenv("CLR2_REPORTING_BROWSER") == "1", "Enable isolated Chromium contract test with CLR2_REPORTING_BROWSER=1")
    def test_personal_beneficiary_browser_with_console_enterprise_header(self):
        import socket
        import threading
        import time
        import uvicorn
        from playwright.sync_api import sync_playwright

        self.personal_beneficiary_fixture()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            base = "http://127.0.0.1:" + str(listener.getsockname()[1])
            server = uvicorn.Server(uvicorn.Config(self.m.app, log_level="error", lifespan="off"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(server.started)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
                    try:
                        page = browser.new_page()
                        page.goto(base + "/api/health")
                        result = page.evaluate("""async ({token, prefix}) => {
                            const headers = {'Authorization': 'Bearer ' + token, 'X-Enterprise-Id': 'buyer_tenant'};
                            const identities = await fetch(prefix + '/beneficiaries', {headers});
                            const selected = await identities.json();
                            const params = new URLSearchParams({year: '2026', business_view: 'beneficiary', participant_id: 'personal-expert'});
                            const report = await fetch(prefix + '/report?' + params, {headers});
                            const data = await report.json();
                            const exported = await fetch(prefix + '/export?' + params, {headers});
                            const other = await fetch(prefix + '/report?' + new URLSearchParams({year: '2026', business_view: 'beneficiary', participant_id: 'bound-expert'}),
                                {headers: {...headers, 'X-Enterprise-Id': 'other_tenant'}});
                            return {selector: identities.status, identities: selected.items, status: report.status,
                                data, exportStatus: exported.status, csv: await exported.text(), boundWrongEnterprise: other.status};
                        }""", dict(token=self.tokens["buyer"], prefix=self.prefix))
                        self.assertEqual((result["selector"], result["status"], result["exportStatus"]), (200, 200, 200))
                        self.assertIn("personal-expert", [p["participant_id"] for p in result["identities"]])
                        self.assertEqual(result["data"]["summary"]["profit_share"], "6.00")
                        self.assertNotIn("gross_amount", result["data"]["summary"])
                        self.assertIn("personal-expert", result["csv"])
                        self.assertEqual(result["boundWrongEnterprise"], 403)
                    finally:
                        browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), "Isolated browser server did not stop")

    def test_annual_formal_separate_immutable_record_and_cutoff_change_rejection(self):
        _, sid = self.capture_order()
        open_snapshot = self.request("POST", self.prefix + "/annual-reports", actor="finance", json=dict(year=2026, request_id="open-balance"))
        failure = self.request("POST", self.prefix + "/annual-reports/" + open_snapshot["id"] + "/finalize", actor="ops", expected=409)
        self.assertIn("open_fund_balances", failure["detail"]["unresolved"])
        self.assertNotIn("fund_balances_unknown", failure["detail"]["unresolved"])
        self.record_payment(sid)
        snapshot = self.request("POST", self.prefix + "/annual-reports", actor="finance", json=dict(year=2026, request_id="ready"))
        body = self.finalization_body(snapshot)
        path = self.prefix + "/annual-reports/" + snapshot["id"] + "/finalize"
        self.request("POST", path, actor="finance", expected=403, json=body)
        self.request("POST", path, actor="ops", expected=422)
        formal = self.request("POST", path, actor="ops", json=body)
        self.assertEqual(formal["status"], "formal")
        self.assertEqual(self.request("POST", path, actor="ops", json=body), formal)
        download = self.download("/annual-reports/" + snapshot["id"] + "/export")
        self.assertEqual(download.headers["X-Report-Status"], "formal")
        with self.m.SessionLocal() as db:
            draft = db.get(self.hooks["Model"], snapshot["id"])
            self.assertEqual((draft.status, draft.digest), ("draft", snapshot["digest"]))
            finalization = db.get(self.hooks["Finalization"], snapshot["id"])
            finalization.reviewer_id = "other"
            with self.assertRaisesRegex(ValueError, "immutable"):
                db.flush()
            db.rollback()
        changed = self.request("POST", self.prefix + "/annual-reports", actor="finance", json=dict(year=2026, request_id="before-change"))
        with self.m.SessionLocal() as db:
            db.get(self.m.Settlement, sid).gross_amount = Decimal("151.00")
            db.commit()
        source_changed = self.request("POST", self.prefix + "/annual-reports/" + changed["id"] + "/finalize", actor="ops", expected=409,
                                      json=self.finalization_body(changed))
        self.assertEqual(source_changed["detail"], "annual_data_changed_after_cutoff")
        self.seed()
        added = self.request("POST", self.prefix + "/annual-reports/" + changed["id"] + "/finalize", actor="ops", expected=409,
                             json=self.finalization_body(changed))
        self.assertEqual(added["detail"], "annual_data_changed_after_cutoff")


if __name__ == "__main__":
    unittest.main()
