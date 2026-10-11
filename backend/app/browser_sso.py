"""Short-lived, browser-bound consent transactions; no credentials in redirects."""
import json
import secrets
import time
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import Column, ForeignKey, Integer, String, Text, select

from .oidc_provider import AuthorizationBody, NO_STORE, SCOPES, digest, issuer, keys

COOKIE = 'market_oidc_flow'
COOKIE_PATH = '/api/oidc/browser'


def install(ns):
    hooks = ns['oidc_provider']

    class BrowserRequest(ns['Base']):
        __tablename__ = 'oidc_browser_requests'
        id = Column(String(64), primary_key=True)
        client_id = Column(ForeignKey('oidc_clients.client_id'), nullable=False)
        payload = Column(Text, nullable=False)
        browser_hash = Column(String(64), nullable=False)
        expires_at = Column(Integer, nullable=False)
        consumed_at = Column(Integer)
        user_id = Column(ForeignKey('users.id'))

    def validate(db, body):
        client = hooks['client_for'](db, body.client_id)
        if (not client or not client.check_redirect_uri(body.redirect_uri)
                or not hooks['valid_redirect'](body.redirect_uri) or body.response_type != 'code'
                or 'openid' not in body.scope.split() or not set(body.scope.split()) <= SCOPES):
            raise HTTPException(400, 'OIDC客户端、回调或授权参数无效')
        return client

    def load(db, request, request_id, user, lock=False):
        if len(request_id) != 64:
            raise HTTPException(404, '授权请求不存在')
        statement = select(BrowserRequest).where(BrowserRequest.id == request_id)
        if lock:
            statement = statement.with_for_update()
        row = db.scalar(statement)
        cookie = request.cookies.get(COOKIE, '')
        if not row or not cookie or not secrets.compare_digest(row.browser_hash, digest(cookie)):
            raise HTTPException(403, '请从应用入口重新发起登录')
        if row.consumed_at or row.expires_at <= int(time.time()):
            raise HTTPException(410, '授权请求已处理或过期，请重新登录')
        body = AuthorizationBody.model_validate_json(row.payload)
        client = validate(db, body)
        subscription = hooks['eligible'](db, user, client, body.enterprise_id)
        return row, body, client, subscription

    def origin_check(request):
        base = urlsplit(issuer())
        if request.headers.get('origin') != urlunsplit((base.scheme, base.netloc, '', '', '')):
            raise HTTPException(403, '授权确认来源无效')

    def finish(db, row, user, location):
        row.consumed_at = int(time.time())
        row.user_id = user.id
        db.commit()
        response = JSONResponse({'redirect_url': location}, headers=NO_STORE)
        response.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, samesite='lax')
        return response

    @ns['app'].get('/oidc/authorize')
    def begin(request: Request, db=Depends(ns['db_session'])):
        issuer()
        keys()
        pairs = list(request.query_params.multi_items())
        if len(request.url.query) > 8192 or len(pairs) > 12 or len(dict(pairs)) != len(pairs):
            raise HTTPException(400, '授权参数过大或重复')
        try:
            body = AuthorizationBody.model_validate(dict(pairs))
        except ValidationError as exc:
            raise HTTPException(400, '授权参数无效') from exc
        validate(db, body)
        cookie = secrets.token_urlsafe(32)
        row = BrowserRequest(id=secrets.token_hex(32), client_id=body.client_id,
            payload=body.model_dump_json(), browser_hash=digest(cookie), expires_at=int(time.time()) + 300)
        db.add(row)
        db.commit()
        response = RedirectResponse('/console/sso?request=' + row.id, status_code=303, headers=NO_STORE)
        response.set_cookie(COOKIE, cookie, max_age=300, path=COOKIE_PATH,
            secure=urlsplit(issuer()).scheme == 'https', httponly=True, samesite='lax')
        return response

    @ns['app'].get('/api/oidc/browser/{request_id}')
    def details(request_id: str, request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        row, body, client, subscription = load(db, request, request_id, user)
        return JSONResponse({'request_id': row.id, 'product_name': db.get(ns['Product'], client.product_id).name,
            'enterprise_name': db.get(ns['Enterprise'], body.enterprise_id).name,
            'enterprise_id': body.enterprise_id, 'scopes': body.scope.split(),
            'expires_at': row.expires_at}, headers=NO_STORE)

    @ns['app'].post('/api/oidc/browser/{request_id}/approve')
    def approve(request_id: str, request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        origin_check(request)
        row, body, client, subscription = load(db, request, request_id, user, lock=True)
        location = hooks['issue_authorization'](db, body, request, user)
        return finish(db, row, user, location)

    @ns['app'].post('/api/oidc/browser/{request_id}/reject')
    def reject(request_id: str, request: Request, user=Depends(ns['current_user']), db=Depends(ns['db_session'])):
        origin_check(request)
        row, body, client, subscription = load(db, request, request_id, user, lock=True)
        callback = urlsplit(body.redirect_uri)
        query = parse_qsl(callback.query, keep_blank_values=True)
        query += [('error', 'access_denied'), ('state', body.state)]
        location = urlunsplit(callback._replace(query=urlencode(query)))
        ns['audit'](db, user.email or user.id, 'oidc_reject', 'saas_subscription',
            subscription.id, '用户拒绝SSO授权', tenant_id=body.enterprise_id)
        return finish(db, row, user, location)

    ns['OIDCBrowserRequest'] = BrowserRequest
