"""Funded retained-cost approvals and ledger helper; no live database access."""
import copy
import json
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import Column, String, func, select, update
from sqlalchemy.orm import Session

from app.settlement_parties import install as install_parties
from app.settlement_funded_cost_exception import digest
from app.settlement_funded_refund_capture import install as install_capture
import test_settlement_parties_multi_cost as party_fixture
import test_settlement_funded_refund as funded_fixture


class FundedCostPartyTests(unittest.TestCase):
    def setUp(self):
        self.f = party_fixture.MultiCostExceptionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ns, self.h = self.f.ns, self.f.h
        self.ns['settlement_funded_refund'] = {'raw_funded_basis': lambda db, refund: dict(
            status='ready', kind='confirmed_funded_refund', buyer_cash_amount=str(refund.amount),
            references=copy.deepcopy(self.f.refs))}

    def propose(self, **values):
        return self.f.propose(basis_kind='funded', **values)

    def test_funded_single_source_uses_actual_cash_and_normal_refund_cost_cap(self):
        self.f.refs[:] = self.f.refs[:1]
        self.f.refs[0]['gross_amount'] = '-53.33'
        e = self.propose(retained_cost='5', source_allocations=self.f.allocations(('5',)))
        self.assertEqual(e['refund_amount'], '50.00')
        self.assertEqual(e['baseline_id'], '')
        self.assertEqual(e['basis_kind'], 'funded')
        self.f.f.decision(e['id'])
        with Session(self.f.f.engine) as db:
            refund = db.get(self.ns['Refund'], 'r1')
            proof = self.h['cost_exception_funded_basis'](db, refund, self.f.refs)
            self.assertEqual(proof['status'], 'ready')
            self.assertEqual(self.h['cost_exception_multi_basis'](db, refund, self.f.refs)['status'], 'waiting')

    def test_pending_funded_and_legacy_reservations_share_source_budget(self):
        model = self.ns['settlement_finance']['Baseline']
        with Session(self.f.f.engine) as db:
            db.get(model, 'b2').order_id = 'o2'
            db.commit()
        self.f.f.exception(key='legacy', refund_id='r2', retained_cost='96')
        with Session(self.f.f.engine) as db:
            db.get(model, 'b2').order_id = 'o1'
            db.commit()
        self.propose(retained_cost='8', expected=409)
        with Session(self.f.f.engine) as db:
            row = db.scalar(select(self.h['CostException']).where(self.h['CostException'].refund_id == 'r2'))
            identity = row.id
        self.f.f.decision(identity, decision='reject')
        self.propose()
        with Session(self.f.f.engine) as db:
            db.get(model, 'b2').order_id = 'o2'
            db.commit()
        self.f.f.exception(key='legacy-after-funded', refund_id='r2', retained_cost='96', expected=409)

    def test_each_source_revalidated_without_first_source_fallback(self):
        e = self.propose()
        self.f.refs[1]['baseline_hash'] = 'tampered'
        self.f.f.decision(e['id'], expected=409)
        with Session(self.f.f.engine) as db:
            self.assertIsNone(db.get(self.h['Decision'], e['id']))

    def test_funded_source_also_counts_other_legacy_pending_reservations(self):
        self.propose()
        with Session(self.f.f.engine) as db:
            db.get(self.ns['settlement_finance']['Baseline'], 'b2').order_id = 'o2'
            db.get(self.ns['Refund'], 'r3').order_id = 'o1'
            db.commit()
        self.f.f.exception(key='legacy-pending', refund_id='r2', retained_cost='90')
        self.f.f.exception(key='legacy-over-pending', refund_id='r3', retained_cost='6', expected=409)

    def test_missing_raw_funded_hook_does_not_fall_back_or_recurse(self):
        self.ns['settlement_funded_refund'] = {}
        self.ns['refund_settlement']['raw_multi_basis'] = lambda *_: self.fail('wrong raw basis')
        self.propose(expected=409)

    def test_approved_pending_funded_settlement_requires_financial_refresh(self):
        e = self.propose()
        model = self.f.bind_assessment(dict(status='ready', kind='confirmed_funded_refund'))
        self.f.f.decision(e['id'])
        with Session(self.f.f.engine) as db:
            rows = db.scalars(select(model).order_by(model.version)).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual(json.loads(rows[-1].values_json)['status'], 'waiting')
            self.assertEqual(db.get(self.ns['Refund'], 'r1').status, 'completed')

    def test_locked_negative_without_baseline_cannot_be_rewritten(self):
        e = self.propose()
        self.f.bind_assessment(dict(status='ready', kind='confirmed_funded_refund'))
        with Session(self.f.f.engine) as db:
            db.get(self.ns['Settlement'], 'negative').status = 'locked'
            db.commit()
        self.f.f.decision(e['id'], expected=409)


class FundedCostApplicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = funded_fixture.FundedRefundTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.db, self.ns = self.fixture.db, self.fixture.ns
        class Product(self.ns['Base']):
            __tablename__ = 'products'
            id = Column(String, primary_key=True)
        self.ns['Product'] = Product
        self.parties = install_parties(self.ns)
        install_capture(self.ns, dict(self.f.h, audit=self.ns['audit']))
        self.ns['Base'].metadata.create_all(self.f.engine)
        self.cost = self.ns['settlement_funded_cost_exception']
        if getattr(self, 'external_funding', False):
            import test_settlement_funded_refund_normal_plan as external_fixture
            external_fixture.NormalPlanTests.external(self.fixture)
        else:
            self.fixture.funded()
        self.raw_installed = (self.ns['settlement_funded_refund'].get('raw_funded_basis')
                              or self.cost['raw_funded_basis'])

    def prepare(self, *, amount='30', cumulative='30', retained='5', kind='provider', key='r', multi=False):
        refund = self.fixture.refund(amount, cumulative, key)
        if multi:
            source = self.fixture.source_fixture()
            segment = dict(self.fixture.plans[key], refund_amount='30.00')
            other = dict(segment, segment_id='segment-2', original_amount='75.00',
                         cumulative_refund_amount='15.00', refund_amount='15.00')
            self.fixture.plans[key] = dict(segments=[segment, other])
            self.fixture.bindings += [dict(segment_id='segment-2', reference_settlement_id=source.settlement_id,
                                          baseline_hash=source.snapshot_hash)]
        if self.raw_installed and not multi:
            normal = self.raw_installed(self.db, refund)
        else:
            # Boundary-only fixture until the funded-plan owner exposes its
            # exception-disabled source mapper. Not a buyer Saga claim.
            with patch.dict(self.parties, cost_exception=lambda *_: None):
                normal = self.fixture.h['basis'](self.db, refund, self.fixture.bindings)
        self.assertEqual(normal['status'], 'ready', normal)
        if not self.raw_installed or multi:
            self.ns['settlement_funded_refund']['raw_funded_basis'] = lambda db, r: copy.deepcopy(normal)
        bearer = self.parties['Participant'](id='bearer-'+key, kind=kind, name='Explicit loss bearer',
            enterprise_id='provider' if kind == 'provider' else '', owner_enterprise_id='provider',
            user_id='', currency='CNY', creator_id=self.f.user.id)
        self.db.add(bearer)
        self.db.flush()
        body = dict(request_id='proposal-'+key, refund_id=key, basis_kind='funded', schema_version=2,
            baseline_id='', retained_cost=retained, reason='Non-returnable work', bearer_participant_id=bearer.id,
            source_allocations=[dict(segment_id=r['segment_id'], baseline_id=r['reference_settlement_id'],
                retained_cost=retained if i == 0 else '0.00', evidence_refs=['invoice:'+key+':'+str(i)])
                for i, r in enumerate(normal['references'])])
        proposal = self.parties['create_exception'](self.db, self.f.user, body)
        self.assertEqual(proposal['status'], 'pending')
        with self.assertRaises(HTTPException) as denied:
            self.parties['decide'](self.db, self.f.reviewer, proposal['id'], dict(
                request_id='same-side-'+key, expected_version=1, decision='approve', reason='Same side'))
        self.assertEqual(denied.exception.status_code, 403)
        reviewer = SimpleNamespace(id='provider-review', platform_role='', is_active=True, verified_status='verified')
        membership = SimpleNamespace(status='active', role='enterprise_admin', business_roles='provider')
        with patch('app.console_business_scope.resolve_business_scope', return_value=dict(
                mode='provider', membership=membership, enterprise_id='provider')):
            approved = self.parties['decide'](self.db, reviewer, proposal['id'], dict(
                request_id='approve-'+key, expected_version=1, decision='approve', reason='Checked all evidence'))
        self.assertEqual(approved['status'], 'approved')
        proof = self.parties['cost_exception_funded_basis'](self.db, refund, normal['references'])
        self.assertEqual(proof['status'], 'ready', proof)
        return refund, normal, proof

    def test_partial_actual_approval_cost_profit_loss_and_cash_unchanged(self):
        refund, normal, proof = self.prepare()
        original = copy.deepcopy(normal)
        result = self.cost['apply'](self.db, refund, normal, proof)
        self.assertEqual(normal, original)
        self.assertEqual(Decimal(result['gross_amount']), Decimal('-32'))
        self.assertEqual(Decimal(result['cost_amount']), Decimal('-15'))
        self.assertEqual(Decimal(result['profit_amount']), Decimal('-17'))
        self.assertEqual(Decimal(result['shares']['provider_share']), Decimal('-15'))
        self.assertEqual(result['buyer_cash_amount'], normal['buyer_cash_amount'])
        self.assertEqual(result['absorption_reversal'], normal['absorption_reversal'])
        ref = result['references'][0]
        self.assertEqual(sum(Decimal(e['amount']) for e in ref['entries']), Decimal('32'))
        self.assertEqual(sum(Decimal(e['amount']) for e in ref['entries'] if e['kind'] == 'cost'), Decimal('15'))
        self.assertEqual(sum(Decimal(e['amount']) for e in ref['entries'] if e['kind'] == 'profit'), Decimal('12'))
        self.assertEqual(sum(Decimal(e['amount']) for e in ref['entries'] if e['kind'] == 'loss'), Decimal('5'))
        self.assertEqual(refund.amount, 30)
        self.assertEqual(self.cost['apply'](self.db, refund, normal, proof), result)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.cost['Application'])), 1)

    def test_full_refund_extra_loss_may_exceed_original_share_cap(self):
        refund, normal, proof = self.prepare(amount='150', cumulative='150', retained='5')
        result = self.cost['apply'](self.db, refund, normal, proof)
        self.assertEqual(Decimal(result['gross_amount']), -160)
        self.assertEqual(Decimal(result['cost_amount']), -95)
        self.assertEqual(Decimal(result['profit_amount']), -65)
        self.assertEqual(Decimal(result['shares']['provider_share']), -55)
        self.assertEqual(Decimal(result['absorption_reversal']), 10)
        self.assertEqual(sum(Decimal(e['amount']) for e in result['references'][0]['entries']), 160)

    def test_multi_source_zero_retained_row_keeps_evidence_and_all_credit_legs(self):
        refund, normal, proof = self.prepare(amount='45', retained='5', multi=True)
        result = self.cost['apply'](self.db, refund, normal, proof)
        self.assertEqual(len(result['references']), 2)
        self.assertEqual(Decimal(result['gross_amount']), -47)
        self.assertEqual(Decimal(result['cost_amount']), -25)
        self.assertEqual(Decimal(result['profit_amount']), -22)
        self.assertEqual(Decimal(result['buyer_cash_amount']), 45)
        self.assertEqual(Decimal(result['absorption_reversal']), 2)
        for actual, original in zip(result['references'], normal['references']):
            self.assertEqual(actual['funding'], original['funding'])
            self.assertEqual(actual['buyer_cash_amount'], original['buyer_cash_amount'])
            self.assertEqual(actual['absorption_reversal'], original['absorption_reversal'])
            self.assertEqual(sum(Decimal(e['amount']) for e in actual['entries']), -Decimal(actual['gross_amount']))
        zero = result['references'][1]
        self.assertIsNone(zero['cost_exception']['loss'])
        self.assertTrue(zero['cost_exception']['evidence_refs'])
        self.assertEqual(zero['entries'], [e for e in normal['references'][1]['entries'] if Decimal(e['amount'])])

    def test_transport_status_does_not_change_frozen_application(self):
        refund, normal, proof = self.prepare()
        result = self.cost['apply'](self.db, refund, normal, proof)
        raw = {k: v for k, v in normal.items() if k != 'status'}
        self.assertEqual(self.cost['apply'](self.db, refund, raw, proof), result)
        self.assertIsNotNone(self.cost['validate_capture'](self.db, refund.id, result))

    def test_raw_funded_basis_does_not_call_exception_application_or_write(self):
        refund, normal, proof = self.prepare()
        with patch.dict(self.parties, cost_exception_funded_basis=lambda *_: self.fail('recursive application')):
            actual = self.cost['raw_funded_basis'](self.db, refund)
        self.assertTrue(all(actual[k] == normal[k] for k in actual))
        self.assertIsNone(self.db.get(self.cost['Application'], refund.id))
        self.assertFalse(self.db.new)

    def test_raw_ordinary_source_ambiguity_never_selects_first(self):
        refund = self.fixture.refund()
        self.fixture.source_fixture()
        result = self.cost['raw_funded_basis'](self.db, refund)
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['reason'], 'raw_funded_refund_unique_ordinary_source_required')

    def test_frozen_application_cannot_be_edited_or_replaced(self):
        refund, normal, proof = self.prepare()
        result = self.cost['apply'](self.db, refund, normal, proof)
        self.db.commit()
        application = self.db.get(self.cost['Application'], refund.id)
        application.actor = 'changed'
        with self.assertRaises(ValueError):
            self.db.flush()
        self.db.rollback()
        changed = copy.deepcopy(result)
        changed['references'][0]['funding']['amount'] = '999'
        with self.assertRaises(HTTPException):
            self.cost['validate_capture'](self.db, refund.id, changed)

    def test_buyer_cash_cap_stays_at_paid_amount_not_adjusted_revenue(self):
        refund = self.fixture.refund('150.01', '150.01')
        result = self.cost['raw_funded_basis'](self.db, refund)
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)

    def test_platform_bearer_internal_loss_and_external_returns_preserved(self):
        refund, normal, proof = self.prepare(kind='platform')
        # These separate credit fields are an explicit pure-transform boundary,
        # not evidence of an actual external funding receipt or payment.
        normal['funding_returns'] = [{'source_key': 'external-proof', 'amount': '2.00'}]
        normal['external_funding_return'] = '2.00'
        normal['references'][0]['external_funding_return'] = '2.00'
        proof['source_allocations'][0]['basis'] = copy.deepcopy(normal['references'][0])
        proof['source_allocations'][0]['basis_hash'] = digest(normal['references'][0])
        proof['approval_evidence_hash'] = digest({k: v for k, v in proof.items() if k != 'approval_evidence_hash'})
        with patch.dict(self.parties, cost_exception_funded_basis=lambda *_: proof):
            result = self.cost['apply'](self.db, refund, normal, proof)
        self.assertEqual(result['funding_returns'], normal['funding_returns'])
        self.assertEqual(result['external_funding_return'], '2.00')
        self.assertEqual(result['references'][0]['external_funding_return'], '2.00')
        self.assertEqual(result['references'][0]['cost_exception']['loss']['direction'], 'internal')

    def test_audit_failure_rolls_back_application_and_no_fictitious_posting(self):
        refund, normal, proof = self.prepare()
        with patch.dict(self.ns, audit=lambda *_args, **_kw: (_ for _ in ()).throw(RuntimeError('audit failed'))):
            with self.assertRaisesRegex(RuntimeError, 'audit failed'):
                self.cost['apply'](self.db, refund, normal, proof)
        self.assertIsNone(self.db.get(self.cost['Application'], refund.id))
        result = self.cost['apply'](self.db, refund, normal, proof)
        application = self.cost['validate_capture'](self.db, refund.id, result)
        with self.assertRaises(HTTPException):
            self.cost['post'](self.db, application, 'not-captured')
        self.assertIsNone(self.db.get(self.cost['Posting'], refund.id))

    def test_real_funded_capture_after_approved_exception(self):
        if not self.raw_installed:
            self.skipTest('funded owner raw_funded_basis integration pending')
        refund, normal, proof = self.prepare()
        self.cost['apply'](self.db, refund, normal, proof)
        final = self.fixture.h['freeze'](self.db, refund, self.fixture.bindings)
        self.assertEqual(final['status'], 'ready', final)
        negative = self.f.settlement(identifier='negative-combined', factor=-1, reference='s')
        negative.refund_id = refund.id
        for field in ('gross_amount', 'cost_amount', 'profit_amount'):
            setattr(negative, field, Decimal(final[field]))
        for field, value in final['shares'].items():
            setattr(negative, field, Decimal(value))
        self.db.flush()
        result = self.f.h['capture'](self.db, negative)
        self.assertFalse(isinstance(result, dict), result)
        posting = self.db.get(self.cost['Posting'], refund.id)
        self.assertIsNotNone(posting)
        self.assertEqual(posting.settlement_id, negative.id)
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)
        self.assertEqual(refund.amount, 30)


