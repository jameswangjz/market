"""QA-006 internal ledger HTTP acceptance; no external bank/four-ledger claim."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import qa_acceptance_harness as harness


class SettlementAcceptanceTests(harness.AcceptanceHTTPCase):
    PERIOD = dict(cycle='monthly', period_start='2026-09-01T00:00:00+00:00',
                  period_end='2026-10-01T00:00:00+00:00')

    def paid_order(self, paid_at='2026-09-15T12:00:00+00:00', created_at=None):
        oid = self.create_order()
        self.pay(oid)
        with self.m.SessionLocal() as db:
            payment = db.scalar(self.select(self.m.Payment).where(self.m.Payment.order_id == oid))
            payment.paid_at = datetime.fromisoformat(paid_at)
            if created_at:
                db.get(self.m.Order, oid).created_at = datetime.fromisoformat(created_at)
            db.commit()
        return oid

    def batch(self, **overrides):
        return self.request('POST', '/api/settlement-batches', actor='finance',
                            json=self.PERIOD | overrides)

    def batch_settlements(self, bid):
        return self.request('GET', '/api/settlements', actor='finance', params={'batch_id': bid})['items']

    def lock_all(self, bid):
        for row in self.batch_settlements(bid):
            self.request('POST', f'/api/settlements/{row["id"]}/lock', actor='finance')

    def adjustment(self):
        return dict(gross_amount=100, cost_amount=20, profit_amount=80,
                    platform_rate=10, provider_rate=60, service_rate=20, expert_rate=5,
                    channel_rate=5, reason='Correct documented cost')

    def test_monthly_selects_payment_time_half_open_boundaries_and_excludes_unpaid(self):
        inside = {self.paid_order(created_at='2026-08-10T00:00:00+00:00'),
                  self.paid_order(paid_at=self.PERIOD['period_start'])}
        outside = {self.paid_order(paid_at='2026-08-31T23:59:59+00:00'),
                   self.paid_order(paid_at=self.PERIOD['period_end']), self.create_order()}
        batch = self.batch(idempotency_key='monthly-payment-boundary')
        rows = self.batch_settlements(batch['id'])
        self.assertEqual({row['order_id'] for row in rows}, inside)
        self.assertFalse({row['order_id'] for row in rows} & outside)
        self.assertEqual(len(rows), 2)

    def test_explicit_order_ids_do_not_bypass_month_payment_filter(self):
        inside = self.paid_order()
        outside = self.paid_order(paid_at='2026-10-01T00:00:00+00:00')
        batch = self.batch(order_ids=[inside, outside], idempotency_key='explicit-period')
        self.assertEqual({row['order_id'] for row in self.batch_settlements(batch['id'])}, {inside})

    def test_completed_refund_is_negative_by_completion_month_without_rewriting_paid_original(self):
        oid = self.paid_order(paid_at='2026-08-15T00:00:00+00:00')
        original_batch = self.request('POST', '/api/settlement-batches', actor='finance',
            json={'order_ids': [oid], 'idempotency_key': 'original-order'})
        original = self.batch_settlements(original_batch['id'])[0]
        self.lock_all(original_batch['id'])
        self.request('POST', f'/api/settlement-batches/{original_batch["id"]}/confirm', actor='finance', json={})
        self.request('POST', f'/api/settlement-batches/{original_batch["id"]}/pay', actor='finance', json={})
        self.request('POST', f'/api/orders/{oid}/transition', actor='platform',
                     json={'action': 'approve_refund', 'refund_amount': 20, 'reason': 'Partial refund'})
        self.request('POST', f'/api/orders/{oid}/transition', actor='platform', json={'action': 'complete_refund'})
        refund = self.rows(self.m.Refund, order_id=oid)[0]
        with self.m.SessionLocal() as db:
            db.get(self.m.Refund, refund.id).completed_at = datetime(2026, 9, 20, tzinfo=timezone.utc)
            db.commit()
        batch = self.batch(idempotency_key='refund-completion-month')
        rows = self.batch_settlements(batch['id'])
        self.assertEqual(len(rows), 1)
        negative = self.rows(self.m.Settlement, id=rows[0]['id'])[0]
        self.assertTrue(negative.is_refund)
        self.assertEqual(negative.refund_id, refund.id)
        self.assertEqual(negative.reference_settlement_id, original['id'])
        self.assertEqual(Decimal(str(negative.net_amount)), Decimal('-20'))
        self.assertEqual(Decimal(str(negative.profit_amount)), Decimal('-18'))
        lines = self.rows(self.m.SettlementLine, batch_id=batch['id'])
        self.assertEqual(sum(line.amount for line in lines), negative.profit_amount)
        self.assertTrue(all(line.amount <= 0 for line in lines))
        unchanged = self.rows(self.m.Settlement, id=original['id'])[0]
        self.assertEqual((unchanged.status, float(unchanged.net_amount)), ('paid', 100))
        report = self.request('GET', '/api/settlement-reports', actor='finance')
        self.assertAlmostEqual(report['summary']['net_amount'], 80)

    def test_proposal_second_person_confirmation_updates_amounts_lines_and_audit(self):
        self.paid_order()
        bid = self.batch(idempotency_key='proposal-flow')['id']
        sid = self.batch_settlements(bid)[0]['id']
        proposal = self.request('POST', f'/api/settlements/{sid}/proposals', actor='finance', json=self.adjustment())
        self.assertEqual(self.rows(self.m.Settlement, id=sid)[0].status, 'disputed')
        before = self.counts()
        self.request('POST', f'/api/settlement-proposals/{proposal["id"]}/decision', actor='finance',
                     expected=409, json={'decision': 'approve'})
        self.assertEqual(self.counts(), before)
        decided = self.request('POST', f'/api/settlement-proposals/{proposal["id"]}/decision', actor='ops',
                               json={'decision': 'approve', 'comment': 'Independent verification'})
        self.assertEqual(decided['status'], 'accepted')
        settlement = self.rows(self.m.Settlement, id=sid)[0]
        self.assertEqual((settlement.status, float(settlement.profit_amount)), ('adjusted', 80))
        self.assertEqual(sum(line.amount for line in self.rows(self.m.SettlementLine, settlement_id=sid)), Decimal('80'))
        self.assertTrue({'create_settlement_adjustment_proposal', 'decide_settlement_adjustment_proposal'} <=
                        self.audit_actions(order_id=settlement.order_id))
        self.request('POST', f'/api/settlement-proposals/{proposal["id"]}/decision', actor='ops',
                     expected=409, json={'decision': 'approve'})

    def test_pending_proposal_cannot_be_locked_and_paid_without_decision(self):
        self.paid_order()
        bid = self.batch(idempotency_key='pending-proposal-guard')['id']
        sid = self.batch_settlements(bid)[0]['id']
        proposal = self.request('POST', f'/api/settlements/{sid}/proposals', actor='finance', json=self.adjustment())
        response = self.client.post(f'/api/settlements/{sid}/lock',
            headers={'Authorization': 'Bearer ' + self.tokens['finance']})
        evidence = {'lock_status': response.status_code}
        if response.status_code == 200:
            self.request('POST', f'/api/settlement-batches/{bid}/confirm', actor='finance', json={})
            self.request('POST', f'/api/settlement-batches/{bid}/pay', actor='finance', json={})
            evidence['settlement_status'] = self.rows(self.m.Settlement, id=sid)[0].status
            evidence['proposal_status'] = self.rows(self.m.SettlementAdjustmentProposal, id=proposal['id'])[0].status
        self.assertEqual(response.status_code, 409, evidence)
        self.assertEqual(self.rows(self.m.Settlement, id=sid)[0].status, 'disputed')

    def test_legacy_generate_cannot_overwrite_paid_settlement(self):
        oid = self.paid_order()
        bid = self.batch(idempotency_key='paid-regeneration')['id']
        sid = self.batch_settlements(bid)[0]['id']
        self.lock_all(bid)
        self.request('POST', f'/api/settlement-batches/{bid}/confirm', actor='finance', json={})
        self.request('POST', f'/api/settlement-batches/{bid}/pay', actor='finance', json={})
        self.request('POST', f'/api/settlements/generate/{oid}', actor='finance', expected=409, json={})
        self.assertEqual(self.rows(self.m.Settlement, id=sid)[0].status, 'paid')

    def test_batch_lock_confirm_payment_linkage_and_repeats_do_not_duplicate(self):
        self.paid_order()
        self.paid_order()
        bid = self.batch(idempotency_key='payment-linkage')['id']
        self.request('POST', f'/api/settlement-batches/{bid}/pay', actor='finance', expected=409, json={})
        self.request('POST', f'/api/settlement-batches/{bid}/confirm', actor='finance', expected=409, json={})
        self.lock_all(bid)
        self.assertEqual(self.rows(self.m.SettlementBatch, id=bid)[0].status, 'pending_confirm')
        self.request('POST', f'/api/settlement-batches/{bid}/confirm', actor='finance', json={})
        self.request('POST', f'/api/settlement-batches/{bid}/pay', actor='finance', json={})
        self.assertTrue(all(row['status'] == 'paid' for row in self.batch_settlements(bid)))
        lines = self.rows(self.m.SettlementLine, batch_id=bid)
        self.assertEqual(len(lines), 10)
        self.assertTrue(all(line.status == 'paid' and line.payment_no.startswith('SIM-PAY-') for line in lines))
        before = self.counts()
        ids = {line.id: line.payment_no for line in lines}
        self.request('POST', f'/api/settlement-batches/{bid}/pay', actor='finance', expected=409, json={})
        self.request('POST', f'/api/settlement-batches/{bid}/confirm', actor='finance', expected=409, json={})
        self.assertEqual({line.id: line.payment_no for line in self.rows(self.m.SettlementLine, batch_id=bid)}, ids)
        self.assertEqual(self.counts(), before)
        self.assertTrue({'confirm_settlement_batch', 'pay_settlement_batch'} <=
                        self.audit_actions(batch_no=self.rows(self.m.SettlementBatch, id=bid)[0].batch_no))
        audiences = {'buyer_tenant': {'buyer', 'buyer_admin'},
                     'provider_tenant': {'provider'}, '': {'platform', 'ops', 'finance'}}
        messages = self.rows(self.Message,
            target_id=self.rows(self.m.SettlementBatch, id=bid)[0].batch_no, title='清算批次付款完成')
        self.assertEqual({message.tenant_id or '' for message in messages}, set(audiences))
        for message in messages:
            users = {row.user_id for row in self.rows(self.Receipt, message_id=message.id)}
            self.assertEqual(users, audiences[message.tenant_id or ''])
        report = self.request('GET', '/api/settlement-reports', actor='finance')
        self.assertAlmostEqual(report['summary']['net_amount'], 200)
        self.request('GET', '/api/settlement-reports/export', actor='finance')

    def test_monthly_rebuild_supersedes_pending_and_blocks_confirmed(self):
        self.paid_order()
        first = self.batch(idempotency_key='rebuild-first')
        self.request('POST', '/api/settlement-batches', actor='finance', expected=409,
                     json=self.PERIOD | {'idempotency_key': 'rebuild-refused'})
        second = self.batch(idempotency_key='rebuild-second', rebuild=True)
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(self.rows(self.m.SettlementBatch, id=first['id'])[0].status, 'superseded')
        self.assertTrue(all(line.status == 'superseded' for line in self.rows(self.m.SettlementLine, batch_id=first['id'])))
        self.lock_all(second['id'])
        self.request('POST', f'/api/settlement-batches/{second["id"]}/confirm', actor='finance', json={})
        before = len(self.rows(self.m.SettlementBatch))
        self.request('POST', '/api/settlement-batches', actor='finance', expected=409,
                     json=self.PERIOD | {'idempotency_key': 'rebuild-confirmed', 'rebuild': True})
        self.assertEqual(len(self.rows(self.m.SettlementBatch)), before)

    def test_same_batch_request_is_idempotent_without_duplicate_lines_or_notifications(self):
        self.paid_order()
        first = self.batch(idempotency_key='same-batch')
        before = self.counts()
        lines = len(self.rows(self.m.SettlementLine))
        repeated = self.batch(idempotency_key='same-batch')
        self.assertEqual(repeated['id'], first['id'])
        self.assertTrue(repeated['idempotent'])
        self.assertEqual(self.counts(), before)
        self.assertEqual(len(self.rows(self.m.SettlementLine)), lines)

    def test_same_key_changed_period_must_not_return_unrelated_batch(self):
        self.paid_order()
        self.batch(idempotency_key='conflicting-period')
        self.request('POST', '/api/settlement-batches', actor='finance', expected=409,
            json=self.PERIOD | {'idempotency_key': 'conflicting-period',
                               'period_start': '2026-08-01T00:00:00+00:00',
                               'period_end': '2026-09-01T00:00:00+00:00'})

    def test_request_digest_normalizes_order_scope_and_rejects_changed_scope_rule_cycle(self):
        a, b = self.paid_order(), self.paid_order()
        first = self.batch(order_ids=[a, b], idempotency_key='digest-scope')
        batch = self.rows(self.m.SettlementBatch, id=first['id'])[0]
        self.assertEqual(len(batch.request_digest), 64)
        before, lines = self.counts(), len(self.rows(self.m.SettlementLine))
        repeated = self.batch(order_ids=[b, a, a], idempotency_key='digest-scope')
        self.assertEqual(repeated['id'], first['id'])
        for changes in ({'order_ids': [a]}, {'rule_id': batch.rule_id}, {'cycle': 'manual'}):
            self.request('POST', '/api/settlement-batches', actor='finance', expected=409,
                json=self.PERIOD | {'order_ids': [a, b], 'idempotency_key': 'digest-scope'} | changes)
        self.assertEqual(self.counts(), before)
        self.assertEqual(len(self.rows(self.m.SettlementLine)), lines)

    def test_explicit_request_key_and_order_count_limits(self):
        before = self.counts()
        for body in ({'idempotency_key': 'k' * 121}, {'order_ids': ['missing'] * 10001}):
            self.request('POST', '/api/settlement-batches', actor='finance', expected=422, json=body)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.rows(self.m.SettlementBatch), [])
        accepted = self.request('POST', '/api/settlement-batches', actor='finance',
            json={'idempotency_key': 'k' * 120, 'order_ids': ['missing'] * 10000})
        self.assertEqual(self.rows(self.m.SettlementBatch, id=accepted['id'])[0].idempotency_key, 'k' * 120)

    def test_long_automatic_key_is_hashed_and_repeat_is_idempotent(self):
        orders = [self.paid_order() for _ in range(5)]
        first = self.request('POST', '/api/settlement-batches', actor='finance', json={'order_ids': orders})
        stored = self.rows(self.m.SettlementBatch, id=first['id'])[0]
        self.assertLessEqual(len(stored.idempotency_key), 120)
        self.assertTrue(stored.idempotency_key.startswith('auto:'))
        before = self.counts()
        repeated = self.request('POST', '/api/settlement-batches', actor='finance', json={'order_ids': orders})
        self.assertEqual(repeated['id'], first['id'])
        self.assertEqual(self.counts(), before)

    def test_tenant_users_cannot_read_or_mutate_platform_financial_data(self):
        self.paid_order()
        bid = self.batch(idempotency_key='scope')['id']
        sid = self.batch_settlements(bid)[0]['id']
        before = self.counts()
        for actor in ('buyer', 'provider', 'outsider'):
            for path in ('/api/settlements', f'/api/settlements/{sid}/detail', '/api/settlement-batches',
                         f'/api/settlement-batches/{bid}/lines', '/api/settlement-reports',
                         '/api/settlement-reports/export', '/api/audit-logs'):
                with self.subTest(actor=actor, path=path):
                    self.request('GET', path, actor=actor, expected=403)
            self.request('POST', f'/api/settlements/{sid}/lock', actor=actor, expected=403)
            self.request('POST', f'/api/settlement-batches/{bid}/pay', actor=actor, expected=403, json={})
        self.request('GET', '/api/settlement-reports', actor='security')
        self.request('POST', f'/api/settlements/{sid}/lock', actor='security', expected=403)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.request('GET', '/api/notifications', actor='outsider')['items'], [])

    def test_payment_notification_failure_rolls_back_business_audit_and_payment_numbers(self):
        self.paid_order()
        bid = self.batch(idempotency_key='rollback-payment')['id']
        self.lock_all(bid)
        self.request('POST', f'/api/settlement-batches/{bid}/confirm', actor='finance', json={})
        before = self.counts()
        audit_count = len(self.rows(self.m.AuditLog))
        with self.fail_after_create('settlement'):
            self.request('POST', f'/api/settlement-batches/{bid}/pay', actor='finance', expected=500, json={})
        self.assertEqual(self.counts(), before)
        self.assertEqual(len(self.rows(self.m.AuditLog)), audit_count)
        self.assertEqual(self.rows(self.m.SettlementBatch, id=bid)[0].status, 'confirmed')
        self.assertTrue(all(line.status == 'locked' and not line.payment_no for line in self.rows(self.m.SettlementLine, batch_id=bid)))


if __name__ == '__main__':
    unittest.main()
