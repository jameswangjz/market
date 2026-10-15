"""Read-only bridge from persisted probe/gateway facts to frozen-order metrics.

No network, management-health fallback, source-window sealing or resolver side
effects. Runtime slot_query owns the order-to-entry eligibility mapping; API
entries can share facts, while SaaS identity remains contract-specific.
"""
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select

from .sla_online_facts import ProbeFact, aggregate_probes, expected_probe_slots


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def install(ns):
    def measure_order(db, order, contract, period_start, period_end, as_of):
        result = {'measurements': {}, 'source_available': {}, 'source_version': 'persisted-online-v1',
                  'evidence_digest': hashlib.sha256(b'[]').hexdigest()}
        if (not contract or not contract.verified_at or not contract.source_delivery_task_id
                or contract.delivery_method not in {'api', 'model_api', 'tenant_access'}):
            return result
        Term = ns.get('subscription_terms', {}).get('Term')
        if Term is None:
            return result
        terms = list(db.scalars(select(Term).where(Term.order_id == order.id)))
        # Revoked/revised service windows need historical eligibility evidence;
        # a current assignment/health check cannot invent that history.
        if len(terms) != 1 or terms[0].status != 'active':
            return result
        term = terms[0]
        if term.version_id != order.product_version_id:
            return result
        Revision = ns.get('subscription_terms', {}).get('Revision')
        if Revision is not None and db.scalar(select(Revision.id).where(Revision.term_id == term.id).limit(1)):
            return result
        start = max(utc(period_start), utc(contract.verified_at), utc(term.starts_at))
        end = min(utc(period_end), utc(as_of), utc(term.ends_at))
        if start >= end:
            result['not_started'] = utc(term.starts_at) >= min(utc(period_end), utc(as_of))
            return result
        evidence = []
        runtime = ns.get('sla_probe_runtime', {})
        slot_query = runtime.get('slot_query')
        if callable(slot_query):
            rows = list(db.scalars(slot_query(order_id=order.id, period_start=start, period_end=end)))
            rows = [row for row in rows if row.product_id == order.product_id and row.version_id == term.version_id
                    and row.scope == 'business_entry'
                    and (contract.delivery_method in {'api', 'model_api'}
                         or row.contract_evidence_id == contract.id)]
            def status(row):
                if row.status in {'healthy', 'failed'}:
                    proven = (row.completed_at is not None and row.publication_evidence_id
                        and row.eligibility_evidence_id and (row.source_evidence_id or row.reason_code == 'security_withdrawal'))
                    return row.status if proven else 'unknown'
                return 'claimed' if row.status == 'claimed' else 'unknown'
            facts = [ProbeFact(row.slot, status(row)) for row in rows]
            available = any(fact.status in {'healthy', 'failed'} for fact in facts)
            result['measurements']['availability'] = aggregate_probes(facts,
                expected_slots=expected_probe_slots(start, end), source_available=available)
            result['source_available']['availability'] = available
            evidence.extend(('probe', row.id, row.status, row.config_digest, row.reason_code, row.policy_version) for row in rows)
        gateway = ns.get('sla_gateway_events', {})
        aggregate, Model = gateway.get('aggregate'), gateway.get('Model')
        request_end = datetime.fromtimestamp(int(end.timestamp()) // 300 * 300, timezone.utc)
        if contract.delivery_method in {'api', 'model_api'}:
            result['request_source_cutoff'] = request_end.isoformat()
            result['source_available'].update(request_error_rate=False, latency_p95=False)
        if (callable(aggregate) and Model is not None and contract.delivery_method in {'api', 'model_api'}
                and start < request_end):
            Route = ns['ApiGatewayRoute']
            routes = list(db.scalars(select(Route).where(Route.product_id == order.product_id,
                                                        Route.version == term.version_code)))
            if len(routes) == 1:
                route = routes[0]
                # publish_apisix_route PUTs /routes/<route_key>; logger defaults
                # entry_id to this server-owned route ID. Read any explicit label
                # from the successful publication revision instead of guessing.
                Revision = ns['GatewayConfigRevision']
                revision = db.scalar(select(Revision).where(Revision.route_id == route.id,
                    Revision.status == 'active').order_by(Revision.revision.desc(), Revision.id).limit(1))
                if revision is not None:
                    try:
                        payload = json.loads(revision.config_json)
                        labels = payload.get('labels', {}) if isinstance(payload, dict) else {}
                    except (ValueError, TypeError):
                        labels = {}
                    if not isinstance(labels, dict):
                        labels = {}
                    entry = labels.get('market_entry_id') or route.route_key
                    if labels.get('market_product_id') == order.product_id and labels.get('market_version') == term.version_code:
                        rows = list(db.scalars(select(Model).where(Model.product_id == order.product_id,
                            Model.version_id == term.version_id, Model.entry_id == entry,
                            Model.enterprise_id == order.buyer_enterprise_id,
                            Model.completed_at >= start, Model.completed_at < request_end)))
                        measured = aggregate(db, product_id=order.product_id, version_id=term.version_id,
                            entry_id=entry, start=start, end=request_end, enterprise_id=order.buyer_enterprise_id,
                            source_available=bool(rows))
                        result['measurements'].update(request_error_rate=measured.error_rate,
                                                       latency_p95=measured.latency_p95)
                        result['source_available'].update(request_error_rate=measured.source_available,
                                                          latency_p95=measured.source_available)
                        evidence.extend(('gateway', row.event_id, row.content_sha256) for row in rows)
        result['evidence_digest'] = hashlib.sha256(json.dumps(sorted(evidence), separators=(',', ':')).encode()).hexdigest()
        return result

    hooks = {'measure_order': measure_order}
    ns['sla_online_sources'] = hooks
    return hooks
