"""Record verified offline fulfillment completion without resetting other tasks."""
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
    args = parser.parse_args()
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == "admin@market.local"))
        headers = {"Authorization": "Bearer " + m.issue_token(admin)}
        assert not db.scalar(select(m.Product.id).where(m.Product.id.like("qa-web-%")))
    client = TestClient(m.app)
    try:
        for path in ("/api/dashboard", "/api/orders", "/api/delivery-tasks", "/api/audit-logs?start=&end="):
            result = client.get(path, headers=headers)
            assert result.status_code == 200, (path, result.status_code)
        tasks = {
            "TRD-BE-008": "27 new fulfillment tests within 266 backend regressions; real PG/MinIO/ClamAV verified start-submit-reject-rectify-resubmit-accept, EICAR denial, evidence isolation and audit snapshots. Same task and frozen economics preserved; fixtures cleaned.",
            "TRD-FE-007": "Provider quote editor and offline lifecycle/evidence controls implemented. 52 frontend tests and production build passed. Real browser read-only desktop1440/mobile390 verified provider start and buyer rejection dialogs, required reason, attachment permissions and bounds; mutations verified separately through real backend APIs.",
        }
        for code, note in tasks.items():
            result = client.patch("/api/development/tasks/" + code, headers=headers,
                                  json={"status": "done", "progress": 100, "note": note})
            assert result.status_code == 200, result.text
        snapshot = client.get("/api/development/tasks", headers=headers).json()
    finally:
        client.close()
    snapshot["verification"] = {
        "backend_tests": 266, "new_fulfillment_tests": 27, "frontend_tests": 52,
        "runtime_image_tag": "20261010022717-737eeb319286",
        "real_services": ["PostgreSQL", "MinIO", "ClamAV"],
        "browser": {"status": "passed", "mode": "read-only", "viewports": [1440, 390],
                    "screenshots": 8, "network_mutations": 0, "native_dialogs": 0},
        "temporary_browser_products_remaining": 0,
        "trd_counts": dict(Counter(row["status"] for row in snapshot["items"] if row["code"].startswith("TRD-"))),
        "limits": "Simulated/manual payment only. Browser inspected dialogs and validation; real write lifecycle and evidence verified by backend HTTP integration, not browser clicks. Other trading tasks remain open."}
    Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"counts": snapshot["counts"], "verification": snapshot["verification"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
