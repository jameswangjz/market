"""Scoped deployed OIDC browser fixtures; run inside the market-api container.

--prepare --manifest /tmp/qa.json --runtime-tag TAG
--cleanup --manifest /tmp/qa.json
--cleanup-rp --manifest /tmp/qa.json --rp-store /data/mock-saas.sqlite3
--set-owned-state account-disabled --manifest /tmp/qa.json
--set-owned-state restore --manifest /tmp/qa.json

For each of account-disabled, subscription-expired, membership-disabled and
membership-removed: set state in-pod, run Windows driver -Scenario STATE with
the existing private browser fixture and a fresh OutputDir, then restore in-pod
even after browser failure. These are fresh-entry checks, not existing-session
invalidation checks (the baseline covers existing-session grant revocation).
For the public callback use prepare --rp-public http://192.168.10.10:30083
and driver -DirectRP -NoProxy. Baseline evidence verification remains separate;
negative scenario JSON must never stand in for the baseline browser artifact.

Run cleanup-rp inside the mock container BEFORE PostgreSQL cleanup. Copy the
0600 manifest and .oidc.json control file there. Copy .browser.json privately
to Windows for oidc_browser_cdp.ps1. Never publish those three private files.
MOCK_SAAS_ADMIN_SECRET must be injected into this process from the existing
RP secret; this script never configures infrastructure or runs deployments.
Paid/delivered terms are synthetic; machine OPEN and browser HTTP are real.
"""

import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
import hashlib
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from unittest.mock import patch
from urllib.parse import urlsplit

import version_change_quote_acceptance as scope

OWNED_STATES = ('account-disabled', 'subscription-expired', 'membership-disabled', 'membership-removed')


def set_owned_state(args):
    """In-pod only. Restore between scenarios; no HTTP mutation endpoint."""
    scope.check(args.set_owned_state in (*OWNED_STATES, 'restore'), 'Unknown owned state')
    ids = scope.read_scope(args.manifest)
    scope.check(ids['run_id'].startswith('qa-r3-sso-'), 'SSO fixture required')
    owned = read_control(args.manifest, ids)
    scope.check(not owned['rp_cleanup_verified'], 'Fixture already cleaned')
    from sqlalchemy import select

    m = scope.load_main()
    with m.SessionLocal() as db:
        scope.owned_rows(m, db, ids)
        user = db.get(m.User, ids['users']['member'], with_for_update=True)
        product = db.get(m.Product, owned['product_id'])
        subscription = db.get(m.SaaSSubscription, owned['subscription_id'], with_for_update=True)
        scope.check(user and user.name == user.id and user.email == user.id + '@example.invalid'
                    and product and product.name == product.id
                    and product.enterprise_id == ids['enterprises'][1]
                    and subscription and subscription.product_id == product.id
                    and subscription.enterprise_id == owned['enterprise_id']
                    and subscription.external_tenant_id == owned['tenant_id'], 'Owned state identity mismatch')
        members = list(db.scalars(select(m.Membership).where(
            m.Membership.user_id == user.id,
            m.Membership.enterprise_id == owned['enterprise_id']).with_for_update()))
        scope.check(len(members) <= 1, 'Ambiguous owned membership')
        member = members[0] if members else None
        journal = owned.get('state_journal')
        if args.set_owned_state == 'restore':
            scope.check(isinstance(journal, dict), 'No owned state journal')
            saved = journal['membership']
            scope.check(saved['user_id'] == user.id and saved['enterprise_id'] == owned['enterprise_id']
                        and (member is None or member.id == saved['id']), 'Restoration ownership mismatch')
            if member is None:
                scope.check(db.get(m.Membership, saved['id']) is None, 'Membership ID occupied')
                member = m.Membership()
                db.add(member)
            for key in m.Membership.__table__.columns.keys():
                value = saved[key]
                if key in ('created_at', 'joined_at') and value is not None:
                    value = datetime.fromisoformat(value)
                setattr(member, key, value)
            user.is_active = journal['is_active']
            subscription.expires_at = datetime.fromisoformat(journal['expires_at'])
        else:
            scope.check(journal is None and member and member.status == 'active' and user.is_active
                        and subscription.expires_at, 'Restore owned state before another scenario')
            # Fail before journaling or mutation if an earlier run revoked access.
            m.applications_access(db, user, subscription.id)
            saved = {c.name: getattr(member, c.name) for c in m.Membership.__table__.columns}
            saved = {k: v.isoformat() if isinstance(v, datetime) else v for k, v in saved.items()}
            owned['state_journal'] = dict(membership=saved, is_active=user.is_active,
                                         expires_at=subscription.expires_at.isoformat(), state=args.set_owned_state)
            # Persist restoration values before committing any database mutation.
            scope.private_json(control_path(args.manifest), owned)
            if args.set_owned_state == 'account-disabled':
                user.is_active = False
            elif args.set_owned_state == 'subscription-expired':
                subscription.expires_at = m.now() - timedelta(days=1)
            elif args.set_owned_state == 'membership-disabled':
                member.status = 'disabled'
            else:
                db.delete(member)
        db.commit()
    if args.set_owned_state == 'restore':
        owned.pop('state_journal')
        scope.private_json(control_path(args.manifest), owned)
    return {'status': 'passed', 'mode': 'set-owned-state', 'state': args.set_owned_state}


