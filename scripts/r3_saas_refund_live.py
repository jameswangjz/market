"""Isolated actual-app refund with deployed SaaS/OAuth HTTP, not public ingress.

Run only after main review, in a fresh installed-app interpreter. No transport,
deployment, startup, global worker controls, mail, or monthly settlement batch.
Only three journaled public machine-auth roots bridge private business data to
the deployed platform token/introspection endpoints. Remote tenants are closed
and retained, never described as purged. A 0600 recovery journal is always kept.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from unittest.mock import patch

import version_change_saas_runtime_acceptance as base

class AcceptanceFailure(base.AcceptanceFailure):
    pass


def check(condition, label):
    if not condition:
        raise AcceptanceFailure(re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')[:96])


def safe_error(exc):
    tb = exc.__traceback__
    while tb and tb.tb_next:
        tb = tb.tb_next
    result = dict(error_type=type(exc).__name__, line=tb.tb_lineno if tb else None)
    if isinstance(exc, AcceptanceFailure) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', str(exc)):
        result['label'] = str(exc)
    return result


def normalized(value):
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def protected_snapshot(control):
    """Read only the protected order, its product and its payment/refund rows."""
    from sqlalchemy import MetaData, Table, or_, select
    metadata = MetaData()
    tables = {name: Table(name, metadata, schema='public', autoload_with=control,
                          resolve_fks=False)
              for name in ('orders', 'products', 'payments', 'refunds')}
    with control.connect() as conn:
        conn.exec_driver_sql('SET TRANSACTION READ ONLY')
        table = tables['orders']
        orders = list(conn.execute(select(table).where(or_(
            table.c.id == base.PROTECTED_ORDER, table.c.order_no == base.PROTECTED_ORDER))).mappings())
        check(len(orders) == 1, 'Protected order count differs from one')
        order = dict(orders[0])
        rows = {'orders': [order]}
        rows['products'] = [dict(r) for r in conn.execute(select(tables['products']).where(
            tables['products'].c.id == order['product_id'])).mappings()]
        check(len(rows['products']) == 1, 'Protected product missing')
        for name in ('payments', 'refunds'):
            rows[name] = sorted((dict(r) for r in conn.execute(select(tables[name]).where(
                tables[name].c.order_id == order['id'])).mappings()), key=lambda r: r['id'])
    return dict(count=1, sha256=hashlib.sha256(json.dumps(
        normalized(rows), sort_keys=True).encode()).hexdigest())


def assert_private(m, schema, role):
    from sqlalchemy import text
    import r3_backend_acceptance_pg as pg
    check(pg.owned_schema(schema) and role == schema + '_role', 'Invalid private ownership marker')
    with m.engine.connect() as conn:
        check(conn.scalar(text('SHOW search_path')) == schema
              and conn.scalar(text('SELECT current_schema()')) == schema
              and conn.scalar(text('SELECT current_user')) == role, 'Private database escaped isolation')
        check(conn.scalar(text('SELECT count(*) FROM pg_roles WHERE rolname=current_user '
              'AND NOT rolsuper AND NOT rolcreaterole AND NOT rolcreatedb AND NOT rolbypassrls')) == 1,
              'Private role has administrative powers')
        check(conn.scalar(text("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND ("
              "has_table_privilege(current_user, quote_ident(schemaname)||'.'||quote_ident(tablename), 'INSERT') OR "
              "has_table_privilege(current_user, quote_ident(schemaname)||'.'||quote_ident(tablename), 'UPDATE') OR "
              "has_table_privilege(current_user, quote_ident(schemaname)||'.'||quote_ident(tablename), 'DELETE') OR "
              "has_table_privilege(current_user, quote_ident(schemaname)||'.'||quote_ident(tablename), 'TRUNCATE'))")) == 0,
              'Private role can mutate public tables')
        check(not conn.scalar(text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")),
              'Private role can create public objects')


def bridge_tables(m):
    from sqlalchemy import MetaData
    metadata = MetaData()
    return {model.__tablename__: model.__table__.to_metadata(metadata, schema='public')
            for model in (m.Enterprise, m.Product, m.OAuthClient)}


def bridge_expected(m, ids):
    from sqlalchemy import select
    with m.SessionLocal() as db:
        enterprise = db.get(m.Enterprise, ids['enterprises']['provider'])
        product = db.get(m.Product, ids['product'])
        oauth = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == product.id))
        rows = {row.__tablename__: {c.name: getattr(row, c.name) for c in row.__table__.columns}
                for row in (enterprise, product, oauth)}
    rows[m.Product.__tablename__]['status'] = 'draft'
    return rows


def create_bridge(m, control, ids, journal, save):
    from sqlalchemy import select
    tables = bridge_tables(m)
    rows = bridge_expected(m, ids)
    # The exact complete rows, including credentials, stay in the private journal.
    journal['public_roots'] = normalized(rows)
    journal['public_phase'] = 'create_requested'
    save()
    check(protected_snapshot(control) == journal['protected'], 'Protected snapshot changed')
    with control.begin() as conn:
        for name, row in rows.items():
            table = tables[name]
            check(conn.execute(select(table.c.id).where(table.c.id == row['id'])).first() is None,
                  'Public bridge identity collision; no cleanup authorized')
            if 'client_id' in table.c:
                check(conn.execute(select(table.c.id).where(table.c.client_id == row['client_id'])).first() is None,
                      'Public OAuth identity collision; no cleanup authorized')
        journal['public_phase'] = 'collision_checked'
        save()
        for name, row in rows.items():
            conn.execute(tables[name].insert().values(**row))
    journal['public_phase'] = 'created'
    save()


def validate_bridge_rows(actual, expected):
    check(set(actual) == set(expected), 'Public bridge roots incomplete')
    for name, row in expected.items():
        check(normalized(actual[name]) == row, 'Public bridge root changed; retain for review')
    check(expected['products']['status'] == 'draft', 'Public bridge product is not draft')


def delete_bridge(m, control, journal, save):
    """Exact PK deletion only, after full-row and inbound-FK checks."""
    from sqlalchemy import MetaData, Table, and_, inspect, select
    if journal['public_phase'] in ('not_created', 'create_requested', 'removed'):
        return
    tables, expected = bridge_tables(m), journal['public_roots']
    check(protected_snapshot(control) == journal['protected'], 'Protected snapshot changed')
    with control.begin() as conn:
        actual = {}
        for name, row in expected.items():
            found = conn.execute(select(tables[name]).where(
                tables[name].c.id == row['id']).with_for_update()).mappings().first()
            if found is not None:
                actual[name] = dict(found)
        # A transaction that never committed has no roots to remove.
        if not actual and journal['public_phase'] in ('collision_checked', 'delete_requested'):
            journal['public_phase'] = 'removed'
            save()
            return
        validate_bridge_rows(actual, expected)
        inspector = inspect(conn)
        for child_name in inspector.get_table_names(schema='public'):
            for fk in inspector.get_foreign_keys(child_name, schema='public'):
                parent_name = fk['referred_table']
                if parent_name not in expected or fk.get('referred_schema') not in (None, 'public'):
                    continue
                child = Table(child_name, MetaData(), schema='public', autoload_with=conn, resolve_fks=False)
                condition = and_(*(child.c[c] == actual[parent_name][p] for c, p in
                    zip(fk['constrained_columns'], fk['referred_columns'])))
                children = list(conn.execute(select(child).where(condition).with_for_update()).mappings())
                check(all(child_name in expected and normalized(dict(r)) == expected[child_name]
                          for r in children), 'Unjournaled public child; cleanup refused')
        journal['public_phase'] = 'delete_requested'
        save()
        for model in (m.OAuthClient, m.Product, m.Enterprise):
            name = model.__tablename__
            result = conn.execute(tables[name].delete().where(tables[name].c.id == expected[name]['id']))
            check(result.rowcount == 1, 'Exact public root deletion failed')
    journal['public_phase'] = 'removed'
    save()
    check(protected_snapshot(control) == journal['protected'], 'Protected snapshot changed')


def aware(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    check(result.tzinfo is not None, 'Expiry must have a timezone')
    return result.astimezone(timezone.utc)


def verify_status(status, plan, *, shortened):
    check(isinstance(status, dict) and type(status.get('ret')) is int
          and status['ret'] == 0, 'SaaS STATUS failed')
    check(all(status.get(k) == plan[k] for k in
              ('tenant_id', 'product_id', 'enterprise_id', 'subscription_id')),
          'SaaS STATUS identity mismatch')
    check(status.get('refund_shorten_supported') is True
          and status.get('refund_shorten_protocol') == 'refund_shorten_v1',
          'SaaS shortening capability missing')
    key = 'expires_at' if shortened else 'expected_expires_at'
    check(aware(status['expires_at']) == aware(plan[key]), 'SaaS expiry mismatch')
    if shortened:
        check(status.get('refund_id') == plan['refund_id'], 'SaaS refund receipt mismatch')
    check(aware(plan['expires_at']) < aware(plan['expected_expires_at']),
          'Refund did not shorten the frozen tail')


@contextmanager
def owned_journal(m, ids):
    """Hold only newly created fixture tasks/executions, never shared workers."""
    from sqlalchemy import event
    Execution = m.refund_execution['Execution']

    def journal(db, *_):
        for row in db.new.union(db.dirty):
            if isinstance(row, m.Order) and row.product_id == ids['product']:
                check(row.buyer_enterprise_id == ids['enterprises']['buyer']
                      and row.buyer_user_id == ids['users']['buyer'], 'Foreign fixture order')
                if row.id and row.id not in ids['database_ids']['orders']:
                    ids['database_ids']['orders'].append(row.id)
            if isinstance(row, m.Refund) and row.order_id in ids['database_ids']['orders']:
                if row.id and row.id not in ids['database_ids']['refunds']:
                    ids['database_ids']['refunds'].append(row.id)
        base.path_write(ids)

    def hold(db, *_):
        for row in db.new.union(db.dirty):
            if (isinstance(row, m.DeliveryTask)
                    and row.order_id in ids['database_ids']['orders']
                    and row.status in {'in_delivery', 'retrying'}):
                row.next_retry_at = m.now() + timedelta(hours=1)
            if isinstance(row, Execution) and row.refund_id in ids['database_ids']['refunds']:
                row.lease_expires_at = m.now() + timedelta(hours=1)

    event.listen(m.SessionLocal.class_, 'after_flush', journal)
    event.listen(m.SessionLocal.class_, 'before_flush', hold)
    try:
        yield
    finally:
        event.remove(m.SessionLocal.class_, 'before_flush', hold)
        event.remove(m.SessionLocal.class_, 'after_flush', journal)


def purchase(m, ids, path, client, tokens):
    from sqlalchemy import select
    oid = base.request(client, tokens['buyer'], 'POST', '/orders', body=dict(
        product_id=ids['product'], product_version_id=ids['versions']['source'],
        buyer_enterprise_id=ids['enterprises']['buyer'], subscription_months=1))['id']
    check(oid in ids['database_ids']['orders'], 'Purchase was not journaled')
    with m.SessionLocal() as db:
        order = db.get(m.Order, oid)
        check(bool(order.subscription_id), 'Purchase has no subscription')
        previous = ids['database_ids']['subscription_id']
        check(previous in (None, order.subscription_id), 'Renewal changed subscription')
        ids['database_ids']['subscription_id'] = order.subscription_id
        ids['external']['tenant'] = 'tenant-' + order.subscription_id[:12]
    base.path_write(ids)
    base.request(client, tokens['finance'], 'POST', f'/orders/{oid}/transition', body=dict(
        action='confirm_payment', reason='Owned SaaS refund fixture payment'))
    with m.SessionLocal() as db:
        tasks = db.scalars(select(m.DeliveryTask).where(m.DeliveryTask.order_id == oid)).all()
        check(len(tasks) == 1, 'Purchase requires one delivery task')
        task_id = tasks[0].id
    base.deliver_staged(m, ids, path, client, tokens, task_id)
    return oid


def remote_status(m, ids, plan=None):
    from sqlalchemy import select
    with m.SessionLocal() as db:
        sub = db.get(m.SaaSSubscription, ids['database_ids']['subscription_id'])
        check(sub is not None and sub.product_id == ids['product']
              and sub.enterprise_id == ids['enterprises']['buyer']
              and sub.external_tenant_id == ids['external']['tenant'], 'Subscription ownership mismatch')
        config = db.scalar(select(m.SaaSIntegrationConfig).where(
            m.SaaSIntegrationConfig.product_id == ids['product']))
        check(config is not None and config.auth_mode == 'oauth2'
              and config.token_url == m.platform_oauth_token_url(), 'Real platform OAuth required')
        identity = dict(tenant_id=sub.external_tenant_id, product_id=sub.product_id,
                        enterprise_id=sub.enterprise_id, subscription_id=sub.id)
        code, status = base.mock_saas_status(m, config, identity)
        check(code == 200 and isinstance(status, dict) and status.get('ret') == 0
              and all(status.get(k) == v for k, v in identity.items()), 'Remote tenant not owned')
        check(aware(status['expires_at']) == aware(sub.expires_at), 'Local/remote expiry mismatch')
        if plan is not None:
            verify_status(status, plan, shortened=True)
        return status


def collect(m, ids):
    """Read-only exact refund evidence, not a deletion authorization graph."""
    from sqlalchemy import select
    result = {}
    with m.SessionLocal() as db:
        orders = db.scalars(select(m.Order).where(m.Order.product_id == ids['product'])).all()
        check({r.id for r in orders} == set(ids['database_ids']['orders']), 'Order journal mismatch')
        check(all(r.buyer_enterprise_id == ids['enterprises']['buyer']
                  and r.provider_enterprise_id == ids['enterprises']['provider']
                  and r.buyer_user_id == ids['users']['buyer'] for r in orders), 'Foreign order')
        refunds = db.scalars(select(m.Refund).where(
            m.Refund.order_id.in_(ids['database_ids']['orders']))).all()
        check({r.id for r in refunds} == set(ids['database_ids']['refunds']), 'Refund journal mismatch')
        models = [m.Refund, m.refund_workflow['Case'],
                  *(m.refund_execution[k] for k in ('Execution', 'PaymentLeg', 'Completion')),
                  m.refund_execution_resources['Recovery']]
        for model in models:
            column = model.id if model is m.Refund else model.refund_id
            rows = db.scalars(select(model).where(column.in_(ids['database_ids']['refunds']))).all()
            result[model.__tablename__] = sorted([
                (tuple(str(getattr(row, c.name)) for c in model.__table__.primary_key.columns),
                 hashlib.sha256(json.dumps(normalized({c.name: getattr(row, c.name)
                    for c in model.__table__.columns}), sort_keys=True).encode()).hexdigest())
                for row in rows])
    return result


def blocked_earlier_refund(m, ids, client, tokens, oid):
    """The same tenant's unrefunded renewal must block its earlier order."""
    from sqlalchemy import select
    scope = '?enterprise_id=' + ids['enterprises']['buyer'] + '&business_view=buyer'
    before = remote_status(m, ids)
    quote = base.request(client, tokens['buyer'], 'POST', f'/orders/{oid}/refund-quote' + scope,
                         body=dict(amount='50.00'))
    check(quote.get('status') == 'available', 'Earlier economic quote unavailable')
    row = base.request(client, tokens['buyer'], 'POST', f'/orders/{oid}/refund-requests' + scope,
        body=dict(amount='50.00', reason='Owned earlier refund blocking case',
                  idempotency_key=ids['run_id'] + '-earlier-request'))
    rid = row['id']
    check(rid in ids['database_ids']['refunds'], 'Earlier refund was not journaled')
    row = base.request(client, tokens['delivery'], 'POST', f'/refunds/{rid}/review', body=dict(
        decision='approve', reason='Owned earlier approval', expected_version=row['version'],
        idempotency_key=ids['run_id'] + '-earlier-approve'))
    response = client.post(f'/api/refunds/{rid}/execute',
        headers={'Authorization': 'Bearer ' + tokens['delivery']}, json=dict(
            expected_version=row['version'], reason='Must reject earlier tail with unrefunded renewal',
            idempotency_key=ids['run_id'] + '-earlier-execute'))
    reason = ('\u8bf7\u5148\u5b8c\u6210\u540e\u7eed\u7eed\u8d39\u8ba2\u5355\u7684\u9000\u6b3e'
              '\uff0c\u518d\u5bf9\u5f53\u524d\u8ba2\u5355\u9000\u6b3e')
    check(response.status_code == 409 and response.json().get('detail') == reason,
          'Earlier refund did not reject unrefunded renewal')
    with m.SessionLocal() as db:
        for model in (m.refund_execution['Execution'], m.refund_execution['PaymentLeg'],
                      m.refund_execution['Completion'], m.refund_execution_resources['Recovery']):
            check(db.scalar(select(model).where(model.refund_id == rid)) is None,
                  'Blocked earlier refund created execution effects')
        check(db.get(m.Refund, rid).status == 'approved'
              and Decimal(str(db.get(m.Order, oid).refunded_amount)) == 0,
              'Blocked earlier refund changed funds')
    after = remote_status(m, ids)
    check(all(after.get(k) == before.get(k) for k in (
        'tenant_id', 'product_id', 'enterprise_id', 'subscription_id', 'expires_at', 'refund_id', 'status')),
        'Blocked earlier refund changed tenant')
    return dict(status='passed', refund_id=rid, unrefunded_renewal_blocked=True, no_execution_effects=True)


