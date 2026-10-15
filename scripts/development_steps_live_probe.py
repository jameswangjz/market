"""Read-only development task snapshot and deployed API verification, inside API pod."""
import hashlib
import json
import sys

import httpx
from sqlalchemy import select

from app import main as m


def main():
    verify = "--verify" in sys.argv
    with m.SessionLocal() as db:
        tasks = db.scalars(select(m.DevelopmentTask).order_by(m.DevelopmentTask.code)).all()
        state = [{"code": t.code, "status": t.status, "progress": t.progress,
                  "acceptance": t.acceptance, "updated_at": str(t.updated_at)} for t in tasks]
        admin = db.scalar(select(m.User).where(m.User.email == "admin@market.local"))
        assert admin and admin.platform_role == "super_admin"
        token = m.issue_token(admin)
        operator = db.scalar(select(m.User).where(m.User.platform_role == "platform_operator"))
        operator_token = m.issue_token(operator) if operator else None
    result = {"task_count": len(state), "parent_state_sha256": hashlib.sha256(
        json.dumps(state, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
    if verify:
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as client:
            response = client.get("/api/development/tasks", headers={"Authorization": "Bearer " + token})
            response.raise_for_status()
            data = response.json()
            items = [x for x in data["items"] if x["code"].startswith("TRD-R3-")]
            steps = [s for t in items for s in t.get("subtasks", [])]
            assert len(items) == 32 and len(steps) == 124
            assert data["subtask_count"] == 124
            assert len({s["id"] for s in steps}) == 124
            assert all(s["status"] == "untracked" and s["title"] for s in steps)
            denied = client.get("/api/development/tasks")
            assert denied.status_code in (401, 403)
            result.update({"api_status": response.status_code, "r3_tasks": len(items),
                           "r3_substeps": len(steps), "anonymous_status": denied.status_code})
            if operator_token:
                denied = client.get("/api/development/tasks", headers={"Authorization": "Bearer " + operator_token})
                assert denied.status_code == 403
                result["operator_status"] = denied.status_code
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
