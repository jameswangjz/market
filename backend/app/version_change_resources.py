"""Verified external resource changes for the version-change Saga."""
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from .subscription_calendar import CalendarAnchor
from .refund_segment_boundary import freeze_policy_switch
from .refund_segment_store import boundary_periods, saas_switch_evidence


def _quota_period_plan(combination, terms, snapshots, old_route_key, target_route_key, at):
    anchor = CalendarAnchor(combination.anchor_at)
    instant = at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)
    result = {'original': [], 'target': []}
    seen_period_keys = set()
    for term, original_delivery in terms:
        order_id, segment_id = getattr(term, 'order_id', None), getattr(term, 'id', None)
        if any(not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,36}', value) is None
                for value in (order_id, segment_id)):
            raise ValueError('Subscription term charge attribution is missing or invalid')
        bounds = anchor.term(term.months, start_month=term.start_month)
        if term.starts_at != bounds.start or term.ends_at != bounds.end:
            raise ValueError('Subscription term differs from its frozen calendar')
        snapshot = snapshots.get(term.id)
        if term.ends_at > instant and (not isinstance(snapshot, dict)
                or not isinstance(snapshot.get('delivery'), dict)):
            raise ValueError('Target subscription quota snapshot is incomplete')
        target_delivery = snapshot['delivery'] if snapshot else original_delivery
        for index in range(term.start_month, min(term.start_month + term.months, combination.total_months)):
            period_key = f'{combination.id}:{combination.generation}:{index}'
            if period_key in seen_period_keys:
                raise ValueError('Subscription quota period has duplicate charge attribution')
            seen_period_keys.add(period_key)
            period = anchor.period(index)
            active = period.end > instant
            for side, delivery, route_key in (
                    ('original', original_delivery, old_route_key),
                    ('target', target_delivery if active else original_delivery,
                     target_route_key if active else old_route_key)):
                quota = delivery.get('quota', {})
                row = {'start_at': int(period.start.timestamp()), 'end_at': int(period.end.timestamp()),
                    'period_key': period_key,
                    'route_key': route_key, 'order_id': order_id, 'segment_id': segment_id}
                for field in ('rate_limit_per_minute', 'daily_quota', 'monthly_quota'):
                    row[field] = int(quota[field])
                result[side].append(row)
    return result


def _gateway_snapshot_matches(delivery, config, route, version):
    if not isinstance(delivery, dict):
        return False
    gateway = delivery.get('gateway')
    actual = {key: getattr(config, key, None) for key in (
        'upstream_url', 'route_key', 'auth_mode', 'upstream_auth_mode', 'upstream_scope',
        'timeout_ms', 'strip_prefix', 'health_method', 'health_path')}
    actual.update(route_key=route.route_key, version=version.version_code,
        rate_limit_per_minute=version.rate_limit_per_minute,
        daily_quota=version.daily_quota, monthly_quota=version.monthly_quota)
    if gateway is not None:
        if not isinstance(gateway, dict) or any(key not in actual or actual[key] != value
                for key, value in gateway.items()):
            return False
    quota = delivery.get('quota')
    if isinstance(quota, dict):
        for key in ('rate_limit_per_minute', 'daily_quota', 'monthly_quota'):
            if key in quota and actual[key] != quota[key]:
                return False
    return True


def _integration_snapshot_matches(snapshot, config, route, quote_fingerprint):
    integration = snapshot.get('integration') if isinstance(snapshot, dict) else None
    expected = {'config_id': config.id, 'revision_id': config.revision_id,
        'evidence_hash': config.evidence_hash, 'route_id': route.id,
        'route_key': route.route_key, 'quote_fingerprint': quote_fingerprint}
    return isinstance(integration, dict) and all(integration.get(key) == value
        for key, value in expected.items())


