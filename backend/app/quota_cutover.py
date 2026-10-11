"""Atomic Redis cutover; legacy lifetime usage is a conservative watermark."""
import hashlib
import json
import time
from datetime import datetime, timezone

MAX_COUNT = 9007199254740990
MAX_DATABASE_KEYS = 10000
MAX_SOURCES = 16
MAX_PLAN_KEYS = 2048
MAX_PLAN_BYTES = 262144
MAX_KEY_BYTES = 262144
MAX_SCAN_PAGES = 256
SCAN_COUNT = 256
MAX_SCAN_SECONDS = 2


class QuotaCutoverError(ValueError):
    pass

# Fixed ceilings cannot be raised by an imported preview file. All guards run
# before mutations, including inside EVAL to close the preflight/execution race.
SCAN_GUARD = """
local cluster = redis.call('INFO', 'cluster')
if not cluster:match('cluster_enabled:0') then error('Redis Cluster cutover unsupported') end
if not redis.call('INFO', 'replication'):match('role:master') then error('migration requires primary') end
if redis.call('DBSIZE') > 10000 then error('migration database key budget exceeded') end
if #KEYS > 2048 or #ARGV[1] > 262144 then error('migration plan budget exceeded') end
local key_bytes = 0
for _, key in ipairs(KEYS) do key_bytes = key_bytes + #key end
if key_bytes > 262144 then error('migration key byte budget exceeded') end
local started = redis.call('TIME')
local pages = 0
local function check_budget()
    local clock = redis.call('TIME')
    if (tonumber(clock[1])-tonumber(started[1]))*1000000
        + tonumber(clock[2])-tonumber(started[2]) > 100000 then
        error('migration atomic time budget exceeded')
    end
end
local function scan_page(cursor, pattern)
    pages = pages + 1
    if pages > 256 then error('migration scan page budget exceeded') end
    check_budget()
    local page = redis.call('SCAN', cursor, 'MATCH', pattern, 'COUNT', 256)
    check_budget()
    return page
end
"""

SYNC_SCRIPT = SCAN_GUARD + """
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
        local page = scan_page(cursor, pattern)
        cursor = page[1]
        for _, key in ipairs(page[2]) do
            local value = redis.call('GET', key)
            if value and (not value:match('^%d+$') or tonumber(value) ~= 0) then return {0} end
        end
    until cursor == '0'
end
redis.call('HLEN', KEYS[1])
check_budget()
for key, value in pairs(data) do redis.call('HSET', KEYS[1], key, tostring(value)) end
return {1}
"""

MIGRATE_SCRIPT = SCAN_GUARD + """
local plan = cjson.decode(ARGV[1])
if #plan.sources > 16 then error('migration source budget exceeded') end
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
-- Redis cjson emits at most 14 significant digits. Counts must remain exact
-- JSON integers, including values near the Lua safe-integer ceiling.
local function usage_json(minute_count, day_count, month_count)
    return string.format('{"minute":%.0f,"day":%.0f,"month":%.0f}',
        minute_count, day_count, month_count)
end
local function sources_json()
    local rows = {}
    for _, source in ipairs(sources) do
        table.insert(rows, '{"consumer":' .. cjson.encode(source.consumer)
            .. ',"lifetime_usage":' .. string.format('%.0f', source.lifetime_usage) .. '}')
    end
    return '[' .. table.concat(rows, ',') .. ']'
end
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
                local page = scan_page(cursor, pattern)
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
check_budget()
if ARGV[2] == 'preflight' then
    return '{"mode":"preflight_only","scan_pages":' .. pages
        .. ',"before":' .. usage_json(minute,day,month)
        .. ',"after":' .. usage_json(after_minute,after_day,after_month)
        .. ',"sources":' .. sources_json() .. '}'
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
    mode='conservative_lifetime_watermark', scan_pages=pages})
result = result:sub(1, -2) .. ',"sources":' .. sources_json()
    .. ',"before":' .. usage_json(minute,day,month)
    .. ',"after":' .. usage_json(after_minute,after_day,after_month) .. '}'
redis.call('SET', KEYS[4], result)
return result
"""


def token(value):
    return value.replace("%", "%25").replace(":", "%3A")


def glob_token(value):
    return "".join("\\" + c if c in "\\*?[]" else c for c in value)