def exercise_refund(m, ids, client, tokens, oid):
    from sqlalchemy import select
    from r3_native_refund_flow import scoped_sessions
    scope = '?enterprise_id=' + ids['enterprises']['buyer'] + '&business_view=buyer'
    amount = '50.00'
    quote = base.request(client, tokens['buyer'], 'POST', f'/orders/{oid}/refund-quote' + scope,
                         body=dict(amount=amount))
    check(quote.get('status') == 'available', 'SaaS refund quote unavailable')
    body = dict(amount=amount, reason='Owned HTTP SaaS refund',
                idempotency_key=ids['run_id'] + '-refund')
    row = base.request(client, tokens['buyer'], 'POST', f'/orders/{oid}/refund-requests' + scope, body=body)
    rid = row['id']
    check(rid in ids['database_ids']['refunds'], 'Refund was not journaled')
    check(base.request(client, tokens['buyer'], 'POST', f'/orders/{oid}/refund-requests' + scope,
                       body=body)['id'] == rid, 'Request replay changed refund')
    row = base.request(client, tokens['delivery'], 'POST', f'/refunds/{rid}/review', body=dict(
        decision='approve', reason='Owned SaaS approval', expected_version=row['version'],
        idempotency_key=ids['run_id'] + '-approve'))
    execute = dict(expected_version=row['version'], reason='Owned simulated funds',
                   idempotency_key=ids['run_id'] + '-execute')
    base.request(client, tokens['delivery'], 'POST', f'/refunds/{rid}/execute', body=execute)
    worker = importlib.import_module(m.__package__ + '.refund_execution_worker')
    sessions = scoped_sessions(m, rid)
    Execution, Leg, Completion = (m.refund_execution[k] for k in ('Execution', 'PaymentLeg', 'Completion'))
    Recovery = m.refund_execution_resources['Recovery']
    saw_resources = False
    for _ in range(16):
        with m.SessionLocal() as db:
            execution = db.scalar(select(Execution).where(Execution.refund_id == rid))
            check(execution is not None and execution.status != 'failed', 'SaaS worker failed')
            plan = json.loads(execution.resource_plan_json)
            check(plan['kind'] == 'saas' and plan['order_id'] == oid and plan['refund_id'] == rid,
                  'Unexpected resource plan')
            legs = db.scalars(select(Leg).where(Leg.refund_id == rid)).all()
            recoveries = db.scalars(select(Recovery).where(Recovery.refund_id == rid)).all()
            if execution.status == 'resources_applied':
                check(legs and all(r.status == 'prepared' and not r.receipt and r.completed_at is None
                                   for r in legs) and db.get(Completion, rid) is None
                      and Decimal(str(db.get(m.Order, oid).refunded_amount)) == 0,
                      'Funds preceded resource verification')
                check(len(recoveries) == 1 and recoveries[0].execution_id == execution.id
                      and recoveries[0].order_id == oid and recoveries[0].term_id == plan['term_id']
                      and recoveries[0].combination_id == plan['combination_id']
                      and json.loads(recoveries[0].plan_json) == plan and execution.proof_json,
                      'Resource Recovery missing')
                remote_status(m, ids, plan)
                saw_resources = True
            if execution.status == 'completed':
                completion = db.get(Completion, rid)
                check(saw_resources and len(recoveries) == 1 and completion is not None
                      and completion.execution_id == execution.id and Decimal(str(completion.amount)) == Decimal(amount)
                      and db.get(m.Refund, rid).status == 'completed'
                      and Decimal(str(db.get(m.Order, oid).refunded_amount)) == Decimal(amount)
                      and legs and all(r.status == 'completed' and r.completed_at is not None
                          and r.receipt == 'simulated:' + rid + ':' + r.payment_id for r in legs),
                      'Local refund completion missing')
                break
        check(worker.process_once(SessionLocal=sessions, ns=vars(m),
              now=lambda: m.now() + timedelta(hours=2)), 'Owned execution not runnable')
    else:
        raise base.AcceptanceFailure('Bounded SaaS worker phases exhausted')
    before = collect(m, ids)
    base.request(client, tokens['delivery'], 'POST', f'/refunds/{rid}/execute', body=execute)
    check(not worker.process_once(SessionLocal=sessions, ns=vars(m),
          now=lambda: m.now() + timedelta(hours=2)), 'Completed refund remained runnable')
    # Replay the actual production resource adapter against its durable receipt.
    with sessions() as db:
        execution = db.scalar(select(Execution).where(Execution.refund_id == rid))
        db.expunge(execution)
    with sessions(bind=m.engine.execution_options(isolation_level='AUTOCOMMIT')) as db:
        proof = m.refund_execution['adapter']()['apply'](db, execution)
    check(proof == json.loads(execution.proof_json), 'Resource replay changed proof')
    check(collect(m, ids) == before, 'Replay duplicated local evidence')
    remote_status(m, ids, plan)
    return dict(refund_id=rid, resources_before_funds=True, idempotent=True,
                expected_expires_at=plan['expected_expires_at'], expires_at=plan['expires_at'])


