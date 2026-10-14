"""Persistent, non-authoritative health observations for product integrations."""
import secrets
from datetime import datetime

import httpx
from fastapi import Depends, HTTPException
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import mapped_column


INTERVAL_SECONDS = 300
FAILURE_THRESHOLD = 3


class ProbeFailure(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code


def install(ns):
    Base = ns['Base']

    class HealthObservation(Base):
        __tablename__ = 'application_health_observations'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        product_id = mapped_column(ForeignKey('products.id'), nullable=False, index=True)
        version_id = mapped_column(String(36), nullable=False)
        last_slot = mapped_column(Integer, nullable=False)
        last_checked_at = mapped_column(DateTime(timezone=True), nullable=False)
        last_status = mapped_column(String(20), nullable=False)
        failure_streak = mapped_column(Integer, nullable=False, default=0)
        incident_active = mapped_column(Boolean, nullable=False, default=False)
        reason_code = mapped_column(String(80), nullable=False, default='')
        status_code = mapped_column(Integer, nullable=True)
        latency_ms = mapped_column(Integer, nullable=True)
        incident_started_at = mapped_column(DateTime(timezone=True), nullable=True)
        updated_at = mapped_column(DateTime(timezone=True), nullable=False)
        __table_args__ = (UniqueConstraint('product_id', 'version_id', name='uq_application_health_target'),)

    class HealthSlot(Base):
        __tablename__ = 'application_health_slots'
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        product_id = mapped_column(ForeignKey('products.id', ondelete='CASCADE'), nullable=False)
        version_id = mapped_column(String(36), nullable=False)
        slot = mapped_column(Integer, nullable=False)
        claimed_at = mapped_column(DateTime(timezone=True), nullable=False)
        completed_at = mapped_column(DateTime(timezone=True), nullable=True)
        status = mapped_column(String(20), nullable=False, default='claimed')
        status_code = mapped_column(Integer, nullable=True)
        latency_ms = mapped_column(Integer, nullable=True)
        reason_code = mapped_column(String(80), nullable=False, default='')
        __table_args__ = (UniqueConstraint('product_id', 'version_id', 'slot', name='uq_application_health_slot'),)

    def _reason(exc):
        if isinstance(exc, ProbeFailure):
            return 'upstream_unavailable'
        if isinstance(exc, HTTPException):
            return {400: 'invalid_configuration', 403: 'not_authorized', 404: 'configuration_missing',
                    409: 'configuration_not_ready', 502: 'upstream_unavailable',
                    503: 'probe_unavailable', 504: 'probe_timeout'}.get(exc.status_code, 'verification_failed')
        if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
            return 'probe_timeout'
        if isinstance(exc, ValueError):
            return 'address_policy_rejected'
        return 'probe_failed'

    def _notify(db, observation, product, recovered=False):
        phase = 'recovered' if recovered else 'incident'
        incident = observation.incident_started_at or observation.last_checked_at
        event = f'application-health:{product.id}:{observation.version_id}:{phase}:{incident:%Y%m%d%H%M%S}'
        ns['notify_business_event'](db,
            '产品接入健康检查恢复' if recovered else '产品接入健康检查异常',
            f'产品“{product.name}”版本接入健康检查' + ('已恢复。' if recovered else '连续失败三次，请检查服务状态。'),
            'product', product.id, event_key=event, category='system',
            tenant_id=product.enterprise_id, enterprise_ids=[product.enterprise_id],
            platform_roles=['super_admin', 'platform_operator'])

    def _targets(db):
        Product, Version = ns['Product'], ns['ProductReleaseVersion']
        for product in db.scalars(select(Product).where(Product.status.in_(['published', 'pending_integration']))):
            if product.product_type in {'api', 'model'} and product.delivery_method in {'api', 'model_api'}:
                for version in db.scalars(select(Version).where(Version.product_id == product.id, Version.status == 'active')):
                    yield product, version.id
            elif product.product_type == 'saas' and product.delivery_method == 'tenant_access':
                versions = db.scalars(select(ns['SaaSProductVersion'].id).where(
                    ns['SaaSProductVersion'].product_id == product.id,
                    ns['SaaSProductVersion'].status == 'active')).all()
                for version_id in versions or ['integration']:
                    yield product, version_id

    def _claim(db, product_id, version_id, slot, instant):
        row = HealthSlot(product_id=product_id, version_id=version_id, slot=slot, claimed_at=instant)
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
            return True
        except IntegrityError:
            return False

    def _probe(db, product, version_id, actor):
        if product.product_type == 'saas':
            first = product.status == 'pending_integration'
            result = ns['verify_saas_integration_service'](db, product, actor,
                publish=first, commit=False)
            code = result.get('health_check', {}).get('status_code') if first else result.get('status_code')
            return code, None
        hook = ns['version_gateway']
        config = db.scalar(select(hook['Config']).where(hook['Config'].version_id == version_id))
        version = db.get(ns['ProductReleaseVersion'], version_id)
        route = db.get(ns['ApiGatewayRoute'], config.route_id) if config and config.route_id else None
        fields = ('upstream_url', 'route_key', 'auth_mode', 'upstream_auth_mode', 'upstream_scope',
                  'timeout_ms', 'strip_prefix', 'health_method', 'health_path',
                  'rate_limit_per_minute', 'daily_quota', 'monthly_quota')
        if (not config or not route or not version or route.product_id != product.id
                or route.version != version.version_code or config.version_code != version.version_code
                or any(getattr(config, field) != getattr(route, field) for field in fields)):
            raise HTTPException(409, 'Version integration configuration is incomplete')
        if product.status == 'pending_integration' or config.status != 'verified':
            system_actor = ns['User'](id='application-health-scheduler', name='Health scheduler',
                password_hash='', platform_role='platform_operator', is_active=True,
                activation_status='active', verified_status='verified')
            verified = hook['verify_version'](db, product, version_id, system_actor)
            if verified.status != 'verified':
                if config.error_code == 'probe_timeout':
                    raise TimeoutError('Initial integration probe timed out')
                raise HTTPException(502, 'Initial integration verification failed')
            return config.health_status_code, None
        ns['application_address_policy']['validate_url'](config.upstream_url, db=db)
        request = ns.get('validated_http_request')
        if not callable(request):
            raise HTTPException(503, 'Validated HTTP adapter unavailable')
        headers = {}
        if route.upstream_auth_mode == 'oauth2':
            if not route.upstream_client_id or not route.upstream_client_secret:
                raise HTTPException(409, 'Upstream OAuth credentials are incomplete')
            try:
                token_response = ns['httpx'].post(ns['platform_oauth_token_url'](),
                    data={'grant_type': 'client_credentials', 'client_id': route.upstream_client_id,
                          'client_secret': route.upstream_client_secret,
                          'scope': route.upstream_scope or 'resource.invoke'},
                    timeout=5.0, follow_redirects=False)
                token_response.raise_for_status()
                token = token_response.json().get('access_token')
            except (httpx.HTTPError, ValueError):
                raise HTTPException(502, 'Upstream OAuth token acquisition failed') from None
            if not token:
                raise HTTPException(502, 'Upstream OAuth token response is invalid')
            headers['Authorization'] = 'Bearer ' + token
        result = request(config.health_method, config.upstream_url.rstrip('/') + config.health_path,
            timeout=min(config.timeout_ms / 1000, 5.0), follow_redirects=False, headers=headers)
        if not 200 <= result.status_code < 300:
            raise ProbeFailure(result.status_code)
        return result.status_code, None

    def run_once(db=None, *, instant=None):
        instant = instant or ns['now']()
        slot = int(instant.timestamp()) // INTERVAL_SECONDS
        owner = 'application-health-scheduler'
        own_session = db is None
        if own_session:
            db = ns['SessionLocal']()
        count = 0
        try:
            for product, version_id in _targets(db):
                if not _claim(db, product.id, version_id, slot, instant):
                    continue
                observation = db.scalar(select(HealthObservation).where(
                    HealthObservation.product_id == product.id,
                    HealthObservation.version_id == version_id).with_for_update())
                if not observation:
                    observation = HealthObservation(product_id=product.id, version_id=version_id,
                        last_slot=slot, last_checked_at=instant, last_status='unknown',
                        failure_streak=0, incident_active=False, reason_code='', updated_at=instant)
                    db.add(observation)
                    db.flush()
                slot_row = db.scalar(select(HealthSlot).where(HealthSlot.product_id == product.id,
                    HealthSlot.version_id == version_id, HealthSlot.slot == slot).with_for_update())
                try:
                    started = None
                    started = ns['time'].monotonic()
                    status_code, _ = _probe(db, product, version_id, owner)
                    latency = max(0, round((ns['time'].monotonic() - started) * 1000))
                    was_incident = observation.incident_active
                    if slot >= observation.last_slot:
                        observation.last_status, observation.reason_code = 'healthy', ''
                        observation.failure_streak = 0
                        observation.status_code = status_code
                        observation.latency_ms = latency
                        observation.incident_active = False
                        if was_incident:
                            _notify(db, observation, product, recovered=True)
                        observation.incident_started_at = None
                except Exception as exc:
                    latency = max(0, round((ns['time'].monotonic() - started) * 1000)) if started is not None else None
                    reason = _reason(exc)
                    code = getattr(exc, 'status_code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
                    if slot_row:
                        slot_row.status, slot_row.reason_code = 'failed', reason
                        slot_row.status_code, slot_row.latency_ms = code, latency
                        slot_row.completed_at = instant
                    if slot >= observation.last_slot:
                        observation.last_status, observation.reason_code = 'failed', reason
                        observation.status_code, observation.latency_ms = code, latency
                        observation.failure_streak += 1
                        if observation.failure_streak >= FAILURE_THRESHOLD and not observation.incident_active:
                            observation.incident_active = True
                            observation.incident_started_at = instant
                            _notify(db, observation, product)
                else:
                    if slot_row:
                        slot_row.status, slot_row.status_code = 'healthy', status_code
                        slot_row.latency_ms, slot_row.completed_at = latency, instant
                if slot >= observation.last_slot:
                    observation.last_slot, observation.last_checked_at = slot, instant
                    observation.updated_at = instant
                db.commit()
                count += 1
            db.execute(delete(HealthSlot).where(HealthSlot.completed_at.is_not(None),
                HealthSlot.completed_at < instant - ns['timedelta'](days=30)))
            db.commit()
            return count
        except Exception:
            db.rollback()
            raise
        finally:
            if own_session:
                db.close()

    def readiness(product_id, db, user):
        product = ns['Product']
        row = db.get(product, product_id)
        if not row:
            raise HTTPException(404, '产品不存在')
        allowed = user.platform_role in {'super_admin', 'platform_operator', 'product_manager',
            'quality_reviewer', 'security_compliance'}
        if not allowed:
            membership = db.scalar(select(ns['Membership']).where(
                ns['Membership'].user_id == user.id,
                ns['Membership'].enterprise_id == row.enterprise_id,
                ns['Membership'].status == 'active',
                ns['Membership'].role.in_(['super_admin', 'enterprise_admin'])))
            allowed = bool(membership and user.is_active and user.activation_status == 'active'
                           and user.verified_status == 'verified' and not user.platform_role)
        if not allowed:
            raise HTTPException(404, '产品不存在')
        enterprise = db.get(ns['Enterprise'], row.enterprise_id)
        if not enterprise or enterprise.verification_status != 'verified':
            raise HTTPException(404, '产品不存在')
        items = db.scalars(select(HealthObservation).where(
            HealthObservation.product_id == row.id).order_by(HealthObservation.version_id)).all()
        configured = {}
        configuration_errors = {}
        if row.product_type in {'api', 'model'}:
            Version = ns['ProductReleaseVersion']
            for version in db.scalars(select(Version).where(Version.product_id == row.id, Version.status == 'active')):
                config = db.scalar(select(ns['version_gateway']['Config']).where(
                    ns['version_gateway']['Config'].version_id == version.id))
                route = db.get(ns['ApiGatewayRoute'], config.route_id) if config and config.route_id else None
                ready = bool(config and route and config.status == 'verified'
                    and ns['version_gateway']['version_ready'](db, row, version))
                configured[version.id] = 'ready' if ready else (
                    'configuration_missing' if not config else 'configuration_not_ready')
                configuration_errors[version.id] = config.error_code if config and not ready else ''
        elif row.product_type == 'saas':
            config = db.scalar(select(ns['SaaSIntegrationConfig']).where(
                ns['SaaSIntegrationConfig'].product_id == row.id))
            config_state = 'ready' if (config and config.status == 'active'
                and config.verification_status == 'verified' and config.verified_at
                and config.health_status_code and 200 <= config.health_status_code < 300) else (
                'configuration_missing' if not config else 'configuration_not_ready')
            SaaSVersion = ns['SaaSProductVersion']
            version_ids = db.scalars(select(SaaSVersion.id).where(
                SaaSVersion.product_id == row.id, SaaSVersion.status == 'active')).all()
            for version_id in version_ids or ['integration']:
                configured[version_id] = config_state
        observed = {item.version_id: item for item in items}
        details = []
        for version_id, config_state in configured.items():
            item = observed.get(version_id)
            details.append({'version_id': version_id,
                'status': item.last_status if item else 'not_checked',
                'reason': (item.reason_code if item and item.reason_code else
                           config_state if config_state != 'ready' else 'not_checked' if not item else ''),
                'configuration': config_state,
                'configuration_reason': configuration_errors.get(version_id, ''),
                'consecutive_failures': item.failure_streak if item else 0,
                'incident_active': item.incident_active if item else False,
                'latency_ms': item.latency_ms if item else None,
                'last_checked_at': item.last_checked_at if item else None})
        history = db.scalars(select(HealthSlot).where(HealthSlot.product_id == row.id)
            .order_by(HealthSlot.slot.desc()).limit(100)).all()
        public_product = ns.get('storefront', {}).get('public_product')
        visible = row.status == 'published' and bool(public_product(db, row) if public_product
            else any(state == 'ready' for state in configured.values()))
        return {'product_id': row.id, 'visible': visible,
            'visibility_reason': '' if visible else ('product_not_published' if row.status != 'published'
                else 'configuration_not_ready'), 'items': [
            {'version_id': item.version_id, 'status': item.last_status,
             'reason': item.reason_code, 'consecutive_failures': item.failure_streak,
             'incident_active': item.incident_active,
             'last_checked_at': item.last_checked_at, 'latency_ms': item.latency_ms} for item in items],
            'readiness': details,
            'history': [{'version_id': h.version_id, 'slot': h.slot, 'status': h.status,
                'status_code': h.status_code, 'latency_ms': h.latency_ms,
                'reason': h.reason_code, 'checked_at': h.completed_at or h.claimed_at} for h in history]}

    @ns['app'].get('/api/products/{product_id}/integration-readiness')
    def integration_readiness(product_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        return readiness(product_id, db, user)

    ns['application_health'] = {'Observation': HealthObservation, 'Slot': HealthSlot,
        'run_once': run_once, 'readiness': readiness, 'claim': _claim}
    return ns['application_health']
