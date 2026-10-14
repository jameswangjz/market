"""Adversarial cutover tests using real Redis and the gateway's real Lua.

Discovery explicitly skips missing runtimes; standalone execution fails on skips.
"""
import concurrent.futures
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import quota_cutover as cutover
import test_subscription_quota as runtime


class EvalIntercept:
    """Delegate real Redis operations, injecting a write immediately before EVAL."""

    def __init__(self, client, before):
        self.client = client
        self.before = before

    def __getattr__(self, name):
        return getattr(self.client, name)

    def eval(self, *args):
        self.before()
        return self.client.eval(*args)


@unittest.skipUnless(runtime.redis and runtime.LuaRuntime, "requires redis and lupa")
class QuotaCutoverTests(unittest.TestCase):
    tearDownClass = classmethod(runtime.SubscriptionQuotaTests.tearDownClass.__func__)

    @classmethod
    def setUpClass(cls):
        binary = runtime.os.environ.get("QUOTA_REDIS_SERVER") or runtime.shutil.which("redis-server")
        if not binary or not runtime.shutil.which(binary):
            raise unittest.SkipTest("requires executable redis-server or QUOTA_REDIS_SERVER")
        runtime.SubscriptionQuotaTests.setUpClass.__func__(cls)

    def setUp(self):
        self.client.flushdb()  # Only the helper's dedicated ephemeral Redis.
        self.now = int(self.client.time()[0])
        self.scope = "combo"
        self.period = dict(start_at=self.now - 3600, end_at=self.now + 3600,
                           period_key="anchor", route_key="route",
                           rate_limit_per_minute=10000, daily_quota=0, monthly_quota=0)
        self.credentials = [self.credential()]
        self.legacy_metadata("consumer")

    def credential(self, consumer="consumer", route="route", **changes):
        data = dict(mode="subscription", status="active", quota_scope=self.scope,
                    subscription_periods=json.dumps([dict(self.period, route_key=route)]))
        data.update(changes)
        return route, consumer, data

    def legacy_metadata(self, consumer):
        self.client.hset("market:apisix:credential:" + consumer,
                         mapping=dict(status="active", daily_quota=0,
                                      monthly_quota=0, total_quota=0))

    def old(self, kind="total", consumer="consumer", route="route", suffix=None):
        key = f"market:apisix:quota:{kind}:{route}:{consumer}"
        if suffix is not None:
            key += ":" + str(suffix)
        elif kind == "minute":
            key += ":" + str(self.now // 60)
        return key

    def prepare(self, **kwargs):
        return cutover.preview(self.client, self.scope, self.credentials,
                               actor="integration-test", request_id=kwargs.pop("request_id", "nonce"),
                               **kwargs)

    def execute(self, prepared, client=None):
        return cutover.execute(client or self.client, prepared,
                               acknowledge_conservative_usage=True, cutover_guard_verified=True)

    def snapshot(self):
        # DUMP captures every value and type; absolute expiration avoids TTL drift.
        expiry = """
local ttl = redis.call('PTTL', KEYS[1])
if ttl < 0 then return ttl end
local clock = redis.call('TIME')
return clock[1] * 1000 + math.floor(clock[2] / 1000) + ttl
"""
        return {key: (self.client.dump(key), self.client.eval(expiry, 1, key))
                for key in sorted(self.client.scan_iter())}

    def assert_rejected_without_writes(self, operation):
        before = self.snapshot()
        with self.assertRaises((cutover.QuotaCutoverError, runtime.redis.RedisError,
                                ValueError, TypeError, KeyError)):
            operation()
        self.assertEqual(self.snapshot(), before)

    def test_preview_requires_both_explicit_acknowledgements_and_preserves_state(self):
        self.client.set(self.old(), 7)
        before = self.snapshot()
        prepared = self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(prepared["plan"]["estimated_lifetime_watermark"], 7)
        for ack, guard in [(False, False), (True, False), (False, True)]:
            with self.subTest(ack=ack, guard=guard):
                self.assert_rejected_without_writes(lambda: cutover.execute(
                    self.client, prepared, acknowledge_conservative_usage=ack,
                    cutover_guard_verified=guard))

    def test_lifetime_watermark_adds_existing_usage_and_rotation_preserves_counts(self):
        self.credentials.append(self.credential("rotated"))
        self.legacy_metadata("rotated")
        for consumer, total, recent in [("consumer", 7, 2), ("rotated", 5, 1)]:
            self.client.set(self.old(consumer=consumer), total)
            self.client.set(self.old("minute", consumer=consumer), recent)
            self.client.set(self.old("day", consumer=consumer, suffix="old-day"), 1)
        prepared = self.prepare()
        for key, value in zip(prepared["keys"][:3], [3, 4, 6]):
            self.client.set(key, value)
        old_values = {key: self.client.get(key) for key in self.client.scan_iter(
            "market:apisix:quota:*:route:*")}
        receipt = self.execute(prepared)
        self.assertEqual(receipt["before"], dict(minute=3, day=4, month=6))
        self.assertEqual(receipt["after"], dict(minute=6, day=16, month=18))
        self.assertEqual(receipt["mode"], "conservative_lifetime_watermark")
        self.assertEqual(sum(s["lifetime_usage"] for s in receipt["sources"]), 12)
        self.assertEqual(self.client.ttl(prepared["keys"][2]), -1)
        for key, value in old_values.items():
            self.assertEqual(self.client.get(key), value)
        for consumer in ["consumer", "rotated"]:
            self.assertEqual(runtime.Gateway(self.client, self.now).request(consumer=consumer), 200)
        self.assertEqual(self.client.mget(prepared["keys"][:3]), ["8", "18", "20"])
        second = self.execute(self.prepare(request_id="rotation-repeat"))
        self.assertEqual(second["before"], second["after"])

    def test_receipt_replay_concurrent_idempotency_and_conflicting_nonce(self):
        self.client.set(self.old(), 9)
        prepared = self.prepare()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(lambda _: self.execute(prepared), range(16)))
        self.assertTrue(all(receipt == receipts[0] for receipt in receipts))
        self.assertEqual(self.client.mget(prepared["keys"][:3]), ["0", "9", "9"])
        before = self.snapshot()
        self.assertEqual(self.execute(prepared), receipts[0])
        self.assertEqual(self.snapshot(), before)
        conflict = self.prepare()
        self.assertNotEqual(conflict["plan"]["fingerprint"], prepared["plan"]["fingerprint"])
        self.assert_rejected_without_writes(lambda: self.execute(conflict))

    def test_concurrent_legacy_requests_after_preview_are_included(self):
        prepared = self.prepare()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: runtime.Gateway(self.client, self.now).request(), range(24)))
        self.assertEqual(results, [200] * 24)
        stale = [item for pair in self.client.hgetall(
            "market:apisix:credential:consumer").items() for item in pair]
        gateways = []
        for _ in range(24):
            gateway = runtime.Gateway(self.client, self.now)

            def metadata(key, gateway=gateway, loaded=[]):
                if not loaded:
                    loaded.append(True)
                    return gateway.table(stale)
                return gateway.metadata(key)

            gateway.lua.globals().metadata = metadata
            gateways.append(gateway)
        receipt = self.execute(prepared)
        self.assertEqual(receipt["after"], dict(minute=24, day=24, month=24))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda gateway: gateway.request(), gateways))
        self.assertEqual(results, [200] * 24)
        self.assertEqual(self.client.mget(prepared["keys"][:3]), ["48", "48", "48"])
        self.assertEqual(self.client.get(self.old()), "24")

    def test_stale_legacy_metadata_is_guarded_inside_eval_and_reloaded(self):
        self.assertEqual(runtime.Gateway(self.client, self.now).request(), 200)
        prepared = self.prepare()
        gateway = runtime.Gateway(self.client, self.now)
        evaluate = gateway.evaluate
        intercepted = []

        def at_eval(script, count, *args):
            if count == 5:
                intercepted.append(self.execute(prepared))
            return evaluate(script, count, *args)

        gateway.lua.globals().evaluate = at_eval
        self.assertEqual(gateway.request(), 200)
        self.assertEqual(len(intercepted), 1)
        self.assertEqual(self.client.get(self.old()), "1")
        self.assertEqual(self.client.mget(prepared["keys"][:3]), ["2", "2", "2"])

    def test_missing_lifetime_with_positive_other_counter_rejects_atomically(self):
        for kind in ["minute", "day", "month"]:
            with self.subTest(kind=kind):
                self.client.flushdb()
                self.legacy_metadata("consumer")
                self.client.set(self.old(kind, suffix=self.now // 60 if kind == "minute" else "past"), 1)
                prepared = self.prepare()
                self.assert_rejected_without_writes(lambda: self.execute(prepared))

    def test_new_source_counter_after_preview_cannot_bypass_completeness_check(self):
        self.client.set(self.old(), 1)
        prepared = self.prepare()
        self.client.set(self.old("day", suffix="created-after-preview"), 2)
        self.assert_rejected_without_writes(lambda: self.execute(prepared))

    def test_target_counters_and_receipt_reject_malformed_values_and_types(self):
        for position in range(4):
            for bad in ["garbage", "-1", "1.5", "01", str(cutover.MAX_COUNT + 1), "hash"]:
                with self.subTest(position=position, bad=bad):
                    self.client.flushdb()
                    self.legacy_metadata("consumer")
                    prepared = self.prepare()
                    key = prepared["keys"][position]
                    if bad == "hash":
                        self.client.hset(key, mapping={"wrong": "type"})
                    else:
                        self.client.set(key, bad)
                    self.assert_rejected_without_writes(lambda: self.execute(prepared))

    def test_every_source_counter_and_metadata_type_validated_before_writes(self):
        self.credentials.append(self.credential("second"))
        for consumer in ["consumer", "second"]:
            for kind in ["total", "minute", "day", "month", "metadata"]:
                for bad in ["garbage", "-1", "1.5", "01", str(cutover.MAX_COUNT + 1), "hash"]:
                    if kind == "metadata" and bad != "hash":
                        continue
                    with self.subTest(consumer=consumer, kind=kind, bad=bad):
                        self.client.flushdb()
                        for name in ["consumer", "second"]:
                            self.legacy_metadata(name)
                            self.client.set(self.old(consumer=name), 3)
                        key = ("market:apisix:credential:" + consumer if kind == "metadata"
                               else self.old(kind, consumer=consumer))
                        if kind == "metadata":
                            self.client.delete(key)
                            self.client.set(key, "wrong-type")
                        elif bad == "hash":
                            self.client.delete(key)
                            self.client.hset(key, mapping={"wrong": "type"})
                        else:
                            self.client.set(key, bad)
                        self.assert_rejected_without_writes(lambda: self.execute(self.prepare()))

    def test_overflow_in_each_destination_and_sum_of_sources_is_atomic(self):
        for position in range(3):
            with self.subTest(position=position):
                self.client.flushdb()
                self.legacy_metadata("consumer")
                self.client.set(self.old(), 2)
                self.client.set(self.old("minute"), 2)
                prepared = self.prepare()
                self.client.set(prepared["keys"][position], cutover.MAX_COUNT - 1)
                self.assert_rejected_without_writes(lambda: self.execute(prepared))
        self.client.flushdb()
        self.credentials.append(self.credential("second"))
        for name in ["consumer", "second"]:
            self.legacy_metadata(name)
            self.client.set(self.old(consumer=name), cutover.MAX_COUNT)
        self.assert_rejected_without_writes(lambda: self.execute(self.prepare()))

    def test_glob_escaping_scan_selects_only_literal_route_and_consumer(self):
        route, consumer = "r*?[x]\\", "c*?[y]\\"
        self.credentials = [self.credential(consumer, route)]
        self.legacy_metadata(consumer)
        exact = self.old("day", consumer=consumer, route=route, suffix="past")
        self.client.set(exact, 2)
        self.client.set(self.old(consumer=consumer, route=route), 3)
        decoy = self.old("day", consumer="c-other", route="r-other", suffix="past")
        self.client.hset(decoy, mapping={"wrong": "type"})
        scanned = cutover.legacy_keys(self.client, route, consumer)
        self.assertIn(exact, scanned)
        self.assertNotIn(decoy, scanned)
        self.assertEqual(self.execute(self.prepare())["after"]["month"], 3)
        self.assertEqual(self.client.hgetall(decoy), {"wrong": "type"})

    def test_scope_inactive_and_ambiguous_periods_reject_without_writes(self):
        # Keep the inactive fixture's start deadline well ahead of real Redis TIME
        # validation, even when the full suite delays this test.
        cases = [dict(quota_scope="elsewhere"), dict(mode="legacy"),
                 dict(subscription_periods=json.dumps([dict(self.period, end_at=self.now)])),
                 dict(subscription_periods=json.dumps([dict(
                     self.period, start_at=self.now + 3600, end_at=self.now + 7200)])),
                 dict(subscription_periods=json.dumps([self.period, dict(self.period, period_key="other")]))]
        for changes in cases:
            with self.subTest(changes=changes):
                self.credentials = [self.credential(**changes)]
                self.assert_rejected_without_writes(self.prepare)
        self.credentials = [self.credential()]
        prepared = self.prepare()
        self.client.hset("market:apisix:credential:consumer",
                         mapping=dict(mode="subscription", quota_scope="elsewhere"))
        self.assert_rejected_without_writes(lambda: self.execute(prepared))

    def test_each_credential_requires_active_unambiguous_period(self):
        for periods in [[], [dict(self.period, end_at=self.now)],
                        [self.period, dict(self.period)]]:
            with self.subTest(periods=periods):
                self.credentials = [self.credential(), self.credential(
                    "second", subscription_periods=json.dumps(periods))]
                self.assert_rejected_without_writes(self.prepare)

    def test_expired_and_future_preview_reject_using_real_redis_clock(self):
        for field, value in [("valid_until", self.now), ("created_at", self.now + 3600)]:
            with self.subTest(field=field):
                prepared = copy.deepcopy(self.prepare())
                prepared["plan"][field] = value
                # Exercise the Lua clock check independently of the file fingerprint check.
                self.assert_rejected_without_writes(lambda: self.client.eval(
                    cutover.MIGRATE_SCRIPT, len(prepared["keys"]), *prepared["keys"],
                    json.dumps(prepared["plan"])))

    def test_modified_preview_keys_and_metadata_reject_before_eval(self):
        for target in ["keys", "metadata", "expiry"]:
            with self.subTest(target=target):
                prepared = copy.deepcopy(self.prepare())
                if target == "keys":
                    prepared["keys"][0] += ":tampered"
                elif target == "metadata":
                    prepared["plan"]["sources"][0]["data"]["quota_scope"] = "tampered"
                else:
                    prepared["plan"]["valid_until"] += 3600
                self.assert_rejected_without_writes(lambda: self.execute(prepared))

    def test_malformed_approved_period_metadata_cannot_be_published(self):
        incomplete = dict(self.period)
        del incomplete["rate_limit_per_minute"]
        for period in [incomplete, dict(self.period, rate_limit_per_minute=0),
                       dict(self.period, daily_quota=-1), dict(self.period, monthly_quota="1"),
                       dict(self.period, route_key="other"), dict(self.period, period_key="")]:
            with self.subTest(period=period):
                self.client.flushdb()
                self.legacy_metadata("consumer")
                self.credentials = [self.credential(subscription_periods=json.dumps([period]))]
                self.assert_rejected_without_writes(lambda: self.execute(self.prepare()))

    def test_sync_unused_rejects_all_malformed_counters_before_metadata_writes(self):
        for kind in ["total", "minute", "day", "month"]:
            for bad in ["garbage", "-1", "1.5", "00", str(cutover.MAX_COUNT + 1), "hash"]:
                with self.subTest(kind=kind, bad=bad):
                    self.client.flushdb()
                    self.legacy_metadata("consumer")
                    key = self.old(kind)
                    if bad == "hash":
                        self.client.hset(key, mapping={"wrong": "type"})
                    else:
                        self.client.set(key, bad)
                    self.assert_rejected_without_writes(lambda: cutover.sync_unused(
                        self.client, "market:apisix:credential:consumer",
                        self.credentials[0][2], "route", "consumer"))

    def test_sync_unused_new_positive_counter_after_scan_cannot_bypass_guard(self):
        snapshots = []

        def new_counter():
            self.client.set(self.old("day", suffix="new-after-scan"), 1)
            snapshots.append(self.snapshot())

        rejected = False
        try:
            cutover.sync_unused(EvalIntercept(self.client, new_counter),
                                "market:apisix:credential:consumer", self.credentials[0][2],
                                "route", "consumer")
        except (cutover.QuotaCutoverError, runtime.redis.RedisError):
            rejected = True
        self.assertEqual(self.snapshot(), snapshots[0])
        self.assertTrue(rejected, "a new positive source counter must prevent unused cutover")

    def test_sync_unused_zero_counts_atomic_race_preserves_usage(self):
        self.client.set(self.old(), 0)
        self.client.set(self.old("day", suffix="past"), 0)
        before_counts = {key: self.client.get(key) for key in cutover.legacy_keys(
            self.client, "route", "consumer")}
        cutover.sync_unused(self.client, "market:apisix:credential:consumer",
                            self.credentials[0][2], "route", "consumer")
        for key, value in before_counts.items():
            self.assertEqual(self.client.get(key), value)
        self.client.flushdb()
        self.legacy_metadata("consumer")
        self.client.set(self.old(), 0)
        prepared = self.credentials[0][2]
        snapshots = []

        def racing_request():
            self.assertEqual(runtime.Gateway(self.client, self.now).request(), 200)
            snapshots.append(self.snapshot())

        with self.assertRaises(cutover.QuotaCutoverError):
            cutover.sync_unused(EvalIntercept(self.client, racing_request),
                                "market:apisix:credential:consumer", prepared, "route", "consumer")
        self.assertEqual(self.snapshot(), snapshots[0])
        self.assertEqual(self.client.get(self.old()), "1")
        self.assertIsNone(self.client.hget("market:apisix:credential:consumer", "mode"))

    def test_subscription_refresh_preserves_scope_and_usage(self):
        key = 'market:apisix:credential:consumer'
        data = self.credentials[0][2]
        self.client.hset(key, mapping=data)
        counters = runtime.keys(self.scope, self.now, self.period['period_key'])
        for counter in counters:
            self.client.set(counter, 7)
        changed = dict(data, subscription_periods=json.dumps([
            dict(self.period, monthly_quota=3, end_at=self.now + 7200)]))
        cutover.refresh_subscription(self.client, key, changed)
        self.assertEqual(self.client.mget(counters), ['7', '7', '7'])
        self.assertEqual(runtime.Gateway(self.client, self.now).request(), 429)
        self.assert_rejected_without_writes(lambda: cutover.refresh_subscription(
            self.client, key, dict(changed, quota_scope='fresh-combo')))

    def test_subscription_refresh_scope_race_rejects_without_writes(self):
        key = 'market:apisix:credential:consumer'
        data = self.credentials[0][2]
        snapshots = []

        def reassign():
            self.client.hset(key, mapping=dict(data, quota_scope='other-combo'))
            snapshots.append(self.snapshot())

        for operation in [cutover.refresh_subscription,
                          lambda client, key, data: cutover.sync_unused(
                              client, key, data, 'route', 'consumer')]:
            with self.subTest(operation=operation):
                self.client.hset(key, mapping=data)
                with self.assertRaises((cutover.QuotaCutoverError, runtime.redis.RedisError)):
                    operation(EvalIntercept(self.client, reassign), key, data)
                self.assertEqual(self.snapshot(), snapshots[-1])

    def test_subscription_refresh_missing_or_legacy_metadata_rejects(self):
        key = 'market:apisix:credential:consumer'
        self.assert_rejected_without_writes(lambda: cutover.refresh_subscription(
            self.client, key, self.credentials[0][2]))
        self.client.delete(key)
        self.assert_rejected_without_writes(lambda: cutover.refresh_subscription(
            self.client, key, self.credentials[0][2]))

    def test_readonly_preflight_exact_live_mapping_and_conservative_fallback(self):
        self.client.set(self.old(), 17)
        self.client.set(self.old('minute'), 3)
        self.client.set(self.old('day', suffix='utc-day'), 4)
        self.client.set(self.old('month', suffix='utc-month'), 12)
        prepared = self.prepare()
        self.client.mset(dict(zip(prepared['keys'][:3], [2, 5, 7])))
        before = self.snapshot()
        checked = cutover.execute(self.client, prepared, preflight_only=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(checked['mode'], 'preflight_only')
        self.assertEqual(checked['before'], dict(minute=2, day=5, month=7))
        self.assertEqual(checked['after'], dict(minute=5, day=22, month=24))
        self.assertEqual(self.execute(prepared)['after'], checked['after'])
        self.assertEqual(self.client.get(self.old()), '17')

    def test_large_integer_receipt_preflight_and_storage_remain_exact(self):
        self.client.set(self.old(), cutover.MAX_COUNT - 7)
        prepared = self.prepare()
        self.client.mset(dict(zip(prepared['keys'][:3], [0, 3, 7])))
        checked = cutover.execute(self.client, prepared, preflight_only=True)
        expected = dict(minute=0, day=cutover.MAX_COUNT - 4, month=cutover.MAX_COUNT)
        self.assertEqual(checked['after'], expected)
        receipt = self.execute(prepared)
        self.assertEqual(receipt['after'], expected)
        self.assertEqual(receipt['sources'][0]['lifetime_usage'], cutover.MAX_COUNT - 7)
        self.assertEqual(self.client.mget(prepared['keys'][:3]),
                         [str(expected[k]) for k in ['minute', 'day', 'month']])
        self.assertEqual(self.execute(prepared), receipt)

    def pagination_preflight_and_execute(self):
        before = self.snapshot()
        for attempt in range(3):
            prepared = self.prepare()
            try:
                checked = cutover.execute(self.client, prepared, preflight_only=True)
                self.assertEqual(self.snapshot(), before)
                receipt = self.execute(prepared)
                return prepared, before, checked, receipt
            except runtime.redis.ResponseError as exc:
                # Real previews end at the next Redis minute. Retake only a
                # verified expiry with no writes; all other failures stay fatal.
                if (str(exc) != 'migration preview expired'
                        or int(self.client.time()[0]) < prepared['plan']['valid_until']
                        or attempt == 2):
                    raise
                self.assertEqual(self.snapshot(), before)
        self.fail('Pagination preview retry budget exhausted')

    def test_pagination_retake_requires_real_expiry_and_preserves_usage(self):
        self.client.set(self.old(), 23)
        clock = self.client.time()
        with patch.object(self.client, 'time', return_value=(int(clock[0]) - 60, clock[1])):
            expired = self.prepare()
        before = self.snapshot()
        with patch.object(self, 'prepare', side_effect=[expired, self.prepare()]) as prepare:
            prepared, baseline, checked, receipt = self.pagination_preflight_and_execute()
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(baseline, before)
        self.assertEqual(checked['before'], dict(minute=0, day=0, month=0))
        self.assertEqual(receipt['after'], dict(minute=0, day=23, month=23))
        self.assertEqual(self.client.get(self.old()), '23')
        self.assertEqual(self.execute(prepared), receipt)

    def test_real_scan_pagination_preserves_counts_and_replay(self):
        with self.client.pipeline() as pipe:
            for i in range(1300):
                pipe.set(self.old('day', suffix=f'page-{i}'), i % 7)
            for i in range(1300):
                pipe.set(f'unrelated:{i}', 'untouched')
            pipe.set(self.old(), 23)
            pipe.execute()
        prepared, before, checked, receipt = self.pagination_preflight_and_execute()
        self.assertEqual(len(prepared['plan']['sources'][0]['counter_indices']), 1301)
        self.assertGreater(checked['scan_pages'], 2)
        self.assertGreater(receipt['scan_pages'], 2)
        self.assertEqual(receipt['after'], dict(minute=0, day=23, month=23))
        after = self.snapshot()
        self.assertEqual(self.execute(prepared), receipt)
        self.assertEqual(self.snapshot(), after)
        for key, value in before.items():
            if not key.startswith('market:apisix:credential:'):
                self.assertEqual(after[key], value)

    def test_database_growth_after_preflight_rejected_inside_atomic_script(self):
        prepared = self.prepare()
        with self.client.pipeline() as pipe:
            for i in range(cutover.MAX_DATABASE_KEYS):
                pipe.set(f'unrelated:{i}', 'untouched')
            pipe.execute()
        metadata = self.client.dump('market:apisix:credential:consumer')
        with self.assertRaisesRegex(cutover.QuotaCutoverError, 'database key budget'):
            cutover.preflight(self.client)
        for script, keys, args in [
            (cutover.MIGRATE_SCRIPT, prepared['keys'], [json.dumps(prepared['plan'])]),
            (cutover.SYNC_SCRIPT, ['market:apisix:credential:consumer'], ['{}', '[]'])]:
            with self.assertRaisesRegex(runtime.redis.RedisError, 'database key budget'):
                self.client.eval(script, len(keys), *keys, *args)
        self.assertEqual(self.client.dump('market:apisix:credential:consumer'), metadata)
        self.assertEqual(self.client.exists(*prepared['keys'][:4]), 0)

    def test_preview_page_budget_empty_pages_duplicates_and_terminal_page(self):
        # Protocol fault injection only; real pagination has separate Redis coverage.
        exact = self.old('day', suffix='last-page')
        with patch.object(self.client, 'scan', side_effect=[
                (9, []), (4, [exact, exact]), (0, [self.old()]), (0, [])]):
            self.assertEqual(cutover.legacy_keys(self.client, 'route', 'consumer'),
                             sorted([exact, self.old()]))
        with patch.object(self.client, 'scan', return_value=(1, [])) as scan:
            with self.assertRaisesRegex(cutover.QuotaCutoverError, 'scan budget'):
                cutover.legacy_keys(self.client, 'route', 'consumer')
            self.assertEqual(scan.call_count, cutover.MAX_SCAN_PAGES)

    def test_unknown_topology_replica_and_scan_deadline_are_failclosed(self):
        for topology in [{}, {'cluster_enabled': 1}]:
            with patch.object(self.client, 'info', return_value=topology):
                self.assert_rejected_without_writes(lambda: cutover.preflight(self.client))
        with patch.object(self.client, 'info', side_effect=[{'cluster_enabled': 0}, {'role': 'slave'}]):
            self.assert_rejected_without_writes(lambda: cutover.preflight(self.client))
        budget = {'pages': 0, 'started': runtime.time.monotonic() - 3}
        self.assert_rejected_without_writes(lambda: cutover.legacy_keys(
            self.client, 'route', 'consumer', budget=budget))

    def test_atomic_time_deadline_rejects_before_writes(self):
        prepared = self.prepare()
        # Deterministic elapsed-clock fault injection in otherwise real Redis EVAL.
        script = cutover.MIGRATE_SCRIPT.replace("local started = redis.call('TIME')",
            "local started = redis.call('TIME'); started[1] = tostring(tonumber(started[1])-1)")
        self.assert_rejected_without_writes(lambda: self.client.eval(
            script, len(prepared['keys']), *prepared['keys'], json.dumps(prepared['plan'])))

    def test_atomic_page_budget_rejects_without_writes(self):
        # Each source needs two whole-database scans, even if MATCH finds no keys.
        self.credentials = [self.credential(f'c{i}') for i in range(16)]
        for _, consumer, _ in self.credentials:
            self.legacy_metadata(consumer)
        prepared = self.prepare()
        with self.client.pipeline() as pipe:
            for i in range(3000):
                pipe.set(f'unrelated:{i}', 'untouched')
            pipe.execute()
        self.assert_rejected_without_writes(lambda: self.execute(prepared))

    def test_source_key_byte_limits_and_ambiguous_identity_refuse(self):
        self.credentials = [self.credential(f'c{i}') for i in range(17)]
        self.assert_rejected_without_writes(self.prepare)
        self.credentials = [self.credential()]
        with self.client.pipeline() as pipe:
            for i in range(cutover.MAX_PLAN_KEYS):
                pipe.set(self.old('day', suffix=f'k{i}'), 0)
            pipe.execute()
        self.assert_rejected_without_writes(self.prepare)
        self.client.flushdb()
        self.legacy_metadata('consumer')
        self.credentials = [self.credential(extra='x' * cutover.MAX_PLAN_BYTES)]
        self.assert_rejected_without_writes(self.prepare)
        for route, consumer in [('route:alias', 'consumer'), ('route', 'consumer:alias')]:
            self.credentials = [self.credential(consumer, route)]
            self.assert_rejected_without_writes(self.prepare)
        self.credentials = [self.credential()]
        self.client.set(self.old('day', suffix='x' * cutover.MAX_KEY_BYTES), 0)
        self.assert_rejected_without_writes(self.prepare)

    def test_real_cluster_enabled_node_and_cli_are_explicitly_rejected(self):
        binary = os.environ['QUOTA_REDIS_SERVER'] if 'QUOTA_REDIS_SERVER' in os.environ else runtime.shutil.which('redis-server')
        with runtime.tempfile.TemporaryDirectory(prefix='market-quota-cluster-') as directory:
            for _ in range(100):
                with socket.socket() as data_socket, socket.socket() as bus_socket:
                    data_socket.bind(('127.0.0.1', 0))
                    port = data_socket.getsockname()[1]
                    if port > 55535:
                        continue
                    try:
                        bus_socket.bind(('127.0.0.1', port + 10000))
                    except OSError:
                        continue
                    break
            else:
                self.fail('No free local cluster port pair')
            server = subprocess.Popen([binary, '--bind', '127.0.0.1', '--port', str(port),
                '--cluster-enabled', 'yes',
                '--cluster-config-file', str(Path(directory) / 'nodes.conf'),
                '--save', '', '--appendonly', 'no'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            client = runtime.redis.Redis(host='127.0.0.1', port=port, decode_responses=True,
                socket_timeout=0.2, socket_connect_timeout=0.2,
                retry=runtime.redis.retry.Retry(runtime.redis.backoff.NoBackoff(), 0))
            try:
                for _ in range(100):
                    if server.poll() is not None:
                        self.fail('Cluster-enabled Redis failed: ' + server.stderr.read().decode())
                    try:
                        client.ping()
                        break
                    except runtime.redis.RedisError:
                        runtime.time.sleep(0.02)
                self.assertEqual(client.info('cluster')['cluster_enabled'], 1)
                with self.assertRaisesRegex(cutover.QuotaCutoverError, 'Cluster cutover unsupported'):
                    cutover.preflight(client)
                for operation in [lambda: cutover.preview(client, self.scope, self.credentials,
                        actor='test', request_id='cluster'),
                        lambda: cutover.sync_unused(client, 'meta', {}, 'route', 'consumer'),
                        lambda: self.execute(self.prepare(), client=client)]:
                    with self.assertRaisesRegex(cutover.QuotaCutoverError, 'Cluster cutover unsupported'):
                        operation()
                root = Path(__file__).resolve().parents[2]
                env = dict(os.environ, PYTHONPATH=str(root / 'backend'))
                command = [sys.executable, str(root / 'scripts/subscription_quota_migrate.py'),
                           '--redis-preflight', '--redis-url', f'redis://127.0.0.1:{port}/0']
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Cluster cutover unsupported', result.stderr)
                self.assertEqual(client.dbsize(), 0)
            finally:
                client.close()
                server.terminate()
                server.wait(timeout=5)
                server.stderr.close()

    def test_topology_cli_runs_without_business_database_and_is_readonly(self):
        root = Path(__file__).resolve().parents[2]
        url = 'unix://' + self.client.connection_pool.connection_kwargs['path']
        before = self.snapshot()
        result = subprocess.run([sys.executable, str(root / 'scripts/subscription_quota_migrate.py'),
            '--redis-preflight', '--redis-url', url],
            env=dict(os.environ, PYTHONPATH=str(root / 'backend')), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['topology'], 'standalone_primary')
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(QuotaCutoverTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("Skipped runtime tests do not count as passes.", file=sys.stderr)
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped and result.testsRun > 0 else 1)