def install(ns):
    if 'version_change_resources' in ns:
        return ns['version_change_resources']

    def fail(message='Version change resources could not be verified'):
        raise HTTPException(409, message)

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                         ensure_ascii=True).encode()).hexdigest()

    def datetime_identity(value):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def safe_saas_config(config):
        return {key: getattr(config, key, None) for key in (
            'id', 'base_url', 'operation_path', 'token_url', 'client_id', 'scope',
            'auth_mode', 'timeout_ms', 'status')}

    def unwrap(value):
        while isinstance(value, dict) and isinstance(value.get('node'), dict):
            value = value['node']
        while isinstance(value, dict) and isinstance(value.get('value'), dict):
            value = value['value']
        return value

    def authored_route_matches(actual, expected):
        if not isinstance(actual, dict) or not isinstance(expected, dict):
            return False
        if (actual.get('status') in (False, 0, '0', 'disabled', 'Disabled', 'inactive')
                or actual.get('enabled') is False or actual.get('enable') is False):
            return False
        server_fields = {'id', 'key', 'create_time', 'update_time', 'modifiedIndex', 'status'}
        authored = {k: v for k, v in actual.items() if k not in server_fields}
        if isinstance(authored.get('methods'), list) and isinstance(expected.get('methods'), list):
            if set(authored['methods']) != set(expected['methods']):
                return False
            authored['methods'] = expected['methods']
        return authored == expected

    def route_context(db, change, version_id, version_code, frozen=None):
        Version = ns['ProductReleaseVersion']
        Product = ns['Product']
        Config = ns['version_gateway']['Config']
        version = db.get(Version, version_id)
        combo = db.get(ns['subscription_terms']['Combination'], change.combination_id)
        product = db.get(Product, combo.product_id) if combo else None
        config = db.scalar(select(Config).where(Config.version_id == version_id))
        route = db.get(ns['ApiGatewayRoute'], config.route_id) if config and config.route_id else None
        if (not version or not product or version.product_id != product.id
                or version.version_code != version_code or version.status != 'active'
                or not config or not route or route.product_id != product.id
                or route.version != version_code or route.status != 'active'
                or not ns['version_gateway']['version_ready'](db, product, version)):
            fail('API target version has no verified frozen gateway configuration')
        if frozen and (config.id != frozen['config_id'] or config.revision_id != frozen['revision_id']
                or config.evidence_hash != frozen['evidence_hash']
                or route.validated_ip != frozen['validated_ip']):
            fail('Frozen API gateway revision or upstream address changed')
        result = ns['validated_http_request'](config.health_method,
            config.upstream_url.rstrip('/') + config.health_path,
            timeout=min(config.timeout_ms / 1000, 10.0), follow_redirects=False)
        if not 200 <= result.status_code < 300:
            fail('API target health check failed')
        extensions = getattr(result, 'extensions', {})
        addresses = extensions.get('validated_upstream_addresses') if isinstance(extensions, dict) else None
        if not addresses and isinstance(extensions, dict) and extensions.get('validated_upstream_ip'):
            addresses = [extensions['validated_upstream_ip']]
        if (not route.validated_ip or not isinstance(addresses, (list, tuple))
                or route.validated_ip not in addresses):
            fail('Validated API upstream address differs from frozen gateway evidence')
        payload = ns['apisix_route_payload'](route, product, db)
        live = unwrap(ns['apisix_admin_request']('GET', f'/routes/{route.route_key}'))
        if not authored_route_matches(live, payload):
            fail('APISIX route does not match frozen version configuration')
        return version, product, config, route, payload

    def prepare(db, change):
        plan = {'kind': '', 'change_id': change.id}
        combo = db.get(ns['subscription_terms']['Combination'], change.combination_id)
        if not combo or combo.enterprise_id != change.enterprise_id:
            fail('Version change subscription combination is invalid')
        delivery = combo.delivery_method
        if delivery in {'api', 'model_api'}:
            old = route_context(db, change, change.old_version_id, change.old_version_code)
            target = route_context(db, change, change.target_version_id, change.target_version_code)
            if old[3].id == target[3].id or old[3].route_key == target[3].route_key:
                fail('Old and target API routes must be distinct')
            Credentials = ns['ApiCredential']
            target_credentials = db.scalars(select(Credentials).where(
                Credentials.enterprise_id == change.enterprise_id,
                Credentials.route_id == target[3].id)
                .order_by(Credentials.id).with_for_update()).all()
            if target_credentials:
                fail('Enterprise already has credentials on the target API route')
            credentials = db.scalars(select(Credentials).where(
                Credentials.enterprise_id == change.enterprise_id,
                Credentials.route_id == old[3].id, Credentials.order_id == '')
                .order_by(Credentials.id).with_for_update()).all()
            if len([c for c in credentials if c.status == 'active']) > 1:
                fail('Multiple active API credentials require manual resolution')
            plan.update(kind='api', old_route_id=old[3].id, target_route_id=target[3].id,
                old_route_key=old[3].route_key, target_route_key=target[3].route_key,
                old_config_id=old[2].id, target_config_id=target[2].id,
                old_config_revision_id=old[2].revision_id,
                target_config_revision_id=target[2].revision_id,
                old_config_evidence_hash=old[2].evidence_hash,
                target_config_evidence_hash=target[2].evidence_hash,
                old_validated_ip=old[3].validated_ip,
                target_validated_ip=target[3].validated_ip,
                credentials=[{'id': c.id, 'consumer': c.apisix_consumer_name,
                    'key_hash': c.key_hash, 'key_prefix': c.key_prefix, 'status': c.status,
                    'old_route_id': c.route_id, 'rate_limit_per_minute': c.rate_limit_per_minute,
                    'daily_quota': c.daily_quota, 'monthly_quota': c.monthly_quota,
                    'total_quota': c.total_quota} for c in credentials])
            if len({c['consumer'] for c in plan['credentials'] if c['consumer']}) != len(
                    [c for c in plan['credentials'] if c['consumer']]):
                fail('API consumer identity collision')
            snapshots = json.loads(change.target_snapshots_json or '{}')
            try:
                quote_fingerprint = json.loads(change.quote_json or '{}')['quote_id']
            except (TypeError, ValueError, KeyError):
                fail('Version change quote fingerprint is unavailable')
            if not isinstance(snapshots, dict) or not snapshots:
                fail('Frozen target version-change snapshots are unavailable')
            for snapshot in snapshots.values():
                if not _integration_snapshot_matches(snapshot, target[2], target[3], quote_fingerprint):
                    fail('Target API gateway differs from the accepted quote integration snapshot')
            terms_model, order_model = ns['subscription_terms']['Term'], ns['Order']
            term_rows = db.execute(select(terms_model, order_model).join(
                order_model, terms_model.order_id == order_model.id).where(
                terms_model.combination_id == combo.id, terms_model.generation == combo.generation,
                terms_model.status == 'active').order_by(terms_model.start_month)).all()
            original_terms = [(term, ns['subscription_terms']['effective_snapshot'](
                db, order, term)['delivery']) for term, order in term_rows]
            for _term, original_delivery in original_terms:
                if not _gateway_snapshot_matches(original_delivery, old[2], old[3], old[0]):
                    fail('Original API gateway differs from the frozen order delivery snapshot')
            for snapshot in snapshots.values():
                if (not isinstance(snapshot, dict) or not _gateway_snapshot_matches(
                        snapshot.get('delivery'), target[2], target[3], target[0])):
                    fail('Target API gateway differs from the frozen version-change snapshot')
            try:
                plan['periods'] = _quota_period_plan(combo, original_terms, snapshots,
                    old[3].route_key, target[3].route_key, change.created_at)
            except (TypeError, ValueError) as exc:
                fail(str(exc))
            if ns.get('refund_segment_store'):
                client = ns['apisix_policy_redis']()
                try:
                    plan['charge_boundary'] = ns['refund_segment_store']['descriptor'](db, change, plan, client)
                finally:
                    client.close()
        elif delivery == 'tenant_access':
            subscription = db.scalar(select(ns['SaaSSubscription']).where(
                ns['SaaSSubscription'].enterprise_id == change.enterprise_id,
                ns['SaaSSubscription'].product_id == combo.product_id,
                ns['SaaSSubscription'].version_id == change.old_version_id,
                ns['SaaSSubscription'].status == 'active').with_for_update())
            rows = db.scalars(select(ns['SaaSSubscription']).where(
                ns['SaaSSubscription'].enterprise_id == change.enterprise_id,
                ns['SaaSSubscription'].product_id == combo.product_id,
                ns['SaaSSubscription'].status == 'active')).all()
            if not subscription or len(rows) != 1 or not subscription.external_tenant_id:
                fail('SaaS subscription binding is ambiguous or inactive')
            config = db.scalar(select(ns['SaaSIntegrationConfig']).where(
                ns['SaaSIntegrationConfig'].product_id == combo.product_id,
                ns['SaaSIntegrationConfig'].status == 'active'))
            if not config or config.auth_mode != 'oauth2':
                fail('Active SaaS OAuth configuration is required')
            snapshots = json.loads(change.target_snapshots_json or '{}')
            try:
                quote_fingerprint = json.loads(change.quote_json or '{}')['quote_id']
            except (TypeError, ValueError, KeyError):
                fail('Version change quote fingerprint is unavailable')
            accepted_config_fingerprint = digest(safe_saas_config(config))
            if (not isinstance(snapshots, dict) or not snapshots
                    or any(not isinstance(snapshot, dict)
                        or not isinstance(snapshot.get('integration'), dict)
                        or snapshot['integration'].get('kind') != 'saas'
                        or snapshot['integration'].get('config_id') != config.id
                        or snapshot['integration'].get('config_fingerprint') != accepted_config_fingerprint
                        or snapshot['integration'].get('quote_fingerprint') != quote_fingerprint
                        for snapshot in snapshots.values())):
                fail('SaaS integration differs from the accepted quote snapshot')
            source_order = db.get(ns['Order'], change.source_order_id)
            if (not source_order or source_order.buyer_enterprise_id != change.enterprise_id
                    or source_order.product_id != combo.product_id or not source_order.buyer_user_id):
                fail('Original SaaS buyer identity is unavailable')
            active_mappings = db.scalars(select(ns['SaaSUserMapping']).where(
                ns['SaaSUserMapping'].subscription_id == subscription.id,
                ns['SaaSUserMapping'].status == 'active')).all()
            admin_id = source_order.buyer_user_id
            operations = db.scalars(select(ns['SaaSOperation']).where(
                ns['SaaSOperation'].subscription_id == subscription.id,
                ns['SaaSOperation'].operation.in_(['OPEN', 'USER_ASSIGN']),
                ns['SaaSOperation'].status == 'succeeded')
                .order_by(ns['SaaSOperation'].created_at)).all()
            evidenced_admins = set()
            for operation in operations:
                try:
                    payload = json.loads(operation.request_payload or '{}')
                except (TypeError, ValueError):
                    continue
                candidate = payload.get('admin_user_id')
                if candidate and any(row.user_id == candidate for row in active_mappings):
                    evidenced_admins.add(candidate)
            if not any(row.user_id == admin_id for row in active_mappings):
                if len(evidenced_admins) != 1:
                    fail('SaaS onboarding does not identify one mapped admin')
                admin_id = next(iter(evidenced_admins))
            matching_mappings = [row for row in active_mappings if row.user_id == admin_id]
            clients = db.scalars(select(ns['OIDCClient']).where(
                ns['OIDCClient'].product_id == combo.product_id,
                ns['OIDCClient'].status == 'active').order_by(ns['OIDCClient'].id)).all()
            if len(matching_mappings) != 1 or not matching_mappings[0].external_user_id or len(clients) != 1:
                fail('SaaS admin mapping or OIDC client is ambiguous')
            mapping = matching_mappings[0]
            plan.update(kind='saas', subscription_id=subscription.id,
                tenant_id=subscription.external_tenant_id, config_id=config.id,
                product_id=subscription.product_id, enterprise_id=subscription.enterprise_id,
                admin_user_id=admin_id, external_admin_id=mapping.external_user_id,
                oidc_client_id=clients[0].client_id,
                config_fingerprint=accepted_config_fingerprint,
                expires_at=datetime_identity(subscription.expires_at))
        else:
            fail('Unsupported subscription delivery method')
        encoded = json.dumps(plan, sort_keys=True, separators=(',', ':'))
        existing = change.resource_plan_json
        if existing:
            try:
                frozen_plan = json.loads(existing)
            except (TypeError, ValueError):
                fail('Frozen version change resource plan is invalid')
            if not isinstance(frozen_plan, dict) or frozen_plan and frozen_plan != plan:
                fail('Version change resource plan is already frozen')
        change.resource_plan_json = encoded
        return plan

    def api_apply(db, change, direction, plan, redis):
        target = direction == 'target'
        descriptor = plan.get('charge_boundary', {})
        strict = descriptor.get('mode') == 'strict'
        receipts = {}
        receipt_ids = []
        route_id = plan['target_route_id'] if target else plan['old_route_id']
        route_key = plan['target_route_key'] if target else plan['old_route_key']
        Route = ns['ApiGatewayRoute']
        route = db.get(Route, route_id)
        if not route or route.route_key != route_key or not ns['apisix_enabled']():
            fail('APISIX route identity is unavailable')
        version_id = change.target_version_id if target else change.old_version_id
        version_code = change.target_version_code if target else change.old_version_code
        frozen = {'config_id': plan['target_config_id'] if target else plan['old_config_id'],
            'revision_id': plan['target_config_revision_id'] if target else plan['old_config_revision_id'],
            'evidence_hash': plan['target_config_evidence_hash'] if target else plan['old_config_evidence_hash'],
            'validated_ip': plan['target_validated_ip'] if target else plan['old_validated_ip']}
        _version, _product, _config, verified_route, payload = route_context(
            db, change, version_id, version_code, frozen=frozen)
        if verified_route.id != route_id or verified_route.route_key != route_key:
            fail('Frozen API route identity changed')
        live = unwrap(ns['apisix_admin_request']('GET', f'/routes/{route_key}'))
        if not authored_route_matches(live, payload):
            fail('APISIX route changed after gateway verification')
        keys = [f"market:apisix:credential:{item['consumer']}" for item in plan['credentials']
                if item['consumer'] and item['status'] == 'active']
        policy_before = {key: redis.hgetall(key) for key in keys}
        period_identity_before = {}
        for key, policy in policy_before.items():
            try:
                period_identity_before[key] = [(row['period_key'], row['start_at'], row['end_at']) for row in
                    json.loads(policy['subscription_periods'])]
            except (KeyError, TypeError, ValueError):
                fail('Existing subscription quota periods are invalid')
            expected_identity = [(row['period_key'], row['start_at'], row['end_at']) for row in
                plan['periods']['target' if target else 'original']]
            if policy.get('quota_scope') != change.combination_id or period_identity_before[key] != expected_identity:
                fail('Subscription quota scope or period identity changed')
        for item in plan['credentials']:
            if item['status'] != 'active':
                continue
            credential = db.get(ns['ApiCredential'], item['id'])
            if (not credential or credential.status != 'active'
                    or credential.apisix_consumer_name != item['consumer']
                    or credential.key_hash != item['key_hash']):
                fail('API credential identity changed after resource plan')
            consumer = unwrap(ns['apisix_admin_request']('GET', f"/consumers/{item['consumer']}"))
            plugins = consumer.get('plugins', {}) if isinstance(consumer, dict) else {}
            keyauth = plugins.get('key-auth', {})
            if not isinstance(keyauth, dict):
                fail('APISIX key-auth plugin is missing')
            raw = keyauth.get('key') if isinstance(keyauth, dict) else None
            if (not isinstance(consumer, dict)
                    or consumer.get('username') != item['consumer']
                    or consumer.get('status') in (False, 0, '0', 'disabled', 'Disabled', 'inactive')
                    or consumer.get('enabled') is False or consumer.get('enable') is False
                    or keyauth.get('anonymous') or keyauth.get('disable') is True
                    or isinstance(keyauth.get('_meta'), dict) and keyauth['_meta'].get('disable') is True):
                fail('APISIX consumer is disabled or no longer matches its identity')
            if isinstance(raw, str) and raw and not hmac.compare_digest(
                    hashlib.sha256(raw.encode()).hexdigest(), item['key_hash']):
                fail('APISIX consumer key differs from the frozen credential identity')
            if not isinstance(raw, str) or not raw:
                try:
                    raw = ns['initial_api_credentials']['cipher']().decrypt(
                        credential.secret_encrypted.encode()).decode()
                except Exception:
                    fail('API credential key cannot be safely rehydrated')
                if not hmac.compare_digest(hashlib.sha256(raw.encode()).hexdigest(), item['key_hash']):
                    fail('Stored API credential does not match its key hash')
            plugin = plugins.get('market-gateway-quota', {})
            if not isinstance(plugin, dict):
                fail('API quota plugin is missing')
            if (plugin.get('disable') is True or plugin.get('disabled') is True
                    or isinstance(plugin.get('_meta'), dict) and plugin['_meta'].get('disable') is True):
                fail('APISIX quota plugin is deliberately disabled')
            periods = plan['periods']['target' if target else 'original']
            current_period = next((p for p in periods if p['start_at'] <= ns['now']().timestamp() < p['end_at']),
                                  None)
            if not current_period:
                fail('No current subscription quota period exists')
            new_plugin = dict(plugin, route_key=route_key,
                daily_quota=current_period['daily_quota'], monthly_quota=current_period['monthly_quota'],
                total_quota=0 if target else item['total_quota'])
            new_plugins = dict(plugins)
            new_plugins['key-auth'] = dict(keyauth, key=raw)
            new_plugins['market-gateway-quota'] = new_plugin
            ns['apisix_admin_request']('PUT', f"/consumers/{item['consumer']}", {
                'username': item['consumer'], 'plugins': new_plugins})
            credential.route_id = route_id
            for field in ('rate_limit_per_minute', 'daily_quota', 'monthly_quota'):
                setattr(credential, field, current_period[field])
            credential.total_quota = 0 if target else item['total_quota']
            prior = redis.hgetall('market:apisix:credential:' + item['consumer'])
            if prior.get('mode') != 'subscription':
                fail('Credential quota policy is not subscription-scoped')
            mapping = dict(prior, quota_scope=change.combination_id,
                subscription_periods=json.dumps(periods, separators=(',', ':')),
                status=credential.status, daily_quota=credential.daily_quota or 0,
                monthly_quota=credential.monthly_quota or 0,
                total_quota=credential.total_quota or 0)
            if strict:
                if not ns.get('refund_segment_store') or item['id'] != descriptor['credential_id']:
                    fail('Strict charge boundary store is unavailable')
                receipts[item['id']] = freeze_policy_switch(redis,
                    credential_key='market:apisix:credential:' + item['consumer'],
                    change_id=change.id, direction=direction,
                    expected_policy=descriptor['original' if target else 'target'],
                    target_policy=descriptor[direction], periods=boundary_periods(descriptor[direction]),
                    source_context=ns['api_charge_ledger']['source_context'](db, redis))
            else:
                redis.hset('market:apisix:credential:' + item['consumer'], mapping=mapping)
        final_route = unwrap(ns['apisix_admin_request']('GET', f'/routes/{route_key}'))
        if not authored_route_matches(final_route, payload):
            fail('APISIX route readback failed')
        for item in plan['credentials']:
            if item['status'] != 'active':
                continue
            credential = db.get(ns['ApiCredential'], item['id'])
            consumer = unwrap(ns['apisix_admin_request']('GET', f"/consumers/{item['consumer']}"))
            plugins = consumer.get('plugins', {}) if isinstance(consumer, dict) else {}
            keyauth = plugins.get('key-auth', {})
            quota_plugin = plugins.get('market-gateway-quota', {})
            if (not isinstance(consumer, dict) or consumer.get('username') != item['consumer']
                    or consumer.get('status') in (False, 0, '0', 'disabled', 'Disabled', 'inactive')
                    or consumer.get('enabled') is False or consumer.get('enable') is False
                    or not isinstance(keyauth, dict) or not isinstance(quota_plugin, dict)
                    or keyauth.get('anonymous') or keyauth.get('disable') is True
                    or isinstance(keyauth.get('_meta'), dict) and keyauth['_meta'].get('disable') is True
                    or quota_plugin.get('disable') is True or quota_plugin.get('disabled') is True
                    or isinstance(quota_plugin.get('_meta'), dict) and quota_plugin['_meta'].get('disable') is True
                    or credential.route_id != route_id or not isinstance(keyauth.get('key'), str)
                    or not hmac.compare_digest(hashlib.sha256(keyauth['key'].encode()).hexdigest(), item['key_hash'])
                    or quota_plugin.get('route_key') != route_key
                    or quota_plugin.get('daily_quota') != current_period['daily_quota']
                    or quota_plugin.get('monthly_quota') != current_period['monthly_quota']
                    or quota_plugin.get('total_quota') != credential.total_quota):
                fail('APISIX consumer readback failed')
            policy = redis.hgetall('market:apisix:credential:' + item['consumer'])
            try:
                actual_periods = json.loads(policy['subscription_periods'])
                actual_period_identity = [(row['period_key'], row['start_at'], row['end_at'])
                    for row in actual_periods]
            except (KeyError, TypeError, ValueError):
                actual_periods = None
                actual_period_identity = None
            expected_policy = policy_before['market:apisix:credential:' + item['consumer']]
            if (policy.get('quota_scope') != expected_policy.get('quota_scope')
                    or not actual_period_identity
                    or actual_period_identity != period_identity_before['market:apisix:credential:' + item['consumer']]
                    or actual_periods != periods
                    or policy.get('mode') != expected_policy.get('mode')
                    or policy.get('status') != credential.status
                    or policy.get('daily_quota') != str(credential.daily_quota or 0)
                    or policy.get('monthly_quota') != str(credential.monthly_quota or 0)
                    or policy.get('total_quota') != str(credential.total_quota or 0)):
                fail('Redis subscription policy readback failed')
            if policy.get('generation') != expected_policy.get('generation'):
                fail('API subscription generation changed')
            if strict:
                saved_receipt = ns['refund_segment_store']['record_receipt'](db, change, credential, direction,
                    receipts[item['id']], client=redis)
                receipt_ids.append(saved_receipt.id)
        evidence = {'route_id': route_id, 'route_key': route_key,
                    'credential_ids': [item['id'] for item in plan['credentials'] if item['status'] == 'active']}
        if strict:
            evidence['receipt_ids'] = receipt_ids
        return evidence

    def saas_apply(db, change, direction, plan):
        target = direction == 'target'
        subscription = db.get(ns['SaaSSubscription'], plan['subscription_id'])
        config = db.get(ns['SaaSIntegrationConfig'], plan['config_id'])
        version_id = change.target_version_id if target else change.old_version_id
        version_code = change.target_version_code if target else change.old_version_code
        version = db.get(ns['SaaSProductVersion'], version_id)
        if (not subscription or not config or not version or version.version_code != version_code
                or subscription.external_tenant_id != plan['tenant_id']
                or datetime_identity(subscription.expires_at) != plan.get('expires_at')
                or digest(safe_saas_config(config)) != plan.get('config_fingerprint')):
            fail('SaaS subscription or version identity changed')
        idem = f'version-change:{change.id}:{direction}'
        mapping = db.scalar(select(ns['SaaSUserMapping']).where(
            ns['SaaSUserMapping'].subscription_id == subscription.id,
            ns['SaaSUserMapping'].status == 'active',
            ns['SaaSUserMapping'].user_id == plan['admin_user_id'],
            ns['SaaSUserMapping'].external_user_id == plan['external_admin_id']))
        client = db.scalar(select(ns['OIDCClient']).where(
            ns['OIDCClient'].product_id == plan['product_id'],
            ns['OIDCClient'].client_id == plan['oidc_client_id'],
            ns['OIDCClient'].status == 'active'))
        if not mapping or not client:
            fail('SaaS admin or OIDC identity changed after resource preparation')
        result = ns['saas_call'](config, 'CHANGE', {'tenant_id': plan['tenant_id'],
            'product_id': plan['product_id'], 'enterprise_id': plan['enterprise_id'],
            'subscription_id': subscription.id, 'to_version': version_code,
            'admin_user_id': plan['admin_user_id'], 'oidc_client_id': plan['oidc_client_id'],
            'idempotency_key': idem, 'change_id': change.id, 'direction': direction})
        if not isinstance(result, dict) or result.get('ret') != 0:
            fail('SaaS CHANGE was rejected')
        status = ns['saas_call'](config, 'STATUS', {'tenant_id': plan['tenant_id'],
            'product_id': plan['product_id'], 'enterprise_id': plan['enterprise_id'],
            'subscription_id': subscription.id, 'admin_user_id': plan['admin_user_id'],
            'oidc_client_id': plan['oidc_client_id'], 'change_id': change.id, 'direction': direction})
        if (not isinstance(status, dict) or status.get('ret') != 0
                or status.get('tenant_id') != plan['tenant_id']
                or status.get('product_id') != plan['product_id']
                or status.get('enterprise_id') != plan['enterprise_id']
                or status.get('subscription_id') != subscription.id
                or str(status.get('version')) != str(version_code)
                or status.get('status') != 'active'
                or status.get('admin_user_id') != plan['admin_user_id']
                or status.get('admin_active') is not True
                or status.get('application_ready') is not True
                or status.get('oidc_client_id') != plan['oidc_client_id']
                or datetime_identity(status.get('expires_at')) != plan.get('expires_at')
                or datetime_identity(subscription.expires_at) != plan.get('expires_at')):
            fail('SaaS STATUS does not verify requested subscription version')
        try:
            saas_switch_evidence(change.id, direction, result, status)
        except (ValueError, TypeError, KeyError, AttributeError):
            fail('SaaS CHANGE/STATUS effective evidence is incomplete or mismatched')
        evidence = {'subscription_id': subscription.id, 'tenant_id': plan['tenant_id'], 'version_id': version_id}
        if ns.get('refund_segment_store'):
            receipt = ns['refund_segment_store']['record_receipt'](db, change, None, direction,
                {'change': result, 'status': status}, subscription=subscription)
            if receipt is not None:
                evidence['receipt_ids'] = [receipt.id]
        subscription.version_id = version_id
        return evidence

    def apply(db, change, direction='target'):
        if direction not in {'target', 'original'}:
            fail('Invalid resource change direction')
        try:
            plan = json.loads(change.resource_plan_json or '')
        except (TypeError, ValueError):
            plan = prepare(db, change)
        if plan.get('change_id') != change.id:
            fail('Resource plan belongs to another version change')
        if plan.get('kind') == 'api':
            redis = ns['apisix_policy_redis']()
            try:
                evidence = api_apply(db, change, direction, plan, redis)
            finally:
                close = getattr(redis, 'close', None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        elif plan.get('kind') == 'saas':
            evidence = saas_apply(db, change, direction, plan)
        else:
            evidence = None
        if evidence is None:
            fail('Resource type has no verified adapter')
        safe = {'change_id': change.id, 'direction': direction, 'kind': plan['kind'],
                'evidence': evidence}
        fingerprint = digest(safe)
        change.evidence_json = json.dumps({'direction': direction, 'kind': plan['kind'],
            'evidence_fingerprint': fingerprint}, sort_keys=True, separators=(',', ':'))
        return {'request_change_id': change.id, 'resource_change_id': change.id,
                'evidence_fingerprint': fingerprint}

    hooks = {'prepare': prepare, 'apply': apply}
    ns['version_change_resources'] = hooks
    return hooks
