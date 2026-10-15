"""Pure selection of trusted canonical facts; no collection or persistence.

Adapters must resolve tenant/product/version/entry scope before supplying facts.
Evidence flags come from a server-owned ledger, never request headers. Missing
collection or unresolved attribution cannot produce a compliant period result.
"""
from dataclasses import dataclass, replace
from datetime import datetime
from math import ceil
from typing import Iterable

from . import sla_metrics as metrics


POLICY_VERSION = "sla-online-v1.1"
EXCLUSIONS = {
    400: "caller_parameters", 422: "caller_parameters",
    401: "buyer_credentials", 403: "buyer_credentials",
    404: "undeclared_path", 429: "contract_quota", 499: "client_cancel",
}
SYSTEM_FAILURES = frozenset({"upstream_failure", "timeout", "connection_failure",
                             "forward_auth_failure", "missing_published_route",
                             "incorrect_rate_limit", "valid_input_failure"})


def _source_flag(value):
    if type(value) is not bool:
        raise ValueError("source_available must be boolean")


@dataclass(frozen=True)
class RequestFact:
    request_id: str
    source: str
    status_code: int | None
    duration_ms: float | None
    authorized: bool | None
    published_route: bool | None
    terminal: bool = True
    traffic: str = "business"
    trusted_evidence: bool = False
    reason: str = "unknown"
    responsibility: str = "unknown"
    redirect_allowed: bool = False
    maintenance_contract_exclusion: bool = False
    maintenance_preapproved: bool = False
    policy_version: str = POLICY_VERSION

    def __post_init__(self):
        if (not isinstance(self.request_id, str) or not self.request_id.strip()
                or not isinstance(self.policy_version, str) or not self.policy_version.strip()):
            raise ValueError("request identity and policy version required")
        if self.source not in {"gateway", "resource"}:
            raise ValueError("unknown request source")
        if self.responsibility not in {"platform", "provider", "caller", "unknown"}:
            raise ValueError("unknown responsibility")
        if self.traffic not in {"business", "probe", "isolated_test", "internal"}:
            raise ValueError("unknown traffic kind")
        if self.status_code is not None and (type(self.status_code) is not int or
                                             not 100 <= self.status_code <= 599):
            raise ValueError("invalid terminal status")
        for name in ("terminal", "trusted_evidence", "redirect_allowed",
                     "maintenance_contract_exclusion", "maintenance_preapproved"):
            if type(getattr(self, name)) is not bool:
                raise ValueError("flags must be boolean")
        for name in ("authorized", "published_route"):
            if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
                raise ValueError("eligibility must be boolean or unknown")
        if self.duration_ms is not None:
            # Reuse numeric validation without confusing missing duration with 0.
            metrics.latency_p95([self.duration_ms])


@dataclass(frozen=True)
class RequestDecision:
    fact: RequestFact
    included: bool
    attributable_failure: bool | None
    reason: str


def classify_request(fact: RequestFact) -> RequestDecision:
    def decision(included, failed, reason):
        return RequestDecision(fact, included, failed, reason)
    if not fact.terminal:
        return decision(False, False, "upstream_attempt_not_terminal")
    if fact.traffic != "business" and fact.trusted_evidence:
        return decision(False, False, "trusted_" + fact.traffic)
    if (fact.trusted_evidence and fact.maintenance_contract_exclusion
            and fact.maintenance_preapproved):
        return decision(False, False, "contract_preapproved_maintenance")
    if (fact.trusted_evidence and fact.reason == EXCLUSIONS.get(fact.status_code)
            and fact.responsibility == "caller"):
        return decision(False, False, fact.reason)
    system_failure = (fact.trusted_evidence and fact.reason in SYSTEM_FAILURES
                      and fact.responsibility in {"platform", "provider"})
    # Forwarding/token/route failures must not be excluded merely because the
    # failed subsystem reported authorized=False or route=False.
    if not system_failure and (fact.authorized is not True or fact.published_route is not True):
        return decision(True, None, "unresolved_eligibility")
    if fact.source != "gateway":
        return decision(True, None, "missing_terminal_gateway_fact")
    if system_failure:
        return decision(True, True, fact.reason)
    code = fact.status_code
    if code is not None and (200 <= code < 300 or 300 <= code < 400 and fact.redirect_allowed):
        return decision(True, False, "valid_success")
    if (code is not None and code >= 500 and fact.trusted_evidence
            and fact.responsibility in {"platform", "provider"}):
        return decision(True, True, "attributable_server_error")
    return decision(True, None, "unresolved_failure")


