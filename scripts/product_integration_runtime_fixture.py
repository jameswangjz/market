"""Prepare or clean an isolated API integration browser fixture.

Run in the API pod with product_registration_browser_fixture.py and
subscription_pg_concurrency.py beside this file. Preparation creates only
private provider metadata and two version configs; the browser performs the
two real verification requests. This is not a product-review regression.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat

import httpx
from sqlalchemy import delete, select


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "product_registration_browser_fixture", HERE / "product_registration_browser_fixture.py")
registration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(registration)
m = registration.m


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def private_path(value):
    path = Path(value).absolute()
    check(path.parent in (Path("/tmp"), Path("/var/tmp")) and not path.is_symlink(),
          "Use a direct non-symlink path under /tmp or /var/tmp")
    return path


def write_private(path, value, create=False):
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    else:
        info = path.lstat()
        check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
              "Fixture state must remain a regular mode-0600 file")
        temporary = path.with_name(path.name + "." + secrets.token_hex(8))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def environment_fingerprint():
    values = [m.engine.url.render_as_string(hide_password=True),
              os.getenv("APISIX_ADMIN_URL", "http://market-apisix-admin:9180/apisix/admin")]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def admin(method, route_key):
    base = os.getenv("APISIX_ADMIN_URL", "http://market-apisix-admin:9180/apisix/admin").rstrip("/")
    response = httpx.request(method, base + "/routes/" + route_key,
                             headers={"X-API-KEY": os.environ["APISIX_ADMIN_KEY"]}, timeout=15)
    if method == "GET" and response.status_code == 404:
        return None
    check(response.status_code in {200, 201, 204}, "APISIX route operation failed")
    if not response.content:
        return {}
    value = response.json()
    while isinstance(value, dict):
        child = next((value[key] for key in ("node", "value", "route")
                      if isinstance(value.get(key), dict)), None)
        if child is None:
            break
        value = child
    return value


def exact_route_owner(route, expected):
    labels = route.get("labels") or {}
    return (route.get("name") == "market-" + expected["route_key"]
            and route.get("uri") == "/gateway/" + expected["route_key"] + "/*"
            and labels == expected["labels"])


def prepare(state_path, token_path):
    check(state_path != token_path and not state_path.exists() and not token_path.exists(),
          "Private state and token paths must be distinct and absent")
    check(m.engine.dialect.name == "postgresql" and m.apisix_enabled()
          and m.apisix_native_upstream(), "Migrated PostgreSQL and native APISIX are required")
    check(not m.app.dependency_overrides, "Dependency overrides are forbidden")
    with m.SessionLocal() as db:
        m.application_address_policy["validate_url"]("http://market-mock-api:8300/health", db=db)
    bad = m.validated_http_request("GET", "http://market-mock-api:8300/health-missing",
                                   timeout=5, follow_redirects=False)
    good = m.validated_http_request("GET", "http://market-mock-api:8300/health",
                                    timeout=5, follow_redirects=False)
    check(bad.status_code == 404 and good.status_code == 200
          and good.json() == {"status": "ok", "service": "market-mock-api"}
          and bool(good.extensions.get("validated_upstream_addresses")),
          "Mock API 404/200 health preflight failed")

    registration.prepare(state_path, token_path)
    try:
        state = json.loads(state_path.read_text())
        token_data = json.loads(token_path.read_text())
        prefix = state["prefix"]
        product_id = state["product"]
        versions = token_data["versions"]
        check(len(versions) == 2 and [item["id"] for item in versions] == state["versions"],
              "Registration fixture version manifest mismatch")

        routes = []
        with m.SessionLocal() as db:
            product = db.get(m.Product, product_id)
            user = db.get(m.User, state["users"][0])
            check(product is not None and product.enterprise_id == state["enterprises"][0]
              and product.status == "pending_integration", "Fixture product ownership/state mismatch")
            for index, item in enumerate(versions):
                version = db.get(m.ProductReleaseVersion, item["id"])
                check(version is not None and version.product_id == product_id,
                  "Fixture release does not belong to fixture product")
                route_key = item["route_key"]
                check(admin("GET", route_key) is None, "APISIX route key collision; refusing setup")
                route = db.scalar(select(m.ApiGatewayRoute).where(
                m.ApiGatewayRoute.product_id == product_id,
                m.ApiGatewayRoute.route_key == route_key))
                check(route is not None and route.version == version.version_code,
                  "Registration route identity mismatch")
                health_path = "/health-missing" if index == 0 else "/health"
                labels = {"market_product_id": product_id,
                      "market_version": version.version_code,
                      "market_rate_limit_per_minute": str(version.rate_limit_per_minute),
                      "market_daily_quota": str(version.daily_quota),
                      "market_monthly_quota": str(version.monthly_quota),
                      "market_managed": "true", "market_data_plane": "native"}
                routes.append({"id": route.id, "route_key": route_key,
                           "version_id": version.id, "version_code": version.version_code,
                           "health_path": health_path, "product_id": product_id,
                           "labels": labels})

        runtime = {"schema": 1, "authorized": True, "environment": environment_fingerprint(),
               "product_id": product_id, "enterprise_id": state["enterprises"][0],
               "user_id": state["users"][0], "routes": routes,
               "external_route_keys": sorted(route["route_key"] for route in routes)}
        state["product_integration_runtime"] = runtime
        write_private(state_path, state)

        with m.SessionLocal() as db:
            product = db.get(m.Product, product_id)
            user = db.get(m.User, state["users"][0])
            for index, item in enumerate(versions):
                version = db.get(m.ProductReleaseVersion, item["id"])
                route_key = item["route_key"]
                expected = routes[index]
                health_path = expected["health_path"]
                body = {
                "upstream_url": "http://market-mock-api:8300", "route_key": route_key,
                "version": version.version_code, "auth_mode": "api_key",
                "upstream_auth_mode": "none", "upstream_scope": "", "timeout_ms": 10000,
                "strip_prefix": True, "health_method": "GET", "health_path": health_path,
                "rate_limit_per_minute": version.rate_limit_per_minute,
                "daily_quota": version.daily_quota, "monthly_quota": version.monthly_quota,
            }
                m.version_gateway["save_config"](db, product, version.id, body, user)
                route = db.scalar(select(m.ApiGatewayRoute).where(
                m.ApiGatewayRoute.product_id == product_id,
                m.ApiGatewayRoute.route_key == route_key))
                config_model = m.version_gateway["Config"]
                config = db.scalar(select(config_model).where(config_model.version_id == version.id))
                check(route is not None and route.id == expected["id"] and config is not None and route.status == "draft"
                      and config.status == "pending_integration" and not config.healthy
                      and config.health_path == health_path and config.route_id == expected["id"],
                      "Fixture integration config was not left pending/matched to its version")
                item.update({"upstream_url": body["upstream_url"], "health_path": health_path,
                             "route_id": expected["id"],
                             "health_method": "GET", "upstream_auth_mode": "none",
                             "upstream_scope": "", "timeout_ms": 10000,
                             "strip_prefix": True, "rate_limit_per_minute": version.rate_limit_per_minute,
                             "daily_quota": version.daily_quota,
                             "monthly_quota": version.monthly_quota})
            db.commit()

        check([item["health_path"] for item in versions]
              == [route["health_path"] for route in routes]
              and [item["route_id"] for item in versions]
              == [route["id"] for route in routes],
              "Per-version token metadata differs from the runtime route journal")
        token_data["versions"] = versions
        write_private(token_path, token_data)
        return {"status": "prepared", "fixture": prefix, "versions": 2,
            "health_paths": [route["health_path"] for route in routes],
                "scope": "isolated private integration metadata; no review or product audit workflow"}
    except BaseException:
        if state_path.exists() and token_path.exists():
            registration.cleanup(state_path, token_path)
        raise


def cleanup(state_path, token_path):
    info = state_path.lstat()
    check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
          "State manifest must be a regular mode-0600 file")
    state = json.loads(state_path.read_text())
    runtime = state.get("product_integration_runtime")
    check(runtime and runtime.get("authorized") is True and runtime.get("schema") == 1
          and runtime.get("environment") == environment_fingerprint(),
          "Runtime fixture/environment authorization mismatch")
    routes = runtime.get("routes")
    prefix = state.get("prefix", "")
    expected_routes = [
        {"route_key": prefix + "-r1", "version_id": prefix + "-v1", "version_code": "v1",
         "health_path": "/health-missing", "rate": 17, "daily": 123, "monthly": 456000},
        {"route_key": prefix + "-r2", "version_id": prefix + "-v2", "version_code": "v2",
         "health_path": "/health", "rate": 34, "daily": 246, "monthly": 912000},
    ]
    check(isinstance(routes, list) and len(routes) == 2
          and prefix and runtime.get("product_id") == prefix + "-p"
          and runtime.get("enterprise_id") == prefix + "-e"
          and runtime.get("user_id") == prefix + "-u"
          and runtime.get("external_route_keys") == sorted(r["route_key"] for r in expected_routes)
          and all(r.get("product_id") == runtime["product_id"]
                  and r.get("route_key") == expected["route_key"]
                  and r.get("version_id") == expected["version_id"]
                  and r.get("version_code") == expected["version_code"]
                  and r.get("health_path") == expected["health_path"]
                  and re.fullmatch(r"[0-9a-f]{32}", r.get("id", ""))
                  and r.get("labels") == {
                      "market_product_id": runtime["product_id"],
                      "market_version": expected["version_code"],
                      "market_rate_limit_per_minute": str(expected["rate"]),
                      "market_daily_quota": str(expected["daily"]),
                      "market_monthly_quota": str(expected["monthly"]),
                      "market_managed": "true", "market_data_plane": "native"}
                  for r, expected in zip(routes, expected_routes)),
          "External route journal does not match exact fixture routes")

    route_ids = [route["id"] for route in routes]
    version_ids = [route["version_id"] for route in routes]
    with m.SessionLocal() as db:
        product = db.scalar(select(m.Product).where(
            m.Product.id == runtime["product_id"]).with_for_update())
        enterprise = db.scalar(select(m.Enterprise).where(
            m.Enterprise.id == runtime["enterprise_id"]).with_for_update())
        user = db.get(m.User, runtime["user_id"])
        check(product is not None and product.enterprise_id == runtime["enterprise_id"]
              and enterprise is not None and user is not None
              and user.email == prefix + "-provider@example.invalid" and not user.platform_role,
              "Fixture Product/Enterprise/User ownership changed; refusing cleanup")
        published = product.status == "published"
        check(not db.scalar(select(m.Order.id).where(
            m.Order.product_id == runtime["product_id"]).limit(1)),
            "Fixture has orders; preserving APISIX routes and PG rows")
        members = db.scalars(select(m.Membership).where(
            m.Membership.enterprise_id == runtime["enterprise_id"])).all()
        user_memberships = db.scalars(select(m.Membership).where(
            m.Membership.user_id == runtime["user_id"])).all()
        check(all(item.user_id == runtime["user_id"] for item in members)
              and all(item.enterprise_id == runtime["enterprise_id"] for item in user_memberships),
              "Fixture membership ownership changed; refusing cleanup")
        configs = db.scalars(select(m.version_gateway["Config"]).where(
            m.version_gateway["Config"].product_id == runtime["product_id"])).all()
        db_routes = db.scalars(select(m.ApiGatewayRoute).where(
            m.ApiGatewayRoute.product_id == runtime["product_id"])).all()
        check(len(configs) == 2 and {item.version_id for item in configs} == set(version_ids)
              and len(db_routes) == 2 and {item.id for item in db_routes} == set(route_ids)
              and all(item.route_id in route_ids for item in configs),
              "PG integration rows differ from exact fixture manifest; refusing cleanup")
        revision_ids = list(db.scalars(select(m.GatewayConfigRevision.id).where(
            m.GatewayConfigRevision.product_id == runtime["product_id"])))
        revisions = db.scalars(select(m.GatewayConfigRevision).where(
            m.GatewayConfigRevision.id.in_(revision_ids))).all()
        records = db.scalars(select(m.GatewayPublishRecord).where(
            m.GatewayPublishRecord.route_id.in_(route_ids))).all()
        check(all(item.route_id in route_ids for item in revisions)
              and all(item.revision_id in revision_ids for item in records),
              "Revision/publish-record foreign keys cross fixture ownership")

    live_routes = []
    for expected in routes:
        live = admin("GET", expected["route_key"])
        if live is not None:
            check(exact_route_owner(live, expected),
                  "Live APISIX route labels/identity mismatch; refusing cleanup")
            live_routes.append(expected["route_key"])
    if published:
        check(prefix + "-r2" in live_routes,
              "Published fixture has no live v2 APISIX route; refusing cleanup")
    for route_key in live_routes:
        admin("DELETE", route_key)
    check(all(admin("GET", route_key) is None for route_key in runtime["external_route_keys"]),
          "Journaled APISIX route remains after delete")

    with m.SessionLocal() as db:
        product_id = runtime["product_id"]
        configs = db.scalars(select(m.version_gateway["Config"]).where(
            m.version_gateway["Config"].product_id == product_id)).all()
        db_routes = db.scalars(select(m.ApiGatewayRoute).where(
            m.ApiGatewayRoute.product_id == product_id)).all()
        check(len(configs) == 2 and {item.version_id for item in configs} == set(version_ids)
              and len(db_routes) == 2 and {item.id for item in db_routes} == set(route_ids)
              and all(item.product_id == product_id for item in db_routes)
              and all(item.route_id in route_ids for item in configs),
              "PG integration rows differ from exact fixture manifest")
        revision_ids = list(db.scalars(select(m.GatewayConfigRevision.id).where(
            m.GatewayConfigRevision.product_id == product_id)))
        revisions = db.scalars(select(m.GatewayConfigRevision).where(
            m.GatewayConfigRevision.id.in_(revision_ids))).all()
        records = db.scalars(select(m.GatewayPublishRecord).where(
            m.GatewayPublishRecord.route_id.in_(route_ids))).all()
        check(all(item.route_id in route_ids for item in revisions)
              and all(item.revision_id in revision_ids for item in records),
              "Revision/publish-record foreign keys cross fixture ownership")
        db.execute(delete(m.version_gateway["Config"]).where(
            m.version_gateway["Config"].product_id == product_id,
            m.version_gateway["Config"].version_id.in_(version_ids)))
        db.execute(delete(m.GatewayPublishRecord).where(
            m.GatewayPublishRecord.route_id.in_(route_ids)))
        db.execute(delete(m.GatewayConfigRevision).where(
            m.GatewayConfigRevision.id.in_(revision_ids),
            m.GatewayConfigRevision.product_id == product_id,
            m.GatewayConfigRevision.route_id.in_(route_ids)))
        db.commit()
    registration.cleanup(state_path, token_path)
    return {"status": "cleaned", "fixture": state["prefix"],
            "external_routes_removed": len(runtime["external_route_keys"]),
            "live_routes_verified_before_cleanup": live_routes,
            "scope": "exact APISIX labels, integration rows, revision/publish records and registration fixture"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "cleanup"))
    parser.add_argument("--state-file", required=True, type=private_path)
    parser.add_argument("--token-file", required=True, type=private_path)
    args = parser.parse_args()
    check(args.state_file != args.token_file, "State and token paths must differ")
    result = prepare(args.state_file, args.token_file) if args.action == "prepare" else cleanup(
        args.state_file, args.token_file)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