def close_external(m, ids, journal, save):
    import httpx
    from sqlalchemy import select
    tenant = ids['external']['tenant']
    if tenant and journal.get('tenant_cleanup') != 'retained_closed':
        with m.SessionLocal() as db:
            sub = db.get(m.SaaSSubscription, ids['database_ids']['subscription_id'])
            check(sub is not None and sub.product_id == ids['product']
                  and sub.enterprise_id == ids['enterprises']['buyer']
                  and tenant == 'tenant-' + sub.id[:12]
                  and sub.external_tenant_id in (None, '', tenant), 'Tenant cleanup ownership failed')
            config = db.scalar(select(m.SaaSIntegrationConfig).where(
                m.SaaSIntegrationConfig.product_id == ids['product']))
            check(config is not None, 'Tenant cleanup credentials unavailable')
            identity = dict(tenant_id=tenant, product_id=ids['product'],
                            enterprise_id=sub.enterprise_id, subscription_id=sub.id)
            code, status = base.mock_saas_status(m, config, identity)
            if code == 404:
                check(status == {'detail': 'tenant_not_found'} and not ids['external']['tenant_created'],
                      'Created tenant unexpectedly missing')
                journal['tenant_cleanup'] = 'never_created'
            else:
                check(code == 200 and isinstance(status, dict) and status.get('ret') == 0
                      and all(status.get(k) == v for k, v in identity.items()), 'Tenant cleanup STATUS mismatch')
                journal['tenant_cleanup'] = 'close_requested'
                save()
                result = m.saas_call(config, 'CLOSE', dict(identity,
                    idempotency_key=ids['run_id'] + '-refund-cleanup-close'))
                check(result.get('ret') == 0 and result.get('tenant_id') == tenant
                      and result.get('status') == 'closed', 'Tenant CLOSE failed')
                code, status = base.mock_saas_status(m, config, identity)
                check(code == 200 and status.get('ret') == 0 and status.get('status') == 'closed'
                      and all(status.get(k) == v for k, v in identity.items()), 'Tenant CLOSE readback failed')
                journal['tenant_cleanup'] = 'retained_closed'
            save()
    client_id = ids['external']['oidc_client_id']
    if client_id and ids['external']['oidc_client_created']:
        with m.SessionLocal() as db:
            registered = db.scalar(select(m.OIDCClient).where(
                m.OIDCClient.product_id == ids['product'], m.OIDCClient.client_id == client_id))
            check(registered is not None and registered.product_id == ids['product']
                  and registered.client_id == client_id, 'OIDC cleanup ownership failed')
        journal['oidc_cleanup'] = 'delete_requested'
        save()
        response = httpx.delete(base.MOCK_SAAS + '/_test/oidc/clients/' + client_id,
            headers={'X-Mock-Admin-Secret': base.mock_admin_secret()}, timeout=10,
            follow_redirects=False, trust_env=False)
        removed = response.json()
        check(response.status_code == 200 and removed.get('client_id') == client_id
              and type(removed.get('deleted')) is bool, 'Exact OIDC registration deletion failed')
        ids['external']['oidc_client_created'] = False
        journal['oidc_cleanup'] = 'removed'
        save()


