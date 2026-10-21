"""Exact VC mapping and HTTP missing-evidence guards; seeded Saga, no gateway I/O.

These tests do not claim that upstream funded VC transfers are implemented.
"""
import unittest

from sqlalchemy import delete, func, select

import test_cross_version_multi_source_refund as fixture


class FundedSegmentBindingHTTPTests(unittest.TestCase):
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

    def test_bindings_are_each_exact_target_not_original_order_payment_or_first_baseline(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            refund = self.seed_completed(db, self.plans(db))
            before = db.scalar(select(func.count()).select_from(m.settlement_finance['Baseline']))
            bindings = m.settlement_funded_refund_capture['bindings'](db, refund)
            self.assertEqual({r['segment_id']: r['reference_settlement_id'] for r in bindings},
                             dict(window=self.target_ids[0], window2=self.target_ids[1]))
            self.assertTrue(all(r['reference_settlement_id'] != 'original' for r in bindings))
            self.assertTrue(all(r['baseline_hash'] and r['receipt_hash'] and r['segment_context_hash'] for r in bindings))
            self.assertIsNone(m.settlement_funded_refund_capture['segment_basis'](db, refund))
            ordinary = m.refund_settlement['raw_multi_basis'](db, refund)
            self.assertEqual((ordinary['status'], ordinary['kind'], ordinary['reference_settlement_id']),
                             ('ready', 'multi_source_refund', ''))
            self.assertEqual(before, db.scalar(select(func.count()).select_from(m.settlement_finance['Baseline'])))
            self.assertIsNone(db.get(m.settlement_funded_refund['Plan'], refund.id))

    def test_missing_one_exact_segment_link_waits_without_partial_finance_or_buyer_rollback(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            refund = self.seed_completed(db, self.plans(db))
            completion = db.get(m.refund_execution['Completion'], refund.id)
            proof = completion.economic_plan_json, completion.proof_hash, completion.funding_hash
            # Remove one binding only in this disposable adversarial fixture.
            Link = m.version_change_settlement['SegmentBaselineLink']
            db.execute(delete(Link).where(Link.window_id == 'window2'))
            negative = m.refund_settlement['negative_settlement'](db, refund)
            nid = negative.id
            self.assertEqual(m.refund_settlement['assessment'](db, nid)['status'], 'waiting')
            db.commit()
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance', expected=409)
        with m.SessionLocal() as db:
            self.assertIsNone(db.get(m.settlement_finance['Baseline'], nid))
            self.assertEqual(db.scalar(select(func.count()).select_from(m.settlement_finance['RefundSource'])), 0)
            self.assertIsNone(db.get(m.settlement_funded_refund['Plan'], 'multi'))
            self.assertEqual(db.get(m.Refund, 'multi').status, 'completed')
            completion = db.get(m.refund_execution['Completion'], 'multi')
            self.assertEqual(proof, (completion.economic_plan_json, completion.proof_hash, completion.funding_hash))

    def test_source_reversals_counts_vc_legacy_and_multi_child_once_each(self):
        from decimal import Decimal
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            refund = self.seed_completed(db, self.plans(db))
            nid = m.refund_settlement['negative_settlement'](db, refund).id
            db.commit()
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        with m.SessionLocal() as db:
            for source_id, count, gross in ((self.target_ids[0], 2, Decimal('-47.50')),
                                            (self.target_ids[1], 1, Decimal('-10.00'))):
                records = m.settlement_finance['source_reversals'](db, source_id)
                self.assertEqual(len(records), count)
                self.assertEqual(sum(Decimal(r['gross_amount']) for r in records), gross)
            # Fresh raw evaluation excludes its own capture exactly once.
            basis = m.refund_settlement['raw_multi_basis'](db, db.get(m.Refund, 'multi'))
            self.assertEqual(basis['status'], 'ready', basis)


if __name__ == '__main__':
    unittest.main()
