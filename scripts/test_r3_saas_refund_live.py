"""Pure guards: no database, network, deployment, or fixture mutation."""
import copy
import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import ForeignKey, MetaData, String, Table, Column, create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, mapped_column

import r3_saas_refund_live as live
import r3_saas_refund_live_transport as transport


class Models(DeclarativeBase):
    pass


class Enterprise(Models):
    __tablename__ = 'enterprises'
    id = mapped_column(String, primary_key=True)
    name = mapped_column(String)


class Product(Models):
    __tablename__ = 'products'
    id = mapped_column(String, primary_key=True)
    enterprise_id = mapped_column(ForeignKey('enterprises.id'))
    status = mapped_column(String)


class OAuthClient(Models):
    __tablename__ = 'oauth_clients'
    id = mapped_column(String, primary_key=True)
    product_id = mapped_column(ForeignKey('products.id'))
    client_id = mapped_column(String)
    client_secret = mapped_column(String)


class SaaSSubscription(Models):
    __tablename__ = 'subscriptions'
    id = mapped_column(String, primary_key=True)


class SaaSIntegrationConfig(Models):
    __tablename__ = 'configs'
    id = mapped_column(String, primary_key=True)
    product_id = mapped_column(String)


class Evidence(Models):
    __tablename__ = 'evidence'
    id = mapped_column(String, primary_key=True)
    refund_id = mapped_column(String)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.plan = dict(tenant_id='tenant-owned', product_id='owned-product',
            enterprise_id='owned-buyer', subscription_id='owned-sub', refund_id='owned-refund',
            expected_expires_at='2027-04-01T00:00:00+00:00',
            expires_at='2027-03-15T00:00:00+00:00')
        self.status = dict(self.plan, ret=0, refund_shorten_supported=True,
                           refund_shorten_protocol='refund_shorten_v1')

    def test_exact_shortening_receipt(self):
        live.verify_status(self.status, self.plan, shortened=True)

    def test_pre_refund_status(self):
        self.status['expires_at'] = self.plan['expected_expires_at']
        live.verify_status(self.status, self.plan, shortened=False)

    def test_wrong_identity_expiry_or_capability_rejected(self):
        for key, value in dict(tenant_id='foreign', product_id='foreign', enterprise_id='foreign',
                subscription_id='foreign', refund_id='foreign', ret=True,
                expires_at=self.plan['expected_expires_at'], refund_shorten_supported=False,
                refund_shorten_protocol='unknown').items():
            with self.subTest(key=key):
                status = dict(self.status, **{key: value})
                with self.assertRaises(live.base.AcceptanceFailure):
                    live.verify_status(status, self.plan, shortened=True)

    def test_extension_and_noop_rejected(self):
        for expiry in ('2027-04-01T00:00:00+00:00', '2027-05-01T00:00:00+00:00'):
            plan = dict(self.plan, expires_at=expiry)
            with self.assertRaises(live.base.AcceptanceFailure):
                live.verify_status(dict(self.status, expires_at=expiry), plan, shortened=True)

    def test_timezone_equivalence(self):
        live.verify_status(dict(self.status, expires_at='2027-03-14T17:00:00-07:00'),
                           self.plan, shortened=True)

    def test_naive_expiry_rejected(self):
        with self.assertRaises(live.base.AcceptanceFailure):
            live.aware('2027-03-15T00:00:00')

    def test_live_run_requires_review_before_loading_app(self):
        with patch.object(live.base, 'load_main') as load:
            with self.assertRaises(live.base.AcceptanceFailure):
                live.run()
            load.assert_not_called()

    def test_guard_does_not_mutate_receipts(self):
        original = copy.deepcopy((self.status, self.plan))
        live.verify_status(self.status, self.plan, shortened=True)
        self.assertEqual(original, (self.status, self.plan))


