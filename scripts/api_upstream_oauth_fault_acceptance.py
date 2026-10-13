"""Native APISIX OAuth cache faults using a caller-owned runtime fixture.

Called by the API security runner after its baseline worker transactions.
Only exact generated product cache keys and OAuth clients are mutated.
"""
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import time

import httpx
from sqlalchemy import delete, select

import version_change_saga_runtime_acceptance as base


def run_cases(m, ids, path, client, tokens, kind):
    product_id = ids['products'][kind]
    credential_id = ids['database_ids']['credentials'][kind]
    order_id = ids['database_ids']['orders'][kind]
    download = client.get(f'/api/orders/{order_id}/api-credentials/{credential_id}/download',
        headers={'Authorization': 'Bearer ' + tokens['buyer']})
    base.check(download.status_code == 200, 'Owned caller credential unavailable')
    raw_key = download.json()['api_key']
    with m.SessionLocal() as db:
        oauth = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == product_id))
        credential = db.get(m.ApiCredential, credential_id)
        base.check(oauth is not None and credential.enterprise_id == ids['enterprises']['buyer'],
                   'Upstream OAuth fixture ownership mismatch')
        row_id, client_id, secret, original_status = oauth.id, oauth.client_id, oauth.client_secret, oauth.status
        consumer = credential.apisix_consumer_name
    redis = m.apisix_policy_redis()
    key = 'market:apisix:oauth:' + product_id
    base.check(key in base.oauth_cache_keys(ids), 'Refusing unowned OAuth cache key')
    gateway = os.getenv('QA_VCS_GATEWAY_URL', 'http://market-apisix:9080').rstrip('/')
    url = gateway + '/gateway/' + ids['routes'][kind]['source'] + '/v1/echo'
    metadata = redis.hgetall('market:apisix:credential:' + consumer)
    active = [p for p in json.loads(metadata['subscription_periods'])
              if p['start_at'] <= int(redis.time()[0]) < p['end_at']]
    base.check(len(active) == 1, 'Owned credential has no unique active quota period')
    scope, period = metadata['quota_scope'], active[0]['period_key']
    revoked_hash = None
    def call(endpoint='echo', timeout=45):
        base.journal_counter(m, path, ids, redis, scope, period, scope, kind)
        return httpx.get(url.rsplit('/', 1)[0] + '/' + endpoint,
                         headers={'X-API-Key': raw_key}, timeout=timeout,
                         trust_env=False, follow_redirects=False)
    def restore_client():
        with m.SessionLocal() as db:
            row = db.get(m.OAuthClient, row_id)
            base.check(row is not None and row.client_id == client_id and row.product_id == product_id,
                       'Generated OAuth client ownership changed')
            row.status = original_status
            db.commit()
    outcomes = {}
    try:
        redis.delete(key)
        base.check(call().status_code == 200, 'Native gateway baseline OAuth call failed')
        cached = redis.get(key)
        base.check(bool(cached), 'Native OAuth gateway did not populate Redis cache')
        with m.SessionLocal() as db:
            db.get(m.OAuthClient, row_id).status = 'inactive'
            db.commit()
        base.check(call().status_code == 401, 'Warm cached token bypassed inactive OAuth client')
        outcomes['inactive_client_warm_cache'] = 401
        redis.delete(key)
        base.check(call().status_code == 502, 'Cold cache minted a token for inactive OAuth client')
        outcomes['inactive_client_cold_cache'] = 502
        restore_client()
        base.check(call().status_code == 200, 'Native gateway did not recover after client restoration')
        token = redis.get(key)
        revoked_hash = hashlib.sha256(token.encode()).hexdigest()
        response = httpx.post(m.platform_oauth_issuer().rstrip('/') + '/oauth/revoke',
            data={'client_id': client_id, 'client_secret': secret, 'token': token}, timeout=10,
            trust_env=False, follow_redirects=False)
        base.check(response.status_code == 200, 'Owned native gateway token revocation failed')
        base.check(call().status_code == 401, 'Native cached revoked token reached mock upstream')
        outcomes['revoked_token_warm_cache'] = 401
        # Log-phase timers are asynchronous; observe exact-token eviction before retry.
        deadline = time.monotonic() + 5
        while redis.get(key) == token and time.monotonic() < deadline:
            time.sleep(.05)
        base.check(redis.get(key) != token, 'Native gateway retained the rejected token')
        repeated = call().status_code
        base.check(repeated == 200, 'Next native gateway request did not recover automatically')
        outcomes['revoked_token_next_call_after_auto_eviction'] = repeated
        redis.delete(key)
        base.check(call().status_code == 200, 'Explicit owned-cache eviction did not restore gateway access')
        outcomes['recovery_after_owned_cache_eviction'] = 200
        issued = int(m.now().timestamp())
        expired = m.jwt.encode({'iss': m.platform_oauth_issuer(), 'sub': client_id,
            'client_id': client_id, 'product_id': product_id, 'scope': 'resource.invoke',
            'aud': 'market-resource', 'iat': issued - 120, 'exp': issued - 60,
            'jti': ids['run_id'] + '-expired-' + kind}, m.JWT_SECRET, algorithm='HS256')
        redis.setex(key, 1, expired)
        base.check(call().status_code == 401, 'Expired gateway token reached mock upstream')
        outcomes['expired_token_injected_in_owned_cache'] = 401
        time.sleep(1.2)
        base.check(call().status_code == 200, 'Redis TTL expiry did not reacquire a working gateway token')
        outcomes['redis_token_ttl_expiry_reacquisition'] = 200
        base.check(call('error').status_code == 500, 'Native gateway did not propagate owned upstream failure')
        outcomes['upstream_server_error'] = 500
        base.check(call('slow').status_code == 504, 'Native gateway did not enforce upstream read timeout')
        outcomes['native_gateway_upstream_read_timeout'] = 504
        base.check(call().status_code == 200, 'Gateway remained unavailable after slow/error requests')
        outcomes['access_after_upstream_failure'] = 200
        plugin = Path('/tmp/market-gateway-oauth.lua').read_text()
        match = re.search(r'local eviction_script = \[\[(.*?)\]\]', plugin, re.S)
        base.check(match is not None, 'Deployed plugin eviction script fixture is unavailable')
        cas = match.group(1)
        failed_token, replacement = redis.get(key), 'qa-concurrent-replacement-' + kind
        redis.setex(key, 60, replacement)
        base.check(redis.eval(cas, 1, key, failed_token) == 0 and redis.get(key) == replacement,
                   'Atomic Redis eviction removed a concurrent replacement token')
        base.check(redis.eval(cas, 1, key, replacement) == 1 and redis.get(key) is None,
                   'Atomic Redis eviction could not remove the exact failed token')
        outcomes['atomic_new_token_preserved'] = True
        outcomes['atomic_exact_token_deleted'] = True
        base.check(call().status_code == 200, 'Gateway did not recover after CAS primitive check')
        return {'status': 'passed', 'scope': 'native APISIX -> real OAuth -> mock API upstream',
                'kind': kind, 'cases': outcomes,
                'recovery': 'resource 401 evicts matching token asynchronously; next request reacquires; no replay'}
    finally:
        restore_client()
        redis.delete(key)
        redis.close()
        if revoked_hash:
            with m.SessionLocal() as db:
                db.execute(delete(m.OAuthRevokedToken).where(
                    m.OAuthRevokedToken.client_id == row_id,
                    m.OAuthRevokedToken.token_hash == revoked_hash))
                db.execute(delete(m.AuditLog).where(m.AuditLog.actor == client_id,
                    m.AuditLog.action == 'revoke_oauth_token',
                    m.AuditLog.target_type == 'oauth_client', m.AuditLog.target_id == row_id))
                db.commit()
