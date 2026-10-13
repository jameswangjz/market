local redis_state = {}
local timers = {}
local logs = {}
local http_calls = 0
local timer_fails = false
local redis_fails = false
local response_payload = '{"access_token":"fresh-token","expires_in":3600}'
local headers = {}

package.preload["apisix.core"] = function()
    return {
        response = {exit = function(code, body) return {code = code, body = body} end},
        schema = {check = function() return true end},
        json = {decode = function() return {access_token = "fresh-token", expires_in = 3600} end},
    }
end
package.preload["resty.http"] = function()
    return {new = function()
        return {
            set_timeout = function() end,
            request_uri = function()
                http_calls = http_calls + 1
                return {status = 200, body = response_payload}
            end,
        }
    end}
end
package.preload["resty.redis"] = function()
    return {new = function()
        return {
            set_timeout = function() end,
            connect = function() if redis_fails then return nil, "offline" end return true end,
            select = function() return true end,
            get = function(_, key) return redis_state[key] or ngx.null end,
            setex = function(_, key, _, value) redis_state[key] = value return "OK" end,
            eval = function(_, _, _, key, token)
                if redis_fails then return nil, "offline" end
                if redis_state[key] == token then redis_state[key] = nil return 1 end
                return 0
            end,
            set_keepalive = function() return true end,
            close = function() end,
        }
    end}
end

ngx = {
    null = {}, ERR = "ERR", status = 200, var = {},
    req = {set_header = function(name, value) headers[name] = value end},
    escape_uri = function(value) return value end,
    log = function(_, message) logs[#logs + 1] = message end,
    timer = {at = function(delay, callback, key, token)
        if timer_fails then return nil, "timer unavailable" end
        timers[#timers + 1] = {callback = callback, key = key, token = token}
        return true
    end},
}

local plugin = dofile("k8s/market-gateway-oauth.lua")
local conf = {product_id = "test", token_url = "https://issuer/token", client_id = "client", client_secret = "secret"}
local key = "market:apisix:oauth:test"
local function eq(actual, expected, label)
    assert(actual == expected, label .. ": expected " .. tostring(expected) .. ", got " .. tostring(actual))
end
local function reset()
    redis_state, timers, logs, headers = {}, {}, {}, {}
    http_calls, timer_fails, redis_fails = 0, false, false
    ngx.status, ngx.var.upstream_status = 200, nil
end
local function prepare(token)
    redis_state[key] = token
    local ctx = {}
    plugin.access(conf, ctx)
    return ctx
end
local function report(status, upstream, ctx)
    ngx.status, ngx.var.upstream_status = status, upstream
    plugin.log(conf, ctx or {})
end
local function run_timer(index, premature)
    local timer = table.remove(timers, index or 1)
    assert(timer, "expected queued timer")
    timer.callback(premature or false, timer.key, timer.token)
end

reset()
local old_ctx = prepare("old-token")
report(401, "401", old_ctx)
eq(#timers, 1, "401 queues eviction")
redis_state[key] = "new-token"
run_timer()
eq(redis_state[key], "new-token", "concurrent replacement preserved")

reset()
prepare("old-token")
report(401, "401", {})
eq(#timers, 0, "missing context does not evict")
local ctx = prepare("old-token")
report(401, nil, ctx)
eq(#timers, 0, "local 401 does not evict")
report(401, "200", ctx)
report(401, "401, 200", ctx)
eq(#timers, 0, "non-401 upstream final status does not evict")
report(500, "500", ctx)
report(403, "403", ctx)
eq(#timers, 0, "non-401 responses do not evict")

reset()
local failed_ctx = prepare("old-token")
report(401, "502, 401", failed_ctx)
run_timer()
eq(redis_state[key], nil, "upstream 401 evicts failed cached token")

reset()
ctx = prepare("old-token")
report(401, "401", ctx)
run_timer(1, true)
eq(redis_state[key], "old-token", "premature timer is a no-op")

reset()
ctx = prepare("old-token")
timer_fails = true
report(401, "401", ctx)
eq(#timers, 0, "failed timer does not replay or delete synchronously")
eq(redis_state[key], "old-token", "failed timer remains fail-closed")
for _, message in ipairs(logs) do
    assert(not message:find("old-token", 1, true) and not message:find("secret", 1, true))
end

reset()
ctx = prepare("old-token")
report(401, "401", ctx)
redis_fails = true
run_timer()
eq(redis_state[key], "old-token", "Redis failure does not alter unrelated state")
for _, message in ipairs(logs) do
    assert(not message:find("old-token", 1, true) and not message:find("secret", 1, true))
end

reset()
ctx = prepare("old-token")
report(401, "401", ctx)
run_timer()
local before = http_calls
local next_ctx = {}
plugin.access(conf, next_ctx)
eq(http_calls, before + 1, "next request fetches one fresh token")
eq(redis_state[key], "fresh-token", "fresh token cached")
eq(headers.Authorization, "Bearer fresh-token", "fresh token injected")

reset()
local expired_ctx = prepare("expired-token")
report(401, "401", expired_ctx)
run_timer()
local disabled_conf = {product_id = "test", token_url = conf.token_url}
local disabled_result = plugin.access(disabled_conf, {})
eq(disabled_result.code, 502, "disabled client remains fail-closed on the next request")

reset()
ctx = prepare("cached-token")
local fetches = http_calls
report(200, "200", ctx)
eq(http_calls, fetches, "cache hit avoids authentication request")
eq(#timers, 0, "successful cache hit does not schedule eviction")
for _, message in ipairs(logs) do
    assert(not message:find("token", 1, true) and not message:find("secret", 1, true), "logs must not expose credentials")
end

print("market-gateway-oauth harness: all assertions passed")
