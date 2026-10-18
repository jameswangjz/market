"""Read-only cost proposal basis over real HTTP and disposable application DBs.

FILE completion uses the actual buyer workflow. Multi-source VC completion uses
the existing seeded receipt fixture, not a live resource switch or bank claim.
"""
from decimal import Decimal
import unittest
from unittest.mock import patch

from sqlalchemy import func, select

import test_settlement_funded_combinations_http as funded
import test_multi_source_cost_exceptions as multi
import test_settlement_finance_integration as ordinary


class BasisAssertions:
    def row_counts(self):
        with self.m.SessionLocal() as db:
            return {table.name: db.scalar(select(func.count()).select_from(table))
                    for table in self.m.Base.metadata.sorted_tables}

    def basis(self, refund, *, actor='finance', expected=200, **params):
        return self.request('GET', '/api/settlement-cost-exceptions/basis', actor=actor, expected=expected,
                            params=dict(refund_id=refund['id'], **params))

    def readonly_basis(self, refund, **kwargs):
        before = self.row_counts()
        result = self.basis(refund, **kwargs)
        self.assertEqual(self.row_counts(), before)
        return result


class CostBasisSingleHTTPTests(BasisAssertions, unittest.TestCase):
    setUpClass = classmethod(funded.FundedCombinationHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(funded.FundedCombinationHTTPTests.tearDownClass.__func__)
    setUp = funded.FundedCombinationHTTPTests.setUp
    tearDown = funded.FundedCombinationHTTPTests.tearDown
    request = funded.FundedCombinationHTTPTests.request
    create_order = funded.FundedCombinationHTTPTests.create_order
    create = funded.FundedCombinationHTTPTests.create
    manual = funded.FundedCombinationHTTPTests.manual
    review = funded.FundedCombinationHTTPTests.review
    execute = funded.FundedCombinationHTTPTests.execute
    approved = funded.FundedCombinationHTTPTests.approved
    step = funded.FundedCombinationHTTPTests.step
    batch = funded.FundedCombinationHTTPTests.batch
    complete = funded.FundedCombinationHTTPTests.complete
    negative = funded.FundedCombinationHTTPTests.negative
    funded_source = funded.FundedCombinationHTTPTests.funded_source
    original = ordinary.SettlementFinanceIntegrationTests.original
    approved_cost = funded.FundedCombinationHTTPTests.approved_cost

    def test_actual_ordinary_single_uses_exported_completion_evidence_and_no_applied_hook(self):
        batch, sid = self.original(adjusted=True)
        refund = self.complete()
        with patch.dict(self.m.settlement_finance, refund_basis=lambda *_: self.fail('applied refund hook called')):
            result = self.readonly_basis(refund)
        self.assertEqual((result['status'], result['kind'], result['basis_kind'], result['basis_origin']),
                         ('ready', 'ordinary_single_refund', 'single', 'raw_single_basis'))
        self.assertTrue(result['proposal_allowed'], result)
        self.assertEqual(len(result['references']), 1)
        ref = result['references'][0]
        self.assertEqual(ref['reference_settlement_id'], sid)
        self.assertEqual(Decimal(ref['cost_amount']), -8)
        self.assertEqual(ref['batch_nos'], [batch['batch_no']])
        self.assertTrue(ref['order_no'])
        self.assertTrue(ref['settlement_no'])
        self.assertTrue(ref['baseline_hash'])
        self.assertEqual(self.readonly_basis(refund, actor='provider')['references'], result['references'])

    def test_funded_approved_exception_does_not_pollute_raw_cost_or_write_rows(self):
        self.funded_source()
        refund = self.complete()
        before = self.readonly_basis(refund)
        self.assertTrue(before['proposal_allowed'], before)
        self.assertEqual(before['basis_origin'], 'raw_funded_basis')
        self.approved_cost(refund)
        with patch.dict(self.m.refund_settlement, assessment=lambda *_: self.fail('applied assessment called')):
            after = self.readonly_basis(refund)
        self.assertEqual(after['references'], before['references'])
        self.assertEqual(Decimal(after['references'][0]['cost_amount']), -8)
        self.assertFalse(after['proposal_allowed'])
        self.assertEqual(after['reason'], 'cost_exception_approved')

    def test_buyer_and_verified_other_provider_denied_without_rows(self):
        self.original(adjusted=True)
        refund = self.complete()
        self.readonly_basis(refund, actor='buyer', expected=403)
        self.readonly_basis(refund, actor='provider', expected=403, business_view='buyer')
        with self.m.SessionLocal() as db:
            membership = db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == 'outsider'))
            membership.status, membership.business_roles = 'active', 'provider'
            db.get(self.m.Enterprise, 'other_tenant').verification_status = 'verified'
            db.commit()
        self.readonly_basis(refund, actor='outsider', expected=403)

    def test_missing_baseline_waits_without_creating_negative_or_evidence(self):
        refund = self.complete()
        result = self.readonly_basis(refund)
        self.assertEqual(result['status'], 'waiting')
        self.assertFalse(result['proposal_allowed'])
        self.assertEqual(result['references'], [])

    def test_frozen_and_locked_refund_keep_raw_refs_but_disallow_proposal(self):
        self.funded_source()
        refund = self.complete()
        ready = self.readonly_basis(refund)
        nid = self.negative(refund)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        blocked = self.readonly_basis(refund)
        self.assertEqual(blocked['references'], ready['references'])
        self.assertEqual(blocked['status'], 'ready')
        self.assertFalse(blocked['proposal_allowed'])
        self.assertTrue(blocked['reason'])


