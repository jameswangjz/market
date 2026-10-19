"""Bounded installed-pod API provider probe. DO NOT RUN before deployment.

After deployment, copy this file into the backend pod and run from /app:
  python /tmp/api_provider_runtime_probe.py --run

Optional restart check (the probe never restarts pods or deployments):
  python /tmp/api_provider_runtime_probe.py --run --hold-for-restart
  # Operator restarts the mock with its intended persistent storage mounted.
  python /tmp/api_provider_runtime_probe.py --resume --manifest <printed-path>

Interrupted-run recovery:
  python /tmp/api_provider_runtime_probe.py --cleanup --manifest <printed-path>

Uses actual HTTP OAuth token issuance/introspection and mock /management calls.
Creates ONLY a random draft QA product, enterprise and OAuth client. No orders,
payments, subscriptions, gateway resources, business writes or existing client
credentials/endpoints are changed. The protected historical order is read only.
The mode-0600 manifest contains generated IDs, requests and receipts, NOT secrets.
Cleanup closes the QA binding, removes platform fixtures, and leaves closed mock
receipts as audit history: the mock has no authorized HTTP purge operation.
--resume compares a saved receipt in a new process; it cannot independently prove
that the operator restarted the mock or that its database resides on a PVC.
No command, subprocess, deployment operation, or local fallback mock is invoked.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import sys
import tempfile
import time
from urllib.parse import urlsplit


PROTECTED_ORDER = 'ORD-261012005144E1B3'
PLATFORM_URL = 'http://127.0.0.1:8000'
MOCK_URL = 'http://market-mock-api:8300'
STAGE = 'arguments'


class ProbeFailure(RuntimeError):
    """Only fixed, non-sensitive diagnostic messages belong here."""


def check(condition, message):
    if not condition:
        raise ProbeFailure(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def roots(run_id=None):
    run_id = run_id or 'qa-api-provider-' + secrets.token_hex(6)
    check(re.fullmatch(r'qa-api-provider-[0-9a-f]{12}', run_id) is not None,
          'Invalid QA ownership marker')
    return dict(run_id=run_id, enterprise_id=run_id + '-e', product_id=run_id + '-p',
        client_row_id=run_id + '-a', client_id=run_id + '-client', combination_id=run_id + '-c')


def trusted_url(value, kind):
    parsed = urlsplit(value)
    hosts = {'127.0.0.1', 'localhost', 'market-api'} if kind == 'platform' else {
        '127.0.0.1', 'localhost', 'market-mock-api'}
    check(parsed.scheme in {'http', 'https'} and parsed.hostname in hosts and
          not parsed.username and not parsed.password and not parsed.query and
          not parsed.fragment and parsed.path in {'', '/'}, 'Untrusted runtime URL')
    check(parsed.port is None or 1 <= parsed.port <= 65535, 'Invalid runtime port')
    return value.rstrip('/')


def write_manifest(path, manifest, create=False):
    path = Path(path)
    temporary = None
    if create:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    else:
        info = path.lstat()
        check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
              'Manifest must be a private regular file')
        fd, temporary = tempfile.mkstemp(prefix='.api-provider-probe-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(manifest, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if temporary:
            os.replace(temporary, path)
            temporary = None
    finally:
        if temporary:
            os.unlink(temporary)


def read_manifest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
              'Manifest must be a private regular file')
        check(info.st_size <= 1024 * 1024, 'Manifest exceeds the bounded fixture size')
        manifest = json.load(stream)
    check(isinstance(manifest, dict), 'Invalid manifest')
    expected = roots(manifest.get('run_id', ''))
    check(all(manifest.get(key) == value for key, value in expected.items()),
          'QA fixture ownership roots changed')
    check(manifest.get('schema_version') == 1 and isinstance(manifest.get('targets'), list) and
          len(manifest['targets']) <= 6,
          'Invalid manifest protocol')
    trusted_url(manifest['platform_url'], 'platform')
    trusted_url(manifest['mock_url'], 'mock')
    return manifest


def load_runtime():
    # Importing the script, --help, and static review never import app.main.
    os.environ.setdefault('MESSAGE_CENTER_DISABLE_BACKGROUND', 'true')
    for name in ('httpx', 'httpcore', 'sqlalchemy.engine'):
        logging.getLogger(name).setLevel(logging.ERROR)
    sys.path.insert(0, '/app')
    from app import main as m
    from app import api_provider_lifecycle as protocol
    check(m.engine.dialect.name == 'postgresql', 'Installed PostgreSQL runtime is required')
    check(not m.app.dependency_overrides, 'Runtime dependency overrides are forbidden')
    m.engine.echo = False
    return m, protocol


@contextmanager
def session(m):
    from sqlalchemy import text
    with m.SessionLocal() as db:
        db.execute(text("SET LOCAL statement_timeout = '8000ms'"))
        db.execute(text("SET LOCAL lock_timeout = '3000ms'"))
        yield db


def environment(m):
    return hashlib.sha256(m.engine.url.render_as_string(hide_password=False).encode()).hexdigest()


def fingerprint(m, ids):
    """Read-only hashes; no historical row values or credential material escape."""
    from sqlalchemy import select
    result = {}
    with session(m) as db:
        protected = db.scalar(select(m.Order.id).where(m.Order.order_no == PROTECTED_ORDER))
        check(protected is not None, 'Protected historical order is missing')
        for name in ('Order', 'Payment', 'Refund', 'ApiCredential', 'OAuthClient',
                     'Product', 'ProductReleaseVersion', 'ApiGatewayRoute'):
            model = getattr(m, name)
            statement = select(*model.__table__.columns).order_by(model.id)
            if name == 'Product':
                statement = statement.where(model.id != ids['product_id'])
            elif name == 'OAuthClient':
                statement = statement.where(model.id != ids['client_row_id'])
            digest, count = hashlib.sha256(), 0
            for row in db.execute(statement.execution_options(yield_per=200)):
                digest.update(json.dumps(list(row), default=str, separators=(',', ':')).encode())
                digest.update(b'\n')
                count += 1
            result[name] = dict(count=count, sha256=digest.hexdigest())
    return result


def own_rows(m, db, ids, lock=False):
    enterprise = db.get(m.Enterprise, ids['enterprise_id'], with_for_update=lock)
    product = db.get(m.Product, ids['product_id'], with_for_update=lock)
    oauth = db.get(m.OAuthClient, ids['client_row_id'], with_for_update=lock)
    if any(row is not None for row in (enterprise, product, oauth)):
        check(all(row is not None for row in (enterprise, product, oauth)),
              'Partial fixture roots require manual ownership review')
        check(enterprise.name == ids['enterprise_id'] and enterprise.credit_code == ids['enterprise_id'] and
              product.enterprise_id == enterprise.id and product.name == ids['product_id'] and
              product.product_type == 'api' and product.delivery_method == 'api' and product.status == 'draft' and
              product.integration_api_url == ids['mock_url'] + '/management' and
              oauth.product_id == product.id and oauth.client_id == ids['client_id'] and
              oauth.scope == 'resource.invoke resource.manage', 'Fixture ownership proof failed')
    return enterprise, product, oauth


def create_fixture(m, ids):
    from sqlalchemy import select
    with session(m) as db:
        check(all(db.get(model, ids[key]) is None for model, key in (
            (m.Enterprise, 'enterprise_id'), (m.Product, 'product_id'), (m.OAuthClient, 'client_row_id'))),
            'Generated identity collision; fixture creation refused')
        check(db.scalar(select(m.OAuthClient.id).where(m.OAuthClient.client_id == ids['client_id'])) is None,
              'Generated OAuth client collision; fixture creation refused')
        db.add(m.Enterprise(id=ids['enterprise_id'], name=ids['enterprise_id'],
                            credit_code=ids['enterprise_id'], verification_status='verified'))
        db.flush()
        db.add(m.Product(id=ids['product_id'], enterprise_id=ids['enterprise_id'],
            name=ids['product_id'], product_type='api', delivery_method='api', status='draft',
            price=0, currency='CNY', integration_api_url=ids['mock_url'] + '/management'))
        db.flush()
        db.add(m.OAuthClient(id=ids['client_row_id'], product_id=ids['product_id'],
            client_id=ids['client_id'], client_secret=secrets.token_urlsafe(40),
            scope='resource.invoke resource.manage', status='active'))
        db.commit()


class Wire:
    def __init__(self, ids, seconds=180):
        import httpx
        self.ids = ids
        self.deadline = time.monotonic() + seconds
        self.client = httpx.Client(trust_env=False, follow_redirects=False,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1))
        self.calls = 0

    def request(self, method, url, **kwargs):
        remaining = self.deadline - time.monotonic()
        check(remaining > 0 and self.calls < 60, 'Bounded runtime request budget exhausted')
        self.calls += 1
        return self.client.request(method, url, timeout=min(5, remaining), **kwargs)

    def close(self):
        self.client.close()

    def token(self, m, scope):
        with session(m) as db:
            _, _, oauth = own_rows(m, db, self.ids)
            check(oauth is not None and oauth.status == 'active', 'Owned active OAuth client is missing')
            form = dict(grant_type='client_credentials', client_id=oauth.client_id,
                        client_secret=oauth.client_secret, scope=scope)
        response = self.request('POST', self.ids['platform_url'] + '/oauth/token', data=form)
        check(response.status_code == 200, 'Platform OAuth token issuance failed')
        result = response.json()
        token = result.get('access_token')
        check(isinstance(token, str) and token and result.get('scope') == scope,
              'Platform returned an unexpected OAuth scope')
        response = self.request('POST', self.ids['platform_url'] + '/oauth/introspect', data={'token': token})
        check(response.status_code == 200, 'Platform OAuth introspection failed')
        claims = response.json()
        check(claims.get('active') is True and claims.get('product_id') == self.ids['product_id'] and
              claims.get('client_id') == self.ids['client_id'] and claims.get('scope') == scope,
              'Platform token is not scoped to the isolated QA client')
        return token

    def management(self, payload, token, expected=200):
        response = self.request('POST', self.ids['mock_url'] + '/management', json=payload,
            headers={'Authorization': 'Bearer ' + token,
                     'Idempotency-Key': payload['idempotency_key'], 'Accept': 'application/json'})
        check(response.status_code == expected, 'Mock management returned an unexpected HTTP status')
        return response.json() if expected == 200 else None


def verify(protocol, actual, expected):
    check(isinstance(actual, dict) and type(actual.get('ret')) is int and actual['ret'] == 0 and
          'data' not in actual, 'Mock did not return a root integer-success receipt')
    target = protocol.validate_payload(expected)
    check(set(actual) == set(target) | {'ret', 'status', 'effective_at'}, 'Mock receipt fields differ from protocol')
    for field, value in target.items():
        if field != 'tenant_id':
            check(canonical(actual[field]) == canonical(value), 'Mock receipt differs from the frozen target')
    check(protocol.valid_text(actual['tenant_id']) and
          (target['tenant_id'] is None or target['tenant_id'] == actual['tenant_id']),
          'Mock returned an inconsistent tenant identity')
    check(actual['status'] == ('closed' if target['type'] == 'CLOSE' else 'active'),
          'Mock receipt status differs from applied rights')
    protocol.iso(actual['effective_at'])
    return actual


def owned_target(protocol, ids, payload):
    target = protocol.validate_payload(payload)
    check(all(target[key] == ids[key] for key in ('product_id', 'enterprise_id', 'combination_id')) and
          target['generation'] in {1, 2}, 'Target is outside the isolated QA binding')
    check(target['event_id'].startswith(ids['run_id'] + ':') and
          target['idempotency_key'].startswith(ids['run_id'] + ':'), 'Target event is not owned')
    orders = {ids['run_id'] + '-o' + str(index): 'QA-' + ids['run_id'] + '-' + str(index) for index in (1, 2)}
    check(target['order_id'] in orders and target['order_no'] == orders[target['order_id']],
          'Target order identity is not owned')
    for period in target['periods']:
        check(period['order_id'] in orders and period['order_no'] == orders[period['order_id']] and
              period['term_id'] in {ids['run_id'] + '-t' + str(index) for index in (1, 2, 3)},
              'Target period identity is not owned')
    check(target['version']['id'] in {ids['run_id'] + '-v1', ids['run_id'] + '-v2'},
          'Target version identity is not owned')
    return target


def exercise(m, protocol, ids, path, wire):
    from app.subscription_calendar import CalendarAnchor
    anchor = datetime.now(timezone.utc).replace(microsecond=0)
    dates = [CalendarAnchor(anchor).boundary(index).isoformat() for index in range(4)]
    prefix = ids['run_id']
    quota = dict(rate_limit_per_minute=10, daily_quota=100, monthly_quota=1000, quota_amount=1000)
    base = dict(schema_version=1, type='OPEN', event_id=prefix + ':open', idempotency_key=prefix + ':open',
        product_id=ids['product_id'], enterprise_id=ids['enterprise_id'], combination_id=ids['combination_id'],
        generation=1, tenant_id=None, order_id=prefix + '-o1', order_no='QA-' + prefix + '-1',
        version={'id': prefix + '-v1', 'code': 'qa-v1'}, subscription_months=1,
        starts_at=dates[0], expires_at=dates[1], periods=[dict(order_id=prefix + '-o1',
            order_no='QA-' + prefix + '-1', term_id=prefix + '-t1', starts_at=dates[0],
            expires_at=dates[1], quotas=quota)])
    invoke = wire.token(m, 'resource.invoke')
    manage = wire.token(m, 'resource.manage')
    wire.management(base, invoke, expected=403)
    denied = wire.request('GET', ids['mock_url'] + '/v1/echo', headers={'Authorization': 'Bearer ' + manage})
    check(denied.status_code == 403, 'Management-only token reached a business route')
    cases = ['separate_scopes_enforced']
    saved = {}
    current = None
    for operation in ('OPEN', 'RENEW', 'CHANGE', 'ADJUST', 'CLOSE', 'RESTORE'):
        global STAGE
        STAGE = operation
        body = deepcopy(base if current is None else current)
        for field in ('ret', 'status', 'effective_at', 'change_id', 'refund_id', 'direction'):
            body.pop(field, None)
        body.update(type=operation, event_id=prefix + ':' + operation.lower(),
                    idempotency_key=prefix + ':' + operation.lower())
        if operation == 'RENEW':
            body.update(order_id=prefix + '-o2', order_no='QA-' + prefix + '-2', expires_at=dates[2])
            body['periods'].append(dict(order_id=body['order_id'], order_no=body['order_no'],
                term_id=prefix + '-t2', starts_at=dates[1], expires_at=dates[2], quotas=deepcopy(quota)))
        elif operation == 'CHANGE':
            body.update(change_id=prefix + '-change', direction='upgrade', version={'id': prefix + '-v2', 'code': 'qa-v2'})
            for period in body['periods']:
                period['quotas']['monthly_quota'] = 2000
        elif operation == 'ADJUST':
            body['refund_id'] = prefix + '-refund'
            body['periods'][-1]['quotas']['monthly_quota'] = 500
        elif operation == 'CLOSE':
            body['periods'] = []
        elif operation == 'RESTORE':
            body.update(generation=2, starts_at=dates[2], expires_at=dates[3], direction='restore',
                periods=[dict(order_id=body['order_id'], order_no=body['order_no'], term_id=prefix + '-t3',
                              starts_at=dates[2], expires_at=dates[3], quotas=deepcopy(quota))])
        body = owned_target(protocol, ids, body)
        ids['targets'].append(body)
        write_manifest(path, ids)
        current = verify(protocol, wire.management(body, manage), body)
        actual = wire.management(dict(body, type='STATUS', target_type=operation), manage)
        check(actual == current, 'STATUS did not return the actual first stored mutation receipt')
        check(wire.management(body, manage) == current, 'Same-key replay changed the first receipt')
        saved[operation] = (deepcopy(body), deepcopy(current))
        cases.append(operation + '_STATUS_stable_replay')
    changed = deepcopy(saved['RESTORE'][0])
    changed['periods'][0]['quotas']['daily_quota'] += 1
    wire.management(changed, manage, expected=409)
    check(wire.management(saved['OPEN'][0], manage) == saved['OPEN'][1], 'Old event lost its immutable first receipt')
    check(wire.management(dict(saved['RESTORE'][0], type='STATUS', target_type='RESTORE'), manage) == current,
          'Replaying an old event rolled back actual state')
    wrong_product = dict(saved['RESTORE'][0], product_id=prefix + '-other-product')
    wire.management(wrong_product, manage, expected=403)
    wrong_tenant = dict(saved['RESTORE'][0], type='STATUS', target_type='RESTORE', tenant_id=prefix + '-other-tenant')
    wire.management(wrong_tenant, manage, expected=409)
    stale_restore = dict(saved['RESTORE'][0], event_id=prefix + ':stale-restore',
                         idempotency_key=prefix + ':stale-restore', generation=1)
    wire.management(stale_restore, manage, expected=409)
    cases.extend(['same_key_changed_body_rejected', 'old_replay_keeps_latest_STATUS',
                  'cross_product_and_tenant_rejected', 'generation_rollback_rejected'])
    ids.update(last_receipt=current, cases=cases, phase='verified_waiting_cleanup')
    write_manifest(path, ids)
    return cases


def cleanup(m, protocol, ids, path):
    """Close exactly this QA binding before deleting its client and platform roots."""
    from sqlalchemy import delete
    check(environment(m) == ids['environment'], 'Cleanup database does not match the fixture manifest')
    wire = Wire(ids, seconds=45)
    try:
        with session(m) as db:
            _, _, oauth = own_rows(m, db, ids, lock=True)
            exists = oauth is not None
            if exists:
                # Only this generated client can be reactivated for interrupted cleanup.
                oauth.status = 'active'
                db.commit()
        if exists and ids['targets']:
            token = wire.token(m, 'resource.manage')
            generations = {}
            for payload in ids['targets']:
                target = owned_target(protocol, ids, payload)
                generations[target['generation']] = target
            actual = None
            identity_conflict = False
            for generation in sorted(generations, reverse=True):
                target = generations[generation]
                response = wire.request('POST', ids['mock_url'] + '/management',
                    json=dict(target, type='STATUS', target_type=target['type']),
                    headers={'Authorization': 'Bearer ' + token})
                if response.status_code == 409:
                    identity_conflict = True
                    continue
                if response.status_code == 404:
                    continue
                check(response.status_code == 200, 'QA binding cleanup status lookup failed')
                actual = response.json()
                actual_target = owned_target(protocol, ids, {key: value for key, value in actual.items()
                    if key in protocol.ROOT | protocol.OPTIONAL})
                verify(protocol, actual, actual_target)
                break
            check(actual is not None or not identity_conflict,
                  'Could not resolve the QA binding generation during cleanup')
            if actual is not None and actual['status'] != 'closed':
                target = dict(actual_target, type='CLOSE', periods=[],
                    event_id=ids['run_id'] + ':cleanup:' + str(actual_target['generation']),
                    idempotency_key=ids['run_id'] + ':cleanup:' + str(actual_target['generation']))
                closed = verify(protocol, wire.management(target, token), target)
                check(wire.management(dict(target, type='STATUS', target_type='CLOSE'), token) == closed,
                      'QA binding did not remain closed after cleanup')
        with session(m) as db:
            enterprise, product, oauth = own_rows(m, db, ids, lock=True)
            if oauth is not None:
                db.execute(delete(m.OAuthRevokedToken).where(m.OAuthRevokedToken.client_id == oauth.id))
                db.execute(delete(m.OAuthClient).where(m.OAuthClient.id == oauth.id))
                db.execute(delete(m.Product).where(m.Product.id == product.id))
                db.execute(delete(m.Enterprise).where(m.Enterprise.id == enterprise.id))
                db.commit()
        check(fingerprint(m, ids) == ids['baseline'], 'Historical business or credential fingerprint changed')
        ids['phase'] = 'cleaned'
        write_manifest(path, ids)
        Path(path).unlink()
    except BaseException:
        # Revoke every issued token for this client without touching another client.
        with session(m) as db:
            _, _, oauth = own_rows(m, db, ids, lock=True)
            if oauth is not None:
                oauth.status = 'inactive'
                db.commit()
        raise
    finally:
        wire.close()


def interrupted(signum, frame):
    raise ProbeFailure('Probe interrupted')


def main():
    global STAGE
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--run', action='store_true')
    mode.add_argument('--resume', action='store_true')
    mode.add_argument('--cleanup', action='store_true')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--hold-for-restart', action='store_true')
    parser.add_argument('--platform-url', default=PLATFORM_URL)
    parser.add_argument('--mock-url', default=MOCK_URL)
    args = parser.parse_args()
    check(not args.hold_for_restart or args.run, '--hold-for-restart requires --run')
    check(args.run or args.manifest is not None, '--resume/--cleanup require --manifest')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    m, protocol = load_runtime()
    if args.run:
        ids = dict(roots(), schema_version=1, phase='preflight', targets=[],
            platform_url=trusted_url(args.platform_url, 'platform'), mock_url=trusted_url(args.mock_url, 'mock'))
        path = args.manifest or Path('/tmp') / (ids['run_id'] + '.json')
        ids.update(environment=environment(m), baseline=fingerprint(m, ids))
        with session(m) as db:
            check(all(row is None for row in own_rows(m, db, ids)), 'Generated fixture roots already exist')
        wire = Wire(ids)
        health = wire.request('GET', ids['mock_url'] + '/health')
        check(health.status_code == 200 and health.json().get('service') == 'market-mock-api',
              'Target is not the deployed mock API health endpoint')
        write_manifest(path, ids, create=True)
        print(json.dumps({'status': 'prepared', 'manifest': str(path), 'run_id': ids['run_id']}, sort_keys=True), flush=True)
    else:
        path = args.manifest
        ids = read_manifest(path)
        check(ids['environment'] == environment(m), 'Manifest belongs to a different runtime database')
        wire = Wire(ids)
    held = False
    result = None
    try:
        if args.cleanup:
            result = dict(status='passed', mode='cleanup')
        elif args.run:
            STAGE = 'fixture-create'
            create_fixture(m, ids)
            cases = exercise(m, protocol, ids, path, wire)
            result = dict(status='passed', cases=cases, transport='actual platform OAuth and mock HTTP',
                restart_persistence='not independently tested', historical_fingerprints='unchanged')
            if args.hold_for_restart:
                check(fingerprint(m, ids) == ids['baseline'], 'Historical state changed during probe')
                ids['phase'] = 'awaiting_external_restart'
                write_manifest(path, ids)
                held = True
                result.update(status='awaiting_external_restart', manifest=str(path),
                    cleanup='deferred; use --resume or --cleanup', qa_client='retained isolated fixture only')
        else:
            STAGE = 'resume-after-operator-restart'
            check(ids.get('phase') == 'awaiting_external_restart' and ids.get('last_receipt'),
                  'Manifest is not awaiting restart verification')
            token = wire.token(m, 'resource.manage')
            target = owned_target(protocol, ids, ids['targets'][-1])
            replay = verify(protocol, wire.management(target, token), target)
            check(replay == ids['last_receipt'], 'Saved receipt changed or disappeared across the external restart boundary')
            check(wire.management(dict(target, type='STATUS', target_type=target['type']), token) == replay,
                  'Actual state changed across the external restart boundary')
            result = dict(status='passed', mode='resume', saved_receipt_replay=True,
                restart_persistence='verified across operator-supplied boundary; restart/PVC not independently attested')
    finally:
        wire.close()
        if not held:
            previous_stage = STAGE
            STAGE = 'cleanup'
            cleanup(m, protocol, ids, path)
            STAGE = previous_stage
    if not held:
        result.update(cleanup='isolated platform roots removed; mock binding closed; audit receipts retained',
                      historical_fingerprints='unchanged')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except ProbeFailure as exc:
        print(json.dumps({'status': 'failed', 'stage': STAGE, 'check': str(exc),
                          'recovery': '--cleanup --manifest <private manifest path>'}, sort_keys=True))
        raise SystemExit(1)
    except Exception as exc:
        # Never print transport responses, SQL parameters, bearer tokens, or secrets.
        print(json.dumps({'status': 'failed', 'stage': STAGE, 'error_type': type(exc).__name__,
                          'recovery': '--cleanup --manifest <private manifest path>'}, sort_keys=True))
        raise SystemExit(1)
