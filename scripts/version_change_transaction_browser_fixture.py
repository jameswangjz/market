"""Create isolated real-route orders for version-change browser acceptance.

The fixture orders are paid and delivered through acceptance service endpoints.
The browser runner then exercises version changes through the actual console UI.
Its bearer token is issued directly for QA; it is not a password-login claim.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    'version_change_saga_runtime_acceptance', HERE / 'version_change_saga_runtime_acceptance.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def prepare_fixture(base_url, deployment_tag, manifest_path=None):
    from fastapi.testclient import TestClient
    from sqlalchemy import event, select

    site = urlsplit(base_url)
    if site.scheme not in {'http', 'https'} or not site.hostname or site.path not in {'', '/'} or site.query or site.fragment or site.username or site.password:
        raise helper.AcceptanceFailure('Base URL must be an HTTP(S) origin')
    if not deployment_tag or len(deployment_tag) > 128:
        raise helper.AcceptanceFailure('Deployment tag is required')
    base_url = f'{site.scheme}://{site.netloc}'
    m = helper.load_main()
    ids = helper.fixture_ids()
    manifest = ids
    manifest['environment'] = helper.environment_fingerprint(m)
    helper.preflight(m, manifest)
    manifest['authorized'] = True
    path = Path(manifest_path) if manifest_path else Path('/tmp') / (ids['run_id'] + '-browser.json')
    if path.parent.resolve() != Path('/tmp') or path.name != ids['run_id'] + '-browser.json':
        raise helper.AcceptanceFailure('Manifest must use its exact random QA path under /tmp')
    fixture_path = path.with_suffix('.browser.json')
    if path.exists() or fixture_path.exists():
        raise helper.AcceptanceFailure('Manifest path already exists; refusing overwrite')
    helper.write_manifest(path, manifest, create=True)
    client = None
    listeners = []
    redis = None
    try:
        tokens = helper.prepare(m, ids)
        client = TestClient(m.app)
        published = {}
        for kind in ('api', 'model'):
            for side in ('source', 'target'):
                version_id = ids['versions'][kind][side]
                route_key = ids['routes'][kind][side]
                helper.save_external(path, manifest, kind, side, route_key=route_key)
                payload = {'upstream_url': 'http://market-mock-api:8300', 'route_key': route_key,
                    'version': side, 'upstream_auth_mode': 'oauth2', 'upstream_scope': 'resource.invoke',
                    'rate_limit_per_minute': 100, 'daily_quota': 10000,
                    'monthly_quota': 100000, 'health_path': '/health'}
                helper.request(client, tokens['provider'], 'PUT',
                    f"/products/{ids['products'][kind]}/versions/{version_id}/integration", body=payload)
                with m.SessionLocal() as db:
                    route = db.scalar(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.route_key == route_key))
                    oauth = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == ids['products'][kind]))
                    helper.check(route is not None and oauth is not None, 'Fixture route or OAuth client missing')
                    route.upstream_client_id, route.upstream_client_secret = oauth.client_id, oauth.client_secret
                    db.commit()
                verified = helper.request(client, tokens['provider'], 'POST',
                    f"/products/{ids['products'][kind]}/versions/{version_id}/integration/verify")
                helper.check(verified.get('status') == 'verified', 'Fixture gateway verification failed')
                with m.SessionLocal() as db:
                    route = db.scalar(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.route_key == route_key))
                    helper.check(route is not None and route.status == 'active', 'Fixture route is not active')
                    published[(kind, side)] = route.id
                remote = helper.apisix_admin(m, 'GET', '/routes/' + route_key)
                helper.check(remote.get('labels', {}).get('market_product_id') == ids['products'][kind],
                             'Published APISIX route product mismatch')

        journal = helper.journal_consumer(m, path, manifest)
        held_orders = set()
        def hold_delivery(db, *_):
            for task in db.new.union(db.dirty):
                if isinstance(task, m.DeliveryTask) and task.order_id in held_orders and task.status in {'in_delivery', 'retrying'}:
                    task.next_retry_at = m.now() + __import__('datetime').timedelta(hours=1)
        for name, callback in (('after_flush', journal), ('before_flush', hold_delivery)):
            event.listen(m.SessionLocal.class_, name, callback)
            listeners.append((name, callback))
        redis = m.apisix_policy_redis()
        orders = {}
        for kind in ('api', 'model'):
            oid = helper.request(client, tokens['buyer'], 'POST', '/orders', body={
                'product_id': ids['products'][kind], 'product_version_id': ids['versions'][kind]['source'],
                'buyer_enterprise_id': ids['enterprises']['buyer'], 'subscription_months': 3})['id']
            held_orders.add(oid)
            helper.request(client, tokens['finance'], 'POST', f'/orders/{oid}/transition', body={
                'action': 'confirm_payment', 'reason': 'qa-vcs browser fixture'})
            with m.SessionLocal() as db:
                task = db.scalar(select(m.DeliveryTask).where(m.DeliveryTask.order_id == oid))
                helper.check(task is not None, 'Fixture delivery task missing')
                task_id = task.id
            helper.request(client, tokens['delivery'], 'POST', f'/delivery-tasks/{task_id}/process',
                           body={'success': True})
            with m.SessionLocal() as db:
                order = db.get(m.Order, oid)
                helper.check(order is not None and order.delivery_status in {'accepted', 'completed'},
                             'Fixture initial delivery did not complete')
                credential = db.scalar(select(m.ApiCredential).where(
                    m.ApiCredential.enterprise_id == ids['enterprises']['buyer'],
                    m.ApiCredential.route_id == published[(kind, 'source')], m.ApiCredential.status == 'active'))
                helper.check(credential is not None and credential.order_id == '',
                             'Fixture shared credential missing')
                credential_id = credential.id
                manifest['database_ids'].setdefault('orders', {})[kind] = oid
                manifest['database_ids'].setdefault('credentials', {})[kind] = credential_id
                manifest['database_ids'].setdefault('source_routes', {})[kind] = published[(kind, 'source')]
                orders[kind] = {'id': oid, 'orderNo': order.order_no,
                    'sourceVersionId': ids['versions'][kind]['source'],
                    'targetVersionId': ids['versions'][kind]['target'],
                    'sourceRoute': ids['routes'][kind]['source'], 'targetRoute': ids['routes'][kind]['target']}
                db.commit()
            helper.write_manifest(path, manifest)
        expected_credential_ids = set(manifest['database_ids']['credentials'].values())
        helper.check(expected_credential_ids == {item['id'] for item in manifest['consumers']} and
                     all(set(item['allowed_routes']) == set(ids['routes'][kind].values())
                         for kind in ('api', 'model')
                         for item in manifest['consumers'] if item['id'] == manifest['database_ids']['credentials'][kind]),
                     'Consumer journal is incomplete or outside exact product routes')
        fixture = {'baseUrl': base_url.rstrip('/'), 'deploymentTag': deployment_tag,
            'runId': ids['run_id'], 'buyerAdminToken': tokens['buyer'], 'orders': orders,
            'manifest': str(path), 'bootstrap': 'direct QA bearer issuance; no password-login claim'}
        fd = os.open(fixture_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(fixture, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(fixture_path, 0o600)
        return {'status': 'prepared', 'fixture': str(fixture_path), 'manifest': str(path),
            'fixture_mode': '0600', 'manifest_mode': '0600', 'orders': orders,
            'bootstrap': fixture['bootstrap']}
    finally:
        for name, callback in reversed(listeners):
            event.remove(m.SessionLocal.class_, name, callback)
        if redis:
            redis.close()
        if client:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--deployment-tag', required=True)
    parser.add_argument('--manifest')
    args = parser.parse_args()
    result = prepare_fixture(args.base_url, args.deployment_tag, args.manifest)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except helper.AcceptanceFailure as exc:
        print(json.dumps({'status': 'failed', 'check': str(exc)}, sort_keys=True))
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'status': 'failed'} | helper.safe_exception_metadata(exc), sort_keys=True))
        raise SystemExit(1)
