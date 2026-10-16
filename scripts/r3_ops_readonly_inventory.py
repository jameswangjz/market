"""OPS-002 inventory. Run in /app: python - --stdout < this_script.py.

No runtime/bootstrap imports, ORM sessions, flushes, or migration actions.
Only PostgreSQL is accepted in production; SQLite is for isolated tests.
"""
import argparse
import ast
from collections import Counter
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import sys

os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
sys.dont_write_bytecode = True

PROTECTED_ORDER = 'ORD-261012005144E1B3'
LEGACY_TYPES = ('report', 'application')
FINANCIAL = {
    'products': ('id', 'product_type', 'delivery_method', 'price', 'currency',
                 'settlement_rule_mode', 'settlement_rule_id', 'settlement_rule_json'),
    'orders': ('id', 'product_id', 'product_version_id', 'product_version_code',
               'main_status', 'payment_status', 'delivery_status', 'after_sales_status',
               'amount', 'paid_amount', 'refunded_amount', 'billing_cycle',
               'subscription_months', 'snapshot_version', 'unit_price_snapshot',
               'unit_cost_snapshot', 'total_cost_snapshot', 'delivery_method_snapshot',
               'economic_snapshot_json', 'delivery_snapshot_json'),
    'payments': ('id', 'order_id', 'payment_no', 'status', 'amount', 'paid_at'),
    'refunds': ('id', 'order_id', 'payment_id', 'refund_no', 'amount', 'status', 'completed_at'),
}
TICKET_FIELDS = ('id', 'order_id', 'status', 'previous_status', 'version',
                 'buyer_enterprise_id', 'provider_enterprise_id', 'created_by',
                 'sla_snapshot_json', 'evidence_status', 'first_response_at',
                 'submitted_resolution_at', 'closed_at')
STATUSES = set(('created unpaid paid partially_paid refunded partially_refunded pending '
                'confirmed failed completed cancelled canceled active closed resolved processing '
                'awaiting_response awaiting_buyer_info awaiting_buyer_confirmation '
                'platform_intervention withdrawn force_closed none not_started delivered '
                'delivering preparing pending_payment pending_delivery').split())


def canonical(value):
    def convert(item):
        if isinstance(item, Decimal):
            return format(item, 'f')
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError('unsupported fingerprint value')
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=convert,
                      ensure_ascii=True, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def current_types(app_dir):
    """Read the installed policy literal without importing any application code."""
    tree = ast.parse((Path(app_dir) / 'trading_policy.py').read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'product_delivery' for t in node.targets):
            policy = ast.literal_eval(node.value)
            if isinstance(policy, dict) and all(isinstance(k, str) for k in policy):
                return sorted(policy)
    raise ValueError('installed product policy unavailable')


def counts(rows, field, allowed):
    return dict(sorted(Counter(r[field] if r[field] in allowed else 'unknown'
                               for r in rows).items()))


def bundle(products, orders, payments, refunds):
    data = dict(products=products, orders=orders, payments=payments, refunds=refunds)
    return {key: {'count': len(rows), 'sha256': fingerprint(sorted(
        rows, key=canonical))} for key, rows in data.items()} | {
            'sha256': fingerprint({key: sorted(rows, key=canonical)
                                   for key, rows in data.items()})}


