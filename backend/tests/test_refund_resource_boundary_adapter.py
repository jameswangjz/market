"""Descriptor tests use Redis; resource plumbing tests explicitly mock the switch."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

import test_refund_segment_store as store_fixture
import test_version_change_resources as resource_fixture
from app import version_change_resources as resources
from app.refund_segment_store import boundary_periods
from app.quota_cutover import token


class DescriptorTests(unittest.TestCase):
    setUpClass = classmethod(store_fixture.StoreTests.setUpClass.__func__)
    tearDownClass = classmethod(store_fixture.StoreTests.tearDownClass.__func__)
    setUp = store_fixture.StoreTests.setUp

    def descriptor(self):
        self.client.hset('market:apisix:credential:store', mapping=self.original)
        plan = dict(credentials=[dict(id='cred', consumer='store', status='active')],
            periods=dict(original=json.loads(self.original['subscription_periods']), target=self.periods))
        return self.hooks['descriptor'](self.db, self.change, plan, self.client)

    def test_strict_freezes_original_and_complete_target_no_secret(self):
        descriptor = self.descriptor()
        self.assertEqual('strict', descriptor['mode'])
        self.assertEqual(self.original, descriptor['original'])
        self.assertEqual(self.target, descriptor['target'])
        self.assertEqual(2, len(boundary_periods(descriptor['target'])))

    def test_missing_coverage_or_cursor_is_explicit_legacy(self):
        self.client.delete('market:apisix:charges:coverage:combo:' + token('combo:1:1'))
        self.assertEqual('legacy', self.descriptor()['mode'])
        self.db.delete(self.db.get(self.Cursor, 'market:apisix:charges:v1'))
        self.db.flush()
        self.assertEqual('legacy', self.descriptor()['mode'])

    def test_registered_epoch_restart_requires_review(self):
        self.client.hset('market:apisix:charges:source', 'run_id', 'b'*40)
        with self.assertRaises(HTTPException) as exc:
            self.descriptor()
        self.assertEqual(409, exc.exception.status_code)


class ResourcePlumbingTests(unittest.TestCase):
    def run_saas(self, change_fields, status_fields, installed=True):
        original_install = resources.install
        recorded = []
        def install(ns):
            original_call = ns['saas_call']
            def call(config, operation, payload):
                self.assertEqual('chg-1', payload['change_id'])
                self.assertEqual('target', payload['direction'])
                result = original_call(config, operation, payload)
                return dict(result, **(change_fields if operation == 'CHANGE' else status_fields))
            ns['saas_call'] = call
            if installed:
                def record(db, change, credential, direction, receipt, *, subscription):
                    self.assertIsNone(credential)
                    self.assertEqual('sub-1', subscription.id)
                    recorded.append(receipt)
                    return SimpleNamespace(id='saas-receipt') if receipt['change'].get('effective_at') else None
                ns['refund_segment_store'] = {'record_receipt': record}
            return original_install(ns)
        with patch.object(resource_fixture, 'install', install):
            resource_fixture.VersionChangeResourceTests().test_saas_change_requires_identity_bound_status_before_local_version_moves()
        return recorded

    def test_saas_adapter_requests_bound_identity_and_records_matching_response(self):
        fields = dict(change_id='chg-1', direction='target', effective_at='2026-10-15T00:00:00+00:00')
        recorded = self.run_saas(fields, dict(fields, effective_at='2026-10-15T00:00:00Z'))
        self.assertEqual(1, len(recorded))
        self.assertNotIn('product_id', recorded[0]['change'])

    def test_saas_adapter_all_none_legacy_and_partial_mismatch_rejected(self):
        missing = dict(change_id=None, direction=None, effective_at=None)
        self.assertEqual(1, len(self.run_saas(missing, missing)))
        complete = dict(change_id='chg-1', direction='target', effective_at='2026-10-15T00:00:00Z')
        for changed, status in ((missing, complete), (complete, dict(complete, direction='original')),
                                (complete, dict(complete, effective_at='2026-10-15T00:00:01Z'))):
            with self.subTest(changed=changed, status=status), self.assertRaises(HTTPException):
                self.run_saas(changed, status)

    def run_existing(self, mode):
        original_install = resources.install
        switches, recorded = [], []

        def install(ns):
            def record(db, change, credential, direction, receipt, *, client):
                recorded.append((direction, receipt))
                return SimpleNamespace(id='receipt-' + direction)
            ns['refund_segment_store'] = {'record_receipt': record}
            hooks = original_install(ns)
            original_apply = hooks['apply']
            def apply(db, change, direction='target'):
                plan = json.loads(change.resource_plan_json)
                if 'charge_boundary' not in plan and mode is not None:
                    client = ns['apisix_policy_redis']()
                    original = client.hgetall('market:apisix:credential:consumer-2')
                    target = dict(original, subscription_periods=json.dumps(plan['periods']['target'], separators=(',', ':')),
                                  daily_quota='22', monthly_quota='100', total_quota='0')
                    plan['charge_boundary'] = dict(mode=mode, credential_id='cred-2', original=original, target=target)
                    change.resource_plan_json = json.dumps(plan)
                return original_apply(db, change, direction)
            hooks['apply'] = apply
            return hooks

        def switch(client, **kwargs):
            switches.append(copy.deepcopy(kwargs))
            client.hset(kwargs['credential_key'], mapping=kwargs['target_policy'])
            return {'direction': kwargs['direction']}

        with patch.object(resource_fixture, 'install', install), patch.object(resources, 'freeze_policy_switch', switch):
            resource_fixture.VersionChangeResourceTests('test_api_apply_replay_preserves_consumer_key_and_quota_identity').test_api_apply_replay_preserves_consumer_key_and_quota_identity()
        return switches, recorded

    def test_strict_apply_uses_fixed_policies_and_three_key_proof(self):
        switches, recorded = self.run_existing('strict')
        self.assertEqual(switches[0], switches[1])
        self.assertEqual(switches[0]['expected_policy'], switches[2]['target_policy'])
        self.assertEqual(switches[0]['target_policy'], switches[2]['expected_policy'])
        self.assertEqual(['target', 'target', 'original'], [direction for direction, _ in recorded])

    def test_installed_store_historical_plan_and_legacy_still_use_old_behavior(self):
        for mode in (None, 'legacy'):
            with self.subTest(mode=mode):
                switches, recorded = self.run_existing(mode)
                self.assertEqual([], switches)
                self.assertEqual([], recorded)


if __name__ == '__main__':
    unittest.main()
