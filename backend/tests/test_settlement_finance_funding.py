"""Conservative funding evidence; approvals never manufacture cash."""
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select, func

import test_settlement_finance as fixture


class FundingTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FinanceTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.db, self.h, self.ns = self.f.db, self.f.h, self.f.ns
        settlement = self.f.settlement()
        settlement.gross_amount, settlement.profit_amount, settlement.provider_share = 160, 60, 50
        self.db.flush()
        self.baseline = self.h['capture'](self.db, settlement)
        self.db.commit()
        self.frozen = self.baseline.snapshot_json
        self.payables = [p for p in self.f.positions() if p.direction == 'payable']

    def proposal(self, kind='platform_absorption', key='proposal', **values):
        body = dict(request_id=key,
            settlement_id='s', kind=kind, amount='10.00', reason='Documented settlement difference',
            evidence_refs=['approved-cost-adjustment:1'], bearer_enterprise_id='provider')
        body.update(values)
        return self.h['propose_funding'](self.db, self.f.user, body)

    def approve(self, proposal):
        return self.h['decide_funding'](self.db, self.f.reviewer, proposal['id'],
            dict(request_id='decision-' + proposal['id'], decision='approved', note='Reviewed source and bearer', expected_version=1))

    def verify(self, proposal, key='verify', **values):
        return self.h['verify_funding'](self.db, self.f.reviewer, proposal['id'],
            dict(request_id=key, expected_version=2, receipt_reference='manual-receipt:1', **values))

    def assert_blocked(self):
        with self.assertRaises(HTTPException):
            self.f.payout(self.payables, key='blocked')

    def test_platform_absorption_requires_review_and_consumes_only_existing_own_share(self):
        proposed = self.proposal()
        self.assert_blocked()
        with self.assertRaises(HTTPException) as error:
            self.h['decide_funding'](self.db, self.f.user, proposed['id'],
                dict(request_id='self-review', decision='approved', note='self', expected_version=1))
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.approve(proposed)['status'], 'approved')
        self.assert_blocked()
        verified = self.verify(proposed)
        self.assertEqual(verified['status'], 'covered')
        self.assertFalse(verified['bank_receipt_verified'])
        self.assertEqual(self.verify(proposed), verified)
        absorption = self.db.get(self.h['FundingAbsorption'], proposed['id'])
        self.assertEqual(absorption.amount, 10)
        self.assertEqual(json.loads(absorption.evidence_json)['cash_amount'], '0.00')
        self.assertEqual(len(self.f.positions()), len(json.loads(self.frozen)['entries']))
        self.assertIsNone(self.db.scalar(select(self.h['Position']).where(self.h['Position'].kind == 'loss')))
        self.assertEqual(self.db.scalar(select(func.sum(self.h['Allocation'].amount))), 10)
        tx, _ = self.f.payout(self.payables, key='funded')
        self.f.confirm(tx)
        self.assertEqual(sum(self.h['balance'](self.db, p) for p in self.payables), 0)
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)
        self.assertEqual(self.db.get(self.h['Baseline'], 's').snapshot_json, self.frozen)

    def test_external_approval_with_installed_verifier_but_no_reviewed_proof_remains_blocked(self):
        proposed = self.proposal('external_receipt')
        approved = self.approve(proposed)
        self.assertTrue(approved['trusted_receipt_verifier_available'])
        self.assertTrue(approved['can_verify'])
        self.assertEqual(approved['verification_unavailable_reason'], '')
        self.assertEqual(approved['status'], 'approved')
        self.assertFalse(approved['bank_receipt_verified'])
        with self.assertRaises(HTTPException) as error:
            self.verify(proposed)
        self.assertEqual(error.exception.detail, 'reviewed_manual_funding_evidence_required')
        self.assertIsNone(self.db.get(self.h['FundingProof'], 's'))
        self.assertEqual(self.h['funding_status'](self.db, 's')['status'], 'uncovered')
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)
        self.assertEqual(self.db.get(self.h['Baseline'], 's').snapshot_json, self.frozen)
        self.assert_blocked()

    def test_external_approval_without_configured_verifier_remains_blocked(self):
        with patch.dict(self.ns, settlement_funding_proofs={}):
            proposed = self.proposal('external_receipt')
            self.assertFalse(self.approve(proposed)['can_verify'])
            self.assertEqual(proposed['verification_unavailable_reason'], 'trusted_funding_receipt_verifier_unavailable')
            with self.assertRaises(HTTPException) as error:
                self.verify(proposed)
            self.assertEqual(error.exception.detail, 'trusted_funding_receipt_verifier_unavailable')
            self.assertIsNone(self.db.get(self.h['FundingProof'], 's'))
            self.assertEqual(self.h['funding_status'](self.db, 's')['status'], 'uncovered')
            self.assert_blocked()

    def test_trusted_manual_receipt_is_separate_from_buyer_payment_and_refund_basis(self):
        proposed = self.proposal('external_receipt')
        self.approve(proposed)
        proof = dict(status='verified', source_key='manual:receipt-1', amount='10.00', currency='CNY',
            payer_type='enterprise', payer_id='provider', evidence_hash=hashlib.sha256(b'checked evidence').hexdigest(),
            mode='manual', bank_receipt_verified=False)
        self.ns['settlement_funding_proofs'] = dict(verify=lambda db, payload, reference: dict(proof))
        self.assertEqual(self.verify(proposed)['status'], 'covered')
        self.assertEqual(self.db.get(self.ns['Payment'], 'payment').amount, 150)
        refund = SimpleNamespace(id='r', order_id='o', amount=30)
        self.assertEqual(self.h['refund_basis'](self.db, refund,
            dict(original_amount='150', cumulative_refund_amount='30'))['status'], 'unsupported')
        tx, _ = self.f.payout(self.payables, key='external-funded')
        self.f.confirm(tx)
        self.assertEqual(sum(self.h['balance'](self.db, p) for p in self.payables), 0)

    def test_wrong_currency_payer_or_amount_does_not_unlock_cash(self):
        proposed = self.proposal('external_receipt')
        self.approve(proposed)
        base = dict(status='verified', source_key='cash:1', amount='10.00', currency='CNY',
                    payer_type='enterprise', payer_id='provider', evidence_hash='a'*64)
        for change in (dict(currency='USD'), dict(payer_id='someone-else'), dict(amount='9.99')):
            self.ns['settlement_funding_proofs'] = dict(verify=lambda db, payload, reference, c=change: dict(base, **c))
            with self.subTest(change=change), self.assertRaises(HTTPException):
                self.verify(proposed)
            self.assertIsNone(self.db.get(self.h['FundingProof'], 's'))
            self.assert_blocked()

    def test_audit_failure_rolls_back_internal_loss_proof_and_allocations(self):
        proposed = self.proposal()
        self.approve(proposed)
        self.db.commit()
        self.f.audit.side_effect = RuntimeError('audit failed')
        with self.assertRaises(RuntimeError):
            self.verify(proposed)
        self.assertIsNone(self.db.get(self.h['FundingProof'], 's'))
        self.assertIsNone(self.db.get(self.h['FundingAbsorption'], proposed['id']))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.h['Allocation'])), 0)
        self.assertEqual(len(self.f.positions()), 3)
        self.f.audit.side_effect = None
        self.assert_blocked()

    def test_permissions_idempotency_and_immutable_proof(self):
        proposed = self.proposal()
        self.assertEqual(proposed, self.proposal())
        with self.assertRaises(HTTPException):
            self.proposal(key='proposal', kind='external_receipt')
        outsider = SimpleNamespace(id='outsider', platform_role='security_compliance', is_active=True)
        with self.assertRaises(HTTPException) as error:
            self.h['propose_funding'](self.db, outsider, {})
        self.assertEqual(error.exception.status_code, 403)
        self.approve(proposed)
        self.verify(proposed)
        self.db.commit()
        proof = self.db.get(self.h['FundingProof'], 's')
        proof.evidence_hash = 'changed'
        with self.assertRaises(ValueError):
            self.db.flush()
        self.db.rollback()
        self.assertEqual(json.loads(self.db.get(self.h['Baseline'], 's').snapshot_json)['actual_receipts'], '150.00')

    def test_insufficient_internal_share_cannot_back_extra_external_payout(self):
        settlement = self.f.settlement(identifier='larger')
        settlement.gross_amount, settlement.profit_amount, settlement.provider_share = 170, 70, 60
        self.db.flush()
        self.h['capture'](self.db, settlement)
        self.db.commit()
        proposed = self.proposal(settlement_id='larger', amount='20.00')
        self.approve(proposed)
        with self.assertRaises(HTTPException) as error:
            self.verify(proposed)
        self.assertEqual(error.exception.detail, 'platform_absorption_exceeds_proven_available_funds')
        self.assertIsNone(self.db.get(self.h['FundingProof'], 'larger'))
        self.assertEqual(self.db.scalar(select(func.count()).select_from(self.h['Allocation'])), 0)

    def test_verification_rechecks_confirmed_batch(self):
        proposed = self.proposal()
        self.approve(proposed)
        self.db.get(self.ns['SettlementBatch'], 'fixture').status = 'pending_confirm'
        self.db.commit()
        with self.assertRaises(HTTPException) as error:
            self.verify(proposed)
        self.assertEqual(error.exception.detail, 'confirmed_payment_batch_required')
        self.assertIsNone(self.db.get(self.h['FundingProof'], 's'))

    def test_external_source_cannot_be_consumed_by_two_settlements(self):
        proof = dict(status='verified', source_key='manual:single-source', amount='10.00', currency='CNY',
            payer_type='enterprise', payer_id='provider', evidence_hash='b'*64, mode='manual')
        self.ns['settlement_funding_proofs'] = dict(verify=lambda *args: dict(proof))
        first = self.proposal('external_receipt')
        self.approve(first)
        self.verify(first)
        other = self.f.settlement(identifier='other')
        other.gross_amount, other.profit_amount, other.provider_share = 160, 60, 50
        self.db.flush()
        self.h['capture'](self.db, other)
        self.db.commit()
        second = self.proposal('external_receipt', key='second', settlement_id='other')
        self.approve(second)
        with self.assertRaises(HTTPException) as error:
            self.verify(second, key='verify-second')
        self.assertEqual(error.exception.detail, 'funding_source_already_used')
        self.assertIsNone(self.db.get(self.h['FundingProof'], 'other'))

    def test_absorption_preserves_exact_original_entry_identity_and_refund_source_hash(self):
        before = sorted((p.id, p.kind, p.participant_id, p.amount) for p in self.f.positions())
        proposed = self.proposal()
        self.approve(proposed)
        self.verify(proposed)
        self.db.commit()
        after = sorted((p.id, p.kind, p.participant_id, p.amount) for p in self.f.positions())
        self.assertEqual(before, after)
        self.assertEqual(self.db.get(self.h['Baseline'], 's').snapshot_json, self.frozen)
        absorption = self.db.get(self.h['FundingAbsorption'], proposed['id'])
        evidence = json.loads(absorption.evidence_json)
        self.assertEqual(evidence['baseline_hash'], self.baseline.snapshot_hash)
        self.assertEqual(sum(float(a['amount']) for a in evidence['allocations']), 10)
        self.assertEqual(self.db.scalar(select(func.sum(self.h['Allocation'].amount))), 10)


if __name__ == '__main__':
    unittest.main()