class BridgeTests(unittest.TestCase):
    """Execute exact bridge SQL locally; these are not PostgreSQL evidence."""
    def setUp(self):
        self.engine = create_engine('sqlite://')
        with self.engine.begin() as conn:
            conn.exec_driver_sql("ATTACH DATABASE ':memory:' AS public")
        self.m = SimpleNamespace(Enterprise=Enterprise, Product=Product, OAuthClient=OAuthClient)
        self.tables = live.bridge_tables(self.m)
        next(iter(self.tables.values())).metadata.create_all(self.engine)
        self.ids = live.base.fixture_ids('qa-vcs-saas-012345abcdef')
        self.rows = dict(enterprises=dict(id=self.ids['enterprises']['provider'], name='owned'),
            products=dict(id=self.ids['product'], enterprise_id=self.ids['enterprises']['provider'], status='draft'),
            oauth_clients=dict(id='owned-oauth', product_id=self.ids['product'],
                client_id=self.ids['oauth_client_id'], client_secret='private-test-secret'))
        self.journal = dict(public_phase='not_created', protected={'count': 1, 'sha256': 'test'})
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'journal.json'
        live.base.write_manifest(self.path, self.journal, create=True)
        self.snapshot = patch.object(live, 'protected_snapshot', return_value=self.journal['protected'])
        self.expected = patch.object(live, 'bridge_expected', return_value=self.rows)
        self.snapshot.start()
        self.expected.start()
        self.addCleanup(self.snapshot.stop)
        self.addCleanup(self.expected.stop)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.temp.cleanup)

    def save(self):
        live.base.write_manifest(self.path, self.journal)

    def create(self):
        live.create_bridge(self.m, self.engine, self.ids, self.journal, self.save)

    def test_journal_is_private_and_precedes_public_inserts(self):
        inserts = []

        def before(_conn, _cursor, statement, _params, _context, _many):
            if statement.startswith('INSERT'):
                saved = json.loads(self.path.read_text())
                self.assertEqual(saved['public_roots'], self.rows)
                self.assertEqual(saved['public_phase'], 'collision_checked')
                self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
                inserts.append(statement)

        event.listen(self.engine, 'before_cursor_execute', before)
        self.create()
        self.assertEqual(len(inserts), 3)

    def test_exact_cleanup_preserves_unrelated_roots(self):
        self.create()
        with self.engine.begin() as conn:
            conn.execute(self.tables['enterprises'].insert().values(id='unrelated', name='retained'))
        live.delete_bridge(self.m, self.engine, self.journal, self.save)
        self.assertEqual(self.journal['public_phase'], 'removed')
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(select(self.tables['enterprises'].c.id)).scalars().all(), ['unrelated'])
            self.assertEqual(conn.execute(select(self.tables['products'])).all(), [])
            self.assertEqual(conn.execute(select(self.tables['oauth_clients'])).all(), [])
        live.delete_bridge(self.m, self.engine, self.journal, self.save)

    def test_collision_never_authorizes_deletion(self):
        with self.engine.begin() as conn:
            conn.execute(self.tables['enterprises'].insert().values(**self.rows['enterprises']))
        with self.assertRaises(live.base.AcceptanceFailure):
            self.create()
        self.assertEqual(self.journal['public_phase'], 'create_requested')
        live.delete_bridge(self.m, self.engine, self.journal, self.save)
        with self.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(self.tables['enterprises'])).all()), 1)

    def test_changed_public_root_refuses_all_deletion(self):
        self.create()
        with self.engine.begin() as conn:
            conn.execute(self.tables['products'].update().values(status='published'))
        with self.assertRaises(live.base.AcceptanceFailure):
            live.delete_bridge(self.m, self.engine, self.journal, self.save)
        with self.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(self.tables['oauth_clients'])).all()), 1)

    def test_unjournaled_fk_child_refuses_all_deletion(self):
        self.create()
        metadata = next(iter(self.tables.values())).metadata
        child = Table('unexpected_children', metadata, Column('id', String, primary_key=True),
            Column('product_id', ForeignKey('public.products.id')), schema='public')
        child.create(self.engine)
        with self.engine.begin() as conn:
            conn.execute(child.insert().values(id='foreign', product_id=self.ids['product']))
        with self.assertRaises(live.base.AcceptanceFailure):
            live.delete_bridge(self.m, self.engine, self.journal, self.save)
        with self.engine.connect() as conn:
            self.assertEqual(len(conn.execute(select(self.tables['oauth_clients'])).all()), 1)

    def test_uncommitted_create_has_nothing_to_delete(self):
        self.journal.update(public_phase='collision_checked', public_roots=self.rows)
        live.delete_bridge(self.m, self.engine, self.journal, self.save)
        self.assertEqual(self.journal['public_phase'], 'removed')

    def test_protected_drift_blocks_bridge_writes(self):
        with patch.object(live, 'protected_snapshot', return_value={'count': 0, 'sha256': 'changed'}):
            with self.assertRaises(live.base.AcceptanceFailure):
                self.create()
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(select(self.tables['enterprises'])).all(), [])


