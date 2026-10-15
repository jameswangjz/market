local core = require("apisix.core")
local http = require("resty.http")
local redis = require("resty.redis")

local schema = {type = "object", properties = {
    product_id = {type = "string"}, token_url = {type = "string"},
    client_id = {type = "string"}, client_secret = {type = "string"}, scope = {type = "string"},
}}

local function reject(code, message, ctx)
    if ctx then
        ctx.market_sla_evidence = {reason="forward_auth_failure", responsibility="platform",
            evidence_id="oauth:token_acquisition",
            authorized=type(ctx.consumer_name) == "string" and ctx.consumer_name ~= ""}
    end
    return core.response.exit(code, {code = "UPSTREAM_OAUTH_ERROR", message = message})
end

local _M = {version = 0.1, priority = 1700, name = "market-gateway-oauth", schema = schema}

local eviction_script = [[
local token = redis.call("GET", KEYS[1])
if token == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
]]

local function evict_failed_token(premature, cache_key, token)
    if premature then return end

    local red = redis:new()
    red:set_timeout(1000)
    local ok, err = red:connect("market-redis", 6379)
    if not ok then
        ngx.log(ngx.ERR, "OAuth cache eviction Redis connection failed")
        return
    end
    ok, err = red:select(0)
    if not ok then
        ngx.log(ngx.ERR, "OAuth cache eviction Redis database selection failed")
        red:close()
        return
    end
    local result
    result, err = red:eval(eviction_script, 1, cache_key, token)
    if result == nil or result == false then
        ngx.log(ngx.ERR, "OAuth cache eviction Redis operation failed")
        red:close()
        return
    end
    local keepalive, keepalive_err = red:set_keepalive(60000, 100)
    if not keepalive then
        ngx.log(ngx.ERR, "OAuth cache eviction Redis keepalive failed")
    end
end

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

function _M.access(conf, ctx)
    if not conf.token_url or not conf.client_id then
        return reject(502, "API 提供方 OAuth2 配置不完整", ctx)
    end
    local red = redis:new()
    red:set_timeout(1000)
    local ok = red:connect("market-redis", 6379)
    if not ok then return reject(503, "API 网关 OAuth2 缓存不可用", ctx) end
    ok = red:select(0)
    if not ok then red:close(); return reject(503, "API 网关 OAuth2 缓存不可用", ctx) end
    local cache_key = "market:apisix:oauth:" .. (conf.product_id or "unknown")
    local cached = red:get(cache_key)
    if cached == nil then red:close(); return reject(503, "API 网关 OAuth2 缓存不可用", ctx) end
    if cached and cached ~= ngx.null then
        ngx.req.set_header("Authorization", "Bearer " .. cached)
        ctx.market_oauth_cache_key = cache_key
        ctx.market_oauth_token = cached
        red:set_keepalive(60000, 100)
        return
    end
    local form = "grant_type=client_credentials&client_id=" .. ngx.escape_uri(conf.client_id) .. "&client_secret=" .. ngx.escape_uri(conf.client_secret or "")
    if conf.scope and conf.scope ~= "" then form = form .. "&scope=" .. ngx.escape_uri(conf.scope) end
    local client = http.new()
    client:set_timeout(3000)
    local response = client:request_uri(conf.token_url, {method = "POST", body = form, headers = { ["Content-Type"] = "application/x-www-form-urlencoded" }})
    if not response or response.status < 200 or response.status >= 300 then
        red:set_keepalive(60000, 100)
        return reject(502, "获取上游 OAuth2 Token 失败", ctx)
    end
    local payload = core.json.decode(response.body)
    local token = payload and payload.access_token
    if not token then red:set_keepalive(60000, 100); return reject(502, "上游 OAuth2 响应缺少 access_token", ctx) end
    local ttl = tonumber(payload.expires_in or 3600) - 60
    if ttl < 60 then ttl = 60 end
    local stored = red:setex(cache_key, ttl, token)
    if not stored then red:close(); return reject(503, "API 网关 OAuth2 缓存不可用", ctx) end
    red:set_keepalive(60000, 100)
    ngx.req.set_header("Authorization", "Bearer " .. token)
    ctx.market_oauth_cache_key = cache_key
    ctx.market_oauth_token = token
end

function _M.log(conf, ctx)
    if ngx.status ~= 401 or not ctx.market_oauth_cache_key or not ctx.market_oauth_token then
        return
    end
    local upstream_status = ngx.var.upstream_status
    if type(upstream_status) ~= "string" or upstream_status:match("(%d%d%d)%s*$") ~= "401" then
        return
    end
    local ok, err = ngx.timer.at(0, evict_failed_token, ctx.market_oauth_cache_key, ctx.market_oauth_token)
    if not ok then
        ngx.log(ngx.ERR, "OAuth cache eviction timer scheduling failed")
    end
end

return _M
