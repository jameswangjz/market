"""Actual app HTTP/manual funding and FILE buyer refund integration; no bank I/O."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest

from sqlalchemy import func, select

import test_settlement_finance_integration as fixture


class FinalFinanceWiringTests(unittest.TestCase):
    setUpClass = classmethod(fixture.SettlementFinanceIntegrationTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.SettlementFinanceIntegrationTests.tearDownClass.__func__)
    setUp = fixture.SettlementFinanceIntegrationTests.setUp
    tearDown = fixture.SettlementFinanceIntegrationTests.tearDown
    request = fixture.SettlementFinanceIntegrationTests.request
    create_order = fixture.SettlementFinanceIntegrationTests.create_order
    create = fixture.SettlementFinanceIntegrationTests.create
    manual = fixture.SettlementFinanceIntegrationTests.manual
    review = fixture.SettlementFinanceIntegrationTests.review
    execute = fixture.SettlementFinanceIntegrationTests.execute
    approved = fixture.SettlementFinanceIntegrationTests.approved
    step = fixture.SettlementFinanceIntegrationTests.step
    batch = fixture.SettlementFinanceIntegrationTests.batch
    complete = fixture.SettlementFinanceIntegrationTests.complete
    negative = fixture.SettlementFinanceIntegrationTests.negative

    def funded_source(self, kind='platform_absorption', cover=True):
        batch = self.batch()
        m = self.m
        with m.SessionLocal() as db:
            sid = db.scalar(select(m.Settlement.id).where(m.Settlement.order_id == self.order_id))
        proposal = self.request('POST', f'/api/settlements/{sid}/proposals', actor='finance', json=dict(
            gross_amount=110, cost_amount=40, profit_amount=70, platform_rate=50, provider_rate=50,
            service_rate=0, expert_rate=0, channel_rate=0, reason='Final 110 baseline with documented 10 funding gap'))
        self.request('POST', f'/api/settlement-proposals/{proposal["id"]}/decision', actor='provider',
                     json=dict(decision='approve', comment='Agreed final basis'))
        self.request('POST', f'/api/settlements/{sid}/lock', actor='finance')
        self.request('POST', f'/api/settlement-batches/{batch["id"]}/confirm', actor='finance', json={})
        funding = self.request('POST', '/api/settlement-finance/funding-adjustments', actor='finance', json=dict(
            request_id='funding', settlement_id=sid, kind=kind, amount='10.00',
            reason='Supplemental proof, not buyer Payment', evidence_refs=['funding:1'], bearer_enterprise_id='provider_tenant'))
        self.request('POST', f'/api/settlement-finance/funding-adjustments/{funding["id"]}/decision', actor='ops', json=dict(
            request_id='funding-approve', expected_version=1, decision='approved', note='Independent review'))
        if cover and kind == 'platform_absorption':
            self.request('POST', f'/api/settlement-finance/funding-adjustments/{funding["id"]}/verify', actor='ops',
                         json=dict(request_id='funding-verify', expected_version=2))
        return sid, funding

    def test_manual_receipt_routes_are_installed_and_feed_real_funding_verify(self):
        sid, funding = self.funded_source(kind='external_receipt', cover=False)
        receipt = self.request('POST', '/api/settlement-finance/manual-funding-evidence', actor='finance', json=dict(
            request_id='manual-register', funding_request_id=funding['id'], amount='10.00', currency='CNY',
            reference='MANUAL-RECEIPT-HTTP', paid_on=datetime.now(timezone.utc).date().isoformat(),
            evidence_refs=['receipt:1'], note='Manual independent receipt evidence, not bank reconciliation'))
        self.request('POST', f'/api/settlement-finance/manual-funding-evidence/{receipt["id"]}/review', actor='ops', json=dict(
            request_id='manual-review', expected_version=1, decision='verified', note='Reviewed independently'))
        result = self.request('POST', f'/api/settlement-finance/funding-adjustments/{funding["id"]}/verify', actor='ops', json=dict(
            request_id='manual-cover', expected_version=2, receipt_reference=receipt['receipt_reference']))
        self.assertEqual(result['status'], 'covered')
        self.assertFalse(result['bank_receipt_verified'])
        with self.m.SessionLocal() as db:
            payment = db.scalar(select(self.m.Payment).where(self.m.Payment.order_id == self.order_id))
            self.assertEqual(payment.amount, 100)
            self.assertEqual(db.get(self.m.settlement_finance['Baseline'], sid).revenue, 110)

    def test_actual_completed_buyer_refund_captures_final_basis_plus_internal_absorption(self):
        sid, _ = self.funded_source()
        refund = self.complete()
        m = self.m
        with m.SessionLocal() as db:
            completion = db.get(m.refund_execution['Completion'], refund['id'])
            frozen = completion.economic_plan_json, completion.proof_hash, completion.funding_hash
        nid = self.negative(refund)
        detail = self.request('GET', f'/api/settlements/{nid}/detail', actor='finance')
        basis = detail['refund_adjustment']['financial_basis']
        self.assertEqual(basis['status'], 'ready', basis)
        self.assertEqual(Decimal(basis['gross_amount']), -22)
        self.assertEqual(Decimal(basis['buyer_cash_amount']), 20)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        detail = self.request('GET', f'/api/settlements/{nid}/detail', actor='finance')
        self.assertEqual(Decimal(detail['finance']['funded_refund']['absorption_reversal']), 2)
        self.assertEqual(detail['finance']['funded_refund']['external_paid'], '0.00')
        with m.SessionLocal() as db:
            h = m.settlement_finance
            b = db.get(h['Baseline'], nid)
            self.assertEqual((b.revenue, b.cost, b.profit, b.reference_settlement_id), (Decimal(-22), Decimal(-8), Decimal(-14), sid))
            self.assertEqual(db.get(m.Refund, refund['id']).amount, 20)
            payment = db.scalar(select(m.Payment).where(m.Payment.order_id == self.order_id))
            self.assertEqual(payment.amount, 100)
            self.assertEqual(db.get(m.Settlement, nid).refund_amount, -20)
            completion = db.get(m.refund_execution['Completion'], refund['id'])
            self.assertEqual(frozen, (completion.economic_plan_json, completion.proof_hash, completion.funding_hash))
            positions = db.scalars(select(h['Position']).where(h['Position'].settlement_id == nid)).all()
            self.assertFalse(any(p.blocked_reason for p in positions))
            self.assertEqual(sum(p.amount for p in positions), 22)
            self.assertEqual(db.scalar(select(func.count()).select_from(m.settlement_funded_refund['Posting'])), 1)

    def test_unverified_supplement_does_not_block_buyer_but_blocks_financial_lock(self):
        self.funded_source(cover=False)
        refund = self.complete()
        nid = self.negative(refund)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance', expected=409)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Refund, refund['id']).status, 'completed')
            self.assertIsNone(db.get(self.m.settlement_finance['Baseline'], nid))


if __name__ == '__main__':
    unittest.main()
