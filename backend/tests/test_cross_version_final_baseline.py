"""Real monthly/lock HTTP routes and finance-core segment/refund captures.

Posted Saga proofs and completed refunds are isolated seeded evidence, not
external execution. Some arithmetic tests seed final-baseline fixtures; HTTP
tests capture actual core baselines, including current/future renewal terms.
No test claims buyer-refund Saga, gateway, bank or live-data acceptance.
"""
import copy
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select, update

from app.refund_economic_segments import EconomicSegment, plan_economic_segment
from app.refund_execution import canonical, digest
import test_version_change_quotes as fixture


class CrossVersionFinalBaselineTests(unittest.TestCase):
    setUpClass = classmethod(fixture.VersionChangeQuoteTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.VersionChangeQuoteTests.tearDownClass.__func__)
    setUp = fixture.VersionChangeQuoteTests.setUp
    tearDown = fixture.VersionChangeQuoteTests.tearDown
    prepare = fixture.VersionChangeQuoteTests.prepare
    create = fixture.VersionChangeQuoteTests.create
    deliver = fixture.VersionChangeQuoteTests.deliver
    request = fixture.VersionChangeQuoteTests.request

    def scenario(self, kind='upgrade', future=False):
        oid, _ = self.prepare(months=1, future=future)
        m = self.m
        with m.SessionLocal() as db:
            if kind == 'downgrade':
                target = db.get(m.ProductReleaseVersion, 'v2')
                target.price, target.cost = 60, 20
                db.flush()
            prepared = m.version_change_quotes['prepare'](db, db.get(m.User, 'buyer'), oid, 'v2', False, m.now())
            internal = prepared['internal'][0]
            term = db.get(m.subscription_terms['Term'], internal['term_id'])
            self.segment_orders = [s['order_id'] for s in prepared['internal']]
            for i, segment in enumerate(prepared['internal']):
                original = m.Settlement(id='original' if i == 0 else 'original-future', settlement_no=f'SET-FINAL-{i}',
                    order_id=segment['order_id'], status='locked', gross_amount=100, net_amount=100, cost_amount=40,
                    profit_amount=60, platform_fee=12, provider_share=48, service_share=0, expert_fee=0, channel_fee=0)
                db.add(original)
                source_payment = db.scalar(select(m.Payment).where(m.Payment.order_id == segment['order_id']))
                source_payment.status, source_payment.paid_at = 'paid', m.now() - timedelta(days=10)
                db.flush()
                root = m.settlement_finance['capture'](db, original, 'fixture-approved')
                if i == 0:
                    self.root_hash = root.snapshot_hash
            payment = db.scalar(select(m.Payment).where(m.Payment.order_id == oid))
            proof = dict(request_change_id='change', resource_change_id='change', evidence_fingerprint='f' * 64)
            change = m.VersionChangeRequest(id='change', source_order_id=oid, enterprise_id='buyer_tenant',
                combination_id=term.combination_id, target_version_id='v2', target_version_code='v2',
                old_version_id='version', old_version_code='v1', kind=kind, status='completed',
                idempotency_key='seed-change', request_hash='a' * 64,
                quote_json=canonical(dict(net_difference=str(prepared['public']['net_difference']))),
                prepared_json=canonical(prepared['internal']), target_snapshots_json=canonical(prepared['snapshots_by_term']),
                resource_plan_json='{}', evidence_json=canonical(proof), resource_applied=True,
                created_by='buyer', completed_at=m.now())
            db.add(change)
            db.flush()
            net = sum(Decimal(s['target']['revenue']) + Decimal(s['old']['revenue']) for s in prepared['internal'])
            if net > 0:
                funding = m.Order(id='funding', order_no='ORD-FUNDING', buyer_user_id='buyer', buyer_enterprise_id='buyer_tenant',
                    provider_enterprise_id='provider_tenant', product_id='product', business_type='version_change_payment',
                    amount=net, paid_amount=net, payment_status='paid', buyer_name='Buyer', product_name='Net funding')
                db.add(funding)
                db.flush()
                db.add(m.Payment(id='funding-payment', order_id=funding.id, payment_no='PAY-CHANGE', status='paid',
                                 amount=net, paid_at=m.now()))
                change.funding_order_id = funding.id
                db.flush()
                db.add(m.VersionChangeFund(change_id='change', kind='payment', payment_id='funding-payment',
                                           amount=net, idempotency_key='change-payment'))
            else:
                refund = m.Refund(id='downgrade', refund_no='REF-DOWNGRADE', order_id=oid, payment_id=payment.id,
                                  amount=-net, status='completed', completed_at=m.now())
                db.add(refund)
                db.flush()
                db.add(m.VersionChangeFund(change_id='change', kind='refund', payment_id=payment.id,
                    refund_id=refund.id, amount=-net, idempotency_key='change-refund'))
            target_policy = dict(scope=term.combination_id, target_version_id='v2')
            receipt_raw = canonical(dict(scope=term.combination_id, change_id='change', direction='target',
                source_run_id='a' * 40, boundary_id='1-0', target_fingerprint=digest(target_policy),
                periods=[dict(order_id=s['order_id'], segment_id=s['term_id'], available_quota=100)
                         for s in prepared['internal']]))
            db.add(m.ApiGatewayRoute(id='route', product_id='product', route_key='cross-final', upstream_url='https://example.invalid'))
            db.flush()
            db.add(m.ApiCredential(id='credential', route_id='route', enterprise_id='buyer_tenant', key_hash='x' * 64, order_id=oid))
            db.flush()
            receipt = m.refund_segment_store['Receipt'](id='receipt', change_id='change', kind='api', credential_id='credential',
                direction='target', receipt_json=receipt_raw, receipt_hash=hashlib.sha256(receipt_raw.encode()).hexdigest(),
                cutover_at=m.now(), source_run_id='a' * 40, boundary_id='1-0', target_fingerprint=digest(target_policy))
            db.add(receipt)
            db.flush()
            proof['evidence_fingerprint'] = digest(dict(change_id='change', direction='target', kind='api', evidence=dict(
                route_id='route', route_key='cross-final', credential_ids=['credential'], receipt_ids=['receipt'])))
            change.evidence_json = canonical(proof)
            change.resource_plan_json = canonical(dict(kind='api', target_route_id='route', target_route_key='cross-final',
                credentials=[dict(id='credential', status='active')],
                charge_boundary=dict(mode='strict', credential_id='credential', target=target_policy)))
            for i, segment in enumerate(prepared['internal']):
                term = db.get(m.subscription_terms['Term'], segment['term_id'])
                revision = m.subscription_terms['Revision'](id='revision' if i == 0 else 'revision-future',
                    change_id=change.id, term_id=term.id, revision_number=1, version_id='v2', version_code='v2',
                    economic_snapshot_json=canonical(prepared['snapshots_by_term'][term.id]['economic']),
                    delivery_snapshot_json=canonical(prepared['snapshots_by_term'][term.id]['delivery']))
                db.add(revision)
                for side in ('old', 'target'):
                    values = segment[side]
                    db.add(m.VersionChangeAdjustment(id=side if i == 0 else side + '-future', change_id='change',
                        request_change_id='change', source_order_id=segment['order_id'], combination_id=term.combination_id,
                        side=side, revenue=Decimal(values['revenue']), cost=Decimal(values['cost']), profit=Decimal(values['profit']),
                        currency='CNY', original_version_id='version', target_version_id='v2', rule_json=canonical(values['rule']),
                        effective_at=m.now(), status='posted', posted_at=m.now(), payload_fingerprint=prepared['digest'],
                        resource_change_id='change', resource_evidence_fingerprint=proof['evidence_fingerprint']))
                db.flush()
                target = segment['target']
                payload = dict(order_id=segment['order_id'], term_id=term.id, change_id='change', receipt_id=receipt.id,
                    revision_id=revision.id, revenue=Decimal(target['revenue']), cost=Decimal(target['cost']), currency='CNY',
                    starts_at=max(m.now(), term.starts_at), ends_at=term.ends_at, available_quota=100,
                    economic_snapshot_json=revision.economic_snapshot_json, delivery_snapshot_json=revision.delivery_snapshot_json,
                    evidence_json=canonical(dict(proof=proof, receipt_hash=receipt.receipt_hash,
                        economic_hash=digest(json.loads(revision.economic_snapshot_json)), delivery_hash=digest(json.loads(revision.delivery_snapshot_json)))))
                encoded = {k: v.isoformat() if hasattr(v, 'isoformat') else str(v) if isinstance(v, Decimal) else v for k, v in payload.items()}
                db.add(m.refund_segment_store['Window'](id='window' if i == 0 else 'window-future', **payload, payload_hash=digest(encoded)))
            db.commit()
        self.order_id = oid
        start = m.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
        batch = self.request('POST', '/api/settlement-batches', actor='finance', json=dict(
            cycle='monthly', period_start=start.isoformat(), period_end=end.isoformat(),
            idempotency_key='vc-monthly', order_ids=self.segment_orders))
        self.batch_id = batch['id']

    def settlement(self, db, side):
        h = self.m.version_change_settlement
        link = db.scalar(select(h['Model']).where(h['Model'].adjustment_id == side))
        return db.get(self.m.Settlement, link.settlement_id)

    def target_baseline_fixture(self, db, *, bind=True):
        """Seed the approved result required from Arendt's capture_segment contract."""
        m, h = self.m, self.m.version_change_settlement
        target = self.settlement(db, 'target')
        revenue = target.gross_amount
        target.cost_amount = Decimal('35') if revenue == 75 else Decimal('12')
        target.profit_amount = revenue - target.cost_amount
        target.platform_fee = target.profit_amount / 4
        target.provider_share = target.profit_amount - target.platform_fee
        target.service_share = target.expert_fee = target.channel_fee = 0
        target.status = 'locked'
        db.flush()
        context = h['financial_context'](db, target)
        self.assertEqual(context['status'], 'ready', context)
        snapshot = dict(shares=context['values']['shares'], economic_snapshot=context['economic_snapshot'],
                        segment_context_hash=context['context_hash'], funding=context['funding'])
        raw = canonical(snapshot)
        b = m.settlement_finance['Baseline'](settlement_id=target.id, order_id=self.order_id,
            provider_enterprise_id='provider_tenant', currency='CNY', revenue=target.gross_amount,
            cost=target.cost_amount, profit=target.profit_amount, reference_settlement_id='', snapshot_json=raw,
            snapshot_hash=hashlib.sha256(raw.encode()).hexdigest(), actor='fixture-approved-target')
        db.add(b)
        db.flush()
        if bind:
            h['bind_baseline'](db, target, 'fixture')
        return b, context

    def plan(self, db, amount, prior='0'):
        m = self.m
        w = db.get(m.refund_segment_store['Window'], 'window')
        segment = EconomicSegment(w.id, w.order_id, w.change_id, w.revenue, w.cost, 'api', w.available_quota,
            w.starts_at, w.ends_at, json.loads(w.economic_snapshot_json), json.loads(w.delivery_snapshot_json), ('window-proof',))
        plan = plan_economic_segment(segment, amount, at=w.starts_at, used_calls=0, prior_amount=prior,
            prior_recovery=int(Decimal(prior) * w.available_quota / w.revenue))
        plan.update(window_hash=w.payload_hash, receipt_hash=db.get(m.refund_segment_store['Receipt'], w.receipt_id).receipt_hash)
        return plan

    def test_upgrade_context_uses_final_source_and_only_net_funding(self):
        self.scenario()
        with self.m.SessionLocal() as db:
            h = self.m.version_change_settlement
            old = self.settlement(db, 'old')
            before = db.get(self.m.VersionChangeAdjustment, 'old').cost
            context = h['prepare_lock'](db, old, 'fixture')
            self.assertEqual(context['status'], 'ready', context)
            self.assertEqual(old.reference_settlement_id, 'original')
            self.assertEqual(old.cost_amount, -20)
            self.assertEqual(before, -15)
            self.assertEqual(db.get(self.m.VersionChangeAdjustment, 'old').cost, before)
            self.assertEqual(context['source_baseline_hash'], self.root_hash)
            self.assertEqual(context['funding']['net_amount'], '25.00')
            self.assertEqual(context['funding']['legs'][0]['amount'], '25.00')
            target = self.settlement(db, 'target')
            target.status = 'locked'
            db.flush()
            with patch.dict(self.m.settlement_finance, capture_segment=None):
                result = self.m.settlement_finance_adapter.capture_version_change(vars(self.m), db, target)
            self.assertEqual(result['reason'], 'segment_finance_capture_contract_required')
            self.assertIsNone(db.get(self.m.settlement_finance['Baseline'], target.id))

    def repeated_refunds(self, kind):
        self.scenario(kind)
        with self.m.SessionLocal() as db:
            baseline, context = self.target_baseline_fixture(db)
            old_proof = db.get(self.m.VersionChangeRequest, 'change').prepared_json
            amount = baseline.revenue / 3
            totals = dict(cost=Decimal(0), profit=Decimal(0), platform_fee=Decimal(0), provider_share=Decimal(0))
            for i in range(3):
                plan = self.plan(db, str(amount), str(amount * i))
                frozen = copy.deepcopy(plan)
                refund = SimpleNamespace(id=f'ordinary-{i}', order_id=self.order_id, amount=amount)
                result = self.m.refund_settlement['financial_basis'](db, refund, plan)
                self.assertEqual(result['status'], 'ready', result)
                self.assertEqual(result['reference_settlement_id'], baseline.settlement_id)
                self.assertEqual(result['segment_id'], 'window')
                self.assertEqual(result, self.m.refund_settlement['financial_basis'](db, refund, plan))
                totals['cost'] += Decimal(result['cost_amount'])
                totals['profit'] += Decimal(result['profit_amount'])
                for k in ('platform_fee', 'provider_share'):
                    totals[k] += Decimal(result['shares'][k])
                self.assertEqual(plan, frozen)
            self.assertEqual(totals['cost'], -baseline.cost)
            self.assertEqual(totals['profit'], -baseline.profit)
            self.assertEqual(totals['platform_fee'], -baseline.profit / 4)
            self.assertEqual(totals['provider_share'], -baseline.profit * 3 / 4)
            self.assertEqual(db.get(self.m.VersionChangeRequest, 'change').prepared_json, old_proof)
            self.assertEqual(context['funding']['net_amount'], '25.00' if kind == 'upgrade' else '-20.00')
            self.assertEqual(db.query(self.m.settlement_finance['Position']).filter_by(settlement_id=baseline.settlement_id).count(), 0)

    def test_upgrade_repeated_refunds_use_exact_target_final_cost(self):
        self.repeated_refunds('upgrade')

    def test_downgrade_repeated_refunds_keep_net_refund_separate(self):
        self.repeated_refunds('downgrade')

    def test_missing_binding_never_uses_original_whole_order_baseline(self):
        self.scenario()
        with self.m.SessionLocal() as db:
            b, _ = self.target_baseline_fixture(db, bind=False)
            refund = SimpleNamespace(id='ordinary', order_id=self.order_id, amount=Decimal(25))
            result = self.m.refund_settlement['financial_basis'](db, refund, self.plan(db, '25'))
            self.assertEqual(result['status'], 'waiting')
            self.assertEqual(result['reason'], 'exact_refund_segment_final_binding_required')

    def test_cross_scope_window_hash_and_missing_cash_fail_closed(self):
        self.scenario()
        with self.m.SessionLocal() as db:
            self.target_baseline_fixture(db)
            plan = self.plan(db, '25')
            for change in (dict(window_hash='bad'), dict(segment_id='unknown'), dict(change_id='different')):
                result = self.m.version_change_settlement['refund_basis'](db,
                    SimpleNamespace(id='refund', order_id=self.order_id, amount=Decimal(25)), dict(plan, **change))
                self.assertNotEqual(result['status'], 'ready')
            db.get(self.m.Payment, 'funding-payment').status = 'unpaid'
            db.flush()
            context = self.m.version_change_settlement['financial_context'](db, self.settlement(db, 'target'))
            self.assertEqual(context['reason'], 'version_change_payment_proof_mismatch')

    def test_binding_is_immutable_and_protects_source_rebuild(self):
        self.scenario()
        with self.m.SessionLocal() as db:
            self.target_baseline_fixture(db)
            with self.assertRaises(HTTPException):
                self.m.settlement_finance_adapter.protect_rebuild(vars(self.m), db, ['original'])
            link = db.get(self.m.version_change_settlement['SegmentBaselineLink'], 'window')
            link.baseline_hash = 'changed'
            with self.assertRaises(ValueError):
                db.flush()

    def http_lock(self, kind):
        self.scenario(kind)
        with self.m.SessionLocal() as db:
            old_id, target_id = (self.settlement(db, side).id for side in ('old', 'target'))
            frozen = db.get(self.m.VersionChangeRequest, 'change').prepared_json
        old = self.request('POST', f'/api/settlements/{old_id}/lock', actor='finance')
        self.assertEqual(old['status'], 'locked')
        self.assertIsNone(old['financial_capture'])
        target = self.request('POST', f'/api/settlements/{target_id}/lock', actor='finance')
        self.assertEqual(target['batch_status'], 'pending_confirm')
        self.assertIsNone(target['financial_capture'])
        self.request('POST', f'/api/settlement-batches/{self.batch_id}/confirm', actor='finance', json={})
        self.request('POST', f'/api/settlement-batches/{self.batch_id}/pay', actor='finance', expected=409, json={})
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Settlement, old_id).reference_settlement_id, 'original')
            self.assertEqual(db.get(self.m.Settlement, old_id).cost_amount, -20)
            self.assertEqual(db.get(self.m.VersionChangeAdjustment, 'old').cost, -15)
            self.assertEqual(db.get(self.m.VersionChangeRequest, 'change').prepared_json, frozen)
            target_baseline = db.get(self.m.settlement_finance['Baseline'], target_id)
            self.assertIsNotNone(target_baseline)
            self.assertIsNotNone(db.get(self.m.settlement_finance['Baseline'], old_id))
            self.assertEqual(json.loads(target_baseline.snapshot_json)['segment_context']['funding']['net_amount'],
                             '25.00' if kind == 'upgrade' else '-20.00')
            self.assertIsNotNone(db.get(self.m.version_change_settlement['SegmentBaselineLink'], 'window'))

    def test_upgrade_monthly_http_projection_and_financial_capture(self):
        self.http_lock('upgrade')

    def test_downgrade_monthly_http_projection_and_financial_capture(self):
        self.http_lock('downgrade')

    def multiple_terms(self, kind):
        self.scenario(kind, future=True)
        with self.m.SessionLocal() as db:
            ids = {side: self.settlement(db, side).id for side in ('old', 'target', 'old-future', 'target-future')}
            contexts = [self.m.version_change_settlement['financial_context'](db, self.settlement(db, side))
                        for side in ('target', 'target-future')]
            self.assertTrue(all(c['status'] == 'ready' for c in contexts), contexts)
            total = Decimal('75') if kind == 'upgrade' else Decimal('-60')
            local = [Decimal('25'), Decimal('50')] if kind == 'upgrade' else [Decimal('-20'), Decimal('-40')]
            self.assertEqual([Decimal(c['segment_net_amount']) for c in contexts], local)
            for context in contexts:
                funding = context['funding']
                self.assertEqual(Decimal(funding['change_total_net_amount']), total)
                self.assertEqual(sum(Decimal(a['net_amount']) for a in funding['segment_allocations']), total)
                self.assertEqual(len(funding['segment_allocations']), 2)
                self.assertEqual(len(funding['legs']), 1)
        for side in ('old', 'old-future', 'target', 'target-future'):
            result = self.request('POST', f'/api/settlements/{ids[side]}/lock', actor='finance')
            self.assertIsNone(result['financial_capture'], result)
        self.request('POST', f'/api/settlement-batches/{self.batch_id}/confirm', actor='finance', json={})
        with self.m.SessionLocal() as db:
            h = self.m.settlement_finance
            captures = [db.get(h['Baseline'], ids[side]) for side in ('target', 'target-future')]
            self.assertEqual(sum(b.revenue for b in captures), Decimal(225) if kind == 'upgrade' else Decimal(90))
            transfers = db.scalars(select(h['SegmentFundingLink'])).all()
            self.assertEqual({t.source_negative_id for t in transfers}, {ids['old'], ids['old-future']})
            self.assertEqual(len({t.target_settlement_id for t in transfers}), 2)
            self.assertEqual(len(db.scalars(select(self.m.VersionChangeFund)).all()), 1)
            bindings = db.scalars(select(self.m.version_change_settlement['SegmentBaselineLink'])).all()
            self.assertEqual({b.window_id for b in bindings}, {'window', 'window-future'})

    def test_upgrade_current_and_future_terms_share_one_net_cash_proof(self):
        self.multiple_terms('upgrade')

    def test_downgrade_current_and_future_terms_share_one_net_refund_proof(self):
        self.multiple_terms('downgrade')

    def test_target_locked_first_is_captured_when_exact_old_leg_locks(self):
        self.scenario()
        with self.m.SessionLocal() as db:
            old_id, target_id = (self.settlement(db, side).id for side in ('old', 'target'))
        waiting = self.request('POST', f'/api/settlements/{target_id}/lock', actor='finance')
        self.assertEqual(waiting['financial_capture']['reason'], 'exact_old_segment_capture_required')
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.get(self.m.settlement_finance['Baseline'], target_id))
        self.request('POST', f'/api/settlements/{old_id}/lock', actor='finance')
        with self.m.SessionLocal() as db:
            self.assertIsNotNone(db.get(self.m.settlement_finance['Baseline'], target_id))
            self.assertIsNotNone(db.get(self.m.version_change_settlement['SegmentBaselineLink'], 'window'))

    def test_cross_version_incomplete_cost_exception_evidence_stays_waiting(self):
        self.scenario()
        with self.m.SessionLocal() as db:
            self.target_baseline_fixture(db)
            plan = self.plan(db, '25')
            with patch.dict(self.m.settlement_parties, cost_exception_basis=lambda *_: dict(
                    status='waiting', reason='approved_cost_exception_evidence_incomplete')):
                result = self.m.refund_settlement['financial_basis'](db,
                    SimpleNamespace(id='ordinary', order_id=self.order_id, amount=Decimal(25)), plan)
            self.assertEqual(result['status'], 'waiting')

    def completed_segment_refund(self, *, waiting=False, kind='upgrade'):
        self.scenario(kind)
        m = self.m
        with m.SessionLocal() as db:
            target_id, old_id = (self.settlement(db, side).id for side in ('target', 'old'))
        self.request('POST', f'/api/settlements/{old_id}/lock', actor='finance')
        self.request('POST', f'/api/settlements/{target_id}/lock', actor='finance')
        with m.SessionLocal() as db:
            plan = self.plan(db, '25')
            payment = db.scalar(select(m.Payment).where(m.Payment.order_id == self.order_id))
            at = m.now()
            refund = m.Refund(id='ordinary-cost', refund_no='REF-ORDINARY-COST', order_id=self.order_id,
                payment_id=payment.id, amount=25, status='completed', completed_at=at)
            db.add(refund)
            db.flush()
            envelope = dict(refund_id=refund.id, order_id=self.order_id, payment_id=payment.id,
                amount='25.00', economic_plan=plan)
            funding = [dict(payment_id=payment.id, source_order_id=self.order_id, amount='25.00', currency='CNY')]
            resource = dict(kind='api', refund_id=refund.id, order_id=self.order_id, frozen_envelope_hash=digest(envelope))
            resource['plan_hash'] = digest(resource)
            proof = dict(refund_id=refund.id, resource_plan_hash=digest(resource))
            execution = m.refund_execution['Execution'](id='ordinary-execution', refund_id=refund.id,
                status='completed', resume_status='completed', plan_hash=digest(envelope),
                resource_plan_json=canonical(resource), resource_plan_hash=digest(resource),
                proof_json=canonical(proof), funding_json=canonical(funding), requested_by='buyer', completed_at=at)
            db.add(execution)
            db.flush()
            window = db.get(m.refund_segment_store['Window'], 'window')
            term = db.get(m.subscription_terms['Term'], window.term_id)
            db.add(m.refund_execution_resources['Recovery'](refund_id=refund.id, execution_id=execution.id,
                order_id=self.order_id, term_id=term.id, combination_id=term.combination_id,
                generation=1, recovery_number=1, plan_json=canonical(resource), proof_json=canonical(proof),
                payload_hash=digest(dict(plan=resource, proof=proof)), stage='resources_applied'))
            db.add(m.refund_execution['Completion'](refund_id=refund.id, execution_id=execution.id,
                plan_hash=digest(envelope), proof_hash=digest(proof), funding_hash=digest(funding),
                economic_plan_json=canonical(plan), amount=25, completed_at=at))
            db.add(m.refund_execution['PaymentLeg'](refund_id=refund.id, payment_id=payment.id,
                source_order_id=self.order_id, amount=25, currency='CNY', status='completed',
                receipt=f'simulated:{refund.id}:{payment.id}', completed_at=at))
            db.add(m.refund_workflow['Case'](refund_id=refund.id, source='ordinary', requested_by_id='buyer',
                plan_json=canonical(envelope), plan_hash=digest(envelope)))
            db.flush()
            if waiting:
                with patch.dict(m.version_change_settlement, refund_basis=lambda *_: dict(status='waiting', reason='pending')):
                    negative = m.refund_settlement['negative_settlement'](db, refund)
            else:
                negative = m.refund_settlement['negative_settlement'](db, refund)
            self.negative_id, self.target_id = negative.id, target_id
            self.frozen_completion = canonical(plan), digest(proof), digest(funding)
            db.commit()
        bearer = self.request('POST', '/api/settlement-parties', actor='platform', json=dict(
            request_id='segment-bearer', name='Provider loss bearer', kind='provider',
            enterprise_id='provider_tenant', currency='CNY'))
        self.exception_body = dict(request_id='segment-exception', refund_id='ordinary-cost', retained_cost='5.00',
            reason='Documented retained segment work', evidence_refs=['segment-invoice:1'], bearer_participant_id=bearer['id'])

    def approve_segment_exception(self):
        e = self.request('POST', '/api/settlement-cost-exceptions', actor='platform', json=self.exception_body)
        self.assertEqual(e['baseline_id'], self.target_id)
        body = dict(request_id='segment-decision', expected_version=1, decision='approve', reason='Agreed actual retained cost')
        self.request('POST', f'/api/settlement-cost-exceptions/{e["id"]}/decision', actor='platform', expected=403, json=body)
        approved = self.request('POST', f'/api/settlement-cost-exceptions/{e["id"]}/decision', actor='provider', json=body)
        self.assertEqual(approved, self.request('POST', f'/api/settlement-cost-exceptions/{e["id"]}/decision',
                                              actor='provider', json=body))
        return e

    def assert_segment_exception_lock(self):
        self.request('POST', f'/api/settlements/{self.negative_id}/lock', actor='finance')
        m = self.m
        with m.SessionLocal() as db:
            h = m.settlement_finance
            b = db.get(h['Baseline'], self.negative_id)
            app = db.get(h['CostExceptionApplication'], 'ordinary-cost')
            plan = json.loads(app.plan_json)
            ordinary = plan['default_basis']
            self.assertEqual(b.reference_settlement_id, self.target_id)
            self.assertEqual(b.cost, Decimal(ordinary['cost_amount']) + 5)
            self.assertEqual(b.profit, Decimal(ordinary['profit_amount']) - 5)
            loss = db.scalars(select(h['Position']).where(h['Position'].settlement_id == b.settlement_id,
                                                       h['Position'].kind == 'loss')).one()
            self.assertEqual((loss.amount, loss.participant_id, loss.direction), (5, 'provider_tenant', 'receivable'))
            self.assertEqual(db.get(h['CostExceptionPosting'], 'ordinary-cost').settlement_id, b.settlement_id)
            completion = db.get(m.refund_execution['Completion'], 'ordinary-cost')
            self.assertEqual((completion.economic_plan_json, completion.proof_hash, completion.funding_hash), self.frozen_completion)
            self.assertEqual(db.get(m.Refund, 'ordinary-cost').amount, 25)
            self.assertEqual(plan['default_basis']['segment_id'], 'window')
            app.plan_hash = 'changed'
            with self.assertRaises(ValueError):
                db.flush()

    def test_cross_version_cost_exception_http_approval_refresh_and_lock(self):
        self.completed_segment_refund()
        self.approve_segment_exception()
        self.assert_segment_exception_lock()

    def test_cross_version_cost_exception_can_resolve_stale_waiting_assessment(self):
        self.completed_segment_refund(waiting=True)
        self.approve_segment_exception()
        self.assert_segment_exception_lock()

    def test_downgrade_target_cost_exception_keeps_change_funding_separate(self):
        self.completed_segment_refund(kind='downgrade')
        self.approve_segment_exception()
        self.assert_segment_exception_lock()

    def test_cross_version_cost_exception_rejects_wrong_root_and_duplicate_negative(self):
        self.completed_segment_refund()
        m = self.m
        with m.SessionLocal() as db:
            db.get(m.Settlement, self.negative_id).reference_settlement_id = 'original'
            db.commit()
        self.request('POST', '/api/settlement-cost-exceptions', actor='platform', expected=409, json=self.exception_body)
        with m.SessionLocal() as db:
            db.get(m.Settlement, self.negative_id).reference_settlement_id = self.target_id
            model = m.refund_settlement['FinancialAssessment']
            previous = db.get(model, (self.negative_id, 1))
            wrong = dict(json.loads(previous.values_json), segment_id='another-window')
            db.add(model(settlement_id=self.negative_id, version=2, values_json=canonical(wrong), actor='fixture'))
            db.commit()
        self.request('POST', '/api/settlement-cost-exceptions', actor='platform', expected=409, json=self.exception_body)
        with m.SessionLocal() as db:
            original = db.get(model, (self.negative_id, 1))
            db.add(model(settlement_id=self.negative_id, version=3, values_json=original.values_json, actor='fixture'))
            db.add(m.Settlement(settlement_no='DUPLICATE-NEGATIVE', order_id=self.order_id,
                                refund_id='ordinary-cost', is_refund=True, gross_amount=-25))
            db.commit()
        self.request('POST', '/api/settlement-cost-exceptions', actor='platform', expected=409, json=self.exception_body)

    def test_cross_version_cost_exception_rejects_retention_above_exact_slice(self):
        self.completed_segment_refund()
        self.request('POST', '/api/settlement-cost-exceptions', actor='platform', expected=409,
                     json=dict(self.exception_body, retained_cost='20.00'))

    def test_cross_version_cost_exception_requires_completed_verified_refund(self):
        self.completed_segment_refund()
        with self.m.SessionLocal() as db:
            db.get(self.m.Refund, 'ordinary-cost').status = 'approved'
            db.commit()
        self.request('POST', '/api/settlement-cost-exceptions', actor='platform', expected=409, json=self.exception_body)
        with self.m.SessionLocal() as db:
            db.get(self.m.Refund, 'ordinary-cost').status = 'completed'
            # Inject storage corruption past ORM immutability in this private DB.
            model = self.m.refund_execution['Execution']
            db.execute(update(model).where(model.id == 'ordinary-execution').values(proof_json='{}'))
            db.commit()
        self.request('POST', '/api/settlement-cost-exceptions', actor='platform', expected=409, json=self.exception_body)

    def test_cross_version_cost_exception_approval_rejects_existing_financial_reversal(self):
        self.completed_segment_refund()
        e = self.request('POST', '/api/settlement-cost-exceptions', actor='platform', json=self.exception_body)
        self.request('POST', f'/api/settlements/{self.negative_id}/lock', actor='finance')
        self.request('POST', f'/api/settlement-cost-exceptions/{e["id"]}/decision', actor='provider', expected=409,
            json=dict(request_id='too-late', expected_version=1, decision='approve', reason='Must not overwrite posted finance'))

    def posted_repeated_refunds(self, kind):
        self.scenario(kind)
        m = self.m
        with m.SessionLocal() as db:
            target_id, old_id = (self.settlement(db, side).id for side in ('target', 'old'))
            revenue = self.settlement(db, 'target').gross_amount
            payment_id = db.scalar(select(m.Payment.id).where(m.Payment.order_id == self.order_id))
        cost = Decimal(35) if kind == 'upgrade' else Decimal(12)
        proposal = self.request('POST', f'/api/settlements/{target_id}/proposals', actor='finance', json=dict(
            gross_amount=str(revenue), cost_amount=str(cost), profit_amount=str(revenue-cost),
            platform_rate=25, provider_rate=75, service_rate=0, expert_rate=0, channel_rate=0,
            reason='Final target cost and exact recipients'))
        self.request('POST', f'/api/settlement-proposals/{proposal["id"]}/decision', actor='provider',
                     json=dict(decision='approve', comment='Agreed final target baseline'))
        self.request('POST', f'/api/settlements/{old_id}/lock', actor='finance')
        result = self.request('POST', f'/api/settlements/{target_id}/lock', actor='finance')
        self.assertIsNone(result['financial_capture'])
        with m.SessionLocal() as db:
            final = db.get(m.settlement_finance['Baseline'], target_id)
            self.assertEqual(final.cost, cost)
            prepared = db.get(m.VersionChangeRequest, 'change').prepared_json
            amount = revenue / 3
            for i in range(3):
                plan = self.plan(db, str(amount), str(amount * i))
                frozen = canonical(plan)
                refund = m.Refund(id=f'post-{i}', refund_no=f'REF-POST-{i}', order_id=self.order_id,
                    payment_id=payment_id, amount=amount, status='completed', completed_at=m.now())
                db.add(refund)
                db.flush()
                values = m.refund_settlement['financial_basis'](db, refund, plan)
                self.assertEqual(values['status'], 'ready', values)
                negative = m.Settlement(id=f'negative-{i}', settlement_no=f'REF-SET-{i}',
                    order_id=self.order_id, refund_id=refund.id, reference_settlement_id=values['reference_settlement_id'],
                    is_refund=True, status='locked', gross_amount=Decimal(values['gross_amount']),
                    net_amount=Decimal(values['gross_amount']), cost_amount=Decimal(values['cost_amount']),
                    profit_amount=Decimal(values['profit_amount']), **{k: Decimal(v) for k, v in values['shares'].items()})
                db.add(negative)
                db.flush()
                captured = m.settlement_finance['capture'](db, negative, 'fixture-financial-projection')
                self.assertFalse(isinstance(captured, dict), captured)
                self.assertEqual(captured.reference_settlement_id, target_id)
                self.assertEqual(canonical(plan), frozen)
            negatives = db.scalars(select(m.settlement_finance['Baseline']).where(
                m.settlement_finance['Baseline'].reference_settlement_id == target_id)).all()
            self.assertEqual(len(negatives), 3)
            self.assertEqual(sum(b.revenue for b in negatives), -revenue)
            self.assertEqual(sum(b.cost for b in negatives), -cost)
            self.assertEqual(sum(b.profit for b in negatives), -(revenue-cost))
            self.assertEqual(db.get(m.VersionChangeRequest, 'change').prepared_json, prepared)
            self.assertEqual(sum(r.amount for r in db.scalars(select(m.Refund).where(m.Refund.id.like('post-%')))), revenue)
            db.commit()

    def test_upgrade_final_http_agreement_then_repeated_financial_refund_postings(self):
        self.posted_repeated_refunds('upgrade')

    def test_downgrade_final_http_agreement_then_repeated_financial_refund_postings(self):
        self.posted_repeated_refunds('downgrade')

    def test_target_wrong_recipient_capture_rolls_back_all_financial_rows(self):
        self.scenario()
        m = self.m
        with m.SessionLocal() as db:
            old_id, target_id = (self.settlement(db, side).id for side in ('old', 'target'))
        self.request('POST', f'/api/settlements/{old_id}/lock', actor='finance')
        core = m.settlement_finance['capture_segment']

        def wrong_recipient(db, settlement, context, actor):
            result = core(db, settlement, context, actor)
            self.assertFalse(isinstance(result, dict), result)
            snapshot = json.loads(result.snapshot_json)
            next(e for e in snapshot['entries'] if e['share_kind'] == 'expert_fee')['participant_id'] = 'wrong-party'
            return SimpleNamespace(snapshot_json=canonical(snapshot))

        with patch.dict(m.settlement_finance, capture_segment=wrong_recipient):
            response = self.request('POST', f'/api/settlements/{target_id}/lock', actor='finance')
        self.assertEqual(response['financial_capture']['reason'], 'target_participant_snapshot_not_captured')
        with m.SessionLocal() as db:
            self.assertIsNone(db.get(m.settlement_finance['Baseline'], target_id))
            self.assertEqual(db.query(m.settlement_finance['Position']).filter_by(settlement_id=target_id).count(), 0)
            self.assertEqual(db.query(m.settlement_finance['SegmentFundingLink']).filter_by(target_settlement_id=target_id).count(), 0)


if __name__ == '__main__':
    unittest.main()
