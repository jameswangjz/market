"""Focused anonymous HTTP checks against isolated production models, never dev data."""
import importlib
import json
import sys
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_message_business_events import load_isolated_main


class StorefrontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_isolated_main()
        cls.install = staticmethod(importlib.import_module(cls.m.__package__ + ".storefront").install)

    @classmethod
    def tearDownClass(cls):
        cls.m.engine.dispose()
        for name in list(sys.modules):
            if name == cls.m.__package__ or name.startswith(cls.m.__package__ + "."):
                del sys.modules[name]

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        self.Session = sessionmaker(bind=self.engine)
        self.m.Base.metadata.create_all(self.engine)
        with self.Session() as db:
            db.add(self.m.Enterprise(id="provider", name="Provider", credit_code="isolated"))
            db.commit()

        def db_session():
            with self.Session() as db:
                yield db

        self.ns = dict(vars(self.m), app=FastAPI(), db_session=db_session,
                       apisix_enabled=Mock(return_value=False), MINIO_ENDPOINT="isolated:9000",
                       Minio=Mock(side_effect=AssertionError("Unmocked MinIO forbidden")))
        self.install(self.ns)
        self.client = TestClient(self.ns["app"])
        self.addCleanup(self.client.close)
        self.product("file")

    def product(self, pid, **values):
        fields = dict(id=pid, enterprise_id="provider", name="Name " + pid,
                      product_type="dataset", provider_name="Provider", description="Public description",
                      catalog_name="Data", delivery_method="file", status="published",
                      updated_at=datetime(2026, 10, 10, tzinfo=timezone.utc),
                      upstream_url="https://private.invalid", review_comment="PRIVATE REVIEW",
                      settlement_rule_json='{"secret_rule":"PRIVATE RULE"}')
        fields.update(values)
        with self.Session() as db:
            db.add(self.m.Product(**fields))
            db.flush()
            db.add(self.m.ProductReleaseVersion(id=pid + "-v1", product_id=pid,
                   version_code="v1", price=100, cost=10, status="active"))
            db.commit()

    def update(self, model, key, **values):
        with self.Session() as db:
            for name, value in values.items():
                setattr(db.get(model, key), name, value)
            db.commit()

    def version(self, pid, vid, **values):
        fields = dict(id=vid, product_id=pid, version_code=vid, price=50, cost=10, status="active")
        fields.update(values)
        with self.Session() as db:
            db.add(self.m.ProductReleaseVersion(**fields))
            db.commit()

    def route(self, pid="api", **values):
        fields = dict(id=pid + "-route", product_id=pid, route_key=pid, upstream_url="https://upstream.invalid",
                      version="v1", status="active", upstream_client_id="PRIVATE CLIENT",
                      upstream_client_secret="PRIVATE SECRET", health_message="\u5065\u5eb7\u68c0\u67e5\u901a\u8fc7\uff08HTTP 200\uff09")
        fields.update(values)
        with self.Session() as db:
            db.add(self.m.ApiGatewayRoute(**fields))
            db.commit()

    def get(self, path="", **params):
        response = self.client.get("/api/storefront/products" + path, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_public_contract_whitelist_minimum_price_and_no_writes(self):
        self.version("file", "cheap", price=20, cost=5)
        self.version("file", "inactive", price=1, cost=0, status="draft")
        self.version("file", "invalid", price=-1, cost=0)
        self.version("file", "under-cost", price=2, cost=3)
        self.version("file", "blank", version_code=" ")
        self.update(self.m.Product, "file", price=0)
        with self.Session() as db:
            before = {table.name: db.scalar(select(func.count()).select_from(table))
                      for table in self.m.Base.metadata.sorted_tables}
        data = self.get()
        self.assertEqual(set(data), {"items", "total", "page", "page_size"})
        dto = data["items"][0]
        self.assertEqual(set(dto), {"id", "name", "provider_name", "description", "delivery_method",
                         "product_type", "logo_url", "versions", "minimum_price", "price_unit"})
        self.assertEqual(dto["minimum_price"], 20)
        self.assertEqual(dto["price_unit"], "order")
        self.assertEqual({v["id"] for v in dto["versions"]}, {"file-v1", "cheap"})
        for version in dto["versions"]:
            self.assertEqual(set(version), {"id", "version_code", "description", "price"})
        self.assertEqual(self.get("/file"), dto)
        self.assertNotIn("PRIVATE", json.dumps(dto))
        with self.Session() as db:
            after = {table.name: db.scalar(select(func.count()).select_from(table))
                     for table in self.m.Base.metadata.sorted_tables}
        self.assertEqual(after, before)

    def test_hidden_states_missing_and_no_active_versions_are_404(self):
        for state in ("draft", "rejected", "security_unpublished", "unpublished", "operation_review"):
            self.product(state, status=state)
            self.assertEqual(self.client.get("/api/storefront/products/" + state).status_code, 404)
        self.product("empty")
        self.update(self.m.ProductReleaseVersion, "empty-v1", status="disabled")
        self.assertEqual(self.client.get("/api/storefront/products/empty").status_code, 404)
        self.assertEqual(self.client.get("/api/storefront/products/missing").status_code, 404)
        self.assertEqual(self.get()["total"], 1)

    def test_search_category_aliases_pagination_and_literal_wildcards(self):
        self.product("a", name="100% data", description="Alpha", catalog_name="Special")
        self.product("b", name="Other", provider_name="Unique provider", product_type="consulting",
                     delivery_method="consulting")
        self.assertEqual(self.get(q="unique")["items"][0]["id"], "b")
        self.assertEqual(self.get(q="alpha")["items"][0]["id"], "a")
        self.assertEqual(self.get(q="%")["total"], 1)
        self.assertEqual(self.get(q="_")["total"], 0)
        self.assertEqual(self.get(type="dataset", catalog="Special")["items"][0]["id"], "a")
        self.assertEqual(self.get(product_type="dataset", catalog_name="Special")["total"], 1)
        pages = [self.get(page=i, page_size=1) for i in range(1, 5)]
        self.assertEqual([p["total"] for p in pages], [3] * 4)
        self.assertEqual([p["items"][0]["id"] for p in pages[:3]], ["a", "b", "file"])
        self.assertEqual(pages[3]["items"], [])
        self.assertEqual(self.get(q="missing")["total"], 0)

    def test_invalid_pagination_is_rejected(self):
        for params in ({"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "bad"}, {"q": "x" * 241}):
            self.assertEqual(self.client.get("/api/storefront/products", params=params).status_code, 422)

    def test_gateway_requires_health_auth_and_matching_version(self):
        self.product("api", product_type="api", delivery_method="api")
        self.version("api", "unconfigured", price=1, cost=0)
        self.assertEqual(self.get()["total"], 1)
        self.route()
        dto = self.get("/api")
        self.assertEqual(dto["price_unit"], "month")
        self.assertEqual([v["id"] for v in dto["versions"]], ["api-v1"])
        for field, value in (("status", "pending_health"), ("status", "publish_failed"),
                             ("health_message", ""), ("health_message", "HTTP 500"),
                             ("upstream_url", "file:///data"), ("upstream_client_secret", ""),
                             ("version", "missing")):
            with self.subTest(field=field, value=value):
                with self.Session() as db:
                    previous = getattr(db.get(self.m.ApiGatewayRoute, "api-route"), field)
                self.update(self.m.ApiGatewayRoute, "api-route", **{field: value})
                self.assertEqual(self.client.get("/api/storefront/products/api").status_code, 404)
                self.assertEqual(self.get(page_size=1)["total"], 1)
                self.update(self.m.ApiGatewayRoute, "api-route", **{field: previous})
        self.ns["Minio"].assert_not_called()

    def test_model_and_api_delivery_cannot_bypass_gateway(self):
        for pid, kind, method in (("model", "model", "model_api"), ("alias", "dataset", "api")):
            self.product(pid, product_type=kind, delivery_method=method)
            self.assertEqual(self.client.get("/api/storefront/products/" + pid).status_code, 404)
        self.route("model")
        self.assertEqual(self.get("/model")["price_unit"], "month")
        self.product("file-model", product_type="model", delivery_method="file")
        self.assertEqual(self.get("/file-model")["price_unit"], "order")

    def test_apisix_latest_publish_and_snapshot_must_match(self):
        self.product("api", product_type="api", delivery_method="api")
        self.route()
        self.ns["apisix_enabled"].return_value = True
        self.assertEqual(self.client.get("/api/storefront/products/api").status_code, 404)
        with self.Session() as db:
            payload = self.m.apisix_route_payload(db.get(self.m.ApiGatewayRoute, "api-route"),
                                                db.get(self.m.Product, "api"), db)
            db.add(self.m.GatewayConfigRevision(id="revision", route_id="api-route", product_id="api",
                   revision=1, status="active", config_json=json.dumps(payload)))
            db.flush()
            db.add(self.m.GatewayPublishRecord(id="record", route_id="api-route", revision_id="revision",
                   status="succeeded", completed_at=datetime.now(timezone.utc)))
            db.commit()
        self.assertEqual(self.get("/api")["id"], "api")
        for value in ("{}", "not-json"):
            self.update(self.m.GatewayConfigRevision, "revision", config_json=value)
            self.assertEqual(self.client.get("/api/storefront/products/api").status_code, 404)
        self.update(self.m.GatewayConfigRevision, "revision", config_json=json.dumps(payload))
        self.update(self.m.GatewayPublishRecord, "record", status="failed")
        self.assertEqual(self.client.get("/api/storefront/products/api").status_code, 404)
        self.update(self.m.GatewayPublishRecord, "record", status="succeeded")
        with self.Session() as db:
            db.add(self.m.GatewayConfigRevision(id="new", route_id="api-route", product_id="api",
                   revision=2, status="failed", config_json=json.dumps(payload)))
            db.commit()
        self.assertEqual(self.client.get("/api/storefront/products/api").status_code, 404)

    def test_saas_uses_active_monthly_versions_and_configuration(self):
        self.product("saas", product_type="saas", delivery_method="tenant_access",
                     application_url="https://app.invalid")
        self.assertEqual(self.client.get("/api/storefront/products/saas").status_code, 404)
        with self.Session() as db:
            db.add(self.m.SaaSIntegrationConfig(product_id="saas", status="active", base_url="https://saas.invalid",
                   token_url="https://saas.invalid/token", client_id="PRIVATE", client_secret="PRIVATE"))
            db.add(self.m.SaaSProductVersion(id="saas-v", product_id="saas", version_code="monthly", name="Basic",
                   monthly_price=30, annual_price=1, cost=5, status="active"))
            db.add(self.m.SaaSProductVersion(id="saas-draft", product_id="saas", version_code="draft", name="Draft",
                   monthly_price=1, cost=0, status="draft"))
            db.commit()
        dto = self.get("/saas")
        self.assertEqual(dto["minimum_price"], 30)
        self.assertEqual(dto["price_unit"], "month")
        self.assertEqual([v["id"] for v in dto["versions"]], ["saas-v"])

    def logo(self, **values):
        png = BytesIO()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("private", "PRIVATE METADATA")
        Image.new("RGBA", (190, 140), "red").save(png, "PNG", pnginfo=metadata)
        self.png = png.getvalue()
        fields = dict(id="thumb", owner_id="owner", product_id="file", version_id="file-v1",
                      object_name="isolated/thumb.png", original_name="thumb.png", size=len(self.png),
                      file_role="product_logo_thumbnail", content_type="image/png", status="active", scan_status="clean")
        fields.update(values)
        with self.Session() as db:
            db.add(self.m.FileObject(**fields))
            db.get(self.m.Product, "file").logo_thumbnail_file_id = "thumb"
            db.commit()
        self.stream = Mock()
        self.stream.read.return_value = self.png
        self.ns["Minio"] = Mock()
        self.ns["Minio"].return_value.get_object.return_value = self.stream

    def test_public_logo_minio_read_cleanup_and_metadata_removed(self):
        self.logo()
        url = self.get("/file")["logo_url"]
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(b"PRIVATE METADATA", response.content)
        self.ns["Minio"].return_value.get_object.assert_called_once_with(self.ns["MINIO_BUCKET"], "isolated/thumb.png")
        self.stream.close.assert_called_once()
        self.stream.release_conn.assert_called_once()

    def test_logo_denies_data_original_unsafe_foreign_deleted_and_inactive_version(self):
        self.product("other")
        self.logo()
        for field, value in (("file_role", "product_data"), ("file_role", "product_logo"),
                             ("scan_status", "infected"), ("scan_status", "not_scanned"),
                             ("status", "deleted"), ("status", "disabled"), ("product_id", "other"),
                             ("content_type", "image/svg+xml"), ("size", 1024 * 1024 + 1),
                             ("version_id", "other-v1")):
            with self.subTest(field=field, value=value):
                with self.Session() as db:
                    previous = getattr(db.get(self.m.FileObject, "thumb"), field)
                self.update(self.m.FileObject, "thumb", **{field: value})
                self.assertEqual(self.get("/file")["logo_url"], "")
                self.assertEqual(self.client.get("/api/storefront/products/file/logo").status_code, 404)
                self.update(self.m.FileObject, "thumb", **{field: previous})
        self.ns["Minio"].assert_not_called()
        self.update(self.m.ProductReleaseVersion, "file-v1", status="disabled")
        self.assertEqual(self.client.get("/api/storefront/products/file/logo").status_code, 404)

    def test_logo_revocation_and_arbitrary_file_queries_do_not_grant_access(self):
        self.logo()
        with self.Session() as db:
            db.add(self.m.FileObject(id="data", owner_id="owner", product_id="file", object_name="PRIVATE DATA",
                   original_name="data.csv", file_role="product_data", status="active", scan_status="clean"))
            db.commit()
        response = self.client.get("/api/storefront/products/file/logo", params={"file_id": "data"})
        self.assertEqual(response.status_code, 200)
        self.ns["Minio"].return_value.get_object.assert_called_once_with(self.ns["MINIO_BUCKET"], "isolated/thumb.png")
        self.update(self.m.Product, "file", status="security_unpublished")
        self.assertEqual(self.client.get("/api/storefront/products/file/logo").status_code, 404)
        self.assertEqual(self.client.get("/api/storefront/products/data/logo").status_code, 404)

    def test_logo_missing_storage_read_errors_and_fake_png_fail_closed(self):
        self.logo()
        self.ns["MINIO_ENDPOINT"] = ""
        self.assertEqual(self.client.get("/api/storefront/products/file/logo").status_code, 503)
        self.ns["MINIO_ENDPOINT"] = "isolated:9000"
        self.ns["Minio"].return_value.get_object.side_effect = RuntimeError("PRIVATE STORAGE ERROR")
        response = self.client.get("/api/storefront/products/file/logo")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("PRIVATE", response.text)
        self.ns["Minio"].return_value.get_object.side_effect = None
        self.stream.read.side_effect = RuntimeError("read failed")
        self.assertEqual(self.client.get("/api/storefront/products/file/logo").status_code, 404)
        self.stream.close.assert_called_once()
        self.stream.release_conn.assert_called_once()

        self.stream.read.side_effect = None
        for content in (b"data-file", b"<svg onload='alert(1)'/>"):
            self.stream.read.return_value = content
            self.update(self.m.FileObject, "thumb", size=len(content))
            self.assertEqual(self.client.get("/api/storefront/products/file/logo").status_code, 404)

    def test_clean_draft_thumbnail_is_public(self):
        self.logo(status="draft")
        self.assertEqual(self.get("/file")["logo_url"], "/api/storefront/products/file/logo")
        self.assertEqual(self.client.get("/api/storefront/products/file/logo").status_code, 200)


if __name__ == "__main__":
    unittest.main()
