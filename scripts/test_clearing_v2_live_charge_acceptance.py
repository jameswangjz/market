"""Offline tests only; all runtime database/network boundaries are mocked."""
from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import clearing_v2_live_charge_acceptance as live


class HistoricalRedis:
    def __init__(self):
        self.streams = {live.guards.STREAM: [('1-0', {'private': 'SECRET_EVENT'})]}

    def type(self, name):
        return 'stream' if name in self.streams else 'none'

    def xlen(self, name):
        return len(self.streams[name])

    def xrange(self, name, min, max, count):
        rows = self.streams.get(name, [])
        if min != '-':
            position = tuple(map(int, min[1:].split('-')))
            rows = [row for row in rows if tuple(map(int, row[0].split('-'))) > position]
        return rows[:count]

    def scan_iter(self, match):
        return iter(self.streams)

    def close(self):
        pass


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'private.json'
        self.manifest = live.base.fixture_ids('qa-vcs-123456789abc')
        self.manifest['authorized'] = True
        self.manifest['environment'] = 'environment'
        self.manifest['database_ids'][live.KEY] = dict(
            phase='preflight', status='started', journal=[], credentials={})
        self.source = dict(source_stream=live.guards.STREAM + ':' + 'a' * 40,
                           source_run_id='a' * 40, physical_run_id='b' * 40)

    def test_review_required_before_loading_application(self):
        with patch.object(live.base, 'load_main') as load:
            with self.assertRaises(live.base.AcceptanceFailure):
                live.run(self.path)
            load.assert_not_called()

    def test_existing_manifest_never_replayed_or_overwritten(self):
        self.path.write_text('keep')
        with patch.object(live.base, 'load_main') as load:
            with self.assertRaises(live.base.AcceptanceFailure):
                live.run(self.path, reviewed=True)
            load.assert_not_called()
        self.assertEqual(self.path.read_text(), 'keep')

    def test_private_manifest_roundtrip_and_safe_summary(self):
        self.manifest['database_ids'][live.KEY]['credentials'] = dict(api_key='SECRET_KEY', tokens='SECRET_TOKEN')
        live.base.write_manifest(self.path, self.manifest, create=True)
        live.journal(self.path, self.manifest, 'purchase', 'completed')
        actual = live.base.read_manifest(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(actual['database_ids'][live.KEY]['credentials']['api_key'], 'SECRET_KEY')
        self.assertNotIn('SECRET', json.dumps(live.summary(actual)))

    def test_native_phases_use_baseline_and_capture_credentials(self):
        live.base.write_manifest(self.path, self.manifest, create=True)
        original_purchase = MagicMock(return_value='SECRET_KEY')
        def native(*args, **kwargs):
            self.assertEqual(kwargs, {'fixture_mode': 'baseline'})
            live.flow.purchase('native arguments')
            live.flow.charge('native arguments')
            live.flow.refund('native arguments')
            return {'charges': 2}
        with patch.object(live.flow, 'purchase', original_purchase), \
                patch.object(live.flow, 'charge') as charge, \
                patch.object(live.flow, 'refund') as refund, \
                patch.object(live.flow, 'exercise', side_effect=native):
            result = live.exercise(None, self.manifest, self.path, self.manifest, None, {})
        self.assertEqual(result, {'charges': 2})
        original_purchase.assert_called_once_with('native arguments')
        charge.assert_called_once_with('native arguments')
        refund.assert_called_once_with('native arguments')
        self.assertEqual(self.manifest['database_ids'][live.KEY]['credentials']['api_key'], 'SECRET_KEY')
        self.assertEqual([r['phase'] for r in self.manifest['database_ids'][live.KEY]['journal']],
                         ['purchase', 'purchase', 'charge', 'charge', 'refund', 'refund'])

    def runtime_patches(self, stack):
        m = SimpleNamespace(app=object())
        for name, value in (('load_main', m), ('fixture_ids', self.manifest),
                            ('preflight', None), ('environment_fingerprint', 'environment')):
            stack.enter_context(patch.object(live.base, name, return_value=value))
        stack.enter_context(patch.object(live, 'current_source', return_value=self.source))
        stack.enter_context(patch.object(live, 'historical_fingerprint', return_value={'private_digest': 'hash'}))
        return m

    def test_prepare_failure_retains_journal_without_cleanup(self):
        with ExitStack() as stack:
            self.runtime_patches(stack)
            cleanup = stack.enter_context(patch.object(live.guards, 'cleanup'))
            stack.enter_context(patch.object(live.base, 'prepare', side_effect=RuntimeError('SECRET_SQL')))
            with self.assertRaises(RuntimeError):
                live.run(self.path, reviewed=True)
            cleanup.assert_not_called()
        actual = live.base.read_manifest(self.path)
        self.assertEqual(actual['database_ids'][live.KEY]['status'], 'failed_preserved')
        self.assertNotIn('SECRET_SQL', self.path.read_text())

    def test_native_failure_keeps_credentials_and_partial_event_journal(self):
        def interrupted(*args):
            self.manifest['database_ids']['native_refund'] = dict(
                event_ids=['11-0'], source_stream=self.source['source_stream'])
            live.journal(self.path, self.manifest, 'charge', 'started')
            raise RuntimeError('SECRET_TRANSPORT')
        with ExitStack() as stack:
            self.runtime_patches(stack)
            stack.enter_context(patch.object(live.base, 'prepare', return_value={'buyer': 'SECRET_TOKEN'}))
            stack.enter_context(patch.object(live, 'exercise', side_effect=interrupted))
            client = stack.enter_context(patch('fastapi.testclient.TestClient')).return_value
            cleanup = stack.enter_context(patch.object(live.guards, 'cleanup'))
            disable = stack.enter_context(patch.object(live, 'quiesce'))
            with self.assertRaises(RuntimeError):
                live.run(self.path, reviewed=True)
            cleanup.assert_not_called()
            disable.assert_not_called()
            client.close.assert_called_once()
        actual = live.base.read_manifest(self.path)
        self.assertEqual(actual['database_ids']['native_refund']['event_ids'], ['11-0'])
        self.assertEqual(actual['database_ids'][live.KEY]['phase'], 'charge')
        self.assertEqual(actual['database_ids'][live.KEY]['status'], 'failed_preserved')
        self.assertEqual(actual['database_ids'][live.KEY]['credentials']['tokens']['buyer'], 'SECRET_TOKEN')

    def test_source_preflight_closes_client_and_rejects_unregistered_source(self):
        redis = MagicMock()
        m = SimpleNamespace(apisix_policy_redis=MagicMock(return_value=redis), SessionLocal=MagicMock())
        with patch.object(live.guards, 'source_context', return_value=(live.guards.STREAM, None)):
            with self.assertRaises(live.base.AcceptanceFailure):
                live.current_source(m)
        redis.close.assert_called_once()
        redis.info.assert_not_called()

    def test_success_preserves_fixtures_and_quiesces_owned_identities(self):
        def exercise(*args):
            self.manifest['database_ids']['native_refund'] = dict(self.source,
                proof=dict(charges=2, monthly_used=2, refunded_amount=16))
            return self.manifest['database_ids']['native_refund']['proof']
        with ExitStack() as stack:
            self.runtime_patches(stack)
            stack.enter_context(patch.object(live.base, 'prepare', return_value={'buyer': 'SECRET_TOKEN'}))
            stack.enter_context(patch.object(live, 'exercise', side_effect=exercise))
            stack.enter_context(patch.object(live, 'verify_preserved'))
            client = stack.enter_context(patch('fastapi.testclient.TestClient')).return_value
            cleanup = stack.enter_context(patch.object(live.guards, 'cleanup'))
            disable = stack.enter_context(patch.object(live, 'quiesce',
                side_effect=lambda m, path, manifest: live.summary(manifest)))
            result = live.run(self.path, reviewed=True)
            cleanup.assert_not_called()
            disable.assert_called_once()
            client.close.assert_called_once()
        self.assertEqual(result['status'], 'passed')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(live.base.read_manifest(self.path)['database_ids'][live.KEY][
            'credentials']['tokens']['buyer'], 'SECRET_TOKEN')

    def test_disable_only_owned_route_and_preserve_document(self):
        route = self.manifest['routes']['api']['source']
        product = self.manifest['products']['api']
        self.manifest['external']['route_keys'] = [route]
        self.manifest['database_ids']['native_refund'] = dict(route_key=route, product_id=product)
        self.manifest['database_ids'][live.KEY]['acceptance_status'] = 'passed'
        live.base.write_manifest(self.path, self.manifest, create=True)
        document = dict(id=route, labels={'market_product_id': product},
                        plugins={'key-auth': {'private': 'SECRET'}}, status=1)
        expected = dict(document, status=0)
        with patch.object(live.base, 'environment_fingerprint', return_value='environment'), \
                patch.object(live, 'verify_preserved'), \
                patch.object(live, 'owned_accounts'), \
                patch.object(live, 'disable_accounts') as disable, \
                patch.object(live.base, 'apisix_admin', side_effect=[document, {}, expected]) as admin:
            m = SimpleNamespace(SessionLocal=MagicMock())
            result = live.quiesce(m, self.path, self.manifest)
        self.assertEqual([call.args[1] for call in admin.call_args_list], ['GET', 'PUT', 'GET'])
        self.assertEqual(admin.call_args_list[1].args, (m, 'PUT', '/routes/' + route, expected))
        disable.assert_called_once_with(m, self.path, self.manifest)
        self.assertTrue(result['route_disabled'])
        self.assertEqual(result['acceptance_status'], 'passed')
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_foreign_route_refused_before_write(self):
        route = self.manifest['routes']['api']['source']
        self.manifest['external']['route_keys'] = [route]
        self.manifest['database_ids']['native_refund'] = dict(route_key=route,
            product_id=self.manifest['products']['api'])
        with patch.object(live.base, 'environment_fingerprint', return_value='environment'), \
                patch.object(live, 'verify_preserved'), \
                patch.object(live, 'owned_accounts'), \
                patch.object(live.base, 'apisix_admin', return_value=dict(
                    id=route, labels={'market_product_id': 'foreign'})) as admin:
            with self.assertRaises(live.base.AcceptanceFailure):
                live.quiesce(SimpleNamespace(SessionLocal=MagicMock()), self.path, self.manifest)
        self.assertEqual(admin.call_count, 1)

    def account_fixture(self):
        m = SimpleNamespace(User=object(), Enterprise=object(), SessionLocal=MagicMock())
        roots = {}
        for eid in self.manifest['enterprises'].values():
            roots[eid] = SimpleNamespace(id=eid, name=eid, credit_code=eid)
        for uid in self.manifest['users'].values():
            roots[uid] = SimpleNamespace(id=uid, name=uid, email=uid + '@example.invalid',
                                        is_active=True, platform_role='super_admin')
        db = m.SessionLocal.return_value.__enter__.return_value
        db.get.side_effect = lambda model, key, **kwargs: roots.get(key)
        return m, db, roots

    def test_disable_exact_four_accounts_retains_identity_and_acceptance(self):
        m, db, roots = self.account_fixture()
        self.manifest['database_ids'][live.KEY]['acceptance_status'] = 'passed'
        live.base.write_manifest(self.path, self.manifest, create=True)
        live.disable_accounts(m, self.path, self.manifest)
        self.assertTrue(all(roots[uid].is_active is False for uid in self.manifest['users'].values()))
        self.assertTrue(all(roots[uid].platform_role == 'super_admin' for uid in self.manifest['users'].values()))
        self.assertEqual(set(call.args[1] for call in db.get.call_args_list), set(roots))
        db.commit.assert_called_once()
        db.delete.assert_not_called()
        db.execute.assert_not_called()
        result = live.summary(self.manifest)
        self.assertTrue(result['fixture_accounts_disabled'])
        self.assertEqual(result['acceptance_status'], 'passed')

    def test_invalid_account_or_enterprise_rejected_before_any_disable(self):
        for kind in ('users', 'enterprises'):
            with self.subTest(kind=kind):
                m, db, roots = self.account_fixture()
                last = list(self.manifest[kind].values())[-1]
                roots[last].name = 'real_identity'
                live.base.write_manifest(self.path, self.manifest, create=not self.path.exists())
                with self.assertRaises(live.base.AcceptanceFailure):
                    live.disable_accounts(m, self.path, self.manifest)
                self.assertTrue(all(roots[uid].is_active for uid in self.manifest['users'].values()))
                db.commit.assert_not_called()

    def test_modified_manifest_account_roots_rejected_before_database_reads(self):
        m, db, roots = self.account_fixture()
        self.manifest['users']['delivery'] = 'real-admin'
        with self.assertRaises(live.base.AcceptanceFailure):
            live.owned_accounts(m, db, self.manifest)
        db.get.assert_not_called()

    def test_cli_failure_never_prints_exception_or_credentials(self):
        with patch('sys.argv', ['acceptance', 'run', '--manifest', str(self.path), '--reviewed']), \
                patch.object(live, 'run', side_effect=RuntimeError('SECRET_PASSWORD')), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(live.main(), 1)
        self.assertNotIn('SECRET', output.getvalue())
        self.assertFalse(json.loads(output.getvalue())['automatic_cleanup'])

    def historical_fixture(self):
        from sqlalchemy import Column, MetaData, String, Table, create_engine
        from sqlalchemy.orm import registry, sessionmaker
        mapper = registry(metadata=MetaData())
        models = {}
        for name, fields in {'SourceGeneration': ('id', 'stream', 'old_stream'),
                             'Cursor': ('stream', 'last_id', 'redis_run_id'),
                             'Gap': ('id', 'old_stream', 'reason')}.items():
            table = Table(name, mapper.metadata, *[
                Column(field, String, primary_key=index == 0) for index, field in enumerate(fields)])
            model = type(name, (), {})
            mapper.map_imperatively(model, table)
            models[name] = model
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        mapper.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        redis = HistoricalRedis()
        redis.streams[self.source['source_stream']] = [('2-0', {'private': 'ACTIVE_SECRET'})]
        with sessions.begin() as db:
            db.add(models['SourceGeneration'](id='old', stream=live.guards.STREAM, old_stream=None))
            db.add(models['SourceGeneration'](id='active', stream=self.source['source_stream'],
                                             old_stream=live.guards.STREAM))
            db.add(models['Cursor'](stream=live.guards.STREAM, last_id='1-0', redis_run_id='old'))
            db.add(models['Cursor'](stream=self.source['source_stream'], last_id='2-0', redis_run_id='active'))
            db.add(models['Gap'](id='gap', old_stream=live.guards.STREAM, reason='SECRET_REASON'))
        return SimpleNamespace(SessionLocal=sessions, apisix_policy_redis=lambda: redis,
                               api_charge_ledger=models), redis

    def test_historical_digest_covers_full_stream_cursor_gap_and_excludes_active(self):
        m, redis = self.historical_fixture()
        before = live.historical_fingerprint(m, self.source)
        self.assertNotIn('SECRET', json.dumps(before))
        self.assertEqual(before['stream_count'], 1)
        self.assertEqual(before['cursors']['count'], 1)
        self.assertEqual(before['gaps']['count'], 1)
        redis.streams[self.source['source_stream']].append(('3-0', {'new': 'active'}))
        with m.SessionLocal.begin() as db:
            db.get(m.api_charge_ledger['Cursor'], self.source['source_stream']).last_id = '3-0'
        self.assertEqual(before, live.historical_fingerprint(m, self.source))
        redis.streams[live.guards.STREAM][0][1]['private'] = 'CHANGED'
        self.assertNotEqual(before, live.historical_fingerprint(m, self.source))

    def test_verify_preserved_rejects_old_cursor_gap_and_stream_set_changes(self):
        for change in ('cursor', 'gap_update', 'gap_insert', 'gap_delete', 'stream_insert', 'stream_delete'):
            with self.subTest(change=change):
                m, redis = self.historical_fixture()
                state = self.manifest['database_ids'][live.KEY]
                state['source'] = self.source
                state['historical_before'] = live.historical_fingerprint(m, self.source)
                with patch.object(live, 'current_source', return_value=self.source), \
                        patch.object(live.guards, '_protected'):
                    live.verify_preserved(m, self.manifest)
                    with m.SessionLocal.begin() as db:
                        if change == 'cursor':
                            db.get(m.api_charge_ledger['Cursor'], live.guards.STREAM).last_id = '9-0'
                        elif change == 'gap_update':
                            db.get(m.api_charge_ledger['Gap'], 'gap').reason = 'changed'
                        elif change == 'gap_insert':
                            db.add(m.api_charge_ledger['Gap'](id='extra', reason='new'))
                        elif change == 'gap_delete':
                            db.delete(db.get(m.api_charge_ledger['Gap'], 'gap'))
                        elif change == 'stream_insert':
                            redis.streams[live.guards.STREAM + ':' + 'c' * 40] = []
                        else:
                            del redis.streams[live.guards.STREAM]
                    with self.assertRaisesRegex(live.base.AcceptanceFailure,
                                                'historical_stream_cursor_or_gap_changed'):
                        live.verify_preserved(m, self.manifest)
                    self.assertFalse(state['historical_verified'])

    def test_stream_digest_reads_all_pages_and_detects_payload_edit(self):
        redis = HistoricalRedis()
        redis.streams[live.guards.STREAM] = [(f'{index}-0', {'value': str(index)})
                                           for index in range(1, 1002)]
        before = live.stream_fingerprint(redis, live.guards.STREAM)
        self.assertEqual(before['count'], 1001)
        redis.streams[live.guards.STREAM][750][1]['value'] = 'changed'
        self.assertNotEqual(before, live.stream_fingerprint(redis, live.guards.STREAM))


if __name__ == '__main__':
    unittest.main()
