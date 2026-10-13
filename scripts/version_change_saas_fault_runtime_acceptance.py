"""Runtime acceptance for SaaS version-change failure and compensation.

The two phase commands use the existing version_change_saas_runtime_acceptance
fixture helper. Run prepare with the deployed worker batch set to 1. After the
plan handoff, restore the normal worker batch and restart it before fault-check.

  python scripts/version_change_saas_fault_runtime_acceptance.py prepare --manifest /tmp/qa-vcs-saas-fault.json
  # Restore VERSION_CHANGE_WORKER_BATCH to 10 and restart market-version-change.
  python scripts/version_change_saas_fault_runtime_acceptance.py fault-check --manifest /tmp/qa-vcs-saas-fault.json
  # If an older fault-check stopped at its final counter assertion:
  python scripts/version_change_saas_fault_runtime_acceptance.py verify-only --manifest /tmp/qa-vcs-saas-fault.json
  python scripts/version_change_saas_fault_runtime_acceptance.py cleanup --manifest /tmp/qa-vcs-saas-fault.json

Failures retain the 0600 journal and its .base ownership file for cleanup.
The only injected fault is authenticated CLOSE of this run's owned mock-SaaS
tenant after its durable target plan has been committed.
"""
import argparse
from datetime import timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import time


HELPER_PATH = Path(__file__).with_name('version_change_saas_runtime_acceptance.py')
SPEC = importlib.util.spec_from_file_location('version_change_saas_runtime_acceptance', HELPER_PATH)
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)


class AcceptanceFailure(base.AcceptanceFailure):
    pass


STAGE = 'startup'
MAX_WAIT = 150


def require(value, message):
    if not value:
        raise AcceptanceFailure(message)


def check_helper_contract():
    ids = base.fixture_ids('qa-vcs-saas-000000000000')
    require(isinstance(ids['enterprises'], dict) and
            set(ids['enterprises']) == {'buyer', 'provider'},
            'SaaS helper fixture contract requires role-keyed buyer/provider enterprise IDs')
    require(isinstance(ids['versions'], dict) and
            ids['versions'].get('source') == ids['run_id'] + '-v-source',
            'SaaS helper source version identity contract changed')
    require(isinstance(ids['users'], dict) and {'buyer', 'finance', 'provider'} <= set(ids['users']),
            'SaaS helper user role contract changed')
    return {'status': 'passed', 'helper': HELPER_PATH.name,
            'enterprise_ids': 'role-keyed mapping', 'source_version_id': ids['versions']['source'],
            'source_version_code': 'source'}


def runtime_save(path, journal, *, create=False):
    path = Path(path)
    temporary = None
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    else:
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
                'Runtime journal must be a regular 0600 file')
        fd, temporary = tempfile.mkstemp(prefix='.vcs-saas-fault-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(journal, stream, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if temporary:
            os.replace(temporary, path)
            temporary = None
    finally:
        if temporary:
            os.unlink(temporary)


def runtime_load(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0))
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
                'Runtime journal must be a regular 0600 file')
        with os.fdopen(fd, 'r') as stream:
            fd = -1
            journal = json.load(stream)
    finally:
        if fd >= 0:
            os.close(fd)
    require(set(journal) == {'run_id', 'phase', 'change_id', 'plan_fingerprint',
                             'tenant_close_verified', 'source_restore_verified',
                             'compensated_verified'} and
            journal['phase'] in {'preflight', 'fixture', 'plan-handoff', 'tenant-closed',
                                 'exception', 'source-restored', 'compensated'},
            'Runtime journal shape or phase is invalid')
    require(isinstance(journal['run_id'], str) and journal['run_id'].startswith('qa-vcs-saas-') and
            isinstance(journal['change_id'], (str, type(None))) and
            isinstance(journal['plan_fingerprint'], (str, type(None))) and
            all(type(journal[key]) is bool for key in (
                'tenant_close_verified', 'source_restore_verified', 'compensated_verified')),
            'Runtime journal values are invalid')
    return journal


