"""Mock provider lifecycle protocol.

POST /management accepts schema_version=1, type, product_id, enterprise_id,
combination_id, generation and nullable tenant_id. All operations require the
backend's full frozen payload, root event_id/idempotency_key, version {id, code},
starts_at/expires_at and periods with order/term identities, starts_at/expires_at
and nested quotas. Success is the stored receipt at ROOT with integer ret=0.
STATUS accepts target_type alongside the original frozen mutation payload.
Management requires resource.manage; business /v1 routes require resource.invoke.
STATUS returns the latest mutation receipt, including its event_id/type; retries
return the original mutation receipt even after subsequent changes. effective_at
is the first committed provider receipt time, never a caller supplied timestamp.
"""
from __future__ import annotations

import asyncio
import json
import os
import hashlib
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
import httpx
from fastapi import FastAPI, Header, HTTPException, Response, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="Market Mock API Provider", version="0.1.0")


def require_platform_token(authorization: str, headers=None, required_scope='resource.invoke'):
    if not authorization.lower().startswith("bearer ") or not authorization[7:].strip():
        raise HTTPException(401, "missing platform bearer token")
    try:
        response = httpx.post(os.getenv("PLATFORM_OAUTH_INTROSPECTION_URL", "http://market-api:8000/oauth/introspect"), data={"token": authorization[7:].strip()}, timeout=5)
        response.raise_for_status()
        claims = response.json()
        if not isinstance(claims, dict) or claims.get("active") is not True:
            raise HTTPException(401, "invalid platform token")
    except httpx.HTTPError:
        raise HTTPException(503, "platform token validation unavailable") from None
    except ValueError:
        raise HTTPException(503, "platform token validation unavailable") from None
    if not isinstance(claims.get('scope'), str) or required_scope not in claims['scope'].split():
        raise HTTPException(403, 'insufficient scope')
    if not isinstance(claims.get('product_id'), str) or not claims['product_id'].strip():
        raise HTTPException(403, 'missing product identity')
    expected_product = os.getenv('MOCK_API_PRODUCT_ID')
    if expected_product and claims['product_id'] != expected_product:
        raise HTTPException(403, 'product identity mismatch')
    for field in ('product_id', 'enterprise_id', 'combination_id', 'tenant_id'):
        supplied = (headers or {}).get('x-' + field.replace('_', '-'))
        if supplied is not None and field in claims and supplied != claims[field]:
            raise HTTPException(403, 'identity header mismatch')
    return claims


IDENTITY = ('product_id', 'enterprise_id', 'combination_id', 'tenant_id')
OPERATIONS = {'OPEN', 'RENEW', 'CHANGE', 'CLOSE', 'RESTORE', 'ADJUST', 'STATUS'}
REQUEST_FIELDS = set(IDENTITY) | {
    'schema_version', 'type', 'event_id', 'idempotency_key', 'generation',
    'order_id', 'order_no', 'version', 'subscription_months', 'starts_at',
    'expires_at', 'periods', 'change_id', 'refund_id', 'direction',
}
REQUIRED_FIELDS = REQUEST_FIELDS - {'change_id', 'refund_id', 'direction'}
PERIOD_FIELDS = {'order_id', 'order_no', 'term_id', 'starts_at', 'expires_at', 'quotas'}
QUOTA_FIELDS = {'rate_limit_per_minute', 'daily_quota', 'monthly_quota', 'quota_amount'}


