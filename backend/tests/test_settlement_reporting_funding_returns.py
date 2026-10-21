"""Focused additive reporting contracts; isolated SQLite and private test Redis only.

Source/capture and external return integration tests reuse actual finance hooks;
an unproven return Position is rejected instead of inventing a sixth share.
"""
import csv
import io
import json
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from decimal import Decimal

import test_settlement_reporting as existing
import test_settlement_funded_combinations_http as combinations


class FundingReturnReportingTests(existing.SettlementReportingTests):
    def test_original_final_amounts_remain_five_shares_and_no_payer_credit_is_invented(self):
        oid, sid = self.capture_order()
        data = self.report()
        row = next(item for item in data['items'] if item['id'] == sid)
        self.assertEqual((row['gross_amount'], row['cost_amount'], row['profit_amount']), ('150.00', '100.00', '50.00'))
        self.assertEqual(set(row['share_rates']), {'platform', 'provider', 'service', 'expert', 'channel'})
        self.assertEqual(row['funding_returns'], [])
        self.assertEqual(row['external_funding_return'], '0.00')
        self.assertIsNone(row['buyer_cash_refund'])
        self.assertEqual(data['summary']['external_funding_return'], '0.00')
        exported = list(csv.DictReader(io.StringIO(self.download(kind='details').content.decode('utf-8-sig'))))
        self.assertEqual(json.loads(exported[0][self.reporting.HEADERS['funding_returns_json']]), [])

    def test_legacy_multi_manifest_has_frozen_source_economics_human_ids_and_buyer_refund(self):
        self.test_multisource_refund_uses_immutable_manifest_not_current_term()
        row = self.report(source_kind='refund')['items'][0]
        self.assertEqual(row['buyer_cash_refund'], '75.00')
        for ref in row['economic_segments']:
            self.assertTrue(ref['source_order_no'].startswith('ORD-'))
            self.assertTrue(ref['reference_settlement_no'].startswith('SET-'))
            self.assertTrue(all(value.startswith('BATCH-') for value in ref['reference_batch_nos']))
            self.assertEqual((ref['source_gross_amount'], ref['source_cost_amount'], ref['source_profit_amount']),
                             ('150.00', '100.00', '50.00'))
            self.assertEqual(set(ref['share_rates']), {'platform', 'provider', 'service', 'expert', 'channel'})
            self.assertEqual(ref['independent_losses'], [])
        self.assertEqual(row['funding_returns'], [])

    def test_return_position_without_verified_funded_posting_is_rejected(self):
        oid, sid = self.capture_order()
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.settlement_finance['Position'](settlement_id=sid, order_id=oid, kind='funding_return',
                share_kind='', participant_type='enterprise', participant_id='provider_tenant',
                participant_name='Original payer', currency='CNY', direction='payable', amount=1, blocked_reason=''))
            db.commit()
        result = self.request('GET', self.prefix + '/report', actor='finance', expected=409, params={'year': 2026})
        self.assertEqual(result['detail'], 'funding_return_reporting_evidence_unproven')

    def test_provider_filters_cannot_expose_other_source_funding_returns(self):
        self.capture_order()
        self.seed(provider='other_tenant', amount='999.99')
        data = self.report(actor='provider', enterprise_id='provider_tenant', business_view='provider')
        self.assertNotIn('other_tenant', json.dumps(data))
        self.assertNotIn('999.99', json.dumps(data))
        self.assertTrue(all(row['funding_returns'] == [] for row in data['items']))

    def test_explicit_payer_scope_never_includes_same_enterprise_ordinary_cost_or_profit(self):
        self.capture_order()
        data = self.report(actor='provider', enterprise_id='provider_tenant', business_view='funding_payer')
        self.assertEqual(data['total'], 0)
        self.assertEqual(data['summary']['cost_amount'], '0.00')
        self.assertEqual(data['summary']['profit_share'], '0.00')
        self.assertEqual(data['summary']['funding_return_amount'], '0.00')
        self.assertEqual(data['scope']['participant_type'], 'enterprise')
        self.assertTrue(data['scope']['funding_returns_only'])
        self.assertNotIn('gross_amount', json.dumps(data))
        self.request('GET', self.prefix + '/report', actor='buyer', expected=403,
                     params=dict(enterprise_id='provider_tenant', business_view='funding_payer'))


