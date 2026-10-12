"""OIDC protocol core; browser login/consent bridge is a separate delivery gate."""
import hashlib
import json
import os
import secrets
import time
from datetime import timezone
from functools import lru_cache
from urllib.parse import urlsplit

from authlib.jose import JsonWebKey
from authlib.oauth2.rfc6749 import AuthorizationServer, OAuth2Request
from authlib.oauth2.rfc6749.errors import InvalidGrantError, InvalidRequestError, OAuth2Error
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant
from authlib.oauth2.rfc6749.requests import BasicOAuth2Payload
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oidc.core import UserInfo
from authlib.oidc.core.grants import OpenIDCode
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, ForeignKey, Integer, String, Text, UniqueConstraint, select, update
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from .registration_config import validate_redirect_uris

SCOPES = {'openid', 'profile', 'email'}
NO_STORE = {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def issuer():
    value = os.getenv('OIDC_ISSUER', '').rstrip('/')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(503, 'OIDC发行者配置无效') from exc
    development = os.getenv('OIDC_ALLOW_HTTP_DEVELOPMENT', 'false') == 'true'
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or port == 0 or parsed.query or parsed.fragment or '\\' in value
            or any(ch.isspace() or ord(ch) < 32 for ch in value)
            or parsed.scheme != 'https' and not (development and parsed.scheme == 'http')):
        raise HTTPException(503, 'OIDC发行者未配置；生产环境必须使用HTTPS')
    return value


@lru_cache(maxsize=2)
def signing_key(pem):
    if not pem:
        raise HTTPException(503, 'OIDC持久化签名密钥尚未配置')
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError('RSA 2048 required')
        jwk = JsonWebKey.import_key(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)).as_dict()
        kid = digest(jwk['n'])[:24]
        return pem, {**jwk, 'kid': kid, 'use': 'sig', 'alg': 'RS256'}
    except (ValueError, TypeError) as exc:
        raise HTTPException(503, 'OIDC签名密钥配置无效') from exc


def keys():
    return signing_key(os.getenv('OIDC_SIGNING_KEY_PEM', ''))


class AuthorizationBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    response_type: str = Field(default='code', max_length=30)
    client_id: str = Field(min_length=1, max_length=180)
    redirect_uri: str = Field(min_length=1, max_length=2000)
    scope: str = Field(default='openid', max_length=100)
    state: str = Field(min_length=16, max_length=255)
    nonce: str = Field(min_length=16, max_length=255)
    code_challenge: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$')
    code_challenge_method: str = Field(default='S256', pattern=r'^S256$')
    enterprise_id: str = Field(min_length=1, max_length=36)


