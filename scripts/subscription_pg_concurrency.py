"""Real PostgreSQL subscription contention verification with disposable fixtures.

Run in the backend environment against an already migrated database:
  PYTHONPATH=backend DATABASE_URL=postgresql+psycopg://... \
    python scripts/subscription_pg_concurrency.py

This tests the trusted verified-caller hook layer, NOT delivery verification or
real APISIX health/provisioning. Only isolated orders are manually marked paid
and delivery-successful. The installed main.subscription_terms hooks, quoting,
audits, metering, notifications, and HTTP renewal route remain real. Catalog
visibility alone is patched for this run's product; other products delegate to
the original function. TestClient uses normal authentication and SessionLocal
dependencies without lifespan startup (which could seed/migrate shared data).
No deployment, schema creation, or existing fixture/product updates occur.

Each race holds an owned row until pg_blocking_pids proves BOTH independent
connections are waiting. Releasing it lets the production locks serialize the
transactions. Cleanup runs even on assertion failure, in FK dependency order.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import secrets
from threading import Barrier, Lock
import time
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, or_, select, text

from app import main as m
from app.subscription_calendar import CalendarAnchor


def check(condition, detail):
    if not condition:
        raise AssertionError(detail)


def scopes(db, state):
    """Resolve generated IDs through exact owned parents, never prefix deletion."""
    tables = m.Base.metadata.tables
    predicates = {}

    def scope(model, predicate):
        table = getattr(model, "__table__", model)
        predicates[table.name] = predicate
        return list(db.scalars(select(table.c.id).where(predicate))) if "id" in table.c else []

    orders = db.scalars(select(m.Order).where(m.Order.product_id == state["product"])).all()
    check(all(o.buyer_user_id == state["users"][0]
              and o.buyer_enterprise_id == state["enterprises"][0]
              and o.provider_enterprise_id == state["enterprises"][1] for o in orders),
          "Refusing cleanup: unexpected order ownership")
    order_ids = scope(m.Order, m.Order.id.in_([o.id for o in orders]))
    combo_ids = scope(m.SubscriptionCombination,
                      m.SubscriptionCombination.product_id == state["product"])
    term_ids = scope(m.SubscriptionTerm, m.SubscriptionTerm.order_id.in_(order_ids))
    scope(m.SubscriptionRenewal, m.SubscriptionRenewal.combination_id.in_(combo_ids))
    targets = order_ids + combo_ids + term_ids + [state["product"], state["version"]]
    targets += state["users"] + state["enterprises"]
    for model in (m.Payment, m.OrderStateLog, m.SettlementMeasurement):
        targets += scope(model, model.order_id.in_(order_ids))
    scope(m.SettlementMeasurementOrder, m.SettlementMeasurementOrder.order_id.in_(order_ids))
    center = m.message_center
    message_ids = scope(center["Message"], center["Message"].target_id.in_(targets))
    receipt_ids = scope(center["Receipt"], or_(center["Receipt"].message_id.in_(message_ids),
                                               center["Receipt"].user_id.in_(state["users"])))
    delivery_ids = scope(center["Outbox"], or_(center["Outbox"].message_id.in_(message_ids),
                                                center["Outbox"].user_id.in_(state["users"])))
    scope(center["DeliveryAttempt"], center["DeliveryAttempt"].delivery_id.in_(delivery_ids))
    scope(center["ExpiredUrgent"], or_(center["ExpiredUrgent"].message_id.in_(message_ids),
                                       center["ExpiredUrgent"].user_id.in_(state["users"])))
    links = tables["notification_message_attachments"]
    scope(links, links.c.message_id.in_(message_ids))
    scope(center["Preference"], center["Preference"].user_id.in_(state["users"]))
    scope(m.ProductReleaseVersion, m.ProductReleaseVersion.id == state["version"])
    scope(m.Product, m.Product.id == state["product"])
    member_ids = scope(m.Membership, m.Membership.user_id.in_(state["users"]))
    scope(m.MembershipDepartment, m.MembershipDepartment.membership_id.in_(member_ids))
    scope(m.Enterprise, m.Enterprise.id.in_(state["enterprises"]))
    scope(m.User, m.User.id.in_(state["users"]))
    targets += message_ids + receipt_ids + delivery_ids + member_ids
    scope(m.AuditLog, or_(m.AuditLog.target_id.in_(targets),
                          m.AuditLog.order_id.in_(order_ids),
                          m.AuditLog.actor.in_(state["emails"]),
                          m.AuditLog.request_id.in_(state["requests"])))
    return predicates


def snapshot(state):
    """Business rows plus event identities; outbox worker progress is independent."""
    with m.SessionLocal() as db:
        predicates = scopes(db, state)
        result = {}
        for name, predicate in predicates.items():
            table = m.Base.metadata.tables[name]
            columns = list(table.c)
            if name in {"notification_deliveries", "notification_recipients",
                        "notification_delivery_attempts", "notification_expired_urgent"}:
                columns = list(table.primary_key.columns)
            rows = db.execute(select(*columns).where(predicate)).all()
            result[name] = sorted((tuple(row) for row in rows), key=repr)
        return result


def cleanup(state):
    check(state["prefix"].startswith("qa-sub-concurrency-"), "Invalid fixture marker")
    check(all(value.startswith(state["prefix"] + "-") for value in
              [state["product"], state["version"], *state["users"], *state["enterprises"]]),
          "Refusing cleanup outside owned targets")
    with m.SessionLocal() as db:
        # Live outbox workers can insert attempt children during cleanup.
        # Lock owned parents first, then discover children under those locks.
        Outbox = m.message_center["Outbox"]
        db.scalars(select(Outbox).where(Outbox.user_id.in_(state["users"]))
                   .order_by(Outbox.id).with_for_update()).all()
        predicates = scopes(db, state)
        # Remember all owned primary keys to detect unexpected FK leftovers too.
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
            table = m.Base.metadata.tables[name]
            check(db.scalar(select(func.count()).select_from(table).where(predicate)) == 0,
                  f"Fixture remains in {name}")
        for table in m.Base.metadata.tables.values():
            for fk in table.foreign_keys:
                values = owned_keys.get(fk.column, [])
                if values:
                    check(db.scalar(select(func.count()).select_from(table)
                                    .where(fk.parent.in_(values))) == 0,
                          f"Fixture FK remains: {table.name}.{fk.parent.name}")


def prepare(state):
    with m.SessionLocal() as db:
        users = [m.User(id=uid, email=email, name=state["prefix"] + "-" + role,
                        password_hash="not-a-login-password", verified_status="verified",
                        activation_status="active", is_active=True)
                 for uid, email, role in zip(state["users"], state["emails"], ("buyer", "provider"))]
        enterprises = [m.Enterprise(id=eid, name=eid, credit_code=eid,
                                     verification_status="verified") for eid in state["enterprises"]]
        db.add_all(users + enterprises)
        db.flush()
        for user, enterprise in zip(users, enterprises):
            db.add(m.Membership(user_id=user.id, enterprise_id=enterprise.id,
                                role="super_admin", status="active", business_roles="provider,user"))
            db.add(m.message_center["Preference"](user_id=user.id, email_enabled=False))
        db.add(m.Product(id=state["product"], enterprise_id=enterprises[1].id,
                         name=state["product"], provider_name=enterprises[1].name,
                         product_type="api", delivery_method="api", status="published",
                         settlement_rule_mode="custom", settlement_rule_json=json.dumps({
                             "platform_rate": 20, "provider_rate": 80, "service_rate": 0,
                             "expert_rate": 0, "channel_rate": 0})))
        db.flush()
        db.add(m.ProductReleaseVersion(id=state["version"], product_id=state["product"],
                                       version_code="v1", price=12, cost=7, status="active",
                                       **state["limits"]))
        db.flush()
        ids = []
        for months in (2, 3):
            body = m.OrderBody(product_id=state["product"], product_version_id=state["version"],
                               buyer_enterprise_id=enterprises[0].id, subscription_months=months)
            quote = m.order_economics["quote"](db, users[0], body, lock=True)
            order = m.create_quoted_order(db, users[0], quote)
            order.payment_status, order.paid_amount = "paid", order.amount
            order.main_status = order.delivery_status = "pending_acceptance"
            db.flush()
            payment = db.scalar(select(m.Payment).where(m.Payment.order_id == order.id))
            payment.status, payment.paid_at = "paid", state["anchor"]
            payment.confirmed_by = users[0].email
            m.order_economics["economic_snapshot"](order)
            m.order_economics["delivery_snapshot"](order)
            ids.append(order.id)
        db.commit()
        return ids, m.issue_token(users[0])


def race(model, owned_id, worker, pids, pid_lock):
    """Keep the gate locked until PostgreSQL reports two blocked worker PIDs."""
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        with m.SessionLocal() as gate:
            gate.execute(text("SET LOCAL statement_timeout = '30s'"))
            gate.scalar(select(model).where(model.id == owned_id).with_for_update())
            gate_pid = gate.scalar(text("SELECT pg_backend_pid()"))
            futures = [pool.submit(worker, index, barrier) for index in range(2)]
            try:
                deadline = time.monotonic() + 15
                with m.SessionLocal() as observer:
                    while time.monotonic() < deadline:
                        with pid_lock:
                            workers = list(pids)
                        if len(workers) == 2 and len(set(workers)) == 2:
                            waits = observer.execute(text(
                                "SELECT pid, pg_blocking_pids(pid) FROM pg_stat_activity "
                                "WHERE pid IN (:one, :two)"),
                                {"one": workers[0], "two": workers[1]}).all()
                            # PostgreSQL may queue the second waiter behind the first.
                            if len(waits) == 2 and all(blockers and
                                    set(blockers) <= {gate_pid, *workers} for _, blockers in waits):
                                break
                        check(not any(f.done() for f in futures),
                              "Worker finished before observed PostgreSQL contention")
                        time.sleep(0.05)
                    else:
                        raise AssertionError(f"Did not observe two blocked PostgreSQL sessions: {workers}")
            finally:
                gate.rollback()
        return [future.result(timeout=40) for future in futures], workers


def verify_terms(state, order_ids):
    hooks = m.subscription_terms
    with m.SessionLocal() as db:
        combos = db.scalars(select(m.SubscriptionCombination).where(
            m.SubscriptionCombination.product_id == state["product"])).all()
        check(len(combos) == 1, "Expected one serialized combination")
        combo = combos[0]
        terms = db.scalars(select(m.SubscriptionTerm).where(
            m.SubscriptionTerm.combination_id == combo.id).order_by(m.SubscriptionTerm.start_month)).all()
        check(len(terms) == 2 and {t.order_id for t in terms} == set(order_ids)
              and len({t.id for t in terms}) == 2, "Expected two distinct order terms")
        check(combo.total_months == sum(t.months for t in terms) == 5,
              "Concurrent grant lost subscription months")
        check(combo.anchor_at == state["anchor"] and combo.generation == 1,
              "Original anchor/generation changed")
        anchor = CalendarAnchor(state["anchor"])
        check(terms[0].start_month == 0 and terms[1].start_month == terms[0].months
              and terms[0].ends_at == terms[1].starts_at, "Terms are not contiguous")
        for term in terms:
            order = db.get(m.Order, term.order_id)
            bounds = anchor.term(term.months, start_month=term.start_month)
            check(term.months == order.subscription_months and term.generation == 1
                  and term.starts_at == bounds.start and term.ends_at == bounds.end,
                  "Term does not match frozen order/original calendar")
        for model, predicate in (
                (m.AuditLog, (m.AuditLog.order_id.in_(order_ids))
                 & (m.AuditLog.action == "subscription_term_granted")),
                (m.SettlementMeasurement, (m.SettlementMeasurement.order_id.in_(order_ids))
                 & (m.SettlementMeasurement.measurement_type == "subscription_term")),
                (m.message_center["Message"], m.message_center["Message"].event_key.in_(
                    [f"subscription_term_granted:{term.id}" for term in terms]))):
            check(db.scalar(select(func.count()).select_from(model).where(predicate)) == 2,
                  f"Expected exactly two grant events in {model.__tablename__}")
        current = hooks["current_entitlement"](db, state["enterprises"][0], state["product"],
                                                at=state["anchor"] + timedelta(seconds=1))
        check(current is not None and current["term_id"] == terms[0].id
              and current["limits"] == state["limits"] and current["period_index"] == 0,
              "Future terms added quota or changed the current period")
        periods = hooks["quota_periods"](db, state["enterprises"][0], state["product"])
        check(len(periods) == 5 and len({p["period_key"] for p in periods}) == 5,
              "Expected five separate monthly quota periods")
        for index, period in enumerate(periods):
            bounds = anchor.period(index)
            check(period["start_at"] == int(bounds.start.timestamp())
                  and period["end_at"] == int(bounds.end.timestamp())
                  and period["period_key"] == f"{combo.id}:1:{index}"
                  and all(period[key] == state["limits"][key] for key in
                          ("rate_limit_per_minute", "daily_quota", "monthly_quota")),
                  "Quota periods changed anchor or added allowance")
        return combo.id


def run():
    check(m.engine.dialect.name == "postgresql", "Real PostgreSQL DATABASE_URL required")
    check(not m.app.dependency_overrides, "Test requires normal HTTP dependencies")
    prefix = "qa-sub-concurrency-" + secrets.token_hex(5)
    state = {"prefix": prefix, "product": prefix + "-p", "version": prefix + "-v",
             "users": [prefix + "-ub", prefix + "-up"],
             "enterprises": [prefix + "-eb", prefix + "-ep"],
             "emails": [prefix + "-buyer@example.invalid", prefix + "-provider@example.invalid"],
             "requests": [prefix + "-http0", prefix + "-http1"],
             "anchor": m.now(), "limits": {"rate_limit_per_minute": 17, "daily_quota": 123,
                                           "monthly_quota": 456, "quota_amount": 789}}
    original = m.storefront["versions_for"]

    def versions_for(db, product):
        if product.id == state["product"]:
            return [{"id": state["version"]}]
        return original(db, product)

    with m.SessionLocal() as db:
        for model, ids in ((m.User, state["users"]), (m.Enterprise, state["enterprises"]),
                           (m.Product, [state["product"]]),
                           (m.ProductReleaseVersion, [state["version"]])):
            check(db.scalar(select(func.count()).select_from(model).where(model.id.in_(ids))) == 0,
                  "Fixture ID collision; refusing to modify preexisting rows")
    try:
        with patch.dict(m.storefront, {"versions_for": versions_for}):
            order_ids, token = prepare(state)
            pids, pid_lock = [], Lock()

            def grant(index, barrier=None):
                with m.SessionLocal() as db:
                    db.execute(text("SET LOCAL statement_timeout = '30s'"))
                    pid = db.scalar(text("SELECT pg_backend_pid()"))
                    order = db.scalar(select(m.Order).where(m.Order.id == order_ids[index])
                                      .with_for_update().execution_options(populate_existing=True))
                    user = db.get(m.User, state["users"][1])
                    if barrier is not None:
                        with pid_lock:
                            pids.append(pid)
                        barrier.wait(timeout=15)
                    term = m.subscription_terms["on_delivery_success"](
                        db, order, user, at=state["anchor"] if barrier is not None
                        else state["anchor"] + timedelta(days=1))
                    term_id = term.id
                    db.commit()
                    return term_id

            terms, delivery_pids = race(m.Enterprise, state["enterprises"][0], grant, pids, pid_lock)
            check(len(set(terms)) == 2, "Concurrent grants returned the same term")
            combo_id = verify_terms(state, order_ids)
            before = snapshot(state)
            for index in range(2):
                check(grant(index) == terms[index], "Repeat hook changed term identity")
            check(snapshot(state) == before, "Repeat hook changed business rows/events")

            http_pids = set()

            def capture(conn, cursor, statement, parameters, context, executemany):
                values = parameters.values() if isinstance(parameters, dict) else parameters
                if (m.audit_request_id.get() in state["requests"]
                        and "FOR UPDATE" in statement.upper() and "FROM products" in statement
                        and state["product"] in values):
                    # DBAPI cursor avoids recursive SQLAlchemy event invocation.
                    cursor.execute("SET LOCAL statement_timeout = '30s'")
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                    with pid_lock:
                        if pid not in http_pids:
                            http_pids.add(pid)
                            pids.append(pid)

            def renew(index, barrier):
                client = TestClient(m.app)
                try:
                    barrier.wait(timeout=15)
                    response = client.post(f"/api/orders/{order_ids[0]}/renewal",
                        headers={"Authorization": "Bearer " + token,
                                 "X-Request-Id": state["requests"][index]},
                        json={"subscription_months": 4, "idempotency_key": prefix + "-renew"})
                    check(response.status_code == 200,
                          f"Renewal HTTP {response.status_code}: {response.text}")
                    return response.json()
                finally:
                    client.close()

            pids.clear()
            event.listen(m.engine, "before_cursor_execute", capture)
            try:
                responses, renewal_pids = race(m.Product, state["product"], renew, pids, pid_lock)
            finally:
                event.remove(m.engine, "before_cursor_execute", capture)
            check(responses[0]["id"] == responses[1]["id"], "HTTP retries produced distinct orders")
            with m.SessionLocal() as db:
                requests = db.scalars(select(m.SubscriptionRenewal).where(
                    m.SubscriptionRenewal.combination_id == combo_id)).all()
                orders = db.scalars(select(m.Order).where(m.Order.product_id == state["product"])).all()
                check(len(requests) == 1 and len(orders) == 3, "Duplicate renewal request/order")
                request = requests[0]
                check(request.order_id == responses[0]["id"] and request.source_order_id == order_ids[0]
                      and request.months == 4 and request.idempotency_key == prefix + "-renew",
                      "Renewal request does not match HTTP request")
                order = db.get(m.Order, request.order_id)
                payments = db.scalars(select(m.Payment).where(m.Payment.order_id == order.id)).all()
                check(order.payment_status == "unpaid" and order.paid_amount == 0
                      and order.delivery_status == "not_started" and order.business_type == "renewal"
                      and order.related_order_id == order_ids[0] and order.subscription_months == 4
                      and order.snapshot_version == 1 and order.product_version_id == state["version"]
                      and len(payments) == 1 and payments[0].status == "unpaid"
                      and payments[0].amount == order.amount,
                      "Renewal must create exactly one unpaid, unprovisioned order/payment")
            verify_terms(state, order_ids)
            result = {"status": "passed", "database": "postgresql", "fixture": prefix,
                      "layer": "trusted verified-caller hook and HTTP renewal; no APISIX health assertion",
                      "delivery_worker_pids": delivery_pids, "renewal_worker_pids": renewal_pids,
                      "terms": 2, "total_months": 5, "hook_repeat_unchanged": True,
                      "renewal_http_statuses": [200, 200], "renewal_orders": 1}
    finally:
        cleanup(state)
    result["cleanup"] = "verified no owned fixtures remain"
    return result


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
