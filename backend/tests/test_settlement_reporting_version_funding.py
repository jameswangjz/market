"""VC helper/report contracts with isolated SQLite, real funding proof, no live I/O."""
import hashlib
import json
import unittest
import importlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select, update

from test_settlement_reporting_funding_returns import ExternalPayerReportingTests
from test_settlement_reporting import SettlementReportingTests


class VersionFundingReportingTests(ExternalPayerReportingTests):
    def vc_old(self, *, internal=False):
        source_id = self.funded_source()[0] if internal else self.external_payer_source()
        m, finance = self.m, self.m.settlement_finance
        with m.SessionLocal() as db:
            hooks = m.settlement_version_funding
            source = db.get(finance['Baseline'], source_id)
            negative = m.Settlement(settlement_no='SET-VC-FUNDING-OLD', order_id=source.order_id,
                reference_settlement_id=source_id, status='locked', gross_amount=-22, cost_amount=-8,
                profit_amount=-14, platform_fee=-7, provider_share=-7, service_share=0, expert_fee=0, channel_fee=0)
            db.add(negative)
            db.flush()
            projection = hooks['project'](db, source, '100.00', '20.00',
                SimpleNamespace(id='vc-window', payload_hash='a' * 64), SimpleNamespace(id='vc-change'),
                SimpleNamespace(id='vc-receipt', receipt_hash='b' * 64), negative.id)
            context = dict(side='old', settlement_id=negative.id, order_id=source.order_id,
                source_settlement_id=source_id, source_baseline_hash=source.snapshot_hash,
                provider_enterprise_id=source.provider_enterprise_id, currency='CNY',
                economic_snapshot=json.loads(source.snapshot_json)['economic_snapshot'],
                values=projection['values'], version_funding_projection=projection,
                returned_unused_supplement=projection['returned_unused_supplement'],
                **{k: projection[k] for k in ('change_id', 'window_id', 'window_hash', 'receipt_id', 'receipt_hash')})
            context['context_hash'] = hashlib.sha256(self.canonical(context).encode()).hexdigest()
            captured = finance['capture'](db, negative, actor='finance', _segment_context=context)
            self.assertNotIsInstance(captured, dict)
            hooks['post'](db, negative, context, 'finance')
            nid = negative.id
            db.commit()
        return source_id, nid

    @staticmethod
    def canonical(value):
        from app.settlement_finance import canonical
        return canonical(value)

    def read_old(self, nid):
        data = self.request('GET', '/api/settlement-reporting/report', actor='finance',
            params=dict(start='1900-01-01', end='9998-12-31'))
        return data, next(r for r in data['items'] if r['id'] == nid)

    def test_vc_return_uses_independent_helper_not_refund_and_preserves_owner_privacy(self):
        source, nid = self.vc_old()
        with patch.dict(self.m.settlement_funded_refund, describe=lambda *_: self.fail('Refund describe called for VC')):
            data, row = self.read_old(nid)
        self.assertEqual(row['funded_refund'], {})
        self.assertEqual(row['version_funding']['policy'], 'return_unused_to_original_payer_not_target')
        self.assertIsNone(row['buyer_cash_refund'])
        self.assertEqual((row['gross_amount'], row['cost_amount'], row['profit_amount']), ('-22.00', '-8.00', '-14.00'))
        self.assertEqual(row['external_funding_return'], '2.00')
        credit = row['funding_returns'][0]
        self.assertEqual((credit['reference_settlement_id'], credit['amount'], credit['remaining_amount']), (source, '2.00', '2.00'))
        self.assertFalse(credit['bank_receipt_verified'])
        self.assertEqual((credit['paid_amount'], credit['offset_amount']), ('0.00', '0.00'))
        self.assertFalse(any(p['participant_id'] == 'third-party-payer' for p in row['participants']))
        self.assertTrue(data['balance_bridge']['known'], data['balance_bridge'])
        self.assertEqual(data['balance_bridge']['internal_absorption_reversal'], '0.00')
        exported = self.client.get('/api/settlement-reporting/export', params=dict(
            start='1900-01-01', end='9998-12-31', kind='details'),
            headers={'Authorization': 'Bearer ' + self.tokens['finance']})
        self.assertEqual(exported.status_code, 200)
        self.assertIn('版本变更未使用补款返还依据', exported.text)
        own = self.request('GET', '/api/settlement-reporting/report', actor='third-party', params=dict(
            start='1900-01-01', end='9998-12-31', business_view='funding_payer', enterprise_id='third-party-payer'))
        self.assertEqual(own['summary']['funding_return_amount'], '2.00')
        self.assertEqual(own['summary']['unpaid_amount'], '2.00')
        for hidden in ('gross_amount', 'economic_segments', 'source_shares', 'version_funding', 'buyer_cash_refund'):
            self.assertNotIn(hidden, json.dumps(own))
        self.request('GET', '/api/settlement-reporting/report', actor='buyer', expected=403,
            params=dict(business_view='funding_payer', enterprise_id='third-party-payer'))

    def test_vc_credit_wrong_payer_fails_closed_in_platform_and_own_views(self):
        _, nid = self.vc_old()
        P = self.m.settlement_finance['Position']
        with self.m.SessionLocal() as db:
            db.execute(update(P).where(P.settlement_id == nid, P.kind == 'funding_return').values(participant_id='provider_tenant'))
            db.commit()
        self.request('GET', '/api/settlement-reporting/report', actor='finance', expected=409,
                     params=dict(start='1900-01-01', end='9998-12-31'))
        self.request('GET', '/api/settlement-reporting/report', actor='provider', expected=409,
            params=dict(start='1900-01-01', end='9998-12-31', business_view='funding_payer', enterprise_id='provider_tenant'))

    def test_platform_unused_absorption_is_internal_credit_not_external_payable(self):
        _, nid = self.vc_old(internal=True)
        data, row = self.read_old(nid)
        self.assertEqual(row['version_funding']['policy'], 'restore_unused_platform_absorption_not_target')
        self.assertEqual(row['funded_refund'], {})
        self.assertEqual(row['external_funding_return'], '0.00')
        self.assertEqual(row['funding_returns'], [])
        self.assertEqual(row['funding_absorption_reversal'], '2.00')
        self.assertIsNone(row['buyer_cash_refund'])
        self.assertEqual((row['gross_amount'], row['cost_amount'], row['profit_amount']), ('-22.00', '-8.00', '-14.00'))
        self.assertEqual(row['funds']['payable_amount'], '0.00')
        self.assertEqual(row['funds']['paid_amount'], '0.00')
        self.assertEqual(row['funds']['internal_profit'], '-5.00')
        platform = next(p for p in row['participants'] if p['participant_id'] == 'platform')
        self.assertEqual((platform['profit_share'], platform['funding_absorption_reversal'], platform['net_total_receivable']),
                         ('-7.00', '2.00', '-5.00'))
        bridge = data['balance_bridge']
        self.assertTrue(bridge['known'], bridge)
        self.assertEqual(bridge['internal_absorption'], '10.00')
        self.assertEqual(bridge['internal_absorption_reversal'], '2.00')
        self.assertEqual(bridge['closing_internal_profit'], '20.00')
        own = self.request('GET', '/api/settlement-reporting/report', actor='provider', params=dict(
            start='1900-01-01', end='9998-12-31', business_view='funding_payer', enterprise_id='provider_tenant'))
        self.assertEqual(own['total'], 0)
        self.assertEqual(own['summary']['funding_return_amount'], '0.00')

    def test_platform_credit_cannot_be_reclassified_as_external_cash_payable(self):
        _, nid = self.vc_old(internal=True)
        P = self.m.settlement_finance['Position']
        with self.m.SessionLocal() as db:
            db.execute(update(P).where(P.settlement_id == nid, P.kind == 'funding_absorption_return')
                       .values(kind='funding_return', direction='payable'))
            db.commit()
        self.request('GET', '/api/settlement-reporting/report', actor='finance', expected=409,
                     params=dict(start='1900-01-01', end='9998-12-31'))

    def test_platform_restoration_bridge_recognizes_credit_once_in_recorded_period(self):
        _, nid = self.vc_old(internal=True)
        hooks = self.m.settlement_version_funding
        with self.m.SessionLocal() as db:
            db.execute(update(hooks['Projection']).where(hooks['Projection'].settlement_id == nid)
                       .values(created_at=datetime(2028, 1, 1, tzinfo=timezone.utc)))
            db.commit()
        reporting = importlib.import_module(self.m.__package__ + '.settlement_reporting')
        with SettlementReportingTests.clock_patch(self, reporting, datetime(2030, 1, 1, tzinfo=timezone.utc)):
            bridges = [self.request('GET', '/api/settlement-reporting/report', actor='finance',
                                   params=dict(year=year))['balance_bridge'] for year in (2027, 2028, 2029)]
        for bridge in bridges:
            self.assertTrue(bridge['known'], bridge)
        before, during, after = bridges
        self.assertEqual(before['internal_absorption_reversal'], '0.00')
        self.assertEqual(before['closing_internal_profit'], '18.00')
        self.assertEqual(during['opening_internal_profit'], '18.00')
        self.assertEqual(during['internal_absorption_reversal'], '2.00')
        self.assertEqual(during['closing_internal_profit'], '20.00')
        self.assertEqual(after['opening_internal_profit'], '20.00')
        self.assertEqual(after['internal_absorption_reversal'], '0.00')
        self.assertEqual(after['closing_internal_profit'], '20.00')

    def test_vc_credit_source_mapping_tamper_is_not_accepted(self):
        _, nid = self.vc_old()
        finance = self.m.settlement_finance
        with self.m.SessionLocal() as db:
            pid = db.scalar(select(finance['Position'].id).where(finance['Position'].settlement_id == nid,
                finance['Position'].kind == 'funding_return'))
            db.execute(update(finance['RefundPositionSource']).where(finance['RefundPositionSource'].position_id == pid)
                       .values(source_settlement_id=nid))
            db.commit()
        self.request('GET', '/api/settlement-reporting/report', actor='finance', expected=409,
                     params=dict(start='1900-01-01', end='9998-12-31'))

    def test_target_context_cannot_inherit_old_return_even_with_consistent_hashes(self):
        _, nid = self.vc_old()
        B = self.m.settlement_finance['Baseline']
        with self.m.SessionLocal() as db:
            snapshot = json.loads(db.get(B, nid).snapshot_json)
            context = snapshot['segment_context']
            context['side'] = 'target'
            context['context_hash'] = hashlib.sha256(self.canonical({k: v for k, v in context.items()
                if k not in ('status', 'context_hash')}).encode()).hexdigest()
            snapshot['segment_context_hash'] = context['context_hash']
            raw = self.canonical(snapshot)
            db.execute(update(B).where(B.settlement_id == nid).values(snapshot_json=raw,
                snapshot_hash=hashlib.sha256(raw.encode()).hexdigest()))
            db.commit()
        self.request('GET', '/api/settlement-reporting/report', actor='finance', expected=409,
                     params=dict(start='1900-01-01', end='9998-12-31'))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(VersionFundingReportingTests(name) for name in VersionFundingReportingTests.__dict__
                              if name.startswith('test_'))


if __name__ == '__main__':
    unittest.main()
