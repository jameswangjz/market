"""Real manual-funding lifecycle plus isolated refund/negative-ledger fixtures.

No test here represents buyer Saga, bank execution, deployed DB or PG races.
"""
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select, update

import test_settlement_funded_refund as fixture
import test_cross_version_multi_source_refund as segment_fixture


class NormalPlanTests(unittest.TestCase):
    setUp = fixture.FundedRefundTests.setUp
    completed_stub = fixture.FundedRefundTests.completed_stub
    refund = fixture.FundedRefundTests.refund
    source_fixture = fixture.FundedRefundTests.source_fixture
    negative_fixture = fixture.FundedRefundTests.negative_fixture
    funded = fixture.FundedRefundTests.funded

    def external(self, sid='s', key='external'):
        proposal = self.fixture.proposal('external_receipt', key=key, settlement_id=sid)
        self.fixture.approve(proposal)
        manual = self.ns['settlement_manual_funding']
        row = manual['register'](self.db, self.f.user, dict(request_id=key + '-register', funding_request_id=proposal['id'],
            amount='10.00', currency='CNY', reference='MANUAL-' + key, paid_on=datetime.now(timezone.utc).date().isoformat(),
            evidence_refs=['reviewed-receipt:' + key], note='Actual independently reviewed manual receipt fixture'))
        manual['review'](self.db, self.f.reviewer, row['id'], dict(request_id=key + '-review', expected_version=1,
            decision='verified', note='Independent receipt review'))
        self.finance['verify_funding'](self.db, self.f.reviewer, proposal['id'], dict(request_id=key + '-cover',
            expected_version=2, receipt_reference=row['receipt_reference']))
        self.db.commit()
        return row

    def test_verified_external_refunds_original_payer_without_cash_or_platform_credit(self):
        receipt = self.external()
        refund = self.refund()
        original = self.fixture.baseline.snapshot_json
        normal = self.h['raw_basis'](self.db, refund, self.bindings)
        self.assertEqual(normal['status'], 'ready', normal)
        self.assertEqual(tuple(normal[k] for k in ('gross_amount', 'buyer_cash_amount', 'absorption_reversal', 'external_funding_return')),
            ('-32.00', '30.00', '0.00', '2.00'))
        returned = normal['references'][0]['funding_returns'][0]
        self.assertEqual((returned['bearer']['participant_type'], returned['bearer']['participant_id']), ('enterprise', 'provider'))
        self.assertEqual((returned['amount'], returned['status']), ('2.00', 'unpaid'))
        self.assertEqual(returned['manual_evidence_id'], receipt['id'])
        self.assertEqual(returned['manual_evidence_hash'], receipt['evidence_hash'])
        self.assertEqual(returned['execution_policy'], 'offset_or_independent_review_payout')
        plan = self.h['freeze'](self.db, refund, self.bindings)
        self.db.commit()
        self.db.expire_all()
        self.assertEqual(self.h['freeze'](self.db, refund, self.bindings), plan)
        self.assertEqual(self.db.query(self.h['FundingReturn']).count(), 0)
        negative = self.negative_fixture(refund, plan)
        positions = self.db.query(self.finance['Position']).count()
        posted = self.h['post'](self.db, refund, self.bindings, negative.settlement_id)
        self.assertEqual(self.h['post'](self.db, refund, self.bindings, negative.settlement_id), posted)
        self.assertEqual(self.db.query(self.h['FundingReturn']).count(), 1)
        entitlement = self.db.scalar(select(self.h['FundingReturn']))
        self.assertEqual((entitlement.participant_id, entitlement.amount), ('provider', Decimal(2)))
        self.assertEqual(self.db.query(self.finance['Position']).count(), positions)
        self.assertEqual(self.db.query(self.finance['Transaction']).count(), 0)
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)
        self.assertEqual(self.db.get(self.finance['Baseline'], 's').snapshot_json, original)
        described = self.h['describe'](self.db, negative.settlement_id)
        self.assertEqual((described['kind'], described['external_funding_return'], described['absorption_reversal']),
            ('supplementary_funding_reversal', '2.00', '0.00'))
        self.assertFalse(described['bank_receipt_verified'])
        self.db.commit()
        entitlement.amount = 1
        with self.assertRaises(ValueError):
            self.db.flush()
        self.db.rollback()
        self.db.execute(update(self.h['FundingReturn'].__table__).values(amount=Decimal('1.99')))
        self.db.expire_all()
        with self.assertRaises(HTTPException) as error:
            self.h['describe'](self.db, negative.settlement_id)
        self.assertEqual(error.exception.detail, 'funded_refund_return_entitlement_mismatch')

    def test_external_cumulative_cents_close_exactly_and_buyer_cap_remains_150(self):
        self.external()
        plans = []
        for key, amount, cumulative in (('cent', '.01', '.01'), ('partial', '49.99', '50.00'), ('last', '100.00', '150.00')):
            plan = self.h['freeze'](self.db, self.refund(amount, cumulative, key), self.bindings)
            self.assertEqual(-Decimal(plan['gross_amount']), Decimal(plan['buyer_cash_amount']) + Decimal(plan['external_funding_return']))
            plans.append(plan)
        self.assertEqual(sum(Decimal(p['gross_amount']) for p in plans), -160)
        self.assertEqual(sum(Decimal(p['cost_amount']) for p in plans), -100)
        self.assertEqual(sum(Decimal(p['external_funding_return']) for p in plans), 10)
        self.assertEqual(sum(Decimal(p['absorption_reversal']) for p in plans), 0)
        bad = self.refund('.01', '150.01', 'over')
        self.assertEqual(self.h['basis'](self.db, bad, self.bindings)['reason'], 'funded_refund_buyer_cash_cap_exceeded')

    def test_mixed_platform_and_external_sources_keep_both_reversals_separate(self):
        self.funded()
        source = self.source_fixture(supplemental='10.00')
        self.external(source.settlement_id, key='second-source')
        refund = self.refund('45.00', '30.00')
        first = dict(self.plans[refund.id], refund_amount='30.00')
        second = dict(first, segment_id='segment-2', original_amount='75.00', refund_amount='15.00', cumulative_refund_amount='15.00')
        self.plans[refund.id] = dict(segments=[first, second])
        bindings = self.bindings + [dict(segment_id='segment-2', reference_settlement_id=source.settlement_id, baseline_hash=source.snapshot_hash)]
        frozen = self.h['freeze'](self.db, refund, bindings)
        self.assertEqual(tuple(frozen[k] for k in ('gross_amount', 'buyer_cash_amount', 'absorption_reversal', 'external_funding_return')),
            ('-49.00', '45.00', '2.00', '2.00'))
        negative = self.negative_fixture(refund, frozen)
        result = self.h['post'](self.db, refund, bindings, negative.settlement_id)
        self.assertEqual(result['external_funding_return'], '2.00')
        self.assertEqual(result['absorption_reversal'], '2.00')
        self.assertEqual(self.db.query(self.h['FundingReturn']).count(), 1)
        self.assertEqual(self.db.query(self.finance['Transaction']).count(), 1)  # Original internal absorption only.

    def test_external_and_unfunded_sources_freeze_post_and_preserve_cash_caps(self):
        self.external()
        source = self.source_fixture()
        refund = self.refund('45.00', '30.00')
        first = dict(self.plans[refund.id], refund_amount='30.00')
        second = dict(first, segment_id='segment-2', original_amount='75.00',
            refund_amount='15.00', cumulative_refund_amount='15.00')
        self.plans[refund.id] = dict(segments=[first, second])
        bindings = self.bindings + [dict(segment_id='segment-2',
            reference_settlement_id=source.settlement_id, baseline_hash=source.snapshot_hash)]
        frozen = self.h['freeze'](self.db, refund, bindings)
        self.assertEqual(tuple(frozen[k] for k in ('gross_amount', 'buyer_cash_amount',
            'absorption_reversal', 'external_funding_return')), ('-47.00', '45.00', '0.00', '2.00'))
        refs = {r['reference_settlement_id']: r for r in frozen['references']}
        self.assertEqual(refs[source.settlement_id]['funding']['source'], 'none')
        self.assertEqual(refs['s']['funding']['source'], 'external_receipt')
        negative = self.negative_fixture(refund, frozen)
        self.h['post'](self.db, refund, bindings, negative.settlement_id)
        described = self.h['describe'](self.db, negative.settlement_id)
        self.assertEqual(described['external_funding_return'], '2.00')
        self.assertEqual(self.db.query(self.h['FundingReturn']).count(), 1)
        self.assertEqual(self.db.query(self.finance['Transaction']).count(), 0)
        self.assertEqual(len(self.db.scalars(select(self.h['Slice'])).all()), 2)
        over = self.refund('180.01', '150.00', 'over-mixed-cash')
        self.plans[over.id] = dict(segments=[dict(first, refund_amount='120.00', cumulative_refund_amount='150.00'),
            dict(second, refund_amount='60.01', cumulative_refund_amount='75.01')])
        self.assertEqual(self.h['basis'](self.db, over, bindings)['reason'],
            'funded_refund_buyer_cash_cap_exceeded')

    def test_external_cost_above_buyer_cash_has_monotone_cumulative_penny_targets(self):
        source = self.source_fixture(supplemental='10.00', cost='80.00')
        self.external(source.settlement_id, key='cost-above-cash')
        bindings = [dict(segment_id='segment-s', reference_settlement_id=source.settlement_id,
            baseline_hash=source.snapshot_hash)]
        plans, cumulative = [], Decimal(0)
        for index, amount in enumerate(('.01', '.01', '.01', '.97', '24.01', '24.99', '25.00')):
            cumulative += Decimal(amount)
            refund = self.refund(amount, str(cumulative), 'high-cost-' + str(index))
            self.plans[refund.id]['original_amount'] = '75.00'
            plan = self.h['freeze'](self.db, refund, bindings)
            self.assertEqual(-Decimal(plan['gross_amount']), Decimal(amount) + Decimal(plan['external_funding_return']))
            self.assertLessEqual(Decimal(plan['cost_amount']), 0)
            self.assertTrue(all(Decimal(v) <= 0 for v in plan['shares'].values()))
            # Reversal positions carry positive receivables; settlement deltas are negative.
            self.assertEqual(sum(Decimal(e['amount']) for e in plan['references'][0]['entries']), -Decimal(plan['gross_amount']))
            entries = plan['references'][0]['entries']
            self.assertTrue(all(Decimal(e['amount']) >= 0 for e in entries))
            self.assertEqual(sum(Decimal(e['amount']) for e in entries if e['kind'] == 'cost'), -Decimal(plan['cost_amount']))
            for role, share in plan['shares'].items():
                self.assertEqual(sum(Decimal(e['amount']) for e in entries if e['kind'] == 'profit' and e['share_kind'] == role), -Decimal(share))
            if index == 0:
                self.assertEqual((plan['gross_amount'], plan['cost_amount'], plan['profit_amount'],
                    plan['external_funding_return']), ('-0.01', '-0.01', '0.00', '0.00'))
            plans.append(plan)
        self.assertEqual(cumulative, 75)
        self.assertEqual(sum(Decimal(p['gross_amount']) for p in plans), -85)
        self.assertEqual(sum(Decimal(p['cost_amount']) for p in plans), -80)
        self.assertEqual(sum(Decimal(p['profit_amount']) for p in plans), -5)
        self.assertEqual(sum(Decimal(p['shares']['platform_fee']) for p in plans), -1)
        self.assertEqual(sum(Decimal(p['shares']['provider_share']) for p in plans), -4)
        self.assertEqual(sum(Decimal(p['external_funding_return']) for p in plans), 10)
        self.assertEqual(self.db.query(self.finance['Transaction']).count(), 0)

    def test_return_entitlement_audit_failure_rolls_back_without_automatic_payment(self):
        self.external()
        refund = self.refund()
        frozen = self.h['freeze'](self.db, refund, self.bindings)
        negative = self.negative_fixture(refund, frozen)
        self.db.commit()
        self.f.audit.side_effect = RuntimeError('audit unavailable')
        with self.assertRaises(RuntimeError):
            self.h['post'](self.db, refund, self.bindings, negative.settlement_id)
        self.assertEqual(self.db.query(self.h['Posting']).count(), 0)
        self.assertEqual(self.db.query(self.h['FundingReturn']).count(), 0)
        self.assertEqual(self.db.query(self.finance['Transaction']).count(), 0)

    def test_external_manual_evidence_and_review_tampering_fail_closed(self):
        receipt = self.external()
        refund = self.refund()
        manual = self.ns['settlement_manual_funding']
        for model, field, bad in ((manual['Review'], 'decision', 'rejected'), (manual['Evidence'], 'evidence_hash', '0' * 64),
                (manual['Review'], 'reviewer_id', self.f.user.id)):
            with self.subTest(field=field):
                row = self.db.get(model, receipt['id'])
                original = getattr(row, field)
                self.db.execute(update(model.__table__).values(**{field: bad}))
                self.db.expire_all()
                self.assertEqual(self.h['basis'](self.db, refund, self.bindings)['status'], 'waiting')
                self.db.execute(update(model.__table__).values(**{field: original}))
                self.db.expire_all()
        self.assertEqual(self.db.query(self.h['Plan']).count(), 0)

    def test_platform_frozen_dto_has_no_new_external_fields(self):
        self.funded()
        refund = self.refund()
        before = self.h['freeze'](self.db, refund, self.bindings)
        self.assertNotIn('external_funding_return', before)
        self.assertNotIn('funding_returns', before['references'][0])
        self.assertEqual(self.h['raw_basis'](self.db, refund, self.bindings)['references'], before['references'])
        self.assertEqual(self.h['freeze'](self.db, refund, self.bindings), before)

    def test_raw_normal_basis_never_recurses_into_cost_exception_application(self):
        self.funded()
        refund = self.refund()
        self.ns['settlement_parties'] = dict(cost_exception=lambda *args: {'id': 'approved'})
        calls = []
        def apply(db, subject, normal, actor='system'):
            raw = self.h['raw_basis'](db, subject, self.bindings)
            self.assertEqual(raw, normal)
            calls.append(raw)
            return normal
        self.ns['settlement_funded_cost_exception'] = dict(apply=apply)
        self.assertEqual(self.h['basis'](self.db, refund, self.bindings)['status'], 'ready')
        self.assertEqual(len(calls), 1)
        self.h['raw_basis'](self.db, refund, self.bindings)
        self.assertEqual(len(calls), 1)
        self.ns['settlement_funded_cost_exception']['apply'] = lambda db, subject, normal, actor='system': self.h['basis'](db, subject, self.bindings)
        self.assertEqual(self.h['basis'](self.db, refund, self.bindings)['reason'], 'funded_refund_cost_exception_basis_unavailable')


