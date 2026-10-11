"""Credential-free deployed smoke and persisted public signing-key checks."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path

import httpx
from sqlalchemy import select


def run(base_url, runtime_tag, expected_kid=None):
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m

    with m.SessionLocal() as db:
        user = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if not user or not user.is_active:
            raise RuntimeError('Administrator unavailable')
        token = m.issue_token(user)
    with httpx.Client(base_url=base_url, timeout=20) as client:
        statuses = {}
        for path in ('/console', '/.well-known/openid-configuration', '/oidc/jwks'):
            response = client.get(path)
            if response.status_code != 200:
                raise RuntimeError('Public runtime smoke failed')
            statuses[path] = response.status_code
        jwks = client.get('/oidc/jwks').json()['keys']
        if len(jwks) != 1 or 'd' in jwks[0] or jwks[0]['alg'] != 'RS256':
            raise RuntimeError('Unsafe JWKS')
        kid = jwks[0]['kid']
        if expected_kid is not None and kid != expected_kid:
            raise RuntimeError('Signing key changed during restart')
        for path in ('/api/dashboard', '/api/orders',
                     '/api/audit-logs?q=&actor=&order_id=&batch_no=&rule_version=&risk_level=&start=&end=&page=1&page_size=50'):
            response = client.get(path, headers={'Authorization': 'Bearer ' + token})
            if response.status_code != 200:
                raise RuntimeError('Authenticated runtime smoke failed')
            statuses[path] = response.status_code
    return {'status': 'passed', 'runtime_tag': runtime_tag, 'http_statuses': statuses,
            'public_signing_kid': kid, 'persisted_key_after_restart': expected_kid is not None,
            'authlib_version': importlib.metadata.version('Authlib'),
            'cryptography_version': importlib.metadata.version('cryptography'),
            'limits': ['No third-party RP browser session verification',
                       'Key stability checked via public JWKS; not a key rotation test']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--runtime-tag', required=True)
    parser.add_argument('--expected-kid')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = run(args.base_url, args.runtime_tag, args.expected_kid)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'persisted_key_after_restart': result['persisted_key_after_restart']}))
