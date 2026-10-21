"""Actual main HTTP batch lifecycle on an opt-in private PostgreSQL schema.

Requires CLEARING_TEST_DATABASE_URL naming an isolated test database. Never
uses DATABASE_URL or starts application lifespan/background workers.
"""
import os
import secrets
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

import test_settlement_finance_integration as integration
import test_cross_version_final_baseline as cross_version
import test_cross_version_multi_source_refund as multi_source


class PrivatePostgresFixtureMixin:
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        url = make_url(os.environ['CLEARING_TEST_DATABASE_URL'])
        if url.get_backend_name() not in ('postgresql', 'postgres') or not url.database:
            raise ValueError('Explicit isolated PostgreSQL database required')
        url = url.set(drivername='postgresql+psycopg').difference_update_query(['options'])
        schema = 'batch_http_test_' + secrets.token_hex(12)
        admin = create_engine(url, isolation_level='AUTOCOMMIT')
        self.addCleanup(admin.dispose)
        with admin.connect() as connection:
            connection.exec_driver_sql('CREATE SCHEMA "' + schema + '"')

        def drop_schema():
            with admin.connect() as connection:
                connection.exec_driver_sql('DROP SCHEMA "' + schema + '" CASCADE')

        self.addCleanup(drop_schema)
        engine = create_engine(url, connect_args=dict(
            options='-csearch_path=' + schema + ' -clock_timeout=5000 -cstatement_timeout=15000'))
        self.addCleanup(engine.dispose)
        self.m.Base.metadata.create_all(engine)
        # Copy only disposable fixture rows; all subsequent requests use PG.
        sqlite = self.m.engine
        self.addCleanup(sqlite.dispose)
        with sqlite.connect() as source, engine.begin() as target:
            for table in self.m.Base.metadata.sorted_tables:
                rows = [dict(row) for row in source.execute(select(table)).mappings()]
                if rows:
                    target.execute(table.insert(), rows)
        self.m.engine = engine
        self.m.SessionLocal = sessionmaker(bind=engine, autoflush=False)
        with engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql('SELECT current_schema()').scalar(), schema)

    def tearDown(self):
        # Registered cleanup also runs when PG setup fails.
        pass


@unittest.skipUnless(os.environ.get('CLEARING_TEST_DATABASE_URL'),
                     'requires explicit isolated PostgreSQL test database')
class SettlementBatchPostgresHTTPTests(PrivatePostgresFixtureMixin, integration.SettlementFinanceIntegrationTests):
    def test_http_confirm_and_pay_acquire_guard_before_batch_lock(self):
        batch, sid = self.original(adjusted=True)
        hook = self.m.settlement_finance['lock_finance']
        calls = []

        def guarded(db):
            held_batch = db.scalar(self.m.text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() "
                "AND relation=to_regclass('settlement_batches') "
                "AND mode='RowShareLock' AND granted)"))
            calls.append(bool(held_batch))
            return hook(db)

        with patch.dict(self.m.settlement_finance, lock_finance=guarded):
            result = self.request('POST', f'/api/settlement-batches/{batch["id"]}/confirm',
                                  actor='finance', json={})
            self.assertEqual(result['status'], 'confirmed')
            self.assertEqual(calls, [False])
            self.payout(sid)
            calls.clear()
            result = self.request('POST', f'/api/settlement-batches/{batch["id"]}/pay',
                                  actor='finance', json={})
            self.assertEqual(result['status'], 'paid')
            self.assertTrue(result['funds']['positive_payment_complete'])
            self.assertEqual(calls, [False])


@unittest.skipUnless(os.environ.get('CLEARING_TEST_DATABASE_URL'),
                     'requires explicit isolated PostgreSQL test database')
class CrossVersionCostPostgresHTTPTests(PrivatePostgresFixtureMixin, cross_version.CrossVersionFinalBaselineTests):
    """Select the exact combined-cost method to avoid running inherited cases."""


@unittest.skipUnless(os.environ.get('CLEARING_TEST_DATABASE_URL'),
                     'requires explicit isolated PostgreSQL test database')
class CrossVersionMultiPostgresHTTPTests(PrivatePostgresFixtureMixin, multi_source.MultiSourceRefundTests):
    def test_concurrent_http_refunds_cannot_overdraw_the_same_source(self):
        self.two_changes()
        m = self.m
        with m.SessionLocal() as db:
            first = self.seed_completed(db, self.plans(db, amount='20.00'), 'first')
            first_set = m.refund_settlement['negative_settlement'](db, first)
            second = self.seed_completed(db, self.plans(db, amount='20.00', prior='20.00'), 'second', offset=1)
            second_set = m.refund_settlement['negative_settlement'](db, second)
            ids = {first_set.id, second_set.id}
            for sid in ids:
                self.assertEqual(m.refund_settlement['assessment'](db, sid)['status'], 'ready')
            db.commit()
        barrier = threading.Barrier(2)
        observed = set()
        mutex = threading.Lock()

        def simultaneous_parent_locks(connection, cursor, statement, parameters, context, many):
            if 'FOR UPDATE' not in statement.upper() or not isinstance(parameters, dict):
                return
            parent = next((v for v in parameters.values() if isinstance(v, str) and v in ids), None)
            if parent is None:
                return
            with mutex:
                if parent in observed:
                    return
                observed.add(parent)
            barrier.wait(timeout=10)

        def lock(sid):
            return self.client.post(f'/api/settlements/{sid}/lock',
                headers={'Authorization': 'Bearer ' + self.tokens['finance']})

        event.listen(m.engine, 'before_cursor_execute', simultaneous_parent_locks)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(lock, sid) for sid in sorted(ids)]
                responses = [future.result(timeout=30) for future in futures]
        finally:
            event.remove(m.engine, 'before_cursor_execute', simultaneous_parent_locks)
        self.assertEqual(observed, ids, 'Both real PostgreSQL transactions must reach their independent row locks')
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409], [r.text for r in responses])
        h = m.settlement_finance
        with m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(h['Baseline']).where(h['Baseline'].settlement_id.in_(ids))), 1)
            children = db.scalars(select(h['RefundSource']).where(h['RefundSource'].settlement_id.in_(ids))).all()
            self.assertEqual(len(children), 2)
            self.assertEqual(len({r.settlement_id for r in children}), 1)
            source = db.get(h['Baseline'], self.target_ids[0])
            records = h['source_reversals'](db, source.settlement_id)
            from decimal import Decimal
            self.assertLessEqual(sum(abs(Decimal(r['gross_amount'])) for r in records), source.revenue)


if __name__ == '__main__':
    unittest.main()
