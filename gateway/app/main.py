from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


DATABASE_URL = ""


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(40))


class ProductReleaseVersion(Base):
    __tablename__ = "product_release_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    daily_quota: Mapped[int] = mapped_column(Integer, default=10000)
    monthly_quota: Mapped[int] = mapped_column(Integer, default=0)
    quota_amount: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), index=True)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payment_status: Mapped[str] = mapped_column(String(30))
    main_status: Mapped[str] = mapped_column(String(30))
    refunded_amount: Mapped[float] = mapped_column(Integer, default=0)


class ApiGatewayRoute(Base):
    __tablename__ = "api_gateway_routes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    route_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    upstream_url: Mapped[str] = mapped_column(String(500))
    version: Mapped[str] = mapped_column(String(40))
    auth_mode: Mapped[str] = mapped_column(String(30))
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer)
    daily_quota: Mapped[int] = mapped_column(Integer)
    monthly_quota: Mapped[int] = mapped_column(Integer, default=0)
    timeout_ms: Mapped[int] = mapped_column(Integer)
    strip_prefix: Mapped[bool] = mapped_column(Boolean)
    upstream_auth_mode: Mapped[str] = mapped_column(String(30), default="oauth2")
    upstream_scope: Mapped[str] = mapped_column(String(500), default="resource.invoke")
    upstream_client_id: Mapped[str] = mapped_column(String(180), default="")
    upstream_client_secret: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(30), index=True)