def helper_paths(path):
    path = Path(path)
    return path, Path(str(path) + '.base')


def save_pair(path, journal, ids):
    runtime_save(path, journal)
    base.write_manifest(str(path) + '.base', ids)


def check_environment(m, ids):
    require(ids.get('authorized') is True and
            ids.get('environment') == base.environment_fingerprint(m),
            'Application environment differs from the authorized SaaS fixture')
    subscription_id = ids['database_ids'].get('subscription_id')
    tenant = ids['external'].get('tenant')
    require(not tenant or (subscription_id and tenant == 'tenant-' + subscription_id[:12]),
            'Journaled tenant does not match owned subscription identity')


def wait_plan(m, ids, path, journal, timeout=90):
    from sqlalchemy import select
    Request = m.version_change_saga['VersionChangeRequest']
    deadline = time.monotonic() + timeout
    change_id = journal['change_id']
    while time.monotonic() < deadline:
        with m.SessionLocal() as db:
            row = db.scalar(select(Request).where(Request.id == change_id)
                            .with_for_update(skip_locked=True))
            if row is None:
                db.rollback()
            elif row.resource_plan_json not in ('', '{}'):
                require(row.source_order_id == ids['database_ids']['orders'][0] and
                        row.status == 'resource_pending' and not row.resource_applied and
                        not row.lease_owner,
                        'Owned worker request advanced beyond the durable unapplied plan')
                encoded = row.resource_plan_json
                row.next_retry_at = m.now() + timedelta(hours=1)
                db.commit()
                journal['phase'] = 'plan-handoff'
                journal['plan_fingerprint'] = hashlib.sha256(encoded.encode()).hexdigest()
                save_pair(path, journal, ids)
                return
            else:
                db.rollback()
        time.sleep(.2)
    raise AcceptanceFailure('Timed out waiting for durable SaaS target plan')


def read_change(m, change_id, order_id):
    with m.SessionLocal() as db:
        row = db.get(m.version_change_saga['VersionChangeRequest'], change_id)
        require(row is not None and row.source_order_id == order_id,
                'Owned version-change request or source order changed')
        return {key: getattr(row, key) for key in (
            'status', 'retries', 'compensation_retries', 'compensation_started',
            'resource_applied', 'resource_plan_json', 'funding_order_id')}


