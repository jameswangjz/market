"""Actual HTTP and FILE completion for funded combinations; no bank execution."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest

from sqlalchemy import event, func, select

import test_settlement_finance_final_wiring as fixture


class FundedCombinationHTTPTests(unittest.TestCase):
    setUpClass = classmethod(fixture.FinalFinanceWiringTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.FinalFinanceWiringTests.tearDownClass.__func__)
    setUp = fixture.FinalFinanceWiringTests.setUp
    tearDown = fixture.FinalFinanceWiringTests.tearDown
    request = fixture.FinalFinanceWiringTests.request
    create_order = fixture.FinalFinanceWiringTests.create_order
    create = fixture.FinalFinanceWiringTests.create
    manual = fixture.FinalFinanceWiringTests.manual
    review = fixture.FinalFinanceWiringTests.review
    execute = fixture.FinalFinanceWiringTests.execute
    approved = fixture.FinalFinanceWiringTests.approved
    step = fixture.FinalFinanceWiringTests.step
    batch = fixture.FinalFinanceWiringTests.batch
    complete = fixture.FinalFinanceWiringTests.complete
    negative = fixture.FinalFinanceWiringTests.negative
    funded_source = fixture.FinalFinanceWiringTests.funded_source

    def external_source(self):
        sid, funding = self.funded_source(kind='external_receipt', cover=False)
        receipt = self.request('POST', '/api/settlement-finance/manual-funding-evidence', actor='finance', json=dict(
            request_id='receipt', funding_request_id=funding['id'], amount='10.00', currency='CNY',
            reference='FUNDED-COMBINATION-RECEIPT', paid_on=datetime.now(timezone.utc).date().isoformat(),
            evidence_refs=['receipt:combined'], note='Actual manual receipt, independently reviewed'))
        self.request('POST', f'/api/settlement-finance/manual-funding-evidence/{receipt["id"]}/review', actor='ops', json=dict(
            request_id='receipt-review', expected_version=1, decision='verified', note='Independent review'))
        self.request('POST', f'/api/settlement-finance/funding-adjustments/{funding["id"]}/verify', actor='ops', json=dict(
            request_id='receipt-cover', expected_version=2, receipt_reference=receipt['receipt_reference']))
        return sid

    def approved_cost(self, refund):
        m = self.m
        with m.SessionLocal() as db:
            raw = m.settlement_funded_refund['raw_funded_basis'](db, db.get(m.Refund, refund['id']))
            self.assertEqual(raw['status'], 'ready', raw)
        bearer = self.request('POST', '/api/settlement-parties', actor='finance', json=dict(
            request_id='bearer', name='Provider retained work', kind='provider', enterprise_id='provider_tenant', currency='CNY'))
        request = dict(request_id='cost', refund_id=refund['id'], retained_cost='5.00', basis_kind='funded',
            reason='Nonreturnable source work', bearer_participant_id=bearer['id'],
            source_allocations=[dict(segment_id=r['segment_id'], baseline_id=r['reference_settlement_id'],
                retained_cost='5.00', evidence_refs=['invoice:source']) for r in raw['references']])
        exception = self.request('POST', '/api/settlement-cost-exceptions', actor='finance', json=request)
        decision = dict(request_id='cost-review', expected_version=1, decision='approve', reason='Opposing party approval')
        self.request('POST', f'/api/settlement-cost-exceptions/{exception["id"]}/decision', actor='finance', expected=403, json=decision)
        self.request('POST', f'/api/settlement-cost-exceptions/{exception["id"]}/decision', actor='provider', json=decision)
        with m.SessionLocal() as db:
            models = [m.settlement_finance['Baseline'], m.settlement_finance['Position'],
                      m.settlement_funded_refund['Plan'], m.settlement_funded_cost_exception['Application']]
            before = [db.scalar(select(func.count()).select_from(model)) for model in models]
            again = m.settlement_funded_refund['raw_funded_basis'](db, db.get(m.Refund, refund['id']))
            self.assertEqual(again, raw)
            self.assertNotIn('funded_cost_exception_application_hash', again)
            self.assertEqual(before, [db.scalar(select(func.count()).select_from(model)) for model in models])

    def completion_proof(self, refund):
        with self.m.SessionLocal() as db:
            c = db.get(self.m.refund_execution['Completion'], refund['id'])
            e = db.get(self.m.refund_execution['Execution'], c.execution_id)
            return c.economic_plan_json, c.plan_hash, c.proof_hash, c.funding_hash, e.resource_plan_json, e.proof_json, e.funding_json

    def complete_amount(self, amount):
        row = self.create(amount, idempotency_key='buyer-refund-' + amount)
        row = self.manual(row, refundable_limit='100.00', idempotency_key='assess-' + row['id'])
        row = self.review(row, decision='approve', idempotency_key='review-' + row['id'])
        self.execute(row, key='execute-' + row['id'])
        for _ in range(5):
            self.step()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Refund, row['id']).status, 'completed')
        return row

    def cumulative(self, *, external=False, cost=False):
        source = self.external_source() if external else self.funded_source()[0]
        first = self.complete_amount('20.00')
        if cost:
            self.approved_cost(first)
        nid = self.negative(first)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        second = self.complete_amount('80.00')
        proof = self.completion_proof(second)
        nid2 = self.negative(second)
        self.request('POST', f'/api/settlements/{nid2}/lock', actor='finance')
        m, h = self.m, self.m.settlement_finance
        with m.SessionLocal() as db:
            records = h['source_reversals'](db, source)
            self.assertEqual(len(records), 2)
            self.assertEqual(sum(Decimal(r['gross_amount']) for r in records), -110)
            self.assertEqual(sum(Decimal(r['cost_amount']) for r in records), -35 if cost else -40)
            self.assertEqual(sum(Decimal(r['profit_amount']) for r in records), -70)
            positions = db.scalars(select(h['Position']).where(h['Position'].settlement_id.in_([nid, nid2]))).all()
            self.assertEqual(sum(p.amount for p in positions if p.kind == 'funding_return'), 10 if external else 0)
            self.assertEqual(sum(p.amount for p in positions if p.kind == 'loss'), 5 if cost else 0)
            snapshots = [json.loads(db.get(h['Baseline'], sid).snapshot_json) for sid in (nid, nid2)]
            self.assertEqual(sum(Decimal(s['absorption_reversal']) for s in snapshots), 0 if external else 10)
            self.assertEqual(db.get(m.Refund, second['id']).amount, 80)
            self.assertEqual(h['capture'](db, db.get(m.Settlement, nid2)).settlement_id, nid2)
        self.assertEqual(self.completion_proof(second), proof)
        self.create('0.01', idempotency_key='buyer-over-cap', expected=409)

    def test_external_partial_then_full_refund_has_one_cumulative_payer_return_budget(self):
        self.cumulative(external=True)

    def test_external_cost_partial_then_full_excludes_independent_loss_from_profit_budget(self):
        self.cumulative(external=True, cost=True)

    def test_absorbed_cost_partial_then_full_preserves_original_profit_budget(self):
        self.cumulative(cost=True)

    def assert_capture(self, refund, nid, source, *, external=False, cost=False):
        m, h = self.m, self.m.settlement_finance
        with m.SessionLocal() as db:
            baseline = db.get(h['Baseline'], nid)
            self.assertEqual((baseline.revenue, baseline.cost, baseline.profit),
                             (Decimal('-22'), Decimal('-3' if cost else '-8'), Decimal('-19' if cost else '-14')))
            self.assertEqual(baseline.reference_settlement_id, source)
            self.assertEqual(db.get(m.Refund, refund['id']).amount, 20)
            self.assertEqual(db.get(m.Settlement, nid).refund_amount, -20)
            rows = db.scalars(select(h['Position']).where(h['Position'].settlement_id == nid)).all()
            normal = [p for p in rows if p.kind != 'funding_return']
            returns = [p for p in rows if p.kind == 'funding_return']
            self.assertEqual(sum(p.amount for p in normal), 22)
            self.assertEqual(sum(p.amount for p in normal if p.kind == 'loss'), 5 if cost else 0)
            self.assertEqual(sum(p.amount for p in returns), 2 if external else 0)
            self.assertTrue(all(p.direction == 'payable' and p.participant_id == 'provider_tenant' for p in returns))
            self.assertTrue(all(db.get(h['RefundPositionSource'], p.id).source_settlement_id == source for p in rows))
            records = h['source_reversals'](db, source)
            self.assertEqual(len(records), 1)
            self.assertFalse(any(e['kind'] in ('funding_return', 'loss') for e in records[0]['entries']))
            self.assertEqual(Decimal(records[0]['profit_amount']), -14)
            counts = tuple(db.scalar(select(func.count()).select_from(h[k])) for k in ('Baseline', 'Position', 'RefundPositionSource'))
            self.assertEqual(h['capture'](db, db.get(m.Settlement, nid)).settlement_id, nid)
            self.assertEqual(counts, tuple(db.scalar(select(func.count()).select_from(h[k])) for k in ('Baseline', 'Position', 'RefundPositionSource')))

    def test_actual_external_refund_posts_unpaid_exact_payer_return_and_preserves_completion(self):
        source = self.external_source()
        refund = self.complete()
        proof = self.completion_proof(refund)
        nid = self.negative(refund)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        self.assert_capture(refund, nid, source, external=True)
        self.assertEqual(self.completion_proof(refund), proof)

    def test_actual_absorbed_refund_cost_exception_posts_independent_loss(self):
        source, _ = self.funded_source()
        refund = self.complete()
        proof = self.completion_proof(refund)
        self.approved_cost(refund)
        nid = self.negative(refund)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        self.assert_capture(refund, nid, source, cost=True)
        self.assertEqual(self.completion_proof(refund), proof)

    def test_actual_external_refund_cost_exception_preserves_payer_return(self):
        source = self.external_source()
        refund = self.complete()
        proof = self.completion_proof(refund)
        self.approved_cost(refund)
        nid = self.negative(refund)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        self.assert_capture(refund, nid, source, external=True, cost=True)
        self.assertEqual(self.completion_proof(refund), proof)

    def test_external_return_requires_confirmed_batch_and_independent_manual_payment(self):
        self.external_source()
        refund = self.complete()
        nid = self.negative(refund)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        m, h = self.m, self.m.settlement_finance
        with m.SessionLocal() as db:
            payable = db.scalar(select(h['Position']).where(h['Position'].settlement_id == nid,
                                                          h['Position'].kind == 'funding_return'))
            pid, amount = payable.id, str(payable.amount)
            batch = m.SettlementBatch(batch_no='FUNDED-RETURN-BATCH', status='pending_confirm', idempotency_key='return-batch')
            db.add(batch)
            db.flush()
            bid = batch.id
            db.add(m.SettlementLine(batch_id=bid, settlement_id=nid, participant_type='provider', amount=-14))
            db.commit()
        body = dict(request_id='return-payment', kind='payout', mode='manual', reference='PAYER-RETURN-RECEIPT',
                    allocations=[dict(position_id=pid, amount=amount)], note='Independent manual return')
        self.request('POST', '/api/settlement-finance/transactions', actor='finance', expected=409, json=body)
        self.request('POST', f'/api/settlement-batches/{bid}/confirm', actor='finance', json={})
        with m.SessionLocal() as db:
            funds = h['batch_status'](db, bid)
            self.assertFalse(funds['positive_payment_complete'])
            self.assertFalse(funds['funds_settled'])
            self.assertEqual(Decimal(funds['payable_balance']), 2)
        self.request('POST', f'/api/settlement-batches/{bid}/pay', actor='finance', expected=409, json={})
        self.request('POST', '/api/settlement-finance/transactions', actor='finance', expected=409,
                     json=dict(body, request_id='simulated-return', mode='simulated'))
        # A separate financial source with the same payer is not this credit's
        # counterparty debt. Seed only that unrelated ledger for the API guard.
        with m.SessionLocal() as db:
            original = db.get(h['Baseline'], nid)
            db.add(m.Settlement(id='unrelated-source', settlement_no='SET-UNRELATED-SOURCE',
                               order_id=self.other, status='locked', gross_amount=-1))
            db.flush()
            db.add(h['Baseline'](settlement_id='unrelated-source', order_id=self.other,
                provider_enterprise_id='provider_tenant', reference_settlement_id='', currency='CNY',
                revenue=-1, cost=-1, profit=0, snapshot_json=original.snapshot_json,
                snapshot_hash=original.snapshot_hash, actor='isolated-scope-fixture'))
            db.flush()
            debt = h['Position'](settlement_id='unrelated-source', order_id=self.other, currency='CNY',
                kind='cost', share_kind='', participant_type='enterprise', participant_id='provider_tenant',
                participant_name='Provider', amount=1, direction='receivable', blocked_reason='')
            db.add(debt)
            db.flush()
            debt_id = debt.id
            other_batch = m.SettlementBatch(batch_no='UNRELATED-BATCH', status='confirmed', idempotency_key='unrelated-batch')
            db.add(other_batch)
            db.flush()
            db.add(m.SettlementLine(batch_id=other_batch.id, settlement_id='unrelated-source', participant_type='provider', amount=-1))
            db.commit()
        self.request('POST', '/api/settlement-finance/transactions', actor='finance', expected=409, json=dict(
            request_id='wrong-source-offset', kind='offset', mode='manual', reference='WRONG-SOURCE',
            allocations=[dict(position_id=debt_id, amount='1.00')], counter_position_id=pid))
        tx = self.request('POST', '/api/settlement-finance/transactions', actor='finance', json=body)
        review = dict(request_id='return-review', expected_version=1, note='Verified payer return evidence')
        self.request('POST', f'/api/settlement-finance/transactions/{tx["id"]}/confirm', actor='finance', expected=403, json=review)
        self.request('POST', f'/api/settlement-finance/transactions/{tx["id"]}/confirm', actor='ops', json=review)
        with m.SessionLocal() as db:
            self.assertEqual(h['balance'](db, db.get(h['Position'], pid)), 0)
            self.assertEqual(db.get(m.Settlement, nid).status, 'locked')
            self.assertEqual(db.get(m.Refund, refund['id']).amount, 20)

    def test_funded_capture_position_failure_rolls_back_plan_application_and_posting(self):
        self.funded_source()
        refund = self.complete()
        self.approved_cost(refund)
        nid = self.negative(refund)
        Position = self.m.settlement_finance['Position']

        def fail(_mapper, _connection, position):
            if position.settlement_id == nid:
                raise RuntimeError('injected financial position failure')

        event.listen(Position, 'before_insert', fail)
        try:
            self.request('POST', f'/api/settlements/{nid}/lock', actor='finance', expected=500)
        finally:
            event.remove(Position, 'before_insert', fail)
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.get(self.m.settlement_finance['Baseline'], nid))
            self.assertIsNone(db.get(self.m.settlement_funded_refund['Plan'], refund['id']))
            self.assertIsNone(db.get(self.m.settlement_funded_refund['Posting'], refund['id']))
            self.assertIsNone(db.get(self.m.settlement_funded_cost_exception['Posting'], refund['id']))
            self.assertEqual(db.get(self.m.Refund, refund['id']).status, 'completed')


if __name__ == '__main__':
    unittest.main()
