"""Local worker transaction fault tests; adapters here are isolated test doubles."""
import importlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, mapped_column, sessionmaker


worker = importlib.import_module('app.version_change_worker')
instant = datetime(2026, 10, 12, tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class Request(Base):
    __tablename__ = 'fault_requests'
    id = mapped_column(String, primary_key=True)
    status = mapped_column(String)
    next_retry_at = mapped_column(DateTime(timezone=True))
    lease_expires_at = mapped_column(DateTime(timezone=True))
    lease_owner = mapped_column(String)
    created_at = mapped_column(DateTime(timezone=True))
    updated_at = mapped_column(DateTime(timezone=True))
    retries = mapped_column(Integer, default=0)
    last_error = mapped_column(String, default='')
    resource_plan_json = mapped_column(Text, default='{}')
    combination_id = mapped_column(String)
    old_version_id = mapped_column(String)
    old_version_code = mapped_column(String)
    target_version_id = mapped_column(String)
    target_version_code = mapped_column(String)
    target_snapshots_json = mapped_column(Text, default='{}')
    kind = mapped_column(String, default='upgrade')
    evidence_json = mapped_column(Text, default='{}')
    resource_applied = mapped_column(Boolean, default=False)
    compensation_started = mapped_column(Boolean, default=False)
    compensation_retries = mapped_column(Integer, default=0)
    completed_at = mapped_column(DateTime(timezone=True))


class Combination(Base):
    __tablename__ = 'fault_combinations'
    id = mapped_column(String, primary_key=True)
    status = mapped_column(String)
    version_id = mapped_column(String)
    version_code = mapped_column(String)
    anchor_at = mapped_column(DateTime(timezone=True))
    total_months = mapped_column(Integer)


class Ledger(Base):
    __tablename__ = 'fault_ledger'
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_id = mapped_column(String)
    status = mapped_column(String)


class Revision(Base):
    __tablename__ = 'fault_revisions'
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_id = mapped_column(String)
    version_code = mapped_column(String)


class FinancialRecord(Base):
    __tablename__ = 'fault_financial_records'
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_id = mapped_column(String)
    status = mapped_column(String)


class StatefulSQLiteFakeAdapter:
    """Models replay-safe external state; it is not an actual runtime adapter."""
    def __init__(self):
        self.applied = set()
        self.calls = []
        self.proof_override = None
        self.fail_restore = False

    def prepare(self, _db, change):
        return {'resource_change_id': change.id, 'operation': 'fake-replace'}

    def apply(self, _db, change, direction='target'):
        self.calls.append(direction)
        if direction == 'target':
            self.applied.add(change.id)
        elif self.fail_restore:
            raise RuntimeError('restore fault')
        else:
            self.applied.discard(change.id)
        if self.proof_override is not None:
            return self.proof_override
        return {'request_change_id': change.id, 'resource_change_id': change.id,
                'evidence_fingerprint': 'a' * 64}


class WorkerFaultBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        with self.sessions() as db:
            db.add(Request(id='change-1', status='resource_pending', created_at=instant,
                updated_at=instant, retries=0, last_error='', resource_plan_json='{}',
                combination_id='combo-1', old_version_id='v1', old_version_code='v1',
                target_version_id='v2', target_version_code='v2', target_snapshots_json='{}',
                kind='upgrade', evidence_json='{}', resource_applied=False,
                compensation_started=False, compensation_retries=0))
            db.add(Combination(id='combo-1', status='active', version_id='v1', version_code='v1',
                anchor_at=datetime(2026, 1, 1, tzinfo=timezone.utc), total_months=12))
            db.commit()
        self.adapter = StatefulSQLiteFakeAdapter()
        self.ledger = {
            'finalize': lambda db, change_id, _proof, _actor:
                db.add(Ledger(change_id=change_id, status='posted')),
            'void': lambda db, change_id, *_args:
                db.add(Ledger(change_id=change_id, status='void')),
        }
        def refund_failure(db, change, _proof):
            db.add(FinancialRecord(change_id=change.id, status='posted'))
            raise RuntimeError('local refund persistence fault')

        self.refund = Mock(side_effect=refund_failure)
        self.main = SimpleNamespace(SessionLocal=self.sessions,
            version_change_saga={'VersionChangeRequest': Request,
                'refund_upgrade_funding': self.refund},
            now=lambda: instant, json=json, audit=Mock(),
            version_change_resources={'prepare': self.adapter.prepare, 'apply': self.adapter.apply},
            subscription_terms={'Combination': Combination,
                'apply_version_change': self.apply_version_change},
            version_change_ledger=self.ledger)
        self.module_patch = patch.object(worker.importlib, 'import_module', return_value=self.main)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.current_patch = patch.object(worker, '_assert_current', return_value=None)
        self.current_patch.start()
        self.addCleanup(self.current_patch.stop)

    def tearDown(self):
        self.engine.dispose()

    @staticmethod
    def apply_version_change(db, combo, version_id, version_code, _snapshots, _at, change_id):
        combo.version_id, combo.version_code = version_id, version_code
        db.add(Revision(change_id=change_id, version_code=version_code))

    def run_worker(self, at=instant):
        return worker.process_once(worker_id='isolated-test-worker', now=lambda: at)

    def snapshot(self):
        with self.sessions() as db:
            return db.get(Request, 'change-1')

    def test_resource_plan_is_committed_before_adapter_apply(self):
        self.assertTrue(self.run_worker())
        row = self.snapshot()
        self.assertEqual(row.status, 'resource_pending')
        self.assertIn('fake-replace', row.resource_plan_json)
        self.assertEqual(self.adapter.calls, [])

    def test_external_apply_then_database_commit_failure_replays_without_duplicate_rows(self):
        self.run_worker()  # durable plan checkpoint
        should_fail = {'value': True}

        def fail_first_application_commit(session):
            if should_fail['value'] and session.new and any(
                    isinstance(item, (Ledger, Revision)) for item in session.new):
                should_fail['value'] = False
                raise RuntimeError('simulated SQLite commit failure')

        from sqlalchemy import event
        event.listen(self.sessions, 'before_commit', fail_first_application_commit)
        try:
            self.assertTrue(self.run_worker())
        finally:
            event.remove(self.sessions, 'before_commit', fail_first_application_commit)
        row = self.snapshot()
        self.assertEqual(row.status, 'retrying')
        self.assertEqual(row.retries, 1)
        self.assertIn('change-1', self.adapter.applied)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(Ledger)), None)
            self.assertEqual(db.scalar(select(Revision)), None)
        self.assertTrue(self.run_worker(instant + timedelta(seconds=11)))
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(Combination)).version_code, 'v2')
            self.assertEqual(len(db.scalars(select(Ledger)).all()), 1)
            self.assertEqual(len(db.scalars(select(Revision)).all()), 1)
            self.assertEqual(db.scalar(select(Ledger)).status, 'posted')
        self.assertEqual(self.adapter.calls, ['target', 'target'])

    def test_mismatched_resource_proof_never_posts_ledger_or_revision(self):
        self.run_worker()
        self.adapter.proof_override = {'request_change_id': 'other', 'resource_change_id': 'other',
                                       'evidence_fingerprint': 'a' * 64}
        self.run_worker()
        self.assertEqual(self.snapshot().status, 'retrying')
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(Ledger)).all()), 0)
            self.assertEqual(len(db.scalars(select(Revision)).all()), 0)
            self.assertEqual(db.get(Combination, 'combo-1').version_code, 'v1')

    def test_compensation_failure_exhaustion_and_operator_retry_after_repair(self):
        with self.sessions() as db:
            row = db.get(Request, 'change-1')
            row.status = 'compensating'
            row.resource_plan_json = '{"resource_change_id":"change-1"}'
            row.compensation_started = True
            db.commit()
        self.adapter.applied.add('change-1')
        self.adapter.fail_restore = True
        for attempt in range(3):
            self.run_worker(instant + timedelta(seconds=attempt * 11))
        self.assertEqual(self.snapshot().status, 'exception')
        self.assertIn('change-1', self.adapter.applied)
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(Ledger)).all()), 0)
            self.assertEqual(len(db.scalars(select(Revision)).all()), 0)
            row = db.get(Request, 'change-1')
            row.status = 'compensating'  # isolated stand-in for the operator retry endpoint
            row.compensation_retries = 0
            row.next_retry_at = instant
            db.commit()
        self.adapter.fail_restore = False
        self.refund.side_effect = None
        self.run_worker(instant)
        self.assertEqual(self.snapshot().status, 'compensated')
        self.assertNotIn('change-1', self.adapter.applied)

    def test_refund_failure_retries_three_times_without_successful_posted_financial_record(self):
        with self.sessions() as db:
            row = db.get(Request, 'change-1')
            row.status = 'compensating'
            row.resource_plan_json = '{"resource_change_id":"change-1"}'
            row.compensation_started = True
            db.commit()
        self.adapter.applied.add('change-1')
        for attempt in range(3):
            self.run_worker(instant + timedelta(seconds=attempt * 11))
        self.assertEqual(self.snapshot().status, 'exception')
        self.assertEqual(self.refund.call_count, 3)
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(Ledger)).all()), 0)
            self.assertEqual(len(db.scalars(select(Revision)).all()), 0)
            self.assertEqual(len(db.scalars(select(FinancialRecord)).all()), 0)


if __name__ == '__main__':
    unittest.main()
