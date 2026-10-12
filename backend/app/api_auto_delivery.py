"""Verified automatic fulfillment for API and model API orders."""
import hashlib
import logging

from fastapi import HTTPException
from sqlalchemy import event, select

from .order_economics import money

logger = logging.getLogger(__name__)


def install(ns):
    ApiCredential, Route = ns['ApiCredential'], ns['ApiGatewayRoute']
    compensation_key = '_api_delivery_redis_compensations'

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
            state = {'redis': redis, 'policies': {}, 'committed': False}
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
                if not state['committed'] and not state.get('restored'):
                    failed = restore_policies(state['redis'], state['policies'])
                    state['restored'] = True
                    if failed:
                        logger.critical('API delivery transaction rollback could not restore or disable Redis policies for %d consumers',
                            len(failed))
                        raise RuntimeError('API delivery cache rollback compensation failed')

            def rolled_back(session, transaction):
                if transaction is not root or state.get('restored'):
                    return
                failed = restore_policies(state['redis'], state['policies'])
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
        product = db.get(ns['Product'], order.product_id)
        version = db.get(ns['ProductReleaseVersion'], order.product_version_id)
        gateway_product = product and (product.product_type in {'api', 'model'}
            or method in {'api', 'model_api'})
        if (not gateway_product or product.status == 'security_unpublished' or not version
                or version.product_id != product.id or version.version_code != order.product_version_code
                or version.status != 'active'):
            raise HTTPException(409, 'API 产品或订单版本无效')

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
            term = ns['subscription_terms']['on_delivery_success'](db, order, user)
            if not term:
                fail()
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
                if failed:
                    logger.critical('API delivery attempt could not restore or disable %d Redis policies', len(failed))
                    raise RuntimeError('API delivery cache rollback compensation failed') from None
            raise
        return True

    return {'process': process, 'route_matches': route_matches}
