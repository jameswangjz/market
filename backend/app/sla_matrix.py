"""Immutable matrix revisions. All writes share the service-level row lock."""
import json
from math import isfinite
from pathlib import Path

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .enterprise_sla import LEVELS, MATRIX_VERSION, _promise, _iso


DISPLAY_ROWS = json.loads(Path(__file__).with_name('sla_matrix_metadata.json').read_text(encoding='utf-8'))
DISPLAY_METRICS = (None, None, 'first_response', 'urgent_first_response', 'resolution',
    'urgent_resolution', 'rectification', 'intervention_response', 'urgent_intervention_response',
    'intervention_review', 'notification', 'availability', 'request_error_rate', 'latency_p95',
    'delivery_timeliness', 'delivery')


def display_metadata(base_code):
    prefix = dict(standard='STD', enterprise='ENT', strategic='STR')[base_code]
    rows = [row for row in DISPLAY_ROWS if row['指标编号'].startswith(prefix + '-')]
    return {'source': 'SLA级别指标配置矩阵-提前预警修正版-20261014.csv',
        'notes_status': 'original_csv_notes',
        'confirmed_policy': {'intervention_review_pause_states': [],
            'delivery_cohort': 'frozen_deadline_calendar_month',
            'availability_source': 'five_minute_business_entry_probes',
            'latency_source': 'real_request_p95'},
        'base_code': base_code, 'rows': [dict(row, metric_key=metric)
                                       for row, metric in zip(rows, DISPLAY_METRICS)],
        'sample_rules': {'availability': {'min_samples': 100, 'min_coverage_percent': 90},
                         'request_error_rate': {'min_samples': 100},
                         'latency_p95': {'min_samples': 100, 'min_completeness_percent': 95},
                         'delivery_timeliness': {'min_samples': 10}}}


class ThresholdEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: float = Field(strict=True, allow_inf_nan=False)
    warning: float = Field(strict=True, allow_inf_nan=False)


class MatrixEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0, strict=True)
    metrics: dict[str, ThresholdEdit] = Field(min_length=1)


