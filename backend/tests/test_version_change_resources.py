"""External resource adapter contract tests with stateful APISIX/Redis fakes."""
import hashlib
import json
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from app.version_change_resources import install
from app.version_change_resources import _integration_snapshot_matches, _quota_period_plan
from app.subscription_calendar import CalendarAnchor
from app.saas_reconciliation import change_context


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.corrupt_next = None
        self.close_count = 0

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})
        if self.corrupt_next:
            field, value = self.corrupt_next
            self.hashes[key][field] = str(value)
            self.corrupt_next = None

    def close(self):
        self.close_count += 1


class FakeDB:
    def __init__(self, objects):
        self.objects = objects

    def get(self, model, key):
        return self.objects.get((model, key))

    def execute(self, _statement):
        return SimpleNamespace(all=lambda: [])

    def scalar(self, statement):
        model = statement.column_descriptions[0]['entity']
        for clause in getattr(statement, 'filters', []):
            right = getattr(clause, 'right', None)
            value = getattr(right, 'value', None)
            if value is not None:
                match = next((item for (kind, _key), item in self.objects.items()
                              if kind is model and any(getattr(item, field, None) == value
                                  for field in ('version_id', 'id', 'client_id'))), None)
                if match is not None:
                    return match
        return next((value for (kind, _key), value in self.objects.items() if kind is model), None)

    def scalars(self, statement):
        model = statement.column_descriptions[0]['entity']
        return SimpleNamespace(all=lambda: [value for (kind, _key), value in self.objects.items()
                                            if kind is model])


class Query:
    def __init__(self, model): self.model, self.filters = model, []
    @property
    def column_descriptions(self): return [{'entity': self.model}]
    def where(self, *args): self.filters.extend(args); return self
    def order_by(self, *_args): return self
    def with_for_update(self): return self


class FilterColumn:
    def __eq__(self, value):
        return SimpleNamespace(right=SimpleNamespace(value=value))
    def in_(self, values):
        return SimpleNamespace(right=SimpleNamespace(value=values))


class SortColumn(FilterColumn):
    def desc(self): return self


