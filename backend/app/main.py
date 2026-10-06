from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from io import BytesIO
from urllib.parse import parse_qs, quote
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
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
    email: Mapped[str | None] = mapped_column(String(180), unique=True, index=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), unique=True, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_status: Mapped[str] = mapped_column(String(30), default="pending")
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    platform_role: Mapped[str] = mapped_column(String(60), default="")
    temporary_password_hash: Mapped[str] = mapped_column(String(255), default="")
    temporary_password_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Enterprise(Base):
    __tablename__ = "enterprises"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    name: Mapped[str] = mapped_column(String(180), unique=True)
    credit_code: Mapped[str] = mapped_column(String(40), unique=True)
    enterprise_type: Mapped[str] = mapped_column(String(100), default="")
    legal_representative: Mapped[str] = mapped_column(String(120), default="")
    license_file_id: Mapped[str] = mapped_column(String(36), default="")
    verification_status: Mapped[str] = mapped_column(String(30), default="verified")
    verified_by: Mapped[str] = mapped_column(String(180), default="")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Membership(Base):
    __tablename__ = "memberships"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    role: Mapped[str] = mapped_column(String(60), default="member")
    business_roles: Mapped[str] = mapped_column(String(255), default="provider,user")
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    invited_by: Mapped[str] = mapped_column(String(36), default="")
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IdentityVerification(Base):
    __tablename__ = "identity_verifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    id_name: Mapped[str] = mapped_column(String(120))
    id_number: Mapped[str] = mapped_column(String(40))
    id_front_file_id: Mapped[str] = mapped_column(String(36), default="")
    id_back_file_id: Mapped[str] = mapped_column(String(36), default="")
    phone: Mapped[str] = mapped_column(String(30))
    enterprise_id: Mapped[str] = mapped_column(String(36), default="")
    enterprise_role: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    review_comment: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[str] = mapped_column(String(180), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EnterpriseInvitation(Base):
    __tablename__ = "enterprise_invitations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    inviter_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    invitee_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    target: Mapped[str] = mapped_column(String(180))
    token: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class VerificationCode(Base):
    __tablename__ = "verification_codes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    channel: Mapped[str] = mapped_column(String(20))
    target: Mapped[str] = mapped_column(String(180), index=True)
    code: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SystemSetting(Base):
    __tablename__ = "system_settings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    setting_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    setting_value: Mapped[str] = mapped_column(Text, default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[str] = mapped_column(String(180), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ApplicationAccessGrant(Base):
    __tablename__ = "application_access_grants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    granted_by: Mapped[str] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(30), default="active")
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
    versions: Mapped[list["ProductReleaseVersion"]] = relationship(back_populates="product", cascade="all, delete-orphan", order_by="ProductReleaseVersion.created_at")
    security_scans: Mapped[list["ProductSecurityScan"]] = relationship(back_populates="product", cascade="all, delete-orphan", order_by="ProductSecurityScan.scanned_at.desc()")


class ProductReleaseVersion(Base):
    __tablename__ = "product_release_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    version_code: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    daily_quota: Mapped[int] = mapped_column(Integer, default=10000)
    monthly_quota: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProductSecurityScan(Base):
    __tablename__ = "product_security_scans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    engine: Mapped[str] = mapped_column(String(80), default="presidio+market-policy")
    status: Mapped[str] = mapped_column(String(30), default="manual_review", index=True)
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    findings_count: Mapped[int] = mapped_column(Integer, default=0)
    high_risk_count: Mapped[int] = mapped_column(Integer, default=0)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    reviewed_by: Mapped[str] = mapped_column(String(180), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_comment: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    product: Mapped[Product] = relationship(back_populates="security_scans")


class SaaSProductVersion(Base):
    __tablename__ = "saas_product_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    version_code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    monthly_price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    quarterly_price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    annual_price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    perpetual_price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    max_users: Mapped[int] = mapped_column(Integer, default=0)
    max_departments: Mapped[int] = mapped_column(Integer, default=0)
    max_storage_gb: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class SaaSIntegrationConfig(Base):
    __tablename__ = "saas_integration_configs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), unique=True, index=True)
    base_url: Mapped[str] = mapped_column(String(500), default="")
    operation_path: Mapped[str] = mapped_column(String(240), default="/isv.php")
    token_url: Mapped[str] = mapped_column(String(500), default="")
    client_id: Mapped[str] = mapped_column(String(180), default="")
    client_secret: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(String(255), default="")
    auth_mode: Mapped[str] = mapped_column(String(30), default="oauth2")
    timeout_ms: Mapped[int] = mapped_column(Integer, default=30000)
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    updated_by: Mapped[str] = mapped_column(String(180), default="")
    credentials_revealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class OAuthClient(Base):
    __tablename__ = "oauth_clients"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    client_secret: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(500), default="resource.invoke")
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class SaaSSubscription(Base):
    __tablename__ = "saas_subscriptions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("saas_product_versions.id"), index=True)
    billing_cycle: Mapped[str] = mapped_column(String(20), default="annual")
    external_tenant_id: Mapped[str] = mapped_column(String(180), default="", index=True)
    external_app_id: Mapped[str] = mapped_column(String(180), default="")
    status: Mapped[str] = mapped_column(String(30), default="provisioning", index=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recover_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class SaaSDepartment(Base):
    __tablename__ = "saas_departments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    subscription_id: Mapped[str] = mapped_column(ForeignKey("saas_subscriptions.id"), index=True)
    platform_department_id: Mapped[str] = mapped_column(String(36), default="")
    external_department_id: Mapped[str] = mapped_column(String(180), default="")
    name: Mapped[str] = mapped_column(String(180))
    parent_id: Mapped[str] = mapped_column(String(36), default="")
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SaaSUserMapping(Base):
    __tablename__ = "saas_user_mappings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    subscription_id: Mapped[str] = mapped_column(ForeignKey("saas_subscriptions.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    department_id: Mapped[str] = mapped_column(String(36), default="")
    external_user_id: Mapped[str] = mapped_column(String(180), default="")
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SaaSOperation(Base):
    __tablename__ = "saas_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    subscription_id: Mapped[str] = mapped_column(ForeignKey("saas_subscriptions.id"), index=True)
    operation: Mapped[str] = mapped_column(String(50), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    request_payload: Mapped[str] = mapped_column(Text, default="")
    response_payload: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="executing", index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiGatewayRoute(Base):
    __tablename__ = "api_gateway_routes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), unique=True, index=True)
    route_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    upstream_url: Mapped[str] = mapped_column(String(500))
    version: Mapped[str] = mapped_column(String(40), default="v1")
    auth_mode: Mapped[str] = mapped_column(String(30), default="api_key")
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    daily_quota: Mapped[int] = mapped_column(Integer, default=10000)
    monthly_quota: Mapped[int] = mapped_column(Integer, default=0)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=30000)
    strip_prefix: Mapped[bool] = mapped_column(Boolean, default=True)
    health_path: Mapped[str] = mapped_column(String(240), default="/health")
    health_method: Mapped[str] = mapped_column(String(10), default="GET")
    health_message: Mapped[str] = mapped_column(Text, default="")
    upstream_auth_mode: Mapped[str] = mapped_column(String(30), default="oauth2")
    upstream_scope: Mapped[str] = mapped_column(String(500), default="resource.invoke")
    upstream_client_id: Mapped[str] = mapped_column(String(180), default="")
    upstream_client_secret: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    created_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class GatewayConfigRevision(Base):
    __tablename__ = "gateway_config_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    route_id: Mapped[str] = mapped_column(ForeignKey("api_gateway_routes.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GatewayPublishRecord(Base):
    __tablename__ = "gateway_publish_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    route_id: Mapped[str] = mapped_column(ForeignKey("api_gateway_routes.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("gateway_config_revisions.id"), index=True)
    target: Mapped[str] = mapped_column(String(100), default="apisix")
    status: Mapped[str] = mapped_column(String(30), default="executing", index=True)
    response_json: Mapped[str] = mapped_column(Text, default="{}")
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiCredential(Base):
    __tablename__ = "api_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    route_id: Mapped[str] = mapped_column(ForeignKey("api_gateway_routes.id"), index=True)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    order_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    product_version_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    name: Mapped[str] = mapped_column(String(120), default="默认 API 凭证")
    key_prefix: Mapped[str] = mapped_column(String(24), default="")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    apisix_consumer_name: Mapped[str] = mapped_column(String(180), default="")
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ApiUsage(Base):
    __tablename__ = "api_usage"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    route_id: Mapped[str] = mapped_column(ForeignKey("api_gateway_routes.id"), index=True)
    credential_id: Mapped[str] = mapped_column(ForeignKey("api_credentials.id"), index=True)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    method: Mapped[str] = mapped_column(String(12))
    path: Mapped[str] = mapped_column(String(500), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    request_bytes: Mapped[int] = mapped_column(Integer, default=0)
    response_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    order_no: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    buyer_enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    buyer_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), default="", index=True)
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
    product_version_id: Mapped[str] = mapped_column(String(36), default="")
    product_version_code: Mapped[str] = mapped_column(String(60), default="")
    product_version_name: Mapped[str] = mapped_column(String(120), default="")
    billing_cycle: Mapped[str] = mapped_column(String(20), default="")
    subscription_id: Mapped[str] = mapped_column(String(36), default="")
    business_type: Mapped[str] = mapped_column(String(30), default="")
    related_order_id: Mapped[str] = mapped_column(String(36), default="")
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
    identifier: str | None = None
    email: str | None = None
    password: str


class RegisterBody(BaseModel):
    email: str | None = None
    phone: str | None = None
    name: str
    password: str = Field(min_length=8)
    verification_code: str = ""


class CodeBody(BaseModel):
    channel: str
    target: str
    purpose: str = "register"


class PersonalVerificationBody(BaseModel):
    id_name: str
    id_number: str
    id_front_file_id: str
    id_back_file_id: str
    phone: str
    phone_code: str
    enterprise_id: str = ""
    enterprise_role: str = ""


class VerificationReviewBody(BaseModel):
    decision: str
    comment: str = ""


class EnterpriseVerificationBody(BaseModel):
    license_file_id: str
    enterprise_name: str
    credit_code: str
    enterprise_type: str
    legal_representative: str


class InviteMemberBody(BaseModel):
    target: str


class MembershipRoleBody(BaseModel):
    role: str


class NotificationSettingsBody(BaseModel):
    sms_provider: str = ""
    sms_endpoint: str = ""
    email_host: str = "imap.263.net"
    email_ssl: bool = False
    email_port: int = 143
    email_username: str = ""
    email_password: str = ""


class PlatformRoleAssignmentBody(BaseModel):
    identifier: str
    name: str = ""
    role: str
    channel: str = "email"


class ApplicationAccessBody(BaseModel):
    user_id: str


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
    versions: list["ProductVersionBody"] = Field(default_factory=list)


class ProductVersionBody(BaseModel):
    version_code: str = Field(min_length=1, max_length=60)
    description: str = ""
    price: float = Field(default=0, ge=0)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=100000)
    daily_quota: int = Field(default=10000, ge=1, le=100000000)
    monthly_quota: int = Field(default=0, ge=0, le=3000000000)
    status: str = "active"


class ProductVersionUpdateBody(ProductVersionBody):
    pass


class ProductReviewBody(BaseModel):
    decision: str
    comment: str = ""


class ProductUnpublishBody(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class ProductSecurityReviewBody(BaseModel):
    decision: str
    comment: str = ""


class SaaSVersionBody(BaseModel):
    version_code: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    monthly_price: float = Field(default=0, ge=0)
    quarterly_price: float = Field(default=0, ge=0)
    annual_price: float = Field(default=0, ge=0)
    perpetual_price: float = Field(default=0, ge=0)
    max_users: int = Field(default=0, ge=0)
    max_departments: int = Field(default=0, ge=0)
    max_storage_gb: int = Field(default=0, ge=0)
    status: str = "draft"


class SaaSIntegrationBody(BaseModel):
    base_url: str = Field(min_length=8, max_length=500)
    operation_path: str = "/isv.php"
    token_url: str = ""
    client_id: str
    client_secret: str = ""
    scope: str = ""
    auth_mode: str = "oauth2"
    timeout_ms: int = Field(default=30000, ge=100, le=120000)
    retention_days: int = Field(default=30, ge=30, le=3650)


class SaaSSubscriptionBody(BaseModel):
    version_id: str
    billing_cycle: str = "annual"


class SaaSChangeBody(BaseModel):
    version_id: str
    billing_cycle: str = "annual"


class SaaSRenewBody(BaseModel):
    billing_cycle: str = "annual"


class SaaSUserBody(BaseModel):
    user_id: str
    department_id: str = ""


class SaaSDepartmentBody(BaseModel):
    name: str
    parent_id: str = ""
    platform_department_id: str = ""


class GatewayConfigBody(BaseModel):
    upstream_url: str = Field(min_length=8, max_length=500)
    route_key: str = Field(default="", max_length=100)
    version: str = Field(default="v1", max_length=40)
    auth_mode: str = "api_key"
    rate_limit_per_minute: int = Field(default=60, ge=1, le=100000)
    daily_quota: int = Field(default=10000, ge=1, le=100000000)
    monthly_quota: int = Field(default=0, ge=0, le=3000000000)
    timeout_ms: int = Field(default=30000, ge=100, le=120000)
    strip_prefix: bool = True
    health_path: str = Field(default="/health", max_length=240)
    health_method: str = Field(default="GET", pattern="^(GET|HEAD)$")
    upstream_auth_mode: str = Field(default="oauth2", pattern="^(oauth2|none)$")
    upstream_scope: str = Field(default="resource.invoke", max_length=500)


class GatewayCredentialBody(BaseModel):
    name: str = "默认 API 凭证"
    enterprise_id: str = ""
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=100000)
    daily_quota: int | None = Field(default=None, ge=1, le=100000000)
    monthly_quota: int | None = Field(default=None, ge=1, le=3000000000)
    expires_at: datetime | None = None


class GatewayValidateBody(BaseModel):
    publish: bool = False


class ProductFileMetadata(BaseModel):
    file_role: str = "product_data"
    version: str = "v1.0"
    description: str = ""


class OrderBody(BaseModel):
    product_id: str
    product_version_id: str = ""


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
    email: str | None
    phone: str | None
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
    db.add(AuditLog(actor=actor or "unknown", action=action, target_type=target_type, target_id=target_id, detail=detail))


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
    membership = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.status == "active").order_by(Membership.created_at))
    if not membership:
        raise HTTPException(400, "用户尚未加入企业")
    return db.get(Enterprise, membership.enterprise_id)


def current_membership(db: Session, user: User, enterprise_id: str | None = None) -> Membership:
    stmt = select(Membership).where(Membership.user_id == user.id, Membership.status == "active")
    if enterprise_id:
        stmt = stmt.where(Membership.enterprise_id == enterprise_id)
    membership = db.scalar(stmt.order_by(Membership.created_at))
    if not membership:
        raise HTTPException(403, "用户尚未加入有效企业")
    return membership


def require_enterprise_admin(db: Session, user: User, enterprise_id: str | None = None) -> Membership:
    membership = current_membership(db, user, enterprise_id)
    if membership.role not in {"super_admin", "enterprise_admin"}:
        raise HTTPException(403, "只有企业超级管理员或企业管理员可以执行此操作")
    return membership


