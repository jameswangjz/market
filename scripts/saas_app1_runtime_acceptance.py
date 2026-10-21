"""Read back real SaaS-APP1 delivery and test provider credential permissions."""
import json
import os

os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
import httpx
from sqlalchemy import select
from app import main as m


def main():
    with m.SessionLocal() as db:
        product = db.get(m.Product, 'e101107c3f6414ab48b9ebc43de368e8')
        order = db.get(m.Order, '6f4147202bb25c449548a54d8ae3060d')
        sub = db.get(m.SaaSSubscription, order.subscription_id)
        task = db.scalar(select(m.DeliveryTask).where(m.DeliveryTask.order_id == order.id))
        assert product.name == 'SaaS-APP1'
        assert (order.main_status, order.delivery_status, sub.status, task.status) == (
            'completed', 'accepted', 'active', 'completed')
        assert sub.external_tenant_id and sub.expires_at and task.retry_count == 0
        operations = db.scalars(select(m.SaaSOperation).where(
            m.SaaSOperation.subscription_id == sub.id)).all()
        assert {'OPEN', 'USER_ASSIGN', 'STATUS', 'RENEW'} <= {row.operation for row in operations}
        assert all(row.status == 'succeeded' for row in operations)
        provider = db.scalar(select(m.User).join(m.Membership, m.User.id == m.Membership.user_id).where(
            m.Membership.enterprise_id == product.enterprise_id, m.Membership.role == 'super_admin',
            m.Membership.status == 'active', m.User.platform_role == '', m.User.verified_status == 'verified'))
        buyer = db.get(m.User, order.buyer_user_id)
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        tokens = {key: {'Authorization': 'Bearer ' + m.issue_token(user)}
            for key, user in [('provider', provider), ('buyer', buyer), ('admin', admin)]}
        prefix = '/api/products/' + product.id
        machine = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == product.id))
        config = db.scalar(select(m.SaaSIntegrationConfig).where(m.SaaSIntegrationConfig.product_id == product.id))
        original_machine = (machine.id, machine.client_id, machine.client_secret, machine.scope)
        original_config = (config.id, config.client_id, config.client_secret, config.token_url)
        assert machine.client_id and machine.client_secret
        assert (config.client_id, config.client_secret) == (machine.client_id, machine.client_secret)
    with httpx.Client(base_url='http://192.168.10.10:30080', trust_env=False, timeout=20) as client:
        oidc = client.get(prefix + '/oidc-credentials-download', headers=tokens['provider'])
        oauth = client.get(prefix + '/oauth-credentials-download', headers=tokens['provider'])
        assert oidc.status_code == 200 and oauth.status_code == 410
        assert 'saas-integration-credentials-download' in oauth.json()['detail']
        assert oidc.headers['cache-control'] == 'no-store'
        login = oidc.json()
        assert login['client_secret'] and login['client_id'] not in oauth.text
        bundle = client.get(prefix + '/saas-integration-credentials-download', headers=tokens['provider'])
        assert bundle.status_code == 200 and bundle.headers['cache-control'] == 'no-store'
        combined = bundle.json()
        assert combined['schema_version'] == '2.0' and combined['product']['id'] == product.id
        assert combined['oidc_sso'] == login
        assert 'oauth2_api' not in combined
        assert combined['resource_server'] == {
            'authentication': 'Bearer', 'validation_method': 'introspection',
            'introspection_url': m.platform_oauth_issuer().rstrip('/') + '/oauth/introspect',
            'issuer': m.platform_oauth_issuer(), 'audience': 'market-resource',
            'scopes': sorted(original_machine[3].split())}
        assert original_machine[1] not in bundle.text and original_machine[2] not in bundle.text
        assert m.platform_oauth_token_url() not in bundle.text
        assert client.get(prefix + '/saas-integration-credentials-download',
            headers=tokens['provider']).content == bundle.content
        assert combined['instructions']
        for actor in ('buyer', 'admin'):
            assert client.get(prefix + '/saas-integration-credentials-download', headers=tokens[actor]).status_code == 403
            assert client.get(prefix + '/oidc-credentials-download', headers=tokens[actor]).status_code == 403
            assert client.get(prefix + '/oauth-credentials-download', headers=tokens[actor]).status_code == 403
        access = client.get('/api/orders/' + order.id + '/delivery-access', headers=tokens['buyer'])
        assert access.status_code == 200 and access.json()['ready'] is True
        assert '/sso/apps/' in access.json()['saas']['application_url']
        policy = client.get('/api/settings/application-address-policy', headers=tokens['buyer'])
        assert policy.json() == {'allow_http': True}
    with m.SessionLocal() as db:
        machine = db.get(m.OAuthClient, original_machine[0])
        config = db.get(m.SaaSIntegrationConfig, original_config[0])
        assert (machine.id, machine.client_id, machine.client_secret, machine.scope) == original_machine
        assert (config.id, config.client_id, config.client_secret, config.token_url) == original_config
    print(json.dumps({'status': 'passed', 'order_no': order.order_no,
        'delivery_status': 'accepted', 'subscription_status': 'active',
        'retry_count': task.retry_count, 'operation_count': len(operations),
        'provider_unified_download': True, 'existing_secrets_preserved': True,
        'buyer_and_platform_download_denied': True,
        'http_policy_enabled': True, 'browser_sso': 'separate verification'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
