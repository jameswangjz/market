"""Execute the APISIX plugin in Lua against an isolated, real Redis server.

Run with lupa + redis installed and redis-server on PATH (or QUOTA_REDIS_SERVER).
Missing runtime dependencies explicitly skip these integration tests.
Direct execution exits nonzero on any skip; discovery reports skips explicitly.
"""
import concurrent.futures
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from zoneinfo import ZoneInfo

try:
    import redis
    try:
        from lupa.luajit21 import LuaRuntime
    except ImportError:
        from lupa import LuaRuntime
except ImportError:
    redis = LuaRuntime = None


PLUGIN = Path(__file__).resolve().parents[2] / "k8s/market-gateway-quota.lua"


def epoch(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())


def token(value):
    return value.replace("%", "%25").replace(":", "%3A")


def keys(scope, now, period_key):
    prefix = "market:apisix:quota:subscription:"
    day = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    return [f"{prefix}minute:{token(scope)}:{now // 60}",
            f"{prefix}day:{token(scope)}:{day}",
            f"{prefix}month:{token(scope)}:{token(period_key)}"]


class Gateway:
    """Only APISIX/resty transport is stubbed; policy Lua and Redis EVAL are real."""

    def __init__(self, client, now, failure=None):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.client = client
        self.now = now
        self.failure = failure
        globals_ = self.lua.globals()
        globals_.clock = lambda: self.now
        globals_.decode = lambda value: self.table(json.loads(value))
        globals_.metadata = self.metadata
        globals_.evaluate = self.evaluate
        globals_.connect_ok = failure != "connect"
        globals_.select_ok = failure != "select"
        self.lua.execute("""
            ngx = {time = function() return clock() end, var = {}}
            local core = {
                json = {decode = function(value)
                    local ok, result = pcall(decode, value)
                    if ok then return result end
                    return nil
                end},
                response = {exit = function(code, body) return code, body.message end},
                schema = {check = function() return true end}
            }
            package.preload['apisix.core'] = function() return core end
            package.preload['resty.redis'] = function()
                return {new = function()
                    return {
                        set_timeout = function() end,
                        connect = function() if connect_ok then return true end end,
                        select = function() if select_ok then return true end end,
                        close = function() end,
                        set_keepalive = function() end,
                        hgetall = function(_, key) return metadata(key) end,
                        eval = function(_, ...) return evaluate(...) end
                    }
                end}
            end
        """)
        self.plugin = self.lua.execute(PLUGIN.read_text())

    def table(self, value):
        if isinstance(value, dict):
            return self.lua.table_from({k: self.table(v) for k, v in value.items()})
        if isinstance(value, list):
            return self.lua.table_from([self.table(v) for v in value])
        return value

    def metadata(self, key):
        if self.failure == "metadata":
            return None
        values = self.client.hgetall(key)
        return self.table([item for pair in values.items() for item in pair])

    def evaluate(self, script, count, *args):
        if self.failure == "eval":
            return None
        try:
            return self.table(self.client.eval(script, count, *args))
        except redis.RedisError:
            return None

    def request(self, consumer="consumer", route="route", lookup=True, matched_route=None, **limits):
        context = dict(consumer_name=consumer)
        if matched_route is not None:
            context["matched_route"] = {"value": {"plugins": {
                "market-gateway-quota": {"route_key": matched_route, "lookup_consumer": True}}}}
        result = self.plugin.access(self.table(dict(route_key=route, lookup_consumer=lookup, **limits)),
                                    self.table(context))
        return result[0] if isinstance(result, tuple) else 200