def preflight(client):
    """Read-only topology/size check; refuse unknown topology and large databases."""
    if client.info('cluster').get('cluster_enabled') != 0:
        raise QuotaCutoverError('Redis Cluster cutover unsupported; node-local SCAN is incomplete')
    if client.info('replication').get('role') != 'master':
        raise QuotaCutoverError('Migration requires a standalone writable primary')
    size = client.dbsize()
    if size > MAX_DATABASE_KEYS:
        raise QuotaCutoverError('Migration database key budget exceeded')
    return dict(topology='standalone_primary', database_keys=size,
                max_database_keys=MAX_DATABASE_KEYS, max_sources=MAX_SOURCES,
                max_plan_keys=MAX_PLAN_KEYS, max_scan_pages=MAX_SCAN_PAGES,
                max_plan_bytes=MAX_PLAN_BYTES, max_key_bytes=MAX_KEY_BYTES,
                atomic_scan_budget_ms=100)


def legacy_keys(client, route, consumer, *, budget=None):
    if not route or not consumer or ':' in route or ':' in consumer:
        raise QuotaCutoverError('Ambiguous legacy route/consumer key mapping')
    preflight(client)
    if budget is None:
        budget = {'pages': 0, 'started': time.monotonic()}
    prefix = f"market:apisix:quota:*:{glob_token(route)}:{glob_token(consumer)}"
    found = set()
    for pattern in (prefix, prefix + ':*'):
        cursor = 0
        while True:
            budget['pages'] += 1
            if budget['pages'] > MAX_SCAN_PAGES or time.monotonic() - budget['started'] > MAX_SCAN_SECONDS:
                raise QuotaCutoverError('Migration preview scan budget exceeded')
            cursor, page = client.scan(cursor=cursor, match=pattern, count=SCAN_COUNT)
            found.update(page)
            if len(found) > MAX_PLAN_KEYS or sum(len(key.encode()) for key in found) > MAX_KEY_BYTES:
                raise QuotaCutoverError('Migration source key budget exceeded')
            if int(cursor) == 0:
                break
    return sorted(found)


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
    preflight(client)
    if len(credentials) > MAX_SOURCES:
        raise QuotaCutoverError('Migration source budget exceeded')
    budget = {'pages': 0, 'started': time.monotonic()}
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
        for key in legacy_keys(client, route, consumer, budget=budget):
            keys.append(key); source["counter_indices"].append(len(keys))
        if len(keys) > MAX_PLAN_KEYS:
            raise QuotaCutoverError('Migration plan key budget exceeded')
        if client.hget(meta, "mode") != "subscription":
            value = client.get(total)
            if value is None:
                value = '0'
            if not isinstance(value, str) or not value.isascii() or not value.isdigit() or (len(value) > 1 and value[0] == '0') or int(value) > MAX_COUNT:
                raise QuotaCutoverError("历史用量计数无效")
            estimated += int(value)
        sources.append(source)
    plan = {"scope": scope, "created_at": now, "actor": actor, "request_id": request_id,
            "valid_until": min(now + 30, minute_end, day_end, min(p["end_at"] for p in active)),
            "minute_end": minute_end, "day_end": day_end, "sources": sources,
            "estimated_lifetime_watermark": estimated}
    plan["fingerprint"] = hashlib.sha256(json.dumps({"keys": keys, "plan": plan},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if len(json.dumps(plan).encode()) > MAX_PLAN_BYTES or sum(len(key.encode()) for key in keys) > MAX_KEY_BYTES:
        raise QuotaCutoverError('Migration plan byte budget exceeded')
    return {"keys": keys, "plan": plan}


def execute(client, prepared, *, acknowledge_conservative_usage=False, cutover_guard_verified=False,
            preflight_only=False):
    preflight(client)
    if not preflight_only and (not acknowledge_conservative_usage or not cutover_guard_verified):
        raise QuotaCutoverError("须确认保守用量口径，并确认所有 APISIX 副本已启用原子切换保护")
    keys, plan = prepared["keys"], prepared["plan"]
    unsigned = {key: value for key, value in plan.items() if key != "fingerprint"}
    fingerprint = hashlib.sha256(json.dumps({"keys": keys, "plan": unsigned},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if fingerprint != plan["fingerprint"]:
        raise QuotaCutoverError("迁移预览文件已修改，请重新生成")
    if (len(keys) > MAX_PLAN_KEYS or len(plan['sources']) > MAX_SOURCES
            or len(json.dumps(plan).encode()) > MAX_PLAN_BYTES
            or sum(len(key.encode()) for key in keys) > MAX_KEY_BYTES):
        raise QuotaCutoverError('Migration plan budget exceeded')
    result = client.eval(MIGRATE_SCRIPT, len(keys), *keys, json.dumps(plan),
                         'preflight' if preflight_only else 'execute')
    return json.loads(result)
