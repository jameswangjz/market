"""Disposable SQLite ledger tests; no application import or business data."""
import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Column, String, Numeric, create_engine, select, func
from sqlalchemy.orm import declarative_base, Session
from sqlalchemy.pool import StaticPool

from app.settlement_finance import install, SHARES


class FinanceTests(unittest.TestCase):
    def setUp(self):
        Base = declarative_base()

        class Enterprise(Base):
            __tablename__ = 'enterprises'
            id = Column(String, primary_key=True)
            name = Column(String)
            verification_status = Column(String, default='verified')

        class Membership(Base):
            __tablename__ = 'memberships'
            id = Column(String, primary_key=True)
            user_id = Column(String)
            enterprise_id = Column(String)
            role = Column(String)
            status = Column(String)
            business_roles = Column(String)

        class Order(Base):
            __tablename__ = 'orders'
            id = Column(String, primary_key=True)
            order_no = Column(String)
            provider_enterprise_id = Column(String)
            economic_snapshot_json = Column(String)
            business_type = Column(String, default='purchase')

        class Settlement(Base):
            __tablename__ = 'settlements'
            id = Column(String, primary_key=True)
            order_id = Column(String)
            settlement_no = Column(String)
            status = Column(String)
            reference_settlement_id = Column(String, default='')
            refund_id = Column(String, default='')
            gross_amount = Column(Numeric(14, 2))
            cost_amount = Column(Numeric(14, 2))
            profit_amount = Column(Numeric(14, 2))
            platform_fee = Column(Numeric(14, 2))
            provider_share = Column(Numeric(14, 2))
            service_share = Column(Numeric(14, 2))
            expert_fee = Column(Numeric(14, 2))
            channel_fee = Column(Numeric(14, 2))

        class SettlementLine(Base):
            __tablename__ = 'settlement_lines'
            id = Column(String, primary_key=True)
            batch_id = Column(String)
            settlement_id = Column(String)
            status = Column(String, default='pending')

        class SettlementBatch(Base):
            __tablename__ = 'settlement_batches'
            id = Column(String, primary_key=True)
            status = Column(String)

        class Payment(Base):
            __tablename__ = 'payments'
            id = Column(String, primary_key=True)
            order_id = Column(String)
            payment_no = Column(String)
            status = Column(String)
            amount = Column(Numeric(14, 2))

        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        self.db = Session(self.engine)
        self.user = SimpleNamespace(id='maker', platform_role='finance_settlement', is_active=True, verified_status='verified')
        self.reviewer = SimpleNamespace(id='reviewer', platform_role='platform_operator', is_active=True, verified_status='verified')
        self.audit = Mock()
        self.app = FastAPI()
        def session():
            yield self.db
        self.ns = dict(Base=Base, Settlement=Settlement, SettlementLine=SettlementLine,
            SettlementBatch=SettlementBatch, Payment=Payment, Order=Order, Enterprise=Enterprise,
            Membership=Membership, audit=self.audit, app=self.app,
            current_user=lambda: self.user, db_session=session)
        self.h = install(self.ns)
        Base.metadata.create_all(self.engine)
        self.db.add(Enterprise(id='provider', name='Provider'))
        self.db.add(SettlementBatch(id='fixture', status='confirmed'))
        self.order = Order(id='o', order_no='ORD-1', provider_enterprise_id='provider',
                           economic_snapshot_json=json.dumps(dict(currency='CNY')))
        self.db.add(self.order)
        self.db.add(Payment(id='payment', order_id='o', payment_no='PAY-1', status='paid', amount=150))
        self.db.commit()
        self.Settlement = Settlement

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def settlement(self, identifier='s', factor=1, reference='', status='locked'):
        s = self.Settlement(id=identifier, settlement_no='SET-' + identifier, order_id='o', status=status,
            reference_settlement_id=reference, gross_amount=150*factor, cost_amount=100*factor,
            profit_amount=50*factor, platform_fee=10*factor, provider_share=40*factor,
            service_share=0, expert_fee=0, channel_fee=0)
        self.db.add(s)
        self.db.add(self.ns['SettlementLine'](id='fixture-' + identifier, batch_id='fixture', settlement_id=identifier))
        self.db.flush()
        return s

    def capture(self, **kwargs):
        s = self.settlement(**kwargs)
        result = self.h['capture'](self.db, s)
        self.db.commit()
        self.h['reconcile_offsets'](self.db, 'fixture')
        self.db.commit()
        return result

    def positions(self, settlement='s'):
        return self.db.scalars(select(self.h['Position']).where(self.h['Position'].settlement_id == settlement)).all()

    def payout(self, positions, key='draft', amounts=None, **kwargs):
        body = dict(kind='payout', mode='simulated', reference='manual-evidence', request_id=key,
            allocations=[dict(position_id=p.id, amount=str(amounts[i] if amounts else p.amount))
                         for i, p in enumerate(positions)], **kwargs)
        return self.h['draft'](self.db, self.user, body), body

    def confirm(self, transaction, key='confirm', user=None):
        return self.h['confirm'](self.db, user or self.reviewer, transaction['id'],
                                dict(request_id=key, expected_version=1, note='Checked'))

    def test_cost_and_profit_conservation_and_immutable_snapshot(self):
        baseline = self.capture()
        positions = self.positions()
        self.assertEqual(sorted((p.kind, p.direction, p.amount) for p in positions),
            [('cost', 'payable', Decimal('100')), ('profit', 'internal', Decimal('10')),
             ('profit', 'payable', Decimal('40'))])
        original = baseline.snapshot_json
        s = self.db.get(self.Settlement, 's')
        s.provider_share = 500
        self.db.flush()
        self.assertIs(self.h['capture'](self.db, s), baseline)
        self.assertEqual(baseline.snapshot_json, original)
        baseline.cost = 12
        with self.assertRaises(ValueError):
            self.db.flush()
        self.db.rollback()
        position = self.positions()[0]
        self.db.delete(position)
        with self.assertRaises(ValueError):
            self.db.flush()
        self.db.rollback()

    def test_draft_is_not_payment_merge_cost_profit_and_idempotency(self):
        self.capture()
        payable = [p for p in self.positions() if p.direction == 'payable']
        tx, body = self.payout(payable)
        self.assertEqual(tx['status'], 'draft')
        self.assertEqual(sum(self.h['balance'](self.db, p) for p in payable), 140)
        self.assertEqual(self.h['draft'](self.db, self.user, body)['id'], tx['id'])
        with self.assertRaises(HTTPException) as error:
            self.confirm(tx, user=self.user)
        self.assertEqual(error.exception.status_code, 403)
        result = self.confirm(tx)
        self.assertEqual(result['status'], 'confirmed')
        self.assertFalse(result['bank_receipt'])
        self.assertEqual(sum(self.h['balance'](self.db, p) for p in payable), 0)
        self.assertEqual(self.confirm(tx), result)
        body['reference'] = 'different'
        with self.assertRaises(HTTPException) as error:
            self.h['draft'](self.db, self.user, body)
        self.assertEqual(error.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            self.payout(payable, key='overpay')

    def test_stale_draft_rechecked_and_failed_audit_rolls_back(self):
        self.capture()
        p = next(p for p in self.positions() if p.kind == 'cost')
        first, _ = self.payout([p], key='one')
        second, _ = self.payout([p], key='two')
        self.db.commit()
        self.audit.side_effect = RuntimeError('audit unavailable')
        with self.assertRaises(RuntimeError):
            self.confirm(first)
        self.assertEqual(self.h['balance'](self.db, p), 100)
        self.assertIsNone(self.db.get(self.h['Confirmation'], first['id']))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.h['Allocation'])), 0)
        self.audit.side_effect = None
        self.confirm(first)
        with self.assertRaises(HTTPException):
            self.confirm(second, key='second')
        self.assertIsNone(self.db.get(self.h['Confirmation'], second['id']))

    def test_negative_offsets_unpaid_and_keeps_cash_recovery_separate(self):
        self.capture()
        cost = next(p for p in self.positions() if p.kind == 'cost')
        tx, _ = self.payout([cost], amounts=[75])
        self.confirm(tx)
        self.db.commit()
        self.capture(identifier='negative', factor=Decimal('-.5'), reference='s')
        negative = self.positions('negative')
        debt = next(p for p in negative if p.kind == 'cost')
        self.assertEqual(self.h['balance'](self.db, cost), 0)
        self.assertEqual(self.h['balance'](self.db, debt), 25)
        profit = next(p for p in negative if p.share_kind == 'provider_share')
        self.assertEqual(self.h['balance'](self.db, profit), 0)
        offsets = self.db.scalars(select(self.h['Transaction']).where(self.h['Transaction'].kind == 'offset')).all()
        self.assertEqual(len(offsets), 2)
        self.assertFalse(self.h['describe'](self.db, 'negative')['funds_settled'])
        recovery = self.h['draft'](self.db, self.user, dict(kind='recovery', mode='manual', reference='receipt-1',
            request_id='recovery', allocations=[dict(position_id=debt.id, amount='25')]))
        self.confirm(recovery, key='recovery-confirm')
        self.assertEqual(self.h['balance'](self.db, debt), 0)
        self.assertTrue(self.h['describe'](self.db, 'negative')['funds_settled'])

    def test_missing_baseline_waits_legacy_paid_blocks_and_overreverse_fails(self):
        waiting = self.capture(identifier='missing', factor=-1)
        self.assertEqual(waiting['status'], 'waiting')
        self.assertEqual(self.positions('missing'), [])
        self.capture(status='paid')
        p = next(p for p in self.positions() if p.kind == 'cost')
        with self.assertRaises(HTTPException):
            self.payout([p])
        self.capture(identifier='n', factor=-1, reference='s')
        self.assertEqual(self.h['balance'](self.db, next(p for p in self.positions('n') if p.kind == 'cost')), 100)
        with self.assertRaises(HTTPException):
            self.capture(identifier='n2', factor=Decimal('-.01'), reference='s')

    def test_permissions_scope_totals_paging_and_get_no_writes(self):
        self.capture()
        outsider = SimpleNamespace(id='buyer', platform_role='', is_active=True, verified_status='verified')
        with self.assertRaises(HTTPException):
            self.h['list_positions'](self.db, outsider)
        self.db.add(self.ns['Membership'](id='m', user_id='buyer', enterprise_id='provider', role='enterprise_admin',
            status='active', business_roles='provider'))
        self.db.commit()
        response = self.h['list_positions'](self.db, outsider, page_size=1, enterprise_id='provider')
        self.assertEqual(response['total'], 3)
        self.assertEqual(response['summary']['CNY']['payable'], '140.00')
        with self.assertRaises(HTTPException):
            self.h['list_positions'](self.db, outsider, enterprise_id='other')
        with self.assertRaises(HTTPException):
            self.h['draft'](self.db, outsider, {})
        self.user.platform_role = 'security_compliance'
        before = self.audit.call_count
        client = TestClient(self.app)
        self.assertEqual(client.get('/api/settlement-finance/positions?page_size=1').json()['total'], 3)
        self.assertEqual(client.get('/api/settlement-finance/transactions').json()['total'], 0)
        self.assertEqual(self.audit.call_count, before)
        self.assertFalse(self.db.dirty or self.db.new or self.db.deleted)
        with self.assertRaises(HTTPException):
            self.payout([self.positions()[0]])

    def test_validation_subject_currency_and_unassigned(self):
        self.capture()
        p = next(p for p in self.positions() if p.kind == 'cost')
        for bad in ('NaN', 'Infinity', '-1', '0', '1.001', '100.01'):
            with self.subTest(bad=bad), self.assertRaises(HTTPException):
                self.payout([p], key=bad, amounts=[bad])
        self.ns['settlement_parties'] = dict(allocations=lambda db, order, role, amount:
            [dict(participant_id='', participant_name='Unassigned', amount=amount,
                  currency=json.loads(order.economic_snapshot_json)['currency'])])
        s = self.settlement(identifier='third')
        s.provider_share, s.expert_fee = 30, 10
        self.db.flush()
        self.h['capture'](self.db, s)
        expert = next(p for p in self.positions('third') if p.share_kind == 'expert_fee')
        self.assertEqual(expert.blocked_reason, 'missing_subject_mapping')
        with self.assertRaises(HTTPException):
            self.payout([expert], key='expert')
        s2 = self.settlement(identifier='euro')
        self.order.economic_snapshot_json = json.dumps(dict(currency='EUR'))
        self.db.flush()
        self.h['capture'](self.db, s2)
        euro = next(p for p in self.positions('euro') if p.kind == 'cost')
        with self.assertRaises(HTTPException):
            self.payout([p, euro], key='mixed-currency')

    def test_refund_uses_actual_baseline_and_cumulative_difference(self):
        refund = SimpleNamespace(id='refund', order_id='o', amount=Decimal('50'))
        self.assertEqual(self.h['refund_basis'](self.db, refund, {})['status'], 'waiting')
        self.capture()
        parts = [self.h['refund_basis'](self.db, refund, dict(original_amount='150', cumulative_refund_amount=str(n)))
                 for n in (50, 100, 150)]
        self.assertTrue(all(p['status'] == 'ready' for p in parts))
        self.assertEqual(sum(p['cost_amount'] for p in parts), -100)
        self.assertEqual(sum(p['profit_amount'] for p in parts), -50)
        for k, expected in zip(SHARES, (-10, -40, 0, 0, 0)):
            self.assertEqual(sum(p['shares'][k] for p in parts), expected)
        self.ns['settlement_parties'] = dict(cost_exception=lambda db, rid: {'approved': True})
        self.assertEqual(self.h['refund_basis'](self.db, refund,
            dict(original_amount='150', cumulative_refund_amount='50'))['status'], 'unsupported')

    def test_batch_status_requires_coverage_and_separates_recovery(self):
        self.capture()
        self.assertEqual(self.h['batch_status'](self.db, 'fixture'), dict(positive_payment_complete=False,
            funds_settled=False, payable_balance='140.00', receivable_balance='0.00', has_positions=True))
        tx, _ = self.payout([p for p in self.positions() if p.direction == 'payable'])
        self.confirm(tx)
        self.db.commit()
        self.capture(identifier='n', factor=Decimal('-.5'), reference='s')
        status = self.h['batch_status'](self.db, 'fixture')
        self.assertTrue(status['positive_payment_complete'])
        self.assertFalse(status['funds_settled'])
        self.assertEqual(status['receivable_balance'], '70.00')
        self.settlement(identifier='unknown')
        self.assertFalse(self.h['batch_status'](self.db, 'fixture')['positive_payment_complete'])

    def test_manual_offset_evidence_and_wrong_counter_rollback(self):
        self.capture()
        tx, _ = self.payout([p for p in self.positions() if p.direction == 'payable'])
        self.confirm(tx)
        self.db.commit()
        self.capture(identifier='n', factor=Decimal('-.5'), reference='s')
        self.capture(identifier='future')
        debt = next(p for p in self.positions('n') if p.kind == 'cost')
        future = next(p for p in self.positions('future') if p.kind == 'cost')
        body = dict(kind='offset', mode='manual', reference='approved-offset', request_id='offset',
            allocations=[dict(position_id=debt.id, amount='20.00')], counter_position_id=future.id)
        offset = self.h['draft'](self.db, self.user, body)
        self.assertEqual(self.h['balance'](self.db, debt), 50)
        self.confirm(offset, key='offset-confirm')
        self.assertEqual(self.h['balance'](self.db, debt), 30)
        self.assertEqual(self.h['balance'](self.db, future), 80)
        body.update(request_id='bad-offset', counter_position_id=debt.id)
        with self.assertRaises(HTTPException):
            self.h['draft'](self.db, self.user, body)
        self.assertEqual(self.h['balance'](self.db, debt), 30)

    def test_capture_conservation_and_audit_failure_are_atomic(self):
        s = self.settlement()
        s.platform_fee = 11
        self.db.flush()
        with self.assertRaises(HTTPException):
            self.h['capture'](self.db, s)
        self.assertIsNone(self.db.get(self.h['Baseline'], s.id))
        s.platform_fee = 10
        self.db.commit()
        self.audit.side_effect = RuntimeError('audit failed')
        with self.assertRaises(RuntimeError):
            self.h['capture'](self.db, s)
        self.assertIsNone(self.db.get(self.h['Baseline'], s.id))
        self.assertEqual(self.positions(), [])

    def test_fractional_cost_with_zero_provider_share_stays_conservative(self):
        s = self.settlement()
        s.gross_amount, s.cost_amount, s.profit_amount = Decimal('.02'), Decimal('.01'), Decimal('.01')
        s.platform_fee, s.provider_share = Decimal('.01'), 0
        self.db.get(self.ns['Payment'], 'payment').amount = Decimal('.02')
        self.db.flush()
        self.h['capture'](self.db, s)
        refund = SimpleNamespace(id='r', order_id='o', amount=Decimal('.01'))
        results = [self.h['refund_basis'](self.db, refund,
            dict(original_amount='.02', cumulative_refund_amount=n)) for n in ('.01', '.02')]
        self.assertTrue(all(r['status'] == 'ready' for r in results))
        for r in results:
            self.assertEqual(sum(r['shares'].values()), r['profit_amount'])
            self.assertTrue(all(v <= 0 for v in r['shares'].values()))
        self.assertEqual(sum(r['shares']['platform_fee'] for r in results), Decimal('-.01'))

    def test_batch_confirmation_required_on_draft_and_rechecked_on_confirm(self):
        self.capture()
        p = next(p for p in self.positions() if p.kind == 'cost')
        batch = self.db.get(self.ns['SettlementBatch'], 'fixture')
        for state in ('generated', 'pending_confirm', 'rolled_back', 'superseded'):
            batch.status = state
            self.db.flush()
            with self.subTest(state=state), self.assertRaises(HTTPException) as error:
                self.payout([p], key=state)
            self.assertEqual(error.exception.status_code, 409)
        batch.status = 'confirmed'
        self.db.commit()
        tx, _ = self.payout([p])
        self.db.commit()
        batch.status = 'rolled_back'
        self.db.commit()
        with self.assertRaises(HTTPException):
            self.confirm(tx)
        self.assertEqual(self.h['balance'](self.db, p), 100)
        batch.status = 'confirmed'
        self.db.get(self.ns['SettlementLine'], 'fixture-s').status = 'superseded'
        self.db.commit()
        with self.assertRaises(HTTPException):
            self.payout([p], key='no-active-line')

    def test_malformed_transaction_structures_return_422(self):
        self.capture()
        p = next(p for p in self.positions() if p.kind == 'cost')
        body = dict(kind='payout', mode='manual', reference='r', request_id='bad',
                    allocations=[dict(position_id=p.id, amount='1')])
        cases = [dict(allocations=value) for value in ('bad', {}, None, [], ['bad'], [None], [[]],
            [dict(position_id=[], amount='1')], [dict(position_id=p.id, amount={})])]
        cases += [dict(note={}), dict(note=[]), dict(counter_position_id=[]), dict(counter_position_id={}),
                  dict(counter_position_id=12), dict(counter_position_id='')]
        client = TestClient(self.app)
        for change in cases:
            with self.subTest(change=change):
                response = client.post('/api/settlement-finance/transactions', json=dict(body, **change))
                self.assertEqual(response.status_code, 422, response.text)
        tx, _ = self.payout([p])
        self.assertEqual(client.post('/api/settlement-finance/transactions/' + tx['id'] + '/confirm',
            json=dict(request_id='bad-note', expected_version=1, note={})).status_code, 422)

    def test_auto_offset_waits_and_manual_offset_rechecks_batch(self):
        self.capture()
        batch = self.db.get(self.ns['SettlementBatch'], 'fixture')
        batch.status = 'pending_confirm'
        self.db.commit()
        self.capture(identifier='n', factor=Decimal('-.5'), reference='s')
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.h['Allocation'])), 0)
        debt = next(p for p in self.positions('n') if p.kind == 'cost')
        original = next(p for p in self.positions() if p.kind == 'cost')
        body = dict(kind='offset', mode='manual', reference='offset-ref', request_id='offset-pending',
            allocations=[dict(position_id=debt.id, amount='25')], counter_position_id=original.id)
        with self.assertRaises(HTTPException):
            self.h['draft'](self.db, self.user, body)
        batch.status = 'confirmed'
        self.db.commit()
        tx = self.h['draft'](self.db, self.user, body)
        self.db.commit()
        batch.status = 'rolled_back'
        self.db.commit()
        with self.assertRaises(HTTPException):
            self.confirm(tx)
        self.assertEqual(self.h['balance'](self.db, debt), 50)
        self.assertEqual(self.h['balance'](self.db, original), 100)

    def test_unfunded_revenue_adjustment_blocks_every_external_position(self):
        s = self.settlement()
        s.gross_amount, s.profit_amount, s.provider_share = 160, 60, 50
        self.db.add(self.ns['Payment'](id='unpaid', order_id='o', payment_no='PAY-UNPAID', status='unpaid', amount=10))
        self.db.flush()
        baseline = self.h['capture'](self.db, s)
        self.db.commit()
        snapshot = json.loads(baseline.snapshot_json)
        self.assertEqual(baseline.revenue, 160)
        self.assertEqual(snapshot['actual_receipts'], '150.00')
        self.assertEqual(snapshot['blocked_reason'], 'unfunded_settlement_adjustment')
        self.assertEqual([r['payment_id'] for r in snapshot['receipt_records']], ['payment'])
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)
        self.assertEqual(self.db.get(self.ns['Payment'], 'unpaid').status, 'unpaid')
        for p in self.positions():
            if p.direction == 'internal':
                continue
            self.assertEqual(p.blocked_reason, 'unfunded_settlement_adjustment')
            with self.assertRaises(HTTPException):
                self.payout([p], key=p.id)
        self.assertFalse(self.h['batch_status'](self.db, 'fixture')['positive_payment_complete'])

    def test_cost_only_adjustment_and_multiple_paid_receipts_allow_payment(self):
        payment = self.db.get(self.ns['Payment'], 'payment')
        payment.amount = 100
        self.db.add(self.ns['Payment'](id='second', order_id='o', payment_no='PAY-2', status='paid', amount=50))
        s = self.settlement()
        s.cost_amount, s.profit_amount, s.platform_fee, s.provider_share = 90, 60, 12, 48
        self.db.flush()
        baseline = self.h['capture'](self.db, s)
        self.db.commit()
        self.assertEqual(json.loads(baseline.snapshot_json)['actual_receipts'], '150.00')
        payable = [p for p in self.positions() if p.direction == 'payable']
        self.assertTrue(all(not p.blocked_reason for p in payable))
        tx, _ = self.payout(payable)
        self.confirm(tx)
        self.assertEqual(sum(self.h['balance'](self.db, p) for p in payable), 0)

    def test_multi_subject_refunds_use_frozen_entries_and_cumulative_cents(self):
        def parties(db, order, role, amount):
            if role in ('expert', 'channel'):
                amounts = ('3.33', '6.67') if role == 'expert' else ('5', '5')
                return [dict(participant_id=role + str(i), participant_name=role + str(i),
                             amount=value, currency='CNY') for i, value in enumerate(amounts)]
            return []
        self.ns['settlement_parties'] = dict(allocations=parties)
        s = self.settlement()
        s.provider_share, s.expert_fee, s.channel_fee = 20, 10, 10
        self.db.flush()
        original = self.h['capture'](self.db, s)
        self.db.commit()
        # New configuration must not alter the frozen identities or weights.
        self.ns['settlement_parties']['allocations'] = Mock(side_effect=AssertionError('current mapping consulted'))
        refund = SimpleNamespace(id='r', order_id='o', amount=Decimal('50'))
        totals = {}
        source = json.loads(original.snapshot_json)['entries']
        key = lambda e: (e['kind'], e['share_kind'], e['participant_type'], e['participant_id'])
        limits = {key(e): Decimal(e['amount']) for e in source}
        for index, cumulative in enumerate((50, 100, 150)):
            basis = self.h['refund_basis'](self.db, refund,
                dict(original_amount='150', cumulative_refund_amount=str(cumulative)))
            self.assertEqual(basis['status'], 'ready')
            negative = self.settlement(identifier='negative' + str(index), factor=Decimal('-.333333'), reference='s')
            for field in ('gross_amount', 'cost_amount', 'profit_amount'):
                setattr(negative, field, basis[field])
            for field, value in basis['shares'].items():
                setattr(negative, field, value)
            self.db.flush()
            captured = self.h['capture'](self.db, negative)
            self.db.commit()
            entries = json.loads(captured.snapshot_json)['entries']
            self.assertEqual(sum(Decimal(e['amount']) for e in entries), 50)
            for e in entries:
                totals[key(e)] = totals.get(key(e), Decimal(0)) + Decimal(e['amount'])
                self.assertLessEqual(totals[key(e)], limits[key(e)])
        self.assertEqual(totals, limits)

    def test_superseded_baseline_remains_but_current_positions_and_actions_exclude_it(self):
        baseline = self.capture()
        cost = next(p for p in self.positions() if p.kind == 'cost')
        tx, _ = self.payout([cost])
        self.db.commit()
        self.db.get(self.Settlement, 's').status = 'superseded'
        self.db.commit()
        response = self.h['list_positions'](self.db, self.user)
        self.assertEqual(response['total'], 0)
        self.assertEqual(response['summary'], {})
        self.assertEqual(response['totals'], {})
        self.assertEqual(self.h['batch_status'](self.db, 'fixture')['payable_balance'], '0.00')
        self.assertFalse(self.h['batch_status'](self.db, 'fixture')['has_positions'])
        self.assertEqual(self.db.get(self.h['Baseline'], 's').snapshot_hash, baseline.snapshot_hash)
        self.assertEqual(len(self.positions()), 3)
        with self.assertRaises(HTTPException):
            self.payout([cost], key='superseded-draft')
        with self.assertRaises(HTTPException):
            self.confirm(tx)
        self.assertEqual(self.h['balance'](self.db, cost), 100)
        refund = SimpleNamespace(id='r', order_id='o', amount=Decimal('50'))
        self.assertEqual(self.h['refund_basis'](self.db, refund,
            dict(original_amount=150, cumulative_refund_amount=50))['status'], 'waiting')
        result = self.capture(identifier='n', factor=Decimal('-.5'), reference='s')
        self.assertEqual(result['status'], 'waiting')

    def test_unconfirmed_capture_and_read_queries_never_require_payment_confirmation(self):
        batch = self.db.get(self.ns['SettlementBatch'], 'fixture')
        batch.status = 'generated'
        self.db.commit()
        baseline = self.capture()
        frozen = baseline.snapshot_json
        client = TestClient(self.app)
        for state in ('generated', 'pending_confirm'):
            batch.status = state
            self.db.commit()
            before = self.audit.call_count
            response = client.get('/api/settlement-finance/positions')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['total'], 3)
            self.assertEqual(response.json()['summary']['CNY']['payable'], '140.00')
            self.assertEqual(client.get('/api/settlement-finance/transactions').status_code, 200)
            self.assertEqual(self.h['describe'](self.db, 's')['revenue'], '150.00')
            self.assertEqual(self.h['batch_status'](self.db, 'fixture')['payable_balance'], '140.00')
            self.assertEqual(self.audit.call_count, before)
            self.assertFalse(self.db.dirty or self.db.new or self.db.deleted)
        self.db.get(self.Settlement, 's').status = 'superseded'
        self.db.get(self.ns['SettlementLine'], 'fixture-s').status = 'superseded'
        self.db.commit()
        replacement = self.capture(identifier='replacement')
        response = client.get('/api/settlement-finance/positions')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({p['settlement_id'] for p in response.json()['items']}, {replacement.settlement_id})
        self.assertEqual(self.db.get(self.h['Baseline'], 's').snapshot_json, frozen)


if __name__ == '__main__':
    unittest.main()