def identity(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200


def utc_date(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value):
        raise HTTPException(400, 'invalid UTC interval')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        raise HTTPException(400, 'invalid UTC interval') from None


def lifecycle(body, claims, headers=None):
    if not isinstance(body, dict):
        raise HTTPException(400, 'JSON object required')
    operation = body.get('type')
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise HTTPException(400, 'invalid operation')
    allowed = REQUEST_FIELDS | ({'target_type'} if operation == 'STATUS' else set())
    if set(body) - allowed or not REQUIRED_FIELDS <= body.keys():
        raise HTTPException(400, 'invalid lifecycle fields')
    if 'target_type' in body and (
            not isinstance(body['target_type'], str) or body['target_type'] not in OPERATIONS - {'STATUS'}):
        raise HTTPException(400, 'invalid target_type')
    if type(body.get('schema_version')) is not int or body['schema_version'] != 1:
        raise HTTPException(400, 'unsupported schema_version')
    if type(body.get('generation')) is not int or body['generation'] < 1:
        raise HTTPException(400, 'positive generation required')
    for field in ('order_id', 'order_no', 'change_id', 'refund_id'):
        if field in body and not identity(body[field]):
            raise HTTPException(400, 'invalid operation identity')
    if 'direction' in body and body['direction'] not in ('upgrade', 'downgrade', 'restore'):
        raise HTTPException(400, 'invalid change direction')
    if (
            type(body['subscription_months']) is not int or body['subscription_months'] < 1):
        raise HTTPException(400, 'positive subscription_months required')
    if any(not identity(body.get(field)) for field in IDENTITY[:3]) or (
            body['tenant_id'] is not None and not identity(body['tenant_id'])):
        raise HTTPException(400, 'complete lifecycle identity required')
    for field in IDENTITY:
        if field in claims and body[field] != claims[field]:
            raise HTTPException(403, 'token identity mismatch')
        supplied = (headers or {}).get('x-' + field.replace('_', '-'))
        if supplied is not None and supplied != body[field]:
            raise HTTPException(403, 'identity header mismatch')
    if claims.get('product_id') != body['product_id']:
        raise HTTPException(403, 'product identity mismatch')
    identifiers = [body[field] for field in ('event_id', 'idempotency_key') if field in body]
    if any(not identity(value) for value in identifiers):
        raise HTTPException(400, 'invalid idempotency identifier')
    if len(body['idempotency_key']) > 128:
        raise HTTPException(400, 'invalid idempotency key')
    keys = list(dict.fromkeys(identifiers))
    start, end = utc_date(body['starts_at']), utc_date(body['expires_at'])
    if start >= end:
        raise HTTPException(400, 'invalid UTC interval')
    if not isinstance(body['version'], dict) or set(body['version']) != {'id', 'code'} or not all(
            identity(value) for value in body['version'].values()):
        raise HTTPException(400, 'frozen version id and code required')
    periods = validate_periods(body['periods'], start, end, operation)
    body = dict(body, starts_at=start.isoformat(), expires_at=end.isoformat(), periods=periods)
    try:
        canonical = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        raise HTTPException(400, 'invalid JSON values') from None
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    path = Path(os.getenv('MOCK_API_DB_PATH', '/data/mock-api.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        db.execute('PRAGMA busy_timeout=30000')
        db.execute('CREATE TABLE IF NOT EXISTS lifecycle_state (product TEXT, enterprise TEXT, combination TEXT, tenant TEXT, state TEXT NOT NULL, PRIMARY KEY(product, enterprise, combination), UNIQUE(product, enterprise, tenant))')
        db.execute('CREATE TABLE IF NOT EXISTS lifecycle_receipts (product TEXT, enterprise TEXT, event TEXT, bodyhash TEXT NOT NULL, canonical_body TEXT NOT NULL, receipt TEXT NOT NULL, PRIMARY KEY(product, enterprise, event))')
        db.execute('BEGIN IMMEDIATE')
        product, enterprise, combination = (body[field] for field in IDENTITY[:3])
        tenant = body.get('tenant_id')
        if operation != 'STATUS':
            for identifier in keys:
                previous = db.execute('SELECT bodyhash, receipt FROM lifecycle_receipts WHERE product=? AND enterprise=? AND event=?', (product, enterprise, identifier)).fetchone()
                if previous:
                    if previous[0] != digest:
                        raise HTTPException(409, 'idempotency body mismatch')
                    return json.loads(previous[1])
        row = db.execute('SELECT tenant, state FROM lifecycle_state WHERE product=? AND enterprise=? AND combination=?', (product, enterprise, combination)).fetchone()
        if row and tenant is not None and row[0] != tenant:
            raise HTTPException(409, 'tenant identity mismatch')
        state = json.loads(row[1]) if row else None
        if state and state['generation'] != body['generation'] and not (
                operation == 'RESTORE' and body['generation'] > state['generation']):
            raise HTTPException(409, 'generation identity mismatch')
        if operation == 'STATUS':
            if state is None:
                raise HTTPException(404, 'lifecycle identity not found')
            return state
        if operation == 'OPEN':
            if state is not None:
                raise HTTPException(409, 'lifecycle identity already exists')
        elif state is None:
            raise HTTPException(404, 'lifecycle identity not found')
        elif state['status'] == 'closed' and operation != 'RESTORE':
            raise HTTPException(409, 'lifecycle identity is closed')
        tenant = row[0] if row else tenant or str(uuid.uuid5(uuid.NAMESPACE_URL,
            json.dumps([product, enterprise, combination], separators=(',', ':'))))
        receipt = dict(body, ret=0, tenant_id=tenant,
                       status='closed' if operation == 'CLOSE' else 'active',
                       effective_at=datetime.now(timezone.utc).isoformat())
        encoded = json.dumps(receipt, sort_keys=True, separators=(',', ':'))
        db.execute('INSERT INTO lifecycle_state VALUES (?, ?, ?, ?, ?) ON CONFLICT(product, enterprise, combination) DO UPDATE SET state=excluded.state',
                   (product, enterprise, combination, tenant, encoded))
        for identifier in keys:
            db.execute('INSERT INTO lifecycle_receipts VALUES (?, ?, ?, ?, ?, ?)',
                       (product, enterprise, identifier, digest, canonical, encoded))
        db.execute('COMMIT')
        return receipt
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'lifecycle identity conflict') from None
    finally:
        if db.in_transaction:
            db.execute('ROLLBACK')
        db.close()


def validate_periods(periods, start, end, operation):
    if not isinstance(periods, list) or len(periods) > 10000 or (
            not periods and operation not in {'CLOSE', 'ADJUST', 'STATUS'}):
        raise HTTPException(400, 'authoritative periods required')
    seen = set()
    normalized = []
    for period in periods:
        if not isinstance(period, dict) or set(period) != PERIOD_FIELDS or any(not identity(period.get(field)) for field in ('order_id', 'order_no', 'term_id')):
            raise HTTPException(400, 'invalid period identity')
        left, right = utc_date(period['starts_at']), utc_date(period['expires_at'])
        period_identity = (period['order_id'], period['term_id'], left, right)
        if period_identity in seen:
            raise HTTPException(400, 'duplicate period identity')
        seen.add(period_identity)
        if not start <= left < right <= end:
            raise HTTPException(400, 'period outside target interval')
        quotas = period['quotas']
        if not isinstance(quotas, dict) or set(quotas) != QUOTA_FIELDS or any(
                type(value) is not int or value < 0 for value in quotas.values()):
            raise HTTPException(400, 'absolute nonnegative period quotas required')
        normalized.append(dict(period, starts_at=left.isoformat(), expires_at=right.isoformat()))
    return normalized


@app.post('/management')
async def management(request: Request, authorization: str = Header(default='')):
    claims = require_platform_token(authorization, request.headers, required_scope='resource.manage')
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'invalid JSON') from None
    return lifecycle(body, claims, request.headers)


@app.get("/health")
def health():
    return {"status": "ok", "service": "market-mock-api"}


@app.get("/v1/echo")
def echo(request: Request, authorization: str = Header(default="")):
    require_platform_token(authorization, request.headers)
    return {"code": 0, "message": "success", "data": {"provider": "mock-api", "enterprise_id": "injected-by-gateway"}, "request_id": "mock-request"}


@app.get("/v1/file")
def file_response(request: Request, authorization: str = Header(default="")):
    require_platform_token(authorization, request.headers)
    return Response(content="mock api file content\n", media_type="text/plain", headers={"Content-Disposition": 'attachment; filename="mock-api.txt"'})


async def stream_events():
    for index in range(3):
        yield f"data: {json.dumps({'index': index, 'message': 'streaming'}, ensure_ascii=False)}\n\n".encode()
        await asyncio.sleep(0.2)


@app.get("/v1/stream")
def stream(request: Request, authorization: str = Header(default="")):
    require_platform_token(authorization, request.headers)
    return StreamingResponse(stream_events(), media_type="text/event-stream")


@app.get("/v1/slow")
async def slow(request: Request, authorization: str = Header(default="")):
    require_platform_token(authorization, request.headers)
    await asyncio.sleep(35)
    return {"code": 0, "message": "slow response"}


@app.get("/v1/error")
def error(request: Request, authorization: str = Header(default="")):
    require_platform_token(authorization, request.headers)
    return Response(content='{"code":"MOCK_PROVIDER_ERROR","message":"simulated provider failure"}', status_code=500, media_type="application/json")
