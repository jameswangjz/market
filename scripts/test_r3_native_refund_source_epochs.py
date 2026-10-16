"""Pure native acceptance source guards; no Redis or application startup."""
import hashlib
import importlib.util
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import r3_native_refund_runtime_acceptance as native


class NativeSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = 'a' * 40
        self.physical = 'b' * 40
        self.state = dict(source_run_id=self.source, physical_run_id=self.physical,
                          source_stream=native.STREAM + ':' + self.source)
        self.event = '101-1'
        self.raw = dict(order_id='c' * 32, segment_id='d' * 32, scope='e' * 32,
                        period_key='e' * 32 + ':1:0', route_key='qa-vcs-0123456789ab-api-source',
                        source_run_id=self.source, quantity='1', policy='gateway_actual_charge_v1',
                        charged_at_us='1792100000000000')

    def row(self, **changes):
        return SimpleNamespace(**(dict(self.raw, id=native.charge_id(self.state, self.event),
                                       event_id=self.event) | changes))

    def fixture(self):
        manifest = native.base.fixture_ids('qa-vcs-0123456789ab')
        state = dict(self.state, order_ids=[self.raw['order_id']], refund_ids=[],
                     order_id=self.raw['order_id'], term_id=self.raw['segment_id'],
                     combination_id=self.raw['scope'], stream_start='100-0', stream_end='102-0',
                     event_ids=[self.event], extra_keys=[native.COVERAGE + self.raw['scope'] + ':' +
                         native.quota_token(self.raw['period_key'])])
        manifest['database_ids']['native_refund'] = state
        redis = Mock()
        redis.type.return_value = 'none'
        redis.info.return_value = {'run_id': self.physical}
        redis.hget.return_value = self.source
        redis.xrange.return_value = [(self.event, self.raw)]
        cursor = SimpleNamespace(redis_run_id=self.source, last_id='102-0')
        hook = Mock(return_value=(state['source_stream'], cursor))
        m = SimpleNamespace(api_charge_ledger={'source_context': hook, 'Charge': object})
        return manifest, state, redis, m, Mock(), hook

    def test_managed_hash_and_legacy_event_primary_keys(self):
        self.assertEqual(hashlib.sha256((self.source + '|' + self.event).encode()).hexdigest()[:40],
                         native.charge_id(self.state, self.event))
        self.assertEqual(self.event, native.charge_id({}, self.event))
        native.validate_charge_row(self.row(), self.state, self.event, self.raw)

    def test_stream_rejects_arbitrary_manifest_key_or_other_epoch(self):
        for stream in ('unrelated', native.STREAM + ':', native.STREAM + ':' + self.physical, None):
            with self.subTest(stream=stream), self.assertRaises(native.base.AcceptanceFailure):
                native.source_stream(self.state | {'source_stream': stream})

    def test_managed_stream_requires_separate_physical_identity(self):
        state = dict(self.state)
        del state['physical_run_id']
        with self.assertRaises(native.base.AcceptanceFailure):
            native.source_stream(state)

    def test_charge_row_requires_raw_event_and_hashed_primary_key(self):
        for changes in ({'event_id': '101-2'}, {'id': self.event}, {'source_run_id': self.physical},
                        {'order_id': 'f' * 32}, {'quantity': 2}):
            with self.subTest(changes=changes), self.assertRaises(native.base.AcceptanceFailure):
                native.validate_charge_row(self.row(**changes), self.state, self.event, self.raw)

    def test_managed_preflight_uses_hook_and_validated_stream(self):
        manifest, state, redis, m, db, hook = self.fixture()
        _, events = native._redis_preflight(m, db, redis, manifest, [], wait_for_cursor=False)
        self.assertEqual([self.event], events)
        redis.xrange.assert_called_once_with(state['source_stream'], min='(100-0', max='102-0', count=201)
        self.assertEqual(2, hook.call_count)
        db.commit.assert_not_called()
        redis.xdel.assert_not_called()

    def test_restart_rejected_even_when_financial_epoch_is_stable(self):
        manifest, _, redis, m, db, _ = self.fixture()
        redis.info.return_value = {'run_id': 'f' * 40}
        with self.assertRaises(native.base.AcceptanceFailure):
            native._redis_preflight(m, db, redis, manifest, [])
        redis.xrange.assert_not_called()

    def test_untrusted_stream_rejected_before_read(self):
        manifest, state, redis, m, db, _ = self.fixture()
        state['source_stream'] = 'foreign-stream'
        with self.assertRaises(native.base.AcceptanceFailure):
            native._redis_preflight(m, db, redis, manifest, [])
        redis.xrange.assert_not_called()

    def test_missing_event_proof_reads_managed_primary_key(self):
        manifest, state, redis, m, db, _ = self.fixture()
        redis.xrange.return_value = []
        state.update(external_delete_phase=True, validated_events={self.event: self.raw})
        db.get.return_value = self.row()
        _, events = native._redis_preflight(m, db, redis, manifest, [])
        self.assertEqual([], events)
        db.get.assert_called_once_with(object, native.charge_id(state, self.event))
        db.get.return_value.event_id = '101-2'
        with self.assertRaises(native.base.AcceptanceFailure):
            native._redis_preflight(m, db, redis, manifest, [])

    def test_hook_registration_failure_propagates(self):
        manifest, _, redis, m, db, hook = self.fixture()
        hook.side_effect = native.base.AcceptanceFailure('unregistered_process')
        with self.assertRaisesRegex(native.base.AcceptanceFailure, 'unregistered_process'):
            native._redis_preflight(m, db, redis, manifest, [])
        redis.xrange.assert_not_called()

    def test_legacy_source_context_preserves_restart_failure(self):
        db, redis = Mock(), Mock()
        db.get.return_value = SimpleNamespace(redis_run_id=self.source)
        m = SimpleNamespace(api_charge_ledger={'Cursor': object})
        redis.info.return_value = {'run_id': self.physical}
        with self.assertRaises(native.base.AcceptanceFailure):
            native.source_context(m, db, redis)


