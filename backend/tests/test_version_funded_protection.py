"""Review probes for existing guards, not a proposed VC funding API.

All data is disposable SQLite; segmented completion plans below are explicit unit fixtures,
not claims of buyer Saga or PostgreSQL concurrency validation.
"""
from decimal import Decimal
import hashlib
import json
import unittest

from fastapi import HTTPException
from sqlalchemy import Column, String
from sqlalchemy.orm import Session

from app.settlement_finance_adapter import protect, protect_rebuild
import test_settlement_finance as ledger_fixture
import test_settlement_finance_funding as funding_fixture
import test_settlement_funded_refund as refund_fixture
import test_settlement_parties_multi_cost as cost_fixture


class RebuildReferenceReviewTests(unittest.TestCase):
    def test_approved_multi_cost_payload_source_requires_rebuild_protection(self):
        f = cost_fixture.MultiCostExceptionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        Base = f.ns['Base']
        class Settlement(Base):
            __tablename__ = 'review_source_settlements'
            id = Column(String, primary_key=True)
            status = Column(String)
            reference_settlement_id = Column(String, default='')
            refund_id = Column(String, default='')
        Base.metadata.create_all(f.f.engine)
        f.ns['Settlement'] = Settlement
        with f.f.engine.begin() as connection:
            connection.execute(Settlement.__table__.insert(), [
                dict(id='b1', status='locked', reference_settlement_id='', refund_id=''),
                dict(id='b2', status='locked', reference_settlement_id='', refund_id='')])
        proposal = f.propose()
        f.f.decision(proposal['id'])
        with Session(f.f.engine) as db:
            approved = f.h['cost_exception'](db, 'r1')
            self.assertEqual(approved['status'], 'approved')
            self.assertEqual(approved['baseline_id'], '')
            self.assertIn('b2', [a['baseline_id'] for a in approved['source_allocations']])
            with self.assertRaises(HTTPException) as error:
                protect_rebuild(f.ns, db, ['b2'])
            self.assertEqual(error.exception.status_code, 409)

    def test_frozen_funded_slice_nonfunded_source_requires_rebuild_protection(self):
        f = refund_fixture.FundedRefundTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.funded()
        source = f.source_fixture()
        refund = f.refund('45.00', '30.00')
        segment = dict(f.plans[refund.id], refund_amount='30.00')
        second = dict(segment, segment_id='segment-2', original_amount='75.00',
                      cumulative_refund_amount='15.00', refund_amount='15.00')
        f.plans[refund.id] = dict(segments=[segment, second])
        bindings = f.bindings + [dict(segment_id='segment-2', reference_settlement_id=source.settlement_id,
                                     baseline_hash=source.snapshot_hash)]
        frozen = f.h['freeze'](f.db, refund, bindings)
        self.assertEqual(frozen['status'], 'ready')
        self.assertIsNotNone(f.db.get(f.h['Slice'], (refund.id, source.settlement_id)))
        self.assertIsNone(f.db.get(f.finance['FundingProof'], source.settlement_id))
        with self.assertRaises(HTTPException):
            protect_rebuild(f.ns, f.db, ['s'])
        with self.assertRaises(HTTPException) as error:
            protect_rebuild(f.ns, f.db, [source.settlement_id])
        self.assertEqual(error.exception.status_code, 409)


class ExistingPaymentProtectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = funding_fixture.FundingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f, self.db, self.h, self.ns = self.fixture.f, self.fixture.db, self.fixture.h, self.fixture.ns
        proposal = self.fixture.proposal()
        self.fixture.approve(proposal)
        self.fixture.verify(proposal)
        self.db.commit()

    def test_locked_funded_source_cannot_be_rebuilt_or_edited(self):
        for guard in (protect, protect_rebuild):
            with self.assertRaises(HTTPException) as error:
                guard(self.ns, self.db, ['s'])
            self.assertEqual(error.exception.status_code, 409)

    def test_ancestor_proof_does_not_authorize_other_target_payout(self):
        original = self.fixture.baseline.snapshot_json
        target = self.f.settlement(identifier='target')
        target.gross_amount, target.profit_amount, target.provider_share = 160, 60, 50
        self.db.flush()
        baseline = self.h['capture'](self.db, target)
        self.assertFalse(isinstance(baseline, dict))
        positions = [p for p in self.f.positions('target') if p.direction == 'payable']
        with self.assertRaises(HTTPException) as error:
            self.f.payout(positions, key='no-ancestor-proof-transfer')
        self.assertEqual(error.exception.detail, 'position_cash_blocked')
        self.assertIsNone(self.db.get(self.h['FundingProof'], 'target'))
        self.assertEqual(self.db.get(self.h['Baseline'], 's').snapshot_json, original)

    def test_paid_source_keeps_allocations_and_rebuild_guard(self):
        positions = [p for p in self.f.positions() if p.direction == 'payable']
        transaction, _ = self.f.payout(positions, key='source-payment')
        self.f.confirm(transaction)
        self.db.get(self.ns['Settlement'], 's').status = 'paid'
        self.db.commit()
        amounts = {p.id: self.h['balance'](self.db, p) for p in positions}
        self.assertTrue(all(amount == 0 for amount in amounts.values()))
        with self.assertRaises(HTTPException):
            protect_rebuild(self.ns, self.db, ['s'])
        self.assertEqual({p.id: self.h['balance'](self.db, p) for p in positions}, amounts)


