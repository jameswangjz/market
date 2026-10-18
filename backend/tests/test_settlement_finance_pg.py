"""Opt-in ledger races on an explicitly supplied, isolated PostgreSQL database.

CLEARING_TEST_DATABASE_URL=postgresql+psycopg://.../isolated_test_db \
  PYTHONPATH=backend:backend/tests python -m unittest test_settlement_finance_pg -v

Never reads DATABASE_URL. Each test creates a random private schema, uses that
schema alone as search_path, and drops only its own schema in cleanup/finally.
CLEARING_MAIN_SOURCE may point to a copied main.py for the extracted route test;
the repository backend/app/main.py is used otherwise. Neither file is imported.
"""
import ast
import copy
import os
import secrets
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import Column, String, create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

import test_settlement_finance as fixture


def main_confirmation_body(namespace, *, remove_guard=False):
    """Compile only the actual route body; never import main or its DB setup."""
    override = os.environ.get('CLEARING_MAIN_SOURCE')
    path = Path(override) if override else Path(__file__).resolve().parents[1] / 'app' / 'main.py'
    module = ast.parse(path.read_text(encoding='utf-8'))
    matches = [node for node in module.body if isinstance(node, ast.FunctionDef)
               and node.name == 'confirm_settlement_batch']
    if len(matches) != 1:
        raise AssertionError('actual main batch-confirm function required')
    function = copy.deepcopy(matches[0])
    function.decorator_list = []
    function.returns = None
    function.args.defaults = []
    function.args.kw_defaults = [None for _ in function.args.kwonlyargs]
    for argument in function.args.posonlyargs + function.args.args + function.args.kwonlyargs:
        argument.annotation = None
    for argument in (function.args.vararg, function.args.kwarg):
        if argument is not None:
            argument.annotation = None
    guards = [statement for statement in function.body if isinstance(statement, ast.Expr)
              and isinstance(statement.value, ast.Call) and isinstance(statement.value.func, ast.Subscript)
              and isinstance(statement.value.func.value, ast.Name)
              and statement.value.func.value.id == 'settlement_finance'
              and isinstance(statement.value.func.slice, ast.Constant)
              and statement.value.func.slice.value == 'lock_finance']
    if len(guards) != 1:
        raise AssertionError('actual route must contain one early common finance guard')
    if remove_guard:
        function.body.remove(guards[0])
    isolated = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    scope = dict(namespace)
    exec(compile(isolated, str(path), 'exec'), scope)
    return scope['confirm_settlement_batch']


@unittest.skipUnless(os.environ.get('CLEARING_TEST_DATABASE_URL'),
                     'requires explicit CLEARING_TEST_DATABASE_URL for isolated PostgreSQL')
