"""Authenticated terminal gateway facts; no billing/quota changes or SLA tier lookup.

install(ns) registers an internal, separate-Secret authenticated receiver. Direct
ingest(db, events) only flushes; the HTTP receiver owns an atomic transaction.
aggregate returns RequestMetrics and defaults to an unconfirmed source watermark.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, String, Text, and_, or_, not_, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import mapped_column
from starlette.concurrency import run_in_threadpool

from .sla_online_facts import EXCLUSIONS, SYSTEM_FAILURES, RequestFact, aggregate_requests


ENDPOINT = "/internal/sla/gateway-events"
MAX_BYTES = 65536
MAX_EVENTS = 100
ID = r"^[A-Za-z0-9_.:-]{0,180}$"


class GatewayEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    event_id: StrictStr = Field(pattern=r"^gw_[a-f0-9]{32}$")
    request_id: StrictStr = Field(pattern=r"^gw_[a-f0-9]{32}$")
    product_id: StrictStr = Field(pattern=ID)
    version_id: StrictStr = Field(default="", pattern=ID)
    version_code: StrictStr = Field(default="", pattern=ID)
    entry_id: StrictStr = Field(pattern=ID)
    route_id: StrictStr = Field(pattern=ID)
    credential_id: StrictStr = Field(default="", pattern=ID)
    completed_at_ms: StrictInt = Field(ge=0, le=4102444800000)
    status_code: StrictInt = Field(ge=100, le=599)
    duration_ms: float | None = Field(default=None, ge=0, le=86400000)
    authorized: StrictBool | None = None
    published_route: StrictBool | None = None
    traffic: StrictStr = "business"
    traffic_evidence_id: StrictStr = Field(default="", pattern=ID)
    reason: StrictStr = "unknown"
    responsibility: StrictStr = "unknown"
    evidence_id: StrictStr = Field(default="", pattern=ID)
    redirect_allowed: StrictBool = False
    policy_version: StrictStr = Field(default="sla-online-v1.1", pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    upstream_statuses: list[StrictInt] = Field(default_factory=list, max_length=16)
    producer_id: StrictStr = Field(default="apisix", pattern=r"^[A-Za-z0-9_.:-]{1,180}$")
    sequence: StrictInt = Field(default=0, ge=0, le=2147483647)


class SourceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    producer_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_.:-]{1,180}$")
    window_slot: StrictInt = Field(ge=0, le=13674816)
    failure_count: StrictInt = Field(ge=0, le=2147483647)


class WindowSeal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    producer_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_.:-]{1,180}$")
    window_slot: StrictInt = Field(ge=0, le=13674816)
    expected_count: StrictInt = Field(ge=0, le=2147483647)
    last_sequence: StrictInt = Field(ge=0, le=2147483647)
    producer_started_at_ms: StrictInt = Field(ge=0, le=4102444800000)
    closed_at_ms: StrictInt = Field(ge=0, le=4102444800000)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical(event):
    return json.dumps(event.model_dump(), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _event(raw):
    item = GatewayEvent.model_validate(raw)
    if item.event_id != item.request_id or any(not 100 <= code <= 599 for code in item.upstream_statuses):
        raise ValueError("invalid terminal identity")
    if item.traffic not in {"business", "probe", "isolated_test", "internal"}:
        raise ValueError("invalid traffic")
    if item.traffic != "business" and not item.traffic_evidence_id:
        raise ValueError("traffic exclusion requires server evidence")
    if item.reason not in set(EXCLUSIONS.values()) | SYSTEM_FAILURES | {"unknown"}:
        raise ValueError("unknown reason")
    if item.responsibility not in {"platform", "provider", "caller", "unknown"}:
        raise ValueError("unknown responsibility")
    if (item.reason != "unknown" or item.responsibility != "unknown") and not item.evidence_id:
        raise ValueError("attribution requires evidence")
    return item


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware period required")
    return value.astimezone(timezone.utc)


def install(ns):
    class SourceWindow(ns["Base"]):
        __tablename__ = "sla_gateway_source_windows"
        producer_id = mapped_column(String(180), primary_key=True)
        window_slot = mapped_column(Integer, primary_key=True)
        failure_count = mapped_column(Integer, nullable=False, default=0)
        received_count = mapped_column(Integer, nullable=False, default=0)
        complete = mapped_column(Boolean, nullable=False, default=False)
        expected_count = mapped_column(Integer, nullable=True)
        producer_started_at_ms = mapped_column(BigInteger, nullable=True)
        closed_at_ms = mapped_column(BigInteger, nullable=True)
        seal_sha256 = mapped_column(String(64), nullable=True)
        reason = mapped_column(String(80), nullable=False, default="delivery_integrity_unconfirmed")
        updated_at = mapped_column(DateTime(timezone=True), nullable=False)

    class GatewayFact(ns["Base"]):
        __tablename__ = "sla_gateway_terminal_events"
        event_id = mapped_column(String(35), primary_key=True)
        request_id = mapped_column(String(35), nullable=False, unique=True)
        source = mapped_column(String(20), nullable=False, default="gateway")
        producer_id = mapped_column(String(180), nullable=False)
        window_slot = mapped_column(Integer, nullable=False)
        sequence = mapped_column(Integer, nullable=False, default=0)
        product_id = mapped_column(String(180), nullable=False, index=True)
        version_id = mapped_column(String(180), nullable=False, default="")
        version_code = mapped_column(String(180), nullable=False, default="")
        entry_id = mapped_column(String(180), nullable=False)
        route_id = mapped_column(String(180), nullable=False)
        credential_id = mapped_column(String(180), nullable=False, default="")
        enterprise_id = mapped_column(String(180), nullable=False, default="")
        scope_resolved = mapped_column(Boolean, nullable=False, default=False)
        completed_at = mapped_column(DateTime(timezone=True), nullable=False)
        received_at = mapped_column(DateTime(timezone=True), nullable=False)
        status_code = mapped_column(Integer, nullable=False)
        duration_ms = mapped_column(Float, nullable=True)
        authorized = mapped_column(Boolean, nullable=True)
        published_route = mapped_column(Boolean, nullable=True)
        traffic = mapped_column(String(20), nullable=False)
        traffic_evidence_id = mapped_column(String(180), nullable=False, default="")
        reason = mapped_column(String(80), nullable=False)
        responsibility = mapped_column(String(20), nullable=False)
        evidence_id = mapped_column(String(180), nullable=False, default="")
        redirect_allowed = mapped_column(Boolean, nullable=False)
        policy_version = mapped_column(String(80), nullable=False)
        upstream_statuses_json = mapped_column(Text, nullable=False)
        content_sha256 = mapped_column(String(64), nullable=False)
        __table_args__ = (
            Index("ix_sla_gateway_scope_period", "product_id", "version_id", "entry_id", "completed_at"),
            Index("uq_sla_gateway_source_sequence", "producer_id", "window_slot", "sequence", unique=True,
                  sqlite_where=text("sequence > 0"), postgresql_where=text("sequence > 0")),)

    def resolve(db, event):
        custom = ns.get("sla_gateway_event_scope")
        if callable(custom):
            return custom(db, event)
        Route, Version, Credential = (ns.get(key) for key in ("ApiGatewayRoute", "ProductReleaseVersion", "ApiCredential"))
        if Route is None or Version is None:
            return None
        route = db.scalar(select(Route).where(Route.route_key == event.route_id))
        if route is None:
            route = db.get(Route, event.route_id)
        if route is None or route.product_id != event.product_id:
            return None
        version = db.scalar(select(Version).where(Version.product_id == route.product_id,
                                                   Version.version_code == route.version))
        if version is None or (event.version_id and version.id != event.version_id) or (event.version_code and event.version_code != version.version_code):
            return None
        credential = db.get(Credential, event.credential_id) if Credential is not None and event.credential_id else None
        if event.credential_id and (credential is None or credential.route_id != route.id):
            return None
        return {"version_id": version.id, "enterprise_id": credential.enterprise_id if credential else "",
                "credential_id": credential.id if credential else ""}

    def source_window(db, producer, slot):
        identity = (producer, slot)
        window = db.get(SourceWindow, identity, with_for_update=True)
        if window is None:
            try:
                with db.begin_nested():
                    window = SourceWindow(producer_id=producer, window_slot=slot,
                        failure_count=0, received_count=0, complete=False, updated_at=datetime.now(timezone.utc))
                    db.add(window)
                    db.flush()
            except IntegrityError:
                window = db.get(SourceWindow, identity, with_for_update=True)
                if window is None:
                    raise
        return window

    def reconcile(db, window):
        rows = (GatewayFact.producer_id == window.producer_id, GatewayFact.window_slot == window.window_slot)
        count, smallest, largest = db.execute(select(func.count(), func.min(GatewayFact.sequence),
            func.max(GatewayFact.sequence)).select_from(GatewayFact).where(*rows)).one()
        window.received_count = count
        expected = window.expected_count
        window.complete = (expected is not None and count == expected
            and window.producer_started_at_ms <= window.window_slot * 300000
            and window.closed_at_ms >= (window.window_slot + 1) * 300000
            and (expected == 0 or smallest == 1 and largest == expected))
        window.reason = "sequence_count_verified" if window.complete else "delivery_integrity_unconfirmed"
        window.updated_at = datetime.now(timezone.utc)

    def ingest(db, events, *, source_statuses=None, window_seals=None):
        if not isinstance(events, list) or len(events) > MAX_EVENTS or not (events or source_statuses or window_seals):
            raise HTTPException(422, "Invalid gateway batch")
        try:
            items = [_event(event) for event in events]
            if source_statuses is not None and (not isinstance(source_statuses, list) or len(source_statuses) > 100):
                raise ValueError("invalid source status batch")
            statuses = [SourceStatus.model_validate(value) for value in source_statuses or []]
            if window_seals is not None and (not isinstance(window_seals, list) or len(window_seals) > 100):
                raise ValueError("invalid window seal batch")
            seals = [WindowSeal.model_validate(value) for value in window_seals or []]
            if any(seal.expected_count != seal.last_sequence or seal.closed_at_ms < (seal.window_slot + 1) * 300000 for seal in seals):
                raise ValueError("invalid window seal")
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "Invalid gateway event") from None
        unique = {}
        for item in items:
            content = _canonical(item)
            digest = hashlib.sha256(content.encode()).hexdigest()
            if item.event_id in unique and unique[item.event_id][1] != digest:
                raise HTTPException(409, "Gateway event identity conflict")
            unique[item.event_id] = (item, digest)
        inserted = duplicates = 0
        affected = set()
        # Full batch rollback on conflict, including calls from non-HTTP callers.
        with db.begin_nested():
            for item, digest in unique.values():
                old = db.get(GatewayFact, item.event_id)
                if old is not None:
                    if old.content_sha256 != digest:
                        raise HTTPException(409, "Gateway event identity conflict")
                    duplicates += 1
                    continue
                scope = resolve(db, item)
                scope_ok = (isinstance(scope, dict) and isinstance(scope.get("version_id"), str)
                            and bool(scope["version_id"]) and isinstance(scope.get("enterprise_id"), str)
                            and (not item.credential_id or scope.get("credential_id") == item.credential_id))
                values = item.model_dump(exclude={"upstream_statuses", "completed_at_ms"})
                values.update(version_id=scope["version_id"] if scope_ok else item.version_id,
                    enterprise_id=scope["enterprise_id"] if scope_ok else "", scope_resolved=scope_ok,
                    window_slot=item.completed_at_ms // 300000,
                    completed_at=datetime.fromtimestamp(item.completed_at_ms / 1000, timezone.utc),
                    received_at=datetime.now(timezone.utc), status_code=item.status_code,
                    upstream_statuses_json=json.dumps(item.upstream_statuses), content_sha256=digest)
                row = GatewayFact(**values)
                try:
                    with db.begin_nested():
                        db.add(row)
                        db.flush()
                    inserted += 1
                    window = source_window(db, item.producer_id, item.completed_at_ms // 300000)
                    window.complete = False
                    window.updated_at = datetime.now(timezone.utc)
                    affected.add((window.producer_id, window.window_slot))
                except IntegrityError:
                    old = db.get(GatewayFact, item.event_id)
                    if old is None:
                        if item.sequence > 0 and db.scalar(select(GatewayFact.event_id).where(
                                GatewayFact.producer_id == item.producer_id,
                                GatewayFact.window_slot == item.completed_at_ms // 300000,
                                GatewayFact.sequence == item.sequence)):
                            raise HTTPException(409, "Gateway source sequence conflict") from None
                        raise
                    if old.content_sha256 != digest:
                        raise HTTPException(409, "Gateway event identity conflict")
                    duplicates += 1
            for status in statuses:
                window = source_window(db, status.producer_id, status.window_slot)
                window.failure_count = max(window.failure_count, status.failure_count)
                window.updated_at = datetime.now(timezone.utc)
                affected.add((window.producer_id, window.window_slot))
            for seal in seals:
                window = source_window(db, seal.producer_id, seal.window_slot)
                digest = hashlib.sha256(json.dumps(seal.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if window.seal_sha256 is not None and window.seal_sha256 != digest:
                    raise HTTPException(409, "Gateway window seal conflict")
                window.expected_count = seal.expected_count
                window.producer_started_at_ms = seal.producer_started_at_ms
                window.closed_at_ms = seal.closed_at_ms
                window.seal_sha256 = digest
                affected.add((window.producer_id, window.window_slot))
            db.flush()
            for producer, slot in affected:
                reconcile(db, source_window(db, producer, slot))
            db.flush()
        return {"accepted": inserted, "duplicates": duplicates}

    async def receiver(request: Request):
        secret = os.getenv("SLA_GATEWAY_INGEST_SECRET", "")
        if not 32 <= len(secret) <= 512:
            raise HTTPException(503, "Gateway ingestion not configured")
        supplied = request.headers.get("X-SLA-Gateway-Key", "")
        if len(supplied) > 512 or not hmac.compare_digest(supplied.encode(), secret.encode()):
            raise HTTPException(401, "Gateway authentication required")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "JSON required")
        size = request.headers.get("content-length")
        if size is not None and (not size.isdigit() or int(size) > MAX_BYTES):
            raise HTTPException(413, "Gateway payload too large")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "Gateway payload too large")
            body.extend(chunk)
        try:
            payload = json.loads(body, object_pairs_hook=_unique)
            if not isinstance(payload, dict) or not {"events"} <= set(payload) <= {"events", "source_statuses", "window_seals"}:
                raise ValueError("invalid envelope")
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "Invalid gateway payload") from None
        def persist():
            with ns["SessionLocal"].begin() as db:
                return ingest(db, payload["events"], source_statuses=payload.get("source_statuses"), window_seals=payload.get("window_seals"))
        return await run_in_threadpool(persist)

    ns["app"].post(ENDPOINT, include_in_schema=False)(receiver)

    def expected_producers(db, start, end):
        # Registry is written by the actual APISIX worker before queue publication.
        # Never infer retirement from an expired heartbeat or a missing worker.
        import redis
        client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://market-redis:6379/0"),
            decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
        try:
            entries = client.hgetall("market:sla:terminal:producers")
            result = []
            for producer, raw in entries.items():
                value = json.loads(raw)
                if isinstance(value, int) and not isinstance(value, bool):
                    value = {"started_at_ms": value}
                if (not isinstance(value, dict) or type(value.get("started_at_ms")) is not int
                        or not 0 <= value["started_at_ms"] <= 4102444800000):
                    return None
                stopped = value.get("stopped_at_ms")
                if stopped is not None and (type(stopped) is not int or not value.get("retirement_evidence_id")
                        or stopped < value["started_at_ms"]):
                    return None
                result.append(dict(value, producer_id=producer))
            return result
        except (redis.RedisError, ValueError, TypeError):
            return None
        finally:
            client.close()

    if not callable(ns.get("sla_gateway_expected_producers")):
        ns["sla_gateway_expected_producers"] = expected_producers

    def aggregate(db, *, product_id, version_id, entry_id, start, end,
                  enterprise_id=None, credential_id=None, source_available=False):
        start, end = _aware(start), _aware(end)
        if start >= end or not all(isinstance(value, str) and value for value in (product_id, version_id, entry_id)):
            raise ValueError("invalid scoped period")
        period = (GatewayFact.product_id == product_id, GatewayFact.completed_at >= start,
                  GatewayFact.completed_at < end)
        trusted_nonbusiness = and_(GatewayFact.traffic.in_(("probe", "isolated_test", "internal")),
                                   GatewayFact.traffic_evidence_id != "")
        unresolved = db.scalar(select(GatewayFact.event_id).where(*period,
            GatewayFact.scope_resolved.is_(False), not_(or_(trusted_nonbusiness, and_(
                GatewayFact.status_code == 401, GatewayFact.authorized.is_(False),
                GatewayFact.reason == "buyer_credentials", GatewayFact.responsibility == "caller",
                GatewayFact.evidence_id != "")))).limit(1))
        # A live receiver is not completeness. Every expected producer must have
        # an immutable closed window whose distinct sequence set is exactly 1..N.
        roster = ns.get("sla_gateway_expected_producers")
        producers = roster(db, start, end) if callable(roster) else None
        integrity_complete = isinstance(producers, (list, tuple)) and bool(producers)
        covered = []
        if integrity_complete:
            for producer in producers:
                identity = producer if isinstance(producer, str) else producer["producer_id"]
                begin_ms = int(start.timestamp() * 1000)
                end_ms = int(end.timestamp() * 1000)
                if isinstance(producer, dict):
                    begin_ms = max(begin_ms, producer["started_at_ms"])
                    end_ms = min(end_ms, producer.get("stopped_at_ms") or end_ms)
                if begin_ms >= end_ms:
                    continue
                covered.append((begin_ms, end_ms))
                for slot in range(begin_ms // 300000, math.ceil(end_ms / 300000)):
                    window = db.get(SourceWindow, (identity, slot))
                    # A complete sequence for the partial startup window covers
                    # requests starting after worker birth, never earlier history.
                    complete = window is not None and (window.complete or (
                        window.expected_count is not None and window.received_count == window.expected_count
                        and window.producer_started_at_ms <= max(begin_ms, slot * 300000)
                        and window.closed_at_ms >= (slot + 1) * 300000
                        and (window.expected_count == 0 or db.execute(select(func.min(GatewayFact.sequence),
                            func.max(GatewayFact.sequence)).where(GatewayFact.producer_id == identity,
                            GatewayFact.window_slot == slot)).one() == (1, window.expected_count))))
                    if not complete:
                        integrity_complete = False
                        break
                if not integrity_complete:
                    break
            cursor = int(start.timestamp() * 1000)
            for left, right in sorted(covered):
                if left > cursor:
                    break
                cursor = max(cursor, right)
            integrity_complete = integrity_complete and cursor >= int(end.timestamp() * 1000)
        statement = select(GatewayFact).where(*period, GatewayFact.version_id == version_id,
                                               GatewayFact.entry_id == entry_id)
        if enterprise_id is not None:
            statement = statement.where(GatewayFact.enterprise_id == enterprise_id)
        if credential_id is not None:
            statement = statement.where(GatewayFact.credential_id == credential_id)
        def facts():
            for row in db.scalars(statement.execution_options(yield_per=500)):
                yield RequestFact(row.request_id, "gateway", int(row.status_code), row.duration_ms,
                    row.authorized if row.scope_resolved else None, row.published_route,
                    traffic=row.traffic, trusted_evidence=bool(row.evidence_id or row.traffic_evidence_id),
                    reason=row.reason, responsibility=row.responsibility,
                    redirect_allowed=row.redirect_allowed, policy_version=row.policy_version)
        return aggregate_requests(facts(), source_available=source_available is True and unresolved is None and integrity_complete)

    hooks = {"Model": GatewayFact, "SourceWindow": SourceWindow, "aggregate": aggregate, "ingest": ingest,
             "expected_producers": expected_producers, "Event": GatewayEvent}
    ns["sla_gateway_events"] = hooks
    return hooks
