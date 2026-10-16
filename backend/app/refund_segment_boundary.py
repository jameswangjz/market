"""Redis-only policy boundary primitive; no database or HTTP integration.

Requires the gateway's exact-policy EVAL guard and Redis 7 ACL checks. A receipt
is durable intent, not proof that the policy switch completed. A final HSET
failure requires manual reconciliation; retry cannot safely reuse its boundary
because the old policy may continue charging and time continues to elapse.
Callers must retain receipts and serialize all other policy writers through
the same compare-and-switch contract. Redis Cluster is not supported.
"""
import hashlib
import json
import re

from .quota_cutover import token


MAX_INTEGER = 9007199254740991
IDENTITY = ('mode', 'status', 'quota_scope', 'subscription_periods')
NAMESPACE = 'market:apisix:refund:segment-boundary:v1:'
PERIOD_FIELDS = {'start_at', 'end_at', 'period_key', 'route_key', 'monthly_quota'}
PERIOD_IDENTITIES = {'order_id', 'term_id', 'segment_id'}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False)


def _text(value, limit=4096):
    if type(value) is not str or not value or len(value.encode()) > limit or '\x00' in value:
        raise ValueError('invalid boundary string')
    return value


def _integer(value):
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise ValueError('invalid boundary integer')


def _policy(value):
    if type(value) is not dict or not set(IDENTITY) <= value.keys() or len(value) > 64:
        raise ValueError('invalid policy mapping')
    for key, item in value.items():
        _text(key)
        if type(item) is int and key not in IDENTITY:
            _integer(item)
        else:
            _text(item, 262144 if key == 'subscription_periods' else 4096)
    if value['mode'] != 'subscription' or value['status'] != 'active':
        raise ValueError('requires active subscription')
    try:
        rows = json.loads(value['subscription_periods'])
    except (ValueError, TypeError) as exc:
        raise ValueError('invalid subscription periods') from exc
    if type(rows) is not list or not 1 <= len(rows) <= 256:
        raise ValueError('invalid subscription periods')
    previous_end = 0
    seen = set()
    for row in rows:
        if (type(row) is not dict or not PERIOD_FIELDS <= row.keys()
                or row.keys() - (PERIOD_FIELDS | PERIOD_IDENTITIES |
                                 {'rate_limit_per_minute', 'daily_quota'})):
            raise ValueError('invalid metadata period fields')
        for field, item in row.items():
            if field in ('start_at', 'end_at', 'monthly_quota', 'daily_quota', 'rate_limit_per_minute'):
                _integer(item)
                if field == 'rate_limit_per_minute' and item == 0:
                    raise ValueError('invalid rate limit')
            else:
                _text(item)
        if (row['start_at'] < previous_end or row['start_at'] >= row['end_at']
                or row['period_key'] in seen):
            raise ValueError('invalid metadata period order or identity')
        previous_end = row['end_at']
        seen.add(row['period_key'])
    return rows


