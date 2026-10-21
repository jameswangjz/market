"""Real finance/manual hooks in disposable SQLite; no app.main or deployed DB."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

import test_settlement_finance as fixture
from app.settlement_manual_funding import install


class ManualFundingTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FinanceTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.db, self.core, self.ns = self.f.db, self.f.h, self.f.ns
        self.manual = install(self.ns, self.core)
        self.ns['Base'].metadata.create_all(self.f.engine)
        settlement = self.f.settlement()
        settlement.gross_amount, settlement.profit_amount, settlement.provider_share = 160, 60, 50
        self.db.flush()
        self.baseline = self.core['capture'](self.db, settlement)
        self.db.commit()
        self.frozen = self.baseline.snapshot_json
        self.proposal = self.core['propose_funding'](self.db, self.f.user, dict(request_id='funding-propose',
            settlement_id='s', kind='external_receipt', amount='10.00', bearer_enterprise_id='provider',
            reason='Documented supplement', evidence_refs=['source:1']))
        self.core['decide_funding'](self.db, self.f.reviewer, self.proposal['id'],
            dict(request_id='funding-decision', expected_version=1, decision='approved', note='Reviewed gap'))
        self.db.commit()
        self.body = dict(request_id='register-1', funding_request_id=self.proposal['id'], amount='10.00',
            currency='CNY', reference='SUPPLEMENT-001', paid_on=datetime.now(timezone.utc).date().isoformat(),
            evidence_refs=['document:receipt-1'], note='Manual receipt evidence, not bank reconciliation')

    def register(self, user=None, **changes):
        return self.manual['register'](self.db, user or self.f.user, dict(self.body, **changes))

    def review(self, row, user=None, **changes):
        return self.manual['review'](self.db, user or self.f.reviewer, row['id'],
            dict(dict(request_id='review-1', expected_version=1, decision='verified', note='Independently checked receipt'), **changes))

    def cover(self, row):
        return self.core['verify_funding'](self.db, self.f.reviewer, self.proposal['id'],
            dict(request_id='cover-1', expected_version=2, receipt_reference=row['receipt_reference']))

    def test_reviewed_proof_unlocks_existing_path_without_changing_buyer_funds(self):
        row = self.register()
        with self.assertRaises(HTTPException) as error:
            self.cover(row)
        self.assertEqual(error.exception.detail, 'reviewed_manual_funding_evidence_required')
        reviewed = self.review(row)
        self.assertEqual(reviewed['status'], 'verified')
        result = self.cover(row)
        self.assertEqual(result['status'], 'covered')
        self.assertFalse(result['bank_receipt_verified'])
        proof = self.db.get(self.core['FundingProof'], 's')
        receipt = json.loads(proof.evidence_json)['receipt']
        self.assertEqual(receipt['method'], 'manual_independent_review')
        self.assertEqual(receipt['registrar_id'], self.f.user.id)
        self.assertEqual(receipt['reviewer_id'], self.f.reviewer.id)
        self.assertFalse(receipt['bank_receipt_verified'])
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)
        self.assertEqual(self.db.get(self.core['Baseline'], 's').snapshot_json, self.frozen)
        basis = self.core['refund_basis'](self.db, SimpleNamespace(id='refund', order_id='o', amount=160),
                                        dict(original_amount=150, cumulative_refund_amount=160))
        self.assertNotEqual(basis['status'], 'ready')

    def test_submitter_cannot_review_even_as_super_admin(self):
        row = self.register()
        user = SimpleNamespace(id=self.f.user.id, platform_role='super_admin', is_active=True)
        with self.assertRaises(HTTPException) as error:
            self.review(row, user=user)
        self.assertEqual(error.exception.status_code, 403)
        self.assertIsNone(self.db.get(self.manual['Review'], row['id']))
        self.assertIsNone(self.db.get(self.core['FundingProof'], 's'))

    def test_role_activation_and_no_boolean_bypass(self):
        for change in (dict(platform_role='security_compliance'), dict(platform_role=''),
                       dict(is_active=False), dict(activation_status='pending')):
            user = SimpleNamespace(**(vars(self.f.user) | change))
            with self.subTest(change=change), self.assertRaises(HTTPException) as error:
                self.register(user=user)
            self.assertEqual(error.exception.status_code, 403)
        with self.assertRaises(HTTPException) as error:
            self.register(bank_receipt_verified=True)
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.manual['Evidence'])), 0)

    def test_amount_currency_date_and_evidence_validation(self):
        for change in (dict(amount='10.01'), dict(amount='9.99'), dict(amount='0'), dict(amount='NaN'),
                       dict(currency='USD'), dict(paid_on='2999-01-01'), dict(paid_on='2026-02-30'),
                       dict(paid_on='20261001'), dict(evidence_refs=[]), dict(reference=' '), dict(note='')):
            with self.subTest(change=change), self.assertRaises(HTTPException):
                self.register(**change)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.manual['Evidence'])), 0)

    def test_register_and_review_idempotency_payload_conflicts(self):
        row = self.register()
        self.assertEqual(row, self.register())
        with self.assertRaises(HTTPException) as error:
            self.register(reference='different')
        self.assertEqual(error.exception.detail, 'idempotency_payload_mismatch')
        reviewed = self.review(row)
        self.assertEqual(reviewed, self.review(row))
        with self.assertRaises(HTTPException):
            self.review(row, note='changed')
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.manual['Evidence'])), 1)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.manual['Review'])), 1)

    def test_one_evidence_per_request_and_normalized_receipt_reference_unique(self):
        self.register()
        with self.assertRaises(HTTPException) as error:
            self.register(request_id='second', reference='OTHER')
        self.assertEqual(error.exception.detail, 'manual_funding_conflict_refresh_required')
        other = self.core['propose_funding'](self.db, self.f.user, dict(request_id='second-proposal',
            settlement_id='s', kind='external_receipt', amount='10.00', bearer_enterprise_id='provider',
            reason='Another proposal', evidence_refs=['source:2']))
        with self.assertRaises(HTTPException) as error:
            self.register(request_id='third', funding_request_id=other['id'], reference='supplement-001')
        self.assertEqual(error.exception.detail, 'manual_funding_conflict_refresh_required')
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.manual['Evidence'])), 1)

    def test_rejected_or_unreviewed_proof_never_covers(self):
        row = self.register()
        self.review(row, decision='rejected')
        with self.assertRaises(HTTPException) as error:
            self.cover(row)
        self.assertEqual(error.exception.detail, 'reviewed_manual_funding_evidence_required')
        self.assertIsNone(self.db.get(self.core['FundingProof'], 's'))

    def test_review_and_final_verify_recheck_locked_basis_and_payload_identity(self):
        row = self.register()
        settlement = self.db.get(self.ns['Settlement'], 's')
        settlement.status = 'pending'
        self.db.flush()
        with self.assertRaises(HTTPException):
            self.review(row)
        settlement.status = 'locked'
        self.db.flush()
        self.review(row)
        payload = json.loads(self.db.get(self.core['FundingRequest'], self.proposal['id']).payload_json)
        with self.assertRaises(HTTPException):
            self.manual['verify'](self.db, dict(payload, amount='11.00'), row['receipt_reference'])

    def test_audit_failure_rolls_back_evidence_command_and_review(self):
        self.f.audit.side_effect = RuntimeError('audit unavailable')
        with self.assertRaises(RuntimeError):
            self.register()
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.manual['Evidence'])), 0)
        self.assertIsNone(self.db.get(self.core['FundingCommand'], (self.f.user.id, 'register-1')))
        self.f.audit.side_effect = None
        row = self.register()
        self.db.commit()
        self.f.audit.side_effect = RuntimeError('audit unavailable')
        with self.assertRaises(RuntimeError):
            self.review(row)
        self.assertIsNone(self.db.get(self.manual['Review'], row['id']))

    def test_evidence_and_reviews_are_immutable(self):
        row = self.register()
        self.review(row)
        self.db.commit()
        for model, field in ((self.manual['Evidence'], 'reference'), (self.manual['Review'], 'note')):
            value = self.db.get(model, row['id'])
            setattr(value, field, 'rewrite')
            with self.assertRaises(ValueError):
                self.db.flush()
            self.db.rollback()
            self.db.delete(self.db.get(model, row['id']))
            with self.assertRaises(ValueError):
                self.db.flush()
            self.db.rollback()

    def test_http_contract_scope_and_security_read_only(self):
        row = self.register()
        self.db.commit()
        client = TestClient(self.f.app)
        self.addCleanup(client.close)
        path = '/api/settlement-finance/manual-funding-evidence'
        self.assertEqual(client.get(path).json()['items'][0]['receipt_reference'], row['receipt_reference'])
        self.f.user = SimpleNamespace(id='security', platform_role='security_compliance', is_active=True)
        self.assertEqual(client.get(path).status_code, 200)
        self.assertEqual(client.post(path, json=self.body).status_code, 403)
        self.db.add(self.ns['Membership'](id='membership', user_id='provider-user', enterprise_id='provider',
            role='enterprise_admin', status='active', business_roles='provider'))
        self.db.commit()
        self.f.user = SimpleNamespace(id='provider-user', platform_role='', is_active=True, verified_status='verified')
        response = client.get(path, params=dict(enterprise_id='provider', business_view='provider'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['total'], 1)
        self.assertEqual(client.get(path).status_code, 403)
        self.assertEqual(client.get(path, params=dict(enterprise_id='foreign', business_view='provider')).status_code, 403)
        self.assertEqual(client.post(path, json=self.body).status_code, 403)

    def test_http_registration_independent_review_and_existing_verify(self):
        client = TestClient(self.f.app)
        self.addCleanup(client.close)
        path = '/api/settlement-finance/manual-funding-evidence'
        response = client.post(path, json=self.body)
        self.assertEqual(response.status_code, 200)
        row = response.json()
        body = dict(request_id='http-review', expected_version=1, decision='verified', note='Checked manually')
        self.assertEqual(client.post(path + '/' + row['id'] + '/review', json=body).status_code, 403)
        self.f.user = self.f.reviewer
        response = client.post(path + '/' + row['id'] + '/review', json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['method'], 'manual_independent_review')
        self.assertFalse(response.json()['bank_receipt_verified'])
        response = client.post('/api/settlement-finance/funding-adjustments/' + self.proposal['id'] + '/verify',
            json=dict(request_id='http-cover', expected_version=2, receipt_reference=row['receipt_reference']))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'covered')
        self.assertFalse(response.json()['bank_receipt_verified'])

    def test_separate_sessions_cannot_review_twice_and_share_finance_lock(self):
        lock = Mock(wraps=self.core['lock_finance'])
        with patch.dict(self.core, lock_finance=lock):
            row = self.register()
            lock.assert_called_once_with(self.db)
        self.db.commit()
        with Session(self.f.engine) as first:
            self.manual['review'](first, self.f.reviewer, row['id'], dict(request_id='session-one',
                expected_version=1, decision='verified', note='First independent reviewer'))
            first.commit()
        with Session(self.f.engine) as second:
            with self.assertRaises(HTTPException) as error:
                self.manual['review'](second, self.f.reviewer, row['id'], dict(request_id='session-two',
                    expected_version=1, decision='rejected', note='Stale competing review'))
            self.assertEqual(error.exception.detail, 'manual_funding_review_version_conflict')
            self.assertEqual(second.scalar(select(func.count()).select_from(self.manual['Review'])), 1)

    def test_forged_review_fields_and_cross_core_command_reuse_are_rejected(self):
        with self.assertRaises(HTTPException) as error:
            self.register(request_id='funding-propose')
        self.assertEqual(error.exception.detail, 'idempotency_payload_mismatch')
        row = self.register()
        with self.assertRaises(HTTPException):
            self.review(row, bank_receipt_verified=True)
        with self.assertRaises(HTTPException):
            self.review(row, expected_version=True)
        self.assertIsNone(self.db.get(self.manual['Review'], row['id']))

    def test_installer_is_idempotent_and_preserves_single_verifier_and_routes(self):
        verifier = self.ns['settlement_funding_proofs']['verify']
        routes = [(route.path, getattr(route, 'methods', None)) for route in self.f.app.routes]
        tables = set(self.ns['Base'].metadata.tables)
        self.assertIs(install(self.ns, self.core), self.manual)
        self.assertIs(self.ns['settlement_funding_proofs']['verify'], verifier)
        self.assertEqual(routes, [(route.path, getattr(route, 'methods', None)) for route in self.f.app.routes])
        self.assertEqual(tables, set(self.ns['Base'].metadata.tables))

    def test_missing_or_unreviewed_reference_cannot_create_funding_proof(self):
        for reference in ('manual:missing', 'a-user-asserted-receipt'):
            with self.subTest(reference=reference), self.assertRaises(HTTPException):
                self.core['verify_funding'](self.db, self.f.reviewer, self.proposal['id'],
                    dict(request_id='missing-' + reference, expected_version=2, receipt_reference=reference))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.core['FundingProof'])), 0)
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)

    def test_receipt_review_does_not_replace_required_funding_approval(self):
        proposed = self.core['propose_funding'](self.db, self.f.user, dict(request_id='unapproved-proposal',
            settlement_id='s', kind='external_receipt', amount='10.00', bearer_enterprise_id='provider',
            reason='Requires funding approval too', evidence_refs=['source:2']))
        row = self.register(funding_request_id=proposed['id'])
        self.review(row)
        with self.assertRaises(HTTPException) as error:
            self.core['verify_funding'](self.db, self.f.reviewer, proposed['id'], dict(
                request_id='unapproved-cover', expected_version=2, receipt_reference=row['receipt_reference']))
        self.assertEqual(error.exception.detail, 'approved_unfunded_request_required')
        self.assertIsNone(self.db.get(self.core['FundingProof'], 's'))

    def test_competing_approved_requests_cannot_cover_one_gap_twice(self):
        first = self.register()
        self.review(first)
        second_proposal = self.core['propose_funding'](self.db, self.f.user, dict(request_id='competing-proposal',
            settlement_id='s', kind='external_receipt', amount='10.00', bearer_enterprise_id='provider',
            reason='Competing gap proposal', evidence_refs=['source:2']))
        self.core['decide_funding'](self.db, self.f.reviewer, second_proposal['id'], dict(
            request_id='competing-approval', expected_version=1, decision='approved', note='Independent review'))
        second = self.register(request_id='competing-receipt', funding_request_id=second_proposal['id'], reference='SUPPLEMENT-002')
        self.review(second, request_id='competing-receipt-review')
        self.db.commit()
        with Session(self.f.engine) as winner:
            result = self.core['verify_funding'](winner, self.f.reviewer, self.proposal['id'], dict(
                request_id='winning-cover', expected_version=2, receipt_reference=first['receipt_reference']))
            self.assertEqual(result['status'], 'covered')
            winner.commit()
        with Session(self.f.engine) as loser:
            with self.assertRaises(HTTPException) as error:
                self.core['verify_funding'](loser, self.f.reviewer, second_proposal['id'], dict(
                    request_id='losing-cover', expected_version=2, receipt_reference=second['receipt_reference']))
            self.assertEqual(error.exception.detail, 'approved_unfunded_request_required')
            self.assertEqual(loser.scalar(select(func.count()).select_from(self.core['FundingProof'])), 1)
            funding = self.core['funding_status'](loser, 's')
            self.assertEqual(funding['amount'], '10.00')
            self.assertFalse(funding['bank_receipt_verified'])
            self.assertEqual(loser.get(self.ns['Payment'], 'payment').amount, 150)
            self.assertEqual(loser.get(self.core['Baseline'], 's').snapshot_json, self.frozen)

    def test_final_verification_replay_is_idempotent_and_fresh_command_cannot_reconsume(self):
        row = self.register()
        self.review(row)
        covered = self.cover(row)
        self.db.commit()
        self.assertEqual(covered, self.cover(row))
        with self.assertRaises(HTTPException):
            self.core['verify_funding'](self.db, self.f.reviewer, self.proposal['id'], dict(
                request_id='new-cover', expected_version=2, receipt_reference=row['receipt_reference']))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.core['FundingProof'])), 1)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.manual['Evidence'])), 1)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.manual['Review'])), 1)

    def test_final_verification_rejects_corrupt_review_hash_even_with_verified_status(self):
        row = self.register()
        self.review(row)
        self.db.commit()
        # Simulate out-of-band corruption in this disposable DB, bypassing ORM immutability.
        self.db.execute(update(self.manual['Review'].__table__).where(
            self.manual['Review'].evidence_id == row['id']).values(evidence_hash='0' * 64))
        self.db.commit()
        self.db.expire_all()
        with self.assertRaises(HTTPException) as error:
            self.cover(row)
        self.assertEqual(error.exception.detail, 'reviewed_manual_funding_evidence_required')
        self.assertIsNone(self.db.get(self.core['FundingProof'], 's'))

    def test_failed_coverage_audit_preserves_reviewed_evidence_without_creating_proof(self):
        row = self.register()
        self.review(row)
        self.db.commit()
        self.f.audit.side_effect = RuntimeError('coverage audit unavailable')
        with self.assertRaises(RuntimeError):
            self.cover(row)
        self.assertIsNone(self.db.get(self.core['FundingProof'], 's'))
        self.assertIsNone(self.db.get(self.core['FundingCommand'], (self.f.reviewer.id, 'cover-1')))
        self.assertIsNotNone(self.db.get(self.manual['Evidence'], row['id']))
        self.assertEqual(self.db.get(self.manual['Review'], row['id']).decision, 'verified')
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)
        self.assertEqual(self.db.get(self.core['Baseline'], 's').snapshot_json, self.frozen)
        self.f.audit.side_effect = None
        self.assertEqual(self.cover(row)['status'], 'covered')

    def test_rejected_funding_request_cannot_register_manual_receipt(self):
        proposal = self.core['propose_funding'](self.db, self.f.user, dict(request_id='reject-proposal',
            settlement_id='s', kind='external_receipt', amount='10.00', bearer_enterprise_id='provider',
            reason='Proposal to reject', evidence_refs=['source:rejected']))
        self.core['decide_funding'](self.db, self.f.reviewer, proposal['id'], dict(request_id='reject-decision',
            expected_version=1, decision='rejected', note='Evidence not acceptable'))
        with self.assertRaises(HTTPException) as error:
            self.register(funding_request_id=proposal['id'])
        self.assertEqual(error.exception.detail, 'manual_funding_request_unavailable')
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.manual['Evidence'])), 0)


if __name__ == '__main__':
    unittest.main()