class CostBasisMultiHTTPTests(BasisAssertions, unittest.TestCase):
    setUpClass = classmethod(multi.MultiSourceCostTests.setUpClass.__func__)
    tearDownClass = classmethod(multi.MultiSourceCostTests.tearDownClass.__func__)
    setUp = multi.MultiSourceCostTests.setUp
    tearDown = multi.MultiSourceCostTests.tearDown
    request = multi.MultiSourceCostTests.request
    prepare = multi.MultiSourceCostTests.prepare
    create = multi.MultiSourceCostTests.create
    deliver = multi.MultiSourceCostTests.deliver
    scenario = multi.MultiSourceCostTests.scenario
    settlement = multi.MultiSourceCostTests.settlement
    two_changes = multi.MultiSourceCostTests.two_changes
    plans = multi.MultiSourceCostTests.plans
    seed_completed = multi.MultiSourceCostTests.seed_completed
    completed = multi.MultiSourceCostTests.completed
    approved = multi.MultiSourceCostTests.approved

    def test_actual_ordinary_multi_get_preserves_every_raw_source_after_approval(self):
        self.completed()
        refund = dict(id='multi')
        raw = self.m.refund_settlement['raw_multi_basis']
        calls = []
        def observed(db, r, plan=None, *, apply_exception=False):
            calls.append(apply_exception)
            return raw(db, r, plan, apply_exception=apply_exception)
        with patch.dict(self.m.refund_settlement, raw_multi_basis=observed):
            before = self.readonly_basis(refund)
            self.assertEqual(before['status'], 'ready', before)
            self.assertEqual(before['kind'], 'multi_source_refund')
            self.assertEqual(before['basis_kind'], 'multi')
            self.assertEqual(before['basis_origin'], 'raw_multi_basis')
            self.assertTrue(before['proposal_allowed'], before)
            self.assertEqual(len(before['references']), 2)
            self.approved()
            after = self.readonly_basis(refund)
        self.assertTrue(calls)
        self.assertTrue(all(flag is False for flag in calls))
        self.assertEqual(after['references'], before['references'])
        self.assertFalse(after['proposal_allowed'])
        self.assertEqual(after['reason'], 'cost_exception_approved')
        expected = {r['reference_settlement_id']: Decimal(r['cost_amount']) for r in self.ordinary['references']}
        self.assertEqual({r['reference_settlement_id']: Decimal(r['cost_amount']) for r in before['references']}, expected)