def install(ns):
    if 'oidc_provider' in ns:
        return ns['oidc_provider']
    Base = ns['Base']

    class OIDCClient(Base):
        __tablename__ = 'oidc_clients'
        id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        product_id = Column(ForeignKey('products.id'), unique=True, nullable=False)
        client_id = Column(String(180), unique=True, nullable=False)
        secret_hash = Column(String(64), nullable=False)
        secret_encrypted = Column(Text, nullable=True)
        status = Column(String(30), default='active', nullable=False)

        def get_client_id(self):
            return self.client_id

        def get_default_redirect_uri(self):
            return None

        def check_redirect_uri(self, uri):
            return uri in self.redirect_uris

        def get_allowed_scope(self, scope):
            return ' '.join(sorted(set((scope or '').split()) & SCOPES))

        def check_client_secret(self, secret):
            return bool(secret) and secrets.compare_digest(self.secret_hash, digest(secret))

        def check_endpoint_auth_method(self, method, endpoint):
            return endpoint == 'token' and method in {'client_secret_basic', 'client_secret_post'}

        def check_response_type(self, response_type):
            return response_type == 'code'

        def check_grant_type(self, grant_type):
            return grant_type == 'authorization_code'

    class OIDCCode(Base):
        __tablename__ = 'oidc_authorization_codes'
        __table_args__ = (UniqueConstraint('client_id', 'nonce_hash', name='uq_oidc_client_nonce'),)
        id = Column(String(64), primary_key=True)
        client_id = Column(ForeignKey('oidc_clients.client_id'), nullable=False, index=True)
        user_id = Column(ForeignKey('users.id'), nullable=False)
        enterprise_id = Column(ForeignKey('enterprises.id'), nullable=False)
        subscription_id = Column(ForeignKey('saas_subscriptions.id'), nullable=False)
        redirect_uri = Column(Text, nullable=False)
        scope = Column(String(100), nullable=False)
        nonce = Column(String(255), nullable=False)
        nonce_hash = Column(String(64), nullable=False)
        code_challenge = Column(String(128), nullable=False)
        code_challenge_method = Column(String(10), nullable=False)
        auth_time = Column(Integer, nullable=False)
        expires_at = Column(Integer, nullable=False)
        consumed_at = Column(Integer, nullable=True)
        access_hash = Column(String(64), unique=True, nullable=True)
        access_expires_at = Column(Integer, nullable=True)
        revoked_at = Column(Integer, nullable=True)

        def get_redirect_uri(self):
            return self.redirect_uri

        def get_scope(self):
            return self.scope

        def get_nonce(self):
            return self.nonce

        def get_auth_time(self):
            return self.auth_time

        def get_acr(self):
            return None

        def get_amr(self):
            return None

    def client_for(db, client_id):
        client = db.scalar(select(OIDCClient).where(OIDCClient.client_id == client_id,
            OIDCClient.status == 'active').execution_options(populate_existing=True))
        product = db.get(ns['Product'], client.product_id) if client else None
        if (not product or product.product_type != 'saas' or product.delivery_method != 'tenant_access'
                or product.status == 'security_unpublished'):
            return None
        client.redirect_uris = json.loads(product.oidc_redirect_uris_json or '[]')
        return client

    def credential_cipher():
        key = os.getenv('OIDC_CREDENTIAL_ENCRYPTION_KEY', '')
        if not key:
            raise HTTPException(503, 'OIDC凭据加密密钥尚未配置')
        try:
            return Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise HTTPException(503, 'OIDC凭据加密密钥配置无效') from exc

    def ensure_client(db, product, actor='system'):
        redirects = json.loads(product.oidc_redirect_uris_json or '[]')
        if not isinstance(redirects, list) or not redirects or any(
                not isinstance(uri, str) or not valid_redirect(uri, db=db) for uri in redirects):
            raise HTTPException(409, 'SaaS产品须登记有效OIDC回调地址后才能发布')
        client = db.scalar(select(OIDCClient).where(OIDCClient.product_id == product.id)
                           .with_for_update())
        if client:
            if not client.secret_encrypted:
                raise HTTPException(409, '现有OIDC客户端仅保存不可恢复的密钥摘要；请通过受控凭据轮换流程处理')
            if client.status != 'active':
                raise HTTPException(409, '现有OIDC客户端已停用；请通过受控凭据轮换流程处理')
            client_secret(client)
            return client, None
        secret = secrets.token_urlsafe(48)
        cipher = credential_cipher()
        client = OIDCClient(product_id=product.id, client_id='oidc_' + secrets.token_hex(16),
            secret_hash=digest(secret), secret_encrypted=cipher.encrypt(secret.encode()).decode())
        db.add(client)
        db.flush()
        ns['audit'](db, actor, 'create_oidc_client', 'oidc_client', client.id,
            '自动创建SaaS OIDC客户端；不包含密钥', tenant_id=product.enterprise_id)
        return client, secret

    def client_secret(client):
        if not client.secret_encrypted:
            raise HTTPException(409, '现有OIDC客户端仅保存不可恢复的密钥摘要；请通过受控凭据轮换流程处理')
        try:
            secret = credential_cipher().decrypt(client.secret_encrypted.encode()).decode()
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(503, 'OIDC凭据无法解密；请检查持久化加密密钥配置') from exc
        if not secrets.compare_digest(client.secret_hash, digest(secret)):
            raise HTTPException(503, 'OIDC凭据加密内容与摘要不匹配')
        return secret

    def eligible(db, user, client, enterprise_id, subscription_id=None):
        if (not user or not user.is_active or user.activation_status != 'active'
                or user.verified_status != 'verified' or user.platform_role):
            raise HTTPException(403, '该用户不可使用企业SaaS单点登录')
        enterprise = db.get(ns['Enterprise'], enterprise_id)
        member = db.scalar(select(ns['Membership']).where(ns['Membership'].user_id == user.id,
            ns['Membership'].enterprise_id == enterprise_id, ns['Membership'].status == 'active'))
        if not enterprise or enterprise.verification_status != 'verified' or not member:
            raise HTTPException(403, '企业或成员身份无效')
        if member.role not in {'super_admin', 'enterprise_admin'}:
            grant = db.scalar(select(ns['ApplicationAccessGrant']).where(
                ns['ApplicationAccessGrant'].enterprise_id == enterprise_id,
                ns['ApplicationAccessGrant'].product_id == client.product_id,
                ns['ApplicationAccessGrant'].user_id == user.id,
                ns['ApplicationAccessGrant'].status == 'active'))
            if not grant:
                raise HTTPException(403, '用户未获授该SaaS应用访问权限')
        binding = db.scalar(select(ns['SaaSCombinationBinding']).where(
            ns['SaaSCombinationBinding'].enterprise_id == enterprise_id,
            ns['SaaSCombinationBinding'].product_id == client.product_id))
        subscription = db.get(ns['SaaSSubscription'], binding.subscription_id) if binding else None
        active = ns['subscription_terms']['current_entitlement'](db, enterprise_id, client.product_id)
        instant = ns['now']().timestamp()
        def timestamp(value):
            return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
        if (not subscription or subscription.status != 'active' or not subscription.external_tenant_id
                or subscription.enterprise_id != enterprise_id or subscription.product_id != client.product_id
                or subscription_id and subscription.id != subscription_id
                or not subscription.starts_at or timestamp(subscription.starts_at) > instant
                or not subscription.expires_at or timestamp(subscription.expires_at) <= instant
                or not active or active['delivery_method'] != 'tenant_access'
                or active['version_id'] != subscription.version_id):
            raise HTTPException(403, 'SaaS租户未开通、订阅已过期或版本不一致')
        return subscription

    def user_info(user, scope, enterprise_id, subscription):
        info = UserInfo(sub=user.id, enterprise_id=enterprise_id, tenant_id=subscription.external_tenant_id,
                        subscription_id=subscription.id)
        if 'profile' in scope.split():
            info['name'] = user.name
        if 'email' in scope.split():
            info['email'], info['email_verified'] = user.email or '', bool(user.email_verified)
        return info

    class ProtocolRequest(OAuth2Request):
        def __init__(self, method, data, headers=None):
            # Transport policy is enforced on the configured issuer. Authlib's
            # request is an in-process adapter, not a user-controlled HTTP URL.
            super().__init__(method, 'https://oidc.internal/protocol', headers=headers)
            self.payload = BasicOAuth2Payload(data)

        @property
        def form(self):
            return self.payload.data

        @property
        def args(self):
            return {}

    class Server(AuthorizationServer):
        def __init__(self, db):
            super().__init__(scopes_supported=list(SCOPES))
            self.db = db
            self.context = {}
            self.register_grant(Grant, [StrictPKCE(), OpenID(db)])
            self.register_token_generator('authorization_code', self.generate)

        def query_client(self, client_id):
            return client_for(self.db, client_id)

        def create_oauth2_request(self, request):
            return request

        def send_signal(self, name, *args, **kwargs):
            pass

        def handle_response(self, status, body, headers):
            return status, body, headers

        def generate(self, **kwargs):
            return {'token_type': 'Bearer', 'access_token': secrets.token_urlsafe(48),
                    'expires_in': 300, 'scope': kwargs['scope']}

        def save_token(self, token, request):
            code = request.authorization_code
            code.access_hash = digest(token['access_token'])
            code.access_expires_at = int(time.time()) + 300

    class StrictPKCE(CodeChallenge):
        SUPPORTED_CODE_CHALLENGE_METHOD = ['S256']

        def validate_code_challenge(self, grant, redirect_uri):
            if (not grant.request.payload.data.get('code_challenge')
                    or grant.request.payload.data.get('code_challenge_method') != 'S256'):
                raise InvalidRequestError('PKCE S256 is required')
            super().validate_code_challenge(grant, redirect_uri)

    class Grant(AuthorizationCodeGrant):
        def save_authorization_code(self, code, request):
            context = self.server.context
            item = OIDCCode(id=digest(code), client_id=request.client.client_id,
                user_id=request.user.id, enterprise_id=context['enterprise_id'],
                subscription_id=context['subscription_id'], redirect_uri=request.payload.redirect_uri,
                scope=request.payload.scope, nonce=request.payload.data['nonce'],
                nonce_hash=digest(request.payload.data['nonce']),
                code_challenge=request.payload.data['code_challenge'], code_challenge_method='S256',
                auth_time=context['auth_time'], expires_at=int(time.time()) + 120)
            self.server.db.add(item)
            self.server.db.flush()

        def query_authorization_code(self, code, client):
            if not isinstance(code, str) or len(code) > 255:
                return None
            row = self.server.db.scalar(select(OIDCCode).where(OIDCCode.id == digest(code),
                OIDCCode.client_id == client.client_id).with_for_update().execution_options(populate_existing=True))
            if row and row.consumed_at is not None:
                row.revoked_at = int(time.time())
                return None
            return row if row and row.expires_at > int(time.time()) and row.revoked_at is None else None

        def authenticate_user(self, code):
            user = self.server.db.get(ns['User'], code.user_id)
            try:
                subscription = eligible(self.server.db, user, self.request.client,
                    code.enterprise_id, code.subscription_id)
                self.server.context = {'enterprise_id': code.enterprise_id, 'subscription': subscription}
                return user
            except HTTPException:
                return None

        def delete_authorization_code(self, code):
            result = self.server.db.execute(update(OIDCCode).where(OIDCCode.id == code.id,
                OIDCCode.consumed_at.is_(None)).values(consumed_at=int(time.time()))
                .execution_options(synchronize_session=False))
            if result.rowcount != 1:
                raise InvalidGrantError('Authorization code was already consumed')
            code.consumed_at = int(time.time())

    class OpenID(OpenIDCode):
        def __init__(self, db):
            super().__init__(require_nonce=True)
            self.db = db

        def exists_nonce(self, nonce, request):
            return bool(self.db.scalar(select(OIDCCode.id).where(
                OIDCCode.client_id == request.client.client_id, OIDCCode.nonce_hash == digest(nonce))))

        def get_jwt_config(self, grant, client):
            pem, jwk = keys()
            return {'key': pem, 'alg': 'RS256', 'iss': issuer(), 'exp': 300, 'kid': jwk['kid']}

        def generate_user_info(self, user, scope):
            # The extension instance is scoped to its server/request.
            context = self.context
            return user_info(user, scope, context['enterprise_id'], context['subscription'])

        def process_token(self, grant, response):
            self.context = grant.server.context
            return super().process_token(grant, response)

    @ns['app'].get('/.well-known/openid-configuration')
    def discovery():
        base = issuer()
        keys()
        return {'issuer': base, 'authorization_endpoint': base + '/oidc/authorize',
            'token_endpoint': base + '/oidc/token', 'userinfo_endpoint': base + '/oidc/userinfo',
            'jwks_uri': base + '/oidc/jwks', 'response_types_supported': ['code'],
            'grant_types_supported': ['authorization_code'], 'subject_types_supported': ['public'],
            'id_token_signing_alg_values_supported': ['RS256'], 'scopes_supported': sorted(SCOPES),
            'token_endpoint_auth_methods_supported': ['client_secret_basic', 'client_secret_post'],
            'code_challenge_methods_supported': ['S256']}

    @ns['app'].get('/oidc/jwks')
    def jwks():
        issuer()
        return {'keys': [keys()[1]]}

    @ns['app'].post('/api/products/{product_id}/oidc-client')
    def provision(product_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        issuer()
        keys()
        product = db.scalar(select(ns['Product']).where(ns['Product'].id == product_id).with_for_update())
        if not product or product.product_type != 'saas' or product.delivery_method != 'tenant_access':
            raise HTTPException(404, 'SaaS产品不存在')
        if user.verified_status != 'verified' or user.activation_status != 'active':
            raise HTTPException(403, '仅已实名管理员可配置OIDC')
        if user.platform_role not in {'super_admin', 'platform_operator'}:
            if user.platform_role:
                raise HTTPException(403, '当前平台角色不可配置OIDC')
            ns['require_enterprise_admin'](db, user, product.enterprise_id)
        if product.status not in {'published', 'pending_integration'}:
            raise HTTPException(409, '产品需先完成审核')
        client, secret = ensure_client(db, product, user.email or user.phone or user.id)
        redirects = json.loads(product.oidc_redirect_uris_json or '[]')
        db.commit()
        return JSONResponse({'client_id': client.client_id, 'client_secret': secret,
            'issuer': issuer(), 'redirect_uris': redirects, 'status': client.status,
            'secret_available_once': secret is not None}, headers=NO_STORE)

    @ns['app'].get('/api/products/{product_id}/oidc-credentials-download')
    def download_credentials(product_id: str, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        product = db.get(ns['Product'], product_id)
        if not product:
            raise HTTPException(404, '产品不存在')
        if user.platform_role:
            raise HTTPException(403, '接入凭据仅可由产品提供企业管理员下载')
        if user.verified_status != 'verified':
            raise HTTPException(403, '完成个人实名认证后才能下载接入凭据')
        ns['require_enterprise_admin'](db, user, product.enterprise_id)
        if product.product_type != 'saas' or product.delivery_method != 'tenant_access' or product.status not in {'published', 'pending_integration'}:
            raise HTTPException(409, '只有已发布或待集成的SaaS产品可以下载OIDC凭据')
        client = db.scalar(select(OIDCClient).where(OIDCClient.product_id == product.id,
            OIDCClient.status == 'active'))
        if not client:
            raise HTTPException(404, 'OIDC客户端不存在')
        secret = client_secret(client)
        base = issuer()
        redirects = json.loads(product.oidc_redirect_uris_json or '[]')
        ns['audit'](db, user.email or user.phone or user.id, 'download_oidc_credentials',
            'oidc_client', client.id, '下载OIDC凭据；不记录密钥', tenant_id=product.enterprise_id)
        db.commit()
        return JSONResponse({'client_id': client.client_id, 'client_secret': secret, 'issuer': base,
            'discovery_url': base + '/.well-known/openid-configuration', 'token_url': base + '/oidc/token',
            'authorization_url': base + '/oidc/authorize', 'userinfo_url': base + '/oidc/userinfo',
            'jwks_url': base + '/oidc/jwks', 'redirect_uris': redirects,
            'response_type': 'code', 'grant_type': 'authorization_code',
            'code_challenge_method': 'S256', 'scopes': sorted(SCOPES)}, headers={
                **NO_STORE, 'Content-Disposition': 'attachment; filename="oidc-credentials.json"'})

    def valid_redirect(uri, db=None):
        try:
            validate_redirect_uris([uri],
                allow_http=ns['application_address_policy']['allow_http'](db))
            return True
        except ValueError:
            return False

    def issue_authorization(db, body, request, user):
        issuer()
        keys()
        client = client_for(db, body.client_id)
        if not client or not client.check_redirect_uri(body.redirect_uri) or not valid_redirect(body.redirect_uri, db=db):
            raise HTTPException(400, 'OIDC客户端或回调地址无效')
        if 'openid' not in body.scope.split() or not set(body.scope.split()) <= SCOPES:
            raise HTTPException(400, 'OIDC scope无效')
        subscription = eligible(db, user, client, body.enterprise_id)
        claims = ns['jwt'].decode(request.headers['authorization'].split(' ', 1)[1], ns['JWT_SECRET'], algorithms=['HS256'])
        auth_time = claims.get('iat')
        if type(auth_time) is not int or auth_time > int(time.time()) + 60:
            raise HTTPException(401, '请重新登录后进行SaaS单点登录')
        server = Server(db)
        server.context = {'enterprise_id': body.enterprise_id, 'subscription_id': subscription.id, 'auth_time': auth_time}
        protocol = ProtocolRequest('GET', body.model_dump(exclude={'enterprise_id'}))
        try:
            grant = server.get_consent_grant(protocol, end_user=user)
            status, result, headers = server.create_authorization_response(protocol, grant_user=user, grant=grant)
            if status != 302:
                raise HTTPException(400, 'OIDC授权请求无效')
            ns['audit'](db, user.email or user.phone or user.id, 'oidc_authorize', 'saas_subscription',
                subscription.id, '用户SSO授权码签发；不记录授权码、state、nonce', tenant_id=body.enterprise_id)
            return dict(headers)['Location']
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, 'OIDC nonce已使用，请重新发起授权') from exc
        except OAuth2Error:
            db.rollback()
            raise HTTPException(400, 'OIDC授权请求无效')

    @ns['app'].post('/oidc/authorize')
    def authorize(body: AuthorizationBody, request: Request, user=Depends(ns['current_user']),
                  db=Depends(ns['db_session'])):
        location = issue_authorization(db, body, request, user)
        db.commit()
        return JSONResponse({'redirect_url': location}, headers=NO_STORE)

    @ns['app'].post('/oidc/token')
    async def token(request: Request):
        issuer()
        keys()
        if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise HTTPException(415, 'OIDC Token端点仅接受form-urlencoded')
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 16384:
                raise HTTPException(413, 'OIDC请求过大')
            chunks.append(chunk)
        # Parse URL encoding with the standard parser, retaining duplicates.
        from urllib.parse import parse_qsl
        try:
            pairs = parse_qsl(b''.join(chunks).decode(), keep_blank_values=True, max_num_fields=20)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, 'OIDC请求参数无效') from exc
        form = dict(pairs)
        if len(form) != len(pairs) or any(len(value) > 4096 for value in form.values()):
            raise HTTPException(400, 'OIDC请求参数无效或重复')
        return await run_in_threadpool(exchange_transaction, form, request.headers)

    def exchange_transaction(form, request_headers):
        # Keep the transaction in one worker thread, including row-lock waits.
        # Preserve Headers' case-insensitive lookup for Authlib Basic auth.
        with ns['SessionLocal']() as db:
            protocol = ProtocolRequest('POST', form, request_headers)
            server = Server(db)
            try:
                status, result, headers = server.create_token_response(protocol)
                if status == 200:
                    code = protocol.authorization_code
                    ns['audit'](db, protocol.user.email or protocol.user.phone or protocol.user.id,
                        'oidc_exchange_code', 'saas_subscription', code.subscription_id,
                        '授权码兑换用户令牌；不记录令牌', tenant_id=code.enterprise_id)
                db.commit()
                return JSONResponse(result, status_code=status, headers={**dict(headers), **NO_STORE})
            except Exception:
                db.rollback()
                raise

    @ns['app'].get('/oidc/userinfo')
    def userinfo(request: Request, db=Depends(ns['db_session'])):
        issuer()
        header = request.headers.get('authorization', '')
        if not header.lower().startswith('bearer ') or len(header) > 512:
            raise HTTPException(401, 'OIDC用户令牌无效', headers={'WWW-Authenticate': 'Bearer'})
        row = db.scalar(select(OIDCCode).where(OIDCCode.access_hash == digest(header.split(' ', 1)[1]),
            OIDCCode.access_expires_at > int(time.time()), OIDCCode.revoked_at.is_(None)))
        client = client_for(db, row.client_id) if row else None
        if not row or not row.consumed_at or not client:
            raise HTTPException(401, 'OIDC用户令牌已失效', headers={'WWW-Authenticate': 'Bearer'})
        user = db.get(ns['User'], row.user_id)
        subscription = eligible(db, user, client, row.enterprise_id, row.subscription_id)
        return JSONResponse(dict(user_info(user, row.scope, row.enterprise_id, subscription)), headers=NO_STORE)

    ns['OIDCClient'], ns['OIDCCode'] = OIDCClient, OIDCCode
    hooks = {'Client': OIDCClient, 'Code': OIDCCode, 'eligible': eligible,
        'client_for': client_for, 'valid_redirect': valid_redirect,
        'issue_authorization': issue_authorization, 'ensure_client': ensure_client,
        'client_secret': client_secret}
    ns['oidc_provider'] = hooks
    return hooks
