"""Real-PG concurrency and negative checks on the exact owned SSO fixture."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import logging
from pathlib import Path

import httpx
from sqlalchemy import inspect, select

import oidc_browser_acceptance as browser
import version_change_quote_acceptance as scope


def run(args):
    ids = scope.read_scope(args.manifest)
    owned = browser.read_control(args.manifest, ids)
    m = scope.load_main()
    with m.SessionLocal() as db:
        scope.owned_rows(m, db, ids)
        indexes = inspect(db.connection()).get_indexes('application_access_grants')
        scope.check(any(index['unique'] and set(index['column_names']) ==
            {'enterprise_id', 'product_id', 'user_id'} for index in indexes), 'Existing-table unique index absent')
        tokens = {role: m.issue_token(db.get(m.User, ids['users'][role])) for role in ('admin', 'member', 'other')}
        grant = db.scalar(select(m.ApplicationAccessGrant).where(m.ApplicationAccessGrant.enterprise_id == owned['enterprise_id'],
            m.ApplicationAccessGrant.product_id == owned['product_id'], m.ApplicationAccessGrant.user_id == ids['users']['member']))
        original_granted = bool(grant and grant.status == 'active')
        member = db.get(m.User, ids['users']['member'])
        original_active = member.is_active
        subscription = db.get(m.SaaSSubscription, owned['subscription_id'])
        scope.check(subscription is not None and
            subscription.enterprise_id == owned['enterprise_id'] and
            subscription.product_id == owned['product_id'] and
            subscription.external_tenant_id == owned['tenant_id'],
            'Subscription ownership mismatch; acceptance refused')
        original_expiry = subscription.expires_at
    base = browser.origin(args.base_url)
    prefix = '/api/applications/' + owned['subscription_id']

    def request(method, path, role='admin', expected=200, **kwargs):
        with httpx.Client(base_url=base, timeout=20, trust_env=False) as client:
            result = client.request(method, path, headers={'Authorization': 'Bearer ' + tokens[role]}, **kwargs)
        scope.check(result.status_code == expected, 'Application permission HTTP status mismatch')
        return result

    try:
        def grant_once(_):
            return request('POST', prefix + '/grants', json={'user_id': ids['users']['member'], 'granted': True}).status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(grant_once, range(16)))
        scope.check(statuses == [200] * 16, 'Concurrent grants failed')
        with m.SessionLocal() as db:
            count = len(db.scalars(select(m.ApplicationAccessGrant).where(
                m.ApplicationAccessGrant.enterprise_id == owned['enterprise_id'],
                m.ApplicationAccessGrant.product_id == owned['product_id'],
                m.ApplicationAccessGrant.user_id == ids['users']['member'])).all())
            scope.check(count == 1, 'Concurrent grants created duplicates')
            member = db.get(m.User, ids['users']['member'])
            member.is_active = False
            db.commit()
        request('POST', prefix + '/launch', 'member', 401)
        request('POST', prefix + '/grants', json={'user_id': ids['users']['member'], 'granted': False})
        request('POST', prefix + '/grants', expected=403, json={'user_id': ids['users']['member'], 'granted': True})
        with m.SessionLocal() as db:
            db.get(m.User, ids['users']['member']).is_active = original_active
            db.commit()
        request('POST', prefix + '/launch', 'member', 403)
        request('GET', prefix + '/members', 'other', 403)
        request('POST', prefix + '/launch', 'other', 403)
        request('POST', prefix + '/grants', json={'user_id': ids['users']['member'], 'granted': True})
        request('POST', prefix + '/launch', 'member')
        with m.SessionLocal() as db:
            db.get(m.SaaSSubscription, owned['subscription_id']).expires_at = m.now() - timedelta(seconds=1)
            db.commit()
        request('POST', prefix + '/launch', 'member', 403)
        with m.SessionLocal() as db:
            db.get(m.SaaSSubscription, owned['subscription_id']).expires_at = original_expiry
            db.commit()
        request('POST', prefix + '/launch', 'member')
        return {'status': 'passed', 'database': 'real PostgreSQL', 'runtime_tag': owned['runtime_tag'],
            'concurrent_grant_requests': 16, 'grant_rows': count,
            'checks': ['existing-table unique index', 'real HTTP grant concurrency',
                'disabled user denied', 'disabled user grant revocation', 'reactivation does not restore revoked grant',
                'cross-enterprise members/launch denied', 'positive launch before expiry',
                'expired subscription launch denied', 'positive launch after expiry restoration'],
            'limits': ['One API replica; not a distributed load test', 'Only exact owned fixture rows modified']}
    finally:
        with m.SessionLocal() as db:
            db.get(m.User, ids['users']['member']).is_active = original_active
            db.get(m.SaaSSubscription, owned['subscription_id']).expires_at = original_expiry
            db.commit()
        request('POST', prefix + '/grants', json={'user_id': ids['users']['member'], 'granted': original_granted})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--base-url', default='http://192.168.10.10:30080')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    result = run(args)
    Path(args.output).write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'concurrent_grant_requests': result['concurrent_grant_requests']}))


if __name__ == '__main__':
    main()
