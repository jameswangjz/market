"""Bounded live auth-fault acceptance for the owned mock-SaaS version change."""
import argparse
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import secrets
import traceback
from types import SimpleNamespace

HELPER = Path(__file__).with_name('version_change_saas_runtime_acceptance.py')
SPEC = importlib.util.spec_from_file_location('version_change_saas_runtime_acceptance', HELPER)
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)

STAGE = 'startup'


def cleanup_auth_children(m, ids):
    from sqlalchemy import delete, select
    with m.SessionLocal() as db:
        row = db.scalar(select(m.OAuthClient).where(
            m.OAuthClient.client_id == ids['oauth_client_id'],
            m.OAuthClient.product_id == ids['product']))
        if row is None:
            return
        client_row_id = row.id
        revoked = db.scalars(select(m.OAuthRevokedToken).where(
            m.OAuthRevokedToken.client_id == client_row_id)).all()
        hashes = [item.token_hash for item in revoked]
        audits = db.scalars(select(m.AuditLog).where(
            m.AuditLog.actor == ids['oauth_client_id'],
            m.AuditLog.action == 'revoke_oauth_token',
            m.AuditLog.target_type == 'oauth_client',
            m.AuditLog.target_id == client_row_id)).all()
        audit_ids = [item.id for item in audits]
        if hashes:
            db.execute(delete(m.OAuthRevokedToken).where(
                m.OAuthRevokedToken.client_id == client_row_id,
                m.OAuthRevokedToken.token_hash.in_(hashes)))
        if audit_ids:
            db.execute(delete(m.AuditLog).where(m.AuditLog.id.in_(audit_ids),
                m.AuditLog.actor == ids['oauth_client_id'],
                m.AuditLog.action == 'revoke_oauth_token',
                m.AuditLog.target_type == 'oauth_client', m.AuditLog.target_id == client_row_id))
        db.commit()
        base.check(db.scalar(select(m.OAuthRevokedToken.token_hash).where(
            m.OAuthRevokedToken.client_id == client_row_id)) is None and
            db.scalar(select(m.AuditLog.id).where(m.AuditLog.actor == ids['oauth_client_id'],
                m.AuditLog.action == 'revoke_oauth_token', m.AuditLog.target_type == 'oauth_client',
                m.AuditLog.target_id == client_row_id)) is None,
            'Exact generated OAuth revocation children remain after cleanup')


def restore_owned_auth(m, ids):
    from sqlalchemy import select
    with m.SessionLocal() as db:
        oauth = db.scalar(select(m.OAuthClient).where(
            m.OAuthClient.client_id == ids['oauth_client_id'],
            m.OAuthClient.product_id == ids['product']))
        if oauth is None:
            return
        config = db.scalar(select(m.SaaSIntegrationConfig).where(
            m.SaaSIntegrationConfig.product_id == ids['product']))
        subscription_id = ids['database_ids'].get('subscription_id')
        base.check(config is not None and subscription_id, 'Owned auth recovery identity is incomplete')
        subscription = db.get(m.SaaSSubscription, subscription_id)
        base.check(subscription is not None and subscription.product_id == ids['product'] and
              subscription.enterprise_id == ids['enterprises']['buyer'],
              'Owned auth recovery subscription identity changed')
        operations = db.scalars(select(m.SaaSOperation).where(
            m.SaaSOperation.subscription_id == subscription_id,
            m.SaaSOperation.operation == 'USER_ASSIGN',
            m.SaaSOperation.status == 'succeeded').order_by(m.SaaSOperation.created_at.desc())).all()
        external_ids = set()
        for operation in operations:
            try:
                response = json.loads(operation.response_payload or '{}')
            except (TypeError, ValueError):
                continue
            external_id = response.get('external_user_id')
            if external_id:
                external_ids.add(external_id)
        base.check(len(external_ids) == 1,
              'Successful owned USER_ASSIGN evidence does not identify one external admin')
        mapping = db.scalar(select(m.SaaSUserMapping).where(
            m.SaaSUserMapping.subscription_id == subscription_id,
            m.SaaSUserMapping.user_id == ids['users']['buyer']))
        oidc = db.scalar(select(m.OIDCClient).where(
            m.OIDCClient.product_id == ids['product'],
            m.OIDCClient.client_id == ids['external']['oidc_client_id']))
        base.check(mapping is not None and oidc is not None,
              'Owned auth recovery mapping or OIDC client is missing')
        oauth.status = 'active'
        config.client_id = oauth.client_id
        config.client_secret = oauth.client_secret
        mapping.external_user_id = next(iter(external_ids))
        mapping.status = 'active'
        oidc.status = 'active'
        db.commit()


