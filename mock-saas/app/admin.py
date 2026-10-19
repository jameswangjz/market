"""Bounded administration of the private RP client registry."""
import json
from pathlib import Path

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import ValidationError


HEADERS = {'Cache-Control': 'no-store', 'Pragma': 'no-cache',
           'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
           'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}


def install_admin(app, store, *, enabled, admin, settings, metadata, body_type):
    def visible(config):
        return {**{k: config[k] for k in ('client_id', 'product_id', 'product_name', 'issuer', 'redirect_uri')},
                'secret_configured': bool(config.get('client_secret'))}

    def gate():
        if not enabled:
            raise HTTPException(404, 'Not found')

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        if request.url.path == '/admin' or request.url.path.startswith('/admin/'):
            response.headers.update(HEADERS)
        return response

    @app.get('/admin', response_class=HTMLResponse)
    def page():
        gate()
        return HTMLResponse(Path(__file__).with_name('admin.html').read_text(encoding='utf-8'))

    @app.get('/admin/assets/{name}')
    def asset(name: str):
        gate()
        if name not in ('admin.js', 'admin.css'):
            raise HTTPException(404, 'Not found')
        return Response(Path(__file__).with_name(name).read_text(encoding='utf-8'),
                        media_type='text/javascript' if name.endswith('.js') else 'text/css')

    @app.get('/admin/api/clients')
    def clients(request: Request):
        admin(request)
        base, issuer, _ = settings()
        with store.connect() as db:
            rows = db.execute('SELECT config FROM clients ORDER BY client_id').fetchall()
        return JSONResponse({'clients': [visible(json.loads(row['config'])) for row in rows],
                             'issuer': issuer, 'redirect_uri': base + '/sso/callback'})

    @app.put('/admin/api/clients')
    async def save(request: Request):
        admin(request)
        try:
            raw = await request.json()
            body = body_type.model_validate(raw)
        except (ValueError, ValidationError):
            raise HTTPException(422, 'Invalid client configuration') from None
        base, issuer, _ = settings()
        if body.issuer != issuer or body.redirect_uri != base + '/sso/callback':
            raise HTTPException(400, 'Client issuer or redirect URI mismatch')
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT config FROM clients WHERE client_id=?', (body.client_id,)).fetchone()
            previous = json.loads(row['config']) if row else None
            if previous and previous['product_id'] != body.product_id:
                raise HTTPException(409, 'Client product cannot be changed')
            config = body.model_dump()
            config['client_secret'] = body.client_secret or (previous or {}).get('client_secret', '')
            if not config['client_secret']:
                raise HTTPException(400, 'Client secret required for creation')
            if previous and any(previous[k] != config[k] for k in
                                    ('client_secret', 'issuer', 'redirect_uri')):
                for table in ('flows', 'sessions', 'mappings'):
                    db.execute('DELETE FROM ' + table + ' WHERE client_id=?', (body.client_id,))
            db.execute('INSERT INTO clients VALUES (?, ?) ON CONFLICT(client_id) DO UPDATE SET config=excluded.config',
                       (body.client_id, json.dumps(config)))
        return JSONResponse(visible(config), status_code=200 if previous else 201)

    @app.post('/admin/api/check')
    def check(request: Request):
        admin(request)
        try:
            metadata()
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise HTTPException(503, 'OIDC connectivity or metadata check failed') from None
        return JSONResponse({'status': 'ok', 'scope': 'connectivity_and_configuration_only'})
