"""SQL list filters applied after the caller's authorization predicates."""
from datetime import date, datetime, time, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, or_, select


PRODUCT_STATUSES = frozenset({"draft", "pending_review", "quality_review", "security_review",
    "operation_review", "pending_integration", "published", "rejected", "security_unpublished"})
MAIN_STATUSES = frozenset({"created", "pending_review", "pending_provider_review", "pending_payment",
    "pending_fulfillment", "awaiting_start", "fulfilling", "in_delivery", "pending_confirmation", "pending_acceptance",
    "rectifying", "completed", "cancelled", "closed", "refunded", "rejected"})
PAYMENT_STATUSES = frozenset({"unpaid", "paying", "paid", "refunding", "refunded", "cancelled"})
DELIVERY_STATUSES = frozenset({"not_started", "preparing", "awaiting_start", "in_delivery",
    "pending_acceptance", "accepted", "exception", "rectifying", "completed", "retrying"})
TASK_STATUSES = frozenset({"preparing", "awaiting_start", "in_delivery", "pending_acceptance",
    "rectifying", "completed", "exception", "retrying"})
METHODS = frozenset({"file", "object_storage", "api", "model_api", "tenant_access", "training", "consulting", "custom"})


def text(value, name):
    value = (value or "").strip()
    if len(value) > 220:
        raise HTTPException(422, f"{name} must not exceed 220 characters")
    return value


def choice(value, name, allowed):
    value = text(value, name)
    if value and value not in allowed:
        raise HTTPException(422, f"Invalid {name}")
    return value


def contains(column, value):
    # Search text is literal, including SQL LIKE wildcard characters.
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(f"%{escaped}%", escape="\\")


def nonblank(column):
    return func.nullif(func.trim(column), "")


def pagination(page, page_size):
    if page is None and page_size is None:
        return None
    page = 1 if page is None else page
    page_size = 20 if page_size is None else page_size
    if page < 1 or page > 100000 or page_size not in {10, 20, 50, 100} or (page - 1) * page_size > 1000000:
        raise HTTPException(422, "Invalid page or page_size (10, 20, 50, 100); maximum offset is 1000000")
    return page, page_size


def result(db, stmt, paging, serialize):
    metadata = {}
    if paging:
        page, size = paging
        total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
        page = min(page, max(1, (total + size - 1) // size))
        metadata = {"total": total, "page": page, "page_size": size}
        stmt = stmt.limit(size).offset((page - 1) * size)
    return {"items": [serialize(row) for row in db.execute(stmt).all()], **metadata}


def product_query(ns, stmt, values):
    P, E, C = (ns[key] for key in ("Product", "Enterprise", "ProductCategory"))
    stmt = stmt.outerjoin(E, E.id == P.enterprise_id)
    for name, column in (("product_name", P.name), ("provider_name", func.coalesce(nonblank(P.provider_name), E.name))):
        value = text(values[name], name)
        if value:
            stmt = stmt.where(contains(column, value))
    category = text(values["category_id"], "category_id")
    catalog = text(values["catalog_name"], "catalog_name")
    if category:
        stmt = stmt.where(or_(P.category_id == category,
            P.category_id.in_(select(C.id).where(C.parent_id == category))))
    if catalog:
        stmt = stmt.where(or_(P.catalog_name == catalog, P.catalog_name.startswith(catalog + "/", autoescape=True)))
    q = text(values["q"], "q")
    if q:
        stmt = stmt.where(or_(contains(P.name, q), contains(P.description, q)))
    for name, allowed in (("status", PRODUCT_STATUSES),
            ("product_type", {"dataset", "api", "model", "saas", "training", "consulting", "custom", "report", "application"})):
        value = choice(values[name], name, allowed)
        if value:
            stmt = stmt.where(getattr(P, name) == value)
    return stmt.order_by(P.updated_at.desc(), P.id.desc())


def order_query(ns, stmt, values):
    O, E, U = (ns[key] for key in ("Order", "Enterprise", "User"))
    provider = select(E.name).where(E.id == O.provider_enterprise_id).scalar_subquery()
    buyer_enterprise = select(E.name).where(E.id == O.buyer_enterprise_id).scalar_subquery()
    buyer_user = select(U.name).where(U.id == O.buyer_user_id).scalar_subquery()
    # Order has a buyer snapshot but no provider snapshot. Never use buyer_name for the provider.
    provider = func.coalesce(provider, "")
    buyer = func.coalesce(nonblank(O.buyer_name), buyer_enterprise, buyer_user, "")
    stmt = stmt.add_columns(provider.label("provider_name"), buyer.label("buyer_display_name"))
    for name, column in (("order_no", O.order_no), ("product_name", O.product_name),
            ("provider_name", provider), ("buyer_name", buyer)):
        value = text(values[name], name)
        if value:
            predicate = contains(column, value)
            stmt = stmt.where(or_(predicate, O.id == value) if name == "order_no" else predicate)
    q = text(values["q"], "q")
    if q:
        stmt = stmt.where(or_(contains(O.order_no, q), O.id == q,
            contains(O.product_name, q), contains(buyer, q)))
    for name, allowed in (("main_status", MAIN_STATUSES), ("payment_status", PAYMENT_STATUSES),
            ("delivery_status", DELIVERY_STATUSES), ("after_sales_status", {"none", "processing", "resolved", "closed"}),
            ("status", MAIN_STATUSES)):
        value = choice(values[name], name, allowed)
        if value:
            stmt = stmt.where(getattr(O, "main_status" if name == "status" else name) == value)
    return stmt.order_by(O.updated_at.desc(), O.id.desc())


def date_bounds(start, end):
    bounds = []
    for name, value in (("sla_start", start), ("sla_end", end)):
        value = text(value, name)
        try:
            parsed = date.fromisoformat(value) if value else None
            if parsed and parsed.isoformat() != value:
                raise ValueError()
            if parsed and name == "sla_end":
                parsed += timedelta(days=1)
            bounds.append(datetime.combine(parsed, time(), timezone(timedelta(hours=8))).astimezone(timezone.utc)
                if parsed else None)
        except (ValueError, OverflowError):
            raise HTTPException(422, f"Invalid {name}; expected YYYY-MM-DD") from None
    if all(bounds) and bounds[0] >= bounds[1]:
        raise HTTPException(422, "sla_start must not be after sla_end")
    return bounds


def delivery_query(ns, stmt, values):
    T, O, U = (ns[key] for key in ("DeliveryTask", "Order", "User"))
    stmt = stmt.join(O, O.id == T.order_id).add_columns(O.order_no)
    for name, column in (("task_id", T.id), ("order_id", O.order_no)):
        value = text(values[name], name)
        if value:
            predicate = contains(column, value)
            stmt = stmt.where(or_(predicate, O.id == value) if name == "order_id" else predicate)
    assignee = text(values["assignee"], "assignee")
    if assignee:
        users = select(U.id).where(or_(contains(U.name, assignee), contains(U.username, assignee),
            contains(U.email, assignee), contains(U.phone, assignee)))
        stmt = stmt.where(or_(contains(T.assignee, assignee), T.assignee.in_(users)))
    for name, allowed in (("method", METHODS), ("delivery_mode", {"manual", "automatic"}), ("status", TASK_STATUSES)):
        value = choice(values[name], name, allowed)
        if value:
            stmt = stmt.where(getattr(T, name) == value)
    start, end = date_bounds(values["sla_start"], values["sla_end"])
    if start:
        stmt = stmt.where(T.sla_due_at >= start)
    if end:
        stmt = stmt.where(T.sla_due_at < end)
    return stmt.order_by(T.created_at.desc(), T.id.desc())
