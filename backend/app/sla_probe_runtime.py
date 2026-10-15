"""Trusted paid-contract resolver for independent business-entry probe slots.

Integration: install after order_sla_contracts/subscription_terms and collector,
before the parent's schema startup. This module performs no DDL or commits.
Default sla_probe_route_binding reads installed APISIX configuration, checks
server labels and full business-route equivalence, and provisions a dedicated
key-auth consumer derived from SLA_PROBE_SECRET. No buyer credential is used.
SaaS uses sla_saas_business_probe_source(db, order, entitlement) -> SaaSProbeSource
only; management integrations, landing pages and buyer keys are never inferred.
API slots are shared by product/version/gateway route. The collecting contract
is provenance, not exclusive ownership. Each order query clips shared facts to
its own verified service interval and prefers shared over legacy order rows for
the same slot. SaaS retains contract/tenant isolation; no sample pooling occurs.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import os
import re
from urllib.parse import urlsplit

from sqlalchemy import and_, case, func, literal, or_, select

from .safe_upstream import validate_upstream_url, validated_http_request
from .sla_probe_collector import IdentitySourceMissing, INTERVAL_SECONDS, ProbeTarget, install as install_collector


SIGNING_VERSION = 'sla-probe-v1'
POLICY_VERSION = 'paid-contract-probe-v1'


def utc(value):
    if not isinstance(value, datetime):
        raise ValueError('timestamp required')
    # Persisted SQL DateTime(timezone=True) loses UTC tzinfo in SQLite tests.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def probe_headers(secret, *, route_id, product_id, version_id, method, path, instant):
    """HMAC input shared with the parent's route-local Lua verifier; no raw key."""
    values = [route_id, product_id, version_id, method, path]
    if (not isinstance(secret, str) or len(secret) < 32 or any(ord(c) < 33 or ord(c) == 127 for c in secret)
            or any(not isinstance(v, str) or not v or '\n' in v or '\r' in v for v in values)
            or method not in {'GET', 'HEAD'}):
        raise IdentitySourceMissing()
    stamp = str(int(utc(instant).timestamp()))
    message = '\n'.join([SIGNING_VERSION, *values, stamp])
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {'X-Market-SLA-Route': route_id, 'X-Market-SLA-Product': product_id,
            'X-Market-SLA-Version': version_id, 'X-Market-SLA-Timestamp': stamp,
            'X-Market-SLA-Signature': signature}


def probe_consumer_name(route_id, product_id, version_id):
    material = '\n'.join([route_id, product_id, version_id])
    return 'market-sla-probe-' + hashlib.sha256(material.encode()).hexdigest()[:32]