def origin(value):
    url = urlsplit(value)
    scope.check(url.scheme in {'http', 'https'} and url.hostname and
                not url.username and not url.password and not url.query and
                not url.fragment and url.path in {'', '/'}, 'Expected HTTP origin')
    return value.rstrip('/')


def private_read(path):
    path = Path(path)
    mode = path.lstat().st_mode
    scope.check(stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600,
                'Private file must be regular and 0600')
    return json.loads(path.read_text())


def control_path(path):
    return Path(path).with_suffix('.oidc.json')


def read_control(path, ids):
    value = private_read(control_path(path))
    scope.check(value['run_id'] == ids['run_id'] and
                value['product_id'] == ids['products']['tenant_access'] and
                value['enterprise_id'] == ids['enterprises'][0] and
                value['tenant_id'] == ids['run_id'] + '-tenant' and
                isinstance(value['subscription_id'], str) and
                re.fullmatch(r'oidc_[0-9a-f]{32}', value['client_id']),
                'RP ownership control mismatch')
    origin(value['rp_base'])
    return value


def request(client, method, url, expected=200, **kwargs):
    response = client.request(method, url, **kwargs)
    # URLs, bodies and exception text can contain protocol credentials.
    scope.check(response.status_code == expected,
                f'HTTP {method} check expected {expected}, got {response.status_code}')
    return response


