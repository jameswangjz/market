"""Verify credentials after an authorized clock correction, without business writes."""
import argparse
import json
import os
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-token', type=Path, required=True)
    parser.add_argument('--new-token', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    import httpx
    import jwt
    from sqlalchemy import select, func
    from app import main as m
    checks = []
    with m.SessionLocal() as db, httpx.Client(base_url='http://127.0.0.1:8000', timeout=30) as client:
        before = args.old_token.read_text().strip()
        if client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + before}).status_code != 401:
            raise ValueError('Old12h credential was not expired after19h correction')
        checks.append('expired_pre_correction_login_rejected')
        actor = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        token = m.issue_token(actor)
        response = client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + token})
        response.raise_for_status()
        if response.json().get('user', {}).get('id') != actor.id:
            raise ValueError('New login identity mismatch')
        fd = os.open(args.new_token, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(token)
        checks.append('new_login_credential_works')
        application = db.scalar(select(m.OAuthClient).join(m.Product, m.Product.id == m.OAuthClient.product_id)
            .where(m.OAuthClient.status == 'active', m.Product.name == 'SaaS-APP1'))
        if application is None:
            raise ValueError('Existing mock SaaS OAuth client not available for verification')
        response = client.post('/oauth/token', data={'grant_type': 'client_credentials',
            'client_id': application.client_id, 'client_secret': application.client_secret})
        response.raise_for_status()
        resource_token = response.json()['access_token']
        response = client.post('/oauth/introspect', data={'token': resource_token})
        response.raise_for_status()
        if response.json().get('active') is not True:
            raise ValueError('Fresh SaaS token did not introspect active')
        checks.append('existing_mock_saas_client_still_issues_valid_oauth_token')
        claims = jwt.decode(resource_token, m.JWT_SECRET, algorithms=['HS256'],
            audience='market-resource', issuer=m.platform_oauth_issuer())
        claims.update(iat=int(time.time())-7200, exp=int(time.time())-3600)
        expired = jwt.encode(claims, m.JWT_SECRET, algorithm='HS256')
        response = client.post('/oauth/introspect', data={'token': expired})
        response.raise_for_status()
        if response.json().get('active') is not False:
            raise ValueError('Expired resource token remains active')
        checks.append('expired_oauth_token_is_inactive')
        for path in ('/.well-known/openid-configuration', '/api/health'):
            response = client.get(path)
            response.raise_for_status()
        checks.append('oidc_discovery_and_api_health_work')
        active = db.scalar(select(func.count()).select_from(m.refund_execution['Execution']).where(
            m.refund_execution['Execution'].status.in_(['requested', 'processing', 'executing'])))
        checks.append({'active_refund_execution_count': active, 'no_money_execution_requested': True})
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump({'status': 'passed', 'checks': checks}, output, indent=2)
    print(json.dumps({'status': 'passed', 'checks': len(checks)}))


if __name__ == '__main__':
    main()