class ExternalFundedCostCaptureTests(unittest.TestCase):
    external_funding = True
    setUp = FundedCostApplicationTests.setUp
    prepare = FundedCostApplicationTests.prepare

    def test_reviewed_external_funding_cost_capture_preserves_return_and_rejects_wrong_direction(self):
        refund, normal, proof = self.prepare()
        self.assertEqual(normal['external_funding_return'], '2.00')
        final = self.fixture.h['freeze'](self.db, refund, self.fixture.bindings)
        negative = self.f.settlement(identifier='external-combined', factor=-1, reference='s')
        negative.refund_id = refund.id
        for field in ('gross_amount', 'cost_amount', 'profit_amount'):
            setattr(negative, field, Decimal(final[field]))
        for field, value in final['shares'].items():
            setattr(negative, field, Decimal(value))
        self.db.flush()
        captured = self.f.h['capture'](self.db, negative)
        self.assertFalse(isinstance(captured, dict), captured)
        posting = self.db.get(self.cost['Posting'], refund.id)
        self.assertEqual(posting.settlement_id, negative.id)
        Position = self.f.h['Position']
        positions = self.db.scalars(select(Position).where(Position.settlement_id == negative.id)).all()
        returns = [p for p in positions if p.kind == 'funding_return']
        self.assertEqual(sum(p.amount for p in returns), Decimal('2.00'))
        self.assertTrue(all(p.direction == 'payable' for p in returns))
        self.assertEqual(sum(p.amount for p in positions if p.kind != 'funding_return'), Decimal('32.00'))
        self.assertEqual(sum(p.amount for p in positions if p.kind == 'loss'), Decimal('5.00'))
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)
        self.assertEqual(refund.amount, 30)
        application = self.db.get(self.cost['Application'], refund.id)
        self.cost['post'](self.db, application, negative.id)
        self.db.execute(update(Position).where(Position.id == returns[0].id).values(direction='receivable'))
        self.db.expire_all()
        with self.assertRaises(HTTPException) as error:
            self.cost['post'](self.db, application, negative.id)
        self.assertEqual(error.exception.detail, 'funded_cost_captured_positions_mismatch')


if __name__ == '__main__':
    unittest.main()