SCRIPT = r"""
local p = cjson.decode(ARGV[1])
local function fail(message) error('boundary: ' .. message) end
redis.call('HLEN', KEYS[1])
if redis.call('PTTL', KEYS[1]) ~= -1 then fail('missing or temporary metadata') end
local function matches(policy, fields)
    for _, field in ipairs(fields) do
        if redis.call('HGET', KEYS[1], field) ~= policy[field] then return false end
    end
    return true
end
local identity = {'mode', 'status', 'quota_scope', 'subscription_periods'}
local run = redis.call('HGET', KEYS[3], 'run_id')
if not run or #run ~= 40 or not run:match('^[a-f0-9]+$') then fail('invalid source epoch') end
if p.source_run_id and p.source_run_id ~= run then fail('source epoch changed') end
if KEYS[4] ~= 'market:apisix:charges:v1'
    and KEYS[4] ~= 'market:apisix:charges:v1:' .. run then fail('source stream changed') end
local tail = redis.call('XREVRANGE', KEYS[4], '+', '-', 'COUNT', 1)
local boundary = '0-0'
if #tail > 0 then boundary = tail[1][1] end
for _, field in ipairs(redis.call('HKEYS', KEYS[1])) do
    if not p.target[field] then fail('target omits existing metadata field') end
end
local hset = {'HSET', KEYS[1]}
local target_fields = {}
for field, value in pairs(p.target) do
    target_fields[#target_fields+1] = field
    hset[#hset+1] = field
    hset[#hset+1] = value
end
if not redis.acl_check_cmd('SET', KEYS[2], 'preflight')
    or not redis.acl_check_cmd(unpack(hset)) then fail('write ACL denied') end
local saved = redis.call('GET', KEYS[2])
if saved then
    local receipt = cjson.decode(saved)
    if receipt.payload_fingerprint ~= p.fingerprint then fail('replay conflict') end
    if receipt.source_run_id ~= run then fail('replay source epoch changed') end
    if matches(p.target, target_fields) then return saved end
    if matches(p.expected, identity) then
        fail('incomplete_switch_intent_requires_manual_reconciliation')
    end
    fail('replay policy conflict')
end
if not matches(p.expected, identity) then fail('stale policy') end
local clock = redis.call('TIME')
local now = tonumber(clock[1])
local rows = {}
local active = 0
for i, period in ipairs(p.periods) do
    if period.end_at > now then
    if period.start_at <= now then active = active + 1 end
    local counter_key = KEYS[4+i*2-1]
    local coverage_key = KEYS[4+i*2]
    local raw = redis.call('GET', counter_key)
    if raw and (not raw:match('^%d+$') or (#raw > 1 and raw:sub(1,1) == '0')
        or #raw > 16 or tonumber(raw) > 9007199254740991) then fail('invalid counter') end
    if redis.call('PTTL', counter_key) >= 0 then fail('nonpersistent counter') end
    if redis.call('HGET', coverage_key, 'baseline') ~= '0'
        or redis.call('PTTL', coverage_key) ~= -1 then fail('unknown coverage') end
    local used = tonumber(raw or '0')
    if period.start_at > now and used ~= 0 then fail('future usage') end
    -- cjson numbers lose precision above 14 significant digits; append exact
    -- integer fields explicitly, including microseconds and quota differences.
    local available = 'null'
    if period.monthly_quota ~= 0 then
        available = string.format('%.0f', math.max(0, period.monthly_quota-used))
    end
    rows[#rows+1] = p.period_json[i]:sub(1,-2)
        .. ',"used_at_switch":' .. string.format('%.0f',used)
        .. ',"available_quota":' .. available .. '}'
    end
end
if active ~= 1 then fail('requires one current period') end
local receipt = cjson.encode({schema='refund_segment_boundary', version=1,
    change_id=p.change_id, direction=p.direction, scope=p.expected.quota_scope,
    source_run_id=run, boundary_id=boundary,
    target_fingerprint=p.target_fingerprint, payload_fingerprint=p.fingerprint})
receipt = receipt:sub(1,-2) .. ',"cutover_us":'
    .. clock[1] .. string.format('%06d',tonumber(clock[2]))
    .. ',"periods":[' .. table.concat(rows, ',') .. ']}'
-- SET is the first write: OOM cannot leave an unreceipted metadata switch.
redis.call('SET', KEYS[2], receipt)
redis.call(unpack(hset))
return receipt
"""