def wait_for(m, ids, change_id, predicate, message, timeout=MAX_WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = read_change(m, change_id, ids['database_ids']['orders'][0])
        if predicate(state):
            return state
        time.sleep(.5)
    raise AcceptanceFailure(message)


def exact_identity(ids):
    dbids = ids['database_ids']
    return {'tenant_id': ids['external']['tenant'], 'product_id': ids['product'],
            'enterprise_id': ids['enterprises']['buyer'],
            'subscription_id': dbids['subscription_id']}


def config_and_source(m, ids):
    from sqlalchemy import select
    with m.SessionLocal() as db:
        config = db.scalar(select(m.SaaSIntegrationConfig).where(
            m.SaaSIntegrationConfig.product_id == ids['product'],
            m.SaaSIntegrationConfig.status == 'active'))
        sub = db.get(m.SaaSSubscription, ids['database_ids']['subscription_id'])
        require(config is not None and sub is not None and
                sub.product_id == ids['product'] and
                sub.enterprise_id == ids['enterprises']['buyer'] and
                sub.external_tenant_id == ids['external']['tenant'],
                'Exact owned SaaS configuration/subscription identity is missing')
        source_expiry = sub.expires_at
        client_id = ids['external']['oidc_client_id']
        admin_id = ids['users']['buyer']
    identity = exact_identity(ids)
    check_identity = dict(identity, version='source', admin_user_id=admin_id,
                          oidc_client_id=client_id)
    return config, source_expiry, check_identity


def authenticated_status(m, config, identity):
    import httpx
    require(config.base_url.rstrip('/') == base.MOCK_SAAS and
            '/' + config.operation_path.lstrip('/') == '/isv.php',
            'Refusing SaaS operation outside exact mock service endpoint')
    token = m.saas_access_token(config)
    response = httpx.post(config.base_url.rstrip('/') + '/' + config.operation_path.lstrip('/'),
        json={'type': 'STATUS', **identity}, headers={'Accept': 'application/json',
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
        timeout=config.timeout_ms / 1000, follow_redirects=False, trust_env=False)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload


def close_owned_tenant(m, ids):
    from sqlalchemy import select
    config, _, status_identity = config_and_source(m, ids)
    identity = exact_identity(ids)
    code, current = authenticated_status(m, config, status_identity)
    require(code == 200 and isinstance(current, dict) and current.get('ret') == 0 and
            all(current.get(key) == value for key, value in identity.items()) and
            current.get('version') == 'source' and current.get('status') == 'active',
            'Authenticated STATUS did not prove the owned source tenant before CLOSE')
    with m.SessionLocal() as db:
        onboarding = db.scalar(select(m.Order).where(
            m.Order.id == ids['database_ids']['orders'][0]))
        require(onboarding and onboarding.subscription_id == identity['subscription_id'],
                'Source order does not own the tenant being closed')
    result = m.saas_call(config, 'CLOSE', dict(identity,
        idempotency_key=ids['run_id'] + '-fault-close'))
    require(isinstance(result, dict) and result.get('ret') == 0 and
            result.get('tenant_id') == identity['tenant_id'] and result.get('status') == 'closed',
            'Authenticated CLOSE did not close the exact owned tenant')
    code, closed = authenticated_status(m, config, status_identity)
    require(code == 200 and isinstance(closed, dict) and closed.get('status') == 'closed' and
            all(closed.get(key) == value for key, value in identity.items()),
            'Authenticated STATUS did not verify exact tenant close')


def prepare(path):
    global STAGE
    check_helper_contract()
    m = base.load_main()
    ids = base.fixture_ids()
    runtime, base_path = helper_paths(path)
    require(not runtime.exists() and not base_path.exists(),
            'Refusing to overwrite an existing runtime or helper journal')
    STAGE = 'preflight'
    base.preflight(m, ids)
    ids['authorized'] = True
    journal = {'run_id': ids['run_id'], 'phase': 'preflight', 'change_id': None,
        'plan_fingerprint': None, 'tenant_close_verified': False,
        'source_restore_verified': False, 'compensated_verified': False}
    runtime_save(runtime, journal, create=True)
    base.write_manifest(str(base_path), ids, create=True)
    def writer(value, create=False):
        base.write_manifest(str(base_path), value, create=create)
    ids['_manifest_writer'] = writer
    client = None
    try:
        STAGE = 'fixture-create'
        tokens, machine_secret = base.prepare_fixture(m, ids)
        writer(ids)
        base.register_mock_client(ids, machine_secret)
        from fastapi.testclient import TestClient
        client = TestClient(m.app)
        base.request(client, tokens['provider'], 'POST',
                     f"/products/{ids['product']}/saas-integration/verify")

        STAGE = 'source-purchase-and-open'
        order_id = base.request(client, tokens['buyer'], 'POST', '/orders', body={
            'product_id': ids['product'], 'product_version_id': ids['versions']['source'],
            'buyer_enterprise_id': ids['enterprises']['buyer'], 'subscription_months': 3})['id']
        ids['database_ids']['orders'].append(order_id)
        with m.SessionLocal() as db:
            source_order = db.get(m.Order, order_id)
            require(source_order is not None and source_order.subscription_id,
                    'Fixture source purchase lacks its owned subscription')
            ids['database_ids']['subscription_id'] = source_order.subscription_id
            ids['external']['tenant'] = 'tenant-' + source_order.subscription_id[:12]
            db.flush()
        writer(ids)
        base.request(client, tokens['finance'], 'POST', f'/orders/{order_id}/transition', body={
            'action': 'confirm_payment', 'reason': 'qa-vcs-saas fault runtime source payment'})
        with m.SessionLocal() as db:
            from sqlalchemy import select
            task = db.scalar(select(m.DeliveryTask).where(
                m.DeliveryTask.order_id == order_id))
            require(task is not None, 'Fixture source delivery task is missing')
            task_id = task.id
        base.deliver_staged(m, ids, str(base_path), client, tokens, task_id)
        with m.SessionLocal() as db:
            from sqlalchemy import select
            sub = db.get(m.SaaSSubscription, ids['database_ids']['subscription_id'])
            source_order = db.get(m.Order, order_id)
            source_version = db.get(m.SaaSProductVersion, ids['versions']['source'])
            combo = db.scalar(select(m.subscription_terms['Combination']).where(
                    m.subscription_terms['Combination'].enterprise_id == ids['enterprises']['buyer'],
                    m.subscription_terms['Combination'].product_id == ids['product']))
            require(sub is not None and sub.external_tenant_id == ids['external']['tenant'] and
                    sub.version_id == ids['versions']['source'] and source_order is not None and
                    source_order.product_version_code == 'source' and source_version is not None and
                    source_version.version_code == 'source' and combo is not None and
                    combo.version_id == ids['versions']['source'] and combo.version_code == 'source',
                    'Real mock SaaS source tenant did not open at the source version')
            ids['external']['tenant_created'] = True
            ids['database_ids']['combination_id'] = combo.id
        writer(ids)

        STAGE = 'paid-upgrade-create'
        upgrade = base.create_change(client, tokens, order_id, ids['versions']['upgrade'],
                                     ids['run_id'] + '-fault-upgrade')
        change_id = upgrade['id']
        ids['database_ids']['changes'].append(change_id)
        journal['change_id'] = change_id
        journal['phase'] = 'fixture'
        runtime_save(runtime, journal)
        writer(ids)
        base.request(client, tokens['buyer'], 'POST', f'/version-changes/{change_id}/start-payment', body={})
        base.request(client, tokens['buyer'], 'POST', f'/version-changes/{change_id}/confirm-payment',
                     body={'reason': 'qa-vcs-saas simulated upgrade funding payment'})

        STAGE = 'durable-plan-handoff'
        wait_plan(m, ids, runtime, journal)
        require(journal['plan_fingerprint'] is not None,
                'Durable target plan fingerprint was not journaled')
        close_owned_tenant(m, ids)
        journal['tenant_close_verified'] = True
        journal['phase'] = 'tenant-closed'
        runtime_save(runtime, journal)
        return {'status': 'handoff', 'marker': 'SAAS_FAULT_PLAN_HANDOFF',
                'phase': journal['phase'], 'change_id': change_id,
                'worker_action': 'restore batch 10 and restart worker before fault-check',
                'tenant': 'owned tenant closed through authenticated mock-SaaS CLOSE'}
    finally:
        if client:
            client.close()


def restore_source(m, ids, journal, runtime):
    config, expires_at, status_identity = config_and_source(m, ids)
    require(expires_at is not None, 'Owned source subscription has no expiry to restore')
    identity = exact_identity(ids)
    open_body = dict(identity, enterprise_name=ids['enterprises']['buyer'],
        tenant_name=ids['enterprises']['buyer'], version='source',
        idempotency_key=ids['run_id'] + '-restore-open')
    opened = m.saas_call(config, 'OPEN', open_body)
    require(isinstance(opened, dict) and opened.get('ret') == 0 and
            opened.get('tenant_id') == identity['tenant_id'] and
            opened.get('subscription_id') == identity['subscription_id'] and
            opened.get('version') == 'source' and opened.get('status') == 'opened',
            'Authenticated OPEN did not restore the same tenant identity to source')
    renew_body = dict(identity, expires_at=expires_at.isoformat(),
        idempotency_key=ids['run_id'] + '-restore-active')
    renewed = m.saas_call(config, 'RENEW', renew_body)
    require(isinstance(renewed, dict) and renewed.get('ret') == 0 and
            renewed.get('tenant_id') == identity['tenant_id'] and renewed.get('status') == 'active',
            'Authenticated RENEW did not restore the original active subscription')
    code, live = authenticated_status(m, config, status_identity)
    require(code == 200 and isinstance(live, dict) and live.get('ret') == 0 and
            live.get('status') == 'active' and live.get('version') == 'source' and
            live.get('admin_active') is True and live.get('application_ready') is True and
            all(live.get(key) == value for key, value in identity.items()),
            'Live STATUS failed to verify restored source tenant and original admin mapping')
    journal['source_restore_verified'] = True
    journal['phase'] = 'source-restored'
    runtime_save(runtime, journal)


def verify_compensation(m, ids, journal, runtime):
    from sqlalchemy import select
    change_id = journal['change_id']
    with m.SessionLocal() as db:
        change = db.get(m.version_change_saga['VersionChangeRequest'], change_id)
        require(change is not None and change.source_order_id == ids['database_ids']['orders'][0] and
                change.status == 'compensated' and change.compensation_started and
                change.retries == 0 and change.compensation_retries == 0 and
                not change.resource_applied,
                'Operator retry did not complete compensation after the exhausted failure phase')
        adjustments = db.scalars(select(m.VersionChangeAdjustment).where(
            m.VersionChangeAdjustment.change_id == change_id)).all()
        require(adjustments and all(row.status == 'void' for row in adjustments),
                'Compensated upgrade ledger entries were not voided')
        funds = db.scalars(select(m.version_change_saga['VersionChangeFund']).where(
            m.version_change_saga['VersionChangeFund'].change_id == change_id)).all()
        payment_funds = [fund for fund in funds if fund.kind == 'payment']
        refund_funds = [fund for fund in funds if fund.kind == 'refund' and fund.refund_id]
        require(len(payment_funds) == 1 and len(refund_funds) == 1,
                'Simulated upgrade funding refund is missing or duplicated')
        funding_payment = db.get(m.Payment, payment_funds[0].payment_id)
        funding_order = db.get(m.Order, funding_payment.order_id) if funding_payment else None
        refund = db.get(m.Refund, refund_funds[0].refund_id)
        require(funding_payment and funding_payment.status == 'refunded' and funding_order and
                funding_order.payment_status == 'refunded' and refund and refund.status == 'completed' and
                refund.payment_id == funding_payment.id and
                refund.amount == payment_funds[0].amount,
                'Funding simulation refund does not reconcile to its paid funding order')
        sub = db.get(m.SaaSSubscription, ids['database_ids']['subscription_id'])
        combo = db.get(m.subscription_terms['Combination'], ids['database_ids']['combination_id'])
        source_order = db.get(m.Order, ids['database_ids']['orders'][0])
        source_version = db.get(m.SaaSProductVersion, ids['versions']['source'])
        require(sub and sub.id == ids['database_ids']['subscription_id'] and
                sub.product_id == ids['product'] and
                sub.enterprise_id == ids['enterprises']['buyer'] and
                sub.external_tenant_id == ids['external']['tenant'] and
                sub.version_id == ids['versions']['source'] and combo and
                combo.id == ids['database_ids']['combination_id'] and
                combo.product_id == ids['product'] and
                combo.enterprise_id == ids['enterprises']['buyer'] and
                combo.status == 'active' and combo.version_id == ids['versions']['source'] and
                combo.version_code == 'source' and source_order and
                source_order.product_version_id == ids['versions']['source'] and
                source_order.product_version_code == 'source' and source_version and
                source_version.product_id == ids['product'] and source_version.version_code == 'source',
                'Application subscription or combination did not remain at original version')
        retry_rows = db.scalars(select(m.AuditLog).where(
            m.AuditLog.action == 'retry_version_change_phase',
            m.AuditLog.target_type == 'version_change',
            m.AuditLog.target_id == change_id).order_by(m.AuditLog.created_at)).all()
        require(len(retry_rows) == 6 and all(row.order_id == source_order.id for row in retry_rows),
                'Worker audit trail does not contain six owned retry attempts for the source order')
        attempts = [json.loads(row.after_json or '{}') for row in retry_rows]
        require([item.get('retry_count') for item in attempts[:3]] == [1, 2, 3] and
                [item.get('compensation_retry_count') for item in attempts[:3]] == [0, 0, 0] and
                [item.get('retry_count') for item in attempts[3:]] == [3, 3, 3] and
                [item.get('compensation_retry_count') for item in attempts[3:]] == [1, 2, 3] and
                all(item.get('error_type') for item in attempts),
                'Worker audit retry counters do not prove three target and three compensation failures')
        require(all((later.created_at - earlier.created_at).total_seconds() >= 9
                    for earlier, later in zip(retry_rows, retry_rows[1:])),
                'Worker retry audit events do not respect the ten-second retry interval')
        finance_actor = ids['users']['finance'] + '@example.invalid'
        operator_retries = db.scalars(select(m.AuditLog).where(
            m.AuditLog.action == 'retry_version_change',
            m.AuditLog.target_type == 'version_change',
            m.AuditLog.target_id == change_id)).all()
        require(len(operator_retries) == 1 and operator_retries[0].actor == finance_actor and
                operator_retries[0].order_id == source_order.id and operator_retries[0].result == 'success',
                'Successful operator retry audit is missing or belongs to another actor/order')
    config, _, status_identity = config_and_source(m, ids)
    code, live = authenticated_status(m, config, status_identity)
    identity = exact_identity(ids)
    require(code == 200 and isinstance(live, dict) and live.get('ret') == 0 and
            live.get('version') == 'source' and live.get('status') == 'active' and
            live.get('admin_active') is True and live.get('application_ready') is True and
            live.get('admin_user_id') == ids['users']['buyer'] and
            live.get('oidc_client_id') == ids['external']['oidc_client_id'] and
            all(live.get(key) == value for key, value in identity.items()),
            'Live mock tenant source identity/admin/OIDC state was not restored')
    journal['compensated_verified'] = True
    journal['phase'] = 'compensated'
    runtime_save(runtime, journal)


def fault_check(path):
    global STAGE
    m = base.load_main()
    runtime, base_path = helper_paths(path)
    journal = runtime_load(runtime)
    ids = base.read_manifest(str(base_path))
    require(ids['run_id'] == journal['run_id'], 'Runtime and helper journals have different ownership roots')
    require(journal['change_id'] in ids['database_ids']['changes'],
            'Runtime change identity is outside the helper ownership journal')
    check_environment(m, ids)
    require(journal['tenant_close_verified'] and journal['phase'] in {
                'tenant-closed', 'exception', 'source-restored', 'compensated'} and
            journal['plan_fingerprint'], 'Fault phase requires a closed owned tenant and committed plan')
    expected_tenant = 'tenant-' + ids['database_ids']['subscription_id'][:12]
    require(ids['external']['tenant'] == expected_tenant,
            'Refusing fault processing for a tenant outside the owned subscription')

    STAGE = 'worker-target-and-compensation-failure'
    with m.SessionLocal() as db:
        row = db.get(m.version_change_saga['VersionChangeRequest'], journal['change_id'])
        require(row is not None and row.source_order_id == ids['database_ids']['orders'][0] and
                row.resource_plan_json not in ('', '{}') and
                hashlib.sha256(row.resource_plan_json.encode()).hexdigest() == journal['plan_fingerprint'],
                'Durable target plan ownership/state changed before fault run')
        state = {key: getattr(row, key) for key in (
            'status', 'retries', 'compensation_retries', 'compensation_started', 'resource_applied')}
        if journal['phase'] == 'tenant-closed':
            require(state['status'] in {'resource_pending', 'retrying'},
                    'Worker request is not pending the planned target attempt')
            row.next_retry_at = m.now()
            db.commit()
        else:
            db.rollback()

    if journal['phase'] == 'tenant-closed':
        failure = wait_for(m, ids, journal['change_id'], lambda current:
            current['status'] == 'exception' and current['retries'] == 3 and
            current['compensation_started'] is True and current['compensation_retries'] == 3 and
            current['resource_applied'] is False,
            'Worker did not record three target failures and three original-resource compensation failures')
        journal['phase'] = 'exception'
        runtime_save(runtime, journal)
    else:
        failure = {'retries': 3}
        if journal['phase'] == 'exception':
            require(state['status'] == 'exception' and state['retries'] == 3 and
                    state['compensation_started'] is True and state['compensation_retries'] == 3 and
                    state['resource_applied'] is False,
                    'Journaled failure phase does not match exhausted worker retries')

    if not journal['source_restore_verified']:
        STAGE = 'restore-source-before-operator-retry'
        restore_source(m, ids, journal, runtime)

    current = read_change(m, journal['change_id'], ids['database_ids']['orders'][0])
    if current['status'] == 'compensated':
        verify_compensation(m, ids, journal, runtime)
        return {'status': 'passed', 'phase': journal['phase'],
            'worker_failure_counts': {'target': 3, 'compensation_before_operator_retry': 3},
            'operator_retry': 'already completed in prior invocation',
            'resource': 'original SaaS version restored; plan compensation completed',
            'ledger': 'adjustments voided; simulated funding refund completed',
            'cleanup_required': True}
    if journal['phase'] == 'source-restored' and current['status'] == 'compensating':
        current = wait_for(m, ids, journal['change_id'], lambda item:
            item['status'] in {'exception', 'compensated'},
            'Worker did not finish an in-progress compensation retry')
        if current['status'] == 'compensated':
            verify_compensation(m, ids, journal, runtime)
            return {'status': 'passed', 'phase': journal['phase'],
                'worker_failure_counts': {'target': 3, 'compensation_before_operator_retry': 3},
                'operator_retry': 'completed in prior invocation',
                'resource': 'original SaaS version restored; plan compensation completed',
                'ledger': 'adjustments voided; simulated funding refund completed',
                'cleanup_required': True}

    STAGE = 'operator-retry-authorization'
    from fastapi.testclient import TestClient
    client = TestClient(m.app)
    try:
        with m.SessionLocal() as db:
            tokens = {role: m.issue_token(db.get(m.User, uid))
                      for role, uid in ids['users'].items()}
        base.request(client, tokens['buyer'], 'POST',
            f"/version-changes/{journal['change_id']}/retry", expected=403,
            body={'reason': 'unauthorized fixture check'})
        base.request(client, tokens['finance'], 'POST',
            f"/version-changes/{journal['change_id']}/retry", body={
                'reason': 'owned SaaS source restored after retry exhaustion'})
    finally:
        client.close()

    STAGE = 'worker-compensation-retry'
    wait_for(m, ids, journal['change_id'], lambda state:
        state['status'] == 'compensated',
        'Authorized finance retry did not complete original-resource compensation')
    verify_compensation(m, ids, journal, runtime)
    return {'status': 'passed', 'phase': journal['phase'],
        'worker_failure_counts': {'target': failure['retries'],
                                  'compensation_before_operator_retry': 3},
        'operator_retry': 'buyer denied; finance_settlement accepted',
        'resource': 'original SaaS version restored; plan compensation completed',
        'ledger': 'adjustments voided; simulated funding refund completed',
        'cleanup_required': True}


def cleanup(path):
    global STAGE
    m = base.load_main()
    runtime, base_path = helper_paths(path)
    journal = runtime_load(runtime)
    ids = base.read_manifest(str(base_path))
    require(ids['run_id'] == journal['run_id'], 'Runtime/helper cleanup ownership mismatch')
    require(not journal['change_id'] or journal['change_id'] in ids['database_ids']['changes'],
            'Cleanup change identity is outside the helper ownership journal')
    check_environment(m, ids)
    if journal['change_id']:
        from sqlalchemy import select
        with m.SessionLocal() as db:
            Request = m.version_change_saga['VersionChangeRequest']
            row = db.scalar(select(Request).where(Request.id == journal['change_id']).with_for_update())
            if row is not None:
                require(row.source_order_id in ids['database_ids']['orders'] and
                        not row.lease_owner and
                        (not row.lease_expires_at or row.lease_expires_at <= m.now()),
                        'Owned worker lease is active; stop/retry cleanup after the lease expires')
                row.status = 'exception'
                row.next_retry_at = None
                row.lease_owner = ''
                row.lease_expires_at = None
                db.commit()
    STAGE = 'exact-helper-cleanup'
    base.cleanup(m, ids, str(base_path))
    runtime.unlink()
    return {'status': 'passed', 'cleanup': 'exact owned DB fixture, tenant, and mock OIDC client removed',
            'journal': 'runtime and helper ownership manifests removed after verification'}


def verify_only(path):
    global STAGE
    m = base.load_main()
    runtime, base_path = helper_paths(path)
    journal = runtime_load(runtime)
    ids = base.read_manifest(str(base_path))
    require(ids['run_id'] == journal['run_id'] and
            journal['change_id'] in ids['database_ids']['changes'],
            'Runtime/helper verification ownership mismatch')
    check_environment(m, ids)
    require(journal['tenant_close_verified'] and journal['source_restore_verified'] and
            journal['phase'] in {'source-restored', 'compensated'},
            'verify-only requires the owned source-restored journal; it never closes or reopens SaaS')
    STAGE = 'verify-owned-terminal-compensation'
    change = read_change(m, journal['change_id'], ids['database_ids']['orders'][0])
    require(change['status'] == 'compensated' and change['retries'] == 0 and
            change['compensation_retries'] == 0 and change['compensation_started'] is True and
            change['resource_applied'] is False,
            'Owned request is not terminal compensated with operator-retry counters reset')
    verify_compensation(m, ids, journal, runtime)
    return {'status': 'passed', 'phase': journal['phase'],
            'verification': ['terminal source subscription/order/combination identity',
                'authenticated live SaaS source/admin/OIDC state',
                'finance retry audit actor and source order',
                'three target and three compensation retries with ten-second spacing',
                'void adjustment ledger and completed simulated funding refund'],
            'cleanup_required': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare', 'fault-check', 'verify-only', 'cleanup', 'check-contracts'])
    parser.add_argument('--manifest')
    args = parser.parse_args()
    if args.phase == 'check-contracts':
        result = check_helper_contract()
    elif args.phase == 'prepare':
        require(bool(args.manifest), 'prepare requires --manifest')
        result = prepare(args.manifest)
    elif args.phase == 'fault-check':
        require(bool(args.manifest), 'fault-check requires --manifest')
        result = fault_check(args.manifest)
    elif args.phase == 'verify-only':
        require(bool(args.manifest), 'verify-only requires --manifest')
        result = verify_only(args.manifest)
    else:
        require(bool(args.manifest), 'cleanup requires --manifest')
        result = cleanup(args.manifest)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    try:
        main()
    except AcceptanceFailure as exc:
        print(json.dumps({'status': 'failed', 'check': str(exc), 'stage': STAGE}, sort_keys=True), flush=True)
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'stage': STAGE,
                          'error_type': type(exc).__name__}, sort_keys=True), flush=True)
        raise SystemExit(1)