def require_platform_admin(user: User):
    if user.platform_role not in {"super_admin", "platform_operator"}:
        raise HTTPException(403, "只有系统管理员或平台运营管理员可以执行此操作")


def require_security_operator(user: User):
    if user.platform_role not in {"super_admin", "platform_operator", "security_compliance"}:
        raise HTTPException(403, "只有平台管理员或安全合规人员可以执行安全策略检查")


PLATFORM_ROLES = {
    "platform_operator": "平台运营人员",
    "product_manager": "数据产品经理",
    "business_reviewer": "业务审核人员",
    "quality_reviewer": "质量审核人员",
    "security_compliance": "安全合规人员",
    "delivery_monitor": "交付监控人员",
    "finance_settlement": "财务清算人员",
}


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
    "cancel_order": ("main", "cancelled", "cancelled", "取消订单"),
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
        "users": {
            "phone": "VARCHAR(30)",
            "phone_verified": "BOOLEAN",
            "email_verified": "BOOLEAN",
            "platform_role": "VARCHAR(60)",
            "temporary_password_hash": "VARCHAR(255)",
            "temporary_password_expires_at": "TIMESTAMP WITH TIME ZONE",
        },
        "enterprises": {
            "enterprise_type": "VARCHAR(100)",
            "legal_representative": "VARCHAR(120)",
            "license_file_id": "VARCHAR(36)",
            "verified_by": "VARCHAR(180)",
            "verified_at": "TIMESTAMP WITH TIME ZONE",
        },
        "memberships": {
            "status": "VARCHAR(30)",
            "invited_by": "VARCHAR(36)",
            "joined_at": "TIMESTAMP WITH TIME ZONE",
        },
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
        "orders": {
            "buyer_user_id": "VARCHAR(36)",
            "product_version_id": "VARCHAR(36)",
            "product_version_code": "VARCHAR(60)",
            "product_version_name": "VARCHAR(120)",
            "billing_cycle": "VARCHAR(20)",
            "subscription_id": "VARCHAR(36)",
            "business_type": "VARCHAR(30)",
            "related_order_id": "VARCHAR(36)",
        },
        "saas_integration_configs": {
            "credentials_revealed_at": "TIMESTAMP WITH TIME ZONE",
        },
        "api_gateway_routes": {
            "monthly_quota": "INTEGER DEFAULT 0",
            "health_path": "VARCHAR(240) DEFAULT '/health'",
            "health_method": "VARCHAR(10) DEFAULT 'GET'",
            "health_message": "TEXT DEFAULT ''",
            "upstream_auth_mode": "VARCHAR(30) DEFAULT 'oauth2'",
            "upstream_scope": "VARCHAR(500) DEFAULT 'resource.invoke'",
            "upstream_client_id": "VARCHAR(180) DEFAULT ''",
            "upstream_client_secret": "TEXT DEFAULT ''",
        },
        "oauth_clients": {
            "client_id": "VARCHAR(180)",
            "client_secret": "TEXT",
            "scope": "VARCHAR(500) DEFAULT 'resource.invoke'",
            "status": "VARCHAR(30) DEFAULT 'active'",
            "created_at": "TIMESTAMP WITH TIME ZONE",
            "updated_at": "TIMESTAMP WITH TIME ZONE",
        },
        "api_credentials": {
            "order_id": "VARCHAR(36) DEFAULT ''",
            "product_version_id": "VARCHAR(36) DEFAULT ''",
            "monthly_quota": "INTEGER",
            "apisix_consumer_name": "VARCHAR(180) DEFAULT ''",
        },
        "product_release_versions": {
            "rate_limit_per_minute": "INTEGER DEFAULT 60",
            "daily_quota": "INTEGER DEFAULT 10000",
            "monthly_quota": "INTEGER DEFAULT 0",
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
        connection.execute(text("UPDATE memberships SET status = 'active' WHERE status IS NULL"))
        connection.execute(text("UPDATE users SET phone_verified = FALSE WHERE phone_verified IS NULL"))
        connection.execute(text("UPDATE users SET email_verified = FALSE WHERE email_verified IS NULL"))
        if engine.dialect.name != "sqlite":
            connection.execute(text("ALTER TABLE users ALTER COLUMN email DROP NOT NULL"))


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    ensure_product_metadata_schema()
    ensure_review_and_file_schema()
    with SessionLocal() as db:
        for product in db.scalars(select(Product)).all():
            if not product.versions:
                db.add(ProductReleaseVersion(product_id=product.id, version_code=product.version or "v1.0", description=product.description or "", price=product.price or 0, status="active"))
        db.commit()
        for product in db.scalars(select(Product).where(Product.product_type.in_(["api", "model", "saas"]), Product.status == "published")).all():
            client = ensure_oauth_client(db, product)
            route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id))
            if route and not route.upstream_client_id:
                route.upstream_client_id = client.client_id
                route.upstream_client_secret = client.client_secret
        db.commit()
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
        followup_tasks = [
            ("BE-006", "后端 Agent", "手机号/邮箱注册与个人实名认证", "身份认证", "P0", "done", "BE-001", "注册、验证码、身份证材料和人工审核可闭环", 100),
            ("BE-007", "后端 Agent", "企业实名认证、邀请入企和企业角色权限", "租户权限", "P0", "done", "BE-006", "企业人工审核、邀请、成员角色和订单权限可验证", 100),
            ("BE-008", "后端 Agent", "平台角色、临时密码和通知平台配置", "平台管理", "P1", "done", "BE-001", "7 类平台角色可分配，短信/邮件配置可保存", 100),
            ("FE-004", "前端 Agent", "注册、实名认证和系统设置工作台", "前端", "P1", "done", "BE-006,BE-008", "首页注册、认证材料、通知配置和平台角色页面可用", 100),
            ("BE-009", "后端 Agent", "统一 API 网关控制面、鉴权、配额和调用统计", "API 网关", "P0", "done", "BE-002,BE-007", "API 路由、API Key、限流、日配额和调用统计可用", 100),
            ("OPS-004", "部署测试 Agent", "统一 API 网关集群和 API 服务独立部署", "部署", "P0", "done", "BE-009,OPS-001", "market-gateway Pod Ready，API 请求可转发", 100),
            ("ARC-004", "主 Agent", "API Gateway 生产重构需求、目标架构、接口边界和验收标准", "架构", "P0", "done", "ARC-002,BE-009,OPS-004", "生产重构设计文档完成并纳入任务台账", 100),
            ("OPS-006", "部署测试 Agent", "APISIX、etcd、Redis 高可用基础资源设计与部署", "部署", "P0", "todo", "ARC-004", "market 命名空间资源可部署，APISIX 健康检查通过", 0),
            ("BE-012", "后端 Agent", "APISIX Admin API 控制面适配器", "后端", "P0", "todo", "ARC-004,OPS-006", "可创建/更新/删除路由、上游和策略，并记录响应", 0),
            ("BE-013", "后端 Agent", "网关配置版本、发布记录、校验和回滚数据模型", "后端", "P0", "todo", "ARC-004,BE-012", "发布版本、失败原因和回滚目标可查询", 0),
            ("BE-014", "后端 Agent", "API Key、订单授权、OAuth Token 共享缓存迁移", "后端", "P0", "todo", "BE-012,BE-013", "多副本下认证、授权、Token 缓存和回收一致", 0),
            ("BE-015", "后端 Agent", "APISIX 限流、配额、版本策略和统一错误适配", "后端", "P0", "todo", "BE-012,BE-014", "多副本下版本级限流/日配额/月配额准确生效", 0),
            ("BE-016", "后端 Agent", "网关健康检查、自动发布、灰度切换和回滚流程", "后端", "P0", "todo", "BE-013,BE-015", "发布失败不覆盖稳定版本，回滚可恢复服务", 0),
            ("FE-006", "前端 Agent", "API Gateway 路由、上游和版本策略管理页面", "前端", "P0", "todo", "BE-012,BE-013", "可查看/编辑配置、校验并提交发布", 0),
            ("FE-007", "前端 Agent", "网关凭据、配额、限流和调用统计页面", "前端", "P0", "todo", "BE-014,BE-015", "订单所有者和平台角色按权限查看对应数据", 0),
            ("FE-008", "前端 Agent", "网关健康状态、发布历史、回滚和告警页面", "前端", "P1", "todo", "BE-016,OPS-006", "可查看副本、上游、发布、告警和审计状态", 0),
            ("OPS-007", "部署测试 Agent", "APISIX 监控、日志、告警和生产入口", "部署", "P0", "todo", "OPS-006,BE-016", "Prometheus 指标、日志和核心告警可验证", 0),
            ("OPS-008", "部署测试 Agent", "旧 FastAPI 网关兼容、灰度切换和回退方案", "部署", "P1", "todo", "BE-014,BE-016,OPS-007", "API 可按产品灰度切换，故障可回退旧网关", 0),
            ("OPS-009", "部署测试 Agent", "网关故障演练、压测和端到端验收", "测试", "P0", "todo", "FE-006,FE-007,FE-008,OPS-008", "多副本、Redis、上游、发布回滚和配额测试通过", 0),
            ("ARC-005", "主 Agent", "APISIX 原生数据面迁移方案、切换门禁和回退标准", "架构", "P0", "in_progress", "ARC-004", "原生数据面、FastAPI 控制面、灰度和回退边界固化", 60),
            ("BE-017", "后端 Agent", "APISIX Consumer/API Key 原生同步", "后端", "P0", "done", "ARC-005", "凭据创建、停用、重生成可同步 APISIX Consumer", 100),
            ("BE-018", "后端 Agent", "APISIX 原生直连上游和路由发布模式", "后端", "P0", "done", "BE-017", "路由可绕过 FastAPI 兼容网关直接访问第三方上游", 100),
            ("BE-019", "后端 Agent", "原生数据面配额插件和统一错误策略", "后端", "P0", "todo", "BE-018", "原生数据面支持日/月配额、订单授权回收和统一错误", 0),
            ("OPS-010", "部署测试 Agent", "APISIX 原生模式灰度切换、双入口回退和生产验收", "部署", "P0", "todo", "BE-017,BE-018,BE-019", "原生入口灰度成功，故障可自动回退兼容入口", 0),
        ]
        for task in followup_tasks:
            if not db.scalar(select(DevelopmentTask.id).where(DevelopmentTask.code == task[0])):
                db.add(DevelopmentTask(code=task[0], owner=task[1], title=task[2], area=task[3], priority=task[4], status=task[5], dependencies=task[6], acceptance=task[7], progress=task[8]))
        db.commit()
        admin = db.scalar(select(User).where(User.email == "admin@market.local"))
        if admin:
            if not admin.platform_role:
                admin.platform_role = "super_admin"
                db.commit()
            return
        admin = User(email="admin@market.local", name="平台管理员", password_hash=hash_password("Admin123!"), verified_status="verified", platform_role="super_admin", email_verified=True)
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


@app.get("/metrics", include_in_schema=False)
def metrics():
    """Small dependency-free Prometheus endpoint for the control plane."""
    body = "# HELP market_api_up Control plane readiness\n# TYPE market_api_up gauge\nmarket_api_up 1\n"
    return Response(content=body, media_type="text/plain; version=0.0.4")


@app.post("/api/auth/login")
def login(body: LoginBody, db: Session = Depends(db_session)):
    identifier = (body.identifier or body.email or "").lower().strip()
    user = db.scalar(select(User).where(or_(User.email == identifier, User.phone == identifier)))
    valid_password = user and verify_password(body.password, user.password_hash)
    if user and not valid_password and user.temporary_password_hash and user.temporary_password_expires_at and user.temporary_password_expires_at > now():
        valid_password = verify_password(body.password, user.temporary_password_hash)
    if not user or not valid_password:
        raise HTTPException(401, "邮箱或密码错误")
    audit(db, user.email, "login", "user", user.id)
    db.commit()
    return {"token": issue_token(user), "user": UserOut.model_validate(user).model_dump()}


@app.post("/api/auth/send-code")
def send_verification_code(body: CodeBody, db: Session = Depends(db_session)):
    if body.channel not in {"sms", "email"}:
        raise HTTPException(400, "验证码渠道必须是 sms 或 email")
    if not body.target.strip():
        raise HTTPException(400, "验证码目标不能为空")
    item = VerificationCode(channel=body.channel, target=body.target.strip(), code="123456", purpose=body.purpose, expires_at=now() + timedelta(minutes=10 if body.channel == "sms" else 60))
    db.add(item)
    db.commit()
    return {"channel": body.channel, "target": body.target, "expires_at": item.expires_at, "provider_configured": False, "development_hint": "当前为开发环境，验证码固定为 123456，未执行真实短信或邮件发送"}


@app.post("/api/auth/register")
def register(body: RegisterBody, db: Session = Depends(db_session)):
    email = body.email.lower().strip() if body.email else None
    phone = body.phone.strip() if body.phone else None
    if not email and not phone:
        raise HTTPException(400, "邮箱和手机号码至少填写一项")
    if phone and not body.verification_code:
        raise HTTPException(400, "手机注册需要填写开发环境验证码 123456")
    if phone and body.verification_code != "123456":
        raise HTTPException(400, "验证码错误")
    if email and db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "邮箱已注册")
    if phone and db.scalar(select(User).where(User.phone == phone)):
        raise HTTPException(409, "手机号码已注册")
    user = User(email=email, phone=phone, name=body.name, password_hash=hash_password(body.password), phone_verified=bool(phone), email_verified=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"token": issue_token(user), "user": UserOut.model_validate(user).model_dump()}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = first_enterprise(db, user)
    membership = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.enterprise_id == enterprise.id))
    return {"user": UserOut.model_validate(user), "enterprise": {"id": enterprise.id, "name": enterprise.name}, "role": membership.role if membership else "member", "business_roles": (membership.business_roles.split(",") if membership else [])}


def personal_verification_out(item: IdentityVerification) -> dict[str, Any]:
    return {"id": item.id, "user_id": item.user_id, "id_name": item.id_name, "id_number": item.id_number[-4:].rjust(len(item.id_number), "*") if item.id_number else "", "id_front_file_id": item.id_front_file_id, "id_back_file_id": item.id_back_file_id, "phone": item.phone, "enterprise_id": item.enterprise_id, "enterprise_role": item.enterprise_role, "status": item.status, "review_comment": item.review_comment, "reviewed_by": item.reviewed_by, "reviewed_at": item.reviewed_at, "created_at": item.created_at}


@app.get("/api/verification/personal/me")
def personal_verification_me(user: User = Depends(current_user), db: Session = Depends(db_session)):
    item = db.scalar(select(IdentityVerification).where(IdentityVerification.user_id == user.id).order_by(IdentityVerification.created_at.desc()))
    return {"item": personal_verification_out(item) if item else None, "user_verified_status": user.verified_status}