def inventory(connection, types):
    from sqlalchemy import MetaData, Table, select
    captured_at = datetime.now(timezone.utc).isoformat()
    metadata = MetaData()
    tables = {name: Table(name, metadata, autoload_with=connection, resolve_fks=False)
              for name in (*FINANCIAL, 'after_sales_tickets', 'after_sales_events')}

    def read(name, fields):
        table = tables[name]
        # Missing columns fail closed instead of silently weakening the evidence.
        return [dict(row) for row in connection.execute(
            select(*(table.c[field] for field in fields))).mappings()]

    data = {name: read(name, fields) for name, fields in FINANCIAL.items()}
    products, orders = data['products'], data['orders']
    payments, refunds = data['payments'], data['refunds']

    def linked(selected):
        ids = {p['id'] for p in selected}
        linked_orders = [o for o in orders if o['product_id'] in ids]
        order_ids = {o['id'] for o in linked_orders}
        return linked_orders, [p for p in payments if p['order_id'] in order_ids], [
            r for r in refunds if r['order_id'] in order_ids]

    candidates = []
    for kind in LEGACY_TYPES:
        selected = [p for p in products if p['product_type'] == kind]
        oo, pp, rr = linked(selected)
        candidates.append({'legacy_type': kind, 'product_count': len(selected),
                           'linked_order_count': len(oo),
                           'order_status': {f: counts(oo, f, STATUSES) for f in
                               ('main_status', 'payment_status', 'delivery_status', 'after_sales_status')},
                           'frozen_economics': bundle(selected, oo, pp, rr)})

    tickets = read('after_sales_tickets', TICKET_FIELDS)
    events = Counter(r['ticket_id'] for r in read('after_sales_events', ('ticket_id',)))
    evidence = []
    for ticket in tickets:
        reasons = []
        if ticket['status'] in ('resolved', 'closed'):
            reasons.append('legacy_status_requires_review')
        if ticket['evidence_status'] != 'recorded':
            reasons.append('historical_evidence_unknown')
        try:
            snapshot = json.loads(ticket['sla_snapshot_json'] or '{}')
        except (ValueError, TypeError):
            snapshot = None
        if not isinstance(snapshot, dict) or not snapshot:
            reasons.append('missing_or_invalid_sla_snapshot')
        for field in ('buyer_enterprise_id', 'provider_enterprise_id', 'created_by'):
            if not ticket[field]:
                reasons.append('missing_' + field)
        if not events[ticket['id']]:
            reasons.append('missing_business_events')
        if ticket['status'] == 'platform_intervention' and not ticket['previous_status']:
            reasons.append('missing_previous_status')
        for field, statuses in (
            ('first_response_at', {'processing', 'awaiting_buyer_info', 'awaiting_buyer_confirmation', 'completed'}),
            ('submitted_resolution_at', {'awaiting_buyer_confirmation', 'completed'}),
            ('closed_at', {'completed', 'withdrawn', 'force_closed', 'resolved', 'closed'})):
            if ticket['status'] in statuses and ticket[field] is None:
                reasons.append('missing_' + field)
        if reasons:
            evidence.append({'ticket_ref_sha256': fingerprint(ticket['id']),
                             'disposition': 'review_only', 'reasons': sorted(reasons)})

    order_table = tables['orders']
    protected_ids = set(connection.execute(select(order_table.c.id).where(
        order_table.c.order_no == PROTECTED_ORDER)).scalars())
    protected = [o for o in orders if o['id'] in protected_ids]
    product_ids = {o['product_id'] for o in protected}
    return {
        'schema_version': 1, 'task': 'OPS-002', 'database_mode': 'READ ONLY',
        'status': 'passed', 'captured_at': captured_at,
        'migration': {'requires_explicit_user_approval': True, 'approved': False,
                      'executed': False, 'zero_candidates_does_not_imply_approval': True},
        'type_source': 'installed app/trading_policy.py product_delivery; legacy backend boundary tests',
        'current_product_types': types,
        'products_by_type': counts(products, 'product_type', set(types) | set(LEGACY_TYPES)),
        'legacy_candidates': candidates,
        'all_frozen_economics': bundle(products, orders, payments, refunds),
        'tickets': {'count': len(tickets), 'by_status': counts(tickets, 'status', STATUSES),
                    'review_candidate_count': len(evidence),
                    'missing_evidence_review': sorted(evidence, key=canonical),
                    'policy': 'Review only; never invent timestamps, actors, or historical evidence.'},
        'protected_order': {'order_no': PROTECTED_ORDER, **bundle(
            [p for p in products if p['id'] in product_ids], protected,
            [p for p in payments if p['order_id'] in protected_ids],
            [r for r in refunds if r['order_id'] in protected_ids])},
        'fingerprint_policy': 'SHA256 canonical field allowlist, sorted rows; snapshots hashed verbatim, never disclosed.'}


def collect(engine, types, *, sqlite_test=False):
    with engine.connect() as connection:
        try:
            if connection.dialect.name == 'postgresql':
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            elif sqlite_test and connection.dialect.name == 'sqlite':
                connection.exec_driver_sql('PRAGMA query_only=ON')
                connection.exec_driver_sql('BEGIN')
            else:
                raise ValueError('PostgreSQL required')
            return inventory(connection, types)
        finally:
            connection.rollback()


def write_output(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    parser.add_argument('--stdout', action='store_true')
    args = parser.parse_args(argv)
    if not args.output and not args.stdout:
        parser.error('require --output and/or --stdout')
    try:
        from sqlalchemy import create_engine
        with redirect_stdout(io.StringIO()):
            types = current_types('/app/app')
            engine = create_engine(os.environ['DATABASE_URL'], echo=False)
            try:
                payload = canonical(collect(engine, types)) + '\n'
            finally:
                engine.dispose()
        if args.output:
            write_output(args.output, payload)
        if args.stdout:
            sys.stdout.write(payload)
        return 0
    except Exception:
        # Driver exceptions can contain credentials, SQL parameters, or private data.
        sys.stderr.write('Readonly inventory failed; no report produced.\n')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
