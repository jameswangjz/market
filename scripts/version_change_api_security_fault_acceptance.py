"""Runtime security and fault acceptance for owned API resource adapters.

Fault wrappers affect this process only; they are not deployed worker faults.
Run only against the explicitly configured deployed PostgreSQL/APISIX/Redis.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import time

from fastapi import HTTPException

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    'version_change_saga_runtime_acceptance', HERE / 'version_change_saga_runtime_acceptance.py')
base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(base)


class Failure(base.AcceptanceFailure):
    pass


class InjectedLostAck(RuntimeError):
    pass


class InjectedPolicyFault(RuntimeError):
    pass


def require(ok, message):
    base.check(ok, message)


def save_private(path, value, create=False):
    path = Path(path)
    temporary = None
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    else:
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
                'Recovery journal must be a regular 0600 file')
        fd, temporary = tempfile.mkstemp(prefix='.api-security-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True, default=str)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if temporary:
            os.replace(temporary, path)
            temporary = None
    finally:
        if temporary:
            os.unlink(temporary)


def unwrap(value):
    while isinstance(value, dict) and isinstance(value.get('node'), dict):
        value = value['node']
    while isinstance(value, dict) and isinstance(value.get('value'), dict):
        value = value['value']
    return value


def admin(m, method, path, payload=None):
    return unwrap(m.apisix_admin_request(method, path, payload))


def consumer_authored(doc):
    # APISIX server-generated identity/timestamps are not authored consumer state.
    return {key: value for key, value in doc.items()
            if key not in {'id', 'create_time', 'update_time', 'modifiedIndex'}}


def without_consumer_key(method, url, value, consumer_path):
    if method != 'GET' or url != consumer_path:
        return value
    doc = json.loads(json.dumps(unwrap(value)))
    keyauth = doc.get('plugins', {}).get('key-auth', {})
    keyauth.pop('key', None)
    return doc


def snapshot_consumer(m, doc, encrypted_secret):
    value = json.loads(json.dumps(consumer_authored(doc)))
    plugins = value.get('plugins', {})
    keyauth = plugins.get('key-auth', {})
    raw = keyauth.pop('key', None)
    if not isinstance(raw, str) or not raw:
        raw = m.initial_api_credentials['cipher']().decrypt(encrypted_secret.encode()).decode()
    cipher = m.initial_api_credentials['cipher']()
    encrypted = cipher.encrypt(raw.encode())
    value['_recovery_key_ciphertext'] = encrypted.decode() if isinstance(encrypted, bytes) else encrypted
    return value


def restore_consumer(m, name, snapshot):
    value = json.loads(json.dumps(snapshot))
    encrypted = value.pop('_recovery_key_ciphertext')
    key = m.initial_api_credentials['cipher']().decrypt(encrypted.encode()).decode()
    value.setdefault('plugins', {}).setdefault('key-auth', {})['key'] = key
    admin(m, 'PUT', '/consumers/' + name, value)


def counter_snapshot(redis, keys):
    return {key: int(redis.get(key) or 0) for key in keys}


def row_values(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def stable_rows(rows):
    return sorted((row_values(row) for row in rows),
                  key=lambda value: str(value.get('id', '')))


def transaction_snapshot(db, m, change, combination, frozen):
    from sqlalchemy import select
    routes = {frozen['old_route_id'], frozen['target_route_id']}
    fund = m.version_change_saga['VersionChangeFund']
    adjustments = m.version_change_ledger['Model']
    revisions = m.GatewayConfigRevision
    publish = m.GatewayPublishRecord
    return {
        'change': row_values(change),
        'combination': row_values(combination),
        'fund_rows': stable_rows(db.scalars(select(fund).where(fund.change_id == change.id)).all()),
        'ledger_rows': stable_rows(db.scalars(select(adjustments).where(
            adjustments.change_id == change.id)).all()),
        'gateway_revisions': stable_rows(db.scalars(select(revisions).where(
            revisions.route_id.in_(routes))).all()),
        'gateway_publish_records': stable_rows(db.scalars(select(publish).where(
            publish.route_id.in_(routes))).all()),
    }


def verify_transaction_snapshot(m, change_id, combination_id, frozen, expected):
    from sqlalchemy import select
    req = m.version_change_saga['VersionChangeRequest']
    with m.SessionLocal() as db:
        change = db.get(req, change_id)
        combination = db.get(m.subscription_terms['Combination'], combination_id)
        actual = transaction_snapshot(db, m, change, combination, frozen)
        require(actual == expected,
                'Completed request, combination, ledger, fund, or gateway revision state changed')


def restore_policy_fields(redis, key, before):
    # This hash contains policy metadata only; quota counters have distinct keys.
    current = redis.hgetall(key)
    for field in current.keys() - before.keys():
        redis.hdel(key, field)
    if before:
        redis.hset(key, mapping=before)


def fault_case(m, manifest_path, manifest, kind, case, consumer_mutation=None,
               credential_mutation=None, injection=None, concurrent=False,
               delete_credential=False, response_injection=None, redis_fault=None):
    from sqlalchemy import select
    change_id = manifest['database_ids']['downgrades'][kind]
    req = m.version_change_saga['VersionChangeRequest']
    with m.SessionLocal() as db:
        change = db.get(req, change_id)
        require(change is not None and change.status == 'completed' and change.resource_applied,
                'Fault target is not an already completed owned downgrade')
        frozen = json.loads(change.resource_plan_json)
        require(frozen.get('kind') == 'api' and frozen.get('change_id') == change_id,
                'Completed downgrade lacks its exact frozen API resource plan')
        require(change.target_version_id == manifest['versions'][kind]['source'],
                'Completed downgrade does not target the owned source version')
        credential_id = manifest['database_ids']['credentials'][kind]
        credential = db.get(m.ApiCredential, credential_id)
        combination = db.get(m.subscription_terms['Combination'], change.combination_id)
        require(combination is not None and combination.version_id == change.target_version_id and
                credential is not None and credential.route_id == frozen['target_route_id'] and
                credential.enterprise_id == manifest['enterprises']['buyer'],
                'Owned subscription or credential is not on the completed downgrade source')
        credential_values = {column.name: getattr(credential, column.name)
                             for column in m.ApiCredential.__table__.columns}
        plan = dict(frozen)
        evidence_before = change.evidence_json
        combination_id = combination.id
        transaction_before = transaction_snapshot(db, m, change, combination, frozen)
        db.rollback()
    name = next(row['name'] for row in manifest['consumers'] if row['id'] == credential_id)
    path = '/consumers/' + name
    consumer_before = admin(m, 'GET', path)
    require(isinstance(consumer_before, dict) and consumer_before.get('username') == name,
            'Exact owned consumer is unavailable')
    snapshot = snapshot_consumer(m, consumer_before, credential_values['secret_encrypted'])
    policy_key = 'market:apisix:credential:' + name
    redis = m.apisix_policy_redis()
    policy_before = redis.hgetall(policy_key)
    windows = next(row for row in manifest['quota_windows'] if row['kind'] == kind)
    token_scope = windows['scope'].replace('%', '%25').replace(':', '%3A')
    token_period = windows['period'].replace('%', '%25').replace(':', '%3A')
    counter_keys = [f'market:apisix:quota:subscription:month:{token_scope}:{token_period}']
    counters_before = counter_snapshot(redis, counter_keys)
    redis.close()
    require(policy_before.get('mode') == 'subscription' and
            policy_before.get('quota_scope') == windows['combination_id'],
            'Owned policy is not scoped to the exact subscription combination')
    recovery = {'run_id': manifest['run_id'], 'kind': kind, 'case': case,
                'change_id': change_id, 'consumer': name, 'consumer_authored': snapshot,
                'policy': policy_before, 'credential': credential_values,
                'evidence_json': evidence_before,
                'transaction_snapshot_fingerprint': hashlib.sha256(json.dumps(
                    transaction_before, sort_keys=True, default=str).encode()).hexdigest(),
                'counter_keys': counter_keys, 'counter_floor': counters_before,
                'phase': 'snapshotted'}
    recovery_path = Path(str(manifest_path) + '.' + kind + '-' + case + '.recovery')
    save_private(recovery_path, recovery, create=True)
    real_admin = m.apisix_admin_request
    real_redis = m.apisix_policy_redis
    detected = []
    mutation_error = None
    injection_fired = False
    policy_fault_fired = {'value': False}
    try:
        if consumer_mutation:
            changed = json.loads(json.dumps(consumer_authored(consumer_before)))
            consumer_mutation(changed)
            try:
                admin(m, 'PUT', path, changed)
            except RuntimeError:
                actual = admin(m, 'GET', path)
                require(consumer_authored(actual) == consumer_authored(consumer_before),
                        'Rejected consumer mutation changed the owned APISIX consumer')
                mutation_error = 'APISIX rejected or did not apply injected schema shape'
        if credential_mutation:
            with m.SessionLocal() as db:
                row = db.get(m.ApiCredential, credential_id)
                credential_mutation(row)
                db.commit()

        def wrapped(method, url, payload=None):
            nonlocal injection_fired
            result = real_admin(method, url, payload) if payload is not None else real_admin(method, url)
            if injection:
                try:
                    injection(method, url, payload, result)
                except InjectedLostAck:
                    injection_fired = True
                    raise
            if response_injection:
                result = response_injection(method, url, result)
            return result

        m.apisix_admin_request = wrapped if (injection or response_injection or not mutation_error) else real_admin
        if redis_fault:
            class PolicyFaultRedis:
                def __init__(self):
                    self.real = real_redis()
                def __getattr__(self, name):
                    return getattr(self.real, name)
                def hset(self, key, *args, **kwargs):
                    if key == policy_key:
                        if redis_fault == 'after':
                            self.real.hset(key, *args, **kwargs)
                        policy_fault_fired['value'] = True
                        raise InjectedPolicyFault('controlled owned Redis policy write failure')
                    return self.real.hset(key, *args, **kwargs)
            m.apisix_policy_redis = PolicyFaultRedis

        def perform_apply():
            with m.SessionLocal() as db:
                change = db.get(req, change_id)
                if delete_credential:
                    db.delete(db.get(m.ApiCredential, credential_id))
                    db.flush()
                try:
                    m.version_change_resources['apply'](db, change, 'target')
                    db.flush()
                except HTTPException as exc:
                    require(exc.status_code == 409, 'Adapter rejected a fault with unexpected status')
                    detected.append('adapter-rejected')
                    db.rollback()
                    return
                except InjectedLostAck:
                    if case != 'consumer PUT lost acknowledgment':
                        raise
                    require(injection_fired, 'Lost-ack wrapper did not confirm a successful owned PUT')
                    detected.append('write-ack-lost')
                    db.rollback()
                    return
                except InjectedPolicyFault:
                    require(policy_fault_fired['value'], 'Owned Redis write fault was not injected')
                    detected.append('policy-write-fault')
                    db.rollback()
                    return
                db.commit()
                detected.append('apply-succeeded')

        if concurrent and not mutation_error:
            import concurrent.futures
            import httpx
            consumer_key = m.initial_api_credentials['cipher']().decrypt(
                credential_values['secret_encrypted'].encode()).decode()
            gateway = os.getenv('QA_VCS_GATEWAY_URL', 'http://market-apisix:9080').rstrip('/')
            route_key = frozen['target_route_key']
            barrier = threading.Barrier(5)
            journal_lock = threading.Lock()

            def invoke():
                barrier.wait(timeout=10)
                with journal_lock:
                    counter_redis = m.apisix_policy_redis()
                    try:
                        base.journal_counter(m, manifest_path, manifest, counter_redis,
                            windows['scope'], windows['period'], windows['combination_id'], kind)
                    finally:
                        counter_redis.close()
                response = httpx.get(f'{gateway}/gateway/{route_key}/v1/echo',
                    headers={'X-API-Key': consumer_key}, timeout=15)
                return response.status_code

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
                traffic = [pool.submit(invoke) for _ in range(4)]
                barrier.wait(timeout=10)
                perform_apply()
                statuses = [future.result(timeout=25) for future in traffic]
            require(all(status == 200 for status in statuses),
                    'Concurrent owned gateway requests did not remain authorized')
            detected.append('concurrent-success-count-' + str(statuses.count(200)))
        else:
            if mutation_error:
                detected.append('schema-shape-rejected; adapter-not-exercised')
            else:
                perform_apply()
        if mutation_error:
            pass
        elif redis_fault:
            require(detected == ['policy-write-fault'] and policy_fault_fired['value'],
                    'Owned Redis policy failure injection did not execute')
        elif concurrent:
            require('apply-succeeded' in detected,
                    'Concurrent-traffic resource apply did not complete successfully')
        elif case == 'consumer PUT lost acknowledgment':
            require(detected == ['write-ack-lost'] and injection_fired,
                    'Owned consumer lost-ack injection was not observed exactly')
        else:
            require(detected == ['adapter-rejected'],
                    'Fault case did not produce the expected production-adapter rejection')
        require(detected, 'Fault case produced no recorded outcome')
    finally:
        m.apisix_admin_request = real_admin
        m.apisix_policy_redis = real_redis
        try:
            restore_consumer(m, name, snapshot)
            with m.SessionLocal() as db:
                row = db.get(m.ApiCredential, credential_id)
                for column in m.ApiCredential.__table__.columns:
                    setattr(row, column.name, credential_values[column.name])
                db.get(req, change_id).evidence_json = evidence_before
                db.commit()
            redis = m.apisix_policy_redis()
            restore_policy_fields(redis, policy_key, policy_before)
            current_counters = counter_snapshot(redis, counter_keys)
            require(all(current_counters[key] >= counters_before[key] for key in counter_keys),
                    'A quota counter decreased during the fault case')
            if concurrent:
                require(current_counters[counter_keys[-1]] >= counters_before[counter_keys[-1]] + 4,
                        'Concurrent requests did not produce four continuous monthly increments')
            redis.close()
            actual = admin(m, 'GET', path)
            require(consumer_authored(actual) == consumer_authored(consumer_before),
                    'Exact authored APISIX consumer state was not restored')
            with m.SessionLocal() as db:
                row = db.get(m.ApiCredential, credential_id)
                require(all(getattr(row, key) == value for key, value in credential_values.items()),
                        'Exact database credential state was not restored')
                combo = db.get(m.subscription_terms['Combination'], manifest['database_ids']['combinations'][kind]['id'])
                require(combo is not None and combo.version_id == manifest['versions'][kind]['source'] and
                        row.route_id == plan['target_route_id'],
                        'Owned subscription or credential did not remain on source')
            verify_transaction_snapshot(m, change_id, combination_id, frozen, transaction_before)
            recovery['phase'] = 'restored'
            recovery['counter_after'] = current_counters
            save_private(recovery_path, recovery)
        except Exception:
            raise
        # Leave the recovery journal if restoration was interrupted.
        else:
            recovery_path.unlink(missing_ok=True)
    return {'kind': kind, 'case': case, 'outcome': detected}


def fault_matrix(m, path, manifest):
    outcomes = []
    for kind in ('api', 'model'):
        outcomes.append(fault_case(m, path, manifest, kind, 'revoked credential',
            credential_mutation=lambda row: setattr(row, 'status', 'revoked')))
        outcomes.append(fault_case(m, path, manifest, kind, 'credential hash drift',
            credential_mutation=lambda row: setattr(row, 'key_hash', '0' * 64)))
        outcomes.append(fault_case(m, path, manifest, kind, 'consumer key mismatch',
            consumer_mutation=lambda doc: doc['plugins']['key-auth'].__setitem__(
                'key', 'mk_fault_injected_invalid')))
        outcomes.append(fault_case(m, path, manifest, kind, 'disabled quota plugin',
            consumer_mutation=lambda doc: doc['plugins']['market-gateway-quota'].__setitem__(
                '_meta', {'disable': True})))
        outcomes.append(fault_case(m, path, manifest, kind, 'missing key with invalid encrypted value',
            credential_mutation=lambda row: setattr(row, 'secret_encrypted', 'invalid-encrypted-fixture'),
            response_injection=lambda method, url, value: without_consumer_key(
                method, url, value, '/consumers/' + next(
                    row['name'] for row in manifest['consumers']
                    if row['id'] == manifest['database_ids']['credentials'][kind]))))
        outcomes.append(fault_case(m, path, manifest, kind,
            'deleted credential in rolled-back transaction', delete_credential=True))

        def lost_ack(method, url, payload, response):
            if method == 'PUT' and url == '/consumers/' + next(
                    row['name'] for row in manifest['consumers']
                    if row['id'] == manifest['database_ids']['credentials'][kind]):
                raise InjectedLostAck('controlled lost acknowledgment after successful APISIX PUT')

        outcomes.append(fault_case(m, path, manifest, kind, 'consumer PUT lost acknowledgment',
                                   injection=lost_ack))
        outcomes.append(fault_case(m, path, manifest, kind, 'concurrent quota traffic', concurrent=True))
        outcomes.append(fault_case(m, path, manifest, kind,
            'consumer written then Redis policy write fails', redis_fault='before'))
        outcomes.append(fault_case(m, path, manifest, kind,
            'Redis policy written then acknowledgment lost', redis_fault='after'))
        written = {'value': False}
        consumer_path = '/consumers/' + next(row['name'] for row in manifest['consumers']
            if row['id'] == manifest['database_ids']['credentials'][kind])
        def track_write(method, url, payload, response):
            if method == 'PUT' and url == consumer_path:
                written['value'] = True
        def corrupt_readback(method, url, response):
            if method == 'GET' and url == consumer_path and written['value']:
                doc = json.loads(json.dumps(unwrap(response)))
                doc['plugins']['market-gateway-quota']['route_key'] = 'unrequested-route'
                return doc
            return response
        outcomes.append(fault_case(m, path, manifest, kind, 'consumer readback mismatch after write',
            injection=track_write, response_injection=corrupt_readback))
    return outcomes


def run(manifest_path):
    m = base.load_main()
    ids = base.fixture_ids()
    path = Path(manifest_path or ('/tmp/' + ids['run_id'] + '.json'))
    manifest = ids
    manifest['environment'] = base.environment_fingerprint(m)
    base.preflight(m, manifest)
    manifest['authorized'] = True
    base.write_manifest(path, manifest, create=True)
    client = None
    cleaned = False
    try:
        from fastapi.testclient import TestClient
        tokens = base.prepare(m, ids)
        client = TestClient(m.app)
        migration = base.exercise(m, ids, path, manifest, client, tokens)
        outcomes = fault_matrix(m, path, manifest)
        import api_upstream_oauth_fault_acceptance as upstream_oauth
        oauth_results = {kind: upstream_oauth.run_cases(m, ids, path, client, tokens, kind)
                         for kind in ('api', 'model')}
        base.cleanup(m, manifest, path)
        cleaned = True
        return {'status': 'passed', 'migrations': migration, 'fault_cases': outcomes,
                'upstream_oauth': oauth_results,
                'scope': 'owned API/model resource adapter against deployed PostgreSQL/APISIX/Redis',
                'injection_scope': 'controlled wrapper in this process; deployed worker is unmodified',
                'deferred': [],
                'cleanup': 'exact base saga cleanup completed', 'manifest': str(path)}
    finally:
        if client:
            client.close()
        if not cleaned:
            # Keep the base manifest and any recovery journal for exact recovery.
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest')
    args = parser.parse_args()
    print(json.dumps(run(args.manifest), sort_keys=True, default=str))


if __name__ == '__main__':
    try:
        main()
    except base.AcceptanceFailure as exc:
        print(json.dumps({'status': 'failed', 'check': str(exc)}, sort_keys=True))
        raise SystemExit(1)
    except Exception as exc:
        import traceback
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        print(json.dumps({'status': 'failed', 'file': Path(frame.filename).name,
            'line': frame.lineno} | base.safe_exception_metadata(exc), sort_keys=True))
        raise SystemExit(1)
