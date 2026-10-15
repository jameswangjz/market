-- Global terminal-fact logger. Never reads request bodies, query or credentials.
local core = require("apisix.core")
local http = require("resty.http")
local redis = require("resty.redis")
local random = require("resty.random")
local hex = require("resty.string").to_hex
local hmac = require("resty.openssl.hmac")
local bit = require("bit")
local manager = require("apisix.utils.batch-processor-manager").new("market-sla-logger", "market-sla-logger")

local schema = manager:wrap_schema({type = "object", properties = {
    batch_max_size = {type = "integer", minimum = 1, maximum = 16, default = 16},
    max_retry_count = {type = "integer", minimum = 0, maximum = 3, default = 2},
    retry_delay = {type = "integer", minimum = 1, maximum = 5, default = 1},
    buffer_duration = {type = "integer", minimum = 1, maximum = 5, default = 1},
    inactive_timeout = {type = "integer", minimum = 1, maximum = 5, default = 1},
}})
local metadata_schema = manager:wrap_metadata_schema({type = "object", properties = {}})
metadata_schema.properties.max_pending_entries.maximum = 4096
metadata_schema.properties.max_pending_entries.default = 1024
local _M = {version = 0.1, priority = 12000, name = "market-sla-logger",
            schema = schema, metadata_schema = metadata_schema}
local reasons = {caller_parameters=true, buyer_credentials=true, undeclared_path=true,
    contract_quota=true, client_cancel=true, upstream_failure=true, timeout=true,
    connection_failure=true, forward_auth_failure=true, missing_published_route=true,
    incorrect_rate_limit=true, valid_input_failure=true}
local parties = {platform=true, provider=true, caller=true, unknown=true}
local traffic_kinds = {probe=true, isolated_test=true, internal=true}
local producer_session
local producer_started_at_ms
local window_counts = {}
local last_sealed_slot
local draining = false
local timer_started = false
local failure_reports = {}
local QUEUE = "market:sla:terminal:pending"
local FAILURES = "market:sla:terminal:delivery_failures"
local REGISTRY = "market:sla:terminal:producers"
local append_script = [[
if redis.call('LLEN', KEYS[1]) >= 1024 then return 0 end
redis.call('RPUSH', KEYS[1], ARGV[1])
return 1
]]

local function safe_id(value)
    return type(value) == "string" and #value <= 180 and value:match("^[%w_.:-]*$") and value or ""
end

local function producer_id()
    if not producer_session then
        local bytes = random.bytes(8, true)
        producer_session = safe_id(os.getenv("HOSTNAME") or "apisix") .. ":" ..
            tostring(ngx.worker.pid()) .. ":" .. (bytes and hex(bytes) or "unidentified")
        producer_started_at_ms = math.floor(ngx.now() * 1000)
        last_sealed_slot = math.floor(producer_started_at_ms / 300000) - 1
    end
    return producer_session
end

local function redis_connection()
    local red = redis:new()
    red:set_timeout(1000)
    if not red:connect("market-redis", 6379) or not red:select(0) then
        red:close()
        return nil
    end
    return red
end

local function enqueue(packet)
    local encoded = core.json.encode(packet)
    if not encoded or #encoded > 65536 then return false, "SLA telemetry payload limit" end
    local red = redis_connection()
    if not red then
        core.log.error("market-sla-logger queue_append_failed redis_unavailable")
        return false, "SLA telemetry queue unavailable"
    end
    local registered = red:hsetnx(REGISTRY, producer_id(), tostring(producer_started_at_ms))
    local ok = registered and red:eval(append_script, 1, QUEUE, encoded)
    if not ok or tonumber(ok) ~= 1 then
        local slot = math.floor(ngx.now() / 300)
        local count = red:hincrby(FAILURES, producer_id() .. ":" .. slot, 1)
        if type(count) == "number" then failure_reports[slot] = count end
        core.log.error("market-sla-logger queue_append_failed full_or_error")
    end
    red:set_keepalive(60000, 10)
    return tonumber(ok) == 1, "SLA telemetry queue append failed"
