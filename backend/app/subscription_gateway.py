"""Bridge frozen subscription terms to APISIX, retaining legacy policies."""
import json

from fastapi import HTTPException
from sqlalchemy import select


def install(ns):
    terms = ns["subscription_terms"]
    Combo = terms["Combination"]

    def combination(db, enterprise_id, product_id):
        return db.scalar(select(Combo).where(Combo.enterprise_id == enterprise_id,
                                             Combo.product_id == product_id))

    def policy(db, enterprise_id, route):
        combo = combination(db, enterprise_id, route.product_id)
        if not combo or combo.delivery_method not in {"api", "model_api"}:
            return None
        active = terms["current_entitlement"](db, enterprise_id, route.product_id)
        if not active:
            return {"rate_limit_per_minute": 0, "daily_quota": 0, "monthly_quota": 0, "total_quota": 0}
        return {**active["limits"], "total_quota": 0}

    def metadata(db, credential, route):
        combo = combination(db, credential.enterprise_id, route.product_id)
        if not combo or combo.delivery_method not in {"api", "model_api"}:
            return None
        periods = terms["quota_periods"](db, credential.enterprise_id, route.product_id)
        bound = []
        for period in periods:
            if period["version_code"] != route.version:
                continue
            bound.append({key: period[key] for key in (
                "start_at", "end_at", "period_key", "rate_limit_per_minute", "daily_quota", "monthly_quota")} | {"route_key": route.route_key})
        return {"mode": "subscription", "quota_scope": combo.id,
                "status": credential.status, "subscription_periods": json.dumps(bound, separators=(",", ":")),
                "daily_quota": credential.daily_quota or 0, "monthly_quota": credential.monthly_quota or 0,
                "total_quota": 0}

    def sync(db, credential, route):
        if not ns["apisix_enabled"]():
            return False
        data = metadata(db, credential, route)
        if data is None:
            return False
        client = ns["apisix_policy_redis"]()
        key = "market:apisix:credential:" + credential.apisix_consumer_name
        previous = client.hgetall(key)
        if previous.get("mode") != "subscription":
            # Never zero an old counter when moving an existing credential.
            old = list(client.scan_iter(match=f"market:apisix:quota:*:{route.route_key}:{credential.apisix_consumer_name}*"))
            if old and any(int(value or 0) > 0 for value in client.mget(old)):
                raise HTTPException(409, "历史网关用量需完成配额迁移后才可切换订阅计数，当前用量不会清零")
        client.hset(key, mapping=data)
        return True

    def refresh(db, enterprise_id, route):
        combo = combination(db, enterprise_id, route.product_id)
        if not combo or combo.delivery_method not in {"api", "model_api"}:
            return False
        rows = db.scalars(select(ns["ApiCredential"]).where(
            ns["ApiCredential"].enterprise_id == enterprise_id,
            ns["ApiCredential"].route_id == route.id)).all()
        limits = policy(db, enterprise_id, route)
        for credential in rows:
            # Renewal must not reactivate a deliberately revoked credential.
            credential.rate_limit_per_minute = limits["rate_limit_per_minute"]
            credential.daily_quota = limits["daily_quota"]
            credential.monthly_quota = limits["monthly_quota"]
            credential.total_quota = 0
            if credential.apisix_consumer_name:
                sync(db, credential, route)
        return True

    def verify_delivery(db, order):
        if order.snapshot_version != 1 or order.delivery_method_snapshot not in {"api", "model_api", "tenant_access"}:
            return False
        if order.delivery_method_snapshot == "tenant_access":
            subscription = db.get(ns["SaaSSubscription"], order.subscription_id) if order.subscription_id else None
            if (not subscription or subscription.status != "active" or not subscription.external_tenant_id
                    or subscription.enterprise_id != order.buyer_enterprise_id
                    or subscription.product_id != order.product_id
                    or subscription.version_id != order.product_version_id):
                raise HTTPException(409, "SaaS 租户及管理员尚未完成开通，不能开始订阅计时")
            return True
        product = db.get(ns["Product"], order.product_id)
        version = db.get(ns["ProductReleaseVersion"], order.product_version_id)
        if not product or not version or not ns["version_gateway"]["version_ready"](db, product, version):
            raise HTTPException(409, "订单版本网关尚未完成有效接入验证，不能开始订阅计时")
        config = db.scalar(select(ns["version_gateway"]["Config"]).where(
            ns["version_gateway"]["Config"].version_id == version.id))
        result = ns["validated_http_request"](config.health_method,
            config.upstream_url.rstrip("/") + config.health_path,
            timeout=min(config.timeout_ms / 1000, 10.0), follow_redirects=False)
        if not 200 <= result.status_code < 300:
            raise HTTPException(409, "API 健康检查未通过，不能开始订阅计时")
        route = db.get(ns["ApiGatewayRoute"], config.route_id)
        destination = getattr(result, "extensions", {}).get("validated_upstream_ip")
        if destination and destination != route.validated_ip:
            raise HTTPException(409, "API 上游地址已变化，请重新完成网关接入验证")
        return True

    return {"policy": policy, "metadata": metadata, "sync": sync, "refresh": refresh,
            "verify_delivery": verify_delivery, "combination": combination}
