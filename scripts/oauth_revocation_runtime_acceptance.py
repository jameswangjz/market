"""Exercise deployed machine OAuth revocation with an isolated mock SaaS client.

Run inside the backend runtime with --run. The 0600 manifest contains generated
fixture IDs only and supports exact cleanup after an interrupted run.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import traceback

os.environ.setdefault('MESSAGE_CENTER_DISABLE_BACKGROUND', 'true')

import httpx
from sqlalchemy import delete, or_, select

import version_change_saas_runtime_acceptance as fixture


class AcceptanceFailure(RuntimeError):
    pass


STAGE = 'startup'
MOCK_SAAS = 'http://market-mock-saas:8200'
SCOPE = 'saas.tenant saas.user saas.department'


def check(condition, message):
    if not condition:
        raise AcceptanceFailure(message)


def fixture_ids(run_id=None):
    run_id = run_id or 'qa-oauth-revoke-' + secrets.token_hex(6)
    return {
        'run_id': run_id,
        'enterprise_id': run_id + '-enterprise',
        'product_id': run_id + '-product',
        'client_id': run_id + '-client',
        'client_row_id': hashlib.sha256((run_id + '-oauth-row').encode()).hexdigest()[:32],
        'original_client_status': 'active',
        'client_status_revocation_journaled': False,
        'environment': '',
        'protected_order_fingerprint': '',
    }


def environment_fingerprint(m):
    return hashlib.sha256(m.engine.url.render_as_string(hide_password=False).encode()).hexdigest()


def write_manifest(path, value, create=False):
    path = Path(path)
    temporary = None
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    else:
        info = path.lstat()
        check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
              'Manifest must be a regular file with mode 0600')
        fd, temporary = tempfile.mkstemp(prefix='.oauth-revoke-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if temporary:
            os.replace(temporary, path)
            temporary = None
    finally:
        if temporary:
            os.unlink(temporary)


def read_manifest(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) |
                 getattr(os, 'O_CLOEXEC', 0))
    try:
        info = os.fstat(fd)
        check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
              'Manifest must be a regular file with mode 0600')
        with os.fdopen(fd, 'r') as stream:
            fd = -1
            value = json.load(stream)
    finally:
        if fd >= 0:
            os.close(fd)
    run_id = value.get('run_id', '')
    check(re.fullmatch(r'qa-oauth-revoke-[0-9a-f]{12}', run_id) is not None,
          'Invalid generated fixture ownership marker')
    expected = fixture_ids(run_id)
    check(set(value) == set(expected) and all(value.get(key) == expected[key]
          for key in ('run_id', 'enterprise_id', 'product_id', 'client_id', 'client_row_id')),
          'Manifest fixture roots were changed')
    check(value.get('original_client_status') == 'active' and
          type(value.get('client_status_revocation_journaled')) is bool and
          re.fullmatch(r'[0-9a-f]{64}', value.get('environment', '')) and
          re.fullmatch(r'[0-9a-f]{64}', value.get('protected_order_fingerprint', '')),
          'Invalid fixture restoration journal')
    return value


def load_main():
    from app import main as m
    check(m.engine.dialect.name == 'postgresql', 'Dedicated PostgreSQL runtime required')
    check(not m.app.dependency_overrides, 'Dependency overrides are forbidden')
    check(hasattr(m, 'OAuthRevokedToken'), 'OAuth token revocation model is not deployed')
    return m


def preflight(m, ids):
    from sqlalchemy import select
    with m.SessionLocal() as db:
        check(db.get(m.Enterprise, ids['enterprise_id']) is None and
              db.get(m.Product, ids['product_id']) is None and
              db.get(m.OAuthClient, ids['client_row_id']) is None and
              db.scalar(select(m.OAuthClient.id).where(
                  m.OAuthClient.client_id == ids['client_id'])) is None,
              'Generated identity collision; no cleanup is authorized')
        ids['protected_order_fingerprint'] = fixture.fingerprint(db, m)
    ids['environment'] = environment_fingerprint(m)
    response = httpx.get(MOCK_SAAS + '/health', timeout=5,
                         follow_redirects=False, trust_env=False)
    check(response.status_code == 200, 'Exact mock SaaS health check failed')


def write(path, ids):
    write_manifest(path, ids)


def create_fixture(m, ids, path):
    secret = secrets.token_urlsafe(40)
    with m.SessionLocal() as db:
        db.add(m.Enterprise(id=ids['enterprise_id'], name=ids['enterprise_id'],
            credit_code=ids['enterprise_id'], verification_status='verified'))
        db.flush()
        product = m.Product(id=ids['product_id'], enterprise_id=ids['enterprise_id'],
            name=ids['product_id'], product_type='saas', status='draft',
            delivery_method='tenant_access', currency='CNY', price=0,
            integration_api_url=MOCK_SAAS + '/isv.php')
        db.add(product)
        db.flush()
        db.add(m.OAuthClient(id=ids['client_row_id'], product_id=ids['product_id'],
            client_id=ids['client_id'],
            client_secret=secret, scope=SCOPE, status='active'))
        db.add(m.SaaSIntegrationConfig(product_id=ids['product_id'],
            base_url=MOCK_SAAS, operation_path='/isv.php',
            token_url='http://market-api:8000/oauth/token', client_id=ids['client_id'],
            client_secret=secret, scope=SCOPE, auth_mode='oauth2', status='active',
            verification_status='pending'))
        db.commit()
    write(path, ids)
    return secret


def issue_token(client, client_id, secret, expected=200):
    response = client.post('/oauth/token', data={'grant_type': 'client_credentials',
        'client_id': client_id, 'client_secret': secret, 'scope': SCOPE})
    check(response.status_code == expected,
          f'Platform token endpoint returned HTTP {response.status_code}, expected {expected}')
    return response.json().get('access_token', '') if expected == 200 else ''


def introspect(client, token):
    response = client.post('/oauth/introspect', data={'token': token})
    check(response.status_code == 200, 'Platform introspection endpoint did not return HTTP 200')
    payload = response.json()
    check(isinstance(payload, dict), 'Platform introspection response is invalid')
    return payload


def revoke_token(client, client_id, secret, token):
    response = client.post('/oauth/revoke', data={'client_id': client_id,
        'client_secret': secret, 'token': token})
    check(response.status_code == 200 and response.headers.get('cache-control') == 'no-store',
          f'Platform revoke endpoint returned HTTP {response.status_code} or cacheable response')


def mock_status(token, ids, expected):
    response = httpx.post(MOCK_SAAS + '/isv.php', json={'type': 'STATUS',
        'tenant_id': 'qa-never-created-' + ids['run_id'][-12:],
        'product_id': ids['product_id'], 'enterprise_id': ids['enterprise_id'],
        'subscription_id': ids['run_id'] + '-subscription'},
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'},
        timeout=10, follow_redirects=False, trust_env=False)
    if expected is not None:
        check(response.status_code == expected,
              f'Mock SaaS protected STATUS returned HTTP {response.status_code}, expected {expected}')
    if expected == 404:
        check(response.json().get('detail') == 'tenant_not_found',
              'Protected STATUS did not fail specifically because the tenant is absent')
    return response.status_code


def client_snapshot(m):
    with m.SessionLocal() as db:
        rows = db.scalars(select(m.OAuthClient).order_by(m.OAuthClient.client_id)).all()
        return {row.client_id: (row.product_id, row.status) for row in rows}


def cleanup(m, ids, path):
    from sqlalchemy import select
    check(ids['environment'] == environment_fingerprint(m),
          'Cleanup database environment differs from the authorized fixture')
    with m.SessionLocal() as db:
        client = db.scalar(select(m.OAuthClient).where(
            m.OAuthClient.client_id == ids['client_id'],
            m.OAuthClient.product_id == ids['product_id']))
        product = db.get(m.Product, ids['product_id'])
        enterprise = db.get(m.Enterprise, ids['enterprise_id'])
        if client or product or enterprise:
            check(product is not None and product.enterprise_id == ids['enterprise_id'] and
                  enterprise is not None,
                  'Generated product/enterprise ownership proof failed')
            check(client is not None and client.product_id == ids['product_id'],
                  'Generated OAuth client ownership proof failed')
            if ids['client_status_revocation_journaled']:
                client.status = ids['original_client_status']
                db.commit()
                client = db.get(m.OAuthClient, client.id)
                check(client.status == ids['original_client_status'],
                      'Fixture OAuth client status restoration failed')
            config = db.scalar(select(m.SaaSIntegrationConfig).where(
                m.SaaSIntegrationConfig.product_id == ids['product_id']))
            check(config is not None and config.client_id == ids['client_id'],
                  'Generated SaaS config ownership proof failed')
            db.execute(delete(m.OAuthRevokedToken).where(
                m.OAuthRevokedToken.client_id == ids['client_row_id']))
            db.execute(delete(m.SaaSIntegrationConfig).where(
                m.SaaSIntegrationConfig.product_id == ids['product_id']))
            db.execute(delete(m.OAuthClient).where(m.OAuthClient.id == client.id))
            db.execute(delete(m.Product).where(m.Product.id == ids['product_id']))
            db.execute(delete(m.Enterprise).where(m.Enterprise.id == ids['enterprise_id']))
            db.commit()
        # Revocation rows belong to this exact client through its generated FK.
        db.execute(delete(m.OAuthRevokedToken).where(
            m.OAuthRevokedToken.client_id == ids['client_row_id']))
        audit_candidates = db.scalars(select(m.AuditLog).where(or_(
            m.AuditLog.actor == ids['client_id'],
            m.AuditLog.target_id == ids['client_row_id']))).all()
        check(all(row.actor == ids['client_id'] and
                  row.action == 'revoke_oauth_token' and
                  row.target_type == 'oauth_client' and
                  row.target_id == ids['client_row_id']
                  for row in audit_candidates),
              'Fixture-associated audit rows include an unexpected or non-owned record')
        audit_filter = (
            (m.AuditLog.actor == ids['client_id']) &
            (m.AuditLog.action == 'revoke_oauth_token') &
            (m.AuditLog.target_type == 'oauth_client') &
            (m.AuditLog.target_id == ids['client_row_id']))
        audit_ids = {row.id for row in audit_candidates}
        deleted_audits = db.execute(delete(m.AuditLog).where(audit_filter))
        check(deleted_audits.rowcount == len(audit_ids),
              'Exact fixture revocation audit cleanup count changed')
        db.commit()
        check(fixture.fingerprint(db, m) == ids['protected_order_fingerprint'],
              'Protected order fingerprint changed during acceptance or cleanup')
        check(db.get(m.Product, ids['product_id']) is None and
              db.get(m.Enterprise, ids['enterprise_id']) is None and
              db.scalar(select(m.OAuthClient.id).where(
                  m.OAuthClient.client_id == ids['client_id'])) is None and
              db.scalar(select(m.OAuthRevokedToken.token_hash).where(
                  m.OAuthRevokedToken.client_id == ids['client_row_id'])) is None and
              db.scalar(select(m.AuditLog.id).where(
                  m.AuditLog.actor == ids['client_id'],
                  m.AuditLog.action == 'revoke_oauth_token',
                  m.AuditLog.target_type == 'oauth_client',
                  m.AuditLog.target_id == ids['client_row_id'])) is None,
              'Exact fixture roots remain after cleanup')
    Path(path).unlink(missing_ok=True)


def run(manifest_path=None):
    global STAGE
    m = load_main()
    ids = fixture_ids()
    path = Path(manifest_path or '/tmp/' + ids['run_id'] + '.json')
    STAGE = 'preflight'
    preflight(m, ids)
    write_manifest(path, ids, create=True)
    client = httpx.Client(base_url=m.platform_oauth_issuer().rstrip('/'), timeout=10,
                          follow_redirects=False, trust_env=False)
    secret = None
    prior_clients = client_snapshot(m)
    try:
        STAGE = 'fixture-create'
        secret = create_fixture(m, ids, path)
        check(client_snapshot(m) == {**prior_clients, ids['client_id']:
              (ids['product_id'], 'active')}, 'Unexpected OAuth clients changed during fixture setup')
        STAGE = 'token-issuance-and-protected-request'
        token = issue_token(client, ids['client_id'], secret)
        sibling_token = issue_token(client, ids['client_id'], secret)
        claims = introspect(client, token)
        check(claims.get('active') is True and claims.get('client_id') == ids['client_id'] and
              claims.get('product_id') == ids['product_id'] and claims.get('jti') and
              introspect(client, sibling_token).get('jti') != claims.get('jti'),
              'Issued tokens were not active, uniquely identified, and bound to the isolated product')
        mock_status(token, ids, 404)
        mock_status(sibling_token, ids, 404)
        STAGE = 'individual-token-revocation'
        revoke_token(client, ids['client_id'], secret, token)
        revoke_token(client, ids['client_id'], secret, token)
        token_claims = introspect(client, token)
        sibling_claims = introspect(client, sibling_token)
        token_status = mock_status(token, ids, expected=None)
        sibling_status = mock_status(sibling_token, ids, expected=None)
        check(token_claims.get('active') is False and token_status == 401,
              'Individually revoked token remained active or reached protected mock SaaS')
        check(sibling_claims.get('active') is True and sibling_status == 404,
              'Individual token revocation affected another token from the same client')
        fresh_token = issue_token(client, ids['client_id'], secret)
        check(introspect(client, fresh_token).get('active') is True,
              'Client could not mint a new token after individual token revocation')
        mock_status(fresh_token, ids, 404)
        with m.SessionLocal() as db:
            revoked = db.scalars(select(m.OAuthRevokedToken).where(
                m.OAuthRevokedToken.client_id == ids['client_row_id'])).all()
            audit_rows = db.scalars(select(m.AuditLog).where(
                m.AuditLog.actor == ids['client_id'],
                m.AuditLog.action == 'revoke_oauth_token',
                m.AuditLog.target_type == 'oauth_client',
                m.AuditLog.target_id == ids['client_row_id'])).all()
            check(len(revoked) == 1 and
                  revoked[0].token_hash == hashlib.sha256(token.encode()).hexdigest(),
                  'Individual revoke did not persist exactly the revoked token hash')
            check(len(audit_rows) == 2,
                  'Same-token revoke replay did not produce both expected audit events')
        STAGE = 'client-status-revocation'
        ids['client_status_revocation_journaled'] = True
        write(path, ids)
        with m.SessionLocal() as db:
            row = db.scalar(select(m.OAuthClient).where(
                m.OAuthClient.client_id == ids['client_id'],
                m.OAuthClient.product_id == ids['product_id']))
            check(row is not None and row.status == ids['original_client_status'],
                  'Exact generated OAuth client changed before revocation')
            row.status = 'revoked'
            db.commit()
        STAGE = 'revocation-verification'
        sibling_inactive = introspect(client, sibling_token)
        fresh_inactive = introspect(client, fresh_token)
        token_inactive = introspect(client, token)
        issue_token(client, ids['client_id'], secret, expected=401)
        token_status = mock_status(token, ids, expected=None)
        sibling_status = mock_status(sibling_token, ids, expected=None)
        fresh_status = mock_status(fresh_token, ids, expected=None)
        check(token_inactive.get('active') is False and sibling_inactive.get('active') is False and
              fresh_inactive.get('active') is False,
              'Client status revocation left a fixture token introspection-active')
        check(token_status == 401 and sibling_status == 401 and fresh_status == 401,
              'Client status revocation allowed a token to reach protected mock SaaS')
        check(client_snapshot(m) == {**prior_clients, ids['client_id']:
              (ids['product_id'], 'revoked')},
              'A non-fixture OAuth client changed during revocation acceptance')
        return {'status': 'passed', 'scope': 'platform machine OAuth and mock-SaaS STATUS',
            'individual_token_revocation': True,
            'same_client_sibling_token_survives': True,
            'new_token_issuance_after_individual_revoke': True,
            'client_status_revokes_remaining_tokens': True,
            'new_issuance_after_client_status_revoke_denied': True,
            'pre_revoke_protected_status': '404 tenant_not_found',
            'individual_and_client_revoked_token_status': 401,
            'other_clients_unchanged': True, 'cleanup': 'exact QA roots removed',
            'gateway_cache_path': 'not exercised; fixture does not populate saas_access_token cache'}
    finally:
        secret = None
        try:
            STAGE = 'cleanup'
            cleanup(m, ids, path)
        finally:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--run', action='store_true')
    modes.add_argument('--cleanup', action='store_true')
    parser.add_argument('--manifest')
    args = parser.parse_args()
    if args.cleanup:
        check(args.manifest, '--cleanup requires --manifest')
        result = {'status': 'passed', 'cleanup': 'exact journaled fixture removed'}
        cleanup(load_main(), read_manifest(args.manifest), args.manifest)
    else:
        result = run(args.manifest)
    print(json.dumps(result, sort_keys=True))


def failure_frame(exc):
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return None
    frame = frames[-1]
    return {'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}


if __name__ == '__main__':
    try:
        main()
    except AcceptanceFailure as exc:
        print(json.dumps({'status': 'failed', 'check': str(exc),
                          'frame': failure_frame(exc)}, sort_keys=True))
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'stage': STAGE,
                          'error_type': type(exc).__name__,
                          'frame': failure_frame(exc)}, sort_keys=True))
        raise SystemExit(1)
