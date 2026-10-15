"""Actual Redis/Lua debit evidence and isolated database ingestion boundaries."""
from datetime import timedelta
import unittest
import json
from unittest.mock import Mock, patch

from sqlalchemy import select

from app.api_charge_ledger import STREAM, EPOCH, POLICY, event_values, period_keys, microseconds
import test_refund_planning as planning
import test_subscription_quota as native


class ChargeLedgerTests(unittest.TestCase):
    setUpClass = classmethod(planning.RefundPlanningHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(planning.RefundPlanningHTTPTests.tearDownClass.__func__)
    tearDown = planning.RefundPlanningHTTPTests.tearDown
    request = planning.RefundPlanningHTTPTests.request
    create_order = planning.RefundPlanningHTTPTests.create_order
    monthly = planning.RefundPlanningHTTPTests.monthly

    def setUp(self):
        planning.RefundPlanningHTTPTests.setUp(self)
        self.monthly()
        self.hooks = self.m.api_charge_ledger
        self.ledger_client = Mock()
        self.ledger_client.hget.return_value = '0'
        self.ledger_client.hget.side_effect = lambda key, field: ('a' * 40 if field == 'run_id'
            else self.ledger_client.hget.return_value)
        self.ledger_client.get.return_value = '1'
        self.ledger_client.info.return_value = {'run_id': 'a' * 40}
        self.event = dict(order_id=self.order_id, segment_id='refund-segment', scope='refund-combo',
            period_key='refund-combo:1:0', route_key='route', quantity='1', policy=POLICY, source_run_id='a' * 40,
            charged_at_us=str(int((self.at - timedelta(seconds=1)).timestamp() * 1000000)))
        self.ledger_client.xread.return_value = [(STREAM, [('1-0', self.event)])]
        transport = patch.object(self.m, 'apisix_policy_redis', return_value=self.ledger_client)
        transport.start()
        self.addCleanup(transport.stop)
        with self.m.SessionLocal() as db, patch.object(self.m, 'now', return_value=self.at - timedelta(days=15)):
            with patch.object(self.ledger_client, 'xread', return_value=[]):
                self.hooks['ingest'](db, self.ledger_client)
            db.commit()

    def summary(self, db):
        return self.hooks['summary'](db, order_id=self.order_id, segment_id='refund-segment',
            start=self.at - timedelta(days=14), end=self.at, as_of=self.at)

    def test_ingest_and_cursor_are_idempotent_with_exact_segment_summary(self):
        with self.m.SessionLocal() as db:
            self.assertEqual(self.hooks['ingest'](db, self.ledger_client)['inserted'], 1)
            db.commit()
            self.assertEqual(self.hooks['ingest'](db, self.ledger_client)['inserted'], 0)
            summary = self.summary(db)
            self.assertEqual((summary.charged_calls, summary.complete, summary.segment_id), (1, True, 'refund-segment'))
            self.assertEqual(db.query(self.hooks['Charge']).count(), 1)

    def test_unknown_baseline_and_unsynchronized_counter_never_mean_zero(self):
        with self.m.SessionLocal() as db:
            for baseline, current in ((None, None), ('5', '5'), ('0', '1')):
                self.ledger_client.hget.return_value, self.ledger_client.get.return_value = baseline, current
                with self.assertRaises(self.module.PlanUnavailable):
                    self.summary(db)

    def test_tracked_zero_counter_is_distinct_from_missing_evidence(self):
        self.ledger_client.get.return_value = None
        with self.m.SessionLocal() as db:
            self.assertEqual(self.summary(db).charged_calls, 0)
            self.ledger_client.hget.return_value = None
            with self.assertRaises(self.module.PlanUnavailable):
                self.summary(db)

    def test_bad_second_event_rolls_back_facts_and_watermark(self):
        self.ledger_client.xread.return_value[0][1].append(('2-0', dict(self.event, order_id='other')))
        with self.m.SessionLocal() as db:
            with self.assertRaises(ValueError):
                self.hooks['ingest'](db, self.ledger_client)
            db.rollback()
            self.assertEqual(db.query(self.hooks['Charge']).count(), 0)
            self.assertEqual(db.query(self.hooks['Cursor']).count(), 1)
            self.assertEqual(db.scalar(select(self.hooks['Cursor'])).last_id, '0-0')

    def test_existing_financial_event_cannot_change_identity_or_period(self):
        with self.m.SessionLocal() as db:
            self.hooks['ingest'](db, self.ledger_client)
            db.commit()
            self.event['route_key'] = 'rewritten'
            with self.assertRaises(ValueError):
                self.hooks['ingest'](db, self.ledger_client)
            db.rollback()
            self.event['period_key'] = 'other-period'
            with self.assertRaises(ValueError):
                self.hooks['ingest'](db, self.ledger_client)

    def test_foreign_order_segment_cannot_be_summarized(self):
        with self.m.SessionLocal() as db:
            with self.assertRaises(self.module.PlanUnavailable):
                self.hooks['summary'](db, order_id='other', segment_id='refund-segment',
                    start=self.at - timedelta(days=14), end=self.at, as_of=self.at)

    def test_event_has_no_buyer_key_or_upstream_error_dependency(self):
        self.assertEqual(event_values('1-0', self.event)['quantity'], 1)
        with self.assertRaises(ValueError):
            event_values('1-0', dict(self.event, secret='forbidden'))

    def test_financial_time_boundaries_preserve_single_microseconds(self):
        self.assertEqual(microseconds(self.at + timedelta(microseconds=1)) - microseconds(self.at), 1)
        self.assertEqual(microseconds(self.at) - microseconds(self.at - timedelta(microseconds=1)), 1)

    def test_redis_restart_cannot_make_missing_usage_look_like_zero(self):
        self.ledger_client.info.return_value = {'run_id': 'b' * 40}
        self.ledger_client.get.return_value = None
        with self.m.SessionLocal() as db:
            with self.assertRaises(self.module.PlanUnavailable):
                self.summary(db)
            with self.assertRaises(ValueError):
                self.hooks['ingest'](db, self.ledger_client)

    def test_preledger_order_never_gets_fabricated_complete_history(self):
        self.ledger_client.get.return_value = None
        with self.m.SessionLocal() as db:
            db.scalar(select(self.hooks['Cursor'])).activated_at = self.at
            db.commit()
            with self.assertRaises(self.module.PlanUnavailable):
                self.summary(db)

    def test_future_empty_window_requires_zero_tracked_counters_and_no_facts(self):
        with self.m.SessionLocal() as db:
            term = db.get(self.m.subscription_terms['Term'], 'refund-segment')
            term.starts_at = self.at + timedelta(days=1)
            term.ends_at = self.at + timedelta(days=32)
            db.commit()
            self.ledger_client.get.return_value = None
            window = dict(order_id=self.order_id, segment_id=term.id, start=term.starts_at,
                          end=term.starts_at, as_of=self.at)
            value = self.hooks['summary'](db, **window)
            self.assertEqual((value.complete, value.charged_calls), (True, 0))
            self.ledger_client.get.return_value = '1'
            with self.assertRaises(self.module.PlanUnavailable):
                self.hooks['summary'](db, **window)
            self.ledger_client.get.return_value = None
            self.ledger_client.hget.return_value = None
            with self.assertRaises(self.module.PlanUnavailable):
                self.hooks['summary'](db, **window)

    def test_future_quote_uses_real_adapter_not_an_invented_zero_usage_stub(self):
        with self.m.SessionLocal() as db:
            term = db.get(self.m.subscription_terms['Term'], 'refund-segment')
            term.starts_at = self.at + timedelta(days=1)
            term.ends_at = self.at + timedelta(days=32)
            db.commit()
        self.ledger_client.get.return_value = None
        with patch.object(self.m, 'refund_charge_summary', self.hooks['summary']):
            quote = self.request('POST', f'/api/orders/{self.order_id}/refund-quote', json={'amount': '100'})
            self.assertEqual(quote['status'], 'available')
            self.assertEqual(quote['item']['charged_calls'], 0)
            self.assertEqual(quote['item']['recovery_delta'], 100)
            self.ledger_client.hget.return_value = None
            missing = self.request('POST', f'/api/orders/{self.order_id}/refund-quote', json={'amount': '100'})
            self.assertEqual(missing['status'], 'unavailable')

    def test_deleted_source_marker_requires_review_even_without_redis_restart(self):
        self.ledger_client.hget.side_effect = lambda key, field: None if field == 'run_id' else '0'
        self.ledger_client.get.return_value = None
        with self.m.SessionLocal() as db:
            with self.assertRaises(self.module.PlanUnavailable):
                self.summary(db)
            with self.assertRaises(ValueError):
                self.hooks['ingest'](db, self.ledger_client)

    def test_source_loss_during_counter_read_cannot_return_complete(self):
        marker = {'present': True}
        self.ledger_client.hget.side_effect = lambda key, field: (
            ('a' * 40 if marker['present'] else None) if field == 'run_id' else '0')
        self.ledger_client.get.side_effect = lambda key: marker.update(present=False)
        with self.m.SessionLocal() as db:
            with self.assertRaises(self.module.PlanUnavailable):
                self.summary(db)

    def test_old_generation_event_uses_its_original_anchor_after_repurchase(self):
        with self.m.SessionLocal() as db:
            combo = db.get(self.m.subscription_terms['Combination'], 'refund-combo')
            combo.anchor_at = self.at + timedelta(days=365)
            combo.generation = 2
            db.commit()
            self.assertEqual(self.hooks['ingest'](db, self.ledger_client)['inserted'], 1)
            db.commit()
            self.assertEqual(self.summary(db).charged_calls, 1)

    @unittest.skipUnless(native.redis and native.LuaRuntime, 'requires real Redis/Lua')
    def test_real_native_debit_ingests_then_drives_scoped_refund_quote(self):
        owned = type('OwnedFinancialRedis', (), {})
        native.SubscriptionQuotaTests.setUpClass.__func__(owned)
        self.addCleanup(lambda: native.SubscriptionQuotaTests.tearDownClass.__func__(owned))
        client = owned.client
        client.hset(EPOCH, mapping={'run_id': client.info('server')['run_id']})
        with self.m.SessionLocal() as db:
            db.scalar(select(self.hooks['Cursor'])).redis_run_id = client.info('server')['run_id']
            db.commit()
        now = int(self.at.timestamp()) - 1
        period = dict(start_at=int((self.at - timedelta(days=14)).timestamp()),
            end_at=int((self.at + timedelta(days=17)).timestamp()), period_key='refund-combo:1:0',
            order_id=self.order_id, segment_id='refund-segment', route_key='route',
            rate_limit_per_minute=60, daily_quota=0, monthly_quota=100)
        client.hset('market:apisix:credential:consumer', mapping=dict(mode='subscription', status='active',
            quota_scope='refund-combo', subscription_periods=json.dumps([period])))
        self.assertEqual(native.Gateway(client, now).request(), 200)
        self.assertEqual(client.xlen(STREAM), 1)
        with patch.object(self.m, 'apisix_policy_redis', return_value=client), \
                patch.object(self.m, 'refund_charge_summary', self.hooks['summary']):
            before = self.request('POST', f'/api/orders/{self.order_id}/refund-quote', json={'amount': '20'})
            self.assertEqual(before['status'], 'unavailable')
            with self.m.SessionLocal() as db:
                self.hooks['ingest'](db, client)
                db.commit()
            quote = self.request('POST', f'/api/orders/{self.order_id}/refund-quote', json={'amount': '20'})
            self.assertEqual(quote['status'], 'available')
            self.assertEqual(quote['item']['charged_calls'], 1)
            self.assertEqual(quote['item']['original_quota'], 100)
            self.assertEqual(quote['item']['recovery_delta'], 20)


@unittest.skipUnless(native.redis and native.LuaRuntime, 'requires real Redis and Lua runtime')
class NativeChargeTests(unittest.TestCase):
    setUpClass = classmethod(native.SubscriptionQuotaTests.setUpClass.__func__)
    tearDownClass = classmethod(native.SubscriptionQuotaTests.tearDownClass.__func__)
    publish = native.SubscriptionQuotaTests.publish
    gateway = native.SubscriptionQuotaTests.gateway
    usage = native.SubscriptionQuotaTests.usage

    def setUp(self):
        native.SubscriptionQuotaTests.setUp(self)
        self.client.hset(EPOCH, mapping={'run_id': self.client.info('server')['run_id']})
        self.period.update(order_id='order', segment_id='segment')
        self.publish()

    def test_accepted_debit_emits_once_before_upstream_and_rejection_emits_nothing(self):
        self.period['monthly_quota'] = 1
        self.publish()
        gateway = self.gateway()
        self.assertEqual(gateway.request(), 200)
        self.assertEqual(gateway.request(), 429)
        events = self.client.xrange(STREAM)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1]['order_id'], 'order')
        self.assertEqual(events[0][1]['quantity'], '1')
        self.assertEqual(self.usage(), [1, 1, 1])

    def test_corrupt_stream_rejects_without_counting(self):
        self.client.set(STREAM, 'not-a-stream')
        self.assertEqual(self.gateway().request(), 503)
        self.assertEqual(self.usage(), [0, 0, 0])

    def test_preexisting_usage_is_not_mislabeled_complete(self):
        count_key, coverage_key = period_keys('combo', self.period['period_key'])
        self.client.set(count_key, '5')
        self.assertEqual(self.gateway().request(), 200)
        self.assertEqual(self.client.hget(coverage_key, 'baseline'), '5')

    def test_rotated_credentials_share_count_but_each_debit_has_one_event(self):
        self.publish(consumer='rotated')
        gateway = self.gateway()
        self.assertEqual(gateway.request(), 200)
        self.assertEqual(gateway.request(consumer='rotated'), 200)
        self.assertEqual(len(self.client.xrange(STREAM)), 2)
        self.assertEqual(self.usage(), [2, 2, 2])

    def test_malformed_attribution_does_not_debit(self):
        self.period['segment_id'] = ''
        self.publish()
        self.assertEqual(self.gateway().request(), 403)
        self.assertEqual(self.client.xlen(STREAM), 0)
        self.assertEqual(self.usage(), [0, 0, 0])

    def test_source_marker_loss_rejects_without_any_debit(self):
        self.client.delete(EPOCH)
        self.assertEqual(self.gateway().request(), 503)
        self.assertEqual(self.client.xlen(STREAM), 0)
        self.assertEqual(self.usage(), [0, 0, 0])

    def test_acl_denied_later_write_does_not_leave_a_false_charge(self):
        for command in ('incr', 'hsetnx', 'expireat', 'persist'):
            username = 'ledger-' + command
            self.client.execute_command('ACL', 'SETUSER', username, 'on', '>test-only', '~*', '+@all', '-' + command)
            restricted = native.redis.Redis(unix_socket_path=self.client.connection_pool.connection_kwargs['path'],
                username=username, password='test-only', decode_responses=True)
            try:
                self.assertEqual(native.Gateway(restricted, self.now).request(), 503)
                self.assertEqual(self.client.xlen(STREAM), 0)
                self.assertEqual(self.usage(), [0, 0, 0])
            finally:
                restricted.close()

    def test_period_selection_and_event_timestamp_use_one_clock_read(self):
        gateway = self.gateway()
        gateway.lua.execute("ngx.now = function() return clock() + 1.1 end")
        self.period['end_at'] = self.now + 1
        self.publish()
        self.assertEqual(gateway.request(), 403)
        self.assertEqual(self.client.xlen(STREAM), 0)
        self.assertEqual(self.usage(), [0, 0, 0])
