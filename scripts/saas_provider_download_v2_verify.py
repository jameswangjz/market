"""Deployment checks for provider-only SaaS export; never prints credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path

os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', action='store_true')
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    import httpx
    from sqlalchemy import select
    from app import main as m
    products = ('e0d7e27906f32c69ee11fa8a6093dadf', 'e101107c3f6414ab48b9ebc43de368e8')
    snapshots, checks = {}, []
    with m.SessionLocal() as db:
        for pid in products:
            product = db.get(m.Product, pid)
            machine = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == pid))
            config = db.scalar(select(m.SaaSIntegrationConfig).where(m.SaaSIntegrationConfig.product_id == pid))
            oidc = db.scalar(select(m.OIDCClient).where(m.OIDCClient.product_id == pid))
            assert product and machine and config and oidc
            raw = [machine.id, machine.client_id, machine.client_secret, machine.scope, machine.status,
                   config.id, config.client_id, config.client_secret, config.token_url,
                   oidc.client_id, oidc.secret_hash, oidc.secret_encrypted, oidc.status]
            snapshots[pid] = hashlib.sha256(json.dumps(raw).encode()).hexdigest()
            if args.before:
                continue
            provider = db.scalar(select(m.User).join(m.Membership, m.User.id == m.Membership.user_id).where(
                m.Membership.enterprise_id == product.enterprise_id, m.Membership.role == 'super_admin',
                m.Membership.status == 'active', m.User.is_active.is_(True),
                m.User.verified_status == 'verified', m.User.platform_role == ''))
            assert provider
            admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
            headers = {'Authorization': 'Bearer ' + m.issue_token(provider)}
            prefix = '/api/products/' + pid
            with httpx.Client(base_url='http://127.0.0.1:8000', timeout=20, trust_env=False) as client:
                response = client.get(prefix + '/saas-integration-credentials-download', headers=headers)
                assert response.status_code == 200
                assert response.headers['cache-control'] == 'no-store'
                value = response.json()
                assert value['schema_version'] == '2.0' and 'oauth2_api' not in value
                assert machine.client_id not in response.text and machine.client_secret not in response.text
                assert value['resource_server'] == {
                    'authentication': 'Bearer', 'validation_method': 'introspection',
                    'introspection_url': m.platform_oauth_issuer().rstrip('/') + '/oauth/introspect',
                    'issuer': m.platform_oauth_issuer(), 'audience': 'market-resource',
                    'scopes': sorted(machine.scope.split())}
                assert value['oidc_sso']['client_id'] == oidc.client_id
                assert value['oidc_sso']['client_secret'] == m.oidc_provider['client_secret'](oidc)
                legacy = client.get(prefix + '/oauth-credentials-download', headers=headers)
                assert legacy.status_code == 410
                assert machine.client_secret not in legacy.text
                denied = client.get(prefix + '/saas-integration-credentials-download',
                    headers={'Authorization': 'Bearer ' + m.issue_token(admin)})
                assert denied.status_code == 403
            # Exercise actual mint -> SaaS -> introspection through read-only STATUS.
            operation = db.scalar(select(m.SaaSOperation).join(m.SaaSSubscription,
                m.SaaSSubscription.id == m.SaaSOperation.subscription_id).where(
                m.SaaSSubscription.product_id == pid, m.SaaSOperation.operation == 'STATUS')
                .order_by(m.SaaSOperation.created_at.desc()))
            assert operation
            status = m.saas_call(config, 'STATUS', json.loads(operation.request_payload))
            assert status['ret'] == 0 and status['product_id'] == pid
            checks.append({'product': product.name, 'download_schema': '2.0',
                'machine_credentials_omitted': True, 'oidc_credentials_preserved': True,
                'legacy_machine_download_gone': True, 'platform_download_denied': True,
                'actual_machine_status_call': 'passed',
                'application_ready': status.get('application_ready')})
        order = db.scalar(select(m.Order).where(m.Order.order_no == 'ORD-261014032800AA14'))
        assert order
        snapshots['exception_order'] = hashlib.sha256(json.dumps(
            {c.name: str(getattr(order, c.name)) for c in order.__table__.columns}, sort_keys=True).encode()).hexdigest()
    if not args.before:
        assert args.baseline and json.loads(args.baseline.read_text())['snapshots'] == snapshots
    result = {'status': 'passed', 'before': args.before, 'snapshots': snapshots, 'checks': checks,
              'credential_rotation': False, 'business_order_modified': False}
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(result, output, indent=2)
    print(json.dumps({'status': 'passed', 'before': args.before, 'products_checked': len(checks)}))


if __name__ == '__main__':
    main()
