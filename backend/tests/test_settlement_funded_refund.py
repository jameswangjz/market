"""Isolated module contracts, not end-to-end buyer Saga or PostgreSQL evidence.

Funding facts are produced by the real finance approval/verification lifecycle.
The completion hook and explicit segmented/negative baselines are unit fixtures;
production integration requires the refund owner's original full validator.
"""
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import Column, DateTime, Numeric, String, event, select, update
from sqlalchemy.dialects import postgresql

import test_settlement_finance_funding as funding_fixture
from app.settlement_funded_refund import allocation, canonical, digest, install


class FundedRefundTests(unittest.TestCase):
    def setUp(self):
        self.fixture = funding_fixture.FundingTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.db, self.ns, self.finance = self.f.db, self.f.ns, self.f.h

        class Refund(self.ns['Base']):
            __tablename__ = 'refunds'
            id = Column(String, primary_key=True)
            order_id = Column(String)
            amount = Column(Numeric(14, 2))
            status = Column(String)
            completed_at = Column(DateTime(timezone=True))

        self.ns['Refund'] = Refund
        self.h = install(self.ns)
        self.ns['Base'].metadata.create_all(self.f.engine)
        self.plans = {}
        self.ns['settlement_funded_refund_evidence'] = dict(completed_evidence=self.completed_stub)
        self.bindings = [dict(segment_id='segment-s', reference_settlement_id='s', baseline_hash=self.fixture.baseline.snapshot_hash)]

    def completed_stub(self, db, refund):
        """Deliberate unit stub, never represented as original resource validation."""
        plan = self.plans[refund.id]
        raw = canonical(dict(economic_plan=plan, completion_execution_id='fixture:' + refund.id,
            plan_hash='a' * 64, proof_hash='b' * 64, funding_hash='c' * 64, recovery_id='recovery:' + refund.id))
        return refund, plan, raw, (refund.amount, Decimal(0), Decimal(0), {})

    def funded(self):
        proposal = self.fixture.proposal()
        self.fixture.approve(proposal)
        self.fixture.verify(proposal)
        self.db.commit()
        return proposal

    def refund(self, amount='30.00', cumulative='30.00', key='r'):
        row = self.ns['Refund'](id=key, order_id='o', amount=Decimal(amount), status='completed',
            completed_at=datetime(2026, 10, 18, tzinfo=timezone.utc))
        self.db.add(row)
        self.db.flush()
        self.plans[key] = dict(segment_id='segment-s', source_order_id='o', original_amount='150.00',
            cumulative_refund_amount=cumulative, refund_amount=amount, economic_snapshot=dict(currency='CNY'),
            calculation_version='ordinary-file-refund-plan-v1')
        return row

    def source_fixture(self, paid='75.00', *, overlap=False, supplemental='0.00', cost=None):
        """An explicit immutable per-segment fixture, not a core capture claim."""
        paid = Decimal(paid)
        supplemental = Decimal(supplemental)
        if not overlap:
            self.db.add(self.ns['Payment'](id='payment-2', order_id='o', payment_no='PAY-2', status='paid', amount=paid))
        s = self.f.settlement(identifier='source-2')
        s.gross_amount = paid + supplemental
        s.cost_amount = paid * 2 / 3 if cost is None else Decimal(cost)
        s.profit_amount = s.gross_amount - s.cost_amount
        s.platform_fee = paid / 15 if cost is None else s.profit_amount / 5
        s.provider_share = s.profit_amount - s.platform_fee
        entries = [dict(kind=p.kind, share_kind=p.share_kind, participant_type=p.participant_type,
            participant_id=p.participant_id, participant_name=p.participant_name, blocked_reason='',
            amount=str(s.cost_amount if p.kind == 'cost' else s.platform_fee if p.share_kind == 'platform_fee' else s.provider_share))
            for p in self.f.positions()]
        shares = {k: str(getattr(s, k)) for k in ('platform_fee', 'provider_share', 'service_share', 'expert_fee', 'channel_fee')}
        snap = dict(currency='CNY', revenue=str(s.gross_amount), cost=str(s.cost_amount), profit=str(s.profit_amount), shares=shares,
            entries=entries, actual_receipts=str(paid), receipt_records=[dict(payment_id='payment' if overlap else 'payment-2',
                status='paid', amount=str(paid))], bank_receipt_verified=False)
        raw = canonical(snap)
        b = self.finance['Baseline'](settlement_id=s.id, order_id='o', provider_enterprise_id='provider', currency='CNY',
            revenue=s.gross_amount, cost=s.cost_amount, profit=s.profit_amount, snapshot_json=raw, snapshot_hash=digest(raw), actor='unit-fixture')
        self.db.add(b)
        self.db.flush()
        for entry in entries:
            self.db.add(self.finance['Position'](settlement_id=s.id, order_id='o', currency='CNY',
                direction='internal' if entry['share_kind'] == 'platform_fee' else 'payable', **entry))
        self.db.flush()
        return b

    def negative_fixture(self, refund, plan):
        s = self.f.settlement(identifier='negative-' + refund.id, factor=-1, reference='s' if len(plan['references']) == 1 else '')
        s.refund_id = refund.id
        for k in ('gross_amount', 'cost_amount', 'profit_amount'):
            setattr(s, k, Decimal(plan[k]))
        for k, v in plan['shares'].items():
            setattr(s, k, Decimal(v))
        groups = []
        for ref in plan['references']:
            entries = [e for e in ref['entries'] if Decimal(e['amount'])]
            entries += [dict(kind='funding_return', share_kind='', amount=r['amount'], blocked_reason='',
                **{k: r['bearer'][k] for k in ('participant_type', 'participant_id', 'participant_name')})
                for r in ref.get('funding_returns', []) if Decimal(r['amount'])]
            groups.append((ref, entries))
        entries = [e for _, group in groups for e in group]
        raw = canonical(dict(shares=plan['shares'], entries=entries, references=plan['references'], funded_refund_plan_hash=plan['plan_hash']))
        b = self.finance['Baseline'](settlement_id=s.id, order_id='o', provider_enterprise_id='provider', currency='CNY',
            reference_settlement_id=s.reference_settlement_id, revenue=s.gross_amount, cost=s.cost_amount, profit=s.profit_amount,
            snapshot_json=raw, snapshot_hash=digest(raw), actor='unit-fixture')
        self.db.add(b)
        self.db.flush()
        for ref, group in groups:
            for entry in group:
                position = self.finance['Position'](settlement_id=s.id, order_id='o', currency='CNY',
                    direction='payable' if entry['kind'] == 'funding_return' else 'internal' if entry['share_kind'] == 'platform_fee' else 'receivable', **entry)
                self.db.add(position)
                self.db.flush()
                self.db.add(self.finance['RefundPositionSource'](position_id=position.id, settlement_id=s.id,
                    segment_id=ref['segment_id'], source_settlement_id=ref['reference_settlement_id']))
        self.db.flush()
        return b

    def test_partial_150_cash_160_final_and_independent_10_absorption(self):
        self.funded()
        refund = self.refund()
        original = self.fixture.baseline.snapshot_json
        count = self.db.query(self.finance['Position']).count()
        basis = self.h['basis'](self.db, refund, self.bindings)
        self.assertEqual(basis['status'], 'ready', basis)
        self.assertEqual(tuple(basis[k] for k in ('buyer_cash_amount', 'gross_amount', 'cost_amount', 'profit_amount', 'absorption_reversal')),
            ('30.00', '-32.00', '-20.00', '-12.00', '2.00'))
        self.assertEqual((basis['shares']['platform_fee'], basis['shares']['provider_share']), ('-2.00', '-10.00'))
        self.assertEqual(self.db.query(self.h['Plan']).count(), 0)
        frozen = self.h['freeze'](self.db, refund, self.bindings, actor='maker')
        self.assertEqual(self.h['freeze'](self.db, refund, self.bindings), frozen)
        self.assertEqual(self.db.query(self.finance['Position']).count(), count)
        self.assertEqual(self.fixture.baseline.snapshot_json, original)
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)
        self.assertEqual(refund.amount, 30)
        self.assertFalse(frozen['bank_receipt_verified'])
        self.assertEqual((frozen['external_paid'], frozen['external_recovered']), ('0.00', '0.00'))

    def test_partial_then_final_cumulative_rounding_closes_every_cent(self):
        self.funded()
        plans = []
        for key, amount, cumulative in (('one-cent', '.01', '.01'), ('partial', '49.99', '50.00'), ('final', '100.00', '150.00')):
            plans.append(self.h['freeze'](self.db, self.refund(amount, cumulative, key), self.bindings))
        self.assertEqual(sum(Decimal(p['gross_amount']) for p in plans), -160)
        self.assertEqual(sum(Decimal(p['cost_amount']) for p in plans), -100)
        self.assertEqual(sum(Decimal(p['shares']['platform_fee']) for p in plans), -10)
        self.assertEqual(sum(Decimal(p['shares']['provider_share']) for p in plans), -50)
        self.assertEqual(sum(Decimal(p['absorption_reversal']) for p in plans), 10)
        self.assertEqual(sum(Decimal(p['buyer_cash_amount']) for p in plans), 150)
        self.assertEqual(self.h['freeze'](self.db, self.db.get(self.ns['Refund'], 'one-cent'), self.bindings), plans[0])

    def test_missing_contract_or_unconfirmed_or_unproven_never_ready(self):
        refund = self.refund()
        self.assertEqual(self.h['basis'](self.db, refund, self.bindings)['reason'], 'funded_refund_independent_funding_proof_required')
        hook = self.ns.pop('settlement_funded_refund_evidence')
        self.assertEqual(self.h['basis'](self.db, refund, self.bindings)['reason'], 'funded_refund_completion_contract_required')
        self.ns['settlement_funded_refund_evidence'] = hook
        self.db.get(self.ns['SettlementBatch'], 'fixture').status = 'pending_confirm'
        self.assertEqual(self.h['basis'](self.db, refund, self.bindings)['reason'], 'funded_refund_source_batch_unconfirmed')
        self.ns['settlement_parties'] = dict(cost_exception=lambda db, rid: dict(id='approved-exception'))
        self.assertEqual(self.h['basis'](self.db, refund, self.bindings)['reason'],
            'funded_refund_cost_exception_combination_requires_explicit_plan')

    def test_corrupt_proof_absorption_allocations_and_baseline_are_rejected(self):
        proposal = self.funded()
        refund = self.refund()
        models = ((self.finance['FundingProof'], 'evidence_hash'), (self.finance['FundingAbsorption'], 'evidence_hash'),
                  (self.finance['Baseline'], 'snapshot_hash'))
        for model, field in models:
            with self.subTest(model=model.__name__):
                row = self.db.get(model, 's' if model != self.finance['FundingAbsorption'] else proposal['id'])
                original = getattr(row, field)
                self.db.execute(update(model.__table__).values(**{field: '0' * 64}))
                self.db.expire_all()
                self.assertEqual(self.h['basis'](self.db, refund, self.bindings)['status'], 'waiting')
                self.db.execute(update(model.__table__).values(**{field: original}))
                self.db.expire_all()
        absorption = self.db.get(self.finance['FundingAbsorption'], proposal['id'])
        original_raw = absorption.evidence_json
        forged = json.loads(original_raw)
        forged['allocations'][0]['amount'] = '9.99'
        raw = canonical(forged)
        self.db.execute(update(self.finance['FundingAbsorption'].__table__).values(evidence_json=raw, evidence_hash=digest(raw)))
        self.db.expire_all()
        self.assertEqual(self.h['basis'](self.db, refund, self.bindings)['reason'], 'funded_refund_funding_allocations_mismatch')

    def test_competing_cumulative_reservations_and_actual_buyer_cap(self):
        self.funded()
        first = self.refund('90.00', '90.00', 'first')
        self.h['freeze'](self.db, first, self.bindings)
        second = self.refund('60.00', '60.00', 'competing')
        with self.assertRaises(HTTPException) as error:
            self.h['freeze'](self.db, second, self.bindings)
        self.assertEqual(error.exception.detail, 'funded_refund_cumulative_history_conflict')
        self.plans[second.id]['cumulative_refund_amount'] = '150.00'
        self.h['freeze'](self.db, second, self.bindings)
        over = self.refund('.01', '150.01', 'over-cap')
        self.assertEqual(self.h['basis'](self.db, over, self.bindings)['reason'], 'funded_refund_buyer_cash_cap_exceeded')
        self.assertEqual(self.db.query(self.h['Plan']).count(), 2)

    def test_post_once_without_duplicate_positions_or_external_cash(self):
        self.funded()
        refund = self.refund()
        plan = self.h['freeze'](self.db, refund, self.bindings)
        negative = self.negative_fixture(refund, plan)
        count = self.db.query(self.finance['Position']).count()
        post = self.h['post'](self.db, refund, self.bindings, negative.settlement_id)
        self.assertEqual(self.h['post'](self.db, refund, self.bindings, negative.settlement_id), post)
        self.assertEqual(post['absorption_reversal'], '2.00')
        self.assertEqual(self.db.query(self.h['Posting']).count(), 1)
        self.assertEqual(self.db.query(self.finance['Position']).count(), count)
        self.assertEqual(self.db.query(self.finance['Transaction']).count(), 1)
        self.assertFalse(post['bank_receipt_verified'])
        projection = self.h['describe'](self.db, negative.settlement_id)
        self.assertEqual((projection['kind'], projection['absorption_reversal']), ('internal_funding_absorption_reversal', '2.00'))
        self.assertTrue(projection['recorded_at'])
        self.assertIsNone(self.h['describe'](self.db, 's'))
        self.db.commit()
        for model, key, field in ((self.h['Slice'], (refund.id, 's'), 'source_hash'),
                                  (self.h['Posting'], refund.id, 'plan_hash')):
            with self.subTest(immutable=model.__name__):
                setattr(self.db.get(model, key), field, 'changed')
                with self.assertRaises(ValueError):
                    self.db.flush()
                self.db.rollback()

    def test_immutable_records_and_bad_capture_cannot_create_posting(self):
        self.funded()
        refund = self.refund()
        self.h['freeze'](self.db, refund, self.bindings)
        self.db.commit()
        self.db.get(self.h['Plan'], refund.id).actor = 'changed'
        with self.assertRaises(ValueError):
            self.db.flush()
        self.db.rollback()
        with self.assertRaises(HTTPException):
            self.h['post'](self.db, refund, self.bindings, 's')
        self.assertEqual(self.db.query(self.h['Posting']).count(), 0)

    def test_multi_source_distinct_cash_bindings_and_posting_contract(self):
        self.funded()
        source = self.source_fixture()
        refund = self.refund('45.00', '30.00')
        segment = dict(self.plans[refund.id])
        segment['refund_amount'] = '30.00'
        other = dict(segment, segment_id='segment-2', original_amount='75.00', cumulative_refund_amount='15.00', refund_amount='15.00')
        self.plans[refund.id] = dict(segments=[segment, other])
        bindings = self.bindings + [dict(segment_id='segment-2', reference_settlement_id=source.settlement_id, baseline_hash=source.snapshot_hash)]
        plan = self.h['freeze'](self.db, refund, bindings)
        self.assertEqual((plan['buyer_cash_amount'], plan['gross_amount'], plan['cost_amount'], plan['absorption_reversal']),
            ('45.00', '-47.00', '-30.00', '2.00'))
        negative = self.negative_fixture(refund, plan)
        self.assertEqual(len(self.h['post'](self.db, refund, bindings, negative.settlement_id)['references']), 2)
        final = self.refund('180.00', '150.00', 'multi-final')
        self.plans[final.id] = dict(segments=[dict(segment, refund_amount='120.00', cumulative_refund_amount='150.00'),
            dict(other, refund_amount='60.00', cumulative_refund_amount='75.00')])
        remaining = self.h['freeze'](self.db, final, bindings)
        self.assertEqual(Decimal(plan['gross_amount']) + Decimal(remaining['gross_amount']), -235)
        self.assertEqual(Decimal(plan['cost_amount']) + Decimal(remaining['cost_amount']), -150)
        self.assertEqual(Decimal(plan['absorption_reversal']) + Decimal(remaining['absorption_reversal']), 10)

    def test_cross_tenant_source_and_duplicate_cash_basis_are_denied(self):
        self.funded()
        source = self.source_fixture('150.00', overlap=True)
        refund = self.refund()
        first = dict(self.plans[refund.id], refund_amount='15.00', cumulative_refund_amount='15.00')
        second = dict(first, segment_id='segment-2')
        self.plans[refund.id] = dict(segments=[first, second])
        bindings = self.bindings + [dict(segment_id='segment-2', reference_settlement_id=source.settlement_id, baseline_hash=source.snapshot_hash)]
        self.assertEqual(self.h['basis'](self.db, refund, bindings)['reason'], 'funded_refund_overlapping_source_cash_basis')
        self.db.get(self.ns['Order'], 'o').provider_enterprise_id = 'other-provider'
        self.assertEqual(self.h['basis'](self.db, refund, bindings)['reason'], 'funded_refund_source_scope_mismatch')

    def test_install_idempotent_and_freeze_lock_order_is_explicit(self):
        self.assertIs(install(self.ns), self.h)
        self.funded()
        refund = self.refund()
        locks = []
        def inspect(state):
            statement = state.statement
            if getattr(statement, '_for_update_arg', None) is not None:
                locks.append(str(statement.compile(dialect=postgresql.dialect())))
        event.listen(self.db, 'do_orm_execute', inspect)
        try:
            self.h['freeze'](self.db, refund, self.bindings)
        finally:
            event.remove(self.db, 'do_orm_execute', inspect)
        self.assertIn('FROM orders', locks[0])
        self.assertIn('FROM refunds', locks[1])
        self.assertIn('ORDER BY settlement_finance_baselines.settlement_id FOR UPDATE', locks[2])

    def test_other_refund_cannot_rebind_the_same_paid_cash_to_a_new_baseline(self):
        self.funded()
        first = self.refund()
        self.h['freeze'](self.db, first, self.bindings)
        other_source = self.source_fixture('150.00', overlap=True)
        second = self.refund('10.00', '10.00', 'second')
        self.plans[second.id]['segment_id'] = 'segment-2'
        binding = [dict(segment_id='segment-2', reference_settlement_id=other_source.settlement_id, baseline_hash=other_source.snapshot_hash)]
        self.assertEqual(self.h['basis'](self.db, second, binding)['reason'], 'funded_refund_overlapping_reserved_cash_basis')

    def test_arbitrary_verified_receipt_without_manual_evidence_is_rejected(self):
        proposal = self.fixture.proposal('external_receipt')
        self.fixture.approve(proposal)
        self.ns['settlement_funding_proofs'] = dict(verify=lambda *args: dict(status='verified', source_key='unit:cash',
            amount='10.00', currency='CNY', payer_type='enterprise', payer_id='provider', evidence_hash='f' * 64))
        self.fixture.verify(proposal)
        refund = self.refund()
        self.assertEqual(self.h['basis'](self.db, refund, self.bindings)['status'], 'waiting')
        self.assertEqual(self.db.query(self.h['Plan']).count(), 0)

    def test_post_rejects_extra_positions_and_audit_failure_is_atomic(self):
        self.funded()
        refund = self.refund()
        self.f.audit.side_effect = RuntimeError('audit unavailable')
        with self.assertRaises(RuntimeError):
            self.h['freeze'](self.db, refund, self.bindings)
        self.assertEqual(self.db.query(self.h['Plan']).count(), 0)
        self.assertEqual(self.db.query(self.h['Slice']).count(), 0)
        self.f.audit.side_effect = None
        plan = self.h['freeze'](self.db, refund, self.bindings)
        negative = self.negative_fixture(refund, plan)
        self.db.add(self.finance['Position'](settlement_id=negative.settlement_id, order_id='o', currency='CNY',
            kind='profit', share_kind='platform_fee', participant_type='platform', participant_id='platform',
            participant_name='Platform', direction='internal', amount=1))
        self.db.flush()
        with self.assertRaises(HTTPException) as error:
            self.h['post'](self.db, refund, self.bindings, negative.settlement_id)
        self.assertEqual(error.exception.detail, 'funded_refund_capture_positions_mismatch')
        self.assertEqual(self.db.query(self.h['Posting']).count(), 0)

    def test_phase_one_core_contract_is_not_silently_replaced(self):
        original = self.finance['refund_basis']
        self.funded()
        refund = self.refund()
        self.h['freeze'](self.db, refund, self.bindings)
        self.assertIs(self.finance['refund_basis'], original)
        self.assertEqual(original(self.db, refund, dict(original_amount='150.00', cumulative_refund_amount='30.00'))['status'], 'unsupported')

    def test_apportionment_is_monotone_and_exact_at_full_source(self):
        weights = ['.05', '.03', '.01', '.01', '.02']
        prior = [Decimal(0)] * len(weights)
        for cents in range(13):
            current = allocation(weights, Decimal(cents) / 100)
            self.assertEqual(sum(current), Decimal(cents) / 100)
            self.assertTrue(all(a >= b for a, b in zip(current, prior)))
            prior = current
        self.assertEqual(current, [Decimal(v) for v in weights])


if __name__ == '__main__':
    unittest.main()
