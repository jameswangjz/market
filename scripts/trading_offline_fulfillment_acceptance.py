"""Real PG/MinIO/ClamAV offline fulfillment acceptance, without deployment.

Run with the backend dependencies and app on PYTHONPATH, against an already
migrated PostgreSQL database and configured MinIO/ClamAV services:
    PYTHONPATH=backend .venv/bin/python scripts/trading_offline_fulfillment_acceptance.py

Uses main's installed HTTP hooks, never substitutes fulfillment or scanners.
All DB writes use savepoints inside an outer transaction that is rolled back.
Only this run's provider-user and API-created order storage prefixes are removed.
Do not run concurrently with other in-process dependency override users.
"""

import hashlib
import json
import secrets
import sys
from collections import Counter
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import main as m


# Standard harmless antivirus test signature, never an executable payload.
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def run():
    assert m.engine.dialect.name == "postgresql", "Real PostgreSQL is required"
    assert m.MINIO_ENDPOINT and m.CLAMAV_ENABLED, "MinIO and ClamAV must be configured"
    assert hasattr(m, "order_fulfillment"), "Main must install offline fulfillment hooks"
    prefix = "qa-off-" + secrets.token_hex(6)
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY,
                      secret_key=m.MINIO_SECRET_KEY, secure=False)
    assert storage.bucket_exists(m.MINIO_BUCKET), "Use an existing MinIO bucket"
    scan, report = m.clamav_scan_stream(BytesIO(EICAR), len(EICAR))
    assert scan == "infected" and "FOUND" in report, (scan, report)
    connection = m.engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    missing = object()
    previous = m.app.dependency_overrides.get(m.db_session, missing)
    client = None
    object_prefixes = [prefix + "-provider/"]
    order_id = None
    try:
        roles = {"provider": "", "provideradmin": "", "buyer": "", "buyeradmin": "",
                 "outsider": "", "member": "", "providermember": "",
                 "platform": "super_admin", "ops": "platform_operator",
                 "monitor": "delivery_monitor"}
        users = {name: m.User(id=prefix + "-" + name,
                             email=prefix + "-" + name + "@example.invalid",
                             name="Isolated offline " + name,
                             password_hash="not-a-login-password", is_active=True,
                             verified_status="verified", activation_status="active",
                             platform_role=role) for name, role in roles.items()}
        enterprises = {name: m.Enterprise(id=prefix + "-e" + name,
                                         name="Isolated offline " + name + " " + prefix,
                                         credit_code=prefix + "-" + name,
                                         verification_status="verified")
                       for name in ("provider", "buyer", "outsider")}
        ep, eb = enterprises["provider"], enterprises["buyer"]
        db.add_all(list(users.values()) + list(enterprises.values()))
        db.flush()
        memberships = (("provider", ep, "super_admin"),
                       ("provideradmin", ep, "enterprise_admin"),
                       ("buyer", eb, "super_admin"), ("buyeradmin", eb, "enterprise_admin"),
                       ("member", eb, "member"), ("providermember", ep, "member"),
                       ("outsider", enterprises["outsider"], "super_admin"),
                       ("platform", ep, "super_admin"), ("platform", eb, "super_admin"))
        db.add_all([m.Membership(user_id=users[name].id, enterprise_id=enterprise.id,
                                role=role, status="active")
                    for name, enterprise, role in memberships])
        rule = json.dumps(dict(platform_rate=20, provider_rate=80, service_rate=0,
                               expert_rate=0, channel_rate=0))
        product = m.Product(id=prefix + "-product", enterprise_id=ep.id,
                            name="Isolated offline fulfillment", provider_name=ep.name,
                            product_type="consulting", delivery_method="consulting",
                            status="draft", settlement_rule_mode="custom",
                            settlement_rule_json=rule)
        db.add(product)
        db.flush()
        version = m.ProductReleaseVersion(id=prefix + "-version", product_id=product.id,
                                          version_code="v1", price=100, cost=50, status="active")
        db.add(version)
        db.commit()
        m.app.dependency_overrides[m.db_session] = lambda: db
        client = TestClient(m.app)

        def request(method, path, actor="buyer", status=200, body=None, **kwargs):
            response = client.request(method, "/api" + path,
                headers={"Authorization": "Bearer " + m.issue_token(users[actor])},
                **({"json": body} if body is not None else {}), **kwargs)
            assert response.status_code == status, (
                actor, method, path, response.status_code, response.text[:800])
            return response

        image = BytesIO()
        Image.new("RGB", (380, 280), (20, 136, 85)).save(image, "PNG")
        logo = request("POST", "/files/upload", "provider",
            data={"product_id": product.id, "file_role": "product_logo"},
            files={"upload": ("logo.png", image.getvalue(), "image/png")}).json()
        assert logo["clamav_status"] == "clean", logo
        m.trading_policy["validate_submission"](db, product)
        product.status = "published"
        db.commit()
        public = request("GET", "/storefront/products/" + product.id).json()
        assert version.id in {row["id"] for row in public["versions"]}, public
        assert client.get(public["logo_url"]).status_code == 200
        body = {"product_id": product.id, "product_version_id": version.id,
                "buyer_enterprise_id": eb.id, "subscription_months": 1}
        quote = request("POST", "/orders/quote", body=body).json()
        created = request("POST", "/orders", body={**body, "quote_id": quote["quote_id"]}).json()
        order_id = created["id"]
        object_prefixes.append("delivery/" + order_id + "/")
        path = "/orders/" + order_id
        assert created["main_status"] == "pending_provider_review", created
        order = db.get(m.Order, order_id)
        assert order.snapshot_version == 1 and order.delivery_method_snapshot == "consulting"
        assert (order.buyer_enterprise_id, order.provider_enterprise_id) == (eb.id, ep.id)
        request("POST", path + "/transition", status=409, body={"action": "confirm_payment"})
        for actor in ("buyer", "buyeradmin", "outsider", "member", "platform"):
            request("POST", path + "/provider-review", actor, status=403,
                    body={"decision": "approve"})
        provider_quote = request("GET", path + "/provider-quote", "provider").json()
        reviewed = request("POST", path + "/provider-review", "provider", body={
            "decision": "approve", "amount": 120, "cost": 60,
            "reason": "Isolated revised consulting scope",
            "expected_updated_at": provider_quote["expected_updated_at"]}).json()
        assert reviewed["main_status"] == "pending_payment" and reviewed["amount"] == 120
        request("POST", path + "/transition", "buyeradmin", status=403,
                body={"action": "confirm_payment"})
        request("POST", path + "/transition", body={"action": "confirm_payment"})
        payment = db.scalar(select(m.Payment).where(m.Payment.order_id == order_id))
        assert payment.status == "paid" and payment.amount == m.Decimal("120.00")
        assert m.order_cost(db, order) == m.Decimal("60.00")
        frozen = (order.economic_snapshot_json, order.delivery_snapshot_json)
        tasks = db.scalars(select(m.DeliveryTask).where(m.DeliveryTask.order_id == order_id)).all()
        assert len(tasks) == 1, tasks
        task_id = tasks[0].id
        attachments_path = "/delivery-tasks/" + task_id + "/attachments"

        def state(main, delivery=None):
            db.expire_all()
            current = db.get(m.Order, order_id)
            task_rows = db.scalars(select(m.DeliveryTask).where(m.DeliveryTask.order_id == order_id)).all()
            assert len(task_rows) == 1 and task_rows[0].id == task_id
            task = task_rows[0]
            expected = (main, delivery or main, "completed" if main == "completed" else main)
            assert (current.main_status, current.delivery_status, task.status) == expected
            assert (task.method, task.delivery_mode, task.assignee) == ("consulting", "manual", ep.id)
            assert task.next_retry_at is None
            assert (current.economic_snapshot_json, current.delivery_snapshot_json) == frozen
            detail = request("GET", path, "buyeradmin").json()["order"]
            assert (detail["main_status"], detail["delivery_status"]) == expected[:2]
            visible = request("GET", "/delivery-tasks", "provideradmin").json()["items"]
            assert [row["status"] for row in visible if row["id"] == task_id] == [expected[2]]

        def footprint():
            db.expire_all()
            current, task = db.get(m.Order, order_id), db.get(m.DeliveryTask, task_id)
            counts = tuple(db.scalar(select(func.count()).select_from(model).where(predicate))
                for model, predicate in (
                    (m.DeliveryTask, m.DeliveryTask.order_id == order_id),
                    (m.DeliveryAttachment, m.DeliveryAttachment.task_id == task_id),
                    (m.FileObject, m.FileObject.owner_id.in_([user.id for user in users.values()])),
                    (m.OrderStateLog, m.OrderStateLog.order_id == order_id),
                    (m.AuditLog, m.AuditLog.order_id == order_id),
                    (m.SettlementMeasurement, m.SettlementMeasurement.order_id == order_id)))
            return (current.main_status, current.delivery_status, task.status, task.last_error,
                    task.note, current.updated_at, counts)

        def objects():
            return {obj.object_name for own_prefix in object_prefixes
                    for obj in storage.list_objects(m.MINIO_BUCKET, prefix=own_prefix, recursive=True)}

        def denied(action, actor, status=403, reason=""):
            before = footprint()
            request("POST", path + "/transition", actor, status=status,
                    body={"action": action, "reason": reason})
            assert footprint() == before, (actor, action, "Denied transition mutated fixtures")

        def upload(actor, name, content, status=200):
            before, before_objects = footprint(), objects()
            result = request("POST", attachments_path, actor, status=status,
                data={"description": name}, files={"upload": (name, content, "text/plain")})
            if status != 200:
                assert footprint() == before, "Denied upload mutated fixtures"
                assert objects() == before_objects, "Denied upload left a storage object"
                return None
            attachment = result.json()
            item = db.get(m.FileObject, attachment["file_id"])
            assert item.scan_status == "clean" and item.scanned_at and item.scan_report
            assert item.file_role == "delivery_attachment" and item.owner_id == users[actor].id
            assert item.object_name.startswith(object_prefixes[1])
            assert item.size == len(content) and item.checksum == hashlib.sha256(content).hexdigest()
            stream = storage.get_object(m.MINIO_BUCKET, item.object_name)
            try:
                assert stream.read() == content, "Persisted MinIO bytes differ"
            finally:
                stream.close()
                stream.release_conn()
            return attachment

        state("awaiting_start")
        for actor in ("buyer", "buyeradmin", "outsider", "member", "providermember",
                      "platform", "ops", "monitor"):
            for action in ("start_delivery", "submit_delivery"):
                denied(action, actor)
            upload(actor, "denied.txt", b"Denied provider evidence", 403)
        for action in ("submit_delivery", "accept_delivery", "reject_delivery"):
            denied(action, "buyer" if action != "submit_delivery" else "provider", 409, "Reason")
        original = upload("provider", "original.txt", (prefix + " original scope\n").encode())
        upload("provider", "eicar.txt", EICAR, 400)
        request("POST", path + "/transition", "provideradmin", body={"action": "start_delivery"})
        state("in_delivery")
        working = upload("provider", "working.txt", (prefix + " consulting report\n").encode())
        request("POST", path + "/transition", "provider", body={"action": "submit_delivery"})
        state("pending_acceptance")
        upload("provider", "frozen.txt", b"Evidence frozen during acceptance", 409)
        for actor in ("provider", "provideradmin", "outsider", "member", "providermember",
                      "platform", "ops", "monitor"):
            for action in ("accept_delivery", "reject_delivery"):
                denied(action, actor, reason="Revise")
        for reason in ("", "   ", "\t\n", None):
            denied("reject_delivery", "buyeradmin", 400, reason)
        request("POST", path + "/transition", "buyeradmin",
                body={"action": "reject_delivery", "reason": "  Revise consulting scope  "})
        state("rectifying")
        assert db.get(m.DeliveryTask, task_id).last_error == "Revise consulting scope"
        revision = upload("provideradmin", "revision.txt", (prefix + " revised scope\n").encode())
        # Catalog edits must not alter this order's fulfillment/economic snapshots.
        product.delivery_method = "file"
        version.price, version.cost = 999, 500
        db.commit()
        request("POST", path + "/transition", "provideradmin", body={"action": "submit_delivery"})
        state("pending_acceptance")
        assert db.get(m.DeliveryTask, task_id).last_error == ""
        request("POST", path + "/transition", "buyer", body={"action": "accept_delivery"})
        state("completed", "accepted")
        upload("provider", "terminal.txt", b"Evidence frozen after completion", 409)
        for action, actor in (("start_delivery", "provider"), ("submit_delivery", "provider"),
                              ("accept_delivery", "buyer"), ("reject_delivery", "buyeradmin"),
                              ("confirm_order", "buyer")):
            denied(action, actor, 409, "Reason")
        before_process = footprint()
        request("POST", "/delivery-tasks/" + task_id + "/process", "platform",
                status=409, body={"success": True})
        assert footprint() == before_process, "Legacy processing mutated fixtures"
        state("completed", "accepted")

        evidence = (original, working, revision)
        allowed_readers = ("provider", "provideradmin", "buyer", "buyeradmin", "platform", "ops", "monitor")
        for actor in allowed_readers:
            listed = request("GET", attachments_path, actor).json()["items"]
            assert {row["id"] for row in listed} == {item["id"] for item in evidence}
            for attachment in evidence:
                file_item = db.get(m.FileObject, attachment["file_id"])
                downloaded = request("GET", "/files/" + file_item.id + "/download", actor)
                assert hashlib.sha256(downloaded.content).hexdigest() == file_item.checksum
        for actor in ("outsider", "member", "providermember"):
            request("GET", attachments_path, actor, status=403)
            for attachment in evidence:
                request("GET", "/files/" + attachment["file_id"] + "/download", actor, status=403)
            listed = request("GET", "/delivery-tasks", actor).json()["items"]
            assert task_id not in {row["id"] for row in listed}

        expected_events = (
            ("payment_fulfillment_ready", "pending_fulfillment", "awaiting_start", "buyer", ""),
            ("start_delivery", "awaiting_start", "in_delivery", "provideradmin", ""),
            ("submit_delivery", "in_delivery", "pending_acceptance", "provider", ""),
            ("reject_delivery", "pending_acceptance", "rectifying", "buyeradmin", "Revise consulting scope"),
            ("submit_delivery", "rectifying", "pending_acceptance", "provideradmin", ""),
            ("accept_delivery", "pending_acceptance", "completed", "buyer", ""))
        audits = db.scalars(select(m.AuditLog).where(m.AuditLog.order_id == order_id,
            m.AuditLog.action.in_({event[0] for event in expected_events}))).all()
        assert len(audits) == len(expected_events)
        for action, old, new, actor, reason in expected_events:
            matches = [row for row in audits if row.action == action
                       and json.loads(row.before_json)["main_status"] == old]
            assert len(matches) == 1, (action, old, matches)
            row = matches[0]
            before, after = json.loads(row.before_json), json.loads(row.after_json)
            old_delivery = "preparing" if action == "payment_fulfillment_ready" else old
            assert (before["delivery_status"], before["task_status"]) == (old_delivery, old_delivery)
            assert (after["main_status"], after["delivery_status"], after["task_status"]) == (
                new, "accepted" if new == "completed" else new, new)
            assert before["task_id"] == after["task_id"] == task_id
            assert row.actor == users[actor].email and row.category == "delivery"
            assert row.tenant_id == (eb.id if action in {"accept_delivery", "reject_delivery"} else ep.id)
            if action != "payment_fulfillment_ready":
                assert after["reason"] == reason
        logs = db.scalars(select(m.OrderStateLog).where(m.OrderStateLog.order_id == order_id,
            m.OrderStateLog.action.in_({event[0] for event in expected_events}))).all()
        expected_logs = Counter()
        for action, old, new, _, reason in expected_events:
            expected_logs[(action, "main", old, new, reason)] += 1
            expected_logs[(action, "delivery", "preparing" if action == "payment_fulfillment_ready" else old,
                           "accepted" if new == "completed" else new, reason)] += 1
        assert Counter((row.action, row.domain, row.from_status, row.to_status, row.reason)
                       for row in logs) == expected_logs
        upload_audits = db.scalars(select(m.AuditLog).where(m.AuditLog.order_id == order_id,
            m.AuditLog.action == "upload_delivery_attachment")).all()
        assert {json.loads(row.after_json)["file_id"] for row in upload_audits} == {
            item["file_id"] for item in evidence}
        assert len(upload_audits) == len(evidence)
        assert all(json.loads(row.after_json)["scan_status"] == "clean" for row in upload_audits)
        return {"status": "passed", "database": "postgresql", "checks": [
            "isolated verified party roles and consulting product/logo",
            "API quote/order snapshot, provider review and payment",
            "awaiting_start/in_delivery/pending_acceptance/rectifying/resubmit/completed-accepted",
            "party role denials, required rejection reason and terminal guards",
            "real clean ClamAV/MinIO uploads, EICAR rejection and upload state freezes",
            "party/platform evidence list/download and outsider/member denial",
            "one preserved manual task, immutable snapshots and exact audit/state logs"],
            "fixture": "outer transaction rollback verified; own-prefix MinIO objects removed",
            "limits": "Manual payment confirmation; no live payment channel or deployment"}
    finally:
        primary = sys.exc_info()[1]
        errors = []

        def cleanup(label, operation):
            try:
                operation()
            except Exception as exc:
                errors.append(label + ": " + str(exc))

        if client is not None:
            cleanup("TestClient close", client.close)
        if previous is missing:
            m.app.dependency_overrides.pop(m.db_session, None)
        else:
            m.app.dependency_overrides[m.db_session] = previous
        cleanup("Session close", db.close)
        cleanup("Outer rollback", outer.rollback)
        cleanup("Connection close", connection.close)

        def verify_rollback():
            with m.engine.connect() as check:
                for model, predicate in (
                    (m.User, m.User.id.like(prefix + "-%")),
                    (m.Enterprise, m.Enterprise.id.like(prefix + "-%")),
                    (m.Product, m.Product.id == prefix + "-product"),
                    (m.FileObject, m.FileObject.owner_id.like(prefix + "-%"))):
                    assert check.scalar(select(func.count()).select_from(model).where(predicate)) == 0
                if order_id is not None:
                    for model, predicate in (
                        (m.Order, m.Order.id == order_id),
                        (m.Payment, m.Payment.order_id == order_id),
                        (m.DeliveryTask, m.DeliveryTask.order_id == order_id),
                        (m.OrderStateLog, m.OrderStateLog.order_id == order_id),
                        (m.AuditLog, m.AuditLog.order_id == order_id),
                        (m.SettlementMeasurement, m.SettlementMeasurement.order_id == order_id)):
                        assert check.scalar(select(func.count()).select_from(model).where(predicate)) == 0

        cleanup("Rollback verification", verify_rollback)
        for own_prefix in object_prefixes:
            def remove_objects(own_prefix=own_prefix):
                for obj in storage.list_objects(m.MINIO_BUCKET, prefix=own_prefix, recursive=True):
                    assert obj.object_name.startswith(own_prefix)
                    storage.remove_object(m.MINIO_BUCKET, obj.object_name)
                assert not list(storage.list_objects(m.MINIO_BUCKET, prefix=own_prefix, recursive=True))
            cleanup("Storage prefix " + own_prefix, remove_objects)
        if errors:
            message = "Acceptance cleanup failed: " + "; ".join(errors)
            if primary is not None:
                primary.add_note(message)
            else:
                raise RuntimeError(message)


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
