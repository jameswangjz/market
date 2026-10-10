"""Real PG/APISIX acceptance; DB fixtures roll back and external objects are removed."""
import json
import secrets
from io import BytesIO

import httpx
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
from app import main as m


def run():
    marker = secrets.token_hex(8)
    prefix = "qa-int-" + marker
    connection = m.engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    previous = m.app.dependency_overrides.get(m.db_session)
    route_keys, consumers = [], []
    storage = m.Minio(m.MINIO_ENDPOINT, access_key=m.MINIO_ACCESS_KEY,
                      secret_key=m.MINIO_SECRET_KEY, secure=False)
    client = None
    try:
        users = [m.User(id=prefix + suffix, email=prefix + suffix + "@example.invalid",
                        name="Isolated integration " + suffix, verified_status="verified",
                        activation_status="active", password_hash="not-a-login-password",
                        platform_role=role) for suffix, role in (("-provider", ""), ("-buyer", ""),
                                                                 ("-admin", ""), ("-platform", "super_admin"),
                                                                 ("-finance", "finance_settlement"))]
        provider, buyer, admin, platform, finance = users
        enterprises = [m.Enterprise(id=prefix + suffix, name=prefix + suffix,
                                    credit_code=prefix + suffix, verification_status="verified")
                       for suffix in ("-ep", "-eb")]
        ep, eb = enterprises
        db.add_all(users + enterprises); db.flush()
        db.add_all([m.Membership(user_id=u.id, enterprise_id=e.id, role=role)
                    for u, e, role in ((provider, ep, "super_admin"), (buyer, eb, "super_admin"),
                                       (admin, eb, "enterprise_admin"))])
        rule = json.dumps(dict(platform_rate=20, provider_rate=80, service_rate=0, expert_rate=0, channel_rate=0))
        product = m.Product(id=prefix + "-p", enterprise_id=ep.id, name="Isolated gateway acceptance",
                            product_type="api", delivery_method="api", upstream_url="http://market-mock-api:8300",
                            status="draft", settlement_rule_mode="custom", settlement_rule_json=rule)
        db.add(product); db.flush()
        versions = [m.ProductReleaseVersion(id=prefix + "-v" + str(i), product_id=product.id,
                                            version_code="v" + str(i), price=10*i, cost=3*i,
                                            rate_limit_per_minute=2*i, daily_quota=10*i, monthly_quota=100*i)
                    for i in (1, 2)]
        db.add_all(versions); db.commit()
        m.app.dependency_overrides[m.db_session] = lambda: db
        client = TestClient(m.app)
        def request(method, path, user=platform, status=200, body=None):
            response = client.request(method, "/api" + path,
                headers={"Authorization": "Bearer " + m.issue_token(user)}, json=body)
            assert response.status_code == status, (path, response.status_code, response.text[:500])
            return response.json()

        image = BytesIO(); Image.new("RGB", (380, 280), (20, 136, 85)).save(image, "PNG")
        response = client.post("/api/files/upload", headers={"Authorization": "Bearer " + m.issue_token(provider)},
                               data={"product_id": product.id, "file_role": "product_logo"},
                               files={"upload": ("logo.png", image.getvalue(), "image/png")})
        assert response.status_code == 200, response.text
        for version in versions:
            key = prefix + "-" + version.version_code
            route_keys.append(key)
            request("PUT", f"/products/{product.id}/versions/{version.id}/integration", provider,
                    body=dict(upstream_url=product.upstream_url, route_key=key,
                              version=version.version_code, upstream_auth_mode="none",
                              rate_limit_per_minute=version.rate_limit_per_minute,
                              daily_quota=version.daily_quota, monthly_quota=version.monthly_quota))
            request("POST", f"/products/{product.id}/versions/{version.id}/integration/verify", provider, status=409)
        product.status = "operation_review"
        db.commit()
        reviewed = request("POST", f"/products/{product.id}/review", body={"decision": "approve", "comment": "Isolated runtime final approval"})
        assert reviewed["status"] == "published", reviewed
        public = request("GET", f"/storefront/products/{product.id}")
        assert {v["id"] for v in public["versions"]} == {v.id for v in versions}
        assert "upstream_url" not in json.dumps(public) and "cost" not in json.dumps(public)
        for version, key in zip(versions, route_keys):
            evidence = request("GET", f"/products/{product.id}/versions/{version.id}/integration", provider)
            assert evidence["item"]["status"] == "verified" and evidence["route"]["status"] == "active"
            route = db.get(m.ApiGatewayRoute, evidence["item"]["route_id"])
            assert route.validated_ip and route.version == version.version_code
            payload = m.apisix_admin_request("GET", "/routes/" + key)["value"]
            assert payload["labels"]["market_version"] == version.version_code
            assert payload["plugins"]["limit-count"]["count"] == version.rate_limit_per_minute
            credential = m.ApiCredential(route_id=route.id, enterprise_id=eb.id,
                key_hash=secrets.token_hex(32), key_prefix="test", daily_quota=version.daily_quota,
                monthly_quota=version.monthly_quota, status="active")
            db.add(credential); db.flush()
            raw_key = "qa_" + secrets.token_urlsafe(24)
            m.sync_apisix_consumer(credential, raw_key, route)
            consumers.append(credential.apisix_consumer_name)
            url = "http://market-apisix:9080/gateway/" + key + "/health"
            assert httpx.get(url, timeout=10).status_code == 401
            for _ in range(version.rate_limit_per_minute):
                response = httpx.get(url, headers={"X-API-Key": raw_key}, timeout=10)
                assert response.status_code == 200, response.text
                assert response.json()["service"] == "market-mock-api"
            assert httpx.get(url, headers={"X-API-Key": raw_key}, timeout=10).status_code == 429
        request("GET", f"/products/{product.id}/gateway-config", provider, status=409)
        online = request("POST", "/orders", buyer, body={"product_id": product.id,
                         "product_version_id": versions[0].id, "buyer_enterprise_id": eb.id, "subscription_months": 3})
        assert online["main_status"] == "pending_payment" and online["amount"] == 30
        request("POST", "/orders/" + online["id"] + "/transition", admin, status=403, body={"action": "confirm_payment"})
        request("POST", "/orders/" + online["id"] + "/transition", finance, body={"action": "confirm_payment"})
        _, _, chosen, _ = m.api_order_context(online["id"], buyer, db)
        assert chosen.version == "v1"
        task_count = db.scalar(select(m.func.count()).select_from(m.DeliveryTask).where(m.DeliveryTask.order_id == online["id"]))
        request("POST", "/orders/" + online["id"] + "/transition", finance, body={"action": "confirm_payment"})
        assert db.scalar(select(m.func.count()).select_from(m.DeliveryTask).where(m.DeliveryTask.order_id == online["id"])) == task_count == 1

        offline = m.Product(id=prefix + "-off", enterprise_id=ep.id, name="Offline runtime acceptance",
                            product_type="consulting", delivery_method="consulting", status="published",
                            settlement_rule_mode="custom", settlement_rule_json=rule)
        db.add(offline); db.flush()
        ov = m.ProductReleaseVersion(id=prefix + "-ov", product_id=offline.id, version_code="v1", price=100, cost=50)
        db.add(ov); db.commit()
        order = request("POST", "/orders", admin, body={"product_id": offline.id, "product_version_id": ov.id, "buyer_enterprise_id": eb.id})
        assert order["main_status"] == "pending_provider_review"
        path = "/orders/" + order["id"]
        request("POST", path + "/transition", buyer, status=409, body={"action": "confirm_payment"})
        request("GET", path + "/provider-quote", admin, status=403)
        quote = request("GET", path + "/provider-quote", provider)
        request("POST", path + "/provider-review", provider, body={"decision": "approve", "amount": 120, "cost": 60,
            "reason": "Revised work scope", "expected_updated_at": quote["expected_updated_at"]})
        request("POST", path + "/provider-review", provider, status=409, body={"decision": "approve"})
        detail = request("GET", path, admin)
        assert detail["order"]["amount"] == 120 and "cost" not in json.dumps(detail)
        request("POST", path + "/transition", buyer, body={"action": "confirm_payment"})
        assert m.order_cost(db, db.get(m.Order, order["id"])) == 60
        return {"status": "passed", "database": "postgresql", "checks": [
            "two independently verified native APISIX routes", "version-specific policies and authenticated upstream forwarding",
            "401 without key and 429 rate limit", "pre-review integration denied and ambiguous legacy request denied",
            "monthly order version routing", "buyer admin payment denied; finance confirm and idempotency",
            "offline provider quote adjustment, stale review rejection, buyer cost redaction"],
            "limits": "No real payment channel; test upstream auth mode none, no new OAuth/OIDC assertion", "fixture": "outer transaction rollback and external object cleanup"}
    finally:
        if client: client.close()
        if previous is None: m.app.dependency_overrides.pop(m.db_session, None)
        else: m.app.dependency_overrides[m.db_session] = previous
        db.close(); outer.rollback(); connection.close()
        for key in route_keys:
            m.apisix_admin_request("DELETE", "/routes/" + key)
        for consumer in consumers:
            m.apisix_admin_request("DELETE", "/consumers/" + consumer)
            redis = m.apisix_policy_redis()
            keys = list(redis.scan_iter(match="*" + consumer + "*"))
            if keys: redis.delete(*keys)
        for obj in storage.list_objects(m.MINIO_BUCKET, prefix=prefix + "-provider/", recursive=True):
            storage.remove_object(m.MINIO_BUCKET, obj.object_name)


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
