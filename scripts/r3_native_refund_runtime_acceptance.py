"""Offline ownership proofs for native refund acceptance; runtime unfinished.

No application import, network, manifest writes, or cleanup execution occurs in
this module. Main can integrate these checks into a reviewed runtime runner.
Neither run nor cleanup is enabled, even with reviewed=True.
"""
import re

import version_change_saga_runtime_acceptance as base

check = base.check
STREAM = 'market:apisix:charges:v1'
COVERAGE = 'market:apisix:charges:coverage:'
INTENT = 'market:apisix:refund:execution:v1:'
FINANCIAL_TABLES = frozenset({
    'refunds', 'ordinary_refund_cases', 'ordinary_refund_events',
    'ordinary_refund_executions', 'ordinary_refund_payment_legs',
    'ordinary_refund_local_completions', 'refund_resource_recoveries',
    'refund_funding_allocations',
})


def exact_id(value):
    check(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value),
          'Expected an exact generated fixture ID')
    return value


def stream_position(value):
    check(isinstance(value, str) and re.fullmatch(r'(0|[1-9]\d{0,19})-(0|[1-9]\d{0,19})', value),
          'Invalid stream position')
    parts = tuple(map(int, value.split('-')))
    check(all(part <= 18446744073709551615 for part in parts), 'Stream position overflow')
    return parts


def quota_token(value):
    return value.replace('%', '%25').replace(':', '%3A')


def extra_keys(*, combination_id, generation, period_index, refund_id):
    """Derive a whitelist, never accept arbitrary caller-provided Redis keys."""
    exact_id(combination_id)
    exact_id(refund_id)
    check(type(generation) is int and generation == 1 and
          type(period_index) is int and period_index == 0,
          'Baseline requires exactly the original one-month period')
    period = f'{combination_id}:{generation}:{period_index}'
    return frozenset({COVERAGE + quota_token(combination_id) + ':' + quota_token(period),
                      INTENT + refund_id})


def validate_charge(event_id, values, *, start, end, order_id, term_id,
                    combination_id, route_key, source_run_id):
    """Prove one journaled event belongs to an exact bounded fixture period."""
    for value in (order_id, term_id, combination_id):
        exact_id(value)
    check(isinstance(route_key, str) and re.fullmatch(r'qa-vcs-[0-9a-f]{12}-api-source', route_key),
          'Unexpected fixture route')
    check(isinstance(source_run_id, str) and re.fullmatch(r'[0-9a-f]{40}', source_run_id),
          'Invalid charge source epoch')
    check(stream_position(start) < stream_position(event_id) <= stream_position(end),
          'Charge is outside the journaled call window')
    expected = dict(order_id=order_id, segment_id=term_id, scope=combination_id,
                    period_key=combination_id + ':1:0', route_key=route_key,
                    source_run_id=source_run_id, quantity='1', policy='gateway_actual_charge_v1')
    check(isinstance(values, dict) and set(values) == set(expected) | {'charged_at_us'}
          and all(values[key] == value for key, value in expected.items())
          and isinstance(values['charged_at_us'], str)
          and re.fullmatch(r'[1-9]\d{0,15}', values['charged_at_us']),
          'Charge is not an exact owned native event')
    return event_id


def prove_fk_edges(owned, edges):
    """Check extracted FK edges against exact PK tuples, without DB access.

    edges entries: (child_table, child_pk, parent_table, parent_pk). The DB
    collector must enumerate every FK on every selected row, resolving full
    parent PKs. A null parent_pk denotes a nullable FK. This function does not
    establish root ownership or completeness; retain guarded.owned_rows and
    base.expand_fk_ids before collecting edges.
    """
    for child, child_pk, parent, parent_pk in edges:
        check(child_pk in owned.get(child, set()), 'FK edge has an unowned child')
        if parent_pk is not None:
            check(parent_pk in owned.get(parent, set()), 'FK edge reaches an unowned parent')


def validate_refund_audit(row, *, refund_id, order_id, buyer_id, operator_id):
    """Validate a refund audit exception before unchanged base guard delegation."""
    for value in (refund_id, order_id):
        exact_id(value)
    check(isinstance(row, dict) and row.get('target_type') == 'refund'
          and row.get('target_id') == refund_id and row.get('order_id') == order_id
          and row.get('category') == 'refund' and row.get('business_domain') == 'refund'
          and isinstance(row.get('action'), str) and row['action'].startswith('ordinary_refund_')
          and row.get('actor') in {buyer_id + '@example.invalid',
                                   operator_id + '@example.invalid', 'refund-worker'},
          'Refund audit does not have exact local ownership')


def validate_financial_tables(names):
    check(set(names) <= FINANCIAL_TABLES, 'Unknown financial child requires explicit review')


def run(manifest_path=None, *, reviewed=False):
    raise base.AcceptanceFailure(
        'Runtime implementation unfinished; no runtime activity performed')


def cleanup(*args, **kwargs):
    raise base.AcceptanceFailure(
        'Exact cleanup executor unfinished; no cleanup activity performed')
