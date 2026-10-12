"""Run monthly settlement HTTP against an exact snapshot of real ledger rows.

Input is a JSON manifest containing source_order_ids and change_ids from a
completed trade/version-change worker run. Rows are copied verbatim to a random
private PostgreSQL schema. No financial fixture values are synthesized.
"""
import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import stat
import traceback
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import MetaData, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema


def check(value, message):
    if not value:
        raise AssertionError(message)


def read_manifest(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0))
    with os.fdopen(fd, 'r') as stream:
        file_stat = os.fstat(stream.fileno())
        check(stat.S_ISREG(file_stat.st_mode) and stat.S_IMODE(file_stat.st_mode) == 0o600,
              'Monthly manifest must be a regular 0600 file')
        value = json.load(stream)
    check(set(value) == {'source_order_ids', 'change_ids', 'finance_user_id'},
          'Unexpected monthly manifest fields')
    match = re.fullmatch(r'(qa-vcs-[0-9a-f]{12})-u-finance', value.get('finance_user_id', ''))
    check(match is not None, 'Invalid worker finance ownership root')
    root = match.group(1)
    orders, changes = value['source_order_ids'], value['change_ids']
    check(isinstance(orders, list) and orders and len(set(orders)) == len(orders) and
          all(isinstance(item, str) for item in orders), 'Invalid source order list')
    check(isinstance(changes, list) and changes and len(set(changes)) == len(changes) and
          all(isinstance(item, str) for item in changes), 'Invalid change list')
    return value


def protected_fingerprint(engine):
    from sqlalchemy import Table
    meta = MetaData()
    orders = Table('orders', meta, schema='public', autoload_with=engine, resolve_fks=False)
    products = Table('products', meta, schema='public', autoload_with=engine, resolve_fks=False)
    with engine.connect() as conn:
        order = conn.execute(select(orders).where(
            orders.c.order_no == 'ORD-261012005144E1B3')).mappings().first()
        product = conn.execute(select(products).where(
            products.c.id == order['product_id'])).mappings().first() if order else None
    check(order is not None and product is not None, 'Protected public order or product is absent')
    return hashlib.sha256(json.dumps([dict(order) if order else None,
        dict(product) if product else None], sort_keys=True, default=str).encode()).hexdigest()