class FinancePostgresTests(unittest.TestCase):
    def setUp(self):
        # Reuse disposable model definitions and hooks, without importing main.
        self.fixture = fixture.FinanceTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ns, self.h = self.fixture.ns, self.fixture.h
        self.maker, self.reviewer = self.fixture.user, self.fixture.reviewer
        self.fail_audit = False
        Base = self.ns['Base']

        class Audit(Base):
            __tablename__ = 'finance_pg_audit'
            id = Column(String(36), primary_key=True, default=lambda: secrets.token_hex(16))
            actor = Column(String(120), nullable=False)
            action = Column(String(120), nullable=False)

        self.Audit = Audit

        def audit(db, actor, action, *args, **kwargs):
            db.add(Audit(actor=actor, action=action))
            db.flush()
            if self.fail_audit and action == 'finance_confirm':
                raise RuntimeError('injected finance audit failure')

        self.ns['audit'] = audit
        url = make_url(os.environ['CLEARING_TEST_DATABASE_URL'])
        if url.get_backend_name() not in ('postgresql', 'postgres') or not url.database:
            raise ValueError('CLEARING_TEST_DATABASE_URL must explicitly name an isolated PostgreSQL database')
        url = url.set(drivername='postgresql+psycopg').difference_update_query(['options'])
        self.schema = 'clearing_finance_test_' + secrets.token_hex(12)
        self.schema_created = False
        self.engine = None
        self.admin = create_engine(url, isolation_level='AUTOCOMMIT', connect_args={'connect_timeout': 10})
        self.addCleanup(self.cleanup_schema)
        with self.admin.connect() as connection:
            connection.exec_driver_sql('CREATE SCHEMA "' + self.schema + '"')
            self.schema_created = True
        self.engine = create_engine(url, connect_args=dict(connect_timeout=10,
            options='-csearch_path=' + self.schema + ' -clock_timeout=5000 -cstatement_timeout=15000'),
            pool_size=4, max_overflow=0)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.db.close)
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql('SELECT current_schema()').scalar(), self.schema)
        self.db.add_all([
            self.ns['Enterprise'](id='provider', name='Provider'),
            self.ns['SettlementBatch'](id='fixture', status='confirmed'),
            self.ns['Order'](id='o', order_no='ORD-PG', provider_enterprise_id='provider',
                             economic_snapshot_json='{"currency":"CNY"}'),
            self.ns['Payment'](id='payment', order_id='o', payment_no='PAY-PG', status='paid', amount=150),
            self.ns['Settlement'](id='s', order_id='o', settlement_no='SET-PG', status='locked',
                gross_amount=150, cost_amount=100, profit_amount=50,
                platform_fee=10, provider_share=40, service_share=0, expert_fee=0, channel_fee=0),
            self.ns['SettlementLine'](id='line', batch_id='fixture', settlement_id='s', status='pending'),
        ])
        self.db.commit()
        self.h['capture'](self.db, self.db.get(self.ns['Settlement'], 's'))
        self.db.commit()
        self.cost = self.db.scalar(select(self.h['Position']).where(self.h['Position'].kind == 'cost'))
        self.profit = self.db.scalar(select(self.h['Position']).where(self.h['Position'].share_kind == 'provider_share'))

    def cleanup_schema(self):
        try:
            if self.engine is not None:
                self.engine.dispose()
            if self.schema_created:
                with self.admin.connect() as connection:
                    connection.exec_driver_sql('DROP SCHEMA "' + self.schema + '" CASCADE')
        finally:
            self.admin.dispose()

    def draft(self, key, allocations=None):
        result = self.h['draft'](self.db, self.maker, dict(kind='payout', mode='simulated',
            reference='Isolated PostgreSQL evidence; no bank transfer', request_id=key,
            allocations=allocations or [dict(position_id=self.cost.id, amount='100.00')]))
        self.db.commit()
        return result

    def race(self, operations):
        barrier = threading.Barrier(2)

        def run(operation):
            with Session(self.engine) as db:
                barrier.wait(timeout=10)
                try:
                    result = operation(db)
                    db.commit()
                    return ('ok', result)
                except HTTPException as error:
                    db.rollback()
                    return ('http', error.status_code, error.detail)

        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(run, op) for op in operations]
            return [future.result(timeout=30) for future in futures]

    def confirmation(self, tx, key):
        def operation(db):
            return self.h['confirm'](db, self.reviewer, tx['id'],
                dict(request_id=key, expected_version=1, note='Independent PostgreSQL race reviewer'))
        return operation

    def count(self, model, *conditions):
        return self.db.scalar(select(func.count()).select_from(model).where(*conditions))

    def test_competing_drafts_share_100_only_one_confirm_succeeds(self):
        first, second = self.draft('first'), self.draft('second')
        outcomes = self.race([self.confirmation(first, 'confirm-first'), self.confirmation(second, 'confirm-second')])
        self.assertEqual(sorted(result[0] for result in outcomes), ['http', 'ok'], outcomes)
        loser = next(result for result in outcomes if result[0] == 'http')
        self.assertEqual(loser[1], 409, outcomes)
        self.assertEqual(self.count(self.h['Confirmation']), 1)
        self.assertEqual(self.count(self.h['Allocation']), 1)
        self.assertEqual(self.h['balance'](self.db, self.cost), 0)

    def test_same_confirmation_request_key_concurrently_replays_one_evidence(self):
        tx = self.draft('one-draft')
        outcomes = self.race([self.confirmation(tx, 'same-confirm'), self.confirmation(tx, 'same-confirm')])
        self.assertTrue(all(result[0] == 'ok' for result in outcomes), outcomes)
        self.assertEqual(outcomes[0][1], outcomes[1][1])
        self.assertEqual(self.count(self.h['Confirmation']), 1)
        self.assertEqual(self.count(self.h['Allocation']), 1)
        self.assertEqual(self.count(self.h['Idempotency'], self.h['Idempotency'].actor_id == self.reviewer.id,
                                    self.h['Idempotency'].request_id == 'same-confirm'), 1)
        self.assertEqual(self.count(self.Audit, self.Audit.action == 'finance_confirm'), 1)

    def test_same_draft_request_key_concurrently_creates_one_draft(self):
        body = dict(kind='payout', mode='manual', reference='Isolated manual record', request_id='same-draft',
                    allocations=[dict(position_id=self.cost.id, amount='100.00')])
        outcomes = self.race([lambda db: self.h['draft'](db, self.maker, body),
                              lambda db: self.h['draft'](db, self.maker, body)])
        self.assertTrue(all(result[0] == 'ok' for result in outcomes), outcomes)
        self.assertEqual(outcomes[0][1]['id'], outcomes[1][1]['id'])
        self.assertEqual(self.count(self.h['Transaction']), 1)
        self.assertEqual(self.count(self.h['Confirmation']), 0)
        self.assertEqual(self.h['balance'](self.db, self.cost), 100)

    def test_opposite_multi_position_order_does_not_deadlock(self):
        allocations = [dict(position_id=self.cost.id, amount='30.00'),
                       dict(position_id=self.profit.id, amount='10.00')]
        first = self.draft('forward', allocations)
        second = self.draft('reverse', list(reversed(allocations)))
        outcomes = self.race([self.confirmation(first, 'forward-confirm'), self.confirmation(second, 'reverse-confirm')])
        self.assertTrue(all(result[0] == 'ok' for result in outcomes), outcomes)
        self.assertEqual(self.count(self.h['Confirmation']), 2)
        self.assertEqual(self.count(self.h['Allocation']), 4)
        self.assertEqual(self.h['balance'](self.db, self.cost), Decimal('40.00'))
        self.assertEqual(self.h['balance'](self.db, self.profit), Decimal('20.00'))

    def test_audit_failure_rolls_back_confirmation_allocations_request_and_audit(self):
        tx = self.draft('audit-draft')
        self.fail_audit = True
        with Session(self.engine) as db:
            with self.assertRaisesRegex(RuntimeError, 'injected finance audit failure'):
                self.h['confirm'](db, self.reviewer, tx['id'],
                    dict(request_id='audit-confirm', expected_version=1, note='Must roll back'))
            # Committing the caller transaction must not resurrect savepoint work.
            db.commit()
        self.assertEqual(self.count(self.h['Confirmation']), 0)
        self.assertEqual(self.count(self.h['Allocation']), 0)
        self.assertEqual(self.count(self.h['Idempotency'], self.h['Idempotency'].request_id == 'audit-confirm'), 0)
        self.assertEqual(self.count(self.Audit, self.Audit.action == 'finance_confirm'), 0)
        self.assertEqual(self.h['balance'](self.db, self.cost), 100)
        self.fail_audit = False
        with Session(self.engine) as db:
            self.confirmation(tx, 'audit-confirm')(db)
            db.commit()
        self.assertEqual(self.h['balance'](self.db, self.cost), 0)

    def test_capture_holding_negative_settlement_does_not_invert_batch_lock_order(self):
        tx = self.draft('before-refund')
        negative = self.ns['Settlement'](id='negative', order_id='o', settlement_no='SET-NEG-PG', status='locked',
            reference_settlement_id='s', gross_amount=-75, cost_amount=-50, profit_amount=-25,
            platform_fee=-5, provider_share=-20, service_share=0, expert_fee=0, channel_fee=0)
        self.db.add(negative)
        self.db.add(self.ns['SettlementLine'](id='negative-line', batch_id='fixture',
            settlement_id='negative', status='pending'))
        self.db.commit()
        locked = threading.Barrier(2)

        def capture(db):
            row = db.scalar(select(self.ns['Settlement']).where(self.ns['Settlement'].id == 'negative').with_for_update())
            locked.wait(timeout=10)
            result = self.h['capture'](db, row)
            self.assertFalse(isinstance(result, dict), result)
            self.assertEqual(db.scalar(select(func.count()).select_from(self.h['Allocation'])), 0)
            return dict(settlement_id=result.settlement_id)

        def payment(db):
            self.h['lock_finance'](db)
            db.scalar(select(self.ns['SettlementBatch']).where(self.ns['SettlementBatch'].id == 'fixture').with_for_update())
            locked.wait(timeout=10)
            # Exercise the Batch -> Settlement order used by lifecycle/payment
            # work while capture already owns the opposite Settlement row.
            db.scalar(select(self.ns['Settlement']).where(self.ns['Settlement'].id == 'negative').with_for_update())
            return self.confirmation(tx, 'after-refund')(db)

        outcomes = self.race([capture, payment])
        self.assertEqual(outcomes[0][0], 'ok', outcomes)
        self.assertEqual(outcomes[1][0:2], ('http', 409), outcomes)
        self.assertEqual(outcomes[1][2], 'matching_recovery_requires_offset')
        self.assertEqual(self.count(self.h['Allocation']), 0)
        self.assertEqual(self.count(self.h['Confirmation']), 0)
        self.db.expire_all()
        result = self.h['reconcile_offsets'](self.db, 'fixture', 'independent-reconciliation')
        self.db.commit()
        self.assertEqual(result['amounts']['CNY'], '70.00')
        self.assertEqual(self.h['balance'](self.db, self.cost), 50)

    def test_crosslinked_batch_confirmations_take_advisory_guard_before_own_batch(self):
        self.db.add_all([
            self.ns['SettlementBatch'](id='other', status='pending_confirm'),
            self.ns['Order'](id='other-order', order_no='ORD-OTHER', provider_enterprise_id='provider',
                             economic_snapshot_json='{"currency":"CNY"}'),
            self.ns['Payment'](id='other-payment', order_id='other-order', payment_no='PAY-OTHER', status='paid', amount=150),
            self.ns['Settlement'](id='other-source', order_id='other-order', settlement_no='SET-OTHER', status='locked',
                gross_amount=150, cost_amount=100, profit_amount=50, platform_fee=10,
                provider_share=40, service_share=0, expert_fee=0, channel_fee=0),
            self.ns['SettlementLine'](id='other-line', batch_id='other', settlement_id='other-source', status='pending'),
        ])
        self.db.commit()
        self.h['capture'](self.db, self.db.get(self.ns['Settlement'], 'other-source'))
        for name, reference, order_id, batch_id in (
                ('negative-a', 'other-source', 'other-order', 'fixture'),
                ('negative-b', 's', 'o', 'other')):
            negative = self.ns['Settlement'](id=name, order_id=order_id, settlement_no='SET-' + name, status='locked',
                reference_settlement_id=reference, gross_amount=-75, cost_amount=-50, profit_amount=-25,
                platform_fee=-5, provider_share=-20, service_share=0, expert_fee=0, channel_fee=0)
            self.db.add(negative)
            self.db.add(self.ns['SettlementLine'](id='line-' + name, batch_id=batch_id, settlement_id=name, status='pending'))
            self.db.flush()
            self.h['capture'](self.db, negative)
        self.db.get(self.ns['SettlementBatch'], 'fixture').status = 'pending_confirm'
        self.db.commit()

        def confirmation(batch_id):
            def operation(db):
                self.h['lock_finance'](db)
                batch = db.scalar(select(self.ns['SettlementBatch']).where(self.ns['SettlementBatch'].id == batch_id)
                                  .with_for_update().execution_options(populate_existing=True))
                batch.status = 'confirmed'
                db.flush()
                return self.h['reconcile_offsets'](db, batch_id, 'batch-reviewer')
            return operation

        outcomes = self.race([confirmation('fixture'), confirmation('other')])
        self.assertTrue(all(result[0] == 'ok' for result in outcomes), outcomes)
        self.db.expire_all()
        debts = self.db.scalars(select(self.h['Position']).where(self.h['Position'].direction == 'receivable')).all()
        self.assertEqual(sum(self.h['balance'](self.db, p) for p in debts), 0)
        self.assertEqual(self.count(self.h['Confirmation']), 4)

    def test_late_guard_after_batch_lock_fails_closed(self):
        with Session(self.engine) as db:
            db.scalar(select(self.ns['SettlementBatch']).where(self.ns['SettlementBatch'].id == 'fixture').with_for_update())
            with self.assertRaises(HTTPException) as error:
                self.h['lock_finance'](db)
            self.assertEqual(error.exception.detail, 'finance_lock_must_precede_batch_lock')
            db.rollback()

    def test_actual_main_confirmation_body_and_guard_removed_control(self):
        Base = self.ns['Base']

        class Proposal(Base):
            __tablename__ = 'finance_pg_empty_proposals'
            id = Column(String(36), primary_key=True)
            settlement_id = Column(String(36))
            status = Column(String(30))

        Proposal.__table__.create(self.engine)
        batch = self.db.get(self.ns['SettlementBatch'], 'fixture')
        batch.status = 'pending_confirm'
        # The minimal ledger fixture intentionally does not map these legacy
        # presentation attributes. Keep the instance alive across route loads.
        batch.batch_no = 'BATCH-PG-ACTUAL-MAIN'
        self.db.commit()
        user = SimpleNamespace(id='route-reviewer', email='route-reviewer@example.invalid',
                               platform_role='finance_settlement', is_active=True)

        def require_operator(actor):
            self.assertTrue(actor.is_active)
            self.assertIn(actor.platform_role, ('super_admin', 'platform_operator', 'finance_settlement'))

        namespace = dict(self.ns, settlement_finance=self.h, SettlementAdjustmentProposal=Proposal,
                         select=select, HTTPException=HTTPException, require_settlement_operator=require_operator,
                         now=lambda: datetime.now(timezone.utc))
        body = SimpleNamespace(comment='Actual main body on disposable PostgreSQL schema')
        unguarded = main_confirmation_body(namespace, remove_guard=True)
        with self.assertRaises(HTTPException) as error:
            unguarded(batch_id='fixture', body=body, user=user, db=self.db)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail, 'finance_lock_must_precede_batch_lock')
        self.db.rollback()
        self.assertEqual(self.db.get(self.ns['SettlementBatch'], 'fixture').status, 'pending_confirm')
        self.assertEqual(self.db.get(self.ns['SettlementLine'], 'line').status, 'pending')
        self.assertEqual(self.count(self.Audit, self.Audit.action == 'confirm_settlement_batch'), 0)
        self.db.rollback()

        actual = main_confirmation_body(namespace)
        result = actual(batch_id='fixture', body=body, user=user, db=self.db)
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['batch_no'], batch.batch_no)
        self.assertIsInstance(result['confirmed_at'], datetime)
        self.assertEqual(self.db.get(self.ns['SettlementBatch'], 'fixture').status, 'confirmed')
        self.assertEqual(self.db.get(self.ns['SettlementLine'], 'line').status, 'locked')
        self.assertEqual(self.count(self.Audit, self.Audit.action == 'confirm_settlement_batch'), 1)
        self.assertEqual(self.count(self.h['Allocation']), 0)


if __name__ == '__main__':
    unittest.main()
