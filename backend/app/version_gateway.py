"""Per-release integration evidence; install before Base.metadata.create_all.

No routes, commits, raw network calls, or changes to existing route identities.
The caller owns transactions and supplies a validated_http_request adapter.
"""
import hashlib
import json
import re
import secrets
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, HTTPException
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import mapped_column


def install(ns):
    Base, Version, Route = (ns[k] for k in
                            ("Base", "ProductReleaseVersion", "ApiGatewayRoute"))
    Product = ns["Product"]
    Revision, Record = ns["GatewayConfigRevision"], ns["GatewayPublishRecord"]
    clock = ns["now"]
    # Repeated installation in one namespace must not register the table twice.
    if "version_gateway" in ns:
        return ns["version_gateway"]

    class VersionGatewayConfig(Base):
        __tablename__ = "version_gateway_configs"
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        product_id = mapped_column(ForeignKey("products.id"), index=True, nullable=False)
        version_id = mapped_column(ForeignKey("product_release_versions.id"), unique=True, nullable=False)
        route_id = mapped_column(ForeignKey("api_gateway_routes.id"), nullable=True)
        upstream_url = mapped_column(String(500), nullable=False)
        route_key = mapped_column(String(100), nullable=False)
        version_code = mapped_column(String(60), nullable=False)
        auth_mode = mapped_column(String(30), nullable=False)
        upstream_auth_mode = mapped_column(String(30), nullable=False)
        upstream_scope = mapped_column(String(500), nullable=False)
        timeout_ms = mapped_column(Integer, nullable=False)
        strip_prefix = mapped_column(Boolean, nullable=False)
        health_method = mapped_column(String(10), nullable=False)
        health_path = mapped_column(String(240), nullable=False)
        rate_limit_per_minute = mapped_column(Integer, nullable=False)
        daily_quota = mapped_column(Integer, nullable=False)
        monthly_quota = mapped_column(Integer, nullable=False)
        status = mapped_column(String(30), default="pending_integration", nullable=False)
        healthy = mapped_column(Boolean, default=False, nullable=False)
        verified_at = mapped_column(DateTime(timezone=True), nullable=True)
        verified_by = mapped_column(String(180), default="", nullable=False)
        health_status_code = mapped_column(Integer, nullable=True)
        evidence_hash = mapped_column(String(64), default="", nullable=False)
        revision_id = mapped_column(ForeignKey("gateway_config_revisions.id"), nullable=True)
        publish_record_id = mapped_column(ForeignKey("gateway_publish_records.id"), nullable=True)
        error_code = mapped_column(String(60), default="", nullable=False)

    fields = ("upstream_url", "route_key", "auth_mode", "upstream_auth_mode",
              "upstream_scope", "timeout_ms", "strip_prefix", "health_method",
              "health_path", "rate_limit_per_minute", "daily_quota", "monthly_quota")

    def requires_integration(product):
        return product.delivery_method in {"api", "model_api"}

    def actor(user):
        return user.email or user.phone or user.id

    def locked_product(db, product):
        if db.scalar(select(Product).where(Product.id == product.id).with_for_update()) is None:
            raise HTTPException(404, "Product not found")

    def get_version(db, product, version_id):
        version = db.get(Version, version_id)
        if not version or version.product_id != product.id:
            raise HTTPException(404, "Version not found")
        return version

    def config_for(db, version_id, lock=False):
        stmt = select(VersionGatewayConfig).where(VersionGatewayConfig.version_id == version_id)
        return db.scalar(stmt.with_for_update() if lock else stmt)

    def invalidate(config):
        config.status, config.healthy = "pending_integration", False
        config.verified_at, config.health_status_code = None, None
        config.verified_by, config.evidence_hash, config.error_code = "", "", ""
        config.revision_id, config.publish_record_id = None, None

    def on_review_complete(db, product, user):
        ns["require_product_review_role"](user, "operation")
        locked_product(db, product)
        if product.status != "operation_review":
            raise HTTPException(409, "Hook requires the final approved review stage")
        if requires_integration(product):
            product.status = "pending_integration"
            for config in db.scalars(select(VersionGatewayConfig).where(
                    VersionGatewayConfig.product_id == product.id)).all():
                invalidate(config)
        else:
            product.status = "published"
        ns["audit"](db, actor(user), "review_complete_integration_gate", "product",
                    product.id, product.status)
        return product.status

    def save_config(db, product, version_id, body, user):
        ns["gateway_operator_allowed"](product, user, db, "configure")
        locked_product(db, product)
        if product.delivery_method not in {"api", "model_api"}:
            raise HTTPException(409, "Only release API integration is implemented")
        version = get_version(db, product, version_id)
        body = ns["GatewayConfigBody"].model_validate(body.model_dump() if hasattr(body, "model_dump") else body)
        try:
            ns["validate_upstream_url"](body.upstream_url.rstrip("/") + body.health_path)
        except ValueError as exc:
            raise HTTPException(400, "后端地址或健康检查路径格式不安全") from exc
        if body.version != version.version_code or body.auth_mode != "api_key":
            raise HTTPException(400, "Version and API key authentication must match")
        url = urlsplit(body.upstream_url)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username
                or url.password or url.fragment or url.query):
            raise HTTPException(400, "Invalid upstream URL")
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", body.route_key)
                or not body.health_path.startswith("/") or body.health_path.startswith("//")
                or "#" in body.health_path or "\\" in body.health_path):
            raise HTTPException(400, "Invalid route key or health path")
        config = config_for(db, version_id, lock=True)
        values = {key: getattr(body, key) for key in fields}
        if config and all(getattr(config, k) == v for k, v in values.items()):
            return config
        Order = ns["Order"]
        if db.scalar(select(Order.id).where(Order.product_version_id == version.id,
                                          Order.paid_amount > 0).limit(1)):
            raise HTTPException(409, "Sold version configuration is immutable")
        if not config:
            config = VersionGatewayConfig(product_id=product.id, version_id=version.id,
                                          version_code=version.version_code, **values)
            db.add(config)
        else:
            for key, value in values.items():
                setattr(config, key, value)
            config.version_code = version.version_code
        route = db.scalar(select(Route).where(Route.product_id == product.id,
                                             Route.version == version.version_code).with_for_update())
        collision = db.scalar(select(Route).where(Route.route_key == body.route_key))
        if collision and (not route or collision.id != route.id):
            raise HTTPException(409, "API路由标识已被其它版本占用")
        if not route:
            route = Route(product_id=product.id, version=version.version_code,
                          created_by=actor(user), **values)
            db.add(route)
        else:
            for key, value in values.items():
                setattr(route, key, value)
        route.status = "draft"
        route.validated_ip = ""
        route.health_message = "版本配置已保存，等待审核及接入验证"
        for key in ("rate_limit_per_minute", "daily_quota", "monthly_quota"):
            setattr(version, key, values[key])
        invalidate(config)
        db.flush()
        config.route_id = route.id
        ns["audit"](db, actor(user), "save_version_gateway_config", "product", product.id, version.id)
        return config

    def matches(config, route, version):
        return (route and route.product_id == config.product_id
                and route.version == version.version_code == config.version_code
                and all(getattr(config, k) == getattr(route, k) for k in fields))

    def fingerprint(config, route, version, payload):
        value = {k: getattr(config, k) for k in fields}
        value.update(product_id=config.product_id, version_id=version.id,
                     version_code=version.version_code, route_id=route.id,
                     upstream_client_id=route.upstream_client_id,
                     upstream_client_secret=route.upstream_client_secret,
                     quotas=[version.rate_limit_per_minute, version.daily_quota, version.monthly_quota],
                     payload=payload)
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def published_evidence(db, route, product):
        revision = db.scalar(select(Revision).where(Revision.route_id == route.id,
            Revision.product_id == product.id).order_by(Revision.revision.desc(), Revision.created_at.desc(), Revision.id.desc()))
        if not revision or revision.status != "active":
            return None
        record = db.scalar(select(Record).where(Record.route_id == route.id,
            Record.revision_id == revision.id, Record.target == "apisix").order_by(Record.created_at.desc(), Record.id.desc()))
        if not record or record.status != "succeeded" or record.completed_at is None:
            return None
        try:
            payload = ns["apisix_route_payload"](route, product, db)
            if json.loads(revision.config_json) != payload:
                return None
        except (ValueError, TypeError):
            return None
        return revision, record, payload

    def verify_version(db, product, version_id, user):
        ns["gateway_operator_allowed"](product, user, db, "verify")
        locked_product(db, product)
        if product.status not in {"pending_integration", "published"}:
            raise HTTPException(409, "Complete all reviews before verification")
        version = get_version(db, product, version_id)
        config = config_for(db, version_id, lock=True)
        if not config or version.status != "active":
            raise HTTPException(409, "Active version configuration required")
        route = db.scalar(select(Route).where(Route.product_id == product.id,
                                             Route.version == version.version_code).with_for_update())
        if not matches(config, route, version):
            raise HTTPException(409, "Version route does not match its integration configuration")
        Order = ns["Order"]
        if route.status != "active" and db.scalar(select(Order.id).where(
                Order.product_version_id == version.id, Order.paid_amount > 0).limit(1)):
            raise HTTPException(409, "Sold product route cannot be republished by this adapter")
        request = ns.get("validated_http_request")
        if not callable(request):
            raise HTTPException(503, "Validated HTTP adapter with SSRF allowlist required")
        if not ns["apisix_enabled"]():
            raise HTTPException(503, "APISIX evidence is required")
        if route.upstream_auth_mode == "oauth2" and (not route.upstream_client_id or not route.upstream_client_secret):
            raise HTTPException(409, "Upstream OAuth credentials required")
        invalidate(config)
        config.route_id = route.id
        try:
            response = request(config.health_method,
                config.upstream_url.rstrip("/") + config.health_path,
                timeout=min(config.timeout_ms / 1000, 10.0), follow_redirects=False)
            config.health_status_code = response.status_code
            if not 200 <= response.status_code < 300:
                raise RuntimeError("health_failed")
            config.healthy = True
            pinned = getattr(response, "extensions", {})
            if isinstance(pinned, dict) and pinned.get("validated_upstream_addresses"):
                route.validated_ip = pinned["validated_upstream_addresses"][0]
            if route.status != "active":
                if not ns["publish_apisix_route"](route, product, db, actor(user)):
                    raise RuntimeError("publish_failed")
            db.flush()
            evidence = published_evidence(db, route, product)
            if not evidence:
                raise RuntimeError("publish_evidence_missing")
            revision, record, payload = evidence
            route.status = "active"
            route.health_message = f"\u5065\u5eb7\u68c0\u67e5\u901a\u8fc7\uff08HTTP {response.status_code}\uff09"
            config.revision_id, config.publish_record_id = revision.id, record.id
            config.evidence_hash = fingerprint(config, route, version, payload)
            config.status, config.verified_at, config.verified_by = "verified", clock(), actor(user)
            if product.status == "pending_integration":
                product.status = "published"
        except (httpx.HTTPError, RuntimeError, ValueError):
            config.status, config.error_code = "failed", "integration_failed"
            if route.status != "active":
                route.status = "publish_failed"
        ns["audit"](db, actor(user), "verify_version_gateway", "product", product.id,
                    version.id, result="success" if config.status == "verified" else "failed")
        db.flush()
        return config

    def version_ready(db, product, version):
        if not requires_integration(product):
            return product.delivery_method != "tenant_access" and version.status == "active"
        if product.delivery_method not in {"api", "model_api"} or version.status != "active":
            return False
        config = config_for(db, version.id)
        if (not config or config.product_id != product.id or config.status != "verified"
                or not config.healthy or config.verified_at is None
                or not config.health_status_code or not 200 <= config.health_status_code < 300
                or not ns["apisix_enabled"]()):
            return False
        route = db.get(Route, config.route_id)
        if not matches(config, route, version) or route.status != "active":
            return False
        evidence = published_evidence(db, route, product)
        if not evidence:
            return False
        revision, record, payload = evidence
        return (config.revision_id == revision.id and config.publish_record_id == record.id
                and config.evidence_hash == fingerprint(config, route, version, payload))

    def public_versions(db, product):
        if product.status != "published":
            return []
        rows = db.scalars(select(Version).where(Version.product_id == product.id,
            Version.status == "active").order_by(Version.created_at, Version.id)).all()
        result = []
        for version in rows:
            try:
                price, cost = Decimal(str(version.price)), Decimal(str(version.cost))
                valid_price = price.is_finite() and cost.is_finite() and price >= cost >= 0
            except (InvalidOperation, ValueError, TypeError):
                valid_price = False
            if valid_price and version.version_code.strip() and version_ready(db, product, version):
                result.append({"id": version.id, "version_code": version.version_code,
                               "description": version.description or "", "price": float(price)})
        return result

    def require_ready(db, product, version_id=None):
        if not requires_integration(product):
            return
        versions = ([get_version(db, product, version_id)] if version_id else
                    db.scalars(select(Version).where(Version.product_id == product.id,
                               Version.status == "active")).all())
        if not versions or any(not version_ready(db, product, v) for v in versions):
            raise HTTPException(409, "Version integration evidence is not ready")

    hooks = dict(requires_integration=requires_integration, on_review_complete=on_review_complete,
                 save_config=save_config, verify_version=verify_version,
                 version_ready=version_ready, public_versions=public_versions,
                 require_ready=require_ready, Config=VersionGatewayConfig)
    ns["version_gateway"] = hooks

    def config_out(config):
        return {"version_id": config.version_id, "route_id": config.route_id,
                "status": config.status, "healthy": config.healthy,
                "verified_at": config.verified_at, "error_code": config.error_code,
                "health_status_code": config.health_status_code}

    @ns["app"].get("/api/products/{product_id}/versions/{version_id}/integration")
    def get_integration(product_id: str, version_id: str,
                        user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        product = ns["gateway_product"](product_id, db)
        ns["gateway_operator_allowed"](product, user, db)
        get_version(db, product, version_id)
        config = config_for(db, version_id)
        route = db.get(Route, config.route_id) if config and config.route_id else None
        return {"item": config_out(config) if config else None,
                "route": ns["gateway_route_out"](route, product) if route else None}

    @ns["app"].put("/api/products/{product_id}/versions/{version_id}/integration")
    def save_integration(product_id: str, version_id: str, body: ns["GatewayConfigBody"],
                         user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        product = ns["gateway_product"](product_id, db)
        config = save_config(db, product, version_id, body, user)
        db.commit()
        return config_out(config)

    @ns["app"].post("/api/products/{product_id}/versions/{version_id}/integration/verify")
    def verify_integration(product_id: str, version_id: str,
                           user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        product = ns["gateway_product"](product_id, db)
        config = verify_version(db, product, version_id, user)
        db.commit()
        return config_out(config) | {"product_status": product.status}

    return hooks
