"""Classify shared measurements against one order's detached frozen promise.

No current assignment, product tier, ORM, or default matrix is consulted. This
adapter does not create historical order snapshots or persist assessment rows.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from math import isfinite
import re

from .sla_metrics import MetricResult, Threshold


RULE_KEYS = ("matrix_version", "service_level_code", "calendar", "metrics", "legacy_level_config")
CONTRACTS = {
    "availability": ("percent", "lower", 100),
    "request_error_rate": ("percent", "upper", 100),
    "latency_p95": ("milliseconds", "upper", 100),
    "delivery_timeliness": ("percent", "lower", 10),
}


@dataclass(frozen=True)
class OrderAssessment:
    metric: str
    value: float | None
    status: str
    reason: str
    sample_count: int
    rule_version: str | None = None
    contract_rule_version: str | None = None
    frozen_tier: str | None = None
    target: float | None = None
    warning: float | None = None
    coverage_percent: float | None = None
    completeness_percent: float | None = None


def _number(value, maximum=None):
    try:
        return (not isinstance(value, bool) and isinstance(value, (float, int))
                and isfinite(value) and value >= 0 and (maximum is None or value <= maximum))
    except OverflowError:
        return False


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate snapshot field")
        result[key] = value
    return result


def _snapshot(value, enterprise_id, metric):
    if isinstance(value, str):
        value = json.loads(value, object_pairs_hook=_unique_object)
    # Round trip detaches caller-owned nested data and rejects non-JSON numbers.
    value = json.loads(_canonical(value))
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported snapshot")
    if not isinstance(enterprise_id, str) or not enterprise_id.strip() or value.get("enterprise_id") != enterprise_id:
        raise ValueError("snapshot enterprise mismatch")
    for key in ("rule_version", "contract_rule_version", "calendar_content_hash"):
        if not isinstance(value.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", value[key]):
            raise ValueError("missing snapshot version")
    rules = {key: value[key] for key in RULE_KEYS}
    if _hash(rules) != value["rule_version"] or _hash(rules["calendar"]) != value["calendar_content_hash"]:
        raise ValueError("snapshot content mismatch")
    if not all(isinstance(rules[key], str) and rules[key].strip()
               for key in ("matrix_version", "service_level_code")):
        raise ValueError("missing frozen policy identity")
    frozen_at = datetime.fromisoformat(value["frozen_at"].replace("Z", "+00:00"))
    if frozen_at.tzinfo is None or frozen_at.utcoffset() is None:
        raise ValueError("missing frozen timestamp timezone")
    unit, direction, _ = CONTRACTS[metric]
    rule = rules["metrics"][metric]
    if rule["unit"] != unit or rule["direction"] != direction or rule["clock"] != "natural":
        raise ValueError("incompatible frozen metric")
    maximum = 100 if unit == "percent" else None
    if not _number(rule["target"], maximum) or not _number(rule["warning"], maximum):
        raise ValueError("invalid frozen threshold")
    if unit == "milliseconds" and (rule["target"] == 0 or rule["warning"] == 0):
        raise ValueError("invalid latency threshold")
    bound = Threshold(rule["target"], rule["warning"], direction)
    return value, bound


def assess_order_metric(snapshot, metric: str, measurement: MetricResult | None, *,
                        buyer_enterprise_id: str, source_available: bool) -> OrderAssessment:
    """Reclassify values, not an earlier standard-tier compliance conclusion.

    The caller supplies the order's originally persisted snapshot, not a fresh
    enterprise assignment or a ticket created later. Explicit insufficient input
    remains insufficient; independent sample gates also reject forged `met`.
    Snapshot/sample errors produce a safe reason code, not a fabricated target.
    """
    observed = isinstance(measurement, MetricResult)
    value = measurement.value if observed and _number(measurement.value) else None
    count = measurement.sample_count if observed and type(measurement.sample_count) is int and measurement.sample_count >= 0 else 0
    result = dict(metric=metric, value=value, sample_count=count,
                  coverage_percent=measurement.coverage_percent if observed and _number(measurement.coverage_percent, 100) else None,
                  completeness_percent=measurement.completeness_percent if observed and _number(measurement.completeness_percent, 100) else None)

    def insufficient(reason):
        return OrderAssessment(**result, status="insufficient_data", reason=reason)

    if not isinstance(metric, str) or metric not in CONTRACTS:
        return insufficient("unsupported_metric")
    if snapshot is None or snapshot == "" or snapshot == {}:
        return insufficient("missing_snapshot")
    try:
        frozen, bound = _snapshot(snapshot, buyer_enterprise_id, metric)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return insufficient("invalid_snapshot")
    result.update(rule_version=frozen["rule_version"], contract_rule_version=frozen["contract_rule_version"],
                  frozen_tier=frozen["service_level_code"], target=bound.target, warning=bound.warning)
    if source_available is not True:
        return insufficient("source_missing")
    if (not observed or measurement.status not in {"met", "warning", "breached", "insufficient_data"}
            or value is None or type(measurement.sample_count) is not int or measurement.sample_count < 0
            or CONTRACTS[metric][0] == "percent" and not _number(value, 100)):
        return insufficient("invalid_measurement")
    if measurement.status == "insufficient_data":
        return insufficient("upstream_insufficient")
    if count < CONTRACTS[metric][2]:
        return insufficient("sample_count")
    if metric == "availability" and (result["coverage_percent"] is None or result["coverage_percent"] < 90):
        return insufficient("probe_coverage")
    if metric == "latency_p95" and (result["completeness_percent"] is None or result["completeness_percent"] < 95):
        return insufficient("duration_completeness")
    return OrderAssessment(**result, status=bound.classify(value, sufficient=True), reason="frozen_order_threshold")
