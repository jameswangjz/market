"""Verified automatic fulfillment for API and model API orders."""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import Column, ForeignKey, String, Text, event, select

from .order_economics import money
from .subscription_calendar import CalendarAnchor
from .api_provider_lifecycle import QUOTAS, frozen_snapshot, period_snapshot

logger = logging.getLogger(__name__)


def provider_periods(ns, db, rows):
    """Translate verified gateway rights without changing quota units."""
    result = []
    for row in rows:
        order = db.get(ns['Order'], row['order_id'])
        term = db.get(ns['subscription_terms']['Term'], row['segment_id'])
        if not order or not term:
            raise HTTPException(409, 'Provider period ownership is unavailable')
        delivery = ns['subscription_terms']['effective_snapshot'](db, order, term)['delivery']
        quotas = {key: row.get(key, delivery['quota'][key]) for key in QUOTAS}
        combo = db.get(ns['subscription_terms']['Combination'], term.combination_id)
        if not combo or combo.generation != term.generation:
            raise HTTPException(409, 'Provider period calendar is unavailable')
        index = int(row['period_key'].rsplit(':', 1)[1])
        bounds = CalendarAnchor(combo.anchor_at).period(index)
        if (row['period_key'] != f'{combo.id}:{combo.generation}:{index}'
                or index < term.start_month or index >= term.start_month + term.months):
            raise HTTPException(409, 'Provider period calendar identity changed')
        # Gateway native buckets use integer seconds; retain the exact calendar
        # edges when those seconds still identify an unmodified native bucket.
        start = row.get('starts_at') or (bounds.start if row['start_at'] == int(bounds.start.timestamp())
            else datetime.fromtimestamp(row['start_at'], timezone.utc))
        end = row.get('ends_at') or (bounds.end if row['end_at'] == int(bounds.end.timestamp())
            else datetime.fromtimestamp(row['end_at'], timezone.utc))
        start = datetime.fromisoformat(start) if isinstance(start, str) else start
        end = datetime.fromisoformat(end) if isinstance(end, str) else end
        start, end = max(start, bounds.start, term.starts_at), min(end, bounds.end, term.ends_at)
        result.append(period_snapshot(order, term, start, end, quotas))
    return result


def provider_operation(ns, db, order, term, combo, *, endpoint, periods,
                       event_type, operation_key, version=None, **optional):
    if not endpoint:
        return {'status': 'skipped_unmanaged', 'provider_synced': False}
    hooks = ns.get('api_provider_lifecycle')
    if not hooks:
        raise HTTPException(409, 'API provider lifecycle is not installed')
    old = db.scalar(select(hooks['Plan']).where(
        hooks['Plan'].enterprise_id == order.buyer_enterprise_id,
        hooks['Plan'].product_id == order.product_id,
        hooks['Plan'].operation_key == operation_key))
    tenant = db.scalar(select(hooks['Tenant']).where(
        hooks['Tenant'].enterprise_id == order.buyer_enterprise_id,
        hooks['Tenant'].product_id == order.product_id))
    body = frozen_snapshot(order, term, combo, event_type=event_type,
        operation_key=operation_key, periods=periods,
        starts_at=min(datetime.fromisoformat(p['starts_at']) for p in periods) if periods else combo.anchor_at,
        expires_at=max(datetime.fromisoformat(p['expires_at']) for p in periods) if periods
            else CalendarAnchor(combo.anchor_at).boundary(combo.total_months),
        tenant_id=json.loads(old.body_json)['tenant_id'] if old else (tenant.tenant_id if tenant else None),
        version=version, **optional)
    record, _ = hooks['prepare'](db, body, endpoint)
    if isinstance(record, dict):
        raise HTTPException(409, 'Configured provider operation was not prepared')
    return {'plan_id': record.id, 'endpoint': endpoint, 'payload': body}


