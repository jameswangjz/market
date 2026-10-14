"""Isolated registry behavior tests; SQLite does not prove PG lock concurrency."""
import json
import os
import threading
import uuid
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import fixture_faults as facility, version_change_worker as worker
from test_fixture_faults import Base, Order, Combo, Term, Request, scope


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine)
        self.scope = scope()
        self.token = 'owner-' + 'x' * 40
        self.instant = datetime.now(timezone.utc)
        self.prepare = Mock(return_value={'kind': 'synthetic'})
        self.main = SimpleNamespace(SessionLocal=self.sessions, Order=Order,
            now=lambda: self.instant, json=json,
            version_change_saga={'VersionChangeRequest': Request},
            subscription_terms={'Combination': Combo, 'Term': Term},
            version_change_resources={'prepare': self.prepare})
        with self.sessions() as db:
            db.add(Order(id=self.scope.order_id, order_no='QA',
                product_id=self.scope.product_id, buyer_enterprise_id=self.scope.enterprise_id))
            db.add(Combo(id=self.scope.combination_id, product_id=self.scope.product_id,
                enterprise_id=self.scope.enterprise_id))
            db.add(Term(id='term', order_id=self.scope.order_id,
                combination_id=self.scope.combination_id))
            db.add(Request(id=self.scope.change_id, source_order_id=self.scope.order_id,
                enterprise_id=self.scope.enterprise_id, combination_id=self.scope.combination_id,
                status='resource_pending', created_at=self.instant))
            db.commit()
        self.addCleanup(self.engine.dispose)

    def reserve(self, **kwargs):
        with self.sessions.begin() as db:
            return facility.reserve_fixture(db, self.main.__dict__, self.scope,
                token=kwargs.pop('token', self.token), **kwargs)

    def provision(self):
        facility.reservation_metadata.create_all(self.engine)

    def process(self, **kwargs):
        with patch.object(worker.importlib, 'import_module', return_value=self.main), \
                patch.object(worker, '_assert_current'):
            return worker.process_once(**kwargs)

    def test_default_without_registry_has_no_ddl_or_selection_effect(self):
        self.assertTrue(self.process())
        with self.sessions() as db:
            self.assertFalse(facility.registry_present(db))

    def test_competing_owner_renewal_and_release(self):
        self.provision()
        self.reserve()
        with self.assertRaises(ValueError):
            self.reserve(token='different-' + 'y' * 40)
        self.reserve(seconds=300, require_existing=True)
        with self.sessions.begin() as db:
            with self.assertRaises(ValueError):
                facility.release_fixture(db, self.main.__dict__, self.scope, 'bad-' + 'z' * 40)
        self.assertFalse(self.process())
        self.assertTrue(self.process(fixture_scope=self.scope, reservation_token=self.token))
        with self.sessions.begin() as db:
            facility.release_fixture(db, self.main.__dict__, self.scope, self.token)
            db.get(Request, self.scope.change_id).resource_plan_json = '{}'
        self.assertTrue(self.process())
        with self.assertRaises(ValueError):
            self.reserve(require_existing=True)

    def test_expiry_quarantines_and_outstanding_lease_blocks_recovery(self):
        self.provision()
        self.reserve()
        with self.sessions.begin() as db:
            db.execute(facility.reservations.update().values(
                expires_at=self.instant - timedelta(seconds=10)))
        self.assertFalse(self.process())
        with self.assertRaises(ValueError):
            self.process(fixture_scope=self.scope, reservation_token=self.token)
        with self.sessions.begin() as db:
            db.get(Request, self.scope.change_id).lease_owner = 'crashed-worker'
        with self.assertRaises(ValueError):
            self.reserve()
        with self.sessions.begin() as db:
            with self.assertRaises(ValueError):
                facility.release_fixture(db, self.main.__dict__, self.scope, self.token)
        self.assertFalse(self.process())

    def test_mismatched_live_ownership_and_protected_product(self):
        self.provision()
        with self.sessions.begin() as db:
            db.get(Order, self.scope.order_id).buyer_enterprise_id = 'other'
        with self.assertRaises(ValueError):
            self.reserve()
        with self.assertRaises(ValueError):
            facility.FixtureScope(**(self.scope.__dict__ | {
                'product_id': facility.PROTECTED_PRODUCT}))
        with self.sessions.begin() as db:
            db.get(Order, self.scope.order_id).buyer_enterprise_id = self.scope.enterprise_id
            db.add(Order(id='protected', order_no=facility.PROTECTED_ORDER,
                product_id=self.scope.product_id))
        with self.assertRaises(ValueError):
            self.reserve()

    def test_dedicated_worker_cannot_run_without_reservation_or_wrong_token(self):
        with self.assertRaises(ValueError):
            self.process(fixture_scope=self.scope)
        self.provision()
        self.reserve()
        with self.assertRaises(ValueError):
            self.process(fixture_scope=self.scope, reservation_token='wrong-' + 'z' * 40)
        self.prepare.assert_not_called()

    def test_expiry_between_claim_and_apply_leaves_quarantined_lease(self):
        self.provision()
        self.reserve()
        with patch.object(facility, '_clock', side_effect=[
                self.instant, self.instant + timedelta(hours=1)]):
            with self.assertRaises(ValueError):
                self.process(fixture_scope=self.scope, reservation_token=self.token)
        self.prepare.assert_not_called()
        with self.sessions() as db:
            self.assertTrue(db.get(Request, self.scope.change_id).lease_owner)
        self.assertFalse(self.process())

    def test_initial_reservation_refuses_even_expired_worker_lease(self):
        self.provision()
        with self.sessions.begin() as db:
            row = db.get(Request, self.scope.change_id)
            row.lease_owner = 'other-worker'
            row.lease_expires_at = self.instant - timedelta(hours=1)
        with self.assertRaises(ValueError):
            self.reserve()
        with self.sessions() as db:
            self.assertIsNone(db.scalar(select(facility.reservations.c.change_id)))

    def test_sibling_change_is_excluded_and_not_selected_by_dedicated_worker(self):
        self.provision()
        self.reserve()
        with self.sessions.begin() as db:
            db.add(Request(id='b' * 32, source_order_id=self.scope.order_id,
                enterprise_id=self.scope.enterprise_id, combination_id=self.scope.combination_id,
                status='resource_pending', created_at=self.instant - timedelta(seconds=1)))
        self.assertFalse(self.process())
        self.assertTrue(self.process(fixture_scope=self.scope, reservation_token=self.token))
        with self.sessions() as db:
            self.assertEqual(db.get(Request, 'b' * 32).resource_plan_json, '{}')
            record = db.execute(select(facility.reservations)).mappings().one()
            self.assertNotEqual(record['token_hash'], self.token)


