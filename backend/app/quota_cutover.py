"""Atomic Redis cutover; legacy lifetime usage is a conservative watermark."""
import hashlib
import json
from datetime import datetime, timezone

MAX_COUNT = 9007199254740990


class QuotaCutoverError(ValueError):
    pass

SYNC_SCRIPT = """
for i = 2, #KEYS do
    local value = redis.call('GET', KEYS[i])
    if value and (not value:match('^%d+$') or (#value > 1 and value:sub(1,1) == '0')
        or tonumber(value) > 9007199254740990) then
        return {-1}
    end
    if value and tonumber(value) > 0 then return {0} end
end
local data = cjson.decode(ARGV[1])
for _, pattern in ipairs(cjson.decode(ARGV[2])) do
    local cursor = '0'
    repeat
        local page = redis.call('SCAN', cursor, 'MATCH', pattern, 'COUNT', 1000)
        cursor = page[1]
        for _, key in ipairs(page[2]) do
            local value = redis.call('GET', key)
            if value and (not value:match('^%d+$') or tonumber(value) ~= 0) then return {0} end
        end
    until cursor == '0'
end
for key, value in pairs(data) do redis.call('HSET', KEYS[1], key, tostring(value)) end
return {1}
"""

MIGRATE_SCRIPT = """
local plan = cjson.decode(ARGV[1])
local receipt = redis.call('GET', KEYS[4])
if receipt then
    local saved = cjson.decode(receipt)
    if saved.fingerprint ~= plan.fingerprint then return redis.error_reply('migration request conflict') end
    return receipt
end
local clock = tonumber(redis.call('TIME')[1])
if clock >= plan.valid_until or clock < plan.created_at then
    return redis.error_reply('migration preview expired')
end
local function count(index)
    local value = redis.call('GET', KEYS[index])
    if value and (not value:match('^%d+$') or (#value > 1 and value:sub(1,1) == '0')
        or tonumber(value) > 9007199254740990) then
        error('invalid migration counter')
    end
    return tonumber(value or '0'), value ~= false
end
local minute, day, month = count(1), count(2), count(3)
local old_minute, watermark = 0, 0
local sources = {}
for _, source in ipairs(plan.sources) do
    local mode = redis.call('HGET', KEYS[source.metadata_index], 'mode')
    if mode and mode ~= 'subscription' then return redis.error_reply('unknown legacy mode') end
    if mode == 'subscription' then
        if redis.call('HGET', KEYS[source.metadata_index], 'quota_scope') ~= plan.scope then
            return redis.error_reply('credential already belongs to another scope')
        end
    else
        local total, present = count(source.total_index)
        local recent = count(source.minute_index)
        for _, index in ipairs(source.counter_indices) do
            local value = count(index)
            if value > total then return redis.error_reply('legacy lifetime counter incomplete') end
        end
        -- Counters created after preview must also be complete. This explicit
        -- maintenance operation scans only read-only until all checks pass.
        for _, pattern in ipairs(source.counter_patterns) do
            local cursor = '0'
            repeat
                local page = redis.call('SCAN', cursor, 'MATCH', pattern, 'COUNT', 1000)
                cursor = page[1]
                for _, key in ipairs(page[2]) do
                    local raw = redis.call('GET', key)
                    if raw and (not raw:match('^%d+$') or (#raw > 1 and raw:sub(1,1) == '0')
                        or tonumber(raw) > total) then
                        return redis.error_reply('legacy lifetime counter incomplete')
                    end
                end
            until cursor == '0'
        end
        if recent > total or (not present and recent > 0) then
            return redis.error_reply('legacy lifetime counter incomplete')
        end
        watermark = watermark + total
        old_minute = old_minute + recent
        table.insert(sources, {consumer=source.consumer, lifetime_usage=total})
    end
end
local after_minute, after_day, after_month = minute+old_minute, day+watermark, month+watermark
if after_minute > 9007199254740990 or after_day > 9007199254740990
    or after_month > 9007199254740990 then return redis.error_reply('migration counter overflow') end
-- Validate all hash types before any mutation (Redis scripts cannot roll back).
for _, source in ipairs(plan.sources) do
    redis.call('HLEN', KEYS[source.metadata_index])
end
redis.call('SET', KEYS[1], string.format('%.0f', after_minute))
redis.call('EXPIREAT', KEYS[1], plan.minute_end+10)
redis.call('SET', KEYS[2], string.format('%.0f', after_day))
redis.call('EXPIREAT', KEYS[2], plan.day_end+86400)
redis.call('SET', KEYS[3], string.format('%.0f', after_month))
for _, source in ipairs(plan.sources) do
    for key, value in pairs(source.data) do
        redis.call('HSET', KEYS[source.metadata_index], key, tostring(value))
    end
end
local result = cjson.encode({fingerprint=plan.fingerprint, scope=plan.scope,
    request_id=plan.request_id, actor=plan.actor, executed_at=clock,
    mode='conservative_lifetime_watermark', sources=sources,
    before={minute=minute,day=day,month=month},
    after={minute=after_minute,day=after_day,month=after_month}})
redis.call('SET', KEYS[4], result)
return result
"""


def token(value):
    return value.replace("%", "%25").replace(":", "%3A")


def glob_token(value):
    return "".join("\\" + c if c in "\\*?[]" else c for c in value)


def legacy_keys(client, route, consumer):
    prefix = f"market:apisix:quota:*:{glob_token(route)}:{glob_token(consumer)}"
    return sorted(set(client.scan_iter(match=prefix)) |
                  set(client.scan_iter(match=prefix + ":*")))


