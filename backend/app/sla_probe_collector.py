"""Independent business-entry probe persistence, without publication side effects.

The trusted resolver supplies publication/contract evidence and actual service
intervals. No current product status or enterprise SLA tier is inferred here.
Caller owns commit/rollback. Only explicitly invoked pruning can remove history.
ns['validated_http_request'] must be the safe_upstream adapter (or an equally
bounded trusted implementation): it streams internally, caps GET at 4096 bytes,
does not read HEAD bodies, disables proxies and redirects, and pins Host/SNI.
Its public signature has no stream argument. No live resolver/worker is installed
by this module. ns['sla_probe_targets'] and optional ns['sla_probe_headers'] are
trusted internal hooks, never supplied by an HTTP client.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import secrets
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import mapped_column


INTERVAL_SECONDS = 300
MIN_RETENTION_DAYS = 180


class IdentitySourceMissing(Exception):
    pass


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware timestamp required")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ProbeTarget:
    product_id: str
    version_id: str
    entry_id: str
    service_from: datetime
    service_until: datetime | None
    publication_evidence_id: str
    eligibility_evidence_id: str
    currently_published: bool
    active_sold_contract: bool | None
    contract_evidence_id: str = ""
    url: str | None = None
    method: str = "HEAD"
    timeout_seconds: float = 5
    source_scope: str = "business_entry"
    entry_kind: str = "unsupported"
    requires_identity: bool = False
    source_evidence_id: str = ""
    config_version: str = ""
    failure_responsibility: str = "unknown"
    responsibility_evidence_id: str = ""

    def __post_init__(self):
        for name in ("product_id", "version_id", "entry_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 180:
                raise ValueError("invalid target identity")
        for name in ("publication_evidence_id", "eligibility_evidence_id", "contract_evidence_id",
                     "source_evidence_id", "config_version", "responsibility_evidence_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > 180:
                raise ValueError("invalid evidence reference")
        start = _utc(self.service_from)
        if self.service_until is not None and _utc(self.service_until) <= start:
            raise ValueError("invalid service interval")
        if type(self.requires_identity) is not bool or type(self.currently_published) is not bool or (self.active_sold_contract is not None
                and type(self.active_sold_contract) is not bool):
            raise ValueError("invalid eligibility flags")
        if self.method not in {"GET", "HEAD"}:
            raise ValueError("probe must be read-only GET/HEAD")
        if (isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds) or not 0.1 <= self.timeout_seconds <= 5):
            raise ValueError("probe timeout must be between 0.1 and five seconds")
        if self.source_scope not in {"business_entry", "management", "api_upstream"}:
            raise ValueError("invalid probe scope")
        if self.entry_kind not in {"api_gateway", "saas_business", "unsupported"}:
            raise ValueError("invalid business entry kind")
        if self.url is not None and not isinstance(self.url, str):
            raise ValueError("invalid probe URL type")
        if self.failure_responsibility not in {"platform", "provider", "unknown"}:
            raise ValueError("invalid responsibility")


def install(ns):
    class ProbeSlot(ns["Base"]):
        __tablename__ = "sla_business_probe_slots"
        id = mapped_column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
        # Polymorphic version IDs and historical evidence deliberately do not
        # cascade away when a product/configuration is removed.
        product_id = mapped_column(String(180), nullable=False, index=True)
        version_id = mapped_column(String(180), nullable=False)
        entry_id = mapped_column(String(180), nullable=False)
        scope = mapped_column(String(30), nullable=False, default="business_entry")
        slot = mapped_column(Integer, nullable=False)
        scheduled_at = mapped_column(DateTime(timezone=True), nullable=False, index=True)
        observed_at = mapped_column(DateTime(timezone=True), nullable=False)
        completed_at = mapped_column(DateTime(timezone=True), nullable=True)
        service_from = mapped_column(DateTime(timezone=True), nullable=False)
        service_until = mapped_column(DateTime(timezone=True), nullable=True)
        config_digest = mapped_column(String(64), nullable=False)
        config_summary_json = mapped_column(Text, nullable=False)
        status = mapped_column(String(30), nullable=False, default="claimed")
        reason_code = mapped_column(String(80), nullable=False, default="")
        status_code = mapped_column(Integer, nullable=True)
        latency_ms = mapped_column(Integer, nullable=True)
        responsibility = mapped_column(String(20), nullable=False, default="unknown")
        responsibility_evidence_id = mapped_column(String(180), nullable=False, default="")
        publication_evidence_id = mapped_column(String(180), nullable=False, default="")
        eligibility_evidence_id = mapped_column(String(180), nullable=False, default="")
        contract_evidence_id = mapped_column(String(180), nullable=False, default="")
        source_evidence_id = mapped_column(String(180), nullable=False, default="")
        policy_version = mapped_column(String(80), nullable=False, default="sla-business-probe-v1")
        __table_args__ = (UniqueConstraint("product_id", "version_id", "entry_id", "slot",
                                          name="uq_sla_business_probe_slot"),)

    def eligible(target, instant, scheduled):
        # Slot belongs to [service_from, service_until), not to the sweep time.
        if scheduled < _utc(target.service_from) or (target.service_until is not None
                and (scheduled >= _utc(target.service_until) or instant >= _utc(target.service_until))):
            return "outside_service_interval"
        if not target.publication_evidence_id or not target.eligibility_evidence_id:
            return "eligibility_source_missing"
        if target.currently_published:
            return "eligible"
        if target.active_sold_contract is True and target.contract_evidence_id:
            return "eligible"
        return "outside_service_scope" if target.active_sold_contract is False else "eligibility_source_missing"

    def collect(db, targets, *, instant=None):
        instant = _utc(instant if instant is not None else ns.get("now", lambda: datetime.now(timezone.utc))())
        slot = int(instant.timestamp()) // INTERVAL_SECONDS
        scheduled = datetime.fromtimestamp(slot * INTERVAL_SECONDS, timezone.utc)
        # Resolve/validate the full batch first, so duplicate target inputs cannot
        # choose an arbitrary configuration based on iteration order.
        distinct = {}
        for target in targets:
            if not isinstance(target, ProbeTarget):
                raise ValueError("expected trusted ProbeTarget")
            key = (target.product_id, target.version_id, target.entry_id)
            if key in distinct and distinct[key] != target:
                raise ValueError("conflicting target configurations")
            distinct[key] = target
        counts = dict(created=0, duplicates=0, skipped=0, healthy=0, failed=0, source_missing=0, unknown=0)
        for target in distinct.values():
            eligibility = eligible(target, instant, scheduled)
            if eligibility in {"outside_service_interval", "outside_service_scope"}:
                counts["skipped"] += 1
                continue
            summary = {"method": target.method, "timeout_seconds": target.timeout_seconds,
                       "config_version": target.config_version, "source_scope": target.source_scope,
                       "entry_kind": target.entry_kind, "requires_identity": target.requires_identity}
            encoded = json.dumps(summary, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(json.dumps({**summary, "url": target.url}, sort_keys=True,
                                               separators=(",", ":")).encode()).hexdigest()
            row = ProbeSlot(product_id=target.product_id, version_id=target.version_id,
                entry_id=target.entry_id, slot=slot, scheduled_at=scheduled, observed_at=instant,
                service_from=target.service_from, service_until=target.service_until,
                config_digest=digest, config_summary_json=encoded,
                publication_evidence_id=target.publication_evidence_id,
                eligibility_evidence_id=target.eligibility_evidence_id,
                contract_evidence_id=target.contract_evidence_id,
                source_evidence_id=target.source_evidence_id)
            identity = (ProbeSlot.product_id == target.product_id, ProbeSlot.version_id == target.version_id,
                        ProbeSlot.entry_id == target.entry_id, ProbeSlot.slot == slot)
            try:
                with db.begin_nested():
                    db.add(row)
                    db.flush()
            except IntegrityError:
                if db.scalar(select(ProbeSlot.id).where(*identity)) is None:
                    raise
                counts["duplicates"] += 1
                continue
            counts["created"] += 1
            request = ns.get("validated_http_request")
            policy = ns.get("application_address_policy", {}).get("validate_url")
            if eligibility != "eligible":
                row.status, row.reason_code = "source_missing", eligibility
            elif (target.source_scope != "business_entry" or target.entry_kind == "unsupported" or not target.source_evidence_id
                    or not target.config_version or not target.url or not callable(request) or not callable(policy)):
                row.status, row.reason_code = "source_missing", "business_probe_source_missing"
            else:
                started = time.monotonic()
                response = None
                try:
                    parsed = urlsplit(target.url)
                    if parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
                        raise ValueError("probe URL cannot contain credentials, query or fragment")
                    policy(target.url, db=db)
                    # Credential resolution is explicit, ephemeral and server-owned.
                    # SaaS landing/login pages cannot prove an authorized user path.
                    header_source = ns.get("sla_probe_headers")
                    headers = header_source(db, target) if callable(header_source) else None
                    if target.requires_identity or target.entry_kind == "saas_business":
                        if not isinstance(headers, dict) or not headers:
                            raise IdentitySourceMissing()
                    if headers is not None and (not isinstance(headers, dict)
                            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items())):
                        raise ValueError("invalid ephemeral headers")
                    response = request(target.method, target.url, timeout=target.timeout_seconds,
                                       follow_redirects=False, headers=headers or {})
                    code = response.status_code
                    if type(code) is not int or not 100 <= code <= 599:
                        raise ValueError("invalid probe response status")
                    row.status_code = code
                    row.status, row.reason_code = ("healthy", "http_success") if 200 <= code < 300 else ("failed", "http_failure")
                except IdentitySourceMissing:
                    row.status, row.reason_code = "source_missing", "business_identity_source_missing"
                except (httpx.TimeoutException, TimeoutError):
                    row.status, row.reason_code = "failed", "probe_timeout"
                except httpx.NetworkError:
                    row.status, row.reason_code = "failed", "connection_failure"
                except ValueError:
                    row.status, row.reason_code = "source_missing", "invalid_or_unsafe_source"
                except Exception:
                    row.status, row.reason_code = "unknown", "probe_adapter_error"
                finally:
                    row.latency_ms = max(0, round((time.monotonic() - started) * 1000))
                    if response is not None:
                        try:
                            response.close()
                        except Exception:
                            pass
                if row.status == "failed" and target.responsibility_evidence_id:
                    row.responsibility = target.failure_responsibility
                    row.responsibility_evidence_id = target.responsibility_evidence_id
            row.completed_at = _utc(ns.get("now", lambda: datetime.now(timezone.utc))())
            counts[row.status] += 1
            db.flush()
        return counts

    def run_once(db, *, instant=None):
        instant = _utc(instant if instant is not None else ns.get("now", lambda: datetime.now(timezone.utc))())
        resolver = ns.get("sla_probe_targets")
        if not callable(resolver):
            return {"status": "source_missing", "reason_code": "target_resolver_missing", "created": 0}
        targets = resolver(db, instant)
        if targets is None:
            return {"status": "source_missing", "reason_code": "target_scope_unknown", "created": 0}
        return {"status": "collected", **collect(db, targets, instant=instant)}

    def prune(db, *, as_of, retention_days=MIN_RETENTION_DAYS):
        if type(retention_days) is not int or retention_days < MIN_RETENTION_DAYS:
            raise ValueError("probe history retention must be at least 180 days")
        cutoff = _utc(as_of) - timedelta(days=retention_days)
        result = db.execute(delete(ProbeSlot).where(ProbeSlot.scheduled_at < cutoff))
        return result.rowcount

    hooks = {"Target": ProbeTarget, "Slot": ProbeSlot, "collect": collect, "run_once": run_once, "prune": prune}
    ns["sla_probe_collector"] = hooks
    return hooks
