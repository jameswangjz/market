"""Authlib authorization-code RP; credentials never leave private SQLite.

Use a PVC for MOCK_SAAS_DB_PATH (default /data/mock-saas.sqlite3).
Development fixture administration is disabled unless explicitly enabled.
Public issuer URLs are validated, but all OP HTTP calls use the fixed trusted
backchannel base. No JWT jku/x5u or supplied metadata URL is ever fetched.
"""
import html
import json
import os
import secrets
import time
from urllib.parse import urlsplit

from authlib.integrations.httpx_client import OAuth2Client
from authlib.integrations.base_client.errors import OAuthError
from authlib.jose import JsonWebToken
from authlib.jose.errors import JoseError
from authlib.oidc.core import CodeIDToken
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from .store import hashed, tenant_expired
from .admin import install_admin


FLOW_COOKIE = 'mock_saas_oidc_flow'
SESSION_COOKIE = 'mock_saas_session'
NO_STORE = {'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'no-referrer'}
IDENTITY_FIELDS = ('sub', 'enterprise_id', 'subscription_id', 'tenant_id')


class ClientBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_id: str = Field(min_length=1, max_length=180)
    client_secret: str = Field(min_length=1, max_length=4096)
    redirect_uri: str = Field(min_length=1, max_length=2000)
    product_id: str = Field(min_length=1, max_length=180)
    issuer: str = Field(min_length=1, max_length=2000)
    product_name: str = Field(default='应用', max_length=200)


class AdminClientBody(ClientBody):
    client_secret: str | None = Field(default=None, max_length=4096)


def configured_url(value, allow_http, *, backchannel=False):
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in ({'http', 'https'} if allow_http or backchannel else {'https'})
            or parsed.path not in ('', '/') or any(c.isspace() for c in value)):
        raise HTTPException(503, 'SSO configuration unavailable')
    try:
        parsed.port
    except ValueError:
        raise HTTPException(503, 'SSO configuration unavailable') from None
    return value.rstrip('/')


