"""Disposable real-service browser fixture; never modifies existing products/users."""
import argparse
import json
from pathlib import Path
import secrets
import zipfile
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, or_, select
from app import main as m


def cleanup(state):
    with m.SessionLocal() as db:
        orders = db.scalars(select(m.Order).where(m.Order.product_id == state["product_id"])).all()
        assert all(not o.paid_amount for o in orders), "Paid fixture orders require manual cleanup"
        ids = [o.id for o in orders]
        files = db.scalars(select(m.FileObject).where(m.FileObject.product_id == state["product_id"])).all()
        file_ids = [f.id for f in files]
        targets = ids + file_ids + state["version_ids"] + [state["product_id"]]
        messages = m.message_center["Message"]
        receipt = m.message_center["Receipt"]
        delivery = m.message_center["Delivery"]
        message_ids = db.scalars(select(messages.id).where(messages.target_id.in_(targets))).all()
        delivery_ids = select(delivery.id).where(delivery.message_id.in_(message_ids))
        db.execute(delete(m.message_center["DeliveryAttempt"]).where(m.message_center["DeliveryAttempt"].delivery_id.in_(delivery_ids)))
        db.execute(delete(m.message_center["ExpiredUrgent"]).where(m.message_center["ExpiredUrgent"].message_id.in_(message_ids)))
        links = m.Base.metadata.tables["notification_message_attachments"]
        db.execute(delete(links).where(links.c.message_id.in_(message_ids)))
        db.execute(delete(receipt).where(receipt.message_id.in_(message_ids)))
        db.execute(delete(delivery).where(delivery.message_id.in_(message_ids)))
        db.execute(delete(messages).where(messages.id.in_(message_ids)))
        db.execute(delete(m.message_center["Preference"]).where(m.message_center["Preference"].user_id.in_(state["user_ids"])))
        db.execute(delete(m.AuditLog).where(or_(m.AuditLog.target_id.in_(targets), m.AuditLog.actor.in_(state["emails"]))))
        for model in (m.OrderStateLog, m.Payment, m.SettlementMeasurement):
            db.execute(delete(model).where(model.order_id.in_(ids)))
        db.execute(delete(m.Order).where(m.Order.id.in_(ids)))
        db.execute(delete(m.FileDownloadLog).where(m.FileDownloadLog.file_id.in_(file_ids)))
        db.execute(delete(m.ProductSecurityScan).where(m.ProductSecurityScan.product_id == state["product_id"]))
        db.execute(delete(m.FileObject).where(m.FileObject.id.in_(file_ids)))
        db.execute(delete(m.ProductReleaseVersion).where(m.ProductReleaseVersion.id.in_(state["version_ids"])))
        db.execute(delete(m.Product).where(m.Product.id == state["product_id"]))
        db.execute(delete(m.Membership).where(m.Membership.user_id.in_(state["user_ids"])))
        db.execute(delete(m.Enterprise).where(m.Enterprise.id.in_(state["enterprise_ids"])))
        db.execute(delete(m.User).where(m.User.id.in_(state["user_ids"])))
        db.commit()
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY, secret_key=m.MINIO_SECRET_KEY, secure=False)
    for owner in state["user_ids"]:
        for obj in storage.list_objects(m.MINIO_BUCKET, prefix=owner + "/", recursive=True):
            storage.remove_object(m.MINIO_BUCKET, obj.object_name)
    return {"status": "cleaned", "orders": len(ids), "files": len(file_ids)}


