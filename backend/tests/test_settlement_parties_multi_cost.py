"""Disposable SQLite multi-source cost approvals; raw basis is a boundary stub."""
import copy
import json
import unittest

from sqlalchemy import Column, Integer, String, select
from sqlalchemy.orm import Session

import test_settlement_parties as fixtures


class MultiCostExceptionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SettlementPartiesTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.h, self.ns = self.f.h, self.f.ns
        Baseline = self.ns['settlement_finance']['Baseline']
        Baseline.snapshot_hash = Column(String)
        Baseline.provider_enterprise_id = Column(String)
        Baseline.__table__.drop(self.f.engine)
        Baseline.__table__.create(self.f.engine)
        with Session(self.f.engine) as db:
            for identity in ('b1', 'b2'):
                db.add(Baseline(settlement_id=identity, order_id='o1', currency='CNY', revenue=150,
                    cost=100, reference_settlement_id='', snapshot_hash=identity+'hash', provider_enterprise_id='e1'))
            db.commit()
        self.refs = [dict(segment_id='seg'+str(i), reference_settlement_id='b'+str(i),
            baseline_hash='b'+str(i)+'hash', cost_amount='-20.00', gross_amount='-25.00',
            profit_amount='-5.00', currency='CNY', source_order_id='o1') for i in (1, 2)]
        self.ns['refund_settlement'] = {'raw_multi_basis': lambda db, refund:
            dict(status='ready', references=copy.deepcopy(self.refs))}
        self.f.bearer = self.f.party()['id']

    def allocations(self, amounts=('5.00', '3.00')):
        return [dict(segment_id=r['segment_id'], baseline_id=r['reference_settlement_id'],
                     retained_cost=amount, evidence_refs=['invoice:'+r['segment_id']])
                for r, amount in zip(self.refs, amounts)]

    def propose(self, **kwargs):
        values = dict(retained_cost='8.00', source_allocations=self.allocations())
        values.update(kwargs)
        return self.f.exception(**values)

    def test_full_proof_opposite_approval_idempotency_no_refund_write(self):
        pending = self.propose()
        self.assertEqual(pending['baseline_id'], '')
        self.assertEqual(pending['schema_version'], 2)
        self.assertEqual(len(pending['source_allocations']), 2)
        self.assertEqual(self.propose(), pending)
        self.f.decision(pending['id'], actor='provider', expected=403)
        self.f.decision(pending['id'], actor='other', expected=404)
        approved = self.f.decision(pending['id'])
        self.assertEqual(approved['status'], 'approved')
        self.assertEqual(self.f.decision(pending['id']), approved)
        with Session(self.f.engine) as db:
            refund = db.get(self.ns['Refund'], 'r1')
            proof = self.h['cost_exception_multi_basis'](db, refund, list(reversed(self.refs)))
            self.assertEqual(proof['status'], 'ready')
            self.assertEqual(len(proof['approval_evidence_hash']), 64)
            self.assertEqual(str(refund.amount), '50.00')
            changed = copy.deepcopy(self.refs)
            changed[1]['baseline_hash'] = 'changed'
            self.assertEqual(self.h['cost_exception_multi_basis'](db, refund, changed)['status'], 'waiting')

    def test_each_cap_total_precision_and_explicit_all_sources(self):
        self.propose(expected=409, retained_cost='24', source_allocations=self.allocations(('21', '3')))
        self.propose(expected=409, retained_cost='24', source_allocations=self.allocations(('3', '21')))
        self.propose(expected=422, retained_cost='9')
        self.propose(expected=422, source_allocations=self.allocations()[:1])
        self.propose(expected=422, source_allocations=[{}, {}])
        self.propose(expected=422, source_allocations=self.allocations(('5.001', '3')))
        rows = self.allocations()
        rows[1]['evidence_refs'] = []
        self.propose(expected=422, source_allocations=rows)
        rows = self.allocations()
        rows[1] = rows[0]
        self.propose(expected=422, source_allocations=rows)

    def test_revalidation_at_approval_and_no_raw_hook(self):
        e = self.propose()
        self.refs[1]['baseline_hash'] = 'changed'
        self.f.decision(e['id'], expected=409)
        with Session(self.f.engine) as db:
            self.assertIsNone(db.get(self.h['Decision'], e['id']))
        self.ns['refund_settlement'] = {}
        self.propose(key='missing', refund_id='r2', expected=409)

    def test_approved_single_and_multi_budget_shared(self):
        # Existing single-source approval reserves 96 of source b2's 100.
        with Session(self.f.engine) as db:
            row = self.h['CostException'](id='legacy', refund_id='r2', order_id='o1',
                provider_enterprise_id='e1', baseline_id='b2', payload_json='{"retained_cost":"96.00"}',
                creator_id='provider', creator_side='provider')
            db.add(row)
            db.flush()
            db.add(self.h['Decision'](exception_id='legacy', decision='approve', reason='old proof', reviewer_id='ops'))
            db.commit()
        self.propose(retained_cost='10', source_allocations=self.allocations(('5', '5')), expected=409)
        e = self.propose()
        self.f.decision(e['id'])
        with Session(self.f.engine) as db:
            self.assertEqual(len(db.scalars(select(self.h['Decision'])).all()), 2)

    def test_already_posted_refund_requires_explicit_adjustment(self):
        Base = self.ns['Base']
        class Posting(Base):
            __tablename__ = 'fixture_posting'
            refund_id = Column(String, primary_key=True)
        Base.metadata.create_all(self.f.engine)
        self.ns['settlement_finance']['CostExceptionPosting'] = Posting
        with Session(self.f.engine) as db:
            db.add(Posting(refund_id='r1'))
            db.commit()
        self.propose(expected=409)

    def test_no_approved_exception_and_cent_allocations(self):
        with Session(self.f.engine) as db:
            self.assertIsNone(self.h['cost_exception_multi_basis'](db, db.get(self.ns['Refund'], 'r1'), self.refs))
        e = self.propose(retained_cost='.03', source_allocations=self.allocations(('.01', '.02')))
        self.assertEqual(e['retained_cost'], '0.03')
        self.f.decision(e['id'])

    def test_pending_approvals_repeat_shared_budget_check(self):
        with Session(self.f.engine) as db:
            refund = db.get(self.ns['Refund'], 'r3')
            refund.order_id, refund.amount = 'o1', 145.50
            db.add(self.h['CostException'](id='prior', refund_id='r3', order_id='o1',
                provider_enterprise_id='e1', baseline_id='b1', payload_json='{"retained_cost":"97.00"}',
                creator_id='provider', creator_side='provider'))
            db.flush()
            db.add(self.h['Decision'](exception_id='prior', decision='approve', reason='proof', reviewer_id='ops'))
            db.commit()
        first = self.propose(retained_cost='3', source_allocations=self.allocations(('2', '1')))
        second = self.propose(key='second', refund_id='r2', retained_cost='3',
                              source_allocations=self.allocations(('2', '1')))
        self.f.decision(first['id'])
        self.f.decision(second['id'], expected=409, request_id='second-review')
        with Session(self.f.engine) as db:
            self.assertIsNone(db.get(self.h['Decision'], second['id']))

    def test_multisource_approval_counts_against_later_single(self):
        e = self.propose()
        self.f.decision(e['id'])
        with Session(self.f.engine) as db:
            # A later ordinary refund resolves only b1; previously approved
            # multi-source retention must still consume b1's budget.
            db.get(self.ns['settlement_finance']['Baseline'], 'b2').order_id = 'o2'
            db.commit()
        self.f.exception(key='single-over', refund_id='r2', retained_cost='96', expected=409)

    def test_platform_proposal_needs_provider_review_and_evidence_is_immutable(self):
        e = self.propose(actor='ops')
        self.f.decision(e['id'], actor='finance', expected=403)
        self.f.decision(e['id'], actor='provider')
        with Session(self.f.engine) as db:
            db.get(self.h['CostException'], e['id']).payload_json = '{}'
            with self.assertRaises(ValueError):
                db.flush()

    def test_audit_failure_rolls_back_entire_proposal(self):
        def fail(*args, **kwargs):
            raise RuntimeError('audit unavailable')
        self.ns['audit'] = fail
        with self.assertRaisesRegex(RuntimeError, 'audit unavailable'):
            self.propose()
        with Session(self.f.engine) as db:
            self.assertEqual(db.scalars(select(self.h['CostException'])).all(), [])
            self.assertEqual(db.scalars(select(self.h['Decision'])).all(), [])

    def test_source_identity_and_top_schema_rejected(self):
        self.propose(expected=422, baseline_id='b1')
        self.propose(expected=422, schema_version=1)
        self.refs[1]['source_order_id'] = 'o2'
        self.propose(expected=409)

    def test_execution_hook_rechecks_source_and_never_writes(self):
        e = self.propose()
        self.f.decision(e['id'])
        with Session(self.f.engine) as db:
            source = db.get(self.ns['settlement_finance']['Baseline'], 'b2')
            source.snapshot_hash = 'changed-after-approval'
            db.commit()
        with Session(self.f.engine) as db:
            result = self.h['cost_exception_multi_basis'](db, db.get(self.ns['Refund'], 'r1'), self.refs)
            self.assertEqual(result['status'], 'waiting')
            self.assertFalse(db.new)
            self.assertFalse(db.dirty)

    def test_posting_after_proposal_blocks_approval(self):
        e = self.propose()
        Base = self.ns['Base']
        class Posting(Base):
            __tablename__ = 'fixture_later_posting'
            refund_id = Column(String, primary_key=True)
        Base.metadata.create_all(self.f.engine)
        self.ns['settlement_finance']['CostExceptionPosting'] = Posting
        with Session(self.f.engine) as db:
            db.add(Posting(refund_id='r1'))
            db.commit()
        self.f.decision(e['id'], expected=409)
        with Session(self.f.engine) as db:
            self.assertIsNone(db.get(self.h['Decision'], e['id']))

    def test_per_source_evidence_can_supply_top_level_evidence(self):
        body = dict(request_id='per-source-only', refund_id='r1', retained_cost='8.00',
                    reason='Evidence per segment', bearer_participant_id=self.f.bearer,
                    source_allocations=self.allocations())
        e = self.f.request('POST', '/api/settlement-cost-exceptions', 'provider', json=body)
        self.assertEqual(e['evidence_refs'], ['invoice:seg1', 'invoice:seg2'])

    def bind_assessment(self, previous):
        Base = self.ns['Base']
        class Settlement(Base):
            __tablename__ = 'fixture_assessment_settlements'
            id = Column(String, primary_key=True)
            refund_id = Column(String)
            status = Column(String)
        class Link(Base):
            __tablename__ = 'fixture_assessment_links'
            refund_id = Column(String, primary_key=True)
            settlement_id = Column(String)
        class Assessment(Base):
            __tablename__ = 'fixture_financial_assessments'
            settlement_id = Column(String, primary_key=True)
            version = Column(Integer, primary_key=True)
            values_json = Column(String)
            actor = Column(String)
        Base.metadata.create_all(self.f.engine)
        self.ns['Settlement'] = Settlement
        def assessment(db, identity):
            row = db.scalar(select(Assessment).where(Assessment.settlement_id == identity)
                            .order_by(Assessment.version.desc()))
            return json.loads(row.values_json)
        self.ns['refund_settlement'].update(Model=Link, FinancialAssessment=Assessment, assessment=assessment)
        with Session(self.f.engine) as db:
            db.add_all([Settlement(id='b1', status='locked'), Settlement(id='b2', status='paid'),
                        Settlement(id='negative', refund_id='r1', status='pending')])
            db.add(Link(refund_id='r1', settlement_id='negative'))
            db.add(Assessment(settlement_id='negative', version=1, values_json=json.dumps(previous), actor='system'))
            db.commit()
        return Assessment

    def test_multi_ready_approval_invalidates_only_financial_assessment(self):
        e = self.propose()
        previous = dict(status='ready', kind='multi_source_refund', references=self.refs)
        model = self.bind_assessment(previous)
        approved = self.f.decision(e['id'])
        self.assertEqual(self.f.decision(e['id']), approved)
        with Session(self.f.engine) as db:
            rows = db.scalars(select(model).order_by(model.version)).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual(json.loads(rows[0].values_json), previous)
            self.assertEqual(json.loads(rows[1].values_json), dict(status='waiting',
                reason='approved_cost_exception_requires_final_basis_refresh', exception_id=e['id']))
            self.assertEqual(db.get(self.ns['Refund'], 'r1').status, 'completed')

    def test_existing_single_ready_assessment_still_refreshes(self):
        e = self.propose()
        model = self.bind_assessment(dict(status='ready', segment_id='seg1'))
        self.f.decision(e['id'])
        with Session(self.f.engine) as db:
            self.assertEqual(len(db.scalars(select(model)).all()), 2)

    def test_waiting_assessment_not_invalidated_again(self):
        e = self.propose()
        model = self.bind_assessment(dict(status='waiting', kind='multi_source_refund'))
        self.f.decision(e['id'])
        with Session(self.f.engine) as db:
            self.assertEqual(len(db.scalars(select(model)).all()), 1)

    def test_rejected_proposal_does_not_refresh_ready_multi_assessment(self):
        e = self.propose()
        model = self.bind_assessment(dict(status='ready', kind='multi_source_refund'))
        self.f.decision(e['id'], decision='reject')
        with Session(self.f.engine) as db:
            self.assertEqual(len(db.scalars(select(model)).all()), 1)


if __name__ == '__main__':
    unittest.main()