def install(ns):
    Level, Revision = ns['ServiceLevel'], ns['SLAMatrixVersion']
    app, current_user, session = ns['app'], ns['current_user'], ns['db_session']

    def latest(db, level_id):
        return db.scalar(select(Revision).where(Revision.service_level_id == level_id)
            .order_by(Revision.version.desc()).limit(1).execution_options(populate_existing=True))

    def matrix(db, level):
        revision = latest(db, level.id)
        if revision:
            return json.loads(revision.rules_json), revision.version, revision.base_code
        if level.code not in LEVELS:
            raise HTTPException(409, 'Service level has no complete matrix')
        metrics, calendar = _promise(level.code)
        # Preserve pre-matrix manual response settings without mutating them.
        from .enterprise_sla import response_configuration
        response = response_configuration(level)
        for metric, field in (('first_response', 'initial_response_minutes'),
                              ('urgent_first_response', 'urgent_initial_response_minutes')):
            target = response[field]
            if target is not None:
                metrics[metric].update(target=target, warning=target * 0.8,
                    target_seconds=target * 60, warning_seconds=target * 48)
        return {'metrics': metrics, 'calendar': calendar}, 0, level.code

    def output(db, level):
        rules, version, base = matrix(db, level)
        revision = latest(db, level.id)
        return {'service_level_id': level.id, 'code': level.code, 'name': level.name,
            'status': level.status, 'version': version, 'base_code': base,
            'matrix_version': revision.id if revision else MATRIX_VERSION,
            **rules, 'editable_fields': ['target', 'warning'],
            'display_metadata': display_metadata(base),
            'created_by': revision.created_by if revision else level.created_by,
            'created_at': _iso(revision.created_at) if revision else _iso(level.created_at)}

    def append(db, level, rules, base, actor, version):
        row = Revision(service_level_id=level.id, version=version, base_code=base,
            rules_json=json.dumps(rules, sort_keys=True, allow_nan=False), created_by=actor)
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, 'Matrix version changed; reload before saving')
        return row

    def initialize(db):
        # Called only from schema/seed initialization, never from a GET request.
        for code in LEVELS:
            level = db.scalar(select(Level).where(Level.code == code).with_for_update()
                .execution_options(populate_existing=True))
            if level is not None and latest(db, level.id) is None:
                rules, _, base = matrix(db, level)
                append(db, level, rules, base, 'system:matrix-baseline', 0)

    def copy_matrix(db, level, base_code, actor):
        if base_code not in LEVELS:
            raise HTTPException(400, 'base_code must be standard, enterprise or strategic')
        parent = db.scalar(select(Level).where(Level.code == base_code).with_for_update()
            .execution_options(populate_existing=True))
        if parent is None:
            raise HTTPException(409, 'Source service level is unavailable')
        rules, _, _ = matrix(db, parent)
        append(db, level, rules, base_code, actor, 1)

    def locked(db, level_id):
        level = db.scalar(select(Level).where(Level.id == level_id).with_for_update()
            .execution_options(populate_existing=True))
        if level is None:
            raise HTTPException(404, 'Service level not found')
        return level

    @app.get('/api/sla/matrices')
    def matrices(user=Depends(current_user), db=Depends(session)):
        ns['require_platform_admin'](user)
        levels = list(db.scalars(select(Level)))
        levels.sort(key=lambda row: (LEVELS.index(row.code) if row.code in LEVELS else 3, row.code, row.id))
        return {'items': [output(db, level) for level in levels
                         if level.code in LEVELS or latest(db, level.id) is not None]}

    @app.get('/api/sla/service-levels/{level_id}/matrix')
    def get_matrix(level_id: str, user=Depends(current_user), db=Depends(session)):
        ns['require_platform_admin'](user)
        level = db.get(Level, level_id)
        if level is None:
            raise HTTPException(404, 'Service level not found')
        return {'item': output(db, level)}

    @app.get('/api/sla/service-levels/{level_id}/matrix/versions')
    def versions(level_id: str, user=Depends(current_user), db=Depends(session)):
        ns['require_platform_admin'](user)
        return {'items': [{'id': row.id, 'version': row.version, 'base_code': row.base_code,
            **json.loads(row.rules_json), 'created_by': row.created_by, 'created_at': _iso(row.created_at)}
            for row in db.scalars(select(Revision).where(Revision.service_level_id == level_id)
                .order_by(Revision.version.desc()))]}

    @app.patch('/api/sla/service-levels/{level_id}/matrix')
    def update_matrix(level_id: str, body: MatrixEdit, user=Depends(current_user), db=Depends(session)):
        ns['require_platform_admin'](user)
        level = locked(db, level_id)
        rules, version, base = matrix(db, level)
        if version != body.expected_version:
            raise HTTPException(409, 'Matrix version changed; reload before saving')
        before = json.loads(json.dumps(rules))
        for name, edit in body.metrics.items():
            if name not in rules['metrics']:
                raise HTTPException(422, 'Unknown metric: ' + name)
            rule = rules['metrics'][name]
            target, warning = edit.target, edit.warning
            percent = rule['unit'] == 'percent'
            if (not all(isfinite(value) and (0 <= value <= 100 if percent else value > 0)
                        for value in (target, warning))
                    or rule['direction'] == 'upper' and warning > target
                    or rule['direction'] == 'lower' and warning < target):
                raise HTTPException(422, 'Invalid range or warning direction: ' + name)
            rule.update(target=target, warning=warning)
            if rule['unit'] == 'minutes':
                if not target.is_integer():
                    raise HTTPException(422, 'Minute targets must be positive integers')
                if not all(isfinite(value * 60) for value in (target, warning)):
                    raise HTTPException(422, 'Derived seconds overflow')
                rule.update(target_seconds=target * 60, warning_seconds=warning * 60)
        actor = user.email or user.id
        # Persist the baseline too, so the first edit has a reviewable predecessor.
        if version == 0 and latest(db, level.id) is None:
            append(db, level, before, base, actor, 0)
        row = append(db, level, rules, base, actor, version + 1)
        ns['audit'](db, actor, 'update_sla_matrix', 'sla_matrix_version', row.id,
            'Update future frozen SLA promises', category='sla', business_domain='service_level',
            before={'version': version, **before}, after={'version': row.version, **rules})
        db.commit()
        return {'item': output(db, level)}

    ns['sla_matrix'] = dict(matrix=matrix, latest=latest, output=output,
                           copy_matrix=copy_matrix, initialize=initialize)
    return ns['sla_matrix']
