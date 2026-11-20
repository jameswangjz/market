#!/usr/bin/env python3
"""Exercise Market HTTP routes in the isolated K8S1 DM or PostgreSQL lab."""

from argparse import ArgumentParser
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlalchemy import inspect, literal, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
parser = ArgumentParser(description=__doc__)
parser.add_argument("--postgres-lab", action="store_true")
args = parser.parse_args()
if args.postgres_lab:
    password = quote(Path("/tmp/qa-pg-password").read_text().strip(), safe="")
    host = "market-pg-compat-qa.market-dm8-qa.svc.cluster.local"
    os.environ["DATABASE_URL"] = f"postgresql+psycopg://marketqa:{password}@{host}:5432/marketqa"
else:
    user = os.environ["DM_APP_USER"]
    if user not in {"MARKET_APP_QA", "MARKET_APP_DM_FINAL"}:
        parser.error("Use the isolated K8S1 test schema")
    host = "dm8.market-dm8-qa.svc.cluster.local"
    os.environ["DATABASE_URL"] = f"dm+dmPython://{user}:{quote(os.environ['DM_APP_PASSWORD'], safe='')}@{host}:5236"
os.environ["APISIX_ENABLED"] = "false"
os.environ["MESSAGE_CENTER_DISABLE_BACKGROUND"] = "true"
os.environ["MINIO_ENDPOINT"] = ""
os.environ["PRESIDIO_ANALYZER_URL"] = "http://127.0.0.1:1/analyze"

from app import main as m

prefix = "dmq-" + secrets.token_hex(5)


def check_route(api, method, path, status=200, actor=None, **kwargs):
    headers = {"Authorization": "Bearer " + actor} if actor else {}
    response = api.request(method, "/api" + path, headers=headers, **kwargs)
    assert response.status_code == status, (method, path, response.status_code, response.text[:650])
    return response.json()


def verify_file_refund(api, admin, buyer, buyer_id, source_product_id, parent_id):
    from app.refund_execution_worker import process_once

    category = check_route(api, "POST", "/admin/product-categories", actor=admin,
                           json={"name": "数据集", "parent_id": parent_id, "delivery_method": "file"})
    product_id, version_id = prefix + "-file", prefix + "-file-v1"
    data = b"isolated-data-for-DM-smoke"
    with m.SessionLocal.begin() as db:
        source = db.get(m.Product, source_product_id)
        source_logo = db.get(m.FileObject, source.logo_file_id)
        product = m.Product(id=product_id, enterprise_id=source.enterprise_id,
            name="达梦文件交付-" + prefix, provider_name=source.provider_name, product_type="dataset",
            delivery_method="file", status="published", catalog_name="达梦测试目录-数据集",
            category_id=category["id"], settlement_rule_mode="custom",
            settlement_rule_json=source.settlement_rule_json)
        db.add(product)
        db.flush()
        db.add(m.ProductReleaseVersion(id=version_id, product_id=product_id, version_code="v1",
                                      price=100, cost=40, status="active"))
        db.flush()
        logo = m.FileObject(owner_id=source_logo.owner_id, product_id=product_id,
            object_name=prefix + "/file-logo.png", original_name="logo.png", content_type="image/png",
            size=100, file_role="product_logo", scan_status="clean", status="active")
        db.add(logo)
        db.add(m.FileObject(owner_id=source_logo.owner_id, product_id=product_id, version_id=version_id,
            object_name=prefix + "/data.zip", original_name="data.zip", content_type="application/zip",
            size=len(data), checksum=sha256(data).hexdigest(), file_role="product_data",
            scan_status="clean", scan_report='{"presidio_status":"available"}', status="active"))
        db.flush()
        product.logo_file_id = logo.id

    body = {"product_id": product_id, "product_version_id": version_id,
            "buyer_enterprise_id": buyer_id, "subscription_months": 1}
    quote_result = check_route(api, "POST", "/orders/quote", actor=buyer, json=body)
    order = check_route(api, "POST", "/orders", actor=buyer, json={**body, "quote_id": quote_result["quote_id"]})
    order_path = f"/orders/{order['id']}"
    check_route(api, "POST", order_path + "/transition", actor=buyer, json={"action": "start_payment"})
    check_route(api, "POST", order_path + "/transition", actor=admin, json={"action": "confirm_payment"})
    storage = Mock()
    storage.stat_object.return_value.size = len(data)
    storage.get_object.return_value.read.return_value = data[:1]
    with patch("app.online_delivery.open_storage", return_value=SimpleNamespace(client=storage, bucket="qa")):
        assert m.online_delivery["process_once"]()
    assert check_route(api, "GET", order_path + "/product-files", actor=buyer)["items"]

    request = check_route(api, "POST", order_path + "/refund-requests", actor=buyer,
                          json={"amount": "20.00", "reason": "隔离演练", "idempotency_key": prefix + "-ask"})
    refund_path = f"/refunds/{request['id']}"
    assessed = check_route(api, "POST", refund_path + "/manual-assessments", actor=admin, json={
        "amount": "20.00", "refundable_limit": "100.00", "used_calls": 0, "reason": "隔离演练核定",
        "evidence_refs": ["qa:" + prefix], "expected_version": request["version"], "idempotency_key": prefix + "-assess",
    })
    approved = check_route(api, "POST", refund_path + "/review", actor=admin,
                           json={"decision": "approve", "reason": "隔离演练批准",
                                 "expected_version": assessed["version"], "idempotency_key": prefix + "-review"})
    assert approved["execution_available"]
    check_route(api, "POST", refund_path + "/execute", actor=admin,
                json={"reason": "隔离演练执行", "expected_version": approved["version"],
                      "idempotency_key": prefix + "-execute"})
    assert process_once(m.SessionLocal, ns=vars(m))
    check_route(api, "GET", order_path + "/product-files", 409, buyer)
    for _ in range(6):
        if not process_once(m.SessionLocal, ns=vars(m)):
            break
    completed = check_route(api, "GET", refund_path, actor=admin)
    assert completed["status"] == "completed", completed["status"]
    with m.SessionLocal.begin() as db:
        refund = db.get(m.Refund, request["id"])
        adjustment = m.refund_settlement["negative_settlement"](db, refund)
        assert adjustment.order_id == order["id"] and adjustment.is_refund
        assert (adjustment.net_amount, adjustment.cost_amount, adjustment.profit_amount) == (
            m.Decimal("-20.00"), m.Decimal("-8.00"), m.Decimal("-12.00"))
    print("File refund execution, download access revoked and proportional cost reversal passed", order["order_no"], flush=True)


