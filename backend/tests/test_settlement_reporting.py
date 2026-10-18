"""Reporting contract against isolated host models; no startup or business data."""
import csv
import importlib
import io
import json
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
        self.assertFalse(data["scope"]["beneficiary_grants_supported"])
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
        self.assertEqual(len(reporting_routes), 6)
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

    def capture_order(self, amount="150.00", *, expert="0.00", channel="0.00", configuration=None):
        m = self.m
        oid, ids = self.seed(amount=amount, status="locked", expert_fee=Decimal(expert), channel_fee=Decimal(channel))
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
            settlement.provider_share -= Decimal(expert) + Decimal(channel)
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
