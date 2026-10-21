"""Opt-in real PostgreSQL manual-receipt races in the runner's disposable DB.

Runner allowlist entry: test_settlement_manual_funding_pg
Never reads DATABASE_URL. Requires CLEARING_TEST_DATABASE_URL naming exactly
market_clr2_test_<16 hex>; base harness creates/drops only its random schema.
load_tests selects this subclass's focused tests, excluding inherited finance tests.
"""
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import re
from types import SimpleNamespace
import unittest

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.engine import make_url

import test_settlement_finance_pg as pg
from app.settlement_manual_funding import install


@unittest.skipUnless(os.environ.get('CLEARING_TEST_DATABASE_URL'),
                     'requires disposable runner CLEARING_TEST_DATABASE_URL; no database accessed')
class ManualFundingPostgresTests(pg.FinancePostgresTests):
    def setUp(self):
        url = make_url(os.environ['CLEARING_TEST_DATABASE_URL'])
        if not re.fullmatch(r'market_clr2_test_[a-f0-9]{16}', url.database or ''):
            raise ValueError('Manual funding PG tests require the disposable runner database name')
        super().setUp()
        self.manual = install(self.ns, self.h)
        self.ns['Base'].metadata.create_all(self.engine)
        settlement = self.ns['Settlement'](id='manual-s', order_id='o', settlement_no='SET-MANUAL-PG',
            status='locked', gross_amount=160, cost_amount=100, profit_amount=60, platform_fee=10,
            provider_share=50, service_share=0, expert_fee=0, channel_fee=0)
        self.db.add(settlement)
        self.db.add(self.ns['SettlementLine'](id='manual-line', batch_id='fixture',
                                             settlement_id=settlement.id, status='locked'))
        self.db.flush()
        self.h['capture'](self.db, settlement, 'isolated-manual-funding-pg')
        self.proposal = self.propose('manual-first')
        self.db.commit()
        self.buyer_before = self.buyer_facts()
        self.db.commit()

    def propose(self, key):
        proposal = self.h['propose_funding'](self.db, self.maker, dict(request_id=key,
            settlement_id='manual-s', kind='external_receipt', amount='10.00',
            bearer_enterprise_id='provider', reason='Disposable PostgreSQL manual evidence', evidence_refs=['test:gap']))
        self.h['decide_funding'](self.db, self.reviewer, proposal['id'], dict(request_id=key + '-approve',
            expected_version=1, decision='approved', note='Independent funding proposal approval'))
        return proposal

    def body(self, key, proposal=None, reference='PG-SUPPLEMENT-001'):
        return dict(request_id=key, funding_request_id=(proposal or self.proposal)['id'],
            amount='10.00', currency='CNY', reference=reference,
            paid_on=datetime.now(timezone.utc).date().isoformat(), evidence_refs=['test:manual-receipt'],
            note='Manual supplemental evidence; no automated bank reconciliation')

    def buyer_facts(self):
        result = {}
        for label, model in (('orders', self.ns['Order']), ('payments', self.ns['Payment']), ('baselines', self.h['Baseline'])):
            rows = self.db.scalars(select(model)).all()
            result[label] = sorted(tuple((column.name, str(getattr(row, column.name)))
                                         for column in model.__table__.columns) for row in rows)
        return result

    def assert_buyer_unchanged(self):
        self.db.expire_all()
        self.assertEqual(self.buyer_facts(), self.buyer_before)
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, Decimal('150.00'))
        result = self.h['refund_basis'](self.db, SimpleNamespace(id='manual-refund', order_id='o', amount=160),
                                       dict(original_amount='150.00', cumulative_refund_amount='160.00'))
        self.assertNotEqual(result['status'], 'ready')

    def assert_one_winner(self, outcomes, code=409):
        self.assertEqual(sorted(result[0] for result in outcomes), ['http', 'ok'], outcomes)
        self.assertEqual(next(result for result in outcomes if result[0] == 'http')[1], code, outcomes)
        return next(result[1] for result in outcomes if result[0] == 'ok')

    def review_operation(self, row, key, user=None, decision='verified'):
        def operation(db):
            return self.manual['review'](db, user or self.reviewer, row['id'], dict(request_id=key,
                expected_version=1, decision=decision, note='Independent PostgreSQL evidence review'))
        return operation

    def cover_operation(self, proposal, row, key):
        def operation(db):
            return self.h['verify_funding'](db, self.reviewer, proposal['id'], dict(request_id=key,
                expected_version=2, receipt_reference=row['receipt_reference']))
        return operation

    def test_manual_duplicate_registration_race_creates_one_evidence_and_command(self):
        first, second = self.body('duplicate-a'), self.body('duplicate-b')
        outcomes = self.race([lambda db: self.manual['register'](db, self.maker, first),
                              lambda db: self.manual['register'](db, self.maker, second)])
        self.assert_one_winner(outcomes)
        self.assertEqual(self.count(self.manual['Evidence']), 1)
        self.assertEqual(self.count(self.manual['Review']), 0)
        self.assertEqual(self.count(self.h['FundingProof']), 0)
        self.assertEqual(self.count(self.h['FundingCommand'], self.h['FundingCommand'].request_id.in_(
            ['duplicate-a', 'duplicate-b'])), 1)
        self.assertEqual(self.count(self.Audit, self.Audit.action == 'finance_manual_funding_register'), 1)
        self.assert_buyer_unchanged()

    def test_manual_same_registration_key_race_replays_exactly_one_fact(self):
        body = self.body('identical-registration')
        outcomes = self.race([lambda db: self.manual['register'](db, self.maker, body),
                              lambda db: self.manual['register'](db, self.maker, body)])
        self.assertTrue(all(result[0] == 'ok' for result in outcomes), outcomes)
        self.assertEqual(outcomes[0][1], outcomes[1][1])
        self.assertEqual(self.count(self.manual['Evidence']), 1)
        self.assertEqual(self.count(self.h['FundingCommand'],
                                    self.h['FundingCommand'].request_id == 'identical-registration'), 1)
        self.assertEqual(self.count(self.Audit, self.Audit.action == 'finance_manual_funding_register'), 1)
        self.assert_buyer_unchanged()

    def test_manual_normalized_receipt_race_across_requests_cannot_reuse_source(self):
        other = self.propose('manual-other')
        self.db.commit()
        first = self.body('reference-first')
        second = self.body('reference-second', other, 'pg-supplement-001')
        outcomes = self.race([lambda db: self.manual['register'](db, self.maker, first),
                              lambda db: self.manual['register'](db, self.maker, second)])
        self.assert_one_winner(outcomes)
        self.assertEqual(self.count(self.manual['Evidence']), 1)
        self.assertEqual(self.count(self.h['FundingProof']), 0)
        self.assertEqual(self.count(self.Audit, self.Audit.action == 'finance_manual_funding_register'), 1)
        self.assert_buyer_unchanged()

    def test_manual_independent_review_race_has_one_immutable_decision(self):
        row = self.manual['register'](self.db, self.maker, self.body('review-race-register'))
        self.db.commit()
        other = SimpleNamespace(id='reviewer-two', platform_role='finance_settlement', is_active=True)
        outcomes = self.race([self.review_operation(row, 'review-verified'),
                              self.review_operation(row, 'review-rejected', other, 'rejected')])
        winner = self.assert_one_winner(outcomes)
        self.assertEqual(self.count(self.manual['Review']), 1)
        checked = self.db.get(self.manual['Review'], row['id'])
        self.assertEqual(checked.decision, winner['status'])
        self.assertNotEqual(checked.reviewer_id, self.maker.id)
        self.assertEqual(self.count(self.h['FundingCommand'], self.h['FundingCommand'].request_id.in_(
            ['review-verified', 'review-rejected'])), 1)
        self.assertEqual(self.count(self.Audit,
            self.Audit.action == 'finance_manual_funding_review:' + row['id']), 1)
        self.assertEqual(self.count(self.h['FundingProof']), 0)
        self.assert_buyer_unchanged()

    def test_manual_self_review_race_fails_even_if_it_acquires_lock_first(self):
        row = self.manual['register'](self.db, self.maker, self.body('self-review-register'))
        self.db.commit()
        outcomes = self.race([self.review_operation(row, 'forbidden-self-review', self.maker),
                              self.review_operation(row, 'independent-review')])
        self.assert_one_winner(outcomes, code=403)
        checked = self.db.get(self.manual['Review'], row['id'])
        self.assertEqual(checked.reviewer_id, self.reviewer.id)
        self.assertEqual(checked.decision, 'verified')
        self.assertEqual(self.count(self.manual['Review']), 1)
        self.assertEqual(self.count(self.h['FundingCommand'],
                                    self.h['FundingCommand'].request_id == 'forbidden-self-review'), 0)
        self.assert_buyer_unchanged()

    def test_manual_competing_coverages_cannot_exceed_one_exact_gap(self):
        other = self.propose('competing-cover-proposal')
        first = self.manual['register'](self.db, self.maker, self.body('cover-first'))
        second = self.manual['register'](self.db, self.maker, self.body('cover-second', other, 'PG-SUPPLEMENT-002'))
        self.review_operation(first, 'first-proof-review')(self.db)
        self.review_operation(second, 'second-proof-review')(self.db)
        self.db.commit()
        outcomes = self.race([self.cover_operation(self.proposal, first, 'cover-a'),
                              self.cover_operation(other, second, 'cover-b')])
        self.assert_one_winner(outcomes)
        self.assertEqual(self.count(self.h['FundingProof']), 1)
        self.assertEqual(self.count(self.manual['Evidence']), 2)
        self.assertEqual(self.count(self.manual['Review']), 2)
        self.assertEqual(self.count(self.h['FundingCommand'],
                                    self.h['FundingCommand'].request_id.in_(['cover-a', 'cover-b'])), 1)
        proof = self.db.get(self.h['FundingProof'], 'manual-s')
        evidence = json.loads(proof.evidence_json)
        self.assertEqual(Decimal(evidence['amount']), Decimal('10.00'))
        self.assertEqual(evidence['receipt']['method'], 'manual_independent_review')
        self.assertFalse(evidence['bank_receipt_verified'])
        self.assertFalse(evidence['receipt']['bank_receipt_verified'])
        self.assert_buyer_unchanged()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ManualFundingPostgresTests(name) for name in
        loader.getTestCaseNames(ManualFundingPostgresTests) if name.startswith('test_manual_'))


if __name__ == '__main__':
    unittest.main()