def run(manifest_path=None, *, reviewed=False):
    """Installed-app entry point; main owns transport and explicit invocation.

    Requires a control DATABASE_URL allowed to create an ephemeral NOLOGIN role
    and schema. The application connects as that restricted role. This function
    intentionally has no remote runner, source uploader, or CLI auto-execution.
    """
    check(reviewed is True, 'Main review required before live fixture execution')
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.schema import CreateSchema, DropSchema
    from fastapi.testclient import TestClient
    import r3_backend_acceptance_pg as pg
    check(not any(n == 'app' or n.startswith('app.') for n in sys.modules),
          'Fresh installed-app interpreter required')
    url = make_url(os.environ['DATABASE_URL'])
    check(url.get_backend_name() == 'postgresql', 'PostgreSQL required')
    schema = 'qa_r3_be_' + secrets.token_hex(8)
    role = schema + '_role'
    check(pg.owned_schema(schema), 'Invalid owned schema')
    control = create_engine(url.update_query_dict({'options':
        '-csearch_path=public -cstatement_timeout=8000 -clock_timeout=3000'}),
        hide_parameters=True, connect_args={'connect_timeout': 5})
    ids = base.fixture_ids()
    path = Path(manifest_path or '/tmp/' + ids['run_id'] + '-refund.json')
    journal = dict(journal_version=1, schema=schema, role=role, public_roots={}, public_phase='not_created',
                   schema_phase='not_created', role_phase='not_created',
                   environment=hashlib.sha256(url.render_as_string(hide_password=False).encode()).hexdigest(),
                   protected=protected_snapshot(control), tenant_cleanup='not_created', oidc_cleanup='not_created')

    def save(create=False):
        journal['fixture'] = {k: v for k, v in ids.items() if k != '_manifest_writer'}
        base.write_manifest(path, journal, create=create)

    # Durable identities and protected fingerprint precede even schema/role DDL.
    save(create=True)
    ids['_manifest_writer'] = lambda _: save()
    m = client = None
    result = None
    try:
        journal['role_phase'] = 'create_requested'
        save()
        with control.begin() as conn:
            conn.exec_driver_sql(f'CREATE ROLE "{role}" NOLOGIN NOINHERIT NOSUPERUSER '
                                 'NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS')
            # Membership permits SET ROLE even when the control user is not superuser.
            conn.exec_driver_sql(f'GRANT "{role}" TO CURRENT_USER')
        journal['role_phase'] = 'created'
        journal['schema_phase'] = 'create_requested'
        save()
        with control.begin() as conn:
            conn.execute(CreateSchema(schema))
            conn.exec_driver_sql(f'ALTER SCHEMA "{schema}" OWNER TO "{role}"')
        journal['schema_phase'] = 'created'
        save()
        private_url = url.update_query_dict({'options': f'-csearch_path={schema} -crole={role} '
                                             '-cstatement_timeout=8000 -clock_timeout=3000'})
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                'DATABASE_URL': private_url.render_as_string(hide_password=False),
                'MESSAGE_CENTER_DISABLE_BACKGROUND': 'true',
                'OIDC_ISSUER': base.OIDC_ISSUER}))
            m = base.load_main()
            assert_private(m, schema, role)
            m.Base.metadata.create_all(m.engine)
            # No lifespan/startup: it seeds unrelated application defaults.
            for name in ('SMTP', 'SMTP_SSL'):
                stack.enter_context(patch.object(m.smtplib, name,
                    side_effect=AssertionError('Mail forbidden in isolated SaaS runtime')))
            with m.SessionLocal.begin() as db:
                db.add(m.SystemSetting(setting_key='application_integration_allow_http',
                    setting_value='true', updated_by=ids['run_id']))
            tokens, oidc_secret = base.prepare_fixture(m, ids)
            save()
            with m.SessionLocal.begin() as db:
                db.get(m.User, ids['users']['delivery']).platform_role = 'super_admin'
                from sqlalchemy import select
                config = db.scalar(select(m.SaaSIntegrationConfig).where(
                    m.SaaSIntegrationConfig.product_id == ids['product']))
                config.token_url = m.platform_oauth_token_url()
            create_bridge(m, control, ids, journal, save)
            base.register_mock_client(ids, oidc_secret)
            client = TestClient(m.app)
            with owned_journal(m, ids):
                base.request(client, tokens['provider'], 'POST',
                             f"/products/{ids['product']}/saas-integration/verify")
                original = purchase(m, ids, path, client, tokens)
                opened = remote_status(m, ids)
                renewal = purchase(m, ids, path, client, tokens)
                renewed = remote_status(m, ids)
                check(aware(renewed['expires_at']) > aware(opened['expires_at']), 'RENEW did not extend tenant')
                blocked = blocked_earlier_refund(m, ids, client, tokens, original)
                proof = exercise_refund(m, ids, client, tokens, renewal)
            assert_private(m, schema, role)
            result = dict(status='passed', proof=proof, earlier_refund_blocked=blocked, owned_evidence=collect(m, ids),
                manifest=str(path), scope='isolated actual-app refund; deployed SaaS and platform OAuth HTTP',
                public_ingress=False, browser=False, funds='simulated', public_business_orders=0)
    finally:
        if client is not None:
            client.close()
        try:
            check(protected_snapshot(control) == journal['protected'], 'Protected snapshot changed')
            if m is not None:
                close_external(m, ids, journal, save)
                delete_bridge(m, control, journal, save)
                m.engine.dispose()
            # A failed external/bridge cleanup raises before dropping recovery data.
            if journal['schema_phase'] == 'created':
                with control.begin() as conn:
                    conn.execute(DropSchema(schema, cascade=True))
                journal['schema_phase'] = 'removed'
                save()
            if journal['role_phase'] == 'created':
                with control.begin() as conn:
                    conn.exec_driver_sql(f'DROP ROLE "{role}"')
                journal['role_phase'] = 'removed'
                save()
            check(protected_snapshot(control) == journal['protected'], 'Protected snapshot changed')
        finally:
            save()
            control.dispose()
    check(result is not None, 'Runtime did not produce evidence')
    result.update(cleanup=dict(tenant=journal['tenant_cleanup'], oidc=journal['oidc_cleanup'],
        public_roots=journal['public_phase'], schema=journal['schema_phase'], role=journal['role_phase']),
        protected_preserved=True)
    return result


