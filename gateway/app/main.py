from __future__ import annotations

import hashlib
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


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payment_status: Mapped[str] = mapped_column(String(30))
    main_status: Mapped[str] = mapped_column(String(30))


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
    status: Mapped[str] = mapped_column(String(30), index=True)


class ApiCredential(Base):
    __tablename__ = "api_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("api_gateway_routes.id"), index=True)
    enterprise_id: Mapped[str] = mapped_column(String(36), index=True)
    order_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
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


def check_quota(credential: ApiCredential, route: ApiGatewayRoute):
    minute_limit = credential.rate_limit_per_minute or route.rate_limit_per_minute
    daily_limit = credential.daily_quota or route.daily_quota
    monthly_limit = credential.monthly_quota or route.monthly_quota
    now_epoch = int(time.time())
    minute_key = f"market:gateway:minute:{credential.id}:{now_epoch // 60}"
    day_key = f"market:gateway:day:{credential.id}:{datetime.now(timezone.utc).date().isoformat()}"
    month_key = f"market:gateway:month:{credential.id}:{datetime.now(timezone.utc).strftime('%Y-%m')}"
    try:
        pipe = redis_client.pipeline()
        pipe.incr(minute_key)
        pipe.expire(minute_key, 70)
        pipe.incr(day_key)
        pipe.expire(day_key, 86400)
        pipe.incr(month_key)
        pipe.expire(month_key, 2678400)
        result = pipe.execute()
        minute_count, daily_count, monthly_count = int(result[0]), int(result[2]), int(result[4])
    except redis.RedisError:
        raise HTTPException(503, "API 网关限流服务暂不可用")
    if minute_count > minute_limit:
        raise HTTPException(429, "超过 API 每分钟调用频率限制")
    if daily_count > daily_limit:
        raise HTTPException(429, "超过 API 每日调用配额")
    if monthly_limit and monthly_count > monthly_limit:
        raise HTTPException(429, "超过 API 每月调用配额")


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
        if credential and credential.order_id:
            order = db.get(Order, credential.order_id)
            if not order or order.payment_status != "paid" or order.main_status in {"cancelled", "closed"}:
                db.close()
                raise HTTPException(403, "订单授权已失效")
        check_quota(credential, route)
    else:
        credential = db.scalar(select(ApiCredential).where(ApiCredential.route_id == route.id, ApiCredential.status == "active").limit(1))
    target = route.upstream_url.rstrip("/")
    if path:
        target += "/" + path
    headers = {key: value for key, value in request.headers.items() if key.lower() not in {"host", "content-length", "authorization", "x-api-key"}}
    if credential:
        headers["x-market-enterprise-id"] = credential.enterprise_id
        headers["x-market-route-key"] = route.route_key
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
