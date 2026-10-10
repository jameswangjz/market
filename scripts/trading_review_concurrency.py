"""Race two approvals on an explicitly prepared, unpaid offline QA fixture."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

import httpx
from sqlalchemy import select
from app import main as m


def run(state, base):
    assert state["product_id"].startswith("qa-web-")
    headers = {"Authorization": "Bearer " + state["provider_token"]}
    path = base.rstrip("/") + "/api/orders/" + state["order_id"]
    quote = httpx.get(path + "/provider-quote", headers=headers, timeout=15)
    quote.raise_for_status()
    quote = quote.json()
    assert quote["main_status"] == "pending_provider_review"
    barrier = Barrier(2)
    def submit(amount):
        barrier.wait(timeout=15)
        response = httpx.post(path + "/provider-review", headers=headers, timeout=30,
            json={"decision": "approve", "amount": amount, "cost": 7,
                  "reason": "Isolated concurrent review test", "expected_updated_at": quote["expected_updated_at"]})
        return response.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(submit, (20, 21)))
    assert statuses == [200, 409], statuses
    with m.SessionLocal() as db:
        order = db.get(m.Order, state["order_id"])
        assert order.product_id == state["product_id"] and order.payment_status == "unpaid"
        payment = db.scalar(select(m.Payment).where(m.Payment.order_id == order.id))
        assert payment.amount == order.amount and order.amount in (20, 21)
        assert m.order_cost(db, order) == 7
        count = db.scalar(select(m.func.count()).select_from(m.AuditLog).where(
            m.AuditLog.target_id == order.id, m.AuditLog.action == "provider_review_approve"))
        assert count == 1
    return {"status": "passed", "database": "postgresql", "concurrent_responses": statuses,
            "successful_review_audits": count, "payment_matches_quote": True, "paid": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--base", default="http://market-api:8000")
    args = parser.parse_args()
    print(json.dumps(run(json.loads(Path(args.state).read_text()), args.base)))
