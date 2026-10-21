"""VC equity transfers must not bypass genuine supplement evidence rules."""
from unittest.mock import patch
import hashlib
import json

from sqlalchemy import event

import test_cross_version_final_baseline as cross
import test_settlement_cost_exception_basis_http as discovery
import test_settlement_version_funding_http as version_funding


class SegmentClassificationTests(cross.CrossVersionFinalBaselineTests):
    def test_legacy_exception_rejects_unverified_segment_context(self):
        self.completed_segment_refund()
        hook = self.m.version_change_settlement['financial_context']

        def invalid(db, settlement):
            return dict(hook(db, settlement), context_hash='unverified')

        with patch.dict(self.m.version_change_settlement, financial_context=invalid):
            result = self.request('POST', '/api/settlement-cost-exceptions', actor='platform',
                                  expected=422, json=self.exception_body)
        self.assertEqual(result['detail'], 'funded_source_allocations_required')

    def test_legacy_exception_rejects_funded_projection_even_with_target_binding(self):
        self.completed_segment_refund()
        hook = self.m.version_change_settlement['financial_context']

        def projected(db, settlement):
            return dict(hook(db, settlement), version_funding_projection={'present': True})

        with patch.dict(self.m.version_change_settlement, financial_context=projected):
            result = self.request('POST', '/api/settlement-cost-exceptions', actor='platform',
                                  expected=422, json=self.exception_body)
        self.assertEqual(result['detail'], 'funded_source_allocations_required')


class FundedLegacyClassificationTests(discovery.CostBasisSingleHTTPTests):
    def test_real_funded_source_rejects_legacy_single_payload(self):
        self.funded_source()
        refund = self.complete()
        bearer = self.request('POST', '/api/settlement-parties', actor='finance', json=dict(
            request_id='legacy-bearer', name='Provider loss', kind='provider',
            enterprise_id='provider_tenant', currency='CNY'))
        result = self.request('POST', '/api/settlement-cost-exceptions', actor='finance', expected=422,
            json=dict(request_id='legacy-funded', refund_id=refund['id'], retained_cost='5.00',
                      reason='Retained work', evidence_refs=['invoice:1'], bearer_participant_id=bearer['id']))
        self.assertEqual(result['detail'], 'funded_source_allocations_required')


class ProjectedTargetCostTests(version_funding.VersionFundingHTTPTests):
    def test_projected_funding_ends_at_old_side_for_target_cost_exception(self):
        def prepare(*args, **kwargs):
            result = version_funding.VersionFundingHTTPTests.prepare(self, *args, **kwargs)
            with self.m.SessionLocal() as db:
                target = db.get(self.m.ProductReleaseVersion, 'v2')
                target.price = 400
                db.commit()
            return result

        with patch.object(self, 'prepare', prepare):
            self.funded_scenario()
        Baseline = self.m.settlement_finance['Baseline']

        def legacy_receipts(_mapper, _connection, row):
            # Historical captures stored original order cash, not transferred
            # economic rights. Preserve that immutable shape for this regression.
            if row.settlement_id == self.target_id:
                snapshot = json.loads(row.snapshot_json)
                snapshot['actual_receipts'] = '150.00'
                row.snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(',', ':'))
                row.snapshot_hash = hashlib.sha256(row.snapshot_json.encode()).hexdigest()

        event.listen(Baseline, 'before_insert', legacy_receipts)
        try:
            for sid in (self.old_id, self.target_id):
                self.request('POST', f'/api/settlements/{sid}/lock', actor='finance')
        finally:
            event.remove(Baseline, 'before_insert', legacy_receipts)
        with self.m.SessionLocal() as db:
            baseline = db.get(self.m.settlement_finance['Baseline'], self.target_id)
            self.assertNotEqual(str(baseline.revenue), str(json.loads(baseline.snapshot_json)['actual_receipts']))
            nid = self.target_refund(db)
            db.commit()
        bearer = self.request('POST', '/api/settlement-parties', actor='finance', json=dict(
            request_id='target-bearer', name='Target retained work', kind='provider',
            enterprise_id='provider_tenant', currency='CNY'))
        exception = self.request('POST', '/api/settlement-cost-exceptions', actor='finance', json=dict(
            request_id='target-cost', refund_id='multi', retained_cost='1.00',
            reason='Target work retained', evidence_refs=['invoice:target'], bearer_participant_id=bearer['id']))
        self.request('POST', f'/api/settlement-cost-exceptions/{exception["id"]}/decision', actor='provider',
            json=dict(request_id='target-review', expected_version=1, decision='approve', reason='Independent approval'))
        self.request('POST', f'/api/settlements/{nid}/lock', actor='finance')
