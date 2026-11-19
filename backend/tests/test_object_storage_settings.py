from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Boolean, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from app import object_storage


class Model(DeclarativeBase):
    pass


class Setting(Model):
    __tablename__ = "settings"
    setting_key: Mapped[str] = mapped_column(String, primary_key=True)
    setting_value: Mapped[str] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean)
    updated_by: Mapped[str] = mapped_column(String)


class File(Model):
    __tablename__ = "files"
    object_name: Mapped[str] = mapped_column(String, primary_key=True)
    size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)


class Attachment(Model):
    __tablename__ = "attachments"
    object_name: Mapped[str] = mapped_column(String, primary_key=True)
    size: Mapped[int] = mapped_column(Integer)


class StorageSettingsTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.addCleanup(engine.dispose)
        Model.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)

        def session():
            with self.session() as db:
                yield db

        self.user = SimpleNamespace(platform_role="super_admin", email="admin@example.invalid",
                                    phone="", id="test-admin")
        app = FastAPI()
        self.mock_storage = Mock()
        self.mock_storage.bucket_exists.return_value = True
        self.mock_storage.stat_object.return_value.size = len(object_storage.PROBE_PAYLOAD)
        stream = Mock(status=206)
        stream.read.side_effect = lambda *args: object_storage.PROBE_PAYLOAD[1:4]
        self.mock_storage.get_object.return_value = stream
        self.mock_storage.list_objects.return_value = []
        self.factory = Mock(return_value=self.mock_storage)
        self.audit = Mock()
        self.ns = dict(app=app, current_user=lambda: self.user, db_session=session,
                       audit=self.audit, SystemSetting=Setting, FileObject=File,
                       message_center={"Attachment": Attachment}, Minio=self.factory,
                       MINIO_ENDPOINT="minio:9000", MINIO_ACCESS_KEY="default-access",
                       MINIO_SECRET_KEY="default-secret", MINIO_BUCKET="market-files",
                       JWT_SECRET="disposable-test-jwt-key-only")
        object_storage.install(self.ns)
        self.api = TestClient(app)
        self.addCleanup(self.api.close)
        self.body = dict(provider="h3c", endpoint="h3c.internal:443", secure=True, region="us-east-1",
                         bucket="market.data", ca_pem="", auth_mode="ak_sk_v4",
                         access_key="candidate-access", secret_key="candidate-secret")

    def save(self, body=None):
        return self.api.patch(object_storage.ROUTE, json=self.body if body is None else body)

    def test_only_system_admin_can_read_test_or_update(self):
        self.user.platform_role = "platform_operator"
        self.assertEqual(self.api.get(object_storage.ROUTE).status_code, 403)
        self.assertEqual(self.api.post(object_storage.ROUTE + "/test", json=self.body).status_code, 403)
        self.assertEqual(self.save().status_code, 403)
        self.factory.assert_not_called()

    def test_environment_default_probe_cleanup_and_encrypted_activation(self):
        fallback = self.api.get(object_storage.ROUTE).json()
        self.assertEqual((fallback["source"], fallback["provider"], fallback["endpoint"]),
                         ("environment", "minio", "minio:9000"))
        self.assertNotIn("secret_key", fallback)
        test = self.api.post(object_storage.ROUTE + "/test", json=self.body)
        self.assertTrue(test.json()["ok"])
        key = self.mock_storage.put_object.call_args.args[1]
        self.mock_storage.remove_object.assert_called_once_with("market.data", key)
        self.assertEqual(self.api.get(object_storage.ROUTE).json(), fallback)
        saved = self.save()
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn("candidate-secret", saved.text)
        self.assertNotIn("candidate-access", saved.text)
        with self.session() as db:
            row = db.scalar(select(Setting))
            self.assertTrue(row.is_secret)
            self.assertNotIn("candidate-secret", row.setting_value)
            self.assertNotIn("candidate-access", row.setting_value)
            bound = object_storage.open_storage(db, self.ns)
        self.assertEqual(bound.bucket, "market.data")
        self.assertFalse(bound.allow_bucket_creation)
        self.assertEqual(self.factory.call_args.kwargs["secret_key"], "candidate-secret")
        self.assertEqual(self.factory.call_args.kwargs["region"], "us-east-1")
        self.assertTrue(self.factory.call_args.kwargs["secure"])
        self.assertNotIn("candidate-secret", repr(self.audit.call_args))
        without_credentials = {key: value for key, value in self.body.items()
                               if key not in {"access_key", "secret_key"}}
        self.assertEqual(self.save(without_credentials).status_code, 200)
        self.assertEqual(self.save({**without_credentials, "endpoint": "other.internal"}).status_code, 422)

    def test_history_gate_keeps_active_config_until_files_and_attachments_are_present(self):
        with self.session.begin() as db:
            db.add_all([File(object_name="dataset", size=7, status="draft"),
                        File(object_name="deleted", size=8, status="deleted"),
                        Attachment(object_name="message", size=6)])
        self.mock_storage.list_objects.return_value = [SimpleNamespace(object_name="dataset", size=7),
                                                       SimpleNamespace(object_name="message", size=5)]
        blocked = self.save()
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("1 个历史引用对象", blocked.json()["detail"])
        self.assertEqual(self.api.get(object_storage.ROUTE).json()["source"], "environment")
        self.mock_storage.list_objects.return_value[1].size = 6
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.api.get(object_storage.ROUTE).json()["provider"], "h3c")

    def test_rejects_missing_h3c_bucket_and_invalid_address_or_ca(self):
        self.mock_storage.bucket_exists.return_value = False
        self.assertEqual(self.save().status_code, 422)
        self.mock_storage.make_bucket.assert_not_called()
        for invalid in (dict(endpoint="https://h3c.internal/path"), dict(region=""),
                        dict(ca_pem="untrusted-content"), dict(secret_key=None)):
            result = self.save({**self.body, **invalid})
            self.assertEqual(result.status_code, 422, result.text)
            self.assertNotIn("candidate-secret", result.text)

    def test_provider_probe_rejects_range_incompatible_replies(self):
        self.mock_storage.get_object.return_value.status = 200
        reply = self.save()
        self.assertEqual(reply.status_code, 502)
        self.mock_storage.remove_object.assert_called_once()
        self.assertEqual(self.api.get(object_storage.ROUTE).json()["source"], "environment")
