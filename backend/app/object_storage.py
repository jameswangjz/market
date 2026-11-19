"""Object storage settings shared by API processes and background workers."""

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import ssl
from dataclasses import dataclass
from io import BytesIO
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool
from sqlalchemy import func, select
from urllib3 import PoolManager, Timeout

SETTING_KEY = "s3_storage"
STORAGE_LOCK = 723401830042
ROUTE = "/api/admin/settings/s3-storage"
DEV_SIGNING_KEY = "market-development-secret-change-me"
PROBE_PAYLOAD = b"market-s3-storage-check"
CERTIFICATE = re.compile(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----")


class Settings(BaseModel):
    provider: Literal["minio", "h3c"] = "minio"
    endpoint: str = Field(max_length=260)
    secure: StrictBool = False
    region: str = Field(default="", max_length=64)
    bucket: str = Field(max_length=63)
    ca_pem: str = Field(default="", max_length=64 * 1024)
    auth_mode: Literal["ak_sk_v4"] = "ak_sk_v4"


class SettingsBody(Settings):
    model_config = ConfigDict(extra="forbid")
    access_key: SecretStr | None = None
    secret_key: SecretStr | None = None


@dataclass(frozen=True)
class Storage:
    client: object
    bucket: str
    allow_bucket_creation: bool

    def ensure_bucket(self):
        if self.client.bucket_exists(self.bucket):
            return
        if not self.allow_bucket_creation:
            raise HTTPException(503, "预建的华三 S3 私有桶不可用")
        self.client.make_bucket(self.bucket)


def _lock(db, exclusive=False):
    if db.get_bind().dialect.name != "postgresql":
        return
    lock = func.pg_advisory_xact_lock if exclusive else func.pg_advisory_xact_lock_shared
    db.execute(select(lock(STORAGE_LOCK)))


def _from_environment(ns):
    config = Settings(endpoint=ns.get("MINIO_ENDPOINT", ""), bucket=ns.get("MINIO_BUCKET", "market-files"))
    return config, (ns.get("MINIO_ACCESS_KEY", ""), ns.get("MINIO_SECRET_KEY", ""))


def _setting(db, ns):
    model = ns.get("SystemSetting")
    if model is None:
        return None
    return db.scalar(select(model).where(model.setting_key == SETTING_KEY)
                     .execution_options(populate_existing=True))


def _public(config, credentials_configured, source):
    return {**config.model_dump(), "credentials_configured": credentials_configured, "source": source}


def _stored(row, ns):
    if row is None:
        config, credentials = _from_environment(ns)
        return config, credentials
    values = json.loads(row.setting_value)
    return Settings.model_validate(values["config"]), values["encrypted_credentials"]


def _cipher(ns):
    signing_key = ns["JWT_SECRET"]
    if not signing_key or signing_key == DEV_SIGNING_KEY:
        raise HTTPException(503, "保存 S3 凭据前请配置部署环境的 JWT_SECRET")
    key = hmac.digest(signing_key.encode(), b"market/object-storage/credentials/v1", hashlib.sha256)
    return Fernet(base64.urlsafe_b64encode(key))


def _credentials(stored, ns):
    if isinstance(stored, tuple):
        return stored
    try:
        access_key, secret_key = json.loads(_cipher(ns).decrypt(stored.encode()))
    except (InvalidToken, TypeError, ValueError) as error:
        raise HTTPException(503, "S3 凭据无法解密，请重新录入访问密钥") from error
    return access_key, secret_key


def _tls_context(ca_pem):
    if ca_pem and (not CERTIFICATE.search(ca_pem) or CERTIFICATE.sub("", ca_pem).strip()):
        raise HTTPException(422, "私有 CA 须为 PEM 格式的证书链")
    context = ssl.create_default_context()
    if ca_pem:
        try:
            context.load_verify_locations(cadata=ca_pem)
        except ssl.SSLError as error:
            raise HTTPException(422, "无法读取私有 CA 证书") from error
    return context


def _connect(config, credentials, ns, timeout=None):
    args = dict(access_key=credentials[0], secret_key=credentials[1], secure=config.secure)
    if config.region:
        args["region"] = config.region
    if config.secure or timeout is not None:
        http_options = {}
        if config.secure:
            http_options["ssl_context"] = _tls_context(config.ca_pem)
        if timeout is not None:
            http_options.update(timeout=timeout, retries=False)
        else:
            http_options["timeout"] = Timeout(connect=5, read=300)
        args["http_client"] = PoolManager(**http_options)
    return Storage(ns["Minio"](config.endpoint, **args), config.bucket, config.provider == "minio")


def open_storage(db, ns, *, writing=False, required=True, timeout=None):
    if writing and "SystemSetting" in ns:
        _lock(db)
    row = _setting(db, ns)
    config, stored = _stored(row, ns)
    if not config.endpoint:
        if required:
            raise HTTPException(503, "文件存储服务未配置")
        return None
    return _connect(config, _credentials(stored, ns), ns, timeout)


def _validate(body):
    endpoint = body.endpoint.strip().lower()
    try:
        parsed = urlsplit("//" + endpoint)
        port = parsed.port
        host = parsed.hostname
        if (not host or not re.fullmatch(r"[a-z0-9.\-:\[\]]+", endpoint)
                or endpoint.endswith(":") or port == 0):
            raise ValueError
        if ":" in host:
            ipaddress.IPv6Address(host)
        elif any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                 for label in host.split(".")):
            raise ValueError
    except ValueError as error:
        raise HTTPException(422, "存储访问地址须为主机名或 IP，可附端口") from error

    bucket = body.bucket.strip()
    if (len(bucket) < 3 or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", bucket)
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                   for label in bucket.split("."))):
        raise HTTPException(422, "桶名须为 3 至 63 位小写字母、数字、短横线或点，且符合域名式命名规则")
    region = body.region.strip()
    if region and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", region):
        raise HTTPException(422, "Region 须使用小写字母、数字和连字符")
    if body.provider == "h3c" and not region:
        raise HTTPException(422, "请选择华三网关要求的 Region")
    ca_pem = body.ca_pem.strip()
    if ca_pem:
        if not body.secure:
            raise HTTPException(422, "启用 HTTPS 后可配置私有 CA")
        _tls_context(ca_pem)
    return Settings(provider=body.provider, endpoint=endpoint, secure=body.secure,
                    region=region, bucket=bucket, ca_pem=ca_pem, auth_mode=body.auth_mode)