class SharedSourceBudgetReviewTests(unittest.TestCase):
    def test_prior_partial_refund_and_repeated_reversal_share_original_budget(self):
        f = ledger_fixture.FinanceTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        source = f.capture()
        first = f.settlement(identifier='partial-refund', factor=Decimal('-.4'), reference='s')
        f.h['capture'](f.db, first)
        second = f.settlement(identifier='old-switch-reversal', factor=Decimal('-.4'), reference='s')
        f.h['capture'](f.db, second)
        records = f.h['source_reversals'](f.db, source.settlement_id)
        self.assertEqual(sum(abs(Decimal(r['gross_amount'])) for r in records), Decimal('120'))
        repeated = f.settlement(identifier='second-switch-reversal', factor=Decimal('-.3'), reference='s')
        with self.assertRaises(HTTPException) as error:
            f.h['capture'](f.db, repeated)
        self.assertEqual(error.exception.detail, 'reversal_exceeds_original_baseline')
        self.assertIsNone(f.db.get(f.h['Baseline'], repeated.id))


class FrozenReferenceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.f = ledger_fixture.FinanceTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.db, self.ns = self.f.db, self.f.ns
        self.source = self.f.capture()
        Base = self.ns['Base']
        class Plan(Base):
            __tablename__ = 'review_funded_plans'
            refund_id = Column(String, primary_key=True)
            payload_json = Column(String)
            payload_hash = Column(String)
        class Slice(Base):
            __tablename__ = 'review_funded_slices'
            refund_id = Column(String, primary_key=True)
            source_settlement_id = Column(String, primary_key=True)
            segment_id = Column(String)
            source_hash = Column(String)
            payload_json = Column(String)
            payload_hash = Column(String)
        class Projection(Base):
            __tablename__ = 'review_version_projections'
            settlement_id = Column(String, primary_key=True)
            source_settlement_id = Column(String)
            change_id = Column(String)
            window_id = Column(String)
            payload_json = Column(String)
            payload_hash = Column(String)
        class Credit(Base):
            __tablename__ = 'review_version_credits'
            position_id = Column(String, primary_key=True)
            settlement_id = Column(String)
            source_settlement_id = Column(String)
            projection_hash = Column(String)
        class CostException(Base):
            __tablename__ = 'review_cost_exceptions'
            id = Column(String, primary_key=True)
            baseline_id = Column(String)
            payload_json = Column(String)
        Base.metadata.create_all(self.f.engine)
        self.Plan, self.Slice, self.Projection, self.Credit, self.CostException = Plan, Slice, Projection, Credit, CostException
        self.ns['settlement_funded_refund'] = {}
        self.ns['settlement_version_funding'] = {}
        self.ns['settlement_parties'] = {}
        self.ref = dict(segment_id='segment', reference_settlement_id='s', baseline_hash=self.source.snapshot_hash)

    def encode(self, payload):
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def blocked(self, identity):
        for guard in (protect, protect_rebuild):
            with self.subTest(guard=guard.__name__), self.assertRaises(HTTPException) as error:
                guard(self.ns, self.db, [identity])
            self.assertEqual(error.exception.status_code, 409)

    def plan(self):
        self.ns['settlement_funded_refund'] = dict(Plan=self.Plan, Slice=self.Slice)
        raw, digest = self.encode(dict(refund_id='r', references=[self.ref]))
        row = self.Plan(refund_id='r', payload_json=raw, payload_hash=digest)
        self.db.add(row)
        self.db.flush()
        return row

    def projection(self):
        self.ns['settlement_version_funding'] = dict(Projection=self.Projection, Credit=self.Credit)
        payload = dict(settlement_id='old', source_settlement_id='s', change_id='change', window_id='window')
        raw, digest = self.encode(payload)
        row = self.Projection(**payload, payload_json=raw, payload_hash=digest)
        self.db.add(row)
        self.db.flush()
        return row

    def test_optional_modules_absent_preserves_unused_baseline_and_pending_rebuild(self):
        for key in ('settlement_funded_refund', 'settlement_version_funding', 'settlement_parties'):
            self.ns.pop(key)
        protect_rebuild(self.ns, self.db, ['s'])
        pending = self.f.settlement(identifier='pending', status='pending')
        protect(self.ns, self.db, [pending.id])
        protect_rebuild(self.ns, self.db, [pending.id])

    def test_hashed_plan_protects_all_sources_and_unbaselined_target(self):
        self.plan()
        target = self.f.settlement(identifier='pending', status='pending')
        target.refund_id = 'r'
        self.db.flush()
        self.blocked('s')
        self.blocked(target.id)
        self.assertIsNone(self.db.get(self.f.h['Baseline'], target.id))
        protect_rebuild(self.ns, self.db, ['unrelated'])

    def test_bad_plan_hash_and_missing_source_proof_fail_closed(self):
        plan = self.plan()
        plan.payload_hash = 'wrong'
        self.db.flush()
        self.blocked('unrelated')
        raw, digest = self.encode(dict(refund_id='r', references=[dict(reference_settlement_id='s')]))
        plan.payload_json, plan.payload_hash = raw, digest
        self.db.flush()
        self.blocked('unrelated')

    def test_funded_slice_requires_hashed_matching_parent_and_column_bindings(self):
        self.plan()
        raw, digest = self.encode(self.ref)
        row = self.Slice(refund_id='r', source_settlement_id='s', segment_id='segment',
                         source_hash=self.source.snapshot_hash, payload_json=raw, payload_hash=digest)
        self.db.add(row)
        self.db.flush()
        protect_rebuild(self.ns, self.db, ['unrelated'])
        row.segment_id = 'wrong'
        self.db.flush()
        self.blocked('unrelated')

    def test_orphan_slice_namespace_fails_closed(self):
        self.ns['settlement_funded_refund'] = dict(Slice=self.Slice)
        raw, digest = self.encode(self.ref)
        self.db.add(self.Slice(refund_id='orphan', source_settlement_id='s', segment_id='segment',
            source_hash=self.source.snapshot_hash, payload_json=raw, payload_hash=digest))
        self.db.flush()
        self.blocked('unrelated')

    def test_multicost_basis_hash_protects_source_without_blocking_unrelated(self):
        self.ns['settlement_parties'] = dict(CostException=self.CostException)
        _, digest = self.encode(self.ref)
        allocation = dict(baseline_id='s', segment_id='segment', baseline_hash=self.source.snapshot_hash,
                          basis=self.ref, basis_hash=digest)
        row = self.CostException(id='cost', baseline_id='', payload_json=json.dumps(
            dict(schema_version=2, source_allocations=[allocation])))
        self.db.add(row)
        self.db.flush()
        self.blocked('s')
        protect_rebuild(self.ns, self.db, ['unrelated'])
        allocation['basis_hash'] = 'wrong'
        row.payload_json = json.dumps(dict(schema_version=2, source_allocations=[allocation]))
        self.db.flush()
        self.blocked('unrelated')

    def test_projection_and_credit_bind_both_ends_and_do_not_blanket_block(self):
        projection = self.projection()
        credit = self.Credit(position_id='credit', settlement_id='old', source_settlement_id='s',
                             projection_hash=projection.payload_hash)
        self.db.add(credit)
        self.db.flush()
        self.blocked('old')
        self.blocked('s')
        protect_rebuild(self.ns, self.db, ['unrelated'])
        credit.projection_hash = 'wrong'
        self.db.flush()
        self.blocked('unrelated')

    def test_projection_bad_hash_and_credit_missing_parent_fail_closed(self):
        projection = self.projection()
        projection.payload_hash = 'wrong'
        self.db.flush()
        self.blocked('unrelated')
        self.ns['settlement_version_funding'] = dict(Credit=self.Credit)
        self.db.add(self.Credit(position_id='credit', settlement_id='old', source_settlement_id='s', projection_hash='missing'))
        self.db.flush()
        self.blocked('unrelated')


if __name__ == '__main__':
    unittest.main()
