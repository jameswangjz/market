import os
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from .oidc_rp import install
from .store import MachineOperationError, Store


def create_app(*, transport=None):
    app = FastAPI(title="Market Mock SaaS")
    store = Store(os.getenv('MOCK_SAAS_DB_PATH', '/data/mock-saas.sqlite3'))
    app.state.store = store
    install(app, store, transport=transport)

    @app.middleware("http")
    async def identify_mock_app(request, call_next):
        response = await call_next(request)
        if request.url.path == '/app' and response.headers.get('content-type', '').startswith('text/html'):
            content = b''.join([chunk async for chunk in response.body_iterator])
            content = content.replace(b'<header>', b'<div role="status" style="position:sticky;top:0;z-index:9999;background:#fff3cd;color:#5c4300;padding:8px 14px;text-align:center;font:600 14px sans-serif;border-bottom:1px solid #d6bd68">DEVELOPMENT MOCK SaaS</div><header>', 1)
            headers = dict(response.headers)
            headers.pop('content-length', None)
            return Response(content, status_code=response.status_code, headers=headers,
                            media_type=response.media_type, background=response.background)
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic's default errors echo invalid input, including client secrets.
        return JSONResponse({'detail': 'Invalid request'}, status_code=422)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "mock-saas"}

    @app.post("/oauth/token")
    async def token(request: Request):
        form = await request.form()
        if form.get("grant_type") != "client_credentials" or not form.get("client_id") or not form.get("client_secret"):
            raise HTTPException(401, "invalid_client")
        return {"access_token": "mock-token", "token_type": "Bearer", "expires_in": 3600,
                "scope": form.get("scope", "saas.tenant saas.user saas.department")}

    @app.post("/isv.php")
    async def operation(request: Request, authorization: str = Header(default="")):
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "invalid_token")
        try:
            async with httpx.AsyncClient(transport=transport, timeout=5, follow_redirects=False,
                                         trust_env=False) as client:
                response = await client.post(os.getenv("PLATFORM_OAUTH_INTROSPECTION_URL",
                    "http://market-api:8000/oauth/introspect"), data={"token": authorization[7:].strip()})
            response.raise_for_status()
            claims = response.json()
            if not isinstance(claims, dict) or claims.get("active") is not True:
                raise HTTPException(401, "invalid_token")
        except httpx.HTTPError:
            raise HTTPException(503, "token introspection unavailable") from None
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "invalid_operation")
        request_id = request.headers.get('X-Request-Id')
        if request_id:
            if body.get('idempotency_key', request_id) != request_id:
                raise HTTPException(409, 'idempotency_key_mismatch')
            body = {**body, 'idempotency_key': request_id}
        try:
            return store.machine_operation(body, claims)
        except MachineOperationError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    return app


app = create_app()
