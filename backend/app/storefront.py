"""Anonymous storefront, installed with install_storefront(globals()) at main's end."""
import json
import re
from decimal import Decimal, InvalidOperation
from io import BytesIO
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Query, Response
from PIL import Image
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload


def _http_url(value):
    try:
        url = urlsplit(value or "")
        return (url.scheme in {"http", "https"} and bool(url.hostname)
                and url.username is None and url.password is None)
    except ValueError:
        return False


def _valid_price(value, cost):
    try:
        price, cost = Decimal(str(value)), Decimal(str(cost))
        return price.is_finite() and cost.is_finite() and price >= cost >= 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def install(ns):
    app, Product = ns["app"], ns["Product"]
    Version, File = ns["ProductReleaseVersion"], ns["FileObject"]
    Route = ns["ApiGatewayRoute"]
    db_session = ns["db_session"]
    monthly_methods = {"api", "model_api", "tenant_access"}
    health_success = re.compile(r"\u5065\u5eb7\u68c0\u67e5\u901a\u8fc7\uff08HTTP 2\d\d\uff09")

    def gateway_ready(db, product):
        route = db.scalar(select(Route).where(Route.product_id == product.id))
        if (not route or route.status != "active" or not route.route_key
                or not _http_url(route.upstream_url)
                or not health_success.fullmatch(route.health_message or "")
                or route.health_method not in {"GET", "HEAD"}
                or not route.health_path or route.timeout_ms <= 0):
            return None
        if route.upstream_auth_mode == "oauth2":
            if not route.upstream_client_id or not route.upstream_client_secret:
                return None
        elif route.upstream_auth_mode != "none":
            return None
        # Existing health checks are product-route evidence, not per-version proof.
        # Only the version actually configured on that route can be offered.
        if ns["apisix_enabled"]():
            Revision, Record = ns["GatewayConfigRevision"], ns["GatewayPublishRecord"]
            revision = db.scalar(select(Revision).where(
                Revision.route_id == route.id, Revision.product_id == product.id
            ).order_by(Revision.revision.desc(), Revision.created_at.desc(), Revision.id.desc()))
            if not revision or revision.status != "active":
                return None
            record = db.scalar(select(Record).where(
                Record.route_id == route.id, Record.revision_id == revision.id,
                Record.target == "apisix"
            ).order_by(Record.created_at.desc(), Record.id.desc()))
            if not record or record.status != "succeeded" or record.completed_at is None:
                return None
            try:
                if json.loads(revision.config_json) != ns["apisix_route_payload"](route, product, db):
                    return None
            except (ValueError, TypeError):
                return None
        return route

    def versions_for(db, product):
        if product.delivery_method not in {"file", "api", "model_api", "tenant_access", "training", "consulting", "custom"}:
            return []
        if product.product_type == "saas":
            Config, SaaSVersion = ns["SaaSIntegrationConfig"], ns["SaaSProductVersion"]
            config = db.scalar(select(Config).where(Config.product_id == product.id))
            if (product.delivery_method != "tenant_access" or not config
                    or config.status != "active" or not _http_url(config.base_url)
                    or not _http_url(product.application_url)
                    or not config.operation_path.startswith("/")
                    or config.auth_mode != "oauth2" or not _http_url(config.token_url)
                    or not config.client_id or not config.client_secret):
                return []
            rows = db.scalars(select(SaaSVersion).where(
                SaaSVersion.product_id == product.id, SaaSVersion.status == "active"
            ).order_by(SaaSVersion.created_at, SaaSVersion.id)).all()
            return [public_version(v, v.monthly_price) for v in rows
                    if v.version_code and v.version_code.strip()
                    and _valid_price(v.monthly_price, v.cost)]
        requires_gateway = product.delivery_method in {"api", "model_api"}
        route = gateway_ready(db, product) if requires_gateway else None
        if requires_gateway and (not route or product.delivery_method not in {"api", "model_api"}):
            return []
        if product.delivery_method == "tenant_access":
            return []
        return [public_version(v, v.price) for v in product.versions
                if v.status == "active" and v.version_code and v.version_code.strip()
                and _valid_price(v.price, v.cost)
                and (not requires_gateway or v.version_code == route.version)]

    def public_version(version, price):
        return {"id": version.id, "version_code": version.version_code,
                "description": version.description or "", "price": float(price)}

    def thumbnail(db, product):
        if not product.logo_thumbnail_file_id:
            return None
        item = db.get(File, product.logo_thumbnail_file_id)
        if (not item or item.product_id != product.id
                or item.file_role != "product_logo_thumbnail" or item.status not in {"draft", "active"}
                or item.scan_status != "clean" or item.content_type != "image/png"
                or not item.object_name or not 0 < item.size <= 1024 * 1024):
            return None
        if item.version_id and not db.scalar(select(Version.id).where(
                Version.id == item.version_id, Version.product_id == product.id,
                Version.status == "active")):
            return None
        return item

    def public_product(db, product):
        versions = versions_for(db, product)
        if not versions:
            return None
        # Explicit public whitelist; never reuse product_out or ORM serialization.
        provider = db.get(ns["Enterprise"], product.enterprise_id)
        return {"id": product.id, "name": product.name,
                "provider_name": provider.name if provider else "",
                "description": product.description or "",
                "delivery_method": product.delivery_method,
                "product_type": product.product_type,
                "logo_url": f"/api/storefront/products/{product.id}/logo" if thumbnail(db, product) else "",
                "versions": versions, "minimum_price": min(v["price"] for v in versions),
                "price_unit": "month" if product.delivery_method in monthly_methods else "order"}

    def visible_product(db, product_id):
        product = db.scalar(select(Product).options(selectinload(Product.versions)).where(
            Product.id == product_id, Product.status == "published"))
        if not product or not (dto := public_product(db, product)):
            raise HTTPException(404, "Product not found")
        return product, dto

    @app.get("/api/storefront/products")
    def products(q: str = Query(default="", max_length=240),
                 type: str = Query(default="", max_length=50),
                 catalog: str = Query(default="", max_length=180),
                 product_type: str = Query(default="", max_length=50),
                 catalog_name: str = Query(default="", max_length=180),
                 page: int = Query(default=1, ge=1),
                 page_size: int = Query(default=20, ge=1, le=100),
                 db=Depends(db_session)):
        stmt = select(Product).options(selectinload(Product.versions)).where(Product.status == "published")
        if q.strip():
            term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(or_(*(column.ilike(f"%{term}%", escape="\\") for column in
                                  (Product.name, Product.description, Product.provider_name))))
        for value in (type, product_type):
            if value.strip():
                stmt = stmt.where(Product.product_type == value.strip())
        for value in (catalog, catalog_name):
            if value.strip():
                stmt = stmt.where(Product.catalog_name == value.strip())
        # Readiness includes persisted gateway evidence. Filter before counting or
        # slicing so a failed route never inflates total or leaves a short page.
        items = []
        for product in db.scalars(stmt.order_by(Product.updated_at.desc(), Product.id)).all():
            item = public_product(db, product)
            if item:
                items.append(item)
        start = (page - 1) * page_size
        return {"items": items[start:start + page_size], "total": len(items),
                "page": page, "page_size": page_size}

    @app.get("/api/storefront/products/{product_id}")
    def product_detail(product_id: str, db=Depends(db_session)):
        return visible_product(db, product_id)[1]

    @app.get("/api/storefront/products/{product_id}/logo")
    def product_logo(product_id: str, db=Depends(db_session)):
        product, _ = visible_product(db, product_id)
        item = thumbnail(db, product)
        if not item:
            raise HTTPException(404, "Logo not found")
        if not ns["MINIO_ENDPOINT"]:
            raise HTTPException(503, "File storage unavailable")
        response = None
        try:
            client = ns["Minio"](ns["MINIO_ENDPOINT"], access_key=ns["MINIO_ACCESS_KEY"],
                                  secret_key=ns["MINIO_SECRET_KEY"], secure=False)
            response = client.get_object(ns["MINIO_BUCKET"], item.object_name)
            content = response.read(1024 * 1024 + 1)
        except Exception as exc:
            raise HTTPException(404, "Logo not found") from exc
        finally:
            if response is not None:
                try:
                    response.close()
                finally:
                    response.release_conn()
        try:
            if len(content) != item.size:
                raise ValueError("Invalid thumbnail size")
            with Image.open(BytesIO(content)) as img:
                if img.format != "PNG" or not (0 < img.width <= 190 and 0 < img.height <= 140):
                    raise ValueError("Invalid thumbnail")
                img.verify()
            # Re-encode rather than expose trailing data or embedded metadata.
            with Image.open(BytesIO(content)) as img:
                output = BytesIO()
                img.convert("RGBA").save(output, format="PNG")
            content = output.getvalue()
        except Exception as exc:
            raise HTTPException(404, "Logo not found") from exc
        return Response(content, media_type="image/png", headers={
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    return {"public_product": public_product, "versions_for": versions_for}