@unittest.skipUnless(redis and LuaRuntime, "requires redis and lupa")
class SubscriptionQuotaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get("QUOTA_REDIS_SERVER") or shutil.which("redis-server")
        if not binary:
            raise unittest.SkipTest("requires redis-server or QUOTA_REDIS_SERVER")
        cls.temp = tempfile.TemporaryDirectory(prefix="market-quota-")
        socket = str(Path(cls.temp.name) / "redis.sock")
        cls.server = subprocess.Popen([binary, "--port", "0", "--unixsocket", socket,
                                       "--save", "", "--appendonly", "no"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cls.client = redis.Redis(unix_socket_path=socket, decode_responses=True)
        for _ in range(100):
            try:
                cls.client.ping()
                return
            except redis.RedisError:
                time.sleep(0.02)
        cls.server.terminate()
        cls.server.wait(timeout=5)
        cls.temp.cleanup()
        raise RuntimeError("isolated Redis failed to start")

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server.terminate()
        cls.server.wait(timeout=5)
        cls.server.stderr.close()
        cls.temp.cleanup()

    def setUp(self):
        self.client.flushdb()  # Dedicated ephemeral server, never deployment Redis.
        self.now = epoch("2031-01-31T12:00:00")
        self.period = dict(start_at=epoch("2031-01-31T00:00:00"),
                           end_at=epoch("2031-02-28T00:00:00"), period_key="2031-01-31",
                           route_key="route", rate_limit_per_minute=100,
                           daily_quota=10, monthly_quota=20)
        self.publish()

    def publish(self, periods=None, consumer="consumer", **extra):
        values = dict(mode="subscription", status="active", quota_scope="combo",
                      subscription_periods=json.dumps(periods if periods is not None else [self.period]))
        values.update(extra)
        self.client.hset("market:apisix:credential:" + consumer, mapping=values)

    def gateway(self, now=None, failure=None):
        return Gateway(self.client, self.now if now is None else now, failure)

    def usage(self, now=None, period=None, scope="combo"):
        return [int(v or 0) for v in self.client.mget(keys(scope, self.now if now is None else now,
                                                         period or self.period["period_key"]))]

    def test_daily_midnight_is_beijing(self):
        self.period["daily_quota"] = 1
        self.publish()
        before = epoch("2031-01-31T23:59:59")
        after = before + 1
        self.assertEqual(self.gateway(before).request(), 200)
        self.assertEqual(self.gateway(before).request(), 429)
        self.assertEqual(self.gateway(after).request(), 200)
        self.assertEqual(self.usage(before)[1:], [1, 2])
        self.assertEqual(self.usage(after)[1:], [1, 2])

    def test_original_anchor_month_and_half_open_boundaries(self):
        self.period["monthly_quota"] = 1
        next_period = dict(self.period, start_at=self.period["end_at"],
                           end_at=epoch("2031-03-31T00:00:00"), period_key="2031-02-28")
        self.publish([self.period, next_period])
        self.assertEqual(self.gateway().request(), 200)
        self.assertEqual(self.gateway(epoch("2031-02-01T00:00:00")).request(), 429)
        self.assertEqual(self.gateway(self.period["end_at"] - 1).request(), 429)
        self.assertEqual(self.gateway(self.period["end_at"]).request(), 200)
        self.assertEqual(self.gateway(next_period["end_at"]).request(), 403)
        self.assertEqual(self.client.ttl(keys("combo", self.now, self.period["period_key"])[2]), -1)

    def test_expired_future_route_and_overlap_reject_without_counters(self):
        self.assertEqual(self.gateway(self.period["start_at"] - 1).request(), 403)
        self.assertEqual(self.gateway(self.period["end_at"]).request(), 403)
        self.assertEqual(self.gateway().request(route="different-version"), 403)
        self.publish([self.period, dict(self.period, period_key="overlap")])
        self.assertEqual(self.gateway().request(), 403)
        self.assertEqual(self.usage(), [0, 0, 0])

    def test_malformed_incomplete_and_missing_metadata_fail_closed(self):
        for value in ["oops", "{}", "[]", "null", '[{"start_at": 0}]']:
            with self.subTest(value=value):
                self.publish(subscription_periods=value)
                self.assertEqual(self.gateway().request(), 403)
        for field, value in [("rate_limit_per_minute", 0), ("daily_quota", -1),
                             ("monthly_quota", "0"), ("start_at", 1.5), ("period_key", "")]:
            with self.subTest(field=field):
                period = dict(self.period, **{field: value})
                self.publish([period])
                self.assertEqual(self.gateway().request(), 403)
        for field in ["mode", "status", "quota_scope", "subscription_periods"]:
            self.publish()
            self.client.hdel("market:apisix:credential:consumer", field)
            self.assertEqual(self.gateway().request(), 403)
        self.client.delete("market:apisix:credential:consumer")
        self.assertEqual(self.gateway().request(), 403)
        self.assertEqual(self.usage(), [0, 0, 0])

    def test_rate_and_zero_unlimited_denials_roll_back_all_counters(self):
        self.period.update(rate_limit_per_minute=2, daily_quota=0, monthly_quota=0)
        self.publish(total_quota="1")
        gateway = self.gateway()
        self.assertEqual([gateway.request() for _ in range(4)], [200, 200, 429, 429])
        self.assertEqual(self.usage(), [2, 2, 2])
        self.assertEqual(self.gateway(self.now + 60).request(), 200)
        self.assertEqual(self.usage(self.now + 60), [1, 3, 3])
        self.assertEqual(list(self.client.scan_iter("market:apisix:quota:total:*")), [])

    def test_daily_and_monthly_denials_roll_back_minute(self):
        for field in ["daily_quota", "monthly_quota"]:
            with self.subTest(field=field):
                self.client.flushdb()
                self.period.update(daily_quota=0, monthly_quota=0)
                self.period[field] = 1
                self.publish()
                self.assertEqual(self.gateway().request(), 200)
                self.assertEqual(self.gateway().request(), 429)
                self.assertEqual(self.usage(), [1, 1, 1])

    def test_renewal_rotation_version_change_and_migration_preserve_usage(self):
        self.period.update(monthly_quota=3)
        self.publish()
        counter_keys = keys("combo", self.now, self.period["period_key"])
        # Simulate a validated migration; production refuses used legacy credentials.
        self.client.set(counter_keys[2], 2)
        legacy = "market:apisix:quota:month:route:consumer:2031-01"
        self.client.set(legacy, 17)
        self.assertEqual(self.gateway().request(), 200)
        self.period.update(route_key="version-two", rate_limit_per_minute=200,
                           end_at=epoch("2031-03-31T00:00:00"))
        self.publish(consumer="regenerated")
        self.assertEqual(self.gateway().request(consumer="regenerated", route="version-two"), 429)
        self.assertEqual(self.usage(), [1, 1, 3])
        self.assertEqual(self.client.get(legacy), "17")
        self.period["monthly_quota"] = 4
        self.publish(consumer="regenerated")
        self.assertEqual(self.gateway().request(consumer="regenerated", route="version-two"), 200)
        self.assertEqual(self.usage(), [2, 2, 4])

    def test_atomic_concurrency(self):
        self.period.update(rate_limit_per_minute=100, daily_quota=0, monthly_quota=7)
        self.publish()
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(lambda _: self.gateway().request(), range(40)))
        self.assertEqual(statuses.count(200), 7)
        self.assertEqual(statuses.count(429), 33)
        self.assertEqual(self.usage(), [7, 7, 7])

    def test_redis_failures_and_revocation(self):
        for failure in ["connect", "select", "metadata", "eval"]:
            with self.subTest(failure=failure):
                self.assertEqual(self.gateway(failure=failure).request(), 503)
        self.publish(status="revoked")
        self.assertEqual(self.gateway().request(), 403)
        self.assertEqual(self.usage(), [0, 0, 0])

    def test_real_redis_errors_fail_closed_before_counter_mutation(self):
        counter_keys = keys("combo", self.now, self.period["period_key"])
        for bad in ["not-an-integer", "01", "9007199254740991"]:
            with self.subTest(value=bad):
                self.client.set(counter_keys[2], bad)
                self.assertEqual(self.gateway().request(), 503)
                self.assertEqual(self.client.mget(counter_keys), [None, None, bad])
        self.client.delete(counter_keys[2])
        self.client.hset(counter_keys[2], mapping={"wrong": "type"})
        self.assertEqual(self.gateway().request(), 503)
        self.assertEqual(self.client.mget(counter_keys[:2]), [None, None])

    def test_scope_tokens_are_unambiguous(self):
        self.publish(quota_scope="combo:one%", subscription_periods=json.dumps(
            [dict(self.period, period_key="anchor:one%")]))
        self.assertEqual(self.gateway().request(), 200)
        self.assertEqual(self.client.get(keys("combo:one%", self.now, "anchor:one%")[2]), "1")

    def test_subscription_lookup_is_required_even_with_consumer_flag_false(self):
        self.period["rate_limit_per_minute"] = 1
        self.publish()
        self.assertEqual(self.gateway().request(lookup=False), 200)
        self.assertEqual(self.gateway().request(lookup=False), 429)
        self.assertEqual(self.gateway().request(lookup=False, route="other"), 403)
        self.assertEqual(self.gateway().request(lookup=False, matched_route="other"), 403)
        self.publish(subscription_periods="[]")
        self.assertEqual(self.gateway().request(lookup=False), 403)
        self.client.delete("market:apisix:credential:consumer")
        self.assertEqual(self.gateway().request(lookup=False, matched_route="route"), 403)

    def test_legacy_limits_and_total_unchanged(self):
        self.client.delete("market:apisix:credential:consumer")
        self.client.hset("market:apisix:credential:consumer",
                         mapping=dict(status="active", daily_quota=0, monthly_quota=0, total_quota=1))
        self.assertEqual(self.gateway().request(), 200)
        self.assertEqual(self.gateway().request(), 403)
        self.assertEqual(self.client.get("market:apisix:quota:total:route:consumer"), "1")
        self.assertEqual(self.gateway().request(consumer="no-metadata", lookup=False), 200)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SubscriptionQuotaTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print("Skipped runtime tests do not count as passes.", file=sys.stderr)
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped and result.testsRun > 0 else 1)
