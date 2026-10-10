local core = require("apisix.core")
local redis = require("resty.redis")

local schema = {
    type = "object",
    properties = {
        route_key = {type = "string"},
        daily_quota = {type = "integer", minimum = 0},
        monthly_quota = {type = "integer", minimum = 0},
        total_quota = {type = "integer", minimum = 0},
        lookup_consumer = {type = "boolean", default = false},
    },
}

local quota_script = [[
local minute = redis.call('INCR', KEYS[1])
local day = redis.call('INCR', KEYS[2])
local month = redis.call('INCR', KEYS[3])
local total = redis.call('INCR', KEYS[4])
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[3], ARGV[3])
redis.call('EXPIRE', KEYS[4], ARGV[4])
local day_limit = tonumber(ARGV[5])
local month_limit = tonumber(ARGV[6])
local total_limit = tonumber(ARGV[7])
if day_limit > 0 and day > day_limit or month_limit > 0 and month > month_limit or total_limit > 0 and total > total_limit then
  redis.call('DECR', KEYS[1])
  redis.call('DECR', KEYS[2])
  redis.call('DECR', KEYS[3])
  redis.call('DECR', KEYS[4])
  return {0, day, month, total}
end
return {1, day, month}
]]

local function reject(code, message)
    return core.response.exit(code, {code = "GATEWAY_POLICY_REJECTED", message = message})
end

