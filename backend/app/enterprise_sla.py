"""Enterprise SLA assignments and detached promises for newly created tickets."""
from datetime import timezone
from decimal import Decimal
import hashlib
import json

from fastapi import Depends, HTTPException, Query
from sqlalchemy import func, or_, select


MATRIX_VERSION = 'sla-warning-matrix-20261014'
# Final accepted CSV columns: standard / enterprise / strategic, in minutes.
TIME_RULES = {
    'first_response': ('provider', 'support', (2880, 1440, 720), (2304, 1152, 576)),
    'urgent_first_response': ('provider', 'natural', (1440, 720, 360), (1152, 576, 288)),
    'resolution': ('provider', 'support', (4320, 2160, 1080), (3456, 1728, 864)),
    'urgent_resolution': ('provider', 'support', (2880, 1440, 720), (2304, 1152, 576)),
    'rectification': ('provider', 'support', (1440, 720, 360), (1152, 576, 288)),
    'intervention_response': ('platform', 'support', (1440, 720, 360), (1152, 576, 288)),
    'urgent_intervention_response': ('platform', 'natural', (720, 360, 180), (576, 288, 144)),
    'intervention_review': ('platform', 'support', (1440, 720, 360), (1152, 576, 288)),
    'notification': ('platform', 'natural', (720, 360, 180), (576, 288, 144)),
    'delivery': ('delivery', 'natural', (10, 5, 4), (8, 4, 3.2)),
}
QUALITY_RULES = {
    'availability': ('percent', 'lower', (90, 95, 98), (92, 96, 98.4)),
    'request_error_rate': ('percent', 'upper', (10, 5, 3), (8, 4, 2.4)),
    'latency_p95': ('milliseconds', 'upper', (5000, 4000, 3000), (4000, 3200, 2400)),
    'delivery_timeliness': ('percent', 'lower', (90, 95, 98), (92, 96, 98.4)),
}
LEVELS = ('standard', 'enterprise', 'strategic')
LEGACY_STRATEGIC_DESCRIPTION = '面向核心合作伙伴和重要客户，提供 7×24 专属支持、15 分钟紧急响应、季度报告和年度优化建议。'
STRATEGIC_DESCRIPTION = '面向核心合作伙伴和重要客户，提供7×24专属支持、普通首次响应720分钟（支持时间）、紧急首次响应360分钟（自然时间）、季度报告和年度优化建议。'


def response_defaults(code):
    if code not in LEVELS:
        return 2880, None
    index = LEVELS.index(code)
    return TIME_RULES['first_response'][2][index], TIME_RULES['urgent_first_response'][2][index]


def is_legacy_strategic_seed(level):
    fingerprint = {'code': 'strategic', 'name': '战略级', 'description': LEGACY_STRATEGIC_DESCRIPTION,
                   'customer_scope': 'strategic', 'support_days_per_week': 7, 'support_hours_per_day': 24,
                   'initial_response_minutes': 15, 'problem_response_hours': 24, 'created_by': 'system',
                   'status': 'active', 'online_docs': True, 'knowledge_base': True, 'standard_api': True,
                   'online_customer_service': True, 'dedicated_manager': True, 'technical_support': True,
                   'quarterly_report': True, 'annual_optimization': True}
    return (all(getattr(level, key) == value for key, value in fingerprint.items())
            and getattr(level, 'urgent_initial_response_minutes', None) in {None, 360})


def response_configuration(level):
    normal_default, urgent_default = response_defaults(level.code)
    legacy_seed = is_legacy_strategic_seed(level)
    urgent = getattr(level, 'urgent_initial_response_minutes', None)
    return {'initial_response_minutes': normal_default if legacy_seed else level.initial_response_minutes,
            'urgent_initial_response_minutes': urgent if urgent is not None else urgent_default,
            'first_response_clock': 'support', 'urgent_first_response_clock': 'natural',
            'legacy_seed_migration_required': legacy_seed}


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value):
    return _utc(value).isoformat() if value is not None else None


