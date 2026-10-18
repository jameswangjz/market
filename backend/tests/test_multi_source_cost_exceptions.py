"""Real HTTP per-source exception approval/capture; seeded completed Saga facts."""
import copy
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from sqlalchemy import event, func, select

from app.refund_execution import canonical, digest
import test_cross_version_multi_source_refund as fixture


class MultiSourceCostTests(unittest.TestCase):
    setUpClass = classmethod(fixture.MultiSourceRefundTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.MultiSourceRefundTests.tearDownClass.__func__)
    setUp = fixture.MultiSourceRefundTests.setUp
    tearDown = fixture.MultiSourceRefundTests.tearDown
    request = fixture.MultiSourceRefundTests.request
    prepare = fixture.MultiSourceRefundTests.prepare
    create = fixture.MultiSourceRefundTests.create
    deliver = fixture.MultiSourceRefundTests.deliver
    scenario = fixture.MultiSourceRefundTests.scenario
    settlement = fixture.MultiSourceRefundTests.settlement
    two_changes = fixture.MultiSourceRefundTests.two_changes
    plans = fixture.MultiSourceRefundTests.plans
    seed_completed = fixture.MultiSourceRefundTests.seed_completed

    def completed(self, *, negative=True):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            refund = self.seed_completed(db, self.plans(db))
            raw = m.refund_settlement['raw_multi_basis'](db, refund)
            self.assertEqual(raw['status'], 'ready', raw)
            self.ordinary = raw
            if negative:
                self.nid = m.refund_settlement['negative_settlement'](db, refund).id
            completion = db.get(m.refund_execution['Completion'], refund.id)
            execution = db.get(m.refund_execution['Execution'], completion.execution_id)
            self.frozen = (completion.economic_plan_json, completion.plan_hash, completion.proof_hash,
                completion.funding_hash, execution.resource_plan_json, execution.resource_plan_hash,
                execution.proof_json, execution.funding_json, refund.amount)
            db.commit()
        bearer = self.request('POST', '/api/settlement-parties', actor='finance', json=dict(
            request_id='multi-cost-bearer', name='Actual provider', kind='provider', enterprise_id='provider_tenant', currency='CNY'))
        self.body = dict(request_id='multi-cost-request', refund_id='multi', retained_cost='3.00',
            reason='Retained work separately documented per economic segment', bearer_participant_id=bearer['id'],
            source_allocations=[dict(segment_id=r['segment_id'], baseline_id=r['reference_settlement_id'],
                retained_cost=str(i + 1), evidence_refs=['invoice:' + r['segment_id']]) for i, r in enumerate(raw['references'])])

    def approved(self):
        result = self.request('POST', '/api/settlement-cost-exceptions', actor='finance', json=self.body)
        self.exception_id = result['id']
        decision = dict(request_id='multi-cost-review', expected_version=1, decision='approve', reason='Agreed both sources')
        self.request('POST', f'/api/settlement-cost-exceptions/{result["id"]}/decision', actor='finance', expected=403, json=decision)
        approved = self.request('POST', f'/api/settlement-cost-exceptions/{result["id"]}/decision', actor='provider', json=decision)
        self.assertEqual(approved['status'], 'approved')
        return approved

    def assert_buyer_proof(self, db):
        m = self.m
        completion = db.get(m.refund_execution['Completion'], 'multi')
        execution = db.get(m.refund_execution['Execution'], completion.execution_id)
        refund = db.get(m.Refund, 'multi')
        self.assertEqual(self.frozen, (completion.economic_plan_json, completion.plan_hash, completion.proof_hash,
            completion.funding_hash, execution.resource_plan_json, execution.resource_plan_hash,
            execution.proof_json, execution.funding_json, refund.amount))

    def test_http_full_approval_lock_posts_two_independent_losses_and_preserves_cash(self):
        self.completed()
        self.approved()
        self.request('POST', f'/api/settlements/{self.nid}/lock', actor='finance')
        self.assert_captured()

    def assert_captured(self):
        m = self.m
        with m.SessionLocal() as db:
            h = m.settlement_finance
            basis = db.get(h['Baseline'], self.nid)
            self.assertEqual(basis.revenue, -20)
            self.assertEqual(basis.cost, Decimal(self.ordinary['cost_amount']) + 3)
            self.assertEqual(basis.profit, Decimal(self.ordinary['profit_amount']) - 3)
            sources = db.scalars(select(h['RefundSource']).where(h['RefundSource'].settlement_id == self.nid)).all()
            self.assertEqual(len(sources), 2)
            for source in sources:
                ref = json.loads(source.evidence_json)
                retained = Decimal(ref['cost_exception']['retained_cost'])
                ordinary = ref['default_basis']
                self.assertEqual(Decimal(ref['cost_amount']), Decimal(ordinary['cost_amount']) + retained)
                entries = ref['entries']
                self.assertEqual(sum(Decimal(e['amount']) for e in entries), -Decimal(ref['gross_amount']))
                self.assertEqual(sum(Decimal(e['amount']) for e in entries if e['kind'] == 'loss'), retained)
                self.assertEqual(sum(Decimal(e['amount']) for e in entries if e['kind'] == 'profit'), -Decimal(ordinary['profit_amount']))
                records = h['source_reversals'](db, source.source_settlement_id)
                recorded = next(r for r in records if r.get('segment_id') == source.segment_id)
                self.assertEqual(recorded['shares'], ordinary['shares'])
                self.assertEqual(recorded['profit_amount'], ordinary['profit_amount'])
                self.assertFalse(any(e['kind'] == 'loss' for e in recorded['entries']))
            cost = m.settlement_multi_cost_exception
            app = db.get(cost['Application'], 'multi')
            self.assertEqual(app.plan_hash, digest(json.loads(app.plan_json)))
            self.assertEqual(db.scalar(select(func.count()).select_from(cost['Segment'])), 2)
            self.assertEqual(db.get(cost['Posting'], 'multi').settlement_id, self.nid)
            self.assert_buyer_proof(db)
            self.assertEqual(h['capture'](db, db.get(m.Settlement, self.nid)).settlement_id, self.nid)
            app.plan_hash = 'tampered'
            with self.assertRaises(ValueError):
                db.flush()

    def test_raw_basis_never_applies_approved_exception_or_writes(self):
        self.completed()
        self.approved()
        m = self.m
        with m.SessionLocal() as db:
            cost = m.settlement_multi_cost_exception
            refund = db.get(m.Refund, 'multi')
            with patch.dict(m.settlement_parties, cost_exception_multi_basis=lambda *_: self.fail('raw basis called approval hook')):
                result = m.refund_settlement['raw_multi_basis'](db, refund, apply_exception=False)
            self.assertEqual(result, self.ordinary)
            self.assertIsNone(db.get(cost['Application'], refund.id))
            self.assertEqual(db.scalar(select(func.count()).select_from(cost['Segment'])), 0)

    def test_approval_before_negative_generation_captures_without_basis_refresh(self):
        self.completed(negative=False)
        self.approved()
        m = self.m
        with m.SessionLocal() as db:
            self.nid = m.refund_settlement['negative_settlement'](db, db.get(m.Refund, 'multi')).id
            db.commit()
        self.request('POST', f'/api/settlements/{self.nid}/lock', actor='finance')
        self.assert_captured()

    def test_incomplete_second_approval_source_waits_without_partial_application(self):
        self.completed()
        self.approved()
        m = self.m
        with m.SessionLocal() as db:
            refund = db.get(m.Refund, 'multi')
            proof = m.settlement_parties['cost_exception_multi_basis'](db, refund, self.ordinary['references'])
            proof['source_allocations'][1]['evidence_refs'] = []
            proof['approval_evidence_hash'] = digest({k: v for k, v in proof.items() if k != 'approval_evidence_hash'})
            with patch.dict(m.settlement_parties, cost_exception_multi_basis=lambda *_: proof):
                result = m.refund_multi_source['financial_basis'](db, refund, self.plans(db))
            self.assertEqual(result['status'], 'waiting')
            cost = m.settlement_multi_cost_exception
            self.assertIsNone(db.get(cost['Application'], refund.id))
            self.assertEqual(db.scalar(select(func.count()).select_from(cost['Segment'])), 0)
            self.assert_buyer_proof(db)

    def test_application_second_segment_write_failure_rolls_back_whole_plan(self):
        self.completed()
        self.approved()
        m = self.m
        cost = m.settlement_multi_cost_exception

        def reject(mapper, connection, target):
            if target.segment_id == 'window2':
                raise ValueError('injected second source failure')

        event.listen(cost['Segment'], 'before_insert', reject)
        try:
            with m.SessionLocal() as db:
                result = m.refund_multi_source['financial_basis'](db, db.get(m.Refund, 'multi'), self.plans(db))
                self.assertEqual(result['status'], 'waiting')
                self.assertIsNone(db.get(cost['Application'], 'multi'))
                self.assertEqual(db.scalar(select(func.count()).select_from(cost['Segment'])), 0)
        finally:
            event.remove(cost['Segment'], 'before_insert', reject)

    def test_source_cap_wrong_binding_duplicates_and_existing_posting_are_rejected(self):
        self.completed()
        for changes in ('overcap', 'wrong', 'duplicate'):
            body = copy.deepcopy(self.body)
            body['request_id'] = changes
            if changes == 'overcap':
                body['source_allocations'][1]['retained_cost'] = '99'
                body['retained_cost'] = '100'
            elif changes == 'wrong':
                body['source_allocations'][1]['baseline_id'] = 'original'
            else:
                body['source_allocations'][1] = copy.deepcopy(body['source_allocations'][0])
            response = self.client.post('/api/settlement-cost-exceptions', json=body,
                headers={'Authorization': 'Bearer ' + self.tokens['finance']})
            self.assertIn(response.status_code, (409, 422), response.text)
        self.request('POST', f'/api/settlements/{self.nid}/lock', actor='finance')
        self.request('POST', '/api/settlement-cost-exceptions', actor='finance', expected=409, json=self.body)

    def test_zero_retained_row_is_frozen_with_evidence_but_has_no_loss_position(self):
        self.completed()
        self.body['source_allocations'][1]['retained_cost'] = '0.00'
        self.body['retained_cost'] = '1.00'
        self.approved()
        self.request('POST', f'/api/settlements/{self.nid}/lock', actor='finance')
        m = self.m
        with m.SessionLocal() as db:
            cost = m.settlement_multi_cost_exception
            self.assertEqual(db.scalar(select(func.count()).select_from(cost['Segment'])), 2)
            losses = db.scalars(select(m.settlement_finance['Position']).where(
                m.settlement_finance['Position'].settlement_id == self.nid,
                m.settlement_finance['Position'].kind == 'loss')).all()
            self.assertEqual(len(losses), 1)
            self.assertEqual(losses[0].amount, 1)
            zero = db.get(cost['Segment'], ('multi', 'window2'))
            self.assertIsNone(json.loads(zero.payload_json)['result']['cost_exception']['loss'])

    def test_zero_retained_row_still_requires_evidence(self):
        self.completed()
        self.body['source_allocations'][1].update(retained_cost='0.00', evidence_refs=[])
        self.body['retained_cost'] = '1.00'
        self.request('POST', '/api/settlement-cost-exceptions', actor='finance', expected=422, json=self.body)

    def test_repeated_cost_refunds_leave_ordinary_profit_budget_available_for_single_reversal(self):
        self.completed()
        self.approved()
        self.request('POST', f'/api/settlements/{self.nid}/lock', actor='finance')
        m = self.m
        with m.SessionLocal() as db:
            second = self.seed_completed(db, self.plans(db, prior='10.00'), 'second', offset=1)
            second_id = m.refund_settlement['negative_settlement'](db, second).id
            raw = m.refund_settlement['raw_multi_basis'](db, second)
            db.commit()
        body = copy.deepcopy(self.body)
        body.update(request_id='second-cost', refund_id='second')
        body['source_allocations'] = [dict(segment_id=r['segment_id'], baseline_id=r['reference_settlement_id'],
            retained_cost=str(i + 1), evidence_refs=['second-invoice:' + r['segment_id']]) for i, r in enumerate(raw['references'])]
        exception = self.request('POST', '/api/settlement-cost-exceptions', actor='finance', json=body)
        self.request('POST', f'/api/settlement-cost-exceptions/{exception["id"]}/decision', actor='provider', json=dict(
            request_id='second-review', expected_version=1, decision='approve', reason='Agreed second retained costs'))
        self.request('POST', f'/api/settlements/{second_id}/lock', actor='finance')
        with m.SessionLocal() as db:
            h = m.settlement_finance
            source = db.get(h['Baseline'], self.target_ids[0])
            records = h['source_reversals'](db, source.settlement_id)
            self.assertEqual(sum(Decimal(r['profit_amount']) for r in records), Decimal('-34.50'))
            from app.settlement_finance_adapter import proportional_values
            values = proportional_values(source, Decimal('17.50'))
            single = m.Settlement(settlement_no='SINGLE-AFTER-COST', order_id=self.order_id,
                status='locked', reference_settlement_id=source.settlement_id, gross_amount=Decimal('-17.50'),
                net_amount=Decimal('-17.50'), cost_amount=-values['cost'], profit_amount=-values['profit'],
                **{k: -v for k, v in values['shares'].items()})
            db.add(single)
            db.flush()
            result = h['capture'](db, single)
            self.assertFalse(isinstance(result, dict), result)
            self.assertEqual(result.revenue, Decimal('-17.50'))

    def test_capture_second_source_failure_leaves_no_partial_financial_posting(self):
        self.completed()
        self.approved()
        m = self.m
        model = m.settlement_finance['RefundSource']

        def reject(mapper, connection, target):
            if target.segment_id == 'window2':
                raise ValueError('injected second financial source failure')

        event.listen(model, 'before_insert', reject)
        try:
            self.request('POST', f'/api/settlements/{self.nid}/lock', actor='finance', expected=500)
        finally:
            event.remove(model, 'before_insert', reject)
        with m.SessionLocal() as db:
            cost = m.settlement_multi_cost_exception
            self.assertIsNone(db.get(cost['Application'], 'multi'))
            self.assertEqual(db.scalar(select(func.count()).select_from(cost['Segment'])), 0)
            self.assertIsNone(db.get(cost['Posting'], 'multi'))
            self.assertIsNone(db.get(m.settlement_finance['Baseline'], self.nid))
            self.assertEqual(db.scalar(select(func.count()).select_from(model)), 0)
            self.assert_buyer_proof(db)


if __name__ == '__main__':
    unittest.main()
