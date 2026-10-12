"""Independent validation harness guards; no production import, PG, or HTTP."""

import json
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import unified_delivery_access_acceptance as acceptance


class ManifestGuards(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'owned.json'
        self.ids = acceptance.fixture_ids('qa-uda-012345abcdef')
        acceptance.write_scope(self.path, self.ids, 'allocated', create=True)

    def test_private_lifecycle_and_collision(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(acceptance.read_scope(self.path), (self.ids, 'allocated'))
        with self.assertRaises(FileExistsError):
            acceptance.write_scope(self.path, self.ids, 'allocated', create=True)
        acceptance.write_scope(self.path, self.ids, 'prepared', database_ids={'orders': {('owned',)}})
        self.assertEqual(acceptance.read_scope(self.path), (self.ids, 'prepared'))
        acceptance.write_scope(self.path, self.ids, 'cleaned')
        with patch.object(acceptance, 'load_main') as load:
            with self.assertRaises(acceptance.AcceptanceFailure):
                acceptance.run('test', self.path, 'http://localhost:30080')
            load.assert_not_called()

    def test_tampering_and_protected_scope_refused_before_database_import(self):
        for replacement in ('real-user', acceptance.PROTECTED[0], acceptance.PROTECTED[1]):
            value = {**self.ids, 'users': {**self.ids['users'], 'buyer': replacement},
                     'schema': 1, 'phase': 'prepared'}
            acceptance.guarded.private_json(self.path, value)
            with patch.object(acceptance, 'load_main') as load:
                with self.assertRaises(acceptance.AcceptanceFailure):
                    acceptance.run('cleanup', self.path)
                load.assert_not_called()

    def test_symlink_and_public_manifest_refused(self):
        link = self.path.with_name('link.json')
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            acceptance.read_scope(link)
        with self.assertRaises(acceptance.AcceptanceFailure):
            acceptance.write_scope(link, self.ids, 'prepared')
        self.path.chmod(0o644)
        with self.assertRaises(acceptance.AcceptanceFailure):
            acceptance.read_scope(self.path)

    def test_private_failure_output_does_not_print_raw_errors(self):
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with patch('sys.argv', ['acceptance', '--mode', 'cleanup', '--manifest', str(self.path)]), \
                patch.object(acceptance, 'run', side_effect=RuntimeError('password=must-not-print')), \
                redirect_stdout(output):
            self.assertEqual(acceptance.main(), 1)
        self.assertNotIn('must-not-print', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['error_type'], 'RuntimeError')


class ContractGuards(unittest.TestCase):
    def test_origin_is_explicit_nodeport_not_platform_proxy(self):
        self.assertEqual(acceptance.validate_url('http://192.168.10.10:30082/'),
                         'http://192.168.10.10:30082')
        for url in ('https://user:pass@host', 'https://host/gateway', 'https://host?key=x',
                    'https://host#frag', 'https://host:0', 'https://host\\bad', 'https://host\x7f'):
            with self.subTest(url=url), self.assertRaises((acceptance.AcceptanceFailure, ValueError)):
                acceptance.validate_url(url)

    def test_nested_fields_and_persisted_sensitive_values_are_hidden(self):
        acceptance.assert_public({'api': {'gateway_url': 'http://192.168.10.10:30082/gateway/slug',
            'limits': {'quota_amount': 789}, 'credentials_path': '/api/orders/owned/api-credentials'}})
        for key in ('upstream_url', 'cost', 'key_hash', 'api_key', 'secret', 'apisix_consumer_name'):
            with self.subTest(key=key), self.assertRaises(acceptance.AcceptanceFailure):
                acceptance.assert_public({'nested': [{key: 'x'}]})
        with self.assertRaises(acceptance.AcceptanceFailure):
            acceptance.assert_public({'reason': 'unexpected persisted-hash'}, ['persisted-hash'])

    def test_snapshot_construction_has_valid_economics_without_app_import(self):
        from decimal import Decimal
        ids = acceptance.fixture_ids('qa-uda-012345abcdef')
        m = SimpleNamespace(Order=lambda **values: SimpleNamespace(**values))
        order = acceptance.order_fixture(m, ids, 'api', 'source')
        economic = json.loads(order.economic_snapshot_json)
        self.assertEqual(set(economic['settlement_rule']['rates']),
            {'platform_rate', 'provider_rate', 'service_rate', 'expert_rate', 'channel_rate'})
        self.assertEqual(Decimal(economic['unit_price']), order.amount)
        self.assertEqual(order.order_no, ids['orders']['api']['source'])
        self.assertNotIn(acceptance.PROTECTED[0], repr(order))


class CleanupLinkageGuards(unittest.TestCase):
    """Exercise the added FK adoption check with an isolated in-memory schema."""

    def test_foreign_user_linkage_and_unplanned_orders_are_refused(self):
        from sqlalchemy import Column, ForeignKey, MetaData, String, Table, create_engine
        from sqlalchemy.orm import Session

        metadata = MetaData()
        def model(name, *columns):
            return SimpleNamespace(__table__=Table(name, metadata, Column('id', String, primary_key=True), *columns))
        user = model('users')
        enterprise = model('enterprises')
        product = model('products', Column('status', String))
        order = model('orders', Column('order_no', String), Column('buyer_user_id', ForeignKey('users.id')))
        file = model('files')
        route = model('routes')
        credential = model('credentials')
        m = SimpleNamespace(Base=SimpleNamespace(metadata=metadata), User=user, Enterprise=enterprise,
                            Product=product, Order=order, FileObject=file, ApiGatewayRoute=route, ApiCredential=credential)
        engine = create_engine('sqlite://')
        metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        ids = acceptance.fixture_ids('qa-uda-012345abcdef')
        oid = ids['orders']['api']['source']
        with Session(engine) as db:
            db.execute(order.__table__.insert().values(id=oid, order_no=oid, buyer_user_id='real-user'))
            with patch.object(acceptance.guarded, 'owned_rows', return_value={'orders': {(oid,)}}):
                with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'External root linkage'):
                    acceptance.owned_rows(m, db, ids)
            db.execute(order.__table__.update().values(buyer_user_id=ids['users']['buyer']))
            with patch.object(acceptance.guarded, 'owned_rows', return_value={'orders': {(oid,)}}):
                self.assertEqual(acceptance.owned_rows(m, db, ids), {'orders': {(oid,)}})
            db.execute(order.__table__.update().values(order_no='real-order-number'))
            with patch.object(acceptance.guarded, 'owned_rows', return_value={'orders': {(oid,)}}):
                with self.assertRaisesRegex(acceptance.AcceptanceFailure, 'Unplanned order'):
                    acceptance.owned_rows(m, db, ids)


class FixtureRestoreGuards(unittest.TestCase):
    def test_snapshot_excludes_only_reviewed_async_tables_without_changing_ownership(self):
        from sqlalchemy import Column, MetaData, String, Table, create_engine
        from sqlalchemy.orm import sessionmaker

        metadata = MetaData()
        preserved = {'orders', 'subscription_terms', 'api_gateway_routes', 'api_credentials', 'audit_logs',
                     'notification_messages', 'notification_expired_urgent', 'notification_preferences',
                     'notification_attachments', 'notification_message_attachments',
                     'notification_future_business_event'}
        self.assertEqual(acceptance.ASYNC_NOTIFICATION_SNAPSHOT_TABLES, {
            'notification_deliveries', 'notification_delivery_attempts', 'notification_recipients'})
        all_names = preserved | acceptance.ASYNC_NOTIFICATION_SNAPSHOT_TABLES
        for name in all_names:
            Table(name, metadata, Column('id', String, primary_key=True), Column('state', String))
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        owned = {name: {('owned',)} for name in all_names}
        with sessions() as db:
            for table in metadata.tables.values():
                db.execute(table.insert().values(id='owned', state='original'))
            db.commit()
        m = SimpleNamespace(Base=SimpleNamespace(metadata=metadata), SessionLocal=sessions)
        with patch.object(acceptance, 'owned_rows', return_value=owned):
            before = acceptance.snapshot(m, {})
            self.assertEqual(set(before), preserved)
            with sessions() as db:
                for name in acceptance.ASYNC_NOTIFICATION_SNAPSHOT_TABLES:
                    db.execute(metadata.tables[name].insert().values(id='async-owned', state='worker-change'))
                    owned[name].add(('async-owned',))
                db.commit()
            self.assertEqual(acceptance.snapshot(m, {}), before)
            self.assertEqual(set(owned), all_names)
            for name in acceptance.ASYNC_NOTIFICATION_SNAPSHOT_TABLES:
                self.assertIn(('async-owned',), owned[name])
            for name in preserved:
                with sessions() as db:
                    db.execute(metadata.tables[name].update().values(state='business-change'))
                    db.commit()
                self.assertNotEqual(acceptance.snapshot(m, {}), before)
                with sessions() as db:
                    db.execute(metadata.tables[name].update().values(state='original'))
                    db.commit()
            self.assertEqual(acceptance.snapshot(m, {}), before)

    def test_explicit_restore_preserves_equal_timestamp_and_restores_after_failure(self):
        from datetime import datetime, timedelta
        from sqlalchemy import Column, DateTime, String, create_engine, select, update
        from sqlalchemy.orm import declarative_base, sessionmaker

        original = datetime(2026, 10, 12)
        calls = []
        def automatic_stamp():
            calls.append(True)
            return original if len(calls) == 1 else original + timedelta(seconds=1)
        base = declarative_base()
        class Fixture(base):
            __tablename__ = 'fixture_restore'
            id = Column(String, primary_key=True)
            status = Column(String)
            updated_at = Column(DateTime, onupdate=automatic_stamp)
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            db.add(Fixture(id='owned', status='draft', updated_at=original))
            db.commit()
        # Reproduce the old ORM restore: unchanged timestamp is omitted, so the
        # second UPDATE generates a fresh timestamp even though we assigned it.
        with sessions() as db:
            row = db.get(Fixture, 'owned')
            row.status = 'blocked'
            db.commit()
        with sessions() as db:
            row = db.get(Fixture, 'owned')
            row.status, row.updated_at = 'draft', original
            db.commit()
            self.assertNotEqual(row.updated_at, original)
        with sessions() as db:
            db.execute(update(Fixture.__table__).values(status='draft', updated_at=original))
            db.commit()
        calls.clear()
        m = SimpleNamespace(Base=base, SessionLocal=sessions)
        with patch.object(acceptance, 'owned_rows', return_value={'fixture_restore': {('owned',)}}):
            before = acceptance.snapshot(m, {})
            acceptance.temporary_fixture_change(m, {}, Fixture, 'owned', {'status': 'blocked'}, lambda: None)
            self.assertEqual(acceptance.snapshot(m, {}), before)
            self.assertEqual(len(calls), 1)
            with self.assertRaisesRegex(ValueError, 'isolated action failure'):
                acceptance.temporary_fixture_change(m, {}, Fixture, 'owned', {'status': 'blocked'},
                    lambda: (_ for _ in ()).throw(ValueError('isolated action failure')))
            self.assertEqual(acceptance.snapshot(m, {}), before)
            with sessions() as db:
                self.assertEqual(db.scalar(select(Fixture.updated_at)), original)

    def test_snapshot_diagnostics_include_only_table_and_field_names(self):
        from sqlalchemy import Column, MetaData, String, Table
        metadata = MetaData()
        Table('owned_table', metadata, Column('id', String, primary_key=True), Column('secret_field', String))
        differences = acceptance.snapshot_differences(metadata,
            {'owned_table': [('private-row-id', 'private-original')]},
            {'owned_table': [('private-row-id', 'private-changed')]})
        self.assertEqual(differences, [{'table': 'owned_table', 'fields': ['secret_field']}])
        self.assertNotIn('private-', json.dumps(differences))


class BrowserFixtureGuards(unittest.TestCase):
    def test_export_mints_only_exact_owned_verified_buyer_and_keeps_token_private(self):
        from datetime import datetime, timezone
        from sqlalchemy import Column, MetaData, String, Table

        ids = acceptance.fixture_ids('qa-uda-012345abcdef')
        user = SimpleNamespace(id=ids['users']['buyer'], name=ids['users']['buyer'],
            email=ids['users']['buyer'] + '@example.invalid', email_verified=True,
            verified_status='verified', platform_role='')
        table = Table('credentials', MetaData(), Column('key_hash', String), Column('created_by', String))
        orders = {group['source']: SimpleNamespace(id=group['source'], order_no=group['source'],
            buyer_user_id=user.id, buyer_enterprise_id=ids['enterprises'][0], product_id=ids['products'][method])
            for method, group in ids['orders'].items()}
        db = SimpleNamespace(get=lambda model, pk: user if model == 'User' else orders[pk],
                             scalars=lambda query: ['synthetic-hash'])
        from unittest.mock import MagicMock
        session = MagicMock()
        session.__enter__.return_value = db
        m = SimpleNamespace(SessionLocal=lambda: session, User='User', Order='Order',
            ApiCredential=SimpleNamespace(key_hash=table.c.key_hash, created_by=table.c.created_by),
            subscription_terms={'current_entitlement': lambda *args: {'version_code': 'current',
                'expires_at': datetime(2026, 11, 1, tzinfo=timezone.utc), 'limits': acceptance.LIMITS}},
            issue_token=MagicMock(return_value='synthetic.private.jwt'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'owned.json'
            acceptance.write_scope(path, ids, 'prepared', create=True)
            with patch.object(acceptance, 'owned_rows', return_value={}):
                browser = acceptance.export_browser(m, ids, path, 'http://192.168.10.10:30080',
                    'http://192.168.10.10:30082', 'new-deployment')
            m.issue_token.assert_called_once_with(user)
            self.assertEqual(stat.S_IMODE(browser.stat().st_mode), 0o600)
            self.assertEqual(json.loads(browser.read_text())['buyerToken'], 'synthetic.private.jwt')
            self.assertNotIn('synthetic.private.jwt', path.read_text())
            acceptance.remove_browser_fixture(path)
            self.assertFalse(browser.exists())
            user.email = 'real-user@example.com'
            m.issue_token.reset_mock()
            with patch.object(acceptance, 'owned_rows', return_value={}):
                with self.assertRaises(acceptance.AcceptanceFailure):
                    acceptance.export_browser(m, ids, path, 'http://192.168.10.10:30080',
                        'http://192.168.10.10:30082', 'new-deployment')
            m.issue_token.assert_not_called()

    def test_browser_cleanup_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / 'owned.json'
            target = Path(directory) / 'token.json'
            target.write_text('not-to-delete')
            manifest.with_suffix('.browser.json').symlink_to(target)
            with self.assertRaises(acceptance.AcceptanceFailure):
                acceptance.remove_browser_fixture(manifest)
            self.assertTrue(target.exists())

    def test_browser_instrumentation_blocks_management_download_mutations_and_external_rp(self):
        import shutil
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node required for isolated browser policy validation')
        script = Path(acceptance.__file__).with_name('unified_delivery_browser_cdp.ps1').read_text()
        source = script.split("$instrumentation = @'\n", 1)[1].split("\n'@", 1)[0]
        spec = {'orders': {}, 'accessPaths': ['/api/orders/owned/delivery-access'], 'sensitiveSentinels': []}
        setup = r"""
const assert=require('node:assert/strict');
global.window=global;global.location={origin:'http://192.168.10.10:30080'};
global.navigator={};global.localStorage={getItem:()=>null};
global.document={querySelector:()=>null};let sent=0;
global.XMLHttpRequest=class {open(){}setRequestHeader(){}send(){sent++;}addEventListener(){}};
global.fetch=()=>{throw Error('Network forbidden in guard test');};
"""
        checks = r"""
function request(method,url){const xhr=new XMLHttpRequest();xhr.open(method,url);xhr.send();}
request('GET','/api/orders/owned/delivery-access');assert.equal(sent,1);
for(const [method,url] of [
['GET','/api/orders/owned/api-credentials'],['GET','/api/orders/other/delivery-access'],
['GET','/api/orders/owned/delivery-launch'],['GET','/api/files/owned/download?order_id=owned'],
['POST','/api/orders/owned/transition'],['POST','/api/orders/owned/delivery-launch'],
['GET','https://external-rp.invalid/authorize'],['GET','http://192.168.10.10:30082/gateway/owned'],
['GET','/api/orders/owned/delivery-access?extra=1'],
['GET','/api/notifications/stream?token=not-allowed'],['POST','/api/notifications/sender-context'],
['GET','https://external-rp.invalid/api/notifications/stream']]){
assert.throws(()=>request(method,url),/Blocked locally/);assert.equal(sent,1);}
assert.equal(window.__udaProbe.blocked,12);
for(const path of ['/api/notifications/stream','/api/notifications/sender-context']){
assert.throws(()=>request('GET',path),/Background GET intentionally suppressed/);assert.equal(sent,1);}
(async()=>{
for(const path of ['/api/notifications/stream','/api/notifications/sender-context']){
await assert.rejects(fetch(new URL(path,location.origin)),/Background GET intentionally suppressed/);}
assert.equal(window.__udaProbe.blocked,12);
assert.deepEqual(window.__udaProbe.takeBackgroundCounts(),{stream:2,senderContext:2});
assert.deepEqual(window.__udaProbe.takeBackgroundCounts(),{stream:0,senderContext:0});
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([node, '-'], input=setup + source.replace('__SPEC__', json.dumps(spec)) + checks,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


class SyntheticRedisCleanupGuards(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import Column, MetaData, String, Table
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'owned.json'
        self.ids = acceptance.fixture_ids('qa-uda-012345abcdef')
        table = Table('scoped_combinations', MetaData(), Column('id', String, primary_key=True),
                      Column('enterprise_id', String), Column('product_id', String))
        combo = SimpleNamespace(__table__=table, id=table.c.id,
                                enterprise_id=table.c.enterprise_id, product_id=table.c.product_id)
        self.client = Mock()
        self.client.eval.return_value = 1
        self.client.exists.return_value = False
        self.db = Mock()
        self.db.scalars.return_value = []
        self.m = SimpleNamespace(apisix_enabled=lambda: True, apisix_policy_redis=lambda: self.client,
                                 subscription_terms={'Combination': combo})
        acceptance.write_scope(self.path, self.ids, 'cleaned', create=True,
                               database_ids={table.name: {('owned-scope-1',), ('owned-scope-2',)}})

    def test_only_exact_synthetic_hash_is_atomically_removed(self):
        self.assertEqual(acceptance.cleanup_synthetic_policy(self.m, self.ids, self.path, self.db), 1)
        script, count, key, *scopes = self.client.eval.call_args.args
        self.assertEqual(count, 1)
        self.assertEqual(key, 'market:apisix:credential:qa-uda-012345abcdef-SYNTHETIC-NOT-APISIX-PROOF')
        self.assertEqual(scopes, ['owned-scope-1', 'owned-scope-2'])
        self.assertIn("'quota_scope'", script)
        self.assertNotIn('SCAN', script)
        self.assertNotIn('FLUSH', script)

    def test_unowned_hash_fails_closed(self):
        self.client.eval.return_value = -1
        with self.assertRaises(acceptance.AcceptanceFailure):
            acceptance.cleanup_synthetic_policy(self.m, self.ids, self.path, self.db)
        self.client.exists.assert_not_called()


if __name__ == '__main__':
    unittest.main()
