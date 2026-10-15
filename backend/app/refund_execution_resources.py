"""Bounded BE012 entitlement recovery; no payment execution or HTTP routes.

Caller commits prepare's plan before apply, serializes executions per term and
owns DB transactions. All other policy writers must use exact-policy CAS.
Redis Cluster is unsupported. Native API expiry rounds up to seconds without
extending existing native periods; economic timestamps remain exact. A saved
Redis intent without complete target readback requires manual reconciliation.
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, Column, String, Text, ForeignKey, Integer, UniqueConstraint, CheckConstraint, event

from .refund_segment_store import canonical, digest, safe_policy, strict_json
from .quota_cutover import token
from .subscription_terms import UTCDateTime

PREFIX = 'market:apisix:refund:execution:v1:'
EPOCH = 'market:apisix:charges:source'


def fail(message):
    raise HTTPException(409, message)


def aware(value):
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None or at.utcoffset() is None:
        fail('Refund expiry requires timezone')
    return at.astimezone(timezone.utc)


SCRIPT = r"""
local p = cjson.decode(ARGV[1])
local function fail(s) error('refund recovery: '..s) end
local function matches(policy)
    if redis.call('HLEN', KEYS[1]) ~= #p.fields then return false end
    for _,field in ipairs(p.fields) do
        if redis.call('HGET', KEYS[1], field) ~= policy[field] then return false end
    end
    return true
