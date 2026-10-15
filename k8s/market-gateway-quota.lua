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
-- A request may have read legacy metadata immediately before cutover. Check
-- again in the same atomic operation as counting, then reload the new policy.
if redis.call('HGET', KEYS[5], 'mode') == 'subscription' then
    return {2}
end
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

local function reject(code, message, ctx, reason, party)
    if ctx and reason then
        local matched = ctx.matched_route and ctx.matched_route.value
        ctx.market_sla_evidence = {reason=reason, responsibility=party,
            evidence_id="quota:" .. tostring(matched and matched.id or "unknown"),
            authorized=type(ctx.consumer_name) == "string" and ctx.consumer_name ~= ""}
    end
    return core.response.exit(code, {code = "GATEWAY_POLICY_REJECTED", message = message})
end

local subscription_script = [[
-- Redis script errors do not roll back earlier writes; validate before INCR.
local tracking = #KEYS == 6
local epoch
if tracking then
    epoch = redis.call('HGET', KEYS[6], 'run_id')
    if not epoch or #epoch ~= 40 or not epoch:match('^[a-f0-9]+$') then
        return redis.error_reply('扣额账本来源尚未就绪')
    end
    local stream_type = redis.call('TYPE', KEYS[4]).ok
    local coverage_type = redis.call('TYPE', KEYS[5]).ok
    if (stream_type ~= 'none' and stream_type ~= 'stream')
        or (coverage_type ~= 'none' and coverage_type ~= 'hash') then
        return redis.error_reply('扣额账本类型无效')
    end
end
local before = {}
for i = 1, 3 do
    local value = redis.call('GET', KEYS[i])
    if value and (not value:match('^%d+$') or (#value > 1 and value:sub(1, 1) == '0')
        or tonumber(value) > 9007199254740990) then
        return redis.error_reply('订阅配额计数器无效')
    end
    before[i] = tonumber(value) or 0
end
if tracking then
    -- Reject before emitting an event. XADD failure must not leave quota debited.
    local reason = 0
    if before[1] + 1 > tonumber(ARGV[3]) then reason = 1
    elseif tonumber(ARGV[4]) > 0 and before[2] + 1 > tonumber(ARGV[4]) then reason = 2
    elseif tonumber(ARGV[5]) > 0 and before[3] + 1 > tonumber(ARGV[5]) then reason = 3 end
    if reason ~= 0 then return {reason, before[1], before[2], before[3]} end
    local writes = {
        {'XADD', KEYS[4], '*', 'quantity', '1'},
        {'HSETNX', KEYS[5], 'baseline', tostring(before[3])},
        {'PERSIST', KEYS[3]},
        {'EXPIREAT', KEYS[1], ARGV[1]}, {'EXPIREAT', KEYS[2], ARGV[2]}}
    for i = 1, 3 do writes[#writes + 1] = {'INCR', KEYS[i]} end
    for _, command in ipairs(writes) do
        if not redis.acl_check_cmd(unpack(command)) then
            return redis.error_reply('扣额账本写权限不完整')
        end
    end
    redis.call('XADD', KEYS[4], '*',
        'order_id', ARGV[6], 'segment_id', ARGV[7], 'scope', ARGV[8],
        'period_key', ARGV[9], 'route_key', ARGV[10], 'charged_at_us', ARGV[11],
        'quantity', '1', 'policy', 'gateway_actual_charge_v1', 'source_run_id', epoch)
    redis.call('HSETNX', KEYS[5], 'baseline', tostring(before[3]))
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
    local precise_now = ngx.now and ngx.now() or ngx.time()
    local now = math.floor(precise_now)
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
        if (period.order_id ~= nil or period.segment_id ~= nil)
            and (type(period.order_id) ~= 'string' or not period.order_id:match('^[%w_-]+$')
                or #period.order_id > 36 or type(period.segment_id) ~= 'string'
                or not period.segment_id:match('^[%w_-]+$') or #period.segment_id > 36) then
            return finish(403, '订阅扣额归属无效')
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
        -- APISIX merges consumer plugins into matched.plugins in place. Its
        -- original route ID remains stable; platform route IDs equal route_key.
        or not matched or tostring(matched.id or "") ~= active.route_key
        or (route_policy and active.route_key ~= route_policy.route_key) then
        return finish(403, "订阅尚未生效、已到期或与 API 路由不匹配")
    end
    -- Stable combination and original-anchor keys survive renewal and key rotation.
    -- Month keys deliberately have no TTL. Migration may seed, but never reset, them.
    local scope = token(values.quota_scope)
    local prefix = "market:apisix:quota:subscription:"
    local beijing_day = math.floor((now + 28800) / 86400)
    local args = {
        prefix .. "minute:" .. scope .. ":" .. math.floor(now / 60),
        prefix .. "day:" .. scope .. ":" .. os.date("!%Y-%m-%d", now + 28800),
        prefix .. "month:" .. scope .. ":" .. token(active.period_key)}
    local key_count = 3
    if active.order_id and active.segment_id then
        key_count = 6
        args[4] = 'market:apisix:charges:v1'
        args[5] = 'market:apisix:charges:coverage:' .. scope .. ':' .. token(active.period_key)
        args[6] = 'market:apisix:charges:source'
    end
    local arguments = {
        (math.floor(now / 60) + 1) * 60 + 10,
        (beijing_day + 1) * 86400 - 28800 + 86400,
        active.rate_limit_per_minute, active.daily_quota, active.monthly_quota}
    if key_count == 6 then
        arguments[6], arguments[7], arguments[8] = active.order_id, active.segment_id, values.quota_scope
        arguments[9], arguments[10] = active.period_key, active.route_key
        arguments[11] = string.format('%.0f', precise_now * 1000000)
    end
    for _, value in ipairs(arguments) do args[#args + 1] = value end
    local result = red:eval(subscription_script, key_count, unpack(args))
    red:set_keepalive(60000, 100)
    if not result then return reject(503, "API 网关配额服务不可用") end
    local reason = tonumber(result[1])
    if reason == 1 then return reject(429, "超过 API 每分钟调用配额", ctx, "contract_quota", "caller") end
    if reason == 2 then return reject(429, "超过 API 每日调用配额", ctx, "contract_quota", "caller") end
    if reason == 3 then return reject(429, "超过 API 订阅月调用配额", ctx, "contract_quota", "caller") end
    if reason ~= 0 then return reject(503, "API 网关配额服务不可用") end
end

local _M = {version = 0.1, priority = 1800, name = "market-gateway-quota", schema = schema}

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

function _M.access(conf, ctx)
    local consumer = ctx.consumer_name or ngx.var.consumer_name or ""
    if ctx.market_sla_probe_verified == true then
        local matched = ctx.matched_route and ctx.matched_route.value
        local labels = matched and matched.labels or {}
        local binding = ctx.market_sla_probe_binding
        if consumer ~= "" and consumer == labels.market_sla_probe_consumer
            and type(binding) == "table" and matched
            and binding.gateway_route_id == tostring(matched.id)
            and binding.database_route_id and binding.database_route_id ~= ""
            and binding.database_route_id == labels.market_sla_probe_route_id
            and binding.product_id == labels.market_product_id and binding.version_id == labels.market_version_id
            and binding.config_version and binding.config_version ~= ""
            and binding.config_version == labels.market_sla_probe_config_version
            and binding.path == labels.market_sla_probe_path
            and binding.path == ctx.market_sla_probe_original_path
            and binding.method == ctx.market_sla_probe_original_method
            and binding.method == ngx.req.get_method() and binding.method == labels.market_sla_probe_method then
            -- Only the server-signed, read-only, dedicated probe identity is
            -- exempt. Standard key-auth and its own rate bucket still apply.
            return
        end
        return reject(403, "SLA 探测身份与路由不匹配", ctx, "valid_input_failure", "platform")
    end
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
    local result = red:eval(quota_script, 5,
        "market:apisix:quota:minute:" .. route .. ":" .. consumer .. ":" .. math.floor(ngx.time() / 60),
        "market:apisix:quota:day:" .. route .. ":" .. consumer .. ":" .. date,
        "market:apisix:quota:month:" .. route .. ":" .. consumer .. ":" .. month,
        "market:apisix:quota:total:" .. route .. ":" .. consumer,
        "market:apisix:credential:" .. consumer,
        70, 86400, 2678400, 31536000, daily_limit, monthly_limit, total_limit)
    if result and tonumber(result[1]) == 2 then
        local metadata = red:hgetall("market:apisix:credential:" .. consumer)
        if not metadata or #metadata == 0 then
            red:set_keepalive(60000, 100)
            return reject(503, "API 订阅配额切换信息不可用")
        end
        local values = {}
        for i = 1, #metadata, 2 do values[metadata[i]] = metadata[i + 1] end
        return subscription_access(red, conf, ctx, values)
    end
    red:set_keepalive(60000, 100)
    if not result then
        return reject(503, "API 网关配额服务不可用")
    end
    if tonumber(result[1]) ~= 1 then
        if total_limit > 0 and tonumber(result[4]) >= total_limit then
            return reject(403, "订单 API 调用额度已耗尽，访问凭据已回收")
        end
        if daily_limit > 0 and tonumber(result[2]) > daily_limit then
            return reject(429, "超过 API 每日调用配额", ctx, "contract_quota", "caller")
        end
        return reject(429, "超过 API 每月调用配额", ctx, "contract_quota", "caller")
    end
end

return _M