@unittest.skipUnless(os.environ.get('TEST_FIXTURE_RESERVATION_PG_URL'),
                     'explicit disposable PostgreSQL URL required')
class PostgreSQLReservationTests(ReservationTests):
    def setUp(self):
        super().setUp()
        original = self.engine
        engine = create_engine(os.environ['TEST_FIXTURE_RESERVATION_PG_URL'],
            isolation_level='READ COMMITTED', connect_args={'connect_timeout': 5,
                'options': '-c statement_timeout=5000 -c lock_timeout=2000'})
        schema = 'qa_fixture_test_' + uuid.uuid4().hex
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA {schema}'))
        def cleanup():
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            engine.dispose()
        self.addCleanup(cleanup)
        # Search path is connection-local, never alters database/service settings.
        from sqlalchemy import event
        @event.listens_for(engine, 'checkout')
        def search_path(connection, *_):
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO {schema}')
            connection.commit()
        self.engine = engine
        self.sessions = sessionmaker(engine)
        self.main.SessionLocal = self.sessions
        Base.metadata.create_all(engine)
        with sessionmaker(original)() as source, self.sessions.begin() as target:
            for model in (Order, Combo, Term, Request):
                for row in source.scalars(select(model)):
                    target.merge(model(**{column.name: getattr(row, column.name)
                                          for column in model.__table__.columns}))

    def test_concurrent_reservation_has_exactly_one_owner(self):
        self.provision()
        barrier = threading.Barrier(2)
        outcomes = []
        def contender(token):
            barrier.wait(timeout=5)
            try:
                self.reserve(token=token)
                outcomes.append('owned')
            except ValueError:
                outcomes.append('rejected')
            except Exception as exc:
                outcomes.append(type(exc).__name__)
        threads = [threading.Thread(target=contender, args=(character * 40,))
                   for character in ('a', 'b')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, ['owned', 'rejected'])
