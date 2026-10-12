"""Independent runtime-runner safety tests, never connect to deployed services.

These test acceptance ownership, cleanup planning and output, not Luna's backend
implementation. The small FK schema is isolated SQLite with no app.main import.
"""
from contextlib import redirect_stdout
from io import StringIO
import json
import logging
from pathlib import Path
import stat
import sys
import tempfile
import secrets
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import api_auto_delivery_runtime_acceptance as acceptance


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.saved = acceptance.fixture_ids('qa-api-auto-012345abcdef')
        self.saved.update(consumers=[], quota_windows=[])

    def test_random_scopes_fit_database_roots_and_are_disjoint(self):
        first, second = acceptance.fixture_ids(), acceptance.fixture_ids()
        self.assertNotEqual(first['run_id'], second['run_id'])
        for values in (first['products'], first['versions'], first['enterprises'], list(first['users'].values())):
            self.assertTrue(all(len(value) <= 36 for value in values))
        acceptance.validate_manifest(first)

    def test_private_atomic_journal_and_existing_file_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scope.json'
            acceptance.write_manifest(path, self.saved, create=True)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                acceptance.write_manifest(path, {}, create=True)
            self.saved['cleaned'] = True
            acceptance.write_manifest(path, self.saved)
            self.assertEqual(acceptance.read_manifest(path), self.saved)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_symlink_and_public_manifest_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path, link = Path(directory) / 'scope.json', Path(directory) / 'link.json'
            acceptance.write_manifest(path, self.saved, create=True)
            link.symlink_to(path)
            with self.assertRaises(OSError):
                acceptance.read_manifest(link)
            with self.assertRaises(acceptance.AcceptanceFailure):
                acceptance.write_manifest(link, self.saved)
            path.chmod(0o644)
            with self.assertRaises(acceptance.AcceptanceFailure):
                acceptance.read_manifest(path)

    def test_changed_roots_refused_before_database_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scope.json'
            self.saved['products'][0] = 'production-product'
            acceptance.write_manifest(path, self.saved, create=True)
            with patch.object(acceptance, 'load_main') as load:
                with self.assertRaises(acceptance.AcceptanceFailure):
                    acceptance.run(SimpleNamespace(manifest=str(path), cleanup=True))
                load.assert_not_called()

    def test_preflight_failure_journal_cannot_be_used_for_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scope.json'
            acceptance.write_manifest(path, self.saved, create=True)
            with patch.object(acceptance, 'load_main') as load:
                with self.assertRaises(acceptance.AcceptanceFailure):
                    acceptance.run(SimpleNamespace(manifest=str(path), cleanup=True))
                load.assert_not_called()

    def test_unplanned_secret_and_external_consumer_name_refused(self):
        self.saved['api_key'] = 'PRIVATE'
        with self.assertRaises(acceptance.AcceptanceFailure):
            acceptance.validate_manifest(self.saved)
        self.saved.pop('api_key')
        self.saved['consumers'] = [{'id': 'a' * 32, 'name': 'production-consumer',
                                   'route': self.saved['routes'][0]}]
        with self.assertRaises(acceptance.AcceptanceFailure):
            acceptance.redis_keys(self.saved)

    def test_exact_counter_keys_cover_minute_and_beijing_day_rollover(self):
        epoch = 1767283199  # UTC 15:59:59, next second is Beijing midnight.
        self.saved['consumers'] = [{'id': 'a' * 32, 'name': 'market-consumer-' + 'a' * 32,
                                   'route': self.saved['routes'][0]}]
        self.saved['quota_windows'] = [{'scope': 'b' * 32, 'period': 'anchor:1%',
            'start': epoch, 'end': epoch + 2, 'product': self.saved['products'][0]}]
        keys = acceptance.redis_keys(self.saved)
        for value in range(epoch, epoch + 3):
            self.assertTrue(set(acceptance.quota_keys('b' * 32, 'anchor:1%', value)) <= set(keys))
        self.assertTrue(any('anchor%3A1%25' in key for key in keys))
        self.assertTrue(all(not any(c in key for c in '*?[]') for key in keys))
        self.assertIn('market:apisix:oauth:' + self.saved['products'][0], keys)
        self.assertEqual(sum(':minute:' in key for key in keys), 2)
        self.assertEqual(sum(':day:' in key for key in keys), 2)

    def test_unbounded_counter_window_refused(self):
        self.saved['quota_windows'] = [{'scope': 'b' * 32, 'period': 'anchor',
            'start': 0, 'end': 100000, 'product': self.saved['products'][0]}]
        with self.assertRaises(acceptance.AcceptanceFailure):
            acceptance.redis_keys(self.saved)

    def test_cli_never_prints_network_exception_with_secrets(self):
        output = StringIO()
        with patch.object(acceptance, 'run', side_effect=RuntimeError('api_key=PRIVATE secret=PRIVATE')):
            with redirect_stdout(output):
                status = acceptance.main(['--manifest', '/tmp/unused'])
        self.assertEqual(status, 1)
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['error_type'], 'RuntimeError')

    def test_cli_restores_logging_on_success_and_failure(self):
        previous = logging.root.manager.disable
        try:
            for level in (logging.NOTSET, logging.WARNING):
                for outcome in ({'status': 'passed'}, RuntimeError('private')):
                    logging.disable(level)
                    with patch.object(acceptance, 'run') as run:
                        if isinstance(outcome, Exception):
                            run.side_effect = outcome
                        else:
                            run.return_value = outcome
                        with redirect_stdout(StringIO()):
                            acceptance.main(['--manifest', '/tmp/unused'])
                    self.assertEqual(logging.root.manager.disable, level)
            logging.disable(logging.NOTSET)
            with self.assertLogs('acceptance-logging-restored', level='INFO'):
                logging.getLogger('acceptance-logging-restored').info('available after CLI')
        finally:
            logging.disable(previous)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import Column, ForeignKey, String, create_engine
        from sqlalchemy.orm import declarative_base, sessionmaker
        Base = declarative_base()

        def model(name, table, fields=(), fks=None):
            attributes = {'__tablename__': table, 'id': Column(String, primary_key=True)}
            attributes.update({name: Column(String) for name in fields})
            attributes.update({name: Column(String, ForeignKey(target)) for name, target in (fks or {}).items()})
            return type(name, (Base,), attributes)

        self.m = SimpleNamespace(Base=Base)
        self.m.User = model('User', 'users', ('name', 'email'))
        self.m.Enterprise = model('Enterprise', 'enterprises', ('name', 'credit_code'))
        self.m.Product = model('Product', 'products', ('name',), {'enterprise_id': 'enterprises.id'})
        self.m.ProductReleaseVersion = model('Version', 'versions', (), {'product_id': 'products.id'})
        self.m.Order = model('Order', 'orders', ('order_no',), {
            'product_id': 'products.id', 'product_version_id': 'versions.id', 'buyer_user_id': 'users.id',
            'buyer_enterprise_id': 'enterprises.id', 'provider_enterprise_id': 'enterprises.id'})
        self.m.ApiGatewayRoute = model('Route', 'routes', ('route_key',), {'product_id': 'products.id'})
        self.m.ApiCredential = model('Credential', 'credentials', ('order_id',), {
            'route_id': 'routes.id', 'enterprise_id': 'enterprises.id'})
        self.m.AuditLog = model('Audit', 'audit', ('target_id', 'actor', 'request_id'))
        self.m.PlatformNotification = model('Notification', 'notifications', ('target_id', 'target_type'))
        Message = model('Message', 'messages', ('target_id', 'target_type', 'tenant_id', 'category', 'event_key'))
        Recipient = model('Recipient', 'recipients', (), {'message_id': 'messages.id', 'user_id': 'users.id'})
        Outbox = model('Outbox', 'outbox', (), {'message_id': 'messages.id', 'user_id': 'users.id'})
        Expired = model('Expired', 'expired', ('message_id', 'user_id'))
        self.m.DeliveryTask = model('Task', 'tasks', (), {'order_id': 'orders.id'})
        self.m.OAuthClient = model('OAuth', 'oauth', ('client_id',), {'product_id': 'products.id'})
        Combo = model('Combo', 'combinations', ('version_id',), {
            'product_id': 'products.id', 'enterprise_id': 'enterprises.id'})
        Term = model('Term', 'terms', ('version_id',), {'order_id': 'orders.id', 'combination_id': 'combinations.id'})
        self.m.subscription_terms = {'Term': Term, 'Combination': Combo}
        self.Child = model('Child', 'order_children', (), {'order_id': 'orders.id', 'user_id': 'users.id',
            'enterprise_id': 'enterprises.id', 'product_id': 'products.id', 'version_id': 'versions.id'})
        self.m.message_center = {'Message': Message, 'Receipt': Recipient, 'Recipient': Recipient,
                                'Delivery': Outbox, 'Outbox': Outbox, 'ExpiredUrgent': Expired}
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.saved = acceptance.fixture_ids('qa-api-auto-012345abcdef')
        for uid in self.saved['users'].values():
            self.db.add(self.m.User(id=uid, name=uid, email=uid + '@example.invalid'))
        for eid in self.saved['enterprises']:
            self.db.add(self.m.Enterprise(id=eid, name=eid, credit_code=eid))
        pid, vid = self.saved['products'][0], self.saved['versions'][0]
        self.db.add(self.m.Product(id=pid, name=pid, enterprise_id=self.saved['enterprises'][1]))
        self.db.add(self.m.ProductReleaseVersion(id=vid, product_id=pid))
        self.order = self.m.Order(id='owned-order', order_no='QA-ORDER', product_id=pid,
            product_version_id=vid, buyer_user_id=self.saved['users']['buyer'],
            buyer_enterprise_id=self.saved['enterprises'][0], provider_enterprise_id=self.saved['enterprises'][1])
        self.db.add(self.order)
        self.db.commit()

    def scopes(self):
        return acceptance.owned_scopes(self.m, self.db, self.saved)

    def message(self, **changes):
        values = {'id': 'message', 'target_type': 'order', 'target_id': 'owned-order',
                  'tenant_id': self.saved['enterprises'][0], 'category': 'order',
                  'event_key': 'business:order:owned-order:confirm_payment:paid'}
        return self.m.message_center['Message'](**(values | changes))

    def test_owned_audit_message_recipient_outbox_and_fk_children_only(self):
        from sqlalchemy import select
        self.db.add_all([self.m.User(id='staff', name='Staff', email='staff@example.invalid'),
            self.Child(id='child', order_id=self.order.id),
            self.m.AuditLog(id='audit', target_id=self.order.id, actor='worker'),
            self.m.AuditLog(id='outside-audit', target_id='outside', actor='worker'),
            self.message(),
            self.m.message_center['Recipient'](id='recipient', message_id='message', user_id='staff'),
            self.m.message_center['Outbox'](id='outbox', message_id='message', user_id='staff'),
            self.m.message_center['ExpiredUrgent'](id='expired', message_id='message', user_id='staff'),
            self.m.message_center['ExpiredUrgent'](id='outside-expired', message_id='outside', user_id='staff')])
        self.db.commit()
        scopes = self.scopes()
        for table, expected in (('audit', {'audit'}), ('messages', {'message'}),
                                ('recipients', {'recipient'}), ('outbox', {'outbox'}), ('expired', {'expired'})):
            ids = set(self.db.scalars(select(self.m.Base.metadata.tables[table].c.id).where(scopes[table])))
            self.assertEqual(ids, expected)
        user_ids = set(self.db.scalars(select(self.m.User.id).where(scopes['users'])))
        self.assertNotIn('staff', user_ids)

    def test_protected_order_is_never_cleanup_authorized(self):
        self.order.order_no = acceptance.PROTECTED_ORDER
        self.db.commit()
        with self.assertRaises(acceptance.AcceptanceFailure):
            self.scopes()
        self.assertIsNotNone(self.db.get(self.m.Order, self.order.id))

    def test_external_order_linked_to_owned_enterprise_refuses_cleanup(self):
        self.db.add(self.m.Order(id='external-order', order_no='ORD-PRODUCTION', product_id='external',
            product_version_id='external-version', buyer_user_id='external-user',
            buyer_enterprise_id=self.saved['enterprises'][0], provider_enterprise_id='external-enterprise'))
        self.db.commit()
        with self.assertRaises(acceptance.AcceptanceFailure):
            self.scopes()

    def test_unplanned_product_and_wrong_credential_enterprise_refused(self):
        self.db.add(self.m.Product(id='external-product', name='External', enterprise_id=self.saved['enterprises'][1]))
        self.db.commit()
        with self.assertRaises(acceptance.AcceptanceFailure):
            self.scopes()
        self.db.delete(self.db.get(self.m.Product, 'external-product'))
        self.db.add(self.m.ApiGatewayRoute(id='route', product_id=self.saved['products'][0], route_key=self.saved['routes'][0]))
        self.db.add(self.m.ApiCredential(id='credential', route_id='route', enterprise_id='external', order_id=''))
        self.db.commit()
        with self.assertRaises(acceptance.AcceptanceFailure):
            self.scopes()

    def test_cleanup_deletes_only_owned_fk_rows_and_exact_external_names(self):
        from sqlalchemy import text
        from sqlalchemy.orm import Session, sessionmaker

        class CleanupSession(Session):
            def execute(self, statement, *args, **kwargs):
                if str(statement).startswith('SET LOCAL lock_timeout'):
                    return None  # PostgreSQL-only transaction setting, no ownership logic skipped.
                return super().execute(statement, *args, **kwargs)

        self.m.engine = self.engine
        self.m.SessionLocal = sessionmaker(bind=self.engine, class_=CleanupSession)
        cid, route_key = 'c' * 32, self.saved['routes'][0]
        name = 'market-consumer-' + cid
        self.saved.update(consumers=[{'id': cid, 'name': name, 'route': route_key}], quota_windows=[], authorized=True)
        self.saved['environment'] = acceptance.environment(self.m)
        self.db.add_all([self.m.ApiGatewayRoute(id='route', product_id=self.saved['products'][0], route_key=route_key),
            self.m.ApiCredential(id=cid, route_id='route', enterprise_id=self.saved['enterprises'][0], order_id=''),
            self.Child(id='child', order_id=self.order.id),
            self.m.User(id='staff', name='Staff', email='staff@example.invalid'),
            self.message(),
            self.m.message_center['Recipient'](id='recipient', message_id='message', user_id='staff'),
            self.m.message_center['Outbox'](id='outbox', message_id='message', user_id='staff'),
            self.m.AuditLog(id='owned-audit', target_id='child'),
            self.m.AuditLog(id='outside-audit', target_id='unowned')])
        self.db.commit()
        self.db.execute(text('PRAGMA foreign_keys=ON'))
        self.db.commit()
        objects = {'/consumers/' + name: {'username': name, 'plugins': {
            'market-gateway-quota': {'route_key': route_key}}},
            '/routes/' + route_key: {'labels': {'market_product_id': self.saved['products'][0]}},
            '/routes/production': {'labels': {'market_product_id': 'production'}}}
        deletes = []

        def admin(_m, method, resource, payload=None, absent=False):
            if method == 'DELETE':
                deletes.append(resource)
                objects.pop(resource, None)
            return objects.get(resource)

        class ExactRedis:
            def __init__(self):
                self.keys = set(acceptance.redis_keys(self_saved)) | {'market:apisix:credential:production'}
                self.deleted = []

            def delete(self, *keys):
                self.deleted.extend(keys)
                self.keys.difference_update(keys)

            def exists(self, key):
                return key in self.keys

            def close(self):
                pass

        self_saved = self.saved
        redis = ExactRedis()
        self.m.apisix_policy_redis = lambda: redis
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scope.json'
            acceptance.write_manifest(path, self.saved, create=True)
            with patch.object(acceptance, 'admin', side_effect=admin):
                acceptance.cleanup(self.m, self.saved, path)
            saved = acceptance.read_manifest(path)
            self.assertTrue(saved['cleaned'])
            self.assertIn('outbox', saved['database_ids'])
            self.assertNotIn('secret', path.read_text())
        self.db.expire_all()
        self.assertIsNone(self.db.get(self.m.Order, 'owned-order'))
        self.assertIsNone(self.db.get(self.Child, 'child'))
        self.assertIsNone(self.db.get(self.m.message_center['Outbox'], 'outbox'))
        self.assertIsNotNone(self.db.get(self.m.User, 'staff'))
        self.assertIsNotNone(self.db.get(self.m.AuditLog, 'outside-audit'))
        self.assertEqual(deletes, ['/consumers/' + name, '/routes/' + route_key])
        self.assertIn('/routes/production', objects)
        self.assertEqual(set(redis.deleted), set(acceptance.redis_keys(self.saved)))
        self.assertEqual(redis.keys, {'market:apisix:credential:production'})

    def test_collision_preflight_cannot_authorize_cleanup(self):
        self.m.SessionLocal = lambda: self.db
        with patch.object(acceptance, 'admin') as admin:
            with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'collision'):
                acceptance.preflight(self.m, self.saved)
            admin.assert_not_called()

    def test_every_child_root_fk_is_validated(self):
        for column in ('user_id', 'enterprise_id', 'product_id', 'version_id'):
            with self.subTest(column=column):
                row = self.Child(id='unexpected-child', order_id='owned-order', **{column: 'production-root'})
                self.db.add(row)
                self.db.commit()
                with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'unowned fixture root'):
                    self.scopes()
                self.db.delete(row)
                self.db.commit()

    def test_recipient_link_to_unowned_message_is_rejected_even_for_fixture_user(self):
        self.db.add(self.m.message_center['Receipt'](id='unexpected-receipt',
            message_id='production-message', user_id=self.saved['users']['buyer']))
        self.db.commit()
        with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'unowned message'):
            self.scopes()

    def test_fixture_message_with_unrecognized_event_is_rejected(self):
        self.db.add(self.message(event_key='unexpected-event'))
        self.db.commit()
        with self.assertRaises(acceptance.AcceptanceFailure):
            self.scopes()

    def test_grant_message_requires_exact_owned_term_order_combo_and_version(self):
        from sqlalchemy import select
        Term, Combo = self.m.subscription_terms['Term'], self.m.subscription_terms['Combination']
        combo = Combo(id='b' * 32, product_id=self.saved['products'][0],
                      enterprise_id=self.saved['enterprises'][0], version_id=self.saved['versions'][0])
        term = Term(id='a' * 32, order_id='owned-order', combination_id=combo.id, version_id=self.saved['versions'][0])
        message = self.message(event_key='subscription_term_granted:' + term.id)
        self.db.add_all([combo, term, message])
        self.db.commit()
        scopes = self.scopes()
        self.assertEqual(list(self.db.scalars(select(Term.id).where(scopes['terms']))), [term.id])
        for field, bad_value in (('event_key', 'subscription_term_granted:' + 'c' * 32),
                                 ('event_key', 'subscription_term_granted:' + term.id + ':suffix'),
                                 ('tenant_id', 'production-tenant')):
            original = getattr(message, field)
            setattr(message, field, bad_value)
            self.db.commit()
            with self.assertRaises(acceptance.AcceptanceFailure):
                self.scopes()
            setattr(message, field, original)
            self.db.commit()
        term.version_id = 'production-version'
        self.db.commit()
        with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'combination proof'):
            self.scopes()

    def test_consumer_journal_after_flush_has_generated_id_before_sync(self):
        from sqlalchemy import ColumnDefault, event
        self.m.ApiCredential.__table__.c.id.default = ColumnDefault(lambda: secrets.token_hex(16))
        self.db.add(self.m.ApiGatewayRoute(id='owned-route', product_id=self.saved['products'][0],
                                          route_key=self.saved['routes'][0]))
        self.db.commit()
        self.saved.update(consumers=[], quota_windows=[])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scope.json'
            acceptance.write_manifest(path, self.saved, create=True)
            journal = acceptance.consumer_journal(self.m, self.saved, path)
            event.listen(self.db, 'after_flush', journal)
            try:
                row = self.m.ApiCredential(route_id='owned-route', enterprise_id=self.saved['enterprises'][0], order_id='')
                self.db.add(row)
                self.assertIsNone(row.id)
                self.db.flush()
                # The production sync call follows flush; the journal must already exist here.
                saved = acceptance.read_manifest(path)
                self.assertEqual(len(row.id), 32)
                self.assertEqual(saved['consumers'], [{'id': row.id, 'name': 'market-consumer-' + row.id,
                                                      'route': self.saved['routes'][0]}])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertNotIn('api_key', path.read_text())
                self.db.flush()
                self.assertEqual(len(acceptance.read_manifest(path)['consumers']), 1)
            finally:
                event.remove(self.db, 'after_flush', journal)

    def empty_preflight_environment(self):
        from sqlalchemy.orm import sessionmaker
        for table in reversed(self.m.Base.metadata.sorted_tables):
            self.db.execute(table.delete())
        self.db.commit()
        self.m.SessionLocal = sessionmaker(bind=self.engine)
        self.m.application_address_policy = {'allow_http': Mock(return_value=True), 'validate_url': Mock()}
        self.m.validated_http_request = Mock(return_value=SimpleNamespace(status_code=200,
            json=lambda: {'status': 'ok', 'service': 'market-mock-api'},
            extensions={'validated_upstream_addresses': ['10.0.0.2'], 'validated_upstream_ip': '10.0.0.2'}))
        self.m.apisix_policy_redis = Mock(return_value=Mock(exists=Mock(return_value=False)))

    def test_preflight_refuses_disabled_http_policy_without_probe_or_mutation(self):
        self.empty_preflight_environment()
        self.m.application_address_policy['allow_http'].return_value = False
        with patch.object(acceptance, 'admin') as admin:
            with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'must allow HTTP'):
                acceptance.preflight(self.m, self.saved)
            admin.assert_not_called()
        self.m.validated_http_request.assert_not_called()
        self.m.application_address_policy['validate_url'].assert_not_called()

    def test_preflight_checks_global_unique_values(self):
        for model_name in ('User', 'Enterprise', 'OAuthClient'):
            with self.subTest(model=model_name):
                self.empty_preflight_environment()
                model = getattr(self.m, model_name)
                values = {'User': {'email': self.saved['users']['buyer'] + '@example.invalid'},
                          'Enterprise': {'credit_code': self.saved['enterprises'][0]},
                          'OAuthClient': {'client_id': self.saved['run_id'] + '-oauth-0'}}[model_name]
                self.db.add(model(id='production-row', **values))
                self.db.commit()
                with patch.object(acceptance, 'admin') as admin:
                    with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'collision'):
                        acceptance.preflight(self.m, self.saved)
                    admin.assert_not_called()
                self.m.validated_http_request.assert_not_called()

    def test_preflight_uses_safe_adapter_and_requires_mock_identity_and_pinning(self):
        self.empty_preflight_environment()
        with patch.object(acceptance, 'admin', return_value=None):
            acceptance.preflight(self.m, self.saved)
        self.m.validated_http_request.assert_called_once_with('GET', acceptance.MOCK_URL + '/health',
                                                             timeout=5, follow_redirects=False)
        self.m.validated_http_request.return_value.extensions = {}
        with patch.object(acceptance, 'admin') as admin:
            with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'destination evidence'):
                acceptance.preflight(self.m, self.saved)
            admin.assert_not_called()


if __name__ == '__main__':
    unittest.main()
