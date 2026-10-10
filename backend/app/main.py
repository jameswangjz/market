from __future__ import annotations

import hashlib
import hmac
import json
import calendar
from contextvars import ContextVar
import os
import re
import secrets
import smtplib
import socket
import ssl
import struct
import subprocess
import shutil
import tarfile
import tempfile
import time
import zipfile
from email.message import EmailMessage
from io import BytesIO
from urllib.parse import parse_qs, quote
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
import redis
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from minio import Minio
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, create_engine, func, inspect, or_, select, text, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./market.db")
JWT_SECRET = os.getenv("JWT_SECRET", "market-development-secret-change-me")
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "market")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "market123456")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "market-files")
CLAMAV_ENABLED = os.getenv("CLAMAV_ENABLED", "true").lower() == "true"
CLAMAV_HOST = os.getenv("CLAMAV_HOST", "market-clamav")
CLAMAV_PORT = int(os.getenv("CLAMAV_PORT", "3310"))

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def membership_department_ids(db: Session, membership: Membership) -> list[str]:
    links = db.scalars(select(MembershipDepartment).where(MembershipDepartment.membership_id == membership.id)).all()
    ids = [item.department_id for item in links]
    if membership.department_id and membership.department_id not in ids:
        ids.insert(0, membership.department_id)
    return ids


def replace_membership_departments(db: Session, membership: Membership, department_ids: list[str]) -> None:
    normalized = list(dict.fromkeys(item for item in department_ids if item))
    for link in db.scalars(select(MembershipDepartment).where(MembershipDepartment.membership_id == membership.id)).all():
        db.delete(link)
    for department_id in normalized:
        db.add(MembershipDepartment(membership_id=membership.id, department_id=department_id))
    membership.department_id = normalized[0] if normalized else ""