def snapshot(source, target, schema, manifest):
    """Copy known financial roots and FK parents without transforming values."""
    from app import main as m
    tables = dict(m.Base.metadata.tables)
    # Resolve required roots and financial dependents from live IDs.
    with source.connect() as src:
        check(src.scalar(text('select current_schema()')) == 'public',
              'Source snapshot connection does not resolve to public schema')
        orders = set(manifest['source_order_ids'])
        changes = set(manifest['change_ids'])
        requests = src.execute(select(tables['version_change_requests']).where(
            tables['version_change_requests'].c.id.in_(changes))).mappings().all()
        check(len(requests) == len(changes), 'One or more real version-change requests do not exist')
        check(all(row['source_order_id'] in orders for row in requests),
              'Selected changes do not belong to the listed source orders')
        orders.update(row['source_order_id'] for row in requests)
        orders.update(row['funding_order_id'] for row in requests if row['funding_order_id'])
        adjustments = src.execute(select(tables['version_change_adjustments']).where(
            tables['version_change_adjustments'].c.change_id.in_(changes))).mappings().all()
        check(adjustments and all(row['status'] == 'posted' and row['posted_at'] for row in adjustments),
              'Selected worker changes have no genuine posted adjustments')
        for row in adjustments:
            orders.add(row['source_order_id'])
        check(all(row['source_order_id'] in manifest['source_order_ids'] for row in adjustments),
              'Posted adjustments escape listed source order roots')
        payments = src.execute(select(tables['payments']).where(
            tables['payments'].c.order_id.in_(orders))).mappings().all()
        payment_ids = {row['id'] for row in payments}
        refunds = src.execute(select(tables['refunds']).where(
            tables['refunds'].c.payment_id.in_(payment_ids))).mappings().all()
        funds = src.execute(select(tables['version_change_funds']).where(
            tables['version_change_funds'].c.change_id.in_(changes))).mappings().all()
        related = {'orders': list(orders), 'payments': list(payment_ids),
                   'refunds': [row['id'] for row in refunds], 'changes': list(changes),
                   'funds': [row['id'] for row in funds],
                   'adjustments': [row['id'] for row in adjustments]}
        ids = {}
        def get(name, key, values):
            if not values:
                ids[name] = []
                return
            rows = src.execute(select(tables[name]).where(tables[name].c[key].in_(values))).mappings().all()
            ids[name] = [dict(row) for row in rows]
        get('orders', 'id', orders)
        get('payments', 'id', payment_ids)
        get('refunds', 'id', related['refunds'])
        get('version_change_requests', 'id', changes)
        get('version_change_funds', 'id', related['funds'])
        get('version_change_adjustments', 'id', related['adjustments'])
        check(len(ids['orders']) == len(orders), 'Referenced source/funding order missing')
        for root, col in [('users', 'buyer_user_id'), ('enterprises', 'buyer_enterprise_id'),
                          ('products', 'product_id')]:
            vals = {r[col] for r in ids['orders'] if r.get(col)}
            if root in tables and vals:
                get(root, 'id', vals)
        get('users', 'id', {manifest['finance_user_id']})
        check(len(ids['users']) == 1 and ids['users'][0]['id'] == manifest['finance_user_id'] and
              ids['users'][0].get('platform_role') == 'finance_settlement',
              'Manifest finance actor is missing or not an authorized finance root')
        root = re.fullmatch(r'(qa-vcs-[0-9a-f]{12})-u-finance',
                            manifest['finance_user_id']).group(1)
        check(all(row['product_id'].startswith(root + '-p-') for row in ids['orders']),
              'Copied source/funding order references a product outside the qa-vcs root')
        for row in ids['orders']:
            check(row['id'] in orders, 'Order snapshot identity mismatch')

        # Follow only referenced FK parents from the selected roots. Snapshot
        # each exact row once; this naturally includes the worker's combination,
        # product/version, enterprise, and user dependencies.
        copied = {name: {row[next(iter(tables[name].primary_key.columns)).name]: row
                         for row in values} for name, values in ids.items() if name in tables and values}
        pending = list(copied)
        while pending:
            child_name = pending.pop()
            child = tables[child_name]
            for fk in child.foreign_keys:
                parent = fk.column.table
                parent_name = parent.name
                value_set = {row.get(fk.parent.name) for row in copied[child_name].values()
                             if row.get(fk.parent.name) is not None}
                if not value_set:
                    continue
                if parent_name not in copied:
                    copied[parent_name] = {}
                missing = value_set - set(copied[parent_name])
                if missing:
                    parent_rows = src.execute(select(parent).where(
                        getattr(parent.c, fk.column.name).in_(missing))).mappings().all()
                    check(len(parent_rows) == len(missing), 'A referenced financial snapshot parent is missing')
                    pk_name = next(iter(parent.primary_key.columns)).name
                    copied[parent_name].update({row[pk_name]: dict(row) for row in parent_rows})
                    pending.append(parent_name)
        ids = {name: list(rows.values()) for name, rows in copied.items()}

    target_tables = MetaData(schema=schema)
    for name in tables:
        tables[name].to_metadata(target_tables, schema=schema)
    target_tables.create_all(target)
    # Parent tables precede their dependents. SQLAlchemy's FK sorter handles
    # the complete metadata graph while preserving every copied scalar value.
    with target.begin() as conn:
        for table in target_tables.sorted_tables:
            rows = ids.get(table.name, [])
            if rows:
                conn.execute(table.insert(), rows)
    return related, ids


