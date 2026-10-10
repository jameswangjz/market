"""Order-linked tracing only: measurements never determine settlement amounts."""
from datetime import timezone
from decimal import Decimal
import hashlib
import json
import os

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, String, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column


class ApiSnapshotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=1, max_length=36)
    credential_id: str = Field(min_length=1, max_length=36)
    period: str = Field(default="day", pattern="^(day|month|total)$")


def install(ns):
    Measurement, Order, User, Membership = (ns[name] for name in ("SettlementMeasurement", "Order", "User", "Membership"))
    class MeasurementOrder(ns["Base"]):
        __tablename__ = "settlement_measurement_orders"
        measurement_id: Mapped[str] = mapped_column(ForeignKey("settlement_measurements.id"), primary_key=True)
        order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), primary_key=True, index=True)

    ns["SettlementMeasurementOrder"] = MeasurementOrder
    HTTP = HTTPException
    viewers = {"super_admin", "platform_operator", "finance_settlement", "security_compliance", "delivery_monitor"}
    operators = {"super_admin", "platform_operator", "finance_settlement"}
    units = {"download": {"count", "bytes"}, "api_call": {"count"}, "model_call": {"count", "token"},
             "offline_delivery": {"count"}, "training_hours": {"hour"}, "training_attendance": {"count"},
             "consulting_hours": {"hour"}, "custom_milestone": {"count"}}

    def utc(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    def active(user):
        if not user.is_active or user.activation_status != "active":
            raise HTTP(403, "账号未激活或已停用")

    def membership(db, user, enterprise_ids, admin=False):
        stmt = select(Membership.id).where(Membership.user_id == user.id, Membership.enterprise_id.in_(enterprise_ids), Membership.status == "active")
        if admin:
            stmt = stmt.where(Membership.role.in_(["super_admin", "enterprise_admin"]))
        return db.scalar(stmt) is not None

    def require_order_access(db, user, order, mode="view"):
        active(user)
        role = user.platform_role
        allowed_platform = operators if mode in {"manual", "snapshot"} else viewers
        if role:
            if role not in allowed_platform:
                raise HTTP(403, "当前平台角色无权访问订单计量")
            return
        tenants = [order.buyer_enterprise_id] if mode == "snapshot" else [order.buyer_enterprise_id, order.provider_enterprise_id]
        if mode == "view" and order.buyer_user_id == user.id:
            return
        if mode == "snapshot" and order.buyer_user_id == user.id and user.verified_status == "verified":
            return
        if not membership(db, user, tenants, admin=mode != "view"):
            raise HTTP(403, "无权访问该订单或登记计量")

    def require_transition(db, user, order, action):
        active(user)
        buyer_actions = {"submit_review", "start_payment", "confirm_payment", "cancel_order", "accept_delivery", "reject_delivery", "confirm_order", "submit_after_sales"}
        if action in buyer_actions:
            # A real enterprise subject always requires its current active administrator.
            if not user.platform_role:
                if not order.buyer_enterprise_id and order.buyer_user_id == user.id and user.verified_status == "verified":
                    return
                if order.buyer_enterprise_id and membership(db, user, [order.buyer_enterprise_id], admin=True):
                    return
            raise HTTP(403, "企业订单须由当前活跃企业管理员操作，个人订单须由实名购买人操作")
        if user.platform_role:
            role_actions = {
                "super_admin": set(ns["TRANSITIONS"]), "platform_operator": set(ns["TRANSITIONS"]),
                "finance_settlement": {"approve_refund", "complete_refund"},
                "delivery_monitor": {"create_task", "start_delivery", "submit_delivery", "mark_exception", "retry_delivery", "close_after_sales"},
            }
            if action not in role_actions.get(user.platform_role, set()):
                raise HTTP(403, "当前平台角色无权执行该订单动作")
            return
        provider_actions = {"create_task", "start_delivery", "submit_delivery", "mark_exception", "retry_delivery", "close_after_sales"}
        if action in provider_actions and membership(db, user, [order.provider_enterprise_id], admin=True):
            return
        raise HTTP(403, "当前用户无权执行该订单动作")

    def scoped_orders(db, user):
        active(user)
        if user.platform_role:
            if user.platform_role not in viewers:
                raise HTTP(403, "当前平台角色无权查看订单")
            return select(Order)
        tenants = select(Membership.enterprise_id).where(Membership.user_id == user.id, Membership.status == "active")
        return select(Order).where(or_(Order.buyer_user_id == user.id, Order.buyer_enterprise_id.in_(tenants), Order.provider_enterprise_id.in_(tenants)))

    def output(item):
        evidence = json.loads(item.evidence_json or "{}")
        return {"id": item.id, "order_id": item.order_id, "measurement_type": item.measurement_type,
                "quantity": float(item.quantity or 0), "unit": item.unit, "source": item.source,
                "event_key": item.event_key, "source_id": item.source_id, "actor_id": item.actor_id,
                "sample_kind": item.sample_kind, "scope": item.scope, "related_order_ids": evidence.get("related_order_ids", [item.order_id]), "evidence": evidence,
                "order_id_role": "storage_anchor" if item.scope == "credential_shared" else "event_order",
                "order_usage_quantity": None if item.scope == "credential_shared" else float(item.quantity or 0),
                "period_start": item.period_start, "period_end": item.period_end,
                "validation_status": item.validation_status, "validation_message": item.validation_message,
                "created_at": item.created_at, "billing_effect": "none"}

    def record(db, order, measurement_type, quantity, unit, source, event_key, source_id="", actor_id="", period_start=None, period_end=None, status="observed", message="", evidence=None, sample_kind="event", scope="order", related_order_ids=()):
        # The primary key is the order-scoped event identity, including concurrent retries.
        identity = "credential_shared" if scope == "credential_shared" else order.id
        event_id = hashlib.sha256(json.dumps([identity, source, event_key], ensure_ascii=True).encode()).hexdigest()[:32]
        existing = db.get(Measurement, event_id)
        if existing:
            return existing, True
        item = Measurement(id=event_id, order_id=order.id, measurement_type=measurement_type,
                           quantity=quantity, unit=unit, source=source, event_key=event_key, source_id=source_id,
                           actor_id=actor_id, period_start=period_start, period_end=period_end,
                           validation_status=status, validation_message=message, sample_kind=sample_kind, scope=scope,
                           evidence_json=json.dumps(evidence or {}, ensure_ascii=True, sort_keys=True))
        # Flush business changes outside the savepoint; never hide their failures.
        db.flush()
        connection = db.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            # SQLite SELECT does not start a transaction; RELEASE must not commit the event.
            connection.exec_driver_sql("BEGIN")
        try:
            with db.begin_nested():
                db.add(item)
                db.flush()
                db.add_all([MeasurementOrder(measurement_id=item.id, order_id=oid) for oid in sorted(set(related_order_ids))])
                db.flush()
        except IntegrityError:
            existing = db.get(Measurement, event_id)
            if existing is None:
                raise
            return existing, True
        return item, False

    def create_manual(db, user, body):
        order = db.get(Order, body.order_id)
        if not order:
            raise HTTP(404, "订单不存在")
        require_order_access(db, user, order, "manual")
        if body.unit not in units[body.measurement_type]:
            raise HTTP(422, "计量类型与单位不匹配")
        if body.unit in {"count", "token", "bytes"} and body.quantity != body.quantity.to_integral_value():
            raise HTTP(422, "次数、字节或 token 数量必须是整数")
        start, end = (utc(value) if value else None for value in (body.period_start, body.period_end))
        if bool(start) != bool(end) or (start and (start > end or end > utc(ns["now"]()))):
            raise HTTP(422, "计量区间必须完整、顺序正确且不能位于未来")
        payload = {"type": body.measurement_type, "quantity": str(body.quantity.normalize()), "unit": body.unit,
                   "source_label": body.source, "source_id": body.source_id, "start": start.isoformat() if start else None, "end": end.isoformat() if end else None}
        key = body.event_key.strip() or "manual:" + hashlib.sha256(json.dumps([user.id, payload], sort_keys=True).encode()).hexdigest()
        item, duplicate = record(db, order, body.measurement_type, body.quantity, body.unit, "manual", key,
                                 source_id=body.source_id, actor_id=user.id, period_start=start, period_end=end,
                                 status="pending", message="人工登记，尚未核验来源凭据", evidence=payload)
        if duplicate and json.loads(item.evidence_json) != payload:
            raise HTTP(409, "同一计量事件键不能用于不同数据")
        if not duplicate:
            ns["audit"](db, user.email or user.phone or user.id, "create_settlement_measurement", "measurement", item.id,
                        body.measurement_type, category="measurement", business_domain="settlement", order_id=order.id,
                        after={"event_key": item.event_key, "quantity": str(body.quantity), "unit": body.unit, "validation_status": "pending"})
        return item, duplicate

    def list_measurements(db, user, order_id, offset=0, limit=100, source="", source_id="", event_key=""):
        if order_id:
            order = db.get(Order, order_id)
            if not order:
                raise HTTP(404, "订单不存在")
            require_order_access(db, user, order)
        permitted = scoped_orders(db, user).with_only_columns(Order.id)
        linked = select(MeasurementOrder.measurement_id).where(MeasurementOrder.order_id.in_(permitted))
        stmt = select(Measurement).where(or_(Measurement.order_id.in_(permitted), Measurement.id.in_(linked)))
        if order_id:
            stmt = stmt.where(or_(Measurement.order_id == order_id, Measurement.id.in_(select(MeasurementOrder.measurement_id).where(MeasurementOrder.order_id == order_id))))
        for name, value in (("source", source), ("source_id", source_id), ("event_key", event_key)):
            if value:
                stmt = stmt.where(getattr(Measurement, name) == value)
        total = db.scalar(select(func.count()).select_from(stmt.subquery()))
        return {"items": [output(item) for item in db.scalars(stmt.order_by(Measurement.created_at.desc(), Measurement.id).offset(offset).limit(limit))], "total": total}

    def record_order_event(db, order, action, version, actor_id="", source_id=""):
        if action == "confirm_payment":
            payment = db.scalar(select(ns["Payment"]).where(ns["Payment"].order_id == order.id).order_by(ns["Payment"].created_at.desc()))
            if payment and payment.status == "paid":
                product = db.get(ns["Product"], order.product_id)
                return record(db, order, "payment", payment.amount, "currency", "payment", payment.id + ":paid", payment.id, actor_id,
                              evidence={"payment_id": payment.id, "currency": product.currency if product else None, "basis": "paid_order", "paid_at": payment.paid_at.isoformat() if payment.paid_at else None})
        elif action == "complete_refund":
            refund = db.scalar(select(ns["Refund"]).where(ns["Refund"].order_id == order.id, ns["Refund"].status == "completed").order_by(ns["Refund"].completed_at.desc()))
            if refund:
                return record(db, order, "refund", refund.amount, "currency", "refund", refund.id + ":completed", refund.id, actor_id,
                              evidence={"refund_id": refund.id, "payment_id": refund.payment_id, "basis": "negative_adjustment"})
        elif action in {"submit_delivery", "accept_delivery", "reject_delivery", "confirm_order", "delivery_succeeded", "delivery_failed"}:
            task = db.get(ns["DeliveryTask"], source_id) if source_id else db.scalar(select(ns["DeliveryTask"]).where(ns["DeliveryTask"].order_id == order.id).order_by(ns["DeliveryTask"].created_at.desc()))
            failed = action in {"reject_delivery", "delivery_failed"}
            kind = "offline_delivery" if task and task.delivery_mode == "manual" else "fulfillment"
            return record(db, order, kind, 1, "count", "delivery", action + ":" + version, task.id if task else order.id, actor_id,
                          status="pending" if failed else "observed", message="交付异常待处理" if failed else "",
                          evidence={"action": action, "delivery_task_id": task.id if task else None, "delivery_status": order.delivery_status})

    def record_download(db, order, download_log, file, size):
        return record(db, order, "download", 1, "count", "file_download", download_log.id,
                      download_log.id, download_log.user_id, evidence={"file_id": file.id, "download_log_id": download_log.id,
                      "response_bytes": size, "observation": "object_read_response_prepared_not_client_receipt"})

    def api_snapshot(body: ApiSnapshotBody, user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        order = db.get(Order, body.order_id)
        if not order:
            raise HTTP(404, "订单不存在")
        require_order_access(db, user, order, "snapshot")
        credential = db.get(ns["ApiCredential"], body.credential_id)
        route = db.get(ns["ApiGatewayRoute"], credential.route_id) if credential else None
        if not credential or credential.enterprise_id != order.buyer_enterprise_id or not route or route.product_id != order.product_id:
            raise HTTP(403, "API 凭证不属于该订单，无法采集计量快照")
        # Mirror api_entitlement_policy: shared subject/product paid orders, not credential.order_id.
        Version = ns["ProductReleaseVersion"]
        related_orders = db.scalars(select(Order).join(Version, Version.id == Order.product_version_id).where(
            Order.buyer_enterprise_id == credential.enterprise_id, Order.product_id == route.product_id,
            Version.product_id == route.product_id, Order.payment_status == "paid",
            Order.main_status.not_in(("cancelled", "closed")), Order.refunded_amount == 0,
        ).order_by(Order.id)).all()
        related_ids = [related.id for related in related_orders]
        if order.id not in related_ids:
            raise HTTP(403, "该订单没有此共享凭证的有效购买权益")
        if not credential.apisix_consumer_name:
            raise HTTP(409, "该凭证没有可关联的 APISIX consumer")
        clock = utc(ns["now"]())
        period = clock.strftime("%Y-%m-%d" if body.period == "day" else "%Y-%m") if body.period != "total" else "total"
        start = clock.replace(hour=0, minute=0, second=0, microsecond=0) if body.period != "total" else None
        if body.period == "month":
            start = start.replace(day=1)
        key = f"market:apisix:quota:{body.period}:{route.route_key}:{credential.apisix_consumer_name}"
        if body.period != "total":
            key += ":" + period
        client, count, status, message = None, Decimal(0), "pending", "Redis 计数缺失，不能认定为零用量"
        counter_available = False
        try:
            client = ns["redis"].Redis.from_url(os.getenv("REDIS_URL", "redis://market-redis:6379/0"), decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
            if int(client.connection_pool.connection_kwargs.get("db", 0)) != 0:
                raise ValueError("Redis database must be zero")
            raw = client.get(key)
            if raw is not None:
                count = Decimal(str(raw))
                if not count.is_finite() or count < 0 or count != count.to_integral_value() or count > Decimal("999999999999"):
                    count, message = Decimal(0), "Redis 累计计数非法，待处理"
                else:
                    counter_available = True
                    status, message = "observed", "凭证累计快照，不是单次调用或额外计费依据"
        except (ns["redis"].RedisError, ValueError, ArithmeticError):
            count, status, message = Decimal(0), "pending", "Redis 计量源不可用或计数非法，待处理"
        finally:
            if client:
                try:
                    client.close()
                except ns["redis"].RedisError:
                    pass
        previous = db.scalar(select(Measurement).where(Measurement.source == "apisix_redis", Measurement.scope == "credential_shared",
                             Measurement.source_id == credential.id, Measurement.sample_kind == "snapshot",
                             Measurement.validation_status == "observed", Measurement.period_start == start,
                             Measurement.event_key.startswith(f"{credential.id}:{body.period}:{period}:", autoescape=True)).order_by(Measurement.created_at.desc()))
        if status == "observed" and previous and count < previous.quantity:
            status, message = "pending", "同一周期累计计数回退，可能发生重置，待处理"
        product = db.get(ns["Product"], order.product_id)
        kind = "model_call" if product and (product.product_type == "model" or product.delivery_method == "model_api") else "api_call"
        linkage = hashlib.sha256(json.dumps(related_ids).encode()).hexdigest()[:12]
        event_key = f"{credential.id}:{body.period}:{period}:{status}:{count}:{hashlib.sha256(message.encode()).hexdigest()[:12]}:{linkage}"
        item, duplicate = record(db, related_orders[0], kind, count, "count", "apisix_redis", event_key, credential.id, user.id,
                                 period_start=start, period_end=clock, status=status, message=message, sample_kind="snapshot",
                                 evidence={"credential_id": credential.id, "route_id": route.id, "counter_period": body.period,
                                           "period_key": period, "allocation": "unattributed", "scope": "credential_shared",
                                           "related_order_ids": related_ids, "cumulative": True, "counter_available": counter_available,
                                           "counter_semantics": "gateway_quota_increments_not_verified_successful_calls"},
                                 scope="credential_shared", related_order_ids=related_ids)
        if not duplicate:
            ns["audit"](db, user.email or user.phone or user.id, "snapshot_settlement_measurement", "measurement", item.id,
                        "APISIX credential counter snapshot", category="measurement", business_domain="settlement", order_id=order.id,
                        after={"event_key": item.event_key, "validation_status": item.validation_status})
        db.commit()
        return {**output(item), "requested_order_id": order.id, "idempotent": duplicate}

    ns["app"].post("/api/settlement-measurements/api-snapshot")(api_snapshot)

    def get_measurement(measurement_id: str, user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        item = db.get(Measurement, measurement_id)
        if not item:
            raise HTTP(404, "计量事件不存在")
        order = db.get(Order, item.order_id)
        if not order:
            raise HTTP(404, "关联订单不存在")
        if item.scope == "credential_shared":
            permitted = scoped_orders(db, user).with_only_columns(Order.id)
            if db.scalar(select(MeasurementOrder.order_id).where(MeasurementOrder.measurement_id == item.id, MeasurementOrder.order_id.in_(permitted)).limit(1)) is None:
                raise HTTP(403, "无权访问该共享凭证计量")
        else:
            require_order_access(db, user, order)
        return output(item)

    ns["app"].get("/api/settlement-measurements/{measurement_id}")(get_measurement)

    def aggregate(order_id: str, user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        order = db.get(Order, order_id)
        if not order:
            raise HTTP(404, "订单不存在")
        require_order_access(db, user, order)
        groups = [Measurement.measurement_type, Measurement.unit, Measurement.source, Measurement.validation_status]
        totals = db.execute(select(*groups, func.sum(Measurement.quantity), func.count()).where(Measurement.order_id == order.id, Measurement.sample_kind == "event").group_by(*groups)).all()
        snapshots, seen = [], set()
        linked = select(MeasurementOrder.measurement_id).where(MeasurementOrder.order_id == order.id)
        for item in db.scalars(select(Measurement).where(or_(Measurement.order_id == order.id, Measurement.id.in_(linked)), Measurement.sample_kind == "snapshot").order_by(Measurement.created_at.desc(), Measurement.id.desc())):
            evidence = json.loads(item.evidence_json or "{}")
            identity = (item.source_id, evidence.get("counter_period"), evidence.get("period_key"))
            if identity not in seen:
                snapshots.append(output(item))
                seen.add(identity)
        Payment, Refund = ns["Payment"], ns["Refund"]
        payment = db.scalar(select(Payment).where(Payment.order_id == order.id, Payment.status.in_(["paid", "refunding", "refunded"]), Payment.paid_at.is_not(None)).order_by(Payment.paid_at.desc()))
        refunds = db.scalar(select(func.coalesce(func.sum(Refund.amount), 0)).where(Refund.order_id == order.id, Refund.status == "completed"))
        return {"order_id": order.id, "billing_basis": "paid_order", "measurement_billing_effect": "none",
                "paid_amount": float(payment.amount) if payment else 0, "completed_refund_amount": float(refunds),
                "payment_id": payment.id if payment else None,
                "event_totals": [{"measurement_type": row[0], "unit": row[1], "source": row[2], "validation_status": row[3], "quantity": float(row[4]), "event_count": row[5]} for row in totals],
                "snapshots": snapshots, "snapshots_non_additive": True, "events_url": f"/api/settlement-measurements?order_id={order.id}"}

    ns["app"].get("/api/orders/{order_id}/settlement-measurements/summary")(aggregate)

    return {"require_order_access": require_order_access, "require_transition": require_transition,
            "scoped_orders": scoped_orders, "create_manual": create_manual, "list": list_measurements,
            "output": output, "record_order_event": record_order_event, "record_download": record_download,
            "record": record}
