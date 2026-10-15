"""Opt-in real PG proof, restricted to a fresh qa_r3_clock_<token> database.

The external runner owns CREATE/DROP DATABASE. No deployment, startup, seeding
of application defaults, or business database connection is permitted here.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import importlib
import json
import os
import re
import secrets
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


class TicketSlaClockPGTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        target = os.environ.get('MARKET_CLOCK_PG_TEST_URL')
        if not target:
            raise unittest.SkipTest('Explicit isolated PostgreSQL URL required')
        url = make_url(target)
        token = os.environ.get('MARKET_CLOCK_PG_ISOLATION_TOKEN', '')
        if (not re.fullmatch(r'[0-9a-f]{16}', token) or url.database != 'qa_r3_clock_' + token
                or url.get_backend_name() != 'postgresql'):
            raise AssertionError('Refusing database not owned by this test runner')
        cls.env = patch.dict(os.environ, {'DATABASE_URL': target, 'MESSAGE_CENTER_DISABLE_BACKGROUND': 'true'})
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        probe = create_engine(url, hide_parameters=True)
        try:
            with probe.connect() as connection:
                assert connection.scalar(text('SELECT current_database()')) == url.database
                assert connection.scalar(text('SHOW transaction_isolation')) == 'read committed'
                assert connection.scalar(text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")) == 0
        finally:
            probe.dispose()
        cls.m = importlib.import_module(os.environ.get('MARKET_CLOCK_PG_APP_MODULE', 'app.main'))
        cls.addClassCleanup(cls.m.engine.dispose)
        with cls.m.engine.connect() as connection:
            assert connection.scalar(text('SELECT current_database()')) == url.database
        cls.m.Base.metadata.create_all(cls.m.engine)

    def setUp(self):
        from fastapi.testclient import TestClient
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.client = TestClient(self.m.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        for name in ('request', 'get', 'post'):
            self.stack.enter_context(patch.object(self.m.httpx, name, side_effect=AssertionError('External HTTP forbidden')))
        self.stack.enter_context(patch.object(self.m.redis.Redis, 'from_url', side_effect=AssertionError('Redis forbidden')))
        for name in ('SMTP', 'SMTP_SSL'):
            self.stack.enter_context(patch.object(self.m.smtplib, name, side_effect=AssertionError('Mail forbidden')))
        self.stack.enter_context(patch.object(self.m, 'Minio', side_effect=AssertionError('Storage forbidden')))
        prefix = secrets.token_hex(6)
        self.ids = {name: prefix + '-' + name for name in ('buyer', 'provider', 'admin', 'ops', 'product', 'order')}
        m = self.m
        module = importlib.import_module(m.__package__ + '.enterprise_sla')
        metrics, calendar = module._promise('strategic')
        self.stack.enter_context(patch.object(m, 'freeze_sla_snapshot', return_value={
            'service_level_code': 'strategic', 'metrics': metrics, 'calendar': calendar,
            'contract_rule_version': 'isolated-pg-proof'}))
        with m.SessionLocal() as db:
            for side in ('buyer', 'provider'):
                db.add(m.Enterprise(id=self.ids[side], name=self.ids[side], credit_code=self.ids[side], verification_status='verified'))
            for name, role in [('buyer', ''), ('provider', ''), ('admin', 'super_admin'), ('ops', 'platform_operator')]:
                db.add(m.User(id=self.ids[name], name=name, email=self.ids[name] + '@example.invalid',
                    password_hash='unused', platform_role=role, verified_status='verified', activation_status='active'))
            db.flush()
            for side in ('buyer', 'provider'):
                db.add(m.Membership(user_id=self.ids[side], enterprise_id=self.ids[side], role='super_admin', status='active'))
            db.add(m.Product(id=self.ids['product'], enterprise_id=self.ids['provider'], name='Isolated PG fixture',
                product_type='dataset', delivery_method='file', status='published'))
            db.flush()
            db.add(m.Order(id=self.ids['order'], order_no=self.ids['order'], product_id=self.ids['product'],
                buyer_enterprise_id=self.ids['buyer'], provider_enterprise_id=self.ids['provider'],
                buyer_user_id=self.ids['buyer'], buyer_name='Fixture buyer', product_name='Fixture product',
                amount=10, paid_amount=10, payment_status='paid', main_status='completed', delivery_status='accepted'))
            db.commit()
            self.tokens = {name: m.issue_token(db.get(m.User, self.ids[name])) for name in ('buyer', 'provider', 'admin', 'ops')}
        self.ticket = self.request('POST', '/api/after-sales', 'buyer', {
            'order_id': self.ids['order'], 'description': 'Isolated PG proof', 'idempotency_key': secrets.token_hex(8)}).json()
        self.ticket = self.action(self.ticket, 'request_intervention', 'buyer').json()

    def request(self, method, path, actor, body=None, expected=200):
        response = self.client.request(method, path, json=body,
            headers={'Authorization': 'Bearer ' + self.tokens[actor]})
        self.assertEqual(response.status_code, expected, response.text)
        return response

    def body(self, ticket, action='respond_intervention', key=None):
        return {'action': action, 'reason': 'Real PostgreSQL fixture response',
                'expected_version': ticket['version'], 'idempotency_key': key or secrets.token_hex(12)}

    def action(self, ticket, action, actor, expected=200):
        return self.request('POST', f"/api/after-sales/{ticket['id']}/actions", actor,
                            self.body(ticket, action), expected)

    def state(self):
        models = [self.m.Order, self.m.AfterSalesTicket, self.m.AfterSalesEvent, self.m.TicketSlaClock,
                  self.m.AuditLog, self.m.TicketCalendarBinding]
        models += [self.m.message_center[key] for key in ('Message', 'Receipt', 'Outbox')]
        with self.m.SessionLocal() as db:
            return {model.__tablename__: sorted(json.dumps(dict(row), default=str, sort_keys=True)
                for row in db.execute(select(model.__table__)).mappings()) for model in models}

    def race(self, requests):
        barrier = threading.Barrier(2, timeout=8)
        local = threading.local()
        def meet_before_order_lock(state):
            statement = state.statement
            if getattr(statement, '_for_update_arg', None) is not None and not getattr(local, 'met', False):
                names = [table.name for table in statement.get_final_froms()]
                if names == ['orders']:
                    local.met = True
                    barrier.wait()
        event.listen(Session, 'do_orm_execute', meet_before_order_lock)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(request) for request in requests]
                return [future.result(timeout=20) for future in futures]
        finally:
            event.remove(Session, 'do_orm_execute', meet_before_order_lock)

    def raw_action(self, actor, body):
        return self.client.post(f"/api/after-sales/{self.ticket['id']}/actions", json=body,
                               headers={'Authorization': 'Bearer ' + self.tokens[actor]})

    def assert_one_response(self):
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.AfterSalesEvent).filter_by(
                ticket_id=self.ticket['id'], event_type='respond_intervention').count(), 1)
            clock = db.scalar(select(self.m.TicketSlaClock).where(
                self.m.TicketSlaClock.ticket_id == self.ticket['id'], self.m.TicketSlaClock.metric == 'intervention_response'))
            self.assertEqual(clock.lifecycle, 'completed')
            self.assertEqual(db.get(self.m.AfterSalesTicket, self.ticket['id']).version, self.ticket['version'] + 1)

    def test_concurrent_identical_event_is_one_business_response(self):
        body = self.body(self.ticket)
        responses = self.race([lambda: self.raw_action('ops', body), lambda: self.raw_action('ops', body)])
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assert_one_response()

    def test_concurrent_distinct_keys_conflict_on_version(self):
        a, b = self.body(self.ticket), self.body(self.ticket)
        responses = self.race([lambda: self.raw_action('ops', a), lambda: self.raw_action('admin', b)])
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.assert_one_response()

    def test_get_and_event_race_keep_order_ticket_lock_order(self):
        seen = {}
        def capture(state):
            if getattr(state.statement, '_for_update_arg', None) is not None:
                seen.setdefault(threading.get_ident(), []).extend(table.name for table in state.statement.get_final_froms())
        event.listen(Session, 'do_orm_execute', capture)
        try:
            body = self.body(self.ticket)
            responses = self.race([lambda: self.raw_action('ops', body), lambda: self.client.get(
                f"/api/after-sales/{self.ticket['id']}/sla", headers={'Authorization': 'Bearer ' + self.tokens['buyer']})])
            self.assertEqual([response.status_code for response in responses], [200, 200])
        finally:
            event.remove(Session, 'do_orm_execute', capture)
        self.assertEqual(len(seen), 2)
        for locks in seen.values():
            self.assertEqual(locks[:2], ['orders', 'after_sales_tickets'])
            self.assertNotIn('orders', locks[1:])
        self.assert_one_response()

    def test_event_clock_audit_outbox_failures_restore_full_rows_and_retry(self):
        real_hook, real_audit, real_notify = self.m.ticket_sla_event, self.m.audit, self.m.notify_business_event
        def bad_clock(*args):
            real_hook(*args)
            raise RuntimeError('Injected post-clock failure')
        def bad_audit(db, actor, action, *args, **kwargs):
            if action == 'materialize_ticket_sla_clocks':
                raise RuntimeError('Injected clock audit failure')
            return real_audit(db, actor, action, *args, **kwargs)
        def bad_outbox(*args, **kwargs):
            real_notify(*args, **kwargs)
            args[0].flush()
            raise RuntimeError('Injected post-outbox failure')
        body = self.body(self.ticket)
        for name, fail in [('ticket_sla_event', bad_clock), ('audit', bad_audit), ('notify_business_event', bad_outbox)]:
            before = self.state()
            with patch.object(self.m, name, side_effect=fail):
                self.assertEqual(self.raw_action('ops', body).status_code, 500)
            self.assertEqual(self.state(), before)
        self.assertEqual(self.raw_action('ops', body).status_code, 200)
        self.assert_one_response()

    def test_uncommitted_event_clocks_audit_outbox_are_invisible_and_rollback(self):
        before = self.state()
        module = importlib.import_module(self.m.__package__ + '.after_sales_workflow')
        with self.m.SessionLocal() as writer:
            user = writer.get(self.m.User, self.ids['ops'])
            self.m.after_sales_workflow['act'](writer, user, self.ticket['id'], module.TicketAction(**self.body(self.ticket)))
            writer.flush()
            self.assertEqual(self.state(), before)
            writer.rollback()
        self.assertEqual(self.state(), before)


if __name__ == '__main__':
    unittest.main()