def sync_unused(client, key, data, route, consumer):
    keys = [key, f"market:apisix:quota:total:{route}:{consumer}"]
    keys.extend(k for k in legacy_keys(client, route, consumer) if k not in keys)
    pattern = f"market:apisix:quota:*:{glob_token(route)}:{glob_token(consumer)}"
    result = client.eval(SYNC_SCRIPT, len(keys), *keys, json.dumps(data),
                         json.dumps([pattern, pattern + ":*"]))
    if result[0] != 1:
        raise QuotaCutoverError("历史网关用量需完成配额迁移后才可切换订阅计数，当前用量不会清零")


def preview(client, scope, credentials, *, actor, request_id):
    """Credentials: [(route_key, consumer_name, approved metadata), ...]."""
    if not credentials or not request_id or len(request_id) > 128:
        raise QuotaCutoverError("缺少待迁移凭据或请求标识")
    now = int(client.time()[0])
    active = []
    for route, consumer, data in credentials:
        if data.get("quota_scope") != scope or data.get("mode") != "subscription":
            raise QuotaCutoverError("迁移凭据与订阅组合不匹配")
        periods = json.loads(data["subscription_periods"])
        if not isinstance(periods, list):
            raise QuotaCutoverError("订阅周期必须为列表")
        for index, period in enumerate(periods):
            if not isinstance(period, dict) or any(type(period.get(field)) is not int or
                period[field] < minimum or period[field] > MAX_COUNT for field, minimum in
                (("start_at", 0), ("end_at", 0), ("rate_limit_per_minute", 1),
                 ("daily_quota", 0), ("monthly_quota", 0))):
                raise QuotaCutoverError("订阅周期计数或时间字段无效")
            if period["start_at"] >= period["end_at"] or not isinstance(period.get("period_key"), str) or not period["period_key"] or period.get("route_key") != route:
                raise QuotaCutoverError("订阅周期身份或路由无效")
            if any(period["start_at"] < previous["end_at"] and previous["start_at"] < period["end_at"]
                   for previous in periods[:index]):
                raise QuotaCutoverError("订阅周期存在重叠")
        current = [p for p in periods if p["start_at"] <= now < p["end_at"]]
        if len(current) != 1 or current[0].get("route_key") != route:
            raise QuotaCutoverError("每条迁移凭据必须绑定唯一有效的版本路由周期")
        active.extend(current)
    period_keys = {p["period_key"] for p in active}
    if len(period_keys) != 1:
        raise QuotaCutoverError("必须存在唯一有效的订阅月周期")
    period_key = next(iter(period_keys))
    minute_end = (now // 60 + 1) * 60
    day_end = ((now + 28800) // 86400 + 1) * 86400 - 28800
    day = datetime.fromtimestamp(now + 28800, timezone.utc).strftime("%Y-%m-%d")
    prefix = "market:apisix:quota:subscription:"
    keys = [f"{prefix}minute:{token(scope)}:{now // 60}",
            f"{prefix}day:{token(scope)}:{day}",
            f"{prefix}month:{token(scope)}:{token(period_key)}",
            f"market:apisix:quota:migration:{token(scope)}:{token(request_id)}"]
    sources, estimated = [], 0
    names = set()
    for route, consumer, data in sorted(credentials, key=lambda row: (row[0], row[1])):
        if not consumer or consumer in names:
            raise QuotaCutoverError("迁移凭据为空或重复")
        names.add(consumer)
        meta = "market:apisix:credential:" + consumer
        total = f"market:apisix:quota:total:{route}:{consumer}"
        minute = f"market:apisix:quota:minute:{route}:{consumer}:{now // 60}"
        pattern = f"market:apisix:quota:*:{glob_token(route)}:{glob_token(consumer)}"
        source = {"consumer": consumer, "data": data,
                  "counter_patterns": [pattern, pattern + ":*"]}
        for field, key in (("metadata_index", meta), ("total_index", total), ("minute_index", minute)):
            keys.append(key); source[field] = len(keys)
        source["counter_indices"] = []
        for key in legacy_keys(client, route, consumer):
            keys.append(key); source["counter_indices"].append(len(keys))
        if client.hget(meta, "mode") != "subscription":
            value = client.get(total) or "0"
            if not str(value).isdigit() or int(value) > MAX_COUNT:
                raise QuotaCutoverError("历史用量计数无效")
            estimated += int(value)
        sources.append(source)
    plan = {"scope": scope, "created_at": now, "actor": actor, "request_id": request_id,
            "valid_until": min(now + 30, minute_end, day_end, min(p["end_at"] for p in active)),
            "minute_end": minute_end, "day_end": day_end, "sources": sources,
            "estimated_lifetime_watermark": estimated}
    plan["fingerprint"] = hashlib.sha256(json.dumps({"keys": keys, "plan": plan},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"keys": keys, "plan": plan}


def execute(client, prepared, *, acknowledge_conservative_usage=False, cutover_guard_verified=False):
    if not acknowledge_conservative_usage or not cutover_guard_verified:
        raise QuotaCutoverError("须确认保守用量口径，并确认所有 APISIX 副本已启用原子切换保护")
    keys, plan = prepared["keys"], prepared["plan"]
    unsigned = {key: value for key, value in plan.items() if key != "fingerprint"}
    fingerprint = hashlib.sha256(json.dumps({"keys": keys, "plan": unsigned},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if fingerprint != plan["fingerprint"]:
        raise QuotaCutoverError("迁移预览文件已修改，请重新生成")
    result = client.eval(MIGRATE_SCRIPT, len(keys), *keys, json.dumps(plan))
    return json.loads(result)