class IsolationTests(unittest.TestCase):
    def test_private_role_schema_and_public_permissions(self):
        schema = 'qa_r3_be_0123456789abcdef'
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        for values, passes in [([schema, schema, schema + '_role', 1, 0, False], True),
                (['public'], False), ([schema, schema, 'admin'], False),
                ([schema, schema, schema + '_role', 0], False),
                ([schema, schema, schema + '_role', 1, 1], False),
                ([schema, schema, schema + '_role', 1, 0, True], False)]:
            with self.subTest(values=values):
                connection.scalar.side_effect = values
                if passes:
                    live.assert_private(SimpleNamespace(engine=engine), schema, schema + '_role')
                else:
                    with self.assertRaises(live.base.AcceptanceFailure):
                        live.assert_private(SimpleNamespace(engine=engine), schema, schema + '_role')


class ExternalCleanupTests(unittest.TestCase):
    def setUp(self):
        self.ids = live.base.fixture_ids('qa-vcs-saas-012345abcdef')
        self.ids['database_ids']['subscription_id'] = '012345abcdef00000000000000000000'
        self.ids['external']['tenant'] = 'tenant-012345abcdef'
        self.ids['external']['tenant_created'] = True
        self.identity = dict(tenant_id=self.ids['external']['tenant'], product_id=self.ids['product'],
            enterprise_id=self.ids['enterprises']['buyer'], subscription_id=self.ids['database_ids']['subscription_id'])
        self.sub = SimpleNamespace(id=self.identity['subscription_id'], product_id=self.identity['product_id'],
            enterprise_id=self.identity['enterprise_id'], external_tenant_id=self.identity['tenant_id'])
        self.m = SimpleNamespace(SessionLocal=MagicMock(), SaaSSubscription=SaaSSubscription,
            SaaSIntegrationConfig=SaaSIntegrationConfig, saas_call=MagicMock(return_value=dict(
                ret=0, tenant_id=self.identity['tenant_id'], status='closed')))
        db = self.m.SessionLocal.return_value.__enter__.return_value
        db.get.return_value = self.sub
        db.scalar.return_value = SimpleNamespace(id='private-config')
        self.journal, self.save = {}, MagicMock()

    def test_close_is_owned_and_reports_retained_closed(self):
        replies = [(200, dict(self.identity, ret=0, status=status)) for status in ('active', 'closed')]
        with patch.object(live.base, 'mock_saas_status', side_effect=replies):
            live.close_external(self.m, self.ids, self.journal, self.save)
        self.assertEqual(self.journal['tenant_cleanup'], 'retained_closed')
        self.m.saas_call.assert_called_once()
        args = self.m.saas_call.call_args.args
        self.assertEqual(args[1], 'CLOSE')
        self.assertEqual({k: args[2][k] for k in self.identity}, self.identity)

    def test_wrong_remote_identity_prevents_close(self):
        with patch.object(live.base, 'mock_saas_status', return_value=(200, dict(
                self.identity, ret=0, enterprise_id='foreign'))):
            with self.assertRaises(live.base.AcceptanceFailure):
                live.close_external(self.m, self.ids, self.journal, self.save)
        self.m.saas_call.assert_not_called()

    def test_close_readback_failure_retains_recovery_state(self):
        replies = [(200, dict(self.identity, ret=0, status='active'))] * 2
        with patch.object(live.base, 'mock_saas_status', side_effect=replies):
            with self.assertRaises(live.base.AcceptanceFailure):
                live.close_external(self.m, self.ids, self.journal, self.save)
        self.assertEqual(self.journal['tenant_cleanup'], 'close_requested')

    def test_created_tenant_missing_is_not_success(self):
        with patch.object(live.base, 'mock_saas_status', return_value=(404, {'detail': 'tenant_not_found'})):
            with self.assertRaises(live.base.AcceptanceFailure):
                live.close_external(self.m, self.ids, self.journal, self.save)
        self.m.saas_call.assert_not_called()