def prepare(args):
    import httpx
    from sqlalchemy import select

    scope.check(re.fullmatch(r'\d{14}-[0-9a-f]{7,40}', args.runtime_tag or ''),
                'Runtime tag required')
    base, public, rp = map(origin, (args.base_url, args.rp_public, args.rp_base))
    admin_secret = os.getenv('MOCK_SAAS_ADMIN_SECRET', '')
    scope.check(bool(admin_secret), 'MOCK_SAAS_ADMIN_SECRET required from RP secret environment')
    manifest = Path(args.manifest)
    browser = manifest.with_suffix('.browser.json')
    control = control_path(manifest)
    scope.check(all(not p.exists() and not p.is_symlink() for p in (manifest, browser, control)),
                'Private artifact path already exists')
    m = scope.load_main()
    scope.check(hasattr(m, 'OIDCBrowserRequest'), 'Browser SSO runtime not deployed')
    ids = scope.fixture_ids('qa-r3-sso-' + secrets.token_hex(6))
    scope.preflight(m, ids)
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
        discovery = request(client, 'GET', base + '/.well-known/openid-configuration').json()
        scope.check(discovery['issuer'] == base, 'Public issuer mismatch')
        request(client, 'GET', rp + '/health')
        # Empty invalid configuration cannot create a client, but proves the route exists.
        request(client, 'POST', rp + '/_test/oidc/clients', expected=422,
                headers={'X-Mock-Admin-Secret': admin_secret}, json={})
        scope.private_json(manifest, ids, create=True)
        retained = False
        try:
            with ExitStack() as guards:
                for name in ('apisix_admin_request', 'publish_apisix_route',
                             'execute_saas_operation', 'validated_http_request'):
                    guards.enter_context(patch.object(m, name, side_effect=scope.AcceptanceFailure(
                        'Unexpected provisioning during synthetic fixture creation')))
                # The existing helper iterates products; keep its identities and cleanup
                # contract intact while constructing only the SaaS product.
                narrow = {**ids, 'products': {'tenant_access': ids['products']['tenant_access']}}
                orders, tokens = scope.prepare(m, narrow, m.now() - timedelta(days=30))
            password = secrets.token_urlsafe(32)
            pid, eid = ids['products']['tenant_access'], ids['enterprises'][0]
            with m.SessionLocal() as db:
                for uid in ids['users'].values():
                    user = db.get(m.User, uid)
                    user.password_hash = m.hash_password(password)
                    user.email_verified = True
                product = db.get(m.Product, pid)
                product.oidc_redirect_uris_json = json.dumps([public + '/sso/callback'])
                subscription = db.get(m.SaaSSubscription,
                                      db.get(m.Order, orders['tenant_access'][0]).subscription_id)
                entitlement = m.subscription_terms['current_entitlement'](db, eid, pid)
                scope.check(entitlement is not None, 'No current synthetic paid entitlement')
                subscription.expires_at = entitlement['expires_at']
                sid = subscription.id
                config = db.scalar(select(m.SaaSIntegrationConfig).where(
                    m.SaaSIntegrationConfig.product_id == pid))
                config.client_secret = secrets.token_urlsafe(32)
                machine = m.ensure_oauth_client(db, product, 'saas.tenant saas.user saas.department')
                machine_id, machine_secret = machine.client_id, machine.client_secret
                db.commit()
            def api(method, path, role, **kwargs):
                return request(client, method, base + path,
                               headers={'Authorization': 'Bearer ' + tokens[role]}, **kwargs)
            oidc = api('POST', f'/api/products/{pid}/oidc-client', 'provider').json()
            cid = oidc['client_id']
            scope.check(bool(oidc['client_secret']), 'OIDC one-time secret missing')
            tenant = ids['run_id'] + '-tenant'
            owned = dict(run_id=ids['run_id'], product_id=pid, enterprise_id=eid,
                         subscription_id=sid, tenant_id=tenant, client_id=cid, rp_base=rp,
                         rp_cleanup_verified=False, runtime_tag=args.runtime_tag)
            # Journal exact identities before the first external mutation.
            scope.private_json(control, owned, create=True)
            access = request(client, 'POST', base + '/oauth/token', data={
                'grant_type': 'client_credentials', 'client_id': machine_id,
                'client_secret': machine_secret, 'scope': 'saas.tenant'}).json()['access_token']
            active = request(client, 'POST', base + '/oauth/introspect',
                             data={'token': access}).json()
            scope.check(active.get('active') is True, 'Machine token not active')
            opened = request(client, 'POST', rp + '/isv.php',
                headers={'Authorization': 'Bearer ' + access}, json={
                    'type': 'OPEN', 'tenant_id': tenant, 'product_id': pid,
                    'subscription_id': sid, 'enterprise_id': eid,
                    'enterprise_name': eid, 'tenant_name': eid, 'version': 'source'}).json()
            scope.check(opened.get('ret') == 0 and opened.get('tenant_id') == tenant,
                        'Real mock machine OPEN failed')
            request(client, 'POST', rp + '/_test/oidc/clients', expected=201,
                headers={'X-Mock-Admin-Secret': admin_secret}, json={
                    'client_id': cid, 'client_secret': oidc['client_secret'],
                    'redirect_uri': public + '/sso/callback', 'product_id': pid,
                    'issuer': base, 'product_name': pid})
            with m.SessionLocal() as db:
                db.get(m.Product, pid).application_url = public + '/sso/apps/' + cid
                db.get(m.SaaSSubscription, sid).external_tenant_id = tenant
                db.commit()
                scope.save_owned(m, db, ids, manifest, scope.owned_rows(m, db, ids))
            api('GET', f'/api/applications/{sid}/members', 'admin')
            api('POST', f'/api/applications/{sid}/grants', 'admin',
                json={'user_id': ids['users']['member'], 'granted': True})
            listing = api('GET', '/api/applications', 'member').json()['items']
            scope.check(any(i['subscription_id'] == sid and i['available'] for i in listing),
                        'Fixture application not available')
            launch = api('POST', f'/api/applications/{sid}/launch', 'member').json()['launch_url']
            scope.check(urlsplit(launch).netloc == urlsplit(public).netloc and
                        'token' not in launch.lower(), 'Unsafe application launch URL')
            fixture = dict(baseUrl=base, rpPublic=public, deploymentTag=args.runtime_tag,
                runId=ids['run_id'], productId=pid, enterpriseId=eid,
                otherEnterpriseId=ids['enterprises'][2], subscriptionId=sid, clientId=cid,
                launchUrl=launch, password=password,
                memberLogin=ids['users']['member'] + '@example.invalid',
                adminLogin=ids['users']['admin'] + '@example.invalid',
                memberUserId=ids['users']['member'], ownedOrderIds=orders['tenant_access'])
            scope.private_json(browser, fixture, create=True)
            retained = True
            return {'status': 'prepared', 'manifest': str(manifest),
                    'browser_fixture': str(browser), 'private_mode': '0600',
                    'runtime_tag': args.runtime_tag, 'machine_open': 'real HTTP with platform token/introspection',
                    'payment_delivery': 'synthetic helper fixtures', 'cleanup': 'required: RP then PostgreSQL'}
        finally:
            if not retained:
                # Preserve exact ownership on failure; never hide a partial run by
                # deleting rows before the operator can inspect its journal.
                with m.SessionLocal() as db:
                    scope.save_owned(m, db, ids, manifest, scope.owned_rows(m, db, ids))


