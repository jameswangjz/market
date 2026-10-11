"""Real PG SaaS reservation acceptance; run in a dedicated backend process.

PYTHONPATH=backend DATABASE_URL=postgresql+psycopg://... \
  python scripts/saas_combination_pg_concurrency.py --scenario all
Recovery in the same database: --recover /tmp/saas-combination-....json

Normal authenticated POST /api/orders, no lifespan or dependency overrides.
Only catalog visibility for the disposable product is patched. No payments,
provisioning, migrations, existing product/user writes or external services.
Both first-purchase races use subscription_pg_concurrency.race to observe two
distinct blocked PostgreSQL PIDs before releasing the product/enterprise gate.
IDs-only 0600 manifests precede commits and survive failed cleanup.
"""

import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import threading
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, or_, select, text


m = helper = None
SCENARIOS = ("product", "enterprise", "history")


def check(condition, detail):
    if not condition:
        raise AssertionError(detail)


def load_backend():
    global m, helper
    from app import main
    m = main
    spec = importlib.util.spec_from_file_location(
        "saas_subscription_helper", Path(__file__).with_name("subscription_pg_concurrency.py"))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)


def new_state(scenario, prefix=None):
    prefix = prefix or "qa-saas-pg-" + secrets.token_hex(8)
    return {"prefix": prefix, "scenario": scenario, "product": prefix + "-p",
            "version": prefix + "-v", "users": [prefix + "-ub", prefix + "-up"],
            "enterprises": [prefix + "-eb", prefix + "-ep"],
            "emails": [prefix + "-buyer@example.invalid", prefix + "-provider@example.invalid"],
            "memberships": [prefix + "-mb", prefix + "-mp"],
            "historical": [prefix + "-s1", prefix + "-s2", prefix + "-sx"],
            "requests": [prefix + f"-http{i}" for i in range(16)]}


def validate_state(state):
    check(isinstance(state, dict), "Invalid manifest")
    prefix = state.get("prefix", "")
    check(isinstance(prefix, str) and re.fullmatch(r"qa-saas-pg-[0-9a-f]{16}", prefix),
          "Invalid fixture marker")
    check(state.get("scenario") in SCENARIOS
          and state == new_state(state.get("scenario"), prefix), "Refusing unowned manifest IDs")


def persist_manifest(state):
    validate_state(state)
    fd, path = tempfile.mkstemp(prefix="saas-combination-", suffix=".json", dir="/tmp")
    with os.fdopen(fd, "w") as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(state, output)
        output.flush()
        os.fsync(output.fileno())
    directory = os.open("/tmp", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return path


def read_manifest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
              and info.st_uid == os.getuid(), "Recovery manifest must be an owned 0600 file")
        state = json.load(source)
    validate_state(state)
    return state


def fixture_models(state):
    return ((m.User, state["users"]), (m.Enterprise, state["enterprises"]),
            (m.Product, [state["product"]]), (m.SaaSProductVersion, [state["version"]]),
            (m.Membership, state["memberships"]),
            (m.SaaSSubscription, state["historical"]))


def check_collisions(state):
    validate_state(state)
    with m.SessionLocal() as db:
        for model, ids in fixture_models(state):
            check(db.scalar(select(func.count()).select_from(model).where(model.id.in_(ids))) == 0,
                  "Fixture collision; refusing writes or cleanup")
        check(db.scalar(select(func.count()).select_from(m.User).where(
            m.User.email.in_(state["emails"]))) == 0, "Fixture email collision")


