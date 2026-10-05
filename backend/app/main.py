from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any

import jwt
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from minio import Minio
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, create_engine, func, or_, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./market.db")
JWT_SECRET = os.getenv("JWT_SECRET", "market-development-secret-change-me")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "market")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "market123456")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "market-files")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    email: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_status: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Enterprise(Base):
    __tablename__ = "enterprises"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    name: Mapped[str] = mapped_column(String(180), unique=True)
    credit_code: Mapped[str] = mapped_column(String(40), unique=True)
    verification_status: Mapped[str] = mapped_column(String(30), default="verified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    role: Mapped[str] = mapped_column(String(60), default="member")
    business_roles: Mapped[str] = mapped_column(String(255), default="provider,user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    name: Mapped[str] = mapped_column(String(220))
    product_type: Mapped[str] = mapped_column(String(50))
    catalog_name: Mapped[str] = mapped_column(String(180), default="未分类", nullable=True)
    provider_name: Mapped[str] = mapped_column(String(180), default="", nullable=True)
    provider_type: Mapped[str] = mapped_column(String(60), default="企业", nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    usage_scenarios: Mapped[str] = mapped_column(Text, default="", nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="draft", index=True)
    delivery_method: Mapped[str] = mapped_column(String(80), default="file")
    price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    pricing_strategy: Mapped[str] = mapped_column(Text, default="", nullable=True)
    currency: Mapped[str] = mapped_column(String(10), default="CNY")
    version: Mapped[str] = mapped_column(String(30), default="v1.0")
    quality_level: Mapped[str] = mapped_column(String(30), default="标准")
    security_level: Mapped[str] = mapped_column(String(40), default="一般", nullable=True)
    authorization_conditions: Mapped[str] = mapped_column(Text, default="", nullable=True)
    data_source_statement: Mapped[str] = mapped_column(Text, default="", nullable=True)
    compliance_statement: Mapped[str] = mapped_column(Text, default="", nullable=True)
    review_comment: Mapped[str] = mapped_column(Text, default="", nullable=True)
    reviewed_by: Mapped[str] = mapped_column(String(180), default="", nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    order_no: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    buyer_enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    provider_enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    main_status: Mapped[str] = mapped_column(String(30), default="created", index=True)
    payment_status: Mapped[str] = mapped_column(String(30), default="unpaid", index=True)
    delivery_status: Mapped[str] = mapped_column(String(30), default="not_started", index=True)
    after_sales_status: Mapped[str] = mapped_column(String(30), default="none", index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    paid_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    refunded_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    buyer_name: Mapped[str] = mapped_column(String(180))
    product_name: Mapped[str] = mapped_column(String(220))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class OrderStateLog(Base):
    __tablename__ = "order_state_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    domain: Mapped[str] = mapped_column(String(30))
    from_status: Mapped[str] = mapped_column(String(40))
    to_status: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(180))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    payment_no: Mapped[str] = mapped_column(String(60), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="unpaid")
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    proof: Mapped[str] = mapped_column(Text, default="")
    confirmed_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Refund(Base):
    __tablename__ = "refunds"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    refund_no: Mapped[str] = mapped_column(String(60), unique=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    payment_id: Mapped[str] = mapped_column(ForeignKey("payments.id"), index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[str] = mapped_column(String(180), default="")
    completed_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeliveryTask(Base):
    __tablename__ = "delivery_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    assignee: Mapped[str] = mapped_column(String(180), default="运营交付团队")
    method: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="preparing")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AfterSalesTicket(Base):
    __tablename__ = "after_sales_tickets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    ticket_no: Mapped[str] = mapped_column(String(60), unique=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), default="processing")
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    description: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(String(180), default="售后团队")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Settlement(Base):
    __tablename__ = "settlements"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    settlement_no: Mapped[str] = mapped_column(String(60), unique=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    gross_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    refund_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    net_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    refund_recovery: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    platform_fee: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    provider_share: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    service_share: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    expert_fee: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    tax_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    adjustment: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SettlementAdjustment(Base):
    __tablename__ = "settlement_adjustments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    settlement_id: Mapped[str] = mapped_column(ForeignKey("settlements.id"), index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(180))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    actor: Mapped[str] = mapped_column(String(180))
    action: Mapped[str] = mapped_column(String(120))
    target_type: Mapped[str] = mapped_column(String(60))
    target_id: Mapped[str] = mapped_column(String(80), default="")
    result: Mapped[str] = mapped_column(String(30), default="success")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FileObject(Base):
    __tablename__ = "file_objects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"), index=True, nullable=True)
    object_name: Mapped[str] = mapped_column(String(500))
    original_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64), default="")
    file_role: Mapped[str] = mapped_column(String(50), default="product_data")
    version: Mapped[str] = mapped_column(String(30), default="v1.0")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DevelopmentTask(Base):
    __tablename__ = "development_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    owner: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(240))
    area: Mapped[str] = mapped_column(String(60))
    priority: Mapped[str] = mapped_column(String(10), default="P0")
    status: Mapped[str] = mapped_column(String(30), default="todo", index=True)
    dependencies: Mapped[str] = mapped_column(Text, default="")
    acceptance: Mapped[str] = mapped_column(Text, default="")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split("$", 1)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 120_000).hex()
        return hmac.compare_digest(candidate, digest_hex)
    except ValueError:
        return False


def issue_token(user: User) -> str:
    return jwt.encode({"sub": user.id, "email": user.email, "exp": now() + timedelta(hours=12)}, JWT_SECRET, algorithm="HS256")


class LoginBody(BaseModel):
    email: str
    password: str


class RegisterBody(BaseModel):
    email: str
    name: str
    password: str = Field(min_length=8)


class ProductBody(BaseModel):
    name: str
    product_type: str
    catalog_name: str = "未分类"
    provider_name: str = ""
    provider_type: str = "企业"
    description: str = ""
    usage_scenarios: str = ""
    delivery_method: str = "file"
    price: float = 0
    pricing_strategy: str = ""
    version: str = "v1.0"
    quality_level: str = "标准"
    security_level: str = "一般"
    authorization_conditions: str = ""
    data_source_statement: str = ""
    compliance_statement: str = ""


class ProductReviewBody(BaseModel):
    decision: str
    comment: str = ""


class ProductFileMetadata(BaseModel):
    file_role: str = "product_data"
    version: str = "v1.0"
    description: str = ""


class OrderBody(BaseModel):
    product_id: str


class TransitionBody(BaseModel):
    action: str
    reason: str = ""
    refund_amount: float | None = Field(default=None, gt=0)


class DevelopmentTaskUpdate(BaseModel):
    status: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    note: str = ""


class SettlementRuleBody(BaseModel):
    platform_rate: float = Field(default=8, ge=0, le=100)
    service_rate: float = Field(default=20, ge=0, le=100)
    expert_rate: float = Field(default=5, ge=0, le=100)
    tax_rate: float = Field(default=6, ge=0, le=100)


class SettlementAdjustmentBody(BaseModel):
    amount: float
    reason: str = Field(min_length=2)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    name: str
    verified_status: str


app = FastAPI(title="Market Operations API", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
security = HTTPBearer(auto_error=False)


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def audit(db: Session, actor: str, action: str, target_type: str, target_id: str = "", detail: str = ""):
    db.add(AuditLog(actor=actor, action=action, target_type=target_type, target_id=target_id, detail=detail))


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security), db: Session = Depends(db_session)) -> User:
    if not credentials:
        raise HTTPException(401, "请先登录")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        user = db.get(User, payload.get("sub"))
    except (jwt.PyJWTError, TypeError):
        user = None
    if not user or not user.is_active:
        raise HTTPException(401, "登录已失效")
    return user


