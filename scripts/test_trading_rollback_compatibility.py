import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import trading_rollback_compatibility as compatibility


class CompatibilityTests(unittest.TestCase):
    container = 'market-rollback-' + 'a' * 32

    def execute(self, failure=None, mode='none', new_detail_checks=False, seed=False):
        calls = []
        def command(args, **kwargs):
            calls.append((args, kwargs))
            if 'inspect' in args and 'image' not in args:
                output = [{'HostConfig': {'NetworkMode': mode}, 'Config': {
                    'Labels': {'market.isolated-rollback': 'true'}}, 'State': {'Running': True}}]
            elif 'image' in args:
                output = [{'Id': 'sha256:' + ('1' if args[-1] == 'new' else '2') * 64}]
            elif 'exec' in args:
                output = (b'net:[999999999999999]\n' if 'readlink' in args else
                          b' lo: 0 0\n' if args[-1] == '/proc/net/dev' else b'Iface Destination\n')
                return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b'')
            elif 'run' in args:
                self.assertTrue(kwargs['stdin'].read(20))
                if failure == 'run':
                    raise subprocess.CalledProcessError(1, args, stderr=b'sensitive response')
                extra = 2 if 'REHEARSAL_NEW_DETAIL_CHECKS=true' in args else 0
                output = {'protected_unchanged': True, 'http_get_checks': 7 + extra,
                          'shared_http_get_checks': 7, 'new_detail_capability_checks': extra,
                          **{key: 'a' * 64 for key in ('finance_read_sha256', 'provider_read_sha256',
                                                     'public_read_sha256', 'protected_sha256')}}
                if seed:
                    output.update(synthetic_fixture=True, original_unchanged=True,
                                  original_sha256='b' * 64, original_columns={'orders': ['id']})
                if failure == 'mismatch' and 'sha256:' + '2' * 64 in args:
                    output['protected_sha256'] = 'b' * 64
                if failure == 'incomplete':
                    output['http_get_checks'] = 0
                if failure == 'original_changed' and 'sha256:' + '2' * 64 in args:
                    output['original_sha256'] = 'c' * 64
            else:
                return subprocess.CompletedProcess(args, 0, stdout=b'', stderr=b'')
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(output).encode(), stderr=b'')
        with patch.object(compatibility.subprocess, 'run', side_effect=command):
            if failure or mode not in {'none', ''}:
                with self.assertRaises(subprocess.CalledProcessError if failure == 'run' else ValueError):
                    compatibility.run_images('old', 'new', ['nerdctl', '--namespace', 'k8s.io'], self.container,
                                              seed_isolated_fixtures=seed)
            else:
                report = compatibility.run_images('old', 'new', ['nerdctl', '--namespace', 'k8s.io'], self.container,
                                                  new_detail_checks=new_detail_checks, seed_isolated_fixtures=seed)
                self.assertTrue(report['cleanup_verified'])
                self.assertFalse(report['full_production_rollback'])
        return calls

    def test_isolated_immutable_images_and_cleanup(self):
        calls = self.execute()
        runs = [args for args, _ in calls if 'run' in args]
        self.assertEqual(len(runs), 2)
        for args in runs:
            self.assertIn('--network=container:' + self.container, args)
            self.assertIn('DATABASE_URL=' + compatibility.DATABASE_URL, args)
            self.assertIn('MESSAGE_CENTER_DISABLE_BACKGROUND=true', args)
            self.assertIn('--pull=never', args)
            self.assertNotIn('-p', args)
            self.assertNotIn('-v', args)
        self.assertIn('sha256:' + '1' * 64, runs[0])
        self.assertIn('sha256:' + '2' * 64, runs[1])
        self.assertEqual(sum('rm' in args for args, _ in calls), 2)
        self.assertEqual(sum('ps' in args for args, _ in calls), 2)

    def test_failed_probe_cleans_up(self):
        calls = self.execute(failure='run')
        self.assertIn('rm', calls[-2][0])
        self.assertIn('ps', calls[-1][0])

    def test_detail_checks_enabled_only_for_new_image(self):
        runs = [args for args, _ in self.execute(new_detail_checks=True) if 'run' in args]
        self.assertIn('REHEARSAL_NEW_DETAIL_CHECKS=true', runs[0])
        self.assertIn('REHEARSAL_NEW_DETAIL_CHECKS=false', runs[1])

    def test_shared_routes_do_not_require_old_image_detail_or_foreign_rows(self):
        calls = []
        def get(path, headers=None, expected=200):
            calls.append(path)
            if path.startswith('/api/enterprise-settlements/'):
                self.fail('old image lacks the new detail route')
            if expected != 200:
                return None
            if path == '/api/storefront/categories':
                return {'items': [{'id': 'public'}]}
            fields = compatibility.FINANCE_FIELDS if path == '/api/settlements' else compatibility.PROVIDER_FIELDS
            return {'items': [{key: 'own' if key == 'id' else 1 for key in fields}]}
        compatibility.read_checks(get, {}, {}, {'own'})
        self.assertEqual(len(calls), 7)
        with self.assertRaisesRegex(ValueError, 'isolated schema fixture'):
            compatibility.read_checks(get, {}, {}, {'own'}, new_detail_checks=True)

    def test_nonisolated_target_rejected_before_api_run(self):
        self.assertFalse(any('run' in args for args, _ in self.execute(mode='bridge')))

    def test_missing_nerdctl_network_mode_still_requires_kernel_checks(self):
        calls = self.execute(mode='')
        self.assertEqual(sum('exec' in args for args, _ in calls), 3)

    def test_changed_fingerprint_and_incomplete_probe_rejected(self):
        self.execute(failure='mismatch')
        self.execute(failure='incomplete')

    def test_finance_shape_rejects_empty_missing_boolean_nan(self):
        for value in ({'items': []}, {'items': [{}]},
                      {'items': [{'id': 'x', 'provider_share': True}]},
                      {'items': [{'id': 'x', 'provider_share': float('nan')}]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                compatibility.validate_rows(value, ('id', 'provider_share'))
        rows = compatibility.validate_rows({'items': [{'id': 'b', 'provider_share': 2},
                                                      {'id': 'a', 'provider_share': 1}]},
                                           ('id', 'provider_share'))
        self.assertEqual([row['id'] for row in rows], ['a', 'b'])

    def test_probe_refuses_nonisolated_database_environment(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://live.invalid/db'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'isolated probe environment'):
                compatibility.probe()

    def test_fixture_flag_new_only_with_safe_original_metadata(self):
        calls = self.execute(seed=True)
        runs = [args for args, _ in calls if 'run' in args][-2:]
        self.assertIn('REHEARSAL_SEED_ISOLATED_FIXTURES=true', runs[0])
        self.assertIn('REHEARSAL_SEED_ISOLATED_FIXTURES=false', runs[1])
        for args in runs:
            self.assertIn('REHEARSAL_FIXTURE_MODE=true', args)
            self.assertIn('REHEARSAL_ISOLATION_PROVEN=true', args)
        self.assertIn('REHEARSAL_ORIGINAL_COLUMNS={"orders": ["id"]}', runs[1])
        self.assertNotIn('password', ' '.join(runs[1]))

    def test_changed_original_seed_fingerprint_rejected(self):
        self.execute(failure='original_changed', seed=True)


class FixtureTests(unittest.TestCase):
    def environment(self):
        return {'DATABASE_URL': compatibility.DATABASE_URL, 'MESSAGE_CENTER_DISABLE_BACKGROUND': 'true',
                'REHEARSAL_ISOLATION_PROVEN': 'true', 'REHEARSAL_PROBE_SIDE': 'new',
                'REHEARSAL_FIXTURE_MODE': 'true'}

    def test_guard_requires_new_side_exact_database_and_proven_isolation(self):
        with patch.dict(os.environ, self.environment(), clear=True):
            compatibility.fixture_guard()
            for key in self.environment():
                with self.subTest(key=key), patch.dict(os.environ, {key: 'wrong'}):
                    with self.assertRaisesRegex(ValueError, 'guard rejected'):
                        compatibility.seed_fixtures(None)

    def test_real_orm_seed_and_collision_rejection(self):
        from sqlalchemy import create_engine, select, func
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from fastapi.testclient import TestClient
        with patch.dict(os.environ, {'DATABASE_URL': 'sqlite://', 'MESSAGE_CENTER_DISABLE_BACKGROUND': 'true'}), \
                patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'backend'), *sys.path]):
            from app import main as m
        engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
        self.addCleanup(engine.dispose)
        m.Base.metadata.create_all(engine)
        ns = SimpleNamespace(engine=engine, SessionLocal=sessionmaker(bind=engine, autoflush=False),
                             **{name: getattr(m, name) for name in ('User', 'Enterprise', 'Membership',
                                                                 'ProductCategory', 'Product', 'Order', 'Settlement')})
        with patch.dict(os.environ, self.environment(), clear=True):
            compatibility.seed_fixtures(ns)
            with ns.SessionLocal() as db:
                self.assertEqual(db.scalar(select(func.count()).select_from(m.User)), 3)
                rows = db.scalars(select(m.Settlement)).all()
                self.assertEqual({row.id for row in rows}, set(compatibility.FIXTURE_IDS['settlements']))
                for row in rows:
                    self.assertEqual(row.platform_fee + row.provider_share + row.service_share +
                                     row.expert_fee + row.channel_fee, row.profit_amount)
                self.assertTrue(all(row.payment_status == 'paid' and row.paid_amount == 100
                                    for row in db.scalars(select(m.Order))))
                finance_headers = {'Authorization': 'Bearer ' + m.issue_token(db.get(m.User, compatibility.FIXTURE_PREFIX + 'finance'))}
                provider_headers = {'Authorization': 'Bearer ' + m.issue_token(db.get(m.User, compatibility.FIXTURE_PREFIX + 'provider')),
                                    'X-Enterprise-Id': compatibility.FIXTURE_PREFIX + 'provider-co'}
            with patch.object(m, 'SessionLocal', ns.SessionLocal), patch.object(m, 'startup') as startup:
                client = TestClient(m.app)
                try:
                    def get(path, headers=None, expected=200):
                        response = client.get(path, headers=headers)
                        self.assertEqual(response.status_code, expected)
                        return response.json() if expected == 200 else None
                    finance, provider, public = compatibility.read_checks(
                        get, finance_headers, provider_headers, {compatibility.FIXTURE_PREFIX + 'settlement'},
                        compatibility.FIXTURE_PREFIX + 'foreign-settlement', new_detail_checks=True)
                    self.assertEqual(len(finance), 2)
                    self.assertEqual(len(provider), 1)
                    self.assertTrue(public['items'])
                    startup.assert_not_called()
                finally:
                    client.close()
            with self.assertRaisesRegex(ValueError, 'identity collision'):
                compatibility.seed_fixtures(ns)
            with ns.SessionLocal() as db:
                self.assertEqual(db.scalar(select(func.count()).select_from(m.User)), 3)

    def test_original_fingerprint_excludes_exact_ids_only_and_preserves_column_projection(self):
        from sqlalchemy import create_engine, MetaData, Table, Column, String, text
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(text("ATTACH DATABASE ':memory:' AS public"))
        tables = {}
        for name in compatibility.PROTECTED_TABLES:
            table = Table(name, MetaData(), Column('id', String, primary_key=True), Column('value', String), schema='public')
            table.create(engine)
            tables[name] = table
            with engine.begin() as connection:
                connection.execute(table.insert(), {'id': 'original', 'value': 'protected'})
        before = compatibility.protected_fingerprint(engine)
        columns = {name: entry['columns'] for name, entry in before.items()}
        with engine.begin() as connection:
            for name, table in tables.items():
                for identifier in compatibility.FIXTURE_IDS.get(name, ()):
                    connection.execute(table.insert(), {'id': identifier, 'value': 'synthetic'})
            connection.execute(text('ALTER TABLE public.products ADD COLUMN category_id VARCHAR(36)'))
        self.assertEqual(before, compatibility.protected_fingerprint(engine, exclude_fixtures=True, columns=columns))
        full = compatibility.protected_fingerprint(engine)
        self.assertNotEqual(full, before)
        with engine.begin() as connection:
            connection.execute(tables['orders'].insert(), {'id': compatibility.FIXTURE_PREFIX + 'not-owned', 'value': 'x'})
        self.assertNotEqual(before, compatibility.protected_fingerprint(engine, exclude_fixtures=True, columns=columns))


if __name__ == '__main__':
    unittest.main()
