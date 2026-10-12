"""Bind only the user-designated simulated SaaS-APP1 to local mock endpoints."""
import json
import os

os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
from app import main as m
from sqlalchemy import select

PRODUCT_ID = 'e101107c3f6414ab48b9ebc43de368e8'
SCOPES = 'saas.tenant saas.user saas.department'


def main():
    with m.SessionLocal() as db:
        product = db.scalar(select(m.Product).where(m.Product.id == PRODUCT_ID).with_for_update())
        if not product or product.name != 'SaaS-APP1' or product.product_type != 'saas':
            raise RuntimeError('Product configuration scope changed')
        config = db.scalar(select(m.SaaSIntegrationConfig).where(
            m.SaaSIntegrationConfig.product_id == PRODUCT_ID).with_for_update())
        client = db.scalar(select(m.OAuthClient).where(
            m.OAuthClient.product_id == PRODUCT_ID).with_for_update())
        admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
        if (not config or not client or client.status != 'active' or not admin
                or admin.platform_role != 'super_admin'
                or config.client_id != client.client_id
                or config.client_secret != client.client_secret):
            raise RuntimeError('Existing integration credentials or audit actor require inspection')

        def snapshot():
            return {'application_url': product.application_url,
                'integration_api_url': product.integration_api_url,
                'oidc_redirect_uris': product.oidc_redirect_uris_json,
                'base_url': config.base_url, 'operation_path': config.operation_path,
                'token_url': config.token_url, 'scope': config.scope,
                'oauth_client_scope': client.scope}

        before = snapshot()
        product.application_url = 'http://192.168.10.10:30083/app'
        product.integration_api_url = 'http://market-mock-saas:8200/isv.php'
        product.oidc_redirect_uris_json = json.dumps(['http://192.168.10.10:30083/sso/callback'])
        config.base_url = 'http://market-mock-saas:8200'
        config.operation_path = '/isv.php'
        config.token_url = 'http://market-api:8000/oauth/token'
        config.scope = client.scope = SCOPES
        after = snapshot()
        if before != after:
            m.audit(db, admin.email, 'configure_local_mock_saas', 'product', product.id,
                'Bind simulated SaaS-APP1 to local mock; preserve orders, economics and credentials',
                tenant_id=product.enterprise_id, before=before, after=after)
        db.commit()
        print(json.dumps({'product': product.name, 'configuration': after,
            'credentials_rotated': False, 'orders_modified': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
