"""Run after deployment against real APISIX + its policy Redis; no backend writes.

Requires redis-py. Set APISIX_ADMIN_KEY and use --admin-url, --gateway-url,
--redis-url, --mock-url for the target deployment. The mock health endpoint must
return 200 with auth mode none. Fixtures use random routes/consumers/raw keys and
are deleted in finally; only this fixture's metadata and scope keys are touched.

Redis contract (also used by notification/usage readers):
  market:apisix:credential:<consumer> hash:
    mode=subscription, status=active, quota_scope=<combinationID>,
    subscription_periods=[{start_at,end_at,period_key,route_key,
                           rate_limit_per_minute,daily_quota,monthly_quota}]
  market:apisix:quota:subscription:minute:<scope>:<floor(epoch/60)>
  market:apisix:quota:subscription:day:<scope>:<Beijing YYYY-MM-DD>
  market:apisix:quota:subscription:month:<scope>:<original-anchor period_key>
Scope/period tokens replace '%' with '%25', then ':' with '%3A'. Month keys
have no TTL; minute expiry = next minute + 10s; day expiry = next Beijing
midnight + 86400s. Daily/monthly 0 means unlimited; minute rate must be positive.
Legacy migration must preserve usage or refuse activation (409), never reset it.
This script validates new managed credentials; it does not complete BE011's
strict legacy migration acceptance. Historical clock boundaries are covered by
backend/tests/test_subscription_quota.py with real Lua + Redis execution.

Production metadata must be derived from current subscription terms in committed
DB state; these isolated synthetic fixtures do not validate backend transaction
visibility. Main must not seed used legacy counters without a validated migration;
BE011 remains partial while that migration is unresolved.

Monthly keys currently persist indefinitely and can grow without bound. BE019's
controlled expiry/cleanup is pending and blocked by counter migration: cleanup
must first durably migrate usage and prove the same scope/period cannot be reused
by renewal, regeneration, or metadata replay to gain quota. No cleanup or monthly
TTL is implemented here. The 180-day message retention policy does not apply to
quota counters and must not be used as their expiry policy.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import secrets
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import redis


def token(value):
    return value.replace("%", "%25").replace(":", "%3A")


def http(method, url, headers=None, body=None):
    request = Request(url, method=method, headers=headers or {},
                      data=None if body is None else json.dumps(body).encode())
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except HTTPError as response:
        return response.code, response.read()


def run(args):
    if not args.admin_key:
        raise RuntimeError("Set APISIX_ADMIN_KEY or --admin-key")
    upstream = urlparse(args.mock_url)
    if upstream.scheme != "http" or not upstream.hostname or upstream.path not in {"", "/"}:
        raise ValueError("--mock-url must be an HTTP origin with auth mode none")
    client = redis.Redis.from_url(args.redis_url, decode_responses=True)
    client.ping()
    marker = "quota-acceptance-" + secrets.token_hex(12)
    scope = marker + "-combo"
    routes = [marker + "-v1", marker + "-v2"]
    consumers = [marker + "-c1", marker + "-c2"]
    raw_keys = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
    created_routes, created_consumers, metadata_keys = [], [], []
    checks = []
    prefix = "market:apisix:quota:subscription:"
    period_key = marker + "-original-anchor"
    month_key = prefix + "month:" + token(scope) + ":" + token(period_key)
    now = int(time.time())
    period = dict(start_at=now - 60, end_at=now + 3600, period_key=period_key,
                  route_key=routes[0], rate_limit_per_minute=10000,
                  daily_quota=0, monthly_quota=3)

    def admin(method, path, body=None):
        return http(method, args.admin_url.rstrip("/") + path,
                    {"X-API-KEY": args.admin_key}, body)

    def publish(index, periods=None, **extra):
        key = "market:apisix:credential:" + consumers[index]
        if key not in metadata_keys:
            if client.exists(key):
                raise RuntimeError("Unexpected fixture metadata collision")
            metadata_keys.append(key)
        values = dict(mode="subscription", status="active", quota_scope=scope,
                      subscription_periods=json.dumps(periods if periods is not None else
                                                       [dict(period, route_key=routes[index])]))
        values.update(extra)
        client.hset(key, mapping=values)

    def request(index=0, route_index=None, raw=True):
        route = routes[index if route_index is None else route_index]
        return http("GET", args.gateway_url.rstrip("/") + "/__subscription_quota/" + route + "/health",
                    {"X-API-Key": raw_keys[index]} if raw else {})[0]

    def expect(name, expected, actual):
        if actual != expected:
            raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")
        checks.append(name)

    try:
        for index, route in enumerate(routes):
            status, _ = admin("GET", "/routes/" + route)
            expect("isolated route absent", 404, status)
            payload = dict(uri="/__subscription_quota/" + route + "/*",
                           plugins={"key-auth": {}, "market-gateway-quota": {
                               "route_key": route, "lookup_consumer": True},
                               "proxy-rewrite": {"regex_uri": [
                                   "^/__subscription_quota/" + route + "/(.*)", "/$1"]}},
                           upstream={"type": "roundrobin", "nodes": {
                               f"{upstream.hostname}:{upstream.port or 80}": 1}})
            created_routes.append(route)
            status, body = admin("PUT", "/routes/" + route, payload)
            if status not in {200, 201}:
                raise RuntimeError(f"Fixture route creation failed: {status} {body[:200]!r}")
            consumer = consumers[index]
            status, _ = admin("GET", "/consumers/" + consumer)
            expect("isolated consumer absent", 404, status)
            created_consumers.append(consumer)
            status, body = admin("PUT", "/consumers/" + consumer, {
                "username": consumer, "plugins": {"key-auth": {"key": raw_keys[index]},
                    "market-gateway-quota": {"route_key": route, "lookup_consumer": False}}})
            if status not in {200, 201}:
                raise RuntimeError(f"Fixture consumer creation failed: {status} {body[:200]!r}")
            publish(index)
        # APISIX watches etcd asynchronously. A route with no key is read-only.
        for _ in range(100):
            if request(raw=False) == 401:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("APISIX fixture route did not become ready")
        expect("raw key required", 401, request(raw=False))
        expect("first managed request", 200, request())
        expect("regenerated/version consumer shares usage", 200, request(1))
        expect("third shared request", 200, request())
        expect("shared quota rejects", 429, request(1))
        expect("denial preserves month usage", "3", client.get(month_key))
        publish(0, [dict(period, end_at=now + 7200)])
        expect("renewal preserves exhausted usage", 429, request())
        expect("consumer cannot cross route", 403, request(0, 1))
        publish(0, [dict(period, start_at=now + 3600, end_at=now + 7200)])
        expect("future subscription inactive", 403, request())
        publish(0, [dict(period, end_at=now - 1)])
        expect("expired subscription", 403, request())
        publish(0, [period, dict(period, period_key="overlap")])
        expect("ambiguous overlap", 403, request())
        publish(0, subscription_periods="[]")
        expect("missing active period", 403, request())
        publish(0, subscription_periods="invalid-json")
        expect("malformed metadata", 403, request())
        publish(0, status="revoked")
        expect("revoked credential", 403, request())
        publish(0, [dict(period, daily_quota=0, monthly_quota=0)])
        publish(1, [dict(period, route_key=routes[1], daily_quota=0, monthly_quota=0)])
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda i: request(i % 2), range(12)))
        expect("zero daily/monthly unlimited", [200] * 12, statuses)
        expect("shared usage after unlimited requests", "15", client.get(month_key))
        publish(0, [dict(period, monthly_quota=20)])
        publish(1, [dict(period, route_key=routes[1], monthly_quota=20)])
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda i: request(i % 2), range(20)))
        expect("concurrent shared quota accepts remaining allowance", 5, statuses.count(200))
        expect("concurrent shared quota denies excess", 15, statuses.count(429))
        expect("concurrent denial preserves usage", "20", client.get(month_key))
        minute = int(time.time()) // 60
        # Seed both sides of a possible clock rollover, only in this fixture scope.
        for bucket in [minute, minute + 1]:
            minute_key = prefix + "minute:" + token(scope) + ":" + str(bucket)
            client.set(minute_key, 5, ex=120)
        publish(0, [dict(period, rate_limit_per_minute=5, daily_quota=0, monthly_quota=0)])
        expect("Lua minute enforcement", 429, request())
        expect("minute denial rolls back monthly usage", "20", client.get(month_key))
        expect("monthly counter persistent", -1, client.ttl(month_key))
        print(json.dumps(dict(result="passed", checks=checks, legacy_migration="BE011 partial"), indent=2))
    finally:
        failures = []
        for consumer in created_consumers:
            try:
                status, _ = admin("DELETE", "/consumers/" + consumer)
                if status not in {200, 204, 404}:
                    failures.append(f"consumer {consumer}: HTTP {status}")
            except Exception as exc:
                failures.append(f"consumer {consumer}: {exc}")
        for route in created_routes:
            try:
                status, _ = admin("DELETE", "/routes/" + route)
                if status not in {200, 204, 404}:
                    failures.append(f"route {route}: HTTP {status}")
            except Exception as exc:
                failures.append(f"route {route}: {exc}")
        try:
            counters = list(client.scan_iter(match=prefix + "*:" + token(scope) + ":*"))
            if metadata_keys or counters:
                client.delete(*(metadata_keys + counters))
        except Exception as exc:
            failures.append(f"Redis fixture cleanup: {exc}")
        client.close()
        if failures:
            raise RuntimeError("Fixture cleanup failed: " + "; ".join(failures))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", default="http://market-apisix:9180/apisix/admin")
    parser.add_argument("--gateway-url", default="http://market-apisix:9080")
    parser.add_argument("--redis-url", default="redis://market-redis:6379/0")
    parser.add_argument("--mock-url", default="http://market-mock-api:8300")
    parser.add_argument("--admin-key", default=os.environ.get("APISIX_ADMIN_KEY"))
    run(parser.parse_args())
