local core = require("apisix.core")
local redis = require("resty.redis")

local schema = {
    type = "object",
    properties = {
        route_key = {type = "string"},
        daily_quota = {type = "integer", minimum = 0},
        monthly_quota = {type = "integer", minimum = 0},
    },
}

local quota_script = [[
local minute = redis.call('INCR', KEYS[1])
local day = redis.call('INCR', KEYS[2])
local month = redis.call('INCR', KEYS[3])
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[3], ARGV[3])
local day_limit = tonumber(ARGV[4])
local month_limit = tonumber(ARGV[5])
if day_limit > 0 and day > day_limit or month_limit > 0 and month > month_limit then
  redis.call('DECR', KEYS[1])
  redis.call('DECR', KEYS[2])
  redis.call('DECR', KEYS[3])
  return {0, day, month}
end
return {1, day, month}
]]

local function reject(code, message)
    return core.response.exit(code, {code = "GATEWAY_POLICY_REJECTED", message = message})
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
    red:select(0)
    local route = conf.route_key or "unknown"
    local date = os.date("!%Y-%m-%d")
    local month = os.date("!%Y-%m")
    local result = red:eval(quota_script, 3,
        "market:apisix:quota:minute:" .. route .. ":" .. consumer .. ":" .. math.floor(ngx.time() / 60),
        "market:apisix:quota:day:" .. route .. ":" .. consumer .. ":" .. date,
        "market:apisix:quota:month:" .. route .. ":" .. consumer .. ":" .. month,
        70, 86400, 2678400, conf.daily_quota or 0, conf.monthly_quota or 0)
    red:set_keepalive(60000, 100)
    if not result then
        return reject(503, "API 网关配额服务不可用")
    end
    if tonumber(result[1]) ~= 1 then
        if conf.daily_quota and conf.daily_quota > 0 and tonumber(result[2]) > conf.daily_quota then
            return reject(429, "超过 API 每日调用配额")
        end
        return reject(429, "超过 API 每月调用配额")
    end
end

return _M