class FundedSourceReportingTests(existing.FundedRefundReportingIntegrationTests):
    def test_funded_source_metadata_keeps_internal_credit_outside_five_shares(self):
        self.exercise_completed_funded_refund('0.05')

    def check_partial_cent_projection(self, reporting, negative_id):
        super().check_partial_cent_projection(reporting, negative_id)
        with self.clock_patch(reporting, datetime(2028, 1, 2, tzinfo=timezone.utc)):
            report = self.request('GET', '/api/settlement-reporting/report', actor='finance', params={'year': 2026})
            row = next(item for item in report['items'] if item['id'] == negative_id)
            self.assertEqual(row['segment_basis'], 'immutable_funded_refund_plan')
            self.assertEqual((row['gross_amount'], row['buyer_cash_refund'], row['funding_absorption_reversal']),
                             ('-0.06', '0.05', '0.01'))
            self.assertEqual(row['funding_returns'], [])
            self.assertEqual(row['external_funding_return'], '0.00')
            ref = row['economic_segments'][0]
            self.assertEqual(ref['reference_settlement_no'], 'SET-FUNDED-REPORT-SOURCE')
            self.assertEqual(ref['reference_batch_nos'], ['BATCH-FUNDED-REPORT-SOURCE'])
            self.assertEqual((ref['source_gross_amount'], ref['source_cost_amount'], ref['source_profit_amount']),
                             ('110.00', '40.00', '70.00'))
            self.assertEqual(sum(Decimal(value) for value in ref['source_shares'].values()), Decimal('70.00'))
            hooks = self.m.settlement_funded_refund
            describe = hooks['describe']
            def zero_external_contract(db, settlement_id):
                return dict(describe(db, settlement_id), external_funding_return='0.00',
                            kind='supplementary_funding_reversal')
            with patch.dict(hooks, describe=zero_external_contract):
                compatible = self.request('GET', '/api/settlement-reporting/report', actor='finance', params={'year': 2026})
            item = next(r for r in compatible['items'] if r['id'] == negative_id)
            self.assertEqual(item['funding_absorption_reversal'], '0.01')
            self.assertEqual(item['external_funding_return'], '0.00')