def cleanup_rp(args):
    ids = scope.read_scope(args.manifest)
    owned = read_control(args.manifest, ids)
    store = Path(args.rp_store)
    scope.check(store.is_absolute() and stat.S_ISREG(store.lstat().st_mode) and
                stat.S_IMODE(store.lstat().st_mode) == 0o600, 'RP SQLite must be regular 0600')
    scope.check(str(store) == os.getenv('MOCK_SAAS_DB_PATH', '/data/mock-saas.sqlite3'),
                'RP store does not match configured store')
    with sqlite3.connect(store.as_uri() + '?mode=rw', uri=True, timeout=15) as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT config FROM clients WHERE client_id=?', (owned['client_id'],)).fetchone()
        if row:
            scope.check(json.loads(row[0])['product_id'] == owned['product_id'],
                        'Foreign RP client; cleanup refused')
        row = db.execute('SELECT payload FROM tenants WHERE tenant_id=?', (owned['tenant_id'],)).fetchone()
        if row:
            payload = json.loads(row[0])
            scope.check(all(payload.get(k) == owned[k] for k in
                            ('product_id', 'subscription_id', 'enterprise_id', 'tenant_id')),
                        'Foreign RP tenant; cleanup refused')
        for table in ('sessions', 'mappings'):
            columns = 'identity' if table == 'sessions' else 'sub,tenant_id,enterprise_id,subscription_id'
            for row in db.execute(f'SELECT {columns} FROM {table} WHERE client_id=?', (owned['client_id'],)):
                identity = json.loads(row[0]) if table == 'sessions' else dict(zip(
                    ('sub', 'tenant_id', 'enterprise_id', 'subscription_id'), row))
                scope.check(all(identity.get(k) == owned[k] for k in
                                ('tenant_id', 'enterprise_id', 'subscription_id')) and
                            identity.get('sub') in ids['users'].values(),
                            'Foreign RP identity; cleanup refused')
        for (uid,) in db.execute('SELECT user_id FROM users WHERE tenant_id=?', (owned['tenant_id'],)):
            scope.check(uid in ids['users'].values(), 'Foreign RP user; cleanup refused')
        scope.check(db.execute('SELECT COUNT(*) FROM departments WHERE tenant_id=?',
                              (owned['tenant_id'],)).fetchone()[0] == 0,
                    'Unexpected RP departments; cleanup refused')
        db.execute('DELETE FROM clients WHERE client_id=?', (owned['client_id'],))
        for table in ('users', 'departments', 'tenants'):
            db.execute(f'DELETE FROM {table} WHERE tenant_id=?', (owned['tenant_id'],))
        for table in ('clients', 'flows', 'mappings', 'sessions'):
            scope.check(db.execute(f'SELECT COUNT(*) FROM {table} WHERE client_id=?',
                                  (owned['client_id'],)).fetchone()[0] == 0, 'RP client child remains')
        for table in ('users', 'departments', 'tenants'):
            scope.check(db.execute(f'SELECT COUNT(*) FROM {table} WHERE tenant_id=?',
                                  (owned['tenant_id'],)).fetchone()[0] == 0, 'RP tenant child remains')
    owned['rp_cleanup_verified'] = True
    scope.private_json(control_path(args.manifest), owned)
    return {'status': 'passed', 'mode': 'cleanup-rp', 'cleanup': 'exact RP rows and children verified absent'}