def validate_ownership(db, state):
    for uid, email in zip(state["users"], state["emails"]):
        user = db.get(m.User, uid)
        check(user is None or (user.email == email and user.name == uid
                              and user.password_hash == "not-a-login-password"),
              "Refusing cleanup: unexpected user ownership")
    for eid in state["enterprises"]:
        row = db.get(m.Enterprise, eid)
        check(row is None or (row.name == eid and row.credit_code == eid),
              "Refusing cleanup: unexpected enterprise ownership")
    product = db.get(m.Product, state["product"])
    check(product is None or (product.enterprise_id == state["enterprises"][1]
          and product.name == state["product"] and product.product_type == "saas"
          and product.delivery_method == "tenant_access"), "Unexpected product ownership")
    versions = db.scalars(select(m.SaaSProductVersion).where(
        or_(m.SaaSProductVersion.product_id == state["product"],
            m.SaaSProductVersion.id == state["version"]))).all()
    check(all(v.id == state["version"] and v.product_id == state["product"] for v in versions),
          "Unexpected version ownership")
    subscriptions = db.scalars(select(m.SaaSSubscription).where(
        or_(m.SaaSSubscription.product_id == state["product"],
            m.SaaSSubscription.id.in_(state["historical"])))).all()
    check(all(s.product_id == state["product"] and s.version_id == state["version"]
              and s.enterprise_id in state["enterprises"] for s in subscriptions),
          "Unexpected subscription ownership")
    ids = {s.id for s in subscriptions}
    bindings = db.scalars(select(m.SaaSCombinationBinding).where(
        or_(m.SaaSCombinationBinding.product_id == state["product"],
            m.SaaSCombinationBinding.subscription_id.in_(ids)))).all()
    check(all(b.product_id == state["product"] and b.enterprise_id == state["enterprises"][0]
              and b.subscription_id in ids for b in bindings), "Unexpected binding ownership")


def scopes(db, state):
    predicates = helper.scopes(db, state)

    def scope(model, predicate):
        table = getattr(model, "__table__", model)
        predicates[table.name] = predicate
        return list(db.scalars(select(table.c.id).where(predicate))) if "id" in table.c else []

    binding_ids = scope(m.SaaSCombinationBinding, m.SaaSCombinationBinding.product_id == state["product"])
    subscription_ids = scope(m.SaaSSubscription, m.SaaSSubscription.product_id == state["product"])
    scope(m.SaaSProductVersion, m.SaaSProductVersion.id == state["version"])
    for model in (m.SaaSOperation, m.SaaSDepartment, m.SaaSUserMapping):
        scope(model, model.subscription_id.in_(subscription_ids))
    order_ids = list(db.scalars(select(m.Order.id).where(m.Order.product_id == state["product"])))
    scope(m.DeliveryTask, m.DeliveryTask.order_id.in_(order_ids))
    targets = binding_ids + subscription_ids
    predicates[m.AuditLog.__tablename__] = or_(predicates[m.AuditLog.__tablename__],
                                              m.AuditLog.target_id.in_(targets))
    center = m.message_center
    messages = scope(center["Message"], or_(predicates[center["Message"].__tablename__],
                                           center["Message"].target_id.in_(targets)))
    for key in ("Receipt", "Outbox", "ExpiredUrgent"):
        model = center[key]
        scope(model, or_(predicates[model.__tablename__], model.message_id.in_(messages)))
    deliveries = list(db.scalars(select(center["Outbox"].id).where(
        predicates[center["Outbox"].__tablename__])))
    scope(center["DeliveryAttempt"], center["DeliveryAttempt"].delivery_id.in_(deliveries))
    links = m.Base.metadata.tables["notification_message_attachments"]
    predicates[links.name] = links.c.message_id.in_(messages)
    return predicates


def snapshot(state):
    with m.SessionLocal() as db:
        result = {}
        for name, predicate in scopes(db, state).items():
            table = m.Base.metadata.tables[name]
            columns = list(table.primary_key.columns) if name.startswith("notification_") else list(table.c)
            result[name] = sorted((tuple(row) for row in db.execute(select(*columns).where(predicate))), key=repr)
        return result


