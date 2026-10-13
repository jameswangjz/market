"""Two-level product directory management and legacy directory migration."""
import hashlib
import secrets

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import ForeignKey, String, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column


DIRECTORIES = [
    ("行业数据集", "供需计划"), ("行业数据集", "产品工艺设计"),
    ("行业数据集", "制造过程"), ("行业数据集", "产品质量"),
    ("行业模型", "供需计划"), ("行业模型", "产品工艺设计"),
    ("行业模型", "制造过程"), ("行业模型", "产品质量"),
    ("数据服务", "数据治理"), ("数据服务", "培训咨询"),
    ("数据服务", "定制开发"), ("其他", "其他"),
]


def _stable_id(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


class CategoryBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    parent_id: str = ""
    delivery_method: str = ""

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("目录名称不能为空")
        return value

    @field_validator("delivery_method")
    @classmethod
    def valid_delivery_method(cls, value):
        value = value.strip()
        allowed = {"", "file", "api", "model_api", "tenant_access", "training", "consulting", "custom"}
        if value not in allowed:
            raise ValueError("交付方式无效")
        return value


def install(ns):
    Base = ns["Base"]

    class ProductCategory(Base):
        __tablename__ = "product_categories"
        id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        parent_id: Mapped[str | None] = mapped_column(ForeignKey("product_categories.id", name="fk_product_category_parent", ondelete="RESTRICT"), nullable=True, index=True)
        name: Mapped[str] = mapped_column(String(80))
        delivery_method: Mapped[str] = mapped_column(String(80), default="")

    ns["ProductCategory"] = ProductCategory
    app, db_session = ns["app"], ns["db_session"]

    def ensure_schema(engine):
        with engine.begin() as connection:
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_product_category_sibling_name ON product_categories (COALESCE(parent_id, ''), name)"))
            if engine.dialect.name == "sqlite":
                for operation in ("INSERT", "UPDATE OF category_id"):
                    suffix = "insert" if operation == "INSERT" else "update"
                    connection.execute(text(f"CREATE TRIGGER IF NOT EXISTS trg_product_category_product_ref_{suffix} BEFORE {operation} ON products WHEN NEW.category_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM product_categories WHERE id = NEW.category_id) BEGIN SELECT RAISE(ABORT, 'invalid product category'); END"))
                connection.execute(text("CREATE TRIGGER IF NOT EXISTS trg_product_category_product_ref_delete BEFORE DELETE ON product_categories WHEN EXISTS (SELECT 1 FROM products WHERE category_id = OLD.id) BEGIN SELECT RAISE(ABORT, 'product category is referenced'); END"))
                connection.execute(text("CREATE TRIGGER IF NOT EXISTS trg_product_category_parent_insert BEFORE INSERT ON product_categories WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM product_categories WHERE id = NEW.parent_id AND parent_id IS NULL) BEGIN SELECT RAISE(ABORT, 'invalid category parent'); END"))
                connection.execute(text("CREATE TRIGGER IF NOT EXISTS trg_product_category_parent_update BEFORE UPDATE OF parent_id ON product_categories WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM product_categories WHERE id = NEW.parent_id AND parent_id IS NULL) BEGIN SELECT RAISE(ABORT, 'invalid category parent'); END"))
                connection.execute(text("CREATE TRIGGER IF NOT EXISTS trg_product_category_child_delete BEFORE DELETE ON product_categories WHEN EXISTS (SELECT 1 FROM product_categories WHERE parent_id = OLD.id) BEGIN SELECT RAISE(ABORT, 'category has children'); END"))
            else:
                constraints = {item["name"] for item in inspect(connection).get_foreign_keys("products")}
                if "fk_products_category_id_product_categories" not in constraints:
                    connection.execute(text("ALTER TABLE products ADD CONSTRAINT fk_products_category_id_product_categories FOREIGN KEY (category_id) REFERENCES product_categories (id) ON DELETE RESTRICT"))
                constraints = {item["name"] for item in inspect(connection).get_foreign_keys("product_categories")}
                if "fk_product_category_parent" not in constraints:
                    connection.execute(text("ALTER TABLE product_categories ADD CONSTRAINT fk_product_category_parent FOREIGN KEY (parent_id) REFERENCES product_categories (id) ON DELETE RESTRICT"))

    def platform_admin(user):
        if user.platform_role not in {"super_admin", "platform_operator"}:
            raise HTTPException(403, "仅平台超级管理员和平台运营方可管理产品目录")

    def level(db, category):
        return 1 if not category.parent_id else 2

    def tree(db, *, public=False, delivery_method=""):
        rows = db.scalars(select(ProductCategory).order_by(ProductCategory.parent_id, ProductCategory.name, ProductCategory.id)).all()
        roots = [x for x in rows if not x.parent_id]
        children = {}
        for row in rows:
            if row.parent_id:
                children.setdefault(row.parent_id, []).append(row)
        result = []
        for parent in roots:
            items = [child for child in children.get(parent.id, []) if not delivery_method or not child.delivery_method or child.delivery_method == delivery_method]
            if delivery_method and not items:
                continue
            result.append({"id": parent.id, "name": parent.name, "level": 1,
                           "children": [{"id": c.id, "name": c.name, "level": 2,
                                         "parent_id": parent.id, "delivery_method": c.delivery_method} for c in items]})
        return result

    def seed(db):
        Marker = ns["SystemSetting"]
        marker = db.scalar(select(Marker).where(Marker.setting_key == "product_categories_seed_v1"))
        if marker:
            return
        for parent_name, child_name in DIRECTORIES:
            parent_id = _stable_id("product-category:1:" + parent_name)
            parent = db.get(ProductCategory, parent_id)
            if not parent:
                parent = ProductCategory(id=parent_id, parent_id=None, name=parent_name)
                db.add(parent)
                db.flush()
            child_id = _stable_id("product-category:2:" + parent_name + "/" + child_name)
            if not db.get(ProductCategory, child_id):
                db.add(ProductCategory(id=child_id, parent_id=parent_id, name=child_name))
                db.flush()
        db.flush()
        Product = ns["Product"]
        for parent_name, child_name in DIRECTORIES:
            value = "其他" if parent_name == "其他" else f"{parent_name}/{child_name}"
            child_id = _stable_id("product-category:2:" + parent_name + "/" + child_name)
            db.query(Product).filter(Product.catalog_name == value, Product.category_id.is_(None)).update({Product.category_id: child_id}, synchronize_session=False)
        db.add(Marker(setting_key="product_categories_seed_v1", setting_value="complete"))

    @app.get("/api/product-categories")
    def list_categories(user=Depends(ns["current_user"]), db=Depends(db_session)):
        return {"items": tree(db)}

    @app.get("/api/admin/product-categories")
    def admin_categories(user=Depends(ns["current_user"]), db=Depends(db_session)):
        platform_admin(user)
        return {"items": tree(db)}

    @app.get("/api/storefront/categories")
    def public_categories(delivery_method: str = "", db=Depends(db_session)):
        return {"items": tree(db, public=True, delivery_method=delivery_method.strip())}

    @app.post("/api/admin/product-categories")
    def create_category(body: CategoryBody, user=Depends(ns["current_user"]), db=Depends(db_session)):
        platform_admin(user)
        parent_id = body.parent_id.strip()
        parent = db.scalar(select(ProductCategory).where(ProductCategory.id == parent_id).with_for_update()) if parent_id else None
        if parent_id and not parent:
            raise HTTPException(400, "上级目录不存在")
        if parent and parent.parent_id:
            raise HTTPException(400, "产品目录仅支持两级")
        if not parent_id and body.delivery_method:
            raise HTTPException(400, "一级目录不能设置交付方式")
        parent_value = parent_id or None
        if db.scalar(select(ProductCategory.id).where(ProductCategory.parent_id == parent_value, ProductCategory.name == body.name.strip())):
            raise HTTPException(409, "同级目录名称已存在")
        row = ProductCategory(parent_id=parent_value, name=body.name.strip(), delivery_method=body.delivery_method.strip())
        db.add(row)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "同级目录名称已存在或上级目录已删除") from exc
        ns["audit"](db, user.email or user.phone or user.id, "create_product_category", "product_category", row.id,
                    before={}, after={"name": row.name, "parent_id": row.parent_id, "delivery_method": row.delivery_method})
        db.commit()
        return {"id": row.id, "parent_id": row.parent_id, "name": row.name, "delivery_method": row.delivery_method}

    @app.put("/api/admin/product-categories/{category_id}")
    def update_category(category_id: str, body: CategoryBody, user=Depends(ns["current_user"]), db=Depends(db_session)):
        platform_admin(user)
        row = db.scalar(select(ProductCategory).where(ProductCategory.id == category_id).with_for_update())
        if not row:
            raise HTTPException(404, "目录不存在")
        if body.parent_id.strip() != (row.parent_id or ""):
            raise HTTPException(400, "目录层级不可变更")
        duplicate = db.scalar(select(ProductCategory.id).where(ProductCategory.parent_id == row.parent_id, ProductCategory.name == body.name.strip(), ProductCategory.id != row.id))
        if duplicate:
            raise HTTPException(409, "同级目录名称已存在")
        before = {"name": row.name, "parent_id": row.parent_id, "delivery_method": row.delivery_method}
        row.name = body.name.strip()
        row.delivery_method = body.delivery_method.strip() if row.parent_id else ""
        ns["audit"](db, user.email or user.phone or user.id, "update_product_category", "product_category", row.id,
                    before=before, after={"name": row.name, "parent_id": row.parent_id, "delivery_method": row.delivery_method})
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "同级目录名称已存在") from exc
        return {"id": row.id, "parent_id": row.parent_id, "name": row.name, "delivery_method": row.delivery_method}

    @app.delete("/api/admin/product-categories/{category_id}")
    def delete_category(category_id: str, user=Depends(ns["current_user"]), db=Depends(db_session)):
        platform_admin(user)
        row = db.scalar(select(ProductCategory).where(ProductCategory.id == category_id).with_for_update())
        if not row:
            raise HTTPException(404, "目录不存在")
        if db.scalar(select(ProductCategory.id).where(ProductCategory.parent_id == row.id).limit(1)):
            raise HTTPException(409, "请先删除下级目录")
        if db.scalar(select(ns["Product"].id).where(ns["Product"].category_id == row.id).limit(1)):
            raise HTTPException(409, "该目录仍被产品引用，不能删除")
        before = {"name": row.name, "parent_id": row.parent_id, "delivery_method": row.delivery_method}
        db.delete(row)
        ns["audit"](db, user.email or user.phone or user.id, "delete_product_category", "product_category", row.id, before=before, after={})
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "该目录仍有下级目录或产品引用，不能删除") from exc
        return {"ok": True}

    return {"seed": seed, "model": ProductCategory, "tree": tree, "ensure_schema": ensure_schema}