def cleanup(args):
    ids = scope.read_scope(args.manifest)
    control = control_path(args.manifest)
    scope.check(control.exists() and not control.is_symlink(),
                'RP ownership journal required; PostgreSQL cleanup refused')
    owned = read_control(args.manifest, ids)
    scope.check(owned['rp_cleanup_verified'] is True,
                'Run cleanup-rp and copy verified control back before PostgreSQL cleanup')
    evidence = verify_browser(args.browser_result, ids, owned) if args.browser_result else None
    m = scope.load_main()
    with m.SessionLocal() as db:
        subscription = db.get(m.SaaSSubscription, owned['subscription_id'])
        scope.check(subscription is None or (subscription.product_id == owned['product_id'] and
                    subscription.enterprise_id == owned['enterprise_id']), 'Subscription ownership mismatch')
    scope.cleanup(m, ids, Path(args.manifest))
    browser = Path(args.manifest).with_suffix('.browser.json')
    if browser.exists():
        private_read(browser)
        browser.unlink()
    if evidence:
        evidence['cleanup'] = 'verified'
        if args.evidence_output:
            output = Path(args.evidence_output)
            scope.check(not output.exists() and not output.is_symlink(), 'Evidence output exists')
            scope.private_json(output, evidence, create=True)
        return evidence
    return {'status': 'passed', 'mode': 'cleanup', 'cleanup': 'exact PostgreSQL/FK rows absent; private login file removed'}


def repair_login(args):
    ids = scope.read_scope(args.manifest)
    owned = read_control(args.manifest, ids)
    scope.check(not owned['rp_cleanup_verified'], 'Fixture already cleaned')
    m = scope.load_main()
    with m.SessionLocal() as db:
        scope.owned_rows(m, db, ids)
        users = [db.get(m.User, uid) for uid in ids['users'].values()]
        scope.check(all(user and user.name == user.id and user.email == user.id + '@example.invalid'
                        for user in users), 'Owned synthetic login users missing')
        for user in users:
            user.email_verified = True
        db.commit()
    return {'status': 'passed', 'mode': 'repair-login', 'owned_synthetic_users': len(users)}


