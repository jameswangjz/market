"""Actual financial HTTP capture over seeded verified VC resource receipts.

No external gateway/bank or live business data is used. The source final baseline
is captured by the real core; source funding uses actual independently reviewed
manual evidence routes. Subsequent ordinary refund completion is seeded evidence.
"""
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest

from fastapi import HTTPException
from sqlalchemy import event, func, select, update

from app.refund_economic_segments import EconomicSegment, plan_economic_segment
from app.settlement_finance import SHARES
from app.settlement_version_funding import cumulative_target
import test_cross_version_multi_source_refund as fixture


class VersionFundingHTTPTests(unittest.TestCase):
    setUpClass = classmethod(fixture.MultiSourceRefundTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.MultiSourceRefundTests.tearDownClass.__func__)
    setUp = fixture.MultiSourceRefundTests.setUp
    tearDown = fixture.MultiSourceRefundTests.tearDown
    request = fixture.MultiSourceRefundTests.request
    create = fixture.MultiSourceRefundTests.create
    deliver = fixture.MultiSourceRefundTests.deliver
    scenario = fixture.MultiSourceRefundTests.scenario
    settlement = fixture.MultiSourceRefundTests.settlement
    seed_completed = fixture.MultiSourceRefundTests.seed_completed

    def prepare(self, *args, **kwargs):
        with self.m.SessionLocal() as db:
            version = db.get(self.m.ProductReleaseVersion, 'version')
            version.price, version.cost = 150, 90
            target = db.get(self.m.ProductReleaseVersion, 'v2')
            target.price, target.cost = 300, 120
            for rule in db.scalars(select(self.m.SettlementRule)):
                rule.platform_rate, rule.provider_rate = 20, 80
                rule.service_rate = rule.expert_rate = rule.channel_rate = 0
            db.commit()
        return fixture.MultiSourceRefundTests.prepare(self, *args, **kwargs)

    def funded_scenario(self, *, verified=True, kind='external_receipt'):
        m = self.m

        def source_final(_mapper, _connection, settlement):
            if settlement.id == 'original':
                settlement.gross_amount = settlement.net_amount = Decimal('160')
                settlement.cost_amount, settlement.profit_amount = Decimal('100'), Decimal('60')
                settlement.platform_fee, settlement.provider_share = Decimal('12'), Decimal('48')

        event.listen(m.Settlement, 'before_insert', source_final)
        try:
            self.scenario()
        finally:
            event.remove(m.Settlement, 'before_insert', source_final)
        with m.SessionLocal() as db:
            batch = m.SettlementBatch(batch_no='FUNDED-SOURCE', status='pending_confirm', idempotency_key='source-batch')
            db.add(batch)
            db.flush()
            bid = batch.id
            db.add(m.SettlementLine(batch_id=bid, settlement_id='original', participant_type='provider', amount=48))
            self.old_id, self.target_id = (self.settlement(db, side).id for side in ('old', 'target'))
            db.commit()
        self.request('POST', f'/api/settlement-batches/{bid}/confirm', actor='finance', json={})
        funding = self.request('POST', '/api/settlement-finance/funding-adjustments', actor='finance', json=dict(
            request_id='vc-source-funding', settlement_id='original', kind=kind, amount='10.00',
            reason='Actual original payer supplement', evidence_refs=['source:supplement'], bearer_enterprise_id='provider_tenant'))
        self.request('POST', f'/api/settlement-finance/funding-adjustments/{funding["id"]}/decision', actor='platform', json=dict(
            request_id='vc-source-approve', expected_version=1, decision='approved', note='Independent original funding review'))
        self.funding_id = funding['id']
        if not verified:
            return
        self.verify_source(kind)

    def verify_source(self, kind='external_receipt'):
        if kind == 'platform_absorption':
            self.request('POST', f'/api/settlement-finance/funding-adjustments/{self.funding_id}/verify', actor='platform', json=dict(
                request_id='vc-source-verify', expected_version=2))
            return
        receipt = self.request('POST', '/api/settlement-finance/manual-funding-evidence', actor='finance', json=dict(
            request_id='vc-source-receipt', funding_request_id=self.funding_id, amount='10.00', currency='CNY',
            reference='VC-SOURCE-RECEIPT', paid_on=datetime.now(timezone.utc).date().isoformat(),
            evidence_refs=['receipt:vc-source'], note='Original source external receipt'))
        self.request('POST', f'/api/settlement-finance/manual-funding-evidence/{receipt["id"]}/review', actor='platform', json=dict(
            request_id='vc-source-receipt-review', expected_version=1, decision='verified', note='Independent receipt review'))
        self.request('POST', f'/api/settlement-finance/funding-adjustments/{self.funding_id}/verify', actor='platform', json=dict(
            request_id='vc-source-verify', expected_version=2, receipt_reference=receipt['receipt_reference']))

    def target_refund(self, db, rid='multi'):
        m = self.m
        window = db.get(m.refund_segment_store['Window'], 'window')
        segment = EconomicSegment(window.id, window.order_id, window.change_id, window.revenue, window.cost,
            'api', window.available_quota, window.starts_at, window.ends_at,
            json.loads(window.economic_snapshot_json), json.loads(window.delivery_snapshot_json), ('proof',))
        plan = plan_economic_segment(segment, '10.00', at=window.starts_at, used_calls=0)
        plan.update(window_hash=window.payload_hash,
            receipt_hash=db.get(m.refund_segment_store['Receipt'], window.receipt_id).receipt_hash)
        refund = self.seed_completed(db, dict(segments=[plan]), rid=rid)
        negative = m.refund_settlement['negative_settlement'](db, refund)
        basis = m.refund_settlement['assessment'](db, negative.id)
        self.assertEqual(basis['status'], 'ready', basis)
        self.assertEqual(Decimal(basis['gross_amount']), -10)
        self.assertNotEqual(basis.get('kind'), 'confirmed_funded_refund')
        return negative.id

    def test_actual_external_source_vc_projection_credit_target_and_subsequent_refund(self):
        self.funded_scenario()
        m, h = self.m, self.m.settlement_finance
        with m.SessionLocal() as db:
            change = db.get(m.VersionChangeRequest, 'change')
            frozen = change.prepared_json, change.evidence_json, change.resource_plan_json
            self.assertEqual(db.get(m.Payment, 'funding-payment').amount, 75)
        for sid in (self.old_id, self.target_id):
            result = self.request('POST', f'/api/settlements/{sid}/lock', actor='finance')
            self.assertIsNone(result['financial_capture'], result)
        with m.SessionLocal() as db:
            old, target = (db.get(h['Baseline'], sid) for sid in (self.old_id, self.target_id))
            self.assertEqual((old.revenue, old.cost, old.profit), (Decimal('-80'), Decimal('-50'), Decimal('-30')))
            self.assertEqual(target.revenue, 150)
            snapshot = json.loads(old.snapshot_json)
            self.assertEqual(Decimal(snapshot['segment_context']['returned_unused_supplement']), 5)
            self.assertEqual(Decimal(snapshot['segment_context']['funding']['net_amount']), 75)
            positions = db.scalars(select(h['Position']).where(h['Position'].settlement_id == self.old_id)).all()
            self.assertEqual(sum(p.amount for p in positions if p.kind != 'funding_return'), 80)
            self.assertEqual(sum(p.amount for p in positions if p.kind == 'funding_return'), 5)
            self.assertTrue(all(p.direction == 'payable' for p in positions if p.kind == 'funding_return'))
            projection = m.settlement_version_funding['describe'](db, self.old_id)
            self.assertEqual(projection['credits'][0]['balance'], '5.00')
            self.assertIsNone(db.get(m.Refund, self.old_id))
            change = db.get(m.VersionChangeRequest, 'change')
            self.assertEqual(frozen, (change.prepared_json, change.evidence_json, change.resource_plan_json))
            nid = self.target_refund(db)
            db.commit()
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        with m.SessionLocal() as db:
            self.assertEqual(db.get(h['Baseline'], nid).reference_settlement_id, self.target_id)
            self.assertEqual(db.scalar(select(func.count()).select_from(m.settlement_version_funding['Credit'])), 1)

    def test_unverified_external_source_cannot_project_or_capture(self):
        self.funded_scenario(verified=False)
        with self.m.SessionLocal() as db:
            status = db.get(self.m.Settlement, self.old_id).status
            batch_status = db.get(self.m.SettlementBatch, self.batch_id).status
        detail = self.request('GET', f'/api/settlements/{self.old_id}/detail', actor='finance')
        self.assertEqual(detail['finance']['status'], 'waiting')
        self.request('POST', f'/api/settlements/{self.old_id}/lock', actor='finance', expected=409)
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.get(self.m.settlement_finance['Baseline'], self.old_id))
            self.assertEqual(db.scalar(select(func.count()).select_from(self.m.settlement_version_funding['Projection'])), 0)
            self.assertEqual(db.get(self.m.Settlement, self.old_id).status, status)
            self.assertEqual(db.get(self.m.SettlementBatch, self.batch_id).status, batch_status)
        self.request('POST', f'/api/settlement-batches/{self.batch_id}/confirm', actor='finance', expected=409, json={})
        self.verify_source()
        self.request('POST', f'/api/settlements/{self.target_id}/lock', actor='finance', expected=409)
        with self.m.SessionLocal() as db:
            self.assertNotEqual(db.get(self.m.Settlement, self.target_id).status, 'locked')
            self.assertIsNone(db.get(self.m.settlement_finance['Baseline'], self.target_id))
        for sid in (self.old_id, self.target_id):
            result = self.request('POST', f'/api/settlements/{sid}/lock', actor='finance')
            self.assertIsNone(result['financial_capture'], result)
        with self.m.SessionLocal() as db:
            self.assertIsNotNone(db.get(self.m.settlement_finance['Baseline'], self.old_id))
            self.assertIsNotNone(db.get(self.m.settlement_finance['Baseline'], self.target_id))

    def test_platform_absorption_vc_restores_internal_credit_never_external_payer_payable(self):
        self.funded_scenario(kind='platform_absorption')
        m, h = self.m, self.m.settlement_finance
        with m.SessionLocal() as db:
            proof = db.get(h['FundingProof'], 'original')
            original_proof = proof.evidence_json, proof.evidence_hash
        for sid in (self.old_id, self.target_id):
            result = self.request('POST', f'/api/settlements/{sid}/lock', actor='finance')
            self.assertIsNone(result['financial_capture'], result)
        with m.SessionLocal() as db:
            old, target = (db.get(h['Baseline'], sid) for sid in (self.old_id, self.target_id))
            self.assertEqual((old.revenue, old.cost, target.revenue), (Decimal('-80'), Decimal('-50'), Decimal('150')))
            rows = db.scalars(select(h['Position']).where(h['Position'].settlement_id == self.old_id)).all()
            restored = [p for p in rows if p.kind == 'funding_absorption_return']
            self.assertEqual(len(restored), 1)
            self.assertEqual((restored[0].direction, restored[0].participant_id, restored[0].amount), ('internal', 'platform', 5))
            self.assertFalse(any(p.kind == 'funding_return' or p.direction == 'payable' for p in rows))
            result = m.settlement_version_funding['describe'](db, self.old_id)
            self.assertEqual(result['credit_kind'], 'internal_absorption_restoration')
            self.assertEqual(Decimal(result['external_return_amount']), 0)
            self.assertEqual(Decimal(result['internal_absorption_return']), 5)
            self.assertEqual(h['source_reversals'](db, 'original')[0]['entries'],
                [e for e in json.loads(old.snapshot_json)['entries'] if e['kind'] != 'funding_absorption_return'])
            proof = db.get(h['FundingProof'], 'original')
            self.assertEqual(original_proof, (proof.evidence_json, proof.evidence_hash))
            pid = restored[0].id
        self.request('POST', f'/api/settlement-batches/{self.batch_id}/confirm', actor='finance', json={})
        self.request('POST', '/api/settlement-finance/transactions', actor='finance', expected=409, json=dict(
            request_id='cannot-pay-internal-restore', kind='payout', mode='manual', reference='INVALID-PAYOUT',
            allocations=[dict(position_id=pid, amount='5.00')]))
        with m.SessionLocal() as db:
            nid = self.target_refund(db, 'platform-target-refund')
            db.commit()
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        with m.SessionLocal() as db:
            self.assertEqual(db.get(h['Baseline'], nid).reference_settlement_id, self.target_id)
            self.assertIsNone(db.get(m.settlement_funded_refund['Plan'], 'platform-target-refund'))

    def test_atomic_credit_failure_rolls_back_negative_projection_and_positions_then_replays(self):
        self.funded_scenario()
        m, h = self.m, self.m.settlement_finance
        Credit = m.settlement_version_funding['Credit']

        def fail(*_):
            raise RuntimeError('injected credit insert failure')

        event.listen(Credit, 'before_insert', fail)
        try:
            self.request('POST', f'/api/settlements/{self.old_id}/lock', actor='finance', expected=500)
        finally:
            event.remove(Credit, 'before_insert', fail)
        with m.SessionLocal() as db:
            self.assertIsNone(db.get(h['Baseline'], self.old_id))
            self.assertIsNone(db.get(m.settlement_version_funding['Projection'], self.old_id))
            self.assertEqual(db.scalar(select(func.count()).select_from(h['Position']).where(
                h['Position'].settlement_id == self.old_id)), 0)
        self.request('POST', f'/api/settlements/{self.old_id}/lock', actor='finance')
        with m.SessionLocal() as db:
            before = tuple(db.scalar(select(func.count()).select_from(model)) for model in (
                h['Baseline'], h['Position'], Credit, m.settlement_version_funding['Projection'], h['RefundPositionSource']))
            context = m.version_change_settlement['financial_context'](db, db.get(m.Settlement, self.old_id))
            result = h['capture_segment'](db, db.get(m.Settlement, self.old_id), context)
            self.assertEqual(result.settlement_id, self.old_id)
            self.assertEqual(before, tuple(db.scalar(select(func.count()).select_from(model)) for model in (
                h['Baseline'], h['Position'], Credit, m.settlement_version_funding['Projection'], h['RefundPositionSource'])))
            records = h['source_reversals'](db, 'original')
            self.assertEqual((len(records), sum(Decimal(r['gross_amount']) for r in records)), (1, Decimal('-80')))
            self.assertFalse(any(e['kind'] == 'funding_return' for e in records[0]['entries']))

    def test_describe_rejects_wrong_payer_currency_source_mapping_and_context_without_writes(self):
        self.funded_scenario()
        self.request('POST', f'/api/settlements/{self.old_id}/lock', actor='finance')
        m, h = self.m, self.m.settlement_finance
        with m.SessionLocal() as db:
            credit = db.scalar(select(m.settlement_version_funding['Credit']))
            for model, condition, values in (
                (h['Position'], h['Position'].id == credit.position_id, dict(participant_id='buyer_tenant')),
                (h['Position'], h['Position'].id == credit.position_id, dict(currency='USD')),
                (h['RefundPositionSource'], h['RefundPositionSource'].position_id == credit.position_id,
                 dict(source_settlement_id=self.old_id)),
            ):
                transaction = db.begin_nested()
                try:
                    db.execute(update(model).where(condition).values(**values))
                    db.expire_all()
                    with self.assertRaises(HTTPException):
                        m.settlement_version_funding['describe'](db, self.old_id)
                finally:
                    transaction.rollback()
                    db.expire_all()
            before = len(db.new), len(db.dirty), len(db.deleted)
            one = m.settlement_version_funding['describe'](db, self.old_id)
            self.assertEqual(one, m.settlement_version_funding['describe'](db, self.old_id))
            self.assertEqual(before, (len(db.new), len(db.dirty), len(db.deleted)))
            self.assertTrue(one['recorded_at'])

    def test_confirmed_batch_credit_progress_and_manual_review_before_target_payment(self):
        self.funded_scenario()
        for sid in (self.old_id, self.target_id):
            self.request('POST', f'/api/settlements/{sid}/lock', actor='finance')
        self.request('POST', f'/api/settlement-batches/{self.batch_id}/confirm', actor='finance', json={})
        m, h = self.m, self.m.settlement_finance
        with m.SessionLocal() as db:
            credit = db.scalar(select(m.settlement_version_funding['Credit']))
            pid = credit.position_id
            target = db.scalar(select(h['Position']).where(h['Position'].settlement_id == self.target_id,
                h['Position'].kind == 'cost'))
            target_id, target_amount = target.id, str(target.amount)
            self.assertFalse(h['batch_status'](db, self.batch_id)['funds_settled'])
            self.assertEqual(h['balance'](db, db.get(h['Position'], pid)), 5)
        target_body = dict(request_id='target-pay', kind='payout', mode='manual', reference='TARGET-PAYOUT',
            allocations=[dict(position_id=target_id, amount=target_amount)])
        self.request('POST', '/api/settlement-finance/transactions', actor='finance', expected=409, json=target_body)
        body = dict(request_id='vc-credit-pay', kind='payout', mode='manual', reference='ORIGINAL-PAYER-RETURN',
                    allocations=[dict(position_id=pid, amount='5.00')])
        self.request('POST', '/api/settlement-finance/transactions', actor='finance', expected=409,
                     json=dict(body, request_id='vc-credit-simulated', mode='simulated'))
        tx = self.request('POST', '/api/settlement-finance/transactions', actor='finance', json=body)
        review = dict(request_id='vc-credit-reviewed', expected_version=1, note='Independent return payment review')
        self.request('POST', f'/api/settlement-finance/transactions/{tx["id"]}/confirm', actor='finance', expected=403, json=review)
        self.request('POST', f'/api/settlement-finance/transactions/{tx["id"]}/confirm', actor='platform', json=review)
        self.request('POST', '/api/settlement-finance/transactions', actor='finance', json=target_body)
        with m.SessionLocal() as db:
            self.assertEqual(h['balance'](db, db.get(h['Position'], pid)), 0)
            self.assertEqual(db.get(m.Settlement, self.old_id).status, 'locked')


    @unittest.skipUnless(os.environ.get('CLEARING_TEST_DATABASE_URL'), 'requires isolated PostgreSQL')
    def test_concurrent_same_source_vc_and_legacy_reversal_cannot_overdraw(self):
        from decimal import Decimal
        self.funded_scenario()
        m, h = self.m, self.m.settlement_finance
        with m.SessionLocal() as db:
            other = m.Settlement(settlement_no='ISOLATED-CONCURRENT-ADJUSTMENT', order_id=self.order_id,
                status='pending', reference_settlement_id='original', gross_amount=-100, net_amount=-100,
                cost_amount=Decimal('-62.50'), profit_amount=Decimal('-37.50'),
                platform_fee=Decimal('-7.50'), provider_share=-30, service_share=0, expert_fee=0, channel_fee=0)
            db.add(other)
            db.flush()
            ids = {self.old_id, other.id}
            db.commit()
        barrier, mutex, observed = threading.Barrier(2), threading.Lock(), set()

        def simultaneous(connection, cursor, statement, parameters, context, many):
            if 'FOR UPDATE' not in statement.upper() or not isinstance(parameters, dict):
                return
            parent = next((value for value in parameters.values() if isinstance(value, str) and value in ids), None)
            if parent:
                connection.info['version_race_parent'] = parent
            parent = connection.info.get('version_race_parent')
            if ('settlement_finance_baselines' not in statement or parent not in ids
                    or 'original' not in parameters.values()):
                return
            with mutex:
                if parent in observed:
                    return
                observed.add(parent)
            barrier.wait(timeout=10)

        event.listen(m.engine, 'before_cursor_execute', simultaneous)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.client.post, f'/api/settlements/{sid}/lock',
                    headers={'Authorization': 'Bearer ' + self.tokens['finance']}) for sid in sorted(ids)]
                responses = [f.result(timeout=40) for f in futures]
        finally:
            event.remove(m.engine, 'before_cursor_execute', simultaneous)
        self.assertEqual(observed, ids)
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409], [r.text for r in responses])
        with m.SessionLocal() as db:
            captured = db.scalars(select(h['Baseline']).where(h['Baseline'].settlement_id.in_(ids))).all()
            self.assertEqual(len(captured), 1)
            records = h['source_reversals'](db, 'original')
            self.assertLessEqual(sum(abs(Decimal(r['gross_amount'])) for r in records), 160)
            self.assertLessEqual(db.scalar(select(func.coalesce(func.sum(m.settlement_version_funding['Credit'].amount), 0))), 10)

    @unittest.skipUnless(os.environ.get('CLEARING_TEST_DATABASE_URL'), 'requires isolated PostgreSQL')
    def test_concurrent_credit_payout_double_confirmation_applies_only_once(self):
        from decimal import Decimal
        self.funded_scenario()
        m, h = self.m, self.m.settlement_finance
        for sid in (self.old_id, self.target_id):
            self.request('POST', f'/api/settlements/{sid}/lock', actor='finance')
        self.request('POST', f'/api/settlement-batches/{self.batch_id}/confirm', actor='finance', json={})
        with m.SessionLocal() as db:
            pid = db.scalar(select(m.settlement_version_funding['Credit'].position_id))
        transactions = [self.request('POST', '/api/settlement-finance/transactions', actor='finance', json=dict(
            request_id='concurrent-credit-' + str(i), kind='payout', mode='manual', reference='RETURN-' + str(i),
            allocations=[dict(position_id=pid, amount='5.00')]))['id'] for i in range(2)]
        barrier, mutex, observed = threading.Barrier(2), threading.Lock(), set()

        def simultaneous(connection, cursor, statement, parameters, context, many):
            if 'pg_advisory_xact_lock' not in statement:
                return
            backend = connection.connection.driver_connection.info.backend_pid
            with mutex:
                if backend in observed:
                    return
                observed.add(backend)
            barrier.wait(timeout=10)

        event.listen(m.engine, 'before_cursor_execute', simultaneous)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.client.post, f'/api/settlement-finance/transactions/{tid}/confirm',
                    headers={'Authorization': 'Bearer ' + self.tokens['platform']},
                    json=dict(request_id='confirm-' + tid, expected_version=1, note='Independent manual confirmation'))
                    for tid in transactions]
                responses = [f.result(timeout=40) for f in futures]
        finally:
            event.remove(m.engine, 'before_cursor_execute', simultaneous)
        self.assertEqual(len(observed), 2, 'Two real PostgreSQL connections must reach the funds guard')
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409], [r.text for r in responses])
        with m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(h['Confirmation']).where(
                h['Confirmation'].transaction_id.in_(transactions))), 1)
            self.assertEqual(h['applied'](db, pid), Decimal('5'))
            self.assertEqual(h['balance'](db, db.get(h['Position'], pid)), 0)


class VersionFundingRoundingTests(unittest.TestCase):
    def test_high_cost_85_80_75_is_monotone_for_every_cent(self):
        source = SimpleNamespace(revenue=Decimal('85'), cost=Decimal('80'))
        snapshot = dict(shares=dict(zip(SHARES, ('2', '3', '0', '0', '0'))))
        previous = cumulative_target(source, snapshot, Decimal('75'), Decimal(0))
        for cent in range(1, 7501):
            current = cumulative_target(source, snapshot, Decimal('75'), Decimal(cent) / 100)
            self.assertTrue(all(current[i] >= previous[i] for i in range(3)))
            self.assertTrue(all(current[3][k] >= previous[3][k] for k in SHARES))
            self.assertEqual(sum(current[3].values()), current[2])
            previous = current
        self.assertEqual(previous[:3], (Decimal('85'), Decimal('80'), Decimal('5')))


if __name__ == '__main__':
    unittest.main()