class ExactSegmentNormalPlanTests(unittest.TestCase):
    """Real VC binding/capture hooks over explicitly seeded isolated Saga proofs."""
    setUpClass = classmethod(segment_fixture.MultiSourceRefundTests.setUpClass.__func__)
    tearDownClass = classmethod(segment_fixture.MultiSourceRefundTests.tearDownClass.__func__)
    setUp = segment_fixture.MultiSourceRefundTests.setUp
    tearDown = segment_fixture.MultiSourceRefundTests.tearDown
    request = segment_fixture.MultiSourceRefundTests.request
    prepare = segment_fixture.MultiSourceRefundTests.prepare
    create = segment_fixture.MultiSourceRefundTests.create
    deliver = segment_fixture.MultiSourceRefundTests.deliver
    scenario = segment_fixture.MultiSourceRefundTests.scenario
    settlement = segment_fixture.MultiSourceRefundTests.settlement
    two_changes = segment_fixture.MultiSourceRefundTests.two_changes
    plans = segment_fixture.MultiSourceRefundTests.plans
    seed_completed = segment_fixture.MultiSourceRefundTests.seed_completed

    def test_exact_vc_segments_share_real_payment_without_duplicate_economic_basis(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            plan = self.plans(db)
            refund = self.seed_completed(db, plan)
            mappings = []
            for segment, sid in zip(plan['segments'], self.target_ids):
                baseline = db.get(m.settlement_finance['Baseline'], sid)
                mappings.append(dict(segment_id=segment['segment_id'], reference_settlement_id=sid, baseline_hash=baseline.snapshot_hash))
                for line in db.scalars(select(m.SettlementLine).where(m.SettlementLine.settlement_id == sid)):
                    db.get(m.SettlementBatch, line.batch_id).status = 'confirmed'
            db.flush()
            result = m.settlement_funded_refund['raw_basis'](db, refund, mappings)
            self.assertEqual(result['status'], 'ready', result)
            self.assertEqual(result['kind'], 'exact_segment_refund_normal_plan')
            left, right = result['references']
            self.assertTrue(set(left['funding']['payment_ids']).intersection(right['funding']['payment_ids']))
            self.assertNotEqual(left['funding']['economic_basis']['key'], right['funding']['economic_basis']['key'])
            self.assertEqual((result['buyer_cash_amount'], result['gross_amount']), ('20.00', '-20.00'))
            self.assertEqual(result['absorption_reversal'], '0.00')
            self.assertEqual(m.settlement_funded_refund['basis'](db, refund, mappings)['reason'], 'funded_refund_not_a_funded_source')
            forged = [dict(mappings[0], baseline_hash='0' * 64), mappings[1]]
            self.assertEqual(m.settlement_funded_refund['raw_basis'](db, refund, forged)['status'], 'waiting')
            caps = {}
            for ref in result['references']:
                basis = ref['funding']['economic_basis']
                self.assertEqual(ref['funding']['payment_ids'], sorted(basis['payment_caps']))
                for payment_id, amount in basis['payment_caps'].items():
                    caps[payment_id] = max(caps.get(payment_id, Decimal(0)), Decimal(amount))
            for payment in db.scalars(select(m.Payment).where(m.Payment.order_id == refund.order_id,
                    m.Payment.status == 'paid')):
                caps[payment.id] = max(caps.get(payment.id, Decimal(0)), Decimal(payment.amount))
            unique_cash = sum(caps.values())
            self.assertLess(unique_cash, sum(Decimal(v) for ref in result['references']
                for v in ref['funding']['economic_basis']['payment_caps'].values()))
            db.add(m.Refund(id='other-completed-cash', refund_no='OTHER-COMPLETED-CASH',
                order_id=refund.order_id, payment_id=refund.payment_id,
                amount=unique_cash - refund.amount + Decimal('.01'), status='completed', completed_at=m.now()))
            db.flush()
            capped = m.settlement_funded_refund['raw_basis'](db, refund, mappings)
            self.assertEqual(capped['reason'], 'funded_refund_buyer_cash_cap_exceeded', capped)