def first_enterprise(db: Session, user: User) -> Enterprise:
    membership = db.scalar(select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at))
    if not membership:
        raise HTTPException(400, "用户尚未加入企业")
    return db.get(Enterprise, membership.enterprise_id)


def make_order_no() -> str:
    return "ORD-" + now().strftime("%y%m%d%H%M%S") + secrets.token_hex(2).upper()


def log_state(db: Session, order: Order, domain: str, old: str, new: str, action: str, user: User, reason: str):
    db.add(OrderStateLog(order_id=order.id, domain=domain, from_status=old, to_status=new, action=action, operator=user.name, reason=reason))


TRANSITIONS: dict[str, tuple[str, str, str, str]] = {
    "submit_review": ("main", "created", "pending_review", "订单提交审核"),
    "approve": ("main", "pending_review", "pending_fulfillment", "审核通过"),
    "reject": ("main", "pending_review", "cancelled", "审核拒绝"),
    "start_payment": ("payment", "unpaid", "paying", "发起模拟支付"),
    "confirm_payment": ("payment", "paying", "paid", "人工确认支付"),
    "approve_refund": ("payment", "paid", "refunding", "同意退款"),
    "complete_refund": ("payment", "refunding", "refunded", "退款完成"),
    "create_task": ("delivery", "not_started", "preparing", "生成履约任务"),
    "start_delivery": ("main", "pending_fulfillment", "fulfilling", "开始履约"),
    "submit_delivery": ("delivery", "preparing", "pending_acceptance", "提交交付物"),
    "accept_delivery": ("delivery", "pending_acceptance", "accepted", "验收通过"),
    "confirm_order": ("main", "pending_confirmation", "completed", "客户确认或自动确认"),
    "mark_exception": ("delivery", "preparing", "exception", "标记交付异常"),
    "retry_delivery": ("delivery", "exception", "preparing", "整改后重试"),
    "submit_after_sales": ("after_sales", "none", "processing", "提交售后申请"),
    "close_after_sales": ("after_sales", "processing", "closed", "售后关闭"),
    "close_order": ("main", "completed", "closed", "清算/期满"),
}


def ensure_product_metadata_schema():
    """Add product registration metadata to an existing development database."""
    columns = {
        "catalog_name": "VARCHAR(180)",
        "provider_name": "VARCHAR(180)",
        "provider_type": "VARCHAR(60)",
        "usage_scenarios": "TEXT",
        "pricing_strategy": "TEXT",
        "security_level": "VARCHAR(40)",
        "authorization_conditions": "TEXT",
        "data_source_statement": "TEXT",
        "compliance_statement": "TEXT",
    }
    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            existing = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(products)").fetchall()}
            for name, sql_type in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE products ADD COLUMN {name} {sql_type}"))
        else:
            for name, sql_type in columns.items():
                connection.execute(text(f"ALTER TABLE products ADD COLUMN IF NOT EXISTS {name} {sql_type}"))


