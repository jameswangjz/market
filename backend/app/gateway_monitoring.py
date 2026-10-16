"""Read-only gateway inventory and durable, publish-record keyed alert actions.

Alert state is materialized only on an action; every failed publish is a distinct
incident, so closing an incident never masks a later failed attempt.
"""
from datetime import datetime, timedelta
from enum import IntEnum
from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, case, func, literal, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class MonitoringBase(DeclarativeBase):
    pass


class GatewayAlertState(MonitoringBase):
    __tablename__ = "gateway_alert_states"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    route_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(30), default="open")
    version: Mapped[int] = mapped_column(Integer, default=0)


class GatewayAlertAction(MonitoringBase):
    __tablename__ = "gateway_alert_actions"
    __table_args__ = (UniqueConstraint("alert_id", "request_id", name="uq_gateway_alert_request"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    alert_id: Mapped[str] = mapped_column(String(36), index=True)
    route_id: Mapped[str] = mapped_column(String(36), index=True)
    request_id: Mapped[str] = mapped_column(String(120))
    actor: Mapped[str] = mapped_column(String(180))
    action: Mapped[str] = mapped_column(String(30))
    note: Mapped[str] = mapped_column(Text)
    before_status: Mapped[str] = mapped_column(String(30))
    after_status: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlertActionBody(BaseModel):
    request_id: str = Field(min_length=1, max_length=120, pattern=r"^\S+$")
    expected_version: int = Field(ge=0)
    note: str = Field(default="", max_length=2000)


class PageSize(IntEnum):
    ten = 10
    twenty = 20
    fifty = 50
    hundred = 100


def ensure_schema(engine):
    # Separate metadata keeps startup's general create_all from racing this DDL.
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(73481204)"))
        MonitoringBase.metadata.create_all(connection)


def page_result(db, stmt, page, page_size):
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    page = min(page, max(1, (total + page_size - 1) // page_size))
    return db.execute(stmt.limit(page_size).offset((page - 1) * page_size)).all(), {
        "total": total, "page": page, "page_size": page_size}


def install(ns):
    app = ns["app"]
    Route, Product, Enterprise = ns["ApiGatewayRoute"], ns["Product"], ns["Enterprise"]
    Publish, Revision, Usage = ns["GatewayPublishRecord"], ns["GatewayConfigRevision"], ns["ApiUsage"]
    auth, session = ns["current_user"], ns["db_session"]

    def authorize(user):
        ns["require_platform_admin"](user)

    def route_for(db, route_id, user):
        authorize(user)
        route = db.get(Route, route_id)
        if route is None:
            raise HTTPException(404, "Route not found")
        return route

    def health_columns():
        Observation = ns.get("application_health", {}).get("Observation")
        Config = ns.get("version_gateway", {}).get("Config")
        Version = ns["ProductReleaseVersion"]
        if Observation is None or Config is None:
            return literal("unknown"), literal(None), literal("unavailable")
        matched = (Config.route_id == Route.id, Config.product_id == Route.product_id,
                   Version.id == Config.version_id, Version.product_id == Route.product_id,
                   Version.version_code == Route.version, Config.version_code == Route.version,
                   Config.upstream_url == Route.upstream_url, Config.health_path == Route.health_path,
                   Config.health_method == Route.health_method)
        def latest(column):
            return select(column).select_from(Observation).join(Config,
                (Config.version_id == Observation.version_id) & (Config.product_id == Observation.product_id)).join(
                Version, Version.id == Config.version_id).where(*matched).order_by(
                Observation.last_checked_at.desc(), Observation.id.desc()).limit(1).correlate(Route).scalar_subquery()
        checked, status = latest(Observation.last_checked_at), latest(Observation.last_status)
        fresh = (checked >= ns["now"]() - timedelta(minutes=10)) & (checked >= Route.updated_at)
        health = case((fresh & (status == "healthy"), "healthy"),
                      (fresh & (status == "failed"), "unhealthy"), else_="unknown")
        reason = case((checked.is_(None), "not_observed"), (~fresh, "stale_or_configuration_changed"),
                      (status.in_(("healthy", "failed")), "observed"), else_="probe_unknown")
        return health, checked, reason

    def routes(page: int | None = Query(None, ge=1, le=10000),
               page_size: PageSize | None = None,
               route_name: str = Query("", max_length=200),
               product_name: str = Query("", max_length=200),
               provider: str = Query("", max_length=200),
               product_status: Literal["all", "published", "draft", "rejected", "pending_review", "quality_review", "security_review", "operation_review", "pending_integration", "security_unpublished", "disabled", "unpublished"] | None = None,
               route_status: Literal["all", "draft", "active", "disabled", "pending_health", "publish_failed"] = "all",
               health_status: Literal["all", "healthy", "unhealthy", "unknown"] = "all",
               user=Depends(auth), db: Session = Depends(session)):
        authorize(user)
        paginated = page is not None or page_size is not None
        health, checked, reason = health_columns()
        stmt = select(Route, Product, Enterprise.name, health, checked, reason).join(Product, Product.id == Route.product_id).outerjoin(Enterprise, Enterprise.id == Product.enterprise_id)
        for column, value in ((Route.route_key, route_name), (Product.name, product_name), (Enterprise.name, provider)):
            if value.strip():
                stmt = stmt.where(column.icontains(value.strip(), autoescape=True))
        status = product_status if product_status is not None else ("published" if paginated else "all")
        if status != "all":
            stmt = stmt.where(Product.status == status)
        if route_status != "all":
            stmt = stmt.where(Route.status == route_status)
        if health_status != "all":
            stmt = stmt.where(health == health_status)
        stmt = stmt.order_by(Route.updated_at.desc(), Route.id.desc())
        rows, result = page_result(db, stmt, page or 1, page_size or 20) if paginated else (db.execute(stmt).all(), {})
        result["items"] = [dict(ns["gateway_route_out"](r, p), provider_name=name or "", product_status=p.status,
                                route_status=r.status, health_status=observed,
                                health_source="application_health_observations" if checked_at else "none",
                                health_checked_at=checked_at, health_reason=why,
                                health_max_age_seconds=600) for r, p, name, observed, checked_at, why in rows]
        return result

    def alert_out(record, state):
        return dict(id=record.id, publish_record_id=record.id, route_id=record.route_id,
                    severity="error", title="APISIX route publish failed", message=record.error_message,
                    created_at=record.created_at, status=state.status if state else "open", version=state.version if state else 0)

    def alerts(route_id: str | None = None, page: int = Query(1, ge=1, le=10000),
               page_size: PageSize = PageSize.twenty,
               status: Literal["all", "open", "acknowledged", "processing", "closed"] = "all",
               user=Depends(auth), db: Session = Depends(session)):
        authorize(user)
        if route_id is not None:
            route_for(db, route_id, user)
        stmt = select(Publish, GatewayAlertState).outerjoin(GatewayAlertState, GatewayAlertState.id == Publish.id).where(Publish.status == "failed")
        if route_id is not None:
            stmt = stmt.where(Publish.route_id == route_id)
        if status != "all":
            stmt = stmt.where(func.coalesce(GatewayAlertState.status, "open") == status)
        rows, result = page_result(db, stmt.order_by(Publish.created_at.desc(), Publish.id.desc()), page, page_size)
        result["items"] = [alert_out(r, s) for r, s in rows]
        return result

    @app.get("/api/gateway/routes/{route_id}/alerts")
    def route_alerts(route_id: str, page: int = Query(1, ge=1, le=10000),
                     page_size: PageSize = PageSize.twenty,
                     status: Literal["all", "open", "acknowledged", "processing", "closed"] = "all",
                     user=Depends(auth), db: Session = Depends(session)):
        return alerts(route_id, page, page_size, status, user, db)

    @app.get("/api/gateway/routes/{route_id}/revisions")
    def revisions(route_id: str, page: int = Query(1, ge=1, le=10000),
                  page_size: PageSize = PageSize.twenty,
                  user=Depends(auth), db: Session = Depends(session)):
        route_for(db, route_id, user)
        stmt = select(Publish, Revision.revision).outerjoin(Revision, (Revision.id == Publish.revision_id) & (Revision.route_id == route_id)).where(Publish.route_id == route_id)
        rows, result = page_result(db, stmt.order_by(Publish.created_at.desc(), Publish.id.desc()), page, page_size)
        result["items"] = [dict(id=r.id, route_id=r.route_id, revision_id=r.revision_id, revision=number, target=r.target,
                                status=r.status, error_message=r.error_message, created_by=r.created_by,
                                created_at=r.created_at, completed_at=r.completed_at) for r, number in rows]
        return result

    @app.get("/api/gateway/routes/{route_id}/stats")
    def stats(route_id: str, page: int = Query(1, ge=1, le=10000),
              page_size: PageSize = PageSize.twenty,
              window_hours: int = Query(24, ge=1, le=2160),
              source: Literal["auto", "api_usage", "sla_gateway_terminal_events"] = "auto",
              user=Depends(auth), db: Session = Depends(session)):
        route = route_for(db, route_id, user)
        end = ns["now"]()
        start = end - timedelta(hours=window_hours)
        conditions = (Usage.route_id == route_id, Usage.created_at >= start, Usage.created_at < end)
        Native = ns.get("sla_gateway_events", {}).get("Model")
        Version = ns["ProductReleaseVersion"]
        native_conditions = () if Native is None else (
            Native.route_id.in_((route.id, route.route_key)), Native.product_id == route.product_id,
            Native.version_id.in_(select(Version.id).where(Version.product_id == route.product_id,
                                                         Version.version_code == route.version)),
            Native.scope_resolved.is_(True),
            Native.completed_at >= start, Native.completed_at < end)
        native_observed = Native is not None and db.scalar(select(Native.event_id).where(*native_conditions).limit(1)) is not None
        if source == "auto":
            source = "sla_gateway_terminal_events" if ns["apisix_native_upstream"]() or native_observed else "api_usage"
        native = source == "sla_gateway_terminal_events"
        if native and Native is None:
            return {"route_id": route_id, "source": source, "window_start": start, "window_end": end,
                    "collection_status": "unknown", "coverage": "unknown", "counts_complete": False,
                    "total": 0, "page": 1, "page_size": page_size, "items": [],
                    "summary": {"total": None, "success": None, "error": None, "avg_latency_ms": None}}
        Model = Native if native else Usage
        identity, instant, duration = (Native.event_id, Native.completed_at, Native.duration_ms) if native else (Usage.id, Usage.created_at, Usage.latency_ms)
        conditions = native_conditions if native else conditions
        count, success, latency = db.execute(select(func.count(identity), func.sum(case((Model.status_code < 400, 1), else_=0)), func.avg(duration)).where(*conditions)).one()
        rows, result = page_result(db, select(Model).where(*conditions).order_by(instant.desc(), identity.desc()), page, page_size)
        result.update(route_id=route_id, source=source, window_start=start, window_end=end,
                      coverage="unknown", counts_complete=False,
                      count_scope="observed_terminal_events" if native else "observed_legacy_requests",
                      collection_status="observed" if count else "unknown", summary={"total": count if count else None,
                      "success": int(success) if count else None, "error": count - int(success) if count else None,
                      "avg_latency_ms": round(latency, 1) if latency is not None else None})
        result["items"] = [dict(id=r.event_id, method=None, path=None, status_code=r.status_code,
                                latency_ms=r.duration_ms, created_at=r.completed_at, traffic=r.traffic,
                                source=source) if native else {k: getattr(r, k) for k in
                                ("id", "method", "path", "status_code", "latency_ms", "created_at")} for (r,) in rows]
        return result

    @app.get("/api/gateway/routes/{route_id}/alerts/{alert_id}/audit")
    def history(route_id: str, alert_id: str, page: int = Query(1, ge=1, le=10000),
                page_size: PageSize = PageSize.twenty,
                user=Depends(auth), db: Session = Depends(session)):
        route_for(db, route_id, user)
        incident(db, route_id, alert_id)
        rows, result = page_result(db, select(GatewayAlertAction).where(GatewayAlertAction.alert_id == alert_id, GatewayAlertAction.route_id == route_id).order_by(GatewayAlertAction.created_at.desc(), GatewayAlertAction.id.desc()), page, page_size)
        result["items"] = [{c.name: getattr(r, c.name) for c in GatewayAlertAction.__table__.columns} for (r,) in rows]
        return result

    def incident(db, route_id, alert_id):
        record = db.scalar(select(Publish).where(Publish.id == alert_id, Publish.route_id == route_id, Publish.status == "failed"))
        if record is None:
            raise HTTPException(404, "Alert not found for route")
        return record

    @app.post("/api/gateway/routes/{route_id}/alerts/{alert_id}/{action}")
    def act(route_id: str, alert_id: str, action: Literal["acknowledge", "process", "close"],
            body: AlertActionBody, user=Depends(auth), db: Session = Depends(session)):
        route_for(db, route_id, user)
        record = incident(db, route_id, alert_id)
        note = body.note.strip()
        if action == "close" and not note:
            raise HTTPException(422, "Closing requires a reason")
        actor = user.id
        prior = db.scalar(select(GatewayAlertAction).where(GatewayAlertAction.alert_id == alert_id, GatewayAlertAction.request_id == body.request_id))
        if prior:
            if (prior.actor, prior.action, prior.note, prior.version - 1) != (actor, action, note, body.expected_version):
                raise HTTPException(409, "Request ID already used with different input")
            return {"id": alert_id, "route_id": route_id, "status": prior.after_status, "version": prior.version, "replayed": True}
        try:
            state = db.get(GatewayAlertState, alert_id)
            if state is None:
                state = GatewayAlertState(id=alert_id, route_id=route_id, status="open", version=0)
                db.add(state)
                db.flush()
            before = state.status
            target = {"acknowledge": "acknowledged", "process": "processing", "close": "closed"}[action]
            if state.version != body.expected_version or (action != "close" and before != {"acknowledge": "open", "process": "acknowledged"}[action]) or before == "closed":
                raise HTTPException(409, "Alert changed or transition is invalid")
            changed = db.execute(update(GatewayAlertState).where(GatewayAlertState.id == alert_id, GatewayAlertState.version == body.expected_version).values(status=target, version=body.expected_version + 1).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise HTTPException(409, "Alert changed; refresh before retrying")
            db.add(GatewayAlertAction(id=ns["secrets"].token_hex(16), alert_id=alert_id, route_id=route_id,
                                     request_id=body.request_id, actor=actor, action=action, note=note,
                                     before_status=before, after_status=target, version=body.expected_version + 1, created_at=ns["now"]()))
            ns["audit"](db, actor, "gateway_alert_" + action, "gateway_alert", alert_id, note,
                        category="ops", business_domain="gateway", request_id=body.request_id,
                        before={"route_id": route_id, "status": before, "version": body.expected_version},
                        after={"route_id": route_id, "status": target, "version": body.expected_version + 1})
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "Concurrent alert action; retry with the same request ID") from exc
        except OperationalError as exc:
            db.rollback()
            code = getattr(exc.orig, "sqlstate", None)
            if code not in {"40001", "40P01", "55P03"} and "locked" not in str(exc.orig).lower():
                raise
            raise HTTPException(409, "Concurrent alert action; retry with the same request ID") from exc
        except Exception:
            db.rollback()
            raise
        return {"id": record.id, "route_id": route_id, "status": target, "version": body.expected_version + 1, "replayed": False}

    return {"routes": routes, "alerts": alerts, "ensure_schema": ensure_schema}
