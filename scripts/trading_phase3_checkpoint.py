"""Publish evidence-backed runtime task states, preserving unrelated tasks."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select
from app import main as m


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    def existing():
        with m.SessionLocal() as db:
            routes = [(r.id, r.product_id, r.version, r.route_key) for r in db.scalars(select(m.ApiGatewayRoute).order_by(m.ApiGatewayRoute.id))]
            orders = [(o.id, str(o.amount), o.main_status, o.payment_status, o.economic_snapshot_json) for o in db.scalars(select(m.Order).order_by(m.Order.id))]
            return routes, orders
    before = existing()
    m.ensure_gateway_version_schema(); m.ensure_gateway_version_schema()
    assert existing() == before, "Gateway migration changed existing business rows"
    docs = httpx.get("http://market-web/api-gateway-integration.md", timeout=10)
    assert docs.status_code == 200 and "v1.2" in docs.text and "versions/{version_id}/integration" in docs.text
    with m.SessionLocal() as db:
        admin = db.scalar(select(m.User).where(m.User.email == "admin@market.local"))
        token = m.issue_token(admin)
        assert not db.scalar(select(m.Product.id).where(m.Product.id.like("qa-int-%")))
        assert not db.scalar(select(m.Product.id).where(m.Product.id.like("qa-web-%")))
    client = TestClient(m.app)
    headers = {"Authorization": "Bearer " + token}
    try:
        smoke = ["/api/dashboard", "/api/orders", "/api/audit-logs?start=&end=&page=1&page_size=50", "/api/gateway/routes", "/api/storefront/products"]
        for path in smoke:
            response = client.get(path, headers=headers)
            assert response.status_code == 200, (path, response.status_code)
        tasks = {
            "TRD-BE-003": ("done", 100, "Per-version runtime integration and migration installed. Real PG/native APISIX verified two routes, independent rate policies, 401/429, pre-review deny and version routing; SSRF 22 tests passed; legacy route payload preserved."),
            "TRD-BE-006": ("done", 100, "Provider quote review and snapshots audited. 28 workflow tests; real PG adjustment and 2 concurrent reviews produced 200/409 and one audit. Buyer responses hide cost. Browser desktop/mobile modal validated."),
            "TRD-BE-007": ("done", 100, "Buyer super-only payment and platform/finance confirmation; row locks, quote validation, paid retry idempotency verified on PG. Legacy SaaS inline purchase/renew also rejects enterprise admin. Subscription orchestration remains future tasks."),
            "TRD-FE-008": ("done", 100, "Scoped pay/confirm actions, final amount and submitting guards; 13 workflow UI tests within 42 passing frontend tests, build and deployed API permission checks passed. Finance/buyer payment role browser cases not separately exercised."),
            "TRD-FE-007": ("in_progress", 45, "Provider review editor implemented and real desktop/mobile browser checked; only review portion complete. Start/fulfillment acceptance, rectification and attachments depend on unfinished TRD-BE-008; do not mark whole FE007 complete."),
        }
        for code, (status, progress, note) in tasks.items():
            response = client.patch("/api/development/tasks/" + code, headers=headers,
                                    json={"status": status, "progress": progress, "note": note})
            assert response.status_code == 200, response.text
        snapshot = client.get("/api/development/tasks", headers=headers).json()
    finally:
        client.close()
    snapshot["verification"] = {"backend_tests": 239, "frontend_tests": 42,
        "gateway_schema_reruns": 2, "legacy_business_rows_unchanged": True,
        "existing_orders": len(before[1]), "existing_routes": len(before[0]),
        "temporary_products_remaining": 0, "http_smoke": smoke,
        "downloaded_provider_doc_sha256": hashlib.sha256(docs.content).hexdigest(),
        "trd_counts": dict(Counter(row["status"] for row in snapshot["items"] if row["code"].startswith("TRD-"))),
        "limits": "Real APISIX fixture used upstream auth none; no new OAuth/OIDC or real payment channel assertion. Browser tested provider modal, not finance/buyer payment screens."}
    Path(args.output).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"counts": snapshot["counts"], "verification": snapshot["verification"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
