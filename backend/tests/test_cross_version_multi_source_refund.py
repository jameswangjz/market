"""Multiple immutable VC sources for one refund; isolated seeded Saga proofs."""
import copy
from datetime import timedelta
from decimal import Decimal
import json
import inspect
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import func, select

from app.refund_economic_segments import EconomicSegment, plan_economic_segment
from app.refund_execution import canonical, digest
from app.settlement_finance import money
from app.settlement_multi_refund_finance import install as install_multi_finance
import test_cross_version_final_baseline as fixture


class MultiSourceRefundTests(unittest.TestCase):
    setUpClass = classmethod(fixture.CrossVersionFinalBaselineTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.CrossVersionFinalBaselineTests.tearDownClass.__func__)
    setUp = fixture.CrossVersionFinalBaselineTests.setUp
    tearDown = fixture.CrossVersionFinalBaselineTests.tearDown
    request = fixture.CrossVersionFinalBaselineTests.request
    prepare = fixture.CrossVersionFinalBaselineTests.prepare
    create = fixture.CrossVersionFinalBaselineTests.create
    deliver = fixture.CrossVersionFinalBaselineTests.deliver
    scenario = fixture.CrossVersionFinalBaselineTests.scenario
    settlement = fixture.CrossVersionFinalBaselineTests.settlement

    def two_changes(self, first_final_rates=None):
        with self.m.SessionLocal() as db:
            for rule in db.scalars(select(self.m.SettlementRule)):
                rule.platform_rate, rule.provider_rate = 20, 80
                rule.service_rate = rule.expert_rate = rule.channel_rate = 0
            db.commit()
        self.scenario()
        m = self.m
        with m.SessionLocal() as db:
            first_ids = [self.settlement(db, side).id for side in ('old', 'target')]
            target = self.settlement(db, 'target')
            amounts = dict(gross_amount=str(target.gross_amount), cost_amount=str(target.cost_amount), profit_amount=str(target.profit_amount))
        if first_final_rates:
            proposal = self.request('POST', f'/api/settlements/{first_ids[1]}/proposals', actor='finance', json=dict(
                **amounts, platform_rate=first_final_rates[0], provider_rate=first_final_rates[1],
                service_rate=0, expert_rate=0, channel_rate=0, reason='Final source share agreement'))
            self.request('POST', f'/api/settlement-proposals/{proposal["id"]}/decision', actor='provider',
                         json=dict(decision='approve', comment='Agreed final source shares'))
        for sid in first_ids:
            self.request('POST', f'/api/settlements/{sid}/lock', actor='finance')

        def clone(row, **updates):
            return type(row)(**dict({c.key: getattr(row, c.key) for c in row.__table__.columns}, **updates))

        with m.SessionLocal() as db:
            old_change = db.get(m.VersionChangeRequest, 'change')
            old_window = db.get(m.refund_segment_store['Window'], 'window')
            term = db.get(m.subscription_terms['Term'], old_window.term_id)
            at = old_window.starts_at + timedelta(hours=1)
            prepared = copy.deepcopy(json.loads(old_change.prepared_json)[0])
            prepared['old'].update(version_id='v2', revenue='-37.50',
                cost=str(-old_window.cost / 2), profit=str(-Decimal('37.50') + old_window.cost / 2))
            prepared['target'].update(version_id='v3', revenue='90.00', cost='30.00', profit='60.00')
            snapshots = copy.deepcopy(json.loads(old_change.target_snapshots_json))
            snapshots[term.id]['economic']['product_version_id'] = 'v3'
            db.add(clone(db.get(m.ProductReleaseVersion, 'v2'), id='v3', version_code='v3'))
            funding = clone(db.get(m.Order, 'funding'), id='funding2', order_no='FUNDING-SECOND', amount=Decimal('52.50'), paid_amount=Decimal('52.50'))
            db.add(funding)
            db.flush()
            payment = clone(db.get(m.Payment, 'funding-payment'), id='funding-payment2',
                order_id=funding.id, payment_no='PAY-SECOND', amount=Decimal('52.50'))
            db.add(payment)
            change = clone(old_change, id='change2', old_version_id='v2', old_version_code='v2', target_version_id='v3',
                target_version_code='v3', idempotency_key='change-second', request_hash='b' * 64,
                prepared_json=canonical([prepared]), target_snapshots_json=canonical(snapshots),
                quote_json=canonical(dict(net_difference='52.50')), funding_order_id='funding2')
            db.add(change)
            db.flush()
            db.add(m.VersionChangeFund(change_id=change.id, kind='payment', payment_id=payment.id,
                amount=Decimal('52.50'), idempotency_key='fund-second'))
            old_receipt = db.get(m.refund_segment_store['Receipt'], 'receipt')
            raw = json.loads(old_receipt.receipt_json)
            policy = dict(scope=term.combination_id, target_version_id='v3')
            raw.update(change_id=change.id, target_fingerprint=digest(policy))
            receipt = clone(old_receipt, id='receipt2', change_id=change.id, receipt_json=canonical(raw),
                receipt_hash=digest(raw), target_fingerprint=digest(policy), cutover_at=at)
            db.add(receipt)
            resource = json.loads(old_change.resource_plan_json)
            resource['charge_boundary']['target'] = policy
            proof = dict(request_change_id=change.id, resource_change_id=change.id, evidence_fingerprint=digest(dict(
                change_id=change.id, direction='target', kind='api', evidence=dict(route_id='route', route_key='cross-final',
                    credential_ids=['credential'], receipt_ids=['receipt2']))))
            change.resource_plan_json, change.evidence_json = canonical(resource), canonical(proof)
            revision = clone(db.get(m.subscription_terms['Revision'], 'revision'), id='revision2',
                change_id=change.id, revision_number=2, version_id='v3', version_code='v3',
                economic_snapshot_json=canonical(snapshots[term.id]['economic']))
            db.add(revision)
            db.flush()
            for side in ('old', 'target'):
                values = prepared[side]
                db.add(clone(db.get(m.VersionChangeAdjustment, side), id=side + '2', change_id=change.id,
                    request_change_id=change.id, resource_change_id=change.id, original_version_id='v2', target_version_id='v3',
                    revenue=Decimal(values['revenue']), cost=Decimal(values['cost']), profit=Decimal(values['profit']),
                    rule_json=canonical(values['rule']), payload_fingerprint=digest(prepared),
                    resource_evidence_fingerprint=proof['evidence_fingerprint'], effective_at=at,
                    posted_at=(m.now() + timedelta(days=32)).replace(day=1)))
            fields = ('order_id', 'term_id', 'change_id', 'receipt_id', 'revision_id', 'revenue', 'cost', 'currency',
                'starts_at', 'ends_at', 'available_quota', 'economic_snapshot_json', 'delivery_snapshot_json', 'evidence_json')
            window = clone(old_window, id='window2', change_id=change.id, receipt_id=receipt.id, revision_id=revision.id,
                revenue=Decimal('90.00'), cost=Decimal('30.00'), starts_at=at,
                economic_snapshot_json=revision.economic_snapshot_json,
                evidence_json=canonical(dict(proof=proof, receipt_hash=receipt.receipt_hash,
                    economic_hash=digest(snapshots[term.id]['economic']), delivery_hash=digest(json.loads(revision.delivery_snapshot_json)))))
            payload = {k: getattr(window, k) for k in fields}
            payload = {k: v.isoformat() if hasattr(v, 'isoformat') else str(v) if isinstance(v, Decimal) else v for k, v in payload.items()}
            window.payload_hash = digest(payload)
            db.add(window)
            db.commit()
        start = (m.now() + timedelta(days=32)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        self.request('POST', '/api/settlement-batches', actor='finance', json=dict(cycle='monthly',
            period_start=start.isoformat(), period_end=(start + timedelta(days=32)).replace(day=1).isoformat(),
            idempotency_key='second-monthly', order_ids=[self.order_id]))
        with m.SessionLocal() as db:
            second_ids = [self.settlement(db, side).id for side in ('old2', 'target2')]
        for sid in second_ids:
            result = self.request('POST', f'/api/settlements/{sid}/lock', actor='finance')
            self.assertIsNone(result['financial_capture'], result)
        self.target_ids = first_ids[1], second_ids[1]

    def plans(self, db, amount='10.00', prior='0.00'):
        plans = []
        for wid in ('window', 'window2'):
            w = db.get(self.m.refund_segment_store['Window'], wid)
            segment = EconomicSegment(w.id, w.order_id, w.change_id, w.revenue, w.cost, 'api', w.available_quota,
                w.starts_at, w.ends_at, json.loads(w.economic_snapshot_json), json.loads(w.delivery_snapshot_json), ('proof',))
            p = plan_economic_segment(segment, amount, at=w.starts_at, used_calls=0, prior_amount=prior,
                prior_recovery=int(Decimal(prior) * w.available_quota / w.revenue))
            p.update(window_hash=w.payload_hash,
                receipt_hash=db.get(self.m.refund_segment_store['Receipt'], w.receipt_id).receipt_hash)
            plans.append(p)
        return dict(segments=plans)

    def seed_completed(self, db, plan, rid='multi', offset=0, at=None):
        m = self.m
        amount = sum(Decimal(p['refund_amount']) for p in plan['segments'])
        payment = db.scalar(select(m.Payment).where(m.Payment.order_id == self.order_id))
        at = at or m.now() + timedelta(seconds=offset)
        refund = m.Refund(id=rid, refund_no='REF-' + rid, order_id=self.order_id, payment_id=payment.id,
                         amount=amount, status='completed', completed_at=at)
        db.add(refund)
        db.flush()
        envelope = dict(refund_id=rid, order_id=self.order_id, payment_id=payment.id, amount=str(amount), economic_plan=plan)
        funding = [dict(payment_id=payment.id, source_order_id=self.order_id, amount=str(amount), currency='CNY')]
        resource = dict(kind='api', refund_id=rid, order_id=self.order_id, frozen_envelope_hash=digest(envelope))
        resource['plan_hash'] = digest(resource)
        proof = dict(refund_id=rid, resource_plan_hash=digest(resource))
        execution = m.refund_execution['Execution'](id='exec-' + rid, refund_id=rid, status='completed',
            plan_hash=digest(envelope), resource_plan_json=canonical(resource), resource_plan_hash=digest(resource),
            proof_json=canonical(proof), funding_json=canonical(funding), requested_by='buyer', completed_at=at)
        db.add(execution)
        db.flush()
        window = db.get(m.refund_segment_store['Window'], 'window')
        term = db.get(m.subscription_terms['Term'], window.term_id)
        db.add(m.refund_execution_resources['Recovery'](refund_id=rid, execution_id=execution.id, order_id=self.order_id,
            term_id=term.id, combination_id=term.combination_id, generation=1, recovery_number=offset + 1,
            plan_json=canonical(resource), proof_json=canonical(proof), payload_hash=digest(dict(plan=resource, proof=proof))))
        db.add(m.refund_execution['Completion'](refund_id=rid, execution_id=execution.id, plan_hash=digest(envelope),
            proof_hash=digest(proof), funding_hash=digest(funding), economic_plan_json=canonical(plan), amount=amount, completed_at=at))
        db.add(m.refund_execution['PaymentLeg'](refund_id=rid, payment_id=payment.id, source_order_id=self.order_id,
            amount=amount, currency='CNY', status='completed', receipt=f'simulated:{rid}:{payment.id}', completed_at=at))
        db.add(m.refund_workflow['Case'](refund_id=rid, source='ordinary', requested_by_id='buyer',
                                      plan_json=canonical(envelope), plan_hash=digest(envelope)))
        db.flush()
        return refund

    def multi_finance(self):
        m, h = self.m, self.m.settlement_finance
        models = {mapper.class_.__name__: mapper.class_ for mapper in m.Base.registry.mappers}
        cash_block = inspect.getclosurevars(h['batch_status']).nonlocals['cash_block']
        core = dict(h, RefundSource=models['RefundSource'], RefundPositionSource=models['RefundPositionSource'],
            cash_block=cash_block, money=money, canonical=canonical,
            audit=lambda db, actor, action, target, **values: m.audit(db, actor, action, 'settlement_finance',
                target, category='settlement', business_domain='settlement', after=values))
        hooks = install_multi_finance(vars(m), core)
        return hooks, core

    def capture_multi(self, db, refund, hooks):
        negative = self.m.refund_settlement['negative_settlement'](db, refund)
        with patch.dict(self.m.settlement_finance, capture_multi_refund=hooks['capture']):
            self.m.refund_settlement['prepare_lock'](db, negative, 'finance')
        negative.status = 'locked'
        db.flush()
        baseline = hooks['capture'](db, negative, 'finance')
        self.assertFalse(isinstance(baseline, dict), baseline)
        return negative, baseline

    def test_actual_multi_capture_preserves_source_positions_and_replays(self):
        self.two_changes()
        m = self.m
        hooks, core = self.multi_finance()
        with m.SessionLocal() as db:
            refund = self.seed_completed(db, self.plans(db))
            completion = db.get(m.refund_execution['Completion'], refund.id)
            frozen = completion.economic_plan_json, completion.proof_hash, completion.funding_hash
            negative, baseline = self.capture_multi(db, refund, hooks)
            self.assertEqual(baseline.reference_settlement_id, '')
            sources = db.scalars(select(core['RefundSource']).where(core['RefundSource'].settlement_id == negative.id)).all()
            self.assertEqual({s.source_settlement_id for s in sources}, set(self.target_ids))
            positions = db.scalars(select(core['Position']).where(core['Position'].settlement_id == negative.id)).all()
            self.assertEqual(sum(p.amount for p in positions), 20)
            for p in positions:
                reference = hooks['position_reference'](db, p.id)
                self.assertIn(reference['reference_settlement_id'], self.target_ids)
                self.assertEqual(reference['reference_settlement_id'], self.target_ids[reference['segment_id'] == 'window2'])
            counts = tuple(db.scalar(select(func.count()).select_from(core[k])) for k in ('Baseline', 'RefundSource', 'Position'))
            self.assertEqual(hooks['capture'](db, negative).settlement_id, baseline.settlement_id)
            self.assertEqual(counts, tuple(db.scalar(select(func.count()).select_from(core[k])) for k in ('Baseline', 'RefundSource', 'Position')))
            self.assertEqual(frozen, (completion.economic_plan_json, completion.proof_hash, completion.funding_hash))
            sources[0].source_hash = 'changed'
            with self.assertRaises(ValueError):
                db.flush()

    def test_http_multi_lock_uses_core_dispatch_without_capture_mock(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            negative = m.refund_settlement['negative_settlement'](db, self.seed_completed(db, self.plans(db)))
            nid = negative.id
            db.commit()
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        with m.SessionLocal() as db:
            baseline = db.get(m.settlement_finance['Baseline'], nid)
            self.assertEqual(baseline.reference_settlement_id, '')
            snapshot = json.loads(baseline.snapshot_json)
            self.assertEqual({r['reference_settlement_id'] for r in snapshot['references']}, set(self.target_ids))
            self.assertEqual(db.get(m.Refund, 'multi').amount, 20)

    def test_multi_capture_audit_failure_rolls_back_all_financial_sources(self):
        self.two_changes()
        m = self.m
        _, core = self.multi_finance()

        def reject(*args, **kwargs):
            raise RuntimeError('injected multi capture audit failure')

        hooks = install_multi_finance(vars(m), dict(core, audit=reject))
        with m.SessionLocal() as db:
            refund = self.seed_completed(db, self.plans(db))
            negative = m.refund_settlement['negative_settlement'](db, refund)
            m.refund_settlement['prepare_lock'](db, negative, 'finance')
            negative.status = 'locked'
            db.flush()
            with self.assertRaisesRegex(RuntimeError, 'injected multi capture audit failure'):
                hooks['capture'](db, negative)
            self.assertIsNone(db.get(core['Baseline'], negative.id))
            self.assertEqual(db.scalar(select(func.count()).select_from(core['RefundSource'])), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(core['RefundPositionSource'])), 0)

    def test_single_reversal_after_multi_uses_the_same_source_budget(self):
        self.two_changes()
        m = self.m
        hooks, core = self.multi_finance()
        with m.SessionLocal() as db:
            self.capture_multi(db, self.seed_completed(db, self.plans(db)), hooks)
            source = db.get(core['Baseline'], self.target_ids[0])
            from app.settlement_finance_adapter import proportional_values
            values = proportional_values(source, Decimal('30'))
            negative = m.Settlement(settlement_no='SINGLE-AFTER-MULTI', order_id=self.order_id,
                status='locked', reference_settlement_id=source.settlement_id, gross_amount=-30, net_amount=-30,
                cost_amount=-values['cost'], profit_amount=-values['profit'], **{k: -v for k, v in values['shares'].items()})
            db.add(negative)
            db.flush()
            with self.assertRaises(HTTPException) as error:
                m.settlement_finance['capture'](db, negative)
            self.assertEqual(error.exception.detail, 'reversal_exceeds_original_baseline')
            self.assertIsNone(db.get(core['Baseline'], negative.id))

    def test_repeated_actual_multi_captures_conserve_each_source_cents(self):
        self.two_changes()
        m = self.m
        hooks, core = self.multi_finance()
        with m.SessionLocal() as db:
            first = self.seed_completed(db, self.plans(db), 'first')
            self.capture_multi(db, first, hooks)
            second = self.seed_completed(db, self.plans(db, prior='10.00'), 'second', offset=1)
            self.capture_multi(db, second, hooks)
            from app.settlement_finance_adapter import proportional_values
            for sid in self.target_ids:
                source = db.get(core['Baseline'], sid)
                expected = proportional_values(source, Decimal('20'))
                records = [json.loads(r.evidence_json) for r in db.scalars(select(core['RefundSource']).where(
                    core['RefundSource'].source_settlement_id == sid))]
                self.assertEqual(len(records), 2)
                self.assertEqual(sum(Decimal(r['gross_amount']) for r in records), -20)
                self.assertEqual(sum(Decimal(r['cost_amount']) for r in records), -expected['cost'])
                self.assertEqual(sum(Decimal(r['profit_amount']) for r in records), -expected['profit'])
                for k in expected['shares']:
                    self.assertEqual(sum(Decimal(r['shares'][k]) for r in records), -expected['shares'][k])

    def test_multi_capture_shared_budget_includes_vc_old_and_single_negative(self):
        self.two_changes()
        m = self.m
        hooks, core = self.multi_finance()
        with m.SessionLocal() as db:
            # First target is 75; next VC already reversed 37.50. An existing
            # ordinary single-source reversal of 30 leaves only 7.50, not 45.
            source = db.get(core['Baseline'], self.target_ids[0])
            from app.settlement_finance_adapter import proportional_values
            values = proportional_values(source, Decimal('30'))
            single = m.Settlement(settlement_no='SINGLE-PRIOR', order_id=self.order_id,
                status='locked', reference_settlement_id=source.settlement_id, gross_amount=-30,
                net_amount=-30, cost_amount=-values['cost'], profit_amount=-values['profit'],
                **{k: -v for k, v in values['shares'].items()})
            db.add(single)
            db.flush()
            m.settlement_finance['capture'](db, single)
            self.assertEqual(len(hooks['source_reversals'](db, source.settlement_id)), 2)
            refund = self.seed_completed(db, self.plans(db))
            negative = m.refund_settlement['negative_settlement'](db, refund)
            basis = m.refund_settlement['assessment'](db, negative.id)
            self.assertEqual(basis['status'], 'waiting')
            self.assertEqual(basis['reason']['reason'], 'multi_source_reversal_exceeds_final_baseline')
            self.assertIsNone(db.get(core['Baseline'], negative.id))
            self.assertEqual(db.scalar(select(func.count()).select_from(core['RefundSource'])), 0)

    def test_actual_multi_offsets_use_only_each_exact_original_source(self):
        self.two_changes()
        m = self.m
        hooks, core = self.multi_finance()
        with m.SessionLocal() as db:
            negative, _ = self.capture_multi(db, self.seed_completed(db, self.plans(db)), hooks)
            batch = m.SettlementBatch(batch_no='MULTI-BATCH', cycle='manual', status='confirmed', idempotency_key='multi-batch')
            db.add(batch)
            db.flush()
            db.add(m.SettlementLine(batch_id=batch.id, settlement_id=negative.id,
                participant_type='provider', participant_id='provider_tenant', participant_name='Provider', amount=1, status='locked'))
            for row in db.scalars(select(m.SettlementBatch)):
                row.status = 'confirmed'
            # Funds reconciliation is a new explicit transaction; its guard
            # must precede any Batch locks taken while preparing the fixture.
            db.commit()
            result = hooks['reconcile_offsets'](db, [negative.id], 'finance')
            self.assertGreater(result['offset_count'], 0)
            for tid in result['transaction_ids']:
                tx = db.get(core['Transaction'], tid)
                payload = json.loads(tx.payload_json)
                debt_id = payload['allocations'][0]['position_id']
                source = db.get(core['Position'], payload['counter_position_id'])
                self.assertEqual(source.settlement_id, hooks['position_reference'](db, debt_id)['reference_settlement_id'])
                self.assertEqual(tx.kind, 'offset')
                self.assertFalse(json.loads(tx.payload_json).get('cash_recovered', False))
            self.assertEqual(hooks['reconcile_offsets'](db, [negative.id])['offset_count'], 0)

    def test_multi_describe_detail_and_list_do_not_write(self):
        self.two_changes()
        m = self.m
        hooks, core = self.multi_finance()
        with m.SessionLocal() as db:
            negative, _ = self.capture_multi(db, self.seed_completed(db, self.plans(db)), hooks)
            nid = negative.id
            models = [mapper.class_ for mapper in m.Base.registry.mappers]
            before = {model.__tablename__: db.scalar(select(func.count()).select_from(model)) for model in models}
            db.commit()
        self.request('GET', f'/api/settlements/{nid}/detail', actor='finance')
        self.request('GET', '/api/settlement-finance/positions', actor='finance')
        self.request('GET', '/api/settlement-finance/transactions', actor='finance')
        with m.SessionLocal() as db:
            self.assertEqual(len(hooks['describe'](db, nid)), 2)
            m.refund_settlement['ledger_status'](db, 'multi')
            self.assertEqual(before, {model.__tablename__: db.scalar(select(func.count()).select_from(model)) for model in models})

    def test_refund_display_references_enrich_final_rates_without_mutating_evidence(self):
        self.two_changes(first_final_rates=(25, 75))
        m = self.m
        hooks, core = self.multi_finance()
        with m.SessionLocal() as db:
            negative, _ = self.capture_multi(db, self.seed_completed(db, self.plans(db)), hooks)
            nid = negative.id
            assessment_model = m.refund_settlement['FinancialAssessment']
            stored = [(r.version, r.values_json) for r in db.scalars(select(assessment_model).where(assessment_model.settlement_id == nid))]
            manifest = db.get(m.refund_settlement['MultiSourceManifest'], nid)
            frozen = manifest.payload_json, manifest.payload_hash
            expected = {}
            for sid in self.target_ids:
                source = db.get(core['Baseline'], sid)
                source_set = db.get(m.Settlement, sid)
                batch = db.scalar(select(m.SettlementBatch).join(m.SettlementLine, m.SettlementLine.batch_id == m.SettlementBatch.id)
                    .where(m.SettlementLine.settlement_id == sid))
                expected[sid] = source_set.settlement_no, batch.batch_no, source.created_at
            db.commit()
        detail = self.request('GET', f'/api/settlements/{nid}/detail', actor='finance')
        refs = detail['refund_adjustment']['financial_basis']['references']
        for ref in refs:
            self.assertEqual(ref['display_status'], 'verified')
            self.assertTrue(ref['source_order_no'].startswith('ORD-'))
            self.assertEqual((ref['reference_settlement_no'], ref['reference_batch_no']), expected[ref['reference_settlement_id']][:2])
            self.assertTrue(ref['baseline_created_at'])
        first = next(r for r in refs if r['reference_settlement_id'] == self.target_ids[0])
        self.assertEqual(first['share_rates']['platform'], '25.0000')
        self.assertEqual(first['share_rates']['provider'], '75.0000')
        with m.SessionLocal() as db:
            self.assertEqual(stored, [(r.version, r.values_json) for r in db.scalars(select(assessment_model).where(assessment_model.settlement_id == nid))])
            manifest = db.get(m.refund_settlement['MultiSourceManifest'], nid)
            self.assertEqual(frozen, (manifest.payload_json, manifest.payload_hash))
            source = db.get(core['Baseline'], self.target_ids[0])
            self.assertEqual(json.loads(source.snapshot_json)['economic_snapshot']['settlement_rule']['rates']['platform_rate'], '20.0000')

    def test_refund_display_reference_rejects_scope_and_hash_mismatches(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            negative = m.refund_settlement['negative_settlement'](db, self.seed_completed(db, self.plans(db)))
            model = m.refund_settlement['FinancialAssessment']
            basis = m.refund_settlement['assessment'](db, negative.id)
            Baseline = m.settlement_finance['Baseline']
            source = db.get(Baseline, self.target_ids[0])
            foreign_set = m.Settlement(settlement_no='WRONG-PROVIDER-SOURCE', order_id=self.order_id, status='locked')
            db.add(foreign_set)
            db.flush()
            db.add(Baseline(settlement_id=foreign_set.id, order_id=self.order_id, provider_enterprise_id='other_tenant',
                currency=source.currency, revenue=source.revenue, cost=source.cost, profit=source.profit,
                snapshot_json=source.snapshot_json, snapshot_hash=source.snapshot_hash, actor='invalid-provider-fixture'))
            db.flush()
            failures = (dict(source_order_id='foreign-order'), dict(baseline_hash='wrong'),
                        dict(reference_settlement_id=foreign_set.id, baseline_hash=source.snapshot_hash))
            for version, wrong in enumerate(failures, start=2):
                changed = copy.deepcopy(basis)
                changed['references'][0].update(wrong)
                db.add(model(settlement_id=negative.id, version=version, values_json=canonical(changed), actor='corrupt-fixture'))
                db.flush()
                ref = m.refund_settlement['describe'](db, negative.id)['financial_basis']['references'][0]
                self.assertEqual(ref['display_status'], 'unavailable')
                self.assertNotIn('source_order_no', ref)
                self.assertNotIn('share_rates', ref)

    def test_two_real_vc_sources_keep_exact_refs_and_buyer_completion(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            plan = self.plans(db)
            refund = self.seed_completed(db, plan)
            frozen = db.get(m.refund_execution['Completion'], refund.id).economic_plan_json
            negative = m.refund_settlement['negative_settlement'](db, refund)
            basis = m.refund_settlement['assessment'](db, negative.id)
            self.assertEqual(basis['status'], 'ready', basis)
            self.assertEqual({r['reference_settlement_id'] for r in basis['references']}, set(self.target_ids))
            self.assertEqual(negative.reference_settlement_id, '')
            self.assertEqual(negative.gross_amount, -20)
            self.assertEqual(frozen, db.get(m.refund_execution['Completion'], refund.id).economic_plan_json)
            self.assertEqual(m.refund_settlement['negative_settlement'](db, refund).id, negative.id)
            nid = negative.id
            db.commit()
        with patch.dict(m.settlement_finance, capture_multi_refund=None):
            response = self.client.post(f'/api/settlements/{nid}/lock', headers={'Authorization': 'Bearer ' + self.tokens['finance']})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('multi_source_refund_capture_contract_required', response.text)

    def test_manifest_freeze_is_immutable_and_revalidated_without_posting(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            refund = self.seed_completed(db, self.plans(db))
            negative = m.refund_settlement['negative_settlement'](db, refund)
            # This advertises the optional contract only; no finance call/posting
            # is mocked as successful. Exercise the owned prepare hook directly.
            with patch.dict(m.settlement_finance, capture_multi_refund=lambda *_: None):
                m.refund_settlement['prepare_lock'](db, negative, 'finance')
            manifest = m.refund_settlement['capture_manifest'](db, negative)
            self.assertEqual(manifest['status'], 'ready', manifest)
            self.assertEqual(len(manifest['references']), 2)
            self.assertEqual(manifest['manifest_hash'], digest({k: v for k, v in manifest.items()
                                                              if k not in ('status', 'manifest_hash')}))
            self.assertIsNone(db.get(m.settlement_finance['Baseline'], negative.id))
            self.assertEqual(manifest, m.refund_settlement['capture_manifest'](db, negative))
            negative.reference_settlement_id = self.target_ids[0]
            invalid = m.refund_settlement['capture_manifest'](db, negative)
            self.assertEqual(invalid['status'], 'waiting')
            negative.reference_settlement_id = ''
            row = db.get(m.refund_settlement['MultiSourceManifest'], negative.id)
            row.payload_hash = 'corrupt'
            with self.assertRaises(ValueError):
                db.flush()

    def test_repeated_multi_refunds_validate_each_cumulative_interval(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            first_plan = self.plans(db)
            first = self.seed_completed(db, first_plan, 'first')
            first_negative = m.refund_settlement['negative_settlement'](db, first)
            first_basis = m.refund_settlement['assessment'](db, first_negative.id)
            second_plan = self.plans(db, prior='10.00')
            second = self.seed_completed(db, second_plan, 'second', offset=1)
            second_negative = m.refund_settlement['negative_settlement'](db, second)
            second_basis = m.refund_settlement['assessment'](db, second_negative.id)
            self.assertEqual(second_basis['status'], 'ready', second_basis)
            for r in second_basis['references']:
                self.assertEqual(r['prior_refund_amount'], '10.00')
                self.assertEqual(r['prior_refunds'][0]['refund_id'], 'first')
                prior_ref = next(p for p in first_basis['references'] if p['segment_id'] == r['segment_id'])
                self.assertEqual(Decimal(prior_ref['gross_amount']) + Decimal(r['gross_amount']), Decimal('-20'))
            self.assertEqual(m.refund_settlement['negative_settlement'](db, second).id, second_negative.id)
            wrong = self.seed_completed(db, self.plans(db, prior='10.00'), 'reused', offset=2)
            wrong_negative = m.refund_settlement['negative_settlement'](db, wrong)
            result = m.refund_settlement['assessment'](db, wrong_negative.id)
            self.assertEqual(result['status'], 'waiting')
            self.assertEqual(result['reason'], 'multi_source_prior_refund_amount_mismatch')
            self.assertEqual(wrong.status, 'completed')

    def test_one_missing_segment_receipt_waits_without_partial_application(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            plan = self.plans(db)
            plan['segments'][1]['receipt_hash'] = 'bad-receipt'
            refund = self.seed_completed(db, plan)
            negative = m.refund_settlement['negative_settlement'](db, refund)
            basis = m.refund_settlement['assessment'](db, negative.id)
            self.assertEqual(basis['status'], 'waiting')
            self.assertEqual(negative.reference_settlement_id, '')
            self.assertEqual(refund.status, 'completed')
            self.assertIsNone(db.get(m.refund_settlement['MultiSourceManifest'], negative.id))
            self.assertIsNone(db.get(m.settlement_finance['Baseline'], negative.id))

    def test_duplicate_segment_completion_never_creates_a_financial_mapping(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            plan = self.plans(db)
            plan['segments'][1] = copy.deepcopy(plan['segments'][0])
            refund = self.seed_completed(db, plan)
            with self.assertRaises(HTTPException):
                m.refund_settlement['negative_settlement'](db, refund)
            self.assertEqual(refund.status, 'completed')
            self.assertIsNone(db.get(m.refund_settlement['Model'], refund.id))

    def test_equal_completion_times_do_not_guess_segment_consumption_order(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            self.seed_completed(db, self.plans(db), 'first')
            second = self.seed_completed(db, self.plans(db, prior='10.00'), 'second', offset=1, at=m.now())
            negative = m.refund_settlement['negative_settlement'](db, second)
            result = m.refund_settlement['assessment'](db, negative.id)
            self.assertEqual(result['status'], 'waiting')
            self.assertEqual(result['reason'], 'multi_source_completion_order_ambiguous')

    def test_multi_source_retained_cost_needs_explicit_per_source_plan(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            refund = self.seed_completed(db, self.plans(db))
            with patch.dict(m.settlement_parties, cost_exception=lambda *_: dict(status='approved')):
                negative = m.refund_settlement['negative_settlement'](db, refund)
            basis = m.refund_settlement['assessment'](db, negative.id)
            self.assertEqual(basis, dict(status='waiting', reason='multi_source_cost_exception_plan_required'))
            self.assertIsNone(db.get(m.settlement_finance['CostExceptionApplication'], refund.id))


if __name__ == '__main__':
    unittest.main()