end

local probe_headers = {"X-Market-SLA-Route", "X-Market-SLA-Product", "X-Market-SLA-Version",
                       "X-Market-SLA-Timestamp", "X-Market-SLA-Signature"}
local function verify_probe(ctx)
    local route = ctx.matched_route and ctx.matched_route.value
    local labels = route and route.labels or {}
    local secret = os.getenv("SLA_PROBE_SECRET")
    if not secret or #secret < 32 or #secret > 512 or labels.market_managed ~= "true" then return false end
    local values = {}
    for _, name in ipairs(probe_headers) do
        local value = core.request.header(ctx, name)
        if type(value) ~= "string" then return false end
        values[#values + 1] = value
    end
    local method, path = ngx.req.get_method(), ngx.var.uri
    if type(path) ~= "string" or path == "" or (method ~= "GET" and method ~= "HEAD") or method ~= labels.market_sla_probe_method
        or path ~= labels.market_sla_probe_path or (ngx.var.args and ngx.var.args ~= "")
        or values[1] ~= tostring(route.id) or values[2] ~= labels.market_product_id
        or values[3] ~= labels.market_version_id then return false end
    local stamp = values[4]
    if not stamp:match("^%d%d%d%d%d%d%d%d%d%d%d?$") or math.abs(ngx.time() - tonumber(stamp)) > 30
        or not values[5]:match("^[a-f0-9]+$") or #values[5] ~= 64 then return false end
    local signer = hmac.new(secret, "sha256")
    if not signer then return false end
    local digest = signer:final(table.concat({"sla-probe-v1", values[1], values[2], values[3], method, path, stamp}, "\n"))
    if not digest then return false end
    local expected, diff = hex(digest), 0
    for i = 1, 64 do diff = bit.bor(diff, bit.bxor(expected:byte(i), values[5]:byte(i))) end
    return diff == 0
end

function _M.check_schema(conf, schema_type)
    return core.schema.check(schema_type == core.schema.TYPE_METADATA and metadata_schema or schema, conf)
end

function _M.rewrite(conf, ctx)
    if ngx.is_subrequest or ctx.market_sla_request_id then return end
    local ok, bytes = pcall(random.bytes, 16, true)
    if not ok or not bytes or #bytes ~= 16 then
        ngx.req.clear_header("X-Request-Id")
        core.log.error("market-sla-logger server_id_generation_failed")
        return
    end
    ctx.market_sla_request_id = "gw_" .. hex(bytes)
    ngx.req.set_header("X-Request-Id", ctx.market_sla_request_id)
    ctx.market_sla_probe_verified = false
    ctx.market_sla_probe_signature_valid = verify_probe(ctx)
end

function _M.access(conf, ctx)
    -- key-auth runs in rewrite at priority 2500. Only after it attached the
    -- independent consumer may global access mark this request quota-exempt.
    ctx.market_sla_probe_verified = false
    local route = ctx.matched_route and ctx.matched_route.value
    local labels = route and route.labels or {}
    if ctx.market_sla_request_id and labels.market_sla_probe_consumer and labels.market_sla_probe_consumer ~= ""
        and ctx.consumer_name == labels.market_sla_probe_consumer and verify_probe(ctx) then
        ctx.market_sla_probe_verified = true
        ctx.market_sla_traffic = {kind="probe", evidence_id="probe-signature:" .. ctx.market_sla_request_id}
    end
    for _, name in ipairs(probe_headers) do ngx.req.clear_header(name) end
end

function _M.header_filter(conf, ctx)
    if ctx.market_sla_request_id then ngx.header["X-Request-Id"] = ctx.market_sla_request_id end
end

local function post(encoded)
    local secret = os.getenv("SLA_GATEWAY_INGEST_SECRET")
    if not secret or #secret < 32 or #secret > 512 then
        core.log.error("market-sla-logger delivery_failed secret_unavailable")
        return false, "SLA telemetry secret unavailable"
    end
    local client = http.new()
    client:set_timeout(1000)
    local ok = client:connect("market-api", 8000)
    if not ok then
        client:close()
        core.log.error("market-sla-logger delivery_failed connect")
        return false, "SLA telemetry connect failed"
    end
    -- request() returns headers without buffering a response body. The internal
    -- destination is fixed; never allow a route to redirect the telemetry Secret.
    local response = client:request({method = "POST", path = "/internal/sla/gateway-events",
        body = encoded, headers = {["Host"] = "market-api", ["Content-Type"] = "application/json",
                                  ["X-SLA-Gateway-Key"] = secret}})
    local accepted = response and response.status == 200
    client:close()
    if not accepted then
        core.log.error("market-sla-logger delivery_failed receiver")
        return false, "SLA telemetry receiver rejected batch"
    end
    return true
end

local function drain(premature)
    if premature or draining then return end
    draining = true
    local red
    local ok = pcall(function()
        producer_id()
        red = redis_connection()
        if not red then error("queue_unavailable") end
        if red:hsetnx(REGISTRY, producer_id(), tostring(producer_started_at_ms)) == nil then error("registry_unavailable") end
        local current = math.floor(ngx.now() / 300)
        -- Close at most four windows per sweep; failed appends are retried, not
        -- forgotten. A startup window only covers evaluation after worker birth.
        for _ = 1, 4 do
            local slot = last_sealed_slot + 1
            if slot >= current then break end
            local count = window_counts[slot] or 0
            local queued = enqueue({events=setmetatable({}, core.json.array_mt), window_seals={{
                producer_id=producer_id(), window_slot=slot, expected_count=count, last_sequence=count,
                producer_started_at_ms=producer_started_at_ms, closed_at_ms=(slot + 1) * 300000}}})
            if not queued then break end
            last_sealed_slot = slot
            window_counts[slot] = nil
        end
        local messages = red:lrange(QUEUE, 0, 3)
        if type(messages) ~= "table" then error("queue_read_failed") end
        for _, encoded in ipairs(messages) do
            if not post(encoded) then
                local slot = math.floor(ngx.now() / 300)
                local count = red:hincrby(FAILURES, producer_id() .. ":" .. slot, 1)
                if type(count) == "number" then failure_reports[slot] = count end
                break -- leave exact bytes for replay by this or another worker
            end
            if red:lrem(QUEUE, 1, encoded) == nil then error("queue_ack_failed") end
        end
        local reported = 0
        for slot, count in pairs(failure_reports) do
            if reported == 4 then break end
            local queued = enqueue({events=setmetatable({}, core.json.array_mt), source_statuses={{
                producer_id=producer_id(), window_slot=slot, failure_count=count}}})
            if queued then failure_reports[slot] = nil; reported = reported + 1 end
        end
    end)
    if red then red:set_keepalive(60000, 10) end
    if not ok then core.log.error("market-sla-logger queue_worker_failed") end
    draining = false
end

-- APISIX invokes plugin.init() while loading plugins in worker initialization.
function _M.init()
    if timer_started then return end
    producer_id()
    local ok = ngx.timer.every(2, drain)
    if ok then
        timer_started = true
    else
        core.log.error("market-sla-logger queue_timer_failed")
    end
end

function _M.log(conf, ctx)
    if ngx.is_subrequest or ctx.market_sla_emitted then return end
    ctx.market_sla_emitted = true
    local matched = ctx.matched_route and ctx.matched_route.value
    local labels = matched and matched.labels or {}
    -- Only server-configured market routes, not arbitrary console/admin traffic.
    if labels.market_managed ~= "true" then return end
    local request_id = ctx.market_sla_request_id
    if not request_id then
        core.log.error("market-sla-logger terminal_fact_missing server_id")
        return
    end
    local evidence = type(ctx.market_sla_evidence) == "table" and ctx.market_sla_evidence or {}
    local evidence_id = safe_id(evidence.evidence_id)
    local reason, party = "unknown", "unknown"
    if evidence_id ~= "" and reasons[evidence.reason] and parties[evidence.responsibility] then
        reason, party = evidence.reason, evidence.responsibility
    end
    local authorized = type(ctx.consumer_name) == "string" and ctx.consumer_name ~= "" or nil
    if type(evidence.authorized) == "boolean" then authorized = evidence.authorized end
    local consumer = type(ctx.consumer_name) == "string" and ctx.consumer_name or ""
    local credential_id = consumer:match("^market%-consumer%-([a-f0-9]+)$") or ""
    local traffic = type(ctx.market_sla_traffic) == "table" and ctx.market_sla_traffic or {}
    local traffic_kind, traffic_evidence = "business", ""
    if traffic_kinds[traffic.kind] and safe_id(traffic.evidence_id) ~= "" then
        traffic_kind, traffic_evidence = traffic.kind, safe_id(traffic.evidence_id)
    end
    local statuses = {}
    for status in tostring(ngx.var.upstream_status or ""):gmatch("%d%d%d") do
        if #statuses == 16 then break end
        local code = tonumber(status)
        if code >= 100 and code <= 599 then statuses[#statuses + 1] = code end
    end
    -- An upstream_status 502 may be synthesized by a failed connect. Require
    -- response-header timing and received bytes before assigning HTTP5xx to a provider.
    local last_status = statuses[#statuses]
    local plugins = matched.plugins or {}
    local native_auth = plugins["key-auth"]
    if reason == "unknown" and ngx.status == 401 and consumer == ""
        and type(native_auth) == "table" and not native_auth._meta
        and #statuses == 0 and (ngx.var.upstream_addr == nil or ngx.var.upstream_addr == ""
            or ngx.var.upstream_addr == "-") then
        reason, party, evidence_id = "buyer_credentials", "caller", "key-auth:" .. request_id
        authorized = false
    end
    local header_time = tostring(ngx.var.upstream_header_time or ""):match("([%d.]+)%s*$")
    local received = tonumber(tostring(ngx.var.upstream_bytes_received or ""):match("(%d+)%s*$"))
    if reason == "unknown" and last_status and last_status >= 500 and last_status == ngx.status
        and tonumber(header_time) and received and received > 0 then
        reason, party, evidence_id = "upstream_failure", "provider", "upstream:" .. request_id
    end
    local seconds = tonumber(ngx.var.request_time)
    local duration = seconds and seconds >= 0 and seconds < math.huge and math.floor(seconds * 1000 + 0.5) or nil
    producer_id()
    local completed_at_ms = math.floor(ngx.now() * 1000)
    local window_slot = math.floor(completed_at_ms / 300000)
    window_counts[window_slot] = (window_counts[window_slot] or 0) + 1
    local entry = {event_id=request_id, request_id=request_id,
        product_id=safe_id(labels.market_product_id), version_id=safe_id(labels.market_version_id),
        version_code=safe_id(labels.market_version), route_id=safe_id(tostring(matched.id or "")),
        entry_id=safe_id(labels.market_entry_id or tostring(matched.id or "")),
        credential_id=safe_id(credential_id), completed_at_ms=completed_at_ms,
        status_code=ngx.status, duration_ms=duration, authorized=authorized,
        published_route=matched.status ~= 0, reason=reason, responsibility=party,
        evidence_id=evidence_id, traffic=traffic_kind, traffic_evidence_id=traffic_evidence,
        redirect_allowed=labels.market_sla_allow_redirect == "true", policy_version="sla-online-v1.1",
        upstream_statuses=#statuses > 0 and statuses or nil,
        producer_id=producer_id(), sequence=window_counts[window_slot]}
    -- Batch buffering only bridges log-phase to the Redis queue. A dropped
    -- pre-queue event leaves a permanent sequence gap, never a false complete.
    if manager:add_entry(conf, entry) then return end
    manager:add_entry_to_new_processor(conf, entry, ctx, function(entries)
        return enqueue({events=entries})
    end)
end

return _M