class EarlierRefundTests(unittest.TestCase):
    def test_specific_later_renewal_denial_has_no_execution_effects(self):
        ids = live.base.fixture_ids('qa-vcs-saas-012345abcdef')
        ids['database_ids']['refunds'] = ['owned-earlier-refund']
        sessions = MagicMock()
        db = sessions.return_value.__enter__.return_value
        db.scalar.return_value = None
        db.get.side_effect = lambda model, _id: SimpleNamespace(
            status='approved') if model == 'refund' else SimpleNamespace(refunded_amount=0)
        m = SimpleNamespace(SessionLocal=sessions, Refund='refund', Order='order',
            refund_execution={k: Evidence for k in ('Execution', 'PaymentLeg', 'Completion')},
            refund_execution_resources={'Recovery': Evidence})
        client = MagicMock()
        reason = ('\u8bf7\u5148\u5b8c\u6210\u540e\u7eed\u7eed\u8d39\u8ba2\u5355\u7684\u9000\u6b3e'
                  '\uff0c\u518d\u5bf9\u5f53\u524d\u8ba2\u5355\u9000\u6b3e')
        client.post.return_value.status_code = 409
        client.post.return_value.json.return_value = {'detail': reason}
        tokens = dict(buyer='private-buyer-token', delivery='private-ops-token')
        replies = [dict(status='available'), dict(id='owned-earlier-refund', version=1),
                   dict(id='owned-earlier-refund', version=2)]
        with patch.object(live.base, 'request', side_effect=replies), \
                patch.object(live, 'remote_status', return_value={'expires_at': 'unchanged'}):
            result = live.blocked_earlier_refund(m, ids, client, tokens, 'original')
        self.assertTrue(result['unrefunded_renewal_blocked'])
        self.assertEqual(db.scalar.call_count, 4)
        # An unrelated 409 must never be accepted as the renewal guard.
        client.post.return_value.json.return_value = {'detail': 'unrelated_conflict'}
        with patch.object(live.base, 'request', side_effect=replies), \
                patch.object(live, 'remote_status', return_value={'expires_at': 'unchanged'}):
            with self.assertRaises(live.AcceptanceFailure):
                live.blocked_earlier_refund(m, ids, client, tokens, 'original')


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'recovery.json'
        self.value = dict(journal_version=1, schema='qa_r3_be_0123456789abcdef',
            role='qa_r3_be_0123456789abcdef_role', environment='a' * 64,
            protected=dict(count=1, sha256='b' * 64), public_roots={}, public_phase='not_created',
            schema_phase='created', role_phase='created', tenant_cleanup='not_created', oidc_cleanup='not_created',
            fixture=live.base.fixture_ids('qa-vcs-saas-012345abcdef'))

    def write(self):
        live.base.write_manifest(self.path, self.value, create=not self.path.exists())

    def test_valid_recovery_journal(self):
        self.write()
        self.assertEqual(live.read_recovery(self.path), self.value)

    def test_symlink_and_world_readable_refused(self):
        self.write()
        link = self.path.with_suffix('.link')
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            live.read_recovery(link)
        self.path.chmod(0o644)
        with self.assertRaises(live.AcceptanceFailure):
            live.read_recovery(self.path)

    def test_tampered_roots_schema_role_and_ambiguous_ddl_refused(self):
        original = copy.deepcopy(self.value)
        for mutation in (
                lambda x: x.update(schema='public'),
                lambda x: x.update(role='postgres'),
                lambda x: x.update(role_phase='create_requested'),
                lambda x: x['fixture'].update(product='shared-product'),
                lambda x: x['fixture']['external'].update(tenant='tenant-foreign'),
                lambda x: x.update(public_phase='created'),
                lambda x: x.update(unexpected='field')):
            self.value = copy.deepcopy(original)
            mutation(self.value)
            self.write()
            with self.assertRaises(live.AcceptanceFailure):
                live.read_recovery(self.path)

    def test_environment_mismatch_fails_before_control_sql(self):
        self.write()
        with patch.dict(os.environ, DATABASE_URL='postgresql://private:private@invalid/private'), \
                patch('sqlalchemy.create_engine') as create:
            with self.assertRaises(live.AcceptanceFailure):
                live.resume_cleanup(self.path, reviewed=True)
            create.assert_not_called()

    def test_arbitrary_error_text_never_exposed(self):
        secret = 'SQL secret bearer private-test-secret'
        self.assertNotIn(secret, json.dumps(live.safe_error(RuntimeError(secret))))
        try:
            live.check(False, 'protected_snapshot_changed')
        except live.AcceptanceFailure as exc:
            self.assertEqual(live.safe_error(exc)['label'], 'protected_snapshot_changed')