def cleanup(state):
    validate_state(state)
    with m.SessionLocal() as db:
        db.execute(text("SET LOCAL statement_timeout = '30s'"))
        validate_ownership(db, state)
        predicates = scopes(db, state)
        Outbox = m.message_center["Outbox"]
        db.scalars(select(Outbox).where(predicates[Outbox.__tablename__])
                   .order_by(Outbox.id).with_for_update()).all()
        predicates = scopes(db, state)
        owned_keys = {}
        for name, predicate in predicates.items():
            table = m.Base.metadata.tables[name]
            for column in table.primary_key.columns:
                owned_keys[column] = list(db.scalars(select(column).where(predicate)))
        for table in reversed(m.Base.metadata.sorted_tables):
            if table.name in predicates:
                db.execute(delete(table).where(predicates[table.name]))
        db.commit()
    with m.SessionLocal() as db:
        for name, predicate in predicates.items():
            check(db.scalar(select(func.count()).select_from(m.Base.metadata.tables[name])
                            .where(predicate)) == 0, f"Fixture remains in {name}")
        for table in m.Base.metadata.tables.values():
            for fk in table.foreign_keys:
                values = owned_keys.get(fk.column, [])
                if values:
                    check(db.scalar(select(func.count()).select_from(table).where(fk.parent.in_(values))) == 0,
                          f"Fixture FK remains: {table.name}.{fk.parent.name}")


def prepare(state):
    with m.SessionLocal() as db:
        users = [m.User(id=uid, email=email, name=uid, password_hash="not-a-login-password",
                        verified_status="verified", activation_status="active", is_active=True)
                 for uid, email in zip(state["users"], state["emails"])]
        db.add_all(users + [m.Enterprise(id=eid, name=eid, credit_code=eid,
                                       verification_status="verified") for eid in state["enterprises"]])
        db.flush()
        for index, user in enumerate(users):
            db.add(m.Membership(id=state["memberships"][index], user_id=user.id,
                                enterprise_id=state["enterprises"][index], role="super_admin",
                                status="active", business_roles="provider,user"))
            db.add(m.message_center["Preference"](user_id=user.id, email_enabled=False))
        db.add(m.Product(id=state["product"], enterprise_id=state["enterprises"][1],
                         name=state["product"], provider_name=state["enterprises"][1],
                         product_type="saas", delivery_method="tenant_access", status="published",
                         application_url="https://fixture.example.invalid/",
                         integration_api_url="https://fixture.example.invalid/isv.php",
                         settlement_rule_mode="custom", settlement_rule_json=json.dumps(dict(
                             platform_rate=20, provider_rate=80, service_rate=0, expert_rate=0, channel_rate=0))))
        db.flush()
        db.add(m.SaaSProductVersion(id=state["version"], product_id=state["product"],
                                   version_code="v1", name="V1", monthly_price=100, cost=20, status="active"))
        db.commit()
        return m.issue_token(users[0])


@contextmanager
def catalog(state):
    original = m.storefront["versions_for"]

    def versions_for(db, product):
        return ([{"id": state["version"]}] if product.id == state["product"]
                else original(db, product))

    with patch.dict(m.storefront, {"versions_for": versions_for}):
        yield


@contextmanager
def observe_saas_calls():
    # Observe the real function without replacing its behavior or HTTP hooks.
    code, calls = m.saas_call.__code__, []
    previous, previous_threads = sys.getprofile(), threading.getprofile()

    def profile(frame, event_name, arg):
        if event_name == "call" and frame.f_code is code:
            calls.append(True)

    check(previous is None and previous_threads is None, "Dedicated process requires no active profiler")
    sys.setprofile(profile)
    threading.setprofile(profile)
    try:
        yield calls
    finally:
        sys.setprofile(previous)
        threading.setprofile(previous_threads)
        check(not calls, "Unexpected real saas_call during unpaid order reservation")


def post_order(state, token, index, target="", expected=200):
    # Deliberately no context manager: TestClient lifespan must never run.
    client = TestClient(m.app, raise_server_exceptions=False)
    try:
        response = client.post("/api/orders", headers={"Authorization": "Bearer " + token,
            "X-Request-Id": state["requests"][index]}, json={"product_id": state["product"],
            "product_version_id": state["version"], "buyer_enterprise_id": state["enterprises"][0],
            "subscription_months": index % 2 + 2, "saas_subscription_id": target})
        check(response.status_code == expected,
              f"POST /api/orders returned {response.status_code}, expected {expected}")
        return response.json()
    finally:
        client.close()