def freeze_policy_switch(client, *, credential_key, change_id, direction,
                         expected_policy: dict, target_policy: dict, periods: list,
                         source_context=None):
    """Return a JSON-compatible receipt dict, or raise without speculative writes.

    Policies contain Redis string fields; selected identity compares exact bytes.
    Periods describe the complete target schedule (including past months), with
    optional order_id/term_id/segment_id identity. Zero monthly quota is unlimited.
    Only current/future periods appear in the receipt; past usage is not read.
    Target must include every existing metadata field: deletion is unsupported.
    Existing intent is replayable only with the complete target still installed
    and the same source epoch. Incomplete switches require manual reconciliation;
    receipt presence alone is not evidence of completion or safe recovery.
    Directions are target and original; compensation has its own immutable key.
    """
    _text(credential_key)
    if (not credential_key.startswith('market:apisix:credential:')
            or credential_key == 'market:apisix:credential:'):
        raise ValueError('invalid credential namespace')
    if type(change_id) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', change_id):
        raise ValueError('invalid change id')
    if direction not in ('target', 'original'):
        raise ValueError('invalid direction')
    _policy(expected_policy)
    target_rows = _policy(target_policy)
    expected_policy = {k: str(v) for k, v in expected_policy.items()}
    target_policy = {k: str(v) for k, v in target_policy.items()}
    if expected_policy['quota_scope'] != target_policy['quota_scope']:
        raise ValueError('scope change forbidden')
    if type(periods) is not list or not 1 <= len(periods) <= 256:
        raise ValueError('invalid period budget')
    required = PERIOD_FIELDS
    allowed = required | PERIOD_IDENTITIES
    seen = set()
    previous_end = 0
    for period in periods:
        if type(period) is not dict or not required <= period.keys() or period.keys() - allowed:
            raise ValueError('invalid period fields')
        for field, value in period.items():
            (_integer if field in ('start_at', 'end_at', 'monthly_quota') else _text)(value)
        if period['start_at'] < previous_end or period['start_at'] >= period['end_at']:
            raise ValueError('unordered or overlapping periods')
        previous_end = period['end_at']
        if period['period_key'] in seen:
            raise ValueError('duplicate period counter')
        seen.add(period['period_key'])
    # Target schedules may include rate/day fields but every boundary identity
    # must match, and no unaccounted target period can be introduced.
    if len(target_rows) != len(periods) or any(
        type(row) is not dict or (row.keys() & allowed) != period.keys()
        or any(row.get(k) != v or type(row.get(k)) is not type(v)
                                   for k, v in period.items())
        for row, period in zip(target_rows, periods)
    ):
        raise ValueError('target schedule differs from boundary periods')
    payload = dict(change_id=change_id, direction=direction, expected=expected_policy,
                   target=target_policy, periods=periods, credential_key=credential_key)
    payload['period_json'] = [_json(period) for period in periods]
    payload['target_fingerprint'] = hashlib.sha256(_json(target_policy).encode()).hexdigest()
    payload['fingerprint'] = hashlib.sha256(_json(payload).encode()).hexdigest()
    stream = 'market:apisix:charges:v1'
    if source_context is not None:
        stream, cursor = source_context
        if (cursor is None or not re.fullmatch(r'[a-f0-9]{40}', cursor.redis_run_id)
                or stream not in ('market:apisix:charges:v1',
                                  'market:apisix:charges:v1:' + cursor.redis_run_id)):
            raise ValueError('invalid boundary source context')
        if stream != 'market:apisix:charges:v1' and any(
                p['start_at'] < cursor.activated_at.timestamp() for p in periods
                if p['end_at'] > cursor.activated_at.timestamp()):
            raise ValueError('boundary period predates source activation')
        payload['source_run_id'] = cursor.redis_run_id
    encoded = _json(payload)
    if len(encoded.encode()) > 262144:
        raise ValueError('boundary payload budget exceeded')
    credential_hash = hashlib.sha256(credential_key.encode()).hexdigest()
    keys = [credential_key, f'{NAMESPACE}{change_id}:{direction}:{credential_hash}',
            'market:apisix:charges:source', stream]
    scope = token(expected_policy['quota_scope'])
    for period in periods:
        suffix = scope + ':' + token(period['period_key'])
        keys.extend(['market:apisix:quota:subscription:month:' + suffix,
                     'market:apisix:charges:coverage:' + suffix])
    if sum(len(key.encode()) for key in keys) > 262144:
        raise ValueError('boundary key budget exceeded')
    return json.loads(client.eval(SCRIPT, len(keys), *keys, encoded))