class ExternalPayerReportingTests(combinations.FundedCombinationHTTPTests):
    def test_cumulative_external_retained_cost_report_never_double_counts_payer_credit(self):
        self.cumulative(external=True, cost=True)
        report = self.request('GET', '/api/settlement-reporting/report', actor='finance',
                              params=dict(start='1900-01-01', end='9998-12-31', source_kind='refund'))
        self.assertEqual(len(report['items']), 2)
        self.assertEqual(report['summary']['gross_amount'], '-110.00')
        self.assertEqual(report['summary']['buyer_cash_refund'], '100.00')
        self.assertEqual(report['summary']['external_funding_return'], '10.00')
        self.assertEqual(report['summary']['cost_amount'], '-35.00')
        self.assertEqual(sum(Decimal(p['profit_share']) for r in report['items'] for p in r['participants']), Decimal('-70.00'))
        self.assertEqual(sum(Decimal(p['loss_amount']) for r in report['items'] for p in r['participants']), Decimal('5.00'))
        self.assertEqual(sum(Decimal(p['remaining_amount']) for r in report['items'] for p in r['funding_returns']), Decimal('10.00'))
        self.assertTrue(all(r['funding_absorption_reversal'] == '0.00' for r in report['items']))

    def external_payer_source(self):
        m = self.m
        with m.SessionLocal() as db:
            db.add(m.Enterprise(id='third-party-payer', name='Supplemental payer only', credit_code='THIRD-PAYER', verification_status='verified'))
            db.add(m.User(id='third-party-admin', name='Payer administrator', email='payer@example.invalid', password_hash='unused',
                          verified_status='verified', activation_status='active', is_active=True, platform_role=''))
            db.flush()
            db.add(m.Membership(user_id='third-party-admin', enterprise_id='third-party-payer', role='enterprise_admin',
                                status='active', business_roles='buyer'))
            db.commit()
            with patch.object(m, 'now', return_value=datetime.now(timezone.utc)):
                self.tokens['third-party'] = m.issue_token(db.get(m.User, 'third-party-admin'))
        sid, _ = self.funded_source(kind='external_receipt', cover=False)
        proposal = self.request('POST', '/api/settlement-finance/funding-adjustments', actor='finance', json=dict(
            request_id='third-party-proposal', settlement_id=sid, kind='external_receipt', amount='10.00', currency='CNY',
            bearer_enterprise_id='third-party-payer', reason='Original supplemental payer', evidence_refs=['third-party:gap']))
        self.request('POST', f'/api/settlement-finance/funding-adjustments/{proposal["id"]}/decision', actor='ops', json=dict(
            request_id='third-party-approve', expected_version=1, decision='approved', note='Independent funding approval'))
        receipt = self.request('POST', '/api/settlement-finance/manual-funding-evidence', actor='finance', json=dict(
            request_id='third-party-receipt', funding_request_id=proposal['id'], amount='10.00', currency='CNY',
            reference='THIRD-PARTY-RECEIPT', paid_on=datetime.now(timezone.utc).date().isoformat(),
            evidence_refs=['third-party:receipt'], note='Manual evidence only'))
        self.request('POST', f'/api/settlement-finance/manual-funding-evidence/{receipt["id"]}/review', actor='ops', json=dict(
            request_id='third-party-receipt-review', expected_version=1, decision='verified', note='Independent manual review'))
        self.request('POST', f'/api/settlement-finance/funding-adjustments/{proposal["id"]}/verify', actor='ops', json=dict(
            request_id='third-party-cover', expected_version=2, receipt_reference=receipt['receipt_reference']))
        return sid

    def report_row(self, nid):
        report = self.request('GET', '/api/settlement-reporting/report', actor='finance',
                              params=dict(start='1900-01-01', end='9998-12-31', source_kind='refund'))
        return report, next(row for row in report['items'] if row['id'] == nid)

    def confirm_return_batch(self, nid):
        m = self.m
        with m.SessionLocal() as db:
            batch = m.SettlementBatch(batch_no='REPORT-RETURN-BATCH', status='pending_confirm', idempotency_key='report-return-batch')
            db.add(batch)
            db.flush()
            bid = batch.id
            db.add(m.SettlementLine(batch_id=bid, settlement_id=nid, participant_type='provider', amount=-14))
            db.commit()
        self.request('POST', f'/api/settlement-batches/{bid}/confirm', actor='finance', json={})

    def test_reviewed_external_source_does_not_manufacture_return_or_buyer_cash(self):
        source = self.external_payer_source()
        report = self.request('GET', '/api/settlement-reporting/report', actor='finance',
                              params=dict(start='1900-01-01', end='9998-12-31'))
        row = next(row for row in report['items'] if row['id'] == source)
        self.assertEqual((row['gross_amount'], row['cost_amount'], row['profit_amount']), ('110.00', '40.00', '70.00'))
        self.assertEqual(row['funding_returns'], [])
        self.assertIsNone(row['buyer_cash_refund'])
        self.assertEqual(row['supplementary_funding']['method'], 'manual_independent_review')
        self.assertFalse(row['supplementary_funding']['bank_receipt_verified'])
        self.assertEqual(row['funds']['paid_amount'], '0.00')
        self.assertEqual(row['funds']['recovered_amount'], '0.00')

    def test_external_third_party_return_is_separate_unpaid_and_owner_scoped(self):
        source = self.external_payer_source()
        refund = self.complete()
        nid = self.negative(refund)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        report, row = self.report_row(nid)
        self.assertEqual((row['gross_amount'], row['buyer_cash_refund'], row['external_funding_return']), ('-22.00', '20.00', '2.00'))
        self.assertEqual(row['funding_absorption_reversal'], '0.00')
        self.assertEqual(len(row['funding_returns']), 1)
        credit = row['funding_returns'][0]
        self.assertEqual((credit['participant_id'], credit['amount'], credit['paid_amount'], credit['offset_amount'], credit['remaining_amount']),
                         ('third-party-payer', '2.00', '0.00', '0.00', '2.00'))
        self.assertEqual(credit['reference_settlement_id'], source)
        self.assertFalse(credit['bank_receipt_verified'])
        segment = row['economic_segments'][0]
        self.assertTrue(segment['reference_settlement_no'].startswith('SET-'))
        self.assertTrue(segment['source_order_no'].startswith('ORD-'))
        self.assertEqual((segment['source_gross_amount'], segment['source_cost_amount'], segment['source_profit_amount']),
                         ('110.00', '40.00', '70.00'))
        exported = self.client.get('/api/settlement-reporting/export', params=dict(
            start='1900-01-01', end='9998-12-31', source_kind='refund', kind='details'),
            headers={'Authorization': 'Bearer ' + self.tokens['finance']})
        self.assertEqual(exported.status_code, 200)
        csv_row = next(csv.DictReader(io.StringIO(exported.content.decode('utf-8-sig'))))
        self.assertEqual(csv_row['买方实际退款'], '20.00')
        self.assertEqual(csv_row['原补差付款方返还权益'], '2.00')
        self.assertEqual(json.loads(csv_row['独立补差返还权益及余额']), row['funding_returns'])
        self.assertFalse(any(subject['participant_id'] == 'third-party-payer' for subject in row['participants']))
        params = dict(start='1900-01-01', end='9998-12-31', business_view='funding_payer', enterprise_id='third-party-payer')
        own = self.request('GET', '/api/settlement-reporting/report', actor='third-party', params=params)
        self.assertEqual(own['total'], 1)
        self.assertEqual((own['summary']['funding_return_amount'], own['summary']['payable_amount'], own['summary']['unpaid_amount']),
                         ('2.00', '2.00', '2.00'))
        self.assertEqual(own['summary']['profit_share'], '0.00')
        self.assertEqual(own['summary']['cost_amount'], '0.00')
        for private in ('buyer_cash_refund', 'economic_segments', 'source_shares', 'gross_amount', 'baseline_version'):
            self.assertNotIn(private, json.dumps(own))
        self.request('GET', '/api/settlement-reporting/report', actor='third-party', expected=403,
                     params=dict(params, enterprise_id='provider_tenant'))
        self.request('GET', '/api/settlement-reporting/report', actor='third-party', expected=403,
                     params=dict(params, business_view='provider'))
        self.request('GET', '/api/settlement-reporting/report', actor='buyer', expected=403, params=params)
        csv_response = self.client.get('/api/settlement-reporting/export', params=dict(params, kind='details'),
                                       headers={'Authorization': 'Bearer ' + self.tokens['third-party']})
        self.assertEqual(csv_response.status_code, 200)
        self.assertNotIn('买方实际退款', csv_response.text)
        self.assertNotIn('原最终基线摘要', csv_response.text)

    def test_external_return_manual_reviewed_partial_payout_moves_only_own_payable(self):
        self.external_payer_source()
        refund = self.complete()
        nid = self.negative(refund)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        _, row = self.report_row(nid)
        position = row['funding_returns'][0]['position_id']
        self.confirm_return_batch(nid)
        tx = self.request('POST', '/api/settlement-finance/transactions', actor='finance', json=dict(
            request_id='payer-manual-payment', kind='payout', mode='manual', reference='PAYER-RETURN-VOUCHER',
            note='Manual return record', allocations=[dict(position_id=position, amount='0.75')]))
        self.request('POST', f'/api/settlement-finance/transactions/{tx["id"]}/confirm', actor='ops', json=dict(
            request_id='payer-payment-review', expected_version=1, note='Independent return payment review'))
        _, updated = self.report_row(nid)
        credit = updated['funding_returns'][0]
        self.assertEqual((credit['amount'], credit['paid_amount'], credit['remaining_amount']), ('2.00', '0.75', '1.25'))
        self.assertFalse(credit['bank_receipt_verified'])
        self.assertEqual(updated['buyer_cash_refund'], '20.00')
        params = dict(start='1900-01-01', end='9998-12-31', business_view='funding_payer', enterprise_id='third-party-payer')
        own = self.request('GET', '/api/settlement-reporting/report', actor='third-party', params=params)
        bridge = own['balance_bridge']
        self.assertTrue(bridge['known'], bridge)
        self.assertEqual((bridge['payable_added'], bridge['paid'], bridge['closing_payable']), ('2.00', '0.75', '1.25'))
        self.assertEqual(bridge['internal_absorption_reversal'], '0.00')

    def test_external_return_offset_changes_balances_not_cost_profit_or_cash(self):
        from sqlalchemy import select
        source = self.external_source()
        with self.m.SessionLocal() as db:
            P = self.m.settlement_finance['Position']
            original_payables = db.scalars(select(P).where(P.settlement_id == source,
                P.direction == 'payable', P.participant_type == 'enterprise', P.participant_id == 'provider_tenant')).all()
            allocations = [dict(position_id=p.id, amount=str(p.amount)) for p in original_payables]
        payment = self.request('POST', '/api/settlement-finance/transactions', actor='finance', json=dict(
            request_id='original-provider-payment', kind='payout', mode='manual', reference='ORIGINAL-PROVIDER-PAYMENT',
            note='Original entitlements paid before refund', allocations=allocations))
        self.request('POST', f'/api/settlement-finance/transactions/{payment["id"]}/confirm', actor='ops', json=dict(
            request_id='original-provider-payment-review', expected_version=1, note='Independent payment review'))
        refund = self.complete()
        nid = self.negative(refund)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        _, before = self.report_row(nid)
        credit = before['funding_returns'][0]
        self.confirm_return_batch(nid)
        # Batch confirmation applies the exact-source return offset policy.
        report, after = self.report_row(nid)
        returned = after['funding_returns'][0]
        self.assertEqual(returned['position_id'], credit['position_id'])
        self.assertEqual((returned['paid_amount'], returned['offset_amount'], returned['remaining_amount']), ('0.00', '2.00', '0.00'))
        self.assertEqual((after['gross_amount'], after['cost_amount'], after['profit_amount']),
                         (before['gross_amount'], before['cost_amount'], before['profit_amount']))
        self.assertEqual(after['funds']['paid_amount'], '0.00')
        self.assertEqual(after['funds']['recovered_amount'], '0.00')
        self.assertEqual(after['funds']['payable_offset_amount'], '2.00')
        self.assertEqual(after['funds']['receivable_offset_amount'], '2.00')

    def test_external_retained_cost_keeps_ordinary_shares_losses_and_payer_return_separate(self):
        self.external_payer_source()
        refund = self.complete()
        self.approved_cost(refund)
        nid = self.negative(refund)
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
        _, row = self.report_row(nid)
        self.assertEqual((row['gross_amount'], row['cost_amount'], row['profit_amount'], row['buyer_cash_refund']),
                         ('-22.00', '-3.00', '-19.00', '20.00'))
        self.assertEqual(row['external_funding_return'], '2.00')
        self.assertEqual(sum(Decimal(p['loss_amount']) for p in row['participants']), Decimal('5.00'))
        self.assertEqual(sum(Decimal(p['profit_share']) for p in row['participants']), Decimal('-14.00'))
        self.assertEqual(sum(Decimal(p['cost_amount']) for p in row['participants']), Decimal('-3.00'))
        self.assertFalse(any(p['participant_id'] == 'third-party-payer' for p in row['participants']))
        self.assertEqual(sum(Decimal(p['amount']) for p in row['funding_returns']), Decimal('2.00'))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (FundingReturnReportingTests, FundedSourceReportingTests, ExternalPayerReportingTests)
                              for name in cls.__dict__ if name.startswith('test_'))


if __name__ == '__main__':
    unittest.main()
