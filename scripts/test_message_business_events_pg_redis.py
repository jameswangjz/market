"""Opt-in PostgreSQL/Redis integration using a temporary schema and test keys.

TEST_DATABASE_URL=postgresql+psycopg://... TEST_REDIS_URL=redis://.../0 \
    python scripts/test_message_business_events_pg_redis.py

No application startup, public-schema migration, dispatcher, or supplier call.
"""
import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import secrets
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def lua_quota_script():
    source = (ROOT / "k8s" / "market-gateway-quota.lua").read_text(encoding="utf-8")
    match = re.search(r"local quota_script = \[\[(.*?)\]\]", source, re.DOTALL)
    if not match:
        raise RuntimeError("Cannot locate the repository's Lua quota script")
    return match.group(1)


def run_integration(database_url, redis_url):
    import redis
    from sqlalchemy import create_engine, func, select, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.schema import CreateSchema, DropSchema

    sys.path.insert(0, str(ROOT / "backend" / "tests"))
    from test_message_business_events import load_isolated_main

    pg_url = make_url(database_url)
    if pg_url.get_backend_name() != "postgresql":
        raise ValueError("TEST_DATABASE_URL must use PostgreSQL")
    pg_url = pg_url.set(drivername="postgresql+psycopg")
    token = secrets.token_hex(16)
    schema = "message_events_test_" + token
    route_key, consumer = "notification-test-" + token, "notification-test-" + token
    clock = datetime.now(timezone.utc)
    suffix = f"{route_key}:{consumer}"
    quota_keys = [
        f"market:apisix:quota:minute:{suffix}:{int(clock.timestamp()) // 60}",
        f"market:apisix:quota:day:{suffix}:{clock:%Y-%m-%d}",
        f"market:apisix:quota:month:{suffix}:{clock:%Y-%m}",
        f"market:apisix:quota:total:{suffix}",
    ]
    metadata_key = "market:apisix:credential:" + consumer
    client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
    connection_db = int(client.connection_pool.connection_kwargs.get("db", 0))
    if connection_db != 0:
        client.close()
        raise ValueError("Lua uses Redis DB 0; TEST_REDIS_URL must select DB 0")
    admin_engine = create_engine(pg_url, connect_args={"connect_timeout": 5})
    engine = None
    created_schema = False
    owned_keys = []
    module = None
    try:
        client.ping()
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        created_schema = True
        print("Temporary test schema: " + schema)
        engine = create_engine(pg_url, connect_args={"connect_timeout": 5, "options": "-csearch_path=" + schema}, execution_options={"schema_translate_map": {None: schema}})
        with engine.connect() as connection:
            if connection.scalar(text("SELECT current_schema()")) != schema:
                raise RuntimeError("Temporary search_path isolation failed")
        module = load_isolated_main()
        module.engine.dispose()
        module.engine = engine
        module.SessionLocal = sessionmaker(bind=engine, autoflush=False)
        module.Base.metadata.create_all(engine)
        Message, Receipt, Outbox = (module.message_center[key] for key in ("Message", "Receipt", "Outbox"))
        with module.SessionLocal() as db:
            db.add_all([module.User(id=uid, name=uid, password_hash="unused", email=uid + "@example.invalid") for uid in ("owner", "admin", "outsider")])
            db.add(module.Enterprise(id="tenant", name="Test tenant", credit_code=token))
            db.flush()
            db.add_all([module.Membership(user_id=uid, enterprise_id="tenant", role="enterprise_admin") for uid in ("owner", "admin")])
            db.add(module.Product(id="product", enterprise_id="tenant", name="Test SaaS", product_type="saas", provider_name="Test", provider_type="enterprise"))
            db.flush()
            db.add(module.SaaSProductVersion(id="version", product_id="product", version_code="v1", name="Test version"))
            db.add(module.ApiGatewayRoute(id="route", product_id="product", route_key=route_key, upstream_url="https://example.invalid", status="active"))
            db.flush()
            db.add(module.ApiCredential(id=token, route_id="route", enterprise_id="tenant", apisix_consumer_name=consumer, key_hash=token, created_by="owner@example.invalid", daily_quota=2, monthly_quota=3, total_quota=4))
            db.add_all([module.SaaSSubscription(id=sid, enterprise_id="tenant", product_id="product", version_id="version", status="active", expires_at=clock + timedelta(days=days), created_by="owner@example.invalid") for sid, days in (("expired", -1), ("expiring", 6))])
            db.commit()

        # Never overwrite an existing key. Every created key has a short TTL.
        for key in quota_keys:
            if not client.set(key, 0, nx=True, ex=600):
                raise RuntimeError("Unique test quota key already exists")
            owned_keys.append(key)
        with client.pipeline() as pipe:
            pipe.watch(metadata_key)
            if pipe.exists(metadata_key):
                raise RuntimeError("Unique test credential key already exists")
            pipe.multi()
            pipe.hset(metadata_key, mapping={"status": "active", "daily_quota": 2, "monthly_quota": 3, "total_quota": 4})
            pipe.expire(metadata_key, 600)
            pipe.execute()
        owned_keys.append(metadata_key)
        script = lua_quota_script()
        def evaluate_lua():
            try:
                return client.eval(script, 4, *quota_keys, 70, 86400, 2678400, 31536000, 2, 3, 4)
            finally:
                for key in owned_keys:
                    client.expire(key, 600)
        result = evaluate_lua()
        if int(result[0]) != 1 or list(map(int, client.mget(quota_keys))) != [1, 1, 1, 1]:
            raise AssertionError("Real Lua accepted-call counters differ")
        # Simulate the durable state after Lua has exhausted each quota.
        for key, used in zip(quota_keys, (1, 2, 3, 4)):
            if not client.set(key, used, xx=True, ex=600):
                raise RuntimeError("Test key expired before simulation")
        before_rejection = client.mget(quota_keys)
        result = evaluate_lua()
        if int(result[0]) != 0 or client.mget(quota_keys) != before_rejection:
            raise AssertionError("Real Lua rejection did not roll back INCRs")
        print("PASS real Redis Lua acceptance/rejection and manual exhausted-state keys")

        def counts():
            with module.SessionLocal() as db:
                return tuple(db.scalar(select(func.count()).select_from(model)) for model in (Message, Receipt, Outbox))

        with patch.dict(os.environ, {"REDIS_URL": redis_url}), patch.object(module, "now", return_value=clock):
            with module.SessionLocal() as db:
                summary = module.notification_scheduled_events(db)
                if summary != {"saas_candidates": 2, "quota_candidates": 3, "redis_unavailable": False}:
                    raise AssertionError("Unexpected scheduled candidates")
                db.flush()
                if db.scalar(select(func.count()).select_from(Message)) != 5:
                    raise AssertionError("Hook did not create all five messages")
                db.rollback()
            if counts() != (0, 0, 0):
                raise AssertionError("PG rollback left message/receipt/outbox rows")
            result = module.message_center["scheduled_scan"]()
            if result["status"] != "scanned":
                raise AssertionError("Scheduled wrapper did not commit")
            committed = counts()
            if committed[0:2] != (5, 10) or committed[2] < 10:
                raise AssertionError("Unexpected committed messages/receipts/outbox")
            module.message_center["scheduled_scan"]()
            if counts() != committed:
                raise AssertionError("Repeated scheduled scan created duplicates")
            with module.SessionLocal() as db:
                for message in db.scalars(select(Message)):
                    recipients = set(db.scalars(select(Receipt.user_id).where(Receipt.message_id == message.id)))
                    if recipients != {"owner", "admin"} or message.tenant_id != "tenant":
                        raise AssertionError("Tenant/recipient scope differs")
                if db.get(module.ApiCredential, token).status != "active" or any(item.status != "active" for item in db.scalars(select(module.SaaSSubscription))):
                    raise AssertionError("Hook mutated business states")
        print("PASS real PostgreSQL hook/create/commit, rollback, Outbox, deduplication, and tenant scope")
    finally:
        cleanup_errors = []
        if engine is not None:
            engine.dispose()
        if created_schema:
            try:
                with admin_engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
            except Exception as exc:
                cleanup_errors.append("temporary schema: " + type(exc).__name__)
        if owned_keys:
            try:
                client.delete(*owned_keys)
            except redis.RedisError as exc:
                cleanup_errors.append("test keys: " + type(exc).__name__)
        client.close()
        admin_engine.dispose()
        if module is not None:
            package = module.__package__
            for name in list(sys.modules):
                if name == package or name.startswith(package + "."):
                    del sys.modules[name]
        if cleanup_errors:
            raise RuntimeError("Cleanup failed for " + ", ".join(cleanup_errors))
        print("Temporary schema and owned test keys cleaned up")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-script", action="store_true", help="Validate Lua extraction without opening any connection")
    args = parser.parse_args()
    if args.check_script:
        script = lua_quota_script()
        if "redis.call('DECR', KEYS[4])" not in script:
            raise AssertionError("Repository Lua total counter rollback is missing")
        print("Repository Lua extraction OK; no database or Redis connection opened")
        return
    database_url = os.getenv("TEST_DATABASE_URL")
    redis_url = os.getenv("TEST_REDIS_URL")
    if not database_url or not redis_url:
        parser.error("Set TEST_DATABASE_URL and TEST_REDIS_URL explicitly; production URL variables are never used")
    try:
        run_integration(database_url, redis_url)
    except Exception as exc:
        # Connection errors can contain URL credentials; do not print their text.
        print("Integration failed: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