def _candidate(body, previous, stored, ns):
    config = _validate(body)
    access_key = body.access_key.get_secret_value() if body.access_key else ""
    secret_key = body.secret_key.get_secret_value() if body.secret_key else ""
    if access_key or secret_key:
        if (not access_key or not secret_key or len(access_key) > 256 or len(secret_key) > 256
                or any(ch.isspace() or not ch.isascii() or not ch.isprintable()
                       for ch in access_key + secret_key)):
            raise HTTPException(422, "请同时填写有效的 Access Key 和 Secret Key")
        return config, (access_key, secret_key)
    if (config.provider, config.endpoint, config.secure) != (
            previous.provider, previous.endpoint.lower(), previous.secure):
        raise HTTPException(422, "变更存储类型、地址或 HTTPS 配置时须填写访问密钥")
    credentials = _credentials(stored, ns)
    if not all(credentials):
        raise HTTPException(422, "请配置 Access Key 和 Secret Key")
    return config, credentials


def _probe(storage):
    client, bucket = storage.client, storage.bucket
    try:
        present = client.bucket_exists(bucket)
    except Exception as error:
        raise HTTPException(502, "无法检查 S3 桶，请核对访问地址、HTTPS、CA、Region 和访问密钥") from error
    if not present:
        raise HTTPException(422, "指定的 S3 私有桶不存在，请先在存储侧创建")
    key = f"market-storage-probe/{secrets.token_hex(16)}"
    try:
        client.put_object(bucket, key, BytesIO(PROBE_PAYLOAD), len(PROBE_PAYLOAD))
        if client.stat_object(bucket, key).size != len(PROBE_PAYLOAD):
            raise ValueError("Unexpected size")
        response = client.get_object(bucket, key, offset=1, length=3)
        try:
            if response.status != 206 or response.read(3) != PROBE_PAYLOAD[1:4]:
                raise ValueError("Unexpected Range response")
        finally:
            response.close()
            response.release_conn()
    except Exception as error:
        try:
            client.remove_object(bucket, key)
        except Exception:
            raise HTTPException(502, "S3 读写验证失败，测试对象清理未确认") from error
        raise HTTPException(502, "S3 对象上传、元数据或范围读取验证失败，请核对桶权限和网关兼容性") from error
    try:
        client.remove_object(bucket, key)
    except Exception as error:
        raise HTTPException(502, "S3 对象删除验证失败，请检查桶的删除权限") from error


