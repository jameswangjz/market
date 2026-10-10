"""Real PG/MinIO/ClamAV order snapshot acceptance with outer fixture rollback."""
import json
import secrets
import zipfile
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
from app import main as m


def run():
    marker = secrets.token_hex(10)
    actor_id = "trd-u-" + marker
    product_id = "trd-p-" + marker
    version_id = "trd-v-" + marker
    connection = m.engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    previous = m.app.dependency_overrides.get(m.db_session)
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY, secret_key=m.MINIO_SECRET_KEY, secure=False)
    try:
        actor = m.User(id=actor_id, email=marker + "@example.invalid", name="Isolated trading QA",
                       password_hash="not-a-login-password", verified_status="verified", activation_status="active")
        first = m.Enterprise(id="trd-e1-" + marker, name="Isolated first enterprise", credit_code="FIRST" + marker)
        selected = m.Enterprise(id="trd-e2-" + marker, name="Isolated selected enterprise", credit_code="SECOND" + marker)
        session.add_all([actor, first, selected]); session.flush()
        session.add_all([m.Membership(user_id=actor_id, enterprise_id=e.id, role="super_admin") for e in (first, selected)])
        product = m.Product(id=product_id, enterprise_id=first.id, name="TRD PG rollback acceptance",
            product_type="dataset", delivery_method="file", status="draft", provider_name=first.name,
            settlement_rule_mode="custom", settlement_rule_json=json.dumps(dict(platform_rate=20,
            provider_rate=75, service_rate=0, expert_rate=0, channel_rate=5)))
        session.add(product); session.flush()
        session.add(m.ProductReleaseVersion(id=version_id, product_id=product_id, version_code="v1", price=12, cost=7, status="active"))
        session.flush()
        m.app.dependency_overrides[m.db_session] = lambda: session
        client = TestClient(m.app, raise_server_exceptions=True)
        try:
            headers = {"Authorization": "Bearer " + m.issue_token(actor)}
            image = BytesIO()
            Image.new("RGB", (380, 280), (28, 132, 91)).save(image, "PNG")
            archive = BytesIO()
            with zipfile.ZipFile(archive, "w") as dataset:
                dataset.writestr("sample.csv", "quality,value\nhealthy,1\n")
            for role, name, content, content_type in (
                ("product_logo", "logo.png", image.getvalue(), "image/png"),
                ("product_data", "dataset.zip", archive.getvalue(), "application/zip"),
            ):
                uploaded = client.post("/api/files/upload", headers=headers,
                    data={"product_id": product_id, "version_id": version_id, "file_role": role},
                    files={"upload": (name, content, content_type)})
                assert uploaded.status_code == 200, uploaded.text
                assert uploaded.json()["clamav_status"] == "clean"
            m.trading_policy["validate_submission"](session, product)
            product.status = "published"; session.flush()
            public = client.get(f"/api/storefront/products/{product_id}")
            assert public.status_code == 200, public.text
            assert client.get(public.json()["logo_url"]).status_code == 200
            body = dict(product_id=product_id, product_version_id=version_id,
                        buyer_enterprise_id=selected.id, subscription_months=1)
            quoted = client.post("/api/orders/quote", headers=headers, json=body)
            assert quoted.status_code == 200, quoted.text
            quote = quoted.json()
            assert quote["amount"] == 12 and "cost" not in json.dumps(quote)
            created = client.post("/api/orders", headers=headers, json={**body, "quote_id": quote["quote_id"]})
            assert created.status_code == 200, created.text
            order_id = created.json()["id"]
            order = session.get(m.Order, order_id)
            assert order.buyer_enterprise_id == selected.id and order.snapshot_version == 1
            assert m.order_cost(session, order) == m.Decimal("7.00")
            payment = session.scalar(select(m.Payment).where(m.Payment.order_id == order.id))
            assert payment.amount == 12
            version = session.get(m.ProductReleaseVersion, version_id)
            version.price, version.cost = 30, 20
            product.settlement_rule_json = json.dumps(dict(platform_rate=10, provider_rate=90,
                service_rate=0, expert_rate=0, channel_rate=0))
            session.commit()
            assert m.order_cost(session, order) == m.Decimal("7.00")
            rates, rule_version = m.settlement_values_for_order(session, order, None)
            assert rates["platform_rate"] == 20 and rates["channel_rate"] == 5
            for path in ("/api/orders", f"/api/orders/{order_id}"):
                details = client.get(path, headers=headers)
                assert details.status_code == 200, details.text
                assert "cost" not in json.dumps(details.json())
                assert "upstream_url" not in json.dumps(details.json())
            stale = client.post("/api/orders", headers=headers, json={**body, "quote_id": quote["quote_id"]})
            assert stale.status_code == 409, stale.text
            count = session.scalar(select(m.func.count()).select_from(m.Order).where(m.Order.product_id == product_id))
            assert count == 1
            return {"status": "passed", "database": m.engine.dialect.name,
                    "checks": ["real file/Logo ClamAV uploads and MinIO read", "PG order/payment snapshots",
                    "explicit second buyer enterprise", "buyer cost/URL redaction", "cost/rule immutability",
                    "stale quote rejection without duplicate order"], "fixture": "outer transaction rolled back"}
        finally:
            client.close()
    finally:
        if previous is None: m.app.dependency_overrides.pop(m.db_session, None)
        else: m.app.dependency_overrides[m.db_session] = previous
        session.close(); outer.rollback(); connection.close()
        for item in storage.list_objects(m.MINIO_BUCKET, prefix=actor_id + "/", recursive=True):
            storage.remove_object(m.MINIO_BUCKET, item.object_name)


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