def request_decisions(facts: Iterable[RequestFact]) -> tuple[RequestDecision, ...]:
    """One terminal gateway request per ID; resource copies/retries cannot inflate N.

    Conflicting copies within a source are rejected, not silently averaged or
    overwritten. Cross-source attempts remain in the source ledger, not here.
    """
    terminals = {}
    attempts = []
    for fact in facts:
        if not fact.terminal:
            attempts.append(classify_request(fact))
            continue
        key = (fact.request_id, fact.source)
        if key in terminals and terminals[key] != fact:
            raise ValueError("conflicting terminal request facts")
        terminals[key] = fact
    ids = sorted({key[0] for key in terminals})
    selected = [terminals.get((key, "gateway")) or terminals[(key, "resource")] for key in ids]
    return tuple(classify_request(fact) for fact in selected) + tuple(attempts)


@dataclass(frozen=True)
class RequestMetrics:
    error_rate: metrics.MetricResult
    latency_p95: metrics.MetricResult
    decisions: tuple[RequestDecision, ...]
    unresolved_requests: int
    source_available: bool


def aggregate_requests(facts: Iterable[RequestFact], *, source_available: bool,
                       tier="standard") -> RequestMetrics:
    _source_flag(source_available)
    decisions = request_decisions(facts)
    eligible = [item for item in decisions if item.included]
    unresolved = sum(item.attributable_failure is None for item in eligible)
    errors = metrics.request_error_rate(sum(item.attributable_failure is True for item in eligible),
                                       len(eligible), tier=tier)
    latency = metrics.latency_p95([item.fact.duration_ms for item in eligible], tier=tier)
    if unresolved or not source_available:
        # Unknown failures are not silently counted as successes, even with N>=100.
        errors = replace(errors, value=None, status="insufficient_data")
    if not source_available or any(item.reason in {"unresolved_eligibility", "missing_terminal_gateway_fact"}
                                   for item in eligible):
        latency = replace(latency, status="insufficient_data")
    return RequestMetrics(errors, latency, decisions, unresolved, source_available)


@dataclass(frozen=True)
class ProbeFact:
    slot: int
    status: str

    def __post_init__(self):
        if type(self.slot) is not int or self.slot < 0:
            raise ValueError("invalid probe slot")
        if self.status not in {"healthy", "failed", "claimed", "unknown"}:
            raise ValueError("unknown probe status")


def expected_probe_slots(start: datetime, end: datetime) -> range:
    """UTC-aligned 5-minute slots in actual eligible half-open service time."""
    for value in (start, end):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("aware service interval required")
    if end < start:
        raise ValueError("reversed service interval")
    return range(ceil(start.timestamp() / 300), ceil(end.timestamp() / 300))


def aggregate_probes(facts: Iterable[ProbeFact], *, expected_slots: Iterable[int],
                     source_available: bool, tier="standard") -> metrics.MetricResult:
    _source_flag(source_available)
    expected = set(expected_slots)
    if any(type(slot) is not int or slot < 0 for slot in expected):
        raise ValueError("invalid expected probe slot")
    rows = {}
    for fact in facts:
        if fact.slot not in expected:
            continue
        if fact.slot in rows and rows[fact.slot] != fact:
            raise ValueError("conflicting probe slot facts")
        rows[fact.slot] = fact
    valid = [fact for fact in rows.values() if fact.status in {"healthy", "failed"}]
    result = metrics.availability(sum(f.status == "healthy" for f in valid), len(valid),
                                  len(expected), tier=tier)
    return result if source_available else replace(result, status="insufficient_data")


def aggregate_deliveries(facts: Iterable[metrics.DeliveryFact], *, missing_frozen_facts: int,
                         source_available: bool, year: int, month: int, as_of: datetime,
                         tier="standard") -> metrics.MetricResult:
    """Use only frozen payment/deadline facts; never infer them from updated_at.

    Adapter counts missing facts in the scoped candidate set instead of dropping
    them to manufacture a successful denominator. Refunds do not exclude orders.
    """
    _source_flag(source_available)
    if type(missing_frozen_facts) is not int or missing_frozen_facts < 0:
        raise ValueError("invalid missing fact count")
    result = metrics.delivery_timeliness(facts, year=year, month=month, as_of=as_of, tier=tier)
    return result if source_available and not missing_frozen_facts else replace(result, status="insufficient_data")
