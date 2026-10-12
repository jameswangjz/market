import os
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from .oidc_rp import install
from .store import MachineOperationError, Store


def create_app(*, transport=None):
    app = FastAPI(title="Market Mock SaaS")
    store = Store(os.getenv('MOCK_SAAS_DB_PATH', '/data/mock-saas.sqlite3'))
    app.state.store = store
    install(app, store, transport=transport)

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
        try:
            return store.machine_operation(body, claims)
        except MachineOperationError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    return app


app = create_app()
