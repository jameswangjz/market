import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_message_business_events import load_isolated_main


class ProductCategoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_isolated_main()

    @classmethod
    def tearDownClass(cls):
        cls.m.engine.dispose()
        package = cls.m.__package__
        for name in list(sys.modules):
            if name == package or name.startswith(package + "."):
                del sys.modules[name]

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.m.Base.metadata.create_all(self.engine)
        self.m.product_categories["ensure_schema"](self.engine)
        with self.Session() as db:
            db.add(self.m.Enterprise(id="provider", name="Provider", credit_code="category-test"))
            db.commit()
        self.role = "super_admin"
        self.audit = Mock()
        self.m.audit = self.audit

        def current_user():
            return SimpleNamespace(platform_role=self.role, id="operator", email="operator@example.test", phone="")

        def db_session():
            with self.Session() as db:
                yield db

        self.m.app.dependency_overrides[self.m.current_user] = current_user
        self.m.app.dependency_overrides[self.m.db_session] = db_session
        self.client = TestClient(self.m.app)

    def tearDown(self):
        self.client.close()
        self.m.app.dependency_overrides.clear()
        self.engine.dispose()

    def create(self, name, parent_id="", delivery_method=""):
        return self.client.post("/api/admin/product-categories", json={
            "name": name, "parent_id": parent_id, "delivery_method": delivery_method,
        })

    def test_platform_roles_trimmed_names_delivery_enum_and_two_levels(self):
        self.role = "product_manager"
        self.assertEqual(self.client.get("/api/admin/product-categories").status_code, 403)
        self.role = "platform_operator"
        self.assertEqual(self.create("   ").status_code, 422)
        self.assertEqual(self.create("Bad delivery", delivery_method="object_storage").status_code, 422)
        root = self.create("  Data  ")
        self.assertEqual(root.status_code, 200, root.text)
        self.assertEqual(root.json()["name"], "Data")
        child = self.create("Quality", root.json()["id"], "file")
        self.assertEqual(child.status_code, 200, child.text)
        self.assertEqual(self.create("Quality", root.json()["id"]).status_code, 409)
        self.assertEqual(self.create("Too deep", child.json()["id"]).status_code, 400)

    def test_referenced_categories_cannot_be_deleted_and_mutations_audited(self):
        root = self.create("Data").json()
        child = self.create("Quality", root["id"], "file").json()
        with self.Session() as db:
            db.add(self.m.Product(id="referencing-product", enterprise_id="provider", name="Product",
                                  product_type="dataset", catalog_name="Data/Quality", category_id=child["id"]))
            db.commit()
        self.assertEqual(self.client.delete(f"/api/admin/product-categories/{root['id']}").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/admin/product-categories/{child['id']}").status_code, 409)
        self.assertEqual([call.args[2] for call in self.audit.call_args_list], ["create_product_category", "create_product_category"])

    def test_seed_is_deterministic_one_time_and_keeps_unclassified_unassigned(self):
        with self.Session() as db:
            db.add_all([
                self.m.Product(id="legacy", enterprise_id="provider", name="Legacy", product_type="dataset",
                               catalog_name="行业数据集/产品质量"),
                self.m.Product(id="unclassified", enterprise_id="provider", name="Unclassified", product_type="dataset",
                               catalog_name="未分类"),
            ])
            db.commit()
            self.m.product_categories["seed"](db)
            db.commit()
            legacy_id = db.get(self.m.Product, "legacy").category_id
            self.assertTrue(legacy_id)
            self.assertIsNone(db.get(self.m.Product, "unclassified").category_id)
            count = len(db.scalars(select(self.m.ProductCategory)).all())
            to_delete = db.scalar(select(self.m.ProductCategory).where(self.m.ProductCategory.name == "其他", self.m.ProductCategory.parent_id.is_not(None)))
            db.delete(to_delete)
            db.commit()
            self.m.product_categories["seed"](db)
            db.commit()
            self.assertEqual(len(db.scalars(select(self.m.ProductCategory)).all()), count - 1)


if __name__ == "__main__":
    unittest.main()