def verify_browser(path, ids, owned):
    path = Path(path)
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    fields = ('browser_sso', 'mock_session', 'application_access', 'negative_cases')
    scope.check(value.get('status') == 'passed' and all(value.get(k) == 'passed' for k in fields)
                and value.get('database') == 'real PostgreSQL' and value.get('blocked_requests') == 0,
                'PowerShell browser artifact did not pass')
    scope.check(value.get('run_id') == ids['run_id'] and value.get('runtime_tag') == owned['runtime_tag'],
                'Browser artifact fixture/runtime mismatch')
    required = {'desktop_login_consent_rp_session', 'mobile_login_consent_rp_session',
                'reject_no_rp_session', 'cross_enterprise_denied', 'applications_listing_members_ui',
                'applications_launch_ui', 'grant_revoke_rp_session_and_listing'}
    scope.check(required <= set(value.get('checks', [])), 'Browser artifact checks incomplete')
    shots = ['desktop-consent.png', 'desktop-app.png', 'mobile-consent.png', 'mobile-app.png', 'applications.png']
    if 'mobile-members.png' in value.get('screenshots', []):
        shots.append('mobile-members.png')
    scope.check(value.get('screenshots') == shots, 'Browser screenshots incomplete')
    for name in shots:
        screenshot = path.parent / name
        scope.check(screenshot.is_file() and not screenshot.is_symlink(), 'Screenshot file missing')
        raw = screenshot.read_bytes()
        scope.check(len(raw) > 1000 and raw[:8] == b'\x89PNG\r\n\x1a\n' and
                    hashlib.sha256(raw).hexdigest() == value.get('screenshot_hashes', {}).get(name),
                    'Screenshot signature or digest mismatch')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'status': 'passed', 'database': 'real PostgreSQL', 'cleanup': 'pending',
            'runtime_tag': owned['runtime_tag'], **{k: 'passed' for k in fields},
            'run_id': ids['run_id'], 'browser_artifact_sha256': digest,
            'limits': value['limits']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--prepare', action='store_true')
    modes.add_argument('--cleanup', action='store_true')
    modes.add_argument('--cleanup-rp', action='store_true')
    modes.add_argument('--repair-login', action='store_true', help='Set email verification only for manifest-owned synthetic users')
    modes.add_argument('--set-owned-state', choices=(*OWNED_STATES, 'restore'),
                       help='In-pod: prepare one negative browser scenario; restore before the next')
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--runtime-tag')
    parser.add_argument('--base-url', default='http://192.168.10.10:30080')
    parser.add_argument('--rp-public', default='http://127.0.0.1:30082')
    parser.add_argument('--rp-base', default='http://market-mock-saas:8200')
    parser.add_argument('--rp-store', default='/data/mock-saas.sqlite3')
    parser.add_argument('--browser-result', help='Checked PS browser-results.json with sibling PNGs; cleanup mode only')
    parser.add_argument('--evidence-output', help='Final service evidence; requires --cleanup --browser-result')
    args = parser.parse_args()
    if (args.browser_result and not args.cleanup) or (args.evidence_output and not args.browser_result):
        parser.error('Browser evidence options require --cleanup --browser-result')
    logging.disable(logging.CRITICAL)
    try:
        result = set_owned_state(args) if args.set_owned_state else prepare(args) if args.prepare else cleanup_rp(args) if args.cleanup_rp else repair_login(args) if args.repair_login else cleanup(args)
    except Exception as exc:
        result = {'status': 'failed', 'error_type': type(exc).__name__,
                  'manifest': args.manifest, 'cleanup': 'inspect private journal; retry RP then PG cleanup if created'}
        if isinstance(exc, scope.AcceptanceFailure):
            result['check_failed'] = str(exc)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] in {'passed', 'prepared'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