def _promise(code):
    index = LEVELS.index(code)
    metrics = {}
    for key, (party, clock, targets, warnings) in TIME_RULES.items():
        pause = ('waiting_buyer_material', 'waiting_buyer_confirmation') if key in {
            'resolution', 'urgent_resolution'} else ('waiting_buyer_material',) if key == 'rectification' else ()
        metrics[key] = {'unit': 'minutes', 'target': targets[index], 'warning': warnings[index],
                        'target_seconds': float(Decimal(str(targets[index])) * 60),
                        'warning_seconds': float(Decimal(str(warnings[index])) * 60),
                        'direction': 'upper', 'responsibility': party, 'clock': clock,
                        'pause_states': list(pause)}
    for key, (unit, direction, targets, warnings) in QUALITY_RULES.items():
        metrics[key] = {'unit': unit, 'target': targets[index], 'warning': warnings[index],
                        'direction': direction, 'responsibility': 'delivery' if key == 'delivery_timeliness'
                        else 'provider', 'clock': 'natural'}
    calendar = {'version': MATRIX_VERSION, 'timezone': 'Asia/Shanghai', 'weekday_numbering': 'python',
                'weekdays': list(range(5 if index == 0 else 7)),
                'windows': [['00:00', '24:00'] if index == 2 else ['09:00', '17:00']],
                'holidays': [], 'workdays': [], 'covered_years': [],
                'support_days_per_week': 5 if index == 0 else 7,
                'support_hours_per_day': 24 if index == 2 else 8,
                'exclude_public_holidays': index != 2, 'holiday_calendar_version': None,
                'holiday_calendar_status': 'unconfigured',
                'period': 'calendar_month'}
    return metrics, calendar