def read_recovery(path):
    """Validate private journal ownership and generated identities before SQL."""
    import r3_backend_acceptance_pg as pg
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
              and info.st_uid == os.getuid() and info.st_nlink == 1 and info.st_size <= 1048576,
              'Recovery journal must be owned regular private file')
        journal = json.load(stream)
    check(isinstance(journal, dict) and set(journal) == {
        'journal_version', 'schema', 'role', 'public_roots', 'public_phase', 'schema_phase',
        'role_phase', 'environment', 'protected', 'tenant_cleanup', 'oidc_cleanup', 'fixture'},
        'Invalid recovery journal fields')
    check(journal['journal_version'] == 1 and pg.owned_schema(journal['schema'])
          and journal['role'] == journal['schema'] + '_role', 'Invalid recovery schema or role')
    check(isinstance(journal['environment'], str) and re.fullmatch(r'[0-9a-f]{64}', journal['environment'])
          and journal['protected'].get('count') == 1
          and re.fullmatch(r'[0-9a-f]{64}', journal['protected'].get('sha256', '')),
          'Invalid recovery environment or protected fingerprint')
    ids = journal['fixture']
    check(isinstance(ids, dict) and isinstance(ids.get('run_id'), str)
          and re.fullmatch(r'qa-vcs-saas-[0-9a-f]{12}', ids['run_id']), 'Invalid recovery fixture marker')
    expected = base.fixture_ids(ids['run_id'])
    check(set(ids) == set(expected), 'Invalid recovery fixture fields')
    for key in ('users', 'enterprises', 'product', 'versions', 'oauth_client_id', 'request_id'):
        check(ids[key] == expected[key], 'Recovery fixture roots changed')
    ext, database = ids['external'], ids['database_ids']
    check(set(ext) == set(expected['external']) and set(database) == set(expected['database_ids']),
          'Invalid recovery external or database fields')
    for key in ('tenant_created', 'oidc_client_created'):
        check(type(ext[key]) is bool, 'Invalid recovery external creation flag')
    for key in ('orders', 'refunds'):
        check(isinstance(database[key], list) and len(database[key]) <= 4
              and len(database[key]) == len(set(database[key]))
              and all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]{32}', v) for v in database[key]),
              'Invalid recovery business identities')
    sid = database['subscription_id']
    check(sid is None or isinstance(sid, str) and re.fullmatch(r'[0-9a-f]{32}', sid),
          'Invalid recovery subscription identity')
    check(ext['tenant'] is None or sid and ext['tenant'] == 'tenant-' + sid[:12],
          'Invalid recovery tenant identity')
    check(ext['oidc_client_id'] is None or isinstance(ext['oidc_client_id'], str)
          and re.fullmatch(r'oidc_[0-9a-f]{32}', ext['oidc_client_id']), 'Invalid recovery OIDC identity')
    check(not ext['tenant_created'] or ext['tenant'] is not None, 'Created tenant identity missing')
    check(not ext['oidc_client_created'] or ext['oidc_client_id'] is not None, 'Created OIDC identity missing')
    check(journal['public_phase'] in {'not_created', 'create_requested', 'collision_checked',
          'created', 'delete_requested', 'removed'}, 'Invalid recovery public phase')
    # Ambiguous CREATE acknowledgements require manual review, not guessed ownership.
    check(journal['role_phase'] in {'created', 'removed'}
          and journal['schema_phase'] in {'created', 'removed'}, 'Ambiguous DDL phase requires review')
    check(journal['tenant_cleanup'] in {'not_created', 'close_requested', 'never_created', 'retained_closed'}
          and journal['oidc_cleanup'] in {'not_created', 'delete_requested', 'removed'},
          'Invalid recovery external cleanup phase')
    roots = journal['public_roots']
    if roots:
        check(set(roots) == {'enterprises', 'products', 'oauth_clients'}
              and roots['enterprises']['id'] == ids['enterprises']['provider']
              and roots['products']['id'] == ids['product']
              and roots['products']['enterprise_id'] == ids['enterprises']['provider']
              and roots['products']['status'] == 'draft'
              and roots['oauth_clients']['product_id'] == ids['product']
              and roots['oauth_clients']['client_id'] == ids['oauth_client_id']
              and re.fullmatch(r'[0-9a-f]{32}', roots['oauth_clients']['id']), 'Invalid recovery public roots')
    else:
        check(journal['public_phase'] == 'not_created', 'Recovery public roots missing')
    return journal


