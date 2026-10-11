"""Persist isolated browser fixtures using the real runtime's trusted hook.

Copy this file and subscription_pg_concurrency.py into /tmp in the API pod.
Run with PYTHONPATH=/app; no lifespan, migration, provisioning or health probe.
Token/state files must be private temporary files, never repository artifacts.
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
from unittest.mock import patch

import subscription_pg_concurrency as fixture
from sqlalchemy import func, select

m = fixture.m


def temporary_path(value):
    path = Path(value).absolute()
    roots = [Path("/tmp"), Path("/var/tmp")]
    if path.parent not in roots or path.is_symlink():
        raise ValueError("Use a direct, non-symlink file in /tmp or /var/tmp")
    return path


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, default=lambda value: value.isoformat())
        stream.flush()
        os.fsync(stream.fileno())


def prepare(state_path, token_path):
    if state_path == token_path or state_path.exists() or token_path.exists():
        raise ValueError("Fixture files must be distinct and absent")
    prefix = "qa-sub-concurrency-b" + secrets.token_hex(6)
    state = {
        "prefix": prefix, "product": prefix + "-p", "version": prefix + "-v",
        "users": [prefix + "-ub", prefix + "-up"],
        "enterprises": [prefix + "-eb", prefix + "-ep"],
        "emails": [prefix + "-buyer@example.invalid", prefix + "-provider@example.invalid"],
        "requests": [prefix + "-browser"], "anchor": m.now(),
        "limits": {"rate_limit_per_minute": 17, "daily_quota": 123,
                   "monthly_quota": 456, "quota_amount": 789},
    }
    with m.SessionLocal() as db:
        fixture.check(db.bind.dialect.name == "postgresql", "Real PostgreSQL required")
        for model, ids in ((m.User, state["users"]), (m.Enterprise, state["enterprises"]),
                           (m.Product, [state["product"]]),
                           (m.ProductReleaseVersion, [state["version"]])):
            fixture.check(db.scalar(select(func.count()).select_from(model)
                                    .where(model.id.in_(ids))) == 0, "Fixture ID collision")
    write_private(state_path, state)
    original = m.storefront["versions_for"]

    def versions_for(db, product):
        return [{"id": state["version"]}] if product.id == state["product"] else original(db, product)

    try:
        with patch.dict(m.storefront, {"versions_for": versions_for}):
            orders, token = fixture.prepare(state)
        for oid in orders:
            with m.SessionLocal() as db:
                order = db.scalar(select(m.Order).where(m.Order.id == oid).with_for_update())
                fixture.check(order.product_id == state["product"], "Unexpected order ownership")
                m.subscription_terms["on_delivery_success"](
                    db, order, db.get(m.User, state["users"][1]), at=state["anchor"])
                db.commit()
        combination = fixture.verify_terms(state, orders)
        with m.SessionLocal() as db:
            numbers = [db.get(m.Order, oid).order_no for oid in orders]
        write_private(token_path, {
            "token": token, "order_id": orders[0], "order_no": numbers[0],
            "order_ids": orders, "order_nos": numbers, "product_id": state["product"],
            "enterprise_id": state["enterprises"][0], "combination_id": combination,
            "limits": state["limits"], "months": [2, 3],
        })
    except BaseException:
        fixture.cleanup(state)
        state_path.unlink()
        token_path.unlink(missing_ok=True)
        raise
    return {"status": "prepared", "fixture": prefix, "orders": orders,
            "terms": 2, "months": 5, "layer": "trusted hook; no actual health/provisioning assertion"}


def cleanup(state_path, token_path):
    state = json.loads(state_path.read_text())
    state["anchor"] = datetime.fromisoformat(state["anchor"])
    fixture.check(state["prefix"].startswith("qa-sub-concurrency-b"), "Not a browser fixture")
    original_scopes = fixture.scopes

    def browser_scopes(db, owned):
        predicates = original_scopes(db, owned)
        # A pod restart creates an OAuth client for every published API product.
        # Lock this exact fixture product before discovering that owned child.
        db.scalar(select(m.Product).where(m.Product.id == owned["product"]).with_for_update())
        predicates[m.OAuthClient.__tablename__] = m.OAuthClient.product_id == owned["product"]
        db.scalars(select(m.Enterprise).where(m.Enterprise.id.in_(owned["enterprises"]))
                   .order_by(m.Enterprise.id).with_for_update()).all()
        predicates[m.ServiceLevelAssignment.__tablename__] = (
            m.ServiceLevelAssignment.enterprise_id.in_(owned["enterprises"]))
        return predicates

    # Retain shared outbox locks, FK deletion ordering and leftover verification.
    with patch.object(fixture, "scopes", browser_scopes):
        fixture.cleanup(state)
    token_path.unlink(missing_ok=True)
    state_path.unlink()
    return {"status": "cleaned", "fixture": state["prefix"],
            "cleanup": "verified no owned rows or FK leftovers"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "cleanup"))
    parser.add_argument("--state-file", required=True, type=temporary_path)
    parser.add_argument("--token-file", required=True, type=temporary_path)
    args = parser.parse_args()
    print(json.dumps(globals()[args.action](args.state_file, args.token_file), indent=2))