def install(ns):
    Enterprise, Level, Assignment = (ns[key] for key in ('Enterprise', 'ServiceLevel', 'ServiceLevelAssignment'))

    def require_manager(user):
        ns['require_platform_admin'](user)

    def locked_enterprise(db, enterprise_id):
        enterprise = db.scalar(select(Enterprise).where(Enterprise.id == enterprise_id)
            .with_for_update().execution_options(populate_existing=True))
        if enterprise is None:
            raise HTTPException(404, '企业不存在')
        return enterprise

    def assignment_value(item):
        return {'id': item.id, 'service_level_id': item.service_level_id,
                'effective_at': _iso(item.effective_at), 'expires_at': _iso(item.expires_at),
                'source': item.source, 'assigned_by': item.created_by}

    def latest_assignment(db, enterprise_id, instant):
        return db.scalar(select(Assignment).where(Assignment.enterprise_id == enterprise_id,
            Assignment.user_id == '', Assignment.effective_at <= instant)
            .order_by(Assignment.effective_at.desc(), Assignment.created_at.desc(), Assignment.id.desc())
            .limit(1)
            .execution_options(populate_existing=True))

    def current_assignment(db, enterprise_id, instant):
        item = latest_assignment(db, enterprise_id, instant)
        return item if item is not None and (item.expires_at is None or _utc(item.expires_at) > _utc(instant)) else None

    def audit_expiry(db, enterprise_id, instant):
        item = latest_assignment(db, enterprise_id, instant)
        Marker = ns['SLAExpiryFallback']
        if (item is None or item.expires_at is None or _utc(item.expires_at) > _utc(instant)
                or db.get(Marker, item.id) is not None):
            return False
        db.add(Marker(assignment_id=item.id))
        ns['audit'](db, 'system', 'expire_service_level_assignment', 'service_level_assignment', item.id,
            'Expired SLA binding; new promises use standard', category='sla', business_domain='service_level',
            tenant_id=enterprise_id, before={'assignment': assignment_value(item)},
            after={'service_level_code': 'standard', 'event_key': 'sla-expiry:' + item.id})
        db.flush()
        return True

    def frozen_rules(level, db=None):
        code = level.code if level is not None else 'standard'
        matrix_version = MATRIX_VERSION
        matrix = ns.get('sla_matrix')
        if matrix and db is not None and level is not None:
            rules, _, _ = matrix['matrix'](db, level)
            metrics, calendar = rules['metrics'], rules['calendar']
            revision = matrix['latest'](db, level.id)
            if revision:
                matrix_version = revision.id
        else:
            metrics, calendar = _promise(code)
        if level is not None and not (matrix and db is not None):
            response = response_configuration(level)
            for key, field in (('first_response', 'initial_response_minutes'),
                               ('urgent_first_response', 'urgent_initial_response_minutes')):
                target = response[field]
                if target is not None:
                    warning = float(Decimal(str(target)) * Decimal('0.8'))
                    metrics[key].update(target=target, warning=warning,
                        target_seconds=float(Decimal(str(target)) * 60),
                        warning_seconds=float(Decimal(str(warning)) * 60))
        legacy = ns['service_level_out'](level) if level is not None else None
        if legacy is not None:
            legacy = {**legacy, 'initial_response_minutes': level.initial_response_minutes,
                      'description': level.description}
        # JSON round trip detaches ORM data and preserves original legacy units.
        if legacy is not None:
            legacy = json.loads(json.dumps(legacy, default=lambda value: _iso(value)))
        rules = {'matrix_version': matrix_version, 'service_level_code': code,
                 'calendar': calendar, 'metrics': metrics, 'legacy_level_config': legacy}
        contract_version = hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if db is not None and ns.get('enrich_support_calendar'):
            rules['calendar'] = ns['enrich_support_calendar'](db, calendar)
        calendar_hash = hashlib.sha256(json.dumps(rules['calendar'], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        version = hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'rule_version': version, 'contract_rule_version': contract_version,
                'calendar_content_hash': calendar_hash, **rules}

    def freeze_sla_snapshot(db, enterprise_id):
        enterprise = locked_enterprise(db, enterprise_id)
        if enterprise.verification_status != 'verified':
            raise HTTPException(409, '企业尚未认证，不能冻结SLA承诺')
        instant = ns['now']()
        # Observe assignments added earlier in the caller's transaction.
        db.flush()
        audit_expiry(db, enterprise_id, instant)
        assignment = current_assignment(db, enterprise_id, instant)
        level = db.get(Level, assignment.service_level_id, populate_existing=True) if assignment else db.scalar(
            select(Level).where(Level.code == 'standard').execution_options(populate_existing=True))
        if level is not None and level.status != 'active':
            raise HTTPException(409, '当前服务级别未启用或无已确认SLA矩阵')
        return {'schema_version': 1, **frozen_rules(level, db),
                'enterprise_id': enterprise_id, 'service_level_id': level.id if level is not None else None,
                'assignment': assignment_value(assignment) if assignment is not None else None,
                'frozen_at': _iso(instant)}

    def bind(db, user, enterprise, level_id, reason, *, source, expires_at=None):
        require_manager(user)
        level = db.scalar(select(Level).where(Level.id == level_id).with_for_update()
            .execution_options(populate_existing=True))
        if level is None or level.status != 'active':
            raise HTTPException(404, '服务级别不存在或未启用')
        if level.code not in LEVELS and (not ns.get('sla_matrix') or ns['sla_matrix']['latest'](db, level.id) is None):
            raise HTTPException(409, '服务级别尚无已确认SLA矩阵')
        instant = ns['now']()
        audit_expiry(db, enterprise.id, instant)
        if expires_at is not None:
            expires_at = _utc(expires_at)
            if expires_at <= _utc(instant):
                raise HTTPException(400, 'SLA等级到期时间必须晚于生效时间')
        current = db.scalars(select(Assignment).where(Assignment.enterprise_id == enterprise.id,
            Assignment.user_id == '', or_(Assignment.expires_at.is_(None), Assignment.expires_at > instant))
            .order_by(Assignment.effective_at, Assignment.id).with_for_update()
            .execution_options(populate_existing=True)).all()
        before = [assignment_value(item) for item in current]
        for item in current:
            # Cancel any legacy future assignment with an empty validity window.
            item.expires_at = max(_utc(item.effective_at), _utc(instant))
        actor = user.email or user.phone or user.id
        item = Assignment(service_level_id=level.id, enterprise_id=enterprise.id, user_id='',
            source=source, effective_at=instant, expires_at=expires_at, created_by=actor)
        db.add(item)
        db.flush()
        ns['audit'](db, actor, 'assign_service_level', 'service_level_assignment', item.id, reason,
            category='sla', business_domain='service_level', tenant_id=enterprise.id,
            before={'assignments': before}, after={'assignment': assignment_value(item),
                'reason': reason, 'actor_role': user.platform_role, 'matrix_version': MATRIX_VERSION,
                'rule_version': frozen_rules(level, db)['rule_version']})
        return item, level

    def adjust_assignment(db, user, body):
        require_manager(user)
        enterprise = locked_enterprise(db, body.enterprise_id)
        if enterprise.verification_status != 'verified':
            raise HTTPException(409, '仅已认证企业可调整SLA等级')
        if body.user_id:
            raise HTTPException(400, 'SLA等级仅绑定企业，不接受个人覆盖')
        reason = body.reason.strip()
        if not reason:
            raise HTTPException(400, '调整企业SLA等级必须填写原因')
        item, level = bind(db, user, enterprise, body.service_level_id, reason,
                           source='manual', expires_at=body.expires_at)
        return item, level, enterprise

    def review_binding(db, user, enterprise, decision, service_level_id='', reason=''):
        require_manager(user)
        if decision != 'approve' or not service_level_id:
            return None
        reason = reason.strip()
        if enterprise.verification_status == 'verified' and not reason:
            raise HTTPException(400, '已认证企业调整SLA等级必须填写原因')
        return bind(db, user, enterprise, service_level_id,
                    reason or 'Enterprise verification approval', source='verification')

    ns['freeze_sla_snapshot'] = freeze_sla_snapshot
    def expire_due(db):
        instant = ns['now']()
        count = 0
        ids = db.scalars(select(Assignment.enterprise_id).where(Assignment.user_id == '',
            Assignment.expires_at <= instant).distinct().order_by(Assignment.enterprise_id)).all()
        for enterprise_id in ids:
            locked_enterprise(db, enterprise_id)
            count += int(audit_expiry(db, enterprise_id, instant))
        return count

    hooks = {'freeze_sla_snapshot': freeze_sla_snapshot, 'adjust_assignment': adjust_assignment,
             'review_binding': review_binding, 'current_assignment': current_assignment,
             'frozen_rules': frozen_rules, 'audit_expiry': audit_expiry, 'expire_due': expire_due}

    @ns['app'].get('/api/sla/enterprise-bindings')
    def bindings(q: str = '', service_level_id: str = '', assigned_by: str = '',
                 expires_before: str = '', expires_after: str = '',
                 page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
                 user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        require_manager(user)
        from datetime import datetime
        bounds = []
        for raw in (expires_after, expires_before):
            try:
                value = datetime.fromisoformat(raw.replace('Z', '+00:00')) if raw else None
                if value is not None and value.tzinfo is None:
                    raise ValueError('Timezone required')
                bounds.append(value)
            except ValueError:
                raise HTTPException(422, 'Expiry filters require ISO timestamps with timezone')
        instant = ns['now']()
        ranked = select(Assignment.id.label('assignment_id'), Assignment.enterprise_id,
            func.row_number().over(partition_by=Assignment.enterprise_id, order_by=(
                Assignment.effective_at.desc(), Assignment.created_at.desc(), Assignment.id.desc())).label('rank')).where(
                    Assignment.user_id == '', Assignment.effective_at <= instant).subquery()
        current = select(Assignment).join(ranked, ranked.c.assignment_id == Assignment.id).where(
            ranked.c.rank == 1, or_(Assignment.expires_at.is_(None), Assignment.expires_at > instant)).subquery()
        standard = db.scalar(select(Level.id).where(Level.code == 'standard'))
        effective_id = func.coalesce(current.c.service_level_id, standard)
        query = select(Enterprise, current.c.id, effective_id.label('level_id'),
            current.c.effective_at, current.c.expires_at, current.c.created_by, current.c.source).outerjoin(
                current, current.c.enterprise_id == Enterprise.id)
        if q:
            query = query.where(Enterprise.name.icontains(q, autoescape=True))
        if service_level_id:
            query = query.where(effective_id == service_level_id)
        if assigned_by:
            query = query.where(current.c.created_by.icontains(assigned_by, autoescape=True))
        if bounds[0]:
            query = query.where(current.c.expires_at >= bounds[0])
        if bounds[1]:
            query = query.where(current.c.expires_at <= bounds[1])
        total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        rows = db.execute(query.order_by(Enterprise.name, Enterprise.id).offset((page - 1) * page_size).limit(page_size)).all()
        return {'items': [{'enterprise_id': row[0].id, 'enterprise_name': row[0].name,
            'verification_status': row[0].verification_status, 'assignment_id': row[1],
            'service_level_id': row[2], 'service_level': ns['service_level_out'](db.get(Level, row[2])) if row[2] else None,
            'effective_at': _iso(row[3]), 'expires_at': _iso(row[4]),
            'assigned_by': row[5] or '', 'source': row[6] or 'default'} for row in rows],
            'total': total, 'page': page, 'page_size': page_size, 'pages': pages}

    @ns['app'].post('/api/sla/expire-bindings')
    def expire_bindings(user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        require_manager(user)
        count = expire_due(db)
        db.commit()
        return {'audited': count}
    ns['enterprise_sla'] = hooks
    return hooks
