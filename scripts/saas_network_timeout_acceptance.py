"""Real HTTP timeout boundaries for the production SaaS client and owned mock tenant.

Only the network hop is fault-controlled by a loopback proxy. This is a client
transport acceptance, not a deployed Saga-worker or APISIX timeout acceptance.
"""
import importlib.util
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import httpx
from sqlalchemy import select

spec = importlib.util.spec_from_file_location('saas_runtime',
    Path(__file__).with_name('version_change_saas_runtime_acceptance.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


class Proxy(ThreadingHTTPServer):
    daemon_threads = False
    mode = 'before'
    forwarded = 0
    applied = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        server = self.server
        try:
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            base.check(self.path == '/isv.php' and body.get('product_id') == server.identity['product_id']
                and body.get('tenant_id') == server.identity['tenant_id']
                and body.get('subscription_id') == server.identity['subscription_id']
                and body.get('enterprise_id') == server.identity['enterprise_id']
                and body.get('type') == 'CHANGE', 'Proxy refuses unowned operation')
            if server.mode == 'before':
                time.sleep(.8)
                self.send_response(504)
                self.end_headers()
                return
            response = httpx.post(base.MOCK_SAAS + '/isv.php', json=body,
                headers={'Authorization': self.headers.get('Authorization', '')},
                timeout=10, follow_redirects=False, trust_env=False)
            server.forwarded += 1
            server.applied.set()
            # Forwarding has completed, but the client must not receive the acknowledgment.
            time.sleep(.8)
            self.send_response(response.status_code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            server.failure = type(exc).__name__


def run():
    m = base.load_main()
    ids = base.fixture_ids()
    path = '/tmp/' + ids['run_id'] + '-network.json'
    base.preflight(m, ids)
    ids['authorized'] = True
    base.write_manifest(path, ids, create=True)
    server = None
    thread = None
    result = None
    try:
        base.prepare_fixture(m, ids)
        with m.SessionLocal() as db:
            sub = m.SaaSSubscription(product_id=ids['product'],
                enterprise_id=ids['enterprises']['buyer'], version_id=ids['versions']['source'],
                created_by=ids['users']['buyer'])
            db.add(sub)
            db.flush()
            ids['database_ids']['subscription_id'] = sub.id
            ids['external']['tenant'] = 'tenant-' + sub.id[:12]
            sub.external_tenant_id = ids['external']['tenant']
            db.commit()
            config = db.scalar(select(m.SaaSIntegrationConfig).where(
                m.SaaSIntegrationConfig.product_id == ids['product']))
        base.write_manifest(path, ids)
        identity = {'product_id': ids['product'], 'enterprise_id': ids['enterprises']['buyer'],
            'subscription_id': ids['database_ids']['subscription_id'], 'tenant_id': ids['external']['tenant']}
        opened = m.saas_call(config, 'OPEN', dict(identity, version='source',
            idempotency_key=ids['run_id'] + '-open'))
        base.check(opened.get('tenant_id') == identity['tenant_id'], 'Tenant OPEN identity mismatch')
        ids['external']['tenant_created'] = True
        base.write_manifest(path, ids)
        m.saas_access_token(config)
        server = Proxy(('127.0.0.1', 0), Handler)
        server.identity, server.failure = identity, None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        proxy_config = SimpleNamespace(**{key: getattr(config, key) for key in (
            'id', 'client_id', 'client_secret', 'scope', 'auth_mode', 'operation_path')},
            base_url='http://127.0.0.1:' + str(server.server_port), timeout_ms=200)

        def timeout(payload):
            try:
                m.saas_call(proxy_config, 'CHANGE', payload)
            except m.HTTPException as exc:
                base.check(exc.status_code == 502 and isinstance(exc.__cause__ or exc.__context__, httpx.ReadTimeout),
                    'Expected real HTTP read timeout mapped to controlled 502')
            else:
                raise base.AcceptanceFailure('Timeout unexpectedly produced a successful acknowledgment')

        before_payload = dict(identity, to_version='upgrade', idempotency_key=ids['run_id'] + '-before')
        timeout(before_payload)
        code, live = base.mock_saas_status(m, config, identity)
        base.check(code == 200 and live.get('version') == 'source' and server.forwarded == 0,
            'Timeout before dispatch changed remote state')
        server.mode = 'after'
        payload = dict(identity, to_version='upgrade', idempotency_key=ids['run_id'] + '-after')
        for attempt in range(3):
            server.applied.clear()
            timeout(payload)
            base.check(server.applied.wait(10), 'Remote application was not observed before lost acknowledgment')
            code, live = base.mock_saas_status(m, config, identity)
            base.check(code == 200 and live.get('version') == 'upgrade'
                and all(live.get(key) == value for key, value in identity.items()),
                'Remote state after acknowledgment timeout is not identity-bound target')
            if attempt < 2:
                time.sleep(10)
        replay = m.saas_call(config, 'CHANGE', payload)
        base.check(replay.get('ret') == 0 and replay.get('version') == 'upgrade',
            'Same idempotency key could not safely replay acknowledged operation')
        restored = m.saas_call(config, 'CHANGE', dict(identity, to_version='source',
            idempotency_key=ids['run_id'] + '-restore'))
        code, live = base.mock_saas_status(m, config, identity)
        base.check(restored.get('ret') == 0 and code == 200 and live.get('version') == 'source',
            'Original remote version was not restored')
        base.check(not server.failure and server.forwarded == 3, 'Proxy forwarding verification failed')
        result = {'status': 'passed', 'scope': 'production SaaS client with real HTTP timeout and owned mock tenant',
            'before_dispatch': 'remote state unchanged; controlled 502',
            'after_remote_commit': 'three lost acknowledgments; target confirmed through STATUS',
            'replay': 'same idempotency key accepted; original version restored',
            'retry_interval_seconds': 10, 'retry_driver': 'acceptance script, not deployed Saga worker',
            'not_tested': ['APISIX timeout', 'deployed worker network-timeout compensation', 'real vendor behavior']}
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=5)
        base.cleanup(m, ids, path)
    result['cleanup'] = 'exact owned DB fixture removed; mock tenant closed; protected order unchanged'
    return result


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(run(), sort_keys=True), flush=True)
    except base.AcceptanceFailure as exc:
        print(json.dumps({'status': 'failed', 'check': str(exc)}), flush=True)
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1)
