"""Real PG/MinIO/ClamAV storefront check, with rolled-back database fixtures."""
import hashlib
import json
import secrets
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session
from app import main as m


def run():
    marker = secrets.token_hex(12)
    product_id = "trd-qa-" + marker
    object_name = f"isolated-qa/trading/{marker}.png"
    image = BytesIO()
    Image.new("RGB", (380, 280), (28, 132, 91)).save(image, "PNG")
    virus_status, _ = m.clamav_scan_stream(BytesIO(image.getvalue()), len(image.getvalue()))
    assert virus_status == "clean", virus_status
    thumbnail = BytesIO()
    Image.new("RGB", (190, 140), (28, 132, 91)).save(thumbnail, "PNG")
    content = thumbnail.getvalue()
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY,
                      secret_key=m.MINIO_SECRET_KEY, secure=False)
    storage.put_object(m.MINIO_BUCKET, object_name, BytesIO(content), len(content), content_type="image/png")
    connection = m.engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    previous = m.app.dependency_overrides.get(m.db_session)
    try:
        enterprise = m.Enterprise(id="qa-" + marker, name="TRD isolated " + marker,
                                  credit_code="QA" + marker)
        session.add(enterprise)
        session.flush()
        product = m.Product(id=product_id, enterprise_id=enterprise.id,
                            name="TRD isolated storefront", provider_name=enterprise.name,
                            product_type="dataset", delivery_method="file", status="published")
        session.add(product)
        session.flush()
        session.add(m.ProductReleaseVersion(id="qa-v-" + marker, product_id=product_id,
                                             version_code="v1", price=12, cost=7, status="active"))
        file = m.FileObject(id="qa-f-" + marker, owner_id="isolated-qa", product_id=product_id,
                            object_name=object_name, original_name="thumbnail.png", content_type="image/png",
                            size=len(content), checksum=hashlib.sha256(content).hexdigest(),
                            file_role="product_logo_thumbnail", scan_status="clean", status="active")
        session.add(file)
        session.flush()
        product.logo_thumbnail_file_id = file.id
        session.flush()
        m.app.dependency_overrides[m.db_session] = lambda: session
        client = TestClient(m.app, raise_server_exceptions=True)
        try:
            response = client.get(f"/api/storefront/products/{product_id}")
            assert response.status_code == 200, response.text
            data = response.json()
            assert "cost" not in json.dumps(data) and "upstream_url" not in data
            logo = client.get(data["logo_url"])
            assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
            with Image.open(BytesIO(logo.content)) as rendered:
                assert rendered.size == (190, 140)
            assert client.get(f"/api/files/{file.id}/download").status_code == 401
        finally:
            client.close()
        return {"status": "passed", "database": m.engine.dialect.name,
                "checks": ["real ClamAV clean", "real PG transaction", "anonymous public DTO",
                           "real MinIO PNG delivery", "private file requires authentication"],
                "fixture": "rolled back; temporary object removed"}
    finally:
        if previous is None:
            m.app.dependency_overrides.pop(m.db_session, None)
        else:
            m.app.dependency_overrides[m.db_session] = previous
        session.close()
        transaction.rollback()
        connection.close()
        storage.remove_object(m.MINIO_BUCKET, object_name)


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
