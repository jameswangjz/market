"""Isolated mock identity regression tests; no application lifespan or services."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


STORE_PATH = Path(__file__).resolve().parents[2] / 'mock-saas/app/store.py'
spec = importlib.util.spec_from_file_location('mock_saas_identity_store', STORE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MockExternalIdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = module.Store(Path(self.directory.name) / 'state.sqlite3')
        self.token = {'active': True, 'product_id': 'owned-product',
                      'scope': 'saas.tenant saas.user saas.department'}
        for tenant in ('tenant-one', 'tenant-two'):
            self.call('OPEN', tenant=tenant, product_id='owned-product',
                      enterprise_id='enterprise-' + tenant, subscription_id='subscription-' + tenant)

    def call(self, operation, tenant='tenant-one', **fields):
        return self.store.machine_operation({'type': operation, 'tenant_id': tenant, **fields}, self.token)

    def cases(self):
        return [('USER_ASSIGN', 'USER_UNASSIGN', 'users', 'user_id', 'external_user_id', 'mock-user-'),
                ('DEPT_CREATE', 'DEPT_REMOVE', 'departments', 'department_id', 'external_department_id', 'mock-dept-')]

    def stored(self, table, field, platform, tenant='tenant-one'):
        with self.store.connect() as db:
            row = db.execute(f'SELECT payload FROM {table} WHERE tenant_id=? AND {field}=?',
                             (tenant, platform)).fetchone()
            return json.loads(row['payload'])

    def test_common_prefix_and_full_tenant_identity_are_distinct(self):
        for assign, _, table, field, external_field, prefix in self.cases():
            with self.subTest(kind=table):
                values = []
                for tenant in ('tenant-one', 'tenant-two'):
                    for platform in ('qa-common-prefix-buyer', 'qa-common-prefix-member'):
                        response = self.call(assign, tenant, **{field: platform})
                        external = self.stored(table, field, platform, tenant)[external_field]
                        expected = prefix + hashlib.sha256(json.dumps(
                            [tenant, platform], separators=(',', ':')).encode()).hexdigest()
                        self.assertEqual(external, expected)
                        self.assertEqual(response[field], external)
                        self.assertLessEqual(len(external), 180)
                        values.append(external)
                self.assertEqual(len(set(values)), 4)

    def test_reassign_restart_and_idempotency_replay_keep_identity(self):
        for assign, _, table, field, external_field, _ in self.cases():
            with self.subTest(kind=table):
                platform = 'qa-common-prefix-replay'
                fields = {field: platform, 'idempotency_key': table + '-assign'}
                first = self.call(assign, **fields)
                before = self.stored(table, field, platform)
                self.store = module.Store(self.store.path)
                self.assertEqual(self.call(assign, **fields), first)
                self.assertEqual(self.stored(table, field, platform), before)
                changed = self.call(assign, **{field: platform, 'name': 'updated'})
                self.assertEqual(changed[field], before[external_field])
                with self.assertRaises(module.MachineOperationError) as caught:
                    self.call(assign, **{**fields, 'name': 'changed replay'})
                self.assertEqual(caught.exception.status, 409)

    def test_hash_input_is_framed_not_concatenated(self):
        for tenant in ('ab', 'a'):
            self.call('OPEN', tenant, product_id='owned-product',
                      enterprise_id='enterprise-' + tenant, subscription_id='subscription-' + tenant)
        for assign, _, _, field, _, _ in self.cases():
            with self.subTest(kind=assign):
                first = self.call(assign, 'ab', **{field: 'c'})[field]
                second = self.call(assign, 'a', **{field: 'bc'})[field]
                self.assertNotEqual(first, second)

    def test_external_removal_does_not_remove_common_prefix_peer(self):
        for assign, remove, table, field, external_field, _ in self.cases():
            with self.subTest(kind=table):
                ids = ['qa-common-prefix-primary', 'qa-common-prefix-member']
                for platform in ids:
                    self.call(assign, **{field: platform})
                external = self.stored(table, field, ids[1])[external_field]
                self.call(remove, **{field: external})
                self.assertEqual(self.stored(table, field, ids[0])['status'], 'active')
                removed = self.stored(table, field, ids[1])
                self.assertEqual(removed['status'], 'deleted')
                self.call(remove, **{field: external})
                self.assertEqual(self.stored(table, field, ids[1]), removed)
                self.call(assign, **{field: ids[1]})
                self.assertEqual(self.stored(table, field, ids[1])[external_field], external)

    def test_legacy_ids_and_saas_app1_custom_ids_never_rotate(self):
        for assign, remove, table, field, external_field, prefix in self.cases():
            for legacy in (prefix + 'qa-common-pr', 'saas-app1-existing-' + table):
                with self.subTest(kind=table, legacy=legacy):
                    platform = 'legacy-' + legacy
                    with self.store.connect() as db:
                        db.execute(f'INSERT INTO {table} VALUES (?, ?, ?)', ('tenant-one', platform,
                            json.dumps({field: platform, external_field: legacy, 'status': 'active'})))
                    self.assertEqual(self.call(assign, **{field: platform})[field], legacy)
                    self.call(remove, **{field: legacy})
                    self.assertEqual(self.call(assign, **{field: platform})[field], legacy)

    def test_legacy_department_missing_external_field_keeps_old_identity(self):
        platform = 'qa-common-prefix-deleted'
        with self.store.connect() as db:
            db.execute('INSERT INTO departments VALUES (?, ?, ?)', ('tenant-one', platform,
                json.dumps({'department_id': platform, 'status': 'deleted'})))
        self.assertEqual(self.call('DEPT_CREATE', department_id=platform)['department_id'],
                         'mock-dept-' + platform[:12])

    def test_explicit_identity_is_validated_and_immutable(self):
        for assign, _, table, field, external_field, _ in self.cases():
            with self.subTest(kind=table):
                platform = 'explicit-' + table
                explicit = 'existing-external-' + table
                self.assertEqual(self.call(assign, **{field: platform, external_field: explicit})[field], explicit)
                self.assertEqual(self.call(assign, **{field: platform, external_field: explicit})[field], explicit)
                before = self.stored(table, field, platform)
                with self.assertRaises(module.MachineOperationError) as caught:
                    self.call(assign, **{field: platform, external_field: 'replacement'})
                self.assertEqual(caught.exception.status, 409)
                self.assertEqual(self.stored(table, field, platform), before)
                for invalid in ('', ' ', ' padded ', None, 1, [], 'x' * 181):
                    with self.subTest(invalid=type(invalid).__name__):
                        with self.assertRaises(module.MachineOperationError) as caught:
                            self.call(assign, **{field: 'new-' + table, external_field: invalid})
                        self.assertEqual(caught.exception.status, 400)
                        with self.store.connect() as db:
                            self.assertIsNone(db.execute(f'SELECT 1 FROM {table} WHERE {field}=?',
                                                       ('new-' + table,)).fetchone())

    def test_explicit_identity_cannot_alias_another_platform_or_external_id(self):
        for assign, _, table, field, external_field, _ in self.cases():
            with self.subTest(kind=table):
                self.call(assign, **{field: 'owner', external_field: 'reserved'})
                for alias in ('owner', 'reserved'):
                    with self.assertRaises(module.MachineOperationError) as caught:
                        self.call(assign, **{field: 'intruder', external_field: alias})
                    self.assertEqual(caught.exception.status, 409)
                with self.assertRaises(module.MachineOperationError) as caught:
                    self.call(assign, **{field: 'reserved'})
                self.assertEqual(caught.exception.status, 409)
                self.assertEqual(self.call(assign, 'tenant-two', **{field: 'peer', external_field: 'reserved'})[field], 'reserved')

    def test_legacy_ambiguous_alias_removal_fails_without_mutation(self):
        for _, remove, table, field, external_field, prefix in self.cases():
            with self.subTest(kind=table):
                alias = prefix + 'qa-common-pr'
                with self.store.connect() as db:
                    for platform in ('qa-common-prefix-first', 'qa-common-prefix-second'):
                        db.execute(f'INSERT INTO {table} VALUES (?, ?, ?)', ('tenant-one', platform,
                            json.dumps({field: platform, external_field: alias, 'status': 'active'})))
                assign = 'USER_ASSIGN' if table == 'users' else 'DEPT_CREATE'
                for platform in ('qa-common-prefix-first', 'qa-common-prefix-second'):
                    self.assertEqual(self.call(assign, **{field: platform, external_field: alias})[field], alias)
                with self.assertRaises(module.MachineOperationError) as caught:
                    self.call(remove, **{field: alias})
                self.assertEqual(caught.exception.status, 409)
                for platform in ('qa-common-prefix-first', 'qa-common-prefix-second'):
                    self.assertEqual(self.stored(table, field, platform)['status'], 'active')
                self.call(remove, **{field: 'qa-common-prefix-second'})
                self.assertEqual(self.stored(table, field, 'qa-common-prefix-first')['status'], 'active')


if __name__ == '__main__':
    unittest.main()