def run():
    dialect = "postgresql" if args.postgres_lab else "dm"
    assert m.engine.dialect.name == dialect
    with TestClient(m.app) as api:
        tables = set(inspect(m.engine).get_table_names())
        assert {name.lower() for name in m.Base.metadata.tables} <= {name.lower() for name in tables}
        print(dialect, "schema and startup verified", len(tables), "tables", flush=True)
        admin = check_route(api, "POST", "/auth/login",
                            json={"identifier": "admin@market.local", "password": "Admin123!"})["token"]
        assert check_route(api, "GET", "/health")["status"] == "ok"
        check_route(api, "GET", "/storefront/products", params={"page": 1, "page_size": 5})
        parent = check_route(api, "POST", "/admin/product-categories", actor=admin,
                             json={"name": "达梦目录-" + prefix})
        child = check_route(api, "POST", "/admin/product-categories", actor=admin,
                            json={"name": "数据咨询", "parent_id": parent["id"], "delivery_method": "consulting"})
        check_route(api, "POST", "/admin/product-categories", 409, admin, json={"name": "达梦目录-" + prefix})
        print("Admin login, storefront pagination and category uniqueness passed", flush=True)

        with m.SessionLocal.begin() as db:
            accounts, enterprises = {}, {}
            for role in ("provider", "buyer", "outsider"):
                accounts[role] = m.User(id=prefix + "-" + role, email=f"{prefix}-{role}@example.invalid",
                    name="达梦账户-" + role, password_hash="unused-qa", is_active=True,
                    activation_status="active", verified_status="verified")
                enterprises[role] = m.Enterprise(id=prefix + "-e-" + role,
                    name="达梦企业-" + prefix + role, credit_code=prefix + "-" + role,
                    verification_status="verified")
            db.add_all([*accounts.values(), *enterprises.values()])
            db.flush()
            for role in accounts:
                db.add(m.Membership(user_id=accounts[role].id, enterprise_id=enterprises[role].id,
                                    role="super_admin", status="active"))
            product_id, version_id = prefix + "-product", prefix + "-version"
            product = m.Product(id=product_id, enterprise_id=enterprises["provider"].id,
                name="达梦线下咨询-" + prefix, provider_name=enterprises["provider"].name,
                product_type="consulting", delivery_method="consulting", status="draft",
                category_id=child["id"], catalog_name="达梦目录-数据咨询",
                description="隔离环境咨询产品", usage_scenarios="咨询回归", pricing_strategy="一次性",
                authorization_conditions="购买企业内部", data_source_statement="合成夹具",
                compliance_statement="仅用于兼容回归",
                settlement_rule_mode="custom", settlement_rule_json=json.dumps(
                    {"platform_rate":20, "provider_rate":80, "service_rate":0, "expert_rate":0, "channel_rate":0}))
            db.add(product)
            db.flush()
            db.add(m.ProductReleaseVersion(id=version_id, product_id=product_id, version_code="v1",
                                          price=100, cost=50, status="active"))
            logo = m.FileObject(owner_id=accounts["provider"].id, product_id=product_id,
                               object_name=prefix+"/logo.png", original_name="logo.png",
                               content_type="image/png", size=100, file_role="product_logo",
                               scan_status="clean", status="active")
            db.add(logo)
            db.flush()
            product.logo_file_id = logo.id
            tokens = {name: m.issue_token(row) for name, row in accounts.items()}
            buyer_id = enterprises["buyer"].id
        product_path = f"/products/{product_id}"
        submitted = check_route(api, "POST", product_path + "/submit", actor=tokens["provider"])
        assert submitted["status"] == "pending_review"
        check_route(api, "POST", product_path + "/review", 403, tokens["outsider"], json={"decision": "approve"})
        check_route(api, "POST", product_path + "/review", 400, admin, json={"decision": "reject"})
        for path, expected in [
            ("/review", "quality_review"), ("/review", "security_review"),
            ("/security-review", "operation_review"), ("/review", "published"),
        ]:
            result = check_route(api, "POST", product_path + path, actor=admin, json={"decision": "approve"})
            assert result["status"] == expected, (path, result["status"])
        storefront = check_route(api, "GET", "/storefront/products", params={"q": prefix})
        assert any(row["id"] == product_id for row in storefront["items"])
        with m.SessionLocal.begin() as db:
            actions = set(db.scalars(select(m.AuditLog.action).where(m.AuditLog.target_id == product_id)))
            assert {"submit_product_review", "business_approve_product", "quality_approve_product",
                    "approve_product_security", "operation_approve_product"} <= actions
            client = m.ensure_oauth_client(db, db.get(m.Product, product_id))
            machine = {"client_id": client.client_id, "client_secret": client.client_secret}
        print("Product submission, four review stages, storefront visibility and audit passed", flush=True)

        issued = api.post("/oauth/token", data={**machine, "grant_type": "client_credentials",
                                               "scope": "resource.invoke"})
        assert issued.status_code == 200, issued.status_code
        machine_token = issued.json()["access_token"]
        assert api.post("/oauth/introspect", data={"token": machine_token}).json()["active"] is True
        for _ in range(2):
            assert api.post("/oauth/revoke", data={**machine, "token": machine_token}).status_code == 200
        assert api.post("/oauth/introspect", data={"token": machine_token}).json() == {"active": False}
        print("Machine OAuth issue, introspection and idempotent revocation passed", flush=True)

        body = {"product_id": product_id, "product_version_id": version_id,
                "buyer_enterprise_id": buyer_id, "subscription_months": 1}
        quote_result = check_route(api, "POST", "/orders/quote", actor=tokens["buyer"], json=body)
        order = check_route(api, "POST", "/orders", actor=tokens["buyer"],
                            json={**body, "quote_id": quote_result["quote_id"]})
        order_path = "/orders/" + order["id"]
        assert order["main_status"] == "pending_provider_review", order["main_status"]
        check_route(api, "POST", order_path+"/provider-review", 403, tokens["outsider"], json={"decision":"approve"})
        result = check_route(api, "POST", order_path+"/provider-review", actor=tokens["provider"],
                             json={"decision":"approve","amount":120,"cost":60,"reason":"达梦演练报价"})
        assert result["main_status"] == "pending_payment" and result["amount"] == 120
        check_route(api, "POST", order_path+"/transition", actor=tokens["buyer"], json={"action":"start_payment"})
        paid = check_route(api, "POST", order_path+"/transition", actor=admin, json={"action":"confirm_payment"})
        assert paid["payment_status"] == "paid" and paid["main_status"] == "awaiting_start", (
            paid["payment_status"], paid["main_status"])
        for action, role, expected in [
            ("start_delivery","provider","in_delivery"), ("submit_delivery","provider","pending_acceptance"),
            ("accept_delivery","buyer","completed"),
        ]:
            result=check_route(api,"POST",order_path+"/transition",actor=tokens[role],json={"action":action})
            assert result["main_status"] == expected, (action,result["main_status"])
        audit_payload = check_route(api,"GET","/audit-logs",actor=admin,params={"order_id":order["id"],"page_size":100})
        assert audit_payload["items"]
        with m.SessionLocal.begin() as db:
            order_row = db.get(m.Order, order["id"])
            payment = db.scalar(select(m.Payment).where(m.Payment.order_id==order["id"]))
            assert m.order_cost(db,order_row) == m.Decimal("60.00") and payment.amount == m.Decimal("120.00")
            # Reserve a unique historical period so repeated smoke runs do not touch earlier evidence.
            used_periods = set(db.scalars(select(m.SettlementBatch.period_start).where(m.SettlementBatch.cycle == "monthly")))
            year = next(y for y in range(2000, 2025)
                        if all(value is None or value.year != y for value in used_periods))
            payment.paid_at = datetime(year, 1, 15, tzinfo=timezone.utc)
        print("Offline order, review ACL, payment, fulfillment, audit, and amounts passed", order["order_no"], flush=True)

        batch_body = {"cycle":"manual", "order_ids":[order["id"]], "idempotency_key":prefix}
        batch = check_route(api,"POST","/settlement-batches",actor=admin,json=batch_body)
        assert (batch["total_amount"], batch["total_profit"]) == (120, 60), batch
        again = check_route(api,"POST","/settlement-batches",actor=admin,json=batch_body)
        assert again["id"] == batch["id"] and again["idempotent"] is True
        lines = check_route(api,"GET",f"/settlement-batches/{batch['id']}/lines",actor=admin)["items"]
        assert {item["participant_type"]:item["amount"] for item in lines} == {
            "platform":12, "provider":48, "service":0, "expert":0, "channel":0
        }
        print("Manual settlement calculation, five shares and idempotency passed", batch["batch_no"], flush=True)

        period = {"cycle": "monthly", "order_ids": [order["id"]],
                  "period_start": datetime(year, 1, 1, tzinfo=timezone.utc).isoformat(),
                  "period_end": datetime(year, 2, 1, tzinfo=timezone.utc).isoformat()}
        monthly = check_route(api, "POST", "/settlement-batches", actor=admin, json=period)
        assert (monthly["total_amount"], monthly["total_profit"]) == (120, 60)
        lines = check_route(api, "GET", f"/settlement-batches/{monthly['id']}/lines", actor=admin)["items"]
        settlement_ids = {row["settlement_id"] for row in lines}
        assert len(settlement_ids) == 1
        sid = settlement_ids.pop()
        locked = check_route(api, "POST", f"/settlements/{sid}/lock", actor=admin)
        assert locked["status"] == "locked"
        confirmed = check_route(api, "POST", f"/settlement-batches/{monthly['id']}/confirm", actor=admin, json={})
        assert confirmed["status"] == "confirmed"
        check_route(api, "POST", "/settlement-batches", 409, admin,
                    json={**period, "rebuild": True, "idempotency_key": prefix + "-after-confirm"})
        print("Monthly settlement, lock, confirmation and rebuild guard passed", monthly["batch_no"], flush=True)

        verify_file_refund(api, admin, tokens["buyer"], buyer_id, product_id, parent["id"])

        from app.api_charge_ledger import after_stream_position, stream_position_sql
        with m.SessionLocal() as db:
            position = stream_position_sql(literal("123-11"), dialect)
            assert db.scalar(select(literal(1)).where(after_stream_position(position, (123, 9), dialect))) == 1
            assert db.scalar(select(literal(1)).where(after_stream_position(position, (123, 11), dialect))) is None
            assert db.scalar(select(literal(1)).where(after_stream_position(position, (124, 0), dialect))) is None
        print("Numeric Redis stream position comparisons passed", flush=True)

        if dialect == "dm":
            from app.database_dameng import transaction_lock
            with m.SessionLocal() as entry, m.SessionLocal() as contender:
                assert transaction_lock(entry.connection(), prefix)
                assert not transaction_lock(contender.connection(), prefix, timeout=0)
                entry.rollback()
                assert transaction_lock(contender.connection(), prefix, timeout=0)
                assert not transaction_lock(entry.connection(), prefix, timeout=0)
                contender.commit()
                assert transaction_lock(entry.connection(), prefix, timeout=0)
            print("DM user lock release after read-only rollback and commit passed", flush=True)


if __name__ == "__main__":
    run()