def probe_consumer_key(secret, route_id, product_id, version_id):
    # Reuse identity input validation; the signature is not the consumer key.
    probe_headers(secret, route_id=route_id, product_id=product_id, version_id=version_id,
        method='HEAD', path='/', instant=datetime.now(timezone.utc))
    material = '\n'.join(['sla-consumer-v1', route_id, product_id, version_id])
    return hmac.new(secret.encode(), material.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class SaaSProbeSource:
    product_id: str
    version_id: str
    subscription_id: str
    url: str
    evidence_id: str
    config_version: str
    identity_id: str
    headers: object = field(repr=False)
    method: str = 'HEAD'


def install(ns):
    if ns.get('sla_probe_runtime'):
        return ns['sla_probe_runtime']
    # Never replace the bounded adapter with httpx or trust a client supplied URL.
    if ns.get('validated_http_request') is not validated_http_request:
        raise ValueError('The bounded safe_upstream adapter must be installed')
    collector = ns.get('sla_probe_collector') or install_collector(ns)
    Slot = collector['Slot']
    Contract = ns['order_sla_contracts']['Contract']
    Order, Product, Route = ns['Order'], ns['Product'], ns['ApiGatewayRoute']
    Term = ns['subscription_terms']['Term']

    def secret_value():
        source = ns.get('sla_probe_secret')
        return source() if callable(source) else os.environ.get('SLA_PROBE_SECRET', '')

    def unwrap(value):
        if isinstance(value, dict) and isinstance(value.get('node'), dict):
            value = value['node']
        if isinstance(value, dict) and isinstance(value.get('value'), dict):
            value = value['value']
        return value

    def default_binding(db, route, version_id):
        """Verify actual data plane and create only our deterministic consumer.

        Admin writes are confined to /consumers/market-sla-probe-*. Route/global
        rule/Lua/quota metadata are never changed here. Standard key-auth and
        rate limiting remain enabled; only server-verified probes can be quota
        exempted by the parent's route-local Lua policy.
        """
        admin, payload = ns.get('apisix_admin_request'), ns.get('apisix_route_payload')
        if not callable(admin) or not callable(payload) or not ns.get('apisix_native_auth', lambda: False)():
            return None
        if (not isinstance(route.route_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', route.route_key)
                or route.health_method not in {'GET', 'HEAD'}
                or not isinstance(route.health_path, str) or not route.health_path.startswith('/')
                or route.health_path.startswith('//')):
            return None
        try:
            cidrs = [str(ipaddress.ip_network(c.strip(), strict=True)) for c in
                os.environ.get('SLA_PROBE_SOURCE_CIDRS', '').split(',') if c.strip()]
            if not cidrs or any(ipaddress.ip_network(c).prefixlen == 0 for c in cidrs):
                return None
            product = db.get(Product, route.product_id)
            if not product or product.status == 'security_unpublished' or route.status != 'active':
                return None
            name = probe_consumer_name(route.id, route.product_id, version_id)
            key = probe_consumer_key(secret_value(), route.id, route.product_id, version_id)
            actual = unwrap(admin('GET', f'/routes/{route.route_key}'))
            labels = actual.get('labels', {}) if isinstance(actual, dict) else {}
            path = f'/gateway/{route.route_key}{route.health_path}'
            required = {'market_sla_probe_enabled': 'true', 'market_sla_probe_path': path,
                'market_sla_probe_method': route.health_method, 'market_sla_probe_route_id': route.id,
                'market_version_id': version_id, 'market_sla_probe_consumer': name,
                'market_sla_probe_source_cidrs': ','.join(cidrs)}
            if (any(labels.get(k) != v for k, v in required.items())
                    or not isinstance(labels.get('market_sla_probe_config_version'), str)
                    or not labels['market_sla_probe_config_version']):
                return None
            from .api_auto_delivery import install as install_api_delivery
            matcher = install_api_delivery(ns)['route_matches']
            if not matcher(actual, payload(route, product, db)):
                return None
            plugins = actual.get('plugins', {})
            if (plugins.get('key-auth', {}).get('header') != 'X-API-Key'
                    or plugins.get('key-auth', {}).get('anonymous')
                    or plugins.get('limit-count', {}).get('key') != 'consumer_name'
                    or plugins.get('market-gateway-quota', {}).get('route_key') != route.route_key):
                return None
            terminal = unwrap(admin('GET', '/global_rules/market-sla-terminal'))
            logger_config = terminal.get('plugins', {}).get('market-sla-logger') if isinstance(terminal, dict) else None
            if (not isinstance(logger_config, dict) or logger_config.get('disable') is True
                    or logger_config.get('_meta', {}).get('disable') is True):
                return None
            # No ApiCredential row, grant, subscription or buyer Redis bucket.
            admin('PUT', f'/consumers/{name}', {'username': name, 'plugins': {'key-auth': {'key': key}}})
            consumer = unwrap(admin('GET', f'/consumers/{name}'))
            if (not isinstance(consumer, dict) or consumer.get('username') != name
                    or set(consumer.get('plugins', {})) != {'key-auth'}
                    or not hmac.compare_digest(str(consumer['plugins']['key-auth'].get('key', '')), key)):
                return None
            return dict(enabled=True, verified=True, quota_isolated=True, route_id=route.id,
                product_id=route.product_id, version_id=version_id, route_key=route.route_key,
                probe_path=path, method=route.health_method, source_cidrs=cidrs,
                config_version=labels['market_sla_probe_config_version'])
        except Exception:
            # Do not disclose admin responses, URLs or keys in diagnostics.
            return None

    if not callable(ns.get('sla_probe_route_binding')):
        ns['sla_probe_route_binding'] = default_binding

    def page(db, *, after_id='', limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('page limit must be in [1, 100]')
        return list(db.scalars(select(Contract.id).where(Contract.id > after_id,
            Contract.verified_at.is_not(None), Contract.delivery_eligible.is_(True))
            .order_by(Contract.id).limit(limit)))

    def api_source(db, route, order, entitlement):
        verifier = ns.get('sla_probe_route_binding')
        if not route or not callable(verifier) or route.status != 'active':
            return None
        binding = verifier(db, route, entitlement['version_id'])
        path = f'/gateway/{route.route_key}{route.health_path}'
        expected = {'enabled': True, 'verified': True, 'quota_isolated': True,
            'route_id': route.id, 'product_id': order.product_id, 'version_id': entitlement['version_id'],
            'route_key': route.route_key, 'probe_path': path, 'method': route.health_method}
        if (not isinstance(binding, dict) or any(binding.get(k) != v for k, v in expected.items())
                or any(binding.get(k) is not True for k in ('enabled', 'verified', 'quota_isolated'))
                or not isinstance(binding.get('config_version'), str) or not binding['config_version']
                or not isinstance(binding.get('source_cidrs'), list) or not binding['source_cidrs']
                or not re.fullmatch(r'[A-Za-z0-9_-]+', route.route_key)
                or route.health_method not in {'GET', 'HEAD'} or not route.health_path.startswith('/')
                or route.health_path.startswith('//')):
            return None
        origin = ns.get('sla_probe_gateway_origin') or os.environ.get('SLA_PROBE_GATEWAY_ORIGIN', '')
        try:
            parsed = urlsplit(origin)
            if parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
                return None
            url = origin.rstrip('/') + path
            validate_upstream_url(url)
        except (TypeError, ValueError):
            return None
        return {**binding, 'url': url, 'evidence_id': f'gateway-binding:{route.id}', 'kind': 'api_gateway'}

    def resolve_detail(db, contract_id, instant, *, resolve_source=True):
        at = utc(instant)
        contract = db.get(Contract, contract_id)
        if not contract or not contract.delivery_eligible or not contract.verified_at or not contract.source_delivery_task_id:
            return None
        order = db.get(Order, contract.order_id)
        if (not order or order.payment_status != 'paid' or order.delivery_status != 'accepted'
                or order.main_status != 'completed' or contract.delivery_method not in {'api', 'model_api', 'tenant_access'}
                or utc(contract.paid_at) > utc(contract.verified_at)):
            return None
        entitlement = ns['subscription_terms']['current_entitlement'](db, order.buyer_enterprise_id, order.product_id, at=at)
        if not entitlement or entitlement.get('order_id') != order.id or entitlement.get('delivery_method') != contract.delivery_method:
            return None
        term = db.get(Term, entitlement.get('term_id'))
        product = db.get(Product, order.product_id)
        if (not term or not product or term.order_id != order.id or term.status != 'active'
                or term.version_id != entitlement.get('version_id')):
            return None
        start = max(utc(contract.verified_at), utc(term.starts_at))
        end = min(utc(term.ends_at), utc(entitlement['expires_at']))
        if not start <= at < end:
            return None
        blocked = product.status == 'security_unpublished'
        source = None
        kind = 'saas_business' if contract.delivery_method == 'tenant_access' else 'api_gateway'
        entry_id = f'order:{order.id}'
        if kind == 'api_gateway':
            routes = list(db.scalars(select(Route).where(Route.product_id == order.product_id,
                Route.version == entitlement['version_code'])))
            if len(routes) == 1:
                entry_id = f'gateway:{routes[0].route_key}'
                if resolve_source and not blocked:
                    source = api_source(db, routes[0], order, entitlement)
        elif resolve_source and not blocked:
            resolver = ns.get('sla_saas_business_probe_source')
            candidate = resolver(db, order, entitlement) if callable(resolver) else None
            if (isinstance(candidate, SaaSProbeSource) and candidate.product_id == order.product_id
                    and candidate.version_id == entitlement['version_id']
                    and candidate.subscription_id and candidate.subscription_id == order.subscription_id
                    and candidate.evidence_id and candidate.config_version
                    and candidate.identity_id.startswith('sla-probe:') and callable(candidate.headers)):
                source = candidate
        def value(name, default=''):
            return source.get(name, default) if isinstance(source, dict) else getattr(source, name, default)
        target = ProbeTarget(product_id=order.product_id, version_id=entitlement['version_id'],
            entry_id=entry_id, service_from=start, service_until=end,
            publication_evidence_id=f'delivery:{contract.source_delivery_task_id}',
            eligibility_evidence_id=f'term:{term.id}', currently_published=product.status == 'published',
            active_sold_contract=True, contract_evidence_id=contract.id,
            url=value('url', None), method=value('method', 'HEAD'), timeout_seconds=5,
            entry_kind=kind, requires_identity=True, source_evidence_id=value('evidence_id'),
            config_version=value('config_version'))
        return target, source, blocked

    def resolve(db, contract_id, *, instant):
        detail = resolve_detail(db, contract_id, instant)
        return detail[0] if detail else None

    def headers(db, target):
        contract = db.get(Contract, target.contract_evidence_id)
        detail = resolve_detail(db, contract.id, ns['now']()) if contract else None
        if not detail or detail[2] or detail[0] != target or detail[1] is None:
            raise IdentitySourceMissing()
        source = detail[1]
        if isinstance(source, SaaSProbeSource):
            return source.headers(db, target)
        secret = secret_value()
        result = probe_headers(secret, route_id=source['route_id'], product_id=target.product_id,
            version_id=target.version_id, method=target.method, path=urlsplit(target.url).path, instant=ns['now']())
        result['X-API-Key'] = probe_consumer_key(secret, source['route_id'], target.product_id, target.version_id)
        return result

    ns['sla_probe_headers'] = headers

    def collect_one(db, contract_id, *, instant):
        detail = resolve_detail(db, contract_id, instant, resolve_source=False)
        if detail is None:
            return {'created': 0, 'skipped': 1}
        target = detail[0]
        existing = db.scalar(select(Slot.id).where(Slot.entry_id == target.entry_id,
            Slot.product_id == target.product_id, Slot.version_id == target.version_id,
            Slot.slot == int(utc(instant).timestamp()) // INTERVAL_SECONDS))
        if existing:
            return {'created': 0, 'duplicates': 1}
        # Avoid control-plane requests/consumer writes for already recorded slots.
        detail = resolve_detail(db, contract_id, instant)
        if detail is None:
            return {'created': 0, 'skipped': 1}
        target, _, blocked = detail
        result = collector['collect'](db, [target], instant=instant)
        if blocked and result['created']:
            # Security withdrawal is positive evidence of a blocked sold service,
            # not an excluded contract or a successful management health check.
            row = db.scalar(select(Slot).where(Slot.entry_id == target.entry_id,
                Slot.product_id == target.product_id, Slot.version_id == target.version_id,
                Slot.slot == int(utc(instant).timestamp()) // INTERVAL_SECONDS))
            result[row.status] -= 1
            row.status, row.reason_code = 'failed', 'security_withdrawal'
            row.responsibility = 'unknown'
            result['failed'] += 1
            db.flush()
        return result

    def slot_query(*, order_id, period_start, period_end):
        start, end = utc(period_start), utc(period_end)
        if start >= end:
            raise ValueError('increasing probe period required')
        # Pure SQL: no current_entitlement hook, resolver, network or provisioning.
        # Ambiguous terms/routes fail closed rather than selecting arbitrary data.
        term_count = select(func.count()).select_from(Term).where(Term.order_id == order_id,
            Term.status == 'active').correlate(None).scalar_subquery()
        route_count = select(Route.product_id.label('product_id'), Route.version.label('version'),
            func.count().label('count')).group_by(Route.product_id, Route.version).subquery()
        service = (select(Order.product_id.label('product_id'), Term.version_id.label('version_id'),
            Contract.id.label('contract_id'), Contract.delivery_method.label('method'),
            (literal('gateway:') + Route.route_key).label('shared_entry'),
            Contract.verified_at.label('verified_at'), Term.starts_at.label('starts_at'), Term.ends_at.label('ends_at'))
            .select_from(Order).join(Contract, Contract.order_id == Order.id).join(Term, Term.order_id == Order.id)
            .outerjoin(route_count, and_(route_count.c.product_id == Order.product_id, route_count.c.version == Term.version_code))
            .outerjoin(Route, and_(Route.product_id == Order.product_id, Route.version == Term.version_code))
            .where(Order.id == order_id, Term.status == 'active', Term.version_id == Order.product_version_id,
                term_count == 1, or_(route_count.c.count.is_(None), route_count.c.count == 1),
                Contract.delivery_eligible.is_(True), Contract.verified_at.is_not(None),
                Contract.source_delivery_task_id.is_not(None), Contract.source_delivery_task_id != '')).subquery()
        shared = and_(service.c.method.in_(['api', 'model_api']), Slot.entry_id == service.c.shared_entry,
            Slot.contract_evidence_id != '')
        legacy = and_(Slot.entry_id == f'order:{order_id}', Slot.contract_evidence_id == service.c.contract_id)
        ranked = (select(Slot.id.label('id'), func.row_number().over(partition_by=Slot.slot,
            order_by=(case((shared, 0), else_=1), Slot.id)).label('rank'))
            .select_from(Slot).join(service, and_(Slot.product_id == service.c.product_id, Slot.version_id == service.c.version_id))
            .where(or_(shared, legacy), Slot.scope == 'business_entry', Slot.scheduled_at >= start,
                Slot.scheduled_at < end, Slot.scheduled_at >= service.c.verified_at,
                Slot.scheduled_at >= service.c.starts_at, Slot.scheduled_at < service.c.ends_at)).subquery()
        return select(Slot).join(ranked, ranked.c.id == Slot.id).where(ranked.c.rank == 1).order_by(Slot.scheduled_at, Slot.id)

    def query(db, order, *, period_start, period_end):
        return slot_query(order_id=order.id, period_start=period_start, period_end=period_end)

    hooks = dict(Slot=Slot, Target=ProbeTarget, page=page, resolve=resolve,
        collect_one=collect_one, slot_query=slot_query, query=query, policy_version=POLICY_VERSION,
        verify_gateway_binding=default_binding)
    ns['sla_probe_runtime'] = hooks
    return hooks