def lock_targets(statement, parameters, request_id, state, table="products"):
    check(table in {"products", "enterprises"}, "Invalid lock table")
    values = parameters.values() if isinstance(parameters, dict) else parameters or ()
    owned_id = state["product"] if table == "products" else state["enterprises"][0]
    return (request_id in state["requests"][:2] and owned_id in values
            and "FOR UPDATE" in statement.upper()
            and re.search(r"\bFROM " + table + r"\b", statement) is not None)


def first_purchase_race(state, token):
    pids, pid_lock, captured, enterprise_requests = [], threading.Lock(), {}, set()

    def capture(conn, cursor, statement, parameters, context, executemany):
        request_id = m.audit_request_id.get()
        if lock_targets(statement, parameters, request_id, state, "enterprises"):
            with pid_lock:
                enterprise_requests.add(request_id)
        if lock_targets(statement, parameters, request_id, state):
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute("SELECT pg_backend_pid()")
            pid = cursor.fetchone()[0]
            with pid_lock:
                if request_id not in captured:
                    captured[request_id] = pid
                    pids.append(pid)

    def worker(index, barrier):
        barrier.wait(timeout=15)
        return post_order(state, token, index)

    model = m.Product if state["scenario"] == "product" else m.Enterprise
    owned_id = state["product"] if model is m.Product else state["enterprises"][0]
    event.listen(m.engine, "before_cursor_execute", capture)
    try:
        responses, workers = helper.race(model, owned_id, worker, pids, pid_lock)
    finally:
        event.remove(m.engine, "before_cursor_execute", capture)
    check(len(set(workers)) == 2 and len(captured) == 2, "Expected two independent HTTP DB sessions")
    check(enterprise_requests == set(state["requests"][:2]), "Both HTTP requests must reach enterprise lock")
    verify_orders(state, responses, subscriptions=1)
    return {"http_statuses": [200, 200], "worker_pids": workers,
            "two_blocked_sessions_observed": True,
            "gate": model.__tablename__, "proof": "pg_blocking_pids: two distinct blocked sessions",
            "orders": 2, "unpaid": True, "bindings": 1, "subscriptions": 1,
            "subscription_id": responses[0]["subscription_id"]}


def verify_orders(state, responses, subscriptions):
    check(len(responses) == 2 and responses[0]["id"] != responses[1]["id"]
          and responses[0]["subscription_id"] == responses[1]["subscription_id"],
          "Expected two distinct orders sharing one subscription")
    with m.SessionLocal() as db:
        orders = db.scalars(select(m.Order).where(m.Order.product_id == state["product"])).all()
        subs = db.scalars(select(m.SaaSSubscription).where(m.SaaSSubscription.product_id == state["product"])).all()
        bindings = db.scalars(select(m.SaaSCombinationBinding).where(
            m.SaaSCombinationBinding.product_id == state["product"])).all()
        check(len(orders) == 2 and {o.id for o in orders} == {r["id"] for r in responses}, "Wrong order count")
        check(len(subs) == subscriptions and len(bindings) == 1
              and bindings[0].subscription_id == responses[0]["subscription_id"]
              and bindings[0].enterprise_id == state["enterprises"][0], "Wrong binding/subscription count or target")
        target = db.get(m.SaaSSubscription, bindings[0].subscription_id)
        if subscriptions == 1:
            check(target.status == "provisioning" and target.starts_at is None and target.expires_at is None
                  and target.external_tenant_id == "" and target.external_app_id == "", "Tenant was provisioned")
        for order in orders:
            check(order.subscription_id == target.id and order.payment_status == "unpaid"
                  and order.paid_amount == 0 and order.delivery_status == "not_started"
                  and order.product_version_id == state["version"] and order.snapshot_version == 1,
                  "Order must remain unpaid and unprovisioned")
            payments = db.scalars(select(m.Payment).where(m.Payment.order_id == order.id)).all()
            check(len(payments) == 1 and payments[0].status == "unpaid"
                  and payments[0].paid_at is None and payments[0].amount == order.amount, "Wrong unpaid payment")
        for model in (m.SaaSOperation, m.SaaSDepartment, m.SaaSUserMapping):
            check(db.scalar(select(func.count()).select_from(model).where(
                model.subscription_id.in_([s.id for s in subs]))) == 0, "Unexpected SaaS provisioning children")
        for model in (m.DeliveryTask, m.SubscriptionTerm, m.SettlementMeasurement):
            check(db.scalar(select(func.count()).select_from(model).where(
                model.order_id.in_([o.id for o in orders]))) == 0, "Unexpected delivery/grant/metering")
        check(db.scalar(select(func.count()).select_from(m.AuditLog).where(
            m.AuditLog.target_id == bindings[0].id, m.AuditLog.action == "bind_saas_combination")) == 1,
            "Expected one binding-target audit")


