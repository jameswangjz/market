"""Acceptance safety tests: local files and isolated SQLite; no live services."""

import ast
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, create_engine, delete, select
from sqlalchemy.orm import Session


SCRIPT = Path(__file__).parents[2] / 'scripts' / 'version_change_quote_acceptance.py'
spec = importlib.util.spec_from_file_location('version_change_quote_acceptance_safety', SCRIPT)
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


class ManifestSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'owned.json'
        self.ids = acceptance.fixture_ids('qa-vcq-012345abcdef')

    def test_ids_fit_production_columns_and_contain_no_tokens(self):
        values = [*self.ids['users'].values(), *self.ids['enterprises'], *self.ids['products'].values(),
                  *(v for group in self.ids['versions'].values() for v in group.values())]
        self.assertEqual(len(values), len(set(values)))
        self.assertTrue(all(len(value) <= 36 for value in values))
        self.assertNotIn('Token', json.dumps(self.ids))
        self.assertNotIn('secret', json.dumps(self.ids))

    def test_private_manifest_and_existing_file_refusal(self):
        acceptance.private_json(self.path, self.ids, create=True)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(acceptance.read_scope(self.path), self.ids)
        with self.assertRaises(FileExistsError):
            acceptance.private_json(self.path, {}, create=True)
        self.assertEqual(json.loads(self.path.read_text()), self.ids)

    def test_atomic_replace_is_private_and_does_not_save_credentials(self):
        acceptance.private_json(self.path, self.ids, create=True)
        updated = {**self.ids, 'database_ids': {'orders': [['owned-order']]}}
        acceptance.private_json(self.path, updated)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(acceptance.read_scope(self.path), self.ids)
        self.assertFalse(list(self.path.parent.glob('.vcq-*')))

    def test_browser_credentials_are_separate_and_private(self):
        acceptance.private_json(self.path, self.ids, create=True)
        browser = self.path.with_suffix('.browser.json')
        fixture = {'buyerAdminToken': 'fixture-token', 'expectedOrderNo': 'fixture-order',
                   'baseUrl': 'https://example.invalid', 'deploymentTag': 'fixture-tag'}
        acceptance.private_json(browser, fixture, create=True)
        self.assertEqual(stat.S_IMODE(browser.stat().st_mode), 0o600)
        self.assertEqual(json.loads(browser.read_text()), fixture)
        self.assertNotIn('fixture-token', self.path.read_text())

    def test_symlink_is_never_followed(self):
        target = self.path.parent / 'target.json'
        target.write_text('unchanged')
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            acceptance.private_json(self.path, self.ids, create=True)
        with self.assertRaises(acceptance.AcceptanceFailure):
            acceptance.private_json(self.path, self.ids)
        with self.assertRaises(acceptance.AcceptanceFailure):
            acceptance.read_scope(self.path)
        self.assertEqual(target.read_text(), 'unchanged')

    def test_changed_scope_rejected_before_database_import(self):
        for key, value in (('run_id', 'qa-vcq-not-valid'), ('products', {'api': 'existing-product'}),
                           ('users', {'buyer': 'existing-user'}), ('extraToken', 'private')):
            with self.subTest(key=key):
                changed = {**self.ids, key: value}
                acceptance.private_json(self.path, changed, create=not self.path.exists())
                with patch.object(acceptance, 'load_main') as load:
                    with self.assertRaises(acceptance.AcceptanceFailure):
                        acceptance.cleanup_saved(self.path)
                    load.assert_not_called()

    def test_broad_manifest_mode_rejected_before_database_import(self):
        acceptance.private_json(self.path, self.ids, create=True)
        self.path.chmod(0o644)
        with patch.object(acceptance, 'load_main') as load:
            with self.assertRaises(acceptance.AcceptanceFailure):
                acceptance.cleanup_saved(self.path)
            load.assert_not_called()

    def test_failed_replacement_preserves_previous_manifest(self):
        acceptance.private_json(self.path, self.ids, create=True)
        with patch.object(acceptance.os, 'replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                acceptance.private_json(self.path, {})
        self.assertEqual(acceptance.read_scope(self.path), self.ids)
        self.assertFalse(list(self.path.parent.glob('.vcq-*')))

    def test_cleanup_failure_keeps_browser_token_for_retry(self):
        acceptance.private_json(self.path, self.ids, create=True)
        browser = self.path.with_suffix('.browser.json')
        acceptance.private_json(browser, {'buyerAdminToken': 'fixture-token'}, create=True)
        with patch.object(acceptance, 'load_main', return_value=Mock()), \
             patch.object(acceptance, 'cleanup', side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):
                acceptance.cleanup_saved(self.path)
        self.assertTrue(browser.exists())

    def test_cleanup_removes_private_browser_artifact(self):
        acceptance.private_json(self.path, self.ids, create=True)
        browser = self.path.with_suffix('.browser.json')
        acceptance.private_json(browser, {'buyerAdminToken': 'fixture-token'}, create=True)
        with patch.object(acceptance, 'load_main', return_value=Mock()), \
             patch.object(acceptance, 'cleanup') as cleanup:
            result = acceptance.cleanup_saved(self.path)
        self.assertEqual(result['status'], 'passed')
        cleanup.assert_called_once()
        self.assertFalse(browser.exists())
        self.assertTrue(self.path.exists())


class ForeignKeyScopeTests(unittest.TestCase):
    def test_exact_children_composite_keys_and_no_parent_expansion(self):
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        metadata = MetaData()
        parents = Table('parents', metadata, Column('id', String, primary_key=True))
        orders = Table('orders', metadata, Column('id', String, primary_key=True),
                       Column('parent_id', ForeignKey('parents.id')))
        children = Table('children', metadata, Column('id', String, primary_key=True),
                         Column('order_id', ForeignKey('orders.id')))
        links = Table('links', metadata, Column('order_id', ForeignKey('orders.id'), primary_key=True),
                      Column('tag', Integer, primary_key=True))
        metadata.create_all(engine)
        with Session(engine) as db:
            db.execute(parents.insert(), [{'id': 'owned'}, {'id': 'existing'}])
            db.execute(orders.insert(), [{'id': 'own-order', 'parent_id': 'owned'},
                                        {'id': 'existing-order', 'parent_id': 'existing'}])
            db.execute(children.insert(), [{'id': 'own-child', 'order_id': 'own-order'},
                                          {'id': 'existing-child', 'order_id': 'existing-order'}])
            db.execute(links.insert(), [{'order_id': 'own-order', 'tag': 1},
                                       {'order_id': 'existing-order', 'tag': 1}])
            db.commit()
            scoped = acceptance.expand_fk_ids(db, metadata, {'parents': {('owned',)}})
            self.assertEqual(scoped, {'parents': {('owned',)}, 'orders': {('own-order',)},
                                     'children': {('own-child',)}, 'links': {('own-order', 1)}})
            from_child = acceptance.expand_fk_ids(db, metadata, {'children': {('own-child',)}})
            self.assertEqual(from_child, {'children': {('own-child',)}})
            self.assertEqual(acceptance.expand_fk_ids(db, metadata, {}), {})
            for table in reversed(metadata.sorted_tables):
                if table.name in scoped:
                    db.execute(delete(table).where(acceptance.row_predicate(table, scoped[table.name])))
            db.commit()
            self.assertEqual(db.scalars(select(parents.c.id)).all(), ['existing'])
            self.assertEqual(db.scalars(select(orders.c.id)).all(), ['existing-order'])
            self.assertEqual(db.scalars(select(children.c.id)).all(), ['existing-child'])
            self.assertEqual(db.execute(select(links)).all(), [('existing-order', 1)])


class LifecycleSafetyTests(unittest.TestCase):
    def fake_main(self):
        from datetime import datetime, timezone

        return SimpleNamespace(now=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc))

    def test_partial_prepare_failure_still_cleans_and_redacts(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'owned.json'
            with patch.object(acceptance, 'load_main', return_value=self.fake_main()), \
                 patch.object(acceptance, 'preflight'), \
                 patch.object(acceptance, 'controlled_clock', side_effect=lambda at: at), \
                 patch.object(acceptance, 'prepare', side_effect=RuntimeError('secret SQL parameters')), \
                 patch.object(acceptance, 'cleanup') as cleanup:
                result = acceptance.run(manifest)
            cleanup.assert_called_once()
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error_type'], 'RuntimeError')
            self.assertNotIn('secret', json.dumps(result))

    def test_collision_never_writes_manifest_or_cleans(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'owned.json'
            with patch.object(acceptance, 'load_main', return_value=self.fake_main()), \
                 patch.object(acceptance, 'preflight', side_effect=acceptance.AcceptanceFailure('collision')), \
                 patch.object(acceptance, 'cleanup') as cleanup:
                with self.assertRaises(acceptance.AcceptanceFailure):
                    acceptance.run(manifest)
            self.assertFalse(manifest.exists())
            cleanup.assert_not_called()

    def test_invalid_browser_url_never_loads_database(self):
        for url in ('file:///tmp/fixture', 'https://user:password@example.invalid',
                    'https://example.invalid/?token=private'):
            with self.subTest(url=url), patch.object(acceptance, 'load_main') as load:
                with self.assertRaises(acceptance.AcceptanceFailure):
                    acceptance.run(prepare_browser=True, base_url=url, deployment_tag='fixture')
                load.assert_not_called()

    def test_hidden_fields_are_checked_recursively(self):
        acceptance.assert_hidden({'segments': [{'net_difference': 75.02}], 'digest': 'fixture-hmac'})
        for key in ('cost', 'unit_cost', 'secret', 'upstream_url', 'settlement_rule'):
            with self.subTest(key=key), self.assertRaises(acceptance.AcceptanceFailure):
                acceptance.assert_hidden({'nested': [{'deeper': {key: 'fixture'}}]})

    def test_production_imports_are_lazy_and_no_lifespan_start(self):
        tree = ast.parse(SCRIPT.read_text())
        top_imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertFalse(any(isinstance(n, ast.ImportFrom) and n.module and n.module.startswith('app')
                             for n in top_imports))
        self.assertNotIn('create_all(', SCRIPT.read_text())
        self.assertNotIn('TestClient(m.app) as', SCRIPT.read_text())


class ProductionFixtureSafetyTests(unittest.TestCase):
    def test_matrix_cleanup_preserves_preexisting_rows_and_browser_is_live(self):
        from contextlib import ExitStack
        from datetime import timedelta
        import importlib
        from sqlalchemy import event
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from test_message_business_events import load_isolated_main

        m = load_isolated_main()  # Explicit sqlite:///:memory: isolated package.
        m.engine.dispose()
        m.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        m.SessionLocal = sessionmaker(bind=m.engine, autoflush=False)

        @event.listens_for(m.engine, 'connect')
        def foreign_keys(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')

        m.Base.metadata.create_all(m.engine)
        calendar = importlib.import_module(m.__package__ + '.subscription_calendar')

        def all_rows():
            with m.SessionLocal() as db:
                return {table.name: sorted((tuple(row) for row in db.execute(select(table))), key=repr)
                        for table in m.Base.metadata.tables.values()}

        try:
            with ExitStack() as guards:
                guards.enter_context(patch.object(m, 'apisix_enabled', return_value=True))
                for name in ('apisix_admin_request', 'publish_apisix_route', 'execute_saas_operation', 'Minio'):
                    guards.enter_context(patch.object(m, name, side_effect=AssertionError('External service forbidden')))
                guards.enter_context(patch.object(m.redis.Redis, 'from_url', side_effect=AssertionError('Redis forbidden')))
                with m.SessionLocal() as db:
                    db.add(m.Enterprise(id='existing-enterprise', name='Existing', credit_code='existing'))
                    db.add(m.User(id='existing-user', email='existing@example.invalid', name='Existing', password_hash='unused'))
                    db.flush()
                    db.add(m.Product(id='existing-product', enterprise_id='existing-enterprise',
                                     name='Existing', product_type='dataset', status='draft'))
                    db.flush()
                    db.add(m.ProductReleaseVersion(id='existing-version', product_id='existing-product',
                        version_code='existing', price=10, cost=1, status='active'))
                    db.commit()
                before = all_rows()
                ids = acceptance.fixture_ids()
                anchor = m.now() - timedelta(days=45)
                bounds = calendar.CalendarAnchor(anchor).term(3)
                current = bounds.start + (bounds.end - bounds.start) / 2
                with tempfile.TemporaryDirectory() as directory:
                    manifest = Path(directory) / 'owned.json'
                    acceptance.preflight(m, ids)
                    acceptance.private_json(manifest, ids, create=True)
                    orders, tokens = acceptance.prepare(m, ids, anchor, include_browser=True)
                    with patch.object(m, 'now', return_value=current):
                        result = acceptance.exercise(m, ids, orders, tokens, current)
                    self.assertTrue(result['readonly_business_rows'])
                    with m.SessionLocal() as db:
                        unexpected = m.Product(id='external-linked-product',
                            enterprise_id=ids['enterprises'][1], name='Not fixture owned',
                            product_type='dataset', status='draft')
                        db.add(unexpected)
                        db.commit()
                        with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'External business row'):
                            acceptance.owned_rows(m, db, ids)
                        db.delete(unexpected)
                        db.commit()
                        membership = m.Membership(user_id='existing-user',
                            enterprise_id=ids['enterprises'][0], role='member', status='active')
                        db.add(membership)
                        db.commit()
                        with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'External membership'):
                            acceptance.owned_rows(m, db, ids)
                        db.delete(membership)
                        db.commit()
                        order = db.get(m.Order, orders['api'][0])
                        original_version = order.product_version_id
                        order.product_version_id = ids['versions']['model_api']['source']
                        db.commit()
                        with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'External order linkage'):
                            acceptance.owned_rows(m, db, ids)
                        order.product_version_id = original_version
                        db.commit()
                    with m.SessionLocal() as db:
                        live = m.subscription_terms['current_entitlement'](db, ids['enterprises'][0],
                            ids['products']['browser_api'], at=m.now())
                        self.assertIsNotNone(live)
                    from fastapi.testclient import TestClient

                    browser_client = TestClient(m.app)
                    try:
                        response = browser_client.get(
                            f"/api/orders/{orders['browser_api'][0]}/version-change-options",
                            headers={'Authorization': 'Bearer ' + tokens['admin']})
                        self.assertEqual(response.status_code, 200)
                        acceptance.assert_hidden(response.json())
                    finally:
                        browser_client.close()
                    execute = Session.execute

                    def sqlite_execute(session, statement, *args, **kwargs):
                        if str(statement).startswith('SET LOCAL lock_timeout'):
                            return None
                        return execute(session, statement, *args, **kwargs)

                    with patch.object(Session, 'execute', sqlite_execute):
                        acceptance.cleanup(m, ids, manifest)
                    self.assertEqual(all_rows(), before)
        finally:
            m.engine.dispose()
            for name in list(sys.modules):
                if name == m.__package__ or name.startswith(m.__package__ + '.'):
                    del sys.modules[name]


if __name__ == '__main__':
    unittest.main()
