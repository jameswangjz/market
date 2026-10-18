"""Production HTTP routes, disposable SQLite, real FILE refund worker; no bank I/O."""
from decimal import Decimal
import json
import unittest

from sqlalchemy import select

import test_refund_file_resources as fixture
import test_version_change_quotes as quotes_fixture


class SettlementFinanceIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(fixture.FileWorkflowTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.FileWorkflowTests.tearDownClass.__func__)
    tearDown = fixture.FileWorkflowTests.tearDown
    request = fixture.FileWorkflowTests.request
    create_order = fixture.FileWorkflowTests.create_order
    create = fixture.FileWorkflowTests.create
    manual = fixture.FileWorkflowTests.manual
    review = fixture.FileWorkflowTests.review
    execute = fixture.FileWorkflowTests.execute
    approved = fixture.FileWorkflowTests.approved
    step = fixture.FileWorkflowTests.step
    setUp = fixture.FileWorkflowTests.setUp

    def batch(self, key='original', **values):
        return self.request('POST', '/api/settlement-batches', actor='finance', json=dict(
            order_ids=[self.order_id], idempotency_key=key, **values))

    def original(self, adjusted=False):
        batch = self.batch()
        with self.m.SessionLocal() as db:
            sid = db.scalar(select(self.m.Settlement.id).where(self.m.Settlement.order_id == self.order_id,
                            self.m.Settlement.is_refund.is_(False)))
        if adjusted:
            proposal = self.request('POST', f'/api/settlements/{sid}/proposals', actor='finance', json=dict(
                gross_amount=100, cost_amount=40, profit_amount=60, platform_rate=20,
                provider_rate=80, service_rate=0, expert_rate=0, channel_rate=0, reason='Documented final agreement'))
            self.request('POST', f'/api/settlement-proposals/{proposal["id"]}/decision', actor='provider',
                         json=dict(decision='approve', comment='Agreed'))
        self.request('POST', f'/api/settlements/{sid}/lock', actor='finance')
        return batch, sid

    def complete(self):
        refund = self.approved()
        self.execute(refund)
        for _ in range(5):
            self.step()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Refund, refund['id']).status, 'completed')
        return refund

    def negative(self, refund):
        with self.m.SessionLocal() as db:
            settlement = self.m.refund_settlement['negative_settlement'](db, db.get(self.m.Refund, refund['id']))
            db.commit()
            return settlement.id

    def payout(self, sid):
        with self.m.SessionLocal() as db:
            positions = db.scalars(select(self.m.settlement_finance['Position']).where(
                self.m.settlement_finance['Position'].settlement_id == sid,
                self.m.settlement_finance['Position'].direction == 'payable')).all()
            groups = {}
            for p in positions:
                groups.setdefault((p.participant_type, p.participant_id), []).append(dict(position_id=p.id, amount=str(p.amount)))
        for i, rows in enumerate(groups.values()):
            tx = self.request('POST', '/api/settlement-finance/transactions', actor='finance', json=dict(
                request_id=f'payout-{sid}-{i}', kind='payout', mode='simulated', reference='TEST-RECEIPT', allocations=rows))
            self.request('POST', f'/api/settlement-finance/transactions/{tx["id"]}/confirm', actor='ops',
                         json=dict(request_id=f'confirm-{sid}-{i}', expected_version=1, note='Independent check'))

    def test_final_adjustment_drives_refund_without_changing_execution_evidence(self):
        batch, sid = self.original(adjusted=True)
        refund = self.complete()
        with self.m.SessionLocal() as db:
            completion = db.get(self.m.refund_execution['Completion'], refund['id'])
            before = (completion.economic_plan_json, completion.proof_hash, completion.funding_hash)
        nid = self.negative(refund)
        detail = self.request('GET', f'/api/settlements/{nid}/detail', actor='finance')
        self.assertEqual(detail['amounts']['cost_amount'], -8)
        self.assertEqual(detail['amounts']['profit_amount'], -12)
        self.assertEqual(detail['refund_adjustment']['financial_basis']['reference_settlement_id'], sid)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        detail = self.request('GET', f'/api/settlements/{nid}/detail', actor='provider')
        self.assertEqual(detail['finance']['reference_settlement_id'], sid)
        self.assertEqual(Decimal(detail['finance']['shares']['platform_fee']), Decimal('-2.40'))
        self.assertEqual(Decimal(detail['finance']['shares']['provider_share']), Decimal('-9.60'))
        with self.m.SessionLocal() as db:
            completion = db.get(self.m.refund_execution['Completion'], refund['id'])
            self.assertEqual(before, (completion.economic_plan_json, completion.proof_hash, completion.funding_hash))
            self.assertEqual(db.get(self.m.Refund, refund['id']).amount, 20)
            original = db.get(self.m.settlement_finance['Baseline'], sid)
            self.assertEqual(original.cost, 40)

    def test_missing_baseline_allows_buyer_completion_blocks_lock_then_controlled_refresh(self):
        refund = self.complete()
        nid = self.negative(refund)
        detail = self.request('GET', f'/api/settlements/{nid}/detail', actor='finance')
        self.assertEqual(detail['refund_adjustment']['financial_basis']['status'], 'waiting')
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance', expected=409)
        _, sid = self.original(adjusted=True)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        with self.m.SessionLocal() as db:
            row = db.get(self.m.Settlement, nid)
            self.assertEqual(row.reference_settlement_id, sid)
            self.assertEqual(row.cost_amount, -8)
            rows = db.scalars(select(self.m.refund_settlement['FinancialAssessment']).where(
                self.m.refund_settlement['FinancialAssessment'].settlement_id == nid)).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual(json.loads(rows[0].values_json)['status'], 'waiting')
            self.assertEqual(json.loads(rows[1].values_json)['status'], 'ready')

    def test_batch_pay_requires_vouchers_and_negative_never_becomes_simulated_recovery(self):
        batch, sid = self.original(adjusted=True)
        self.request('POST', f'/api/settlement-batches/{batch["id"]}/confirm', actor='finance', json={})
        self.request('POST', f'/api/settlement-batches/{batch["id"]}/pay', actor='finance', expected=409, json={})
        self.payout(sid)
        paid = self.request('POST', f'/api/settlement-batches/{batch["id"]}/pay', actor='finance', json={})
        self.assertTrue(paid['funds']['positive_payment_complete'])
        refund = self.complete()
        nid = self.negative(refund)
        with self.m.SessionLocal() as db:
            b = self.m.SettlementBatch(batch_no='NEGATIVE-BATCH', status='generated', idempotency_key='negative')
            db.add(b)
            db.flush()
            bid = b.id
            db.add(self.m.SettlementLine(batch_id=bid, settlement_id=nid, participant_type='provider', amount=-12))
            db.commit()
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        self.request('POST', f'/api/settlement-batches/{bid}/confirm', actor='finance', json={})
        paid = self.request('POST', f'/api/settlement-batches/{bid}/pay', actor='finance', json={})
        self.assertTrue(paid['funds']['positive_payment_complete'])
        self.assertFalse(paid['funds']['funds_settled'])
        self.assertEqual(paid['funds']['receivable_balance'], '17.60')
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Settlement, nid).status, 'locked')
            lines = db.scalars(select(self.m.SettlementLine).where(self.m.SettlementLine.batch_id == bid)).all()
            self.assertTrue(all(line.status == 'locked' and not line.payment_no for line in lines))
            self.assertEqual(self.m.refund_settlement['ledger_status'](db, refund['id']), 'recovery_pending')
        funds = self.request('GET', '/api/settlement-batches', actor='finance')['items']
        self.assertFalse(next(b for b in funds if b['id'] == bid)['funds']['funds_settled'])

    def test_unassigned_actual_subject_blocks_payment_despite_legacy_provider_placeholder(self):
        batch, sid = self.original()
        self.request('POST', f'/api/settlement-batches/{batch["id"]}/confirm', actor='finance', json={})
        positions = self.request('GET', '/api/settlement-finance/positions', actor='finance')['items']
        expert = next(p for p in positions if p['share_kind'] == 'expert_fee')
        self.assertEqual(expert['participant_id'], '')
        self.assertEqual(expert['status'], 'blocked')
        self.request('POST', '/api/settlement-finance/transactions', actor='finance', expected=409, json=dict(
            request_id='wrong-expert', kind='payout', mode='simulated', reference='TEST',
            allocations=[dict(position_id=expert['id'], amount=expert['amount'])]))
        self.request('POST', f'/api/settlement-batches/{batch["id"]}/pay', actor='finance', expected=409, json={})

    def test_new_order_freezes_configuration_history_is_not_backfilled(self):
        with self.m.SessionLocal() as db:
            old = db.get(self.m.Order, self.order_id).economic_snapshot_json
        party = self.request('POST', '/api/settlement-parties', actor='ops', json=dict(request_id='expert',
            name='Actual Expert', kind='expert', currency='CNY', user_id='provider'))
        first = self.request('POST', '/api/settlement-parties/products/product/allocations', actor='ops', json=dict(
            request_id='mapping', allocations=[dict(participant_id=party['id'], participant_type='expert', weight='100')]))
        oid = self.create_order()
        self.request('POST', '/api/settlement-parties/products/product/allocations', actor='ops', json=dict(
            request_id='mapping2', allocations=[]))
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Order, self.order_id).economic_snapshot_json, old)
            order = db.get(self.m.Order, oid)
            self.assertEqual(json.loads(order.economic_snapshot_json)['settlement_parties']['id'], first['id'])
            result = self.m.settlement_parties['allocations'](db, order, 'expert', Decimal('4.50'))
            self.assertEqual(result[0]['participant_id'], party['id'])

    def test_final_baseline_cannot_be_edited_even_if_legacy_status_is_corrupt(self):
        _, sid = self.original(adjusted=True)
        with self.m.SessionLocal() as db:
            db.get(self.m.Settlement, sid).status = 'pending'
            for line in db.scalars(select(self.m.SettlementLine).where(self.m.SettlementLine.settlement_id == sid)):
                line.status = 'pending'
            db.commit()
        self.request('POST', f'/api/settlements/generate/{self.order_id}', actor='finance', expected=409, json={})

    def test_approved_cost_exception_stays_unsupported_until_explicit_loss_plan(self):
        _, sid = self.original(adjusted=True)
        refund = self.complete()
        bearer = self.request('POST', '/api/settlement-parties', actor='ops', json=dict(
            request_id='loss-bearer', name='Provider bearer', kind='provider', enterprise_id='provider_tenant', currency='CNY'))
        e = self.request('POST', '/api/settlement-cost-exceptions', actor='ops', json=dict(
            request_id='retained', refund_id=refund['id'], retained_cost='5.00', reason='Nonreturnable work',
            evidence_refs=['invoice:1'], bearer_participant_id=bearer['id']))
        self.request('POST', f'/api/settlement-cost-exceptions/{e["id"]}/decision', actor='provider', json=dict(
            request_id='retained-review', expected_version=1, decision='approve', reason='Agreed'))
        nid = self.negative(refund)
        detail = self.request('GET', f'/api/settlements/{nid}/detail', actor='finance')
        basis = detail['refund_adjustment']['financial_basis']
        self.assertEqual(basis['status'], 'unsupported')
        self.assertEqual(basis['reason'], 'approved_cost_exception_requires_explicit_plan')
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance', expected=409)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Refund, refund['id']).amount, 20)
            self.assertIsNone(db.get(self.m.settlement_finance['Baseline'], nid))

    def test_legacy_report_and_export_cover_more_than_2000_records(self):
        with self.m.SessionLocal() as db:
            db.add_all(self.m.Settlement(settlement_no=f'ALL-{i:04}', order_id=self.order_id,
                       gross_amount=Decimal('.01'), net_amount=Decimal('.01'), profit_amount=Decimal('.01'),
                       platform_fee=Decimal('.01'), cost_amount=0) for i in range(2005))
            db.commit()
        report = self.request('GET', '/api/settlement-reports', actor='finance')
        self.assertEqual(report['summary']['count'], 2005)
        self.assertEqual(len(report['items']), 2005)
        response = self.client.get('/api/settlement-reports/export',
            headers={'Authorization': 'Bearer ' + self.tokens['finance']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.text.splitlines()), 2006)