def execute_provider(ns, db, operation):
    if operation == {'status': 'skipped_unmanaged', 'provider_synced': False}:
        return operation
    hooks = ns.get('api_provider_lifecycle')
    if not hooks or not isinstance(operation, dict):
        raise HTTPException(409, 'Frozen provider operation is unavailable')
    record = db.get(hooks['Plan'], operation.get('plan_id'))
    if (not record or record.endpoint != operation.get('endpoint')
            or json.loads(record.body_json) != operation.get('payload')):
        raise HTTPException(409, 'Frozen provider operation changed')
    return hooks['execute'](db, record)


def install(ns):
    ApiCredential, Route = ns['ApiCredential'], ns['ApiGatewayRoute']
    compensation_key = '_api_delivery_redis_compensations'
    Preparation = ns.get('_api_delivery_preparation_model')
    if Preparation is None:
        class Preparation(ns['Base']):
            __tablename__ = 'api_delivery_preparations'
            order_id = Column(ForeignKey('orders.id'), primary_key=True)
            enterprise_id = Column(String(36), nullable=False)
            product_id = Column(String(36), nullable=False)
            frozen_json = Column(Text, nullable=False)
        ns['_api_delivery_preparation_model'] = Preparation

        def immutable(*args):
            raise ValueError('API delivery preparations are immutable')
        event.listen(Preparation, 'before_update', immutable)
        event.listen(Preparation, 'before_delete', immutable)

    def assert_no_pending(db, enterprise_id, product_id, order_id=None):
        Term = ns['subscription_terms']['Term']
        pending = db.scalars(select(Preparation).outerjoin(Term,
            Term.order_id == Preparation.order_id).where(
                Preparation.enterprise_id == enterprise_id,
                Preparation.product_id == product_id, Term.id.is_(None))).all()
        if any(row.order_id != order_id for row in pending):
            raise HTTPException(409, 'Another API delivery is awaiting provider and gateway verification')
        lifecycle = ns.get('api_provider_lifecycle')
        if lifecycle:
            plans = db.scalars(select(lifecycle['Plan']).where(
                lifecycle['Plan'].enterprise_id == enterprise_id,
                lifecycle['Plan'].product_id == product_id,
                lifecycle['Plan'].operation_key.like('delivery:%'))).all()
            for plan in plans:
                owner = json.loads(plan.body_json)['order_id']
                if owner != order_id and db.scalar(select(Term.id).where(Term.order_id == owner)) is None:
                    raise HTTPException(409, 'Another provider delivery is awaiting gateway verification')

    def combination_state(combo):
        if combo is None:
            return None
        return {key: (getattr(combo, key).isoformat() if key == 'anchor_at' else getattr(combo, key))
            for key in ('id', 'generation', 'anchor_at', 'total_months', 'version_id',
                        'version_code', 'delivery_method', 'status')}

    def prepare_delivery(db, order, delivery):
        terms = ns['subscription_terms']
        combo = terms['lock_combination'](db, order.buyer_enterprise_id, order.product_id)
        assert_no_pending(db, order.buyer_enterprise_id, order.product_id, order.id)
        guard = ns.get('version_change_saga', {}).get('assert_no_pending')
        if callable(guard):
            guard(db, order.buyer_enterprise_id, order.product_id)
        saved = db.get(Preparation, order.id)
        if saved:
            frozen = json.loads(saved.frozen_json)
            if frozen['before'] != combination_state(combo):
                raise HTTPException(409, 'Subscription changed after API delivery preparation')
            body = frozen['provider']['payload']
            if (body['version'] != {'id': order.product_version_id, 'code': order.product_version_code}
                    or body['subscription_months'] != order.subscription_months
                    or body['order_no'] != order.order_no or frozen['delivery'] != delivery):
                raise HTTPException(409, 'API delivery frozen version changed')
            return frozen, False
        if not (delivery.get('integration_api_url') or '').strip():
            return None, False
        at = ns['now']()
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        live = combo is not None and combo.status == 'active' and at < CalendarAnchor(combo.anchor_at).boundary(combo.total_months)
        if live and (combo.version_id != order.product_version_id
                or combo.version_code != order.product_version_code
                or combo.delivery_method != order.delivery_method_snapshot):
            raise HTTPException(409, 'API delivery version differs from active subscription')
        if live and at < combo.anchor_at:
            raise HTTPException(409, 'API delivery precedes the subscription anchor')
        start_month = combo.total_months if live else 0
        projected = SimpleNamespace(id=combo.id if combo else str(uuid.uuid4()),
            enterprise_id=order.buyer_enterprise_id, product_id=order.product_id,
            generation=combo.generation if live else (combo.generation + 1 if combo else 1),
            anchor_at=combo.anchor_at if live else at, total_months=start_month + order.subscription_months)
        term = SimpleNamespace(id=str(uuid.uuid4()), order_id=order.id, combination_id=projected.id,
            generation=projected.generation, months=order.subscription_months,
            version_id=order.product_version_id, version_code=order.product_version_code)
        periods = provider_periods(ns, db, terms['quota_periods'](
            db, order.buyer_enterprise_id, order.product_id)) if live else []
        anchor = CalendarAnchor(projected.anchor_at)
        for index in range(start_month, projected.total_months):
            bounds = anchor.period(index)
            periods.append(period_snapshot(order, term, bounds.start, bounds.end, delivery['quota']))
        lifecycle = ns.get('api_provider_lifecycle')
        tenant = db.scalar(select(lifecycle['Tenant']).where(
            lifecycle['Tenant'].enterprise_id == order.buyer_enterprise_id,
            lifecycle['Tenant'].product_id == order.product_id)) if lifecycle else None
        operation = provider_operation(ns, db, order, term, projected,
            endpoint=delivery['integration_api_url'].strip(), periods=periods,
            event_type='RENEW' if live else ('RESTORE' if tenant is not None else 'OPEN'),
            operation_key='delivery:' + order.id)
        frozen = dict(before=combination_state(combo), planned_at=at.isoformat(),
            combination_id=projected.id, term_id=term.id, provider=operation, delivery=delivery)
        db.add(Preparation(order_id=order.id, enterprise_id=order.buyer_enterprise_id,
            product_id=order.product_id, frozen_json=json.dumps(frozen, sort_keys=True)))
        db.flush()
        return frozen, True

    def fail():
        raise HTTPException(409, 'API 路由或订阅策略尚未通过交付核验')

    def unwrap(value):
        if isinstance(value, dict):
            if isinstance(value.get('node'), dict):
                return unwrap(value['node'])
            if isinstance(value.get('value'), dict):
                return unwrap(value['value'])
            if isinstance(value.get('route'), dict):
                return unwrap(value['route'])
        return value

    def route_matches(actual, expected):
        actual = unwrap(actual)
        if not isinstance(actual, dict):
            return False
        disabled = (actual.get('status') in (False, 0, '0', 'disabled', 'Disabled')
            or actual.get('enabled') is False or actual.get('enable') is False)
        if disabled or actual.get('uri') != expected.get('uri'):
            return False
        actual_methods, expected_methods = actual.get('methods'), expected.get('methods')
        if (not isinstance(actual_methods, list) or not isinstance(expected_methods, list)
                or set(actual_methods) != set(expected_methods)):
            return False
        if any(actual.get(key) for key in ('uris', 'host', 'hosts', 'vars',
                'upstream_id', 'service_id', 'plugin_config_id')):
            return False
        if not upstream_matches(expected.get('upstream'), actual.get('upstream')):
            return False
        expected_plugins, actual_plugins = expected.get('plugins'), actual.get('plugins')
        if (not isinstance(expected_plugins, dict) or not isinstance(actual_plugins, dict)
                or set(expected_plugins) != set(actual_plugins)):
            return False
        for name, config in expected_plugins.items():
            actual_config = actual_plugins.get(name)
            if not subset(config, actual_config):
                return False
            if (not isinstance(actual_config, dict)
                    or (isinstance(actual_config.get('_meta'), dict)
                        and actual_config['_meta'].get('disable') is True)
                    or actual_config.get('disable') is True):
                return False
            if name == 'key-auth' and actual_config.get('anonymous'):
                return False
            if name == 'limit-count' and actual_config.get('allow_degradation') is True:
                return False
            if name == 'proxy-rewrite' and any(actual_config.get(key)
                    for key in ('uri', 'host', 'scheme', 'headers', 'use_real_request_uri_unsafe')):
                return False
        return subset(expected.get('labels'), actual.get('labels'))

    def subset(expected, actual):
        if isinstance(expected, dict):
            return (isinstance(actual, dict) and all(key in actual and subset(value, actual[key])
                for key, value in expected.items()))
        if isinstance(expected, list):
            return isinstance(actual, list) and expected == actual
        return expected == actual

    def upstream_matches(expected, actual):
        if not isinstance(expected, dict) or not isinstance(actual, dict):
            return False
        if actual.get('upstream_id') or actual.get('service_id') or actual.get('discovery_type'):
            return False
        expected_nodes = expected.get('nodes')
        actual_nodes = actual.get('nodes')
        return (isinstance(expected_nodes, dict) and isinstance(actual_nodes, dict)
            and actual_nodes == expected_nodes and subset(expected, actual))

    def restore_policies(redis, policies):
        failed = []
        for consumer, values in policies.items():
            key = 'market:apisix:credential:' + consumer
            try:
                redis.delete(key)
                if values:
                    redis.hset(key, mapping=values)
            except Exception:
                try:
                    redis.delete(key)
                except Exception:
                    failed.append(consumer)
        return failed

    def track_root_transaction(db, redis, policies):
        root = db.get_transaction()
        if root is None:
            raise RuntimeError('API delivery has no owning database transaction')
        key = id(root)
        tracked = db.info.setdefault(compensation_key, {})
        state = tracked.get(key)
        if state is None:
            state = {'redis': redis, 'policies': {}, 'consumers': set(),
                     'failed_consumers': set(), 'committed': False}
            tracked[key] = state

            def committed(session):
                if session.get_nested_transaction() is None:
                    owner = session.get_transaction()
                    if owner is root:
                        state['committed'] = True

            def transaction_end(session, transaction):
                if transaction is not root:
                    return
                tracked.pop(key, None)
                if state['committed']:
                    failed = compensate_new_consumers(state, state['failed_consumers'])
                elif not state.get('restored'):
                    failed = restore_policies(state['redis'], state['policies'])
                    failed.extend(remove_consumers(state['consumers']))
                    state['restored'] = True
                else:
                    failed = []
                if failed:
                    logger.critical('API delivery transaction compensation failed for %d consumers',
                        len(failed))
                    raise RuntimeError('API delivery cache rollback compensation failed')

            def rolled_back(session, transaction):
                if transaction is not root or state.get('restored'):
                    return
                failed = restore_policies(state['redis'], state['policies'])
                failed.extend(remove_consumers(state['consumers']))
                state['restored'] = True
                tracked.pop(key, None)
                if failed:
                    logger.critical('API delivery rollback could not restore or disable Redis policies for %d consumers',
                        len(failed))
                    raise RuntimeError('API delivery cache rollback compensation failed')

            event.listen(db, 'after_commit', committed)
            event.listen(db, 'after_transaction_end', transaction_end)
            event.listen(db, 'after_soft_rollback', rolled_back)
        for name, values in policies.items():
            state['policies'].setdefault(name, dict(values))
        return state

    def remove_consumers(consumers):
        failed = []
        for consumer in consumers:
            try:
                delete_consumer(consumer)
            except Exception:
                failed.append(consumer)
        return failed

    def compensate_new_consumers(state, consumers):
        failed = []
        for consumer in consumers:
            try:
                state['redis'].delete('market:apisix:credential:' + consumer)
            except Exception:
                failed.append(consumer)
            try:
                delete_consumer(consumer)
            except Exception:
                failed.append(consumer)
        return failed

    def delete_consumer(consumer):
        try:
            return ns['apisix_admin_request']('DELETE', f'/consumers/{consumer}')
        except Exception as exc:
            response = getattr(exc, 'response', None)
            if response is None:
                response = getattr(getattr(exc, '__cause__', None), 'response', None)
            if (getattr(exc, 'status_code', None) == 404
                    or getattr(response, 'status_code', None) == 404):
                return None
            raise

    def register_consumer_compensation(db, consumer):
        redis = ns['apisix_policy_redis']()
        state = track_root_transaction(db, redis, {consumer: {}})
        state['consumers'].add(consumer)

    def cleanup_consumer_compensation(db, consumer):
        root = db.get_transaction()
        state = db.info.get(compensation_key, {}).get(id(root)) if root else None
        redis = state['redis'] if state else ns['apisix_policy_redis']()
        if state:
            state['failed_consumers'].add(consumer)
        errors = []
        try:
            redis.delete('market:apisix:credential:' + consumer)
        except Exception:
            errors.append('redis')
        try:
            delete_consumer(consumer)
        except Exception:
            errors.append('apisix')
        if not errors and state:
            state['consumers'].discard(consumer)
            state['failed_consumers'].discard(consumer)
            state['policies'].pop(consumer, None)
        if errors:
            logger.critical('API credential eager compensation failed for external systems: %s',
                ','.join(errors))
            raise RuntimeError('API credential external compensation failed')

    ns['_track_api_consumer_compensation'] = register_consumer_compensation
    ns['_track_api_policy_compensation'] = track_root_transaction
    ns['_restore_api_policies'] = restore_policies
    ns['_cleanup_api_consumer_compensation'] = cleanup_consumer_compensation

    def process(db, order, task, user):
        economic = ns['order_economics']['economic_snapshot'](order)
        delivery = ns['order_economics']['delivery_snapshot'](order)
        method = order.delivery_method_snapshot
        if (method not in {'api', 'model_api'} or order.snapshot_version != 1
                or task.order_id != order.id or task.method != method or task.delivery_mode != 'automatic'
                or economic.get('product_id') != order.product_id
                or economic.get('product_version_id') != order.product_version_id
                or economic.get('version_code') != order.product_version_code
                or delivery.get('method') != method):
            raise HTTPException(409, 'API 交付任务与订单冻结快照不一致')
        if (order.payment_status != 'paid' or money(order.paid_amount) != money(order.amount)
                or money(order.refunded_amount) != 0
                or order.main_status in {'cancelled', 'closed', 'refunded'}):
            raise HTTPException(409, 'API 自动交付须使用全额支付且未退款的有效订单')
        product = db.scalar(select(ns['Product']).where(ns['Product'].id == order.product_id)
            .with_for_update().execution_options(populate_existing=True))
        version = db.get(ns['ProductReleaseVersion'], order.product_version_id)
        gateway_product = product and (product.product_type in {'api', 'model'}
            or method in {'api', 'model_api'})
        if (not gateway_product or product.status == 'security_unpublished' or not version
                or version.product_id != product.id or version.version_code != order.product_version_code
                or version.status != 'active'):
            raise HTTPException(409, 'API 产品或订单版本无效')

        frozen, created = prepare_delivery(db, order, delivery)
        if created:
            return None
        if frozen:
            execute_provider(ns, db, frozen['provider'])

        # verify_delivery performs SSRF-safe live health validation and checks the
        # version's stored integration evidence. APISIX enabled is required below too.
        ns['subscription_gateway']['verify_delivery'](db, order)
        config = db.scalar(select(ns['version_gateway']['Config']).where(
            ns['version_gateway']['Config'].version_id == version.id))
        route = db.get(Route, config.route_id) if config and config.route_id else None
        if (not config or not route or route.product_id != product.id
                or route.version != version.version_code or route.route_key != config.route_key
                or route.status != 'active' or not ns['apisix_enabled']()):
            fail()
        expected = ns['apisix_route_payload'](route, product, db)
        live = ns['apisix_admin_request']('GET', f'/routes/{route.route_key}')
        if not route_matches(live, expected):
            fail()

        credentials = db.scalars(select(ApiCredential).where(
            ApiCredential.route_id == route.id,
            ApiCredential.enterprise_id == order.buyer_enterprise_id,
            ApiCredential.order_id == '', ApiCredential.status == 'active',
        ).with_for_update()).all()
        live_consumers = {}
        for credential in credentials:
            if not credential.apisix_consumer_name:
                fail()
            consumer = unwrap(ns['apisix_admin_request']('GET',
                f'/consumers/{credential.apisix_consumer_name}'))
            plugins = consumer.get('plugins', {}) if isinstance(consumer, dict) else {}
            key_auth = plugins.get('key-auth', {}) if isinstance(plugins, dict) else {}
            quota = plugins.get('market-gateway-quota', {}) if isinstance(plugins, dict) else {}
            raw_key = key_auth.get('key') if isinstance(key_auth, dict) else None
            if (not isinstance(consumer, dict)
                    or consumer.get('status') in (False, 0, '0', 'disabled', 'Disabled', 'inactive')
                    or consumer.get('enabled') is False or consumer.get('enable') is False
                    or consumer.get('username') != credential.apisix_consumer_name
                    or not isinstance(raw_key, str) or not raw_key
                    or hashlib.sha256(raw_key.encode()).hexdigest() != credential.key_hash
                    or not raw_key.startswith(credential.key_prefix)
                    or key_auth.get('anonymous')
                    or key_auth.get('disable') is True
                    or (isinstance(key_auth.get('_meta'), dict)
                        and key_auth['_meta'].get('disable') is True)
                    or not isinstance(quota, dict)
                    or quota.get('route_key') != route.route_key
                    or quota.get('disabled') is True or quota.get('disable') is True
                    or (isinstance(quota.get('_meta'), dict)
                        and quota['_meta'].get('disable') is True)):
                fail()
            live_consumers[credential.id] = credential.apisix_consumer_name

        prior = order.delivery_status, order.main_status
        redis, old_policies, compensation = None, {}, None
        try:
            if credentials:
                redis = ns['apisix_policy_redis']()
                refreshed = db.scalars(select(ApiCredential).where(
                    ApiCredential.route_id == route.id,
                    ApiCredential.enterprise_id == order.buyer_enterprise_id,
                    ApiCredential.apisix_consumer_name != '',
                ).with_for_update()).all()
                old_policies = {row.apisix_consumer_name:
                    redis.hgetall('market:apisix:credential:' + row.apisix_consumer_name)
                    for row in refreshed}
                compensation = track_root_transaction(db, redis, old_policies)
            order.delivery_status, order.main_status = 'accepted', 'pending_confirmation'
            reserved = dict(at=datetime.fromisoformat(frozen['planned_at']),
                combination_id=frozen['combination_id'], term_id=frozen['term_id']) if frozen else {}
            term = ns['subscription_terms']['on_delivery_success'](db, order, user, **reserved)
            if not term:
                fail()
            if method in {'api', 'model_api'}:
                ns['initial_api_credentials']['ensure_initial'](db, order, route, user)
            if credentials:
                ns['subscription_gateway']['refresh'](db, order.buyer_enterprise_id, route)
                for credential in credentials:
                    expected_policy = ns['subscription_gateway']['metadata'](db, credential, route)
                    if not expected_policy:
                        fail()
                    policy = redis.hgetall('market:apisix:credential:' + credential.apisix_consumer_name)
                    if (set(policy) != set(expected_policy)
                            or any(str(policy[key]) != str(value)
                                for key, value in expected_policy.items())):
                        fail()
        except Exception:
            order.delivery_status, order.main_status = prior
            if redis is not None:
                failed = restore_policies(redis, old_policies)
                failed.extend(remove_consumers(
                    db.info.get(compensation_key, {}).get(id(db.get_transaction()), {}).get('consumers', set())))
                if failed:
                    logger.critical('API delivery attempt could not restore or disable %d Redis policies', len(failed))
                    raise RuntimeError('API delivery cache rollback compensation failed') from None
            raise
        return True

    hooks = {'process': process, 'route_matches': route_matches,
        'assert_no_pending': assert_no_pending, 'Preparation': Preparation}
    ns['api_auto_delivery'] = hooks
    return hooks