def prepare(path, offline=False):
    marker = secrets.token_hex(8)
    prefix = "qa-web-" + marker
    state = {"product_id": prefix + "-p", "version_ids": [prefix + "-v1", prefix + "-v2"],
        "user_ids": [prefix + "-u1", prefix + "-u2"],
        "enterprise_ids": [prefix + "-e1", prefix + "-e2", prefix + "-ep"],
        "emails": [marker + "-buyer@example.invalid", marker + "-provider@example.invalid"]}
    path.write_text(json.dumps(state), encoding="utf-8"); path.chmod(0o600)
    try:
        with m.SessionLocal() as db:
            buyer, provider = [m.User(id=id, email=email, name=name, password_hash="not-a-login-password",
                verified_status="verified", activation_status="active") for id, email, name in
                zip(state["user_ids"], state["emails"], ("Temporary browser buyer", "Temporary browser provider"))]
            enterprises = [m.Enterprise(id=id, name=name, credit_code="QA" + marker + str(i), verification_status="verified")
                for i, (id, name) in enumerate(zip(state["enterprise_ids"],
                ("浏览器验收企业甲", "浏览器验收企业乙", "浏览器验收提供方")))]
            db.add_all([buyer, provider, *enterprises]); db.flush()
            db.add_all([m.Membership(user_id=buyer.id, enterprise_id=e.id, role="super_admin") for e in enterprises[:2]])
            db.add(m.Membership(user_id=provider.id, enterprise_id=enterprises[2].id, role="super_admin"))
            product = m.Product(id=state["product_id"], enterprise_id=enterprises[2].id, name="商城浏览器隔离验收商品",
                product_type="consulting" if offline else "dataset", delivery_method="consulting" if offline else "file", status="draft", provider_name=enterprises[2].name,
                description="隔离验收商品，验证完成后自动删除", settlement_rule_mode="custom",
                settlement_rule_json=json.dumps(dict(platform_rate=20, provider_rate=80, service_rate=0, expert_rate=0, channel_rate=0)))
            db.add(product); db.flush()
            for i, id in enumerate(state["version_ids"]):
                db.add(m.ProductReleaseVersion(id=id, product_id=product.id, version_code=f"v{i+1}",
                    description=f"验收版本{i+1}", price=12 + i * 10, cost=7 + i * 3, status="active"))
            db.commit()
            provider_token, buyer_token = m.issue_token(provider), m.issue_token(buyer)
        image = BytesIO(); Image.new("RGB", (380, 280), (28, 132, 91)).save(image, "PNG")
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as dataset: dataset.writestr("sample.csv", "quality,value\nhealthy,1\n")
        client = TestClient(m.app)
        try:
            headers = {"Authorization": "Bearer " + provider_token}
            for role, version_id, name, content, content_type in [
                ("product_logo", "", "logo.png", image.getvalue(), "image/png"),
                *[("product_data", id, "sample.zip", archive.getvalue(), "application/zip") for id in state["version_ids"]],
            ]:
                response = client.post("/api/files/upload", headers=headers,
                    data={"product_id": state["product_id"], "version_id": version_id, "file_role": role},
                    files={"upload": (name, content, content_type)})
                assert response.status_code == 200, response.text
                assert response.json()["clamav_status"] == "clean"
        finally:
            client.close()
        with m.SessionLocal() as db:
            product = db.get(m.Product, state["product_id"])
            m.trading_policy["validate_submission"](db, product)
            product.status = "published"; db.commit()
        state["token"] = buyer_token
        state["provider_token"] = provider_token
        if offline:
            client = TestClient(m.app)
            try:
                response = client.post("/api/orders", headers={"Authorization": "Bearer " + buyer_token},
                    json={"product_id": state["product_id"], "product_version_id": state["version_ids"][0],
                          "buyer_enterprise_id": state["enterprise_ids"][0]})
                assert response.status_code == 200, response.text
                state["order_id"] = response.json()["id"]
                assert response.json()["main_status"] == "pending_provider_review"
            finally:
                client.close()
        path.write_text(json.dumps(state), encoding="utf-8"); path.chmod(0o600)
        return {"status": "prepared", "product_id": state["product_id"], "token_file": str(path)}
    except Exception:
        cleanup(state); path.unlink(missing_ok=True); raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "cleanup"))
    parser.add_argument("--state", default="/tmp/trd-phase2-fixture.json")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(); path = Path(args.state)
    if args.action == "prepare":
        assert not path.exists(), "Clean up the previous fixture first"
        result = prepare(path, args.offline)
    else:
        result = cleanup(json.loads(path.read_text(encoding="utf-8")))
        path.unlink()
    print(json.dumps(result, ensure_ascii=False))
