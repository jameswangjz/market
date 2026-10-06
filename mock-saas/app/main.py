from datetime import datetime, timezone
from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="Market Mock SaaS")
tenants = {}
users = {}
departments = {}


@app.get("/health")
def health():
    return {"status": "ok", "service": "mock-saas"}


@app.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    if form.get("grant_type") != "client_credentials" or not form.get("client_id") or not form.get("client_secret"):
        raise HTTPException(401, "invalid_client")
    return {"access_token": "mock-token", "token_type": "Bearer", "expires_in": 3600, "scope": form.get("scope", "saas.tenant saas.user saas.department")}


@app.post("/isv.php")
async def operation(request: Request, authorization: str = Header(default="")):
    if authorization != "Bearer mock-token":
        raise HTTPException(401, "invalid_token")
    body = await request.json()
    operation_type = body.get("type")
    tenant_id = body.get("tenant_id") or "tenant-" + body.get("subscription_id", "unknown")[:12]
    if operation_type == "OPEN":
        tenants.setdefault(tenant_id, {"tenant_id": tenant_id, "app_id": "app-" + tenant_id[7:], "version": body.get("version"), "status": "opened"})
        tenant = tenants[tenant_id]
        return {"ret": 0, "msg": "ok", **tenant, "url": f"https://mock-saas.local/t/{tenant_id}"}
    if operation_type == "CLOSE":
        tenants.setdefault(tenant_id, {"tenant_id": tenant_id})["status"] = "closed"
        return {"ret": 0, "tenant_id": tenant_id, "status": "closed"}
    if operation_type == "RENEW":
        return {"ret": 0, "tenant_id": tenant_id, "status": "active", "expires_at": datetime.now(timezone.utc).isoformat()}
    if operation_type == "CHANGE":
        tenants.setdefault(tenant_id, {"tenant_id": tenant_id})["version"] = body.get("to_version")
        tenants[tenant_id]["status"] = "active"
        return {"ret": 0, "tenant_id": tenant_id, "version": body.get("to_version"), "status": "active"}
    if operation_type == "USER_ASSIGN":
        key = f"{tenant_id}:{body.get('user_id')}"
        users[key] = {"user_id": body.get("user_id"), "status": "active"}
        return {"ret": 0, "tenant_id": tenant_id, "user_id": "mock-user-" + body.get("user_id", "")[:12], "status": "active"}
    if operation_type == "USER_UNASSIGN":
        return {"ret": 0, "tenant_id": tenant_id, "user_id": body.get("user_id"), "status": "deleted"}
    if operation_type == "DEPT_CREATE":
        key = f"{tenant_id}:{body.get('department_id')}"
        departments[key] = body
        return {"ret": 0, "tenant_id": tenant_id, "department_id": "mock-dept-" + body.get("department_id", "")[:12], "status": "active"}
    if operation_type == "DEPT_REMOVE":
        return {"ret": 0, "tenant_id": tenant_id, "department_id": body.get("department_id"), "status": "deleted"}
    return {"ret": 1, "msg": f"unsupported operation: {operation_type}", "retryable": False}
