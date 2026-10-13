"""Owned SaaS post-CHANGE verification faults, with explicit response injection.

Remote CHANGE/STATUS and restoration are real mock-SaaS HTTP. Only returned
STATUS fields are corrupted within this acceptance process, not the daemon.
"""
import json
import secrets
from types import SimpleNamespace

from sqlalchemy import select

import version_change_saas_runtime_acceptance as base


def run_cases(m, ids):
    Change = m.version_change_saga['VersionChangeRequest']
    sub_id = ids['database_ids']['subscription_id']
    with m.SessionLocal() as db:
        persisted = db.get(Change, ids['database_ids']['changes'][0])
        sub = db.get(m.SaaSSubscription, sub_id)
        base.check(sub is not None and sub.product_id == ids['product'] and
            sub.enterprise_id == ids['enterprises']['buyer'] and persisted.status == 'completed',
            'Status-fault fixture ownership mismatch')
        old_id = sub.version_id
        old_code = db.get(m.SaaSProductVersion, old_id).version_code
        target_id, target_code = persisted.target_version_id, persisted.target_version_code
        frozen = json.loads(persisted.resource_plan_json)
        config_id, tenant = frozen['config_id'], sub.external_tenant_id
        protected = base.fingerprint(db, m)
        counts = {
            'changes': len(db.scalars(select(Change).where(Change.enterprise_id == ids['enterprises']['buyer'])).all()),
            'adjustments': len(db.scalars(select(m.VersionChangeAdjustment).where(
                m.VersionChangeAdjustment.change_id.in_(ids['database_ids']['changes']))).all()),
            'revisions': len(db.scalars(select(m.subscription_terms['Revision']).where(
                m.subscription_terms['Revision'].term_id.in_(ids['database_ids']['terms']))).all())}
    real_call = m.saas_call
    identity = {'tenant_id': tenant, 'product_id': ids['product'],
                'enterprise_id': ids['enterprises']['buyer'], 'subscription_id': sub_id}
    corruptions = {
        'wrong_tenant': lambda value: dict(value, tenant_id='foreign-tenant'),
        'wrong_product': lambda value: dict(value, product_id='foreign-product'),
        'wrong_enterprise': lambda value: dict(value, enterprise_id='foreign-enterprise'),
        'wrong_subscription': lambda value: dict(value, subscription_id='foreign-subscription'),
        'wrong_version': lambda value: dict(value, version='unrequested-version'),
        'admin_inactive': lambda value: dict(value, admin_active=False),
        'application_not_ready': lambda value: dict(value, application_ready=False),
        'wrong_oidc': lambda value: dict(value, oidc_client_id='foreign-oidc'),
        'wrong_expiry': lambda value: dict(value, expires_at='2099-01-01T00:00:00+00:00'),
    }
    results = []
    for label, corrupt in corruptions.items():
        attempt_id = secrets.token_hex(16)
        plan = dict(frozen, change_id=attempt_id)
        attempt = SimpleNamespace(id=attempt_id, old_version_id=old_id, old_version_code=old_code,
            target_version_id=target_id, target_version_code=target_code,
            resource_plan_json=json.dumps(plan), evidence_json='')
        injected = {'count': 0}
        def call(config, operation, payload):
            result = real_call(config, operation, payload)
            if (operation == 'STATUS' and config.id == config_id
                    and all(payload.get(key) == value for key, value in identity.items())):
                injected['count'] += 1
                return corrupt(result)
            return result
        try:
            m.saas_call = call
            with m.SessionLocal() as db:
                try:
                    m.version_change_resources['apply'](db, attempt, direction='target')
                except m.HTTPException as exc:
                    base.check(exc.status_code == 409 and injected['count'] == 1,
                               'Corrupted STATUS did not reject after one real verification response')
                else:
                    raise base.AcceptanceFailure('Corrupted STATUS unexpectedly authorized version activation')
                finally:
                    db.rollback()
            m.saas_call = real_call
            with m.SessionLocal() as db:
                sub = db.get(m.SaaSSubscription, sub_id)
                config = db.get(m.SaaSIntegrationConfig, config_id)
                remote = real_call(config, 'STATUS', identity)
                base.check(sub.version_id == old_id and remote.get('version') == target_code,
                    'Verification failure did not distinguish applied remote state from unchanged local state')
                proof = m.version_change_resources['apply'](db, attempt, direction='original')
                base.check(proof.get('request_change_id') == attempt.id and sub.version_id == old_id,
                           'Original SaaS resource restoration lacked identity-bound proof')
                db.commit()
                restored = real_call(config, 'STATUS', identity)
                base.check(restored.get('version') == old_code and restored.get('status') == 'active'
                    and all(restored.get(key) == value for key, value in identity.items()),
                    'Original remote SaaS version did not recover after verification rejection')
                base.check(base.fingerprint(db, m) == protected, 'Protected business order changed')
            results.append({'case': label, 'status': 'passed', 'rejection': 409,
                            'remote_target_observed': True, 'original_restored': True})
        finally:
            m.saas_call = real_call
    with m.SessionLocal() as db:
        base.check(len(db.scalars(select(Change).where(Change.enterprise_id == ids['enterprises']['buyer'])).all())
                   == counts['changes'], 'Direct STATUS fault created a persisted Saga')
        base.check(len(db.scalars(select(m.VersionChangeAdjustment).where(
            m.VersionChangeAdjustment.change_id.in_(ids['database_ids']['changes']))).all()) == counts['adjustments'],
            'Direct STATUS fault created duplicate ledger rows')
        base.check(len(db.scalars(select(m.subscription_terms['Revision']).where(
            m.subscription_terms['Revision'].term_id.in_(ids['database_ids']['terms']))).all()) == counts['revisions'],
            'Direct STATUS fault created duplicate term revisions')
    return {'status': 'passed', 'cases': results,
            'scope': 'production SaaS adapter; real mock HTTP with local STATUS field corruption',
            'daemon_worker_fault': False}