def _verify_existing_objects(db, ns, storage):
    file = ns["FileObject"]
    attachment = ns["message_center"]["Attachment"]
    expected = dict(db.execute(select(file.object_name, file.size).where(file.status != "deleted")).all())
    expected.update(db.execute(select(attachment.object_name, attachment.size)).all())
    if not expected:
        return
    try:
        for item in storage.client.list_objects(storage.bucket, recursive=True):
            if item.object_name in expected and item.size == expected[item.object_name]:
                del expected[item.object_name]
                if not expected:
                    return
    except Exception as error:
        raise HTTPException(502, "无法核对目标桶对象，请检查桶列举权限") from error
    raise HTTPException(409, f"目标桶有 {len(expected)} 个历史引用对象缺失或大小不一致，请完成迁移后再启用")


def install(ns):
    def require_admin(user):
        if user.platform_role != "super_admin":
            raise HTTPException(403, "只有平台系统管理员可以配置 S3 存储")

    def candidate_storage(body, previous, stored):
        config, credentials = _candidate(body, previous, stored, ns)
        return config, credentials, _connect(config, credentials, ns, Timeout(connect=3, read=8))

    @ns["app"].get(ROUTE)
    def settings(user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        require_admin(user)
        row = _setting(db, ns)
        config, stored = _stored(row, ns)
        return _public(config, bool(stored) if row else all(stored), "database" if row else "environment")

    @ns["app"].post(ROUTE + "/test")
    def test_settings(body: SettingsBody, user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        require_admin(user)
        previous, stored = _stored(_setting(db, ns), ns)
        _, _, storage = candidate_storage(body, previous, stored)
        _probe(storage)
        return {"ok": True, "message": "连接成功，已验证桶访问、写入、范围读取和删除"}

    @ns["app"].patch(ROUTE)
    def update_settings(body: SettingsBody, user=Depends(ns["current_user"]), db=Depends(ns["db_session"])):
        require_admin(user)
        cipher = _cipher(ns)
        _lock(db, exclusive=True)
        row = _setting(db, ns)
        previous, stored = _stored(row, ns)
        config, credentials, storage = candidate_storage(body, previous, stored)
        _probe(storage)
        if (previous.endpoint, previous.bucket, previous.region, previous.secure, previous.provider) != (
                config.endpoint, config.bucket, config.region, config.secure, config.provider):
            _verify_existing_objects(db, ns, storage)
        source = "database" if row else "environment"
        if row is None:
            row = ns["SystemSetting"](setting_key=SETTING_KEY)
            db.add(row)
        row.is_secret = True
        row.updated_by = user.email or user.phone or user.id
        row.setting_value = json.dumps({
            "config": config.model_dump(),
            "encrypted_credentials": cipher.encrypt(json.dumps(credentials).encode()).decode(),
        })
        before = _public(previous, bool(stored) if source == "database" else all(stored), source)
        after = _public(config, True, "database")
        ns["audit"](db, row.updated_by, "update_s3_storage_settings", "system_setting", SETTING_KEY,
                    category="ops", before={**before, "ca_pem": bool(previous.ca_pem)},
                    after={**after, "ca_pem": bool(config.ca_pem)})
        db.commit()
        return after