def run(manifest=None):
    global STAGE
    import httpx
    from sqlalchemy import select
    m = base.load_main()
    ids = base.fixture_ids()
    path = Path(manifest or ('/tmp/' + ids['run_id'] + '-auth.json'))
    STAGE = 'preflight'
    base.preflight(m, ids)
    ids['authorized'] = True
    def writer(value, create=False):
        base.write_manifest(path, value, create=create)
    ids['_manifest_writer'] = writer
    writer(ids, create=True)
    client = None
    try:
        from fastapi.testclient import TestClient
        STAGE = 'fixture-setup'
        tokens, secret = base.prepare_fixture(m, ids)
        writer(ids)
        base.register_mock_client(ids, secret)
        client = TestClient(m.app)
        STAGE = 'integration-verification'
        base.request(client, tokens['provider'], 'POST',
            f"/products/{ids['product']}/saas-integration/verify")
        STAGE = 'baseline-upgrade-downgrade'
        baseline = base.worker_acceptance(m, ids, path, client, tokens)

        with m.SessionLocal() as db:
            Change = m.version_change_saga['VersionChangeRequest']
            change = db.get(Change, ids['database_ids']['changes'][0])
            sub = db.get(m.SaaSSubscription, ids['database_ids']['subscription_id'])
            config = db.scalar(select(m.SaaSIntegrationConfig).where(
                m.SaaSIntegrationConfig.product_id == ids['product']))
            mapping = db.scalar(select(m.SaaSUserMapping).where(
                m.SaaSUserMapping.subscription_id == sub.id,
                m.SaaSUserMapping.user_id == ids['users']['buyer'],
                m.SaaSUserMapping.status == 'active'))
            oidc_client = db.scalar(select(m.OIDCClient).where(
                m.OIDCClient.client_id == ids['external']['oidc_client_id'],
                m.OIDCClient.product_id == ids['product']))
            oauth = db.scalar(select(m.OAuthClient).where(
                m.OAuthClient.client_id == config.client_id,
                m.OAuthClient.product_id == ids['product']))
            check = base.check
            check(change is not None and change.status == 'completed' and change.resource_applied,
                  'Owned upgrade change is not completed with applied resources')
            check(sub is not None and sub.external_tenant_id == ids['external']['tenant'],
                  'Owned subscription tenant identity changed')
            check(config is not None and oauth is not None and mapping is not None and oidc_client is not None,
                  'Owned SaaS auth identity is incomplete')
            persisted_change_id, config_id, sub_id = change.id, config.id, sub.id
            baseline_version_id = sub.version_id
            persisted_snapshot = (change.status, change.resource_applied, change.evidence_json,
                                  change.resource_plan_json, change.retries)
            original_config = {key: getattr(config, key) for key in
                ('base_url', 'operation_path', 'token_url', 'client_id', 'client_secret', 'scope',
                 'auth_mode', 'timeout_ms', 'status')}
            original_client_status, original_mapping = oauth.status, mapping.external_user_id
            original_oidc_status = oidc_client.status
            token_secret, client_id = oauth.client_secret, oauth.client_id
            tenant = sub.external_tenant_id
            term_ids = [row.id for row in db.scalars(select(m.subscription_terms['Term']).where(
                m.subscription_terms['Term'].combination_id == ids['database_ids']['combination_id']).order_by(
                    m.subscription_terms['Term'].id))]
            Revision = m.subscription_terms['Revision']
            before_terms = [(row.id, row.term_id, row.revision_number, row.version_id)
                for row in db.scalars(select(Revision).where(Revision.term_id.in_(term_ids)).order_by(
                    Revision.term_id, Revision.revision_number, Revision.id))]
            before_ledger = [(row.id, row.status) for row in db.scalars(select(m.VersionChangeAdjustment).where(
                m.VersionChangeAdjustment.change_id == persisted_change_id).order_by(m.VersionChangeAdjustment.id))]

        result = {}
        cache = m._saas_tokens

        def make_attempt(db, persisted, subscription):
            old = db.get(m.SaaSProductVersion, subscription.version_id)
            attempt_id = secrets.token_hex(16)
            plan = json.loads(persisted.resource_plan_json)
            plan['change_id'] = attempt_id
            return SimpleNamespace(id=attempt_id, old_version_id=subscription.version_id,
                old_version_code=old.version_code, target_version_id=persisted.target_version_id,
                target_version_code=persisted.target_version_code,
                resource_plan_json=json.dumps(plan, sort_keys=True, separators=(',', ':')),
                evidence_json='')

        def direct_mock_request(token, body):
            return httpx.post(base.MOCK_SAAS + '/isv.php', json=body,
                headers={'Authorization': 'Bearer ' + token}, timeout=10,
                follow_redirects=False, trust_env=False)

        def rejected(label, mutate, restore, expected_code):
            with m.SessionLocal() as db:
                sub = db.get(m.SaaSSubscription, sub_id)
                version = sub.version_id
                term_ids = [row.id for row in db.scalars(select(m.subscription_terms['Term']).where(
                    m.subscription_terms['Term'].combination_id == ids['database_ids']['combination_id']))]
                Revision = m.subscription_terms['Revision']
                revisions = [(row.id, row.term_id, row.revision_number, row.version_id)
                    for row in db.scalars(select(Revision).where(Revision.term_id.in_(term_ids)).order_by(
                        Revision.term_id, Revision.revision_number, Revision.id))]
                ledger = [(row.id, row.status) for row in db.scalars(select(m.VersionChangeAdjustment).where(
                    m.VersionChangeAdjustment.change_id == persisted_change_id).order_by(
                        m.VersionChangeAdjustment.id))]
                persisted = db.get(Change, persisted_change_id)
                mutate(db, persisted)
                db.commit()
            try:
                if label in {'inactive_client_with_warm_token', 'individually_revoked_warm_token',
                             'expired_signed_warm_token'}:
                    active_cached = cache.get(config_id, (cached_token, None))[0]
                    check(direct_mock_request(active_cached, {'type': 'STATUS', 'tenant_id': tenant,
                        'product_id': ids['product'], 'enterprise_id': ids['enterprises']['buyer'],
                        'subscription_id': sub_id}).status_code == 401,
                        label + ' did not produce the expected real mock-SaaS HTTP 401')
                elif label == 'insufficient_scope_for_change':
                    active_cached = cache[config_id][0]
                    change_probe = {'type': 'CHANGE', 'tenant_id': tenant,
                        'product_id': ids['product'], 'enterprise_id': ids['enterprises']['buyer'],
                        'subscription_id': sub_id, 'to_version': 'upgrade',
                        'idempotency_key': ids['run_id'] + '-scope-probe'}
                    check(direct_mock_request(active_cached, change_probe).status_code == 403,
                        'saas.user-only token did not produce the expected mock-SaaS HTTP 403')
                elif label == 'bad_client_secret_cold_token':
                    with m.SessionLocal() as db:
                        config = db.get(m.SaaSIntegrationConfig, config_id)
                        token_response = httpx.post(m.platform_oauth_token_url(), data={
                            'grant_type': 'client_credentials', 'client_id': config.client_id,
                            'client_secret': config.client_secret, 'scope': config.scope}, timeout=10,
                            follow_redirects=False, trust_env=False)
                    base.check(token_response.status_code == 401,
                        'Bad cold client secret did not produce the expected real token endpoint HTTP 401')
                with m.SessionLocal() as db:
                    persisted = db.get(Change, persisted_change_id)
                    live_attempt = make_attempt(db, persisted, db.get(m.SaaSSubscription, sub_id))
                    try:
                        m.version_change_resources['apply'](db, live_attempt, 'target')
                    except Exception as exc:
                        code = getattr(exc, 'status_code', None)
                        base.check(code == expected_code, label +
                            ' rejected with unexpected production error status')
                        result[label] = code
                        if label == 'expired_signed_warm_token':
                            base.check(config_id not in cache,
                                'Production SaaS token cache retained the resource-401 token')
                            result['expired_401_cache_auto_evicted'] = True
                        if label in {'individually_revoked_warm_token', 'expired_signed_warm_token'}:
                            fresh = m.saas_access_token(db.get(m.SaaSIntegrationConfig, config_id))
                            base.check(fresh != active_cached,
                                'Next safe attempt reused the rejected token')
                            response = direct_mock_request(fresh, {'type': 'STATUS', 'tenant_id': tenant,
                                'product_id': ids['product'], 'enterprise_id': ids['enterprises']['buyer'],
                                'subscription_id': sub_id})
                            base.check(response.status_code == 200,
                                'Fresh token did not restore resource access after 401')
                            result[label + '_next_attempt_recovered'] = True
                    else:
                        raise base.AcceptanceFailure(label + ' unexpectedly applied')
                    finally:
                        db.rollback()
            finally:
                with m.SessionLocal() as db:
                    restore(db)
                    db.commit()
            with m.SessionLocal() as db:
                sub = db.get(m.SaaSSubscription, sub_id)
                term_ids = [row.id for row in db.scalars(select(m.subscription_terms['Term']).where(
                    m.subscription_terms['Term'].combination_id == ids['database_ids']['combination_id']))]
                Revision = m.subscription_terms['Revision']
                current_revisions = [(row.id, row.term_id, row.revision_number, row.version_id)
                    for row in db.scalars(select(Revision).where(Revision.term_id.in_(term_ids)).order_by(
                        Revision.term_id, Revision.revision_number, Revision.id))]
                current_ledger = [(row.id, row.status) for row in db.scalars(select(m.VersionChangeAdjustment).where(
                    m.VersionChangeAdjustment.change_id == persisted_change_id).order_by(
                        m.VersionChangeAdjustment.id))]
                base.check(sub.version_id == version and current_revisions == revisions and current_ledger == ledger,
                    label + ' advanced local version, term revisions, or ledger')
                current_change = db.get(Change, persisted_change_id)
                base.check((current_change.status, current_change.resource_applied,
                    current_change.evidence_json, current_change.resource_plan_json,
                    current_change.retries) == persisted_snapshot,
                    label + ' changed the completed persisted change record')
                config = db.get(m.SaaSIntegrationConfig, config_id)
                cache.pop(config_id, None)
                try:
                    status = m.saas_call(config, 'STATUS', {'tenant_id': tenant,
                        'product_id': ids['product'], 'enterprise_id': ids['enterprises']['buyer'],
                        'subscription_id': sub_id, 'admin_user_id': ids['users']['buyer'],
                        'oidc_client_id': ids['external']['oidc_client_id']})
                    base.check(status.get('ret') == 0 and status.get('version') ==
                        db.get(m.SaaSProductVersion, version).version_code,
                        label + ' changed the external tenant despite local rejection')
                finally:
                    cache.pop(config_id, None)

        STAGE = 'warm-cache-and-revoke-client'
        with m.SessionLocal() as db:
            config = db.get(m.SaaSIntegrationConfig, config_id)
            cached_token = m.saas_access_token(config)
            cache_entry = cache.get(config_id)
        base.check(bool(cached_token) and cache_entry and cache_entry[0] == cached_token,
                   'Production SaaS token cache was not warmed')

        STAGE = 'expired-cache-ttl-reacquisition'
        with m.SessionLocal() as db:
            config = db.get(m.SaaSIntegrationConfig, config_id)
            cache[config_id] = (cached_token, m.now() - timedelta(seconds=1))
            reacquired = m.saas_access_token(config)
            reacquired_entry = cache.get(config_id)
        base.check(reacquired and reacquired != cached_token and reacquired_entry and
                   reacquired_entry[0] == reacquired and
                   reacquired_entry[1] > m.now() + timedelta(seconds=30),
                   'Expired fixture cache entry did not trigger production token reacquisition')
        live_status = direct_mock_request(reacquired, {'type': 'STATUS', 'tenant_id': tenant,
            'product_id': ids['product'], 'enterprise_id': ids['enterprises']['buyer'],
            'subscription_id': sub_id, 'admin_user_id': ids['users']['buyer'],
            'oidc_client_id': ids['external']['oidc_client_id']})
        live_payload = live_status.json() if live_status.status_code == 200 else {}
        with m.SessionLocal() as db:
            baseline_code = db.get(m.SaaSProductVersion, baseline_version_id).version_code
        base.check(live_status.status_code == 200 and live_payload.get('ret') == 0 and
                   live_payload.get('tenant_id') == tenant and
                   live_payload.get('product_id') == ids['product'] and
                   live_payload.get('enterprise_id') == ids['enterprises']['buyer'] and
                   live_payload.get('subscription_id') == sub_id and
                   live_payload.get('version') == baseline_code and
                   live_payload.get('admin_user_id') == ids['users']['buyer'] and
                   live_payload.get('admin_active') is True and
                   live_payload.get('application_ready') is True and
                   live_payload.get('oidc_client_id') == ids['external']['oidc_client_id'],
                   'Reacquired token did not authorize full-identity mock-SaaS STATUS')

        STAGE = 'expired-signed-jwt-cache-fault'
        claims = m.jwt.decode(cached_token, m.JWT_SECRET, algorithms=['HS256'],
            options={'verify_exp': False, 'verify_aud': False})
        claims['exp'] = int(m.now().timestamp()) - 60
        expired_token = m.jwt.encode(claims, m.JWT_SECRET, algorithm='HS256')
        cache[config_id] = (expired_token, m.now() + timedelta(minutes=10))
        rejected('expired_signed_warm_token', lambda _db, _change: None,
                 lambda _db: cache.__setitem__(config_id, reacquired_entry), 502)

        STAGE = 'insufficient-scope-change-fault'
        scoped_response = client.post('/oauth/token', data={'grant_type': 'client_credentials',
            'client_id': client_id, 'client_secret': token_secret, 'scope': 'saas.user'})
        base.check(scoped_response.status_code == 200,
                   'Could not issue owned saas.user-only platform token')
        scoped_token = scoped_response.json().get('access_token')
        check(isinstance(scoped_token, str) and scoped_token,
              'Scoped token response was empty')
        cache[config_id] = (scoped_token, m.now() + timedelta(minutes=10))
        rejected('insufficient_scope_for_change', lambda _db, _change: None,
                 lambda _db: cache.__setitem__(config_id, reacquired_entry), 502)

        def disable_client(db, _change):
            db.scalar(select(m.OAuthClient).where(m.OAuthClient.client_id == client_id)).status = 'revoked'
        def restore_client(db):
            db.scalar(select(m.OAuthClient).where(m.OAuthClient.client_id == client_id)).status = original_client_status
            cache[config_id] = cache_entry
        rejected('inactive_client_with_warm_token', disable_client, restore_client, 502)

        STAGE = 'individual-token-revoke'
        response = client.post('/oauth/revoke', data={'client_id': client_id,
            'client_secret': token_secret, 'token': cached_token})
        base.check(response.status_code == 200, 'Fixture cached token revoke failed')
        def keep_client(db, _change):
            cache[config_id] = cache_entry
        rejected('individually_revoked_warm_token', keep_client,
                 lambda db: cache.__setitem__(config_id, cache_entry), 502)
        base.check(client.post('/oauth/introspect', data={'token': cached_token}).json().get('active') is False,
                   'Individually revoked cached token remained active')

        STAGE = 'bad-secret-cold-token'
        def bad_secret(db, _change):
            config = db.get(m.SaaSIntegrationConfig, config_id)
            config.client_secret = 'invalid-fixture-secret-' + secrets.token_hex(8)
            cache.pop(config_id, None)
        def restore_secret(db):
            db.get(m.SaaSIntegrationConfig, config_id).client_secret = original_config['client_secret']
            cache.pop(config_id, None)
        rejected('bad_client_secret_cold_token', bad_secret, restore_secret, 502)

        STAGE = 'inactive-oidc-preflight'
        def inactive_oidc(db, _change):
            db.scalar(select(m.OIDCClient).where(m.OIDCClient.client_id == ids['external']['oidc_client_id'],
                m.OIDCClient.product_id == ids['product'])).status = 'inactive'
        def restore_oidc(db):
            db.scalar(select(m.OIDCClient).where(m.OIDCClient.client_id == ids['external']['oidc_client_id'],
                m.OIDCClient.product_id == ids['product'])).status = original_oidc_status
        rejected('inactive_oidc_client_preflight', inactive_oidc, restore_oidc, 409)

        STAGE = 'admin-mapping-preflight'
        def wrong_mapping(db, _change):
            row = db.scalar(select(m.SaaSUserMapping).where(
                m.SaaSUserMapping.subscription_id == sub_id, m.SaaSUserMapping.user_id == ids['users']['buyer']))
            row.external_user_id = 'wrong-' + secrets.token_hex(6)
        def restore_mapping(db):
            db.scalar(select(m.SaaSUserMapping).where(
                m.SaaSUserMapping.subscription_id == sub_id, m.SaaSUserMapping.user_id == ids['users']['buyer'])).external_user_id = original_mapping
        rejected('admin_mapping_mismatch_preflight', wrong_mapping, restore_mapping, 409)

        STAGE = 'restore-and-apply-proof'
        cache.pop(config_id, None)
        with m.SessionLocal() as db:
            persisted = db.get(Change, persisted_change_id)
            sub = db.get(m.SaaSSubscription, sub_id)
            config = db.get(m.SaaSIntegrationConfig, config_id)
            for key, value in original_config.items():
                setattr(config, key, value)
            db.scalar(select(m.OAuthClient).where(m.OAuthClient.client_id == client_id)).status = original_client_status
            mapping = db.scalar(select(m.SaaSUserMapping).where(
                m.SaaSUserMapping.subscription_id == sub_id, m.SaaSUserMapping.user_id == ids['users']['buyer']))
            mapping.external_user_id = original_mapping
            db.scalar(select(m.OIDCClient).where(
                m.OIDCClient.product_id == ids['product'],
                m.OIDCClient.client_id == ids['external']['oidc_client_id'])).status = original_oidc_status
            change = make_attempt(db, persisted, sub)
            proof = m.version_change_resources['apply'](db, change, 'target')
            db.flush()
            target_evidence = json.loads(change.evidence_json)
            base.check(sub.version_id == change.target_version_id and proof.get('request_change_id') == change.id and
                       proof.get('resource_change_id') == change.id and
                       isinstance(proof.get('evidence_fingerprint'), str) and
                       len(proof['evidence_fingerprint']) == 64 and
                       target_evidence.get('direction') == 'target' and
                       target_evidence.get('evidence_fingerprint') == proof['evidence_fingerprint'],
                       'Restored target apply did not update the exact subscription')
            target_status = m.saas_call(config, 'STATUS', {'tenant_id': tenant,
                'product_id': ids['product'], 'enterprise_id': ids['enterprises']['buyer'],
                'subscription_id': sub_id, 'version': change.target_version_code,
                'admin_user_id': ids['users']['buyer'], 'oidc_client_id': ids['external']['oidc_client_id']})
            base.check(target_status.get('ret') == 0 and target_status.get('status') == 'active' and
                target_status.get('tenant_id') == tenant and target_status.get('product_id') == ids['product'] and
                target_status.get('enterprise_id') == ids['enterprises']['buyer'] and
                target_status.get('subscription_id') == sub_id and
                target_status.get('version') == change.target_version_code and
                target_status.get('admin_user_id') == ids['users']['buyer'] and
                target_status.get('admin_active') is True and target_status.get('application_ready') is True and
                target_status.get('oidc_client_id') == ids['external']['oidc_client_id'],
                'Target STATUS did not prove full tenant/admin/OIDC identity')
            proof = m.version_change_resources['apply'](db, change, 'original')
            db.flush()
            original_evidence = json.loads(change.evidence_json)
            base.check(sub.version_id == baseline_version_id and proof.get('request_change_id') == change.id and
                       proof.get('resource_change_id') == change.id and
                       isinstance(proof.get('evidence_fingerprint'), str) and
                       len(proof['evidence_fingerprint']) == 64 and
                       original_evidence.get('direction') == 'original' and
                       original_evidence.get('evidence_fingerprint') == proof['evidence_fingerprint'],
                       'Original resource apply did not restore the actual pre-test baseline')
            restored = m.saas_call(config, 'STATUS', {'tenant_id': tenant,
                'product_id': ids['product'], 'enterprise_id': ids['enterprises']['buyer'],
                'subscription_id': sub_id, 'version': db.get(m.SaaSProductVersion, baseline_version_id).version_code,
                'admin_user_id': ids['users']['buyer'], 'oidc_client_id': ids['external']['oidc_client_id']})
            base.check(restored.get('ret') == 0 and restored.get('tenant_id') == tenant and
                restored.get('product_id') == ids['product'] and restored.get('enterprise_id') == ids['enterprises']['buyer'] and
                restored.get('subscription_id') == sub_id and
                restored.get('version') == db.get(m.SaaSProductVersion, baseline_version_id).version_code and
                restored.get('status') == 'active' and restored.get('admin_user_id') == ids['users']['buyer'] and
                restored.get('admin_active') is True and restored.get('application_ready') is True and
                restored.get('oidc_client_id') == ids['external']['oidc_client_id'],
                'Restored STATUS did not prove full tenant/admin/OIDC identity')
            db.commit()
        with m.SessionLocal() as db:
            final_terms = [row.id for row in db.scalars(select(m.subscription_terms['Term']).where(
                m.subscription_terms['Term'].combination_id == ids['database_ids']['combination_id']))]
            Revision = m.subscription_terms['Revision']
            final_revisions = [(row.id, row.term_id, row.revision_number, row.version_id)
                for row in db.scalars(select(Revision).where(Revision.term_id.in_(final_terms)).order_by(
                    Revision.term_id, Revision.revision_number, Revision.id))]
            final_ledger = [(row.id, row.status) for row in db.scalars(select(m.VersionChangeAdjustment).where(
                m.VersionChangeAdjustment.change_id == persisted_change_id).order_by(
                    m.VersionChangeAdjustment.id))]
            base.check(final_revisions == before_terms and final_ledger == before_ledger,
                       'Direct resource apply changed term revisions or version-change ledger')
            persisted = db.get(Change, persisted_change_id)
            base.check((persisted.status, persisted.resource_applied, persisted.evidence_json,
                persisted.resource_plan_json, persisted.retries) == persisted_snapshot,
                'Transient apply changed the completed persisted change record')
        STAGE = 'status-verification-faults'
        import saas_resource_status_fault_acceptance
        status_faults = saas_resource_status_fault_acceptance.run_cases(m, ids)
        STAGE = 'cleanup'
        restore_owned_auth(m, ids)
        cleanup_auth_children(m, ids)
        base.cleanup(m, ids, path)
        return {'status': 'passed', 'scope': 'owned mock-SaaS auth faults through production resource apply',
            'baseline': baseline, 'rejections': result, 'status_faults': status_faults,
            'restored_target_and_original_identity_proofs': True,
            'cleanup': 'exact runtime helper ownership cleanup verified'}
    finally:
        if client:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--run', action='store_true')
    modes.add_argument('--cleanup', action='store_true')
    parser.add_argument('--manifest')
    args = parser.parse_args()
    if args.cleanup:
        if not args.manifest:
            raise base.AcceptanceFailure('--cleanup requires --manifest')
        ids = base.read_manifest(args.manifest)
        m = base.load_main()
        restore_owned_auth(m, ids)
        cleanup_auth_children(m, ids)
        base.cleanup(m, ids, args.manifest)
        result = {'status': 'passed', 'cleanup': 'exact owned runtime fixture removed'}
    else:
        result = run(args.manifest)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'stage': STAGE,
            'error_type': type(exc).__name__, 'frame': traceback.extract_tb(exc.__traceback__)[-1].lineno}, sort_keys=True))
        raise SystemExit(1)
