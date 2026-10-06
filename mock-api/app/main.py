from __future__ import annotations

import asyncio
import json
import os
import httpx
from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import StreamingResponse

app = FastAPI(title="Market Mock API Provider", version="0.1.0")


def require_platform_token(authorization: str):
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing platform bearer token")
    try:
        response = httpx.post(os.getenv("PLATFORM_OAUTH_INTROSPECTION_URL", "http://market-api:8000/oauth/introspect"), data={"token": authorization[7:].strip()}, timeout=5)
        response.raise_for_status()
        if not response.json().get("active"):
            raise HTTPException(401, "invalid platform token")
    except httpx.HTTPError as exc:
        raise HTTPException(503, "platform token validation unavailable") from exc


@app.get("/health")
def health():
    return {"status": "ok", "service": "market-mock-api"}


@app.get("/v1/echo")
def echo(authorization: str = Header(default="")):
    require_platform_token(authorization)
    return {"code": 0, "message": "success", "data": {"provider": "mock-api", "enterprise_id": "injected-by-gateway"}, "request_id": "mock-request"}


@app.get("/v1/file")
def file_response(authorization: str = Header(default="")):
    require_platform_token(authorization)
    return Response(content="mock api file content\n", media_type="text/plain", headers={"Content-Disposition": 'attachment; filename="mock-api.txt"'})


async def stream_events():
    for index in range(3):
        yield f"data: {json.dumps({'index': index, 'message': 'streaming'}, ensure_ascii=False)}\n\n".encode()
        await asyncio.sleep(0.2)


@app.get("/v1/stream")
def stream(authorization: str = Header(default="")):
    require_platform_token(authorization)
    return StreamingResponse(stream_events(), media_type="text/event-stream")


@app.get("/v1/slow")
async def slow(authorization: str = Header(default="")):
    require_platform_token(authorization)
    await asyncio.sleep(35)
    return {"code": 0, "message": "slow response"}


@app.get("/v1/error")
def error(authorization: str = Header(default="")):
    require_platform_token(authorization)
    return Response(content='{"code":"MOCK_PROVIDER_ERROR","message":"simulated provider failure"}', status_code=500, media_type="application/json")