local subscription_script = [[
-- Redis script errors do not roll back earlier writes; validate before INCR.
for i = 1, 3 do
    local value = redis.call('GET', KEYS[i])
    if value and (not value:match('^%d+$') or (#value > 1 and value:sub(1, 1) == '0')
        or tonumber(value) > 9007199254740990) then
        return redis.error_reply('订阅配额计数器无效')
    end
end
local minute = redis.call('INCR', KEYS[1])
local day = redis.call('INCR', KEYS[2])
local month = redis.call('INCR', KEYS[3])
redis.call('EXPIREAT', KEYS[1], ARGV[1])
redis.call('EXPIREAT', KEYS[2], ARGV[2])
redis.call('PERSIST', KEYS[3])
local rate_limit = tonumber(ARGV[3])
local day_limit = tonumber(ARGV[4])
local month_limit = tonumber(ARGV[5])
local reason = 0
if minute > rate_limit then reason = 1
elseif day_limit > 0 and day > day_limit then reason = 2
elseif month_limit > 0 and month > month_limit then reason = 3 end
if reason ~= 0 then
    redis.call('DECR', KEYS[1])
    redis.call('DECR', KEYS[2])
    redis.call('DECR', KEYS[3])
end
return {reason, minute, day, month}
]]

local function integer(value, minimum)
    return type(value) == "number" and value == value and value < math.huge
        and value >= minimum and value == math.floor(value)
end

local function token(value)
    return (value:gsub("%%", "%%25"):gsub(":", "%%3A"))
end

local function subscription_access(red, conf, ctx, values)
    local function finish(code, message)
        red:set_keepalive(60000, 100)
        return reject(code, message)
    end
    if values.mode ~= "subscription" or values.status ~= "active"
        or type(values.quota_scope) ~= "string" or values.quota_scope == ""
        or type(values.subscription_periods) ~= "string"
        or not values.subscription_periods:match("^%s*%[") then
        return finish(403, "订阅配额元数据无效")
    end
    local periods = core.json.decode(values.subscription_periods)
    if type(periods) ~= "table" or #periods == 0 then
        return finish(403, "订阅配额周期列表无效")
    end
    local now = ngx.time()
    local active
    for i, period in ipairs(periods) do
        if type(period) ~= "table" or not integer(period.start_at, 0)
            or not integer(period.end_at, 0) or period.end_at <= period.start_at
            or type(period.period_key) ~= "string" or period.period_key == ""
            or type(period.route_key) ~= "string" or period.route_key == ""
            or not integer(period.rate_limit_per_minute, 1)
            or not integer(period.daily_quota, 0) or not integer(period.monthly_quota, 0) then
            return finish(403, "订阅配额周期无效")
        end
        for j = 1, i - 1 do
            local previous = periods[j]
            if period.start_at < previous.end_at and previous.start_at < period.end_at then
                return finish(403, "订阅配额周期重叠，无法确定有效周期")
            end
        end
        if period.start_at <= now and now < period.end_at then active = period end
    end
    local matched = ctx.matched_route and ctx.matched_route.value
    local route_policy = matched and matched.plugins and matched.plugins["market-gateway-quota"]
    if not active or active.route_key ~= conf.route_key
        or (route_policy and active.route_key ~= route_policy.route_key) then
        return finish(403, "订阅尚未生效、已到期或与 API 路由不匹配")
    end
    -- Stable combination and original-anchor keys survive renewal and key rotation.
    -- Month keys deliberately have no TTL. Migration may seed, but never reset, them.
    local scope = token(values.quota_scope)
    local prefix = "market:apisix:quota:subscription:"
    local beijing_day = math.floor((now + 28800) / 86400)
    local result = red:eval(subscription_script, 3,
        prefix .. "minute:" .. scope .. ":" .. math.floor(now / 60),
        prefix .. "day:" .. scope .. ":" .. os.date("!%Y-%m-%d", now + 28800),
        prefix .. "month:" .. scope .. ":" .. token(active.period_key),
        (math.floor(now / 60) + 1) * 60 + 10,
        (beijing_day + 1) * 86400 - 28800 + 86400,
        active.rate_limit_per_minute, active.daily_quota, active.monthly_quota)
    red:set_keepalive(60000, 100)
    if not result then return reject(503, "API 网关配额服务不可用") end
    local reason = tonumber(result[1])
    if reason == 1 then return reject(429, "超过 API 每分钟调用配额") end
    if reason == 2 then return reject(429, "超过 API 每日调用配额") end
    if reason == 3 then return reject(429, "超过 API 订阅月调用配额") end
    if reason ~= 0 then return reject(503, "API 网关配额服务不可用") end
end

local _M = {version = 0.1, priority = 1800, name = "market-gateway-quota", schema = schema}

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

function _M.access(conf, ctx)
    local consumer = ctx.consumer_name or ngx.var.consumer_name or ""
    if consumer == "" then
        return reject(401, "缺少有效的 API Consumer")
    end
    local red = redis:new()
    red:set_timeout(1000)
    local ok = red:connect("market-redis", 6379)
    if not ok then
        return reject(503, "API 网关配额服务不可用")
    end
    local selected = red:select(0)
    if not selected then
        red:close()
        return reject(503, "API 网关配额服务不可用")
    end
    local route = conf.route_key or "unknown"
    local daily_limit = conf.daily_quota or 0
    local monthly_limit = conf.monthly_quota or 0
    local total_limit = conf.total_quota or 0
    if consumer ~= "" then
        local metadata = red:hgetall("market:apisix:credential:" .. consumer)
        if not metadata then
            red:set_keepalive(60000, 100)
            return reject(503, "API 网关配额服务不可用")
        end
        local matched = ctx.matched_route and ctx.matched_route.value
        local route_policy = matched and matched.plugins and matched.plugins["market-gateway-quota"]
        if #metadata == 0 and (conf.lookup_consumer or (route_policy and route_policy.lookup_consumer)) then
            red:set_keepalive(60000, 100)
            return reject(403, "缺少 API 访问凭据配额元数据")
        end
        if metadata and #metadata > 0 then
            local values = {}
            for i = 1, #metadata, 2 do values[metadata[i]] = metadata[i + 1] end
            if values.mode or values.quota_scope or values.subscription_periods then
                return subscription_access(red, conf, ctx, values)
            end
            if conf.lookup_consumer then
                if values.status and values.status ~= "active" then
                    red:set_keepalive(60000, 100)
                    return reject(403, "订单 API 访问凭据已回收")
                end
                daily_limit = tonumber(values.daily_quota) or daily_limit
                monthly_limit = tonumber(values.monthly_quota) or monthly_limit
                total_limit = tonumber(values.total_quota) or total_limit
            end
        end
    end
    local date = os.date("!%Y-%m-%d")
    local month = os.date("!%Y-%m")
    local result = red:eval(quota_script, 4,
        "market:apisix:quota:minute:" .. route .. ":" .. consumer .. ":" .. math.floor(ngx.time() / 60),
        "market:apisix:quota:day:" .. route .. ":" .. consumer .. ":" .. date,
        "market:apisix:quota:month:" .. route .. ":" .. consumer .. ":" .. month,
        "market:apisix:quota:total:" .. route .. ":" .. consumer,
        70, 86400, 2678400, 31536000, daily_limit, monthly_limit, total_limit)
    red:set_keepalive(60000, 100)
    if not result then
        return reject(503, "API 网关配额服务不可用")
    end
    if tonumber(result[1]) ~= 1 then
        if total_limit > 0 and tonumber(result[4]) >= total_limit then
            return reject(403, "订单 API 调用额度已耗尽，访问凭据已回收")
        end
        if daily_limit > 0 and tonumber(result[2]) > daily_limit then
            return reject(429, "超过 API 每日调用配额")
        end
        return reject(429, "超过 API 每月调用配额")
    end
end

return _M
