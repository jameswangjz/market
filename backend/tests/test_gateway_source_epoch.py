"""Managed source checks using the existing isolated Redis/Lua gateway harness."""
import unittest

import test_subscription_quota as quota


@unittest.skipUnless(quota.redis and quota.LuaRuntime, 'requires redis and lupa')
class GatewaySourceEpochTests(unittest.TestCase):
    # Reuse server lifecycle and fixtures, without inheriting unrelated tests.
    setUpClass = classmethod(quota.SubscriptionQuotaTests.setUpClass.__func__)
    tearDownClass = classmethod(quota.SubscriptionQuotaTests.tearDownClass.__func__)
    publish = quota.SubscriptionQuotaTests.publish
    gateway = quota.SubscriptionQuotaTests.gateway

    def setUp(self):
        quota.SubscriptionQuotaTests.setUp(self)
        self.period.update(order_id='order', segment_id='segment')
        self.publish()
        self.source = 'market:apisix:charges:source'
        self.run_id = 'a' * 40
        self.stream = 'market:apisix:charges:v1:' + self.run_id
        self.marker = dict(run_id=self.run_id, stream=self.stream, status='ready',
                           process_run_id=self.client.info('server')['run_id'],
                           not_before_us=str(self.period['start_at'] * 1000000))
        self.client.hset(self.source, mapping=self.marker)

    def test_managed_ready_uses_epoch_stream(self):
        self.assertEqual(self.gateway().request(), 200)
        event = self.client.xrange(self.stream)[0][1]
        self.assertEqual(event['source_run_id'], self.run_id)
        self.assertFalse(self.client.exists('market:apisix:charges:v1'))

    def test_process_run_id_mismatch_rejects_without_debit(self):
        physical = self.marker['process_run_id']
        mismatched = ('b' if physical[0] != 'b' else 'c') + physical[1:]
        self.client.hset(self.source, 'process_run_id', mismatched)
        self.assertEqual(self.gateway().request(), 503)
        self.assertFalse(any(self.client.exists(k) for k in quota.keys('combo', self.now, self.period['period_key'])))
        self.assertFalse(self.client.exists(self.stream))
        self.assertFalse(self.client.exists('market:apisix:charges:v1'))

    def test_invalid_markers_leave_no_debit(self):
        for field, value in [('status', 'paused'), ('status', ''), ('run_id', 'bad'),
                             ('stream', 'wrong'), ('not_before_us', 'bad'),
                             ('not_before_us', str(self.period['start_at'] * 1000000 + 1))]:
            with self.subTest(field=field, value=value):
                self.client.hset(self.source, mapping={**self.marker, field: value})
                self.assertEqual(self.gateway().request(), 503)
                self.assertFalse(any(self.client.exists(k) for k in quota.keys('combo', self.now, self.period['period_key'])))
                self.assertFalse(self.client.exists(self.stream))

    def test_legacy_marker_still_works(self):
        self.client.delete(self.source)
        self.client.hset(self.source, mapping={'run_id': self.run_id})
        self.assertEqual(self.gateway().request(), 200)
        self.assertEqual(self.client.xlen('market:apisix:charges:v1'), 1)

    def test_atomic_marker_change_rejects(self):
        gateway = self.gateway()
        evaluate = gateway.evaluate
        def race(script, count, *args):
            self.client.hset(self.source, 'status', 'paused')
            return evaluate(script, count, *args)
        gateway.lua.globals().evaluate = race
        self.assertEqual(gateway.request(), 503)
        self.assertFalse(self.client.exists(self.stream))
        self.assertFalse(any(self.client.exists(k) for k in quota.keys('combo', self.now, self.period['period_key'])))

    def test_atomic_stream_rotation_rejects(self):
        gateway = self.gateway()
        evaluate = gateway.evaluate
        def race(script, count, *args):
            self.client.hset(self.source, mapping={
                'run_id': 'b' * 40, 'stream': 'market:apisix:charges:v1:' + 'b' * 40})
            return evaluate(script, count, *args)
        gateway.lua.globals().evaluate = race
        self.assertEqual(gateway.request(), 503)
        self.assertFalse(self.client.exists(self.stream))
        self.assertFalse(any(self.client.exists(k) for k in quota.keys('combo', self.now, self.period['period_key'])))

    def test_partial_managed_marker_never_falls_back(self):
        for missing in ('stream', 'status', 'not_before_us'):
            with self.subTest(missing=missing):
                self.client.hset(self.source, mapping=self.marker)
                self.client.hdel(self.source, missing)
                self.assertEqual(self.gateway().request(), 503)
                self.assertFalse(self.client.exists('market:apisix:charges:v1'))
                self.assertFalse(self.client.exists(self.stream))


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(GatewaySourceEpochTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped and result.testsRun else 1)
