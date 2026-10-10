"""Isolated production ORM and publisher tests; no external services."""
import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_message_business_events import load_isolated_main


class VersionGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_isolated_main()
        cls.ns = vars(cls.m)
        cls.install = staticmethod(importlib.import_module(cls.m.__package__ + ".version_gateway").install)
        cls.hooks = cls.install(cls.ns)
        cls.Config = cls.hooks["Config"]

    @classmethod
    def tearDownClass(cls):
        cls.m.engine.dispose()
        for name in list(sys.modules):
            if name == cls.m.__package__ or name.startswith(cls.m.__package__ + "."):
                del sys.modules[name]

    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")

        self.m.Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.addCleanup(self.db.close)
        self.user = self.m.User(id="operator", email="ops@example.invalid", platform_role="super_admin")
        self.db.add(self.m.Enterprise(id="provider", name="Provider", credit_code="test"))
        self.db.flush()
        self.product = self.m.Product(id="p", enterprise_id="provider", name="API",
            product_type="api", delivery_method="api", status="pending_integration")
        self.db.add(self.product)
        self.db.flush()
        self.version = self.m.ProductReleaseVersion(id="v", product_id="p", version_code="v1", price=10, cost=1)
        self.route = self.m.ApiGatewayRoute(id="r", product_id="p", route_key="p-v1",
            version="v1", upstream_url="https://provider.invalid", upstream_auth_mode="none",
            status="draft")
        self.db.add_all([self.version, self.route])
        self.db.flush()
        self.http = Mock(return_value=Mock(status_code=204))
        self.ns["validated_http_request"] = self.http
        self.ns["apisix_enabled"] = Mock(return_value=True)
        self.ns["publish_apisix_route"] = self.m.publish_apisix_route
        self.addCleanup(patch.stopall)
        patch.object(self.m, "apisix_enabled", return_value=True).start()
        patch.object(self.m, "apisix_native_upstream", return_value=False).start()
        patch.object(self.m, "apisix_native_auth", return_value=False).start()
        self.admin = patch.object(self.m, "apisix_admin_request", return_value={"key": "route"}).start()
        patch.object(self.m.httpx, "Client", side_effect=AssertionError("Raw HTTP forbidden")).start()
        patch.object(self.m.httpx, "request", side_effect=AssertionError("Raw HTTP forbidden")).start()
        self.body = dict(upstream_url=self.route.upstream_url, route_key=self.route.route_key,
                        version="v1", upstream_auth_mode="none")

    def save(self, **changes):
        return self.hooks["save_config"](self.db, self.product, "v", self.body | changes, self.user)

    def verify(self):
        return self.hooks["verify_version"](self.db, self.product, "v", self.user)

    def ready(self):
        return self.hooks["version_ready"](self.db, self.product, self.version)

    def assert_http(self, code, call):
        with self.assertRaises(HTTPException) as error:
            call()
        self.assertEqual(error.exception.status_code, code)

    def test_install_idempotent_and_shared_metadata(self):
        self.assertIs(self.install(self.ns), self.hooks)
        self.assertIs(self.Config.metadata, self.m.Base.metadata)

    def test_review_gate_and_no_commit_rollback(self):
        self.product.status = "operation_review"
        self.assertEqual(self.hooks["on_review_complete"](self.db, self.product, self.user), "pending_integration")
        self.assert_http(409, lambda: self.hooks["on_review_complete"](self.db, self.product, self.user))
        self.save()
        self.db.rollback()
        self.assertEqual(self.db.scalars(select(self.Config)).all(), [])

    def test_non_gateway_review_and_unsupported_saas_fail_closed(self):
        self.product.status, self.product.delivery_method = "operation_review", "file"
        self.assertEqual(self.hooks["on_review_complete"](self.db, self.product, self.user), "published")
        self.product.delivery_method = "tenant_access"
        self.assertFalse(self.hooks["requires_integration"](self.product))
        self.assertFalse(self.ready())
        self.assert_http(409, lambda: self.save())

    def test_real_publisher_evidence_and_public_whitelist(self):
        config = self.save()
        self.assertEqual(self.verify().status, "verified")
        self.http.assert_called_once_with("GET", "https://provider.invalid/health",
                                          timeout=10.0, follow_redirects=False)
        self.admin.assert_called_once()
        self.assertTrue(self.ready())
        self.hooks["require_ready"](self.db, self.product)
        self.assertEqual(self.product.status, "published")
        self.product.status = "published"
        public = self.hooks["public_versions"](self.db, self.product)
        self.assertEqual(public, [dict(id="v", version_code="v1", description="", price=10.0)])
        self.assertNotIn("provider.invalid", json.dumps(public))
        self.assertIsNotNone(config.verified_at)
        self.assertIsNotNone(config.publish_record_id)

    def test_http_redirect_failure_and_retry(self):
        self.save()
        self.http.return_value.status_code = 302
        self.assertEqual(self.verify().status, "failed")
        self.assertFalse(self.ready())
        self.admin.assert_not_called()
        self.http.return_value.status_code = 200
        self.assertEqual(self.verify().status, "verified")

    def test_adapter_missing_or_allowlist_rejection_never_publish(self):
        self.save()
        self.ns["validated_http_request"] = None
        self.assert_http(503, self.verify)
        self.ns["validated_http_request"] = self.http
        self.http.side_effect = ValueError("Private destination is not allowlisted")
        config = self.verify()
        self.assertEqual(config.status, "failed")
        self.assertFalse(config.healthy)
        self.admin.assert_not_called()

    def test_apisix_disabled_is_not_success(self):
        self.save()
        self.ns["apisix_enabled"].return_value = False
        self.assert_http(503, self.verify)
        self.http.assert_not_called()
        self.assertFalse(self.ready())

    def test_publish_failure_persisted_but_not_ready(self):
        self.save()
        self.admin.side_effect = RuntimeError("unavailable")
        config = self.verify()
        self.assertTrue(config.healthy)
        self.assertEqual(config.status, "failed")
        self.assertFalse(self.ready())
        self.assertEqual(self.db.scalar(select(self.m.GatewayPublishRecord)).status, "failed")

    def test_latest_failed_revision_and_payload_drift_revoke_readiness(self):
        self.save()
        self.verify()
        self.route.upstream_client_secret = "rotated"
        self.assertFalse(self.ready())
        self.route.upstream_client_secret = ""
        self.assertTrue(self.ready())
        self.version.monthly_quota += 1
        self.assertFalse(self.ready())
        self.version.monthly_quota -= 1
        self.assertTrue(self.ready())
        self.db.add(self.m.GatewayConfigRevision(id="failed", route_id="r", product_id="p",
                                               revision=2, status="failed"))
        self.db.flush()
        self.assertFalse(self.ready())

    def test_second_version_has_independent_route_without_repointing_first(self):
        self.save()
        self.verify()
        self.admin.reset_mock()
        self.assertEqual(self.verify().status, "verified")
        self.admin.assert_not_called()
        self.db.add(self.m.ProductReleaseVersion(id="v2", product_id="p", version_code="v2", price=20, cost=1))
        self.db.flush()
        self.hooks["save_config"](self.db, self.product, "v2", self.body | {"version": "v2", "route_key": "p-v2"}, self.user)
        second = self.hooks["verify_version"](self.db, self.product, "v2", self.user)
        self.assertEqual(second.status, "verified")
        self.assertNotEqual(second.route_id, self.route.id)
        self.hooks["require_ready"](self.db, self.product)
        self.assertEqual((self.route.route_key, self.route.version, self.route.status), ("p-v1", "v1", "active"))
        self.assertTrue(self.ready())

    def test_unsold_config_change_invalidates_and_can_be_reverified(self):
        self.save()
        self.verify()
        config = self.save(timeout_ms=5000)
        self.assertIsNone(config.verified_at)
        self.assertFalse(self.ready())
        self.assertEqual(self.route.timeout_ms, 5000)
        self.assertEqual(self.verify().status, "verified")
        self.assertTrue(self.ready())

    def test_sold_configuration_immutable(self):
        self.save()
        # A real production Order row has several unrelated required fields;
        # intercept only the sold-order query, leaving all ORM behavior real.
        original = self.db.scalar

        def scalar(stmt, *args, **kwargs):
            if stmt.column_descriptions[0].get("entity") is self.m.Order:
                return "paid-order"
            return original(stmt, *args, **kwargs)

        with patch.object(self.db, "scalar", side_effect=scalar):
            self.save()
            self.assert_http(409, lambda: self.save(timeout_ms=5000))

    def test_auth_ownership_and_review_preconditions(self):
        outsider = self.m.User(id="outsider", platform_role="quality_reviewer", email="outside@example.invalid")
        self.assert_http(403, lambda: self.hooks["on_review_complete"](self.db, self.product, outsider))
        with patch.dict(self.ns, gateway_operator_allowed=Mock(side_effect=HTTPException(403, "Forbidden"))):
            self.assert_http(403, self.save)
        self.db.add(self.m.Product(id="other", enterprise_id="provider", name="Other",
                                  product_type="api", delivery_method="api"))
        self.db.flush()
        other = self.db.get(self.m.Product, "other")
        self.assert_http(404, lambda: self.hooks["save_config"](self.db, other, "v", self.body, self.user))
        self.save()
        self.product.status = "operation_review"
        self.assert_http(409, self.verify)
        self.http.assert_not_called()

    def test_url_path_and_version_validation(self):
        for values in ({"upstream_url": "https://user:secret@provider.invalid"},
                       {"health_path": "//private/health"}, {"route_key": "../route"},
                       {"version": "v2"}, {"auth_mode": "none"}):
            with self.subTest(values=values):
                self.assert_http(400, lambda: self.save(**values))
        self.assertEqual(self.db.scalars(select(self.Config)).all(), [])

    def test_active_without_publish_evidence_cannot_be_adopted(self):
        self.save()
        self.route.status = "active"
        self.assertEqual(self.verify().status, "failed")
        self.admin.assert_not_called()
        self.assertFalse(self.ready())

    def test_sold_draft_route_never_published(self):
        self.save()
        original = self.db.scalar

        def scalar(stmt, *args, **kwargs):
            if stmt.column_descriptions[0].get("entity") is self.m.Order:
                return "paid-order"
            return original(stmt, *args, **kwargs)

        with patch.object(self.db, "scalar", side_effect=scalar):
            self.assert_http(409, self.verify)
        self.http.assert_not_called()
        self.admin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
