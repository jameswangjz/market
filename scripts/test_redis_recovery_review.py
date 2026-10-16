"""Offline evidence validation and transactional metadata-only submission."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from sqlalchemy import Integer, String, create_engine, select
from sqlalchemy.orm import mapped_column, sessionmaker

import redis_recovery_review as review
import test_r3_submit_refund_review as fixtures


class Gap(fixtures.Models):
    __tablename__ = 'recovery_review_test_gaps'
    id = mapped_column(Integer, primary_key=True)
    status = mapped_column(String)
    missing_count = mapped_column(Integer, nullable=True)


class RecoveryReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / 'app'
        self.app.mkdir()
        for name in ['main.py', *(f'module_{i}.py' for i in range(81))]:
            (self.app / name).write_text('# fixture\n')
        self.native = dict(status='passed', proof=dict(charges=2, monthly_quota=2, monthly_used=2,
            next_http_status=429, refunded_amount='16'), cleanup=dict(retained_financial_events=2))
        self.native_path = self.root / 'native.json'
        self.native_path.write_text(json.dumps(self.native))
        self.proof = dict(schema='redis_recovery_runtime_v1', status='passed', tag=review.TAG,
            commit=review.COMMIT, captured_at='2026-10-16T14:00:00+00:00', source_id='a'*40,
            physical_run_id='b'*40, checks=dict.fromkeys(review.CHECKS, True), gap_count=1,
            source_sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.app.glob('*.py')},
            native_evidence=dict(status='passed', path='/original/native.json',
                sha256=hashlib.sha256(self.native_path.read_bytes()).hexdigest()))
        self.evidence = self.root / 'runtime.json'
        self.output = self.root / 'review.json'
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        fixtures.Models.metadata.create_all(self.engine)
        sessions = sessionmaker(bind=self.engine)
        self.client = Mock()
        self.client.info.return_value = {'run_id': self.proof['physical_run_id']}
        self.client.hgetall.return_value = {'run_id': self.proof['source_id']}
        def source_context(db, client):
            client.hgetall('market:apisix:charges:source')
            return ('market:apisix:charges:v1:' + self.proof['source_id'],
                    SimpleNamespace(redis_run_id=self.proof['source_id']))
        self.fail_audit = False
        self.corrupt_protected = False
        def audit(db, actor, action, kind, target, note, **kwargs):
            db.add(fixtures.Audit(actor=actor, target=target, before=json.dumps(kwargs['before']),
                                  after=json.dumps(kwargs['after'])))
            if self.corrupt_protected:
                db.get(fixtures.Product, 'product').name = 'corrupt'
            if self.fail_audit:
                db.flush()
                raise RuntimeError('injected audit failure')
        self.m = SimpleNamespace(__file__=str(self.app / 'main.py'), SessionLocal=sessions,
            User=fixtures.User, DevelopmentTask=fixtures.Task, Order=fixtures.Order, Product=fixtures.Product,
            Payment=fixtures.Payment, Refund=fixtures.Refund, audit=audit,
            now=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc),
            api_charge_ledger=dict(Gap=Gap, source_context=source_context), apisix_policy_redis=lambda: self.client)
        with sessions.begin() as db:
            db.add(fixtures.User(id='admin', email='admin@market.local', platform_role='super_admin',
                                is_active=True, activation_status='active'))
            db.add(fixtures.Product(id='product', name='protected'))
            db.add(fixtures.Order(id='order', order_no=review.PROTECTED, product_id='product'))
            db.add(Gap(id=1, status='unresolved', missing_count=None))
            for code in (*review.CODES, 'unrelated'):
                db.add(fixtures.Task(code=code, status='in_progress', progress=90, acceptance='Original\nHistory'))

    def submit(self):
        self.evidence.write_text(json.dumps(self.proof))
        return review.submit(self.m, self.evidence, self.native_path, self.output)

    def unchanged(self):
        with self.m.SessionLocal() as db:
            self.assertTrue(all(t.status == 'in_progress' and t.acceptance == 'Original\nHistory'
                                for t in db.scalars(select(fixtures.Task))))
            self.assertEqual(db.scalars(select(fixtures.Audit)).all(), [])

    def test_exact_four_review_changes_and_readonly_client_closed(self):
        result = self.submit()
        self.assertEqual(result['changed'], 4)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['protected_before'], result['protected_after'])
        self.assertEqual(result['gap_status'], 'unresolved')
        self.assertIsNone(result['missing_count'])
        with self.m.SessionLocal() as db:
            for task in db.scalars(select(fixtures.Task)):
                self.assertTrue(task.acceptance.startswith('Original\nHistory'))
                self.assertEqual(task.status, 'review' if task.code in review.CODES else 'in_progress')
            audits = db.scalars(select(fixtures.Audit)).all()
            self.assertEqual(len(audits), 4)
            self.assertTrue(all(a.actor == 'admin@market.local' and json.loads(a.before)['status'] == 'in_progress'
                                and json.loads(a.after)['progress'] == 100 for a in audits))
        self.client.close.assert_called_once()
        self.assertLessEqual({c[0] for c in self.client.mock_calls}, {'info', 'hgetall', 'close'})

    def test_bad_evidence_refuses_before_output(self):
        original = copy.deepcopy(self.proof)
        mutations = [lambda p: p.update(status='failed'), lambda p: p.update(commit='c'*40),
            lambda p: p.update(captured_at='2026-10-16'), lambda p: p.update(gap_count=0),
            lambda p: p['checks'].update(old_cursor_unchanged=1),
            lambda p: p['source_sha256'].pop('main.py'),
            lambda p: p['native_evidence'].update(sha256='0'*64)]
        for mutate in mutations:
            self.proof = copy.deepcopy(original)
            mutate(self.proof)
            with self.assertRaises(ValueError):
                self.submit()
            self.assertFalse(self.output.exists())
            self.unchanged()

    def test_native_core_and_retained_events_required(self):
        original = copy.deepcopy(self.native)
        for mutate in (lambda p: p['proof'].update(charges=0),
                       lambda p: p['proof'].update(refunded_amount='15'),
                       lambda p: p['cleanup'].pop('retained_financial_events')):
            native = copy.deepcopy(original)
            mutate(native)
            with self.assertRaises(ValueError):
                review.validate(self.proof, native, self.proof['native_evidence']['sha256'])

    def test_live_identity_and_gap_failures_roll_back_and_close(self):
        self.client.info.return_value = {'run_id': 'c'*40}
        with self.assertRaisesRegex(ValueError, 'live_source_mismatch'):
            self.submit()
        self.unchanged()
        self.client.close.assert_called_once()
        self.output.unlink()
        self.client.info.return_value = {'run_id': self.proof['physical_run_id']}
        with self.m.SessionLocal.begin() as db:
            db.get(Gap, 1).missing_count = 0
        with self.assertRaisesRegex(ValueError, 'gap_disclosure_mismatch'):
            self.submit()
        self.unchanged()

    def test_admin_task_audit_and_protected_failures_roll_back(self):
        for mode in ('admin', 'task', 'audit', 'protected'):
            with self.subTest(mode=mode):
                with self.m.SessionLocal.begin() as db:
                    db.get(fixtures.User, 'admin').is_active = mode != 'admin'
                    db.get(fixtures.Task, review.CODES[0]).status = 'todo' if mode == 'task' else 'in_progress'
                self.fail_audit, self.corrupt_protected = mode == 'audit', mode == 'protected'
                with self.assertRaises((ValueError, RuntimeError)):
                    self.submit()
                with self.m.SessionLocal() as db:
                    self.assertEqual(db.scalars(select(fixtures.Audit)).all(), [])
                    self.assertEqual(db.get(fixtures.Product, 'product').name, 'protected')
                    self.assertTrue(all(t.status != 'review' for t in db.scalars(select(fixtures.Task))))
                self.output.unlink()

    def test_existing_output_preserved(self):
        self.output.write_text('keep')
        with self.assertRaises(FileExistsError):
            self.submit()
        self.assertEqual(self.output.read_text(), 'keep')
        self.unchanged()


if __name__ == '__main__':
    unittest.main()