def invitation_department_ids(invitation: EnterpriseInvitation) -> list[str]:
    try:
        ids = json.loads(invitation.department_ids_json or "[]")
    except (TypeError, json.JSONDecodeError):
        ids = []
    if not isinstance(ids, list):
        ids = []
    ids = [item for item in ids if isinstance(item, str) and item]
    if invitation.department_id and invitation.department_id not in ids:
        ids.insert(0, invitation.department_id)
    return list(dict.fromkeys(ids))


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    username: Mapped[str | None] = mapped_column(String(80), unique=True, index=True, nullable=True)
    email: Mapped[str | None] = mapped_column(String(180), unique=True, index=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), unique=True, index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_status: Mapped[str] = mapped_column(String(30), default="pending")
    activation_status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    phone_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    email_activation_token: Mapped[str] = mapped_column(String(120), default="")
    email_activation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    registered_capital: Mapped[str] = mapped_column(String(120), default="")
    establishment_date: Mapped[str] = mapped_column(String(30), default="")
    business_address: Mapped[str] = mapped_column(String(500), default="")
    business_scope: Mapped[str] = mapped_column(Text, default="")
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
    department_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    business_roles: Mapped[str] = mapped_column(String(255), default="provider,user")
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    invited_by: Mapped[str] = mapped_column(String(36), default="")
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MembershipDepartment(Base):
    __tablename__ = "membership_departments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    membership_id: Mapped[str] = mapped_column(ForeignKey("memberships.id"), index=True)
    department_id: Mapped[str] = mapped_column(ForeignKey("enterprise_departments.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EnterpriseDepartment(Base):
    __tablename__ = "enterprise_departments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    parent_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    created_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


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
    department_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    department_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    channel: Mapped[str] = mapped_column(String(20), default="email")
    created_user: Mapped[bool] = mapped_column(Boolean, default=False)
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


class PlatformNotification(Base):
    __tablename__ = "platform_notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    recipient_user_id: Mapped[str] = mapped_column(String(36), index=True)
    recipient_role: Mapped[str] = mapped_column(String(60), default="", index=True)
    title: Mapped[str] = mapped_column(String(220))
    content: Mapped[str] = mapped_column(Text, default="")
    target_type: Mapped[str] = mapped_column(String(60), default="")
    target_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    status: Mapped[str] = mapped_column(String(20), default="unread", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


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
    upstream_url: Mapped[str] = mapped_column(String(500), default="")
    application_url: Mapped[str] = mapped_column(String(500), default="")
    integration_api_url: Mapped[str] = mapped_column(String(500), default="")
    download_limit: Mapped[int] = mapped_column(Integer, default=0)
    logo_file_id: Mapped[str] = mapped_column(String(36), default="")
    logo_thumbnail_file_id: Mapped[str] = mapped_column(String(36), default="")
    price: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    pricing_strategy: Mapped[str] = mapped_column(Text, default="", nullable=True)
    currency: Mapped[str] = mapped_column(String(10), default="CNY")
    version: Mapped[str] = mapped_column(String(30), default="v1.0")
    quality_level: Mapped[str] = mapped_column(String(30), default="标准")
    security_level: Mapped[str] = mapped_column(String(40), default="一般", nullable=True)
    authorization_conditions: Mapped[str] = mapped_column(Text, default="", nullable=True)
    data_source_statement: Mapped[str] = mapped_column(Text, default="", nullable=True)
    compliance_statement: Mapped[str] = mapped_column(Text, default="", nullable=True)
    settlement_rule_mode: Mapped[str] = mapped_column(String(20), default="global")
    settlement_rule_id: Mapped[str] = mapped_column(String(36), default="")
    settlement_rule_json: Mapped[str] = mapped_column(Text, default="{}")
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
    cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    daily_quota: Mapped[int] = mapped_column(Integer, default=10000)
    monthly_quota: Mapped[int] = mapped_column(Integer, default=0)
    quota_unit: Mapped[str] = mapped_column(String(20), default="")
    quota_amount: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    product: Mapped[Product] = relationship(back_populates="versions")


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
    cost: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
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
    __table_args__ = (UniqueConstraint("product_id", "version", name="uq_gateway_product_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), index=True)
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
    validated_ip: Mapped[str] = mapped_column(String(64), default="")
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
    total_quota: Mapped[int] = mapped_column(Integer, default=0)
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
    snapshot_version: Mapped[int] = mapped_column(Integer, default=0)
    subscription_months: Mapped[int] = mapped_column(Integer, default=1)
    unit_price_snapshot: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    unit_cost_snapshot: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    total_cost_snapshot: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    delivery_method_snapshot: Mapped[str] = mapped_column(String(40), default="")
    economic_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    delivery_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
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
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    delivery_mode: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(30), default="preparing")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[str] = mapped_column(Text, default="")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DeliveryAttachment(Base):
    __tablename__ = "delivery_attachments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    task_id: Mapped[str] = mapped_column(ForeignKey("delivery_tasks.id"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("file_objects.id"), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[str] = mapped_column(String(180), default="")
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
    service_level_code: Mapped[str] = mapped_column(String(30), default="standard")
    response_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Settlement(Base):
    __tablename__ = "settlements"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    settlement_no: Mapped[str] = mapped_column(String(60), unique=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    refund_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    reference_settlement_id: Mapped[str] = mapped_column(String(36), default="")
    is_refund: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    gross_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    refund_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    net_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    cost_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    profit_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    refund_recovery: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    platform_fee: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    provider_share: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    service_share: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    expert_fee: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    channel_fee: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    tax_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    adjustment: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SettlementRule(Base):
    __tablename__ = "settlement_rules"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    rule_no: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    version: Mapped[str] = mapped_column(String(40), default="v1")
    name: Mapped[str] = mapped_column(String(180))
    scope_json: Mapped[str] = mapped_column(Text, default="{}")
    formula_json: Mapped[str] = mapped_column(Text, default="{}")
    platform_rate: Mapped[float] = mapped_column(Numeric(8, 4), default=8)
    provider_rate: Mapped[float] = mapped_column(Numeric(8, 4), default=61)
    service_rate: Mapped[float] = mapped_column(Numeric(8, 4), default=20)
    expert_rate: Mapped[float] = mapped_column(Numeric(8, 4), default=5)
    channel_rate: Mapped[float] = mapped_column(Numeric(8, 4), default=0)
    tax_rate: Mapped[float] = mapped_column(Numeric(8, 4), default=6)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    approved_by: Mapped[str] = mapped_column(String(180), default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    change_reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SettlementBatch(Base):
    __tablename__ = "settlement_batches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    batch_no: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    cycle: Mapped[str] = mapped_column(String(30), default="manual")
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rule_id: Mapped[str] = mapped_column(String(36), default="")
    status: Mapped[str] = mapped_column(String(30), default="generated", index=True)
    total_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    total_profit: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    exception_count: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    request_digest: Mapped[str] = mapped_column(String(64), default="")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SettlementLine(Base):
    __tablename__ = "settlement_lines"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    batch_id: Mapped[str] = mapped_column(ForeignKey("settlement_batches.id"), index=True)
    settlement_id: Mapped[str] = mapped_column(ForeignKey("settlements.id"), index=True)
    participant_type: Mapped[str] = mapped_column(String(40))
    participant_id: Mapped[str] = mapped_column(String(36), default="")
    participant_name: Mapped[str] = mapped_column(String(180), default="")
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    payment_no: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SettlementMeasurement(Base):
    __tablename__ = "settlement_measurements"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    measurement_type: Mapped[str] = mapped_column(String(50))
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    unit: Mapped[str] = mapped_column(String(30), default="count")
    source: Mapped[str] = mapped_column(String(120), default="platform")
    event_key: Mapped[str] = mapped_column(String(180), default="", index=True)
    source_id: Mapped[str] = mapped_column(String(180), default="")
    actor_id: Mapped[str] = mapped_column(String(36), default="")
    sample_kind: Mapped[str] = mapped_column(String(20), default="event")
    scope: Mapped[str] = mapped_column(String(30), default="order")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validation_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    validation_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SettlementReconciliation(Base):
    __tablename__ = "settlement_reconciliations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    batch_id: Mapped[str] = mapped_column(ForeignKey("settlement_batches.id"), index=True)
    ledger_type: Mapped[str] = mapped_column(String(30))
    expected_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    actual_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    difference_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    status: Mapped[str] = mapped_column(String(30), default="matched", index=True)
    resolution: Mapped[str] = mapped_column(Text, default="")
    closed_by: Mapped[str] = mapped_column(String(180), default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SettlementCorrection(Base):
    __tablename__ = "settlement_corrections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    settlement_id: Mapped[str] = mapped_column(ForeignKey("settlements.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    correction_type: Mapped[str] = mapped_column(String(30))
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    reason: Mapped[str] = mapped_column(Text)
    source_ref: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    recovery_mode: Mapped[str] = mapped_column(String(30), default="future_offset")
    approved_by: Mapped[str] = mapped_column(String(180), default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SettlementAdjustment(Base):
    __tablename__ = "settlement_adjustments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    settlement_id: Mapped[str] = mapped_column(ForeignKey("settlements.id"), index=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(180))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SettlementAdjustmentProposal(Base):
    __tablename__ = "settlement_adjustment_proposals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    settlement_id: Mapped[str] = mapped_column(ForeignKey("settlements.id"), index=True)
    proposed_by: Mapped[str] = mapped_column(String(180))
    reason: Mapped[str] = mapped_column(Text)
    values_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    reviewed_by: Mapped[str] = mapped_column(String(180), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_comment: Mapped[str] = mapped_column(Text, default="")
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
    category: Mapped[str] = mapped_column(String(40), default="ops", index=True)
    business_domain: Mapped[str] = mapped_column(String(50), default="")
    tenant_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    order_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    batch_no: Mapped[str] = mapped_column(String(60), default="", index=True)
    rule_version: Mapped[str] = mapped_column(String(40), default="")
    request_id: Mapped[str] = mapped_column(String(80), default="")
    risk_level: Mapped[str] = mapped_column(String(20), default="normal")
    before_json: Mapped[str] = mapped_column(Text, default="{}")
    after_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FileObject(Base):
    __tablename__ = "file_objects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"), index=True, nullable=True)
    version_id: Mapped[str | None] = mapped_column(ForeignKey("product_release_versions.id"), index=True, nullable=True)
    object_name: Mapped[str] = mapped_column(String(500))
    original_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64), default="")
    file_role: Mapped[str] = mapped_column(String(50), default="product_data")
    version: Mapped[str] = mapped_column(String(30), default="v1.0")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    scan_status: Mapped[str] = mapped_column(String(30), default="not_scanned", index=True)
    scan_report: Mapped[str] = mapped_column(Text, default="")
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FileDownloadLog(Base):
    __tablename__ = "file_download_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    file_id: Mapped[str] = mapped_column(ForeignKey("file_objects.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="")
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


class SLAProfile(Base):
    __tablename__ = "sla_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    name: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    service_scope: Mapped[str] = mapped_column(String(40), default="platform")
    product_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    evaluation_period: Mapped[str] = mapped_column(String(20), default="daily")
    availability_target: Mapped[float] = mapped_column(Numeric(8, 4), default=99.9)
    latency_target_ms: Mapped[int] = mapped_column(Integer, default=1000)
    error_rate_target: Mapped[float] = mapped_column(Numeric(8, 4), default=1)
    delivery_hours: Mapped[int] = mapped_column(Integer, default=24)
    recovery_minutes: Mapped[int] = mapped_column(Integer, default=60)
    warning_margin: Mapped[float] = mapped_column(Numeric(8, 4), default=0.5)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ServiceLevel(Base):
    __tablename__ = "service_levels"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    customer_scope: Mapped[str] = mapped_column(String(40), default="all")
    support_days_per_week: Mapped[int] = mapped_column(Integer, default=5)
    support_hours_per_day: Mapped[int] = mapped_column(Integer, default=8)
    online_docs: Mapped[bool] = mapped_column(Boolean, default=True)
    knowledge_base: Mapped[bool] = mapped_column(Boolean, default=True)
    standard_api: Mapped[bool] = mapped_column(Boolean, default=True)
    online_customer_service: Mapped[bool] = mapped_column(Boolean, default=True)
    dedicated_manager: Mapped[bool] = mapped_column(Boolean, default=False)
    technical_support: Mapped[bool] = mapped_column(Boolean, default=False)
    initial_response_minutes: Mapped[int] = mapped_column(Integer, default=2880)
    problem_response_hours: Mapped[int] = mapped_column(Integer, default=48)
    quarterly_report: Mapped[bool] = mapped_column(Boolean, default=False)
    annual_optimization: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    created_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class ServiceLevelAssignment(Base):
    __tablename__ = "service_level_assignments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    service_level_id: Mapped[str] = mapped_column(ForeignKey("service_levels.id"), index=True)
    enterprise_id: Mapped[str] = mapped_column(ForeignKey("enterprises.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    source: Mapped[str] = mapped_column(String(30), default="manual")
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(180), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SLAResult(Base):
    __tablename__ = "sla_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
    profile_id: Mapped[str] = mapped_column(ForeignKey("sla_profiles.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    availability: Mapped[float] = mapped_column(Numeric(8, 4), default=100)
    avg_latency_ms: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    error_rate: Mapped[float] = mapped_column(Numeric(8, 4), default=0)
    delivery_count: Mapped[int] = mapped_column(Integer, default=0)
    delivery_on_time: Mapped[int] = mapped_column(Integer, default=0)
    delivery_compliance: Mapped[float] = mapped_column(Numeric(8, 4), default=100)
    status: Mapped[str] = mapped_column(String(30), default="met", index=True)
    breach_reason: Mapped[str] = mapped_column(Text, default="")
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


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


class ContactUpdateBody(BaseModel):
    channel: str = Field(pattern="^(phone|email)$")
    target: str = Field(min_length=3, max_length=180)
    verification_code: str = Field(min_length=4, max_length=20)


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
    registered_capital: str = ""
    establishment_date: str = ""
    business_address: str = ""
    business_scope: str = ""


class InviteMemberBody(BaseModel):
    target: str
    department_id: str = ""
    department_ids: list[str] = []
    channel: str = ""


class MembershipRoleBody(BaseModel):
    role: str


class EnterpriseDepartmentBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    code: str = Field(default="", max_length=60)
    parent_id: str = ""


class MemberDepartmentBody(BaseModel):
    department_id: str = ""
    department_ids: list[str] = []


class MembershipStatusBody(BaseModel):
    action: str = Field(pattern="^(disable|enable|delete)$")


class TransferSuperAdminBody(BaseModel):
    target_membership_id: str


class NotificationSettingsBody(BaseModel):
    sms_provider: str = ""
    sms_endpoint: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_ssl: bool = False
    smtp_starttls: bool = True
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_name: str = "数据集运营服务管理平台"


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
    upstream_url: str = ""
    application_url: str = ""
    integration_api_url: str = ""
    download_limit: int = Field(default=0, ge=0)
    logo_file_id: str = ""
    price: float = 0
    pricing_strategy: str = ""
    version: str = "v1.0"
    quality_level: str = "标准"
    security_level: str = "一般"
    authorization_conditions: str = ""
    data_source_statement: str = ""
    compliance_statement: str = ""
    settlement_rule_mode: str = Field(default="global", pattern="^(global|custom)$")
    settlement_rule_id: str = ""
    settlement_rule: dict[str, Any] = Field(default_factory=dict)
    versions: list["ProductVersionBody"] = Field(default_factory=list)


class ProductVersionBody(BaseModel):
    version_code: str = Field(min_length=1, max_length=60)
    description: str = ""
    price: float = Field(default=0, ge=0)
    cost: float = Field(default=0, ge=0)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=100000)
    daily_quota: int = Field(default=10000, ge=0, le=100000000)
    monthly_quota: int = Field(default=0, ge=0, le=3000000000)
    quota_unit: str = ""
    quota_amount: int = Field(default=0, ge=0, le=3000000000)
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
    cost: float = Field(default=0, ge=0)
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
    daily_quota: int = Field(default=10000, ge=0, le=100000000)
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
    total_quota: int | None = Field(default=None, ge=0, le=3000000000)
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
    buyer_enterprise_id: str = Field(min_length=1, max_length=36)
    subscription_months: int = Field(default=1, ge=1, le=36, strict=True)
    quote_id: str = Field(default="", max_length=64)


class TransitionBody(BaseModel):
    action: str
    reason: str = ""
    refund_amount: float | None = Field(default=None, gt=0)


class DeliveryProcessBody(BaseModel):
    success: bool = True
    error: str = ""


class DeliveryAttachmentBody(BaseModel):
    file_id: str
    description: str = ""


class DevelopmentTaskUpdate(BaseModel):
    status: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    note: str = ""
    title: str | None = Field(default=None, min_length=1, max_length=240)
    acceptance: str | None = Field(default=None, max_length=4000)
    dependencies: str | None = Field(default=None, max_length=1000)


class SLAProfileBody(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    service_scope: str = Field(default="platform", pattern="^(platform|api|delivery|product)$")
    product_id: str = ""
    evaluation_period: str = Field(default="daily", pattern="^(hourly|daily|monthly)$")
    availability_target: float = Field(default=99.9, ge=0, le=100)
    latency_target_ms: int = Field(default=1000, ge=1, le=120000)
    error_rate_target: float = Field(default=1, ge=0, le=100)
    delivery_hours: int = Field(default=24, ge=1, le=8760)
    recovery_minutes: int = Field(default=60, ge=1, le=10080)
    warning_margin: float = Field(default=0.5, ge=0, le=20)
    description: str = ""
    status: str = Field(default="active", pattern="^(active|disabled)$")


class SLAProfileUpdate(SLAProfileBody):
    pass


class ServiceLevelBody(BaseModel):
    code: str = Field(min_length=2, max_length=30, pattern="^[a-z0-9_]+$")
    name: str = Field(min_length=2, max_length=80)
    description: str = ""
    customer_scope: str = Field(default="all", pattern="^(all|paid|strategic)$")
    support_days_per_week: int = Field(default=5, ge=1, le=7)
    support_hours_per_day: int = Field(default=8, ge=1, le=24)
    online_docs: bool = True
    knowledge_base: bool = True
    standard_api: bool = True
    online_customer_service: bool = True
    dedicated_manager: bool = False
    technical_support: bool = False
    initial_response_minutes: int = Field(default=2880, ge=1, le=525600)
    problem_response_hours: int = Field(default=48, ge=1, le=8760)
    quarterly_report: bool = False
    annual_optimization: bool = False
    status: str = Field(default="active", pattern="^(active|disabled)$")


class ServiceLevelAssignmentBody(BaseModel):
    enterprise_id: str
    service_level_id: str
    user_id: str = ""
    expires_at: datetime | None = None


class SettlementRuleBody(BaseModel):
    platform_rate: float = Field(default=8, ge=0, le=100)
    service_rate: float = Field(default=20, ge=0, le=100)
    expert_rate: float = Field(default=5, ge=0, le=100)
    channel_rate: float = Field(default=0, ge=0, le=100)
    tax_rate: float = Field(default=6, ge=0, le=100)


class SettlementAdjustmentBody(BaseModel):
    gross_amount: float = Field(ge=0)
    cost_amount: float = Field(ge=0)
    profit_amount: float = Field(ge=0)
    platform_rate: float = Field(ge=0, le=100)
    provider_rate: float = Field(ge=0, le=100)
    service_rate: float = Field(ge=0, le=100)
    expert_rate: float = Field(ge=0, le=100)
    channel_rate: float = Field(ge=0, le=100)
    reason: str = Field(min_length=2)


class SettlementProposalDecisionBody(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = ""


class SettlementRuleCreateBody(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    version: str = Field(default="v1", min_length=1, max_length=40)
    scope: dict[str, Any] = Field(default_factory=dict)
    platform_rate: float = Field(default=8, ge=0, le=100)
    provider_rate: float = Field(default=67, ge=0, le=100)
    service_rate: float = Field(default=20, ge=0, le=100)
    expert_rate: float = Field(default=5, ge=0, le=100)
    channel_rate: float = Field(default=0, ge=0, le=100)
    tax_rate: float = Field(default=6, ge=0, le=100)
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    change_reason: str = ""


class SettlementRuleDecisionBody(BaseModel):
    decision: str
    comment: str = ""


class SettlementMeasurementBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=1, max_length=36)
    measurement_type: str = Field(pattern="^(download|api_call|model_call|offline_delivery|training_hours|training_attendance|consulting_hours|custom_milestone)$")
    quantity: Decimal = Field(ge=0, le=Decimal("999999999999.999999"), max_digits=18, decimal_places=6, allow_inf_nan=False)
    unit: str = Field(default="count", pattern="^(count|hour|token|bytes)$")
    source: str = Field(default="manual", min_length=1, max_length=120)
    event_key: str = Field(default="", max_length=180)
    source_id: str = Field(default="", max_length=180)
    period_start: datetime | None = None
    period_end: datetime | None = None


class SettlementBatchBody(BaseModel):
    cycle: str = "manual"
    period_start: datetime | None = None
    period_end: datetime | None = None
    rule_id: str = ""
    order_ids: list[str] = Field(default_factory=list, max_length=10000)
    idempotency_key: str = Field(default="", max_length=120)
    rebuild: bool = False


class SettlementReconciliationBody(BaseModel):
    ledger_type: str = Field(pattern="^(platform|payment|bank|split)$")
    actual_amount: float
    resolution: str = ""


class SettlementActionBody(BaseModel):
    comment: str = ""


class SettlementCorrectionBody(BaseModel):
    correction_type: str = Field(pattern="^(refund|reversal|supplement|recovery)$")
    amount: float = Field(gt=0)
    reason: str = Field(min_length=2)
    source_ref: str = ""
    recovery_mode: str = Field(default="future_offset", pattern="^(original_route|future_offset|manual)$")


class ReconciliationCloseBody(BaseModel):
    resolution: str = Field(min_length=2)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    username: str | None
    email: str | None
    phone: str | None
    name: str
    verified_status: str
    activation_status: str = "active"
    platform_role: str = ""
    phone_verified: bool = False
    email_verified: bool = False


app = FastAPI(title="Market Operations API", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def audit_request_context(request: Request, call_next):
    supplied = request.headers.get("X-Request-Id", "")
    request_id = supplied if re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", supplied) else secrets.token_hex(16)
    marker = audit_request_id.set(request_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        audit_request_id.reset(marker)
security = HTTPBearer(auto_error=False)


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


audit_request_id = ContextVar("audit_request_id", default="")


def audit(db: Session, actor: str, action: str, target_type: str, target_id: str = "", detail: str = "", *, category: str = "", business_domain: str = "", tenant_id: str = "", order_id: str = "", batch_no: str = "", rule_version: str = "", request_id: str = "", risk_level: str = "normal", before: Any | None = None, after: Any | None = None, result: str = "success"):
    inferred = category or ("settlement" if target_type in {"settlement", "settlement_batch", "settlement_rule", "reconciliation"} or "settlement" in action else "payment_refund" if target_type in {"payment", "refund"} or "payment" in action or "refund" in action else "order" if target_type in {"order", "order_state"} or "order" in action else "product" if target_type in {"product", "product_review"} or "product" in action else "auth" if target_type in {"user", "membership", "identity"} or "login" in action or "register" in action else "ops")
    if order_id:
        order = db.scalar(select(Order).where(or_(Order.id == order_id, Order.order_no == order_id)))
        if order:
            order_id = order.id
            tenant_id = tenant_id or order.buyer_enterprise_id or ""
    if target_type == "settlement_proposal":
        proposal = db.get(SettlementAdjustmentProposal, target_id)
        settlement = db.get(Settlement, proposal.settlement_id) if proposal else None
        if settlement:
            order_id = order_id or settlement.order_id
            line = db.scalar(select(SettlementLine).where(SettlementLine.settlement_id == settlement.id, SettlementLine.status != "superseded"))
            batch = db.get(SettlementBatch, line.batch_id) if line else None
            batch_no = batch_no or (batch.batch_no if batch else "")
    db.add(AuditLog(actor=actor or "unknown", action=action, target_type=target_type, target_id=target_id, detail=detail, category=inferred, business_domain=business_domain, tenant_id=tenant_id, order_id=order_id, batch_no=batch_no, rule_version=rule_version, request_id=request_id or audit_request_id.get(), risk_level=risk_level, result=result, before_json=json.dumps(before or {}, ensure_ascii=False, default=str), after_json=json.dumps(after or {}, ensure_ascii=False, default=str)))
    notify_settlement_event(db, action, target_type, target_id, order_id, batch_no, after)


def notify_business_event(db: Session, title: str, content: str, target_type: str, target_id: str, *, event_key: str, category: str, tenant_id: str = "", recipient_ids: list[str] | tuple[str, ...] = (), enterprise_ids: list[str] | tuple[str, ...] = (), platform_roles: list[str] | tuple[str, ...] = (), severity: str = "important") -> None:
    recipients = set(recipient_ids)
    if enterprise_ids:
        recipients.update(db.scalars(select(User.id).join(Membership, Membership.user_id == User.id).where(
            Membership.enterprise_id.in_(enterprise_ids), Membership.status == "active",
            Membership.role.in_(["super_admin", "enterprise_admin"]),
            or_(User.platform_role == "", User.platform_role.is_(None)),
        )).all())
    if platform_roles:
        recipients.update(db.scalars(select(User.id).where(User.platform_role.in_(platform_roles))).all())
    recipients = db.scalars(select(User.id).where(User.id.in_(recipients), User.is_active.is_(True), User.activation_status == "active")).all() if recipients else []
    if not recipients:
        return
    if len(event_key) > 180:
        event_key = "business:" + hashlib.sha256(event_key.encode()).hexdigest()
    message_center["create"](db, recipients, title, content, target_type, target_id,
                             severity=severity, event_key=event_key, category=category, tenant_id=tenant_id)


def notify_order_event(db: Session, order: Order, action: str, version: str, *, target_type: str = "order", target_id: str = "") -> None:
    titles = {
        "confirm_payment": "订单支付完成", "approve_refund": "订单退款处理中", "complete_refund": "订单退款完成",
        "start_delivery": "订单开始交付", "submit_delivery": "订单待验收", "accept_delivery": "订单验收通过",
        "reject_delivery": "订单交付退回整改", "mark_exception": "订单交付异常", "retry_delivery": "订单交付重试",
        "confirm_order": "订单已完成", "delivery_succeeded": "订单待验收", "delivery_failed": "订单自动交付异常",
    }
    if action not in titles:
        return
    roles = ("super_admin", "platform_operator", "delivery_monitor") if action in {"mark_exception", "delivery_failed"} else ()
    owners = [order.buyer_user_id] if order.buyer_user_id else []
    if order.subscription_id:
        subscription = db.get(SaaSSubscription, order.subscription_id)
        if subscription and subscription.created_by:
            owner = db.scalar(select(User.id).where(or_(User.id == subscription.created_by, User.email == subscription.created_by, User.phone == subscription.created_by)))
            if owner:
                owners.append(owner)
    notify_business_event(db, titles[action], f"订单 {order.order_no}：{titles[action]}，请查看业务详情。", target_type, target_id or order.id,
                          event_key=f"business:{target_type}:{target_id or order.id}:{action}:{version}", category="order",
                          tenant_id=order.buyer_enterprise_id, recipient_ids=owners,
                          enterprise_ids=[order.buyer_enterprise_id, order.provider_enterprise_id], platform_roles=roles)


def notify_settlement_event(db: Session, action: str, target_type: str, target_id: str, order_id: str, batch_no: str, after: Any) -> None:
    titles = {
        "generate_settlement": "清算单已生成", "create_refund_negative_settlement": "退款清算单已生成",
        "generate_settlement_batch": "清算单已生成", "adjust_settlement": "清算单已调整",
        "create_settlement_adjustment_proposal": "清算调整待审核", "decide_settlement_adjustment_proposal": "清算调整审核完成",
        "lock_settlement": "清算单已锁定", "create_settlement_batch": "清算批次已生成",
        "confirm_settlement_batch": "清算批次已确认", "rollback_settlement_batch": "清算批次已回滚",
        "pay_settlement_batch": "清算批次付款完成",
        "create_settlement_correction": "清算更正待审核", "approve_settlement_correction": "清算更正已批准",
        "reject_settlement_correction": "清算更正已驳回",
    }
    if action not in titles:
        return
    db.flush()
    orders = [db.get(Order, order_id)] if order_id else []
    if target_type == "settlement_batch":
        orders = db.scalars(select(Order).join(Settlement, Settlement.order_id == Order.id).join(SettlementLine, SettlementLine.settlement_id == Settlement.id).join(SettlementBatch, SettlementBatch.id == SettlementLine.batch_id).where(SettlementBatch.batch_no == batch_no).distinct()).all()
    orders = [order for order in orders if order]
    version = str((after or {}).get("proposal_status") or (after or {}).get("status") or action)
    if action in {"adjust_settlement", "create_settlement_adjustment_proposal", "decide_settlement_adjustment_proposal"}:
        version = str((after or {}).get("proposal_id") or target_id) + ":" + now().isoformat()
    key = f"business:{target_type}:{target_id}:{action}:{version}"
    # Keep batch notifications tenant-specific; never copy audit snapshots or amounts.
    enterprises = {eid for order in orders for eid in (order.buyer_enterprise_id, order.provider_enterprise_id) if eid}
    for eid in sorted(enterprises):
        owners = [order.buyer_user_id for order in orders if order.buyer_enterprise_id == eid and order.buyer_user_id]
        notify_business_event(db, titles[action], "相关清算业务状态已更新，请查看业务详情。", target_type, target_id,
                              event_key=f"{key}:{eid}", category="settlement", tenant_id=eid, recipient_ids=owners, enterprise_ids=[eid])
    notify_business_event(db, titles[action], "清算业务状态已更新，请查看业务详情。", target_type, target_id,
                          event_key=f"{key}:platform", category="settlement", platform_roles=("super_admin", "platform_operator", "finance_settlement"))


def notify_platform_role(db: Session, role: str, title: str, content: str, target_type: str, target_id: str, *, event_key: str | None = None) -> None:
    roles = [role, "super_admin"] + (["product_manager"] if role == "business_reviewer" else [])
    tenant_id = ""
    if target_type == "product":
        product = db.get(Product, target_id)
        if not product:
            return
        tenant_id = product.enterprise_id
        if event_key is None:
            db.flush()
            event_key = f"review:product:{product.id}:{product.status}:{role}:{product.updated_at.isoformat()}"
    event_key = event_key or f"review:{target_type}:{target_id}:{role}:{now().isoformat()}"
    notify_business_event(db, title, content, target_type, target_id, event_key=event_key, category="review", tenant_id=tenant_id, platform_roles=roles)


def notification_scheduled_events(db: Session) -> dict[str, int | bool]:
    """Stage periodic notifications; the worker owns commit and rollback."""
    current = now()
    horizon = current + timedelta(days=7)
    summary: dict[str, int | bool] = {"saas_candidates": 0, "quota_candidates": 0, "redis_unavailable": False}
    subscriptions = db.scalars(select(SaaSSubscription).where(
        SaaSSubscription.status == "active", SaaSSubscription.expires_at.is_not(None), SaaSSubscription.expires_at <= horizon,
    )).all()
    for subscription in subscriptions:
        expires_at = subscription.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expired = expires_at <= current
        state = "expired" if expired else "expiring"
        owner = db.scalar(select(User.id).where(or_(User.id == subscription.created_by, User.email == subscription.created_by, User.phone == subscription.created_by))) if subscription.created_by else None
        notify_business_event(db, "SaaS 订阅已到期" if expired else "SaaS 订阅即将到期",
                              "当前订阅周期已到期，请查看续费状态。" if expired else "当前订阅将在七天内到期，请查看订阅详情。",
                              "saas_subscription", subscription.id, event_key=f"saas:{subscription.id}:{state}:{expires_at.isoformat()}",
                              category="system", tenant_id=subscription.enterprise_id, recipient_ids=[owner] if owner else [], enterprise_ids=[subscription.enterprise_id])
        summary["saas_candidates"] += 1
    credentials = db.scalars(select(ApiCredential).where(ApiCredential.status.in_(["active", "exhausted"]))).all()
    if not credentials:
        return summary
    try:
        client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://market-redis:6379/0"), decode_responses=True,
                                      socket_connect_timeout=1, socket_timeout=1)
    except (redis.RedisError, ValueError):
        summary["redis_unavailable"] = True
        return summary
    try:
        for credential in credentials:
            if not credential.apisix_consumer_name:
                continue
            if credential.expires_at:
                expires_at = credential.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= current:
                    continue
            route = db.get(ApiGatewayRoute, credential.route_id)
            if not route or route.status != "active":
                continue
            # Match k8s/market-gateway-quota.lua (UTC periods, Redis database 0).
            prefix = f"market:apisix:quota:"
            suffix = f"{route.route_key}:{credential.apisix_consumer_name}"
            day, month = current.strftime("%Y-%m-%d"), current.strftime("%Y-%m")
            try:
                metadata = client.hgetall(f"market:apisix:credential:{credential.apisix_consumer_name}")
                counts = client.mget([f"{prefix}day:{suffix}:{day}", f"{prefix}month:{suffix}:{month}", f"{prefix}total:{suffix}"])
            except redis.RedisError:
                summary["redis_unavailable"] = True
                break
            if metadata.get("status", "active") != "active":
                continue
            defaults = (credential.daily_quota if credential.daily_quota is not None else route.daily_quota,
                        credential.monthly_quota if credential.monthly_quota is not None else route.monthly_quota,
                        credential.total_quota)
            owner = None
            for kind, period, raw_count, default in zip(("day", "month", "total"), (day, month, "total"), counts, defaults):
                try:
                    limit = int(metadata.get({"day": "daily_quota", "month": "monthly_quota", "total": "total_quota"}[kind], default) or 0)
                    used = int(raw_count or 0)
                except (TypeError, ValueError):
                    continue
                # Lua rolls back rejected INCRs, so the durable exhausted count is == limit.
                if limit <= 0 or used < limit:
                    continue
                if credential.created_by and owner is None:
                    owner = db.scalar(select(User.id).where(or_(User.id == credential.created_by, User.email == credential.created_by, User.phone == credential.created_by)))
                notify_business_event(db, "API 调用额度已耗尽", {"day": "当前日调用额度已耗尽。", "month": "当前月调用额度已耗尽。", "total": "当前总调用额度已耗尽。"}[kind],
                                      "api_credential", credential.id, event_key=f"quota:{credential.id}:{kind}:{period}:{limit}", category="system",
                                      tenant_id=credential.enterprise_id, recipient_ids=[owner] if owner else [], enterprise_ids=[credential.enterprise_id])
                summary["quota_candidates"] += 1
    finally:
        try:
            client.close()
        except redis.RedisError:
            pass
    return summary


def require_product_review_role(user: User, stage: str) -> None:
    allowed = {
        "business": {"super_admin", "business_reviewer", "product_manager"},
        "quality": {"super_admin", "quality_reviewer"},
        "security": {"super_admin", "security_compliance"},
        "operation": {"super_admin", "platform_operator"},
    }
    if user.platform_role not in allowed.get(stage, set()):
        raise HTTPException(403, "当前角色无权执行该审核环节")


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security), db: Session = Depends(db_session)) -> User:
    if not credentials:
        raise HTTPException(401, "请先登录")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        user = db.get(User, payload.get("sub"))
    except (jwt.PyJWTError, TypeError):
        user = None
    if not user or not user.is_active or user.activation_status == "deleted":
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


def enterprise_management_scope(db: Session, user: User, enterprise_id: str | None = None) -> Enterprise:
    """Resolve an enterprise managed by its super-admin or enterprise admin."""
    membership = require_enterprise_admin(db, user, enterprise_id)
    enterprise = db.get(Enterprise, membership.enterprise_id)
    if not enterprise:
        raise HTTPException(404, "企业不存在")
    return enterprise


def require_platform_admin(user: User):
    if user.platform_role not in {"super_admin", "platform_operator"}:
        raise HTTPException(403, "只有系统管理员或平台运营管理员可以执行此操作")


def require_security_operator(user: User):
    if user.platform_role not in {"super_admin", "platform_operator", "security_compliance"}:
        raise HTTPException(403, "只有平台管理员或安全合规人员可以执行安全策略检查")


def require_settlement_operator(user: User):
    if user.platform_role not in {"super_admin", "platform_operator", "finance_settlement"}:
        raise HTTPException(403, "只有平台管理员、平台运营或财务清算人员可以执行清算操作")


def require_delivery_operator(user: User):
    if user.platform_role not in {"super_admin", "platform_operator", "delivery_monitor"}:
        raise HTTPException(403, "只有平台管理员、平台运营或交付监控人员可以处理交付任务")


def require_settlement_viewer(user: User):
    if user.platform_role not in {"super_admin", "platform_operator", "finance_settlement", "security_compliance"}:
        raise HTTPException(403, "只有平台运营、财务清算或安全审计人员可以查看清算数据")


def require_audit_viewer(user: User):
    if user.platform_role not in {"super_admin", "platform_operator", "finance_settlement", "security_compliance"}:
        raise HTTPException(403, "当前角色无权查看平台审计日志")


PLATFORM_ROLES = {
    "platform_operator": "平台运营人员",
    "product_manager": "数据产品经理",
    "business_reviewer": "业务审核人员",
    "quality_reviewer": "质量审核人员",
    "security_compliance": "安全合规人员",
    "delivery_monitor": "交付监控人员",
    "finance_settlement": "财务清算人员",
}

PLATFORM_ROLE_ACCOUNTS = {
    "ptyy": ("平台运营人员", "platform_operator"),
    "sjcp": ("数据产品经理", "product_manager"),
    "ywsh": ("业务审核人员", "business_reviewer"),
    "zlsh": ("质量审核人员", "quality_reviewer"),
    "aqsh": ("安全合规人员", "security_compliance"),
    "jfsh": ("交付监控人员", "delivery_monitor"),
    "cwqs": ("财务清算人员", "finance_settlement"),
}


def ensure_platform_role_accounts(db: Session):
    """Keep the seven operational roles as fixed platform accounts."""
    for username, (name, role) in PLATFORM_ROLE_ACCOUNTS.items():
        account = db.scalar(select(User).where(User.username == username))
        if not account:
            account = User(username=username, email=f"{username}@market.local", name=name, password_hash=hash_password("Admin123!"), verified_status="verified", platform_role=role, email_verified=True)
            db.add(account)
            db.flush()
        else:
            if not account.email:
                account.email = f"{username}@market.local"
            account.name = name
            account.platform_role = role
            account.is_active = True
        # Platform role accounts are not enterprise members; they manage tenants through platform scope.
        for membership in db.scalars(select(Membership).where(Membership.user_id == account.id)).all():
            db.delete(membership)
    # Platform administrators and specialist accounts are tenant-less. Repair
    # historical invitations or seed data that attached them to an enterprise.
    for account in db.scalars(select(User).where(User.platform_role != "")).all():
        for membership in db.scalars(select(Membership).where(Membership.user_id == account.id)).all():
            db.delete(membership)
    # Invitations create membership immediately. Normalize legacy rows created
    # with the old pending_activation membership state.
    for membership in db.scalars(select(Membership).where(Membership.status == "pending_activation")).all():
        invitee = db.get(User, membership.user_id)
        membership.status = "active" if invitee and invitee.is_active and invitee.activation_status != "deleted" else "expired"
    for membership in db.scalars(select(Membership).where(Membership.department_id != "")).all():
        if not db.scalar(select(MembershipDepartment.id).where(MembershipDepartment.membership_id == membership.id, MembershipDepartment.department_id == membership.department_id)):
            db.add(MembershipDepartment(membership_id=membership.id, department_id=membership.department_id))


def make_order_no() -> str:
    return "ORD-" + now().strftime("%y%m%d%H%M%S") + secrets.token_hex(2).upper()


def order_cost(db: Session, order: Order) -> Decimal:
    """Resolve the cost of the purchased product version for this order."""
    if order.snapshot_version == 1:
        if order.total_cost_snapshot is None:
            raise HTTPException(409, "订单成本快照不完整，不能清算")
        snapshot = order_economics["economic_snapshot"](order)
        return Decimal(str(snapshot["total_cost"])).quantize(Decimal("0.01"))
    product = db.get(Product, order.product_id)
    if product and product.product_type == "saas":
        version = db.get(SaaSProductVersion, order.product_version_id)
    else:
        version = db.get(ProductReleaseVersion, order.product_version_id)
    if not version:
        return Decimal("0.00")
    cost = Decimal(str(getattr(version, "cost", 0) or 0))
    if product and product.product_type == "saas":
        # SaaS version cost is the monthly cost; a subscription order carries
        # its billing cycle and therefore determines the total order cost.
        cycle_months = {"monthly": 1, "quarterly": 3, "annual": 12}.get(order.billing_cycle, 1)
        cost *= cycle_months
    return cost.quantize(Decimal("0.01"))


def settlement_values_for_order(db: Session, order: Order, fallback: SettlementRule) -> tuple[dict[str, Decimal], str]:
    if order.snapshot_version == 1:
        snapshot = order_economics["economic_snapshot"](order)
        rule = snapshot["settlement_rule"]
        return ({key: Decimal(str(rule["rates"][key])) for key in order_economics["rate_keys"]}, rule["version"])
    product = db.get(Product, order.product_id)
    if product and product.settlement_rule_mode == "custom":
        custom = json.loads(product.settlement_rule_json or "{}")
        return ({key: Decimal(str(custom.get(key, 0) or 0)) for key in ("platform_rate", "provider_rate", "service_rate", "expert_rate", "channel_rate")}, f"product:{product.id}")
    if product and product.settlement_rule_id:
        selected = db.get(SettlementRule, product.settlement_rule_id)
        if selected:
            return ({key: Decimal(str(getattr(selected, key, 0) or 0)) for key in ("platform_rate", "provider_rate", "service_rate", "expert_rate", "channel_rate")}, f"global:{selected.version}")
    return ({key: Decimal(str(getattr(fallback, key, 0) or 0)) for key in ("platform_rate", "provider_rate", "service_rate", "expert_rate", "channel_rate")}, f"global:{fallback.version}")


def create_refund_negative_settlement(db: Session, refund: Refund, actor: str = "system") -> Settlement:
    """Create one idempotent negative settlement for a completed refund."""
    existing = db.scalar(select(Settlement).where(Settlement.refund_id == refund.id).order_by(Settlement.created_at.desc()))
    if existing:
        return existing
    order = db.get(Order, refund.order_id)
    if not order:
        raise HTTPException(409, "退款关联订单不存在，无法生成负向清算单")
    original = db.scalar(select(Settlement).where(Settlement.order_id == order.id, Settlement.is_refund.is_(False), Settlement.status != "superseded").order_by(Settlement.created_at.desc()))
    refund_amount = Decimal(str(refund.amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if refund_amount <= 0:
        raise HTTPException(400, "退款金额必须大于0")
    if original:
        base_gross = max(Decimal(str(original.gross_amount or 0)), Decimal("0.01"))
        ratio = refund_amount / base_gross
        cost = -(Decimal(str(original.cost_amount or 0)) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        profit = -(Decimal(str(original.profit_amount or 0)) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        shares = {key: -(Decimal(str(getattr(original, key) or 0)) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for key in ("platform_fee", "provider_share", "service_share", "expert_fee", "channel_fee")}
        reference_id = original.id
    else:
        fallback = db.scalar(select(SettlementRule).where(SettlementRule.status == "active").order_by(SettlementRule.created_at.desc()))
        if not fallback:
            fallback = SettlementRule(version="v1", platform_rate=8, provider_rate=67, service_rate=20, expert_rate=5, channel_rate=0)
        rates, _ = settlement_values_for_order(db, order, fallback)
        gross = Decimal(str(order.paid_amount or order.amount or 0)).quantize(Decimal("0.01"))
        cost = -(order_cost(db, order) * refund_amount / max(gross, Decimal("0.01"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        profit = -(refund_amount + cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        shares = {"platform_fee": (profit * rates["platform_rate"] / 100).quantize(Decimal("0.01")), "provider_share": (profit * rates["provider_rate"] / 100).quantize(Decimal("0.01")), "service_share": (profit * rates["service_rate"] / 100).quantize(Decimal("0.01")), "expert_fee": (profit * rates["expert_rate"] / 100).quantize(Decimal("0.01")), "channel_fee": (profit * rates["channel_rate"] / 100).quantize(Decimal("0.01"))}
        reference_id = ""
    settlement = Settlement(settlement_no="REF-SET-" + secrets.token_hex(6).upper(), order_id=order.id, refund_id=refund.id, reference_settlement_id=reference_id, is_refund=True, gross_amount=-refund_amount, refund_amount=-refund_amount, net_amount=-refund_amount, cost_amount=cost, profit_amount=profit, refund_recovery=-refund_amount, platform_fee=shares["platform_fee"], provider_share=shares["provider_share"], service_share=shares["service_share"], expert_fee=shares["expert_fee"], channel_fee=shares["channel_fee"], tax_amount=Decimal("0.00"), status="pending")
    db.add(settlement)
    db.flush()
    audit(db, actor, "create_refund_negative_settlement", "settlement", settlement.settlement_no, "退款完成生成负向清算单", category="settlement_adjustment", business_domain="settlement", order_id=order.id, after={"refund_id": refund.id, "gross_amount": float(settlement.gross_amount), "cost_amount": float(cost), "profit_amount": float(profit)})
    return settlement


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
    "reject_delivery": ("delivery", "pending_acceptance", "preparing", "拒绝交付并退回整改"),
    "confirm_order": ("main", "pending_confirmation", "completed", "客户确认或自动确认"),
    "mark_exception": ("delivery", "preparing", "exception", "标记交付异常"),
    "retry_delivery": ("delivery", "exception", "preparing", "整改后重试"),
    "submit_after_sales": ("after_sales", "none", "processing", "提交售后申请"),
    "close_after_sales": ("after_sales", "processing", "closed", "售后关闭"),
    "close_order": ("main", "completed", "closed", "清算/期满"),
    "cancel_order": ("main", "cancelled", "cancelled", "取消订单"),
}

ONLINE_DELIVERY_METHODS = {"file", "object_storage", "api", "model_api", "tenant_access"}


def delivery_task_config(db: Session, order: Order) -> tuple[str, str, str]:
    product = db.get(Product, order.product_id)
    method = order_economics["delivery_snapshot"](order)["method"] if order.snapshot_version == 1 else product.delivery_method if product else "file"
    mode = "automatic" if method in ONLINE_DELIVERY_METHODS else "manual"
    return method, mode, "支付成功后进入自动交付" if mode == "automatic" else "等待交付人员处理"


def create_delivery_task(db: Session, order: Order, actor: str = "") -> DeliveryTask:
    method, mode, note = delivery_task_config(db, order)
    task = DeliveryTask(order_id=order.id, method=method, delivery_mode=mode, status="in_delivery" if mode == "automatic" else "preparing", assignee="自动交付服务" if mode == "automatic" else "运营交付团队", next_retry_at=now() if mode == "automatic" else None, sla_due_at=now() + timedelta(hours=24), note=note)
    db.add(task)
    order.delivery_status = "in_delivery" if mode == "automatic" else "preparing"
    if mode == "automatic":
        order.main_status = "fulfilling"
    if actor:
        audit(db, actor, "create_delivery_task", "delivery_task", task.id, f"mode={mode} method={method}", category="delivery", business_domain="delivery", order_id=order.id)
    return task


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
        "upstream_url": "VARCHAR(500) DEFAULT ''",
        "application_url": "VARCHAR(500) DEFAULT ''",
        "integration_api_url": "VARCHAR(500) DEFAULT ''",
        "download_limit": "INTEGER DEFAULT 0",
        "logo_file_id": "VARCHAR(36) DEFAULT ''",
        "logo_thumbnail_file_id": "VARCHAR(36) DEFAULT ''",
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
            "username": "VARCHAR(80)",
            "activation_status": "VARCHAR(30) DEFAULT 'active'",
            "phone": "VARCHAR(30)",
            "phone_verified": "BOOLEAN",
            "email_verified": "BOOLEAN",
            "email_activation_token": "VARCHAR(120)",
            "email_activation_expires_at": "TIMESTAMP WITH TIME ZONE",
            "platform_role": "VARCHAR(60)",
            "temporary_password_hash": "VARCHAR(255)",
            "temporary_password_expires_at": "TIMESTAMP WITH TIME ZONE",
        },
        "enterprises": {
            "enterprise_type": "VARCHAR(100)",
            "legal_representative": "VARCHAR(120)",
            "registered_capital": "VARCHAR(120)",
            "establishment_date": "VARCHAR(30)",
            "business_address": "VARCHAR(500)",
            "business_scope": "TEXT",
            "license_file_id": "VARCHAR(36)",
            "verified_by": "VARCHAR(180)",
            "verified_at": "TIMESTAMP WITH TIME ZONE",
        },
        "memberships": {
            "status": "VARCHAR(30)",
            "invited_by": "VARCHAR(36)",
            "joined_at": "TIMESTAMP WITH TIME ZONE",
            "department_id": "VARCHAR(36) DEFAULT ''",
        },
        "enterprise_invitations": {
            "department_id": "VARCHAR(36) DEFAULT ''",
            "department_ids_json": "TEXT DEFAULT '[]'",
            "channel": "VARCHAR(20) DEFAULT 'email'",
            "created_user": "BOOLEAN DEFAULT FALSE",
        },
        "products": {
            "review_comment": "TEXT",
            "reviewed_by": "VARCHAR(180)",
            "reviewed_at": "TIMESTAMP WITH TIME ZONE",
            "settlement_rule_mode": "VARCHAR(20) DEFAULT 'global'",
            "settlement_rule_id": "VARCHAR(36) DEFAULT ''",
            "settlement_rule_json": "TEXT DEFAULT '{}'",
        },
        "file_objects": {
            "product_id": "VARCHAR(36)",
            "version_id": "VARCHAR(36)",
            "checksum": "VARCHAR(64)",
            "file_role": "VARCHAR(50)",
            "version": "VARCHAR(30)",
            "description": "TEXT",
            "status": "VARCHAR(30)",
            "scan_status": "VARCHAR(30) DEFAULT 'not_scanned'",
            "scan_report": "TEXT DEFAULT ''",
            "scanned_at": "TIMESTAMP WITH TIME ZONE",
        },
        "file_download_logs": {
            "file_id": "VARCHAR(36)",
            "order_id": "VARCHAR(36)",
            "user_id": "VARCHAR(36)",
            "success": "BOOLEAN DEFAULT FALSE",
            "detail": "TEXT DEFAULT ''",
        },
        "settlement_measurements": {
            "event_key": "VARCHAR(180) DEFAULT '' NOT NULL",
            "source_id": "VARCHAR(180) DEFAULT '' NOT NULL",
            "actor_id": "VARCHAR(36) DEFAULT '' NOT NULL",
            "sample_kind": "VARCHAR(20) DEFAULT 'event' NOT NULL",
            "scope": "VARCHAR(30) DEFAULT 'order' NOT NULL",
            "evidence_json": "TEXT DEFAULT '{}' NOT NULL",
        },
        "settlements": {
            "refund_amount": "NUMERIC(14,2)",
            "net_amount": "NUMERIC(14,2)",
            "refund_recovery": "NUMERIC(14,2)",
            "cost_amount": "NUMERIC(14,2) DEFAULT 0",
            "profit_amount": "NUMERIC(14,2) DEFAULT 0",
            "channel_fee": "NUMERIC(14,2) DEFAULT 0",
            "refund_id": "VARCHAR(36) DEFAULT ''",
            "reference_settlement_id": "VARCHAR(36) DEFAULT ''",
            "is_refund": "BOOLEAN DEFAULT FALSE",
        },
        "payments": {
            "paid_at": "TIMESTAMP WITH TIME ZONE",
        },
        "settlement_batches": {
            "total_profit": "NUMERIC(14,2) DEFAULT 0",
            "request_digest": "VARCHAR(64) DEFAULT ''",
        },
        "product_release_versions": {
            "cost": "NUMERIC(14,2) DEFAULT 0",
            "quota_unit": "VARCHAR(20) DEFAULT ''",
            "quota_amount": "INTEGER DEFAULT 0",
        },
        "saas_product_versions": {
            "cost": "NUMERIC(14,2) DEFAULT 0",
        },
        "audit_logs": {
            "category": "VARCHAR(40) DEFAULT 'ops'",
            "business_domain": "VARCHAR(50) DEFAULT ''",
            "tenant_id": "VARCHAR(36) DEFAULT ''",
            "order_id": "VARCHAR(36) DEFAULT ''",
            "batch_no": "VARCHAR(60) DEFAULT ''",
            "rule_version": "VARCHAR(40) DEFAULT ''",
            "request_id": "VARCHAR(80) DEFAULT ''",
            "risk_level": "VARCHAR(20) DEFAULT 'normal'",
            "before_json": "TEXT DEFAULT '{}'",
            "after_json": "TEXT DEFAULT '{}'",
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
            "snapshot_version": "INTEGER DEFAULT 0",
            "subscription_months": "INTEGER DEFAULT 1",
            "unit_price_snapshot": "NUMERIC(14,2)",
            "unit_cost_snapshot": "NUMERIC(14,2)",
            "total_cost_snapshot": "NUMERIC(14,2)",
            "delivery_method_snapshot": "VARCHAR(40) DEFAULT ''",
            "economic_snapshot_json": "TEXT DEFAULT '{}'",
            "delivery_snapshot_json": "TEXT DEFAULT '{}'",
        },
        "delivery_tasks": {
            "delivery_mode": "VARCHAR(20) DEFAULT 'manual'",
            "retry_count": "INTEGER DEFAULT 0",
            "max_retries": "INTEGER DEFAULT 3",
            "last_error": "TEXT DEFAULT ''",
            "next_retry_at": "TIMESTAMP WITH TIME ZONE",
            "sla_due_at": "TIMESTAMP WITH TIME ZONE",
        },
        "after_sales_tickets": {
            "service_level_code": "VARCHAR(30) DEFAULT 'standard'",
            "response_due_at": "TIMESTAMP WITH TIME ZONE",
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
            "total_quota": "INTEGER DEFAULT 0",
            "apisix_consumer_name": "VARCHAR(180) DEFAULT ''",
        },
        "delivery_attachments": {
            "task_id": "VARCHAR(36)",
            "file_id": "VARCHAR(36)",
            "description": "TEXT DEFAULT ''",
            "uploaded_by": "VARCHAR(180) DEFAULT ''",
            "created_at": "TIMESTAMP WITH TIME ZONE",
        },
        "product_release_versions": {
            "rate_limit_per_minute": "INTEGER DEFAULT 60",
            "daily_quota": "INTEGER DEFAULT 10000",
            "monthly_quota": "INTEGER DEFAULT 0",
            "cost": "NUMERIC(14,2) DEFAULT 0",
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
        connection.execute(text("UPDATE settlement_measurements SET validation_status = 'pending', validation_message = 'Legacy manual measurement; source evidence not verified' WHERE event_key = '' AND validation_status = 'validated'"))
        connection.execute(text("UPDATE settlement_measurements SET event_key = 'legacy:' || id WHERE event_key = ''"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_settlement_measurements_event_key ON settlement_measurements(event_key)"))
        connection.execute(text("UPDATE users SET phone_verified = FALSE WHERE phone_verified IS NULL"))
        connection.execute(text("UPDATE users SET email_verified = FALSE WHERE email_verified IS NULL"))
        # Historical development payments predate paid_at. Backfill from the
        # payment row timestamps so the first monthly run remains reproducible.
        connection.execute(text("UPDATE payments SET paid_at = COALESCE(updated_at, created_at) WHERE paid_at IS NULL AND status IN ('paid', 'refunding', 'refunded')"))
        # Batch-level disputes/exceptions are retired. The underlying audit or
        # reconciliation records remain available for traceability.
        connection.execute(text("UPDATE settlement_batches SET status = 'generated' WHERE status IN ('exception', 'disputed', 'recon_exception')"))
        if engine.dialect.name != "sqlite":
            connection.execute(text("ALTER TABLE users ALTER COLUMN email DROP NOT NULL"))
        # Keep one global default rule active. Older development data may have
        # multiple active rows from before activation was made exclusive.
        connection.execute(text("UPDATE settlement_rules SET status = 'disabled' WHERE status = 'active' AND id NOT IN (SELECT id FROM settlement_rules WHERE status = 'active' ORDER BY created_at DESC LIMIT 1)"))
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_settlement_rules_one_active ON settlement_rules(status) WHERE status = 'active'"))


def repair_settlement_state_consistency():
    """Repair legacy rows created before batch payment cascaded to settlements."""
    with SessionLocal() as db:
        paid_ids = select(SettlementLine.settlement_id).join(SettlementBatch, SettlementBatch.id == SettlementLine.batch_id).where(SettlementBatch.status == "paid", SettlementLine.status != "superseded")
        db.execute(update(Settlement).where(Settlement.id.in_(paid_ids), Settlement.status != "paid").values(status="paid"))
        locked_ids = select(SettlementLine.settlement_id).join(SettlementBatch, SettlementBatch.id == SettlementLine.batch_id).where(SettlementBatch.status == "confirmed", SettlementLine.status != "superseded")
        db.execute(update(Settlement).where(Settlement.id.in_(locked_ids), Settlement.status.notin_(["paid", "superseded"])).values(status="locked"))
        db.commit()


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    ensure_product_metadata_schema()
    ensure_review_and_file_schema()
    ensure_gateway_version_schema()
    repair_settlement_state_consistency()
    with SessionLocal() as db:
        for product in db.scalars(select(Product)).all():
            if not product.versions:
                db.add(ProductReleaseVersion(product_id=product.id, version_code=product.version or "v1.0", description=product.description or "", price=product.price or 0, status="active"))
        db.commit()
        for product in db.scalars(select(Product).where(Product.product_type.in_(["api", "model", "saas"]), Product.status == "published")).all():
            client = ensure_oauth_client(db, product)
            for route in db.scalars(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id)).all():
                if not route.upstream_client_id:
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
            ("BE-020", "后端 Agent", "清算规则、版本、审批和试算模型/API", "清算", "P0", "todo", "BE-005", "规则可版本化、审批、生效，历史订单命中原版本，试算可对比", 0),
            ("BE-021", "后端 Agent", "清算资格、周期、批次和幂等", "清算", "P0", "todo", "BE-020", "自动/手动触发、周期清算，重复请求不重复分账", 0),
            ("BE-022", "后端 Agent", "计量数据采集、校验和计费明细", "清算", "P0", "todo", "BE-021", "API/模型/下载/培训/咨询/定制计量可追溯，异常阻断清算", 0),
            ("BE-023", "后端 Agent", "多参与方收益分配和分账明细", "清算", "P0", "todo", "BE-020,BE-022", "各类参与方和税费可复核，资格/账户异常可冻结", 0),
            ("BE-024", "后端 Agent", "四账对账、差异单和差异关闭", "清算", "P0", "todo", "BE-023", "平台/支付/银行/分账账可对账，差异未关闭不得付款", 0),
            ("BE-025", "后端 Agent", "参与方确认、异议、付款和归档", "清算", "P0", "todo", "BE-024", "确认、超时自动确认、异议暂停、付款流水和归档完整", 0),
            ("BE-026", "后端 Agent", "退款、冲正、追回、抵扣和清算调整", "清算", "P0", "todo", "BE-025", "原始结果不可覆盖，退款后分账/税费重算", 0),
            ("BE-027", "后端 Agent", "清算报表、明细查询和导出", "清算", "P1", "todo", "BE-025,BE-026", "多维查询、权限过滤和导出可用", 0),
            ("BE-028", "后端 Agent", "结构化审计事件和关联链", "审计", "P0", "todo", "BE-020,BE-026", "清算全过程可关联订单、规则、计量、批次、差异和付款", 0),
            ("BE-029", "后端 Agent", "审计分类、组合检索、详情、分页和导出 API", "审计", "P0", "todo", "BE-028", "分类快捷查询、组合条件、权限过滤、导出可验证", 0),
            ("FE-009", "前端 Agent", "清算规则、版本、审批和试算页面", "前端", "P0", "todo", "BE-020", "规则编辑、试算差异、审批和生效状态可操作", 0),
            ("FE-010", "前端 Agent", "清算批次、清算单确认/异议/付款页面", "前端", "P0", "todo", "BE-021,BE-025", "批次、参与方明细、确认、异议和付款状态可操作", 0),
            ("FE-011", "前端 Agent", "计量、对账差异、退款冲正和调整页面", "前端", "P0", "todo", "BE-022,BE-024,BE-026", "异常可处理关闭，调整有审批依据", 0),
            ("FE-012", "前端 Agent", "清算报表和导出页面", "前端", "P1", "todo", "BE-027", "多维筛选、汇总、明细和权限导出可用", 0),
            ("FE-013", "前端 Agent", "审计分类按钮、检索、详情时间线和导出", "前端", "P0", "todo", "BE-028,BE-029", "清算快捷按钮和订单/批次/规则检索可用", 0),
            ("OPS-011", "部署测试 Agent", "数据库迁移、索引、备份和任务初始化", "部署", "P0", "todo", "BE-020,BE-029", "迁移可重复，关键索引生效，任务出现在控制台", 0),
            ("QA-006", "部署测试 Agent", "清算端到端、四账、幂等、退款和权限测试", "测试", "P0", "todo", "BE-020..BE-027,FE-009..FE-012", "正常、异常、重试、重复和跨租户场景通过", 0),
            ("QA-007", "部署测试 Agent", "审计分类、检索、导出、安全和性能测试", "测试", "P0", "todo", "BE-028,BE-029,FE-013", "查询准确、越权拒绝、导出留痕、分页性能达标", 0),
            ("ARC-006", "主 Agent", "清算与审计专项集成门禁和验收报告", "架构", "P1", "todo", "BE-020..QA-007", "需求、接口、数据、页面、部署和测试闭环归档", 0),
            ("ARC-007", "主 Agent", "平台服务与 SLA 保障需求、指标口径、数据模型和验收标准", "SLA 架构", "P0", "done", "已确认 SLA 规则、指标来源、权限和处置边界", "需求文档、接口边界和验收标准固化", 100),
            ("BE-030", "后端 Agent", "SLA 规则、产品绑定、考核结果和审计 API", "SLA 后端", "P0", "done", "ARC-007", "规则 CRUD、指标计算、结果查询和审计可用", 100),
            ("FE-014", "前端 Agent", "SLA 规则配置、运行考核、违约结果和报告页面", "SLA 前端", "P0", "done", "BE-030", "运营人员可配置规则、执行考核并查看结果", 100),
            ("OPS-012", "部署测试 Agent", "SLA 数据库迁移、监控接入和端到端验证", "SLA 部署", "P0", "done", "BE-030,FE-014", "K8S 部署成功，正常/预警/违约场景验证通过", 100),
            ("BE-031", "后端 Agent", "企业部门、邀请、成员角色和部门归属接口", "用户与企业", "P0", "done", "BE-007", "部门 CRUD、邀请接受、角色调整、成员部门调整和审计可用", 100),
            ("FE-015", "前端 Agent", "企业成员、部门、邀请和角色管理页面", "用户与企业", "P0", "done", "BE-031", "企业管理员可邀请用户、维护部门和调整成员角色/部门", 100),
            ("QA-008", "部署测试 Agent", "企业组织权限、邀请和部门端到端验证", "用户与企业", "P0", "done", "BE-031,FE-015", "普通成员越权拒绝，管理员流程和删除保护通过", 100),
            ("SEC-001", "后端 Agent", "文件格式、MIME、10GB大小和压缩包安全校验", "文件安全", "P0", "in_progress", "BE-002", "非法格式、超限文件和压缩炸弹被拒绝", 10),
            ("SEC-002", "部署测试 Agent", "ClamAV服务及病毒库持久化部署", "文件安全", "P0", "todo", "SEC-001", "ClamAV在Kubernetes中Ready，病毒库可持久化", 0),
            ("SEC-003", "部署测试 Agent", "ClamAV病毒库定时更新和版本监控", "文件安全", "P0", "todo", "SEC-002", "定时更新失败可告警，版本可查询", 0),
            ("SEC-004", "后端 Agent", "文件ClamAV扫描接口和扫描报告", "文件安全", "P0", "todo", "SEC-002", "上传文件得到扫描状态、报告和病毒命中结果", 0),
            ("SEC-005", "后端 Agent", "文件Presidio内容扫描和安全审核报告", "文件安全", "P0", "todo", "SEC-004", "可解析内容完成敏感信息扫描，报告可供审核人员查看", 0),
            ("PROD-001", "后端 Agent", "产品Logo上传、精确尺寸校验、SVG清洗和缩略图", "产品登记", "P0", "todo", "BE-002", "产品Logo可持久化，恶意SVG被拒绝，缩略图可展示", 0),
            ("PROD-002", "后端 Agent", "产品版本文件绑定和已发布版本不可覆盖", "产品登记", "P0", "todo", "SEC-001,PROD-001", "新版本使用新文件，历史订单文件不被覆盖", 0),
            ("PROD-003", "后端 Agent", "产品审核撤回、原因和权限控制", "产品审核", "P0", "todo", "BE-002", "企业管理员仅能撤回本企业待审核产品，撤回留痕", 0),
            ("API-001", "后端 Agent", "API/模型API额度单位和订单独立授权", "API交付", "P0", "todo", "BE-009", "千次/万次转换为整数，订单额度独立扣减", 0),
            ("API-002", "后端 Agent", "取消、退款、额度耗尽后的网关访问回收", "API交付", "P0", "todo", "API-001", "取消/退款/耗尽后凭据和路由访问不可用", 0),
            ("SAAS-001", "后端 Agent", "SaaS按购买日对应日期计算月付到期时间", "SaaS订阅", "P0", "in_progress", "BE-007", "1月31日、闰年2月、大小月和提前续费测试通过", 10),
            ("SAAS-002", "后端 Agent", "SaaS订单独立租户标识和多租户订阅隔离", "SaaS订阅", "P0", "todo", "SAAS-001", "同企业多个租户、版本和订单互不串用", 0),
            ("DELIVERY-001", "后端 Agent", "线下履约状态、附件、拒绝和审计闭环", "线下交付", "P0", "todo", "BE-004", "支付、履约、服务方提交、购买方审核状态完整", 0),
            ("FE-016", "前端 Agent", "登记弹框按交付方式动态展示字段和上传控件", "产品登记", "P0", "todo", "PROD-001,API-001,SAAS-001", "文件/API/SaaS/线下字段按条件展示并校验", 0),
            ("FE-017", "前端 Agent", "扫描报告、Logo、下载次数和额度展示", "产品与安全", "P0", "todo", "SEC-005,PROD-001,API-001", "审核人员可查看报告，订单用户可查看额度", 0),
            ("OPS-013", "部署测试 Agent", "数据库迁移、ClamAV、定时更新和监控告警", "文件安全部署", "P0", "todo", "SEC-002,SEC-003", "K8S部署成功，健康检查、更新和告警可验证", 0),
            ("QA-009", "部署测试 Agent", "文件安全、交付、额度、SaaS计费端到端验证", "专项测试", "P0", "todo", "SEC-005,API-002,SAAS-002,DELIVERY-001", "正常、异常、权限、退款和边界日期测试通过", 0),
            ("PROD-004", "后端 Agent", "产品文件支持 RAR/7z 压缩格式", "产品登记", "P0", "todo", "SEC-001", "RAR、7z 扩展名与 MIME 校验通过，非法格式仍被拒绝", 0),
            ("REVIEW-001", "后端 Agent", "产品业务/质量/安全/运营四阶段审核状态机", "产品审核", "P0", "todo", "PROD-003,SEC-005", "四阶段按顺序流转，审核角色隔离，拒绝必须填写原因", 0),
            ("REVIEW-002", "后端 Agent", "审核阶段内部通知与审计日志", "产品审核", "P0", "todo", "REVIEW-001", "进入下一审核阶段自动通知对应角色，每个审批动作形成审计记录", 0),
            ("REVIEW-003", "前端 Agent", "审核人员产品详情、撤回和审核操作界面", "产品审核", "P0", "todo", "REVIEW-001", "企业管理员仅可撤回，平台审核人员仅可审核，拒绝弹窗强制填写原因", 0),
            ("SEC-006", "后端 Agent", "病毒扫描报告 PDF 生成、查看和下载", "文件安全", "P0", "todo", "SEC-004", "报告包含文件名、大小、工具及版本、结果、结论和结论日期", 0),
            ("SEC-007", "后端 Agent", "数据集文件 ClamAV 后逐文件 Presidio 扫描", "文件安全", "P0", "todo", "SEC-004,SEC-005", "病毒扫描通过后逐文件执行 Presidio，形成可下载结构化报告", 0),
            ("FE-018", "前端 Agent", "扫描报告链接、Logo 单组展示和审核详情优化", "产品审核", "P0", "todo", "SEC-006,SEC-007", "每个文件只展示一组扫描结果，报告文字可查看和下载 PDF", 0),
            ("QA-010", "部署测试 Agent", "产品四阶段审核、通知、报告和权限端到端验证", "专项测试", "P0", "todo", "REVIEW-001,REVIEW-002,REVIEW-003,SEC-006,SEC-007,FE-018", "四角色流程、拒绝原因、通知、审计、报告下载和越权测试通过", 0),
        ]
        for task in followup_tasks:
            if not db.scalar(select(DevelopmentTask.id).where(DevelopmentTask.code == task[0])):
                db.add(DevelopmentTask(code=task[0], owner=task[1], title=task[2], area=task[3], priority=task[4], status=task[5], dependencies=task[6], acceptance=task[7], progress=task[8]))
        if not db.scalar(select(SLAProfile.id).limit(1)):
            db.add(SLAProfile(name="平台默认服务等级", service_scope="platform", evaluation_period="daily", availability_target=99.9, latency_target_ms=1000, error_rate_target=1, delivery_hours=24, recovery_minutes=60, warning_margin=0.5, description="覆盖平台 API 可用性、调用质量和交付及时率的默认规则", status="active", created_by="system"))
        service_level_seeds = [
            ("standard", "标准级", "面向所有注册用户，提供在线文档、知识库、标准 API、5×8 在线客服和 48 小时问题响应。", "all", 5, 8, 2880, 48, False, False, False),
            ("enterprise", "企业级", "面向付费客户，在标准级基础上提供专属技术支持经理、7×8 技术支持和 24 小时问题响应。", "paid", 7, 8, 1440, 24, True, False, False),
            ("strategic", "战略级", "面向核心合作伙伴和重要客户，提供 7×24 专属支持、15 分钟紧急响应、季度报告和年度优化建议。", "strategic", 7, 24, 15, 24, True, True, True),
        ]
        for code, name, description, scope, days, hours, response_minutes, response_hours, manager, quarterly, annual in service_level_seeds:
            if not db.scalar(select(ServiceLevel.id).where(ServiceLevel.code == code)):
                db.add(ServiceLevel(code=code, name=name, description=description, customer_scope=scope, support_days_per_week=days, support_hours_per_day=hours, online_docs=True, knowledge_base=True, standard_api=True, online_customer_service=True, dedicated_manager=manager, technical_support=code != "standard", initial_response_minutes=response_minutes, problem_response_hours=response_hours, quarterly_report=quarterly, annual_optimization=annual, status="active", created_by="system"))
        db.flush()
        standard_level = db.scalar(select(ServiceLevel).where(ServiceLevel.code == "standard"))
        if standard_level:
            for enterprise in db.scalars(select(Enterprise)).all():
                if not db.scalar(select(ServiceLevelAssignment.id).where(ServiceLevelAssignment.enterprise_id == enterprise.id, ServiceLevelAssignment.user_id == "")):
                    db.add(ServiceLevelAssignment(service_level_id=standard_level.id, enterprise_id=enterprise.id, source="default", created_by="system"))
        active_wave = {"BE-020": 25, "BE-028": 15, "BE-029": 10, "FE-013": 5, "OPS-011": 5}
        for code, progress in active_wave.items():
            task = db.scalar(select(DevelopmentTask).where(DevelopmentTask.code == code))
            if task and task.status == "todo":
                task.status = "in_progress"
                task.progress = progress
        db.commit()
        admin = db.scalar(select(User).where(User.email == "admin@market.local"))
        if admin:
            if not admin.platform_role:
                admin.platform_role = "super_admin"
            for membership in db.scalars(select(Membership).where(Membership.user_id == admin.id)).all():
                db.delete(membership)
            ensure_platform_role_accounts(db)
            db.commit()
            return
        admin = User(email="admin@market.local", name="平台管理员", password_hash=hash_password("Admin123!"), verified_status="verified", platform_role="super_admin", email_verified=True)
        enterprise = Enterprise(name="天地奔牛示范企业", credit_code="DEMO-20261004", verification_status="verified")
        db.add_all([admin, enterprise])
        db.flush()
        ensure_platform_role_accounts(db)
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
    user = db.scalar(select(User).where(or_(User.username == identifier, User.email == identifier, User.phone == identifier)))
    if user and user.activation_status == "pending_activation" and user.temporary_password_expires_at and user.temporary_password_expires_at <= now():
        user.activation_status = "expired"
        user.is_active = False
        db.commit()
        raise HTTPException(401, "邀请已过期，请联系企业管理员重新邀请")
    valid_password = user and verify_password(body.password, user.password_hash)
    if user and not valid_password and user.temporary_password_hash and user.temporary_password_expires_at and user.temporary_password_expires_at > now():
        valid_password = verify_password(body.password, user.temporary_password_hash)
    if not user or not valid_password:
        raise HTTPException(401, "邮箱或密码错误")
    if user.email and user.activation_status != "pending_activation" and not user.phone_verified and not user.email_verified:
        raise HTTPException(403, "邮箱尚未激活，请先点击激活链接后再登录")
    if user.activation_status == "pending_activation":
        user.activation_status = "active"
        user.temporary_password_hash = ""
        user.temporary_password_expires_at = None
        memberships = db.scalars(select(Membership).where(Membership.user_id == user.id, Membership.status == "pending_activation")).all()
        for membership in memberships:
            membership.status = "active"
            membership.joined_at = now()
        invitations = db.scalars(select(EnterpriseInvitation).where(EnterpriseInvitation.invitee_id == user.id, EnterpriseInvitation.created_user.is_(True), EnterpriseInvitation.status == "pending")).all()
        for invitation in invitations:
            invitation.status = "accepted"
    audit(db, user.email, "login", "user", user.id)
    db.commit()
    return {"token": issue_token(user), "user": UserOut.model_validate(user).model_dump()}


@app.post("/api/auth/send-code")
def send_verification_code(body: CodeBody, db: Session = Depends(db_session)):
    if body.channel not in {"sms", "email"}:
        raise HTTPException(400, "验证码渠道必须是 sms 或 email")
    if not body.target.strip():
        raise HTTPException(400, "验证码目标不能为空")
    item = VerificationCode(channel=body.channel, target=body.target.strip(), code="123456", purpose=body.purpose, expires_at=now() + timedelta(minutes=10))
    db.add(item)
    db.commit()
    return {"channel": body.channel, "target": body.target, "expires_at": item.expires_at, "provider_configured": False, "development_hint": "当前为开发环境，验证码固定为 123456，有效期 10 分钟，未执行真实短信或邮件发送"}


def send_activation_email(db: Session, recipient: str, activation_url: str) -> tuple[bool, str]:
    values = {item.setting_key: item.setting_value for item in db.scalars(select(SystemSetting)).all()}
    host = values.get("smtp_host", "").strip()
    username = values.get("smtp_username", "").strip()
    password = values.get("smtp_password", "")
    if not host or not username or not password:
        return False, "SMTP 发件配置未完成"
    try:
        message = EmailMessage()
        message["Subject"] = "请激活您的数据集运营服务管理平台账户"
        message["From"] = f"{values.get('smtp_from_name', '数据集运营服务管理平台')} <{username}>"
        message["To"] = recipient
        message.set_content(f"您好，\n\n请在 10 分钟内点击以下链接激活账户：\n{activation_url}\n\n开发环境邮箱验证码：123456\n\n如非本人操作，请忽略此邮件。")
        message.add_alternative(f"""<html><body style=\"font-family:Arial,'Microsoft YaHei',sans-serif;color:#243044;line-height:1.7\"><h2>激活数据集运营服务管理平台账户</h2><p>您好，欢迎注册数据集运营服务管理平台。</p><p>请在 <strong>10 分钟</strong>内点击下方按钮完成邮箱激活：</p><p><a href=\"{activation_url}\" style=\"display:inline-block;padding:10px 18px;background:#2563eb;color:#fff;text-decoration:none;border-radius:6px\">激活账户</a></p><p>如果按钮无法打开，请复制以下地址：</p><p>{activation_url}</p><p style=\"color:#6b7280\">开发环境邮箱验证码：123456。如非本人操作，请忽略此邮件。</p></body></html>""", subtype="html")
        use_ssl = values.get("smtp_ssl", "false") == "true"
        use_starttls = values.get("smtp_starttls", "true") == "true"
        port = int(values.get("smtp_port", "587"))
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                if use_starttls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(username, password)
                server.send_message(message)
        return True, ""
    except Exception as exc:
        return False, str(exc)[:240]


def send_invitation_email(db: Session, recipient: str, enterprise_name: str, login_username: str, temp_password: str, expires_at: datetime, platform_url: str) -> tuple[bool, str]:
    values = {item.setting_key: item.setting_value for item in db.scalars(select(SystemSetting)).all()}
    host = values.get("smtp_host", "").strip()
    username = values.get("smtp_username", "").strip()
    password = values.get("smtp_password", "")
    if not host or not username or not password:
        return False, "SMTP 发件配置未完成"
    try:
        message = EmailMessage()
        message["Subject"] = f"您已被邀请加入企业：{enterprise_name}"
        message["From"] = f"{values.get('smtp_from_name', '数据集运营服务管理平台')} <{username}>"
        message["To"] = recipient
        message.set_content(f"您已被邀请加入企业 {enterprise_name}。\n\n平台地址：{platform_url}\n用户名：{login_username}\n临时密码：{temp_password}\n\n请在 {expires_at.strftime('%Y-%m-%d %H:%M:%S')} 前登录，首次登录后账号自动激活。")
        message.add_alternative(f"""<html><body style=\"font-family:Arial,'Microsoft YaHei',sans-serif;color:#243044;line-height:1.7"><h2>您已被邀请加入企业：{enterprise_name}</h2><p>请使用以下信息登录数据集运营服务管理平台：</p><p><strong>平台地址：</strong>{platform_url}<br><strong>用户名：</strong>{login_username}<br><strong>临时密码：</strong>{temp_password}</p><p>请在 <strong>{expires_at.strftime('%Y-%m-%d %H:%M:%S')}</strong> 前完成首次登录。首次登录后账号将自动激活，并进入实名认证流程。</p><p style=\"color:#6b7280\">如非本人操作，请忽略此邮件。</p></body></html>""", subtype="html")
        port = int(values.get("smtp_port", "587"))
        use_ssl = values.get("smtp_ssl", "false") == "true"
        use_starttls = values.get("smtp_starttls", "true") == "true"
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=15) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                if use_starttls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(username, password)
                server.send_message(message)
        return True, ""
    except Exception as exc:
        return False, str(exc)[:240]


@app.post("/api/auth/register")
def register(body: RegisterBody, request: Request, db: Session = Depends(db_session)):
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
    activation_token = secrets.token_urlsafe(36) if email and not phone else ""
    user = User(email=email, phone=phone, name=body.name, password_hash=hash_password(body.password), phone_verified=bool(phone), email_verified=False, email_activation_token=activation_token, email_activation_expires_at=now() + timedelta(minutes=10) if activation_token else None)
    db.add(user)
    if activation_token:
        db.add(VerificationCode(channel="email", target=email, code="123456", purpose="email_activation", expires_at=now() + timedelta(minutes=10)))
    db.commit()
    db.refresh(user)
    if activation_token:
        public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/") or str(request.base_url).rstrip("/")
        activation_url = f"{public_base_url}/api/auth/activate-email?token={quote(activation_token)}"
        email_sent, mail_error = send_activation_email(db, email, activation_url)
        return {"activation_required": True, "activation_url": activation_url, "email_sent": email_sent, "user": UserOut.model_validate(user).model_dump(), "development_hint": f"{'激活邮件已发送，请在 10 分钟内点击邮件中的链接' if email_sent else f'当前未能发送激活邮件（{mail_error}），请在 10 分钟内使用开发环境激活链接：{activation_url}；验证码为 123456'}"}
    return {"token": issue_token(user), "user": UserOut.model_validate(user).model_dump()}


@app.get("/api/auth/activate-email")
def activate_email(token: str, db: Session = Depends(db_session)):
    user = db.scalar(select(User).where(User.email_activation_token == token))
    if not user or not user.email_activation_expires_at or user.email_activation_expires_at <= now():
        raise HTTPException(400, "邮箱激活链接无效或已过期，请重新注册")
    user.email_verified = True
    user.email_activation_token = ""
    user.email_activation_expires_at = None
    audit(db, user.email or user.username or user.id, "activate_email", "user", user.id, "邮箱激活")
    db.commit()
    return {"status": "activated", "message": "邮箱已激活，现在可以登录平台并进行实名认证"}


def verify_contact_code(target: str, code: str, db: Session) -> None:
    item = db.scalar(select(VerificationCode).where(VerificationCode.target == target, VerificationCode.code == code, VerificationCode.used_at.is_(None), VerificationCode.expires_at > now()).order_by(VerificationCode.created_at.desc()))
    if code != "123456" and not item:
        raise HTTPException(400, "验证码错误或已过期")
    if item:
        item.used_at = now()


@app.post("/api/auth/profile/contact")
def update_profile_contact(body: ContactUpdateBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    target = body.target.strip().lower() if body.channel == "email" else body.target.strip()
    verify_contact_code(target, body.verification_code, db)
    if body.channel == "phone":
        existing = db.scalar(select(User).where(User.phone == target, User.id != user.id))
        if existing:
            raise HTTPException(409, "该手机号已绑定其他用户")
        user.phone = target
        user.phone_verified = True
    else:
        existing = db.scalar(select(User).where(User.email == target, User.id != user.id))
        if existing:
            raise HTTPException(409, "该邮箱已绑定其他用户")
        user.email = target
        user.email_verified = True
    audit(db, user.email or user.phone or user.username or user.id, "update_profile_contact", "user", user.id, body.channel)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user).model_dump()


@app.get("/api/auth/me")
def me(user: User = Depends(current_user), db: Session = Depends(db_session)):
    membership = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.status == "active").order_by(Membership.created_at))
    enterprise = db.get(Enterprise, membership.enterprise_id) if membership else None
    return {"user": UserOut.model_validate(user).model_dump(), "enterprise": {"id": enterprise.id, "name": enterprise.name} if enterprise else None, "role": membership.role if membership else "member", "business_roles": (membership.business_roles.split(",") if membership else [])}


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
    for file_id in (body.id_front_file_id, body.id_back_file_id):
        file_item = db.get(FileObject, file_id)
        if not file_item or file_item.owner_id != user.id:
            raise HTTPException(400, "身份证图片不存在或不属于当前用户")
    item = IdentityVerification(user_id=user.id, id_name=body.id_name, id_number=body.id_number, id_front_file_id=body.id_front_file_id, id_back_file_id=body.id_back_file_id, phone=body.phone, enterprise_id=body.enterprise_id, enterprise_role=body.enterprise_role, status="pending_review")
    user.verified_status = "pending_review"
    db.add(item)
    db.flush()
    notify_business_event(db, "个人实名认证待审核", "有新的个人实名认证申请待审核。", "identity_verification", item.id,
                          event_key=f"identity:{item.id}:pending_review:{item.created_at.isoformat()}", category="review",
                          platform_roles=("super_admin", "platform_operator"))
    audit(db, user.email or user.phone or user.id, "submit_personal_verification", "identity_verification", item.id)
    db.commit()
    db.refresh(item)
    return personal_verification_out(item)


@app.put("/api/verification/personal/{verification_id}")
def update_personal_verification(verification_id: str, body: PersonalVerificationBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    item = db.get(IdentityVerification, verification_id)
    if not item or item.user_id != user.id:
        raise HTTPException(404, "实名认证申请不存在")
    if body.phone != user.phone:
        raise HTTPException(400, "实名认证手机号必须与注册手机号一致")
    if body.phone_code != "123456":
        raise HTTPException(400, "请先完成手机验证码验证")
    for file_id in (body.id_front_file_id, body.id_back_file_id):
        file_item = db.get(FileObject, file_id)
        if not file_item or file_item.owner_id != user.id:
            raise HTTPException(400, "身份证图片不存在或不属于当前用户")
    item.id_name = body.id_name
    if "*" not in body.id_number:
        item.id_number = body.id_number
    item.id_front_file_id = body.id_front_file_id
    item.id_back_file_id = body.id_back_file_id
    item.phone = body.phone
    item.enterprise_id = body.enterprise_id
    item.enterprise_role = body.enterprise_role
    item.status = "pending_review"
    item.review_comment = ""
    item.reviewed_by = ""
    item.reviewed_at = None
    user.verified_status = "pending_review"
    notify_business_event(db, "个人实名认证待审核", "个人实名认证申请已重新提交，待审核。", "identity_verification", item.id,
                          event_key=f"identity:{item.id}:pending_review:{now().isoformat()}", category="review",
                          platform_roles=("super_admin", "platform_operator"))
    audit(db, user.email or user.phone or user.id, "resubmit_personal_verification", "identity_verification", item.id)
    db.commit()
    db.refresh(item)
    return personal_verification_out(item)


@app.get("/api/admin/verifications/personal")
def personal_verifications(user: User = Depends(current_user), db: Session = Depends(db_session)):
    if user.platform_role in {"super_admin", "platform_operator"}:
        items = db.scalars(select(IdentityVerification).order_by(IdentityVerification.created_at.desc())).all()
    else:
        items = db.scalars(select(IdentityVerification).where(IdentityVerification.user_id == user.id).order_by(IdentityVerification.created_at.desc())).all()
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
    notify_business_event(db, "个人实名认证审核结果", "个人实名认证审核已通过。" if item.status == "verified" else "个人实名认证审核未通过，请查看申请详情。", "identity_verification", item.id,
                          event_key=f"identity:{item.id}:{item.status}:{item.reviewed_at.isoformat()}", category="review", recipient_ids=[item.user_id])
    audit(db, user.email or user.phone or user.id, "review_personal_verification", "identity_verification", item.id, item.review_comment)
    db.commit()
    return personal_verification_out(item)


@app.post("/api/verification/enterprise")
def submit_enterprise_verification(body: EnterpriseVerificationBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    if user.verified_status != "verified":
        raise HTTPException(403, "请先完成个人实名认证")
    license_file = db.get(FileObject, body.license_file_id)
    if not license_file or license_file.owner_id != user.id:
        raise HTTPException(400, "营业执照文件不存在或不属于当前用户")
    existing = db.scalar(select(Enterprise).where(Enterprise.credit_code == body.credit_code))
    if existing:
        membership = db.scalar(select(Membership).where(Membership.enterprise_id == existing.id, Membership.user_id == user.id, Membership.status == "active"))
        if existing.verification_status != "rejected" or not membership or membership.role != "super_admin":
            raise HTTPException(409, "统一社会信用代码已存在")
        enterprise = existing
        enterprise.name = body.enterprise_name
        enterprise.enterprise_type = body.enterprise_type
        enterprise.legal_representative = body.legal_representative
        enterprise.registered_capital = body.registered_capital
        enterprise.establishment_date = body.establishment_date
        enterprise.business_address = body.business_address
        enterprise.business_scope = body.business_scope
        enterprise.license_file_id = body.license_file_id
        enterprise.verification_status = "pending_review"
        enterprise.verified_by = ""
        enterprise.verified_at = None
    else:
        enterprise = Enterprise(name=body.enterprise_name, credit_code=body.credit_code, enterprise_type=body.enterprise_type, legal_representative=body.legal_representative, registered_capital=body.registered_capital, establishment_date=body.establishment_date, business_address=body.business_address, business_scope=body.business_scope, license_file_id=body.license_file_id, verification_status="pending_review")
        db.add(enterprise)
        db.flush()
        db.add(Membership(user_id=user.id, enterprise_id=enterprise.id, role="super_admin", business_roles="provider,user,service_provider", status="active", joined_at=now()))
    db.flush()
    notify_business_event(db, "企业实名认证待审核", "有新的企业实名认证申请待审核。", "enterprise", enterprise.id,
                          event_key=f"enterprise:{enterprise.id}:pending_review:{now().isoformat()}", category="review", tenant_id=enterprise.id,
                          platform_roles=("super_admin", "platform_operator"))
    audit(db, user.email or user.phone or user.id, "submit_enterprise_verification", "enterprise", enterprise.id, enterprise.name, after={"applicant_user_id": user.id})
    db.commit()
    return {"id": enterprise.id, "name": enterprise.name, "credit_code": enterprise.credit_code, "enterprise_type": enterprise.enterprise_type, "legal_representative": enterprise.legal_representative, "registered_capital": enterprise.registered_capital, "establishment_date": enterprise.establishment_date, "business_address": enterprise.business_address, "business_scope": enterprise.business_scope, "verification_status": enterprise.verification_status}


@app.get("/api/verification/enterprise/me")
def enterprise_verification_me(user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = first_enterprise(db, user)
    return {"id": enterprise.id, "name": enterprise.name, "credit_code": enterprise.credit_code, "enterprise_type": enterprise.enterprise_type, "legal_representative": enterprise.legal_representative, "registered_capital": enterprise.registered_capital, "establishment_date": enterprise.establishment_date, "business_address": enterprise.business_address, "business_scope": enterprise.business_scope, "verification_status": enterprise.verification_status, "license_file_id": enterprise.license_file_id}


@app.get("/api/admin/verifications/enterprise")
def enterprise_verifications(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    items = db.scalars(select(Enterprise).order_by(Enterprise.created_at.desc())).all()
    return {"items": [{"id": x.id, "name": x.name, "credit_code": x.credit_code, "enterprise_type": x.enterprise_type, "legal_representative": x.legal_representative, "registered_capital": x.registered_capital, "establishment_date": x.establishment_date, "business_address": x.business_address, "business_scope": x.business_scope, "license_file_id": x.license_file_id, "verification_status": x.verification_status, "verified_by": x.verified_by, "verified_at": x.verified_at, "created_at": x.created_at} for x in items]}


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
    submission = db.scalar(select(AuditLog).where(AuditLog.action == "submit_enterprise_verification", AuditLog.target_id == enterprise.id).order_by(AuditLog.created_at.desc()))
    applicant_id = json.loads(submission.after_json or "{}").get("applicant_user_id", "") if submission else ""
    notify_business_event(db, "企业实名认证审核结果", "企业实名认证审核已通过。" if enterprise.verification_status == "verified" else "企业实名认证审核未通过，请查看申请详情。", "enterprise", enterprise.id,
                          event_key=f"enterprise:{enterprise.id}:{enterprise.verification_status}:{enterprise.verified_at.isoformat()}", category="review", tenant_id=enterprise.id,
                          recipient_ids=[applicant_id] if applicant_id else [], enterprise_ids=[enterprise.id])
    audit(db, user.email or user.phone or user.id, "review_enterprise_verification", "enterprise", enterprise.id, body.comment)
    db.commit()
    return {"id": enterprise.id, "verification_status": enterprise.verification_status, "verified_by": enterprise.verified_by, "verified_at": enterprise.verified_at}


@app.post("/api/enterprise/invitations")
def invite_member(body: InviteMemberBody, request: Request, enterprise_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = enterprise_management_scope(db, user, enterprise_id)
    department_ids = list(dict.fromkeys(body.department_ids or ([body.department_id] if body.department_id else [])))
    if department_ids:
        valid_department_ids = set(db.scalars(select(EnterpriseDepartment.id).where(EnterpriseDepartment.id.in_(department_ids), EnterpriseDepartment.enterprise_id == enterprise.id, EnterpriseDepartment.status == "active")).all())
        if valid_department_ids != set(department_ids):
            raise HTTPException(404, "指定部门不存在")
    target = body.target.strip().lower()
    invitee = db.scalar(select(User).where(or_(User.email == target, User.phone == target)))
    channel = body.channel if body.channel in {"sms", "email"} else ("email" if "@" in target else "sms")
    created_user = False
    temp_password = ""
    if not invitee:
        temp_password = secrets.token_urlsafe(9)
        if channel == "email":
            invitee = User(email=target, name=target.split("@", 1)[0], password_hash=hash_password(temp_password), email_verified=True, activation_status="pending_activation", temporary_password_hash=hash_password(temp_password), temporary_password_expires_at=now() + timedelta(days=1))
        else:
            invitee = User(phone=target, name=target, password_hash=hash_password(temp_password), phone_verified=True, activation_status="pending_activation", temporary_password_hash=hash_password(temp_password), temporary_password_expires_at=now() + timedelta(days=1))
        db.add(invitee)
        db.flush()
        created_user = True
    if invitee.platform_role:
        raise HTTPException(403, "平台角色账号不属于任何企业，不能被邀请加入企业")
    existing = db.scalar(select(Membership).where(Membership.user_id == invitee.id, Membership.enterprise_id == enterprise.id).order_by(Membership.created_at.desc()))
    if existing and existing.status == "active":
        raise HTTPException(409, "用户已经加入该企业")
    if invitee.activation_status == "deleted" or not invitee.is_active:
        raise HTTPException(400, "用户已被禁用或删除，不能邀请加入企业")
    if created_user:
        # Newly created invitees are associated with the enterprise immediately,
        # but remain pending activation until their first login.
        if existing:
            existing.role = "member"
            existing.business_roles = existing.business_roles or "provider,user"
            existing.status = "pending_activation"
            existing.invited_by = user.id
            existing.joined_at = None
        else:
            existing = Membership(user_id=invitee.id, enterprise_id=enterprise.id, role="member", department_id="", business_roles="provider,user", status="pending_activation", invited_by=user.id)
            db.add(existing)
            db.flush()
        replace_membership_departments(db, existing, department_ids)
    elif existing and existing.status == "pending_activation":
        # Preserve compatibility with invitations created before the explicit
        # accept/reject flow was introduced.
        replace_membership_departments(db, existing, department_ids)
    invitation = EnterpriseInvitation(enterprise_id=enterprise.id, inviter_id=user.id, invitee_id=invitee.id, target=target, department_id=department_ids[0] if department_ids else "", department_ids_json=json.dumps(department_ids, ensure_ascii=False), channel=channel, created_user=created_user, token=secrets.token_urlsafe(24), expires_at=now() + timedelta(days=1))
    db.add(invitation)
    audit(db, user.email or user.phone or user.id, "invite_enterprise_member", "enterprise_invitation", invitation.id, target)
    db.commit()
    delivery = "用户已关联企业，首次登录后账号激活" if created_user else "邀请已创建，用户登录后可选择接受或拒绝加入"
    platform_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/") or str(request.base_url).rstrip("/")
    login_username = invitee.username or invitee.email or invitee.phone or target
    invitation_message = f"平台地址：{platform_url}\n用户名：{login_username}"
    sent = False
    mail_error = ""
    if created_user and channel == "email":
        sent, mail_error = send_invitation_email(db, target, enterprise.name, login_username, temp_password, invitation.expires_at, platform_url)
        delivery = "邀请邮件已发送" if sent else f"邮件未发送：{mail_error}"
    elif created_user:
        delivery = "开发环境短信发送接口已预留，邀请信息已生成"
        invitation_message += f"\n临时密码：{temp_password}\n有效期至：{invitation.expires_at.strftime('%Y-%m-%d %H:%M:%S')}"
    return {"id": invitation.id, "target": invitation.target, "department_id": invitation.department_id, "department_ids": department_ids, "channel": invitation.channel, "created_user": created_user, "temporary_password": temp_password, "token": invitation.token, "status": invitation.status, "expires_at": invitation.expires_at, "delivery": delivery, "platform_url": platform_url, "login_username": login_username, "invitation_message": invitation_message}


@app.get("/api/enterprise/invitations")
def enterprise_invitations(enterprise_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = enterprise_management_scope(db, user, enterprise_id)
    items = db.scalars(select(EnterpriseInvitation).where(EnterpriseInvitation.enterprise_id == enterprise.id).order_by(EnterpriseInvitation.created_at.desc())).all()
    for invitation in items:
        if invitation.status == "pending" and invitation.expires_at <= now():
            invitation.status = "expired"
            if invitation.created_user:
                invitee = db.get(User, invitation.invitee_id)
                if invitee and invitee.activation_status == "pending_activation":
                    invitee.activation_status = "expired"
                    invitee.is_active = False
                    invitee.temporary_password_hash = ""
                    invitee.temporary_password_expires_at = None
                membership = db.scalar(select(Membership).where(Membership.user_id == invitation.invitee_id, Membership.enterprise_id == invitation.enterprise_id, Membership.status.in_(["active", "pending_activation"])))
                if membership:
                    membership.status = "expired"
    db.commit()
    return {"items": [{"id": x.id, "target": x.target, "department_id": x.department_id, "department_ids": invitation_department_ids(x), "channel": x.channel, "created_user": x.created_user, "status": x.status, "expires_at": x.expires_at, "created_at": x.created_at} for x in items]}


@app.post("/api/enterprise/invitations/{invitation_id}/resend")
def resend_enterprise_invitation(invitation_id: str, request: Request, user: User = Depends(current_user), db: Session = Depends(db_session)):
    invitation = db.get(EnterpriseInvitation, invitation_id)
    if not invitation:
        raise HTTPException(404, "邀请不存在")
    enterprise_management_scope(db, user, invitation.enterprise_id)
    invitee = db.get(User, invitation.invitee_id)
    if not invitee:
        raise HTTPException(404, "受邀用户不存在，请重新发起邀请")
    temp_password = secrets.token_urlsafe(9)
    if invitation.created_user:
        invitee.password_hash = hash_password(temp_password)
        invitee.temporary_password_hash = hash_password(temp_password)
        invitee.temporary_password_expires_at = now() + timedelta(days=1)
        invitee.activation_status = "pending_activation"
        invitee.is_active = True
    membership = db.scalar(select(Membership).where(Membership.user_id == invitee.id, Membership.enterprise_id == invitation.enterprise_id).order_by(Membership.created_at.desc()))
    if invitation.created_user and membership and membership.status in {"expired", "rejected", "pending_activation"}:
        membership.status = "pending_activation"
        replace_membership_departments(db, membership, invitation_department_ids(invitation))
        membership.joined_at = None
    invitation.status = "pending"
    invitation.expires_at = now() + timedelta(days=1)
    request_platform_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/") or str(request.base_url).rstrip("/")
    login_username = invitee.username or invitee.email or invitee.phone or invitation.target
    delivery = "邀请已重新发送，用户登录后可选择接受或拒绝加入"
    if invitation.created_user and invitation.channel == "email":
        sent, error = send_invitation_email(db, invitation.target, db.get(Enterprise, invitation.enterprise_id).name, login_username, temp_password, invitation.expires_at, request_platform_url)
        delivery = "邀请邮件已发送" if sent else f"邮件未发送：{error}"
    audit(db, user.email or user.phone or user.id, "resend_enterprise_invitation", "enterprise_invitation", invitation.id, invitation.target)
    db.commit()
    if invitation.created_user:
        if invitation.channel != "email":
            delivery = "开发环境短信发送接口已预留，临时密码请通过接口响应获取"
        message = f"平台地址：{request_platform_url}\n用户名：{login_username}\n临时密码：{temp_password}\n有效期至：{invitation.expires_at.strftime('%Y-%m-%d %H:%M:%S')}"
    else:
        message = f"平台地址：{request_platform_url}\n用户名：{login_username}\n请登录后在‘待接受的企业邀请’中选择接受或拒绝"
    return {"id": invitation.id, "status": invitation.status, "expires_at": invitation.expires_at, "temporary_password": temp_password if invitation.created_user else "", "delivery": delivery, "platform_url": request_platform_url, "login_username": login_username, "invitation_message": message}


@app.get("/api/enterprise/my-invitations")
def my_enterprise_invitations(user: User = Depends(current_user), db: Session = Depends(db_session)):
    rows = db.execute(
        select(EnterpriseInvitation, Enterprise)
        .join(Enterprise, Enterprise.id == EnterpriseInvitation.enterprise_id)
        .where(EnterpriseInvitation.invitee_id == user.id, EnterpriseInvitation.status == "pending")
        .order_by(EnterpriseInvitation.created_at.desc())
    ).all()
    return {"items": [{"id": invitation.id, "enterprise_id": invitation.enterprise_id, "enterprise_name": enterprise.name, "target": invitation.target, "department_id": invitation.department_id, "department_ids": invitation_department_ids(invitation), "token": invitation.token, "status": invitation.status, "expires_at": invitation.expires_at, "created_at": invitation.created_at} for invitation, enterprise in rows if invitation.expires_at > now()]}


@app.post("/api/enterprise/invitations/{token}/accept")
def accept_enterprise_invitation(token: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    if user.platform_role:
        raise HTTPException(403, "平台角色账号不属于任何企业，不能接受企业邀请")
    invitation = db.scalar(select(EnterpriseInvitation).where(EnterpriseInvitation.token == token, EnterpriseInvitation.invitee_id == user.id))
    if not invitation or invitation.status != "pending" or invitation.expires_at <= now():
        raise HTTPException(400, "邀请不存在、已处理或已过期")
    existing = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.enterprise_id == invitation.enterprise_id).order_by(Membership.created_at.desc()))
    invitation.status = "accepted"
    if existing:
        existing.status = "active"
        existing.role = existing.role if existing.role in {"super_admin", "enterprise_admin"} else "member"
        existing.business_roles = existing.business_roles or "provider,user"
        existing.invited_by = invitation.inviter_id
        existing.joined_at = now()
        replace_membership_departments(db, existing, invitation_department_ids(invitation))
    else:
        existing = Membership(user_id=user.id, enterprise_id=invitation.enterprise_id, role="member", department_id="", business_roles="provider,user", status="active", invited_by=invitation.inviter_id, joined_at=now())
        db.add(existing)
        db.flush()
        replace_membership_departments(db, existing, invitation_department_ids(invitation))
    audit(db, user.email or user.phone or user.id, "accept_enterprise_invitation", "enterprise_invitation", invitation.id)
    db.commit()
    return {"status": invitation.status, "enterprise_id": invitation.enterprise_id, "role": "member"}


@app.post("/api/enterprise/invitations/{invitation_id}/reject")
def reject_enterprise_invitation(invitation_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    invitation = db.get(EnterpriseInvitation, invitation_id)
    if not invitation or invitation.invitee_id != user.id or invitation.status != "pending":
        raise HTTPException(400, "邀请不存在、已处理或已过期")
    if invitation.expires_at <= now():
        invitation.status = "expired"
        db.commit()
        raise HTTPException(400, "邀请已过期")
    pending_membership = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.enterprise_id == invitation.enterprise_id, Membership.status.in_(["active", "pending_activation"])))
    if pending_membership:
        pending_membership.status = "rejected"
    invitation.status = "rejected"
    audit(db, user.email or user.phone or user.id, "reject_enterprise_invitation", "enterprise_invitation", invitation.id, "用户拒绝加入企业", category="auth", business_domain="enterprise", before={"status": "pending", "membership_status": "pending_activation" if pending_membership else "none"}, after={"status": "rejected", "membership_status": "rejected" if pending_membership else "none"})
    db.commit()
    return {"id": invitation.id, "status": invitation.status, "enterprise_id": invitation.enterprise_id}


@app.get("/api/enterprise/members")
def enterprise_members(enterprise_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = enterprise_management_scope(db, user, enterprise_id) if enterprise_id else db.get(Enterprise, current_membership(db, user).enterprise_id)
    rows = db.execute(select(Membership, User).join(User, User.id == Membership.user_id).where(Membership.enterprise_id == enterprise.id, Membership.status.in_(["active", "pending_activation", "disabled", "deleted"]))).all()
    departments = {x.id: x.name for x in db.scalars(select(EnterpriseDepartment).where(EnterpriseDepartment.enterprise_id == enterprise.id, EnterpriseDepartment.status == "active")).all()}
    return {"items": [{"membership_id": m.id, "user_id": u.id, "name": u.name, "email": u.email, "phone": u.phone, "role": m.role, "department_id": m.department_id, "department_ids": membership_department_ids(db, m), "department_name": "、".join(departments.get(department_id, "") for department_id in membership_department_ids(db, m) if departments.get(department_id)) or "未分配", "department_names": [departments[department_id] for department_id in membership_department_ids(db, m) if department_id in departments], "business_roles": m.business_roles.split(","), "verified_status": u.verified_status, "activation_status": u.activation_status, "is_active": u.is_active, "membership_status": m.status, "account_status": "deleted" if u.activation_status == "deleted" else "disabled" if not u.is_active else "active"} for m, u in rows]}


@app.patch("/api/enterprise/members/{membership_id}")
def update_enterprise_member(membership_id: str, body: MembershipRoleBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    if body.role not in {"member", "enterprise_admin"}:
        raise HTTPException(400, "企业成员角色只能是 member 或 enterprise_admin")
    target = db.get(Membership, membership_id)
    if not target or target.status not in {"active", "pending_activation"}:
        raise HTTPException(404, "企业成员不存在")
    target_user = db.get(User, target.user_id)
    if target_user and target_user.platform_role:
        raise HTTPException(403, "平台角色账号不属于企业，不能调整企业角色")
    if user.platform_role not in {"super_admin", "platform_operator"}:
        admin = require_enterprise_admin(db, user, target.enterprise_id)
    else:
        admin = enterprise_management_scope(db, user, target.enterprise_id)
    if target.role == "super_admin":
        raise HTTPException(403, "企业超级管理员角色不可在此调整")
    target.role = body.role
    audit(db, user.email or user.phone or user.id, "update_enterprise_member_role", "membership", target.id, body.role)
    db.commit()
    return {"membership_id": target.id, "role": target.role}


@app.patch("/api/enterprise/members/{membership_id}/status")
def update_enterprise_member_status(membership_id: str, body: MembershipStatusBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    target = db.get(Membership, membership_id)
    if not target or target.status not in {"active", "pending_activation", "disabled", "deleted"}:
        raise HTTPException(404, "企业成员不存在")
    if user.platform_role:
        raise HTTPException(403, "只有企业超级管理员可以执行成员账号状态操作")
    operator = require_enterprise_admin(db, user, target.enterprise_id)
    if operator.role != "super_admin":
        raise HTTPException(403, "只有企业超级管理员可以执行成员账号状态操作")
    target_user = db.get(User, target.user_id)
    if not target_user:
        raise HTTPException(404, "成员用户不存在")
    if target_user.platform_role:
        raise HTTPException(403, "平台角色账号不属于企业，不能执行成员状态操作")
    if target_user.id == user.id:
        raise HTTPException(400, "不能对当前登录账号执行此操作")
    if target.role == "super_admin":
        raise HTTPException(403, "企业超级管理员不能被企业成员操作")
    before = {"user_active": target_user.is_active, "activation_status": target_user.activation_status, "membership_status": target.status}
    if body.action == "disable":
        if target_user.activation_status == "deleted":
            raise HTTPException(400, "已删除用户不能重复禁用")
        target_user.is_active = False
        target_user.activation_status = "disabled"
    elif body.action == "enable":
        if target_user.activation_status == "deleted" or target.status == "deleted":
            raise HTTPException(400, "已删除用户不能解禁，请重新邀请用户")
        target_user.is_active = True
        target_user.activation_status = "active"
        if target.status == "disabled":
            target.status = "active"
    else:
        target_user.is_active = False
        target_user.activation_status = "deleted"
        target.status = "deleted"
        for grant in db.scalars(select(ApplicationAccessGrant).where(ApplicationAccessGrant.user_id == target_user.id, ApplicationAccessGrant.enterprise_id == target.enterprise_id, ApplicationAccessGrant.status == "active")).all():
            grant.status = "revoked"
    after = {"user_active": target_user.is_active, "activation_status": target_user.activation_status, "membership_status": target.status}
    action_name = {"disable": "disable_enterprise_member", "enable": "enable_enterprise_member", "delete": "delete_enterprise_member"}[body.action]
    audit(db, user.email or user.phone or user.id, action_name, "membership", target.id, target_user.email or target_user.phone or target_user.id, category="auth", business_domain="enterprise", before=before, after=after)
    db.commit()
    return {"membership_id": target.id, "user_id": target_user.id, "action": body.action, "account_status": "deleted" if target_user.activation_status == "deleted" else "disabled" if not target_user.is_active else "active", "membership_status": target.status}


@app.post("/api/enterprise/{enterprise_id}/transfer-super-admin")
def transfer_enterprise_super_admin(enterprise_id: str, body: TransferSuperAdminBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    if user.platform_role:
        raise HTTPException(403, "只有企业超级管理员可以转移企业超级管理员角色")
    current = require_enterprise_admin(db, user, enterprise_id)
    if current.role != "super_admin":
        raise HTTPException(403, "只有企业超级管理员可以转移企业超级管理员角色")
    target = db.get(Membership, body.target_membership_id)
    if not target or target.enterprise_id != enterprise_id or target.status != "active":
        raise HTTPException(404, "目标用户不是该企业的有效成员")
    if target.user_id == user.id:
        raise HTTPException(400, "不能将企业超级管理员转移给当前用户")
    target_user = db.get(User, target.user_id)
    if not target_user or target_user.platform_role or not target_user.is_active or target_user.activation_status == "deleted" or target_user.verified_status != "verified":
        raise HTTPException(400, "目标用户必须是已实名且未被禁用、删除的企业成员，不能是平台角色账号")
    if target.role == "super_admin":
        raise HTTPException(409, "该用户已经是企业超级管理员")
    enterprise = db.get(Enterprise, enterprise_id)
    before = {"current_super_admin": user.id, "target_role": target.role}
    current.role = "member"
    target.role = "super_admin"
    after = {"current_super_admin": target.user_id, "old_super_admin_role": current.role, "target_role": target.role}
    audit(db, user.email or user.phone or user.id, "transfer_enterprise_super_admin", "enterprise", enterprise_id, f"转移企业超级管理员：{user.id} -> {target.user_id}", category="auth", business_domain="enterprise", before=before, after=after)
    db.commit()
    return {"enterprise_id": enterprise_id, "enterprise_name": enterprise.name if enterprise else "", "old_super_admin_user_id": user.id, "new_super_admin_user_id": target.user_id, "old_role": "member", "new_role": "super_admin"}


@app.get("/api/enterprise/departments")
def enterprise_departments(enterprise_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = enterprise_management_scope(db, user, enterprise_id) if enterprise_id else db.get(Enterprise, current_membership(db, user).enterprise_id)
    items = db.scalars(select(EnterpriseDepartment).where(EnterpriseDepartment.enterprise_id == enterprise.id, EnterpriseDepartment.status == "active").order_by(EnterpriseDepartment.name)).all()
    return {"items": [{"id": x.id, "enterprise_id": x.enterprise_id, "parent_id": x.parent_id, "name": x.name, "code": x.code, "status": x.status, "created_at": x.created_at} for x in items]}


@app.post("/api/enterprise/departments")
def create_enterprise_department(body: EnterpriseDepartmentBody, enterprise_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = enterprise_management_scope(db, user, enterprise_id)
    if body.parent_id and not db.scalar(select(EnterpriseDepartment).where(EnterpriseDepartment.id == body.parent_id, EnterpriseDepartment.enterprise_id == enterprise.id, EnterpriseDepartment.status == "active")):
        raise HTTPException(404, "上级部门不存在")
    if db.scalar(select(EnterpriseDepartment).where(EnterpriseDepartment.enterprise_id == enterprise.id, EnterpriseDepartment.name == body.name, EnterpriseDepartment.status == "active")):
        raise HTTPException(409, "同级部门名称已存在")
    item = EnterpriseDepartment(enterprise_id=enterprise.id, parent_id=body.parent_id, name=body.name, code=body.code, created_by=user.email or user.phone or user.id)
    db.add(item)
    audit(db, user.email or user.phone or user.id, "create_enterprise_department", "enterprise_department", item.id, item.name, category="auth", business_domain="enterprise")
    db.commit()
    return {"id": item.id, "parent_id": item.parent_id, "name": item.name, "code": item.code, "status": item.status}


@app.patch("/api/enterprise/departments/{department_id}")
def update_enterprise_department(department_id: str, body: EnterpriseDepartmentBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    item = db.get(EnterpriseDepartment, department_id)
    if item:
        enterprise_management_scope(db, user, item.enterprise_id)
    if item and item.status != "active":
        item = None
    if not item:
        raise HTTPException(404, "部门不存在")
    if body.parent_id == department_id:
        raise HTTPException(400, "部门不能将自己设置为上级")
    if body.parent_id and not db.scalar(select(EnterpriseDepartment).where(EnterpriseDepartment.id == body.parent_id, EnterpriseDepartment.enterprise_id == item.enterprise_id, EnterpriseDepartment.status == "active")):
        raise HTTPException(404, "上级部门不存在")
    item.name, item.code, item.parent_id = body.name, body.code, body.parent_id
    audit(db, user.email or user.phone or user.id, "update_enterprise_department", "enterprise_department", item.id, item.name, category="auth", business_domain="enterprise")
    db.commit()
    return {"id": item.id, "parent_id": item.parent_id, "name": item.name, "code": item.code, "status": item.status}


@app.delete("/api/enterprise/departments/{department_id}")
def delete_enterprise_department(department_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    item = db.get(EnterpriseDepartment, department_id)
    if item:
        enterprise_management_scope(db, user, item.enterprise_id)
    if item and item.status != "active":
        item = None
    if not item:
        raise HTTPException(404, "部门不存在")
    if db.scalar(select(EnterpriseDepartment.id).where(EnterpriseDepartment.parent_id == department_id, EnterpriseDepartment.enterprise_id == item.enterprise_id, EnterpriseDepartment.status == "active")):
        raise HTTPException(409, "部门存在子部门，请先迁移或删除子部门")
    if db.scalar(select(Membership.id).where(Membership.enterprise_id == item.enterprise_id, Membership.department_id == department_id, Membership.status == "active")) or db.scalar(select(MembershipDepartment.id).join(Membership, Membership.id == MembershipDepartment.membership_id).where(Membership.enterprise_id == item.enterprise_id, MembershipDepartment.department_id == department_id, Membership.status == "active")):
        raise HTTPException(409, "部门存在成员，请先调整成员部门")
    item.status = "deleted"
    audit(db, user.email or user.phone or user.id, "delete_enterprise_department", "enterprise_department", item.id, item.name, category="auth", business_domain="enterprise")
    db.commit()
    return {"id": item.id, "status": item.status}


@app.patch("/api/enterprise/members/{membership_id}/department")
def update_member_department(membership_id: str, body: MemberDepartmentBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    target = db.get(Membership, membership_id)
    if not target or target.status not in {"active", "pending_activation"}:
        raise HTTPException(404, "企业成员不存在")
    enterprise = enterprise_management_scope(db, user, target.enterprise_id)
    department_ids = list(dict.fromkeys(body.department_ids or ([body.department_id] if body.department_id else [])))
    if department_ids:
        valid_department_ids = set(db.scalars(select(EnterpriseDepartment.id).where(EnterpriseDepartment.id.in_(department_ids), EnterpriseDepartment.enterprise_id == enterprise.id, EnterpriseDepartment.status == "active")).all())
        if valid_department_ids != set(department_ids):
            raise HTTPException(404, "部门不存在")
    replace_membership_departments(db, target, department_ids)
    audit(db, user.email or user.phone or user.id, "update_enterprise_member_department", "membership", target.id, "、".join(department_ids) or "未分配", category="auth", business_domain="enterprise")
    db.commit()
    return {"membership_id": target.id, "department_id": target.department_id, "department_ids": department_ids}


@app.get("/api/admin/settings/notifications")
def notification_settings(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    values = {item.setting_key: item.setting_value for item in db.scalars(select(SystemSetting)).all()}
    return {"sms_provider": values.get("sms_provider", ""), "sms_endpoint": values.get("sms_endpoint", ""), "smtp_host": values.get("smtp_host", ""), "smtp_ssl": values.get("smtp_ssl", "false") == "true", "smtp_starttls": values.get("smtp_starttls", "true") == "true", "smtp_port": int(values.get("smtp_port", "587")), "smtp_username": values.get("smtp_username", ""), "smtp_password_configured": bool(values.get("smtp_password")), "smtp_password": "", "smtp_from_name": values.get("smtp_from_name", "数据集运营服务管理平台")}


@app.patch("/api/admin/settings/notifications")
def update_notification_settings(body: NotificationSettingsBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    values = body.model_dump()
    for key, value in values.items():
        if key == "smtp_password" and not value:
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
    items = db.scalars(select(User).where(User.username.in_(PLATFORM_ROLE_ACCOUNTS)).order_by(User.username)).all()
    return {"roles": PLATFORM_ROLES, "fixed": True, "initial_password": "Admin123!", "items": [{"id": x.id, "username": x.username, "name": x.name, "email": x.email, "phone": x.phone, "role": x.platform_role, "role_name": PLATFORM_ROLES.get(x.platform_role, x.platform_role)} for x in items]}


@app.post("/api/admin/platform-roles")
def assign_platform_role(body: PlatformRoleAssignmentBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    raise HTTPException(410, "平台角色已改为七个固化账号，不再支持动态分配；请使用固定用户名登录")


@app.get("/api/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(db_session)):
    platform_scope = user.platform_role in {"super_admin", "platform_operator", "security_compliance"}
    enterprise = None if platform_scope else first_enterprise(db, user)
    product_filter = True if platform_scope else Product.enterprise_id == enterprise.id
    order_filter = True if platform_scope else or_(Order.buyer_enterprise_id == enterprise.id, Order.provider_enterprise_id == enterprise.id)
    products = db.scalar(select(func.count(Product.id)).where(product_filter)) or 0
    orders = db.scalar(select(func.count(Order.id)).where(order_filter)) or 0
    active = db.scalar(select(func.count(Order.id)).where(order_filter, Order.main_status.in_(["pending_review", "pending_fulfillment", "fulfilling", "pending_confirmation"]))) or 0
    completed = db.scalar(select(func.count(Order.id)).where(order_filter, Order.main_status == "completed")) or 0
    revenue = db.scalar(select(func.coalesce(func.sum(Order.paid_amount), 0)).where(order_filter, Order.provider_enterprise_id == (None if platform_scope else enterprise.id))) if not platform_scope else db.scalar(select(func.coalesce(func.sum(Order.paid_amount), 0)).where(Order.payment_status == "paid"))
    return {"metrics": {"products": products, "orders": orders, "active_orders": active, "completed_orders": completed, "revenue": float(revenue or 0)}, "status_breakdown": [{"label": "履约中", "value": active, "color": "orange"}, {"label": "已完成", "value": completed, "color": "green"}], "notice": "首版外部连接器接口暂未开发，当前工作台展示平台内部运营闭环。"}


def product_out(p: Product) -> dict[str, Any]:
    versions = [{"id": x.id, "product_id": x.product_id, "version_code": x.version_code, "description": x.description or "", "price": float(x.price or 0), "cost": float(x.cost or 0), "rate_limit_per_minute": x.rate_limit_per_minute, "daily_quota": x.daily_quota, "monthly_quota": x.monthly_quota, "quota_unit": x.quota_unit or "", "quota_amount": x.quota_amount or 0, "status": x.status, "created_at": x.created_at} for x in (p.versions or [])]
    return {"id": p.id, "name": p.name, "product_type": p.product_type, "catalog_name": p.catalog_name or "未分类", "provider_name": p.provider_name or "", "provider_type": p.provider_type or "企业", "description": p.description, "usage_scenarios": p.usage_scenarios or "", "status": p.status, "delivery_method": p.delivery_method, "upstream_url": p.upstream_url or "", "application_url": p.application_url or "", "integration_api_url": p.integration_api_url or "", "download_limit": p.download_limit or 0, "logo_file_id": p.logo_file_id or "", "logo_thumbnail_file_id": p.logo_thumbnail_file_id or "", "price": float(p.price or 0), "pricing_strategy": p.pricing_strategy or "", "currency": p.currency, "version": p.version, "versions": versions, "settlement_rule_mode": p.settlement_rule_mode or "global", "settlement_rule_id": p.settlement_rule_id or "", "settlement_rule": json.loads(p.settlement_rule_json or "{}"), "quality_level": p.quality_level, "security_level": p.security_level or "一般", "authorization_conditions": p.authorization_conditions or "", "data_source_statement": p.data_source_statement or "", "compliance_statement": p.compliance_statement or "", "review_comment": p.review_comment or "", "reviewed_by": p.reviewed_by or "", "reviewed_at": p.reviewed_at, "created_at": p.created_at, "updated_at": p.updated_at}


def product_for_enterprise(product_id: str, user: User, db: Session) -> Product:
    if user.platform_role in {"super_admin", "platform_operator", "product_manager", "business_reviewer", "quality_reviewer", "security_compliance"}:
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
    if user.verified_status != "verified":
        stmt = stmt.where(Product.status == "published")
    elif user.platform_role not in {"super_admin", "platform_operator", "product_manager", "business_reviewer", "quality_reviewer", "security_compliance"}:
        if user.platform_role:
            raise HTTPException(403, "当前平台角色无权访问产品管理")
        enterprise = first_enterprise(db, user)
        stmt = stmt.where(Product.enterprise_id == enterprise.id)
    if q:
        stmt = stmt.where(or_(Product.name.ilike(f"%{q}%"), Product.description.ilike(f"%{q}%")))
    if status:
        stmt = stmt.where(Product.status == status)
    if product_type:
        stmt = stmt.where(Product.product_type == product_type)
    rows = db.scalars(stmt.order_by(Product.updated_at.desc())).all()
    if user.verified_status != "verified":
        public_items = []
        for product in rows:
            dto = storefront["public_product"](db, product)
            if dto:
                public_items.append({**dto, "status": "published", "price": dto["minimum_price"], "currency": product.currency})
        return {"items": public_items}
    return {"items": [product_out(x) for x in rows]}


@app.get("/api/products/{product_id}")
def product_detail(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    if user.verified_status != "verified":
        product = db.scalar(select(Product).where(Product.id == product_id, Product.status == "published"))
        if not product:
            raise HTTPException(404, "产品不存在或未发布")
        dto = storefront["public_product"](db, product)
        if not dto:
            raise HTTPException(404, "产品不存在或未就绪")
        return {**dto, "status": "published", "price": dto["minimum_price"], "currency": product.currency, "review_logs": []}
    else:
        result = product_out(product_for_enterprise(product_id, user, db))
    review_actions = (
        "submit_product_review",
        "enter_product_security_review",
        "approve_product",
        "reject_product",
        "business_approve_product",
        "business_reject_product",
        "quality_approve_product",
        "quality_reject_product",
        "approve_product_security",
        "reject_product_security",
        "operation_approve_product",
        "operation_reject_product",
        "withdraw_product_review",
    )
    review_items = db.scalars(select(AuditLog).where(AuditLog.target_type == "product", AuditLog.target_id == product_id, AuditLog.action.in_(review_actions)).order_by(AuditLog.created_at.asc())).all()
    result["review_logs"] = [{"id": item.id, "actor": item.actor, "action": item.action, "result": item.result, "detail": item.detail, "before": json.loads(item.before_json or "{}"), "after": json.loads(item.after_json or "{}"), "created_at": item.created_at} for item in review_items]
    return result


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
    member_user = db.get(User, body.user_id)
    if not member_user or not member_user.is_active or member_user.activation_status == "deleted":
        raise HTTPException(400, "目标用户已被禁用或删除，不能授予应用访问权限")
    existing = db.scalar(select(ApplicationAccessGrant).where(ApplicationAccessGrant.product_id == product.id, ApplicationAccessGrant.user_id == body.user_id, ApplicationAccessGrant.status == "active"))
    if existing:
        return {"id": existing.id, "status": existing.status, "user_id": existing.user_id}
    grant = ApplicationAccessGrant(enterprise_id=enterprise.id, product_id=product.id, user_id=body.user_id, granted_by=user.email or user.phone or user.id)
    db.add(grant)
    audit(db, user.email or user.phone or user.id, "grant_saas_access", "application_access_grant", grant.id, product.name)
    db.commit()
    db.refresh(grant)
    return {"id": grant.id, "status": grant.status, "user_id": grant.user_id, "product_id": grant.product_id}


def validate_product_settlement_rule(values: dict[str, Any], db: Session) -> None:
    mode = values.get("settlement_rule_mode", "global")
    rule_id = values.get("settlement_rule_id", "")
    custom = values.get("settlement_rule", {}) or {}
    if mode == "global":
        if rule_id and not db.get(SettlementRule, rule_id):
            raise HTTPException(400, "所选全局清算规则不存在")
        return
    rates = [custom.get(key, 0) for key in ("platform_rate", "provider_rate", "service_rate", "expert_rate", "channel_rate")]
    if any(float(rate) < 0 or float(rate) > 100 for rate in rates) or sum(float(rate) for rate in rates) > 100:
        raise HTTPException(400, "产品专属清算规则的分配比例必须在 0-100% 之间且合计不超过 100%")


@app.post("/api/products")
def create_product(body: ProductBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = first_enterprise(db, user)
    trading_policy["require_buyer"](db, user, enterprise.id)
    trading_policy["delivery_category"](body.delivery_method)
    values = body.model_dump()
    versions = values.pop("versions", [])
    custom_rule = values.pop("settlement_rule", {}) or {}
    validate_product_settlement_rule({**values, "settlement_rule": custom_rule}, db)
    values["settlement_rule_json"] = json.dumps(custom_rule, ensure_ascii=False)
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
    if not user.platform_role:
        trading_policy["require_buyer"](db, user, product.enterprise_id)
    if product.status not in {"draft", "rejected", "security_unpublished"}:
        raise HTTPException(409, "只有草稿或被驳回的产品可以修改")
    if db.scalar(select(Order.id).where(Order.product_id == product.id, Order.paid_amount > 0).limit(1)):
        if any(getattr(product, key) != getattr(body, key) for key in ("delivery_method", "upstream_url", "application_url", "integration_api_url")):
            raise HTTPException(409, "已售产品的交付类型和接入地址不可覆盖")
    values = body.model_dump()
    trading_policy["delivery_category"](body.delivery_method)
    versions = values.pop("versions", [])
    custom_rule = values.pop("settlement_rule", {}) or {}
    validate_product_settlement_rule({**values, "settlement_rule": custom_rule}, db)
    values["settlement_rule_json"] = json.dumps(custom_rule, ensure_ascii=False)
    values["provider_name"] = values["provider_name"] or db.get(Enterprise, product.enterprise_id).name
    if versions:
        values["version"] = versions[0]["version_code"]
        values["price"] = versions[0]["price"]
        trading_policy["update_versions"](db, product, versions)
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
    if not user.platform_role:
        trading_policy["require_buyer"](db, user, product.enterprise_id)
    if product.status not in {"draft", "rejected", "security_unpublished"}:
        raise HTTPException(409, "只有草稿或被驳回的产品可以增加版本")
    if db.scalar(select(ProductReleaseVersion).where(ProductReleaseVersion.product_id == product.id, ProductReleaseVersion.version_code == body.version_code)):
        raise HTTPException(409, "该版本号已存在")
    version = ProductReleaseVersion(product_id=product.id, **body.model_dump())
    db.add(version)
    db.commit()
    db.refresh(version)
    return {"id": version.id, "product_id": version.product_id, "version_code": version.version_code, "description": version.description, "price": float(version.price or 0), "cost": float(version.cost or 0), "rate_limit_per_minute": version.rate_limit_per_minute, "daily_quota": version.daily_quota, "monthly_quota": version.monthly_quota, "status": version.status}


@app.put("/api/products/{product_id}/versions/{version_id}")
def update_product_version(product_id: str, version_id: str, body: ProductVersionUpdateBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if not user.platform_role:
        trading_policy["require_buyer"](db, user, product.enterprise_id)
    if product.status not in {"draft", "rejected", "security_unpublished"}:
        raise HTTPException(409, "只有草稿或被驳回的产品可以编辑版本")
    version = db.scalar(select(ProductReleaseVersion).where(ProductReleaseVersion.id == version_id, ProductReleaseVersion.product_id == product.id))
    if not version:
        raise HTTPException(404, "产品版本不存在")
    duplicate = db.scalar(select(ProductReleaseVersion).where(ProductReleaseVersion.product_id == product.id, ProductReleaseVersion.version_code == body.version_code, ProductReleaseVersion.id != version.id))
    if duplicate:
        raise HTTPException(409, "该版本号已存在")
    trading_policy["protect_sold_version"](db, version, body.model_dump())
    for key, value in body.model_dump().items():
        setattr(version, key, value)
    if product.versions and version.id == product.versions[0].id:
        product.version = version.version_code
        product.price = version.price
    db.commit()
    db.refresh(version)
    return {"id": version.id, "product_id": version.product_id, "version_code": version.version_code, "description": version.description, "price": float(version.price or 0), "cost": float(version.cost or 0), "rate_limit_per_minute": version.rate_limit_per_minute, "daily_quota": version.daily_quota, "monthly_quota": version.monthly_quota, "status": version.status}


@app.post("/api/products/{product_id}/submit")
def submit_product(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if not user.platform_role:
        trading_policy["require_buyer"](db, user, product.enterprise_id)
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
    trading_policy["validate_submission"](db, product)
    product.status = "pending_review"
    product.review_comment = ""
    notify_platform_role(db, "business_reviewer", "产品待业务审核", f"产品“{product.name}”已提交审核，请进行业务审核。", "product", product.id)
    audit(db, user.email, "submit_product_review", "product", product.id, after={"status": product.status})
    db.commit()
    return product_out(product)


@app.post("/api/products/{product_id}/withdraw")
def withdraw_product(product_id: str, body: ProductUnpublishBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = product_for_enterprise(product_id, user, db)
    if user.platform_role not in {"super_admin", "platform_operator"}:
        require_enterprise_admin(db, user, product.enterprise_id)
    if product.status != "pending_review":
        raise HTTPException(409, "只有待审核产品可以撤回")
    before = {"status": product.status, "review_comment": product.review_comment or ""}
    product.status = "draft"
    product.review_comment = body.reason.strip()
    product.reviewed_by = user.email or user.phone or user.id
    product.reviewed_at = now()
    audit(db, user.email or user.phone or user.id, "withdraw_product_review", "product", product.id, body.reason.strip(), before=before, after={"status": product.status, "review_comment": product.review_comment})
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
            existing_saas.client_id = client.client_id
            existing_saas.client_secret = client.client_secret
            existing_saas.scope = client.scope
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
        existing_saas.scope = client.scope
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
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "产品不存在")
    stage_by_status = {"pending_review": "business", "quality_review": "quality", "operation_review": "operation"}
    stage = stage_by_status.get(product.status)
    if not stage:
        raise HTTPException(409, "当前产品不在业务、质量或运营审核环节")
    require_product_review_role(user, stage)
    if body.decision not in {"approve", "reject"}:
        raise HTTPException(400, "审核结论必须是 approve 或 reject")
    if body.decision == "reject" and not body.comment.strip():
        raise HTTPException(400, "驳回时必须填写审核意见")
    actor = user.email or user.phone or user.id
    before_status = product.status
    security_scan = None
    if body.decision == "reject":
        product.status = "rejected"
    elif stage == "business":
        product.status = "quality_review"
    elif stage == "quality":
        product.status = "security_review"
        security_scan = run_product_security_scan(product, db, actor)
    elif stage == "operation":
        version_gateway["on_review_complete"](db, product, user)
    product.review_comment = body.comment.strip() or ("已通过，进入" + {"business": "质量审核", "quality": "安全审核", "operation": "发布"}[stage])
    product.reviewed_by = actor
    product.reviewed_at = now()
    if body.decision == "approve" and stage == "business":
        notify_platform_role(db, "quality_reviewer", "产品待质量审核", f"产品“{product.name}”已通过业务审核，请进行质量审核。", "product", product.id)
    elif body.decision == "approve" and stage == "quality":
        notify_platform_role(db, "security_compliance", "产品待安全审核", f"产品“{product.name}”已通过质量审核，请查看病毒和数据安全扫描报告。", "product", product.id)
    if body.decision == "approve" and stage == "operation" and product.product_type == "saas":
        config = db.scalar(select(SaaSIntegrationConfig).where(SaaSIntegrationConfig.product_id == product.id))
        if not config:
            client_id = "market_" + secrets.token_urlsafe(12)
            client_secret = secrets.token_urlsafe(32)
            mock_base_url = os.getenv("MOCK_SAAS_BASE_URL", "http://market-mock-saas:8200").rstrip("/")
            config = SaaSIntegrationConfig(product_id=product.id, base_url=mock_base_url, operation_path="/isv.php", token_url=mock_base_url + "/oauth/token", client_id=client_id, client_secret=client_secret, auth_mode="oauth2", status="active", updated_by=user.email or user.phone or user.id)
            db.add(config)
    if body.decision == "approve" and stage == "operation" and product.product_type in {"api", "model", "saas"}:
        client = ensure_oauth_client(db, product)
        for route in db.scalars(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id)).all():
            route.upstream_client_id = client.client_id
            route.upstream_client_secret = client.client_secret
        audit(db, user.email or user.phone or user.id, "ensure_platform_oauth_client", "oauth_client", client.id, product.product_type)
    if body.decision == "approve" and stage == "operation" and product.delivery_method in {"api", "model_api"}:
        for version in product.versions:
            if version.status != "active":
                continue
            config = db.scalar(select(version_gateway["Config"]).where(version_gateway["Config"].version_id == version.id))
            if not config:
                legacy = product_gateway_route(db, product.id, version.version_code)
                body_config = GatewayConfigBody(**({key: getattr(legacy, key) for key in ("upstream_url", "route_key", "version", "auth_mode", "upstream_auth_mode", "upstream_scope", "timeout_ms", "strip_prefix", "health_method", "health_path", "rate_limit_per_minute", "daily_quota", "monthly_quota")} if legacy else {
                    "upstream_url": product.upstream_url, "route_key": f"{product.id[:12]}-{version.id[:12]}",
                    "version": version.version_code, "rate_limit_per_minute": version.rate_limit_per_minute,
                    "daily_quota": version.daily_quota, "monthly_quota": version.monthly_quota}))
                config = version_gateway["save_config"](db, product, version.id, body_config, user)
            route = db.get(ApiGatewayRoute, config.route_id)
            client = ensure_oauth_client(db, product)
            route.upstream_client_id, route.upstream_client_secret = client.client_id, client.client_secret
            try:
                version_gateway["verify_version"](db, product, version.id, user)
            except HTTPException as exc:
                audit(db, actor, "version_integration_pending", "product", product.id,
                      f"version={version.id}; status={exc.status_code}", result="failed")
    audit(db, actor, f"{stage}_{'approve' if body.decision == 'approve' else 'reject'}_product", "product", product.id, product.review_comment, before={"status": before_status}, after={"status": product.status})
    db.commit()
    return product_out(product) | ({"security_report": security_scan_out(security_scan)} if security_scan else {})


@app.get("/api/products/{product_id}/security-report")
def product_security_report(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    if user.platform_role not in {"super_admin", "platform_operator", "product_manager", "business_reviewer", "quality_reviewer", "security_compliance"}:
        raise HTTPException(403, "当前角色无权查看产品安全报告")
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "产品不存在")
    scan = db.scalar(select(ProductSecurityScan).where(ProductSecurityScan.product_id == product.id).order_by(ProductSecurityScan.scanned_at.desc()))
    if not scan:
        raise HTTPException(404, "该数据集尚未生成安全审核报告")
    return {"product": product_out(product), "security_report": security_scan_out(scan)}


@app.post("/api/products/{product_id}/security-review")
def review_product_security(product_id: str, body: ProductSecurityReviewBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_product_review_role(user, "security")
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "产品不存在")
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
    product.status = "operation_review" if body.decision == "approve" else "rejected"
    product.review_comment = body.comment.strip() or "安全审核通过"
    product.reviewed_by = actor
    product.reviewed_at = now()
    if body.decision == "approve":
        notify_platform_role(db, "platform_operator", "产品待运营审核", f"产品“{product.name}”已通过安全审核，请执行运营审核发布。", "product", product.id)
    audit(db, actor, "approve_product_security" if body.decision == "approve" else "reject_product_security", "product", product.id, body.comment.strip(), before={"status": "security_review"}, after={"status": product.status})
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


def clamav_scan(content: bytes) -> tuple[str, str]:
    """Scan an uploaded object through the in-cluster clamd INSTREAM protocol."""
    if not CLAMAV_ENABLED:
        return "disabled", "ClamAV scanning is disabled by configuration"
    try:
        with socket.create_connection((CLAMAV_HOST, CLAMAV_PORT), timeout=30) as connection:
            connection.sendall(b"zINSTREAM\0")
            for offset in range(0, len(content), 1024 * 1024):
                chunk = content[offset:offset + 1024 * 1024]
                connection.sendall(struct.pack("!I", len(chunk)))
                connection.sendall(chunk)
            connection.sendall(struct.pack("!I", 0))
            response = connection.recv(4096).decode("utf-8", errors="replace").replace("\x00", "").strip()
        if "FOUND" in response:
            return "infected", response
        if response.endswith("OK"):
            return "clean", response
        return "error", response or "ClamAV returned an empty response"
    except (OSError, TimeoutError) as exc:
        return "unavailable", str(exc)[:240]


def clamav_scan_stream(fileobj, size: int) -> tuple[str, str]:
    """Scan an UploadFile stream without materializing the whole object."""
    if not CLAMAV_ENABLED:
        return "disabled", "ClamAV scanning is disabled by configuration"
    try:
        with socket.create_connection((CLAMAV_HOST, CLAMAV_PORT), timeout=30) as connection:
            connection.sendall(b"zINSTREAM\0")
            remaining = size
            while remaining:
                chunk = fileobj.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                connection.sendall(struct.pack("!I", len(chunk)))
                connection.sendall(chunk)
                remaining -= len(chunk)
            connection.sendall(struct.pack("!I", 0))
            response = connection.recv(4096).decode("utf-8", errors="replace").replace("\x00", "").strip()
        if remaining:
            return "error", "上传文件流长度与声明大小不一致"
        if "FOUND" in response:
            return "infected", response
        if response.endswith("OK"):
            return "clean", response
        return "error", response or "ClamAV returned an empty response"
    except (OSError, TimeoutError) as exc:
        return "unavailable", str(exc)[:240]


def clamav_version() -> str:
    """Read the running clamd version for security reports."""
    if not CLAMAV_ENABLED:
        return "disabled"
    try:
        with socket.create_connection((CLAMAV_HOST, CLAMAV_PORT), timeout=5) as connection:
            connection.sendall(b"VERSION\0")
            return connection.recv(4096).decode("utf-8", errors="replace").replace("\x00", "").strip() or "unknown"
    except (OSError, TimeoutError):
        return "unavailable"


def presidio_version() -> str:
    """Return the analyzer package version used by the in-cluster service."""
    return os.getenv("PRESIDIO_VERSION", "2.2.364")


def presidio_analyze(text_value: str) -> tuple[list[dict[str, Any]], str]:
    """Call the in-cluster Presidio Analyzer; no data leaves Kubernetes."""
    if not text_value.strip():
        return [], "empty"
    url = os.getenv("PRESIDIO_ANALYZER_URL", "http://market-presidio-analyzer:3000/analyze")
    try:
        response = httpx.post(url, json={"text": text_value[:200000], "language": "en"}, timeout=30)
        response.raise_for_status()
        items = response.json()
        entity_labels = {
            "PERSON": "个人姓名",
            "PHONE_NUMBER": "电话号码",
            "EMAIL_ADDRESS": "邮箱地址",
            "LOCATION": "地理位置",
            "ORGANIZATION": "组织机构",
            "CREDIT_CARD": "银行卡/信用卡号",
            "IBAN_CODE": "银行账户",
            "IP_ADDRESS": "IP地址",
            "URL": "网址",
        }
        findings = []
        for item in items:
            if not isinstance(item, dict):
                continue
            entity = item.get("entity_type", "UNKNOWN")
            start = int(item.get("start") or 0)
            end = int(item.get("end") or start)
            matched = text_value[start:end].replace("\n", " ").strip()
            line_start = text_value.rfind("\n", 0, start) + 1
            line_end = text_value.find("\n", end)
            if line_end < 0:
                line_end = len(text_value)
            line_content = text_value[line_start:line_end].strip()
            findings.append({
                "entity": entity,
                "entity_label": entity_labels.get(entity, entity),
                "score": item.get("score", 0),
                "matched_text": matched[:240],
                "line_number": text_value.count("\n", 0, start) + 1,
                "line_content": line_content[:1000],
                "message": f"检测到{entity_labels.get(entity, entity)}：{matched[:240] or '未提取到具体内容'}",
                "source": "presidio",
            })
        return filter_presidio_dataset_false_positives(findings), "available"
    except (httpx.HTTPError, ValueError) as exc:
        return [{"entity": "PRESIDIO_UNAVAILABLE", "severity": "medium", "message": str(exc)[:240], "source": "platform"}], "unavailable"


def filter_presidio_dataset_false_positives(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suppress known English-model false positives in Chinese dataset records."""
    filtered = []
    dataset_id = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,40}$")
    for finding in findings:
        entity = finding.get("entity")
        matched = str(finding.get("matched_text") or "").strip()
        has_cjk = bool(re.search(r"[\u3400-\u9fff]", matched))
        if dataset_id.fullmatch(matched) or re.fullmatch(r"(?:v|ver)[0-9]+", matched, re.IGNORECASE):
            continue
        if entity == "LOCATION" and has_cjk:
            continue
        if entity == "PERSON" and (has_cjk and len(re.sub(r"[^\u3400-\u9fff]", "", matched)) > 6 or ("-" in matched and any(char.isdigit() for char in matched))):
            continue
        filtered.append(finding)
    return filtered


def enrich_legacy_presidio_findings(text_value: str, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert older coordinate-only findings into readable report entries."""
    enriched = []
    for item in findings:
        if not isinstance(item, dict) or "start" not in item:
            enriched.append(item)
            continue
        start = int(item.get("start") or 0)
        end = int(item.get("end") or start)
        matched = text_value[start:end].replace("\n", " ").strip()
        line_start = text_value.rfind("\n", 0, start) + 1
        line_end = text_value.find("\n", end)
        if line_end < 0:
            line_end = len(text_value)
        line_content = text_value[line_start:line_end].strip()
        entity = item.get("entity", "UNKNOWN")
        labels = {"PERSON": "个人姓名", "PHONE_NUMBER": "电话号码", "EMAIL_ADDRESS": "邮箱地址", "LOCATION": "地理位置", "ORGANIZATION": "组织机构", "CREDIT_CARD": "银行卡/信用卡号", "IBAN_CODE": "银行账户", "IP_ADDRESS": "IP地址", "URL": "网址"}
        label = labels.get(entity, entity)
        enriched.append({**item, "entity_label": label, "matched_text": matched[:240], "line_number": text_value.count("\n", 0, start) + 1, "line_content": line_content[:1000], "message": f"检测到{label}：{matched[:240] or '未提取到具体内容'}"})
    return enriched


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


def read_product_sample(file_item: FileObject, raw_content: bytes | None = None) -> str:
    if not MINIO_ENDPOINT or file_item.size <= 0:
        return ""
    if not (file_item.content_type.startswith("text/") or file_item.content_type in {"application/json", "application/csv", "application/xml"} or file_item.original_name.lower().endswith((".csv", ".json", ".jsonl", ".md", ".txt", ".xml", ".zip", ".tar", ".tgz", ".tar.gz", ".rar", ".7z"))):
        return ""
    response = None
    temp_path = None
    if raw_content is None:
        client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=False)
        response = client.get_object(MINIO_BUCKET, file_item.object_name)
    try:
        if raw_content is not None:
            sample = raw_content
        elif file_item.size > 100 * 1024 * 1024:
            # Archive indexes can be at the end of large files. Spool to disk
            # so extraction is seekable without holding the object in memory.
            with tempfile.NamedTemporaryFile(prefix="market-presidio-", suffix=".archive", delete=False) as handle:
                temp_path = handle.name
                while True:
                    chunk = response.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            sample = b""
        else:
            sample = response.read(file_item.size)
        name = file_item.original_name.lower()
        if name.endswith(".zip"):
            archive_source = temp_path or BytesIO(sample)
            with zipfile.ZipFile(archive_source) as archive:
                text_parts = []
                for entry in archive.infolist()[:50]:
                    if entry.is_dir() or not entry.filename.lower().endswith((".txt", ".csv", ".json", ".jsonl", ".md", ".xml")):
                        continue
                    text_parts.append(archive.open(entry).read(2_000_000).decode("utf-8", errors="ignore"))
                return "\n".join(text_parts)
        if name.endswith((".tar", ".tgz", ".tar.gz")):
            archive = tarfile.open(name=temp_path, mode="r:*") if temp_path else tarfile.open(fileobj=BytesIO(sample), mode="r:*")
            with archive:
                text_parts = []
                for entry in archive.getmembers()[:50]:
                    if not entry.isfile() or not entry.name.lower().endswith((".txt", ".csv", ".json", ".jsonl", ".md", ".xml")):
                        continue
                    handle = archive.extractfile(entry)
                    if handle:
                        text_parts.append(handle.read(2_000_000).decode("utf-8", errors="ignore"))
                return "\n".join(text_parts)
        if name.endswith((".rar", ".7z")):
            archive_path = temp_path
            temporary_archive = None
            if not archive_path:
                with tempfile.NamedTemporaryFile(prefix="market-presidio-", suffix=os.path.splitext(name)[1], delete=False) as handle:
                    temporary_archive = handle.name
                    handle.write(sample)
                archive_path = temporary_archive
            try:
                with tempfile.TemporaryDirectory(prefix="market-presidio-extract-") as extract_dir:
                    if name.endswith(".rar") and shutil.which("unar"):
                        extraction_command = ["unar", "-f", "-o", extract_dir, archive_path]
                    else:
                        extraction_command = ["7z", "x", "-y", f"-o{extract_dir}", archive_path]
                    result = subprocess.run(extraction_command, capture_output=True, text=True, timeout=120)
                    text_parts = []
                    for root, _, names in os.walk(extract_dir):
                        for entry_name in names:
                            if not entry_name.lower().endswith((".txt", ".csv", ".json", ".jsonl", ".md", ".xml")):
                                continue
                            with open(os.path.join(root, entry_name), "rb") as handle:
                                text_parts.append(handle.read(2_000_000).decode("utf-8", errors="ignore"))
                            if len(text_parts) >= 50:
                                break
                        if len(text_parts) >= 50:
                            break
                    # 7z may return 2 for a partially damaged archive while
                    # still extracting readable entries. Scan those entries
                    # instead of discarding the usable sample altogether.
                    return "\n".join(text_parts)
            finally:
                if temporary_archive:
                    try:
                        os.unlink(temporary_archive)
                    except OSError:
                        pass
        return sample.decode("utf-8", errors="ignore")
    finally:
        if response is not None:
            response.close()
            response.release_conn()
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def run_product_security_scan(product: Product, db: Session, actor: str) -> ProductSecurityScan:
    metadata_text = json.dumps({"name": product.name, "description": product.description, "usage_scenarios": product.usage_scenarios, "authorization_conditions": product.authorization_conditions, "data_source_statement": product.data_source_statement, "compliance_statement": product.compliance_statement}, ensure_ascii=False)
    text_parts = [metadata_text]
    files = db.scalars(select(FileObject).where(FileObject.product_id == product.id, FileObject.status != "deleted")).all()
    file_reports = []
    file_findings = []
    for item in files:
        try:
            sample = read_product_sample(item)
            file_presidio_findings, file_presidio_status = (presidio_analyze(sample) if item.scan_status == "clean" and sample else ([], "not_scanned"))
            try:
                stored_scan = json.loads(item.scan_report or "{}")
            except json.JSONDecodeError:
                stored_scan = {"clamav_report": item.scan_report}
            if sample:
                text_parts.append(sample)
            file_reports.append({"file_id": item.id, "name": item.original_name, "size": item.size, "clamav_status": stored_scan.get("clamav_status", item.scan_status), "clamav_report": stored_scan.get("clamav_report", item.scan_report), "sample_scanned": bool(sample), "presidio_status": file_presidio_status, "presidio_findings": file_presidio_findings})
            file_findings.extend(file_presidio_findings)
        except Exception as exc:
            file_reports.append({"file_id": item.id, "name": item.original_name, "size": item.size, "clamav_status": item.scan_status, "clamav_report": item.scan_report, "sample_scanned": False, "presidio_status": "error", "error": str(exc)[:240]})
    combined = "\n".join(text_parts)
    presidio_findings, presidio_status = presidio_analyze(combined)
    findings = market_sensitive_patterns(combined) + presidio_findings + file_findings
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
    for route in db.scalars(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product.id)).all():
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
    raise HTTPException(409, "产品须依次通过业务、质量、安全和运营审核，不能直接发布")


def ensure_gateway_version_schema():
    """Preserve legacy route IDs while relaxing only product-level uniqueness."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(73481203)"))
        connection.execute(text("ALTER TABLE api_gateway_routes ADD COLUMN IF NOT EXISTS validated_ip VARCHAR(64) DEFAULT ''"))
        schema = inspect(connection)
        quote_name = connection.dialect.identifier_preparer.quote
        for constraint in schema.get_unique_constraints("api_gateway_routes"):
            if constraint["column_names"] == ["product_id"]:
                connection.execute(text("ALTER TABLE api_gateway_routes DROP CONSTRAINT " + quote_name(constraint["name"])))
        for index in schema.get_indexes("api_gateway_routes"):
            if index["unique"] and index["column_names"] == ["product_id"] and not index.get("duplicates_constraint"):
                connection.execute(text("DROP INDEX " + quote_name(index["name"])))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_api_gateway_routes_product_id ON api_gateway_routes (product_id)"))
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_gateway_product_version ON api_gateway_routes (product_id, version)"))


def product_gateway_route(db: Session, product_id: str, version: str = "", active: bool = False):
    stmt = select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == product_id)
    if version:
        stmt = stmt.where(ApiGatewayRoute.version == version)
    if active:
        stmt = stmt.where(ApiGatewayRoute.status == "active")
    rows = db.scalars(stmt.order_by(ApiGatewayRoute.created_at, ApiGatewayRoute.id)).all()
    if len(rows) > 1:
        raise HTTPException(409, "产品存在多版本网关配置，请指定版本")
    return rows[0] if rows else None


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


def apisix_policy_redis():
    return redis.Redis.from_url(os.getenv("REDIS_URL", "redis://market-redis:6379/0"), decode_responses=True)


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
    parsed_upstream = urlparse(target_upstream)
    node_host = route.validated_ip if use_native_upstream and route.validated_ip else parsed_upstream.hostname
    node_host = f"[{node_host}]" if node_host and ":" in node_host else node_host
    node = f"{node_host}:{parsed_upstream.port or (443 if parsed_upstream.scheme == 'https' else 80)}"
    prefix = parsed_upstream.path.rstrip("/")
    plugins = {
        "proxy-rewrite": {"regex_uri": [f"^/gateway/{route.route_key}(.*)", (prefix if route.validated_ip else "") + r"$1"] if use_native_upstream else [f"^/gateway/{route.route_key}(.*)", r"/gateway/" + route.route_key + r"$1"]},
        "limit-count": {"count": rate_limit, "time_window": 60, "rejected_code": 429, "rejected_msg": '{"code":"RATE_LIMIT_EXCEEDED","message":"超过 API 每分钟调用频率限制"}', "key": "consumer_name" if apisix_native_auth() else "http_x_api_key", "key_type": "var", "policy": "redis", "redis_host": "market-redis", "redis_port": 6379, "redis_database": 2},
        "market-gateway-quota": {"route_key": route.route_key, "daily_quota": daily_quota or 0, "monthly_quota": monthly_quota or 0, "lookup_consumer": True},
    }
    if apisix_native_auth():
        plugins["key-auth"] = {"header": "X-API-Key", "query": "api_key"}
    if apisix_native_upstream() and (route.upstream_auth_mode == "oauth2" or not route.validated_ip):
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
        "upstream": {"type": "roundrobin", "nodes": {node: 1}, "scheme": parsed_upstream.scheme,
                     "pass_host": "rewrite", "upstream_host": parsed_upstream.hostname,
                     "timeout": {"connect": route.timeout_ms / 1000, "send": route.timeout_ms / 1000, "read": route.timeout_ms / 1000}}
                    if route.validated_ip else {"type": "roundrobin", "nodes": {target_upstream.replace("http://", "").replace("https://", ""): 1}, "scheme": "https" if target_upstream.startswith("https://") else "http"},
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
    apisix_admin_request("PUT", f"/consumers/{credential.apisix_consumer_name}", {"username": credential.apisix_consumer_name, "plugins": {"key-auth": {"key": raw_key}, "market-gateway-quota": {"route_key": route_key, "daily_quota": daily_quota or 0, "monthly_quota": monthly_quota or 0, "total_quota": credential.total_quota or 0}}})
    try:
        apisix_policy_redis().hset(f"market:apisix:credential:{credential.apisix_consumer_name}", mapping={"status": credential.status, "daily_quota": daily_quota or 0, "monthly_quota": monthly_quota or 0, "total_quota": credential.total_quota or 0})
    except redis.RedisError as exc:
        raise RuntimeError(f"API 凭据策略缓存同步失败：{exc}") from exc
    return True


def api_entitlement_policy(db: Session, enterprise_id: str, route: ApiGatewayRoute) -> dict[str, int]:
    """Aggregate active paid orders into one enterprise/API entitlement."""
    orders = db.scalars(select(Order).where(
        Order.buyer_enterprise_id == enterprise_id,
        Order.product_id == route.product_id,
        Order.payment_status == "paid",
        Order.main_status.not_in(("cancelled", "closed")),
        Order.refunded_amount == 0,
    )).all()
    versions = {x.id: x for x in db.scalars(select(ProductReleaseVersion).where(ProductReleaseVersion.id.in_([o.product_version_id for o in orders]))).all()} if orders else {}
    limits = {"rate_limit_per_minute": 0, "daily_quota": 0, "monthly_quota": 0, "total_quota": 0}
    for order in orders:
        version = versions.get(order.product_version_id)
        if not version:
            continue
        limits["rate_limit_per_minute"] = max(limits["rate_limit_per_minute"], int(version.rate_limit_per_minute or 0))
        for key, value in (("daily_quota", version.daily_quota), ("monthly_quota", version.monthly_quota), ("total_quota", version.quota_amount)):
            value = int(value or 0)
            if value == 0:
                limits[key] = 0
            elif limits[key] != 0:
                limits[key] += value
    return limits


def refresh_enterprise_api_credentials(db: Session, enterprise_id: str, route: ApiGatewayRoute) -> None:
    """Refresh shared enterprise credentials after an order entitlement changes."""
    policy = api_entitlement_policy(db, enterprise_id, route)
    has_orders = db.scalar(select(func.count(Order.id)).where(
        Order.buyer_enterprise_id == enterprise_id,
        Order.product_id == route.product_id,
        Order.payment_status == "paid",
        Order.main_status.not_in(("cancelled", "closed")),
        Order.refunded_amount == 0,
    )) > 0
    items = db.scalars(select(ApiCredential).where(
        ApiCredential.route_id == route.id,
        ApiCredential.enterprise_id == enterprise_id,
        ApiCredential.order_id == "",
    )).all()
    for credential in items:
        if has_orders:
            credential.status = "active"
            credential.rate_limit_per_minute = policy["rate_limit_per_minute"] or None
            credential.daily_quota = policy["daily_quota"] or None
            credential.monthly_quota = policy["monthly_quota"] or None
            credential.total_quota = policy["total_quota"]
            sync_apisix_consumer_policy(credential, route)
        else:
            credential.status = "revoked"
            remove_apisix_consumer(credential)


def sync_apisix_consumer_policy(credential: ApiCredential, route: ApiGatewayRoute) -> bool:
    """Update APISIX/Redis policy without changing the existing secret."""
    if not apisix_enabled():
        return True
    if not credential.apisix_consumer_name:
        credential.apisix_consumer_name = f"market-consumer-{credential.id}"
    daily_quota = credential.daily_quota if credential.daily_quota is not None else (route.daily_quota or 0)
    monthly_quota = credential.monthly_quota if credential.monthly_quota is not None else (route.monthly_quota or 0)
    apisix_policy_redis().hset(f"market:apisix:credential:{credential.apisix_consumer_name}", mapping={
        "status": credential.status,
        "daily_quota": daily_quota,
        "monthly_quota": monthly_quota,
        "total_quota": credential.total_quota or 0,
    })
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
        response = validated_http_request(route.health_method or "GET", health_url,
                                          timeout=min(max(route.timeout_ms / 1000, 0.1), 10.0), follow_redirects=False)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"健康检查返回 HTTP {response.status_code}")
        addresses = response.extensions.get("validated_upstream_addresses", [])
        if addresses:
            route.validated_ip = addresses[0]
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
    route = product_gateway_route(db, product.id, order.product_version_code, active=True)
    if not route:
        raise HTTPException(409, "API 网关路由尚未启用")
    return order, product, route, order.buyer_enterprise_id


def api_credential_out(item: ApiCredential, route: ApiGatewayRoute) -> dict[str, Any]:
    return {"id": item.id, "name": item.name, "key_prefix": item.key_prefix, "status": item.status, "product_version_id": item.product_version_id, "rate_limit_per_minute": item.rate_limit_per_minute or route.rate_limit_per_minute, "daily_quota": item.daily_quota or route.daily_quota, "monthly_quota": item.monthly_quota or route.monthly_quota, "total_quota": item.total_quota or 0, "expires_at": item.expires_at, "last_used_at": item.last_used_at, "created_at": item.created_at}


@app.get("/api/products/{product_id}/gateway-config")
def get_gateway_config(product_id: str, version: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    if user.platform_role not in {"super_admin", "platform_operator"}:
        membership = current_membership(db, user, product.enterprise_id)
        if membership.role not in {"super_admin", "enterprise_admin"}:
            raise HTTPException(403, "只有产品提供企业管理员或平台管理员可以查看网关配置")
    route = product_gateway_route(db, product.id, version)
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
def validate_gateway_config(product_id: str, version: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    gateway_operator_allowed(product, user, db, "校验")
    route = product_gateway_route(db, product.id, version)
    if not route:
        raise HTTPException(404, "请先保存 API 网关配置")
    parsed = urlparse(route.upstream_url)
    checks = {"upstream_url": parsed.scheme in {"http", "https"} and bool(parsed.netloc), "route_key": bool(route.route_key), "version": bool(route.version), "health_path": bool(route.health_path), "policy": route.rate_limit_per_minute > 0 and route.daily_quota >= 0}
    return {"valid": all(checks.values()), "checks": checks, "route": gateway_route_out(route, product)}


@app.post("/api/products/{product_id}/gateway-config/rollback")
def rollback_gateway_config(product_id: str, version: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    gateway_operator_allowed(product, user, db, "回滚")
    route = product_gateway_route(db, product.id, version)
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
    release = db.scalar(select(ProductReleaseVersion).where(ProductReleaseVersion.product_id == product.id,
                                                           ProductReleaseVersion.version_code == body.version))
    if not release:
        raise HTTPException(404, "请先登记对应产品版本")
    if not body.route_key.strip():
        body = body.model_copy(update={"route_key": f"{product.id[:12]}-{release.id[:12]}"})
    config = version_gateway["save_config"](db, product, release.id, body, user)
    route = db.get(ApiGatewayRoute, config.route_id)
    db.commit()
    return gateway_route_out(route, product)


@app.post("/api/products/{product_id}/gateway-config/publish")
def publish_gateway_config(product_id: str, version: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    if user.platform_role not in {"super_admin", "platform_operator"}:
        membership = current_membership(db, user, product.enterprise_id)
        if membership.role not in {"super_admin", "enterprise_admin"}:
            raise HTTPException(403, "只有产品提供企业管理员或平台管理员可以发布 API 网关路由")
    if product.status not in {"published", "pending_integration"}:
        raise HTTPException(409, "产品必须先发布后才能启用 API 网关路由")
    route = product_gateway_route(db, product.id, version)
    if not route:
        raise HTTPException(404, "请先保存 API 网关配置")
    config = db.scalar(select(version_gateway["Config"]).where(version_gateway["Config"].route_id == route.id))
    if config:
        verified = version_gateway["verify_version"](db, product, config.version_id, user)
        db.commit()
        if verified.status != "verified":
            raise HTTPException(409, "版本接入验证或APISIX发布失败，请查看接入状态")
        return gateway_route_out(route, product)
    if not gateway_auto_publish(route, product, db, user.email or user.phone or user.id):
        db.commit()
        raise HTTPException(409, f"网关后端健康检查失败：{route.health_message}")
    db.commit()
    return gateway_route_out(route, product)


@app.post("/api/products/{product_id}/gateway-config/health-check")
def check_gateway_config(product_id: str, version: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    return publish_gateway_config(product_id, version, user, db)


@app.post("/api/products/{product_id}/gateway-credentials")
def create_gateway_credential(product_id: str, body: GatewayCredentialBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    route = product_gateway_route(db, product.id, active=True)
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
    items = db.scalars(select(ApiCredential).where(ApiCredential.route_id == route.id, ApiCredential.enterprise_id == enterprise_id, ApiCredential.order_id == "").order_by(ApiCredential.created_at.desc())).all()
    return {"order_id": order.id, "product_id": product.id, "product_name": product.name, "route_key": route.route_key, "gateway_base_path": f"/gateway/{route.route_key}", "shared_scope": "enterprise", "items": [api_credential_out(x, route) for x in items]}


@app.post("/api/orders/{order_id}/api-credentials")
def create_order_api_credential(order_id: str, body: GatewayCredentialBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    order, product, route, enterprise_id = api_order_context(order_id, user, db)
    version = db.get(ProductReleaseVersion, order.product_version_id)
    if not version:
        raise HTTPException(409, "订单对应的 API 版本不存在")
    credential = db.scalar(select(ApiCredential).where(ApiCredential.route_id == route.id, ApiCredential.enterprise_id == enterprise_id, ApiCredential.order_id == "").order_by(ApiCredential.created_at.desc()))
    if credential and credential.status in {"active", "exhausted"}:
        refresh_enterprise_api_credentials(db, enterprise_id, route)
        db.commit()
        db.refresh(credential)
        return {**api_credential_out(credential, route), "route_key": route.route_key, "gateway_base_path": f"/gateway/{route.route_key}", "shared": True, "warning": "该企业已有共享 API 凭据，本次订单已合并额度；API Key 不会重复生成，请继续使用原凭据"}
    raw_key = "mk_" + secrets.token_urlsafe(30)
    policy = api_entitlement_policy(db, enterprise_id, route)
    credential = ApiCredential(route_id=route.id, enterprise_id=enterprise_id, order_id="", product_version_id="", name=body.name.strip() or f"{product.name} 企业共享 API 凭据", key_prefix=raw_key[:12], key_hash=hashlib.sha256(raw_key.encode()).hexdigest(), rate_limit_per_minute=body.rate_limit_per_minute if body.rate_limit_per_minute is not None else policy["rate_limit_per_minute"], daily_quota=body.daily_quota if body.daily_quota is not None else policy["daily_quota"], monthly_quota=body.monthly_quota if body.monthly_quota is not None else policy["monthly_quota"], total_quota=body.total_quota if body.total_quota is not None else policy["total_quota"], expires_at=body.expires_at, created_by=user.email or user.phone or user.id)
    db.add(credential)
    db.flush()
    try:
        sync_apisix_consumer(credential, raw_key, route)
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(502, f"API 凭据同步 APISIX Consumer 失败：{exc}") from exc
    audit(db, user.email or user.phone or user.id, "create_shared_api_credential", "api_credential", credential.id, f"enterprise={enterprise_id};order={order.order_no}")
    db.commit()
    db.refresh(credential)
    return {**api_credential_out(credential, route), "api_key": raw_key, "route_key": route.route_key, "gateway_base_path": f"/gateway/{route.route_key}", "shared": True, "warning": "这是企业共享 API Key，仅在本次生成响应中返回，请妥善保存；同企业其它已支付订单会合并到该凭证"}


@app.post("/api/orders/{order_id}/api-credentials/{credential_id}/revoke")
def revoke_order_api_credential(order_id: str, credential_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    order, _, route, enterprise_id = api_order_context(order_id, user, db)
    credential = db.scalar(select(ApiCredential).where(ApiCredential.id == credential_id, ApiCredential.route_id == route.id, ApiCredential.enterprise_id == enterprise_id, ApiCredential.order_id == ""))
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
    audit(db, user.email or user.phone or user.id, "revoke_shared_api_credential", "api_credential", credential.id, f"enterprise={enterprise_id};order={order.order_no}")
    db.commit()
    return {"id": credential.id, "status": credential.status}


@app.post("/api/orders/{order_id}/api-credentials/{credential_id}/regenerate")
def regenerate_order_api_credential(order_id: str, credential_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    order, product, route, enterprise_id = api_order_context(order_id, user, db)
    old = db.scalar(select(ApiCredential).where(ApiCredential.id == credential_id, ApiCredential.route_id == route.id, ApiCredential.enterprise_id == enterprise_id, ApiCredential.order_id == ""))
    if not old:
        raise HTTPException(404, "API 凭据不存在")
    old.status = "revoked"
    raw_key = "mk_" + secrets.token_urlsafe(30)
    policy = api_entitlement_policy(db, enterprise_id, route)
    credential = ApiCredential(route_id=route.id, enterprise_id=enterprise_id, order_id="", product_version_id="", name=old.name, key_prefix=raw_key[:12], key_hash=hashlib.sha256(raw_key.encode()).hexdigest(), rate_limit_per_minute=policy["rate_limit_per_minute"] or old.rate_limit_per_minute, daily_quota=policy["daily_quota"] or old.daily_quota, monthly_quota=policy["monthly_quota"] or old.monthly_quota, total_quota=policy["total_quota"], expires_at=old.expires_at, created_by=user.email or user.phone or user.id)
    db.add(credential)
    db.flush()
    try:
        remove_apisix_consumer(old)
        sync_apisix_consumer(credential, raw_key, route)
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(502, f"API 凭据重生成同步 APISIX Consumer 失败：{exc}") from exc
    audit(db, user.email or user.phone or user.id, "regenerate_shared_api_credential", "api_credential", credential.id, f"enterprise={enterprise_id};from={old.id}")
    db.commit()
    db.refresh(credential)
    return {**api_credential_out(credential, route), "api_key": raw_key, "route_key": route.route_key, "gateway_base_path": f"/gateway/{route.route_key}", "warning": f"旧凭据 {old.key_prefix} 已停用，新 API Key 仅在本次响应中返回，请妥善保存"}


@app.get("/api/products/{product_id}/gateway-credentials")
def list_gateway_credentials(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    route = product_gateway_route(db, product.id)
    if not route:
        return {"items": []}
    target = first_enterprise(db, user).id
    if user.platform_role not in {"super_admin", "platform_operator"}:
        require_enterprise_admin(db, user, target)
    items = db.scalars(select(ApiCredential).where(ApiCredential.route_id == route.id, ApiCredential.enterprise_id == target).order_by(ApiCredential.created_at.desc())).all()
    return {"items": [{"id": x.id, "name": x.name, "key_prefix": x.key_prefix, "status": x.status, "rate_limit_per_minute": x.rate_limit_per_minute or route.rate_limit_per_minute, "daily_quota": x.daily_quota or route.daily_quota, "monthly_quota": x.monthly_quota or route.monthly_quota, "expires_at": x.expires_at, "last_used_at": x.last_used_at, "created_at": x.created_at} for x in items]}


@app.get("/api/products/{product_id}/gateway-usage")
def gateway_usage(product_id: str, version: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = gateway_product(product_id, db)
    membership = current_membership(db, user, product.enterprise_id)
    if membership.role not in {"super_admin", "enterprise_admin"} and user.platform_role not in {"super_admin", "platform_operator"}:
        raise HTTPException(403, "只有产品提供企业管理员或平台管理员可以查看调用统计")
    route = product_gateway_route(db, product.id, version)
    if not route:
        return {"summary": {"total": 0, "success": 0, "error": 0, "avg_latency_ms": 0}, "items": []}
    rows = db.scalars(select(ApiUsage).where(ApiUsage.route_id == route.id).order_by(ApiUsage.created_at.desc()).limit(1000)).all()
    total = len(rows)
    success = sum(1 for x in rows if x.status_code < 400)
    return {"summary": {"total": total, "success": success, "error": total - success, "avg_latency_ms": round(sum(x.latency_ms for x in rows) / total, 1) if total else 0}, "items": [{"method": x.method, "path": x.path, "status_code": x.status_code, "latency_ms": x.latency_ms, "request_bytes": x.request_bytes, "response_bytes": x.response_bytes, "created_at": x.created_at} for x in rows]}


SAAS_CYCLES = {"monthly": 1, "quarterly": 3, "annual": 12, "perpetual": None}


def add_calendar_months(value: datetime, months: int) -> datetime:
    """Advance a subscription by calendar months, clamping missing days to month end."""
    month_index = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def subscription_expiry(start: datetime, cycle: str) -> datetime | None:
    months = SAAS_CYCLES.get(cycle)
    return None if months is None else add_calendar_months(start, months)
SAAS_CYCLE_LABELS = {"monthly": "月付", "quarterly": "季付", "annual": "年付", "perpetual": "永久"}
_saas_tokens: dict[str, tuple[str, datetime]] = {}


def saas_version_out(item: SaaSProductVersion) -> dict[str, Any]:
    return {"id": item.id, "product_id": item.product_id, "version_code": item.version_code, "name": item.name, "description": item.description, "monthly_price": float(item.monthly_price or 0), "quarterly_price": float(item.quarterly_price or 0), "annual_price": float(item.annual_price or 0), "perpetual_price": float(item.perpetual_price or 0), "cost": float(item.cost or 0), "max_users": item.max_users, "max_departments": item.max_departments, "max_storage_gb": item.max_storage_gb, "status": item.status, "created_at": item.created_at, "updated_at": item.updated_at}


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
    creator = (subscription.created_by or "").strip()
    buyer = db.get(User, creator) if creator else None
    if buyer is None and creator:
        buyer = db.scalar(select(User).where(or_(User.email == creator, User.phone == creator)).order_by(User.id))
    if buyer is None:
        buyer = db.scalar(select(User).join(Membership, Membership.user_id == User.id).where(
            Membership.enterprise_id == subscription.enterprise_id,
            Membership.role == "super_admin", Membership.status == "active",
            User.is_active.is_(True), User.activation_status == "active",
            or_(User.platform_role.is_(None), User.platform_role == ""),
        ).order_by(User.id))
    if buyer is None:
        raise HTTPException(422, "SaaS 订阅无法解析购买人，请配置本企业活跃超级管理员")
    order = Order(order_no=make_order_no(), buyer_user_id=buyer.id, buyer_enterprise_id=subscription.enterprise_id, provider_enterprise_id=product.enterprise_id, product_id=product.id, product_version_id=version.id, product_version_code=version.version_code, product_version_name=version.name, billing_cycle=subscription.billing_cycle, subscription_id=subscription.id, business_type=business_type, related_order_id=related_order_id, product_name=product.name, buyer_name=enterprise.name if enterprise else "", amount=amount, paid_amount=amount if paid else 0, main_status="completed" if paid else "created", payment_status="paid" if paid else "unpaid")
    db.add(order)
    db.flush()
    db.add(Payment(order_id=order.id, payment_no="PAY-" + secrets.token_hex(6).upper(), amount=amount, status="paid" if paid else "unpaid", paid_at=now() if paid and business_type != "downgrade" else None))
    if paid and business_type != "downgrade":
        db.flush()
        settlement_metering["record_order_event"](db, order, "confirm_payment", "paid", buyer.id)
        notify_order_event(db, order, "confirm_payment", "paid")
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


def require_legacy_saas_payer(db: Session, user: User, enterprise_id: str) -> None:
    """Legacy purchase/renew simulate payment inline, so cannot admit administrators."""
    trading_policy["require_buyer"](db, user, enterprise_id)
    member = current_membership(db, user, enterprise_id)
    if member.role != "super_admin":
        raise HTTPException(403, "该旧订阅接口包含模拟付款，仅企业超级管理员可执行")


@app.post("/api/products/{product_id}/saas-subscriptions")
def create_saas_subscription(product_id: str, body: SaaSSubscriptionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product = saas_product(product_id, db)
    enterprise = first_enterprise(db, user)
    require_legacy_saas_payer(db, user, enterprise.id)
    version = db.scalar(select(SaaSProductVersion).where(SaaSProductVersion.id == body.version_id, SaaSProductVersion.product_id == product.id, SaaSProductVersion.status == "active"))
    config = db.scalar(select(SaaSIntegrationConfig).where(SaaSIntegrationConfig.product_id == product.id, SaaSIntegrationConfig.status == "active"))
    if not version or not config:
        raise HTTPException(400, "SaaS 版本或第三方接口配置不可用")
    saas_cycle_price(version, body.billing_cycle)
    starts = now()
    expires = subscription_expiry(starts, body.billing_cycle)
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
    require_legacy_saas_payer(db, user, subscription.enterprise_id)
    expires_at = subscription.expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if subscription.status == "active" and expires_at and expires_at <= now():
        owner = db.scalar(select(User.id).where(or_(User.id == subscription.created_by, User.email == subscription.created_by, User.phone == subscription.created_by))) if subscription.created_by else None
        notify_business_event(db, "SaaS 订阅已到期", "当前订阅周期已到期，请查看续费状态。", "saas_subscription", subscription.id,
                              event_key=f"saas:{subscription.id}:expired:{expires_at.isoformat()}", category="system", tenant_id=subscription.enterprise_id,
                              recipient_ids=[owner] if owner else [], enterprise_ids=[subscription.enterprise_id])
    amount = saas_cycle_price(version, body.billing_cycle)
    result = execute_saas_operation(db, subscription, "RENEW", {"tenant_id": subscription.external_tenant_id, "billing_cycle": body.billing_cycle, "enterprise_id": subscription.enterprise_id}, config, f"renew:{subscription.id}:{body.billing_cycle}:{subscription.expires_at}")
    if subscription.expires_at and body.billing_cycle != "perpetual": subscription.expires_at = subscription_expiry(subscription.expires_at, body.billing_cycle)
    elif body.billing_cycle != "perpetual": subscription.expires_at = subscription_expiry(now(), body.billing_cycle)
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
    total_days = max(1, ((subscription.expires_at - subscription.starts_at).total_seconds() / 86400) if subscription.expires_at and subscription.starts_at else 365)
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
    notify_order_event(db, order, "complete_refund", "refunded")
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
    return {"id": o.id, "order_no": o.order_no, "buyer_user_id": o.buyer_user_id, "buyer_enterprise_id": o.buyer_enterprise_id, "provider_enterprise_id": o.provider_enterprise_id, "buyer_name": o.buyer_name, "product_name": o.product_name, "product_version_id": o.product_version_id, "product_version_code": o.product_version_code, "product_version_name": o.product_version_name, "billing_cycle": o.billing_cycle, "subscription_months": o.subscription_months or 1, "delivery_method": o.delivery_method_snapshot or "", "snapshot_version": o.snapshot_version or 0, "unit_price": float(o.unit_price_snapshot) if o.unit_price_snapshot is not None else None, "subscription_id": o.subscription_id, "business_type": o.business_type, "related_order_id": o.related_order_id, "main_status": o.main_status, "payment_status": o.payment_status, "delivery_status": o.delivery_status, "after_sales_status": o.after_sales_status, "amount": float(o.amount or 0), "paid_amount": float(o.paid_amount or 0), "refunded_amount": float(o.refunded_amount or 0), "created_at": o.created_at, "updated_at": o.updated_at}


@app.get("/api/orders")
def orders(q: str = "", status: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    stmt = settlement_metering["scoped_orders"](db, user)
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
    settlement_metering["require_order_access"](db, user, order)
    logs = db.scalars(select(OrderStateLog).where(OrderStateLog.order_id == order.id).order_by(OrderStateLog.created_at)).all()
    payment = db.scalar(select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at.desc()))
    task = db.scalar(select(DeliveryTask).where(DeliveryTask.order_id == order.id).order_by(DeliveryTask.created_at.desc()))
    refunds = db.scalars(select(Refund).where(Refund.order_id == order.id).order_by(Refund.created_at.desc())).all()
    return {"order": order_out(order), "logs": [{"domain": x.domain, "from_status": x.from_status, "to_status": x.to_status, "action": x.action, "reason": x.reason, "operator": x.operator, "created_at": x.created_at} for x in logs], "payment": {"status": payment.status, "payment_no": payment.payment_no, "amount": float(payment.amount or 0), "proof": payment.proof} if payment else None, "refunds": [{"id": x.id, "refund_no": x.refund_no, "amount": float(x.amount or 0), "status": x.status, "reason": x.reason, "requested_by": x.requested_by, "completed_by": x.completed_by, "created_at": x.created_at, "completed_at": x.completed_at} for x in refunds], "delivery": {"status": task.status, "method": task.method, "assignee": task.assignee, "note": task.note} if task else None}


@app.post("/api/orders")
def create_order(body: OrderBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    buyer = trading_policy["require_buyer"](db, user, body.buyer_enterprise_id)
    quote = order_economics["quote"](db, user, body, lock=True)
    if body.quote_id and not hmac.compare_digest(body.quote_id, quote["public"]["quote_id"]):
        raise HTTPException(409, "商品报价或配置已变更，请重新确认报价")
    product, version = quote["product"], quote["version"]
    order = Order(order_no=make_order_no(), buyer_enterprise_id=buyer.id, buyer_user_id=user.id, provider_enterprise_id=product.enterprise_id, product_id=product.id, product_version_id=version.id, product_version_code=version.version_code, product_version_name=(getattr(version, "name", "") or version.description or version.version_code)[:120], product_name=product.name, buyer_name=buyer.name, amount=quote["amount"], billing_cycle="monthly" if quote["public"]["billing_unit"] == "month" else "", main_status="created")
    order_economics["apply_snapshot"](order, quote)
    order_workflow["initialize_order"](db, order, user)
    db.add(order)
    db.flush()
    db.add(Payment(order_id=order.id, payment_no="PAY-" + secrets.token_hex(6).upper(), amount=order.amount, status="unpaid"))
    db.add(OrderStateLog(order_id=order.id, domain="main", from_status="", to_status=order.main_status, action="提交订单", operator=user.name, reason="用户提交"))
    audit(db, user.email, "create_order", "order", order.id, order.order_no)
    db.commit()
    return order_out(order)


@app.post("/api/orders/{order_id}/transition")
def transition_order(order_id: str, body: TransitionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    order = db.scalar(select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True))
    if not order:
        raise HTTPException(404, "订单不存在")
    item = TRANSITIONS.get(body.action)
    if not item:
        raise HTTPException(400, "不支持的订单动作")
    domain, expected, target, label = item
    authorized = order_workflow["authorize_transition"](db, user, order, body.action)
    if not authorized:
        settlement_metering["require_transition"](db, user, order, body.action)
    if order_workflow["payment_repeat"](db, order, body.action):
        return order_out(order)
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
        if order.main_status not in {"created", "pending_review", "pending_provider_review", "pending_payment", "pending_fulfillment"} or order.payment_status not in {"unpaid", "paying"}:
            raise HTTPException(409, "当前订单状态不允许取消")
        old_main = order.main_status
        order.main_status = "cancelled"
        for route in db.scalars(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == order.product_id)).all():
            refresh_enterprise_api_credentials(db, order.buyer_enterprise_id, route)
        log_state(db, order, "main", old_main, "cancelled", body.action, user, body.reason)
        audit(db, user.email or user.phone or user.id, body.action, "order", order.id, body.reason)
        db.commit()
        return order_out(order)
    if body.action == "confirm_payment":
        payment = db.scalar(select(Payment).where(Payment.order_id == order.id).order_by(Payment.created_at.desc()))
        if not payment:
            raise HTTPException(400, "支付单不存在")
        if order.payment_status not in {"unpaid", "paying", "paid"} or payment.status not in {"unpaid", "paying", "paid"}:
            raise HTTPException(409, "当前支付状态不允许确认支付")
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
        payment.paid_at = payment.paid_at or now()
        order.payment_status = "paid"
        order.paid_amount = order.amount
        log_state(db, order, "payment", current, target, body.action, user, body.reason)
        if order.main_status in ["created", "pending_review", "pending_payment"]:
            old_main = order.main_status
            order.main_status = "pending_fulfillment"
            log_state(db, order, "main", old_main, order.main_status, "支付完成/生成任务", user, body.reason)
        if not db.scalar(select(DeliveryTask).where(DeliveryTask.order_id == order.id).order_by(DeliveryTask.created_at.desc())):
            create_delivery_task(db, order, user.email or user.name)
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
        create_refund_negative_settlement(db, refund, user.email or user.name)
        order.refunded_amount = Decimal(str(order.refunded_amount or 0)) + Decimal(str(refund.amount or 0))
        fully_refunded = Decimal(str(order.refunded_amount or 0)) >= Decimal(str(order.paid_amount or order.amount or 0))
        payment.status = "refunded" if fully_refunded else "paid"
        refund_target = "refunded" if fully_refunded else "paid"
        order.payment_status = refund_target
        if refund_target == "refunded" or order.refunded_amount > 0:
            for route in db.scalars(select(ApiGatewayRoute).where(ApiGatewayRoute.product_id == order.product_id)).all():
                refresh_enterprise_api_credentials(db, order.buyer_enterprise_id, route)
        if order.after_sales_status == "processing":
            old_after_sales = order.after_sales_status
            order.after_sales_status = "resolved"
            log_state(db, order, "after_sales", old_after_sales, "resolved", "退款完成", user, body.reason)
        log_state(db, order, "payment", current, refund_target, body.action, user, body.reason)
    elif body.action == "create_task":
        if order.delivery_status != "not_started":
            raise HTTPException(400, "当前交付状态不能生成任务")
        create_delivery_task(db, order, user.email or user.name)
        log_state(db, order, "delivery", current, target, body.action, user, body.reason)
    elif body.action == "submit_after_sales":
        if order.after_sales_status != "none":
            raise HTTPException(400, "当前订单已有售后事项")
        order.after_sales_status = "processing"
        level = current_service_level(db, order.buyer_enterprise_id, order.buyer_user_id or "")
        db.add(AfterSalesTicket(ticket_no="AS-" + secrets.token_hex(5).upper(), order_id=order.id, type="质量异议", description=body.reason or "客户提交售后申请", service_level_code=level.code if level else "standard", response_due_at=now() + timedelta(minutes=(level.initial_response_minutes if level else 2880)), owner="专属技术支持经理" if level and level.dedicated_manager else "售后团队"))
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
            task = db.scalar(select(DeliveryTask).where(DeliveryTask.order_id == order.id).order_by(DeliveryTask.created_at.desc()))
            if task:
                task.status = "completed"
                task.note = body.reason or task.note
        if body.action == "reject_delivery":
            order.main_status = "fulfilling"
            task = db.scalar(select(DeliveryTask).where(DeliveryTask.order_id == order.id).order_by(DeliveryTask.created_at.desc()))
            if task:
                task.status = "preparing"
                task.last_error = body.reason or "购买方拒绝履约结果"
                task.note = body.reason or task.note
        if body.action == "confirm_order":
            order.delivery_status = "accepted"
        if body.action == "retry_delivery":
            task = db.scalar(select(DeliveryTask).where(DeliveryTask.order_id == order.id).order_by(DeliveryTask.created_at.desc()))
            if task:
                task.status = "retrying" if task.delivery_mode == "automatic" else "preparing"
                task.next_retry_at = now() if task.delivery_mode == "automatic" else None
                task.last_error = ""
    db.flush()
    event_version = now().isoformat()
    if body.action == "confirm_payment":
        event_version = "paid"
    elif body.action in {"approve_refund", "complete_refund"}:
        latest_refund = db.scalar(select(Refund).where(Refund.order_id == order.id).order_by(Refund.created_at.desc()))
        event_version = f"{latest_refund.id}:{latest_refund.status}" if latest_refund else event_version
    settlement_metering["record_order_event"](db, order, body.action, event_version, user.id)
    notify_order_event(db, order, body.action, event_version)
    audit(db, user.email, body.action, "order", order.id, body.reason)
    db.commit()
    return order_out(order)


@app.post("/api/delivery-tasks/{task_id}/process")
def process_delivery_task(task_id: str, body: DeliveryProcessBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_delivery_operator(user)
    task = db.get(DeliveryTask, task_id)
    if not task:
        raise HTTPException(404, "交付任务不存在")
    order = db.get(Order, task.order_id)
    if not order:
        raise HTTPException(404, "关联订单不存在")
    if task.status not in {"in_delivery", "retrying", "preparing"}:
        raise HTTPException(409, "当前交付任务不允许处理")
    if body.success:
        task.status = "pending_acceptance"
        task.next_retry_at = None
        task.last_error = ""
        order.delivery_status = "pending_acceptance"
        order.main_status = "pending_confirmation"
        audit(db, user.email, "delivery_succeeded", "delivery_task", task.id, "自动交付成功", category="delivery", business_domain="delivery", order_id=order.id, after={"retry_count": task.retry_count})
    else:
        task.retry_count += 1
        task.last_error = body.error or "交付服务返回失败"
        if task.retry_count < task.max_retries:
            task.status = "retrying"
            task.next_retry_at = now() + timedelta(seconds=10)
            order.delivery_status = "in_delivery"
        else:
            task.status = "exception"
            task.next_retry_at = None
            order.delivery_status = "exception"
            order.main_status = "fulfilling"
        audit(db, user.email, "delivery_failed", "delivery_task", task.id, task.last_error, category="delivery", business_domain="delivery", order_id=order.id, risk_level="warning" if task.status != "exception" else "high", after={"retry_count": task.retry_count, "status": task.status, "next_retry_at": task.next_retry_at})
    event_version = f"{task.status}:{task.retry_count}" + (f":{now().isoformat()}" if body.success else "")
    settlement_metering["record_order_event"](db, order, "delivery_succeeded" if body.success else "delivery_failed", event_version, user.id, task.id)
    notify_order_event(db, order, "delivery_succeeded" if body.success else "delivery_failed", event_version, target_type="delivery_task", target_id=task.id)
    db.commit()
    return {"task": {"id": task.id, "status": task.status, "retry_count": task.retry_count, "max_retries": task.max_retries, "last_error": task.last_error, "next_retry_at": task.next_retry_at, "sla_due_at": task.sla_due_at}, "order": order_out(order)}


def delivery_task_access(task_id: str, user: User, db: Session) -> tuple[DeliveryTask, Order]:
    task = db.get(DeliveryTask, task_id)
    if not task:
        raise HTTPException(404, "交付任务不存在")
    order = db.get(Order, task.order_id)
    if not order:
        raise HTTPException(404, "关联订单不存在")
    if user.platform_role in {"super_admin", "platform_operator", "delivery_monitor"}:
        return task, order
    membership = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.enterprise_id.in_([order.buyer_enterprise_id, order.provider_enterprise_id]), Membership.status == "active", Membership.role.in_(["super_admin", "enterprise_admin"])))
    if not membership:
        raise HTTPException(403, "无权访问该履约任务")
    return task, order


@app.post("/api/delivery-tasks/{task_id}/attachments")
def upload_delivery_attachment(task_id: str, upload: UploadFile = File(...), description: str = Form(default=""), user: User = Depends(current_user), db: Session = Depends(db_session)):
    task, order = delivery_task_access(task_id, user, db)
    if task.status in {"completed", "cancelled"}:
        raise HTTPException(409, "履约任务已完成，不能继续上传附件")
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(0)
    if size > 100 * 1024 * 1024:
        raise HTTPException(413, "履约附件不能超过100MB")
    scan_status, scan_report = clamav_scan_stream(upload.file, size)
    if scan_status == "infected":
        audit(db, user.email or user.phone or user.id, "reject_infected_delivery_attachment", "delivery_task", task.id, scan_report, category="security", business_domain="delivery", order_id=order.id, risk_level="high")
        db.commit()
        raise HTTPException(400, "履约附件未通过病毒扫描")
    upload.file.seek(0)
    hasher = hashlib.sha256()
    while True:
        chunk = upload.file.read(1024 * 1024)
        if not chunk:
            break
        hasher.update(chunk)
    upload.file.seek(0)
    filename = upload.filename or "delivery-attachment"
    object_name = f"delivery/{order.id}/{task.id}/{secrets.token_hex(6)}-{filename}"
    if not MINIO_ENDPOINT:
        raise HTTPException(503, "文件存储服务未配置")
    client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=False)
    if not client.bucket_exists(MINIO_BUCKET):
        client.make_bucket(MINIO_BUCKET)
    client.put_object(MINIO_BUCKET, object_name, upload.file, length=size, content_type=upload.content_type or "application/octet-stream")
    file_item = FileObject(owner_id=user.id, object_name=object_name, original_name=filename, content_type=upload.content_type or "application/octet-stream", size=size, checksum=hasher.hexdigest(), file_role="delivery_attachment", version="", description=description, status="active", scan_status=scan_status, scan_report=scan_report, scanned_at=now())
    db.add(file_item)
    db.flush()
    attachment = DeliveryAttachment(task_id=task.id, file_id=file_item.id, description=description, uploaded_by=user.email or user.phone or user.id)
    db.add(attachment)
    audit(db, user.email or user.phone or user.id, "upload_delivery_attachment", "delivery_attachment", attachment.id, filename, category="delivery", business_domain="delivery", order_id=order.id, after={"file_id": file_item.id, "size": size, "scan_status": scan_status})
    db.commit()
    return {"id": attachment.id, "task_id": task.id, "file_id": file_item.id, "name": filename, "description": description, "uploaded_by": attachment.uploaded_by, "created_at": attachment.created_at}


@app.get("/api/delivery-tasks/{task_id}/attachments")
def list_delivery_attachments(task_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    task, _ = delivery_task_access(task_id, user, db)
    rows = db.scalars(select(DeliveryAttachment).where(DeliveryAttachment.task_id == task.id).order_by(DeliveryAttachment.created_at.desc())).all()
    files = {x.id: x for x in db.scalars(select(FileObject).where(FileObject.id.in_([item.file_id for item in rows]))).all()} if rows else {}
    return {"items": [{"id": x.id, "task_id": x.task_id, "file_id": x.file_id, "name": files.get(x.file_id).original_name if files.get(x.file_id) else "", "description": x.description, "uploaded_by": x.uploaded_by, "created_at": x.created_at} for x in rows]}


@app.get("/api/delivery-tasks")
def delivery_tasks(user: User = Depends(current_user), db: Session = Depends(db_session)):
    items = db.scalars(select(DeliveryTask).order_by(DeliveryTask.created_at.desc())).all()
    return {"items": [{"id": x.id, "order_id": x.order_id, "assignee": x.assignee, "method": x.method, "delivery_mode": x.delivery_mode, "status": x.status, "retry_count": x.retry_count, "max_retries": x.max_retries, "last_error": x.last_error, "next_retry_at": x.next_retry_at, "sla_due_at": x.sla_due_at, "note": x.note, "attachment_count": db.scalar(select(func.count(DeliveryAttachment.id)).where(DeliveryAttachment.task_id == x.id)) or 0, "created_at": x.created_at} for x in items]}


@app.get("/api/after-sales")
def after_sales(user: User = Depends(current_user), db: Session = Depends(db_session)):
    items = db.scalars(select(AfterSalesTicket).order_by(AfterSalesTicket.created_at.desc())).all()
    return {"items": [{"id": x.id, "ticket_no": x.ticket_no, "order_id": x.order_id, "type": x.type, "status": x.status, "priority": x.priority, "description": x.description, "owner": x.owner, "service_level_code": x.service_level_code, "response_due_at": x.response_due_at, "created_at": x.created_at} for x in items]}


@app.get("/api/settlements")
def settlements(batch_id: str | None = None, settlement_id: str | None = None, order_no: str | None = None, status: str | None = None, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_viewer(user)
    stmt = select(Settlement).where(Settlement.status != "superseded")
    if status:
        stmt = stmt.where(Settlement.status == status)
    if settlement_id:
        stmt = stmt.where(or_(Settlement.id == settlement_id, Settlement.settlement_no.ilike(f"%{settlement_id}%")))
    if batch_id:
        batch_match = select(SettlementLine.settlement_id).join(SettlementBatch, SettlementBatch.id == SettlementLine.batch_id).where(or_(SettlementBatch.id == batch_id, SettlementBatch.batch_no.ilike(f"%{batch_id}%")))
        stmt = stmt.where(Settlement.id.in_(batch_match))
    if order_no:
        stmt = stmt.where(Settlement.order_id.in_(select(Order.id).where(Order.order_no.ilike(f"%{order_no}%"))))
    items = db.scalars(stmt.order_by(Settlement.created_at.desc())).all()
    orders = {x.id: x for x in db.scalars(select(Order).where(Order.id.in_([item.order_id for item in items]))).all()} if items else {}
    line_rows = db.scalars(select(SettlementLine).where(SettlementLine.settlement_id.in_([item.id for item in items]))).all() if items else []
    batch_ids = {x.settlement_id: x.batch_id for x in line_rows}
    batches = {x.id: x for x in db.scalars(select(SettlementBatch).where(SettlementBatch.id.in_(list(set(batch_ids.values()))))).all()} if batch_ids else {}
    def rate(amount: Any, profit: Any) -> float:
        return round(float(Decimal(str(amount or 0)) / Decimal(str(profit or 0)) * 100), 4) if profit else 0.0
    return {"items": [{"id": x.id, "settlement_no": x.settlement_no, "batch_id": batch_ids.get(x.id, ""), "batch_no": batches.get(batch_ids.get(x.id)).batch_no if batches.get(batch_ids.get(x.id)) else "", "batch_status": batches.get(batch_ids.get(x.id)).status if batches.get(batch_ids.get(x.id)) else "", "order_id": x.order_id, "order_no": orders.get(x.order_id).order_no if orders.get(x.order_id) else "", "buyer_name": orders.get(x.order_id).buyer_name if orders.get(x.order_id) else "", "gross_amount": float(x.gross_amount or 0), "refund_amount": float(x.refund_amount or 0), "net_amount": float(x.net_amount or 0), "cost_amount": float(x.cost_amount or 0), "profit_amount": float(x.profit_amount or 0), "refund_recovery": float(x.refund_recovery or 0), "platform_fee": float(x.platform_fee or 0), "provider_share": float(x.provider_share or 0), "service_share": float(x.service_share or 0), "expert_fee": float(x.expert_fee or 0), "channel_fee": float(x.channel_fee or 0), "platform_rate": rate(x.platform_fee, x.profit_amount), "provider_rate": rate(x.provider_share, x.profit_amount), "service_rate": rate(x.service_share, x.profit_amount), "expert_rate": rate(x.expert_fee, x.profit_amount), "channel_rate": rate(x.channel_fee, x.profit_amount), "tax_amount": float(x.tax_amount or 0), "adjustment": float(x.adjustment or 0), "status": x.status, "created_at": x.created_at} for x in items]}


@app.get("/api/settlements/{settlement_id}/detail")
def settlement_detail(settlement_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_viewer(user)
    settlement = db.get(Settlement, settlement_id)
    if not settlement:
        settlement = db.scalar(select(Settlement).where(Settlement.settlement_no == settlement_id))
    if not settlement:
        raise HTTPException(404, "清算单不存在")
    order = db.get(Order, settlement.order_id)
    lines = db.scalars(select(SettlementLine).where(SettlementLine.settlement_id == settlement.id, SettlementLine.status != "superseded").order_by(SettlementLine.created_at)).all()
    batch = db.get(SettlementBatch, lines[0].batch_id) if lines else None
    profit = Decimal(str(settlement.profit_amount or 0))
    def participant_out(participant_type: str, name: str, amount: Any) -> dict[str, Any]:
        return {"participant_type": participant_type, "participant_name": name, "rate": round(float(Decimal(str(amount or 0)) / profit * 100), 4) if profit else 0.0, "amount": float(amount or 0)}
    participant_map = {line.participant_type: participant_out(line.participant_type, line.participant_name, line.amount) for line in lines}
    defaults = [("platform", "平台运营方"), ("provider", "数据/服务提供方"), ("service", "数据服务方"), ("expert", "专家"), ("channel", "渠道")]
    participants = [participant_map.get(kind, participant_out(kind, name, 0)) for kind, name in defaults]
    audit_ids = {settlement.id, settlement.settlement_no}
    # Scope lifecycle records by the current settlement ID. A batch contains
    # multiple orders, so batch_no/order_id alone would leak other settlements.
    lifecycle_logs = db.scalars(select(AuditLog).where(AuditLog.target_id.in_(audit_ids), AuditLog.business_domain == "settlement").order_by(AuditLog.created_at)).all()
    lifecycle = [{"action": item.action, "actor": item.actor, "result": item.result, "detail": item.detail, "before": json.loads(item.before_json or "{}"), "after": json.loads(item.after_json or "{}"), "created_at": item.created_at} for item in lifecycle_logs]
    proposals = db.scalars(select(SettlementAdjustmentProposal).where(SettlementAdjustmentProposal.settlement_id == settlement.id).order_by(SettlementAdjustmentProposal.created_at.desc())).all()
    return {"id": settlement.id, "settlement_no": settlement.settlement_no, "status": settlement.status, "created_at": settlement.created_at, "batch": {"id": batch.id, "batch_no": batch.batch_no, "status": batch.status, "cycle": batch.cycle, "period_start": batch.period_start, "period_end": batch.period_end, "created_at": batch.created_at} if batch else None, "order": {"id": order.id, "order_no": order.order_no, "buyer_name": order.buyer_name, "product_name": order.product_name, "payment_status": order.payment_status, "created_at": order.created_at} if order else None, "amounts": {"gross_amount": float(settlement.gross_amount or 0), "cost_amount": float(settlement.cost_amount or 0), "profit_amount": float(settlement.profit_amount or 0), "refund_amount": float(settlement.refund_amount or 0), "net_amount": float(settlement.net_amount or 0)}, "participants": participants, "proposals": [{"id": item.id, "status": item.status, "proposed_by": item.proposed_by, "reason": item.reason, "values": json.loads(item.values_json or "{}"), "reviewed_by": item.reviewed_by, "reviewed_at": item.reviewed_at, "review_comment": item.review_comment, "created_at": item.created_at} for item in proposals], "lifecycle": lifecycle}


@app.post("/api/settlements/generate/{order_id}")
def generate_settlement(order_id: str, body: SettlementRuleBody | None = None, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    order = db.get(Order, order_id)
    if not order or order.payment_status not in {"paid", "refunding", "refunded"}:
        raise HTTPException(400, "订单尚未满足清算条件")
    existing = db.scalar(select(Settlement).where(Settlement.order_id == order.id, Settlement.is_refund.is_(False), Settlement.status != "superseded").order_by(Settlement.created_at.desc()))
    if existing and existing.status in {"locked", "paid", "archived", "disputed"}:
        raise HTTPException(409, "订单清算单已锁定、付款或存在待处理提案，不允许覆盖")
    if existing and db.scalar(select(SettlementLine.id).where(SettlementLine.settlement_id == existing.id, SettlementLine.status != "superseded")):
        raise HTTPException(409, "清算单已关联清算批次，请通过批次重算或调整提案处理")
    body = body or SettlementRuleBody()
    gross = Decimal(str(order.paid_amount or order.amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    refund_amount = min(Decimal(str(order.refunded_amount or 0)), gross).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    net_amount = max(gross - refund_amount, Decimal("0.00"))
    cost_amount = order_cost(db, order)
    profit_amount = (net_amount - cost_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    distributable = max(profit_amount, Decimal("0.00"))
    if order.snapshot_version == 1:
        rates, _ = settlement_values_for_order(db, order, None)
        body = SettlementRuleBody(**{key: float(value) for key, value in rates.items() if key != "provider_rate"})
    platform_fee = (distributable * Decimal(str(body.platform_rate)) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    service_share = (distributable * Decimal(str(body.service_rate)) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    expert_fee = (distributable * Decimal(str(body.expert_rate)) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    channel_fee = (distributable * Decimal(str(body.channel_rate)) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Tax is borne and declared by the relevant participants; it is not a
    # standalone deduction from the profit allocation base.
    tax_amount = Decimal("0.00")
    provider_share = (distributable - platform_fee - service_share - expert_fee - channel_fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    settlement = existing or Settlement(settlement_no="SET-" + secrets.token_hex(6).upper(), order_id=order.id)
    settlement.gross_amount = gross
    settlement.refund_amount = refund_amount
    settlement.net_amount = net_amount
    settlement.refund_recovery = refund_amount
    settlement.cost_amount = cost_amount
    settlement.profit_amount = profit_amount
    settlement.platform_fee = platform_fee
    settlement.service_share = service_share
    settlement.expert_fee = expert_fee
    settlement.channel_fee = channel_fee
    settlement.tax_amount = tax_amount
    settlement.provider_share = provider_share
    settlement.status = "pending"
    if not existing:
        db.add(settlement)
    audit(db, user.email, "generate_settlement", "settlement", settlement.settlement_no, f"cost={cost_amount} profit={profit_amount}", category="settlement", business_domain="settlement", order_id=order.id, after={"cost_amount": float(cost_amount), "profit_amount": float(profit_amount), "distributable_profit": float(distributable)})
    db.commit()
    db.refresh(settlement)
    return {"id": settlement.id, "settlement_no": settlement.settlement_no, "order_id": settlement.order_id, "gross_amount": float(settlement.gross_amount), "refund_amount": float(settlement.refund_amount), "net_amount": float(settlement.net_amount), "cost_amount": float(settlement.cost_amount), "profit_amount": float(settlement.profit_amount), "refund_recovery": float(settlement.refund_recovery), "platform_fee": float(settlement.platform_fee), "provider_share": float(settlement.provider_share), "service_share": float(settlement.service_share), "expert_fee": float(settlement.expert_fee), "channel_fee": float(settlement.channel_fee), "tax_amount": float(settlement.tax_amount), "adjustment": float(settlement.adjustment), "status": settlement.status}


@app.post("/api/settlements/{settlement_id}/adjust")
def adjust_settlement(settlement_id: str, body: SettlementAdjustmentBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    settlement = db.get(Settlement, settlement_id)
    if not settlement:
        raise HTTPException(404, "清算单不存在")
    if settlement.status in {"locked", "paid", "superseded"}:
        raise HTTPException(409, "当前清算单已锁定、付款或作废，不能直接调整")
    line = db.scalar(select(SettlementLine).where(SettlementLine.settlement_id == settlement.id))
    batch = db.get(SettlementBatch, line.batch_id) if line else None
    if settlement.status == "disputed":
        raise HTTPException(409, "当前清算单处于异议状态，请通过调整提案处理")
    if batch and batch.status not in {"generated", "pending_confirm"}:
        raise HTTPException(409, "当前清算批次已确认或付款，不能直接调整")
    rates = [body.platform_rate, body.provider_rate, body.service_rate, body.expert_rate, body.channel_rate]
    if abs(sum(rates) - 100) > 0.01:
        raise HTTPException(400, "五方分成比例合计必须为100%")
    expected_profit = Decimal(str(body.gross_amount)) - Decimal(str(body.cost_amount))
    if expected_profit != Decimal(str(body.profit_amount)):
        raise HTTPException(400, "订单利润必须等于订单金额减订单成本")
    before = {"status": settlement.status, "gross_amount": float(settlement.gross_amount or 0), "cost_amount": float(settlement.cost_amount or 0), "profit_amount": float(settlement.profit_amount or 0), "platform_fee": float(settlement.platform_fee or 0), "provider_share": float(settlement.provider_share or 0), "service_share": float(settlement.service_share or 0), "expert_fee": float(settlement.expert_fee or 0), "channel_fee": float(settlement.channel_fee or 0)}
    profit = Decimal(str(body.profit_amount)).quantize(Decimal("0.01"))
    amounts = [
        ("platform", (profit * Decimal(str(body.platform_rate)) / 100).quantize(Decimal("0.01"))),
        ("provider", (profit * Decimal(str(body.provider_rate)) / 100).quantize(Decimal("0.01"))),
        ("service", (profit * Decimal(str(body.service_rate)) / 100).quantize(Decimal("0.01"))),
        ("expert", (profit * Decimal(str(body.expert_rate)) / 100).quantize(Decimal("0.01"))),
        ("channel", (profit * Decimal(str(body.channel_rate)) / 100).quantize(Decimal("0.01"))),
    ]
    settlement.gross_amount = Decimal(str(body.gross_amount)).quantize(Decimal("0.01"))
    settlement.cost_amount = Decimal(str(body.cost_amount)).quantize(Decimal("0.01"))
    settlement.profit_amount = profit
    settlement.net_amount = settlement.gross_amount - Decimal(str(settlement.refund_amount or 0))
    settlement.platform_fee, settlement.provider_share, settlement.service_share, settlement.expert_fee, settlement.channel_fee = [amount for _, amount in amounts]
    adjustment = SettlementAdjustment(settlement_id=settlement.id, amount=profit - Decimal(str(before["profit_amount"])), reason=body.reason, created_by=user.name)
    settlement.adjustment = Decimal(str(settlement.adjustment or 0)) + adjustment.amount
    settlement.status = "adjusted"
    db.add(adjustment)
    for line in db.scalars(select(SettlementLine).where(SettlementLine.settlement_id == settlement.id)).all():
        amount = next((value for participant_type, value in amounts if participant_type == line.participant_type), Decimal("0"))
        line.amount = amount
    after = {"status": settlement.status, "gross_amount": float(settlement.gross_amount), "cost_amount": float(settlement.cost_amount), "profit_amount": float(settlement.profit_amount), "platform_fee": float(settlement.platform_fee), "provider_share": float(settlement.provider_share), "service_share": float(settlement.service_share), "expert_fee": float(settlement.expert_fee), "channel_fee": float(settlement.channel_fee)}
    audit(db, user.email, "adjust_settlement", "settlement", settlement.settlement_no, body.reason, category="settlement_adjustment", business_domain="settlement", order_id=settlement.order_id, batch_no=batch.batch_no if batch else "", before=before, after=after)
    db.commit()
    return {"settlement_no": settlement.settlement_no, "adjustment": float(settlement.adjustment), "status": settlement.status, "before": before, "after": after}


@app.post("/api/settlements/{settlement_id}/proposals")
def create_settlement_adjustment_proposal(settlement_id: str, body: SettlementAdjustmentBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    settlement = db.get(Settlement, settlement_id)
    if not settlement:
        raise HTTPException(404, "清算单不存在")
    if settlement.status in {"locked", "paid", "superseded"}:
        raise HTTPException(409, "当前清算单已锁定、付款或作废，不能提交调整提案")
    line = db.scalar(select(SettlementLine).where(SettlementLine.settlement_id == settlement.id, SettlementLine.status != "superseded"))
    batch = db.get(SettlementBatch, line.batch_id) if line else None
    if batch and batch.status not in {"generated", "pending_confirm"}:
        raise HTTPException(409, "当前清算批次已确认或付款，不能提交调整提案")
    rates = [body.platform_rate, body.provider_rate, body.service_rate, body.expert_rate, body.channel_rate]
    if abs(sum(rates) - 100) > 0.01:
        raise HTTPException(400, "五方分成比例合计必须为100%")
    if Decimal(str(body.gross_amount)) - Decimal(str(body.cost_amount)) != Decimal(str(body.profit_amount)):
        raise HTTPException(400, "订单利润必须等于订单金额减订单成本")
    pending = db.scalar(select(SettlementAdjustmentProposal).where(SettlementAdjustmentProposal.settlement_id == settlement.id, SettlementAdjustmentProposal.status == "pending"))
    if pending:
        raise HTTPException(409, "当前清算单已有待处理调整提案")
    values = body.model_dump(exclude={"reason"})
    proposal = SettlementAdjustmentProposal(settlement_id=settlement.id, proposed_by=user.email or user.name, reason=body.reason, values_json=json.dumps(values), status="pending")
    current_profit = Decimal(str(settlement.profit_amount or 0))
    current_rate = lambda amount: float((Decimal(str(amount or 0)) / current_profit * 100).quantize(Decimal("0.01"))) if current_profit else 0.0
    before = {"status": settlement.status, "gross_amount": float(settlement.gross_amount or 0), "cost_amount": float(settlement.cost_amount or 0), "profit_amount": float(settlement.profit_amount or 0), "platform_rate": current_rate(settlement.platform_fee), "provider_rate": current_rate(settlement.provider_share), "service_rate": current_rate(settlement.service_share), "expert_rate": current_rate(settlement.expert_fee), "channel_rate": current_rate(settlement.channel_fee)}
    settlement.status = "disputed"
    db.add(proposal)
    db.flush()
    audit(db, user.email, "create_settlement_adjustment_proposal", "settlement", settlement.settlement_no, body.reason, category="settlement_adjustment", business_domain="settlement", order_id=settlement.order_id, batch_no=batch.batch_no if batch else "", risk_level="high", before=before, after={"status": settlement.status, "proposal_id": proposal.id, **values})
    db.commit()
    db.refresh(proposal)
    return {"id": proposal.id, "settlement_id": proposal.settlement_id, "status": proposal.status, "proposed_by": proposal.proposed_by, "values": values, "created_at": proposal.created_at}


@app.get("/api/settlements/{settlement_id}/proposals")
def settlement_adjustment_proposals(settlement_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_viewer(user)
    items = db.scalars(select(SettlementAdjustmentProposal).where(SettlementAdjustmentProposal.settlement_id == settlement_id).order_by(SettlementAdjustmentProposal.created_at.desc())).all()
    return {"items": [{"id": item.id, "settlement_id": item.settlement_id, "status": item.status, "proposed_by": item.proposed_by, "reason": item.reason, "values": json.loads(item.values_json or "{}"), "reviewed_by": item.reviewed_by, "reviewed_at": item.reviewed_at, "review_comment": item.review_comment, "created_at": item.created_at} for item in items]}


@app.post("/api/settlement-proposals/{proposal_id}/decision")
def decide_settlement_adjustment_proposal(proposal_id: str, body: SettlementProposalDecisionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    proposal = db.get(SettlementAdjustmentProposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "调整提案不存在")
    if proposal.status != "pending":
        raise HTTPException(409, "调整提案已经处理")
    if proposal.proposed_by == (user.email or user.name):
        raise HTTPException(409, "调整提案不能由提交人本人确认")
    settlement = db.get(Settlement, proposal.settlement_id)
    if not settlement or settlement.status != "disputed":
        raise HTTPException(409, "关联清算单当前不在异议状态")
    before = {"status": settlement.status}
    proposal.status = "accepted" if body.decision == "approve" else "rejected"
    proposal.reviewed_by = user.email or user.name
    proposal.reviewed_at = now()
    proposal.review_comment = body.comment
    if body.decision == "approve":
        values = json.loads(proposal.values_json or "{}")
        profit = Decimal(str(values["profit_amount"])).quantize(Decimal("0.01"))
        amounts = {"platform": (profit * Decimal(str(values["platform_rate"])) / 100).quantize(Decimal("0.01")), "provider": (profit * Decimal(str(values["provider_rate"])) / 100).quantize(Decimal("0.01")), "service": (profit * Decimal(str(values["service_rate"])) / 100).quantize(Decimal("0.01")), "expert": (profit * Decimal(str(values["expert_rate"])) / 100).quantize(Decimal("0.01")), "channel": (profit * Decimal(str(values["channel_rate"])) / 100).quantize(Decimal("0.01"))}
        settlement.gross_amount = Decimal(str(values["gross_amount"])).quantize(Decimal("0.01"))
        settlement.cost_amount = Decimal(str(values["cost_amount"])).quantize(Decimal("0.01"))
        settlement.profit_amount = profit
        settlement.net_amount = settlement.gross_amount - Decimal(str(settlement.refund_amount or 0))
        settlement.platform_fee, settlement.provider_share, settlement.service_share, settlement.expert_fee, settlement.channel_fee = amounts["platform"], amounts["provider"], amounts["service"], amounts["expert"], amounts["channel"]
        for line in db.scalars(select(SettlementLine).where(SettlementLine.settlement_id == settlement.id, SettlementLine.status != "superseded")).all():
            line.amount = amounts.get(line.participant_type, Decimal("0"))
        settlement.status = "adjusted"
    else:
        settlement.status = "proposal_rejected"
    audit(db, user.email, "decide_settlement_adjustment_proposal", "settlement_proposal", proposal.id, body.comment, category="settlement_adjustment", business_domain="settlement", order_id=settlement.order_id, before=before, after={"status": settlement.status, "proposal_status": proposal.status, "reviewed_by": proposal.reviewed_by})
    db.commit()
    return {"id": proposal.id, "status": proposal.status, "settlement_id": settlement.id, "settlement_status": settlement.status}


@app.post("/api/settlements/{settlement_id}/lock")
def lock_settlement(settlement_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    settlement = db.get(Settlement, settlement_id)
    if not settlement:
        raise HTTPException(404, "清算单不存在")
    if settlement.status in {"locked", "paid", "superseded"}:
        raise HTTPException(409, "当前清算单已经锁定、付款或作废")
    if settlement.status == "disputed" or db.scalar(select(SettlementAdjustmentProposal.id).where(SettlementAdjustmentProposal.settlement_id == settlement.id, SettlementAdjustmentProposal.status == "pending")):
        raise HTTPException(409, "清算单存在未处理调整提案，请先由对方确认或拒绝")
    line = db.scalar(select(SettlementLine).where(SettlementLine.settlement_id == settlement.id, SettlementLine.status != "superseded"))
    batch = db.get(SettlementBatch, line.batch_id) if line else None
    if batch and batch.status in {"confirmed", "payment_processing", "partial_paid", "paid", "archived"}:
        raise HTTPException(409, "当前清算批次已确认或进入付款流程")
    before = {"status": settlement.status}
    settlement.status = "locked"
    if line:
        for related_line in db.scalars(select(SettlementLine).where(SettlementLine.settlement_id == settlement.id, SettlementLine.status != "superseded")).all():
            related_line.status = "locked"
    batch_after = batch.status if batch else ""
    if batch:
        settlement_ids = select(SettlementLine.settlement_id).where(SettlementLine.batch_id == batch.id, SettlementLine.status != "superseded")
        related = db.scalars(select(Settlement).where(Settlement.id.in_(settlement_ids), Settlement.status != "superseded")).all()
        if related and all(item.status == "locked" for item in related):
            batch.status = "pending_confirm"
            batch_after = batch.status
    audit(db, user.email, "lock_settlement", "settlement", settlement.settlement_no, "清算单锁定", category="settlement", business_domain="settlement", order_id=settlement.order_id, batch_no=batch.batch_no if batch else "", before=before, after={"status": settlement.status, "batch_status": batch_after})
    if batch and batch_after == "pending_confirm":
        audit(db, user.email, "settlement_batch_ready_for_confirmation", "settlement_batch", batch.batch_no, "所有关联清算单已锁定", category="settlement", business_domain="settlement", batch_no=batch.batch_no, before={"status": "generated"}, after={"status": batch.status})
    db.commit()
    return {"settlement_no": settlement.settlement_no, "status": settlement.status, "batch_no": batch.batch_no if batch else "", "batch_status": batch.status if batch else ""}


def settlement_rule_out(item: SettlementRule) -> dict[str, Any]:
    return {"id": item.id, "rule_no": item.rule_no, "name": item.name, "version": item.version, "scope": json.loads(item.scope_json or "{}"), "platform_rate": float(item.platform_rate or 0), "provider_rate": float(item.provider_rate or 0), "service_rate": float(item.service_rate or 0), "expert_rate": float(item.expert_rate or 0), "channel_rate": float(item.channel_rate or 0), "tax_rate": float(item.tax_rate or 0), "effective_at": item.effective_at, "expires_at": item.expires_at, "status": item.status, "approved_by": item.approved_by, "approved_at": item.approved_at, "change_reason": item.change_reason, "created_by": item.created_by, "created_at": item.created_at}


@app.get("/api/settlement-rules")
def settlement_rules(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_viewer(user)
    require_settlement_operator(user)
    items = db.scalars(select(SettlementRule).order_by(SettlementRule.created_at.desc())).all()
    return {"items": [settlement_rule_out(x) for x in items]}


@app.post("/api/settlement-rules")
def create_settlement_rule(body: SettlementRuleCreateBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    total = sum(Decimal(str(x)) for x in [body.platform_rate, body.provider_rate, body.service_rate, body.expert_rate, body.channel_rate])
    if total > Decimal("100"):
        raise HTTPException(400, "分配比例和税费比例不能超过 100%")
    item = SettlementRule(rule_no="RULE-" + secrets.token_hex(5).upper(), name=body.name, version=body.version, scope_json=json.dumps(body.scope, ensure_ascii=False), formula_json=json.dumps({"basis": "net_paid_minus_version_cost", "tax": "participant_profile"}, ensure_ascii=False), platform_rate=body.platform_rate, provider_rate=body.provider_rate, service_rate=body.service_rate, expert_rate=body.expert_rate, channel_rate=body.channel_rate, tax_rate=0, effective_at=body.effective_at, expires_at=body.expires_at, change_reason=body.change_reason, created_by=user.email or user.phone or user.name)
    db.add(item)
    audit(db, user.email, "create_settlement_rule", "settlement_rule", item.rule_no, body.change_reason, category="settlement_rule", business_domain="settlement")
    db.commit()
    db.refresh(item)
    return settlement_rule_out(item)


@app.post("/api/settlement-rules/{rule_id}/decision")
def decide_settlement_rule(rule_id: str, body: SettlementRuleDecisionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    item = db.get(SettlementRule, rule_id)
    if not item:
        raise HTTPException(404, "清算规则不存在")
    if body.decision not in {"approve", "reject", "activate", "disable"}:
        raise HTTPException(400, "不支持的规则动作")
    before = {"status": item.status}
    if body.decision == "activate":
        db.execute(update(SettlementRule).where(SettlementRule.status == "active", SettlementRule.id != item.id).values(status="disabled"))
    item.status = {"approve": "approved", "activate": "active", "reject": "rejected", "disable": "disabled"}[body.decision]
    if body.decision in {"approve", "activate"}:
        item.approved_by = user.email or user.name
        item.approved_at = now()
    audit(db, user.email, f"{body.decision}_settlement_rule", "settlement_rule", item.rule_no, body.comment, category="settlement_rule", business_domain="settlement", rule_version=item.version, before=before, after={"status": item.status})
    db.commit()
    return settlement_rule_out(item)


@app.post("/api/settlement-rules/{rule_id}/simulate")
def simulate_settlement_rule(rule_id: str, body: SettlementBatchBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    rule = db.get(SettlementRule, rule_id)
    if not rule:
        raise HTTPException(404, "清算规则不存在")
    order_ids = body.order_ids or [x.id for x in db.scalars(select(Order).where(Order.payment_status.in_(["paid", "refunded"])).limit(20)).all()]
    rows = []
    for order_id in order_ids:
        order = db.get(Order, order_id)
        if not order:
            continue
        gross = Decimal(str(order.paid_amount or order.amount or 0))
        refund = min(Decimal(str(order.refunded_amount or 0)), gross)
        net = max(gross - refund, Decimal("0"))
        cost_amount = order_cost(db, order)
        profit = (net - cost_amount).quantize(Decimal("0.01"))
        distributable = max(profit, Decimal("0"))
        rates, matched_rule_version = settlement_values_for_order(db, order, rule)
        platform_fee = (distributable * rates["platform_rate"] / 100).quantize(Decimal("0.01"))
        service_share = (distributable * rates["service_rate"] / 100).quantize(Decimal("0.01"))
        expert_fee = (distributable * rates["expert_rate"] / 100).quantize(Decimal("0.01"))
        channel_fee = (distributable * rates["channel_rate"] / 100).quantize(Decimal("0.01"))
        provider_share = (distributable - platform_fee - service_share - expert_fee - channel_fee).quantize(Decimal("0.01"))
        rows.append({"order_id": order.id, "order_no": order.order_no, "rule_version": matched_rule_version, "net_amount": float(net), "cost_amount": float(cost_amount), "profit_amount": float(profit), "platform_fee": float(platform_fee), "provider_share": float(provider_share), "service_share": float(service_share), "expert_fee": float(expert_fee), "channel_fee": float(channel_fee), "tax_amount": 0.0})
    audit(db, user.email, "simulate_settlement_rule", "settlement_rule", rule.rule_no, f"orders={len(rows)}", category="settlement_rule", business_domain="settlement", rule_version=rule.version)
    db.commit()
    return {"rule": settlement_rule_out(rule), "items": rows, "total": len(rows)}


@app.post("/api/settlement-measurements")
def create_settlement_measurement(body: SettlementMeasurementBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    item, duplicate = settlement_metering["create_manual"](db, user, body)
    db.commit()
    db.refresh(item)
    return {**settlement_metering["output"](item), "idempotent": duplicate}


@app.get("/api/settlement-measurements")
def settlement_measurements(order_id: str | None = None, offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500), source: str = Query(default="", max_length=120), source_id: str = Query(default="", max_length=180), event_key: str = Query(default="", max_length=180), user: User = Depends(current_user), db: Session = Depends(db_session)):
    return settlement_metering["list"](db, user, order_id, offset, limit, source, source_id, event_key)


@app.post("/api/settlement-batches")
def create_settlement_batch(body: SettlementBatchBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    if body.cycle == "monthly" and (not body.period_start or not body.period_end):
        raise HTTPException(400, "按月清算必须提供自然月起止时间")
    key = body.idempotency_key or f"{body.cycle}:{body.period_start}:{body.period_end}:{','.join(sorted(body.order_ids))}"
    if len(key) > 120:
        key = "auto:" + hashlib.sha256(key.encode()).hexdigest()
    request_digest = hashlib.sha256(json.dumps({"cycle": body.cycle, "start": str(body.period_start), "end": str(body.period_end), "orders": sorted(set(body.order_ids)), "rule": body.rule_id}, sort_keys=True).encode()).hexdigest()
    connection = db.connection()
    if connection.dialect.name == "postgresql":
        lock_scope = f"monthly:{body.period_start}:{body.period_end}" if body.cycle == "monthly" else key
        lock_id = int.from_bytes(hashlib.sha256(lock_scope.encode()).digest()[:8], "big", signed=True)
        db.execute(select(func.pg_advisory_xact_lock(lock_id)))
    elif connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    existing = db.scalar(select(SettlementBatch).where(SettlementBatch.idempotency_key == key))
    if existing and existing.status != "superseded":
        def normalized(value):
            return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)) if value else None
        if (existing.request_digest and existing.request_digest != request_digest) or existing.cycle != body.cycle or normalized(existing.period_start) != normalized(body.period_start) or normalized(existing.period_end) != normalized(body.period_end) or (body.rule_id and existing.rule_id != body.rule_id):
            raise HTTPException(409, "幂等键已用于不同的清算周期或规则")
        return {"id": existing.id, "batch_no": existing.batch_no, "status": existing.status, "idempotent": True}
    if body.cycle == "monthly":
        existing_period = db.scalar(select(SettlementBatch).where(SettlementBatch.cycle == "monthly", SettlementBatch.period_start == body.period_start, SettlementBatch.period_end == body.period_end, SettlementBatch.status != "superseded").order_by(SettlementBatch.created_at.desc()))
        if existing_period:
            if existing_period.status in {"confirmed", "payment_processing", "partial_paid", "paid", "archived"}:
                raise HTTPException(409, "该月份清算批次已确认或进入付款流程，不允许重新生成")
            if not body.rebuild:
                raise HTTPException(409, f"该月份已有未完成清算批次 {existing_period.batch_no}，如需重算请确认作废原批次")
            old_settlement_ids = select(SettlementLine.settlement_id).where(SettlementLine.batch_id == existing_period.id)
            if db.scalar(select(SettlementLine.id).where(SettlementLine.batch_id == existing_period.id, SettlementLine.status == "paid")):
                raise HTTPException(409, "该月份批次存在已付款清算明细，不允许重新生成")
            db.execute(update(Settlement).where(Settlement.id.in_(old_settlement_ids)).values(status="superseded"))
            db.execute(update(SettlementLine).where(SettlementLine.batch_id == existing_period.id).values(status="superseded"))
            existing_period.status = "superseded"
            audit(db, user.email, "supersede_settlement_batch", "settlement_batch", existing_period.batch_no, "按月清算重算，原批次作废", category="settlement", business_domain="settlement", batch_no=existing_period.batch_no, risk_level="warning")
    rule = db.get(SettlementRule, body.rule_id) if body.rule_id else db.scalar(select(SettlementRule).where(SettlementRule.status == "active").order_by(SettlementRule.created_at.desc()))
    if not rule:
        rule = SettlementRule(rule_no="RULE-DEFAULT", version="v1", name="默认清算规则", status="active", platform_rate=8, provider_rate=67, service_rate=20, expert_rate=5, tax_rate=0, created_by="system")
        db.add(rule)
        db.flush()
    order_stmt = select(Order).join(Payment, Payment.order_id == Order.id).where(Payment.status.in_(["paid", "refunding", "refunded"]), Payment.paid_at.is_not(None))
    if body.period_start:
        order_stmt = order_stmt.where(Payment.paid_at >= body.period_start)
    if body.period_end:
        order_stmt = order_stmt.where(Payment.paid_at < body.period_end)
    if body.order_ids:
        order_stmt = order_stmt.where(Order.id.in_(body.order_ids))
    order_ids = list(dict.fromkeys(db.scalars(order_stmt.with_only_columns(Order.id).distinct()).all()))
    if body.cycle == "monthly" and order_ids:
        legacy_batches = db.scalars(select(SettlementBatch).where(SettlementBatch.status.notin_(["superseded", "confirmed", "paid", "archived"]), SettlementBatch.id != (existing_period.id if body.cycle == "monthly" and existing_period else ""), SettlementBatch.id.in_(select(SettlementLine.batch_id).where(SettlementLine.settlement_id.in_(select(Settlement.id).where(Settlement.order_id.in_(order_ids))))))).all()
        for legacy in legacy_batches:
            legacy.status = "superseded"
            audit(db, user.email, "supersede_legacy_settlement_batch", "settlement_batch", legacy.batch_no, "月度批次覆盖旧手工批次", category="settlement", business_domain="settlement", batch_no=legacy.batch_no, risk_level="warning")
    batch = SettlementBatch(batch_no="BATCH-" + secrets.token_hex(6).upper(), cycle=body.cycle, period_start=body.period_start, period_end=body.period_end, rule_id=rule.id, status="generated", idempotency_key=key, request_digest=request_digest, created_by=user.email or user.name)
    db.add(batch)
    db.flush()
    total = Decimal("0")
    total_profit = Decimal("0")
    exceptions = 0
    for order_id in order_ids:
        order = db.get(Order, order_id)
        if not order or order.payment_status not in {"paid", "refunding", "refunded"}:
            continue
        prior_settlements = db.scalars(select(Settlement).where(Settlement.order_id == order.id, Settlement.is_refund.is_(False), Settlement.status != "superseded")).all()
        finalized = next((item for item in prior_settlements if item.status in {"locked", "paid", "archived"}), None)
        if finalized:
            audit(db, user.email, "skip_finalized_settlement_order", "settlement", finalized.settlement_no, "订单已有已完成清算，批次不重复生成", category="settlement", business_domain="settlement", order_id=order.id, risk_level="warning")
            continue
        for prior in prior_settlements:
            prior.status = "superseded"
        gross = Decimal(str(order.paid_amount or order.amount or 0)).quantize(Decimal("0.01"))
        # The original paid order is settled positively. Completed refunds are
        # represented by separate negative settlement adjustments below.
        refund = Decimal("0.00")
        net = gross
        cost_amount = order_cost(db, order)
        profit = (net - cost_amount).quantize(Decimal("0.01"))
        distributable = max(profit, Decimal("0.00"))
        rates, matched_rule_version = settlement_values_for_order(db, order, rule)
        platform_fee = (distributable * rates["platform_rate"] / 100).quantize(Decimal("0.01"))
        service_share = (distributable * rates["service_rate"] / 100).quantize(Decimal("0.01"))
        expert_fee = (distributable * rates["expert_rate"] / 100).quantize(Decimal("0.01"))
        channel_fee = (distributable * rates["channel_rate"] / 100).quantize(Decimal("0.01"))
        tax_amount = Decimal("0.00")
        provider_share = (distributable - platform_fee - service_share - expert_fee - channel_fee).quantize(Decimal("0.01"))
        settlement = Settlement(settlement_no="SET-" + secrets.token_hex(6).upper(), order_id=order.id, gross_amount=gross, refund_amount=refund, net_amount=net, cost_amount=cost_amount, profit_amount=profit, refund_recovery=refund, platform_fee=platform_fee, provider_share=provider_share, service_share=service_share, expert_fee=expert_fee, channel_fee=channel_fee, tax_amount=tax_amount, status="pending")
        db.add(settlement)
        db.flush()
        db.add_all([SettlementLine(batch_id=batch.id, settlement_id=settlement.id, participant_type="platform", participant_name="平台运营方", amount=settlement.platform_fee), SettlementLine(batch_id=batch.id, settlement_id=settlement.id, participant_type="provider", participant_id=order.provider_enterprise_id, participant_name="数据/服务提供方", amount=settlement.provider_share), SettlementLine(batch_id=batch.id, settlement_id=settlement.id, participant_type="service", participant_name="数据服务方", amount=settlement.service_share), SettlementLine(batch_id=batch.id, settlement_id=settlement.id, participant_type="expert", participant_name="专家", amount=settlement.expert_fee), SettlementLine(batch_id=batch.id, settlement_id=settlement.id, participant_type="channel", participant_name="渠道", amount=settlement.channel_fee)])
        total += net
        total_profit += profit
        audit(db, user.email, "generate_settlement_batch", "settlement", settlement.settlement_no, f"rule={matched_rule_version}", category="settlement", business_domain="settlement", order_id=order.id, batch_no=batch.batch_no, rule_version=matched_rule_version, before={"status": "none"}, after={"status": settlement.status, "net_amount": float(net), "cost_amount": float(cost_amount), "profit_amount": float(profit), "distributable_profit": float(distributable), "platform_fee": float(platform_fee), "provider_share": float(provider_share), "service_share": float(service_share), "expert_fee": float(expert_fee), "channel_fee": float(channel_fee)})
    # A completed refund is an independent negative settlement adjustment.
    # It is selected by refund completion time, so a refund never rewrites the
    # original paid order settlement and can be audited independently.
    refund_stmt = select(Refund).where(Refund.status == "completed", Refund.completed_at.is_not(None))
    if body.period_start:
        refund_stmt = refund_stmt.where(Refund.completed_at >= body.period_start)
    if body.period_end:
        refund_stmt = refund_stmt.where(Refund.completed_at < body.period_end)
    refund_count = 0
    for refund in (db.scalars(refund_stmt).all() if body.cycle == "monthly" else []):
        refund_count += 1
        settlement = create_refund_negative_settlement(db, refund, user.email or user.name)
        prior_lines = db.scalars(select(SettlementLine).where(SettlementLine.settlement_id == settlement.id, SettlementLine.status != "superseded")).all()
        if prior_lines:
            linked_batch = db.get(SettlementBatch, prior_lines[0].batch_id)
            if linked_batch and linked_batch.status in {"confirmed", "payment_processing", "partial_paid", "paid", "archived"}:
                continue
            for line in prior_lines:
                line.status = "superseded"
        settlement.status = "pending"
        db.add_all([SettlementLine(batch_id=batch.id, settlement_id=settlement.id, participant_type="platform", participant_name="平台运营方", amount=settlement.platform_fee), SettlementLine(batch_id=batch.id, settlement_id=settlement.id, participant_type="provider", participant_id=(db.get(Order, settlement.order_id).provider_enterprise_id if db.get(Order, settlement.order_id) else ""), participant_name="数据/服务提供方", amount=settlement.provider_share), SettlementLine(batch_id=batch.id, settlement_id=settlement.id, participant_type="service", participant_name="数据服务方", amount=settlement.service_share), SettlementLine(batch_id=batch.id, settlement_id=settlement.id, participant_type="expert", participant_name="专家", amount=settlement.expert_fee), SettlementLine(batch_id=batch.id, settlement_id=settlement.id, participant_type="channel", participant_name="渠道", amount=settlement.channel_fee)])
        total += Decimal(str(settlement.net_amount or 0))
        total_profit += Decimal(str(settlement.profit_amount or 0))
        audit(db, user.email, "generate_refund_settlement_line", "settlement", settlement.settlement_no, "退款完成日期纳入月度清算", category="settlement_adjustment", business_domain="settlement", order_id=settlement.order_id, batch_no=batch.batch_no, after={"refund_id": refund.id, "status": settlement.status, "net_amount": float(settlement.net_amount or 0), "profit_amount": float(settlement.profit_amount or 0)})
    batch.total_amount = total
    batch.total_profit = total_profit
    batch.exception_count = 0
    audit(db, user.email, "create_settlement_batch", "settlement_batch", batch.batch_no, f"orders={len(order_ids)} refunds={refund_count}", category="settlement", business_domain="settlement", batch_no=batch.batch_no, rule_version=rule.version, before={"status": "none"}, after={"status": batch.status, "total_amount": float(total), "total_profit": float(total_profit), "order_count": len(order_ids), "refund_count": refund_count, "exception_count": 0})
    db.commit()
    return {"id": batch.id, "batch_no": batch.batch_no, "total_amount": float(total), "total_profit": float(total_profit), "exception_count": exceptions, "status": batch.status, "rule_version": rule.version}


@app.get("/api/settlement-batches")
def settlement_batches(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_viewer(user)
    items = db.scalars(select(SettlementBatch).order_by(SettlementBatch.created_at.desc()).limit(200)).all()
    return {"items": [{"id": x.id, "batch_no": x.batch_no, "cycle": x.cycle, "rule_id": x.rule_id, "status": x.status, "total_amount": float(x.total_amount or 0), "total_profit": float(x.total_profit or 0), "exception_count": x.exception_count, "confirmed_at": x.confirmed_at, "paid_at": x.paid_at, "created_at": x.created_at} for x in items]}


@app.get("/api/settlement-batches/{batch_id}/lines")
def settlement_batch_lines(batch_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_viewer(user)
    batch = db.get(SettlementBatch, batch_id)
    if not batch:
        raise HTTPException(404, "清算批次不存在")
    items = db.scalars(select(SettlementLine).where(SettlementLine.batch_id == batch.id).order_by(SettlementLine.participant_type, SettlementLine.created_at)).all()
    return {"batch_no": batch.batch_no, "items": [{"id": x.id, "settlement_id": x.settlement_id, "participant_type": x.participant_type, "participant_id": x.participant_id, "participant_name": x.participant_name, "amount": float(x.amount or 0), "status": x.status, "payment_no": x.payment_no} for x in items]}


@app.post("/api/settlement-batches/{batch_id}/confirm")
def confirm_settlement_batch(batch_id: str, body: SettlementActionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    batch = db.get(SettlementBatch, batch_id)
    if not batch:
        raise HTTPException(404, "清算批次不存在")
    if batch.status != "pending_confirm":
        raise HTTPException(409, "只有待确认状态的清算批次可以确认")
    settlement_ids = select(SettlementLine.settlement_id).where(SettlementLine.batch_id == batch.id, SettlementLine.status != "superseded")
    settlements = db.scalars(select(Settlement).where(Settlement.id.in_(settlement_ids), Settlement.status != "superseded")).all()
    if not settlements:
        raise HTTPException(409, "批次没有可确认的清算单")
    if any(item.status == "disputed" for item in settlements) or db.scalar(select(SettlementAdjustmentProposal.id).where(SettlementAdjustmentProposal.settlement_id.in_(settlement_ids), SettlementAdjustmentProposal.status == "pending")):
        raise HTTPException(409, "批次存在未处理调整提案，不能确认锁定")
    before = {"status": batch.status, "settlement_statuses": {item.settlement_no: item.status for item in settlements}}
    for item in settlements:
        if item.status == "paid":
            raise HTTPException(409, "批次存在已付款清算单，不能重新确认")
        item.status = "locked"
    for line in db.scalars(select(SettlementLine).where(SettlementLine.batch_id == batch.id, SettlementLine.status != "superseded")).all():
        line.status = "locked"
    batch.status = "confirmed"
    batch.confirmed_at = now()
    audit(db, user.email, "confirm_settlement_batch", "settlement_batch", batch.batch_no, body.comment, category="settlement", business_domain="settlement", batch_no=batch.batch_no, before=before, after={"status": batch.status, "settlement_statuses": {item.settlement_no: item.status for item in settlements}})
    db.commit()
    return {"batch_no": batch.batch_no, "status": batch.status, "confirmed_at": batch.confirmed_at}


@app.post("/api/settlement-batches/{batch_id}/dispute")
def dispute_settlement_batch(batch_id: str, body: SettlementActionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    raise HTTPException(410, "清算异议应针对清算单提交调整提案，清算批次不再设置 disputed 状态")


@app.post("/api/settlement-batches/{batch_id}/rollback")
def rollback_settlement_batch(batch_id: str, body: SettlementActionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    batch = db.get(SettlementBatch, batch_id)
    if not batch:
        raise HTTPException(404, "清算批次不存在")
    if batch.status not in {"generated", "pending_confirm", "rolled_back"}:
        raise HTTPException(409, "只有未付款且未归档批次可以回滚")
    before = {"status": batch.status}
    batch.status = "rolled_back"
    audit(db, user.email, "rollback_settlement_batch", "settlement_batch", batch.batch_no, body.comment or "异常回滚", category="settlement", business_domain="settlement", batch_no=batch.batch_no, risk_level="high", before=before, after={"status": batch.status})
    db.commit()
    return {"batch_no": batch.batch_no, "status": batch.status}


@app.post("/api/settlement-batches/{batch_id}/pay")
def pay_settlement_batch(batch_id: str, body: SettlementActionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    batch = db.get(SettlementBatch, batch_id)
    if not batch:
        raise HTTPException(404, "清算批次不存在")
    if batch.status != "confirmed":
        raise HTTPException(409, "只有已确认批次可以付款")
    unresolved = db.scalar(select(SettlementReconciliation.id).where(SettlementReconciliation.batch_id == batch.id, SettlementReconciliation.status.in_(["difference", "exception"])))
    if unresolved:
        raise HTTPException(409, "批次存在未关闭的对账差异，不能付款")
    settlement_ids = select(SettlementLine.settlement_id).where(SettlementLine.batch_id == batch.id, SettlementLine.status != "superseded")
    settlements = db.scalars(select(Settlement).where(Settlement.id.in_(settlement_ids), Settlement.status != "superseded")).all()
    if not settlements or any(item.status != "locked" for item in settlements):
        raise HTTPException(409, "所有清算单锁定后才可以付款")
    if db.scalar(select(SettlementAdjustmentProposal.id).where(SettlementAdjustmentProposal.settlement_id.in_(settlement_ids), SettlementAdjustmentProposal.status == "pending")):
        raise HTTPException(409, "批次存在未处理调整提案，不能付款")
    before = {"status": batch.status, "settlement_statuses": {item.settlement_no: item.status for item in settlements}}
    batch.status = "paid"
    batch.paid_at = now()
    for item in settlements:
        item.status = "paid"
    for line in db.scalars(select(SettlementLine).where(SettlementLine.batch_id == batch.id)).all():
        line.status = "paid"
        line.payment_no = "SIM-PAY-" + secrets.token_hex(5).upper()
    audit(db, user.email, "pay_settlement_batch", "settlement_batch", batch.batch_no, body.comment, category="settlement_payment", business_domain="settlement", batch_no=batch.batch_no, before=before, after={"status": batch.status, "settlement_statuses": {item.settlement_no: item.status for item in settlements}})
    db.commit()
    return {"batch_no": batch.batch_no, "status": batch.status, "paid_at": batch.paid_at}


@app.post("/api/settlement-batches/{batch_id}/reconcile")
def reconcile_settlement_batch(batch_id: str, body: SettlementReconciliationBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    batch = db.get(SettlementBatch, batch_id)
    if not batch:
        raise HTTPException(404, "清算批次不存在")
    expected = Decimal(str(batch.total_amount or 0))
    actual = Decimal(str(body.actual_amount))
    difference = (actual - expected).quantize(Decimal("0.01"))
    item = SettlementReconciliation(batch_id=batch.id, ledger_type=body.ledger_type, expected_amount=expected, actual_amount=actual, difference_amount=difference, status="matched" if difference == 0 else "difference", resolution=body.resolution)
    db.add(item)
    audit(db, user.email, "reconcile_settlement_batch", "reconciliation", item.id, body.resolution, category="reconciliation", business_domain="settlement", batch_no=batch.batch_no, risk_level="high" if difference != 0 else "normal", after={"difference": float(difference), "ledger_type": body.ledger_type})
    db.commit()
    db.refresh(item)
    return {"id": item.id, "batch_no": batch.batch_no, "ledger_type": item.ledger_type, "expected_amount": float(expected), "actual_amount": float(actual), "difference_amount": float(difference), "status": item.status}


@app.get("/api/settlement-reconciliations")
def settlement_reconciliations(status: str | None = None, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_viewer(user)
    stmt = select(SettlementReconciliation).order_by(SettlementReconciliation.created_at.desc())
    if status:
        stmt = stmt.where(SettlementReconciliation.status == status)
    items = db.scalars(stmt.limit(500)).all()
    return {"items": [{"id": x.id, "batch_id": x.batch_id, "ledger_type": x.ledger_type, "expected_amount": float(x.expected_amount or 0), "actual_amount": float(x.actual_amount or 0), "difference_amount": float(x.difference_amount or 0), "status": x.status, "resolution": x.resolution, "closed_by": x.closed_by, "closed_at": x.closed_at, "created_at": x.created_at} for x in items]}


@app.post("/api/settlement-reconciliations/{reconciliation_id}/close")
def close_settlement_reconciliation(reconciliation_id: str, body: ReconciliationCloseBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    item = db.get(SettlementReconciliation, reconciliation_id)
    if not item:
        raise HTTPException(404, "对账记录不存在")
    if item.status == "closed":
        return {"id": item.id, "status": item.status}
    item.status = "closed"
    item.resolution = body.resolution
    item.closed_by = user.email or user.name
    item.closed_at = now()
    batch = db.get(SettlementBatch, item.batch_id)
    # 对账差异属于对账记录本身，不再污染清算批次状态。
    audit(db, user.email, "close_reconciliation_difference", "reconciliation", item.id, body.resolution, category="reconciliation", business_domain="settlement", batch_no=batch.batch_no if batch else "")
    db.commit()
    return {"id": item.id, "status": item.status, "closed_by": item.closed_by, "closed_at": item.closed_at}


@app.get("/api/settlement-corrections")
def settlement_corrections(status: str | None = None, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_viewer(user)
    stmt = select(SettlementCorrection).order_by(SettlementCorrection.created_at.desc())
    if status:
        stmt = stmt.where(SettlementCorrection.status == status)
    items = db.scalars(stmt.limit(500)).all()
    return {"items": [{"id": x.id, "settlement_id": x.settlement_id, "order_id": x.order_id, "correction_type": x.correction_type, "amount": float(x.amount or 0), "reason": x.reason, "source_ref": x.source_ref, "status": x.status, "recovery_mode": x.recovery_mode, "approved_by": x.approved_by, "approved_at": x.approved_at, "created_by": x.created_by, "created_at": x.created_at} for x in items]}


@app.post("/api/settlements/{settlement_id}/corrections")
def create_settlement_correction(settlement_id: str, body: SettlementCorrectionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    settlement = db.get(Settlement, settlement_id)
    if not settlement:
        raise HTTPException(404, "清算单不存在")
    if body.amount > float(settlement.net_amount or 0) and body.correction_type == "refund":
        raise HTTPException(400, "退款金额不能超过清算净额")
    correction = SettlementCorrection(settlement_id=settlement.id, order_id=settlement.order_id, correction_type=body.correction_type, amount=body.amount, reason=body.reason, source_ref=body.source_ref, recovery_mode=body.recovery_mode, status="pending", created_by=user.email or user.name)
    db.add(correction)
    db.flush()
    audit(db, user.email, "create_settlement_correction", "settlement_correction", correction.id, body.reason, category="settlement_adjustment", business_domain="settlement", order_id=settlement.order_id, after={"type": body.correction_type, "amount": body.amount, "recovery_mode": body.recovery_mode}, risk_level="high" if body.correction_type in {"refund", "reversal"} else "normal")
    db.commit()
    db.refresh(correction)
    return {"id": correction.id, "settlement_id": correction.settlement_id, "order_id": correction.order_id, "correction_type": correction.correction_type, "amount": float(correction.amount), "status": correction.status, "recovery_mode": correction.recovery_mode, "created_at": correction.created_at}


@app.post("/api/settlement-corrections/{correction_id}/approve")
def approve_settlement_correction(correction_id: str, body: SettlementActionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    correction = db.get(SettlementCorrection, correction_id)
    if not correction:
        raise HTTPException(404, "清算调整不存在")
    if correction.status != "pending":
        raise HTTPException(409, "当前调整不处于待审批状态")
    correction.status = "approved"
    correction.approved_by = user.email or user.name
    correction.approved_at = now()
    audit(db, user.email, "approve_settlement_correction", "settlement_correction", correction.id, body.comment, category="settlement_adjustment", business_domain="settlement", order_id=correction.order_id)
    db.commit()
    return {"id": correction.id, "status": correction.status, "approved_by": correction.approved_by, "approved_at": correction.approved_at}


@app.post("/api/settlement-corrections/{correction_id}/reject")
def reject_settlement_correction(correction_id: str, body: SettlementActionBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_operator(user)
    correction = db.get(SettlementCorrection, correction_id)
    if not correction:
        raise HTTPException(404, "清算调整不存在")
    if correction.status != "pending":
        raise HTTPException(409, "当前调整不处于待审批状态")
    correction.status = "rejected"
    audit(db, user.email, "reject_settlement_correction", "settlement_correction", correction.id, body.comment or "调整依据不足", category="settlement_adjustment", business_domain="settlement", order_id=correction.order_id, risk_level="high")
    db.commit()
    return {"id": correction.id, "status": correction.status}


@app.get("/api/settlement-reports")
def settlement_reports(status: str | None = None, start: str | None = None, end: str | None = None, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_settlement_viewer(user)
    stmt = select(Settlement).where(Settlement.status != "superseded")
    if status:
        stmt = stmt.where(Settlement.status == status)
    if start:
        stmt = stmt.where(Settlement.created_at >= datetime.fromisoformat(start))
    if end:
        stmt = stmt.where(Settlement.created_at < datetime.fromisoformat(end) + timedelta(days=1))
    items = db.scalars(stmt.order_by(Settlement.created_at.desc()).limit(2000)).all()
    summary = {"gross_amount": 0.0, "refund_amount": 0.0, "net_amount": 0.0, "cost_amount": 0.0, "profit_amount": 0.0, "platform_fee": 0.0, "provider_share": 0.0, "service_share": 0.0, "expert_fee": 0.0, "channel_fee": 0.0, "tax_amount": 0.0, "count": len(items), "participants": []}
    participant_totals: dict[tuple[str, str], dict[str, Any]] = {}
    rows = []
    for x in items:
        order = db.get(Order, x.order_id)
        fields = ["gross_amount", "refund_amount", "net_amount", "cost_amount", "profit_amount", "platform_fee", "provider_share", "service_share", "expert_fee", "channel_fee", "tax_amount"]
        values = {field: float(getattr(x, field) or 0) for field in fields}
        for field in fields:
            summary[field] += values[field]
        lines = db.scalars(select(SettlementLine).where(SettlementLine.settlement_id == x.id).order_by(SettlementLine.created_at)).all()
        participants = [{"participant_type": line.participant_type, "participant_id": line.participant_id, "participant_name": line.participant_name, "amount": float(line.amount or 0)} for line in lines]
        participant_defaults = [
            ("platform", "", "平台运营方", "platform_fee"),
            ("provider", order.provider_enterprise_id if order else "", "数据/服务提供方", "provider_share"),
            ("service", "", "数据服务方", "service_share"),
            ("expert", "", "专家", "expert_fee"),
            ("channel", "", "渠道", "channel_fee"),
        ]
        existing_types = {participant["participant_type"] for participant in participants}
        # 补齐历史清算单缺少的参与方明细，保证专家和渠道在总表、明细中始终可见。
        for participant_type, participant_id, participant_name, field in participant_defaults:
            if participant_type not in existing_types:
                participants.append({"participant_type": participant_type, "participant_id": participant_id, "participant_name": participant_name, "amount": values[field]})
        for participant in participants:
            key = (participant["participant_type"], participant["participant_id"] or participant["participant_name"])
            total = participant_totals.setdefault(key, {"participant_type": participant["participant_type"], "participant_id": participant["participant_id"], "participant_name": participant["participant_name"], "amount": 0.0, "order_count": 0})
            total["amount"] += participant["amount"]
            total["order_count"] += 1
        public_participants = [{"participant_type": item["participant_type"], "participant_name": item["participant_name"], "amount": item["amount"]} for item in participants]
        rows.append({"id": x.id, "settlement_no": x.settlement_no, "order_id": x.order_id, "order_no": order.order_no if order else "", "product_id": order.product_id if order else "", "product_name": order.product_name if order else "未命名产品", "status": x.status, "created_at": x.created_at, "participants": public_participants, "participant_total": round(sum(item["amount"] for item in participants), 2), **values})
    summary["participants"] = sorted([{"participant_type": item["participant_type"], "participant_name": item["participant_name"], "amount": round(item["amount"], 2), "order_count": item["order_count"]} for item in participant_totals.values()], key=lambda item: (-item["amount"], item["participant_type"], item["participant_name"]))

    def empty_breakdown(label: str) -> dict[str, Any]:
        return {"label": label, "order_count": 0, "gross_amount": 0.0, "cost_amount": 0.0, "profit_amount": 0.0, "platform_fee": 0.0, "provider_share": 0.0, "service_share": 0.0, "expert_fee": 0.0, "channel_fee": 0.0}

    monthly: dict[str, dict[str, Any]] = {}
    products: dict[tuple[str, str], dict[str, Any]] = {}
    breakdown_fields = ["gross_amount", "cost_amount", "profit_amount", "platform_fee", "provider_share", "service_share", "expert_fee", "channel_fee"]
    for row in rows:
        month = row["created_at"].strftime("%Y-%m") if row.get("created_at") else "未知月份"
        monthly_item = monthly.setdefault(month, empty_breakdown(month))
        product_key = (row.get("product_id", ""), row.get("product_name", "未命名产品"))
        product_item = products.setdefault(product_key, empty_breakdown(row.get("product_name", "未命名产品")))
        for bucket in (monthly_item, product_item):
            bucket["order_count"] += 1
            for field in breakdown_fields:
                bucket[field] += row.get(field, 0.0)

    def clean_breakdown(item: dict[str, Any]) -> dict[str, Any]:
        return {key: (round(value, 2) if isinstance(value, float) else value) for key, value in item.items()}

    return {"summary": summary, "monthly": [clean_breakdown(monthly[key]) for key in sorted(monthly)], "products": [clean_breakdown(item) | {"product_id": key[0], "product_name": key[1]} for key, item in sorted(products.items(), key=lambda pair: pair[0][1])], "items": rows}


@app.get("/api/settlement-reports/export")
def export_settlement_report(status: str | None = None, start: str | None = None, end: str | None = None, kind: str = "details", user: User = Depends(current_user), db: Session = Depends(db_session)):
    data = settlement_reports(status=status, start=start, end=end, user=user, db=db)
    if kind == "summary":
        lines = ["总体订单清算汇总", "指标,金额", f"订单金额,{data['summary']['gross_amount']}", f"订单成本,{data['summary']['cost_amount']}", f"订单利润,{data['summary']['profit_amount']}", "", "总体参与方汇总"]
        lines.append("participant_type,participant_name,order_count,settlement_amount")
        for item in data["summary"]["participants"]:
            lines.append(",".join(str(item.get(key, "")).replace(",", " ") for key in ["participant_type", "participant_name", "order_count", "amount"]))
        lines.append("")
        lines.append("自然月汇总")
        lines.append("月份,订单数量,订单金额,订单成本,订单利润,平台运营方,数据/服务提供方,数据服务方,专家,渠道")
        for item in data["monthly"]:
            lines.append(",".join(str(item.get(key, "")).replace(",", " ") for key in ["label", "order_count", "gross_amount", "cost_amount", "profit_amount", "platform_fee", "provider_share", "service_share", "expert_fee", "channel_fee"]))
    elif kind == "products":
        lines = ["产品维度清算汇总", "product_id,product_name,订单数量,订单金额,订单成本,订单利润,平台运营方,数据/服务提供方,数据服务方,专家,渠道"]
        for item in data["products"]:
            lines.append(",".join(str(item.get(key, "")).replace(",", " ") for key in ["product_id", "product_name", "order_count", "gross_amount", "cost_amount", "profit_amount", "platform_fee", "provider_share", "service_share", "expert_fee", "channel_fee"]))
    else:
        lines = ["settlement_no,order_id,order_no,product_name,status,order_amount,refund_amount,net_amount,cost_amount,profit_amount,平台运营方,数据/服务提供方,数据服务方,专家,渠道,created_at"]
        for item in data["items"]:
            amounts = {p["participant_type"]: p["amount"] for p in item["participants"]}
            lines.append(",".join(str(item.get(key, "")).replace(",", " ") for key in ["settlement_no", "order_id", "order_no", "product_name", "status", "gross_amount", "refund_amount", "net_amount", "cost_amount", "profit_amount"]) + "," + ",".join(str(amounts.get(key, 0)).replace(",", " ") for key in ["platform", "provider", "service", "expert", "channel"]) + "," + str(item.get("created_at", "")))
    audit(db, user.email, "export_settlement_report", "settlement_report", "", f"status={status or 'all'} rows={len(data['items'])}", category="settlement_report", business_domain="settlement")
    db.commit()
    filename = "settlement-summary.csv" if kind == "summary" else "settlement-products.csv" if kind == "products" else "settlement-details.csv"
    return Response(content="\ufeff" + "\n".join(lines), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/api/audit-logs")
def audit_logs(category: str | None = None, q: str | None = None, actor: str | None = None, order_id: str | None = None, batch_no: str | None = None, rule_version: str | None = None, risk_level: str | None = None, start: str | None = None, end: str | None = None, page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200), user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_audit_viewer(user)
    stmt = audit_support["query"](db, category, q, actor, order_id, batch_no, rule_version, risk_level, start, end)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    audit_items = audit_support["objects"](db, items)
    return {"items": audit_items, "total": total, "page": page, "page_size": page_size, "pages": (total + page_size - 1) // page_size}


@app.get("/api/audit-logs/categories")
def audit_log_categories(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_audit_viewer(user)
    rows = db.execute(select(AuditLog.category, func.count(AuditLog.id)).group_by(AuditLog.category).order_by(func.count(AuditLog.id).desc())).all()
    settlement_count = db.scalar(select(func.count()).select_from(audit_support["query"](db, category="settlement_all").subquery())) or 0
    return {"items": [{"category": row[0] or "ops", "count": row[1]} for row in rows] + [{"category": "settlement_all", "count": settlement_count}]}


@app.get("/api/users")
def users(user: User = Depends(current_user), db: Session = Depends(db_session)):
    if user.platform_role in {"super_admin", "platform_operator"}:
        items = db.scalars(select(User).order_by(User.created_at.desc())).all()
    else:
        managed_enterprises = db.scalars(select(Membership.enterprise_id).where(Membership.user_id == user.id, Membership.status == "active", Membership.role.in_(["super_admin", "enterprise_admin"]))).all()
        if managed_enterprises:
            member_user_ids = db.scalars(select(Membership.user_id).where(Membership.enterprise_id.in_(managed_enterprises), Membership.status.in_(["active", "pending_activation"]))).all()
            invited_user_ids = db.scalars(select(EnterpriseInvitation.invitee_id).where(EnterpriseInvitation.enterprise_id.in_(managed_enterprises), EnterpriseInvitation.status == "pending")).all()
            visible_user_ids = set(member_user_ids) | set(invited_user_ids) | {user.id}
            items = db.scalars(select(User).where(User.id.in_(visible_user_ids)).order_by(User.created_at.desc())).all()
        else:
            items = [user]
    user_ids = [item.id for item in items]
    role_priority = {"super_admin": 3, "enterprise_admin": 2, "member": 1}
    role_names = {"super_admin": "超级管理员", "enterprise_admin": "管理员", "member": "普通用户"}
    memberships = db.scalars(select(Membership).where(Membership.user_id.in_(user_ids), Membership.status.in_(["active", "pending_activation", "disabled"]))).all() if user_ids else []
    roles_by_user: dict[str, str] = {}
    for membership in memberships:
        current = roles_by_user.get(membership.user_id, "")
        if role_priority.get(membership.role, 0) > role_priority.get(current, 0):
            roles_by_user[membership.user_id] = membership.role
    enterprise_names: dict[str, list[str]] = {}
    if user_ids:
        enterprise_rows = db.execute(select(Membership.user_id, Enterprise.name).join(Enterprise, Enterprise.id == Membership.enterprise_id).where(Membership.user_id.in_(user_ids), Membership.status.in_(["active", "pending_activation", "disabled"]))).all()
        for user_id, enterprise_name in enterprise_rows:
            enterprise_names.setdefault(user_id, []).append(enterprise_name)
    membership_details: dict[str, list[dict[str, str]]] = {}
    if user_ids:
        detail_rows = db.execute(select(Membership, Enterprise).join(Enterprise, Enterprise.id == Membership.enterprise_id).where(Membership.user_id.in_(user_ids), Membership.status.in_(["active", "pending_activation", "disabled"]))).all()
        for membership, enterprise in detail_rows:
            membership_details.setdefault(membership.user_id, []).append({"membership_id": membership.id, "enterprise_id": enterprise.id, "enterprise_name": enterprise.name, "role": membership.role, "status": membership.status})
    return {"items": [{"id": x.id, "username": x.username, "name": x.name, "email": x.email, "phone": x.phone, "verified_status": x.verified_status, "activation_status": x.activation_status, "is_active": x.is_active, "platform_role": x.platform_role, "user_role": "平台角色账号" if x.platform_role else role_names.get(roles_by_user.get(x.id, ""), "未加入企业"), "enterprise_name": "平台" if x.platform_role else "、".join(dict.fromkeys(enterprise_names.get(x.id, []))), "enterprise_memberships": membership_details.get(x.id, []), "created_at": x.created_at} for x in items]}


@app.get("/api/admin/enterprises")
def admin_enterprises(user: User = Depends(current_user), db: Session = Depends(db_session)):
    if user.platform_role in {"super_admin", "platform_operator"}:
        items = db.scalars(select(Enterprise).order_by(Enterprise.created_at.desc())).all()
    else:
        items = db.scalars(select(Enterprise).join(Membership, Membership.enterprise_id == Enterprise.id).where(Membership.user_id == user.id, Membership.status == "active").order_by(Enterprise.created_at.desc())).all()
    return {"items": [{"id": x.id, "name": x.name, "credit_code": x.credit_code, "enterprise_type": x.enterprise_type, "legal_representative": x.legal_representative, "registered_capital": x.registered_capital, "establishment_date": x.establishment_date, "business_address": x.business_address, "business_scope": x.business_scope, "license_file_id": x.license_file_id, "verification_status": x.verification_status, "verified_by": x.verified_by, "verified_at": x.verified_at, "created_at": x.created_at} for x in items]}


@app.get("/api/development/tasks")
def development_tasks(user: User = Depends(current_user), db: Session = Depends(db_session)):
    tasks = db.scalars(select(DevelopmentTask).order_by(DevelopmentTask.status, DevelopmentTask.priority, DevelopmentTask.code)).all()
    counts = {status: sum(1 for x in tasks if x.status == status) for status in ["todo", "in_progress", "review", "blocked", "done"]}
    total = len(tasks)
    return {"total": total, "counts": counts, "completion_rate": round((counts["done"] / total * 100) if total else 0, 1), "updated_at": now(), "items": [{"id": x.id, "code": x.code, "owner": x.owner, "title": x.title, "area": x.area, "priority": x.priority, "status": x.status, "dependencies": x.dependencies, "acceptance": x.acceptance, "progress": x.progress, "updated_at": x.updated_at} for x in tasks]}


@app.patch("/api/development/tasks/{code}")
def update_development_task(code: str, body: DevelopmentTaskUpdate, user: User = Depends(current_user), db: Session = Depends(db_session)):
    if user.platform_role not in {"super_admin", "platform_operator"}:
        raise HTTPException(403, "只有平台管理员或平台运营人员可以更新开发任务")
    task = db.scalar(select(DevelopmentTask).where(DevelopmentTask.code == code))
    if not task:
        raise HTTPException(404, "开发任务不存在")
    if body.status is not None and body.status not in {"todo", "in_progress", "review", "blocked", "done"}:
        raise HTTPException(400, "不支持的任务状态")
    if body.status is not None:
        task.status = body.status
    if body.progress is not None:
        task.progress = body.progress
    for field in ("title", "acceptance", "dependencies"):
        value = getattr(body, field)
        if value is not None:
            setattr(task, field, value)
    if task.status == "done":
        task.progress = 100
    audit(db, user.email, "update_development_task", "development_task", task.code, body.note)
    db.commit()
    return {"code": task.code, "status": task.status, "progress": task.progress, "updated_at": task.updated_at}


def sla_profile_out(item: SLAProfile) -> dict[str, Any]:
    return {"id": item.id, "name": item.name, "service_scope": item.service_scope, "product_id": item.product_id,
            "evaluation_period": item.evaluation_period, "availability_target": float(item.availability_target or 0),
            "latency_target_ms": item.latency_target_ms, "error_rate_target": float(item.error_rate_target or 0),
            "delivery_hours": item.delivery_hours, "recovery_minutes": item.recovery_minutes,
            "warning_margin": float(item.warning_margin or 0), "status": item.status, "description": item.description,
            "created_by": item.created_by, "created_at": item.created_at, "updated_at": item.updated_at}


def service_level_out(item: ServiceLevel) -> dict[str, Any]:
    return {"id": item.id, "code": item.code, "name": item.name, "description": item.description, "customer_scope": item.customer_scope,
            "support_days_per_week": item.support_days_per_week, "support_hours_per_day": item.support_hours_per_day,
            "support_schedule": f"{item.support_days_per_week}×{item.support_hours_per_day}", "online_docs": item.online_docs,
            "knowledge_base": item.knowledge_base, "standard_api": item.standard_api, "online_customer_service": item.online_customer_service,
            "dedicated_manager": item.dedicated_manager, "technical_support": item.technical_support,
            "initial_response_minutes": item.initial_response_minutes, "problem_response_hours": item.problem_response_hours,
            "quarterly_report": item.quarterly_report, "annual_optimization": item.annual_optimization, "status": item.status,
            "created_by": item.created_by, "created_at": item.created_at, "updated_at": item.updated_at}


def service_level_assignment_out(item: ServiceLevelAssignment, level: ServiceLevel | None = None, enterprise: Enterprise | None = None) -> dict[str, Any]:
    return {"id": item.id, "service_level_id": item.service_level_id, "service_level": service_level_out(level) if level else None,
            "enterprise_id": item.enterprise_id, "enterprise_name": enterprise.name if enterprise else "", "user_id": item.user_id,
            "source": item.source, "effective_at": item.effective_at, "expires_at": item.expires_at, "created_by": item.created_by, "created_at": item.created_at}


def current_service_level(db: Session, enterprise_id: str, user_id: str = "") -> ServiceLevel:
    statement = select(ServiceLevelAssignment).where(ServiceLevelAssignment.enterprise_id == enterprise_id, ServiceLevelAssignment.effective_at <= now(), or_(ServiceLevelAssignment.expires_at.is_(None), ServiceLevelAssignment.expires_at >= now()))
    if user_id:
        statement = statement.where(or_(ServiceLevelAssignment.user_id == user_id, ServiceLevelAssignment.user_id == ""))
    assignment = db.scalar(statement.order_by(ServiceLevelAssignment.user_id.desc(), ServiceLevelAssignment.effective_at.desc()))
    level = db.get(ServiceLevel, assignment.service_level_id) if assignment else None
    return level or db.scalar(select(ServiceLevel).where(ServiceLevel.code == "standard"))


def sla_result_out(item: SLAResult, profile: SLAProfile | None = None) -> dict[str, Any]:
    return {"id": item.id, "profile_id": item.profile_id, "profile_name": profile.name if profile else "",
            "product_id": item.product_id, "period_start": item.period_start, "period_end": item.period_end,
            "sample_count": item.sample_count, "success_count": item.success_count,
            "availability": float(item.availability or 0), "avg_latency_ms": float(item.avg_latency_ms or 0),
            "error_rate": float(item.error_rate or 0), "delivery_count": item.delivery_count,
            "delivery_on_time": item.delivery_on_time, "delivery_compliance": float(item.delivery_compliance or 0),
            "status": item.status, "breach_reason": item.breach_reason, "calculated_at": item.calculated_at}


@app.get("/api/sla/overview")
def sla_overview(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    profiles = db.scalars(select(SLAProfile).order_by(SLAProfile.created_at.desc())).all()
    all_results = db.scalars(select(SLAResult).order_by(SLAResult.calculated_at.desc()).limit(200)).all()
    latest_result_ids = set()
    results = []
    for result in all_results:
        if result.profile_id not in latest_result_ids:
            latest_result_ids.add(result.profile_id)
            results.append(result)
    profile_map = {x.id: x for x in profiles}
    service_levels = db.scalars(select(ServiceLevel).order_by(ServiceLevel.code)).all()
    assignments = db.scalars(select(ServiceLevelAssignment).order_by(ServiceLevelAssignment.created_at.desc()).limit(100)).all()
    return {"summary": {"profiles": len(profiles), "active_profiles": sum(x.status == "active" for x in profiles),
                         "met": sum(x.status == "met" for x in results), "warning": sum(x.status == "warning" for x in results),
                         "breached": sum(x.status == "breached" for x in results)},
            "profiles": [sla_profile_out(x) for x in profiles],
            "results": [sla_result_out(x, profile_map.get(x.profile_id)) for x in results],
            "service_levels": [service_level_out(x) for x in service_levels],
            "service_level_assignments": [service_level_assignment_out(x, db.get(ServiceLevel, x.service_level_id), db.get(Enterprise, x.enterprise_id)) for x in assignments]}


@app.post("/api/sla/profiles")
def create_sla_profile(body: SLAProfileBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    if db.scalar(select(SLAProfile).where(SLAProfile.name == body.name)):
        raise HTTPException(409, "SLA 规则名称已存在")
    item = SLAProfile(**body.model_dump(), created_by=user.email)
    db.add(item)
    audit(db, user.email, "create_sla_profile", "sla_profile", item.id, f"创建 SLA 规则：{item.name}", category="sla", business_domain="sla")
    db.commit()
    return {"item": sla_profile_out(item)}


@app.patch("/api/sla/profiles/{profile_id}")
def update_sla_profile(profile_id: str, body: SLAProfileUpdate, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    item = db.get(SLAProfile, profile_id)
    if not item:
        raise HTTPException(404, "SLA 规则不存在")
    before = sla_profile_out(item)
    for key, value in body.model_dump().items():
        setattr(item, key, value)
    audit(db, user.email, "update_sla_profile", "sla_profile", item.id, f"更新 SLA 规则：{item.name}", category="sla", business_domain="sla", before=before, after=sla_profile_out(item))
    db.commit()
    return {"item": sla_profile_out(item)}


@app.post("/api/sla/service-levels")
def create_service_level(body: ServiceLevelBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    if db.scalar(select(ServiceLevel).where(or_(ServiceLevel.code == body.code, ServiceLevel.name == body.name))):
        raise HTTPException(409, "服务级别编码或名称已存在")
    item = ServiceLevel(**body.model_dump(), created_by=user.email)
    db.add(item)
    audit(db, user.email, "create_service_level", "service_level", item.id, f"创建服务级别：{item.name}", category="sla", business_domain="service_level")
    db.commit()
    return {"item": service_level_out(item)}


@app.patch("/api/sla/service-levels/{level_id}")
def update_service_level(level_id: str, body: ServiceLevelBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    item = db.get(ServiceLevel, level_id)
    if not item:
        raise HTTPException(404, "服务级别不存在")
    before = service_level_out(item)
    for key, value in body.model_dump().items():
        setattr(item, key, value)
    audit(db, user.email, "update_service_level", "service_level", item.id, f"更新服务级别：{item.name}", category="sla", business_domain="service_level", before=before, after=service_level_out(item))
    db.commit()
    return {"item": service_level_out(item)}


@app.get("/api/sla/service-levels")
def list_service_levels(user: User = Depends(current_user), db: Session = Depends(db_session)):
    levels = db.scalars(select(ServiceLevel).order_by(ServiceLevel.code)).all()
    return {"items": [service_level_out(x) for x in levels]}


@app.get("/api/sla/service-level-assignments")
def list_service_level_assignments(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    items = db.scalars(select(ServiceLevelAssignment).order_by(ServiceLevelAssignment.created_at.desc())).all()
    return {"items": [service_level_assignment_out(x, db.get(ServiceLevel, x.service_level_id), db.get(Enterprise, x.enterprise_id)) for x in items]}


@app.post("/api/sla/service-level-assignments")
def assign_service_level(body: ServiceLevelAssignmentBody, user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    if not db.get(Enterprise, body.enterprise_id):
        raise HTTPException(404, "企业不存在")
    level = db.get(ServiceLevel, body.service_level_id)
    if not level or level.status != "active":
        raise HTTPException(404, "服务级别不存在或未启用")
    existing = db.scalars(select(ServiceLevelAssignment).where(ServiceLevelAssignment.enterprise_id == body.enterprise_id, ServiceLevelAssignment.user_id == body.user_id, ServiceLevelAssignment.expires_at.is_(None))).all()
    for assignment in existing:
        assignment.expires_at = now()
    item = ServiceLevelAssignment(service_level_id=level.id, enterprise_id=body.enterprise_id, user_id=body.user_id, source="manual", expires_at=body.expires_at, created_by=user.email)
    db.add(item)
    audit(db, user.email, "assign_service_level", "service_level_assignment", item.id, f"绑定{level.name}服务级别", category="sla", business_domain="service_level", tenant_id=body.enterprise_id)
    db.commit()
    return {"item": service_level_assignment_out(item, level, db.get(Enterprise, body.enterprise_id))}


@app.get("/api/sla/my-service-level")
def my_service_level(user: User = Depends(current_user), db: Session = Depends(db_session)):
    enterprise = first_enterprise(db, user)
    level = current_service_level(db, enterprise.id, user.id)
    return {"enterprise_id": enterprise.id, "enterprise_name": enterprise.name, "item": service_level_out(level)}


@app.post("/api/sla/evaluate")
def evaluate_sla(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    end = now()
    start = end - timedelta(hours=24)
    created = []
    for profile in db.scalars(select(SLAProfile).where(SLAProfile.status == "active")).all():
        usage_query = select(ApiUsage).join(ApiGatewayRoute, ApiUsage.route_id == ApiGatewayRoute.id).where(ApiUsage.created_at >= start, ApiUsage.created_at <= end)
        if profile.product_id:
            usage_query = usage_query.where(ApiGatewayRoute.product_id == profile.product_id)
        usage = db.scalars(usage_query).all()
        sample_count = len(usage)
        success_count = sum(1 for x in usage if x.status_code < 400)
        availability = round(success_count / sample_count * 100, 4) if sample_count else 100.0
        avg_latency = round(sum(x.latency_ms for x in usage) / sample_count, 2) if sample_count else 0.0
        error_rate = round((sample_count - success_count) / sample_count * 100, 4) if sample_count else 0.0
        delivery_query = select(DeliveryTask).where(DeliveryTask.created_at >= start, DeliveryTask.created_at <= end)
        deliveries = db.scalars(delivery_query).all()
        delivery_count = len(deliveries)
        on_time = sum(1 for task in deliveries if task.status in {"completed", "pending_acceptance"} and (not task.sla_due_at or task.sla_due_at >= end))
        delivery_compliance = round(on_time / delivery_count * 100, 4) if delivery_count else 100.0
        reasons = []
        if availability < float(profile.availability_target): reasons.append(f"可用性 {availability}% < {float(profile.availability_target)}%")
        if avg_latency > profile.latency_target_ms: reasons.append(f"平均延迟 {avg_latency}ms > {profile.latency_target_ms}ms")
        if error_rate > float(profile.error_rate_target): reasons.append(f"错误率 {error_rate}% > {float(profile.error_rate_target)}%")
        if delivery_compliance < 100: reasons.append(f"交付及时率 {delivery_compliance}%")
        has_samples = sample_count > 0 or delivery_count > 0
        warning_reasons = []
        if has_samples and not reasons:
            availability_warning_threshold = min(100.0, float(profile.availability_target) + float(profile.warning_margin))
            if availability < availability_warning_threshold: warning_reasons.append(f"可用性接近阈值：当前 {availability}% / 目标 {float(profile.availability_target)}%")
            if avg_latency > profile.latency_target_ms * 0.8: warning_reasons.append(f"延迟接近阈值：当前 {avg_latency}ms / 目标 {profile.latency_target_ms}ms")
            if error_rate > float(profile.error_rate_target) * 0.8: warning_reasons.append(f"错误率接近阈值：当前 {error_rate}% / 上限 {float(profile.error_rate_target)}%")
            if delivery_compliance < 100: warning_reasons.append(f"交付及时率存在风险：当前 {delivery_compliance}%")
        warning = has_samples and bool(warning_reasons) and not reasons
        status = "breached" if reasons else "warning" if warning else "met"
        result = SLAResult(profile_id=profile.id, product_id=profile.product_id, period_start=start, period_end=end, sample_count=sample_count,
                           success_count=success_count, availability=availability, avg_latency_ms=avg_latency, error_rate=error_rate,
                           delivery_count=delivery_count, delivery_on_time=on_time, delivery_compliance=delivery_compliance,
                           status=status, breach_reason="；".join(reasons or warning_reasons))
        db.add(result)
        db.flush()
        if status in {"warning", "breached"}:
            notify_business_event(db, "SLA 服务异常" if status == "breached" else "SLA 服务预警", "服务等级考核发现异常，请查看考核结果。", "sla_result", result.id,
                                  event_key=f"sla:{result.id}:{status}:{end.isoformat()}", category="system",
                                  platform_roles=("super_admin", "platform_operator", "delivery_monitor"))
        created.append(result)
        audit(db, user.email, "evaluate_sla", "sla_result", result.id, f"SLA 考核：{profile.name} / {status}", category="sla", business_domain="sla", risk_level="high" if status == "breached" else "normal")
    db.commit()
    return {"items": [sla_result_out(x, db.get(SLAProfile, x.profile_id)) for x in created]}


@app.get("/api/sla/results")
def sla_results(user: User = Depends(current_user), db: Session = Depends(db_session)):
    require_platform_admin(user)
    items = db.scalars(select(SLAResult).order_by(SLAResult.calculated_at.desc()).limit(100)).all()
    return {"items": [sla_result_out(x, db.get(SLAProfile, x.profile_id)) for x in items]}


def file_out(item: FileObject) -> dict[str, Any]:
    try:
        scan = json.loads(item.scan_report or "{}")
    except json.JSONDecodeError:
        scan = {"clamav_report": item.scan_report}
    findings = scan.get("presidio_findings", [])
    if any(isinstance(entry, dict) and "start" in entry and "matched_text" not in entry for entry in findings):
        try:
            findings = enrich_legacy_presidio_findings(read_product_sample(item), findings)
        except Exception:
            pass
    findings = filter_presidio_dataset_false_positives(findings)
    return {"id": item.id, "product_id": item.product_id, "version_id": item.version_id, "object_name": item.object_name, "original_name": item.original_name, "content_type": item.content_type, "size": item.size, "checksum": item.checksum, "file_role": item.file_role, "version": item.version, "description": item.description, "status": item.status, "scan_status": item.scan_status, "scan_report": item.scan_report, "clamav_status": scan.get("clamav_status", item.scan_status), "clamav_report": scan.get("clamav_report", item.scan_report), "clamav_version": clamav_version(), "presidio_version": presidio_version(), "presidio_status": scan.get("presidio_status", "not_scanned"), "presidio_findings": findings, "scan_report_url": f"/api/files/{item.id}/scan-report.pdf", "clamav_report_url": f"/api/files/{item.id}/scan-report.pdf?report_type=clamav", "presidio_report_url": f"/api/files/{item.id}/scan-report.pdf?report_type=presidio", "download_url": f"/api/files/{item.id}/download", "scanned_at": item.scanned_at, "created_at": item.created_at}


def scan_report_pdf(item: FileObject, report_type: str = "combined") -> bytes:
    """Build a dependency-free PDF report; JSON details remain available in the API."""
    try:
        raw = json.loads(item.scan_report or "{}")
    except json.JSONDecodeError:
        raw = {"result": item.scan_report}
    clamav_status = raw.get("clamav_status", item.scan_status)
    clamav_report = raw.get("clamav_report", item.scan_report)
    presidio_status = raw.get("presidio_status", "not_scanned")
    presidio_findings = raw.get("presidio_findings", [])
    if any(isinstance(entry, dict) and "start" in entry and "matched_text" not in entry for entry in presidio_findings):
        try:
            presidio_findings = enrich_legacy_presidio_findings(read_product_sample(item), presidio_findings)
        except Exception:
            pass
    presidio_findings = filter_presidio_dataset_false_positives(presidio_findings)
    result = "通过" if clamav_status == "clean" else "发现风险" if clamav_status == "infected" else clamav_status
    lines = [
        "Market File Security Scan Report",
        f"File name: {item.original_name}",
        f"File size: {item.size} bytes",
        "Virus scanner: ClamAV",
        f"Virus scanner version: {clamav_version()}",
        f"Virus scan result: {clamav_status}",
        f"Conclusion: {result}",
        f"Conclusion date: {(item.scanned_at or now()).isoformat()}",
    ]
    if report_type in {"combined", "clamav"}:
        lines.append("ClamAV detail: " + str(clamav_report))
    if report_type in {"combined", "presidio"}:
        lines.extend([f"Presidio version: {presidio_version()}", f"Presidio result: {presidio_status}"])
        if presidio_findings:
            lines.append(f"Presidio findings count: {len(presidio_findings)}")
            labels = {"PERSON": "个人姓名", "PHONE_NUMBER": "电话号码", "EMAIL_ADDRESS": "邮箱地址", "LOCATION": "地理位置", "ORGANIZATION": "组织机构", "CREDIT_CARD": "银行卡/信用卡号", "IBAN_CODE": "银行账户", "IP_ADDRESS": "IP地址", "URL": "网址"}
            for index, finding in enumerate(presidio_findings[:100], 1):
                entity = finding.get("entity", "UNKNOWN") if isinstance(finding, dict) else "UNKNOWN"
                label = finding.get("entity_label") or labels.get(entity, entity) if isinstance(finding, dict) else entity
                score = finding.get("score") if isinstance(finding, dict) else None
                matched = finding.get("matched_text") or "未提取到具体内容" if isinstance(finding, dict) else str(finding)
                line_number = finding.get("line_number", "-") if isinstance(finding, dict) else "-"
                line_content = finding.get("line_content") or "-" if isinstance(finding, dict) else "-"
                lines.extend([
                    f"Finding {index}: {label} ({entity})",
                    f"Confidence: {float(score) * 100:.1f}%" if score is not None else "Confidence: -",
                    f"Matched content: {matched[:240]}",
                    f"Line: {line_number}",
                    f"Line content: {line_content[:1000]}",
                ])
        else:
            lines.append("Presidio findings: 未发现通过当前规则确认的敏感信息")
    # Keep the original Helvetica look for ASCII and switch only non-ASCII
    # runs to the Adobe GB CID font so Chinese text remains readable.
    def render_line(value: str) -> str:
        runs = []
        current = ""
        current_ascii = None
        for char in str(value):
            is_ascii = ord(char) < 128
            if current_ascii is not None and is_ascii != current_ascii:
                encoded = current.encode("ascii").hex().upper() if current_ascii else current.encode("utf-16-be").hex().upper()
                runs.append(f"/{'F1' if current_ascii else 'F2'} 9 Tf <{encoded}> Tj")
                current = ""
            current += char
            current_ascii = is_ascii
        if current:
            encoded = current.encode("ascii").hex().upper() if current_ascii else current.encode("utf-16-be").hex().upper()
            runs.append(f"/{'F1' if current_ascii else 'F2'} 9 Tf <{encoded}> Tj")
        return " ".join(runs) + " T*"

    stream = "BT /F1 9 Tf 48 780 Td 12 TL " + " ".join(render_line(line) for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        "<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /UniGB-UCS2-H /DescendantFonts [7 0 R] >>",
        f"<< /Length {len(stream.encode('ascii'))} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light /CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> /DW 1000 >>",
    ]
    pdf = "%PDF-1.4\n"; offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf.encode("ascii")))
        pdf += f"{index} 0 obj\n{obj}\nendobj\n"
    xref = len(pdf.encode("ascii"))
    pdf += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n"
    pdf += "".join(f"{offset:010d} 00000 n \n" for offset in offsets[1:])
    pdf += f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    return pdf.encode("ascii")


@app.get("/api/files/{file_id}/scan-report.pdf")
def download_scan_report(file_id: str, report_type: str = Query(default="combined"), user: User = Depends(current_user), db: Session = Depends(db_session)):
    item = db.get(FileObject, file_id)
    if not item:
        raise HTTPException(404, "文件不存在")
    if user.platform_role not in {"super_admin", "platform_operator", "product_manager", "business_reviewer", "quality_reviewer", "security_compliance"} and item.owner_id != user.id:
        raise HTTPException(403, "无权查看扫描报告")
    if report_type not in {"combined", "clamav", "presidio"}:
        raise HTTPException(400, "报告类型必须是 combined、clamav 或 presidio")
    report_name = f"{item.original_name}.{report_type}.scan-report.pdf"
    return Response(content=scan_report_pdf(item, report_type), media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=scan-report.pdf; filename*=UTF-8''{quote(report_name)}"})


def validate_product_archive(content: bytes, filename: str) -> None:
    """Reject unsafe archive metadata before persisting a product data file."""
    lower_name = filename.lower()
    if lower_name.endswith((".zip",)):
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                entries = archive.infolist()
                if len(entries) > 100_000:
                    raise HTTPException(400, "压缩包文件数量超过安全上限")
                total_size = sum(item.file_size for item in entries)
                if total_size > 50 * 1024 * 1024 * 1024:
                    raise HTTPException(400, "压缩包解压后总大小超过安全上限")
                for item in entries:
                    name = item.filename.replace("\\", "/")
                    if name.startswith("/") or "../" in name.split("/"):
                        raise HTTPException(400, "压缩包包含路径穿越条目")
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, "ZIP压缩包结构无效") from exc
    elif lower_name.endswith((".tar", ".tgz", ".tar.gz")):
        try:
            with tarfile.open(fileobj=BytesIO(content), mode="r:*") as archive:
                entries = archive.getmembers()
                if len(entries) > 100_000:
                    raise HTTPException(400, "压缩包文件数量超过安全上限")
                total_size = sum(item.size for item in entries if item.isfile())
                if total_size > 50 * 1024 * 1024 * 1024:
                    raise HTTPException(400, "压缩包解压后总大小超过安全上限")
                for item in entries:
                    name = item.name.replace("\\", "/")
                    if name.startswith("/") or "../" in name.split("/") or item.issym() or item.islnk():
                        raise HTTPException(400, "压缩包包含路径穿越或链接条目")
        except tarfile.TarError as exc:
            raise HTTPException(400, "TAR压缩包结构无效") from exc
    elif lower_name.endswith(".rar"):
        if not content.startswith(b"Rar!\x1a\x07"):
            raise HTTPException(400, "RAR压缩包文件头无效")
    elif lower_name.endswith(".7z"):
        if not content.startswith(b"7z\xbc\xaf\x27\x1c"):
            raise HTTPException(400, "7z压缩包文件头无效")


@app.get("/api/products/{product_id}/files")
def product_files(product_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    product_for_enterprise(product_id, user, db)
    items = db.scalars(select(FileObject).where(FileObject.product_id == product_id).order_by(FileObject.created_at.desc())).all()
    # 详情页只展示当前 Logo，历史 Logo 和缩略图仍保留在存储中供审计追溯。
    logos = [item for item in items if item.file_role == "product_logo"]
    latest_logo_id = logos[0].id if logos else ""
    visible = [item for item in items if item.file_role != "product_logo_thumbnail" and (item.file_role != "product_logo" or item.id == latest_logo_id)]
    return {"items": [file_out(item) for item in visible]}


@app.post("/api/files/upload")
def upload_file(
    upload: UploadFile = File(...),
    product_id: str | None = Form(default=None),
    version_id: str | None = Form(default=None),
    file_role: str = Form(default="product_data"),
    version: str = Form(default="v1.0"),
    description: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(db_session),
):
    if product_id:
        product = product_for_enterprise(product_id, user, db)
        if file_role in {"product_data", "product_logo"} and not user.platform_role:
            trading_policy["require_buyer"](db, user, product.enterprise_id)
        if file_role in {"product_data", "product_logo"} and product.status not in {"draft", "rejected", "security_unpublished"}:
            raise HTTPException(409, "已发布或审核中的产品不可替换文件")
    version_item = None
    if version_id:
        version_item = db.scalar(select(ProductReleaseVersion).where(ProductReleaseVersion.id == version_id, ProductReleaseVersion.product_id == product_id))
        if not version_item:
            raise HTTPException(400, "产品版本不存在或不属于该产品")
    if file_role == "product_data" and product_id:
        if not version_item:
            raise HTTPException(400, "产品交付文件必须绑定具体版本")
        trading_policy["protect_sold_file"](db, version_item)
    filename = upload.filename or "file"
    lower_name = filename.lower()
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(0)
    content = b""
    if file_role == "product_data":
        allowed = (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".rar", ".7z")
        if not lower_name.endswith(allowed):
            raise HTTPException(400, "数据文件仅支持 zip、tar、tar.gz、tgz、bz2、xz、rar 或 7z 压缩格式")
        if size >= 10 * 1024 * 1024 * 1024:
            raise HTTPException(413, "数据文件必须小于10GB")
        if size <= 100 * 1024 * 1024 or lower_name.endswith((".rar", ".7z")):
            content = upload.file.read() if size <= 100 * 1024 * 1024 else upload.file.read(1024 * 1024)
        else:
            content = upload.file.read(1024 * 1024)
        upload.file.seek(0)
        if size <= 100 * 1024 * 1024 or lower_name.endswith((".rar", ".7z")):
            validate_product_archive(content, filename)
    if file_role == "product_logo":
        name = lower_name
        content = upload.file.read()
        upload.file.seek(0)
        if not (name.endswith(".svg") or (upload.content_type or "").startswith("image/")):
            raise HTTPException(400, "Logo仅支持SVG和图片格式")
        if name.endswith(".svg"):
            svg_text = content.decode("utf-8", errors="ignore").lower()
            if "<script" in svg_text or "javascript:" in svg_text or "external" in svg_text or re.search(r"\son[a-z]+\s*=", svg_text):
                raise HTTPException(400, "SVG包含脚本、外部资源或事件属性，无法上传")
            root = re.search(r"<svg\b([^>]*)>", svg_text)
            attributes = root.group(1) if root else ""
            has_exact_size = bool(re.search(r"\bwidth\s*=\s*['\"]380(?:px)?['\"]", attributes) and re.search(r"\bheight\s*=\s*['\"]280(?:px)?['\"]", attributes))
            has_exact_viewbox = bool(re.search(r"\bviewbox\s*=\s*['\"]0\s+0\s+380\s+280['\"]", attributes))
            if not (has_exact_size or has_exact_viewbox):
                raise HTTPException(400, "SVG Logo必须声明380×280尺寸或对应viewBox")
        else:
            try:
                with Image.open(BytesIO(content)) as image:
                    if image.size != (380, 280):
                        raise HTTPException(400, "Logo图片尺寸必须为380×280")
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(400, "Logo图片无法解析") from exc
    scan_status, scan_report = ("not_scanned", "")
    if file_role in {"product_data", "product_logo"}:
        scan_status, scan_report = clamav_scan_stream(upload.file, size)
        if scan_status == "infected":
            audit(db, user.email, "reject_infected_file", "file", filename, scan_report, category="security", business_domain="product")
            raise HTTPException(400, "文件未通过ClamAV病毒扫描")
    upload.file.seek(0)
    hasher = hashlib.sha256()
    while True:
        chunk = upload.file.read(1024 * 1024)
        if not chunk:
            break
        hasher.update(chunk)
    upload.file.seek(0)
    object_name = f"{user.id}/{now().strftime('%Y%m%d')}/{secrets.token_hex(6)}-{filename}"
    if MINIO_ENDPOINT:
        client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=False)
        if not client.bucket_exists(MINIO_BUCKET):
            client.make_bucket(MINIO_BUCKET)
        client.put_object(MINIO_BUCKET, object_name, upload.file, length=size, content_type=upload.content_type or "application/octet-stream")
    if file_role == "product_logo" and product_id:
        old_files = db.scalars(select(FileObject).where(FileObject.product_id == product_id, FileObject.file_role.in_(["product_logo", "product_logo_thumbnail"]), FileObject.status != "deleted")).all()
        for old_file in old_files:
            old_file.status = "deleted"
    if file_role == "product_data" and product_id:
        version_filter = FileObject.version_id == version_item.id if version_item else FileObject.version_id.is_(None)
        old_files = db.scalars(select(FileObject).where(FileObject.product_id == product_id, FileObject.file_role == "product_data", version_filter, FileObject.status != "deleted")).all()
        for old_file in old_files:
            old_file.status = "deleted"
    if file_role in {"product_data", "product_logo"} and product_id:
        # A replacement invalidates all aggregate Presidio/security reports that
        # may still contain findings from the previous file.
        old_scans = db.scalars(select(ProductSecurityScan).where(ProductSecurityScan.product_id == product_id)).all()
        for old_scan in old_scans:
            db.delete(old_scan)
    item = FileObject(owner_id=user.id, product_id=product_id, version_id=version_item.id if version_item else None, object_name=object_name, original_name=filename, content_type=upload.content_type or "application/octet-stream", size=size, checksum=hasher.hexdigest(), file_role=file_role, version=version_item.version_code if version_item else version, description=description, scan_status=scan_status, scan_report=json.dumps({"clamav_status": scan_status, "clamav_report": scan_report}, ensure_ascii=False), scanned_at=now() if scan_status not in {"not_scanned", "unavailable", "disabled"} else None)
    db.add(item)
    db.flush()
    if file_role == "product_data" and scan_status == "clean":
        try:
            sample = read_product_sample(item, content if size <= 100 * 1024 * 1024 else None)
            presidio_findings, presidio_status = presidio_analyze(sample) if sample else ([], "not_scanned")
            item.scan_report = json.dumps({"clamav_status": scan_status, "clamav_report": scan_report, "presidio_status": presidio_status, "presidio_findings": presidio_findings, "sample_scanned": bool(sample)}, ensure_ascii=False)
        except Exception as exc:
            item.scan_report = json.dumps({"clamav_status": scan_status, "clamav_report": scan_report, "presidio_status": "error", "presidio_findings": [], "presidio_error": str(exc)[:240]}, ensure_ascii=False)
    if file_role == "product_logo" and product_id and not lower_name.endswith(".svg") and MINIO_ENDPOINT:
        try:
            with Image.open(BytesIO(content)) as image:
                image = image.convert("RGBA")
                image.thumbnail((190, 140), Image.Resampling.LANCZOS)
                thumbnail_content = BytesIO()
                image.save(thumbnail_content, format="PNG", optimize=True)
                thumbnail_bytes = thumbnail_content.getvalue()
            thumbnail_name = f"{user.id}/{now().strftime('%Y%m%d')}/{secrets.token_hex(6)}-{filename}.thumb.png"
            client.put_object(MINIO_BUCKET, thumbnail_name, BytesIO(thumbnail_bytes), length=len(thumbnail_bytes), content_type="image/png")
            thumbnail = FileObject(owner_id=user.id, product_id=product_id, version_id=version_item.id if version_item else None, object_name=thumbnail_name, original_name=f"{filename}.thumb.png", content_type="image/png", size=len(thumbnail_bytes), checksum=hashlib.sha256(thumbnail_bytes).hexdigest(), file_role="product_logo_thumbnail", version=item.version, description="Logo缩略图", status="active", scan_status="clean", scan_report="由平台生成", scanned_at=now())
            db.add(thumbnail)
            db.flush()
            product.logo_thumbnail_file_id = thumbnail.id
        except Exception as exc:
            audit(db, user.email, "generate_logo_thumbnail_failed", "file", item.id, str(exc)[:240], category="security", business_domain="product", risk_level="warning")
    if file_role == "product_logo" and product_id:
        product.logo_file_id = item.id
    audit(db, user.email, "upload_file", "file", item.id, f"{item.original_name}; scan={scan_status}; size={size}", category="security" if file_role == "product_data" else "ops", business_domain="product")
    db.commit()
    db.refresh(item)
    return file_out(item)


@app.get("/api/files/{file_id}/download")
def download_file(file_id: str, order_id: str = "", user: User = Depends(current_user), db: Session = Depends(db_session)):
    """Serve persisted identity/license files to their owner or authorized reviewers."""
    item = db.get(FileObject, file_id)
    if not item or item.status == "deleted":
        raise HTTPException(404, "文件不存在")
    reviewer = user.platform_role in {"super_admin", "platform_operator", "product_manager", "business_reviewer", "quality_reviewer", "security_compliance"}
    owner = item.owner_id == user.id
    if item.file_role == "delivery_attachment" and not owner and not reviewer:
        attachment = db.scalar(select(DeliveryAttachment).where(DeliveryAttachment.file_id == item.id))
        task = db.get(DeliveryTask, attachment.task_id) if attachment else None
        order = db.get(Order, task.order_id) if task else None
        member = db.scalar(select(Membership).where(Membership.user_id == user.id, Membership.enterprise_id.in_([order.buyer_enterprise_id, order.provider_enterprise_id]) if order else False, Membership.status == "active")) if order else None
        owner = bool(member)
    if item.file_role != "product_data" and not owner and not reviewer:
        raise HTTPException(403, "无权查看该文件")
    download_log = None
    if item.file_role == "product_data":
        if reviewer:
            order = None
        elif not order_id:
            raise HTTPException(400, "下载产品数据文件必须提供订单号")
        else:
            order = db.get(Order, order_id)
            member = db.scalar(select(Membership).where(Membership.enterprise_id == order.buyer_enterprise_id, Membership.user_id == user.id, Membership.status == "active")) if order and not user.platform_role else None
            if not order or order.product_id != item.product_id or order.product_version_id != item.version_id or order.payment_status != "paid" or user.platform_role or (order.buyer_user_id != user.id and not member):
                raise HTTPException(403, "当前用户没有该订单文件的下载权限")
            settlement_metering["require_order_access"](db, user, order)
            product = db.get(Product, item.product_id)
            download_limit = order_economics["delivery_snapshot"](order)["download_limit"] if order.snapshot_version == 1 else int(product.download_limit or 0) if product else 0
            used = db.scalar(select(func.count(FileDownloadLog.id)).where(FileDownloadLog.file_id == item.id, FileDownloadLog.order_id == order.id, FileDownloadLog.success.is_(True))) or 0
            download_log = FileDownloadLog(file_id=item.id, order_id=order.id, user_id=user.id, success=False)
            if download_limit > 0 and used >= download_limit:
                download_log.detail = f"下载次数已达上限：{used}/{download_limit}"
                db.add(download_log)
                audit(db, user.email or user.phone or user.id, "download_file_denied", "file", item.id, download_log.detail, category="delivery", business_domain="product", order_id=order.id, risk_level="warning")
                db.commit()
                raise HTTPException(429, "该订单的文件下载次数已用尽")
    if not MINIO_ENDPOINT:
        raise HTTPException(503, "文件存储服务未配置")
    client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS_KEY, secret_key=MINIO_SECRET_KEY, secure=False)
    try:
        response = client.get_object(MINIO_BUCKET, item.object_name)
        content = response.read()
        response.close()
        response.release_conn()
    except Exception as exc:
        raise HTTPException(404, "文件内容不存在") from exc
    if download_log:
        download_log.success = True
        download_log.detail = "下载成功"
        db.add(download_log)
        db.flush()
        settlement_metering["record_download"](db, order, download_log, item, len(content))
        audit(db, user.email or user.phone or user.id, "download_file", "file", item.id, item.original_name, category="delivery", business_domain="product", order_id=download_log.order_id)
        db.commit()
    return Response(content=content, media_type=item.content_type, headers={"Content-Disposition": f"inline; filename=download; filename*=UTF-8''{quote(item.original_name)}"})


@app.get("/api/orders/{order_id}/product-files")
def order_product_files(order_id: str, user: User = Depends(current_user), db: Session = Depends(db_session)):
    order = db.get(Order, order_id)
    if not order or order.payment_status != "paid":
        raise HTTPException(404, "订单不存在或尚未支付")
    reviewer = user.platform_role in {"super_admin", "platform_operator"}
    member = db.scalar(select(Membership.id).where(Membership.enterprise_id == order.buyer_enterprise_id, Membership.user_id == user.id, Membership.status == "active")) if not user.platform_role else None
    if not reviewer and (user.platform_role or (order.buyer_user_id != user.id and not member)):
        raise HTTPException(403, "无权查看该订单文件")
    settlement_metering["require_order_access"](db, user, order)
    items = db.scalars(select(FileObject).where(FileObject.product_id == order.product_id, FileObject.version_id == order.product_version_id, FileObject.file_role == "product_data", FileObject.status != "deleted")).all()
    product = db.get(Product, order.product_id)
    limit = order_economics["delivery_snapshot"](order)["download_limit"] if order.snapshot_version == 1 else int(product.download_limit or 0) if product else 0
    return {"download_limit": limit, "items": [{**file_out(item), "downloaded": db.scalar(select(func.count(FileDownloadLog.id)).where(FileDownloadLog.file_id == item.id, FileDownloadLog.order_id == order.id, FileDownloadLog.success.is_(True))) or 0, "download_url": f"/api/files/{item.id}/download?order_id={order.id}"} for item in items]}


from .message_center import install as install_message_center

message_center = install_message_center(globals())

from .settlement_metering import install as install_settlement_metering

settlement_metering = install_settlement_metering(globals())

from .audit_support import install as install_audit_support

audit_support = install_audit_support(globals())

from .reconciliation_support import install as install_reconciliation_support

ledger_support = install_reconciliation_support(globals())

from .trading_policy import install as install_trading_policy

trading_policy = install_trading_policy(globals())

from .safe_upstream import validate_upstream_url, validated_http_request
from .version_gateway import install as install_version_gateway

version_gateway = install_version_gateway(globals())

from .storefront import install as install_storefront

storefront = install_storefront(globals())

from .order_economics import install as install_order_economics

order_economics = install_order_economics(globals())

from .order_workflow import install as install_order_workflow

order_workflow = install_order_workflow(globals())