class ApiCredential(Base):
    __tablename__ = "api_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("api_gateway_routes.id"), index=True)
    enterprise_id: Mapped[str] = mapped_column(String(36), index=True)
    order_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    product_version_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_quota: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiUsage(Base):
    __tablename__ = "api_usage"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("api_gateway_routes.id"), index=True)
    credential_id: Mapped[str] = mapped_column(ForeignKey("api_credentials.id"), index=True)
    enterprise_id: Mapped[str] = mapped_column(String(36), index=True)
    method: Mapped[str] = mapped_column(String(12))
    path: Mapped[str] = mapped_column(String(500), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    request_bytes: Mapped[int] = mapped_column(Integer, default=0)
    response_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


engine = create_engine(__import__("os").environ.get("DATABASE_URL", "sqlite:///./market.db"), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
redis_client = redis.Redis.from_url(__import__("os").environ.get("REDIS_URL", "redis://market-redis:6379/1"), decode_responses=True)
app = FastAPI(title="Market Unified API Gateway", version="0.1.0")
_upstream_tokens: dict[str, tuple[str, float]] = {}

QUOTA_SCRIPT = """
local minute = redis.call('INCR', KEYS[1])
local day = redis.call('INCR', KEYS[2])
local month = redis.call('INCR', KEYS[3])
local total = redis.call('INCR', KEYS[4])
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[2])
redis.call('EXPIRE', KEYS[3], ARGV[3])
redis.call('EXPIRE', KEYS[4], ARGV[4])
local minute_limit = tonumber(ARGV[5])
local day_limit = tonumber(ARGV[6])
local month_limit = tonumber(ARGV[7])
local total_limit = tonumber(ARGV[8])
if minute > minute_limit or day > day_limit or (month_limit > 0 and month > month_limit) or (total_limit > 0 and total > total_limit) then
  redis.call('DECR', KEYS[1])
  redis.call('DECR', KEYS[2])
  redis.call('DECR', KEYS[3])
  redis.call('DECR', KEYS[4])
  return {0, minute, day, month, total}
end
return {1, minute, day, month, total}
"""


def new_id() -> str:
    import secrets
    return secrets.token_hex(16)


def extract_key(request: Request) -> str:
    value = request.headers.get("x-api-key", "").strip()
    if value:
        return value
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def upstream_access_token(route: ApiGatewayRoute) -> str:
    if route.upstream_auth_mode != "oauth2":
        return ""
    cache_key = f"market:gateway:oauth:{route.product_id}"
    try:
        cached_raw = redis_client.get(cache_key)
        cached = json.loads(cached_raw) if cached_raw else None
        if cached and cached.get("expires_at", 0) > time.time() + 30:
            return cached["token"]
    except (redis.RedisError, TypeError, ValueError, KeyError):
        raise HTTPException(503, "API 网关 OAuth Token 缓存服务不可用")
    if not route.upstream_client_id or not route.upstream_client_secret:
        raise HTTPException(502, "API 提供方 OAuth 凭据未配置")
    token_url = os.environ.get("PLATFORM_OAUTH_TOKEN_URL", "http://market-api:8000/oauth/token")
    try:
        response = httpx.post(token_url, data={"grant_type": "client_credentials", "client_id": route.upstream_client_id, "client_secret": route.upstream_client_secret, "scope": route.upstream_scope}, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"获取平台 OAuth Token 失败：{exc}") from exc
    token = payload.get("access_token")
    if not token:
        raise HTTPException(502, "平台 OAuth 响应缺少 access_token")
    expires_at = time.time() + max(60, int(payload.get("expires_in", 3600)))
    try:
        redis_client.setex(cache_key, max(60, int(expires_at - time.time())), json.dumps({"token": token, "expires_at": expires_at}))
    except redis.RedisError as exc:
        raise HTTPException(503, "API 网关 OAuth Token 缓存服务不可用") from exc
    return token


def check_quota(credential: ApiCredential, route: ApiGatewayRoute, version: ProductReleaseVersion | None = None):
    minute_limit = credential.rate_limit_per_minute or (version.rate_limit_per_minute if version else route.rate_limit_per_minute)
    daily_limit = credential.daily_quota or (version.daily_quota if version else route.daily_quota)
    monthly_limit = credential.monthly_quota if credential.monthly_quota is not None else (version.monthly_quota if version else route.monthly_quota)
    now_epoch = int(time.time())
    minute_key = f"market:gateway:minute:{credential.id}:{now_epoch // 60}"
    day_key = f"market:gateway:day:{credential.id}:{datetime.now(timezone.utc).date().isoformat()}"
    month_key = f"market:gateway:month:{credential.id}:{datetime.now(timezone.utc).strftime('%Y-%m')}"
    total_key = f"market:gateway:total:{credential.id}"
    try:
        result = redis_client.eval(
            QUOTA_SCRIPT,
            4,
            minute_key,
            day_key,
            month_key,
            total_key,
            70,
            86400,
            2678400,
            31536000,
            minute_limit,
            daily_limit,
            monthly_limit or 0,
            credential.total_quota or 0,
        )
        allowed, minute_count, daily_count, monthly_count, total_count = (int(value) for value in result)
    except redis.RedisError:
        raise HTTPException(503, "API 网关限流服务暂不可用")
    if not allowed:
        if minute_count > minute_limit:
            raise HTTPException(429, "超过 API 每分钟调用频率限制")
        if daily_count > daily_limit:
            raise HTTPException(429, "超过 API 每日调用配额")
        if monthly_limit and monthly_count > monthly_limit:
            raise HTTPException(429, "超过 API 每月调用配额")
        if credential.total_quota and total_count >= credential.total_quota:
            credential.status = "exhausted"
            db = SessionLocal()
            try:
                stored = db.get(ApiCredential, credential.id)
                if stored:
                    stored.status = "exhausted"
                    db.commit()
            finally:
                db.close()
            raise HTTPException(403, "订单 API 调用额度已耗尽，访问凭据已回收")


def record_usage(db: Session, route: ApiGatewayRoute, credential: ApiCredential, request: Request, status_code: int, latency_ms: int, request_bytes: int, response_bytes: int):
    credential.last_used_at = datetime.now(timezone.utc)
    db.add(ApiUsage(id=new_id(), route_id=route.id, credential_id=credential.id, enterprise_id=credential.enterprise_id, method=request.method, path=request.url.path[:500], status_code=status_code, latency_ms=latency_ms, request_bytes=request_bytes, response_bytes=response_bytes))
    db.commit()


@app.get("/health")
def health():
    return {"status": "ok", "service": "market-api-gateway"}


@app.api_route("/gateway/{route_key}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy(route_key: str, path: str, request: Request):
    started = time.perf_counter()
    db = SessionLocal()
    body = await request.body()
    route = db.scalar(select(ApiGatewayRoute).join(Product, Product.id == ApiGatewayRoute.product_id).where(ApiGatewayRoute.route_key == route_key, ApiGatewayRoute.status == "active", Product.status == "published"))
    if not route:
        db.close()
        raise HTTPException(404, "API 路由不存在或未发布")
    credential = None
    if route.auth_mode != "none":
        raw_key = extract_key(request)
        if not raw_key:
            db.close()
            raise HTTPException(401, "缺少 API Key")
        credential = db.scalar(select(ApiCredential).where(ApiCredential.route_id == route.id, ApiCredential.key_hash == hashlib.sha256(raw_key.encode()).hexdigest(), ApiCredential.status == "active"))
        if not credential or (credential.expires_at and credential.expires_at < datetime.now(timezone.utc)):
            db.close()
            raise HTTPException(401, "API Key 无效或已过期")
        if credential.order_id:
            order = db.get(Order, credential.order_id)
            valid = order and order.payment_status == "paid" and float(order.refunded_amount or 0) == 0 and order.main_status not in {"cancelled", "closed"}
        else:
            valid = db.scalar(select(Order.id).where(
                Order.buyer_enterprise_id == credential.enterprise_id,
                Order.product_id == route.product_id,
                Order.payment_status == "paid",
                Order.refunded_amount == 0,
                Order.main_status.not_in(("cancelled", "closed")),
            ).limit(1)) is not None
        if not valid:
            db.close()
            raise HTTPException(403, "企业 API 授权已失效")
        version = db.get(ProductReleaseVersion, credential.product_version_id) if credential.product_version_id else None
        check_quota(credential, route, version)
    else:
        credential = db.scalar(select(ApiCredential).where(ApiCredential.route_id == route.id, ApiCredential.status == "active").limit(1))
    target = route.upstream_url.rstrip("/")
    if path:
        target += "/" + path
    headers = {key: value for key, value in request.headers.items() if key.lower() not in {"host", "content-length", "authorization", "x-api-key"}}
    if credential:
        headers["x-market-enterprise-id"] = credential.enterprise_id
        headers["x-market-route-key"] = route.route_key
    upstream_token = upstream_access_token(route)
    if upstream_token:
        headers["authorization"] = f"Bearer {upstream_token}"
    stream_requested = request.headers.get("x-market-stream", "").lower() == "true" or "text/event-stream" in request.headers.get("accept", "")
    if stream_requested:
        client = httpx.AsyncClient(timeout=route.timeout_ms / 1000)
        upstream_request = client.build_request(request.method, target, params=request.query_params, content=body, headers=headers)
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.TimeoutException:
            await client.aclose()
            db.close()
            raise HTTPException(504, "API 后端服务响应超时")
        except httpx.HTTPError:
            await client.aclose()
            db.close()
            raise HTTPException(502, "API 后端服务不可用")
        response_headers = {key: value for key, value in upstream.headers.items() if key.lower() not in {"content-length", "transfer-encoding", "connection", "keep-alive"}}
        started_stream = time.perf_counter()

        async def stream_body():
            response_bytes = 0
            try:
                async for chunk in upstream.aiter_bytes():
                    response_bytes += len(chunk)
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()
                if credential:
                    record_usage(db, route, credential, request, upstream.status_code, int((time.perf_counter() - started_stream) * 1000), len(body), response_bytes)
                db.close()

        return StreamingResponse(stream_body(), status_code=upstream.status_code, headers=response_headers, media_type=upstream.headers.get("content-type"))
    status_code = 502
    response_bytes = 0
    try:
        async with httpx.AsyncClient(timeout=route.timeout_ms / 1000) as client:
            upstream = await client.request(request.method, target, params=request.query_params, content=body, headers=headers)
        status_code = upstream.status_code
        response_bytes = len(upstream.content)
        response_headers = {key: value for key, value in upstream.headers.items() if key.lower() not in {"content-length", "transfer-encoding", "connection", "keep-alive"}}
        return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers, media_type=upstream.headers.get("content-type"))
    except httpx.TimeoutException:
        status_code = 504
        raise HTTPException(504, "API 后端服务响应超时")
    except httpx.HTTPError:
        status_code = 502
        raise HTTPException(502, "API 后端服务不可用")
    finally:
        if credential:
            record_usage(db, route, credential, request, status_code, int((time.perf_counter() - started) * 1000), len(body), response_bytes)
        db.close()
