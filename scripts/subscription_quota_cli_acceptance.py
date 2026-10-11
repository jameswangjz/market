"""Committed PostgreSQL/Redis CLI acceptance, with exact owned fixture cleanup.

Run with the backend and repository root on PYTHONPATH. Supply
--cutover-guard-verified only after verifying every deployed gateway replica.
This exercises the administrative CLI layer, not APISIX requests/provisioning.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import delete, func, select

from app import main as m
from app.quota_cutover import preview
from scripts import subscription_pg_concurrency as fixture
from scripts import subscription_quota_migrate as migrator


class InjectedAuditFailure(RuntimeError):
    pass


def run(*, cutover_guard_verified=False):
    check = fixture.check
    check(cutover_guard_verified, "Verify all gateway replicas before supplying the guard acknowledgement")
    check(m.engine.dialect.name == "postgresql", "Real migrated PostgreSQL is required")
    with m.SessionLocal() as db:
        actor = db.scalar(select(m.User).where(m.User.email == "admin@market.local"))
        check(actor is not None and actor.is_active and actor.verified_status == "verified"
              and actor.activation_status == "active", "Administrator must be active and verified")
        m.require_platform_admin(actor)

    prefix = "qa-sub-concurrency-" + secrets.token_hex(4)
    state = {"prefix": prefix, "product": prefix + "-p", "version": prefix + "-v",
             "users": [prefix + "-ub", prefix + "-up"],
             "enterprises": [prefix + "-eb", prefix + "-ep"],
             "emails": [prefix + "-buyer@example.invalid", prefix + "-provider@example.invalid"],
             "requests": [prefix + "-cli"], "anchor": m.now(),
             "limits": {"rate_limit_per_minute": 17, "daily_quota": 123,
                        "monthly_quota": 456, "quota_amount": 789}}
    route_id, credential_id = prefix + "-r", prefix + "-c"
    route_key, consumer = prefix + "-route", prefix + "-consumer"
    metadata_key = "market:apisix:credential:" + consumer
    total_key = f"market:apisix:quota:total:{route_key}:{consumer}"
    client = m.apisix_policy_redis()
    client.ping()
    fixture_started = False
    redis_owned = False
    owned_keys = []
    original_versions = m.storefront["versions_for"]

    def versions_for(db, product):
        if product.id == state["product"]:
            return [{"id": state["version"]}]
        return original_versions(db, product)

    def audit_count(combo_id):
        with m.SessionLocal() as db:
            return db.scalar(select(func.count()).select_from(m.AuditLog).where(
                m.AuditLog.action == "migrate_subscription_quota",
                m.AuditLog.target_id == combo_id,
                m.AuditLog.request_id == state["requests"][0]))

    with m.SessionLocal() as db:
        for model, ids in [(m.User, state["users"]), (m.Enterprise, state["enterprises"]),
                           (m.Product, [state["product"]]), (m.ProductReleaseVersion, [state["version"]]),
                           (m.ApiGatewayRoute, [route_id]), (m.ApiCredential, [credential_id])]:
            check(db.scalar(select(func.count()).select_from(model).where(model.id.in_(ids))) == 0,
                  "Fixture collision; refusing to touch existing rows")
        check(db.scalar(select(m.ApiGatewayRoute.id).where(m.ApiGatewayRoute.route_key == route_key)) is None,
              "Route key collision")
        check(db.scalar(select(m.ApiCredential.id).where(
            m.ApiCredential.apisix_consumer_name == consumer)) is None, "Consumer collision")

    try:
        fixture_started = True
        with patch.dict(m.storefront, {"versions_for": versions_for}):
            order_ids, _ = fixture.prepare(state)
        for order_id in order_ids:
            with m.SessionLocal() as db:
                order = db.scalar(select(m.Order).where(m.Order.id == order_id).with_for_update())
                provider = db.get(m.User, state["users"][1])
                check(m.subscription_terms["on_delivery_success"](
                    db, order, provider, at=state["anchor"]) is not None, "Fixture term was not granted")
                db.commit()
        combo_id = fixture.verify_terms(state, order_ids)
        with m.SessionLocal() as db:
            route = m.ApiGatewayRoute(id=route_id, product_id=state["product"], route_key=route_key,
                                      version="v1", upstream_url="http://fixture.invalid",
                                      status="draft", created_by=prefix)
            db.add(route)
            db.flush()
            credential = m.ApiCredential(
                id=credential_id, route_id=route.id, enterprise_id=state["enterprises"][0],
                order_id=order_ids[0], product_version_id=state["version"], name=prefix,
                key_hash=hashlib.sha256(consumer.encode()).hexdigest(),
                apisix_consumer_name=consumer, status="active", created_by=prefix)
            db.add(credential)
            db.flush()
            approved_metadata = m.subscription_gateway["metadata"](db, credential, route)
            check(approved_metadata["quota_scope"] == combo_id, "Credential combination mismatch")
            db.commit()

        # Leave enough of this Redis minute for preview, execution, and recovery.
        while int(client.time()[0]) % 60 >= 57:
            time.sleep(0.2)
        prepared = preview(client, combo_id, [(route_key, consumer, approved_metadata)],
                           actor="admin@market.local", request_id=state["requests"][0])
        minute_key = prepared["keys"][6]
        check(minute_key.startswith(f"market:apisix:quota:minute:{route_key}:{consumer}:"),
              "Unexpected legacy minute key")
        owned_keys = [metadata_key, total_key, minute_key, *prepared["keys"][:4]]
        check(len(set(owned_keys)) == 7 and not client.exists(*owned_keys),
              "Redis fixture collision; refusing to overwrite")
        redis_owned = True
        with client.pipeline(transaction=True) as pipe:
            pipe.hset(metadata_key, mapping={"status": "active", "daily_quota": "0",
                                            "monthly_quota": "0", "total_quota": "0"})
            pipe.set(total_key, 5)
            pipe.set(minute_key, 5)
            pipe.execute()

        with tempfile.TemporaryDirectory(prefix=prefix + "-") as directory:
            plan_path = Path(directory) / "preview.json"
            fd = os.open(plan_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            args = SimpleNamespace(actor="admin@market.local", combination_id=combo_id,
                                   request_id=state["requests"][0], plan_file=str(plan_path),
                                   execute=False, acknowledge_conservative_usage=True,
                                   cutover_guard_verified=cutover_guard_verified)
            before_preview = {key: client.dump(key) for key in owned_keys}
            preview_result = migrator.run(args)
            check(preview_result["mode"] == "preview_only"
                  and preview_result["estimated_lifetime_watermark"] == 5, "CLI preview was incorrect")
            check({key: client.dump(key) for key in owned_keys} == before_preview,
                  "CLI preview mutated Redis")
            check(audit_count(combo_id) == 0, "Preview wrote a migration audit")
            check(stat.S_IMODE(plan_path.stat().st_mode) == 0o600, "Plan must remain private (0600)")
            saved = json.loads(plan_path.read_text())
            check(saved["keys"][:4] == prepared["keys"][:4], "Preview crossed a Redis clock boundary")
            destinations, receipt_key = saved["keys"][:3], saved["keys"][3]
            args.execute = True
            real_audit = m.audit
            audit_calls = []

            def fail_once(*audit_args, **audit_kwargs):
                audit_calls.append(True)
                if len(audit_calls) == 1:
                    raise InjectedAuditFailure("Intentional failure after Redis migration commit")
                return real_audit(*audit_args, **audit_kwargs)

            with patch.object(m, "audit", side_effect=fail_once):
                try:
                    migrator.run(args)
                except InjectedAuditFailure:
                    pass
                else:
                    raise AssertionError("Audit failure injection was not reached")
                receipt_raw = client.get(receipt_key)
                check(receipt_raw is not None, "Audit failure lost the committed Redis receipt")
                receipt = json.loads(receipt_raw)
                check(receipt["before"] == {"minute": 0, "day": 0, "month": 0}
                      and receipt["after"] == {"minute": 5, "day": 5, "month": 5},
                      "Conservative cutover must charge exactly five")
                check(client.mget(destinations) == ["5", "5", "5"]
                      and audit_count(combo_id) == 0, "Audit failure did not preserve Redis-only commit")
                recovered = migrator.run(args)
                check(recovered == receipt and audit_count(combo_id) == 1, "Audit recovery failed")
                repeated = migrator.run(args)
                check(repeated == receipt and audit_count(combo_id) == 1, "Repeat execution duplicated audit")
                check(len(audit_calls) == 2, "Expected one failed and one successful audit call")
            check(client.get(receipt_key) == receipt_raw and client.mget(destinations) == ["5", "5", "5"],
                  "Recovery/repeat execution changed receipt or double charged counters")
            check(client.mget([total_key, minute_key]) == ["5", "5"], "Legacy usage was modified")
            check(client.hget(metadata_key, "mode") == "subscription"
                  and client.hget(metadata_key, "quota_scope") == combo_id, "Metadata cutover failed")
            check(client.ttl(destinations[2]) == -1, "Subscription month counter must persist")
            with m.SessionLocal() as db:
                row = db.scalar(select(m.AuditLog).where(
                    m.AuditLog.action == "migrate_subscription_quota", m.AuditLog.target_id == combo_id,
                    m.AuditLog.request_id == args.request_id))
                check(row.actor == args.actor and row.tenant_id == state["enterprises"][0],
                      "Recovered audit ownership mismatch")
            result = {"status": "passed", "fixture": prefix, "database": "postgresql",
                      "layer": "CLI administrative Redis/PostgreSQL only; no APISIX calls or provisioning",
                      "preview_watermark": 5, "counts_after_execute_recovery_repeat": [5, 5, 5],
                      "injected_audit_failures": 1, "migration_audits": 1,
                      "receipt_unchanged": True, "plan_permissions": "0600"}
        check(not plan_path.exists(), "Temporary preview file remains")
    finally:
        try:
            if redis_owned:
                client.delete(*owned_keys)
                check(client.exists(*owned_keys) == 0, "Owned Redis keys remain")
        finally:
            try:
                if fixture_started:
                    # These FKs are outside the helper's scope; remove them first.
                    with m.SessionLocal() as db:
                        db.execute(delete(m.ApiCredential).where(
                            m.ApiCredential.id == credential_id,
                            m.ApiCredential.route_id == route_id,
                            m.ApiCredential.enterprise_id == state["enterprises"][0]))
                        db.execute(delete(m.ApiGatewayRoute).where(
                            m.ApiGatewayRoute.id == route_id,
                            m.ApiGatewayRoute.product_id == state["product"]))
                        db.commit()
                    fixture.cleanup(state)
                    with m.SessionLocal() as db:
                        check(db.get(m.ApiCredential, credential_id) is None
                              and db.get(m.ApiGatewayRoute, route_id) is None,
                              "Owned route/credential remain")
            finally:
                client.close()
    result["cleanup"] = {"redis_keys_removed": len(owned_keys), "owned_route_credential_removed": True,
                         "fixture_and_audits_removed": True, "private_plan_removed": True}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutover-guard-verified", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(cutover_guard_verified=args.cutover_guard_verified), sort_keys=True))
