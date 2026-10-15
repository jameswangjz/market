"""Isolated Redis 7 tests; never connect to deployment Redis or import app.main."""
import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

import redis

from app import refund_segment_boundary as boundary
from app.quota_cutover import token


META = 'market:apisix:credential:boundary-test'
SOURCE = 'market:apisix:charges:source'
STREAM = 'market:apisix:charges:v1'


class BoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get('QUOTA_REDIS_SERVER') or shutil.which('redis-server')
        if not binary:
            raise RuntimeError('Redis 7 required')
        cls.temp = tempfile.TemporaryDirectory(prefix='market-boundary-')
        cls.socket = str(Path(cls.temp.name) / 'redis.sock')
        cls.server = subprocess.Popen([binary, '--port', '0', '--unixsocket', cls.socket,
                                      '--save', '', '--appendonly', 'no'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cls.client = redis.Redis(unix_socket_path=cls.socket, decode_responses=True)
        try:
            for _ in range(150):
                try:
                    cls.client.ping()
                    break
                except redis.RedisError:
                    time.sleep(.02)
            else:
                raise RuntimeError('isolated Redis failed to start')
            if int(cls.client.info('server')['redis_version'].split('.')[0]) < 7:
                raise RuntimeError('Redis 7 required')
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server.terminate()
        cls.server.wait(timeout=5)
        cls.server.stderr.close()
        cls.temp.cleanup()

    def setUp(self):
        self.client.flushdb()  # This test class owns this disposable Unix socket.
        now = self.client.time()[0]
        self.periods = [dict(start_at=now-100, end_at=now+1000, period_key='p:1%',
                             route_key='new', monthly_quota=10, order_id='o', term_id='t'),
                        dict(start_at=now+1000, end_at=now+2000, period_key='p2',
                             route_key='new', monthly_quota=0, order_id='o', term_id='t')]
        self.expected = dict(mode='subscription', status='active', quota_scope='scope:%',
                             subscription_periods=json.dumps([dict(p, route_key='old')
                                                              for p in self.periods]), extra='kept')
        self.target = dict(self.expected, subscription_periods=json.dumps(self.periods))
        self.client.hset(META, mapping=self.expected)
        self.client.hset(SOURCE, mapping={'run_id': 'a'*40})
        for i in range(2):
            self.client.hset(self.coverage(i), mapping={'baseline': '0'})
        self.client.set(self.counter(), '3')

    def counter(self, i=0):
        return ('market:apisix:quota:subscription:month:' + token('scope:%') + ':'
                + token(self.periods[i]['period_key']))

    def coverage(self, i=0):
        return ('market:apisix:charges:coverage:' + token('scope:%') + ':'
                + token(self.periods[i]['period_key']))

    def switch(self, **changes):
        args = dict(credential_key=META, change_id='change-1', direction='target',
                    expected_policy=self.expected, target_policy=self.target,
                    periods=self.periods)
        args.update(changes)
        return boundary.freeze_policy_switch(self.client, **args)

    def snapshot(self):
        result = {}
        for key in self.client.scan_iter():
            kind = self.client.type(key)
            if kind == 'hash':
                value = self.client.hgetall(key)
            elif kind == 'stream':
                value = self.client.xrange(key)
            elif kind == 'list':
                value = self.client.lrange(key, 0, -1)
            else:
                value = self.client.get(key)
            result[key] = (kind, value)
        return result

    def rejected(self, **changes):
        before = self.snapshot()
        with self.assertRaises((ValueError, redis.ResponseError)):
            self.switch(**changes)
        self.assertEqual(before, self.snapshot())

    def test_freeze_watermark_replay_and_counters_unchanged(self):
        event = self.client.xadd(STREAM, {'quantity': '1'})
        before = {k: self.client.dump(k) for k in (self.counter(), self.coverage(), SOURCE, STREAM)}
        clock_before = self.client.time()
        receipt = self.switch()
        clock_after = self.client.time()
        self.assertEqual(receipt['boundary_id'], event)
        self.assertEqual(receipt['source_run_id'], 'a'*40)
        self.assertEqual(receipt['periods'][0]['used_at_switch'], 3)
        self.assertEqual(receipt['periods'][0]['available_quota'], 7)
        self.assertIsNone(receipt['periods'][1]['available_quota'])
        self.assertLessEqual(clock_before[0]*1000000+clock_before[1], receipt['cutover_us'])
        self.assertLessEqual(receipt['cutover_us'], clock_after[0]*1000000+clock_after[1])
        self.assertEqual(before, {k: self.client.dump(k) for k in before})
        self.client.incr(self.counter())
        self.client.xadd(STREAM, {'quantity': '1'})
        self.assertEqual(receipt, self.switch())
        self.assertEqual(self.client.hgetall(META), self.target)
        self.assertEqual(receipt['target_fingerprint'], hashlib.sha256(
            boundary._json(self.target).encode()).hexdigest())

    def test_no_stream_and_zero_missing_counter_are_known_by_coverage(self):
        self.client.delete(self.counter())
        receipt = self.switch()
        self.assertEqual(receipt['boundary_id'], '0-0')
        self.assertEqual(receipt['periods'][0]['used_at_switch'], 0)
        self.assertIsNone(self.client.get(self.counter()))

    def test_conflicting_replay_and_changed_epoch(self):
        self.switch()
        self.rejected(expected_policy=self.target)
        self.rejected(target_policy=dict(self.target, extra='changed'))
        self.client.hset(SOURCE, 'run_id', 'b'*40)
        self.rejected()
        self.client.delete(SOURCE)
        self.rejected()

    def test_receipt_replay_requires_complete_target(self):
        receipt = self.switch()
        self.assertEqual(receipt, self.switch())
        self.client.hset(META, mapping=self.expected)
        before = self.snapshot()
        with self.assertRaisesRegex(redis.ResponseError,
                'incomplete_switch_intent_requires_manual_reconciliation'):
            self.switch()
        self.assertEqual(before, self.snapshot())
        self.client.hset(META, 'status', 'disabled')
        self.rejected()

    def test_untrusted_reads_fail_without_any_write(self):
        cases = [lambda: self.client.delete(self.coverage()),
                 lambda: self.client.hset(self.coverage(), 'baseline', '1'),
                 lambda: self.client.hset(self.coverage(), 'baseline', '00'),
                 lambda: self.client.delete(self.coverage(1)),
                 lambda: self.client.set(self.counter(1), '1'),
                 lambda: self.client.expire(self.counter(), 60),
                 lambda: self.client.hset(SOURCE, 'run_id', 'X'*40),
                 lambda: self.client.delete(SOURCE),
                 lambda: self.client.set(STREAM, 'wrongtype'),
                 lambda: self.client.hset(META, 'status', 'disabled'),
                 lambda: self.client.delete(META)]
        for value in ('-1', '01', '1.0', 'nan', '9007199254740992', '999999999999999999999'):
            cases.append(lambda value=value: self.client.set(self.counter(), value))
        for key in (META, SOURCE, self.counter(), self.coverage()):
            def wrongtype(key=key):
                self.client.delete(key)
                self.client.rpush(key, 'wrong')
            cases.append(wrongtype)
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                case()
                self.rejected()

    def test_acl_denied_hset_leaves_no_receipt(self):
        self.client.execute_command('ACL', 'SETUSER', 'boundary', 'on', '>secret',
                                    '~*', '+@all', '-hset')
        restricted = redis.Redis(unix_socket_path=self.socket, username='boundary',
                                 password='secret', decode_responses=True)
        try:
            before = self.snapshot()
            with self.assertRaises(redis.ResponseError):
                boundary.freeze_policy_switch(restricted, credential_key=META,
                    change_id='change-1', direction='target', expected_policy=self.expected,
                    target_policy=self.target, periods=self.periods)
            self.assertEqual(before, self.snapshot())
        finally:
            restricted.close()
            self.client.execute_command('ACL', 'DELUSER', 'boundary')

    def test_input_guards(self):
        self.rejected(expected_policy=dict(self.expected, status='disabled'))
        self.rejected(target_policy=dict(self.target, extra=float('nan')))
        self.rejected(target_policy={k: v for k, v in self.target.items() if k != 'extra'})
        self.rejected(direction='other')
        self.rejected(change_id='bad:id')
        self.rejected(periods=self.periods * 129)
        for field, value in [('monthly_quota', -1), ('monthly_quota', True),
                             ('start_at', 1.1), ('route_key', ''), ('unknown', 'x')]:
            periods = copy.deepcopy(self.periods)
            periods[0][field] = value
            self.rejected(periods=periods)
        self.rejected(periods=list(reversed(self.periods)))
        self.rejected(periods=[self.periods[0], self.periods[0]])

    def test_exact_safe_integer_receipt(self):
        self.periods[0]['monthly_quota'] = boundary.MAX_INTEGER
        self.target['subscription_periods'] = json.dumps(self.periods)
        self.client.set(self.counter(), str(boundary.MAX_INTEGER-2))
        receipt = self.switch()
        self.assertEqual(receipt['periods'][0]['monthly_quota'], boundary.MAX_INTEGER)
        self.assertEqual(receipt['periods'][0]['used_at_switch'], boundary.MAX_INTEGER-2)
        self.assertEqual(receipt['periods'][0]['available_quota'], 2)

    def test_actual_36_month_policy_exceeds_4k(self):
        now = self.client.time()[0]
        month = 30*86400
        self.periods = [dict(start_at=now-100+i*month, end_at=now-100+(i+1)*month,
                             period_key=f'month-{i}', route_key='version-two',
                             monthly_quota=1000, order_id='order-36-months',
                             term_id='term-36-months', segment_id=f'segment-{i}')
                        for i in range(36)]
        self.target['subscription_periods'] = json.dumps([
            dict(p, rate_limit_per_minute=100, daily_quota=500) for p in self.periods])
        self.expected['subscription_periods'] = self.target['subscription_periods'].replace(
            'version-two', 'version-one')
        self.assertGreater(len(self.target['subscription_periods']), 4096)
        self.client.hset(META, mapping=self.expected)
        for i in range(36):
            self.client.hset(self.coverage(i), mapping={'baseline': '0'})
        receipt = self.switch()
        self.assertEqual(len(receipt['periods']), 36)
        self.assertEqual(receipt['periods'][35]['available_quota'], 1000)

    def test_expired_period_is_not_refundable_and_needs_no_evidence(self):
        now = self.client.time()[0]
        past = dict(self.periods[0], start_at=now-2000, end_at=now-100,
                    period_key='expired')
        self.periods.insert(0, past)
        self.target['subscription_periods'] = json.dumps(self.periods)
        self.expected['subscription_periods'] = json.dumps([
            dict(p, route_key='old') for p in self.periods])
        self.client.hset(META, mapping=self.expected)
        # Even wrong types for expired evidence are irrelevant and untouched.
        self.client.rpush(self.counter(0), 'untrusted-past-counter')
        self.client.set(self.coverage(0), 'untrusted-past-coverage')
        receipt = self.switch()
        self.assertEqual([p['period_key'] for p in receipt['periods']], ['p:1%', 'p2'])
        self.assertEqual(receipt['periods'][0]['used_at_switch'], 3)
        self.assertEqual(receipt['periods'][0]['available_quota'], 7)
        self.assertEqual(self.client.lrange(self.counter(0), 0, -1), ['untrusted-past-counter'])

    def leave_incomplete_intent(self):
        class FailFinalWrite:
            def eval(inner, script, count, *args):
                # Execute the real script on Redis, substituting only its final
                # HSET with an invalid arity to leave an incomplete intent.
                ending = "redis.call(unpack(hset))\nreturn receipt"
                self.assertIn(ending, script)
                return self.client.eval(script.replace(ending,
                    "redis.call('HSET', KEYS[1])\nreturn receipt"), count, *args)
        with self.assertRaises(redis.ResponseError):
            boundary.freeze_policy_switch(FailFinalWrite(), credential_key=META,
                change_id='change-1', direction='target', expected_policy=self.expected,
                target_policy=self.target, periods=self.periods)
        self.assertEqual(self.client.hgetall(META), self.expected)
        key, = list(self.client.scan_iter(boundary.NAMESPACE+'*'))
        return key, self.client.get(key)

    def test_final_hset_failure_without_usage_requires_manual_reconciliation(self):
        key, intent = self.leave_incomplete_intent()
        before = self.snapshot()
        with self.assertRaisesRegex(redis.ResponseError,
                'incomplete_switch_intent_requires_manual_reconciliation'):
            self.switch()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.client.hgetall(META), self.expected)
        self.assertEqual(self.client.get(key), intent)
        self.assertEqual(self.client.get(self.counter()), '3')

    def test_old_gateway_charge_after_hset_failure_cannot_complete_stale_intent(self):
        from test_subscription_quota import Gateway
        now = self.client.time()[0]
        for period in self.periods:
            period['segment_id'] = 'segment'
        self.expected['subscription_periods'] = json.dumps([
            dict(p, route_key='old', rate_limit_per_minute=100, daily_quota=0)
            for p in self.periods])
        self.target['subscription_periods'] = json.dumps([
            dict(p, rate_limit_per_minute=100, daily_quota=0) for p in self.periods])
        self.client.hset(META, mapping=self.expected)
        key, intent = self.leave_incomplete_intent()
        frozen = json.loads(intent)
        self.assertEqual(frozen['periods'][0]['used_at_switch'], 3)
        self.assertEqual(frozen['boundary_id'], '0-0')
        self.assertEqual(Gateway(self.client, now).request(
            consumer='boundary-test', route='old'), 200)
        self.assertEqual(self.client.get(self.counter()), '4')
        self.assertEqual(self.client.xlen(STREAM), 1)
        before = self.snapshot()
        with self.assertRaisesRegex(redis.ResponseError,
                'incomplete_switch_intent_requires_manual_reconciliation'):
            self.switch()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.client.hgetall(META), self.expected)
        self.assertEqual(list(self.client.scan_iter(boundary.NAMESPACE+'*')), [key])
        self.assertEqual(self.client.get(key), intent)

    def test_set_exception_cannot_write_metadata(self):
        class FailFirstWrite:
            def eval(inner, script, count, *args):
                return self.client.eval(script.replace("redis.call('SET', KEYS[2], receipt)",
                    "redis.call('SET', KEYS[2])"), count, *args)
        before = self.snapshot()
        with self.assertRaises(redis.ResponseError):
            boundary.freeze_policy_switch(FailFirstWrite(), credential_key=META,
                change_id='change-1', direction='target', expected_policy=self.expected,
                target_policy=self.target, periods=self.periods)
        self.assertEqual(before, self.snapshot())

    def test_replay_wrong_stream_type_and_disabled_metadata_rejected(self):
        self.switch()
        self.client.set(STREAM, 'wrongtype')
        self.rejected()
        self.client.delete(STREAM)
        self.client.hset(META, 'status', 'disabled')
        self.rejected()

    def test_real_redis_oom_leaves_no_receipt_or_metadata_change(self):
        before = self.snapshot()
        self.client.config_set('maxmemory-policy', 'noeviction')
        self.client.config_set('maxmemory', 1)
        try:
            with self.assertRaises(redis.exceptions.OutOfMemoryError):
                self.switch()
        finally:
            self.client.config_set('maxmemory', 0)
        self.assertEqual(before, self.snapshot())

    def test_metadata_integer_fields_and_entire_target_replay(self):
        target = dict(self.target, numeric=123, zero=0)
        receipt = self.switch(target_policy=target)
        self.assertEqual(self.client.hget(META, 'numeric'), '123')
        self.assertEqual(self.client.hget(META, 'zero'), '0')
        self.assertEqual(receipt, self.switch(target_policy=target))
        # Matching selected identity alone cannot falsely complete a replay.
        self.client.hset(META, 'numeric', '124')
        self.rejected(target_policy=target)

    def test_compensation_uses_separate_receipt(self):
        self.switch()
        original_periods = json.loads(self.expected['subscription_periods'])
        restored = self.switch(direction='original', expected_policy=self.target,
                               target_policy=self.expected, periods=original_periods)
        self.assertEqual(restored['direction'], 'original')
        self.assertEqual(len(list(self.client.scan_iter(boundary.NAMESPACE+'*'))), 2)
        self.rejected()

    def test_concurrent_stale_charge_cannot_be_undercounted(self):
        # Same exact-policy guard as gateway debit; each charge and switch is one
        # EVAL. A stale request either precedes the receipt or cannot increment.
        charge = """
        if redis.call('HGET', KEYS[1], 'subscription_periods') ~= ARGV[1] then return 0 end
        redis.call('XADD', KEYS[3], '*', 'quantity', '1')
        redis.call('INCR', KEYS[2])
        return 1
        """
        for _ in range(15):
            self.setUp()
            barrier = threading.Barrier(2)
            def debit():
                barrier.wait(timeout=5)
                return self.client.eval(charge, 3, META, self.counter(), STREAM,
                                        self.expected['subscription_periods'])
            def switch():
                barrier.wait(timeout=5)
                return self.switch()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                charged, frozen = pool.submit(debit), pool.submit(switch)
                accepted, receipt = charged.result(), frozen.result()
            self.assertEqual(receipt['periods'][0]['used_at_switch'], 3+accepted)
            self.assertEqual(int(self.client.get(self.counter())), 3+accepted)
            self.assertEqual(receipt['boundary_id'] == '0-0', not accepted)
            self.assertEqual(self.client.eval(charge, 3, META, self.counter(), STREAM,
                             self.expected['subscription_periods']), 0)

    def test_actual_gateway_stale_eval_after_switch_never_debits(self):
        # Import only the lightweight native Lua/Redis adapter, never fixtures
        # that import the application main module or its database setup.
        from test_subscription_quota import Gateway
        now = self.client.time()[0]
        for period in self.periods:
            period.update(segment_id='segment')
        old = [dict(p, route_key='old', rate_limit_per_minute=100,
                    daily_quota=0) for p in self.periods]
        new = [dict(p, rate_limit_per_minute=100, daily_quota=0) for p in self.periods]
        self.expected['subscription_periods'] = json.dumps(old)
        self.target['subscription_periods'] = json.dumps(new)
        self.client.hset(META, mapping=self.expected)
        gateway = Gateway(self.client, now)
        self.assertEqual(gateway.request(consumer='boundary-test', route='old'), 200)
        self.assertEqual(self.client.get(self.counter()), '4')
        original_eval = gateway.evaluate
        receipts = []
        def after_read(script, count, *args):
            receipts.append(self.switch())
            return original_eval(script, count, *args)
        gateway.lua.globals().evaluate = after_read
        self.assertEqual(gateway.request(consumer='boundary-test', route='old'), 503)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['periods'][0]['used_at_switch'], 4)
        self.assertEqual(self.client.get(self.counter()), '4')
        self.assertEqual(self.client.xlen(STREAM), 1)


if __name__ == '__main__':
    unittest.main()
