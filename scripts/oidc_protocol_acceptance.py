"""Partial OIDC protocol acceptance: real PG, scoped synthetic SaaS tenant.

Requires deployed schema/key/issuer and --runtime-tag. --base-url exercises
actual deployed HTTP routes; without it only production TestClient is tested.
No real SaaS provisioning, browser sessions, payment or settlement is performed.
Reuses the guarded ownership/FK cleanup from the quote acceptance helper.
Never prints codes, client secrets, access tokens or private keys.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import secrets
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import version_change_quote_acceptance as scope


def run(runtime_tag, base_url=None):
    from fastapi.testclient import TestClient
    import httpx
    import jwt
    from sqlalchemy import select

    scope.check(bool(re.fullmatch(r'\d{14}-[0-9a-f]{7,40}', runtime_tag)), 'Invalid runtime tag')
    if base_url:
        parsed = urlsplit(base_url)
        scope.check(parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment, 'Invalid base URL')
    m = scope.load_main()
    ids = scope.fixture_ids()
    scope.preflight(m, ids)
    manifest = Path('/tmp') / (ids['run_id'] + '.json')
    scope.private_json(manifest, ids, create=True)
    result = {'status': 'failed', 'database': 'real PostgreSQL', 'runtime_tag': runtime_tag,
        'transport': 'deployed HTTP' if base_url else 'production TestClient',
        'limits': ['Protocol core only; no browser session or third-party RP verification',
                   'Tenant/payment/delivery are synthetic isolated fixtures, not real provisioning']}
    try:
        with ExitStack() as guards:
            for name in ('apisix_admin_request', 'publish_apisix_route', 'execute_saas_operation', 'validated_http_request'):
                guards.enter_context(patch.object(m, name, side_effect=scope.AcceptanceFailure('Unexpected provisioning')))
            orders, tokens = scope.prepare(m, ids, m.now() - timedelta(days=30))
            product_id = ids['products']['tenant_access']
            enterprise_id = ids['enterprises'][0]
            with m.SessionLocal() as db:
                product = db.get(m.Product, product_id)
                callback = 'https://oidc-fixture.example.invalid/callback'
                product.oidc_redirect_uris_json = json.dumps([callback])
                order = db.get(m.Order, orders['tenant_access'][0])
                subscription = db.get(m.SaaSSubscription, order.subscription_id)
                entitlement = m.subscription_terms['current_entitlement'](db, enterprise_id, product_id)
                scope.check(entitlement is not None, 'Fixture lacks current entitlement')
                subscription.expires_at = entitlement['expires_at']
                sid = subscription.id
                db.add(m.ApplicationAccessGrant(enterprise_id=enterprise_id, product_id=product_id,
                    user_id=ids['users']['member'], granted_by=ids['run_id']))
                db.commit()
            client = guards.enter_context(httpx.Client(base_url=base_url, timeout=20)) if base_url else TestClient(m.app)
            if not base_url:
                guards.callback(client.close)

            def request(method, path, expected=200, role=None, **kwargs):
                headers = kwargs.pop('headers', {})
                if role:
                    headers['Authorization'] = 'Bearer ' + tokens[role]
                response = client.request(method, path, headers=headers, **kwargs)
                scope.check(response.status_code == expected, f'{method} {path}: expected {expected}, got {response.status_code}')
                return response

            discovery = request('GET', '/.well-known/openid-configuration').json()
            jwk = request('GET', '/oidc/jwks').json()['keys'][0]
            scope.check('d' not in jwk and jwk['alg'] == 'RS256', 'JWKS leaked private key or wrong algorithm')
            config = request('POST', f'/api/products/{product_id}/oidc-client', role='provider').json()
            cid, secret = config['client_id'], config['client_secret']
            scope.check(cid.startswith('oidc_') and secret, 'Independent OIDC client missing')
            verifier = 'V' * 64
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()

            def authorize(role='member', expected=200, **overrides):
                body = {'client_id': cid, 'redirect_uri': callback, 'response_type': 'code',
                    'scope': 'openid profile email', 'state': secrets.token_urlsafe(24),
                    'nonce': secrets.token_urlsafe(24), 'code_challenge': challenge,
                    'code_challenge_method': 'S256', 'enterprise_id': enterprise_id} | overrides
                response = request('POST', '/oidc/authorize', expected, role, json=body)
                if expected != 200:
                    return None, body['nonce']
                scope.check(response.headers['cache-control'] == 'no-store', 'Authorization is cacheable')
                query = parse_qs(urlsplit(response.json()['redirect_url']).query)
                scope.check(query['state'][0] == body['state'], 'State was not returned unchanged')
                return query['code'][0], body['nonce']

            def exchange(code, **overrides):
                data = {'client_id': cid, 'client_secret': secret, 'grant_type': 'authorization_code',
                        'redirect_uri': callback, 'code': code, 'code_verifier': verifier} | overrides
                return client.post('/oidc/token', data=data)

            code, nonce = authorize()
            scope.check(exchange(code, code_verifier='X' * 64).status_code == 400, 'Wrong PKCE verifier accepted')
            response = exchange(code)
            scope.check(response.status_code == 200, 'Valid code exchange failed')
            token = response.json()
            claims = jwt.decode(token['id_token'], jwt.PyJWK.from_dict(jwk).key, algorithms=['RS256'],
                audience=cid, issuer=discovery['issuer'])
            scope.check(claims['nonce'] == nonce and claims['sub'] == ids['users']['member']
                and claims['enterprise_id'] == enterprise_id and claims['subscription_id'] == sid,
                'OIDC identity binding wrong')
            def info(access, expected=200):
                return request('GET', '/oidc/userinfo', expected,
                    headers={'Authorization': 'Bearer ' + access}).json()
            scope.check(info(token['access_token'])['sub'] == claims['sub'], 'Userinfo subject mismatch')
            scope.check(exchange(code).status_code == 400, 'Authorization code replay accepted')
            info(token['access_token'], 401)
            authorize(expected=403, enterprise_id=ids['enterprises'][2])
            authorize(expected=400, redirect_uri='https://attacker.invalid/callback')
            authorize(role='other', expected=403)
            basic_code, _ = authorize()
            basic = client.post('/oidc/token', auth=(cid, secret), data={
                'grant_type': 'authorization_code', 'code': basic_code,
                'redirect_uri': callback, 'code_verifier': verifier})
            scope.check(basic.status_code == 200, 'Basic client authentication failed')
            code, _ = authorize()
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: exchange(code), range(2)))
            scope.check(sorted(r.status_code for r in responses) == [200, 400], 'Concurrent code exchange was not single-use')
            with m.SessionLocal() as db:
                row = db.scalar(select(m.OIDCCode).where(m.OIDCCode.id == hashlib.sha256(code.encode()).hexdigest()))
                scope.check(row.consumed_at and row.revoked_at, 'Concurrent replay not persisted')
            code, _ = authorize()
            token = exchange(code).json()
            with m.SessionLocal() as db:
                grant = db.scalar(select(m.ApplicationAccessGrant).where(
                    m.ApplicationAccessGrant.user_id == ids['users']['member']))
                grant.status = 'revoked'
                db.commit()
            info(token['access_token'], 403)
            authorize(expected=403)
            info(tokens['member'], 401)
            repeated = request('POST', f'/api/products/{product_id}/oidc-client', role='provider').json()
            scope.check(repeated['client_secret'] is None, 'OIDC secret unexpectedly echoed')
            with m.SessionLocal() as db:
                db.get(m.Product, product_id).status = 'security_unpublished'
                db.commit()
            request('GET', '/oidc/userinfo', 401,
                headers={'Authorization': 'Bearer ' + basic.json()['access_token']})
            with m.SessionLocal() as db:
                logs = db.scalars(select(m.AuditLog).where(m.AuditLog.actor.in_(
                    [uid + '@example.invalid' for uid in ids['users'].values()]))).all()
                text = json.dumps([{c.name: str(getattr(row, c.name)) for c in m.AuditLog.__table__.columns}
                    for row in logs])
                scope.check(all(private not in text for private in (secret, token['access_token'], token['id_token'], code)),
                    'Audit leaked protocol credentials')
            result.update(status='passed', checks={'rs256_jwks_identity_nonce': True,
                'pkce_wrong_then_correct': True, 'code_replay_revokes_access': True,
                'concurrent_pg_exchange': 'one 200 and one 400', 'cross_enterprise_denied': True,
                'callback_whitelist': True, 'grant_revocation': True, 'management_token_denied': True,
                'audit_no_credentials': True, 'secret_not_echoed_again': True,
                'basic_client_auth': True, 'security_down_denied': True})
    except Exception as exc:
        result['error_type'] = type(exc).__name__
        if isinstance(exc, scope.AcceptanceFailure):
            result['check_failed'] = str(exc)
    finally:
        try:
            scope.cleanup(m, ids, manifest)
            result['cleanup'] = 'verified exact owned DB rows and FK children absent'
        except Exception as exc:
            result.update(status='failed', cleanup_error_type=type(exc).__name__)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-tag', required=True)
    parser.add_argument('--base-url')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = run(args.runtime_tag, args.base_url)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['status'] == 'passed' else 1)
