"""Approved retained-cost economics and offset timing on disposable SQLite."""
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import Column, String, Numeric, select, func

import test_settlement_finance as fixture
from app.settlement_parties import install as install_parties


class CostApplicationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FinanceTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.db, self.ns, self.h = self.f.db, self.f.ns, self.f.h
        Base = self.ns['Base']

        class User(Base):
            __tablename__ = 'users'
            id = Column(String, primary_key=True)

        class Product(Base):
            __tablename__ = 'products'
            id = Column(String, primary_key=True)
            enterprise_id = Column(String)

        class Refund(Base):
            __tablename__ = 'refunds'
            id = Column(String, primary_key=True)
            order_id = Column(String)
            status = Column(String)
            amount = Column(Numeric(14, 2))

        self.ns.update(User=User, Product=Product, Refund=Refund)
        self.parties = install_parties(self.ns)
        Base.metadata.create_all(self.f.engine)
        self.provider = SimpleNamespace(id='provider-admin', platform_role='', is_active=True,
                                        activation_status='active', verified_status='verified')
        self.request = SimpleNamespace(headers={'X-Enterprise-Id': 'provider'}, query_params={})
        self.db.add(self.ns['Membership'](id='provider-admin', user_id='provider-admin', enterprise_id='provider',
            role='enterprise_admin', status='active', business_roles='provider'))
        self.db.commit()
        self.f.capture()

    def approve(self, refund_id='r', amount='30', retained='5', kind='expert'):
        refund = self.ns['Refund'](id=refund_id, order_id='o', status='completed', amount=Decimal(amount))
        self.db.add(refund)
        self.db.commit()
        bearer = self.parties['create_participant'](self.db, self.f.user,
            dict(request_id='party-' + refund_id, name='Approved bearer ' + refund_id,
                 enterprise_id='provider', kind=kind, currency='CNY'))
        exception = self.parties['create_exception'](self.db, self.f.user,
            dict(request_id='exception-' + refund_id, refund_id=refund_id, retained_cost=retained,
                 reason='Performed work cannot be returned', evidence_refs=['invoice:' + refund_id],
                 bearer_participant_id=bearer['id']))
        approved = self.parties['decide'](self.db, self.provider, exception['id'],
            dict(request_id='approval-' + refund_id, decision='approve', reason='Reviewed invoice and bearer',
                 expected_version=1), self.request)
        self.db.commit()
        return refund, approved

    def basis(self, refund, cumulative):
        return self.h['refund_basis'](self.db, refund, dict(original_amount='150', cumulative_refund_amount=cumulative))

    def capture_refund(self, refund, result):
        settlement = self.f.settlement(identifier='negative-' + refund.id, factor=Decimal('-.1'), reference='s')
        settlement.refund_id = refund.id
        for field in ('gross_amount', 'cost_amount', 'profit_amount'):
            setattr(settlement, field, result[field])
        for field, value in result['shares'].items():
            setattr(settlement, field, value)
        self.db.flush()
        captured = self.h['capture'](self.db, settlement)
        self.db.commit()
        self.h['reconcile_offsets'](self.db, 'fixture')
        self.db.commit()
        return settlement, captured

    def test_partial_30_retains_5_with_explicit_bearer_loss_and_no_extra_provider_profit(self):
        refund, approved = self.approve()
        result = self.basis(refund, '30')
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual((result['gross_amount'], result['cost_amount'], result['profit_amount']), (-30, -15, -15))
        self.assertEqual(result['shares']['platform_fee'], -2)
        self.assertEqual(result['shares']['provider_share'], -8)
        self.assertEqual(result['shares']['expert_fee'], -5)
        self.assertEqual(sum(result['shares'].values()), result['profit_amount'])
        settlement, baseline = self.capture_refund(refund, result)
        positions = self.f.positions(settlement.id)
        loss = next(p for p in positions if p.kind == 'loss')
        self.assertEqual((loss.amount, loss.participant_id, loss.direction), (5, approved['bearer_participant_id'], 'receivable'))
        self.assertEqual(self.h['balance'](self.db, loss), 5)
        plan = json.loads(baseline.snapshot_json)['cost_exception_application']
        self.assertEqual(plan['evidence']['reviewer_id'], self.provider.id)
        self.assertEqual(plan['evidence']['evidence_refs'], ['invoice:r'])
        self.assertEqual(sum(p.amount for p in positions), 30)
        self.assertEqual(self.db.get(self.ns['Refund'], refund.id).amount, 30)
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)

    def test_full_refund_loss_can_exceed_original_bearer_share_cap(self):
        refund, approved = self.approve(amount='150')
        result = self.basis(refund, '150')
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual((result['cost_amount'], result['profit_amount']), (-95, -55))
        self.assertEqual(result['shares']['provider_share'], -40)
        self.assertEqual(result['shares']['expert_fee'], -5)
        settlement, baseline = self.capture_refund(refund, result)
        self.assertFalse(isinstance(baseline, dict), baseline)
        self.assertEqual(sum(p.amount for p in self.f.positions(settlement.id)), 150)
        cost = next(p for p in self.f.positions() if p.kind == 'cost')
        self.assertEqual(self.h['balance'](self.db, cost), 5)
        self.assertEqual(self.h['describe'](self.db, settlement.id)['funds_settled'], False)

    def test_multiple_partials_and_replays_do_not_duplicate_retention_or_loss(self):
        first, _ = self.approve(refund_id='r1', amount='30', retained='5')
        second, _ = self.approve(refund_id='r2', amount='120', retained='5')
        first_result = self.basis(first, '30')
        self.db.commit()
        self.assertEqual(self.basis(first, '30'), first_result)
        first_settlement, baseline = self.capture_refund(first, first_result)
        self.assertEqual(self.h['capture'](self.db, first_settlement).snapshot_hash, baseline.snapshot_hash)
        second_result = self.basis(second, '150')
        second_settlement, second_baseline = self.capture_refund(second, second_result)
        self.assertFalse(isinstance(second_baseline, dict), second_baseline)
        self.assertEqual(first_result['cost_amount'] + second_result['cost_amount'], -90)
        self.assertEqual(first_result['profit_amount'] + second_result['profit_amount'], -60)
        self.assertEqual(first_result['shares']['provider_share'] + second_result['shares']['provider_share'], -40)
        loss_total = self.db.scalar(select(func.sum(self.h['Position'].amount)).where(self.h['Position'].kind == 'loss'))
        self.assertEqual(loss_total, 10)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.h['CostExceptionApplication'])), 2)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.h['CostExceptionPosting'])), 2)
        duplicate = self.f.settlement(identifier='duplicate', factor=Decimal('-.1'), reference='s')
        duplicate.refund_id = first.id
        for field in ('gross_amount', 'cost_amount', 'profit_amount'):
            setattr(duplicate, field, first_result[field])
        for field, value in first_result['shares'].items():
            setattr(duplicate, field, value)
        self.db.flush()
        self.assertEqual(self.h['capture'](self.db, duplicate)['reason'], 'cost_exception_already_posted')

    def test_bad_or_missing_evidence_and_audit_failure_wait_without_blocking_refund(self):
        refund, _ = self.approve(retained='25')  # Default reversible cost is only 20.
        result = self.basis(refund, '30')
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.h['CostExceptionApplication'])), 0)
        self.assertEqual(refund.status, 'completed')
        other, _ = self.approve(refund_id='r2')
        self.f.audit.side_effect = RuntimeError('audit unavailable')
        self.assertEqual(self.basis(other, '60')['status'], 'waiting')
        self.assertIsNone(self.db.get(self.h['CostExceptionApplication'], other.id))
        self.f.audit.side_effect = None
        ready = self.basis(other, '60')
        self.assertEqual(ready['status'], 'ready', ready)
        self.db.commit()
        old = self.db.get(self.h['CostExceptionApplication'], other.id)
        old.plan_hash = 'tampered'
        with self.assertRaises(ValueError):
            self.db.flush()
        self.db.rollback()

    def test_platform_loss_is_internal_and_provider_loss_matches_real_enterprise(self):
        refunds = [(self.approve(refund_id=kind, kind=kind)[0], kind) for kind in ('platform', 'provider')]
        for refund, kind in refunds:
            result = self.basis(refund, '30' if kind == 'platform' else '60')
            self.assertEqual(result['status'], 'ready', result)
            settlement, baseline = self.capture_refund(refund, result)
            loss = next(p for p in self.f.positions(settlement.id) if p.kind == 'loss')
            self.assertEqual(loss.direction, 'internal' if kind == 'platform' else 'receivable')
            self.assertEqual(loss.participant_id, 'platform' if kind == 'platform' else 'provider')

    def test_same_batch_pending_offsets_after_confirmation_and_payout_cannot_bypass(self):
        batch = self.db.get(self.ns['SettlementBatch'], 'fixture')
        batch.status = 'pending_confirm'
        self.db.commit()
        refund, _ = self.approve(kind='provider')
        result = self.basis(refund, '30')
        settlement, baseline = self.capture_refund(refund, result)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.h['Allocation'])), 0)
        self.assertEqual(self.h['reconcile_offsets'](self.db, 'fixture')['status'], 'waiting')
        batch.status = 'confirmed'
        self.db.commit()
        payables = [p for p in self.f.positions() if p.direction == 'payable']
        with self.assertRaises(HTTPException) as error:
            self.f.payout(payables)
        self.assertEqual(error.exception.detail, 'matching_recovery_requires_offset')
        offsets = self.h['reconcile_offsets'](self.db, 'fixture', 'finance')
        self.assertEqual(offsets['amounts']['CNY'], '28.00')
        self.db.commit()
        self.assertEqual(self.h['reconcile_offsets'](self.db, 'fixture')['offset_count'], 0)
        self.assertEqual(sum(self.h['balance'](self.db, p) for p in payables), 112)
        tx, _ = self.f.payout(payables, key='net', amounts=[self.h['balance'](self.db, p) for p in payables])
        self.f.confirm(tx)
        self.assertEqual(sum(self.h['balance'](self.db, p) for p in payables), 0)

    def test_capture_never_offsets_and_confirm_rechecks_new_matching_recovery(self):
        payables = [p for p in self.f.positions() if p.direction == 'payable']
        tx, _ = self.f.payout(payables)
        self.db.commit()
        refund, _ = self.approve(kind='provider')
        result = self.basis(refund, '30')
        settlement = self.f.settlement(identifier='negative', factor=Decimal('-.2'), reference='s')
        settlement.refund_id = refund.id
        for field in ('gross_amount', 'cost_amount', 'profit_amount'):
            setattr(settlement, field, result[field])
        for field, value in result['shares'].items():
            setattr(settlement, field, value)
        self.db.flush()
        self.h['capture'](self.db, settlement)
        self.db.commit()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.h['Allocation'])), 0)
        with self.assertRaises(HTTPException) as error:
            self.f.confirm(tx)
        self.assertEqual(error.exception.detail, 'matching_recovery_requires_offset')
        self.assertIsNone(self.db.get(self.h['Confirmation'], tx['id']))
        self.assertEqual(sum(self.h['balance'](self.db, p) for p in payables), 140)

    def test_exception_then_default_partial_does_not_reverse_retained_cost_again(self):
        first, _ = self.approve(amount='30', retained='5')
        first_result = self.basis(first, '30')
        self.capture_refund(first, first_result)
        second = self.ns['Refund'](id='default', order_id='o', amount=120, status='completed')
        self.db.add(second)
        self.db.commit()
        result = self.basis(second, '150')
        self.assertEqual(result['status'], 'ready', result)
        settlement, baseline = self.capture_refund(second, result)
        self.assertFalse(isinstance(baseline, dict), baseline)
        self.assertEqual(result['cost_amount'], -80)
        self.assertEqual(sum(p.amount for p in self.f.positions(settlement.id) if p.kind == 'loss'), 0)
        self.assertEqual(self.db.scalar(select(func.sum(self.h['Position'].amount)).where(self.h['Position'].kind == 'loss')), 5)

    def test_two_segment_targets_use_local_net_and_wait_for_all_old_obligations(self):
        import hashlib
        from app.settlement_finance import canonical, SHARES
        contexts = {}
        self.ns['version_change_settlement'] = dict(financial_context=lambda db, settlement: contexts[settlement.id])
        funding = dict(change_id='change', net_amount='75.00', basis='verified_net_change_cash_not_target_gross',
            legs=[dict(kind='payment', payment_id='verified-change-payment', amount='75.00')],
            segments=[dict(order_id='o', term_id='term-w1', old_amount='-75', target_amount='100', segment_net_amount='25.00'),
                      dict(order_id='o2', term_id='term-w2', old_amount='-75', target_amount='125', segment_net_amount='50.00')])
        negatives, targets = [], []
        # Distinct term orders and receipts, without reading the current catalog.
        self.db.add(self.ns['Order'](id='o2', order_no='ORD-2', provider_enterprise_id='provider',
            economic_snapshot_json='{"currency":"CNY"}'))
        self.db.add(self.ns['Payment'](id='pay2', order_id='o2', payment_no='PAY-2', status='paid', amount=150))
        second_source = self.f.settlement(identifier='s2')
        second_source.order_id = 'o2'
        self.db.flush()
        self.h['capture'](self.db, second_source)
        self.db.commit()
        for i, (sid, oid, window, net, target_cost, target_profit) in enumerate((
                ('s', 'o', 'w1', 25, 60, 40), ('s2', 'o2', 'w2', 50, 80, 45))):
            b = self.db.get(self.h['Baseline'], sid)
            for side in ('old', 'target'):
                settlement = self.f.settlement(identifier=side + str(i))
                settlement.order_id = oid
                settlement.reference_settlement_id = sid if side == 'old' else ''
                revenue, cost, profit = (-75, -50, -25) if side == 'old' else (75+net, target_cost, target_profit)
                values = dict(gross_amount=str(revenue), cost_amount=str(cost), profit_amount=str(profit),
                    shares={k: '0' for k in SHARES})
                values['shares'].update(platform_fee='-5' if side == 'old' else str(Decimal(profit)/5),
                                        provider_share='-20' if side == 'old' else str(Decimal(profit)*4/5))
                for field in ('gross_amount', 'cost_amount', 'profit_amount'):
                    setattr(settlement, field, Decimal(values[field]))
                for field, value in values['shares'].items():
                    setattr(settlement, field, Decimal(value))
                reversal = dict(gross_amount='-75', cost_amount='-50', profit_amount='-25',
                    shares=dict(platform_fee='-5', provider_share='-20', service_share='0', expert_fee='0', channel_fee='0'))
                context = dict(status='ready', kind='version_change', schema_version=1, settlement_id=settlement.id,
                    order_id=oid, provider_enterprise_id='provider', currency='CNY', side=side, change_id='change',
                    window_id=window, term_id='term-' + window, source_settlement_id=sid, source_baseline_hash=b.snapshot_hash,
                    source_reversal=reversal, segment_net_amount=str(net), funding=funding,
                    receipt_hash='verified-receipt', economic_snapshot={'currency': 'CNY'}, values=values)
                context['context_hash'] = hashlib.sha256(canonical({k: v for k, v in context.items() if k != 'status'}).encode()).hexdigest()
                contexts[settlement.id] = context
                self.db.flush()
                bad_source = dict(context, source_baseline_hash='unverified-source-hash')
                bad_source['context_hash'] = hashlib.sha256(canonical({k: v for k, v in bad_source.items()
                    if k not in ('status', 'context_hash')}).encode()).hexdigest()
                contexts[settlement.id] = bad_source
                self.assertEqual(self.h['capture_segment'](self.db, settlement, bad_source)['status'], 'waiting')
                self.assertIsNone(self.db.get(self.h['Baseline'], settlement.id))
                if side == 'target':
                    bad_net = dict(context, segment_net_amount=funding['net_amount'])
                    bad_net['context_hash'] = hashlib.sha256(canonical({k: v for k, v in bad_net.items()
                        if k not in ('status', 'context_hash')}).encode()).hexdigest()
                    contexts[settlement.id] = bad_net
                    self.assertEqual(self.h['capture_segment'](self.db, settlement, bad_net)['status'], 'waiting')
                    self.assertIsNone(self.db.get(self.h['Baseline'], settlement.id))
                contexts[settlement.id] = context
                captured = self.h['capture_segment'](self.db, settlement, context)
                self.assertFalse(isinstance(captured, dict), captured)
                self.db.commit()
                (negatives if side == 'old' else targets).append(settlement)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.h['SegmentFundingLink'])), 2)
        self.assertEqual(sum(self.db.get(self.h['Baseline'], t.id).revenue for t in targets), 225)
        for target in targets:
            p = next(p for p in self.f.positions(target.id) if p.kind == 'cost')
            with self.assertRaises(HTTPException):
                self.f.payout([p], key='gross-' + target.id)
        self.h['reconcile_offsets'](self.db, 'fixture')
        self.db.commit()
        for target in targets:
            p = next(p for p in self.f.positions(target.id) if p.kind == 'cost')
            tx, _ = self.f.payout([p], key='net-' + target.id)
            self.f.confirm(tx, key='confirm-' + target.id)
        duplicate = dict(contexts[targets[0].id], segment_net_amount='75.00')
        contexts[targets[0].id] = duplicate
        self.assertEqual(self.h['capture_segment'](self.db, targets[0], duplicate)['status'], 'waiting')


if __name__ == '__main__':
    unittest.main()