@unittest.skipUnless(importlib.util.find_spec('sqlalchemy'), 'SQLAlchemy required for owned graph tests')
class ManagedCleanupTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import Column, ForeignKey, String, Table
        from test_r3_native_refund_cleanup import CleanupTests
        self.fixture = CleanupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        m = self.fixture.m
        source = 'a' * 40
        oid, term, combo = 'c' * 32, 'd' * 32, 'e' * 32
        state = dict(source_run_id=source, source_stream=native.STREAM + ':' + source,
                     physical_run_id='b' * 40, event_ids=['101-1'], stream_start='100-0',
                     stream_end='102-0', order_id=oid, term_id=term, combination_id=combo)
        self.fixture.manifest['database_ids']['native_refund'] = state
        record = dict(order_id=oid, segment_id=term, scope=combo, period_key=combo + ':1:0',
                      route_key=self.fixture.manifest['routes']['api']['source'], source_run_id=source,
                      quantity='1', policy='gateway_actual_charge_v1', charged_at_us='1792100000000000')
        state['validated_events'] = {'101-1': record}
        self.table = Table('api_financial_charges', m.Base.metadata,
            Column('id', String, primary_key=True), Column('event_id', String),
            Column('order_id', ForeignKey('orders.id')),
            *(Column(key, String) for key in record if key != 'order_id'))
        self.table.create(self.fixture.engine)
        with m.SessionLocal.begin() as db:
            original = db.get(m.Order, 'order')
            db.add(m.Order(id=oid, order_no=oid, **{key: getattr(original, key) for key in (
                'product_id', 'product_version_id', 'buyer_user_id', 'buyer_enterprise_id',
                'provider_enterprise_id')}))
            db.flush()
            self.pk = native.charge_id(state, '101-1')
            db.execute(self.table.insert().values(id=self.pk, event_id='101-1', **record))

    def test_owned_graph_uses_hashed_primary_key_for_exact_delete(self):
        result = self.fixture.run_cleanup()
        self.assertEqual([[self.pk]], result['cleanup_rows']['api_financial_charges'])
        counts = self.fixture.run_cleanup(dry_run=False, journal_callback=lambda rows: None)
        self.assertEqual(1, counts['api_financial_charges'])

    def test_wrong_raw_event_refuses_delete(self):
        from sqlalchemy import select
        with self.fixture.engine.begin() as db:
            db.execute(self.table.update().values(event_id='101-2'))
        with self.assertRaises(native.base.AcceptanceFailure):
            self.fixture.run_cleanup(dry_run=False, journal_callback=lambda rows: self.fail('must not journal'))
        with self.fixture.engine.connect() as db:
            self.assertEqual(self.pk, db.scalar(select(self.table.c.id)))


if __name__ == '__main__':
    unittest.main()