def resume_cleanup(manifest_path, *, reviewed=False):
    """Resume confirmed owned phases only; no source upload or live invocation."""
    check(reviewed is True, 'Main review required before recovery')
    journal = read_recovery(manifest_path)
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.schema import DropSchema
    check(not any(n == 'app' or n.startswith('app.') for n in sys.modules), 'Fresh recovery interpreter required')
    url = make_url(os.environ['DATABASE_URL'])
    check(url.get_backend_name() == 'postgresql' and hashlib.sha256(
        url.render_as_string(hide_password=False).encode()).hexdigest() == journal['environment'],
        'Recovery environment mismatch')
    control = create_engine(url.update_query_dict({'options':
        '-csearch_path=public -cstatement_timeout=8000 -clock_timeout=3000'}), hide_parameters=True)
    ids = journal['fixture']

    def save():
        journal['fixture'] = {k: v for k, v in ids.items() if k != '_manifest_writer'}
        base.write_manifest(manifest_path, journal)

    ids['_manifest_writer'] = lambda _: save()
    schema, role = journal['schema'], journal['role']
    m = None
    try:
        check(protected_snapshot(control) == journal['protected'], 'Protected recovery snapshot changed')
        with control.connect() as conn:
            owner = conn.scalar(text('SELECT r.rolname FROM pg_namespace n JOIN pg_roles r '
                'ON r.oid=n.nspowner WHERE n.nspname=:schema'), {'schema': schema})
        if journal['schema_phase'] == 'created':
            check(owner == role, 'Recovery schema ownership missing or changed')
            private_url = url.update_query_dict({'options': f'-csearch_path={schema} -crole={role} '
                '-cstatement_timeout=8000 -clock_timeout=3000'})
            with patch.dict(os.environ, {'DATABASE_URL': private_url.render_as_string(hide_password=False),
                    'MESSAGE_CENTER_DISABLE_BACKGROUND': 'true', 'OIDC_ISSUER': base.OIDC_ISSUER}):
                m = base.load_main()
                assert_private(m, schema, role)
                close_external(m, ids, journal, save)
                delete_bridge(m, control, journal, save)
                m.engine.dispose()
            with control.begin() as conn:
                conn.execute(DropSchema(schema, cascade=True))
            journal['schema_phase'] = 'removed'
            save()
        else:
            check(owner is None and journal['public_phase'] in {'not_created', 'removed'}
                  and (not ids['external']['tenant'] or journal['tenant_cleanup'] in {'never_created', 'retained_closed'})
                  and not ids['external']['oidc_client_created'], 'Recovery schema absent with unresolved effects')
        if journal['role_phase'] == 'created':
            with control.begin() as conn:
                check(conn.scalar(text('SELECT count(*) FROM pg_roles WHERE rolname=:role AND NOT rolcanlogin '
                    'AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolbypassrls'),
                    {'role': role}) == 1, 'Recovery role missing or changed')
                conn.exec_driver_sql(f'DROP ROLE "{role}"')
            journal['role_phase'] = 'removed'
            save()
        check(protected_snapshot(control) == journal['protected'], 'Protected recovery snapshot changed')
        return dict(status='cleaned', manifest=str(manifest_path), protected_preserved=True,
            cleanup=dict(tenant=journal['tenant_cleanup'], oidc=journal['oidc_cleanup'],
                public_roots=journal['public_phase'], schema=journal['schema_phase'], role=journal['role_phase']))
    finally:
        if m is not None:
            m.engine.dispose()
        control.dispose()
