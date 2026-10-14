import json
import threading
import unittest
from datetime import datetime, timezone
from http.client import HTTPConnection
import socket
import tempfile
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import DateTime, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, mapped_column, sessionmaker

from app.fixture_faults import (FixtureScope, PROTECTED_ORDER, fault_server,
                               reservation_metadata, reserve_fixture, release_fixture)
from app import version_change_worker as worker


def scope():
    return FixtureScope('qa-vcs-0123456789ab', 'a' * 32, 'owned-order',
                        'qa-vcs-0123456789ab-p-api',
                        'qa-vcs-0123456789ab-e-buyer', 'owned-combo')


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = 'orders'
    id = mapped_column(String, primary_key=True)
    order_no = mapped_column(String)
    product_id = mapped_column(String)
    buyer_enterprise_id = mapped_column(String)


class Combo(Base):
    __tablename__ = 'combos'
    id = mapped_column(String, primary_key=True)
    product_id = mapped_column(String)
    enterprise_id = mapped_column(String)
    generation = mapped_column(Integer, default=1)


class Term(Base):
    __tablename__ = 'terms'
    id = mapped_column(String, primary_key=True)
    order_id = mapped_column(String)
    combination_id = mapped_column(String)
    generation = mapped_column(Integer, default=1)


class Request(Base):
    __tablename__ = 'requests'
    id = mapped_column(String, primary_key=True)
    source_order_id = mapped_column(String)
    enterprise_id = mapped_column(String)
    combination_id = mapped_column(String)
    status = mapped_column(String)
    next_retry_at = mapped_column(DateTime)
    lease_expires_at = mapped_column(DateTime)
    lease_owner = mapped_column(String, default='')
    created_at = mapped_column(DateTime)
    updated_at = mapped_column(DateTime)
    resource_plan_json = mapped_column(String, default='{}')
    retries = mapped_column(Integer, default=0)