class VersionChangeResourceTests(unittest.TestCase):
    def test_api_integration_snapshot_freezes_accepted_quote_identity(self):
        config = SimpleNamespace(id='cfg-1', revision_id='rev-1', evidence_hash='a' * 64)
        route = SimpleNamespace(id='route-1', route_key='route-v2')
        snapshot = {'integration': {'config_id': 'cfg-1', 'revision_id': 'rev-1',
            'evidence_hash': 'a' * 64, 'route_id': 'route-1', 'route_key': 'route-v2',
            'quote_fingerprint': 'q' * 64}}
        self.assertTrue(_integration_snapshot_matches(snapshot, config, route, 'q' * 64))
        self.assertFalse(_integration_snapshot_matches(snapshot, config, route, 'x' * 64))
        config.revision_id = 'rev-edited-after-quote'
        self.assertFalse(_integration_snapshot_matches(snapshot, config, route, 'q' * 64))

    @staticmethod
    def saas_config_fingerprint(config):
        fields = ('id', 'base_url', 'operation_path', 'token_url', 'client_id', 'scope',
                  'auth_mode', 'timeout_ms', 'status')
        safe = {key: getattr(config, key, None) for key in fields}
        encoded = json.dumps(safe, sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(encoded).hexdigest()

    def test_quota_plan_preserves_expired_buckets_and_updates_only_current_period(self):
        anchor_at = __import__('datetime').datetime(2026, 1, 1,
            tzinfo=__import__('datetime').timezone.utc)
        anchor = CalendarAnchor(anchor_at)
        expired_bounds = anchor.term(1, start_month=0)
        active_bounds = anchor.term(2, start_month=1)
        expired = SimpleNamespace(id='expired-term', order_id='expired-order', months=1, start_month=0,
            starts_at=expired_bounds.start, ends_at=expired_bounds.end)
        active = SimpleNamespace(id='active-term', order_id='active-order', months=2, start_month=1,
            starts_at=active_bounds.start, ends_at=active_bounds.end)
        combo = SimpleNamespace(id='combo', generation=4, total_months=3, anchor_at=anchor_at)
        old = {'quota': {'rate_limit_per_minute': 3, 'daily_quota': 10, 'monthly_quota': 40}}
        target = {'delivery': {'quota': {'rate_limit_per_minute': 8,
            'daily_quota': 20, 'monthly_quota': 90}}}
        at = __import__('datetime').datetime(2026, 3, 1,
            tzinfo=__import__('datetime').timezone.utc)
        result = _quota_period_plan(combo, [(expired, old), (active, old)],
            {'active-term': target}, 'route-old', 'route-target', at)
        original, changed = result['original'], result['target']
        self.assertEqual([p['period_key'] for p in original], [p['period_key'] for p in changed])
        self.assertEqual([p['route_key'] for p in changed], ['route-old', 'route-old', 'route-target'])
        self.assertEqual([p['daily_quota'] for p in changed], [10, 10, 20])
        for side in ('original', 'target'):
            self.assertEqual([(p['order_id'], p['segment_id']) for p in result[side]],
                [('expired-order', 'expired-term'), ('active-order', 'active-term'),
                 ('active-order', 'active-term')])

    def test_two_period_order_keeps_charge_identity_across_route_change(self):
        anchor_at = __import__('datetime').datetime(2026, 1, 1,
            tzinfo=__import__('datetime').timezone.utc)
        anchor = CalendarAnchor(anchor_at)
        bounds = anchor.term(2)
        term = SimpleNamespace(id='term-1', order_id='order-1', months=2, start_month=0,
            starts_at=bounds.start, ends_at=bounds.end)
        combo = SimpleNamespace(id='combo', generation=2, total_months=2, anchor_at=anchor_at)
        old = {'quota': {'rate_limit_per_minute': 3, 'daily_quota': 10, 'monthly_quota': 40}}
        target = {'delivery': {'quota': {'rate_limit_per_minute': 8,
            'daily_quota': 20, 'monthly_quota': 90}}}
        result = _quota_period_plan(combo, [(term, old)], {'term-1': target},
            'route-old', 'route-target', anchor_at)
        for side, route in (('original', 'route-old'), ('target', 'route-target')):
            self.assertEqual(len(result[side]), 2)
            for row in result[side]:
                self.assertEqual((row['order_id'], row['segment_id'], row['route_key']),
                                 ('order-1', 'term-1', route))
        for original, changed in zip(result['original'], result['target']):
            for field in ('period_key', 'start_at', 'end_at', 'order_id', 'segment_id'):
                self.assertEqual(original[field], changed[field])

    def test_overlapping_or_duplicate_terms_reject_ambiguous_bucket_identity(self):
        anchor_at = __import__('datetime').datetime(2026, 1, 1,
            tzinfo=__import__('datetime').timezone.utc)
        anchor = CalendarAnchor(anchor_at)
        combo = SimpleNamespace(id='combo', generation=2, total_months=3, anchor_at=anchor_at)
        old = {'quota': {'rate_limit_per_minute': 3, 'daily_quota': 10, 'monthly_quota': 40}}
        terms, snapshots = [], {}
        for name, start_month, months in (('first', 0, 2), ('overlap', 1, 1), ('future', 2, 1)):
            bounds = anchor.term(months, start_month=start_month)
            term = SimpleNamespace(id=name + '-term', order_id=name + '-order',
                months=months, start_month=start_month, starts_at=bounds.start, ends_at=bounds.end)
            terms.append((term, old))
            snapshots[term.id] = {'delivery': old}
        for conflicting in (terms, [terms[0], terms[0]]):
            with self.subTest(terms=conflicting):
                with self.assertRaisesRegex(ValueError, 'duplicate charge attribution'):
                    _quota_period_plan(combo, conflicting, snapshots, 'old', 'target', anchor_at)

    def test_nonoverlapping_current_and_future_orders_keep_each_term_identity(self):
        anchor_at = __import__('datetime').datetime(2026, 1, 1,
            tzinfo=__import__('datetime').timezone.utc)
        anchor = CalendarAnchor(anchor_at)
        combo = SimpleNamespace(id='combo', generation=2, total_months=3, anchor_at=anchor_at)
        old = {'quota': {'rate_limit_per_minute': 3, 'daily_quota': 10, 'monthly_quota': 40}}
        terms, snapshots = [], {}
        for name, start_month, months in (('first', 0, 2), ('future', 2, 1)):
            bounds = anchor.term(months, start_month=start_month)
            term = SimpleNamespace(id=name + '-term', order_id=name + '-order',
                months=months, start_month=start_month, starts_at=bounds.start, ends_at=bounds.end)
            terms.append((term, old))
            snapshots[term.id] = {'delivery': old}
        result = _quota_period_plan(combo, terms, snapshots, 'old', 'target', anchor_at)
        expected = [('first-order', 'first-term'), ('first-order', 'first-term'),
                    ('future-order', 'future-term')]
        for side in ('original', 'target'):
            self.assertEqual([(row['order_id'], row['segment_id']) for row in result[side]], expected)
            self.assertEqual(len({row['period_key'] for row in result[side]}), 3)
        self.assertEqual(result['target'][-1]['period_key'], 'combo:2:2')

    def test_new_period_plan_requires_real_term_charge_identity(self):
        anchor_at = __import__('datetime').datetime(2026, 1, 1,
            tzinfo=__import__('datetime').timezone.utc)
        bounds = CalendarAnchor(anchor_at).term(1)
        combo = SimpleNamespace(id='combo', generation=1, total_months=1, anchor_at=anchor_at)
        old = {'quota': {'rate_limit_per_minute': 3, 'daily_quota': 10, 'monthly_quota': 40}}
        for field in ('id', 'order_id'):
            for value in (None, '', ' ', 123, 'x' * 37, 'bad:id', 'bad.id', 'id\n', '\u00e9', '\uff21'):
                with self.subTest(field=field, value=value):
                    values = dict(id='term', order_id='order', months=1, start_month=0,
                                  starts_at=bounds.start, ends_at=bounds.end)
                    values[field] = value
                    with self.assertRaisesRegex(ValueError, 'charge attribution is missing'):
                        _quota_period_plan(combo, [(SimpleNamespace(**values), old)],
                            {'term': {'delivery': old}}, 'old', 'target', anchor_at)
            values.pop(field)
            with self.subTest(field=field, missing=True):
                with self.assertRaisesRegex(ValueError, 'charge attribution is missing'):
                    _quota_period_plan(combo, [(SimpleNamespace(**values), old)],
                        {'term': {'delivery': old}}, 'old', 'target', anchor_at)
        for value in ('A', 'aZ09_-', 'a' * 36):
            term = SimpleNamespace(id=value, order_id=value, months=1, start_month=0,
                                   starts_at=bounds.start, ends_at=bounds.end)
            result = _quota_period_plan(combo, [(term, old)], {value: {'delivery': old}},
                                       'old', 'target', anchor_at)
            self.assertEqual(result['target'][0]['segment_id'], value)

    def test_saas_change_requires_identity_bound_status_before_local_version_moves(self):
        class SaaSSubscription: pass
        class SaaSIntegrationConfig: pass
        class SaaSProductVersion: pass
        class SaaSUserMapping:
            subscription_id = status = user_id = external_user_id = object()
        class OIDCClient:
            product_id = client_id = status = object()

        sub = SimpleNamespace(id='sub-1', external_tenant_id='tenant-1', version_id='old', expires_at=None)
        config = SimpleNamespace(id='cfg-1')
        old = SimpleNamespace(version_code='v1')
        target = SimpleNamespace(version_code='v2')
        calls = []
        current = {'version': 'v2'}

        def saas_call(_config, operation, payload):
            calls.append((operation, payload))
            if operation == 'CHANGE':
                self.assertEqual(payload['idempotency_key'], 'version-change:chg-1:target')
                current['version'] = payload['to_version']
                return {'ret': 0, 'version': payload['to_version']}
            return {'ret': 0, 'tenant_id': 'tenant-1', 'product_id': 'prod-1',
                'enterprise_id': 'ent-1', 'subscription_id': 'sub-1',
                'version': current['version'], 'status': 'active', 'admin_user_id': 'admin-1',
                'admin_active': True, 'application_ready': True, 'oidc_client_id': 'oidc-1',
                'expires_at': None}

        ns = {'SaaSSubscription': SaaSSubscription, 'SaaSIntegrationConfig': SaaSIntegrationConfig,
              'SaaSProductVersion': SaaSProductVersion, 'SaaSUserMapping': SaaSUserMapping,
              'OIDCClient': OIDCClient, 'saas_call': saas_call}
        hooks = install(ns)
        frozen_context = change_context({'segments': [{'order_id': 'order-1', 'order_no': 'O-1',
            'starts_at': '2026-01-01T00:00:00Z', 'ends_at': '2026-02-01T00:00:00Z'}]},
            SimpleNamespace(id='order-1', order_no='O-1'), None)
        change = SimpleNamespace(id='chg-1', target_version_id='target', old_version_id='old',
            target_version_code='v2', old_version_code='v1', resource_plan_json=json.dumps({
                'change_id': 'chg-1', 'kind': 'saas', 'subscription_id': 'sub-1',
                'tenant_id': 'tenant-1', 'product_id': 'prod-1', 'enterprise_id': 'ent-1',
                'config_id': 'cfg-1', 'admin_user_id': 'admin-1',
                'external_admin_id': 'external-admin', 'oidc_client_id': 'oidc-1',
                'config_fingerprint': self.saas_config_fingerprint(config), 'expires_at': None,
                'reconciliation_context': frozen_context}), evidence_json='')
        db = FakeDB({(SaaSSubscription, 'sub-1'): sub, (SaaSIntegrationConfig, 'cfg-1'): config,
                     (SaaSProductVersion, 'target'): target,
                     (SaaSUserMapping, 'mapping'): SimpleNamespace(user_id='admin-1', external_user_id='external-admin'),
                     (OIDCClient, 'oidc'): SimpleNamespace(client_id='oidc-1')})
        adapter_module = sys.modules['app.version_change_resources']
        original_select = adapter_module.select
        adapter_module.select = lambda model: Query(model)
        try:
            proof = hooks['apply'](db, change, 'target')
        finally:
            adapter_module.select = original_select
        self.assertEqual([c[0] for c in calls], ['CHANGE', 'STATUS'])
        outbound = calls[0][1]['reconciliation_context']
        self.assertEqual(outbound['event_id'], 'version-change:chg-1:target')
        self.assertEqual(outbound['order_no'], 'O-1')
        self.assertEqual(outbound['affected_orders'], frozen_context['affected_orders'])
        self.assertEqual(calls[1][1]['reconciliation_context']['event_type'], 'STATUS')
        self.assertEqual(json.loads(change.resource_plan_json)['reconciliation_context'], frozen_context)
        self.assertEqual(sub.version_id, 'target')
        self.assertEqual(proof['request_change_id'], 'chg-1')
        self.assertEqual(proof['resource_change_id'], 'chg-1')
        self.assertRegex(proof['evidence_fingerprint'], r'^[0-9a-f]{64}$')
        self.assertNotIn('tenant-1', change.evidence_json)

    def test_saas_prepare_uses_original_buyer_mapping_with_other_members_present(self):
        class Combination:
            enterprise_id = product_id = delivery_method = object()
        class Subscription:
            enterprise_id = product_id = version_id = status = object()
        class Config:
            product_id = status = object()
        class Mapping:
            subscription_id = status = user_id = object()
        class Client:
            product_id = status = id = object()
        class Operation:
            subscription_id = status = object()
            operation = FilterColumn()
            created_at = SortColumn()
        class Order: pass

        combo = SimpleNamespace(id='combo-1', enterprise_id='ent-1', product_id='prod-1',
            delivery_method='tenant_access')
        subscription = SimpleNamespace(id='sub-1', enterprise_id='ent-1', product_id='prod-1',
            version_id='old', status='active', external_tenant_id='tenant-1', expires_at=None)
        config = SimpleNamespace(id='cfg-1', auth_mode='oauth2')
        admin = SimpleNamespace(user_id='buyer-user', external_user_id='provider-admin')
        member = SimpleNamespace(user_id='other-user', external_user_id='provider-member')
        client = SimpleNamespace(client_id='oidc-1')
        order = SimpleNamespace(id='order-1', buyer_user_id='buyer-user',
            buyer_enterprise_id='ent-1', product_id='prod-1')
        db = FakeDB({(Combination, 'combo-1'): combo, (Subscription, 'sub-1'): subscription,
            (Config, 'cfg-1'): config, (Mapping, 'admin-map'): admin,
            (Mapping, 'member-map'): member, (Client, 'oidc'): client, (Order, 'order-1'): order})
        ns = {'subscription_terms': {'Combination': Combination}, 'SaaSSubscription': Subscription,
            'SaaSIntegrationConfig': Config, 'SaaSUserMapping': Mapping,
            'OIDCClient': Client, 'SaaSOperation': Operation, 'Order': Order}
        hooks = install(ns)
        change = SimpleNamespace(id='change-prepare', combination_id='combo-1', enterprise_id='ent-1',
            source_order_id='order-1', old_version_id='old', target_version_id='target',
            old_version_code='v1', target_version_code='v2',
            target_snapshots_json=json.dumps({'term-1': {'integration': {
                'kind': 'saas', 'config_id': 'cfg-1',
                'config_fingerprint': self.saas_config_fingerprint(config),
                'quote_fingerprint': 'q' * 64}}}),
            quote_json=json.dumps({'quote_id': 'q' * 64, 'segments': [
                {'order_id': 'order-1', 'order_no': 'frozen-number',
                 'starts_at': '2026-01-01T00:00:00Z', 'ends_at': '2026-02-01T00:00:00Z',
                 'remaining_starts_at': '2026-01-15T00:00:00Z',
                 'remaining_ends_at': '2026-02-01T00:00:00Z'}]}),
            prepared_json=json.dumps([{'order_id': 'order-1', 'term_id': 'term-1'}]),
            resource_plan_json='{}')
        adapter_module = sys.modules['app.version_change_resources']
        original_select = adapter_module.select
        adapter_module.select = lambda model: Query(model)
        try:
            plan = hooks['prepare'](db, change)
            change.resource_plan_json = json.dumps(plan, indent=2)
            order.order_no = 'renamed-after-freeze'
            self.assertEqual(hooks['prepare'](db, change), plan)
            config.client_id = 'changed-after-quote'
            with self.assertRaises(Exception):
                hooks['prepare'](db, change)
        finally:
            adapter_module.select = original_select
        self.assertEqual(plan['admin_user_id'], 'buyer-user')
        self.assertEqual(plan['external_admin_id'], 'provider-admin')
        self.assertEqual(plan['oidc_client_id'], 'oidc-1')
        context = plan['reconciliation_context']
        self.assertEqual(context['order_no'], 'frozen-number')
        self.assertEqual(context['affected_orders'][0]['term_id'], 'term-1')
        self.assertIsNone(context['effective_at'])

    def test_saas_wrong_status_identity_rejects_without_local_mutation(self):
        class SaaSSubscription: pass
        class SaaSIntegrationConfig: pass
        class SaaSProductVersion: pass
        class SaaSUserMapping:
            subscription_id = status = user_id = external_user_id = object()
        class OIDCClient:
            product_id = client_id = status = object()

        sub = SimpleNamespace(id='sub-1', external_tenant_id='tenant-1', version_id='old', expires_at=None)
        config = SimpleNamespace(id='cfg-1')
        version = SimpleNamespace(version_code='v2')
        def call(_config, operation, _payload):
            if operation == 'CHANGE':
                return {'ret': 0}
            return {'ret': 0, 'tenant_id': 'tenant-other', 'product_id': 'prod-1',
                'enterprise_id': 'ent-1', 'subscription_id': 'sub-1', 'version': 'v2', 'status': 'active',
                'admin_user_id': 'admin-1', 'admin_active': True, 'application_ready': True,
                'oidc_client_id': 'oidc-1'}
        hooks = install({'SaaSSubscription': SaaSSubscription,
            'SaaSIntegrationConfig': SaaSIntegrationConfig, 'SaaSProductVersion': SaaSProductVersion,
            'SaaSUserMapping': SaaSUserMapping, 'OIDCClient': OIDCClient,
            'saas_call': call})
        change = SimpleNamespace(id='chg-2', target_version_id='target', old_version_id='old',
            target_version_code='v2', old_version_code='v1', evidence_json='', resource_plan_json=json.dumps({
                'change_id': 'chg-2', 'kind': 'saas', 'subscription_id': 'sub-1',
                'tenant_id': 'tenant-1', 'product_id': 'prod-1', 'enterprise_id': 'ent-1', 'config_id': 'cfg-1',
                'admin_user_id': 'admin-1', 'external_admin_id': 'external-admin', 'oidc_client_id': 'oidc-1',
                'config_fingerprint': self.saas_config_fingerprint(config), 'expires_at': None}))
        db = FakeDB({(SaaSSubscription, 'sub-1'): sub, (SaaSIntegrationConfig, 'cfg-1'): config,
                     (SaaSProductVersion, 'target'): version,
                     (SaaSUserMapping, 'mapping'): SimpleNamespace(user_id='admin-1', external_user_id='external-admin'),
                     (OIDCClient, 'oidc'): SimpleNamespace(client_id='oidc-1')})
        adapter_module = sys.modules['app.version_change_resources']
        original_select = adapter_module.select
        adapter_module.select = lambda model: Query(model)
        try:
            with self.assertRaises(Exception):
                hooks['apply'](db, change, 'target')
        finally:
            adapter_module.select = original_select
        self.assertEqual(sub.version_id, 'old')
        self.assertEqual(change.evidence_json, '')

    def test_saas_status_with_wrong_version_is_rejected(self):
        class SaaSSubscription: pass
        class SaaSIntegrationConfig: pass
        class SaaSProductVersion: pass
        class SaaSUserMapping:
            subscription_id = status = user_id = external_user_id = object()
        class OIDCClient:
            product_id = client_id = status = object()

        sub = SimpleNamespace(id='sub-1', external_tenant_id='tenant-1', version_id='old', expires_at=None)
        config = SimpleNamespace(id='cfg-1')
        version = SimpleNamespace(version_code='v2')
        def call(_config, operation, _payload):
            if operation == 'CHANGE':
                return {'ret': 0}
            return {'ret': 0, 'tenant_id': 'tenant-1', 'product_id': 'prod-1',
                'enterprise_id': 'ent-1', 'subscription_id': 'sub-1', 'version': 'v1',
                'status': 'active', 'admin_user_id': 'admin-1', 'admin_active': True,
                'application_ready': True, 'oidc_client_id': 'oidc-1'}
        hooks = install({'SaaSSubscription': SaaSSubscription,
            'SaaSIntegrationConfig': SaaSIntegrationConfig, 'SaaSProductVersion': SaaSProductVersion,
            'SaaSUserMapping': SaaSUserMapping, 'OIDCClient': OIDCClient, 'saas_call': call})
        change = SimpleNamespace(id='chg-wrong-version', target_version_id='target', old_version_id='old',
            target_version_code='v2', old_version_code='v1', evidence_json='', resource_plan_json=json.dumps({
                'change_id': 'chg-wrong-version', 'kind': 'saas', 'subscription_id': 'sub-1',
                'tenant_id': 'tenant-1', 'product_id': 'prod-1', 'enterprise_id': 'ent-1',
                'config_id': 'cfg-1', 'admin_user_id': 'admin-1',
                'external_admin_id': 'external-admin', 'oidc_client_id': 'oidc-1',
                'config_fingerprint': self.saas_config_fingerprint(config), 'expires_at': None}))
        db = FakeDB({(SaaSSubscription, 'sub-1'): sub, (SaaSIntegrationConfig, 'cfg-1'): config,
            (SaaSProductVersion, 'target'): version,
            (SaaSUserMapping, 'mapping'): SimpleNamespace(user_id='admin-1', external_user_id='external-admin'),
            (OIDCClient, 'oidc'): SimpleNamespace(client_id='oidc-1')})
        adapter_module = sys.modules['app.version_change_resources']
        original_select = adapter_module.select
        adapter_module.select = lambda model: Query(model)
        try:
            with self.assertRaises(Exception):
                hooks['apply'](db, change, 'target')
        finally:
            adapter_module.select = original_select
        self.assertEqual(sub.version_id, 'old')
        self.assertEqual(change.evidence_json, '')

    def test_api_route_write_failure_does_not_issue_proof_and_counters_survive(self):
        class Route: pass
        class ApiCredential: pass
        class Version: pass
        class Product: pass
        class Config: version_id = FilterColumn()
        class Combination: product_id = object()

        route = SimpleNamespace(id='target-route', route_key='route-v2')
        secret = 'mk_existing_secret'
        key_hash = hashlib.sha256(secret.encode()).hexdigest()
        credential = SimpleNamespace(id='cred-1', status='active', route_id='old-route',
            apisix_consumer_name='consumer-1', key_hash=key_hash, secret_encrypted='')
        consumer = {'username': 'consumer-1', 'plugins': {'key-auth': {'key': secret},
            'market-gateway-quota': {'route_key': 'route-v1', 'daily_quota': 9}}}
        live_routes = {'route-v2': {'name': 'route-v2-payload'}}
        writes = []

        def admin(method, path, payload=None):
            writes.append((method, path))
            if method == 'GET' and path.startswith('/routes/'):
                return {**live_routes.get(path.rsplit('/', 1)[-1], {}),
                    'id': 'server-route-id', 'create_time': 123, 'update_time': 456, 'status': 1}
            if method == 'PUT' and path.startswith('/routes/'):
                live_routes[path.rsplit('/', 1)[-1]] = payload
                return {}
            if method == 'GET' and path.startswith('/consumers/'):
                return consumer
            if method == 'PUT' and path.startswith('/consumers/'):
                raise RuntimeError('simulated consumer write failure')
            raise AssertionError((method, path))

        redis = FakeRedis()
        prior_periods = [{'period_key': 'combo-1:3:0', 'start_at': 0, 'end_at': 9999999999,
            'route_key': 'route-v1', 'rate_limit_per_minute': 4, 'daily_quota': 9, 'monthly_quota': 30}]
        redis.hset('market:apisix:credential:consumer-1', mapping={
            'mode': 'subscription', 'quota_scope': 'combo-1', 'generation': 3,
            'subscription_periods': json.dumps(prior_periods), 'counter_total': 17})
        payload = {'name': 'route-v2-payload'}
        route.status, route.product_id, route.version = 'active', 'prod-1', 'v2'
        version = SimpleNamespace(id='target', product_id='prod-1', version_code='v2', status='active')
        product = SimpleNamespace(id='prod-1')
        config = SimpleNamespace(id='target-config', route_id='target-route', revision_id='rev-2', evidence_hash='e' * 64,
            health_method='GET', upstream_url='https://upstream', health_path='/health', timeout_ms=1000)
        route.validated_ip = '192.0.2.10'
        combo = SimpleNamespace(id='combo-1', product_id='prod-1', enterprise_id='ent-1')
        plan = {'change_id': 'chg-api', 'kind': 'api', 'old_route_id': 'old-route',
            'provider': {'target': {'status': 'skipped_unmanaged', 'provider_synced': False}},
            'target_route_id': 'target-route', 'old_route_key': 'route-v1',
            'old_config_id': 'old-config', 'target_config_id': 'target-config',
            'target_route_key': 'route-v2', 'old_config_revision_id': 'rev-1',
            'target_config_revision_id': 'rev-2', 'old_config_evidence_hash': 'a' * 64,
            'target_config_evidence_hash': 'e' * 64, 'old_validated_ip': '192.0.2.9',
            'target_validated_ip': '192.0.2.10', 'periods': {'original': prior_periods,
                'target': [dict(prior_periods[0], route_key='route-v2')]},
            'credentials': [{'id': 'cred-1', 'consumer': 'consumer-1', 'key_hash': key_hash,
                'key_prefix': 'mk_existing', 'status': 'active', 'old_route_id': 'old-route',
                'rate_limit_per_minute': 4, 'daily_quota': 9, 'monthly_quota': 30, 'total_quota': 0}]}
        change = SimpleNamespace(id='chg-api', combination_id='combo-1', enterprise_id='ent-1',
            target_version_id='target', old_version_id='old', target_version_code='v2',
            old_version_code='v1', target_snapshots_json='{}', resource_plan_json=json.dumps(plan), evidence_json='')
        db = FakeDB({(Route, 'target-route'): route, (ApiCredential, 'cred-1'): credential,
            (Version, 'target'): version, (Product, 'prod-1'): product, (Config, 'config'): config,
            (Combination, 'combo-1'): combo})
        ns = {'ApiGatewayRoute': Route, 'ApiCredential': ApiCredential,
            'ProductReleaseVersion': Version, 'Product': Product,
            'version_gateway': {'Config': Config, 'version_ready': lambda *_args: True},
            'validated_http_request': lambda *_args, **_kwargs: SimpleNamespace(status_code=200,
                extensions={'validated_upstream_addresses': ['192.0.2.10']}),
            'apisix_route_payload': lambda *_args: payload,
            'apisix_admin_request': admin, 'apisix_enabled': lambda: True,
            'apisix_policy_redis': lambda: redis,
            'initial_api_credentials': {'cipher': lambda: None},
            'subscription_terms': {'Combination': Combination}, 'Order': object,
            'now': lambda: __import__('datetime').datetime.now(__import__('datetime').timezone.utc)}
        hooks = install(ns)
        class Statement:
            def __init__(self, entity): self.entity = entity
            @property
            def column_descriptions(self): return [{'entity': self.entity}]
            def where(self, *_args): return self
        adapter_module = sys.modules['app.version_change_resources']
        original_select = adapter_module.select
        adapter_module.select = lambda entity: Statement(entity)
        try:
            with self.assertRaises(Exception):
                hooks['apply'](db, change, 'target')
        finally:
            adapter_module.select = original_select
        self.assertEqual(live_routes['route-v2'], payload)
        self.assertEqual(redis.hgetall('market:apisix:credential:consumer-1')['counter_total'], '17')
        self.assertEqual(credential.route_id, 'old-route')
        self.assertEqual(change.evidence_json, '')
        self.assertNotIn(('PUT', '/routes/route-v2'), writes)
        self.assertIn(('PUT', '/consumers/consumer-1'), writes)

    def test_api_apply_replay_preserves_consumer_key_and_quota_identity(self):
        class Route: pass
        class ApiCredential: pass
        class Combination: product_id = object()
        class Version: pass
        class Product: pass
        class Config: version_id = FilterColumn()
        class Term:
            order_id = object()
            combination_id = object()
            generation = object()
            status = object()
            start_month = object()

        secret = 'mk_stable_existing_key'
        key_hash = hashlib.sha256(secret.encode()).hexdigest()
        route = SimpleNamespace(id='target-route', route_key='route-v2')
        credential = SimpleNamespace(id='cred-2', status='active', route_id='old-route',
            apisix_consumer_name='consumer-2', key_hash=key_hash, secret_encrypted='',
            daily_quota=12, monthly_quota=60, total_quota=0)
        route_payload = {'name': 'frozen-target-route'}
        route.status, route.product_id, route.version = 'active', 'prod-2', 'v2'
        version = SimpleNamespace(id='v2', product_id='prod-2', version_code='v2', status='active')
        product = SimpleNamespace(id='prod-2')
        config = SimpleNamespace(id='cfg', version_id='v2', route_id='target-route', revision_id='rev-2', evidence_hash='f' * 64,
            health_method='GET', upstream_url='https://upstream', health_path='/health', timeout_ms=1000)
        route.validated_ip = '192.0.2.20'
        consumer_store = {'username': 'consumer-2', 'plugins': {
            'key-auth': {'key': secret}, 'market-gateway-quota': {'route_key': 'route-v1', 'daily_quota': 12}}}
        routes = {'route-v2': route_payload}
        redis = FakeRedis()
        cache_key = 'market:apisix:credential:consumer-2'
        original_periods = [{'start_at': 0, 'end_at': 9999999999,
            'period_key': 'combo-2:7:0', 'route_key': 'route-v1',
            'rate_limit_per_minute': 5, 'daily_quota': 12, 'monthly_quota': 60}]
        redis.hset(cache_key, mapping={'mode': 'subscription', 'quota_scope': 'combo-2',
            'generation': 7, 'status': 'active', 'daily_quota': 12, 'monthly_quota': 60,
            'total_quota': 0, 'subscription_periods': json.dumps(original_periods), 'usage_total': 41})
        def admin(method, path, payload=None):
            if method == 'GET' and path.startswith('/routes/'):
                return {**routes.get(path.rsplit('/', 1)[-1], {}),
                    'id': 'server-route-id', 'create_time': 123, 'update_time': 456, 'status': 1}
            if method == 'PUT' and path.startswith('/routes/'):
                routes[path.rsplit('/', 1)[-1]] = payload
                return {}
            if method == 'GET' and path.startswith('/consumers/'):
                return consumer_store
            if method == 'PUT' and path.startswith('/consumers/'):
                consumer_store.clear()
                consumer_store.update({'username': payload['username'], 'plugins': payload['plugins']})
                return {}
            raise AssertionError((method, path))
        plan = {'change_id': 'chg-replay', 'kind': 'api', 'old_route_id': 'old-route',
            'provider': {side: {'status': 'skipped_unmanaged', 'provider_synced': False}
                for side in ('target', 'original')},
            'target_route_id': 'target-route', 'old_route_key': 'route-v1', 'target_route_key': 'route-v2',
            'old_config_id': 'old-cfg', 'target_config_id': 'cfg',
            'old_config_revision_id': 'rev-1', 'target_config_revision_id': 'rev-2',
            'old_config_evidence_hash': 'a' * 64, 'target_config_evidence_hash': 'f' * 64,
            'old_validated_ip': '192.0.2.19', 'target_validated_ip': '192.0.2.20',
            'periods': {'original': original_periods, 'target': [dict(original_periods[0],
                route_key='route-v2', daily_quota=22, monthly_quota=100)]},
            'credentials': [{
                'id': 'cred-2', 'consumer': 'consumer-2', 'key_hash': key_hash,
                'key_prefix': 'mk_stable', 'status': 'active', 'old_route_id': 'old-route',
                'rate_limit_per_minute': 5, 'daily_quota': 12, 'monthly_quota': 60, 'total_quota': 0}]}
        change = SimpleNamespace(id='chg-replay', combination_id='combo-2', enterprise_id='ent-2',
            target_version_id='v2', old_version_id='v1', target_version_code='v2', old_version_code='v1',
            target_snapshots_json='{}', resource_plan_json=json.dumps(plan), evidence_json='')
        combo = SimpleNamespace(anchor_at=__import__('datetime').datetime(2026, 1, 1,
            tzinfo=__import__('datetime').timezone.utc), total_months=1, id='combo-2', generation=7,
            product_id='prod-2')
        old_route = SimpleNamespace(id='old-route', route_key='route-v1', status='active',
            product_id='prod-2', version='v1', validated_ip='192.0.2.19')
        old_version = SimpleNamespace(id='v1', product_id='prod-2', version_code='v1', status='active')
        old_config = SimpleNamespace(id='old-cfg', version_id='v1', route_id='old-route', revision_id='rev-1',
            evidence_hash='a' * 64, health_method='GET', upstream_url='https://upstream-old',
            health_path='/health', timeout_ms=1000)
        db = FakeDB({(Route, 'target-route'): route, (ApiCredential, 'cred-2'): credential,
            (Combination, 'combo-2'): combo, (Version, 'v2'): version,
            (Version, 'v1'): old_version, (Route, 'old-route'): old_route,
            (Product, 'prod-2'): product, (Config, 'cfg'): config, (Config, 'old-cfg'): old_config})
        ns = {'ApiGatewayRoute': Route, 'ApiCredential': ApiCredential,
            'ProductReleaseVersion': Version, 'Product': Product,
            'version_gateway': {'Config': Config, 'version_ready': lambda *_args: True},
            'validated_http_request': lambda _method, url, **_kwargs: SimpleNamespace(status_code=200,
                extensions={'validated_upstream_addresses': ['192.0.2.19' if 'upstream-old' in url
                    else '192.0.2.20']}),
            'apisix_route_payload': lambda route_arg, *_args: route_payload if route_arg.version == 'v2'
                else {'name': 'frozen-original-route'},
            'apisix_admin_request': admin, 'apisix_enabled': lambda: True,
            'apisix_policy_redis': lambda: redis,
            'subscription_calendar': {'CalendarAnchor': lambda _value: None},
            'subscription_terms': {'Term': Term, 'Combination': Combination},
            'Order': SimpleNamespace(id=object()),
            'now': lambda: __import__('datetime').datetime.now(__import__('datetime').timezone.utc)}
        routes['route-v1'] = {'name': 'frozen-original-route'}
        hooks = install(ns)
        class Statement:
            def __init__(self, entity): self.entity, self.filters = entity, []
            @property
            def column_descriptions(self): return [{'entity': self.entity}]
            def join(self, *_args): return self
            def where(self, *args): self.filters.extend(args); return self
            def order_by(self, *_args): return self
        adapter_module = sys.modules['app.version_change_resources']
        original_select = adapter_module.select
        adapter_module.select = lambda entity: Statement(entity)
        try:
            first = hooks['apply'](db, change, 'target')
            second = hooks['apply'](db, change, 'target')
            rollback_proof = hooks['apply'](db, change, 'original')
        finally:
            adapter_module.select = original_select
        self.assertEqual(first, second)
        self.assertEqual(consumer_store['plugins']['key-auth']['key'], secret)
        self.assertEqual(credential.apisix_consumer_name, 'consumer-2')
        policy = redis.hgetall(cache_key)
        self.assertEqual(policy['quota_scope'], 'combo-2')
        self.assertEqual(policy['generation'], '7')
        self.assertEqual(policy['usage_total'], '41')
        self.assertEqual(credential.route_id, 'old-route')
        self.assertEqual(credential.daily_quota, 12)
        self.assertEqual(credential.monthly_quota, 60)
        self.assertEqual(consumer_store['plugins']['market-gateway-quota']['route_key'], 'route-v1')
        self.assertEqual(redis.hgetall(cache_key)['daily_quota'], '12')
        self.assertEqual(rollback_proof['request_change_id'], 'chg-replay')
        self.assertEqual(redis.close_count, 3)
        prior_evidence = change.evidence_json
        redis.corrupt_next = ('subscription_periods', '[]')
        with self.assertRaises(Exception):
            hooks['apply'](db, change, 'target')
        self.assertEqual(change.evidence_json, prior_evidence)


if __name__ == '__main__':
    unittest.main()