def exercise(m):
    from fastapi.testclient import TestClient
    from sqlalchemy import func
    from sqlalchemy.orm import Session
    with m.SessionLocal() as db:
        check(db.scalar(text('select current_schema()')) == os.environ['QA_TRADE_SCHEMA'],
              'Application connection escaped private schema')
        user = db.get(m.User, os.environ['QA_TRADE_FINANCE_USER_ID'])
        check(user, 'Snapshot needs a referenced finance settlement user')
        token = m.issue_token(user)
        paid = db.scalars(select(m.Payment).join(m.Order).where(m.Payment.status == 'paid',
            m.Payment.paid_at.is_not(None), m.Order.business_type != 'version_change_payment')).all()
        posted = db.scalars(select(m.VersionChangeAdjustment).where(
            m.VersionChangeAdjustment.status == 'posted')).all()
        check(paid and posted, 'Snapshot lacks original paid payments or posted adjustments')
        from zoneinfo import ZoneInfo
        shanghai = ZoneInfo('Asia/Shanghai')
        all_times = [((t.replace(tzinfo=__import__('datetime').timezone.utc)
                       if t.tzinfo is None else t).astimezone(shanghai))
                     for t in [p.paid_at for p in paid] + [a.posted_at for a in posted]]
        # Convert source instants to business timezone before grouping months.
        months = {(t.year, t.month) for t in all_times}
        check(len(months) == 1, 'Selected actual payments and adjustments span months; stage one month per run')
        year, month = next(iter(months))
        start = datetime(year, month, 1)
        end = datetime(year + (month == 12), month % 12 + 1, 1)
        start = start.replace(tzinfo=shanghai)
        end = end.replace(tzinfo=shanghai)
        originals = db.scalars(select(m.Order).join(m.Payment).where(
            m.Payment.status == 'paid', m.Payment.paid_at.is_not(None),
            m.Order.business_type != 'version_change_payment')).all()
        selected_orders = set(os.environ['QA_TRADE_SOURCE_ORDER_IDS'].split(','))
        originals = [row for row in originals if row.id in selected_orders]
        check(originals, 'No selected paid original order exists in snapshot')
        source_total = sum((Decimal(str(row.paid_amount or row.amount or 0)) for row in originals), Decimal('0'))
        adjustment_total = sum((Decimal(str(a.revenue)) for a in posted), Decimal('0'))
        expected_cost = sum((Decimal(str(row.total_cost_snapshot or 0)) for row in originals), Decimal('0')) + sum(
            (Decimal(str(a.cost)) for a in posted), Decimal('0'))
    client = TestClient(m.app)
    headers = {'Authorization': 'Bearer ' + token}
    try:
        body = {'cycle': 'monthly', 'period_start': start.isoformat(), 'period_end': end.isoformat(),
                'idempotency_key': 'trade-monthly-' + secrets.token_hex(8)}
        response = client.post('/api/settlement-batches', json=body, headers=headers)
        check(response.status_code == 200, 'Monthly production HTTP did not accept the actual ledger snapshot')
        first = response.json()
        check(Decimal(str(first['total_amount'])) == source_total + adjustment_total,
              'Settled gross does not equal actual paid originals plus posted adjustment revenue')
        with m.SessionLocal() as db:
            selected_adjustments = db.scalars(select(m.VersionChangeAdjustment).where(
                m.VersionChangeAdjustment.change_id.in_(os.environ['QA_TRADE_CHANGE_IDS'].split(',')),
                m.VersionChangeAdjustment.status == 'posted')).all()
            check(len(selected_adjustments) == len(posted) and
                  {row.id for row in selected_adjustments} == {row.id for row in posted},
                  'Posted adjustment snapshot differs from the selected worker changes')
            linked_ids = set(db.scalars(select(m.VersionChangeSettlementLink.adjustment_id).join(
                m.Settlement, m.Settlement.id == m.VersionChangeSettlementLink.settlement_id).join(
                m.SettlementLine, m.SettlementLine.settlement_id == m.Settlement.id).where(
                    m.SettlementLine.batch_id == first['id'])))
            check(linked_ids == {row.id for row in posted},
                  'Monthly batch does not link every selected posted adjustment exactly')
            funds = db.scalars(select(m.VersionChangeFund).where(
                m.VersionChangeFund.change_id.in_(os.environ['QA_TRADE_CHANGE_IDS'].split(',')))).all()
            funding_order_ids = {db.get(m.Payment, row.payment_id).order_id for row in funds
                                 if row.kind == 'payment' and row.payment_id}
            check(not funding_order_ids or not db.scalars(select(m.Settlement.id).where(
                m.Settlement.order_id.in_(funding_order_ids))).first(),
                  'Version-change funding payment was settled as an original order')
            change_refunds = {row.refund_id for row in funds if row.kind == 'refund' and row.refund_id}
            check(not change_refunds or not db.scalars(select(m.Settlement.id).where(
                m.Settlement.is_refund.is_(True), m.Settlement.refund_id.in_(change_refunds))).first(),
                'Version-change refund was counted as an ordinary refund')
            lines = db.scalars(select(m.SettlementLine).where(m.SettlementLine.batch_id == first['id'])).all()
            check(len(lines) % 5 == 0, 'Monthly settlements do not have five participant lines')
            for sid in {line.settlement_id for line in lines}:
                group = [line.amount for line in lines if line.settlement_id == sid]
                check(len(group) == 5, 'Settlement does not contain exactly five participant shares')
                check(sum(group, Decimal('0')) == db.get(m.Settlement, sid).profit_amount,
                      'Five-party amounts do not reconcile to actual ledger profit')
            settled = db.scalars(select(m.Settlement).join(m.SettlementLine).where(
                m.SettlementLine.batch_id == first['id']).distinct()).all()
            check(sum((Decimal(str(row.cost_amount)) for row in settled), Decimal('0')) == expected_cost,
                  'Settled cost does not match copied order and adjustment ledger costs')
        replay = client.post('/api/settlement-batches', json=body, headers=headers)
        check(replay.status_code == 200 and replay.json()['id'] == first['id'], 'Replay was not idempotent')
        report_end = end.date().replace(day=1)
        from datetime import timedelta
        report_end = report_end - timedelta(days=1)
        reports = client.get('/api/settlement-reports', params={
            'start': start.date().isoformat(), 'end': report_end.isoformat()}, headers=headers)
        check(reports.status_code == 200, 'Monthly reports endpoint failed')
        report = reports.json()
        check(Decimal(str(report['summary']['gross_amount'])) == source_total + adjustment_total and
              Decimal(str(report['summary']['cost_amount'])) == expected_cost and
              len(report['monthly']) == 1, 'Month-filtered report does not reconcile to source ledger')
        exported = client.get('/api/settlement-reports/export', params={
            'start': start.date().isoformat(), 'end': report_end.isoformat(), 'kind': 'summary'}, headers=headers)
        check(exported.status_code == 200 and bool(exported.content), 'Monthly report export failed')
        rebuild_body = dict(body, idempotency_key='trade-monthly-rebuild-' + secrets.token_hex(8), rebuild=True)
        rebuilt = client.post('/api/settlement-batches', json=rebuild_body, headers=headers)
        check(rebuilt.status_code == 200 and rebuilt.json()['total_amount'] == first['total_amount'],
              'Rebuild changed actual monetary totals')
        with m.SessionLocal() as db:
            active = db.scalars(select(m.SettlementLine.settlement_id).where(
                m.SettlementLine.batch_id == rebuilt.json()['id'],
                m.SettlementLine.status != 'superseded').distinct()).all()
        for settlement_id in active:
            locked = client.post(f'/api/settlements/{settlement_id}/lock', headers=headers)
            check(locked.status_code == 200, 'Could not lock rebuilt settlement')
        confirmed = client.post(f'/api/settlement-batches/{rebuilt.json()["id"]}/confirm',
                                json={}, headers=headers)
        check(confirmed.status_code == 200, 'Could not confirm rebuilt batch')
        immutable = client.post('/api/settlement-batches', json=dict(rebuild_body,
            idempotency_key='trade-monthly-after-confirm-' + secrets.token_hex(8)), headers=headers)
        check(immutable.status_code == 409, 'Confirmed monthly batch was mutable')
        return {'batch_id': first['id'], 'original_paid': str(source_total),
                'posted_adjustment_net': str(adjustment_total), 'settled_gross': str(first['total_amount']),
                'checks': ['source and posted adjustment totals', 'funding/refund exclusion',
                           'exact adjustment links', 'ledger cost and five-party reconciliation',
                           'month-filtered report/export', 'replay/rebuild/confirmed immutability']}
    finally:
        client.close()