def ensure_review_and_file_schema():
    tables = {
        "products": {
            "review_comment": "TEXT",
            "reviewed_by": "VARCHAR(180)",
            "reviewed_at": "TIMESTAMP WITH TIME ZONE",
        },
        "file_objects": {
            "product_id": "VARCHAR(36)",
            "checksum": "VARCHAR(64)",
            "file_role": "VARCHAR(50)",
            "version": "VARCHAR(30)",
            "description": "TEXT",
            "status": "VARCHAR(30)",
        },
        "settlements": {
            "refund_amount": "NUMERIC(14,2)",
            "net_amount": "NUMERIC(14,2)",
            "refund_recovery": "NUMERIC(14,2)",
        },
    }
    with engine.begin() as connection:
        for table, columns in tables.items():
            if engine.dialect.name == "sqlite":
                existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
                for name, sql_type in columns.items():
                    if name not in existing:
                        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
            else:
                for name, sql_type in columns.items():
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {sql_type}"))


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    ensure_product_metadata_schema()
    ensure_review_and_file_schema()
    with SessionLocal() as db:
        if not db.scalar(select(DevelopmentTask.id).limit(1)):
            seed_tasks = [
                ("ARC-001", "主 Agent", "需求、数据模型、接口边界和状态机固化", "架构", "P0", "done", "已确认需求", "文档与代码契约一致", 100),
                ("ARC-002", "主 Agent", "建立任务台账和集成门禁", "架构", "P0", "done", "ARC-001", "任务可追踪、接口可验证", 100),
                ("BE-001", "后端 Agent", "FastAPI、PG 模型、种子数据和认证", "后端", "P0", "in_progress", "ARC-001", "API 可启动，登录和租户可用", 65),
                ("BE-002", "后端 Agent", "产品、审核和文件元数据", "后端", "P0", "todo", "BE-001", "产品生命周期可运行", 0),
                ("BE-003", "后端 Agent", "订单四域状态机", "后端", "P0", "todo", "BE-001", "合法动作成功，非法动作拒绝", 0),
                ("BE-004", "后端 Agent", "模拟支付、交付、售后", "后端", "P0", "todo", "BE-003", "主流程可闭环", 0),
                ("BE-005", "后端 Agent", "清算分账和审计", "后端", "P0", "todo", "BE-004", "金额可复核、流水可追踪", 0),
                ("FE-001", "前端 Agent", "Vue 工作台、导航、登录和进度监控", "前端", "P0", "in_progress", "ARC-001", "可登录并实时查看任务状态", 40),
                ("FE-002", "前端 Agent", "产品、订单、状态时间轴", "前端", "P0", "todo", "BE-001", "页面与 API 联通", 0),
                ("FE-003", "前端 Agent", "交付、清算、审计和用户页面", "前端", "P1", "todo", "BE-005", "核心运营页面可用", 0),
                ("OPS-001", "部署测试 Agent", "PG、Redis、MinIO 和 K8S 资源", "部署", "P0", "in_progress", "ARC-001", "market 命名空间资源可部署", 30),
                ("OPS-002", "部署测试 Agent", "镜像、NodePort 和健康检查", "部署", "P0", "todo", "FE-001,BE-001", "前后端 Pod Ready", 0),
                ("OPS-003", "部署测试 Agent", "接口、状态机和端到端冒烟测试", "测试", "P0", "todo", "FE-002,BE-005", "P0 测试通过", 0),
            ]
            for task in seed_tasks:
                db.add(DevelopmentTask(code=task[0], owner=task[1], title=task[2], area=task[3], priority=task[4], status=task[5], dependencies=task[6], acceptance=task[7], progress=task[8]))
            db.commit()
        admin = db.scalar(select(User).where(User.email == "admin@market.local"))
        if admin:
            return
        admin = User(email="admin@market.local", name="平台管理员", password_hash=hash_password("Admin123!"), verified_status="verified")
        enterprise = Enterprise(name="天地奔牛示范企业", credit_code="DEMO-20261004", verification_status="verified")
        db.add_all([admin, enterprise])
        db.flush()
        db.add(Membership(user_id=admin.id, enterprise_id=enterprise.id, role="super_admin", business_roles="provider,user,service_provider"))
        products = [
            Product(enterprise_id=enterprise.id, name="矿山装备制造质量数据集", product_type="dataset", catalog_name="行业数据集/产品质量", provider_name=enterprise.name, provider_type="企业", description="覆盖 IQC、IPQC、FQC/OQC 和质量追溯的示范数据集。", usage_scenarios="质量趋势分析、缺陷根因分析、质量追溯查询", delivery_method="file", price=68000, pricing_strategy="按授权周期计价，支持企业版年度授权", quality_level="A级", security_level="重要", authorization_conditions="仅限认证企业内部质量分析使用，不得转授权", data_source_statement="来源于企业质量管理和检测业务数据，已完成授权确认", compliance_statement="已完成数据来源、权属和脱敏合规声明"),
            Product(enterprise_id=enterprise.id, name="制造过程行业模型", product_type="model", catalog_name="行业模型/制造过程", provider_name=enterprise.name, provider_type="企业", description="支持设备状态分析、异常诊断和产能预测。", usage_scenarios="制造异常诊断、设备状态分析、产能预测", delivery_method="model_api", price=128000, pricing_strategy="按模型服务周期和调用额度计价", quality_level="生产级", security_level="重要", authorization_conditions="认证企业可调用，禁止反向提取模型参数", data_source_statement="基于制造过程数据集训练形成", compliance_statement="已完成模型训练数据使用范围审核"),
            Product(enterprise_id=enterprise.id, name="数据治理咨询服务", product_type="consulting", catalog_name="数据服务/培训咨询", provider_name=enterprise.name, provider_type="企业", description="面向企业数据资源盘点、标准体系和治理规则建设。", usage_scenarios="数据资源盘点、数据标准设计、治理规则制定", delivery_method="consulting", price=36000, pricing_strategy="按项目范围和里程碑报价", quality_level="标准", security_level="一般", authorization_conditions="交付成果仅限采购企业使用", data_source_statement="服务方法资产由平台运营方提供", compliance_statement="不直接处理客户原始数据，按项目授权实施"),
        ]
        db.add_all(products)
        db.flush()
        for product in products:
            product.status = "published"
        order = Order(order_no="ORD-260930-0001", buyer_enterprise_id=enterprise.id, provider_enterprise_id=enterprise.id, product_id=products[0].id, main_status="fulfilling", payment_status="paid", delivery_status="in_delivery", after_sales_status="none", amount=68000, paid_amount=68000, buyer_name=enterprise.name, product_name=products[0].name)
        db.add(order)
        db.flush()
        db.add(Payment(order_id=order.id, payment_no="PAY-260930-0001", status="paid", amount=68000, proof="人工确认：示范订单"))
        db.add(DeliveryTask(order_id=order.id, method="file", status="in_delivery", assignee="数据交付组", note="正在准备脱敏样例和完整数据包"))
        for action, domain, old, new in [("提交订单", "main", "", "created"), ("人工确认支付", "payment", "unpaid", "paid"), ("开始履约", "main", "pending_fulfillment", "fulfilling")]:
            db.add(OrderStateLog(order_id=order.id, domain=domain, from_status=old, to_status=new, action=action, operator=admin.name, reason="演示初始化"))
        db.add(AuditLog(actor=admin.email, action="seed_demo", target_type="system", detail="初始化演示数据"))
        db.commit()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "market-api", "time": now()}


