"""Backfill the designated local mock product and configure its real OIDC RP."""
import json
import os
from pathlib import Path

import httpx
from sqlalchemy import select

os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
from app import main as m

PRODUCT_ID = 'e101107c3f6414ab48b9ebc43de368e8'


def main():
    secret_path = Path('/tmp/market-mock-admin-key')
    try:
        admin_secret = secret_path.read_text().strip()
        if len(admin_secret) != 64:
            raise RuntimeError('Mock administration credential unavailable')
        with m.SessionLocal() as db:
            product = db.scalar(select(m.Product).where(m.Product.id == PRODUCT_ID).with_for_update())
            admin = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
            if (not product or product.name != 'SaaS-APP1' or product.product_type != 'saas'
                    or product.status != 'published' or not admin or admin.platform_role != 'super_admin'):
                raise RuntimeError('Scoped mock product or audit actor changed')
            policy = db.scalar(select(m.SystemSetting).where(
                m.SystemSetting.setting_key == 'application_integration_allow_http'))
            before = {'allow_http': m.application_address_policy['allow_http'](db)}
            if not policy:
                policy = m.SystemSetting(setting_key='application_integration_allow_http')
                db.add(policy)
            policy.setting_value, policy.updated_by = 'true', admin.email
            db.flush()
            m.audit(db, admin.email, 'update_application_address_policy', 'system_setting',
                policy.setting_key, 'Enable HTTP explicitly for local SaaS development',
                before=before, after={'allow_http': True})
            product.oidc_redirect_uris_json = json.dumps(['http://192.168.10.10:30083/sso/callback'])
            application_url_before = product.application_url
            product.integration_api_url = 'http://market-mock-saas:8200/isv.php'
            config = db.scalar(select(m.SaaSIntegrationConfig).where(
                m.SaaSIntegrationConfig.product_id == PRODUCT_ID).with_for_update())
            if not config:
                raise RuntimeError('Existing mock integration unavailable')
            config.base_url, config.operation_path = 'http://market-mock-saas:8200', '/isv.php'
            machine = m.ensure_oauth_client(db, product)
            machine.scope = config.scope = 'saas.tenant saas.user saas.department'
            config.token_url = 'http://market-api:8000/oauth/token'
            oidc, _ = m.oidc_provider['ensure_client'](db, product, admin.email)
            oidc_secret = m.oidc_provider['client_secret'](oidc)
            product.application_url = f'http://192.168.10.10:30083/sso/apps/{oidc.client_id}'
            m.audit(db, admin.email, 'configure_saas_app1_oidc_entry', 'product', product.id,
                'Set scoped mock SaaS launch URL to its registered OIDC entry route',
                before={'application_url': application_url_before},
                after={'application_url': product.application_url, 'oidc_client_id': oidc.client_id})
            body = {'client_id': oidc.client_id, 'client_secret': oidc_secret,
                'product_id': product.id, 'product_name': product.name,
                'issuer': 'http://192.168.10.10:30080',
                'redirect_uri': 'http://192.168.10.10:30083/sso/callback'}
            db.commit()
        with httpx.Client(timeout=15, trust_env=False) as client:
            result = client.post('http://market-mock-saas:8200/_test/oidc/clients',
                headers={'X-Mock-Admin-Secret': admin_secret}, json=body)
            if result.status_code != 201:
                raise RuntimeError('Mock OIDC configuration requires inspection; HTTP ' + str(result.status_code))
        print(json.dumps({'product': 'SaaS-APP1', 'oidc_configured': True,
            'http_enabled': True, 'credentials_rotated': False, 'orders_modified': False}))
    finally:
        secret_path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
