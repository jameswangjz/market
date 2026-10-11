"""Record the verified subscription stage without resetting unrelated tasks."""
import argparse
from collections import Counter
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from app import main as m


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--runtime-tag", required=True)
    args = parser.parse_args()
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == "admin@market.local"))
        headers = {"Authorization": "Bearer " + m.issue_token(admin)}
        assert not db.scalar(select(m.Product.id).where(
            m.Product.id.like("qa-int-%") | m.Product.id.like("qa-sub-concurrency-%")))
    tasks = {
        "TRD-BE-009": ("done", 100, "Original anchor, generation and per-order half-open terms persisted. 32 term tests within 304 backend regressions; real PostgreSQL contending transactions and native APISIX verified delivery/renewal passed. January31/month-end drift and future-term isolation covered. Historical SaaS is not re-imported."),
        "TRD-BE-010": ("done", 100, "Same-version renewal creates unpaid frozen order/payment atomically with idempotency record. PostgreSQL proves two blocked independent transactions produce one renewal order/payment; paid verified delivery appends contiguous terms. Unpaid/future terms do not increase current quota; expired restart and repeat hook covered. Cross-version changes are separate tasks."),
        "TRD-BE-011": ("in_progress", 80, "New subscription policies run in native APISIX: stable enterprise/product scope, original-anchor month and Beijing day, atomic Redis quotas, zero unlimited, preserved rotation usage, expired/future/revoked/overlap denial, fail-closed route identity. 14 real Lua+Redis and real three-replica APISIX acceptance passed. Existing used legacy counters are deliberately blocked from direct migration; strict online migration and production recovery gates remain open."),
        "TRD-FE-009": ("in_progress", 45, "Order subscription panel, term list and styled same-version renewal dialog implemented; 67 frontend tests and production build passed. Frozen quota display, stale request protection and stable idempotency keys covered. New subscription panel browser E2E is not yet accepted; upgrade/downgrade preview, compatibility consent and refund/change status await BE016/017/018."),
    }
    client = TestClient(m.app)
    try:
        for path in ("/api/dashboard", "/api/orders", "/api/audit-logs?start=&end="):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, (path, response.status_code)
        for code, (status, progress, note) in tasks.items():
            response = client.patch("/api/development/tasks/" + code, headers=headers,
                                    json={"status": status, "progress": progress, "note": note})
            assert response.status_code == 200, (code, response.text)
        snapshot = client.get("/api/development/tasks", headers=headers).json()
    finally:
        client.close()
    snapshot["verification"] = {
        "backend_tests": 304, "real_lua_redis_tests": 14, "frontend_tests": 67,
        "runtime_image_tag": args.runtime_tag,
        "real_services": ["PostgreSQL", "APISIX (3 replicas)", "Redis", "MinIO", "ClamAV"],
        "postgresql_contention": {"terms": 2, "months": 5, "renewal_orders": 1,
                                   "independent_connections": True, "cleanup": "verified"},
        "browser_subscription_panel": "not yet accepted",
        "trd_counts": dict(Counter(row["status"] for row in snapshot["items"]
                                   if row["code"].startswith("TRD-"))),
        "limits": ["Simulated/manual payment only", "Legacy used counter migration remains blocked",
                   "Version-change/refund/settlement compensation remains separate unfinished work",
                   "New SaaS provisioning/OIDC not covered by this stage",
                   "Runtime upstream auth none; no new OAuth/OIDC assertion"],
    }
    Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"counts": snapshot["counts"], "verification": snapshot["verification"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
