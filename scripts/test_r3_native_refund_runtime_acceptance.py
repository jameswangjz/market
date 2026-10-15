"""Offline tests only; no application import or live services required."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import r3_native_refund_runtime_acceptance as acceptance


class OwnershipGuards(unittest.TestCase):
    def setUp(self):
        self.order, self.term, self.combo, self.refund = (c * 32 for c in 'abcd')
        self.context = dict(start='100-0', end='101-2', order_id=self.order,
                            term_id=self.term, combination_id=self.combo,
                            route_key='qa-vcs-0123456789ab-api-source', source_run_id='e' * 40)
        self.event = dict(order_id=self.order, segment_id=self.term, scope=self.combo,
                          period_key=self.combo + ':1:0', route_key=self.context['route_key'],
                          source_run_id='e' * 40, quantity='1', policy='gateway_actual_charge_v1',
                          charged_at_us='1792100000000000')

    def test_runtime_review_gate(self):
        with patch.object(acceptance.base, 'load_main') as load, \
                patch.object(acceptance.base, 'write_manifest') as write, \
                patch.object(acceptance.base, 'cleanup') as cleanup:
            with self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.run('/unused')
            for helper in (load, write, cleanup):
                helper.assert_not_called()

    def test_safe_failure_only_exposes_restricted_guard_labels(self):
        import version_change_quote_acceptance as guarded
        for kind in (acceptance.base.AcceptanceFailure, guarded.AcceptanceFailure):
            self.assertEqual(acceptance.safe_failure(kind('retained_sla_set_mismatch'))[
                'safe_detail'], 'retained_sla_set_mismatch')
        for exc in (RuntimeError('retained_sla_set_mismatch'),
                    acceptance.base.AcceptanceFailure('SELECT secret FROM users'),
                    acceptance.base.AcceptanceFailure('mk_private_value'),
                    acceptance.base.AcceptanceFailure('password_leaked')):
            self.assertNotIn('safe_detail', acceptance.safe_failure(exc))

    def test_reviewed_requires_actual_preflight(self):
        with patch.object(acceptance.base, 'load_main', return_value=object()), \
                patch.object(acceptance.base, 'preflight', side_effect=
                             acceptance.base.AcceptanceFailure('unsafe_environment')), \
                patch.object(acceptance.base, 'write_manifest') as write, \
                patch.object(acceptance, 'cleanup') as cleanup:
            with self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.run('/unused', reviewed=True)
            write.assert_not_called()
            cleanup.assert_not_called()

    def redis_fixture(self):
        manifest = acceptance.base.fixture_ids('qa-vcs-0123456789ab')
        state = dict(order_ids=[self.order], refund_ids=[self.refund],
                     order_id=self.order, term_id=self.term, combination_id=self.combo,
                     stream_start='100-0', stream_end='101-2', source_run_id='e' * 40,
                     event_ids=[], extra_keys=sorted(acceptance.extra_keys(
                         combination_id=self.combo, generation=1, period_index=0,
                         refund_id=self.refund)))
        manifest['database_ids']['native_refund'] = state
        redis = MagicMock()
        redis.get.return_value = None
        redis.type.return_value = 'none'
        redis.xrange.return_value = []
        return manifest, state, redis

    def test_foreign_extra_key_refused(self):
        manifest, state, redis = self.redis_fixture()
        state['extra_keys'].append(acceptance.STREAM)
        with self.assertRaises(acceptance.base.AcceptanceFailure):
            acceptance._redis_preflight(None, None, redis, manifest, [])
        redis.delete.assert_not_called()

    def test_coverage_hash_baseline(self):
        manifest, _, redis = self.redis_fixture()
        redis.type.return_value = 'hash'
        redis.hget.return_value = '0'
        acceptance._redis_preflight(None, None, redis, manifest, [])
        self.assertTrue(all(not call.args[0].startswith(acceptance.COVERAGE)
                            for call in redis.get.call_args_list))
        redis.hget.return_value = '1'
        with self.assertRaises(acceptance.base.AcceptanceFailure):
            acceptance._redis_preflight(None, None, redis, manifest, [])

    def test_missing_event_requires_phase_and_exact_charge(self):
        manifest, state, redis = self.redis_fixture()
        state.update(event_ids=['101-1'], validated_events={'101-1': self.event})
        db, m = MagicMock(), MagicMock()
        with self.assertRaises(acceptance.base.AcceptanceFailure):
            acceptance._redis_preflight(m, db, redis, manifest, [])
        state['external_delete_phase'] = True
        db.get.return_value = SimpleNamespace(id='101-1', **self.event)
        _, events = acceptance._redis_preflight(m, db, redis, manifest, [])
        self.assertEqual(events, [])
        db.get.return_value.order_id = 'f' * 32
        with self.assertRaises(acceptance.base.AcceptanceFailure):
            acceptance._redis_preflight(m, db, redis, manifest, [])

    def test_foreign_journaled_event_refused(self):
        manifest, state, redis = self.redis_fixture()
        state['event_ids'] = ['101-1']
        redis.xrange.return_value = [('101-1', self.event | {'order_id': 'f' * 32})]
        with self.assertRaises(acceptance.base.AcceptanceFailure):
            acceptance._redis_preflight(None, None, redis, manifest, [])
        redis.xdel.assert_not_called()

    def test_unrelated_stream_rows_untouched(self):
        manifest, state, redis = self.redis_fixture()
        redis.xrange.return_value = [('101-1', {'order_id': 'f' * 32})]
        _, events = acceptance._redis_preflight(None, None, redis, manifest, [])
        self.assertEqual(events, [])
        redis.xdel.assert_not_called()

    def test_incomplete_roots_refused(self):
        manifest, state, _ = self.redis_fixture()
        state['local_flow_done'] = True
        model = SimpleNamespace(id=MagicMock(), __tablename__='orders')
        m = SimpleNamespace(Order=model, Refund=model)
        db = MagicMock()
        db.scalars.return_value.all.return_value = []
        with patch('sqlalchemy.select'), self.assertRaises(acceptance.base.AcceptanceFailure):
            acceptance._roots(m, db, manifest, {})

    def test_collect_failure_prevents_external_io(self):
        import r3_native_refund_cleanup as database
        manifest = acceptance.base.fixture_ids('qa-vcs-0123456789ab')
        manifest.update(authorized=True, environment='owned')
        m = MagicMock()
        with patch.object(acceptance, '_journal_sla', return_value=False), \
                patch.object(acceptance, '_probe_preflight', return_value=[]), \
                patch.object(acceptance, '_verify_sla'), \
                patch.object(acceptance.base, 'read_manifest', return_value=manifest), \
                patch.object(acceptance.base, 'environment_fingerprint', return_value='owned'), \
                patch.object(database, 'collect', side_effect=RuntimeError('private')), \
                patch.object(acceptance.base, 'apisix_admin') as admin:
            with self.assertRaises(RuntimeError):
                acceptance.cleanup(m, manifest, '/unused')
            admin.assert_not_called()
            m.apisix_policy_redis.assert_not_called()

    def test_unknown_route_prevents_deletes(self):
        import r3_native_refund_cleanup as database
        manifest = acceptance.base.fixture_ids('qa-vcs-0123456789ab')
        manifest.update(authorized=True, environment='owned')
        manifest['external']['route_keys'] = ['foreign-route']
        m = MagicMock()
        with patch.object(acceptance, '_journal_sla', return_value=False), \
                patch.object(acceptance, '_probe_preflight', return_value=[]), \
                patch.object(acceptance, '_verify_sla'), \
                patch.object(acceptance.base, 'read_manifest', return_value=manifest), \
                patch.object(acceptance.base, 'environment_fingerprint', return_value='owned'), \
                patch.object(database, 'collect', return_value={}), \
                patch.object(acceptance, '_roots', return_value=[]), \
                patch.object(acceptance, '_redis_preflight', return_value=(set(), [])), \
                patch.object(acceptance.base, 'apisix_admin') as admin:
            with self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.cleanup(m, manifest, '/unused')
            admin.assert_not_called()
            m.apisix_policy_redis.return_value.delete.assert_not_called()

    def test_cleanup_drains_before_locks_and_rechecks_without_waiting(self):
        import r3_native_refund_cleanup as database
        manifest = acceptance.base.fixture_ids('qa-vcs-0123456789ab')
        manifest.update(authorized=True, environment='owned')
        manifest['external']['route_keys'] = [manifest['routes']['api']['source']]
        m, calls = MagicMock(), []
        def collect(*args, lock=False):
            calls.append('lock' if lock else 'collect')
            return {'orders': {(self.order,)}}
        def roots(*args):
            calls.append('roots')
            return []
        def redis_preflight(*args, wait_for_cursor=True):
            calls.append('drain' if wait_for_cursor else 'verify_no_wait')
            return set(), []
        def admin(*args, **kwargs):
            if args[1] == 'DELETE':
                calls.append('delete')
                raise acceptance.base.AcceptanceFailure('stop_at_first_delete')
            return None
        with patch.object(acceptance, '_journal_sla', return_value=False), \
                patch.object(acceptance, '_probe_preflight', return_value=[]), \
                patch.object(acceptance, '_verify_sla'), \
                patch.object(acceptance.base, 'read_manifest', return_value=manifest), \
                patch.object(acceptance.base, 'environment_fingerprint', return_value='owned'), \
                patch.object(database, 'collect', side_effect=collect), \
                patch.object(acceptance, '_roots', side_effect=roots), \
                patch.object(acceptance, '_redis_preflight', side_effect=redis_preflight), \
                patch.object(acceptance.base, 'write_manifest'), \
                patch.object(acceptance.base, 'apisix_admin', side_effect=admin):
            with self.assertRaisesRegex(acceptance.base.AcceptanceFailure, 'stop_at_first_delete'):
                acceptance.cleanup(m, manifest, '/unused')
        self.assertEqual(calls, ['collect', 'roots', 'drain', 'lock', 'roots',
                                 'verify_no_wait', 'delete'])

    def test_locked_cursor_check_never_waits_or_mutates_cursor(self):
        manifest, state, redis = self.redis_fixture()
        redis.xrange.return_value = [('101-1', self.event)]
        redis.info.return_value = {'run_id': state['source_run_id']}
        redis.hget.return_value = state['source_run_id']
        cursor = SimpleNamespace(redis_run_id=state['source_run_id'], last_id='100-0')
        m, db = MagicMock(), MagicMock()
        db.get.return_value = cursor
        with patch.object(acceptance.time, 'sleep') as sleep:
            with self.assertRaisesRegex(acceptance.base.AcceptanceFailure, 'ledger_cursor_not_drained'):
                acceptance._redis_preflight(m, db, redis, manifest, [], wait_for_cursor=False)
            sleep.assert_not_called()
        self.assertEqual(cursor.last_id, '100-0')
        db.get.assert_called_once_with(m.api_charge_ledger['Cursor'], acceptance.STREAM)
        db.commit.assert_not_called()
        db.execute.assert_not_called()

    def test_graph_drift_after_drain_prevents_deletes(self):
        import r3_native_refund_cleanup as database
        manifest = acceptance.base.fixture_ids('qa-vcs-0123456789ab')
        manifest.update(authorized=True, environment='owned')
        m = MagicMock()
        with patch.object(acceptance, '_journal_sla', return_value=False), \
                patch.object(acceptance, '_probe_preflight', return_value=[]), \
                patch.object(acceptance, '_verify_sla'), \
                patch.object(acceptance.base, 'read_manifest', return_value=manifest), \
                patch.object(acceptance.base, 'environment_fingerprint', return_value='owned'), \
                patch.object(database, 'collect', side_effect=[{}, {'orders': {(self.order,)}}]), \
                patch.object(acceptance, '_roots', return_value=[]), \
                patch.object(acceptance, '_redis_preflight', return_value=(set(), [])) as preflight, \
                patch.object(acceptance.base, 'apisix_admin') as admin:
            with self.assertRaisesRegex(acceptance.base.AcceptanceFailure, 'cleanup_graph_changed_under_locks'):
                acceptance.cleanup(m, manifest, '/unused')
            preflight.assert_called_once()
            admin.assert_not_called()
            m.apisix_policy_redis.return_value.delete.assert_not_called()

    def test_only_exact_derived_extra_keys(self):
        keys = acceptance.extra_keys(combination_id=self.combo, generation=1,
                                     period_index=0, refund_id=self.refund)
        self.assertEqual(keys, {
            acceptance.COVERAGE + self.combo + ':' + self.combo + '%3A1%3A0',
            acceptance.INTENT + self.refund})
        self.assertNotIn(acceptance.STREAM, keys)
        for updates in ({'period_index': 1}, {'generation': True},
                        {'refund_id': '*'}, {'combination_id': '../unowned'}):
            with self.subTest(updates=updates), self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.extra_keys(**(dict(combination_id=self.combo, generation=1,
                                              period_index=0, refund_id=self.refund) | updates))

    def test_exact_native_charge(self):
        self.assertEqual(acceptance.validate_charge('101-1', self.event, **self.context), '101-1')

    def test_charge_rejects_cross_owner_source_and_extra_fields(self):
        for updates in ({'order_id': 'f' * 32}, {'segment_id': 'f' * 32},
                        {'scope': 'f' * 32}, {'period_key': self.combo + ':1:1'},
                        {'route_key': 'unowned'}, {'source_run_id': 'f' * 40},
                        {'quantity': '2'}, {'charged_at_us': '0'}, {'extra': 'data'}):
            with self.subTest(updates=updates), self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.validate_charge('101-1', self.event | updates, **self.context)

    def test_charge_window_is_exclusive_start_inclusive_end(self):
        for event_id in ('100-0', '101-3', '99999999999999999999-0', '01-0', '*'):
            with self.subTest(event_id=event_id), self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.validate_charge(event_id, self.event, **self.context)
        acceptance.validate_charge('101-2', self.event, **self.context)

    def test_fk_checks_include_both_ends_and_nullable_parent(self):
        owned = {'refunds': {(self.refund,)}, 'orders': {(self.order,)}}
        edge = ('refunds', (self.refund,), 'orders', (self.order,))
        acceptance.prove_fk_edges(owned, [edge, ('refunds', (self.refund,), 'tickets', None)])
        for bad in (('refunds', ('unowned',), 'orders', (self.order,)),
                    ('refunds', (self.refund,), 'orders', ('unowned',)),
                    ('refunds', (self.refund,), 'users', ('admin',))):
            with self.subTest(edge=bad), self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.prove_fk_edges(owned, [bad])

    def test_unknown_financial_tables_refused(self):
        acceptance.validate_financial_tables({'refunds', 'ordinary_refund_executions'})
        with self.assertRaises(acceptance.base.AcceptanceFailure):
            acceptance.validate_financial_tables({'refunds', 'ordinary_refund_settlement_links'})

    def test_refund_audit_exact_scope_and_actor(self):
        roots = dict(refund_id=self.refund, order_id=self.order,
                     buyer_id='qa-vcs-0123456789ab-u-buyer', operator_id='qa-vcs-0123456789ab-u-delivery')
        row = dict(target_type='refund', target_id=self.refund, order_id=self.order,
                   category='refund', business_domain='refund', action='ordinary_refund_completed',
                   actor='refund-worker')
        acceptance.validate_refund_audit(row, **roots)
        for update in ({'actor': 'production-admin'}, {'order_id': 'f' * 32},
                       {'target_id': 'f' * 32}, {'category': 'order'}, {'action': 'delete'}):
            with self.subTest(update=update), self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.validate_refund_audit(row | update, **roots)


class CleanupRetry(unittest.TestCase):
    def setUp(self):
        self.manifest = acceptance.base.fixture_ids('qa-vcs-0123456789ab')
        self.state = {'external_delete_phase': None}
        self.manifest['database_ids']['native_refund'] = self.state

    def test_exact_guard_labels_retry_then_return_result(self):
        import version_change_quote_acceptance as guarded
        for kind in (acceptance.base.AcceptanceFailure, guarded.AcceptanceFailure):
            for label in ('retained_sla_set_mismatch', 'retained_sla_facts_changed'):
                with self.subTest(kind=kind, label=label), \
                        patch.object(acceptance, 'cleanup', side_effect=[kind(label), {'retained_sla_count': 3}]) as cleanup, \
                        patch.object(acceptance.time, 'sleep') as sleep:
                    self.assertEqual(acceptance.cleanup_with_predelete_retry(
                        None, self.manifest, '/unused'), {'retained_sla_count': 3})
                    self.assertEqual(cleanup.call_count, 2)
                    sleep.assert_called_once_with(5)

    def test_three_total_attempts_and_original_error_propagates(self):
        error = acceptance.base.AcceptanceFailure('retained_sla_set_mismatch')
        with patch.object(acceptance, 'cleanup', side_effect=error) as cleanup, \
                patch.object(acceptance.time, 'sleep') as sleep:
            with self.assertRaises(acceptance.base.AcceptanceFailure) as caught:
                acceptance.cleanup_with_predelete_retry(None, self.manifest, '/unused')
            self.assertIs(caught.exception, error)
            self.assertEqual(cleanup.call_count, 3)
            self.assertEqual([call.args for call in sleep.call_args_list], [(5,), (5,)])

    def test_other_exceptions_and_nonexact_labels_never_retry(self):
        for error in (RuntimeError('retained_sla_set_mismatch'),
                      acceptance.base.AcceptanceFailure('retained_sla_set_mismatch:extra'),
                      acceptance.base.AcceptanceFailure('retained_sla_set_mismatch '),
                      acceptance.base.AcceptanceFailure('retained_sla_fact_changed'),
                      acceptance.base.AcceptanceFailure('foreign_probe_consumer')):
            with self.subTest(error=type(error).__name__, label=str(error)), \
                    patch.object(acceptance, 'cleanup', side_effect=error) as cleanup, \
                    patch.object(acceptance.time, 'sleep') as sleep:
                with self.assertRaises(type(error)) as caught:
                    acceptance.cleanup_with_predelete_retry(None, self.manifest, '/unused')
                self.assertIs(caught.exception, error)
                cleanup.assert_called_once()
                sleep.assert_not_called()

    def test_delete_phase_forbids_retry(self):
        self.state['external_delete_phase'] = True
        with patch.object(acceptance, 'cleanup', side_effect=acceptance.base.AcceptanceFailure(
                'retained_sla_facts_changed')) as cleanup, patch.object(acceptance.time, 'sleep') as sleep:
            with self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.cleanup_with_predelete_retry(None, self.manifest, '/unused')
            cleanup.assert_called_once()
            sleep.assert_not_called()

    def test_phase_change_during_delay_forbids_next_attempt(self):
        with patch.object(acceptance, 'cleanup', side_effect=acceptance.base.AcceptanceFailure(
                'retained_sla_facts_changed')) as cleanup, \
                patch.object(acceptance.time, 'sleep', side_effect=lambda _: self.state.update(
                    external_delete_phase=True)):
            with self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.cleanup_with_predelete_retry(None, self.manifest, '/unused')
            cleanup.assert_called_once()


class ProbeConsumers(unittest.TestCase):
    def setUp(self):
        self.manifest = acceptance.base.fixture_ids('qa-vcs-0123456789ab')
        self.manifest['database_ids']['native_refund'] = {}
        self.m = SimpleNamespace(__package__='app')
        self.item = dict(name='market-sla-probe-' + 'a' * 32, route_id='b' * 32,
                         product_id=self.manifest['products']['api'],
                         version_id=self.manifest['versions']['api']['source'],
                         route_key=self.manifest['routes']['api']['source'])
        self.runtime = SimpleNamespace(
            probe_consumer_name=MagicMock(return_value=self.item['name']),
            probe_consumer_key=MagicMock(return_value='derived-test-key'))
        self.module = patch.object(acceptance.importlib, 'import_module', return_value=self.runtime)
        self.module.start()
        self.addCleanup(self.module.stop)

    def test_exact_live_probe_and_no_key_in_journal(self):
        live = {'username': self.item['name'], 'plugins': {'key-auth': {'key': 'derived-test-key'}}}
        with patch.object(acceptance.base, 'apisix_admin', return_value=live):
            self.assertEqual(acceptance._probe_live(self.m, self.manifest, self.item), live)
        self.runtime.probe_consumer_name.assert_called_with(
            self.item['route_id'], self.item['product_id'], self.item['version_id'])
        self.assertNotIn('key', self.item)

    def test_probe_refuses_wrong_key_plugins_or_name(self):
        for live in ({'username': self.item['name'], 'plugins': {'key-auth': {'key': 'foreign'}}},
                     {'username': 'foreign', 'plugins': {'key-auth': {'key': 'derived-test-key'}}},
                     {'username': self.item['name'], 'plugins': {
                         'key-auth': {'key': 'derived-test-key'}, 'other-plugin': {}}}):
            with self.subTest(live=live), \
                    patch.object(acceptance.base, 'apisix_admin', return_value=live), \
                    self.assertRaisesRegex(acceptance.base.AcceptanceFailure, 'foreign_probe_consumer'):
                acceptance._probe_live(self.m, self.manifest, self.item)

    def test_probe_absence_does_not_require_secret(self):
        with patch.object(acceptance.base, 'apisix_admin', return_value=None):
            self.assertIsNone(acceptance._probe_live(self.m, self.manifest, self.item))
        self.runtime.probe_consumer_key.assert_not_called()

    def test_final_verify_refuses_remaining_probe(self):
        self.manifest['database_ids']['cleanup_rows'] = {}
        self.manifest['database_ids']['native_refund']['probe_consumers'] = [self.item]
        with patch.object(acceptance, '_verify_sla'), patch.object(acceptance, '_protected'), \
                patch.object(acceptance.base, 'apisix_admin', return_value={'username': self.item['name']}):
            with self.assertRaisesRegex(acceptance.base.AcceptanceFailure, 'probe_consumer_remains'):
                acceptance._verify_finished(self.m, MagicMock(), self.manifest)


class RetainedSlaFacts(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import Column, MetaData, String, Table, create_engine
        from sqlalchemy.orm import Session
        self.metadata = MetaData()
        self.table = Table('sla_gateway_terminal_events', self.metadata,
                           *(Column(k, String, primary_key=k == 'event_id')
                             for k in acceptance.SLA_FIELDS))
        self.engine = create_engine('sqlite://')
        self.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.manifest = acceptance.base.fixture_ids('qa-vcs-0123456789ab')
        self.state = {}
        self.manifest['database_ids']['native_refund'] = self.state
        self.m = SimpleNamespace(Base=SimpleNamespace(metadata=self.metadata),
            Product=SimpleNamespace(__tablename__='products'),
            ProductReleaseVersion=SimpleNamespace(__tablename__='product_release_versions'))
        self.fact = dict(event_id='gw_' + 'a' * 32, content_sha256='b' * 64,
            product_id=self.manifest['products']['api'],
            version_id=self.manifest['versions']['api']['source'],
            route_id=self.manifest['routes']['api']['source'])
        self.owned = {'products': {(self.fact['product_id'],)},
                      'product_release_versions': {(self.fact['version_id'],)}}

    def insert(self, **updates):
        self.db.execute(self.table.insert().values(self.fact | updates))

    def test_snapshot_matches_collector_and_keeps_facts(self):
        import r3_native_refund_cleanup as database
        self.insert()
        with patch('version_change_quote_acceptance.owned_rows', return_value=self.owned), \
                patch.object(acceptance.base, 'write_manifest') as write:
            self.assertTrue(acceptance._journal_sla(self.m, self.db, self.manifest, '/unused'))
            write.assert_called_once()
        self.assertEqual(self.state['retained_sla_facts'], [self.fact])
        database.retained_sla(self.m, self.db, self.manifest, [])
        acceptance._verify_sla(self.m, self.db, self.manifest)
        result = acceptance._cleanup_result(self.manifest, {'orders': 1})
        self.assertEqual(result['retained_sla_count'], 1)
        self.assertTrue(result['not_tested'])

    def test_late_facts_refresh_only_before_delete(self):
        self.insert()
        with patch('version_change_quote_acceptance.owned_rows', return_value=self.owned), \
                patch.object(acceptance.base, 'write_manifest'):
            acceptance._journal_sla(self.m, self.db, self.manifest, '/unused')
            self.insert(event_id='gw_' + 'c' * 32)
            with self.assertRaisesRegex(acceptance.base.AcceptanceFailure, 'retained_sla_facts_changed'):
                acceptance._verify_sla(self.m, self.db, self.manifest)
            acceptance._journal_sla(self.m, self.db, self.manifest, '/unused')
            self.assertEqual(len(self.state['retained_sla_facts']), 2)
            self.state['external_delete_phase'] = True
            self.insert(event_id='gw_' + 'd' * 32)
            with self.assertRaisesRegex(acceptance.base.AcceptanceFailure, 'after_delete_phase'):
                acceptance._journal_sla(self.m, self.db, self.manifest, '/unused')

    def test_snapshot_rejects_wrong_version_route_pair(self):
        self.insert(route_id=self.manifest['routes']['api']['target'])
        with self.assertRaisesRegex(acceptance.base.AcceptanceFailure, 'ownership_mismatch'):
            acceptance._sla_facts(self.m, self.db, self.manifest)

    def test_snapshot_rejects_unproven_roots(self):
        self.insert()
        with patch('version_change_quote_acceptance.owned_rows', return_value={}), \
                patch.object(acceptance.base, 'write_manifest') as write:
            with self.assertRaisesRegex(acceptance.base.AcceptanceFailure, 'roots_unproven'):
                acceptance._journal_sla(self.m, self.db, self.manifest, '/unused')
            write.assert_not_called()

    def test_snapshot_scoped_and_bounded(self):
        self.insert(product_id='foreign')
        self.assertEqual(acceptance._sla_facts(self.m, self.db, self.manifest), [])
        self.db.execute(self.table.insert(), [self.fact | {'event_id': 'gw_' + format(i, '032x')}
                                             for i in range(201)])
        with self.assertRaisesRegex(acceptance.base.AcceptanceFailure, 'bound_exceeded'):
            acceptance._sla_facts(self.m, self.db, self.manifest)

    def test_final_verify_rejects_deleted_or_changed_fact(self):
        self.insert()
        self.state['retained_sla_facts'] = [self.fact]
        self.db.execute(self.table.update().values(content_sha256='c' * 64))
        with self.assertRaisesRegex(acceptance.base.AcceptanceFailure, 'facts_changed'):
            acceptance._verify_sla(self.m, self.db, self.manifest)
        self.db.execute(self.table.delete())
        with self.assertRaisesRegex(acceptance.base.AcceptanceFailure, 'facts_changed'):
            acceptance._verify_sla(self.m, self.db, self.manifest)


if __name__ == '__main__':
    unittest.main()
