from __future__ import annotations

import asyncio
import json
from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse

app = FastAPI(title="Market Mock API Provider", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "market-mock-api"}


@app.get("/v1/echo")
def echo():
    return {"code": 0, "message": "success", "data": {"provider": "mock-api", "enterprise_id": "injected-by-gateway"}, "request_id": "mock-request"}


@app.get("/v1/file")
def file_response():
    return Response(content="mock api file content\n", media_type="text/plain", headers={"Content-Disposition": 'attachment; filename="mock-api.txt"'})


async def stream_events():
    for index in range(3):
        yield f"data: {json.dumps({'index': index, 'message': 'streaming'}, ensure_ascii=False)}\n\n".encode()
        await asyncio.sleep(0.2)


@app.get("/v1/stream")
def stream():
    return StreamingResponse(stream_events(), media_type="text/event-stream")


@app.get("/v1/slow")
async def slow():
    await asyncio.sleep(35)
    return {"code": 0, "message": "slow response"}


@app.get("/v1/error")
def error():
    return Response(content='{"code":"MOCK_PROVIDER_ERROR","message":"simulated provider failure"}', status_code=500, media_type="application/json")
