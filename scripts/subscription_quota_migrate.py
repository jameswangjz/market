"""Administrator-only, explicit conservative Redis cutover; no silent resets."""
import argparse
import json
import os
from pathlib import Path

from sqlalchemy import select
from app import main as m
from app.quota_cutover import preview, execute, token


def run(args):
    client = m.apisix_policy_redis()
    with m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == args.actor))
        if not actor or not actor.is_active or actor.verified_status != "verified":
            raise RuntimeError("Actor must be an active, verified platform administrator")
        m.require_platform_admin(actor)
        combo = db.scalar(select(m.SubscriptionCombination).where(
            m.SubscriptionCombination.id == args.combination_id).with_for_update())
        if not combo or combo.delivery_method not in {"api", "model_api"}:
            raise RuntimeError("API subscription combination not found")
        rows = db.execute(select(m.ApiCredential, m.ApiGatewayRoute).join(
            m.ApiGatewayRoute, m.ApiGatewayRoute.id == m.ApiCredential.route_id).where(
            m.ApiCredential.enterprise_id == combo.enterprise_id,
            m.ApiGatewayRoute.product_id == combo.product_id).order_by(m.ApiCredential.id)
            .with_for_update()).all()
        sources = [(route.route_key, credential.apisix_consumer_name,
                    m.subscription_gateway["metadata"](db, credential, route))
                   for credential, route in rows if credential.apisix_consumer_name]
        if args.execute:
            prepared = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
            plan = prepared["plan"]
            if plan["scope"] != combo.id or plan["actor"] != actor.email:
                raise RuntimeError("Preview owner or combination changed")
            # Use metadata as the authority, not arbitrary keys in a supplied file.
            expected_metadata = {(c, json.dumps(d, sort_keys=True)) for _, c, d in sources}
            actual_metadata = {(s["consumer"], json.dumps(s["data"], sort_keys=True)) for s in plan["sources"]}
            receipt_key = f"market:apisix:quota:migration:{token(combo.id)}:{token(plan['request_id'])}"
            if prepared["keys"][3] != receipt_key:
                raise RuntimeError("Invalid migration receipt key")
            if not client.exists(receipt_key):
                if expected_metadata != actual_metadata or len(plan["sources"]) != len(sources):
                    raise RuntimeError("Approved source set or frozen subscription policy changed; preview again")
                fresh = preview(client, combo.id, sources, actor=actor.email, request_id=plan["request_id"])
                if prepared["keys"] != fresh["keys"]:
                    raise RuntimeError("Migration key set changed; preview again")
                for field in ("minute_end", "day_end"):
                    if plan[field] != fresh["plan"][field]:
                        raise RuntimeError("Migration clock boundary changed; preview again")
                if plan["valid_until"] > min(plan["created_at"]+30, fresh["plan"]["valid_until"]):
                    raise RuntimeError("Invalid migration preview expiration")
                for saved, approved in zip(plan["sources"], fresh["plan"]["sources"]):
                    if saved != approved:
                        raise RuntimeError("Migration source indexes changed; preview again")
            result = execute(client, prepared,
                             acknowledge_conservative_usage=args.acknowledge_conservative_usage,
                             cutover_guard_verified=args.cutover_guard_verified)
            existing = db.scalar(select(m.AuditLog.id).where(
                m.AuditLog.action == "migrate_subscription_quota", m.AuditLog.target_id == combo.id,
                m.AuditLog.request_id == plan["request_id"]))
            if not existing:
                m.audit(db, actor.email, "migrate_subscription_quota", "subscription", combo.id,
                        "Explicit conservative lifetime watermark; Redis receipt retained for recovery",
                        category="security", tenant_id=combo.enterprise_id,
                        request_id=plan["request_id"], before=result["before"], after=result["after"])
                db.commit()
            return result
        prepared = preview(client, combo.id, sources, actor=actor.email, request_id=args.request_id)
        descriptor = os.open(args.plan_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(prepared, ensure_ascii=False, indent=2))
        return {"mode": "preview_only", "scope": combo.id,
                "estimated_lifetime_watermark": prepared["plan"]["estimated_lifetime_watermark"],
                "valid_until": prepared["plan"]["valid_until"],
                "warning": "Legacy lifetime count is conservatively charged to current subscription month and Beijing day. May exhaust allowance; no accurate historical cycle reconstruction claimed."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--combination-id", required=True)
    parser.add_argument("--actor", default="admin@market.local")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--acknowledge-conservative-usage", action="store_true")
    parser.add_argument("--cutover-guard-verified", action="store_true")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False))