end
local function owns(row) return row.order_id == p.order_id and row.segment_id == p.term_id end
local function scoped_target()
    for _,field in ipairs(p.fields) do
        if field ~= 'subscription_periods' and redis.call('HGET',KEYS[1],field) ~= p.target[field] then return false end
    end
    local current = cjson.decode(redis.call('HGET',KEYS[1],'subscription_periods') or 'null')
    if type(current) ~= 'table' then return false end
    local wanted = cjson.decode(p.target.subscription_periods)
    local a,b = {},{}
    for _,row in ipairs(current) do if owns(row) then a[#a+1]=row end end
    for _,row in ipairs(wanted) do if owns(row) then b[#b+1]=row end end
    if #a ~= #b then return false end
    for i,row in ipairs(a) do
        for k,v in pairs(row) do if b[i][k] ~= v then return false end end
        for k,v in pairs(b[i]) do if row[k] ~= v then return false end end
    end
    return true
end
if redis.call('PTTL', KEYS[1]) ~= -1 then fail('missing or temporary metadata') end
if redis.call('PTTL', KEYS[3]) ~= -1 then fail('missing or temporary source epoch') end
local actual_run = redis.call('INFO', 'server'):match('run_id:([a-f0-9]+)')
if actual_run ~= p.run_id then fail('Redis process epoch changed') end
if redis.call('HGET', KEYS[3], 'run_id') ~= p.run_id then fail('source epoch changed') end
local saved = redis.call('GET', KEYS[2])
if saved then
    local receipt = cjson.decode(saved)
    if receipt.plan_hash ~= p.plan_hash or receipt.run_id ~= p.run_id then fail('replay conflict') end
    for i, row in ipairs(p.checks) do
        local counter, coverage = KEYS[3+i*2-1], KEYS[3+i*2]
        local raw = redis.call('GET', counter) or '0'
        if not raw:match('^%d+$') or (#raw > 1 and raw:sub(1,1) == '0')
            or #raw > 16 or tonumber(raw) > 9007199254740991
            or redis.call('PTTL', counter) >= 0 or redis.call('PTTL',coverage) ~= -1
            or redis.call('HGET',coverage,'baseline') ~= '0'
            or not receipt.observed[i] or tonumber(raw) < tonumber(receipt.observed[i]) then fail('replay source coverage lost') end
    end
    if not scoped_target() then fail('incomplete or superseded intent; manual reconciliation required') end
    return saved
end
if redis.call('PTTL', KEYS[1]) ~= -1 or not matches(p.expected) then fail('stale policy') end
if redis.call('HGET', KEYS[3], 'run_id') ~= p.run_id then fail('source epoch changed') end
local now = redis.call('TIME')
local observed = {}
for i, row in ipairs(p.checks) do
    local counter, coverage = KEYS[3+i*2-1], KEYS[3+i*2]
    local raw = redis.call('GET', counter)
    if raw and (not raw:match('^%d+$') or (#raw > 1 and raw:sub(1,1) == '0')
        or #raw > 16 or tonumber(raw) > 9007199254740991) then fail('invalid counter') end
    if redis.call('PTTL', counter) >= 0 or redis.call('PTTL', coverage) ~= -1
        or redis.call('HGET', coverage, 'baseline') ~= '0' then fail('unknown coverage') end
    local used = tonumber(raw or '0')
    observed[i] = raw or '0'
    if row.minimum_used and used < row.minimum_used then fail('known usage lost') end
    if row.limit and used > row.limit then fail('concurrent usage exceeds reduced quota') end
    if row.future and used ~= 0 then fail('future usage') end
    if row.expired_end and tonumber(now[1]) < row.expired_end then fail('expired rights are still live') end
    if p.basis == 'time' and row.cutoff > 0 and tonumber(now[1]) >= row.cutoff then fail('refunded tail already consumed') end
end
local hset = {'HSET', KEYS[1]}
for _,field in ipairs(p.fields) do hset[#hset+1]=field; hset[#hset+1]=p.target[field] end
if not redis.acl_check_cmd('SET', KEYS[2], 'preflight')
    or not redis.acl_check_cmd(unpack(hset)) then fail('write ACL denied') end
local receipt = cjson.encode({schema='refund_resource_recovery_v1',refund_id=p.refund_id,
    plan_hash=p.plan_hash,target_hash=p.target_hash,run_id=p.run_id,observed=observed})
redis.call('SET', KEYS[2], receipt)
redis.call(unpack(hset))
return receipt
"""


def install(ns):
    if 'refund_execution_resources' in ns:
        return ns['refund_execution_resources']

    class Recovery(ns['Base']):
        __tablename__ = 'refund_resource_recoveries'
        __table_args__ = (UniqueConstraint('term_id', 'recovery_number'),
                         CheckConstraint("stage = 'resources_applied'"))
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        refund_id = Column(ForeignKey('refunds.id'), nullable=False, unique=True)
        execution_id = Column(ForeignKey('ordinary_refund_executions.id'), nullable=False, unique=True)
        order_id = Column(ForeignKey('orders.id'), nullable=False, index=True)
        term_id = Column(ForeignKey('subscription_terms.id'), nullable=False, index=True)
        combination_id = Column(ForeignKey('subscription_combinations.id'), nullable=False)
        generation = Column(Integer, nullable=False)
        recovery_number = Column(Integer, nullable=False)
        plan_json = Column(Text, nullable=False)
        proof_json = Column(Text, nullable=False)
        payload_hash = Column(String(64), nullable=False)
        stage = Column(String(30), nullable=False, default='resources_applied')
        created_at = Column(UTCDateTime(), nullable=False, default=ns['now'])

    def immutable(*_args):
        raise ValueError('refund resource recoveries are immutable')
    event.listen(Recovery, 'before_update', immutable)
    event.listen(Recovery, 'before_delete', immutable)

    def approved(db, order, refund, envelope):
        case = db.get(ns['refund_workflow']['Case'], refund.id)
        if (not case or not case.plan_json or hashlib.sha256(case.plan_json.encode()).hexdigest() != case.plan_hash
                or strict_json(case.plan_json) != envelope or envelope.get('refund_id') != refund.id
                or envelope.get('order_id') != order.id or refund.order_id != order.id
                or refund.status not in ('approved', 'executing', 'failed', 'completed') or envelope.get('basis') not in ('calls', 'time')):
            fail('Approved automatic refund envelope is missing or mismatched')
        plan = envelope.get('economic_plan', {})
        if (plan.get('basis') != envelope['basis'] or plan.get('source_order_id') != order.id
                or not envelope.get('evidence_ids') or str(plan.get('refund_amount')) != str(envelope.get('amount'))):
            fail('Automatic economic resource evidence is missing')
        if type(plan.get('recovery_delta')) is not int or plan['recovery_delta'] < 0:
            fail('Invalid frozen resource recovery')
        return plan

    def context(db, order, economic):
        terms = ns['subscription_terms']
        rows = db.scalars(select(terms['Term']).where(terms['Term'].order_id == order.id).with_for_update()).all()
        if len(rows) != 1:
            fail('Unique refund term required')
        term = rows[0]
        combo = db.get(terms['Combination'], term.combination_id)
        if (not combo or combo.enterprise_id != order.buyer_enterprise_id or combo.product_id != order.product_id
                or term.generation != combo.generation or term.status != 'active'):
            fail('Refund resource ownership changed')
        if economic.get('segment_id') != term.id:
            latest = ns.get('refund_segment_store', {}).get('latest')
            window = latest(db, order) if callable(latest) else None
            if (not window or window.id != economic.get('segment_id')
                    or window.payload_hash != economic.get('window_hash')):
                fail('Refund economic window is no longer verified')
        return term, combo

    def api_plan(db, order, refund, economic, term, combo):
        Credential, Route = ns['ApiCredential'], ns['ApiGatewayRoute']
        rows = db.scalars(select(Credential).join(Route, Route.id == Credential.route_id).where(
            Credential.enterprise_id == order.buyer_enterprise_id, Route.product_id == order.product_id,
            Credential.status == 'active')).all()
        if len(rows) != 1 or rows[0].order_id not in ('', order.id):
            fail('Unique subscription credential required')
        credential = rows[0]
        route = db.get(Route, credential.route_id)
        key = 'market:apisix:credential:' + credential.apisix_consumer_name
        client = ns['apisix_policy_redis']()
        try:
            expected = safe_policy(client.hgetall(key))
            if expected['quota_scope'] != combo.id:
                fail('Refund quota scope changed')
            run_id = client.info('server')['run_id']
            cursor_model = ns.get('api_charge_ledger', {}).get('Cursor')
            cursor = db.get(cursor_model, 'market:apisix:charges:v1') if cursor_model is not None else None
            if (not cursor or cursor.redis_run_id != run_id or client.hget(EPOCH, 'run_id') != run_id
                    or cursor.activated_at > aware(economic['as_of'])):
                fail('Refund charge source is not continuous')
            periods = strict_json(expected['subscription_periods'])
            selected = [p for p in periods if p.get('order_id') == order.id and p.get('segment_id') == term.id]
            if not selected or any(p['route_key'] != route.route_key for p in selected):
                fail('Refund periods are not attributed to this order and route')
            for p in periods:
                if ((p.get('order_id') == order.id) != (p.get('segment_id') == term.id)):
                    fail('Ambiguous refund period ownership')
            changes, checks = {}, []
            rounding = {}
            accounting = []
            observed_used = 0
            delta = economic['recovery_delta']
            now = ns['now']().timestamp()
            if economic['basis'] == 'calls':
                if economic.get('recovery_unit') != 'calls' or not economic.get('charge_ledger', {}).get('complete'):
                    fail('Finite refund requires frozen complete charge evidence')
                # Recognize unused expired rights before reducing live grants.
                # Their counters and zero-baseline coverage remain mandatory.
                for p in sorted(reversed(selected), key=lambda p: p['end_at'] > now):
                    if p['monthly_quota'] <= 0:
                        fail('Finite recovery cannot use unlimited bucket')
                    counter = 'market:apisix:quota:subscription:month:' + token(combo.id) + ':' + token(p['period_key'])
                    raw = client.get(counter)
                    if raw is not None and (not raw.isdigit() or str(int(raw)) != raw):
                        fail('Invalid refund usage counter')
                    used = int(raw or '0')
                    if used > p['monthly_quota']:
                        fail('Refund usage exceeds owned bucket quota')
                    observed_used += used
                    take = min(delta, max(0, p['monthly_quota'] - used))
                    limit = p['monthly_quota'] - take
                    delta -= take
                    changes[p['period_key']] = None if limit == 0 else dict(p, monthly_quota=limit)
                    expired = p['end_at'] <= now
                    checks.append(dict(period_key=p['period_key'], limit=limit, future=p['start_at'] > now,
                        cutoff=0, minimum_used=used, **({'expired_end': p['end_at']} if expired else {})))
                    accounting.append(dict(period_key=p['period_key'], quota_before=p['monthly_quota'],
                        observed_used=used, recovered_calls=take, quota_after=limit,
                        recognition='expired_unused_rights' if expired else 'live_quota_reduction'))
                if delta:
                    fail('Usage consumed the frozen refundable calls')
                if observed_used < economic.get('charged_calls', 0):
                    fail('Refund usage counters lost known charges')
            else:
                if economic.get('recovery_unit') != 'microseconds' or any(p['monthly_quota'] != 0 for p in selected):
                    fail('Time recovery requires unlimited API buckets')
                exact_cutoff = aware(economic['effective_ends_at'])
                epoch_us = (exact_cutoff - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(microseconds=1)
                cutoff = min((epoch_us + 999999) // 1000000, max(p['end_at'] for p in selected))
                rounding = dict(effective_ends_at=exact_cutoff.isoformat(),
                    gateway_effective_ends_at=(datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=cutoff)).isoformat(),
                    rounding_policy='ceil_to_second_clamped_to_native_period_end')
                if cutoff < now:
                    fail('Refunded service tail has already elapsed')
                for p in selected:
                    changes[p['period_key']] = (None if p['start_at'] >= cutoff else dict(p, end_at=min(p['end_at'], int(cutoff))))
                    checks.append(dict(period_key=p['period_key'], limit=False, future=p['start_at'] > now, cutoff=int(cutoff)))
            target_periods = [changes.get(p['period_key'], p) for p in periods]
            target_periods = [p for p in target_periods if p is not None]
            target = dict(expected, subscription_periods=json.dumps(target_periods, separators=(',', ':')))
            return dict(kind='api', credential_id=credential.id, consumer=credential.apisix_consumer_name,
                route_id=route.id, key=key, expected=expected, target=target, checks=checks,
                run_id=run_id, basis=economic['basis'], target_hash=digest(target),
                call_recovery_accounting=accounting, **rounding)
        finally:
            client.close()

    def saas_identity(status, plan, expiry):
        return (isinstance(status, dict) and type(status.get('ret')) is int and status.get('ret') == 0
            and all(status.get(k) == plan[k] for k in ('tenant_id', 'product_id', 'enterprise_id', 'subscription_id'))
            and status.get('refund_shorten_protocol') == 'refund_shorten_v1'
            and status.get('refund_shorten_supported') is True
            and aware(status['expires_at']) == aware(expiry))

    def config_hash(config):
        return digest({k: getattr(config, k, None) for k in ('id', 'product_id', 'base_url',
            'operation_path', 'token_url', 'client_id', 'scope', 'auth_mode', 'timeout_ms', 'status')})

    def saas_request(plan, refund_id):
        return {key: plan[key] for key in ('tenant_id', 'product_id', 'enterprise_id', 'subscription_id')} | dict(
            refund_id=refund_id, action='refund_shorten', protocol='refund_shorten_v1')

    def saas_later_completed(db, term, combo):
        # Shared tenant expiry cannot represent a hole before an unrelated renewal.
        Term = ns['subscription_terms']['Term']
        later = db.scalars(select(Term).where(Term.combination_id == combo.id,
            Term.generation == combo.generation, Term.status == 'active', Term.id != term.id,
            Term.ends_at > term.starts_at)).all()
        for later_term in later:
            reason = '请先完成后续续费订单的退款，再对当前订单退款'
            later_order = db.get(ns['Order'], later_term.order_id)
            recovery = db.scalar(select(Recovery).where(Recovery.term_id == later_term.id)
                .order_by(Recovery.recovery_number.desc()).limit(1))
            Execution, Refund = ns['refund_execution']['Execution'], ns['Refund']
            execution = db.get(Execution, recovery.execution_id) if recovery else None
            refunded = db.get(Refund, recovery.refund_id) if recovery else None
            completion = db.get(ns['refund_execution']['Completion'], recovery.refund_id) if recovery else None
            open_refund = db.scalar(select(Refund.id).where(Refund.order_id == later_term.order_id,
                Refund.status.notin_(('completed', 'rejected', 'cancelled'))).limit(1))
            open_execution = db.scalar(select(Execution.id).join(Refund, Refund.id == Execution.refund_id).where(
                Refund.order_id == later_term.order_id, Execution.status != 'completed').limit(1))
            if (not later_order or not recovery or not execution or execution.status != 'completed'
                    or not refunded or refunded.status != 'completed' or not completion
                    or open_refund or open_execution):
                fail(reason)
            try:
                verified = entitlement(db, later_order, later_term)
            except (HTTPException, ValueError, KeyError, TypeError, AttributeError):
                fail(reason)
            if not verified or verified['periods'] != [] or verified['payment_compatible'] is not True:
                fail(reason)

    def saas_plan(db, order, refund, economic, term, combo):
        if economic['basis'] != 'time':
            fail('SaaS requires time-based recovery')
        saas_later_completed(db, term, combo)
        Model = ns['SaaSSubscription']
        subscriptions = db.scalars(select(Model).where(Model.enterprise_id == combo.enterprise_id,
            Model.product_id == combo.product_id, Model.status == 'active')).all()
        Config = ns['SaaSIntegrationConfig']
        configs = db.scalars(select(Config).where(Config.product_id == combo.product_id, Config.status == 'active')).all()
        if len(subscriptions) != 1 or len(configs) != 1:
            fail('SaaS subscription or configuration is ambiguous')
        sub, config = subscriptions[0], configs[0]
        stored_expiry = sub.expires_at
        if stored_expiry.tzinfo is None and db.bind.dialect.name == 'sqlite':
            stored_expiry = stored_expiry.replace(tzinfo=timezone.utc)
        cutoff = aware(economic['effective_ends_at'])
        prior = entitlement(db, order, term)
        expected_expiry = (max(aware(row['ends_at']) for row in prior['periods']) if prior and prior['periods'] else term.ends_at)
        if (cutoff < ns['now']() or cutoff > expected_expiry
                or (prior is None and stored_expiry != term.ends_at)
                or (prior is not None and stored_expiry not in (term.ends_at, expected_expiry))):
            fail('SaaS expiry differs from owned frozen tail')
        plan = dict(kind='saas', config_id=config.id, subscription_id=sub.id, tenant_id=sub.external_tenant_id,
            product_id=combo.product_id, enterprise_id=combo.enterprise_id, expected_expires_at=expected_expiry.isoformat(),
            expires_at=cutoff.isoformat(), protocol='refund_shorten_v1', config_fingerprint=config_hash(config))
        status = ns['saas_call'](config, 'STATUS', saas_request(plan, refund.id))
        if not saas_identity(status, plan, plan['expected_expires_at']):
            fail('SaaS provider does not support verified idempotent refund shortening')
        return plan

    def prepare(db, order, refund, frozen_envelope):
        try:
            economic = approved(db, order, refund, frozen_envelope)
            term, combo = context(db, order, economic)
            if combo.delivery_method in ('api', 'model_api'):
                plan = api_plan(db, order, refund, economic, term, combo)
            elif combo.delivery_method == 'tenant_access':
                plan = saas_plan(db, order, refund, economic, term, combo)
            else:
                fail('Automatic resource plan unavailable for manual/file/offline refund')
            plan.update(schema='refund_execution_resources_v1', refund_id=refund.id, order_id=order.id,
                term_id=term.id, combination_id=combo.id, generation=combo.generation,
                frozen_envelope_hash=digest(frozen_envelope),
                case_plan_hash=db.get(ns['refund_workflow']['Case'], refund.id).plan_hash)
            if plan['kind'] == 'api':
                plan['expires_at'] = term.ends_at.isoformat()
            if plan['kind'] == 'api':
                plan['entitlement_periods'] = [p for p in strict_json(plan['target']['subscription_periods'])
                    if p.get('order_id') == order.id and p.get('segment_id') == term.id]
            else:
                from .subscription_calendar import CalendarAnchor
                anchor = CalendarAnchor(combo.anchor_at)
                cutoff = aware(plan['expires_at'])
                quota = economic['delivery_snapshot']['quota']
                plan['entitlement_periods'] = []
                for i in range(term.start_month, term.start_month + term.months):
                    bounds = anchor.period(i)
                    if bounds.start < cutoff:
                        end = min(bounds.end, cutoff)
                        plan['entitlement_periods'].append(dict(start_at=int(bounds.start.timestamp()),
                            end_at=int(end.timestamp()), starts_at=bounds.start.isoformat(), ends_at=end.isoformat(),
                            period_key=f'{combo.id}:{combo.generation}:{i}', order_id=order.id, segment_id=term.id,
                            **{k: quota[k] for k in ('rate_limit_per_minute', 'daily_quota', 'monthly_quota')}))
            plan['plan_hash'] = digest(plan)
            return plan
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HTTPException(409, 'Refund resource plan evidence is unavailable') from exc

    def apply(db, execution):
        try:
            plan = strict_json(execution.resource_plan_json)
            unsigned = {k: v for k, v in plan.items() if k != 'plan_hash'}
            if plan['refund_id'] != execution.refund_id or digest(unsigned) != plan['plan_hash']:
                fail('Refund resource plan hash mismatch')
            Execution = ns['refund_execution']['Execution']
            persisted = db.get(Execution, execution.id)
            persisted_phase = persisted.resume_status if persisted and persisted.status == 'failed' else getattr(persisted, 'status', None)
            if (not persisted or persisted.refund_id != execution.refund_id
                    or persisted.resource_plan_json != execution.resource_plan_json
                    or persisted.resource_plan_hash != digest(plan) or persisted.plan_hash != plan['case_plan_hash']
                    or persisted_phase not in ('resources_pending', 'resources_applied', 'funds_pending', 'completed')):
                fail('Persisted refund execution identity is missing')
            refund = db.get(ns['Refund'], execution.refund_id)
            order = db.get(ns['Order'], plan['order_id'])
            case = db.get(ns['refund_workflow']['Case'], refund.id)
            if case.plan_hash != plan['case_plan_hash']:
                fail('Frozen refund case changed')
            envelope = strict_json(case.plan_json)
            economic = approved(db, order, refund, envelope)
            existing = db.scalar(select(Recovery).where(Recovery.refund_id == refund.id))
            if existing:
                proof = strict_json(existing.proof_json)
                if (existing.execution_id != execution.id or existing.stage != 'resources_applied'
                        or existing.order_id != order.id or existing.term_id != plan['term_id']
                        or existing.combination_id != plan['combination_id'] or existing.generation != plan['generation']
                        or existing.plan_json != canonical(plan) or proof.get('refund_id') != refund.id
                        or proof.get('resource_plan_hash') != digest(plan)
                        or existing.payload_hash != digest(dict(plan=plan, proof=proof))
                        or digest(envelope) != plan['frozen_envelope_hash']
                        or (persisted.proof_json is not None and strict_json(persisted.proof_json) != proof)):
                    fail('Persisted external recovery evidence is conflicting')
                return proof
            term, combo = context(db, order, economic)
            if (digest(envelope) != plan['frozen_envelope_hash'] or term.id != plan['term_id']
                    or combo.id != plan['combination_id'] or combo.generation != plan['generation']):
                fail('Refund execution ownership changed')
            if plan['kind'] == 'api':
                credential = db.get(ns['ApiCredential'], plan['credential_id'])
                if (not credential or credential.status != 'active' or credential.route_id != plan['route_id']
                        or credential.enterprise_id != order.buyer_enterprise_id
                        or credential.apisix_consumer_name != plan['consumer']):
                    fail('Refund credential changed')
                client = ns['apisix_policy_redis']()
                try:
                    if client.info('server')['run_id'] != plan['run_id']:
                        fail('Refund recovery Redis source restarted')
                    keys = [plan['key'], PREFIX + refund.id, EPOCH]
                    for check in plan['checks']:
                        suffix = token(combo.id) + ':' + token(check['period_key'])
                        keys.extend(['market:apisix:quota:subscription:month:' + suffix,
                                     'market:apisix:charges:coverage:' + suffix])
                    payload = dict(plan, fields=sorted(plan['expected']))
                    raw = client.eval(SCRIPT, len(keys), *keys, canonical(payload))
                    receipt = strict_json(raw)
                    actual = client.hgetall(plan['key'])
                    scoped = lambda policy: [p for p in strict_json(policy['subscription_periods'])
                        if p.get('order_id') == order.id and p.get('segment_id') == term.id]
                    if (receipt.get('plan_hash') != plan['plan_hash'] or receipt.get('refund_id') != refund.id
                            or receipt.get('target_hash') != plan['target_hash']
                            or any(actual.get(k) != v for k, v in plan['target'].items() if k != 'subscription_periods')
                            or scoped(actual) != scoped(plan['target'])):
                        fail('Refund resource recovery readback failed')
                finally:
                    client.close()
            elif plan['kind'] == 'saas':
                saas_later_completed(db, term, combo)
                config = db.get(ns['SaaSIntegrationConfig'], plan['config_id'])
                if not config or config_hash(config) != plan['config_fingerprint']:
                    fail('Frozen SaaS shortening configuration changed')
                status = ns['saas_call'](config, 'STATUS', saas_request(plan, refund.id))
                if not (saas_identity(status, plan, plan['expires_at']) and status.get('refund_id') == refund.id):
                    if not saas_identity(status, plan, plan['expected_expires_at']):
                        fail('SaaS expiry changed before refund shortening')
                    if ns['now']() > aware(plan['expires_at']):
                        fail('SaaS refunded tail already elapsed')
                    result = ns['saas_call'](config, 'RENEW', dict(saas_request(plan, refund.id),
                        expected_expires_at=plan['expected_expires_at'], expires_at=plan['expires_at'],
                        idempotency_key='refund-shorten:' + refund.id))
                    if (not isinstance(result, dict) or type(result.get('ret')) is not int or result.get('ret') != 0
                            or result.get('refund_id') != refund.id or aware(result['expires_at']) != aware(plan['expires_at'])):
                        fail('SaaS shortening response is not bound to this refund')
                    status = ns['saas_call'](config, 'STATUS', saas_request(plan, refund.id))
                if not saas_identity(status, plan, plan['expires_at']) or status.get('refund_id') != refund.id:
                    fail('SaaS shortening readback failed')
                subscription = db.get(ns['SaaSSubscription'], plan['subscription_id'])
                if (not subscription or subscription.enterprise_id != order.buyer_enterprise_id
                        or subscription.product_id != order.product_id or subscription.external_tenant_id != plan['tenant_id']):
                    fail('SaaS subscription ownership changed')
                subscription.expires_at = aware(plan['expires_at'])
                receipt = dict(refund_id=refund.id, plan_hash=plan['plan_hash'], expires_at=plan['expires_at'])
            else:
                fail('Automatic refund resource adapter unavailable')
            proof = dict(refund_id=refund.id, resource_plan_hash=digest(plan), evidence_fingerprint=digest(receipt))
            encoded, proof_json = canonical(plan), canonical(proof)
            fingerprint = digest(dict(plan=plan, proof=proof))
            existing = db.scalar(select(Recovery).where(Recovery.refund_id == refund.id))
            if existing:
                if existing.plan_json != encoded or existing.proof_json != proof_json or existing.payload_hash != fingerprint:
                    fail('Refund resource recovery replay conflict')
            else:
                previous = db.scalar(select(Recovery.recovery_number).where(Recovery.term_id == term.id)
                    .order_by(Recovery.recovery_number.desc()).limit(1)) or 0
                db.add(Recovery(refund_id=refund.id, execution_id=execution.id, order_id=order.id, term_id=term.id,
                    combination_id=combo.id, generation=combo.generation, recovery_number=previous + 1, plan_json=encoded,
                    proof_json=proof_json, payload_hash=fingerprint))
                db.flush()
            return proof
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HTTPException(409, 'Refund execution resource evidence is unavailable') from exc

    def entitlement(db, order, term):
        rows = db.scalars(select(Recovery).where(Recovery.term_id == term.id)
            .order_by(Recovery.recovery_number)).all()
        Execution, Refund = ns['refund_execution']['Execution'], ns['Refund']
        pending = db.scalars(select(Execution).join(Refund, Refund.id == Execution.refund_id).where(
            Refund.order_id == order.id, Execution.status.in_(('resources_pending', 'failed')))).all()
        recovered = {row.execution_id for row in rows}
        for execution in pending:
            phase = execution.resume_status if execution.status == 'failed' else execution.status
            if phase == 'resources_pending' and execution.id not in recovered:
                # An external effect may have committed before the DB receipt.
                # Deny this term until replay reconciles it; never rebuild its old policy.
                return dict(periods=[], payment_compatible=True)
        if not rows:
            return None
        known = set()
        periods = None
        for row in rows:
            plan, proof = strict_json(row.plan_json), strict_json(row.proof_json)
            refund = db.get(ns['Refund'], row.refund_id)
            case = db.get(ns['refund_workflow']['Case'], row.refund_id)
            execution = db.get(ns['refund_execution']['Execution'], row.execution_id)
            phase = execution.resume_status if execution and execution.status == 'failed' else getattr(execution, 'status', None)
            if (row.order_id != order.id or row.combination_id != term.combination_id or row.generation != term.generation
                    or not refund or refund.order_id != order.id or not case
                    or plan['refund_id'] != row.refund_id or proof['refund_id'] != row.refund_id
                    or not execution or execution.refund_id != row.refund_id
                    or row.stage != 'resources_applied'
                    or phase not in ('resources_pending', 'resources_applied', 'funds_pending', 'completed')
                    or (execution.proof_json is not None and strict_json(execution.proof_json) != proof)
                    or execution.resource_plan_hash != digest(plan) or execution.plan_hash != case.plan_hash
                    or execution.resource_plan_json != row.plan_json
                    or proof['resource_plan_hash'] != digest(plan)
                    or digest({k: v for k, v in plan.items() if k != 'plan_hash'}) != plan['plan_hash']
                    or digest(strict_json(case.plan_json)) != plan['frozen_envelope_hash']
                    or digest(dict(plan=plan, proof=proof)) != row.payload_hash):
                fail('Unverified refund entitlement recovery')
            known.add(row.refund_id)
            periods = plan['entitlement_periods']
        completed = db.scalars(select(ns['Refund']).where(ns['Refund'].order_id == order.id,
            ns['Refund'].status == 'completed')).all()
        from decimal import Decimal
        compatible = (all(r.id in known for r in completed)
            and sum((Decimal(str(r.amount)) for r in completed), Decimal(0)) == Decimal(str(order.refunded_amount)))
        for refund in completed:
            case = db.get(ns['refund_workflow']['Case'], refund.id)
            completion = db.get(ns['refund_execution']['Completion'], refund.id)
            recovery = next((r for r in rows if r.refund_id == refund.id), None)
            execution = db.get(ns['refund_execution']['Execution'], recovery.execution_id) if recovery else None
            Leg = ns['refund_execution']['PaymentLeg']
            legs = db.scalars(select(Leg).where(Leg.refund_id == refund.id)).all()
            funding = strict_json(execution.funding_json) if execution else []
            by_payment = {row['payment_id']: row for row in funding} if isinstance(funding, list) else {}
            if (not completion or not execution or execution.status != 'completed'
                    or completion.execution_id != execution.id or completion.plan_hash != execution.plan_hash
                    or completion.proof_hash != digest(strict_json(execution.proof_json))
                    or completion.funding_hash != digest(strict_json(execution.funding_json))
                    or strict_json(completion.economic_plan_json) != strict_json(case.plan_json)['economic_plan']
                    or Decimal(str(completion.amount)) != Decimal(str(refund.amount))
                    or not legs or len(funding) != len(legs)
                    or any(leg.status != 'completed' or leg.receipt != 'simulated:' + refund.id + ':' + leg.payment_id for leg in legs)
                    or sum((Decimal(str(leg.amount)) for leg in legs), Decimal(0)) != Decimal(str(refund.amount))):
                compatible = False
            for leg in legs:
                allocation = by_payment.get(leg.payment_id)
                payment = db.get(ns['Payment'], leg.payment_id)
                source = db.get(ns['Order'], leg.source_order_id)
                if (not allocation or not payment or not source
                        or payment.order_id != leg.source_order_id
                        or allocation['source_order_id'] != leg.source_order_id
                        or Decimal(str(allocation['amount'])) != Decimal(str(leg.amount))
                        or allocation['currency'] != leg.currency
                        or leg.currency != strict_json(case.plan_json)['economic_plan']['economic_snapshot']['currency']
                        or Decimal(str(payment.amount)) < Decimal(str(leg.amount))
                        or payment.status not in ('paid', 'refunding', 'refunded')
                        or source.payment_status not in ('paid', 'refunding', 'refunded')
                        or Decimal(str(source.paid_amount)) != Decimal(str(payment.amount))
                        or source.provider_enterprise_id != order.provider_enterprise_id
                        or source.buyer_enterprise_id != order.buyer_enterprise_id or source.product_id != order.product_id):
                    compatible = False
        return dict(periods=periods, payment_compatible=compatible)

    hooks = dict(prepare=prepare, apply=apply, Recovery=Recovery, entitlement=entitlement)
    ns['refund_execution_resources'] = hooks
    return hooks
