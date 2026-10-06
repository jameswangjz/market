local core = require("apisix.core")
local http = require("resty.http")
local redis = require("resty.redis")

local schema = {type = "object", properties = {
    product_id = {type = "string"}, token_url = {type = "string"},
    client_id = {type = "string"}, client_secret = {type = "string"}, scope = {type = "string"},
}}

local function reject(code, message)
    return core.response.exit(code, {code = "UPSTREAM_OAUTH_ERROR", message = message})
end

local _M = {version = 0.1, priority = 1700, name = "market-gateway-oauth", schema = schema}

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

function _M.access(conf, ctx)
    if not conf.token_url or not conf.client_id then
        return reject(502, "API 提供方 OAuth2 配置不完整")
    end
    local red = redis:new()
    red:set_timeout(1000)
    local ok = red:connect("market-redis", 6379)
    if not ok then return reject(503, "API 网关 OAuth2 缓存不可用") end
    red:select(0)
    local cache_key = "market:apisix:oauth:" .. (conf.product_id or "unknown")
    local cached = red:get(cache_key)
    if cached and cached ~= ngx.null then
        ngx.req.set_header("Authorization", "Bearer " .. cached)
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
        return reject(502, "获取上游 OAuth2 Token 失败")
    end
    local payload = core.json.decode(response.body)
    local token = payload and payload.access_token
    if not token then red:set_keepalive(60000, 100); return reject(502, "上游 OAuth2 响应缺少 access_token") end
    local ttl = tonumber(payload.expires_in or 3600) - 60
    if ttl < 60 then ttl = 60 end
    red:setex(cache_key, ttl, token)
    red:set_keepalive(60000, 100)
    ngx.req.set_header("Authorization", "Bearer " .. token)
end

return _M
