"""Real Redis EVAL races; only APISIX transport is replaced by the Lua wrapper."""
import json
import unittest

from app import quota_cutover
import test_subscription_quota as native


METADATA = 'market:apisix:credential:consumer'
STREAM = 'market:apisix:charges:v1'
SOURCE = 'market:apisix:charges:source'


@unittest.skipUnless(native.redis and native.LuaRuntime, 'requires real Redis and Lua')
class GatewayPolicyRaceTests(unittest.TestCase):
    setUpClass = classmethod(native.SubscriptionQuotaTests.setUpClass.__func__)
    tearDownClass = classmethod(native.SubscriptionQuotaTests.tearDownClass.__func__)
    publish = native.SubscriptionQuotaTests.publish
    usage = native.SubscriptionQuotaTests.usage

    def setUp(self):
        native.SubscriptionQuotaTests.setUp(self)
        self.client.hset(SOURCE, mapping={'run_id': self.client.info('server')['run_id']})

    def charge_state(self):
        return {key: self.client.dump(key) for key in self.client.scan_iter('market:apisix:*')
                if key.startswith('market:apisix:quota:') or key.startswith('market:apisix:charges:')}

    def race(self, change):
        gateway = native.Gateway(self.client, self.now)
        evaluate = gateway.evaluate
        intercepted = []
        def at_eval(script, count, *args):
            self.assertFalse(intercepted)
            intercepted.append(True)
            self.assertIn(count, (4, 7))
            self.assertEqual(args[count - 1], METADATA)
            expected = self.client.hgetall(METADATA)
            self.assertEqual(list(args[-4:]), [expected[field] for field in
                             ('status', 'mode', 'quota_scope', 'subscription_periods')])
            change()
            return evaluate(script, count, *args)
        gateway.lua.globals().evaluate = at_eval
        before = self.charge_state()
        self.assertEqual(gateway.request(), 503)
        self.assertTrue(intercepted)
        self.assertEqual(self.charge_state(), before)

    def test_refresh_between_read_and_eval_denies_old_and_accepts_new_policy(self):
        for tracked in (False, True):
            with self.subTest(tracked=tracked):
                self.setUp()
                if tracked:
                    self.period.update(order_id='old-order', segment_id='old-segment')
                self.publish()
                # Existing usage is preserved as well as absent stream/coverage.
                for key in native.keys('combo', self.now, self.period['period_key']):
                    self.client.set(key, '2')
                updated = dict(self.period, monthly_quota=9)
                if tracked:
                    updated.update(order_id='new-order', segment_id='new-segment')
                data = self.client.hgetall(METADATA)
                data['subscription_periods'] = json.dumps([updated])
                self.race(lambda: quota_cutover.refresh_subscription(self.client, METADATA, data))
                self.assertEqual(native.Gateway(self.client, self.now).request(), 200)
                self.assertEqual(self.usage(), [3, 3, 3])
                events = self.client.xrange(STREAM)
                self.assertEqual(len(events), int(tracked))
                if tracked:
                    self.assertEqual(events[0][1]['order_id'], 'new-order')
                    self.assertEqual(events[0][1]['segment_id'], 'new-segment')

    def test_revocation_and_deletion_after_read_never_debit(self):
        for tracked in (False, True):
            for deleted in (False, True):
                with self.subTest(tracked=tracked, deleted=deleted):
                    self.setUp()
                    if tracked:
                        self.period.update(order_id='order', segment_id='segment')
                    self.publish()
                    def change():
                        if deleted:
                            self.client.delete(METADATA)
                        else:
                            data = self.client.hgetall(METADATA)
                            data['status'] = 'revoked'
                            quota_cutover.refresh_subscription(self.client, METADATA, data)
                    self.race(change)
                    self.assertEqual(native.Gateway(self.client, self.now).request(), 403)
                    self.assertEqual(self.usage(), [0, 0, 0])

    def test_each_metadata_field_is_compared_before_writes(self):
        changes = [('status', 'disabled'), ('mode', 'legacy'), ('quota_scope', 'other-scope'),
                   ('subscription_periods', json.dumps([self.period], indent=2))]
        for field, value in changes:
            with self.subTest(field=field):
                self.setUp()
                self.period.update(order_id='order', segment_id='segment')
                self.publish()
                self.race(lambda: self.client.hset(METADATA, field, value))

    def test_wrong_metadata_type_rejects_without_writes(self):
        def change():
            self.client.delete(METADATA)
            self.client.set(METADATA, 'not-a-hash')
        self.race(change)

    def test_preexisting_event_and_counters_unchanged_by_stale_policy(self):
        self.period.update(order_id='order', segment_id='segment')
        self.publish()
        self.assertEqual(native.Gateway(self.client, self.now).request(), 200)
        self.race(lambda: self.client.hset(METADATA, 'status', 'revoked'))
        self.assertEqual(self.client.xlen(STREAM), 1)
        self.assertEqual(self.usage(), [1, 1, 1])


if __name__ == '__main__':
    unittest.main()