def worker():
    # Run this file in a fresh interpreter so the private search_path is set
    # before importing backend.app.main and its application engine.
    if os.environ.get('QA_TRADE_CHILD') != '1':
        env = dict(os.environ, QA_TRADE_CHILD='1')
        result = subprocess.run([sys.executable, __file__, '--child', *sys.argv[1:]],
                                env=env, check=False, capture_output=True, text=True)
        if result.stdout:
            print(result.stdout, end='')
        if result.returncode:
            raise SystemExit(result.returncode)
        return
    args = argparse.ArgumentParser()
    args.add_argument('--manifest', required=True)
    args.add_argument('--child', action='store_true')
    options = args.parse_args()
    manifest = read_manifest(options.manifest)
    url = make_url(os.environ['DATABASE_URL'])
    check(url.get_backend_name() == 'postgresql', 'PostgreSQL required')
    admin = create_engine(url, pool_pre_ping=True)
    schema = 'qa_trade_monthly_' + secrets.token_hex(8)
    created = False
    app_module = None
    before_fingerprint = None
    evidence = None
    try:
        before_fingerprint = protected_fingerprint(admin)
        with admin.begin() as conn:
            conn.execute(CreateSchema(schema))
        created = True
        query = dict(url.query)
        query['options'] = str(query.get('options', '')) + ' -csearch_path=' + schema
        os.environ['DATABASE_URL'] = url.set(query=query).render_as_string(hide_password=False)
        os.environ['QA_TRADE_SCHEMA'] = schema
        os.environ['QA_TRADE_FINANCE_USER_ID'] = manifest['finance_user_id']
        os.environ['QA_TRADE_SOURCE_ORDER_IDS'] = ','.join(manifest['source_order_ids'])
        os.environ['QA_TRADE_CHANGE_IDS'] = ','.join(manifest['change_ids'])
        from app import main as app_module
        with app_module.engine.begin() as conn:
            app_module.Base.metadata.create_all(conn)
        related, rows = snapshot(admin, app_module.engine, schema, manifest)
        result = exercise(app_module)
        evidence = {'status': 'passed', 'scope': 'actual trade worker ledger snapshot in private PostgreSQL',
            'source_rows': {k: len(v) for k, v in rows.items()}, 'selected': related, 'result': result,
            'cleanup': 'only this process-created schema dropped',
            'not_tested': ['live trade HTTP/APISIX execution in this run', 'bank transfers',
                           'cross-month batches', 'remote deployment']}
    finally:
        if app_module:
            app_module.engine.dispose()
        if created:
            with admin.begin() as conn:
                conn.execute(DropSchema(schema, cascade=True))
            with admin.connect() as conn:
                check(conn.scalar(text('select count(*) from information_schema.schemata '
                    'where schema_name=:schema'), {'schema': schema}) == 0,
                    'Private schema still exists after cleanup')
        if before_fingerprint is not None:
            check(protected_fingerprint(admin) == before_fingerprint,
                  'Protected public order or dependent product changed')
        admin.dispose()
    check(evidence is not None, 'Monthly acceptance produced no evidence')
    print(json.dumps(evidence, sort_keys=True))


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        worker()
    except AssertionError as exc:
        frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        print(json.dumps({'status': 'failed', 'check': str(exc),
                          'source': Path(frame.filename).name if frame else 'unknown',
                          'line': frame.lineno if frame else 0}))
        raise SystemExit(1)
    except Exception as exc:
        frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        print(json.dumps({'status': 'failed', 'location': 'runner',
                          'error_type': type(exc).__name__,
                          'source': Path(frame.filename).name if frame else 'unknown',
                          'line': frame.lineno if frame else 0}))
        raise SystemExit(1)