@app.post("/api/auth/login")
def login(body: LoginBody, db: Session = Depends(db_session)):
    user = db.scalar(select(User).where(User.email == body.email.lower().strip()))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "邮箱或密码错误")
    audit(db, user.email, "login", "user", user.id)
    db.commit()
    return {"token": issue_token(user), "user": UserOut.model_validate(user).model_dump()}


@app.post("/api/auth/register")
def register(body: RegisterBody, db: Session = Depends(db_session)):
    email = body.email.lower().strip()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "邮箱已注册")
    user = User(email=email, name=body.name, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"token": issue_token(user), "user": UserOut.model_validate(user).model_dump()}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = first_enterprise(db, user)
    membership = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.enterprise_id == enterprise.id))
    return {"user": UserOut.model_validate(user), "enterprise": {"id": enterprise.id, "name": enterprise.name}, "role": membership.role if membership else "member", "business_roles": (membership.business_roles.split(",") if membership else [])}


@app.get("/api/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = first_enterprise(db, user)
    products = db.scalar(select(func.count(Product.id)).where(Product.enterprise_id == enterprise.id)) or 0
    orders = db.scalar(select(func.count(Order.id)).where(or_(Order.buyer_enterprise_id == enterprise.id, Order.provider_enterprise_id == enterprise.id))) or 0
    active = db.scalar(select(func.count(Order.id)).where(Order.main_status.in_(["pending_review", "pending_fulfillment", "fulfilling", "pending_confirmation"]))) or 0
    completed = db.scalar(select(func.count(Order.id)).where(Order.main_status == "completed")) or 0
    revenue = db.scalar(select(func.coalesce(func.sum(Order.paid_amount), 0)).where(Order.provider_enterprise_id == enterprise.id)) or 0
    return {"metrics": {"products": products, "orders": orders, "active_orders": active, "completed_orders": completed, "revenue": float(revenue)}, "status_breakdown": [{"label": "履约中", "value": active, "color": "orange"}, {"label": "已完成", "value": completed, "color": "green"}], "notice": "首版外部连接器接口暂未开发，当前工作台展示平台内部运营闭环。"}


def product_out(p: Product) -> dict[str, Any]:
    return {"id": p.id, "name": p.name, "product_type": p.product_type, "catalog_name": p.catalog_name or "未分类", "provider_name": p.provider_name or "", "provider_type": p.provider_type or "企业", "description": p.description, "usage_scenarios": p.usage_scenarios or "", "status": p.status, "delivery_method": p.delivery_method, "price": float(p.price or 0), "pricing_strategy": p.pricing_strategy or "", "currency": p.currency, "version": p.version, "quality_level": p.quality_level, "security_level": p.security_level or "一般", "authorization_conditions": p.authorization_conditions or "", "data_source_statement": p.data_source_statement or "", "compliance_statement": p.compliance_statement or "", "review_comment": p.review_comment or "", "reviewed_by": p.reviewed_by or "", "reviewed_at": p.reviewed_at, "created_at": p.created_at, "updated_at": p.updated_at}


def product_for_enterprise(product_id: str, user: User, db: Session) -> Product:
    enterprise = first_enterprise(db, user)
    product = db.scalar(select(Product).where(Product.id == product_id, Product.enterprise_id == enterprise.id))
    if not product:
        raise HTTPException(404, "产品不存在")
    return product


PRODUCT_DIRECTORIES = [
    {"value": "行业数据集/供需计划", "label": "行业数据集 · 供需计划"},
    {"value": "行业数据集/产品工艺设计", "label": "行业数据集 · 产品工艺设计"},
    {"value": "行业数据集/制造过程", "label": "行业数据集 · 制造过程"},
    {"value": "行业数据集/产品质量", "label": "行业数据集 · 产品质量"},
    {"value": "行业模型/供需计划", "label": "行业模型 · 供需计划"},
    {"value": "行业模型/产品工艺设计", "label": "行业模型 · 产品工艺设计"},
    {"value": "行业模型/制造过程", "label": "行业模型 · 制造过程"},
    {"value": "行业模型/产品质量", "label": "行业模型 · 产品质量"},
    {"value": "数据服务/数据治理", "label": "数据服务 · 数据治理"},
    {"value": "数据服务/培训咨询", "label": "数据服务 · 培训咨询"},
    {"value": "数据服务/定制开发", "label": "数据服务 · 定制开发"},
    {"value": "其他", "label": "其他"},
]


@app.get("/api/product-directories")
def product_directories(user: User = Depends(current_user)):
    return {"items": PRODUCT_DIRECTORIES}


@app.get("/api/products")
def products(q: str = "", status: str = "", product_type: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = first_enterprise(db, user)
    stmt = select(Product).where(Product.enterprise_id == enterprise.id)
    if q:
        stmt = stmt.where(or_(Product.name.ilike(f"%{q}%"), Product.description.ilike(f"%{q}%")))
    if status:
        stmt = stmt.where(Product.status == status)
    if product_type:
        stmt = stmt.where(Product.product_type == product_type)
    return {"items": [product_out(x) for x in db.scalars(stmt.order_by(Product.updated_at.desc())).all()]}


@app.get("/api/products/{product_id}")
def product_detail(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    return product_out(product_for_enterprise(product_id, user, db))


@app.post("/api/products")
def create_product(body: ProductBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = first_enterprise(db, user)
    values = body.model_dump()
    values["provider_name"] = values["provider_name"] or enterprise.name
    product = Product(enterprise_id=enterprise.id, **values)
    db.add(product)
    db.flush()
    audit(db, user.email, "create_product", "product", product.id, product.name)
    db.commit()
    db.refresh(product)
    return product_out(product)


@app.put("/api/products/{product_id}")
def update_product(product_id: str, body: ProductBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if product.status not in {"draft", "rejected"}:
        raise HTTPException(409, "只有草稿或被驳回的产品可以修改")
    values = body.model_dump()
    values["provider_name"] = values["provider_name"] or first_enterprise(db, user).name
    for name, value in values.items():
        setattr(product, name, value)
    product.review_comment = ""
    audit(db, user.email, "update_product", "product", product.id, product.name)
    db.commit()
    db.refresh(product)
    return product_out(product)


@app.post("/api/products/{product_id}/submit")
def submit_product(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    required = {
        "所属目录": product.catalog_name and product.catalog_name != "未分类",
        "提供方": product.provider_name,
        "描述": product.description,
        "适用场景": product.usage_scenarios,
        "价格策略": product.pricing_strategy,
        "授权条件": product.authorization_conditions,
        "数据来源声明": product.data_source_statement,
        "合规声明": product.compliance_statement,
    }
    missing = [label for label, value in required.items() if not value]
    if missing:
        raise HTTPException(400, f"产品元数据不完整，请补充：{'、'.join(missing)}")
    if product.status not in {"draft", "rejected"}:
        raise HTTPException(409, "当前产品状态不允许提交审核")
    product.status = "pending_review"
    product.review_comment = ""
    audit(db, user.email, "submit_product_review", "product", product.id)
    db.commit()
    return product_out(product)


@app.post("/api/products/{product_id}/review")
def review_product(product_id: str, body: ProductReviewBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if product.status != "pending_review":
        raise HTTPException(409, "只有待审核产品可以审核")
    if body.decision not in {"approve", "reject"}:
        raise HTTPException(400, "审核结论必须是 approve 或 reject")
    if body.decision == "reject" and not body.comment.strip():
        raise HTTPException(400, "驳回时必须填写审核意见")
    product.status = "published" if body.decision == "approve" else "rejected"
    product.review_comment = body.comment.strip()
    product.reviewed_by = user.email
    product.reviewed_at = now()
    audit(db, user.email, "approve_product" if body.decision == "approve" else "reject_product", "product", product.id, product.review_comment)
    db.commit()
    return product_out(product)


@app.post("/api/products/{product_id}/publish")
def publish_product(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if product.status != "pending_review":
        raise HTTPException(409, "只有待审核产品可以发布")
    product.status = "published"
    product.reviewed_by = user.email
    product.reviewed_at = now()
    audit(db, user.email, "publish_product", "product", product.id)
    db.commit()
    return product_out(product)


def order_out(o: Order) -> dict[str, Any]:
    return {"id": o.id, "order_no": o.order_no, "buyer_name": o.buyer_name, "product_name": o.product_name, "main_status": o.main_status, "payment_status": o.payment_status, "delivery_status": o.delivery_status, "after_sales_status": o.after_sales_status, "amount": float(o.amount or 0), "paid_amount": float(o.paid_amount or 0), "refunded_amount": float(o.refunded_amount or 0), "created_at": o.created_at, "updated_at": o.updated_at}


@app.get("/api/orders")
def orders(q: str = "", status: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = first_enterprise(db, user)
    stmt = select(Order).where(or_(Order.buyer_enterprise_id == enterprise.id, Order.provider_enterprise_id == enterprise.id))
    if q:
        stmt = stmt.where(or_(Order.order_no.ilike(f"%{q}%"), Order.product_name.ilike(f"%{q}%"), Order.buyer_name.ilike(f"%{q}%")))
    if status:
        stmt = stmt.where(Order.main_status == status)
    return {"items": [order_out(x) for x in db.scalars(stmt.order_by(Order.updated_at.desc())).all()]}


@app.get("/api/orders/{order_id}")
def order_detail(order_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    logs = db.scalars(select(OrderStateLog).where(OrderStateLog.order_id == order.id).order_by(OrderStateLog.created_at)).all()
    payment = db.scalar(select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at.desc()))
    task = db.scalar(select(DeliveryTask).where(DeliveryTask.order_id == order.id).order_by(DeliveryTask.created_at.desc()))
    refunds = db.scalars(select(Refund).where(Refund.order_id == order.id).order_by(Refund.created_at.desc())).all()
    return {"order": order_out(order), "logs": [{"domain": x.domain, "from_status": x.from_status, "to_status": x.to_status, "action": x.action, "reason": x.reason, "operator": x.operator, "created_at": x.created_at} for x in logs], "payment": {"status": payment.status, "payment_no": payment.payment_no, "amount": float(payment.amount or 0), "proof": payment.proof} if payment else None, "refunds": [{"id": x.id, "refund_no": x.refund_no, "amount": float(x.amount or 0), "status": x.status, "reason": x.reason, "requested_by": x.requested_by, "completed_by": x.completed_by, "created_at": x.created_at, "completed_at": x.completed_at} for x in refunds], "delivery": {"status": task.status, "method": task.method, "assignee": task.assignee, "note": task.note} if task else None}


@app.post("/api/orders")
def create_order(body: OrderBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    buyer = first_enterprise(db, user)
    product = db.get(Product, body.product_id)
    if not product or product.status != "published":
        raise HTTPException(400, "产品不存在或尚未发布")
    order = Order(order_no=make_order_no(), buyer_enterprise_id=buyer.id, provider_enterprise_id=product.enterprise_id, product_id=product.id, product_name=product.name, buyer_name=buyer.name, amount=product.price, main_status="created")
    db.add(order)
    db.flush()
    db.add(Payment(order_id=order.id, payment_no="PAY-" + secrets.token_hex(6).upper(), amount=product.price, status="unpaid"))
    db.add(OrderStateLog(order_id=order.id, domain="main", from_status="", to_status="created", action="提交订单", operator=user.name, reason="用户提交"))
    audit(db, user.email, "create_order", "order", order.id, order.order_no)
    db.commit()
    return order_out(order)


@app.post("/api/orders/{order_id}/transition")
def transition_order(order_id: str, body: TransitionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    item = TRANSITIONS.get(body.action)
    if not item:
        raise HTTPException(400, "不支持的订单动作")
    domain, expected, target, label = item
    if domain == "main":
        current = order.main_status
    elif domain == "payment":
        current = order.payment_status
    elif domain == "delivery":
        current = order.delivery_status
    else:
        current = order.after_sales_status
    if body.action == "submit_review" and current == "created":
        target = "pending_review"
    if body.action == "approve" and current == "pending_review":
        target = "pending_fulfillment"
    if body.action == "confirm_payment":
        payment = db.scalar(select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at.desc()))
        if not payment:
            raise HTTPException(400, "支付单不存在")
        payment.status = "paid"
        payment.confirmed_by = user.name
        order.payment_status = "paid"
        order.paid_amount = order.amount
        log_state(db, order, "payment", current, target, body.action, user, body.reason)
        if order.main_status in ["created", "pending_review"]:
            old_main = order.main_status
            order.main_status = "pending_fulfillment"
            log_state(db, order, "main", old_main, order.main_status, "支付完成/生成任务", user, body.reason)
    elif body.action == "approve_refund":
        if order.payment_status != "paid":
            raise HTTPException(409, "只有已支付订单可以发起退款")
        payment = db.scalar(select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at.desc()))
        if not payment:
            raise HTTPException(400, "支付单不存在")
        remaining = Decimal(str(order.paid_amount or order.amount or 0)) - Decimal(str(order.refunded_amount or 0))
        refund_amount = Decimal(str(body.refund_amount or remaining)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if refund_amount <= 0 or refund_amount > remaining:
            raise HTTPException(400, "退款金额必须大于 0 且不能超过可退款余额")
        payment.status = "refunding"
        db.add(Refund(refund_no="REF-" + secrets.token_hex(6).upper(), order_id=order.id, payment_id=payment.id, amount=refund_amount, reason=body.reason, requested_by=user.name))
        order.payment_status = "refunding"
        log_state(db, order, "payment", current, target, body.action, user, body.reason)
    elif body.action == "complete_refund":
        if order.payment_status != "refunding":
            raise HTTPException(409, "当前订单不在退款中")
        payment = db.scalar(select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at.desc()))
        refund = db.scalar(select(Refund).where(Refund.order_id == order.id, Refund.status == "pending").order_by(Refund.created_at.desc()))
        if not payment or not refund:
            raise HTTPException(400, "退款单不存在")
        refund.status = "completed"
        refund.completed_by = user.name
        refund.completed_at = now()
        order.refunded_amount = Decimal(str(order.refunded_amount or 0)) + Decimal(str(refund.amount or 0))
        fully_refunded = Decimal(str(order.refunded_amount or 0)) >= Decimal(str(order.paid_amount or order.amount or 0))
        payment.status = "refunded" if fully_refunded else "paid"
        refund_target = "refunded" if fully_refunded else "paid"
        order.payment_status = refund_target
        if order.after_sales_status == "processing":
            old_after_sales = order.after_sales_status
            order.after_sales_status = "resolved"
            log_state(db, order, "after_sales", old_after_sales, "resolved", "退款完成", user, body.reason)
        log_state(db, order, "payment", current, refund_target, body.action, user, body.reason)
    elif body.action == "create_task":
        if order.delivery_status != "not_started":
            raise HTTPException(400, "当前交付状态不能生成任务")
        order.delivery_status = "preparing"
        db.add(DeliveryTask(order_id=order.id, method="file", status="preparing", assignee="运营交付团队"))
        log_state(db, order, "delivery", current, target, body.action, user, body.reason)
    elif body.action == "submit_after_sales":
        if order.after_sales_status != "none":
            raise HTTPException(400, "当前订单已有售后事项")
        order.after_sales_status = "processing"
        db.add(AfterSalesTicket(ticket_no="AS-" + secrets.token_hex(5).upper(), order_id=order.id, type="质量异议", description=body.reason or "客户提交售后申请"))
        log_state(db, order, "after_sales", current, target, body.action, user, body.reason)
    elif current != expected:
        raise HTTPException(409, f"当前状态为 {current}，不能执行“{label}”")
    else:
        if domain == "main": order.main_status = target
        elif domain == "payment": order.payment_status = target
        elif domain == "delivery": order.delivery_status = target
        else: order.after_sales_status = target
        log_state(db, order, domain, current, target, body.action, user, body.reason)
        if body.action == "submit_delivery":
            order.main_status = "pending_confirmation"
            log_state(db, order, "main", "fulfilling", "pending_confirmation", "交付完成/待确认", user, body.reason)
        if body.action == "accept_delivery":
            order.main_status = "pending_confirmation"
        if body.action == "confirm_order":
            order.delivery_status = "accepted"
    audit(db, user.email, body.action, "order", order.id, body.reason)
    db.commit()
    return order_out(order)


@app.get("/api/delivery-tasks")
def delivery_tasks(user: User = Depends(current_user), db: Session = Depends(db_session)):
    items = db.scalars(select(DeliveryTask).order_by(DeliveryTask.created_at.desc())).all()
    return {"items": [{"id": x.id, "order_id": x.order_id, "assignee": x.assignee, "method": x.method, "status": x.status, "note": x.note, "created_at": x.created_at} for x in items]}


@app.get("/api/after-sales")
def after_sales(user: User = Depends(current_user), db: Session = Depends(db_session)):
    items = db.scalars(select(AfterSalesTicket).order_by(AfterSalesTicket.created_at.desc())).all()
    return {"items": [{"id": x.id, "ticket_no": x.ticket_no, "order_id": x.order_id, "type": x.type, "status": x.status, "priority": x.priority, "description": x.description, "owner": x.owner, "created_at": x.created_at} for x in items]}


@app.get("/api/settlements")
def settlements(user: User = Depends(current_user), db: Session = Depends(db_session)):
    items = db.scalars(select(Settlement).order_by(Settlement.created_at.desc())).all()
    return {"items": [{"id": x.id, "settlement_no": x.settlement_no, "order_id": x.order_id, "gross_amount": float(x.gross_amount or 0), "refund_amount": float(x.refund_amount or 0), "net_amount": float(x.net_amount or 0), "refund_recovery": float(x.refund_recovery or 0), "platform_fee": float(x.platform_fee or 0), "provider_share": float(x.provider_share or 0), "service_share": float(x.service_share or 0), "expert_fee": float(x.expert_fee or 0), "tax_amount": float(x.tax_amount or 0), "adjustment": float(x.adjustment or 0), "status": x.status, "created_at": x.created_at} for x in items]}


@app.post("/api/settlements/generate/{order_id}")
def generate_settlement(order_id: str, body: SettlementRuleBody | None = None, user: User = Depends(current_user), db: Session = Depends(db_session)):
    order = db.get(Order, order_id)
    if not order or order.payment_status not in {"paid", "refunding", "refunded"}:
        raise HTTPException(400, "订单尚未满足清算条件")
    existing = db.scalar(select(Settlement).where(Settlement.order_id == order.id).order_by(Settlement.created_at.desc()))
    if existing and existing.status == "locked":
        raise HTTPException(409, "订单清算单已锁定")
    body = body or SettlementRuleBody()
    gross = Decimal(str(order.paid_amount or order.amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    refund_amount = min(Decimal(str(order.refunded_amount or 0)), gross).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    net_amount = max(gross - refund_amount, Decimal("0.00"))
    platform_fee = (net_amount * Decimal(str(body.platform_rate)) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    service_share = (net_amount * Decimal(str(body.service_rate)) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    expert_fee = (net_amount * Decimal(str(body.expert_rate)) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    tax_amount = (net_amount * Decimal(str(body.tax_rate)) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    provider_share = (net_amount - platform_fee - service_share - expert_fee - tax_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    settlement = existing or Settlement(settlement_no="SET-" + secrets.token_hex(6).upper(), order_id=order.id)
    settlement.gross_amount = gross
    settlement.refund_amount = refund_amount
    settlement.net_amount = net_amount
    settlement.refund_recovery = refund_amount
    settlement.platform_fee = platform_fee
    settlement.service_share = service_share
    settlement.expert_fee = expert_fee
    settlement.tax_amount = tax_amount
    settlement.provider_share = provider_share
    settlement.status = "pending"
    if not existing:
        db.add(settlement)
    audit(db, user.email, "generate_settlement", "settlement", settlement.settlement_no, f"platform={body.platform_rate} service={body.service_rate} expert={body.expert_rate} tax={body.tax_rate}")
    db.commit()
    db.refresh(settlement)
    return {"id": settlement.id, "settlement_no": settlement.settlement_no, "order_id": settlement.order_id, "gross_amount": float(settlement.gross_amount), "refund_amount": float(settlement.refund_amount), "net_amount": float(settlement.net_amount), "refund_recovery": float(settlement.refund_recovery), "platform_fee": float(settlement.platform_fee), "provider_share": float(settlement.provider_share), "service_share": float(settlement.service_share), "expert_fee": float(settlement.expert_fee), "tax_amount": float(settlement.tax_amount), "adjustment": float(settlement.adjustment), "status": settlement.status}


@app.post("/api/settlements/{settlement_id}/adjust")
def adjust_settlement(settlement_id: str, body: SettlementAdjustmentBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    settlement = db.get(Settlement, settlement_id)
    if not settlement:
        raise HTTPException(404, "清算单不存在")
    if settlement.status == "locked":
        raise HTTPException(409, "已锁定清算单不能直接调整")
    adjustment = SettlementAdjustment(settlement_id=settlement.id, amount=body.amount, reason=body.reason, created_by=user.name)
    settlement.adjustment = Decimal(str(settlement.adjustment or 0)) + Decimal(str(body.amount))
    settlement.status = "adjusted"
    db.add(adjustment)
    audit(db, user.email, "adjust_settlement", "settlement", settlement.settlement_no, body.reason)
    db.commit()
    return {"settlement_no": settlement.settlement_no, "adjustment": float(settlement.adjustment), "status": settlement.status}


@app.post("/api/settlements/{settlement_id}/lock")
def lock_settlement(settlement_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    settlement = db.get(Settlement, settlement_id)
    if not settlement:
        raise HTTPException(404, "清算单不存在")
    settlement.status = "locked"
    audit(db, user.email, "lock_settlement", "settlement", settlement.settlement_no)
    db.commit()
    return {"settlement_no": settlement.settlement_no, "status": settlement.status}


@app.get("/api/audit-logs")
def audit_logs(user: User = Depends(current_user), db: Session = Depends(db_session)):
    items = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100)).all()
    return {"items": [{"id": x.id, "actor": x.actor, "action": x.action, "target_type": x.target_type, "target_id": x.target_id, "result": x.result, "detail": x.detail, "created_at": x.created_at} for x in items]}


@app.get("/api/users")
def users(user: User = Depends(current_user), db: Session = Depends(db_session)):
    items = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return {"items": [{"id": x.id, "name": x.name, "email": x.email, "verified_status": x.verified_status, "is_active": x.is_active, "created_at": x.created_at} for x in items]}


@app.get("/api/development/tasks")
def development_tasks(user: User = Depends(current_user), db: Session = Depends(db_session)):
    tasks = db.scalars(select(DevelopmentTask).order_by(DevelopmentTask.status, DevelopmentTask.priority, DevelopmentTask.code)).all()
    counts = {status: sum(1 for x in tasks if x.status == status) for status in ["todo", "in_progress", "review", "blocked", "done"]}
    total = len(tasks)
    return {"total": total, "counts": counts, "completion_rate": round((counts["done"] / total * 100) if total else 0, 1), "updated_at": now(), "items": [{"id": x.id, "code": x.code, "owner": x.owner, "title": x.title, "area": x.area, "priority": x.priority, "status": x.status, "dependencies": x.dependencies, "acceptance": x.acceptance, "progress": x.progress, "updated_at": x.updated_at} for x in tasks]}


@app.patch("/api/development/tasks/{code}")
def update_development_task(code: str, body: DevelopmentTaskUpdate, user: User = Depends(current_user), db: Session = Depends(db_session)):
    task = db.scalar(select(DevelopmentTask).where(DevelopmentTask.code == code))
    if not task:
        raise HTTPException(404, "开发任务不存在")
    if body.status is not None and body.status not in {"todo", "in_progress", "review", "blocked", "done"}:
        raise HTTPException(400, "不支持的任务状态")
    if body.status is not None:
        task.status = body.status
    if body.progress is not None:
        task.progress = body.progress
    if task.status == "done":
        task.progress = 100
    audit(db, user.email, "update_development_task", "development_task", task.code, body.note)
    db.commit()
    return {"code": task.code, "status": task.status, "progress": task.progress, "updated_at": task.updated_at}


def file_out(item: FileObject) -> dict[str, Any]:
    return {"id": item.id, "product_id": item.product_id, "object_name": item.object_name, "original_name": item.original_name, "content_type": item.content_type, "size": item.size, "checksum": item.checksum, "file_role": item.file_role, "version": item.version, "description": item.description, "status": item.status, "created_at": item.created_at}


@app.get("/api/products/{product_id}/files")
def product_files(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product_for_enterprise(product_id, user, db)
    items = db.scalars(select(FileObject).where(FileObject.product_id == product_id).order_by(FileObject.created_at.desc())).all()
    return {"items": [file_out(item) for item in items]}


@app.post("/api/files/upload")
def upload_file(
    upload: UploadFile = File(...),
    product_id: str | None = Form(default=None),
    file_role: str = Form(default="product_data"),
    version: str = Form(default="v1.0"),
    description: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    if product_id:
        product_for_enterprise(product_id, user, db)
    content = upload.file.read()
    object_name = f"{user.id}/{now().strftime('%Y%m%d')}/{secrets.token_hex(6)}-{upload.filename}"
    if MINIO_ENDPOINT:
        client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=False)
        if not client.bucket_exists(MINIO_BUCKET):
            client.make_bucket(MINIO_BUCKET)
        from io import BytesIO
        client.put_object(MINIO_BUCKET, object_name, BytesIO(content), length=len(content), content_type=upload.content_type or "application/octet-stream")
    item = FileObject(owner_id=user.id, product_id=product_id, object_name=object_name, original_name=upload.filename or "file", content_type=upload.content_type or "application/octet-stream", size=len(content), checksum=hashlib.sha256(content).hexdigest(), file_role=file_role, version=version, description=description)
    db.add(item)
    audit(db, user.email, "upload_file", "file", item.id, item.original_name)
    db.commit()
    db.refresh(item)
    return file_out(item)
