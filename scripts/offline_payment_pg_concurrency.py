"""TRD-QA-004 real PostgreSQL HTTP contention, disposable offline fixtures.

PYTHONPATH=backend .venv/bin/python scripts/offline_payment_pg_concurrency.py
Recovery: same environment/DB, append --recover /tmp/offline-payment-....json

No lifespan, migrations, dependency overrides, storage or external payments.
Catalog visibility alone is supplied for the exact owned product. Normal HTTP
authentication, sessions, payment, review, fulfillment and event hooks stay real.
Each race uses helper.race's pg_blocking_pids proof before releasing an Order
gate. Run in a dedicated process. Manifests contain IDs only, never credentials.
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
import tempfile
from threading import Lock
import time
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, or_, select, text


m = helper = None
METHODS = ("training", "consulting", "custom")


def check(condition, detail):
    if not condition:
        raise AssertionError(detail)


def load_backend():
    global m, helper
    from app import main
    m = main
    spec = importlib.util.spec_from_file_location(
        "offline_subscription_helper", Path(__file__).with_name("subscription_pg_concurrency.py"))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)


def new_state(method):
    prefix = "qa-off-pay-pg-" + secrets.token_hex(8)
    return {"prefix": prefix, "method": method, "product": prefix + "-p",
            "version": prefix + "-v", "users": [prefix + "-ub", prefix + "-up"],
            "enterprises": [prefix + "-eb", prefix + "-ep"],
            "emails": [prefix + "-buyer@example.invalid", prefix + "-provider@example.invalid"],
            "requests": [prefix + f"-http{i}" for i in range(80)]}


def validate_state(state):
    check(isinstance(state, dict), "Invalid manifest")
    check(re.fullmatch(r"qa-off-pay-pg-[0-9a-f]{16}", state.get("prefix", "")),
          "Invalid fixture marker")
    expected = new_state(state.get("method"))
    expected = {key: (value.replace(expected["prefix"], state["prefix"])
                     if isinstance(value, str) else
                     [v.replace(expected["prefix"], state["prefix"]) for v in value])
                for key, value in expected.items()}
    check(state == expected and state["method"] in METHODS, "Refusing unowned manifest IDs")


def persist_manifest(state):
    validate_state(state)
    fd, path = tempfile.mkstemp(prefix="offline-payment-", suffix=".json", dir="/tmp")
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


def scopes(db, state):
    predicates = helper.scopes(db, state)
    orders = list(db.scalars(select(m.Order.id).where(m.Order.product_id == state["product"])))
    predicates[m.DeliveryTask.__tablename__] = m.DeliveryTask.order_id.in_(orders)
    return predicates


def snapshot(state):
    # Attempts/status/timestamps can advance in external outbox workers. Message,
    # recipient and delivery identities remain stable and still detect duplicates.
    volatile = {"notification_deliveries", "notification_recipients",
                "notification_delivery_attempts", "notification_expired_urgent"}
    with m.SessionLocal() as db:
        result = {}
        for name, predicate in scopes(db, state).items():
            if name in {"notification_delivery_attempts", "notification_expired_urgent"}:
                continue
            table = m.Base.metadata.tables[name]
            columns = list(table.primary_key.columns) if name in volatile else list(table.c)
            result[name] = sorted((tuple(row) for row in db.execute(
                select(*columns).where(predicate))), key=repr)
        return result


def cleanup(state):
    validate_state(state)
    with m.SessionLocal() as db:
        db.execute(text("SET LOCAL statement_timeout = '30s'"))
        validate_ownership(db, state)
        # Lock every scoped parent, including message-target deliveries, before
        # discovering children that live workers may insert.
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
                    check(db.scalar(select(func.count()).select_from(table)
                                    .where(fk.parent.in_(values))) == 0,
                          f"Fixture FK remains: {table.name}.{fk.parent.name}")


def validate_ownership(db, state):
    for uid, email in zip(state["users"], state["emails"]):
        user = db.get(m.User, uid)
        check(user is None or (user.email == email and user.name == uid
                              and user.password_hash == "not-a-login-password"),
              "Refusing cleanup: unexpected user ownership")
    for eid in state["enterprises"]:
        enterprise = db.get(m.Enterprise, eid)
        check(enterprise is None or (enterprise.name == eid and enterprise.credit_code == eid),
              "Refusing cleanup: unexpected enterprise ownership")
    product = db.get(m.Product, state["product"])
    check(product is None or (product.enterprise_id == state["enterprises"][1]
                              and product.name == state["product"]
                              and product.delivery_method == state["method"]),
          "Refusing cleanup: unexpected product ownership")
    version = db.get(m.ProductReleaseVersion, state["version"])
    check(version is None or version.product_id == state["product"],
          "Refusing cleanup: unexpected version ownership")


def check_collisions(state):
    with m.SessionLocal() as db:
        for model, ids in ((m.User, state["users"]), (m.Enterprise, state["enterprises"]),
                           (m.Product, [state["product"]]),
                           (m.ProductReleaseVersion, [state["version"]])):
            check(db.scalar(select(func.count()).select_from(model).where(model.id.in_(ids))) == 0,
                  "Fixture collision; refusing writes or cleanup")


def prepare(state):
    with m.SessionLocal() as db:
        users = [m.User(id=uid, email=email, name=uid, password_hash="not-a-login-password",
                        verified_status="verified", activation_status="active", is_active=True)
                 for uid, email in zip(state["users"], state["emails"])]
        enterprises = [m.Enterprise(id=eid, name=eid, credit_code=eid,
                                    verification_status="verified") for eid in state["enterprises"]]
        db.add_all(users + enterprises)
        db.flush()
        for user, enterprise in zip(users, enterprises):
            db.add(m.Membership(user_id=user.id, enterprise_id=enterprise.id,
                                role="super_admin", status="active", business_roles="provider,user"))
            db.add(m.message_center["Preference"](user_id=user.id, email_enabled=False))
        db.add(m.Product(id=state["product"], enterprise_id=state["enterprises"][1],
                         name=state["product"], provider_name=state["enterprises"][1],
                         product_type=state["method"], delivery_method=state["method"],
                         status="published", settlement_rule_mode="custom",
                         settlement_rule_json=json.dumps(dict(platform_rate=20, provider_rate=80,
                             service_rate=0, expert_rate=0, channel_rate=0))))
        db.flush()
        db.add(m.ProductReleaseVersion(id=state["version"], product_id=state["product"],
                                       version_code="v1", price=100, cost=50, status="active"))
        db.commit()
        return [m.issue_token(user) for user in users]


class HTTP:
    def __init__(self, state, tokens):
        self.state, self.tokens, self.index = state, tokens, 0
        self.lock = Lock()

    def request(self, path, body=None, actor=0, expected=200, request_id=None, method="POST"):
        if request_id is None:
            with self.lock:
                request_id = self.state["requests"][self.index]
                self.index += 1
        client = TestClient(m.app, raise_server_exceptions=False)
        try:
            response = client.request(method, path, headers={
                "Authorization": "Bearer " + self.tokens[actor], "X-Request-Id": request_id},
                **({"json": body} if body is not None else {}))
            if expected is not None:
                check(response.status_code == expected,
                      f"Owned HTTP request returned {response.status_code}, expected {expected}")
            return response
        finally:
            client.close()

    def create(self):
        body = {"product_id": self.state["product"], "product_version_id": self.state["version"],
                "buyer_enterprise_id": self.state["enterprises"][0], "subscription_months": 1}
        quote = self.request("/api/orders/quote", body).json()
        order = self.request("/api/orders", {**body, "quote_id": quote["quote_id"]}).json()
        check(order["main_status"] == "pending_provider_review", "Wrong initial state")
        return order["id"]


def review_body(amount=120):
    return {"decision": "approve", "amount": amount, "cost": 60, "reason": "Isolated revised scope"}


def lock_targets(statement, parameters, request_id, own_order, requests):
    values = parameters.values() if isinstance(parameters, dict) else parameters or ()
    return (request_id in requests and own_order in values
            and "FOR UPDATE" in statement.upper()
            and re.search(r"\bFROM orders\b", statement) is not None)


def http_race(http, oid, calls, ordered=False):
    pids, pid_lock = [], Lock()
    with http.lock:
        requests = http.state["requests"][http.index:http.index + 2]
        http.index += 2
    captured = {}

    def capture(conn, cursor, statement, parameters, context, executemany):
        request_id = m.audit_request_id.get()
        if lock_targets(statement, parameters, request_id, oid, requests):
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute("SELECT pg_backend_pid()")
            pid = cursor.fetchone()[0]
            with pid_lock:
                if request_id not in captured:
                    captured[request_id] = pid
                    pids.append(pid)

    def worker(index, barrier):
        barrier.wait(timeout=15)
        if ordered and index == 1:
            deadline = time.monotonic() + 12
            with m.SessionLocal() as observer:
                while time.monotonic() < deadline:
                    with pid_lock:
                        first = captured.get(requests[0])
                    if first and observer.scalar(text("SELECT cardinality(pg_blocking_pids(:pid))"),
                                                 {"pid": first}) > 0:
                        break
                    time.sleep(0.025)
                else:
                    raise AssertionError("First HTTP worker never blocked on owned gate")
        path, body, actor = calls[index]
        return http.request(path, body, actor, expected=None, request_id=requests[index]).status_code

    event.listen(m.engine, "before_cursor_execute", capture)
    try:
        statuses, workers = helper.race(m.Order, oid, worker, pids, pid_lock)
    finally:
        event.remove(m.engine, "before_cursor_execute", capture)
    return {"http_statuses": statuses, "worker_pids": workers,
            "two_blocked_sessions_observed": True}


def verify_order(state, oid, paid, amount=120, review_count=1):
    with m.SessionLocal() as db:
        order = db.get(m.Order, oid)
        check(order.product_id == state["product"], "Order ownership mismatch")
        check(order.amount == amount and order.total_cost_snapshot == 60
              and order.payment_status == ("paid" if paid else "unpaid")
              and order.paid_amount == (amount if paid else 0)
              and order.main_status == ("awaiting_start" if paid else "pending_payment")
              and order.delivery_status == ("awaiting_start" if paid else "not_started"),
              "Inconsistent order economics/state")
        m.order_economics["economic_snapshot"](order)
        m.order_economics["delivery_snapshot"](order)
        payments = db.scalars(select(m.Payment).where(m.Payment.order_id == oid)).all()
        tasks = db.scalars(select(m.DeliveryTask).where(m.DeliveryTask.order_id == oid)).all()
        check(len(payments) == 1 and payments[0].amount == amount
              and payments[0].status == ("paid" if paid else "unpaid")
              and (payments[0].paid_at is not None) == paid, "Duplicate/inconsistent Payment")
        check(len(tasks) == int(paid), "Duplicate/unexpected DeliveryTask")
        if paid:
            check((tasks[0].method, tasks[0].delivery_mode, tasks[0].status) ==
                  (state["method"], "manual", "awaiting_start"), "Wrong manual delivery task")
        for action, count in (("provider_review_approve", review_count),
                              ("confirm_payment", int(paid)),
                              ("create_delivery_task", int(paid)),
                              ("payment_fulfillment_ready", int(paid))):
            for model in (m.AuditLog, m.OrderStateLog):
                if model is m.OrderStateLog and action == "create_delivery_task":
                    continue
                owner = (or_(model.order_id == oid, model.target_id == oid)
                         if model is m.AuditLog else model.order_id == oid)
                actual = db.scalar(select(func.count()).select_from(model).where(
                    owner, model.action == action))
                # fulfillment normalization logs both main and delivery domains.
                expected = count * (2 if model is m.OrderStateLog
                                    and action == "payment_fulfillment_ready" else 1)
                check(actual == expected, f"Duplicate/missing {action} in {model.__tablename__}")
        check(db.scalar(select(func.count()).select_from(m.OrderStateLog).where(
            m.OrderStateLog.order_id == oid)) == (6 if paid else 2),
            "Unexpected/duplicate state transitions")
        measurements = db.scalars(select(m.SettlementMeasurement).where(
            m.SettlementMeasurement.order_id == oid)).all()
        check(len(measurements) == int(paid), "Duplicate/unexpected metering")
        if paid:
            check(measurements[0].measurement_type == "payment"
                  and measurements[0].source_id == payments[0].id
                  and measurements[0].quantity == amount, "Wrong paid metering")
        Message, Receipt, Outbox = (m.message_center[key] for key in ("Message", "Receipt", "Outbox"))
        messages = db.scalars(select(Message).where(Message.target_id == oid)).all()
        keys = {f"business:order:{oid}:provider_review_approve"}
        if paid:
            keys.add(f"business:order:{oid}:confirm_payment:paid")
        check(len(messages) == len(keys) and {msg.event_key for msg in messages} == keys,
              "Duplicate/missing notifications")
        for msg in messages:
            recipients = state["users"] if "confirm_payment" in msg.event_key else state["users"][:1]
            receipts = db.scalars(select(Receipt).where(Receipt.message_id == msg.id)).all()
            check(len(receipts) == len(recipients)
                  and {row.user_id for row in receipts} == set(recipients), "Duplicate/wrong recipients")
            deliveries = db.scalars(select(Outbox).where(Outbox.message_id == msg.id)).all()
            identities = [(row.user_id, row.channel) for row in deliveries]
            check(len(identities) == len(set(identities))
                  and set(identities) == {(uid, channel) for uid in recipients
                                          for channel in ("email", "realtime")},
                  "Duplicate/missing/wrong outbox")


@contextmanager
def fail_notification_once(oid, request_id):
    original = m.notify_order_event
    failures = []

    def notify(db, order, action, version, **kwargs):
        original(db, order, action, version, **kwargs)
        if (order.id == oid and action == "confirm_payment"
                and m.audit_request_id.get() == request_id and not failures):
            db.flush()
            failures.append(oid)
            raise RuntimeError("Injected owned notification failure")

    with patch.object(m, "notify_order_event", notify):
        yield failures


def exercise(state, http):
    results = {}
    oid = http.create()
    review = f"/api/orders/{oid}/provider-review"
    pay = f"/api/orders/{oid}/transition"
    http.request(review, review_body(), actor=1)
    results["duplicate_payment"] = http_race(http, oid, [(pay, {"action": "confirm_payment"}, 0)] * 2)
    check(results["duplicate_payment"]["http_statuses"] == [200, 200], "Duplicate payment failed")
    verify_order(state, oid, True)
    before = snapshot(state)
    http.request(pay, {"action": "confirm_payment"})
    check(snapshot(state) == before, "Paid retry wrote duplicate events/state")
    for first in ("review", "payment"):
        oid = http.create()
        review = (f"/api/orders/{oid}/provider-review", review_body(), 1)
        pay = (f"/api/orders/{oid}/transition", {"action": "confirm_payment"}, 0)
        calls = [review, pay] if first == "review" else [pay, review]
        result = http_race(http, oid, calls, ordered=True)
        check(result["http_statuses"] == ([200, 200] if first == "review" else [409, 200]),
              "Review/payment ordering inconsistent")
        verify_order(state, oid, first == "review")
        if first == "payment":
            http.request(pay[0], pay[1])
            verify_order(state, oid, True)
        results[first + "_first"] = result
    oid = http.create()
    path = f"/api/orders/{oid}/provider-review"
    quote = http.request(f"/api/orders/{oid}/provider-quote", actor=1, method="GET").json()
    bodies = [{**review_body(amount), "expected_updated_at": quote["expected_updated_at"]}
              for amount in (120, 130)]
    result = http_race(http, oid, [(path, body, 1) for body in bodies])
    check(sorted(result["http_statuses"]) == [200, 409], "Optimistic reviews did not conflict")
    winner = result["http_statuses"].index(200)
    verify_order(state, oid, False, bodies[winner]["amount"])
    results["optimistic_reviews"] = result
    oid = http.create()
    http.request(f"/api/orders/{oid}/provider-review", review_body(), actor=1)
    before = snapshot(state)
    with http.lock:
        request_id = state["requests"][http.index]
        http.index += 1
    pay = f"/api/orders/{oid}/transition"
    with fail_notification_once(oid, request_id) as failures:
        http.request(pay, {"action": "confirm_payment"}, expected=500, request_id=request_id)
    check(failures == [oid], "Owned rollback injection did not run exactly once")
    check(snapshot(state) == before, "Notification failure leaked committed rows/state")
    http.request(pay, {"action": "confirm_payment"})
    verify_order(state, oid, True)
    results["notification_failure_rollback_retry"] = True
    return results


def run():
    check(m.engine.dialect.name == "postgresql", "Real PostgreSQL DATABASE_URL required")
    check(not m.app.dependency_overrides, "Normal HTTP dependencies required")
    results = {}
    for method in METHODS:
        state = new_state(method)
        check_collisions(state)
        manifest = persist_manifest(state)
        print(json.dumps({"recovery_manifest": manifest, "method": method}), flush=True)
        original = m.storefront["versions_for"]

        def versions_for(db, product):
            return ([{"id": state["version"]}] if product.id == state["product"]
                    else original(db, product))

        try:
            with patch.dict(m.storefront, {"versions_for": versions_for}):
                tokens = prepare(state)
                results[method] = exercise(state, HTTP(state, tokens))
        finally:
            cleanup(state)
            os.unlink(manifest)
        results[method]["cleanup"] = "verified no owned rows or FK references remain"
    return {"status": "passed", "database": "postgresql", "methods": results,
            "scope": "HTTP concurrency/control plane; simulated manual payments; catalog/storage excluded"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recover", metavar="MANIFEST")
    args = parser.parse_args()
    load_backend()
    if args.recover:
        check(m.engine.dialect.name == "postgresql", "Real PostgreSQL required")
        cleanup(read_manifest(args.recover))
        os.unlink(args.recover)
        print(json.dumps({"cleanup": "verified"}))
    else:
        print(json.dumps(run(), indent=2))