class TransportTests(unittest.TestCase):
    def test_payload_compiles_and_injects_dependencies_in_order(self):
        payload, hashes = transport.build_payload('/tmp/qa-saas-refund-unit.json')
        compile(payload, '<private-payload>', 'exec')
        self.assertEqual(set(hashes), set(transport.HELPERS))
        self.assertLess(payload.index("m=types.ModuleType('r3_native_refund_runtime_acceptance')"),
                        payload.index("m=types.ModuleType('r3_native_refund_flow')"))
        self.assertIn('installed_source_sha256', payload)
        self.assertIn('signal.alarm(150)', payload)
        cleanup, _ = transport.build_payload('/tmp/qa-saas-refund-unit.json', cleanup=True)
        self.assertIn('result=r.resume_cleanup(', cleanup)

    def test_secret_stays_in_stdin_not_argv_or_evidence(self):
        secret = 'private-unit-admin-secret'
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if len(calls) == 1:
                return SimpleNamespace(returncode=0, stdout=json.dumps(dict(
                    data=dict(secret=base64.b64encode(secret.encode()).decode()))))
            self.assertIn(repr(secret), kwargs['input'])
            compile(kwargs['input'], '<private-payload>', 'exec')
            self.assertLessEqual(kwargs['timeout'], 180)
            return SimpleNamespace(returncode=0, stdout=json.dumps(dict(status='passed',
                installed_source_sha256={'main.py': 'a' * 64})))

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, SSHPASS='private-ssh'), \
                patch.object(transport.subprocess, 'run', side_effect=run), redirect_stdout(io.StringIO()) as output:
            path = Path(directory) / 'evidence.json'
            code = transport.main(['--pod', 'market-api-unit', '--manifest',
                '/tmp/qa-saas-refund-unit.json', '--output', str(path)])
            self.assertEqual(code, 0)
            self.assertNotIn(secret, path.read_text() + output.getvalue())
            self.assertNotIn(secret, repr([argv for argv, _ in calls]))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(len(calls), 2)

    def test_secret_fetch_failure_does_not_invoke_api_or_leak_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, SSHPASS='private-ssh'), \
                patch.object(transport.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=1, stdout='sensitive diagnostic', stderr='secret')) as run, \
                redirect_stdout(io.StringIO()) as output:
            path = Path(directory) / 'evidence.json'
            code = transport.main(['--pod', 'market-api-unit', '--manifest',
                '/tmp/qa-saas-refund-unit.json', '--output', str(path)])
            self.assertEqual(code, 1)
            run.assert_called_once()
            self.assertNotIn('sensitive diagnostic', path.read_text() + output.getvalue())
            self.assertNotIn('secret', path.read_text() + output.getvalue())

    def test_existing_evidence_prevents_all_transport(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, SSHPASS='private-ssh'), \
                patch.object(transport.subprocess, 'run') as run:
            path = Path(directory) / 'evidence.json'
            path.write_text('retained')
            with self.assertRaises(FileExistsError):
                transport.main(['--pod', 'market-api-unit', '--manifest',
                    '/tmp/qa-saas-refund-unit.json', '--output', str(path)])
            run.assert_not_called()
            self.assertEqual(path.read_text(), 'retained')

if __name__ == '__main__':
    unittest.main()
