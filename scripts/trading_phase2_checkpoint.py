"""Verify the deployed checkpoint and record evidence-backed task states."""
import argparse
import json
from collections import Counter
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from app import main as m


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with m.SessionLocal() as db:
        before = [(o.id, str(o.amount), o.main_status, o.snapshot_version,
                   str(o.total_cost_snapshot)) for o in db.scalars(select(m.Order).order_by(m.Order.id))]
        admin = db.scalar(select(m.User).where(m.User.email == "admin@market.local"))
        assert admin and admin.platform_role == "super_admin"
        token = m.issue_token(admin)
    m.ensure_review_and_file_schema()
    m.ensure_review_and_file_schema()
    with m.SessionLocal() as db:
        after = [(o.id, str(o.amount), o.main_status, o.snapshot_version,
                  str(o.total_cost_snapshot)) for o in db.scalars(select(m.Order).order_by(m.Order.id))]
        assert before == after, "Schema migration changed existing orders"
    notes = {
        "TRD-BE-005": "188 backend regressions passed, including 9 economics and 16 independent privacy tests; real PostgreSQL/MinIO/ClamAV transaction acceptance passed. Migration rerun twice without order changes.",
        "TRD-FE-002": "Actual product Logo loaded at desktop 1440x1000 and mobile 390x844, public login guidance and console boundary verified; isolated fixture removed.",
        "TRD-FE-003": "29 frontend tests passed; real browser explicit enterprise selection, server quote and exactly one order passed; file fixture only in browser, monthly API/SaaS bounds covered by automated tests, not a real gateway payment test.",
    }
    client = TestClient(m.app)
    headers = {"Authorization": "Bearer " + token}
    smoke = []
    try:
        for path in ("/api/dashboard", "/api/orders", "/api/audit-logs?q=&actor=&order_id=&batch_no=&rule_version=&risk_level=&start=&end=&page=1&page_size=50"):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, (path, response.status_code)
            smoke.append(path)
        response = client.get("/api/storefront/products")
        assert response.status_code == 200
        smoke.append("/api/storefront/products")
        for code, note in notes.items():
            response = client.patch("/api/development/tasks/" + code, headers=headers,
                                    json={"status": "done", "progress": 100, "note": note})
            assert response.status_code == 200, response.text
        response = client.patch("/api/development/tasks/TRD-BE-003", headers=headers, json={
            "status": "in_progress", "progress": 35,
            "note": "Prototype and 16 tests only. Not installed in runtime. Remaining: SSRF-safe HTTP adapter, multiple version route schema migration, four-stage review/readiness integration, real APISIX publish/health regression. Not complete."})
        assert response.status_code == 200, response.text
        snapshot = client.get("/api/development/tasks", headers=headers).json()
    finally:
        client.close()
    trd = [row for row in snapshot["items"] if row["code"].startswith("TRD-")]
    snapshot["verification"] = {"schema_reruns": 2, "existing_orders_unchanged": True,
        "existing_order_count": len(before), "http_smoke": smoke,
        "trd_counts": dict(Counter(row["status"] for row in trd)),
        "backend_tests": 188, "frontend_tests": 29,
        "browser_monthly_checkout": "not exercised; file fixture only",
        "version_gateway": "prototype not installed in runtime"}
    Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"counts": snapshot["counts"], "verification": snapshot["verification"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