def install(app, store, *, transport=None):
    allow_http = os.getenv('OIDC_ALLOW_HTTP_DEVELOPMENT', 'false') == 'true'
    public = os.getenv('MOCK_SAAS_PUBLIC_BASE_URL', 'http://127.0.0.1:30082').rstrip('/')
    issuer = os.getenv('OIDC_ISSUER', '').rstrip('/')
    backchannel = os.getenv('OIDC_BACKCHANNEL_BASE_URL', 'http://market-api:8000').rstrip('/')
    admin_enabled = os.getenv('MOCK_SAAS_ENABLE_TEST_ADMIN', 'false') == 'true'
    admin_secret = os.getenv('MOCK_SAAS_ADMIN_SECRET', '')

    def settings():
        return (configured_url(public, allow_http), configured_url(issuer, allow_http),
                configured_url(backchannel, allow_http, backchannel=True))

    def admin(request):
        if not admin_enabled:
            raise HTTPException(404, 'Not found')
        supplied = request.headers.get('X-Mock-Admin-Secret', '')
        if not admin_secret or not secrets.compare_digest(supplied.encode(), admin_secret.encode()):
            raise HTTPException(403, 'Forbidden')

    def http():
        return httpx.Client(transport=transport, timeout=5, follow_redirects=False, trust_env=False)

    def json_response(response):
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError('Invalid OP response')
        return data

    def metadata():
        _, expected_issuer, internal = settings()
        with http() as client:
            data = json_response(client.get(internal + '/.well-known/openid-configuration'))
        expected = {'issuer': expected_issuer, 'authorization_endpoint': expected_issuer + '/oidc/authorize',
                    'token_endpoint': expected_issuer + '/oidc/token',
                    'userinfo_endpoint': expected_issuer + '/oidc/userinfo', 'jwks_uri': expected_issuer + '/oidc/jwks'}
        if any(data.get(key) != value for key, value in expected.items()):
            raise ValueError('Untrusted OP metadata')
        return data

    install_admin(app, store, enabled=admin_enabled, admin=admin, settings=settings,
                  metadata=metadata, body_type=AdminClientBody)

    def checked_identity(claims, info, flow, client):
        if any(not isinstance(claims.get(k), str) or not claims[k]
               or info.get(k) != claims[k] for k in IDENTITY_FIELDS):
            raise ValueError('Identity mismatch')
        if claims['enterprise_id'] != flow['enterprise_id']:
            raise ValueError('Enterprise mismatch')
        tenant = store.tenant(claims['tenant_id'])
        if (not tenant or not tenant.get('login_open') or tenant_expired(tenant) or tenant.get('status') not in {'opened', 'active'}
                or tenant.get('product_id') != client['product_id']
                or tenant.get('enterprise_id') != claims['enterprise_id']
                or tenant.get('subscription_id') != claims['subscription_id']):
            raise ValueError('Tenant is not open for this identity')
        with store.connect() as db:
            row = db.execute('SELECT payload FROM users WHERE tenant_id = ? AND user_id = ?',
                             (claims['tenant_id'], claims['sub'])).fetchone()
            if row and json.loads(row['payload']).get('status') != 'active':
                raise ValueError('Local user disabled')
            mapping = db.execute('SELECT enterprise_id, subscription_id FROM mappings '
                'WHERE client_id = ? AND sub = ? AND tenant_id = ?',
                (client['client_id'], claims['sub'], claims['tenant_id'])).fetchone()
            if mapping and (mapping['enterprise_id'] != claims['enterprise_id']
                            or mapping['subscription_id'] != claims['subscription_id']):
                raise ValueError('Local identity mapping mismatch')
        return tenant

    def userinfo(access_token):
        _, _, internal = settings()
        with http() as client:
            return json_response(client.get(internal + '/oidc/userinfo',
                                           headers={'Authorization': 'Bearer ' + access_token}))

    @app.post('/_test/oidc/clients', status_code=201)
    def configure(body: ClientBody, request: Request):
        admin(request)
        base, expected_issuer, _ = settings()
        if body.issuer != expected_issuer or body.redirect_uri != base + '/sso/callback':
            raise HTTPException(400, 'Client issuer or redirect URI mismatch')
        with store.connect() as db:
            if db.execute('SELECT 1 FROM clients WHERE client_id = ?', (body.client_id,)).fetchone():
                raise HTTPException(409, 'Client already configured')
            db.execute('INSERT INTO clients VALUES (?, ?)', (body.client_id, json.dumps(body.model_dump())))
        return JSONResponse({key: getattr(body, key) for key in ('client_id', 'redirect_uri', 'product_id', 'issuer')},
                            status_code=201, headers=NO_STORE)

    @app.delete('/_test/oidc/clients/{client_id}')
    def remove_client(client_id: str, request: Request):
        admin(request)
        with store.connect() as db:
            result = db.execute('DELETE FROM clients WHERE client_id = ?', (client_id,))
        return JSONResponse({'deleted': bool(result.rowcount), 'client_id': client_id}, headers=NO_STORE)

    @app.get('/sso/apps/{client_id}')
    def login(client_id: str, enterprise_id: str):
        if not enterprise_id or len(enterprise_id) > 180:
            raise HTTPException(400, 'Enterprise required')
        client = store.client(client_id)
        if not client:
            raise HTTPException(404, 'Application unavailable')
        try:
            data = metadata()
            state, nonce, verifier, browser = (secrets.token_urlsafe(48) for _ in range(4))
            with OAuth2Client(client_id, redirect_uri=client['redirect_uri'], scope='openid profile email',
                              code_challenge_method='S256') as oauth:
                url, _ = oauth.create_authorization_url(data['authorization_endpoint'], state=state,
                    nonce=nonce, code_verifier=verifier, enterprise_id=enterprise_id)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT config FROM clients WHERE client_id=?', (client_id,)).fetchone()
                if not current or json.loads(current['config']) != client:
                    raise ValueError('Client configuration changed')
                db.execute('DELETE FROM flows WHERE expires_at <= ?', (int(time.time()),))
                db.execute('INSERT INTO flows(state_hash,browser_hash,client_id,enterprise_id,nonce,verifier,expires_at) '
                           'VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (hashed(state), hashed(browser), client_id, enterprise_id, nonce, verifier, int(time.time()) + 300))
            response = RedirectResponse(url, status_code=302, headers=NO_STORE)
            response.set_cookie(FLOW_COOKIE, browser, max_age=300, httponly=True, samesite='lax',
                                secure=public.startswith('https://'), path='/sso')
            return response
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, 'SSO provider unavailable') from None

    @app.get('/sso/callback')
    def callback(request: Request):
        query = request.query_params
        state = query.get('state', '')
        cookie = request.cookies.get(FLOW_COOKIE, '')
        if (not state or not cookie or len(state) > 255 or len(cookie) > 255
                or any(len(query.getlist(key)) != 1 for key in query)):
            raise HTTPException(400, 'Invalid SSO callback')
        flow = store.consume_flow(state, cookie)
        if not flow:
            raise HTTPException(400, 'SSO request expired or already used')
        client = store.client(flow['client_id'])
        if not client or query.get('error') or not query.get('code') or len(query['code']) > 255:
            raise HTTPException(400, 'SSO authorization refused')
        try:
            metadata()
            _, expected_issuer, internal = settings()
            with OAuth2Client(client['client_id'], client['client_secret'],
                    token_endpoint_auth_method='client_secret_basic', redirect_uri=client['redirect_uri'],
                    transport=transport, timeout=5, follow_redirects=False, trust_env=False) as oauth:
                token = oauth.fetch_token(internal + '/oidc/token', grant_type='authorization_code',
                    code=query['code'], code_verifier=flow['verifier'])
            if (token.get('token_type', '').lower() != 'bearer'
                    or not isinstance(token.get('access_token'), str)
                    or not token['access_token'] or len(token['access_token']) > 4096
                    or not isinstance(token.get('id_token'), str)):
                raise ValueError('Invalid token response')
            with http() as remote:
                jwks = json_response(remote.get(internal + '/oidc/jwks'))
            claims = JsonWebToken(['RS256']).decode(token['id_token'], jwks, claims_cls=CodeIDToken,
                claims_options={'iss': {'essential': True, 'value': expected_issuer},
                                'aud': {'essential': True, 'value': client['client_id']},
                                'at_hash': {'essential': True}},
                claims_params={'nonce': flow['nonce'], 'client_id': client['client_id'],
                               'access_token': token['access_token']})
            claims.validate(leeway=0)
            info = userinfo(token['access_token'])
            checked_identity(claims, info, flow, client)
            identity = {key: claims[key] for key in IDENTITY_FIELDS}
            expires = min(int(claims['exp']), int(time.time()) + min(300, int(token['expires_in'])))
            if expires <= int(time.time()):
                raise ValueError('Expired session')
            session = secrets.token_urlsafe(48)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT config FROM clients WHERE client_id = ?', (client['client_id'],)).fetchone()
                active_flow = db.execute('SELECT 1 FROM flows WHERE state_hash=?', (flow['state_hash'],)).fetchone()
                if not current or json.loads(current['config']) != client or not active_flow:
                    raise ValueError('Client configuration changed')
                db.execute('INSERT OR IGNORE INTO users VALUES (?, ?, ?)', (identity['tenant_id'], identity['sub'],
                    json.dumps({'user_id': identity['sub'], 'external_user_id': 'mock-user-' + identity['sub'][:12],
                                'status': 'active'})))
                db.execute('INSERT OR IGNORE INTO mappings VALUES (?, ?, ?, ?, ?)',
                    (client['client_id'], identity['sub'], identity['tenant_id'],
                     identity['enterprise_id'], identity['subscription_id']))
                db.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?)',
                    (hashed(session), client['client_id'], json.dumps(identity), token['access_token'], expires))
            response = RedirectResponse('/app', status_code=303, headers=NO_STORE)
            response.delete_cookie(FLOW_COOKIE, path='/sso')
            response.set_cookie(SESSION_COOKIE, session, max_age=expires - int(time.time()), httponly=True,
                                samesite='lax', secure=public.startswith('https://'), path='/')
            return response
        except (httpx.HTTPError, OAuthError, JoseError, ValueError, TypeError, KeyError):
            raise HTTPException(401, 'SSO identity verification failed') from None

    @app.get('/app', response_class=HTMLResponse)
    def application(request: Request):
        cookie = request.cookies.get(SESSION_COOKIE, '')
        with store.connect() as db:
            row = db.execute('SELECT * FROM sessions WHERE session_hash = ?', (hashed(cookie),)).fetchone()
        if not row or row['expires_at'] <= int(time.time()):
            raise HTTPException(401, 'Application session expired')
        client = store.client(row['client_id'])
        identity = json.loads(row['identity'])
        try:
            if not client:
                raise ValueError('Client removed')
            info = userinfo(row['access_token'])
            tenant = checked_identity(identity, info, identity, client)
        except (httpx.HTTPError, ValueError, KeyError):
            with store.connect() as db:
                db.execute('DELETE FROM sessions WHERE session_hash = ?', (row['session_hash'],))
            raise HTTPException(401, 'Application access no longer available') from None
        name = info.get('name') or '用户'
        tenant_name = tenant.get('tenant_name') or tenant.get('enterprise_name') or '企业工作区'
        page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" '
        page += 'content="width=device-width,initial-scale=1"><title>应用</title><style>'
        page += 'body{margin:0;font:16px system-ui;color:#20252b;background:#f5f7f8}header,main{padding:24px;'
        page += 'max-width:960px;margin:auto}header{border-bottom:1px solid #dce1e5}h1{font-size:28px;'
        page += 'overflow-wrap:anywhere}p{overflow-wrap:anywhere}button{padding:8px 16px;cursor:pointer}'
        page += '</style><header><strong>' + html.escape(client['product_name']) + '</strong></header><main><h1>'
        page += html.escape(tenant_name) + '</h1><p>' + html.escape(name) + '</p>'
        page += '<form method="post" action="/sso/logout"><button type="submit">退出</button></form></main></html>'
        return HTMLResponse(page, headers={**NO_STORE, 'Content-Security-Policy':
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"})

    @app.post('/sso/logout')
    def logout(request: Request):
        origin = request.headers.get('origin')
        if origin and origin != public:
            raise HTTPException(403, 'Forbidden')
        with store.connect() as db:
            db.execute('DELETE FROM sessions WHERE session_hash = ?',
                       (hashed(request.cookies.get(SESSION_COOKIE, '')),))
        response = JSONResponse({'status': 'signed_out'}, headers=NO_STORE)
        response.delete_cookie(SESSION_COOKIE, path='/')
        return response
