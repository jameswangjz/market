"""Execute new plugin under LuaJIT with isolated Redis/HTTP/APISIX adapters."""
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import unittest

try:
    from lupa.luajit21 import LuaRuntime
except ImportError:
    LuaRuntime = None


STAMP = int(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
INGEST_SECRET = "owned-ingest-" + "x" * 40
PROBE_SECRET = "owned-probe-" + "y" * 40


class Harness:
    def __init__(self, shared=None):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.shared = shared if shared is not None else {"queue": [], "hashes": {}}
        self.posts, self.logs, self.random_counter = [], [], 0
        g = self.lua.globals()
        g.py_json = lambda table: json.dumps(self.convert(table), separators=(",", ":"))
        g.py_hmac = lambda key, message: hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
        g.py_hex = lambda value: value if len(value) == 64 and all(c in "0123456789abcdef" for c in value) else value.encode().hex()
        def random_bytes(size, strong=True):
            self.random_counter += 1
            return chr(65 + self.random_counter % 26) * size
        g.py_random = random_bytes
        g.py_log = lambda message: self.logs.append(message)
        g.py_append = self.append
        g.py_range = lambda key, first, last: self.lua.table_from(self.shared["queue"][first:last + 1])
        g.py_ack = self.ack
        g.py_hsetnx = self.hsetnx
        g.py_hincrby = self.hincrby
        g.py_post = self.post
        g.py_validate_cidrs = lambda cidrs: all(ipaddress.ip_network(c, strict=True) for c in cidrs.values())
        g.py_match_cidrs = lambda cidrs, remote: any(ipaddress.ip_address(remote) in ipaddress.ip_network(c, strict=True)
                                                    for c in cidrs.values())
        self.lua.execute('''
NOW=STAMP; METHOD="HEAD"; HEADERS={}; BUFFER={}; HTTP_STATUS=200; HTTP_CONNECT=true; REDIS_UP=true
os.getenv=function(key) return ENV[key] end
ngx={is_subrequest=false, status=200, header={}, worker={pid=function() return 7 end},
 time=function() return math.floor(NOW) end, now=function() return NOW end,
 var={uri="/gateway/route/health",args="",remote_addr="10.42.0.5",request_time="0.125",upstream_status="200"},
 req={get_method=function() return METHOD end,
      set_header=function(k,v) HEADERS[k]=v end, clear_header=function(k) HEADERS[k]=nil end},
 timer={every=function(_,cb) TIMER_COUNT=(TIMER_COUNT or 0)+1;TICK=cb;return true end}}
local manager={wrap_schema=function(_,s) return s end,
 wrap_metadata_schema=function(_,s) s.properties.max_pending_entries={};return s end,
 add_entry=function() return false end,
 add_entry_to_new_processor=function(_,conf,entry,ctx,cb) BUFFER[#BUFFER+1]=entry;FLUSH=cb end}
package.preload["apisix.core"]=function() return {
 schema={TYPE_METADATA="metadata",check=function() return true end},
 json={encode=py_json,array_mt={}}, request={header=function(_,name) return HEADERS[name] end},
 log={error=py_log}} end
package.preload["apisix.utils.batch-processor-manager"]=function() return {new=function() return manager end} end
package.preload["resty.random"]=function() return {bytes=py_random} end
package.preload["resty.string"]=function() return {to_hex=py_hex} end
package.preload["resty.ipmatcher"]=function() return {new=function(cidrs)
 py_validate_cidrs(cidrs);return {match=function(_,remote) return py_match_cidrs(cidrs,remote) end} end} end
package.preload["resty.openssl.hmac"]=function() return {new=function(key,alg)
 assert(alg=="sha256");return {final=function(_,message) return py_hmac(key,message) end} end} end
package.preload["resty.redis"]=function() return {new=function() return {
 set_timeout=function() end, connect=function() return REDIS_UP end, select=function() return true end,
 hsetnx=function(_,...) return py_hsetnx(...) end, hincrby=function(_,...) return py_hincrby(...) end,
 eval=function(_,script,n,key,value) assert(n==1 and script:find("RPUSH") and script:find("LLEN"));return py_append(key,value) end,
 lrange=function(_,...) return py_range(...) end, lrem=function(_,...) return py_ack(...) end,
 close=function() end,set_keepalive=function() return true end} end} end
package.preload["resty.http"]=function() return {new=function() return {
 set_timeout=function(_,v) assert(v==1000) end,
 connect=function(_,host,port) assert(host=="market-api" and port==8000);return HTTP_CONNECT end,
 request=function(_,request) py_post(request);return {status=HTTP_STATUS} end,
 close=function() end,read_body=function() error("must never buffer receiver body") end} end} end
'''.replace("STAMP", str(STAMP)))
        g.ENV = self.lua.table_from({"SLA_GATEWAY_INGEST_SECRET": INGEST_SECRET,
                                     "SLA_PROBE_SECRET": PROBE_SECRET, "HOSTNAME": "owned-apisix"})
        self.plugin = self.lua.execute(Path("k8s/market-sla-logger.lua").read_text())
        self.conf = self.lua.table_from({})
        self.plugin.init()

    def convert(self, value):
        if hasattr(value, "items"):
            data = dict(value.items())
            if not data:
                return []
            if set(data) == set(range(1, len(data) + 1)):
                return [self.convert(data[i]) for i in range(1, len(data) + 1)]
            return {key: self.convert(item) for key, item in data.items()}
        return int(value) if isinstance(value, float) and value.is_integer() else value

    def append(self, key, value):
        assert key == "market:sla:terminal:pending"
        if len(self.shared["queue"]) >= 1024:
            return 0
        self.shared["queue"].append(value)
        return 1

    def ack(self, key, count, value):
        assert key == "market:sla:terminal:pending" and count == 1
        try:
            self.shared["queue"].remove(value)
            return 1
        except ValueError:
            return 0

    def hsetnx(self, key, field, value):
        table = self.shared["hashes"].setdefault(key, {})
        if field in table:
            return 0
        table[field] = value
        return 1

    def hincrby(self, key, field, delta):
        table = self.shared["hashes"].setdefault(key, {})
        table[field] = table.get(field, 0) + delta
        return table[field]

    def post(self, request):
        value = self.convert(request)
        assert value["method"] == "POST" and value["path"] == "/internal/sla/gateway-events"
        assert value["headers"]["X-SLA-Gateway-Key"] == INGEST_SECRET
        self.posts.append(json.loads(value["body"]))

    def context(self, consumer="market-consumer-ab12"):
        ctx = self.lua.table_from({"consumer_name": consumer} if consumer else {})
        labels = self.lua.table_from({"market_managed": "true", "market_product_id": "product",
            "market_version_id": "version", "market_version": "v1", "market_sla_probe_consumer": "owned-probe-consumer",
            "market_sla_probe_route_id": "route",
            "market_sla_probe_config_version": "owned-config-v1",
            "market_sla_probe_source_cidrs": "10.42.0.0/16",
            "market_sla_probe_method": "HEAD", "market_sla_probe_path": "/gateway/route/health"})
        ctx.matched_route = self.lua.table_from({"value": self.lua.table_from({"id": "route", "labels": labels})})
        return ctx

    def flush(self, index=1):
        return self.lua.globals().FLUSH(self.lua.table_from([self.lua.globals().BUFFER[index]]))

    def tick(self):
        self.lua.globals().TICK(False)

    def sign(self, **changes):
        from app.sla_probe_runtime import probe_headers
        values = dict(route_id="route", product_id="product", version_id="version", method="HEAD",
                      path="/gateway/route/health", instant=datetime.fromtimestamp(STAMP, timezone.utc))
        values.update(changes)
        for name, value in probe_headers(PROBE_SECRET, **values).items():
            self.lua.globals().HEADERS[name] = value


@unittest.skipIf(LuaRuntime is None, "LuaJIT binding required for executable plugin tests")
class GatewayLoggerTests(unittest.TestCase):
    def test_probe_source_cidr_uses_remote_addr_never_forwarded_header(self):
        for cidrs, remote, allowed in (("10.42.0.0/16", "10.42.0.5", True),
                ("10.42.0.0/16", "203.0.113.5", False),
                ("", "10.42.0.5", False), ("invalid", "10.42.0.5", False),
                ("0.0.0.0/0", "10.42.0.5", False),
                ("10.42.0.0/16,2001:db8::/32", "2001:db8::5", True)):
            h = Harness(); ctx = h.context("owned-probe-consumer"); h.sign()
            ctx.matched_route.value.labels.market_sla_probe_source_cidrs = cidrs
            h.lua.globals().ngx.var.remote_addr = remote
            h.lua.globals().HEADERS["X-Forwarded-For"] = "10.42.0.5"
            h.plugin.rewrite(h.conf, ctx); h.plugin.access(h.conf, ctx)
            self.assertEqual(bool(ctx.market_sla_probe_verified), allowed, (cidrs, remote))

    def test_actual_split_phases_use_database_route_identity_not_admin_route_key(self):
        h = Harness(); ctx = h.context(consumer=None); g = h.lua.globals()
        db_route_id = "0123456789abcdef0123456789abcdef"
        ctx.matched_route.value.labels.market_sla_probe_route_id = db_route_id
        h.sign(route_id=db_route_id)
        # Actual init.lua: global rewrite -> native key-auth rewrite ->
        # consumer merge -> global access -> route quota access.
        h.plugin.rewrite(h.conf, ctx)
        self.assertTrue(ctx.market_sla_probe_signature_valid)
        self.assertEqual(ctx.market_sla_probe_binding.gateway_route_id, "route")
        self.assertEqual(ctx.market_sla_probe_binding.database_route_id, db_route_id)
        self.assertFalse(ctx.market_sla_probe_verified)
        ctx.consumer_name = "owned-probe-consumer"
        g.ngx.var.uri = "/health"
        h.plugin.access(h.conf, ctx)
        self.assertTrue(ctx.market_sla_probe_verified)
        self.assertEqual(ctx.market_sla_probe_original_path, "/gateway/route/health")
        self.assertEqual(ctx.market_sla_probe_original_method, "HEAD")
        self.assertEqual(ctx.market_sla_probe_binding.path, "/gateway/route/health")
        self.assertEqual(ctx.market_sla_probe_binding.config_version, "owned-config-v1")
        self.assertEqual(ctx.market_sla_traffic.kind, "probe")
        self.assertIsNone(g.HEADERS["X-Market-SLA-Signature"])
        h = Harness(); ctx = h.context("owned-probe-consumer")
        ctx.matched_route.value.labels.market_sla_probe_route_id = db_route_id
        h.sign(route_id="route")
        h.plugin.rewrite(h.conf, ctx); h.plugin.access(h.conf, ctx)
        self.assertFalse(ctx.market_sla_probe_verified)

    def test_signed_original_path_must_match_actual_route_key_prefix(self):
        h = Harness(); ctx = h.context("owned-probe-consumer")
        path = "/gateway/different-route/health"
        ctx.matched_route.value.labels.market_sla_probe_path = path
        h.lua.globals().ngx.var.uri = path
        h.sign(path=path)
        h.plugin.rewrite(h.conf, ctx)
        self.assertFalse(ctx.market_sla_probe_signature_valid)
        h.plugin.access(h.conf, ctx)
        self.assertFalse(ctx.market_sla_probe_verified)

    def test_private_probe_candidate_rejects_forged_ctx_and_changed_binding(self):
        for changed in ("forged", "config", "route", "version", "path", "method", "consumer"):
            h = Harness(); ctx = h.context("owned-probe-consumer"); h.sign()
            if changed != "forged":
                h.plugin.rewrite(h.conf, ctx)
            else:
                ctx.market_sla_probe_signature_valid = True
                ctx.market_sla_probe_verified = True
                ctx.market_sla_probe_original_path = "/gateway/route/health"
            if changed == "route":
                ctx.matched_route.value.id = "different-route"
            elif changed != "forged":
                key = {"config":"market_sla_probe_config_version", "version":"market_version_id",
                    "path":"market_sla_probe_path", "method":"market_sla_probe_method",
                    "consumer":"market_sla_probe_consumer"}[changed]
                ctx.matched_route.value.labels[key] = "different"
            h.lua.globals().ngx.var.uri = "/health"
            h.plugin.access(h.conf, ctx)
            self.assertFalse(ctx.market_sla_probe_verified)
            self.assertIsNone(ctx.market_sla_probe_original_path)
            self.assertIsNone(ctx.market_sla_probe_binding)

    def test_apisix_loader_init_hook_starts_drainer_without_request(self):
        h = Harness()
        self.assertIsNone(h.plugin.init_worker)
        self.assertIsNotNone(h.lua.globals().TICK)
        h.plugin.init()
        self.assertEqual(h.lua.globals().TIMER_COUNT, 1)
        h.tick()
        self.assertTrue(h.shared["hashes"].get("market:sla:terminal:producers"))

    def test_server_id_overwrites_client_and_log_is_terminal_once(self):
        h = Harness(); ctx = h.context(); g = h.lua.globals()
        g.HEADERS["X-Request-Id"] = "caller-controlled"
        h.plugin.rewrite(h.conf, ctx)
        self.assertRegex(ctx.market_sla_request_id, r"^gw_[a-f0-9]{32}$")
        self.assertEqual(g.HEADERS["X-Request-Id"], ctx.market_sla_request_id)
        g.ngx.var.upstream_status = "502, 200"
        g.ngx.var.request_time = "12.345"
        h.plugin.log(h.conf, ctx); h.plugin.log(h.conf, ctx)
        self.assertEqual(len(g.BUFFER), 1)
        self.assertEqual(h.posts, [])
        self.assertTrue(h.flush()[0]); h.tick()
        event = h.posts[0]["events"][0]
        self.assertEqual(event["upstream_statuses"], [502, 200])
        self.assertEqual(event["duration_ms"], 12345)
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(h.shared["queue"], [])

    def test_auth_failure_still_logged_and_client_traffic_header_not_trusted(self):
        h = Harness(); ctx = h.context(consumer=None); g = h.lua.globals()
        g.HEADERS["X-Market-SLA-Probe"] = "true"; g.HEADERS["Authorization"] = "private-buyer-token"
        g.ngx.status = 401
        h.plugin.rewrite(h.conf, ctx); h.plugin.log(h.conf, ctx); h.flush(); h.tick()
        record = h.posts[0]["events"][0]
        self.assertEqual(record["traffic"], "business")
        self.assertEqual(record["reason"], "unknown")
        self.assertEqual(record["status_code"], 401)
        self.assertNotIn("private-buyer-token", json.dumps(h.posts))

    def test_native_key_auth_rejection_requires_server_route_and_no_upstream(self):
        for upstream, enabled, expected in ((False, True, "buyer_credentials"),
                                            (True, True, "unknown"), (False, False, "unknown")):
            h = Harness(); ctx = h.context(consumer=None); g = h.lua.globals()
            if enabled:
                ctx.matched_route.value.plugins = h.lua.table_from({"key-auth": h.lua.table()})
            g.ngx.status = 401
            g.ngx.var.upstream_status = ""
            if upstream:
                g.ngx.var.upstream_status = "401"
                g.ngx.var.upstream_addr = "owned-upstream:80"
            h.plugin.rewrite(h.conf, ctx); h.plugin.log(h.conf, ctx); h.flush(); h.tick()
            record = h.posts[0]["events"][0]
            self.assertEqual(record["reason"], expected)
            if expected == "buyer_credentials":
                self.assertIs(record["authorized"], False)
                self.assertEqual(record["responsibility"], "caller")
                self.assertTrue(record["evidence_id"].startswith("key-auth:gw_"))

    def test_probe_real_python_signature_and_independent_auth_required(self):
        h = Harness(); h.sign(); ctx = h.context("owned-probe-consumer")
        h.plugin.rewrite(h.conf, ctx)
        self.assertFalse(ctx.market_sla_probe_verified)
        self.assertTrue(ctx.market_sla_probe_signature_valid)
        h.plugin.access(h.conf, ctx)
        self.assertTrue(ctx.market_sla_probe_verified)
        self.assertEqual(ctx.market_sla_traffic.kind, "probe")
        self.assertIsNone(h.lua.globals().HEADERS["X-Market-SLA-Signature"])
        h.plugin.log(h.conf, ctx); h.flush(); h.tick()
        self.assertEqual(h.posts[0]["events"][0]["traffic"], "probe")

    def test_same_signature_with_buyer_key_or_no_auth_not_exempt(self):
        for consumer in (None, "market-consumer-ab12"):
            h = Harness(); h.sign(); ctx = h.context(consumer)
            h.plugin.rewrite(h.conf, ctx); h.plugin.access(h.conf, ctx)
            self.assertFalse(ctx.market_sla_probe_verified)

    def test_probe_wrong_signature_path_method_product_version_or_stale_denied(self):
        variants = ({"path": "/elsewhere"}, {"method": "GET"}, {"product_id": "foreign"},
                    {"version_id": "foreign"}, {"instant": datetime.fromtimestamp(STAMP - 31, timezone.utc)})
        for changes in variants:
            h = Harness(); h.sign(**changes); ctx = h.context("owned-probe-consumer")
            h.plugin.rewrite(h.conf, ctx); h.plugin.access(h.conf, ctx)
            self.assertFalse(ctx.market_sla_probe_verified)
        h = Harness(); h.sign(); h.lua.globals().ngx.var.args = "extra=query"
        ctx = h.context("owned-probe-consumer"); h.plugin.rewrite(h.conf, ctx); h.plugin.access(h.conf, ctx)
        self.assertFalse(ctx.market_sla_probe_verified)

    def test_queue_failure_replay_ack_only_after_http_commit_ack(self):
        h = Harness(); ctx = h.context(); h.plugin.rewrite(h.conf, ctx); h.plugin.log(h.conf, ctx); h.flush()
        exact = h.shared["queue"][0]
        h.lua.globals().HTTP_STATUS = 503; h.tick()
        self.assertEqual(h.shared["queue"][0], exact)
        self.assertEqual(json.loads(h.shared["queue"][1])["source_statuses"][0]["failure_count"], 1)
        self.assertTrue(h.shared["hashes"]["market:sla:terminal:delivery_failures"])
        h.lua.globals().HTTP_STATUS = 200; h.tick()
        self.assertEqual(h.shared["queue"], [])
        self.assertEqual(h.posts[0], h.posts[1])
        self.assertNotIn(INGEST_SECRET, " ".join(h.logs))

    def test_new_worker_replays_predecessor_queue(self):
        h = Harness(); ctx = h.context(); h.plugin.rewrite(h.conf, ctx); h.plugin.log(h.conf, ctx); h.flush()
        successor = Harness(h.shared); successor.tick()
        self.assertEqual(len(successor.posts), 1)
        self.assertEqual(h.shared["queue"], [])

    def test_close_window_expected_count_and_empty_array(self):
        h = Harness(); ctx = h.context(); h.plugin.rewrite(h.conf, ctx); h.plugin.log(h.conf, ctx); h.flush(); h.tick()
        h.lua.globals().NOW = STAMP + 300
        h.tick()
        seal = next(message for message in h.posts if "window_seals" in message)
        self.assertEqual(seal["events"], [])
        self.assertEqual(seal["window_seals"][0]["expected_count"], 1)
        self.assertEqual(seal["window_seals"][0]["last_sequence"], 1)
        self.assertEqual(seal["window_seals"][0]["closed_at_ms"], (STAMP + 300) * 1000)

    def test_unqueued_event_remains_in_sealed_expected_count(self):
        h = Harness(); ctx = h.context(); h.plugin.rewrite(h.conf, ctx); h.plugin.log(h.conf, ctx)
        # Simulate loss before batch enqueue, never silently lower expected N.
        h.lua.globals().NOW = STAMP + 300; h.tick()
        seal = next(message for message in h.posts if "window_seals" in message)
        self.assertEqual(seal["window_seals"][0]["expected_count"], 1)
        self.assertFalse(any(message["events"] for message in h.posts))

    def test_upstream_502_connect_error_not_provider_without_headers(self):
        for headers_received in (False, True):
            h = Harness(); ctx = h.context(); g = h.lua.globals()
            g.ngx.status = 502; g.ngx.var.upstream_status = "502"
            if headers_received:
                g.ngx.var.upstream_header_time = "0.010"; g.ngx.var.upstream_bytes_received = "123"
            h.plugin.rewrite(h.conf, ctx); h.plugin.log(h.conf, ctx); h.flush(); h.tick()
            event = h.posts[0]["events"][0]
            self.assertEqual(event["responsibility"], "provider" if headers_received else "unknown")

    def test_parent_platform_evidence_preserved_no_secret_fields(self):
        h = Harness(); ctx = h.context(); g = h.lua.globals()
        ctx.market_sla_evidence = h.lua.table_from({"reason": "forward_auth_failure",
            "responsibility": "platform", "evidence_id": "oauth:product", "authorized": True})
        g.ngx.status = 502; g.ngx.var.upstream_status = "502"
        g.ngx.var.upstream_header_time = "0.010"; g.ngx.var.upstream_bytes_received = "123"
        h.plugin.rewrite(h.conf, ctx); h.plugin.log(h.conf, ctx); h.flush(); h.tick()
        event = h.posts[0]["events"][0]
        self.assertEqual((event["reason"], event["responsibility"]), ("forward_auth_failure", "platform"))
        self.assertTrue(set(event).isdisjoint({"headers", "query", "body", "uri", "upstream_addr"}))


if __name__ == "__main__":
    unittest.main()