class TargetSnapshotTests(unittest.TestCase):
    setUpClass = classmethod(quotes_fixture.VersionChangeQuoteTests.setUpClass.__func__)
    tearDownClass = classmethod(quotes_fixture.VersionChangeQuoteTests.tearDownClass.__func__)
    setUp = quotes_fixture.VersionChangeQuoteTests.setUp
    tearDown = quotes_fixture.VersionChangeQuoteTests.tearDown
    request = quotes_fixture.VersionChangeQuoteTests.request
    create = quotes_fixture.VersionChangeQuoteTests.create
    deliver = quotes_fixture.VersionChangeQuoteTests.deliver
    prepare = quotes_fixture.VersionChangeQuoteTests.prepare

    def test_target_configuration_is_in_fingerprint_and_term_snapshot_only(self):
        oid, _ = self.prepare(future=True)
        with self.m.SessionLocal() as db:
            original = db.get(self.m.Order, oid).economic_snapshot_json
        party = self.request('POST', '/api/settlement-parties', actor='ops', json=dict(
            request_id='vc-party', name='Target Expert', enterprise_id='provider_tenant', kind='expert', currency='CNY'))
        v1 = self.request('POST', '/api/settlement-parties/products/product/allocations', actor='ops', json=dict(
            request_id='vc-map-1', allocations=[dict(participant_id=party['id'], participant_type='expert', weight=100)]))
        with self.m.SessionLocal() as db:
            user = db.get(self.m.User, 'buyer')
            first = self.m.version_change_quotes['prepare'](db, user, oid, 'v2', False, self.m.now())
            self.assertTrue(all(s['economic']['settlement_parties']['id'] == v1['id']
                                for s in first['snapshots_by_term'].values()))
        self.request('POST', '/api/settlement-parties/products/product/allocations', actor='ops', json=dict(
            request_id='vc-map-2', allocations=[]))
        with self.m.SessionLocal() as db:
            second = self.m.version_change_quotes['prepare'](db, db.get(self.m.User, 'buyer'), oid, 'v2', False, self.m.now())
            self.assertNotEqual(first['digest'], second['digest'])
            self.assertEqual(db.get(self.m.Order, oid).economic_snapshot_json, original)
            self.assertTrue(all(s['economic']['settlement_parties']['version'] == 2
                                for s in second['snapshots_by_term'].values()))


if __name__ == '__main__':
    unittest.main()
