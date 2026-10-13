"""Prepare and clean an isolated password-login first-purchase browser fixture.

Preparation creates only the buyer, enterprise, API product, versions and
verified gateway routes. Chrome owns the order and simulated payment workflow.
Run --record-consumers after the online worker delivers, then --cleanup.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import tempfile
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    'version_change_saga_runtime_acceptance',
    HERE / 'version_change_saga_runtime_acceptance.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def private_json(path, value, *, create=False):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temporary, path, follow_symlinks=False)
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def prepare(base_url, manifest_path=None):
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    site = urlsplit(base_url)
    helper.check(site.scheme in {'http', 'https'} and site.hostname and
                 site.path in {'', '/'} and not site.query and not site.fragment and
                 not site.username and not site.password, 'Base URL must be an HTTP(S) origin')
    base_url = f'{site.scheme}://{site.netloc}'
    m = helper.load_main()
    ids = helper.fixture_ids()
    buyer_password = secrets.token_urlsafe(30)
    buyer_email = ids['users']['buyer'] + '@example.invalid'
    manifest = ids
    manifest['environment'] = helper.environment_fingerprint(m)
    helper.preflight(m, manifest)
    manifest['authorized'] = True
    manifest_path = Path(manifest_path) if manifest_path else Path('/tmp') / (ids['run_id'] + '-password-first.json')
    fixture_path = manifest_path.with_suffix('.browser.json')
    helper.check(manifest_path.parent.resolve() == Path('/tmp') and
                 manifest_path.name == ids['run_id'] + '-password-first.json',
                 'Manifest must use its exact random QA path under /tmp')
    helper.check(not manifest_path.exists() and not fixture_path.exists(),
                 'Fixture paths already exist; refusing overwrite')
    helper.write_manifest(manifest_path, manifest, create=True)
    client = None
    published = []
    try:
        tokens = helper.prepare(m, ids)
        with m.SessionLocal() as db:
            buyer = db.get(m.User, ids['users']['buyer'])
            buyer.password_hash = m.hash_password(buyer_password)
            buyer.email_verified = True
            buyer.verified_status = 'verified'
            buyer.is_active = True
            buyer.activation_status = 'active'
            db.commit()

        client = TestClient(m.app)
        for side in ('source', 'target'):
            version_id = ids['versions']['api'][side]
            route_key = ids['routes']['api'][side]
            helper.save_external(manifest_path, manifest, 'api', side, route_key=route_key)
            payload = {'upstream_url': 'http://market-mock-api:8300', 'route_key': route_key,
                'version': side, 'upstream_auth_mode': 'oauth2', 'upstream_scope': 'resource.invoke',
                'rate_limit_per_minute': 100, 'daily_quota': 10000,
                'monthly_quota': 100000, 'health_path': '/health'}
            helper.request(client, tokens['provider'], 'PUT',
                f"/products/{ids['products']['api']}/versions/{version_id}/integration", body=payload)
            with m.SessionLocal() as db:
                route = db.scalar(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.route_key == route_key))
                oauth = db.scalar(select(m.OAuthClient).where(m.OAuthClient.product_id == ids['products']['api']))
                helper.check(route is not None and oauth is not None, 'Fixture route or OAuth client missing')
                route.upstream_client_id, route.upstream_client_secret = oauth.client_id, oauth.client_secret
                db.commit()
            verified = helper.request(client, tokens['provider'], 'POST',
                f"/products/{ids['products']['api']}/versions/{version_id}/integration/verify")
            helper.check(verified.get('status') == 'verified', 'Fixture route verification failed')
            with m.SessionLocal() as db:
                route = db.scalar(select(m.ApiGatewayRoute).where(m.ApiGatewayRoute.route_key == route_key))
                helper.check(route is not None and route.status == 'active', 'Fixture route is not active')
            remote = helper.apisix_admin(m, 'GET', '/routes/' + route_key)
            helper.check(remote.get('labels', {}).get('market_product_id') == ids['products']['api'],
                         'Published APISIX route product mismatch')
            published.append(route_key)

        manifest['database_ids'] = {
            'users': {'buyer': ids['users']['buyer']},
            'enterprises': {'buyer': ids['enterprises']['buyer']},
            'products': {'api': ids['products']['api']},
            'versions': {'api': dict(ids['versions']['api'])},
            'routes': {'api': dict(ids['routes']['api'])},
        }
        helper.write_manifest(manifest_path, manifest)
        browser_fixture = {'base_url': base_url, 'run_id': ids['run_id'],
            'product_id': ids['products']['api'], 'enterprise_id': ids['enterprises']['buyer'],
            'product_version_id': ids['versions']['api']['source'],
            'buyer_email': buyer_email, 'buyer_password': buyer_password,
            'source_route': ids['routes']['api']['source'],
            'target_route': ids['routes']['api']['target'], 'manifest': str(manifest_path)}
        private_json(fixture_path, browser_fixture, create=True)
        return {'status': 'prepared', 'fixture': str(fixture_path), 'manifest': str(manifest_path),
                'fixture_mode': '0600', 'manifest_mode': '0600',
                'published_routes': published, 'orders_created': 0, 'credentials_created': 0}
    except Exception:
        raise
    finally:
        if client:
            client.close()


def record_consumers(manifest_path):
    """Journal consumers after delivery; the automatic worker runs in another process.

    An in-process after_flush hook cannot observe its credential insert, so each
    row and APISIX consumer is read back and checked against the exact owned route.
    """
    from sqlalchemy import select

    manifest = helper.read_manifest(manifest_path)
    m = helper.load_main()
    allowed = {value for group in manifest['routes'].values() for value in group.values()}
    with m.SessionLocal() as db:
        routes = {row.route_key: row for row in db.scalars(select(m.ApiGatewayRoute).where(
            m.ApiGatewayRoute.route_key.in_(allowed))).all()}
        orders = db.scalars(select(m.Order).where(
            m.Order.product_id == manifest['products']['api'],
            m.Order.buyer_user_id == manifest['users']['buyer'],
            m.Order.buyer_enterprise_id == manifest['enterprises']['buyer'])).all()
        helper.check(len(orders) == 1 and orders[0].payment_status == 'paid' and
                     orders[0].delivery_status in {'accepted', 'completed'} and
                     orders[0].product_version_id == manifest['versions']['api']['source'],
                     'Browser-owned purchase is not paid and delivered on the source version')
        credentials = db.scalars(select(m.ApiCredential).where(
            m.ApiCredential.route_id.in_([row.id for row in routes.values()]),
            m.ApiCredential.enterprise_id == manifest['enterprises']['buyer'])).all()
        helper.check(credentials, 'No worker-issued fixture credential found')
        for credential in credentials:
            route = next((row for row in routes.values() if row.id == credential.route_id), None)
            helper.check(route is not None and route.route_key in allowed and
                         route.product_id == manifest['products']['api'] and
                         route.route_key == manifest['routes']['api']['source'] and
                         credential.status == 'active' and
                         credential.apisix_consumer_name == 'market-consumer-' + credential.id,
                         'Generated consumer is not owned by the exact fixture route')
            remote_route = helper.apisix_admin(m, 'GET', '/routes/' + route.route_key)
            helper.check(remote_route.get('labels', {}).get('market_product_id') ==
                         manifest['products']['api'], 'Consumer route is not the published fixture route')
            remote_consumer = helper.apisix_admin(m, 'GET',
                '/consumers/' + credential.apisix_consumer_name)
            quota = remote_consumer.get('plugins', {}).get('market-gateway-quota', {})
            helper.check(remote_consumer.get('username') == credential.apisix_consumer_name and
                         quota.get('route_key') == route.route_key,
                         'Worker-issued APISIX consumer ownership check failed')
            item = {'id': credential.id, 'name': credential.apisix_consumer_name,
                    'route': route.route_key,
                    'allowed_routes': list(manifest['routes']['api'].values())}
            if item not in manifest['consumers']:
                manifest['consumers'].append(item)
                manifest['external']['consumer_names'].append(item['name'])
        helper.check(len(manifest['consumers']) == len(credentials),
                     'Consumer journal does not match fixture-owned credentials')
    helper.write_manifest(manifest_path, manifest)
    return {'status': 'recorded', 'consumer_count': len(manifest['consumers'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--record-consumers', action='store_true')
    mode.add_argument('--cleanup', action='store_true')
    parser.add_argument('--base-url')
    parser.add_argument('--manifest')
    args = parser.parse_args()
    if args.prepare:
        helper.check(args.base_url, '--prepare requires --base-url')
        result = prepare(args.base_url, args.manifest)
    elif args.record_consumers:
        helper.check(args.manifest, '--record-consumers requires --manifest')
        result = record_consumers(args.manifest)
    else:
        helper.check(args.manifest, '--cleanup requires --manifest')
        path = Path(args.manifest)
        manifest = helper.read_manifest(path)
        helper.cleanup(helper.load_main(), manifest, path)
        fixture_path = path.with_suffix('.browser.json')
        if fixture_path.exists():
            fixture_path.unlink()
        result = {'status': 'cleaned'}
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