class FixtureFaultTests(unittest.TestCase):
    def test_scope_rejects_broad_or_protected_identifiers(self):
        for key, value in [('run_id', 'qa-*'), ('change_id', '*'),
                           ('product_id', 'real-product'), ('order_id', PROTECTED_ORDER),
                           ('enterprise_id', 'other-buyer')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                FixtureScope(**(scope().__dict__ | {key: value}))
        with self.assertRaises(ValueError):
            worker.process_once(fixture_scope={})
        with self.assertRaises(ValueError):
            worker.process_once(excluded_scopes=['a' * 32])

    def test_manifest_and_worker_configuration_fail_closed(self):
        self.assertEqual(worker.read_fixture_config({}), (None, ()))
        for config in ({'VERSION_CHANGE_FIXTURE_SCOPE_FILE': ''},
                       {'VERSION_CHANGE_EXCLUDED_SCOPE_FILES': ''},
                       {'VERSION_CHANGE_FIXTURE_SCOPE_FILE': 'missing',
                        'VERSION_CHANGE_EXCLUDED_SCOPE_FILES': 'missing'}):
            with self.assertRaises(ValueError):
                worker.read_fixture_config(config)
        with self.assertRaises(OSError):
            worker.read_fixture_config({'VERSION_CHANGE_FIXTURE_SCOPE_FILE': '/nonexistent/fixture'})
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.json') as manifest:
            json.dump(scope().__dict__, manifest)
            manifest.flush()
            self.assertEqual(worker.read_fixture_config(
                {'VERSION_CHANGE_FIXTURE_SCOPE_FILE': manifest.name}), (scope(), ()))
            self.assertEqual(worker.read_fixture_config(
                {'VERSION_CHANGE_EXCLUDED_SCOPE_FILES': manifest.name}), (None, (scope(),)))
            manifest.seek(0)
            manifest.truncate()
            json.dump(scope().__dict__ | {'upstream': 'http://example.com'}, manifest)
            manifest.flush()
            with self.assertRaises(ValueError):
                FixtureScope.load(manifest.name)

    def test_manifest_permissions_links_and_nonregular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scope.json'
            path.write_text(json.dumps(scope().__dict__))
            with self.assertRaises(ValueError):
                FixtureScope.load(path)
            path.chmod(0o600)
            self.assertEqual(FixtureScope.load(path), scope())
            link = Path(directory) / 'symlink.json'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                FixtureScope.load(link)
            directory_link = Path(directory) / 'linked-directory'
            directory_link.symlink_to(directory, target_is_directory=True)
            with self.assertRaises(OSError):
                FixtureScope.load(directory_link / 'scope.json')
            hardlink = Path(directory) / 'hardlink.json'
            os.link(path, hardlink)
            with self.assertRaises(ValueError):
                FixtureScope.load(path)
            fifo = Path(directory) / 'fifo'
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(ValueError):
                FixtureScope.load(fifo)

    def test_selection_exclusion_and_live_ownership_before_lease(self):
        fixture = scope()
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine)
        instant = datetime(2026, 10, 13, tzinfo=timezone.utc)
        with sessions() as db:
            db.add(Order(id=fixture.order_id, order_no='QA-ORDER',
                         product_id=fixture.product_id, buyer_enterprise_id=fixture.enterprise_id))
            db.add(Combo(id=fixture.combination_id, product_id=fixture.product_id,
                         enterprise_id=fixture.enterprise_id))
            db.add(Term(id='owned-term', order_id=fixture.order_id,
                        combination_id=fixture.combination_id))
            for rid in (fixture.change_id, 'b' * 32):
                db.add(Request(id=rid, source_order_id=fixture.order_id,
                               enterprise_id=fixture.enterprise_id,
                               combination_id=fixture.combination_id,
                               status='resource_pending', created_at=instant))
            db.commit()
        prepare = Mock(return_value={'kind': 'synthetic'})
        main = SimpleNamespace(SessionLocal=sessions, Order=Order, now=lambda: instant,
            version_change_saga={'VersionChangeRequest': Request}, json=json,
            subscription_terms={'Combination': Combo, 'Term': Term},
            version_change_resources={'prepare': prepare})
        reservation_metadata.create_all(engine)
        token = 'legacy-test-owner-' + 's' * 32
        with sessions.begin() as db:
            reserve_fixture(db, main.__dict__, fixture, token=token)
        dedicated = lambda: worker.process_once(fixture_scope=fixture, reservation_token=token)
        with patch.object(worker.importlib, 'import_module', return_value=main), \
                patch.object(worker, '_assert_current'):
            self.assertTrue(dedicated())
            with sessions() as db:
                self.assertEqual(db.get(Request, 'b' * 32).resource_plan_json, '{}')
                row = db.get(Request, fixture.change_id)
                self.assertEqual(row.lease_owner, '')
                row.resource_plan_json = '{}'
                db.add(Order(id='protected', order_no=PROTECTED_ORDER,
                             product_id=fixture.product_id))
                db.commit()
            self.assertFalse(dedicated())
            with sessions() as db:
                db.delete(db.get(Order, 'protected'))
                db.get(Term, 'owned-term').combination_id = 'other-combo'
                db.commit()
            self.assertFalse(dedicated())
            with sessions() as db:
                db.get(Term, 'owned-term').combination_id = fixture.combination_id
                db.get(Term, 'owned-term').generation = 2
                db.commit()
            self.assertFalse(dedicated())
            with sessions() as db:
                db.get(Term, 'owned-term').generation = 1
                db.get(Order, fixture.order_id).buyer_enterprise_id = 'other'
                db.commit()
            self.assertFalse(dedicated())
            self.assertFalse(worker.process_once(excluded_scopes=(fixture,)))
            with sessions.begin() as db:
                db.get(Order, fixture.order_id).buyer_enterprise_id = fixture.enterprise_id
                release_fixture(db, main.__dict__, fixture, token)
            self.assertTrue(worker.process_once(excluded_scopes=(fixture,)))
        self.assertEqual(prepare.call_count, 2)
        with sessions() as db:
            self.assertEqual(db.get(Request, fixture.change_id).resource_plan_json, '{}')
        engine.dispose()

    def test_local_subprocess_daemon_fault(self):
        fixture = scope()
        secret = 'subprocess-only-' + 's' * 32
        helper = Path(__file__).resolve().parents[2] / 'scripts' / 'isolated_fixture_fault.py'
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.json') as manifest:
            json.dump(fixture.__dict__, manifest)
            manifest.flush()
            process = subprocess.Popen(
                [sys.executable, str(helper), '--manifest', manifest.name, '--mode', 'unavailable'],
                env=os.environ | {'QA_FIXTURE_FAULT_SECRET': secret},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                import select
                self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'daemon startup timed out')
                from urllib.parse import urlsplit
                url = urlsplit(process.stdout.readline().strip())
                self.assertEqual(url.hostname, '127.0.0.1')
                conn = HTTPConnection(url.hostname, url.port, timeout=2)
                try:
                    conn.request('GET', url.path, headers={'Authorization': 'Bearer ' + secret})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 503)
                    self.assertEqual(json.loads(response.read())['change_id'], fixture.change_id)
                finally:
                    conn.close()
            finally:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate(timeout=5)
                self.assertNotIn(secret, stdout + stderr)

    def test_authenticated_exact_path_no_forwarding(self):
        fixture = scope()
        secret = 'test-only-' + 's' * 32
        server = fault_server(fixture, secret)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        path = f'/fixtures/{fixture.run_id}/{fixture.change_id}/health'
        try:
            for target, token, status in [
                    (path, '', 401), (path, 'wrong', 401),
                    (path, secret, 503), (path + '?upstream=http://127.0.0.1', secret, 404),
                    ('http://example.com/', secret, 404), ('/health', secret, 404)]:
                conn = HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                conn.request('GET', target, headers={'Authorization': 'Bearer ' + token})
                response = conn.getresponse()
                self.assertEqual(response.status, status)
                response.read()
                conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_fault_configuration_fails_closed(self):
        for secret, mode, delay in [('', 'healthy', 0), ('s' * 32, 'proxy', 0),
                                    ('s' * 32, 'timeout', 0), ('s' * 32, 'timeout', 31),
                                    ('s' * 32, 'timeout', float('nan'))]:
            with self.assertRaises(ValueError):
                fault_server(scope(), secret, mode=mode, delay=delay)

    def test_healthy_and_timeout_modes(self):
        fixture = scope()
        secret = 's' * 32
        for mode, delay in [('healthy', 0), ('timeout', 0.15)]:
            server = fault_server(fixture, secret, mode=mode, delay=delay)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            conn = HTTPConnection('127.0.0.1', server.server_port,
                                  timeout=0.03 if delay else 2)
            try:
                conn.request('GET', f'/fixtures/{fixture.run_id}/{fixture.change_id}/health',
                             headers={'Authorization': 'Bearer ' + secret})
                if delay:
                    with self.assertRaises(socket.timeout):
                        conn.getresponse()
                else:
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertTrue(json.loads(response.read())['synthetic'])
            finally:
                conn.close()
                server.shutdown()
                server.server_close()
                thread.join()