def history_checks(state, token):
    with m.SessionLocal() as db:
        for index, sid in enumerate(state["historical"]):
            db.add(m.SaaSSubscription(id=sid, enterprise_id=state["enterprises"][index == 2],
                product_id=state["product"], version_id=state["version"], billing_cycle="monthly",
                status="active", external_tenant_id=sid, created_by=state["users"][0]))
        db.commit()
    with m.SessionLocal() as db:
        originals = [tuple(db.execute(select(m.SaaSSubscription.__table__).where(
            m.SaaSSubscription.id == sid)).one()) for sid in state["historical"]]
    for index, target in ((0, ""), (1, state["historical"][2])):
        before = snapshot(state)
        post_order(state, token, index, target, expected=409)
        check(snapshot(state) == before, "Rejected history/cross-enterprise request wrote business rows")
    chosen = post_order(state, token, 2, state["historical"][1])
    check(chosen["subscription_id"] == state["historical"][1], "Explicit selection ignored")
    before = snapshot(state)
    post_order(state, token, 3, state["historical"][0], expected=409)
    check(snapshot(state) == before, "Bound tenant switch wrote business rows")
    repeated = post_order(state, token, 4)
    verify_orders(state, [chosen, repeated], subscriptions=3)
    with m.SessionLocal() as db:
        current = [tuple(db.execute(select(m.SaaSSubscription.__table__).where(
            m.SaaSSubscription.id == sid)).one()) for sid in state["historical"]]
    check(current == originals, "Historical tenants were changed or merged")
    return {"multiple_history_requires_selection": True, "explicit_target_reused": True,
            "cross_enterprise_rejected": True, "bound_switch_rejected": True,
            "historical_tenants_unchanged": True, "http_statuses": [409, 409, 200, 409, 200]}


def execute_fixture(scenario):
    state = new_state(scenario)
    check_collisions(state)
    manifest = persist_manifest(state)
    print(json.dumps({"scenario": scenario, "recovery_manifest": manifest}), flush=True)
    try:
        with catalog(state), observe_saas_calls() as calls:
            token = prepare(state)
            result = (history_checks(state, token) if scenario == "history"
                      else first_purchase_race(state, token))
        result["saas_call_count"] = len(calls)
    finally:
        cleanup(state)
        os.unlink(manifest)
    result["cleanup"] = "verified no owned rows or FK references remain"
    return result


def run(scenario="all"):
    check(m.engine.dialect.name == "postgresql", "Real PostgreSQL DATABASE_URL required")
    check(not m.app.dependency_overrides, "Normal HTTP dependencies required")
    check(scenario == "all" or scenario in SCENARIOS, "Invalid scenario")
    selected = SCENARIOS if scenario == "all" else (scenario,)
    results = {name: execute_fixture(name) for name in selected}
    races = [value for name, value in results.items() if name != "history"]
    return {"status": "passed", "database": "postgresql",
            "two_blocked_sessions_observed": bool(races) and all(
                value["two_blocked_sessions_observed"] for value in races),
            "cleanup": "verified", "scenarios": results}


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--scenario", choices=("all", *SCENARIOS), default="all")
    group.add_argument("--recover", metavar="MANIFEST")
    args = parser.parse_args()
    load_backend()
    if args.recover:
        check(m.engine.dialect.name == "postgresql", "Real PostgreSQL required")
        cleanup(read_manifest(args.recover))
        os.unlink(args.recover)
        result = {"status": "passed", "database": "postgresql",
                  "two_blocked_sessions_observed": False, "cleanup": "verified"}
    else:
        result = run(args.scenario)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    cli()
