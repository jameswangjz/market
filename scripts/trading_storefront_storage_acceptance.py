"""Real PG/MinIO/ClamAV storefront check, with rolled-back database fixtures."""
import json
import secrets
import zipfile
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session
from app import main as m


def run():
    marker = secrets.token_hex(12)
    product_id = "trd-qa-" + marker
    image = BytesIO()
    Image.new("RGB", (380, 280), (28, 132, 91)).save(image, "PNG")
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as dataset:
        dataset.writestr("sample.csv", "quality,value\nhealthy,1\n")
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY,
                      secret_key=m.MINIO_SECRET_KEY, secure=False)
    connection = m.engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    previous = m.app.dependency_overrides.get(m.db_session)
    try:
        enterprise = m.Enterprise(id="qa-" + marker, name="TRD isolated " + marker,
                                  credit_code="QA" + marker)
        actor = m.User(id="qa-u-" + marker, email=marker + "@example.invalid", name="Isolated QA",
                       password_hash="not-a-login-password", verified_status="verified", activation_status="active")
        session.add_all([enterprise, actor])
        session.flush()
        session.add(m.Membership(user_id=actor.id, enterprise_id=enterprise.id, role="super_admin"))
        product = m.Product(id=product_id, enterprise_id=enterprise.id,
                            name="TRD isolated storefront", provider_name=enterprise.name,
                            product_type="dataset", delivery_method="file", status="draft")
        session.add(product)
        session.flush()
        session.add(m.ProductReleaseVersion(id="qa-v-" + marker, product_id=product_id,
                                             version_code="v1", price=12, cost=7, status="active"))
        session.flush()
        m.app.dependency_overrides[m.db_session] = lambda: session
        client = TestClient(m.app, raise_server_exceptions=True)
        try:
            headers = {"Authorization": "Bearer " + m.issue_token(actor)}
            upload = client.post("/api/files/upload", headers=headers,
                                 data={"product_id": product_id, "file_role": "product_logo"},
                                 files={"upload": ("logo.png", image.getvalue(), "image/png")})
            assert upload.status_code == 200, upload.text
            assert upload.json()["clamav_status"] == "clean"
            upload_data = client.post("/api/files/upload", headers=headers,
                                      data={"product_id": product_id, "file_role": "product_data", "version_id": "qa-v-" + marker},
                                      files={"upload": ("dataset.zip", archive.getvalue(), "application/zip")})
            assert upload_data.status_code == 200, upload_data.text
            assert upload_data.json()["clamav_status"] == "clean"
            assert session.get(m.FileObject, upload_data.json()["id"]).version_id == "qa-v-" + marker
            product = session.get(m.Product, product_id)
            assert product.logo_file_id and product.logo_thumbnail_file_id
            m.trading_policy["validate_submission"](session, product)
            product.status = "published"
            session.flush()
            response = client.get(f"/api/storefront/products/{product_id}")
            assert response.status_code == 200, response.text
            data = response.json()
            assert "cost" not in json.dumps(data) and "upstream_url" not in data
            logo = client.get(data["logo_url"])
            assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
            with Image.open(BytesIO(logo.content)) as rendered:
                assert rendered.size == (190, 140)
            assert client.get(f"/api/files/{upload_data.json()['id']}/download").status_code == 401
        finally:
            client.close()
        return {"status": "passed", "database": m.engine.dialect.name,
                "checks": ["real 380x280 logo upload/ClamAV", "real version-bound ZIP upload/ClamAV",
                           "real PG transaction/savepoints", "submission gate", "anonymous public DTO",
                           "real MinIO thumbnail delivery", "private file requires authentication"],
                "fixture": "rolled back; temporary object removed"}
    finally:
        if previous is None:
            m.app.dependency_overrides.pop(m.db_session, None)
        else:
            m.app.dependency_overrides[m.db_session] = previous
        session.close()
        transaction.rollback()
        connection.close()
        for item in storage.list_objects(m.MINIO_BUCKET, prefix="qa-u-" + marker + "/", recursive=True):
            storage.remove_object(m.MINIO_BUCKET, item.object_name)


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