@app.post("/api/verification/personal")
def submit_personal_verification(body: PersonalVerificationBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    if user.phone and body.phone != user.phone:
        raise HTTPException(400, "实名认证手机号必须与注册手机号一致")
    if body.phone_code != "123456":
        raise HTTPException(400, "请先完成手机验证码验证")
    if not user.phone:
        existing_phone = db.scalar(select(User).where(User.phone == body.phone, User.id != user.id))
        if existing_phone:
            raise HTTPException(409, "该手机号已绑定其他用户")
        user.phone = body.phone
    user.phone_verified = True
    if not body.id_front_file_id or not body.id_back_file_id:
        raise HTTPException(400, "必须提交身份证正反面图片")
    item = IdentityVerification(user_id=user.id, id_name=body.id_name, id_number=body.id_number, id_front_file_id=body.id_front_file_id, id_back_file_id=body.id_back_file_id, phone=body.phone, enterprise_id=body.enterprise_id, enterprise_role=body.enterprise_role)
    user.verified_status = "pending_review"
    db.add(item)
    audit(db, user.email or user.phone or user.id, "submit_personal_verification", "identity_verification", item.id)
    db.commit()
    db.refresh(item)
    return personal_verification_out(item)


@app.get("/api/admin/verifications/personal")
def personal_verifications(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    items = db.scalars(select(IdentityVerification).order_by(IdentityVerification.created_at.desc())).all()
    return {"items": [personal_verification_out(item) for item in items]}


@app.post("/api/admin/verifications/personal/{verification_id}/review")
def review_personal_verification(verification_id: str, body: VerificationReviewBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    item = db.get(IdentityVerification, verification_id)
    if not item:
        raise HTTPException(404, "实名认证申请不存在")
    if body.decision not in {"approve", "reject"}:
        raise HTTPException(400, "审核结论必须是 approve 或 reject")
    if body.decision == "reject" and not body.comment.strip():
        raise HTTPException(400, "驳回时必须填写原因")
    item.status = "verified" if body.decision == "approve" else "rejected"
    item.review_comment = body.comment.strip()
    item.reviewed_by = user.email or user.phone or user.id
    item.reviewed_at = now()
    applicant = db.get(User, item.user_id)
    if applicant:
        applicant.verified_status = item.status
    audit(db, user.email or user.phone or user.id, "review_personal_verification", "identity_verification", item.id, item.review_comment)
    db.commit()
    return personal_verification_out(item)


@app.post("/api/verification/enterprise")
def submit_enterprise_verification(body: EnterpriseVerificationBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    if user.verified_status != "verified":
        raise HTTPException(403, "请先完成个人实名认证")
    existing = db.scalar(select(Enterprise).where(Enterprise.credit_code == body.credit_code))
    if existing:
        raise HTTPException(409, "统一社会信用代码已存在")
    enterprise = Enterprise(name=body.enterprise_name, credit_code=body.credit_code, enterprise_type=body.enterprise_type, legal_representative=body.legal_representative, license_file_id=body.license_file_id, verification_status="pending_review")
    db.add(enterprise)
    db.flush()
    db.add(Membership(user_id=user.id, enterprise_id=enterprise.id, role="super_admin", business_roles="provider,user,service_provider", status="active", joined_at=now()))
    audit(db, user.email or user.phone or user.id, "submit_enterprise_verification", "enterprise", enterprise.id, enterprise.name)
    db.commit()
    return {"id": enterprise.id, "name": enterprise.name, "credit_code": enterprise.credit_code, "enterprise_type": enterprise.enterprise_type, "legal_representative": enterprise.legal_representative, "verification_status": enterprise.verification_status}


@app.get("/api/verification/enterprise/me")
def enterprise_verification_me(user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = first_enterprise(db, user)
    return {"id": enterprise.id, "name": enterprise.name, "credit_code": enterprise.credit_code, "enterprise_type": enterprise.enterprise_type, "legal_representative": enterprise.legal_representative, "verification_status": enterprise.verification_status}


@app.get("/api/admin/verifications/enterprise")
def enterprise_verifications(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    items = db.scalars(select(Enterprise).order_by(Enterprise.created_at.desc())).all()
    return {"items": [{"id": x.id, "name": x.name, "credit_code": x.credit_code, "enterprise_type": x.enterprise_type, "legal_representative": x.legal_representative, "license_file_id": x.license_file_id, "verification_status": x.verification_status, "verified_by": x.verified_by, "verified_at": x.verified_at} for x in items]}


@app.post("/api/admin/verifications/enterprise/{enterprise_id}/review")
def review_enterprise_verification(enterprise_id: str, body: VerificationReviewBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    enterprise = db.get(Enterprise, enterprise_id)
    if not enterprise:
        raise HTTPException(404, "企业认证申请不存在")
    if body.decision not in {"approve", "reject"}:
        raise HTTPException(400, "审核结论必须是 approve 或 reject")
    if body.decision == "reject" and not body.comment.strip():
        raise HTTPException(400, "驳回时必须填写原因")
    enterprise.verification_status = "verified" if body.decision == "approve" else "rejected"
    enterprise.verified_by = user.email or user.phone or user.id
    enterprise.verified_at = now()
    audit(db, user.email or user.phone or user.id, "review_enterprise_verification", "enterprise", enterprise.id, body.comment)
    db.commit()
    return {"id": enterprise.id, "verification_status": enterprise.verification_status, "verified_by": enterprise.verified_by, "verified_at": enterprise.verified_at}


@app.post("/api/enterprise/invitations")
def invite_member(body: InviteMemberBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    membership = require_enterprise_admin(db, user)
    target = body.target.strip().lower()
    invitee = db.scalar(select(User).where(or_(User.email == target, User.phone == target)))
    if not invitee:
        raise HTTPException(404, "被邀请用户尚未注册")
    existing = db.scalar(select(Membership).where(Membership.user_id == invitee.id, Membership.enterprise_id == membership.enterprise_id, Membership.status == "active"))
    if existing:
        raise HTTPException(409, "用户已经加入该企业")
    invitation = EnterpriseInvitation(enterprise_id=membership.enterprise_id, inviter_id=user.id, invitee_id=invitee.id, target=target, token=secrets.token_urlsafe(24), expires_at=now() + timedelta(days=7))
    db.add(invitation)
    audit(db, user.email or user.phone or user.id, "invite_enterprise_member", "enterprise_invitation", invitation.id, target)
    db.commit()
    return {"id": invitation.id, "target": invitation.target, "token": invitation.token, "status": invitation.status, "expires_at": invitation.expires_at, "delivery": "邮件发送接口预留，当前返回开发环境邀请令牌"}


@app.get("/api/enterprise/invitations")
def enterprise_invitations(user: User = Depends(current_user), db: Session = Depends(db_session)):
    membership = require_enterprise_admin(db, user)
    items = db.scalars(select(EnterpriseInvitation).where(EnterpriseInvitation.enterprise_id == membership.enterprise_id).order_by(EnterpriseInvitation.created_at.desc())).all()
    return {"items": [{"id": x.id, "target": x.target, "status": x.status, "expires_at": x.expires_at, "created_at": x.created_at} for x in items]}


@app.post("/api/enterprise/invitations/{token}/accept")
def accept_enterprise_invitation(token: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    invitation = db.scalar(select(EnterpriseInvitation).where(EnterpriseInvitation.token == token, EnterpriseInvitation.invitee_id == user.id))
    if not invitation or invitation.status != "pending" or invitation.expires_at <= now():
        raise HTTPException(400, "邀请不存在、已处理或已过期")
    invitation.status = "accepted"
    db.add(Membership(user_id=user.id, enterprise_id=invitation.enterprise_id, role="member", business_roles="provider,user", status="active", invited_by=invitation.inviter_id, joined_at=now()))
    audit(db, user.email or user.phone or user.id, "accept_enterprise_invitation", "enterprise_invitation", invitation.id)
    db.commit()
    return {"status": invitation.status, "enterprise_id": invitation.enterprise_id, "role": "member"}


@app.get("/api/enterprise/members")
def enterprise_members(user: User = Depends(current_user), db: Session = Depends(db_session)):
    membership = current_membership(db, user)
    rows = db.execute(select(Membership, User).join(User, User.id == Membership.user_id).where(Membership.enterprise_id == membership.enterprise_id, Membership.status == "active")).all()
    return {"items": [{"membership_id": m.id, "user_id": u.id, "name": u.name, "email": u.email, "phone": u.phone, "role": m.role, "business_roles": m.business_roles.split(","), "verified_status": u.verified_status} for m, u in rows]}


@app.patch("/api/enterprise/members/{membership_id}")
def update_enterprise_member(membership_id: str, body: MembershipRoleBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    admin = require_enterprise_admin(db, user)
    if body.role not in {"member", "enterprise_admin"}:
        raise HTTPException(400, "企业成员角色只能是 member 或 enterprise_admin")
    target = db.get(Membership, membership_id)
    if not target or target.enterprise_id != admin.enterprise_id or target.status != "active":
        raise HTTPException(404, "企业成员不存在")
    target.role = body.role
    audit(db, user.email or user.phone or user.id, "update_enterprise_member_role", "membership", target.id, body.role)
    db.commit()
    return {"membership_id": target.id, "role": target.role}


@app.get("/api/admin/settings/notifications")
def notification_settings(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    values = {item.setting_key: item.setting_value for item in db.scalars(select(SystemSetting)).all()}
    return {"sms_provider": values.get("sms_provider", ""), "sms_endpoint": values.get("sms_endpoint", ""), "email_host": values.get("email_host", "imap.263.net"), "email_ssl": values.get("email_ssl", "false") == "true", "email_port": int(values.get("email_port", "143")), "email_username": values.get("email_username", ""), "email_password_configured": bool(values.get("email_password")), "email_password": ""}


@app.patch("/api/admin/settings/notifications")
def update_notification_settings(body: NotificationSettingsBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    values = body.model_dump()
    for key, value in values.items():
        if key == "email_password" and not value:
            continue
        item = db.scalar(select(SystemSetting).where(SystemSetting.setting_key == key))
        if not item:
            item = SystemSetting(setting_key=key, is_secret=key == "email_password")
            db.add(item)
        item.setting_value = str(value).lower() if isinstance(value, bool) else str(value)
        item.updated_by = user.email or user.phone or user.id
    audit(db, user.email or user.phone or user.id, "update_notification_settings", "system_setting")
    db.commit()
    return notification_settings(user, db)


@app.get("/api/admin/platform-roles")
def platform_roles(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    items = db.scalars(select(User).where(User.platform_role != "").order_by(User.created_at.desc())).all()
    return {"roles": PLATFORM_ROLES, "items": [{"id": x.id, "name": x.name, "email": x.email, "phone": x.phone, "role": x.platform_role, "role_name": PLATFORM_ROLES.get(x.platform_role, x.platform_role)} for x in items]}


@app.post("/api/admin/platform-roles")
def assign_platform_role(body: PlatformRoleAssignmentBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    if body.role not in PLATFORM_ROLES:
        raise HTTPException(400, "不支持的平台角色")
    identifier = body.identifier.strip().lower()
    target = db.scalar(select(User).where(or_(User.email == identifier, User.phone == identifier)))
    if not target:
        raise HTTPException(404, "用户不存在，请先完成注册")
    temp_password = secrets.token_urlsafe(9)
    target.name = body.name or target.name
    target.platform_role = body.role
    target.temporary_password_hash = hash_password(temp_password)
    target.temporary_password_expires_at = now() + timedelta(minutes=10 if body.channel == "sms" else 60)
    audit(db, user.email or user.phone or user.id, "assign_platform_role", "user", target.id, body.role)
    db.commit()
    return {"user_id": target.id, "role": body.role, "role_name": PLATFORM_ROLES[body.role], "delivery": f"{body.channel} 发送接口预留", "temporary_password": temp_password, "expires_at": target.temporary_password_expires_at}


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
    versions = [{"id": x.id, "product_id": x.product_id, "version_code": x.version_code, "description": x.description or "", "price": float(x.price or 0), "rate_limit_per_minute": x.rate_limit_per_minute, "daily_quota": x.daily_quota, "monthly_quota": x.monthly_quota, "status": x.status, "created_at": x.created_at, "updated_at": x.updated_at} for x in (p.versions or [])]
    return {"id": p.id, "name": p.name, "product_type": p.product_type, "catalog_name": p.catalog_name or "未分类", "provider_name": p.provider_name or "", "provider_type": p.provider_type or "企业", "description": p.description, "usage_scenarios": p.usage_scenarios or "", "status": p.status, "delivery_method": p.delivery_method, "price": float(p.price or 0), "pricing_strategy": p.pricing_strategy or "", "currency": p.currency, "version": p.version, "versions": versions, "quality_level": p.quality_level, "security_level": p.security_level or "一般", "authorization_conditions": p.authorization_conditions or "", "data_source_statement": p.data_source_statement or "", "compliance_statement": p.compliance_statement or "", "review_comment": p.review_comment or "", "reviewed_by": p.reviewed_by or "", "reviewed_at": p.reviewed_at, "created_at": p.created_at, "updated_at": p.updated_at}


def product_for_enterprise(product_id: str, user: User, db: Session) -> Product:
    if user.platform_role in {"super_admin", "platform_operator", "security_compliance"}:
        product = db.get(Product, product_id)
    else:
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
    stmt = select(Product)
    if user.platform_role not in {"super_admin", "platform_operator", "security_compliance"}:
        enterprise = first_enterprise(db, user)
        stmt = stmt.where(Product.enterprise_id == enterprise.id)
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


@app.get("/api/products/{product_id}/access-grants")
def access_grants(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if product.product_type != "saas":
        raise HTTPException(400, "只有 SaaS 应用支持企业用户访问授权")
    items = db.execute(select(ApplicationAccessGrant, User).join(User, User.id == ApplicationAccessGrant.user_id).where(ApplicationAccessGrant.product_id == product.id, ApplicationAccessGrant.status == "active")).all()
    return {"items": [{"id": grant.id, "user_id": member.id, "user_name": member.name, "user_email": member.email, "status": grant.status, "granted_by": grant.granted_by, "created_at": grant.created_at} for grant, member in items]}


@app.post("/api/products/{product_id}/access-grants")
def grant_application_access(product_id: str, body: ApplicationAccessBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if product.product_type != "saas":
        raise HTTPException(400, "只有 SaaS 应用支持企业用户访问授权")
    enterprise = first_enterprise(db, user)
    require_enterprise_admin(db, user, enterprise.id)
    member = db.scalar(select(Membership).where(Membership.user_id == body.user_id, Membership.enterprise_id == enterprise.id, Membership.status == "active"))
    if not member:
        raise HTTPException(404, "目标用户不是当前企业成员")
    existing = db.scalar(select(ApplicationAccessGrant).where(ApplicationAccessGrant.product_id == product.id, ApplicationAccessGrant.user_id == body.user_id, ApplicationAccessGrant.status == "active"))
    if existing:
        return {"id": existing.id, "status": existing.status, "user_id": existing.user_id}
    grant = ApplicationAccessGrant(enterprise_id=enterprise.id, product_id=product.id, user_id=body.user_id, granted_by=user.email or user.phone or user.id)
    db.add(grant)
    audit(db, user.email or user.phone or user.id, "grant_saas_access", "application_access_grant", grant.id, product.name)
    db.commit()
    db.refresh(grant)
    return {"id": grant.id, "status": grant.status, "user_id": grant.user_id, "product_id": grant.product_id}


@app.post("/api/products")
def create_product(body: ProductBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = first_enterprise(db, user)
    values = body.model_dump()
    versions = values.pop("versions", [])
    values["provider_name"] = values["provider_name"] or enterprise.name
    if not versions:
        versions = [{"version_code": values["version"], "description": values["description"], "price": values["price"], "status": "active"}]
    values["version"] = versions[0]["version_code"]
    values["price"] = versions[0]["price"]
    product = Product(enterprise_id=enterprise.id, **values)
    db.add(product)
    db.flush()
    for item in versions:
        db.add(ProductReleaseVersion(product_id=product.id, **item))
    audit(db, user.email, "create_product", "product", product.id, product.name)
    db.commit()
    db.refresh(product)
    return product_out(product)


@app.put("/api/products/{product_id}")
def update_product(product_id: str, body: ProductBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if product.status not in {"draft", "rejected", "security_unpublished"}:
        raise HTTPException(409, "只有草稿或被驳回的产品可以修改")
    values = body.model_dump()
    versions = values.pop("versions", [])
    values["provider_name"] = values["provider_name"] or first_enterprise(db, user).name
    if versions:
        values["version"] = versions[0]["version_code"]
        values["price"] = versions[0]["price"]
        product.versions.clear()
        for item in versions:
            product.versions.append(ProductReleaseVersion(**item))
    for name, value in values.items():
        setattr(product, name, value)
    product.review_comment = ""
    audit(db, user.email, "update_product", "product", product.id, product.name)
    db.commit()
    db.refresh(product)
    return product_out(product)


@app.get("/api/products/{product_id}/versions")
def list_product_versions(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    return {"items": product_out(product)["versions"]}


@app.post("/api/products/{product_id}/versions")
def add_product_version(product_id: str, body: ProductVersionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if product.status not in {"draft", "rejected", "security_unpublished"}:
        raise HTTPException(409, "只有草稿或被驳回的产品可以增加版本")
    if db.scalar(select(ProductReleaseVersion).where(ProductReleaseVersion.product_id == product.id, ProductReleaseVersion.version_code == body.version_code)):
        raise HTTPException(409, "该版本号已存在")
    version = ProductReleaseVersion(product_id=product.id, **body.model_dump())
    db.add(version)
    db.commit()
    db.refresh(version)
    return {"id": version.id, "product_id": version.product_id, "version_code": version.version_code, "description": version.description, "price": float(version.price or 0), "rate_limit_per_minute": version.rate_limit_per_minute, "daily_quota": version.daily_quota, "monthly_quota": version.monthly_quota, "status": version.status}


@app.put("/api/products/{product_id}/versions/{version_id}")
def update_product_version(product_id: str, version_id: str, body: ProductVersionUpdateBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if product.status not in {"draft", "rejected", "security_unpublished"}:
        raise HTTPException(409, "只有草稿或被驳回的产品可以编辑版本")
    version = db.scalar(select(ProductReleaseVersion).where(ProductReleaseVersion.id == version_id, ProductReleaseVersion.product_id == product.id))
    if not version:
        raise HTTPException(404, "产品版本不存在")
    duplicate = db.scalar(select(ProductReleaseVersion).where(ProductReleaseVersion.product_id == product.id, ProductReleaseVersion.version_code == body.version_code, ProductReleaseVersion.id != version.id))
    if duplicate:
        raise HTTPException(409, "该版本号已存在")
    for key, value in body.model_dump().items():
        setattr(version, key, value)
    if product.versions and version.id == product.versions[0].id:
        product.version = version.version_code
        product.price = version.price
    db.commit()
    db.refresh(version)
    return {"id": version.id, "product_id": version.product_id, "version_code": version.version_code, "description": version.description, "price": float(version.price or 0), "rate_limit_per_minute": version.rate_limit_per_minute, "daily_quota": version.daily_quota, "monthly_quota": version.monthly_quota, "status": version.status}


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
    if product.status not in {"draft", "rejected", "security_unpublished"}:
        raise HTTPException(409, "当前产品状态不允许提交审核")
    product.status = "pending_review"
    product.review_comment = ""
    audit(db, user.email, "submit_product_review", "product", product.id)
    db.commit()
    return product_out(product)


def platform_oauth_token_url() -> str:
    return os.getenv("PLATFORM_OAUTH_TOKEN_URL", "http://market-api:8000/oauth/token")


def platform_oauth_issuer() -> str:
    return os.getenv("PLATFORM_OAUTH_ISSUER", "http://market-api:8000")


def ensure_oauth_client(db: Session, product: Product, scope: str = "resource.invoke") -> OAuthClient:
    client = db.scalar(select(OAuthClient).where(OAuthClient.product_id == product.id))
    if client:
        client.status = "active"
        existing_saas = db.scalar(select(SaaSIntegrationConfig).where(SaaSIntegrationConfig.product_id == product.id))
        if existing_saas:
            existing_saas.token_url = platform_oauth_token_url()
            existing_saas.auth_mode = "oauth2"
        return client
    existing_saas = db.scalar(select(SaaSIntegrationConfig).where(SaaSIntegrationConfig.product_id == product.id))
    client_id = existing_saas.client_id if existing_saas and existing_saas.client_id else "market_" + secrets.token_urlsafe(12)
    client_secret = existing_saas.client_secret if existing_saas and existing_saas.client_secret else secrets.token_urlsafe(32)
    client = OAuthClient(product_id=product.id, client_id=client_id, client_secret=client_secret, scope=scope, status="active")
    db.add(client)
    db.flush()
    if existing_saas:
        existing_saas.client_id = client_id
        existing_saas.client_secret = client_secret
        existing_saas.token_url = platform_oauth_token_url()
        existing_saas.auth_mode = "oauth2"
    return client


@app.post("/oauth/token")
async def platform_oauth_token(request: Request, db: Session = Depends(db_session)):
    raw = (await request.body()).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    form = {key: values[-1] for key, values in parsed.items()}
    grant_type = form.get("grant_type") or request.query_params.get("grant_type", "") or "client_credentials"
    client_id = form.get("client_id") or request.query_params.get("client_id", "")
    client_secret = form.get("client_secret") or request.query_params.get("client_secret", "")
    scope = form.get("scope") or request.query_params.get("scope", "")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("basic "):
        try:
            import base64
            decoded = base64.b64decode(authorization[6:]).decode()
            client_id, client_secret = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError, base64.binascii.Error):
            raise HTTPException(401, "invalid_client")
    if grant_type != "client_credentials" or not client_id or not client_secret:
        raise HTTPException(400, "grant_type 必须为 client_credentials，且必须提供客户端凭据")
    client = db.scalar(select(OAuthClient).where(OAuthClient.client_id == client_id, OAuthClient.status == "active"))
    if not client or not hmac.compare_digest(client.client_secret, client_secret):
        raise HTTPException(401, "invalid_client")
    requested_scope = scope.strip()
    allowed = set(client.scope.split())
    scopes = requested_scope.split() if requested_scope else sorted(allowed)
    if not set(scopes).issubset(allowed):
        raise HTTPException(400, "invalid_scope")
    issued = int(now().timestamp())
    expires = issued + 3600
    token = jwt.encode({"iss": platform_oauth_issuer(), "sub": client.client_id, "client_id": client.client_id, "product_id": client.product_id, "scope": " ".join(scopes), "aud": "market-resource", "iat": issued, "exp": expires}, JWT_SECRET, algorithm="HS256")
    return {"access_token": token, "token_type": "Bearer", "expires_in": 3600, "scope": " ".join(scopes)}


@app.post("/oauth/introspect")
async def platform_oauth_introspect(request: Request):
    raw = (await request.body()).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    token = (parsed.get("token", [""])[-1] if parsed else "") or request.query_params.get("token", "")
    if not token:
        return {"active": False}
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience="market-resource", issuer=platform_oauth_issuer())
        return {"active": True, **claims}
    except jwt.PyJWTError:
        return {"active": False}


@app.post("/api/products/{product_id}/review")
def review_product(product_id: str, body: ProductReviewBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if user.platform_role not in {"super_admin", "platform_operator"}:
        require_enterprise_admin(db, user, product.enterprise_id)
    if product.status != "pending_review":
        raise HTTPException(409, "只有待审核产品可以审核")
    if body.decision not in {"approve", "reject"}:
        raise HTTPException(400, "审核结论必须是 approve 或 reject")
    if body.decision == "reject" and not body.comment.strip():
        raise HTTPException(400, "驳回时必须填写审核意见")
    if body.decision == "approve" and product.product_type == "dataset":
        scan = run_product_security_scan(product, db, user.email or user.phone or user.id)
        product.status = "security_review"
        product.review_comment = "业务审核通过，等待安全合规人员确认"
        product.reviewed_by = user.email or user.phone or user.id
        product.reviewed_at = now()
        audit(db, user.email or user.phone or user.id, "enter_product_security_review", "product", product.id, scan.id)
        db.commit()
        return product_out(product) | {"security_report": security_scan_out(scan)}
    product.status = "published" if body.decision == "approve" else "rejected"
    product.review_comment = body.comment.strip()
    product.reviewed_by = user.email
    product.reviewed_at = now()
    if body.decision == "approve" and product.product_type == "saas":
        config = db.scalar(select(SaaSIntegrationConfig).where(SaaSIntegrationConfig.product_id == product.id))
        if not config:
            client_id = "market_" + secrets.token_urlsafe(12)
            client_secret = secrets.token_urlsafe(32)
            mock_base_url = os.getenv("MOCK_SAAS_BASE_URL", "http://market-mock-saas:8200").rstrip("/")
            config = SaaSIntegrationConfig(product_id=product.id, base_url=mock_base_url, operation_path="/isv.php", token_url=mock_base_url + "/oauth/token", client_id=client_id, client_secret=client_secret, auth_mode="oauth2", status="active", updated_by=user.email or user.phone or user.id)
            db.add(config)
    if body.decision == "approve" and product.product_type in {"api", "model", "saas"}:
        client = ensure_oauth_client(db, product)
        route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id))
        if route:
            route.upstream_client_id = client.client_id
            route.upstream_client_secret = client.client_secret
        audit(db, user.email or user.phone or user.id, "ensure_platform_oauth_client", "oauth_client", client.id, product.product_type)
    if body.decision == "approve" and product.product_type in {"api", "model"}:
        route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id))
        if route:
            gateway_auto_publish(route, product, db, user.email or user.phone or user.id)
        else:
            audit(db, user.email or user.phone or user.id, "gateway_route_pending_config", "product", product.id, "产品已审核，但尚未保存网关配置")
    audit(db, user.email, "approve_product" if body.decision == "approve" else "reject_product", "product", product.id, product.review_comment)
    db.commit()
    return product_out(product)


@app.get("/api/products/{product_id}/security-report")
def product_security_report(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_security_operator(user)
    product = db.get(Product, product_id)
    if not product or product.product_type != "dataset":
        raise HTTPException(404, "数据集产品不存在")
    scan = db.scalar(select(ProductSecurityScan).where(ProductSecurityScan.product_id == product.id).order_by(ProductSecurityScan.scanned_at.desc()))
    if not scan:
        raise HTTPException(404, "该数据集尚未生成安全审核报告")
    return {"product": product_out(product), "security_report": security_scan_out(scan)}


@app.post("/api/products/{product_id}/security-review")
def review_product_security(product_id: str, body: ProductSecurityReviewBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_security_operator(user)
    product = db.get(Product, product_id)
    if not product or product.product_type != "dataset":
        raise HTTPException(404, "数据集产品不存在")
    if product.status != "security_review":
        raise HTTPException(409, "当前数据集不在安全审核环节")
    if body.decision not in {"approve", "reject"}:
        raise HTTPException(400, "安全审核结论必须是 approve 或 reject")
    if body.decision == "reject" and not body.comment.strip():
        raise HTTPException(400, "安全审核驳回时必须填写原因")
    scan = db.scalar(select(ProductSecurityScan).where(ProductSecurityScan.product_id == product.id).order_by(ProductSecurityScan.scanned_at.desc()))
    if not scan:
        raise HTTPException(409, "安全审核报告不存在")
    actor = user.email or user.phone or user.id
    scan.status = "approved" if body.decision == "approve" else "rejected"
    scan.reviewed_by = actor
    scan.reviewed_at = now()
    scan.review_comment = body.comment.strip()
    product.status = "published" if body.decision == "approve" else "rejected"
    product.review_comment = body.comment.strip() or "安全审核通过"
    product.reviewed_by = actor
    product.reviewed_at = now()
    audit(db, actor, "approve_product_security" if body.decision == "approve" else "reject_product_security", "product", product.id, body.comment.strip())
    db.commit()
    return product_out(product) | {"security_report": security_scan_out(scan)}


def product_security_policy_violations(product: Product) -> list[str]:
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
    return [label for label, value in required.items() if not value]


def security_scan_out(scan: ProductSecurityScan | None) -> dict[str, Any] | None:
    if not scan:
        return None
    try:
        report = json.loads(scan.report_json or "{}")
    except json.JSONDecodeError:
        report = {"raw": scan.report_json}
    return {"id": scan.id, "product_id": scan.product_id, "engine": scan.engine, "status": scan.status, "findings_count": scan.findings_count, "high_risk_count": scan.high_risk_count, "scanned_at": scan.scanned_at, "reviewed_by": scan.reviewed_by, "reviewed_at": scan.reviewed_at, "review_comment": scan.review_comment, "report": report}


def presidio_analyze(text_value: str) -> tuple[list[dict[str, Any]], str]:
    """Call the in-cluster Presidio Analyzer; no data leaves Kubernetes."""
    if not text_value.strip():
        return [], "empty"
    url = os.getenv("PRESIDIO_ANALYZER_URL", "http://market-presidio-analyzer:3000/analyze")
    try:
        response = httpx.post(url, json={"text": text_value[:200000], "language": "en"}, timeout=30)
        response.raise_for_status()
        items = response.json()
        return [{"entity": item.get("entity_type", "UNKNOWN"), "score": item.get("score", 0), "start": item.get("start"), "end": item.get("end"), "source": "presidio"} for item in items if isinstance(item, dict)], "available"
    except (httpx.HTTPError, ValueError) as exc:
        return [{"entity": "PRESIDIO_UNAVAILABLE", "severity": "medium", "message": str(exc)[:240], "source": "platform"}], "unavailable"


def market_sensitive_patterns(text_value: str) -> list[dict[str, Any]]:
    patterns = [
        ("CHINA_ID_NUMBER", r"(?<!\d)\d{17}[0-9Xx](?!\d)", "high", "疑似身份证号码"),
        ("PHONE_NUMBER", r"(?<!\d)1[3-9]\d{9}(?!\d)", "medium", "疑似手机号码"),
        ("EMAIL_ADDRESS", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "medium", "疑似邮箱地址"),
        ("BANK_ACCOUNT", r"(?<!\d)\d{16,19}(?!\d)", "high", "疑似银行卡或长数字敏感标识"),
    ]
    findings = []
    for entity, pattern, severity, message in patterns:
        matches = list(re.finditer(pattern, text_value))
        if matches:
            findings.append({"entity": entity, "severity": severity, "count": len(matches), "message": message, "source": "market-policy"})
    return findings


def read_product_sample(file_item: FileObject) -> str:
    if not MINIO_ENDPOINT or file_item.size <= 0:
        return ""
    if not (file_item.content_type.startswith("text/") or file_item.content_type in {"application/json", "application/csv", "application/xml"} or file_item.original_name.lower().endswith((".csv", ".json", ".txt", ".xml"))):
        return ""
    client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=False)
    response = client.get_object(MINIO_BUCKET, file_item.object_name)
    try:
        return response.read(2_000_000).decode("utf-8", errors="ignore")
    finally:
        response.close()
        response.release_conn()


def run_product_security_scan(product: Product, db: Session, actor: str) -> ProductSecurityScan:
    metadata_text = json.dumps({"name": product.name, "description": product.description, "usage_scenarios": product.usage_scenarios, "authorization_conditions": product.authorization_conditions, "data_source_statement": product.data_source_statement, "compliance_statement": product.compliance_statement}, ensure_ascii=False)
    text_parts = [metadata_text]
    files = db.scalars(select(FileObject).where(FileObject.product_id == product.id, FileObject.status != "deleted")).all()
    file_reports = []
    for item in files:
        try:
            sample = read_product_sample(item)
            if sample:
                text_parts.append(sample)
            file_reports.append({"file_id": item.id, "name": item.original_name, "sample_scanned": bool(sample), "size": item.size})
        except Exception as exc:
            file_reports.append({"file_id": item.id, "name": item.original_name, "sample_scanned": False, "error": str(exc)[:240]})
    combined = "\n".join(text_parts)
    presidio_findings, presidio_status = presidio_analyze(combined)
    findings = market_sensitive_patterns(combined) + presidio_findings
    metadata_violations = product_security_policy_violations(product)
    if metadata_violations:
        findings.append({"entity": "PRODUCT_METADATA", "severity": "high", "message": "登记元数据缺少：" + "、".join(metadata_violations), "source": "market-policy"})
    if not files:
        findings.append({"entity": "NO_SAMPLE_FILE", "severity": "medium", "message": "未发现可供自动扫描的数据集样本或数据文件，需安全审核人员人工确认", "source": "market-policy"})
    high_risk = sum(1 for item in findings if item.get("severity") == "high" or item.get("entity") in {"CHINA_ID_NUMBER", "BANK_ACCOUNT"})
    report = {"engine": "Presidio Analyzer + market-policy", "presidio_status": presidio_status, "policy_version": "2026.10", "product_id": product.id, "files": file_reports, "findings": findings, "recommendation": "必须由安全合规人员结合原始数据授权、分类分级和脱敏证明进行人工确认。"}
    scan = ProductSecurityScan(product_id=product.id, engine="presidio+market-policy", status="manual_review_high_risk" if high_risk else "manual_review", report_json=json.dumps(report, ensure_ascii=False), findings_count=len(findings), high_risk_count=high_risk, scanned_at=now())
    db.add(scan)
    db.flush()
    audit(db, actor, "run_product_security_scan", "product_security_scan", scan.id, f"findings={len(findings)}, high_risk={high_risk}, presidio={presidio_status}")
    return scan


def remove_product_delivery(product: Product, db: Session, actor: str) -> None:
    """Disable all delivery entry points before a product becomes unavailable."""
    route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id))
    if route:
        if apisix_enabled():
            try:
                apisix_admin_request("DELETE", f"/routes/{route.route_key}")
            except RuntimeError as exc:
                audit(db, actor, "unpublish_apisix_route_failed", "api_gateway_route", route.id, str(exc)[:500])
        route.status = "disabled"
        route.health_message = "产品已下架，网关路由已停用"
    client = db.scalar(select(OAuthClient).where(OAuthClient.product_id == product.id))
    if client:
        client.status = "inactive"


def unpublish_product(product: Product, db: Session, actor: str, reason: str, source: str = "manual") -> None:
    if product.status != "published":
        raise HTTPException(409, "只有已发布产品可以下架")
    # Manual withdrawal only blocks new purchases. Existing paid orders and
    # subscriptions keep their route, credentials and delivery state.
    product.status = "draft" if source == "manual" else "security_unpublished"
    product.review_comment = reason
    product.reviewed_by = actor
    product.reviewed_at = now()
    if source == "security_policy":
        remove_product_delivery(product, db, actor)
    audit(db, actor, "auto_unpublish_product" if source == "security_policy" else "unpublish_product", "product", product.id, reason)


@app.post("/api/products/{product_id}/unpublish")
def unpublish_product_endpoint(product_id: str, body: ProductUnpublishBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if user.platform_role not in {"super_admin", "platform_operator"}:
        require_enterprise_admin(db, user, product.enterprise_id)
    actor = user.email or user.phone or user.id
    unpublish_product(product, db, actor, body.reason)
    db.commit()
    return product_out(product)


@app.post("/api/products/{product_id}/security-check")
def security_check_product(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_security_operator(user)
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "产品不存在")
    violations = product_security_policy_violations(product)
    unpublished = False
    actor = user.email or user.phone or user.id
    if product.status == "published" and violations:
        unpublish_product(product, db, "系统安全策略自动下架：" + "、".join(violations), actor="system-security-policy", source="security_policy")
        unpublished = True
    audit(db, actor, "run_product_security_check", "product", product.id, "通过" if not violations else "；".join(violations))
    db.commit()
    return {"product": product_out(product), "passed": not violations, "unpublished": unpublished, "violations": violations}


@app.post("/api/security/product-policy-check")
def security_check_all_products(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_security_operator(user)
    checked = 0
    unpublished = 0
    results = []
    for product in db.scalars(select(Product).where(Product.status == "published")).all():
        checked += 1
        violations = product_security_policy_violations(product)
        if violations:
            unpublish_product(product, db, "系统安全策略自动下架：" + "、".join(violations), actor="system-security-policy", source="security_policy")
            unpublished += 1
            results.append({"id": product.id, "name": product.name, "status": "security_unpublished", "violations": violations})
    audit(db, user.email or user.phone or user.id, "run_product_security_policy_scan", "product", "", f"检查{checked}个，下架{unpublished}个")
    db.commit()
    return {"checked": checked, "unpublished": unpublished, "items": results}


@app.get("/api/products/{product_id}/oauth-credentials-download")
def download_oauth_credentials(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    require_enterprise_admin(db, user, product.enterprise_id)
    if product.product_type not in {"api", "model", "saas"} or product.status not in {"published", "draft"}:
        raise HTTPException(409, "只有已审核通过的 API 或 SaaS 产品可以下载接入凭据")
    client = db.scalar(select(OAuthClient).where(OAuthClient.product_id == product.id, OAuthClient.status == "active"))
    config = db.scalar(select(SaaSIntegrationConfig).where(SaaSIntegrationConfig.product_id == product.id, SaaSIntegrationConfig.status == "active"))
    if not client:
        raise HTTPException(404, "平台 OAuth 客户端不存在")
    content = "\n".join([
        "平台统一 OAuth2 接入配置",
        f"产品名称: {product.name}",
        f"产品 ID: {product.id}",
        f"client_id: {client.client_id}",
        f"client_secret: {client.client_secret}",
        f"token_url: {platform_oauth_token_url()}",
        f"introspection_url: {platform_oauth_issuer()}/oauth/introspect",
        f"issuer: {platform_oauth_issuer()}",
        "audience: market-resource",
        f"scope: {client.scope}",
        f"business_url: {config.base_url if config else ''}",
        "认证模式: OAuth2 client_credentials",
        "请妥善保存 client_secret，不要提交到前端代码或公开代码仓库。资源服务应校验平台签发的 Token。",
        "",
    ])
    filename = f"{product.name}-platform-oauth-credentials.txt"
    encoded_filename = quote(filename)
    disposition = f'attachment; filename="platform-oauth-credentials.txt"; filename*=UTF-8\'\'{encoded_filename}'
    return Response(content=content, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": disposition})


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


def gateway_route_out(route: ApiGatewayRoute, product: Product | None = None) -> dict[str, Any]:
    return {"id": route.id, "product_id": route.product_id, "product_name": product.name if product else "", "route_key": route.route_key, "gateway_base_path": f"/gateway/{route.route_key}", "upstream_url": route.upstream_url, "version": route.version, "auth_mode": route.auth_mode, "upstream_auth_mode": route.upstream_auth_mode, "upstream_scope": route.upstream_scope, "upstream_oauth_configured": bool(route.upstream_client_id and route.upstream_client_secret), "rate_limit_per_minute": route.rate_limit_per_minute, "daily_quota": route.daily_quota, "monthly_quota": route.monthly_quota, "timeout_ms": route.timeout_ms, "strip_prefix": route.strip_prefix, "health_path": route.health_path, "health_method": route.health_method, "health_message": route.health_message, "status": route.status, "apisix_enabled": os.getenv("APISIX_ENABLED", "false").lower() == "true", "created_at": route.created_at, "updated_at": route.updated_at}


@app.get("/api/gateway/routes")
def gateway_routes(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    rows = db.execute(select(ApiGatewayRoute, Product).join(Product, Product.id == ApiGatewayRoute.product_id).order_by(ApiGatewayRoute.updated_at.desc())).all()
    return {"items": [gateway_route_out(route, product) for route, product in rows]}


@app.get("/api/gateway/overview")
def gateway_overview(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    routes = db.scalars(select(ApiGatewayRoute)).all()
    usage = db.scalars(select(ApiUsage).order_by(ApiUsage.created_at.desc()).limit(1000)).all()
    total = len(usage)
    return {"summary": {"routes": len(routes), "active_routes": sum(1 for x in routes if x.status == "active"), "requests": total, "success": sum(1 for x in usage if x.status_code < 400), "errors": sum(1 for x in usage if x.status_code >= 400), "avg_latency_ms": round(sum(x.latency_ms for x in usage) / total, 1) if total else 0}, "apisix_enabled": apisix_enabled(), "metrics_url": "http://market-apisix-metrics:9091/apisix/prometheus/metrics"}


@app.get("/api/gateway/alerts")
def gateway_alerts(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    failed = db.scalars(select(GatewayPublishRecord).where(GatewayPublishRecord.status == "failed").order_by(GatewayPublishRecord.created_at.desc()).limit(20)).all()
    return {"items": [{"id": x.id, "severity": "error", "title": "APISIX 路由发布失败", "message": x.error_message, "created_at": x.created_at, "status": "open"} for x in failed]}


def apisix_enabled() -> bool:
    return os.getenv("APISIX_ENABLED", "false").lower() == "true"


def apisix_native_auth() -> bool:
    return os.getenv("APISIX_NATIVE_AUTH", "false").lower() == "true"


def apisix_native_upstream() -> bool:
    return os.getenv("APISIX_NATIVE_UPSTREAM", "false").lower() == "true"


def apisix_admin_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    base_url = os.getenv("APISIX_ADMIN_URL", "http://market-apisix-admin:9180/apisix/admin").rstrip("/")
    admin_key = os.getenv("APISIX_ADMIN_KEY", "")
    if not admin_key:
        raise RuntimeError("APISIX_ADMIN_KEY 未配置")
    headers = {"X-API-KEY": admin_key, "Content-Type": "application/json"}
    try:
        response = httpx.request(method, f"{base_url}{path}", json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json() if response.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"APISIX Admin API 调用失败：{exc}") from exc


def apisix_route_payload(route: ApiGatewayRoute, product: Product | None = None, db: Session | None = None) -> dict[str, Any]:
    """Build either the compatibility route or the native APISIX data-plane route."""
    compat_upstream = os.getenv("APISIX_COMPAT_UPSTREAM", "http://market-gateway:8100").rstrip("/")
    version = db.scalar(select(ProductReleaseVersion).where(ProductReleaseVersion.product_id == product.id, ProductReleaseVersion.version_code == route.version, ProductReleaseVersion.status == "active")) if db and product else None
    rate_limit = version.rate_limit_per_minute if version else route.rate_limit_per_minute
    daily_quota = version.daily_quota if version else route.daily_quota
    monthly_quota = version.monthly_quota if version else route.monthly_quota
    use_native_upstream = apisix_native_upstream()
    target_upstream = route.upstream_url.rstrip("/") if use_native_upstream else compat_upstream
    plugins = {
        "proxy-rewrite": {"regex_uri": [f"^/gateway/{route.route_key}(.*)", r"$1"] if use_native_upstream else [f"^/gateway/{route.route_key}(.*)", r"/gateway/" + route.route_key + r"$1"]},
        "limit-count": {"count": rate_limit, "time_window": 60, "rejected_code": 429, "rejected_msg": '{"code":"RATE_LIMIT_EXCEEDED","message":"超过 API 每分钟调用频率限制"}', "key": "consumer_name" if apisix_native_auth() else "http_x_api_key", "key_type": "var", "policy": "redis", "redis_host": "market-redis", "redis_port": 6379, "redis_database": 2},
    }
    if apisix_native_auth():
        plugins["key-auth"] = {"header": "X-API-Key", "query": "api_key"}
    if apisix_native_upstream():
        plugins["market-gateway-oauth"] = {
            "product_id": route.product_id,
            "token_url": platform_oauth_token_url(),
            "client_id": route.upstream_client_id,
            "client_secret": route.upstream_client_secret,
            "scope": route.upstream_scope,
        }
    return {
        "name": f"market-{route.route_key}",
        "uri": f"/gateway/{route.route_key}/*",
        "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        "upstream": {"type": "roundrobin", "nodes": {target_upstream.replace("http://", "").replace("https://", ""): 1}, "scheme": "https" if target_upstream.startswith("https://") else "http"},
        "plugins": plugins,
        "labels": {"market_product_id": route.product_id, "market_version": route.version, "market_rate_limit_per_minute": str(rate_limit), "market_daily_quota": str(daily_quota), "market_monthly_quota": str(monthly_quota), "market_managed": "true", "market_data_plane": "native" if use_native_upstream else "compatibility"},
    }


def sync_apisix_consumer(credential: ApiCredential, raw_key: str | None = None, route: ApiGatewayRoute | None = None) -> bool:
    """Synchronize a credential to APISIX key-auth without exposing Admin API to clients."""
    if not apisix_enabled():
        return True
    if not credential.apisix_consumer_name:
        credential.apisix_consumer_name = f"market-consumer-{credential.id}"
    if not raw_key:
        raise RuntimeError("历史 API 凭据没有可用于 APISIX 同步的明文密钥，请重新生成")
    route_key = route.route_key if route else "unknown"
    daily_quota = credential.daily_quota if credential.daily_quota is not None else (route.daily_quota if route else 0)
    monthly_quota = credential.monthly_quota if credential.monthly_quota is not None else (route.monthly_quota if route else 0)
    apisix_admin_request("PUT", f"/consumers/{credential.apisix_consumer_name}", {"username": credential.apisix_consumer_name, "plugins": {"key-auth": {"key": raw_key}, "market-gateway-quota": {"route_key": route_key, "daily_quota": daily_quota or 0, "monthly_quota": monthly_quota or 0}}})
    return True


def remove_apisix_consumer(credential: ApiCredential) -> None:
    if apisix_enabled() and credential.apisix_consumer_name:
        apisix_admin_request("DELETE", f"/consumers/{credential.apisix_consumer_name}")


def publish_apisix_route(route: ApiGatewayRoute, product: Product, db: Session, actor: str) -> bool:
    if not apisix_enabled():
        return True
    previous = db.scalar(select(GatewayConfigRevision).where(GatewayConfigRevision.route_id == route.id).order_by(GatewayConfigRevision.revision.desc()))
    payload = apisix_route_payload(route, product, db)
    revision = GatewayConfigRevision(route_id=route.id, product_id=product.id, revision=(previous.revision + 1 if previous else 1), config_json=json.dumps(payload, ensure_ascii=False), status="publishing", created_by=actor)
    db.add(revision)
    db.flush()
    record = GatewayPublishRecord(route_id=route.id, revision_id=revision.id, target="apisix", status="executing", created_by=actor)
    db.add(record)
    db.flush()
    try:
        response = apisix_admin_request("PUT", f"/routes/{route.route_key}", payload)
        revision.status = "active"
        record.status = "succeeded"
        record.response_json = json.dumps(response, ensure_ascii=False)[:10000]
        record.completed_at = now()
        audit(db, actor, "publish_apisix_route", "gateway_config_revision", revision.id, route.route_key)
        return True
    except RuntimeError as exc:
        revision.status = "failed"
        revision.error_message = str(exc)[:1000]
        record.status = "failed"
        record.error_message = str(exc)[:1000]
        record.completed_at = now()
        audit(db, actor, "publish_apisix_route_failed", "gateway_config_revision", revision.id, str(exc)[:500])
        return False


def gateway_auto_publish(route: ApiGatewayRoute, product: Product, db: Session, actor: str) -> bool:
    """Only expose a route after its configured upstream passes a health check."""
    route.status = "pending_health"
    route.health_message = "正在执行审核后的后端健康检查"
    health_path = route.health_path or "/health"
    if not health_path.startswith("/"):
        health_path = "/" + health_path
    health_url = route.upstream_url.rstrip("/") + health_path
    try:
        with httpx.Client(follow_redirects=False, timeout=max(route.timeout_ms / 1000, 1.0)) as client:
            response = client.request(route.health_method or "GET", health_url)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"健康检查返回 HTTP {response.status_code}")
        route.status = "active"
        route.health_message = f"健康检查通过（HTTP {response.status_code}）"
        if not publish_apisix_route(route, product, db, actor):
            route.status = "publish_failed"
            route.health_message = "后端健康检查通过，但 APISIX 路由发布失败"
            return False
        audit(db, actor, "auto_publish_gateway_route", "api_gateway_route", route.id, health_url)
        return True
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        route.status = "publish_failed"
        route.health_message = str(exc)[:500]
        audit(db, actor, "auto_publish_gateway_route_failed", "api_gateway_route", route.id, f"{health_url}: {route.health_message}")
        return False


def gateway_product(product_id: str, db: Session) -> Product:
    product = db.get(Product, product_id)
    if not product or (product.product_type not in {"api", "model"} and product.delivery_method not in {"api", "model_api"}):
        raise HTTPException(400, "只有 API 服务或模型 API 产品可以配置 API 网关")
    return product


def api_order_context(order_id: str, user: User, db: Session) -> tuple[Order, Product, ApiGatewayRoute, str]:
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "订单不存在")
    if order.payment_status != "paid":
        raise HTTPException(409, "订单支付完成后才可以管理 API 凭据")
    if user.platform_role not in {"super_admin", "platform_operator"}:
        if order.buyer_user_id == user.id:
            if user.verified_status != "verified":
                raise HTTPException(403, "完成个人实名认证后才可以管理 API 凭据")
        else:
            require_enterprise_admin(db, user, order.buyer_enterprise_id)
    product = gateway_product(order.product_id, db)
    if product.status not in {"published", "draft"}:
        raise HTTPException(409, "API 产品已被安全策略下架")
    route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id, ApiGatewayRoute.status == "active"))
    if not route:
        raise HTTPException(409, "API 网关路由尚未启用")
    return order, product, route, order.buyer_enterprise_id


def api_credential_out(item: ApiCredential, route: ApiGatewayRoute) -> dict[str, Any]:
    return {"id": item.id, "name": item.name, "key_prefix": item.key_prefix, "status": item.status, "product_version_id": item.product_version_id, "rate_limit_per_minute": item.rate_limit_per_minute or route.rate_limit_per_minute, "daily_quota": item.daily_quota or route.daily_quota, "monthly_quota": item.monthly_quota or route.monthly_quota, "expires_at": item.expires_at, "last_used_at": item.last_used_at, "created_at": item.created_at}


@app.get("/api/products/{product_id}/gateway-config")
def get_gateway_config(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    if user.platform_role not in {"super_admin", "platform_operator"}:
        membership = current_membership(db, user, product.enterprise_id)
        if membership.role not in {"super_admin", "enterprise_admin"}:
            raise HTTPException(403, "只有产品提供企业管理员或平台管理员可以查看网关配置")
    route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id))
    return {"item": gateway_route_out(route, product) if route else None}


def gateway_operator_allowed(product: Product, user: User, db: Session, action: str = "查看") -> None:
    if user.platform_role in {"super_admin", "platform_operator"}:
        return
    membership = current_membership(db, user, product.enterprise_id)
    if not membership or membership.role not in {"super_admin", "enterprise_admin"}:
        raise HTTPException(403, f"只有平台管理员或产品企业管理员可以{action}网关配置")


@app.get("/api/products/{product_id}/gateway-revisions")
def gateway_revisions(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    gateway_operator_allowed(product, user, db)
    rows = db.scalars(select(GatewayConfigRevision).where(GatewayConfigRevision.product_id == product.id).order_by(GatewayConfigRevision.revision.desc())).all()
    return {"items": [{"id": x.id, "revision": x.revision, "status": x.status, "error_message": x.error_message, "created_by": x.created_by, "created_at": x.created_at} for x in rows]}


@app.post("/api/products/{product_id}/gateway-config/validate")
def validate_gateway_config(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    gateway_operator_allowed(product, user, db, "校验")
    route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id))
    if not route:
        raise HTTPException(404, "请先保存 API 网关配置")
    parsed = urlparse(route.upstream_url)
    checks = {"upstream_url": parsed.scheme in {"http", "https"} and bool(parsed.netloc), "route_key": bool(route.route_key), "version": bool(route.version), "health_path": bool(route.health_path), "policy": route.rate_limit_per_minute > 0 and route.daily_quota > 0}
    return {"valid": all(checks.values()), "checks": checks, "route": gateway_route_out(route, product)}


@app.post("/api/products/{product_id}/gateway-config/rollback")
def rollback_gateway_config(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    gateway_operator_allowed(product, user, db, "回滚")
    route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id))
    revisions = db.scalars(select(GatewayConfigRevision).where(GatewayConfigRevision.route_id == route.id, GatewayConfigRevision.status == "active").order_by(GatewayConfigRevision.revision.desc())).all() if route else []
    if not route or len(revisions) < 2:
        raise HTTPException(409, "没有可回滚的稳定网关版本")
    target = json.loads(revisions[1].config_json)
    try:
        if apisix_enabled():
            apisix_admin_request("PUT", f"/routes/{route.route_key}", target)
        revision = GatewayConfigRevision(route_id=route.id, product_id=product.id, revision=revisions[0].revision + 1, config_json=json.dumps(target, ensure_ascii=False), status="active", created_by=user.email or user.phone or user.id)
        db.add(revision)
        db.flush()
        db.add(GatewayPublishRecord(route_id=route.id, revision_id=revision.id, target="apisix" if apisix_enabled() else "legacy", status="succeeded", response_json=json.dumps({"rollback_from": revisions[0].id, "rollback_to": revisions[1].id}), created_by=user.email or user.phone or user.id, completed_at=now()))
        audit(db, user.email, "rollback_gateway_route", "gateway_config_revision", revision.id, route.route_key)
        db.commit()
        return gateway_route_out(route, product) | {"rollback_revision": revision.revision}
    except (RuntimeError, ValueError, TypeError) as exc:
        db.rollback()
        raise HTTPException(502, f"网关回滚失败：{exc}") from exc


@app.put("/api/products/{product_id}/gateway-config")
def save_gateway_config(product_id: str, body: GatewayConfigBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    if user.platform_role not in {"super_admin", "platform_operator"}:
        membership = current_membership(db, user, product.enterprise_id)
        if membership.role not in {"super_admin", "enterprise_admin"}:
            raise HTTPException(403, "只有产品提供企业管理员或平台管理员可以配置 API 网关")
    parsed = urlparse(body.upstream_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(400, "后端服务地址必须是完整的 HTTP 或 HTTPS 地址")
    route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id))
    route_key = body.route_key.strip() or f"{product.id[:12]}-{body.version.replace('.', '-') }"
    existing_key = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.route_key == route_key, ApiGatewayRoute.product_id != product.id))
    if existing_key:
        raise HTTPException(409, "API 路由标识已被占用")
    values = body.model_dump()
    values.pop("route_key")
    if route:
        for key, value in values.items():
            setattr(route, key, value)
        route.route_key = route_key
        route.status = "draft"
        route.health_message = "网关配置已更新，等待产品审核或重新发布"
    else:
        route = ApiGatewayRoute(product_id=product.id, route_key=route_key, created_by=user.email or user.phone or user.id, **values)
        db.add(route)
    audit(db, user.email or user.phone or user.id, "save_gateway_config", "api_gateway_route", product.id, route_key)
    db.commit()
    db.refresh(route)
    return gateway_route_out(route, product)


@app.post("/api/products/{product_id}/gateway-config/publish")
def publish_gateway_config(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    if user.platform_role not in {"super_admin", "platform_operator"}:
        membership = current_membership(db, user, product.enterprise_id)
        if membership.role not in {"super_admin", "enterprise_admin"}:
            raise HTTPException(403, "只有产品提供企业管理员或平台管理员可以发布 API 网关路由")
    if product.status != "published":
        raise HTTPException(409, "产品必须先发布后才能启用 API 网关路由")
    route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id))
    if not route:
        raise HTTPException(404, "请先保存 API 网关配置")
    if not gateway_auto_publish(route, product, db, user.email or user.phone or user.id):
        db.commit()
        raise HTTPException(409, f"网关后端健康检查失败：{route.health_message}")
    db.commit()
    return gateway_route_out(route, product)


@app.post("/api/products/{product_id}/gateway-config/health-check")
def check_gateway_config(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    if user.platform_role not in {"super_admin", "platform_operator"}:
        membership = current_membership(db, user, product.enterprise_id)
        if membership.role not in {"super_admin", "enterprise_admin"}:
            raise HTTPException(403, "只有产品提供企业管理员或平台管理员可以检查 API 网关")
    route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id))
    if not route:
        raise HTTPException(404, "请先保存 API 网关配置")
    passed = gateway_auto_publish(route, product, db, user.email or user.phone or user.id)
    db.commit()
    if not passed:
        raise HTTPException(409, f"网关后端健康检查失败：{route.health_message}")
    return gateway_route_out(route, product)


@app.post("/api/products/{product_id}/gateway-credentials")
def create_gateway_credential(product_id: str, body: GatewayCredentialBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id, ApiGatewayRoute.status == "active"))
    if not route:
        raise HTTPException(409, "API 网关路由尚未启用")
    target_enterprise_id = body.enterprise_id or first_enterprise(db, user).id
    if user.platform_role not in {"super_admin", "platform_operator"}:
        require_enterprise_admin(db, user, target_enterprise_id)
    else:
        if not db.get(Enterprise, target_enterprise_id):
            raise HTTPException(404, "目标企业不存在")
    raw_key = "mk_" + secrets.token_urlsafe(30)
    credential = ApiCredential(route_id=route.id, enterprise_id=target_enterprise_id, name=body.name.strip() or "默认 API 凭证", key_prefix=raw_key[:12], key_hash=hashlib.sha256(raw_key.encode()).hexdigest(), rate_limit_per_minute=body.rate_limit_per_minute, daily_quota=body.daily_quota, monthly_quota=body.monthly_quota, expires_at=body.expires_at, created_by=user.email or user.phone or user.id)
    db.add(credential)
    db.flush()
    try:
        sync_apisix_consumer(credential, raw_key, route)
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(502, f"API 凭据同步 APISIX Consumer 失败：{exc}") from exc
    audit(db, user.email or user.phone or user.id, "create_api_credential", "api_credential", credential.id, route.route_key)
    db.commit()
    db.refresh(credential)
    return {"id": credential.id, "name": credential.name, "key_prefix": credential.key_prefix, "api_key": raw_key, "route_key": route.route_key, "enterprise_id": credential.enterprise_id, "expires_at": credential.expires_at, "warning": "API Key 仅在本次响应中返回，请妥善保存"}


@app.get("/api/orders/{order_id}/api-credentials")
def list_order_api_credentials(order_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    order, product, route, enterprise_id = api_order_context(order_id, user, db)
    items = db.scalars(select(ApiCredential).where(ApiCredential.route_id == route.id, ApiCredential.enterprise_id == enterprise_id).order_by(ApiCredential.created_at.desc())).all()
    return {"order_id": order.id, "product_id": product.id, "product_name": product.name, "route_key": route.route_key, "gateway_base_path": f"/gateway/{route.route_key}", "items": [api_credential_out(x, route) for x in items]}


@app.post("/api/orders/{order_id}/api-credentials")
def create_order_api_credential(order_id: str, body: GatewayCredentialBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    order, product, route, enterprise_id = api_order_context(order_id, user, db)
    raw_key = "mk_" + secrets.token_urlsafe(30)
    credential = ApiCredential(route_id=route.id, enterprise_id=enterprise_id, order_id=order.id, product_version_id=order.product_version_id, name=body.name.strip() or f"{product.name} API 凭据", key_prefix=raw_key[:12], key_hash=hashlib.sha256(raw_key.encode()).hexdigest(), rate_limit_per_minute=body.rate_limit_per_minute, daily_quota=body.daily_quota, monthly_quota=body.monthly_quota, expires_at=body.expires_at, created_by=user.email or user.phone or user.id)
    db.add(credential)
    db.flush()
    try:
        sync_apisix_consumer(credential, raw_key, route)
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(502, f"API 凭据同步 APISIX Consumer 失败：{exc}") from exc
    audit(db, user.email or user.phone or user.id, "create_order_api_credential", "api_credential", credential.id, order.order_no)
    db.commit()
    db.refresh(credential)
    return {**api_credential_out(credential, route), "api_key": raw_key, "route_key": route.route_key, "gateway_base_path": f"/gateway/{route.route_key}", "warning": "API Key 仅在本次响应中返回，请妥善保存"}


@app.post("/api/orders/{order_id}/api-credentials/{credential_id}/revoke")
def revoke_order_api_credential(order_id: str, credential_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    order, _, route, enterprise_id = api_order_context(order_id, user, db)
    credential = db.scalar(select(ApiCredential).where(ApiCredential.id == credential_id, ApiCredential.route_id == route.id, ApiCredential.enterprise_id == enterprise_id))
    if not credential:
        raise HTTPException(404, "API 凭据不存在")
    if credential.status != "active":
        raise HTTPException(409, "API 凭据已经停用")
    credential.status = "revoked"
    try:
        remove_apisix_consumer(credential)
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(502, f"API 凭据从 APISIX Consumer 停用失败：{exc}") from exc
    audit(db, user.email or user.phone or user.id, "revoke_order_api_credential", "api_credential", credential.id, order.order_no)
    db.commit()
    return {"id": credential.id, "status": credential.status}


@app.post("/api/orders/{order_id}/api-credentials/{credential_id}/regenerate")
def regenerate_order_api_credential(order_id: str, credential_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    order, product, route, enterprise_id = api_order_context(order_id, user, db)
    old = db.scalar(select(ApiCredential).where(ApiCredential.id == credential_id, ApiCredential.route_id == route.id, ApiCredential.enterprise_id == enterprise_id))
    if not old:
        raise HTTPException(404, "API 凭据不存在")
    old.status = "revoked"
    raw_key = "mk_" + secrets.token_urlsafe(30)
    credential = ApiCredential(route_id=route.id, enterprise_id=enterprise_id, order_id=order.id, product_version_id=old.product_version_id, name=old.name, key_prefix=raw_key[:12], key_hash=hashlib.sha256(raw_key.encode()).hexdigest(), rate_limit_per_minute=old.rate_limit_per_minute, daily_quota=old.daily_quota, monthly_quota=old.monthly_quota, expires_at=old.expires_at, created_by=user.email or user.phone or user.id)
    db.add(credential)
    db.flush()
    try:
        remove_apisix_consumer(old)
        sync_apisix_consumer(credential, raw_key, route)
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(502, f"API 凭据重生成同步 APISIX Consumer 失败：{exc}") from exc
    audit(db, user.email or user.phone or user.id, "regenerate_order_api_credential", "api_credential", credential.id, f"{order.order_no} from={old.id}")
    db.commit()
    db.refresh(credential)
    return {**api_credential_out(credential, route), "api_key": raw_key, "route_key": route.route_key, "gateway_base_path": f"/gateway/{route.route_key}", "warning": f"旧凭据 {old.key_prefix} 已停用，新 API Key 仅在本次响应中返回，请妥善保存"}


@app.get("/api/products/{product_id}/gateway-credentials")
def list_gateway_credentials(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id))
    if not route:
        return {"items": []}
    target = first_enterprise(db, user).id
    if user.platform_role not in {"super_admin", "platform_operator"}:
        require_enterprise_admin(db, user, target)
    items = db.scalars(select(ApiCredential).where(ApiCredential.route_id == route.id, ApiCredential.enterprise_id == target).order_by(ApiCredential.created_at.desc())).all()
    return {"items": [{"id": x.id, "name": x.name, "key_prefix": x.key_prefix, "status": x.status, "rate_limit_per_minute": x.rate_limit_per_minute or route.rate_limit_per_minute, "daily_quota": x.daily_quota or route.daily_quota, "monthly_quota": x.monthly_quota or route.monthly_quota, "expires_at": x.expires_at, "last_used_at": x.last_used_at, "created_at": x.created_at} for x in items]}


@app.get("/api/products/{product_id}/gateway-usage")
def gateway_usage(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    membership = current_membership(db, user, product.enterprise_id)
    if membership.role not in {"super_admin", "enterprise_admin"} and user.platform_role not in {"super_admin", "platform_operator"}:
        raise HTTPException(403, "只有产品提供企业管理员或平台管理员可以查看调用统计")
    route = db.scalar(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id))
    if not route:
        return {"summary": {"total": 0, "success": 0, "error": 0, "avg_latency_ms": 0}, "items": []}
    rows = db.scalars(select(ApiUsage).where(ApiUsage.route_id == route.id).order_by(ApiUsage.created_at.desc()).limit(1000)).all()
    total = len(rows)
    success = sum(1 for x in rows if x.status_code < 400)
    return {"summary": {"total": total, "success": success, "error": total - success, "avg_latency_ms": round(sum(x.latency_ms for x in rows) / total, 1) if total else 0}, "items": [{"method": x.method, "path": x.path, "status_code": x.status_code, "latency_ms": x.latency_ms, "request_bytes": x.request_bytes, "response_bytes": x.response_bytes, "created_at": x.created_at} for x in rows]}


SAAS_CYCLES = {"monthly": 30, "quarterly": 90, "annual": 365, "perpetual": None}
SAAS_CYCLE_LABELS = {"monthly": "月付", "quarterly": "季付", "annual": "年付", "perpetual": "永久"}
_saas_tokens: dict[str, tuple[str, datetime]] = {}


def saas_version_out(item: SaaSProductVersion) -> dict[str, Any]:
    return {"id": item.id, "product_id": item.product_id, "version_code": item.version_code, "name": item.name, "description": item.description, "monthly_price": float(item.monthly_price or 0), "quarterly_price": float(item.quarterly_price or 0), "annual_price": float(item.annual_price or 0), "perpetual_price": float(item.perpetual_price or 0), "max_users": item.max_users, "max_departments": item.max_departments, "max_storage_gb": item.max_storage_gb, "status": item.status, "created_at": item.created_at, "updated_at": item.updated_at}


def saas_subscription_out(item: SaaSSubscription, version: SaaSProductVersion | None = None) -> dict[str, Any]:
    return {"id": item.id, "enterprise_id": item.enterprise_id, "product_id": item.product_id, "version_id": item.version_id, "version_code": version.version_code if version else "", "version_name": version.name if version else "", "billing_cycle": item.billing_cycle, "billing_cycle_label": SAAS_CYCLE_LABELS.get(item.billing_cycle, item.billing_cycle), "external_tenant_id": item.external_tenant_id, "external_app_id": item.external_app_id, "status": item.status, "starts_at": item.starts_at, "expires_at": item.expires_at, "closed_at": item.closed_at, "recover_until": item.recover_until, "last_error": item.last_error, "created_at": item.created_at, "updated_at": item.updated_at}


def saas_product(product_id: str, db: Session) -> Product:
    product = db.get(Product, product_id)
    if not product or product.product_type != "saas" or product.delivery_method != "tenant_access":
        raise HTTPException(400, "只有 SaaS 类型且交付方式为租户/权限开通的产品支持 SaaS 接口")
    if product.status == "security_unpublished":
        raise HTTPException(409, "该 SaaS 产品已被安全策略下架，已售应用暂不可继续调用")
    return product


def saas_cycle_price(version: SaaSProductVersion, cycle: str) -> Decimal:
    if cycle not in SAAS_CYCLES:
        raise HTTPException(400, "计费周期必须是 monthly、quarterly、annual 或 perpetual")
    return Decimal(str(getattr(version, {"monthly": "monthly_price", "quarterly": "quarterly_price", "annual": "annual_price", "perpetual": "perpetual_price"}[cycle]) or 0))


def saas_integration_out(config: SaaSIntegrationConfig | None) -> dict[str, Any] | None:
    if not config:
        return None
    return {"id": config.id, "product_id": config.product_id, "base_url": config.base_url, "operation_path": config.operation_path, "token_url": config.token_url, "client_id": config.client_id, "scope": config.scope, "auth_mode": config.auth_mode, "timeout_ms": config.timeout_ms, "retention_days": config.retention_days, "status": config.status, "updated_at": config.updated_at}


def saas_access_token(config: SaaSIntegrationConfig) -> str:
    cached = _saas_tokens.get(config.id)
    if cached and cached[1] > now() + timedelta(seconds=30):
        return cached[0]
    if not config.client_id or not config.client_secret:
        raise HTTPException(400, "SaaS OAuth2 配置不完整")
    try:
        response = httpx.post(platform_oauth_token_url(), data={"grant_type": "client_credentials", "client_id": config.client_id, "client_secret": config.client_secret, **({"scope": config.scope or "resource.invoke"} if config.scope else {})}, timeout=config.timeout_ms / 1000)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"获取 SaaS OAuth2 Token 失败：{exc}") from exc
    token = payload.get("access_token")
    if not token:
        raise HTTPException(502, "SaaS OAuth2 响应缺少 access_token")
    expires_in = int(payload.get("expires_in", 3600))
    _saas_tokens[config.id] = (token, now() + timedelta(seconds=max(60, expires_in)))
    return token


def saas_call(config: SaaSIntegrationConfig, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = {"type": operation, **payload}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if config.auth_mode == "oauth2":
        headers["Authorization"] = f"Bearer {saas_access_token(config)}"
    elif config.auth_mode == "hmac":
        path = urlparse(config.operation_path).path or "/isv.php"
        canonical = "POST\n" + path + "\n" + "&".join(f"{key}={value}" for key, value in sorted(body.items()))
        body["signature"] = __import__("urllib.parse", fromlist=["quote"]).quote(__import__("base64").b64encode(hmac.new(config.client_secret.encode(), canonical.encode(), hashlib.sha256).digest()).decode(), safe="")
    else:
        raise HTTPException(400, "不支持的 SaaS 认证模式")
    url = config.base_url.rstrip("/") + "/" + config.operation_path.lstrip("/")
    try:
        response = httpx.post(url, json=body, headers=headers, timeout=config.timeout_ms / 1000)
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"SaaS 接口调用失败：{exc}") from exc
    if result.get("ret") not in (None, 0):
        raise HTTPException(502, result.get("msg", "SaaS 接口返回失败"))
    return result


def execute_saas_operation(db: Session, subscription: SaaSSubscription, operation: str, payload: dict[str, Any], config: SaaSIntegrationConfig, idempotency_key: str) -> dict[str, Any]:
    existing = db.scalar(select(SaaSOperation).where(SaaSOperation.idempotency_key == idempotency_key))
    if existing and existing.status == "succeeded":
        return json.loads(existing.response_payload or "{}")
    record = existing or SaaSOperation(subscription_id=subscription.id, operation=operation, idempotency_key=idempotency_key, request_payload=json.dumps(payload, ensure_ascii=False))
    if not existing:
        db.add(record)
    record.status = "executing"
    db.commit()
    last_error: HTTPException | None = None
    for attempt in range(3):
        try:
            result = saas_call(config, operation, payload)
            record.response_payload = json.dumps(result, ensure_ascii=False)
            record.status = "succeeded"
            record.completed_at = now()
            record.error_message = ""
            db.commit()
            return result
        except HTTPException as exc:
            last_error = exc
            record.retry_count = attempt + 1
            record.error_message = str(exc.detail)
            record.status = "retrying" if attempt < 2 else "failed"
            db.commit()
            if attempt < 2:
                time.sleep(10)
    raise last_error or HTTPException(502, "SaaS 接口调用失败")


def add_saas_order(db: Session, subscription: SaaSSubscription, product: Product, version: SaaSProductVersion, amount: Decimal, business_type: str, related_order_id: str = "", paid: bool = False) -> Order:
    enterprise = db.get(Enterprise, subscription.enterprise_id)
    order = Order(order_no=make_order_no(), buyer_enterprise_id=subscription.enterprise_id, provider_enterprise_id=product.enterprise_id, product_id=product.id, product_version_id=version.id, product_version_code=version.version_code, product_version_name=version.name, billing_cycle=subscription.billing_cycle, subscription_id=subscription.id, business_type=business_type, related_order_id=related_order_id, product_name=product.name, buyer_name=enterprise.name if enterprise else "", amount=amount, paid_amount=amount if paid else 0, main_status="completed" if paid else "created", payment_status="paid" if paid else "unpaid")
    db.add(order)
    db.flush()
    db.add(Payment(order_id=order.id, payment_no="PAY-" + secrets.token_hex(6).upper(), amount=amount, status="paid" if paid else "unpaid"))
    return order


@app.get("/api/products/{product_id}/saas-versions")
def list_saas_versions(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = saas_product(product_id, db)
    current_membership(db, user, product.enterprise_id)
    return {"items": [saas_version_out(x) for x in db.scalars(select(SaaSProductVersion).where(SaaSProductVersion.product_id == product.id).order_by(SaaSProductVersion.created_at)).all()]}


@app.post("/api/products/{product_id}/saas-versions")
def create_saas_version(product_id: str, body: SaaSVersionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = saas_product(product_id, db)
    require_enterprise_admin(db, user, product.enterprise_id)
    if db.scalar(select(SaaSProductVersion).where(SaaSProductVersion.product_id == product.id, SaaSProductVersion.version_code == body.version_code)):
        raise HTTPException(409, "SaaS 版本编号已存在")
    version = SaaSProductVersion(product_id=product.id, **body.model_dump())
    db.add(version)
    audit(db, user.email or user.phone or user.id, "create_saas_version", "saas_version", version.id, version.version_code)
    db.commit()
    db.refresh(version)
    return saas_version_out(version)


@app.get("/api/products/{product_id}/saas-integration")
def get_saas_integration(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = saas_product(product_id, db)
    current_membership(db, user, product.enterprise_id)
    return {"item": saas_integration_out(db.scalar(select(SaaSIntegrationConfig).where(SaaSIntegrationConfig.product_id == product.id)))}


@app.put("/api/products/{product_id}/saas-integration")
def save_saas_integration(product_id: str, body: SaaSIntegrationBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = saas_product(product_id, db)
    require_enterprise_admin(db, user, product.enterprise_id)
    if body.auth_mode == "oauth2" and not body.token_url:
        raise HTTPException(400, "OAuth2 模式必须配置 Token 地址")
    config = db.scalar(select(SaaSIntegrationConfig).where(SaaSIntegrationConfig.product_id == product.id))
    values = body.model_dump()
    if config:
        if not values.get("client_secret"):
            values["client_secret"] = config.client_secret
        for key, value in values.items(): setattr(config, key, value)
    else:
        config = SaaSIntegrationConfig(product_id=product.id, updated_by=user.email or user.phone or user.id, **values)
        db.add(config)
    config.status = "active"
    config.updated_by = user.email or user.phone or user.id
    db.commit()
    db.refresh(config)
    return saas_integration_out(config)


@app.post("/api/products/{product_id}/saas-subscriptions")
def create_saas_subscription(product_id: str, body: SaaSSubscriptionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = saas_product(product_id, db)
    enterprise = first_enterprise(db, user)
    require_enterprise_admin(db, user, enterprise.id)
    version = db.scalar(select(SaaSProductVersion).where(SaaSProductVersion.id == body.version_id, SaaSProductVersion.product_id == product.id, SaaSProductVersion.status == "active"))
    config = db.scalar(select(SaaSIntegrationConfig).where(SaaSIntegrationConfig.product_id == product.id, SaaSIntegrationConfig.status == "active"))
    if not version or not config:
        raise HTTPException(400, "SaaS 版本或第三方接口配置不可用")
    saas_cycle_price(version, body.billing_cycle)
    starts = now()
    expires = None if body.billing_cycle == "perpetual" else starts + timedelta(days=SAAS_CYCLES[body.billing_cycle])
    subscription = SaaSSubscription(enterprise_id=enterprise.id, product_id=product.id, version_id=version.id, billing_cycle=body.billing_cycle, status="provisioning", starts_at=starts, expires_at=expires, created_by=user.email or user.phone or user.id)
    db.add(subscription)
    db.flush()
    result = execute_saas_operation(db, subscription, "OPEN", {"product_id": product.id, "subscription_id": subscription.id, "version": version.version_code, "billing_cycle": body.billing_cycle, "enterprise_id": enterprise.id, "enterprise_name": enterprise.name}, config, f"open:{subscription.id}")
    subscription.external_app_id = str(result.get("app_id", ""))
    subscription.external_tenant_id = str(result.get("tenant_id", result.get("app_id", "")))
    subscription.status = "active"
    add_saas_order(db, subscription, product, version, saas_cycle_price(version, body.billing_cycle), "purchase", paid=True)
    db.commit()
    return saas_subscription_out(subscription, version)


@app.get("/api/saas-subscriptions")
def list_saas_subscriptions(user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = first_enterprise(db, user)
    rows = db.scalars(select(SaaSSubscription).where(SaaSSubscription.enterprise_id == enterprise.id).order_by(SaaSSubscription.created_at.desc())).all()
    return {"items": [saas_subscription_out(row, db.get(SaaSProductVersion, row.version_id)) for row in rows]}


def subscription_context(subscription_id: str, user: User, db: Session) -> tuple[SaaSSubscription, Product, SaaSProductVersion, SaaSIntegrationConfig]:
    subscription = db.get(SaaSSubscription, subscription_id)
    if not subscription:
        raise HTTPException(404, "SaaS 订阅不存在")
    require_enterprise_admin(db, user, subscription.enterprise_id)
    product = saas_product(subscription.product_id, db)
    version = db.get(SaaSProductVersion, subscription.version_id)
    config = db.scalar(select(SaaSIntegrationConfig).where(SaaSIntegrationConfig.product_id == product.id, SaaSIntegrationConfig.status == "active"))
    if not version or not config:
        raise HTTPException(400, "SaaS 订阅版本或接口配置不存在")
    return subscription, product, version, config


@app.post("/api/saas-subscriptions/{subscription_id}/renew")
def renew_saas_subscription(subscription_id: str, body: SaaSRenewBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    subscription, product, version, config = subscription_context(subscription_id, user, db)
    amount = saas_cycle_price(version, body.billing_cycle)
    result = execute_saas_operation(db, subscription, "RENEW", {"tenant_id": subscription.external_tenant_id, "billing_cycle": body.billing_cycle, "enterprise_id": subscription.enterprise_id}, config, f"renew:{subscription.id}:{body.billing_cycle}:{subscription.expires_at}")
    if subscription.expires_at and body.billing_cycle != "perpetual": subscription.expires_at += timedelta(days=SAAS_CYCLES[body.billing_cycle])
    elif body.billing_cycle != "perpetual": subscription.expires_at = now() + timedelta(days=SAAS_CYCLES[body.billing_cycle])
    else: subscription.expires_at = None
    add_saas_order(db, subscription, product, version, amount, "renew", paid=True)
    db.commit()
    return {"subscription": saas_subscription_out(subscription, version), "provider_result": result}


@app.post("/api/saas-subscriptions/{subscription_id}/change-version")
def change_saas_version(subscription_id: str, body: SaaSChangeBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    subscription, product, current_version, config = subscription_context(subscription_id, user, db)
    target = db.scalar(select(SaaSProductVersion).where(SaaSProductVersion.id == body.version_id, SaaSProductVersion.product_id == product.id, SaaSProductVersion.status == "active"))
    if not target or target.id == current_version.id:
        raise HTTPException(400, "目标 SaaS 版本无效")
    current_price = saas_cycle_price(current_version, subscription.billing_cycle)
    target_price = saas_cycle_price(target, body.billing_cycle)
    remaining_days = max(0, (subscription.expires_at - now()).days) if subscription.expires_at else 0
    total_days = SAAS_CYCLES.get(subscription.billing_cycle) or 365
    prorated_current = (current_price * Decimal(str(remaining_days)) / Decimal(str(total_days))).quantize(Decimal("0.01"))
    prorated_target = (target_price * Decimal(str(remaining_days)) / Decimal(str(total_days))).quantize(Decimal("0.01"))
    difference = (prorated_target - prorated_current).quantize(Decimal("0.01"))
    if difference >= 0:
        order = add_saas_order(db, subscription, product, target, difference, "upgrade", paid=False)
        db.commit()
        return {"change_type": "upgrade", "difference": float(difference), "order_id": order.id, "requires_payment": True, "subscription": saas_subscription_out(subscription, current_version)}
    refund = abs(difference)
    result = execute_saas_operation(db, subscription, "CHANGE", {"tenant_id": subscription.external_tenant_id, "from_version": current_version.version_code, "to_version": target.version_code, "change_type": "downgrade"}, config, f"downgrade:{subscription.id}:{target.id}:{subscription.expires_at}")
    subscription.version_id = target.id
    order = add_saas_order(db, subscription, product, target, refund, "downgrade", paid=True)
    order.refunded_amount = refund
    order.payment_status = "refunded"
    subscription.status = "active"
    db.commit()
    return {"change_type": "downgrade", "refund_amount": float(refund), "order_id": order.id, "refunded": True, "provider_result": result, "subscription": saas_subscription_out(subscription, target)}


@app.post("/api/saas-subscriptions/{subscription_id}/close")
def close_saas_subscription(subscription_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    subscription, product, version, config = subscription_context(subscription_id, user, db)
    result = execute_saas_operation(db, subscription, "CLOSE", {"tenant_id": subscription.external_tenant_id}, config, f"close:{subscription.id}")
    subscription.status = "closed"
    subscription.closed_at = now()
    subscription.recover_until = now() + timedelta(days=30)
    db.commit()
    return {"subscription": saas_subscription_out(subscription, version), "provider_result": result}


@app.post("/api/saas-subscriptions/{subscription_id}/restore")
def restore_saas_subscription(subscription_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    subscription, product, version, config = subscription_context(subscription_id, user, db)
    if subscription.status != "closed" or not subscription.recover_until or subscription.recover_until < now():
        raise HTTPException(409, "SaaS 租户已超过可恢复期限")
    result = execute_saas_operation(db, subscription, "OPEN", {"tenant_id": subscription.external_tenant_id, "version": version.version_code, "enterprise_id": subscription.enterprise_id}, config, f"restore:{subscription.id}")
    subscription.status = "active"
    subscription.closed_at = None
    db.commit()
    return {"subscription": saas_subscription_out(subscription, version), "provider_result": result}


@app.get("/api/saas-subscriptions/{subscription_id}/operations")
def list_saas_operations(subscription_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    subscription, _, _, _ = subscription_context(subscription_id, user, db)
    rows = db.scalars(select(SaaSOperation).where(SaaSOperation.subscription_id == subscription.id).order_by(SaaSOperation.created_at.desc())).all()
    return {"items": [{"id": x.id, "operation": x.operation, "status": x.status, "retry_count": x.retry_count, "error_message": x.error_message, "created_at": x.created_at, "completed_at": x.completed_at} for x in rows]}


@app.get("/api/saas-subscriptions/{subscription_id}/users")
def list_saas_users(subscription_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    subscription, _, _, _ = subscription_context(subscription_id, user, db)
    rows = db.scalars(select(SaaSUserMapping).where(SaaSUserMapping.subscription_id == subscription.id).order_by(SaaSUserMapping.created_at)).all()
    return {"items": [{"id": x.id, "user_id": x.user_id, "department_id": x.department_id, "external_user_id": x.external_user_id, "status": x.status} for x in rows]}


@app.post("/api/saas-subscriptions/{subscription_id}/users")
def assign_saas_user(subscription_id: str, body: SaaSUserBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    subscription, _, _, config = subscription_context(subscription_id, user, db)
    target = db.get(User, body.user_id)
    membership = db.scalar(select(Membership).where(Membership.user_id == body.user_id, Membership.enterprise_id == subscription.enterprise_id, Membership.status == "active"))
    if not target or not membership:
        raise HTTPException(400, "用户不是该企业的有效成员")
    mapping = db.scalar(select(SaaSUserMapping).where(SaaSUserMapping.subscription_id == subscription.id, SaaSUserMapping.user_id == target.id))
    if mapping and mapping.status == "active":
        return {"id": mapping.id, "status": mapping.status, "external_user_id": mapping.external_user_id}
    payload = {"tenant_id": subscription.external_tenant_id, "user_id": target.id, "username": target.email or target.phone or target.id, "name": target.name, "department_id": body.department_id}
    result = execute_saas_operation(db, subscription, "USER_ASSIGN", payload, config, f"user-assign:{subscription.id}:{target.id}:{body.department_id}")
    external_id = str(result.get("user_id", result.get("external_user_id", target.id)))
    if not mapping:
        mapping = SaaSUserMapping(subscription_id=subscription.id, user_id=target.id)
        db.add(mapping)
    mapping.department_id = body.department_id
    mapping.external_user_id = external_id
    mapping.status = "active"
    db.commit()
    return {"id": mapping.id, "user_id": mapping.user_id, "department_id": mapping.department_id, "external_user_id": mapping.external_user_id, "status": mapping.status, "provider_result": result}


@app.delete("/api/saas-subscriptions/{subscription_id}/users/{user_id}")
def remove_saas_user(subscription_id: str, user_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    subscription, _, _, config = subscription_context(subscription_id, user, db)
    mapping = db.scalar(select(SaaSUserMapping).where(SaaSUserMapping.subscription_id == subscription.id, SaaSUserMapping.user_id == user_id, SaaSUserMapping.status == "active"))
    if not mapping:
        raise HTTPException(404, "SaaS 用户映射不存在")
    result = execute_saas_operation(db, subscription, "USER_UNASSIGN", {"tenant_id": subscription.external_tenant_id, "user_id": mapping.external_user_id or user_id}, config, f"user-unassign:{subscription.id}:{user_id}:{mapping.id}")
    mapping.status = "deleted"
    db.commit()
    return {"id": mapping.id, "status": mapping.status, "provider_result": result}


@app.get("/api/saas-subscriptions/{subscription_id}/departments")
def list_saas_departments(subscription_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    subscription, _, _, _ = subscription_context(subscription_id, user, db)
    rows = db.scalars(select(SaaSDepartment).where(SaaSDepartment.subscription_id == subscription.id).order_by(SaaSDepartment.created_at)).all()
    return {"items": [{"id": x.id, "name": x.name, "parent_id": x.parent_id, "platform_department_id": x.platform_department_id, "external_department_id": x.external_department_id, "status": x.status} for x in rows]}


@app.post("/api/saas-subscriptions/{subscription_id}/departments")
def create_saas_department(subscription_id: str, body: SaaSDepartmentBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    subscription, _, _, config = subscription_context(subscription_id, user, db)
    department = SaaSDepartment(subscription_id=subscription.id, platform_department_id=body.platform_department_id, name=body.name, parent_id=body.parent_id, status="syncing")
    db.add(department)
    db.flush()
    result = execute_saas_operation(db, subscription, "DEPT_CREATE", {"tenant_id": subscription.external_tenant_id, "department_id": department.id, "parent_id": body.parent_id, "name": body.name}, config, f"dept-create:{subscription.id}:{department.id}")
    department.external_department_id = str(result.get("department_id", result.get("external_department_id", department.id)))
    department.status = "active"
    db.commit()
    return {"id": department.id, "name": department.name, "parent_id": department.parent_id, "platform_department_id": department.platform_department_id, "external_department_id": department.external_department_id, "status": department.status, "provider_result": result}


@app.delete("/api/saas-subscriptions/{subscription_id}/departments/{department_id}")
def remove_saas_department(subscription_id: str, department_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    subscription, _, _, config = subscription_context(subscription_id, user, db)
    department = db.scalar(select(SaaSDepartment).where(SaaSDepartment.id == department_id, SaaSDepartment.subscription_id == subscription.id, SaaSDepartment.status == "active"))
    if not department:
        raise HTTPException(404, "SaaS 部门映射不存在")
    result = execute_saas_operation(db, subscription, "DEPT_REMOVE", {"tenant_id": subscription.external_tenant_id, "department_id": department.external_department_id or department.id}, config, f"dept-remove:{subscription.id}:{department.id}")
    department.status = "deleted"
    db.commit()
    return {"id": department.id, "status": department.status, "provider_result": result}


def order_out(o: Order) -> dict[str, Any]:
    return {"id": o.id, "order_no": o.order_no, "buyer_user_id": o.buyer_user_id, "buyer_name": o.buyer_name, "product_name": o.product_name, "product_version_id": o.product_version_id, "product_version_code": o.product_version_code, "product_version_name": o.product_version_name, "billing_cycle": o.billing_cycle, "subscription_id": o.subscription_id, "business_type": o.business_type, "related_order_id": o.related_order_id, "main_status": o.main_status, "payment_status": o.payment_status, "delivery_status": o.delivery_status, "after_sales_status": o.after_sales_status, "amount": float(o.amount or 0), "paid_amount": float(o.paid_amount or 0), "refunded_amount": float(o.refunded_amount or 0), "created_at": o.created_at, "updated_at": o.updated_at}


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
    if user.verified_status != "verified":
        require_enterprise_admin(db, user, buyer.id)
    product = db.get(Product, body.product_id)
    if not product or product.status != "published":
        raise HTTPException(400, "产品不存在或尚未发布")
    version = db.scalar(select(ProductReleaseVersion).where(ProductReleaseVersion.product_id == product.id, ProductReleaseVersion.status == "active", ProductReleaseVersion.id == body.product_version_id)) if body.product_version_id else db.scalar(select(ProductReleaseVersion).where(ProductReleaseVersion.product_id == product.id, ProductReleaseVersion.status == "active").order_by(ProductReleaseVersion.created_at))
    if not version:
        raise HTTPException(400, "产品版本不存在或未启用")
    order = Order(order_no=make_order_no(), buyer_enterprise_id=buyer.id, buyer_user_id=user.id, provider_enterprise_id=product.enterprise_id, product_id=product.id, product_version_id=version.id, product_version_code=version.version_code, product_version_name=version.description, product_name=product.name, buyer_name=user.name if user.verified_status == "verified" else buyer.name, amount=version.price, main_status="created")
    db.add(order)
    db.flush()
    db.add(Payment(order_id=order.id, payment_no="PAY-" + secrets.token_hex(6).upper(), amount=version.price, status="unpaid"))
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
    if body.action in {"confirm_payment", "cancel_order"}:
        require_enterprise_admin(db, user, order.buyer_enterprise_id)
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
    if body.action == "cancel_order":
        if order.main_status not in {"created", "pending_review", "pending_fulfillment"} or order.payment_status not in {"unpaid", "paying"}:
            raise HTTPException(409, "当前订单状态不允许取消")
        old_main = order.main_status
        order.main_status = "cancelled"
        log_state(db, order, "main", old_main, "cancelled", body.action, user, body.reason)
        audit(db, user.email or user.phone or user.id, body.action, "order", order.id, body.reason)
        db.commit()
        return order_out(order)
    if body.action == "confirm_payment":
        payment = db.scalar(select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at.desc()))
        if not payment:
            raise HTTPException(400, "支付单不存在")
        if order.business_type == "upgrade" and order.subscription_id and order.product_version_id:
            subscription = db.get(SaaSSubscription, order.subscription_id)
            target_version = db.get(SaaSProductVersion, order.product_version_id)
            config = db.scalar(select(SaaSIntegrationConfig).where(SaaSIntegrationConfig.product_id == order.product_id, SaaSIntegrationConfig.status == "active"))
            if not subscription or not target_version or not config:
                raise HTTPException(409, "SaaS 升级订单缺少有效订阅、版本或接口配置")
            current_version = db.get(SaaSProductVersion, subscription.version_id)
            if current_version and current_version.id != target_version.id:
                execute_saas_operation(db, subscription, "CHANGE", {"tenant_id": subscription.external_tenant_id, "from_version": current_version.version_code, "to_version": target_version.version_code, "change_type": "upgrade"}, config, f"upgrade:{subscription.id}:{target_version.id}:{order.id}")
                subscription.version_id = target_version.id
                subscription.status = "active"
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
    return {"items": [{"id": x.id, "name": x.name, "email": x.email, "phone": x.phone, "verified_status": x.verified_status, "is_active": x.is_active, "platform_role": x.platform_role, "created_at": x.created_at} for x in items]}


@app.get("/api/admin/enterprises")
def admin_enterprises(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    items = db.scalars(select(Enterprise).order_by(Enterprise.created_at.desc())).all()
    return {"items": [{"id": x.id, "name": x.name, "credit_code": x.credit_code, "enterprise_type": x.enterprise_type, "legal_representative": x.legal_representative, "license_file_id": x.license_file_id, "verification_status": x.verification_status, "verified_by": x.verified_by, "verified_at": x.verified_at, "created_at": x.created_at} for x in items]}


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
