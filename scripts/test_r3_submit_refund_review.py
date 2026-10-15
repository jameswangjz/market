"""Offline evidence gates and atomic three-task update; no remote execution."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from sqlalchemy import Boolean, DateTime, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, mapped_column, sessionmaker

import r3_submit_refund_review as review


class Models(DeclarativeBase):
    pass


class User(Models):
    __tablename__ = 'users'
    id = mapped_column(String, primary_key=True)
    email = mapped_column(String)
    platform_role = mapped_column(String)
    is_active = mapped_column(Boolean)
    activation_status = mapped_column(String)


class Task(Models):
    __tablename__ = 'development_tasks'
    code = mapped_column(String, primary_key=True)
    status = mapped_column(String)
    progress = mapped_column(Integer)
    acceptance = mapped_column(Text)
    updated_at = mapped_column(DateTime(timezone=True))


class Product(Models):
    __tablename__ = 'products'
    id = mapped_column(String, primary_key=True)
    name = mapped_column(String)


class Order(Models):
    __tablename__ = 'orders'
    id = mapped_column(String, primary_key=True)
    order_no = mapped_column(String)
    product_id = mapped_column(String)


class Payment(Models):
    __tablename__ = 'payments'
    id = mapped_column(String, primary_key=True)
    order_id = mapped_column(String)


class Refund(Models):
    __tablename__ = 'refunds'
    id = mapped_column(String, primary_key=True)
    order_id = mapped_column(String)


class Audit(Models):
    __tablename__ = 'audit_logs'
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor = mapped_column(String)
    target = mapped_column(String)
    before = mapped_column(Text)
    after = mapped_column(Text)


def evidence():
    rid, earlier = 'a' * 32, 'b' * 32
    saas = dict(status='passed', protected_preserved=True, public_business_orders=0,
        cleanup=dict(oidc='removed', public_roots='removed', role='removed', schema='removed', tenant='retained_closed'),
        proof=dict(resources_before_funds=True, idempotent=True, refund_id=rid,
                   expected_expires_at='2027-03-01T00:00:00+00:00', expires_at='2027-02-15T00:00:00+00:00'),
        earlier_refund_blocked=dict(status='passed', refund_id=earlier,
            unrefunded_renewal_blocked=True, no_execution_effects=True), owned_evidence={})
    for table, ids in dict(refunds=[rid, earlier], ordinary_refund_cases=[rid, earlier],
            ordinary_refund_executions=['c' * 32], ordinary_refund_local_completions=[rid],
            ordinary_refund_payment_legs=['d' * 32], refund_resource_recoveries=['e' * 32]).items():
        saas['owned_evidence'][table] = [[[ident], 'f' * 64] for ident in ids]
    pg = dict(status='passed', cleanup='owned schema removed', protected_order_count_before=1,
              protected_order_count_after=1, public_write_operations=0,
              cases=dict(protected_count_only=dict(status='passed', before=1, after=1)))
    file = copy.deepcopy(pg)
    parts = [dict(resources_before_funds=True, partial_refunds=2, negative_costs=['-11.11', '-22.22'],
                  total_cost='-33.33', old_receipt_stable=True, over_refund=409, other_order_download=200),
             dict(independent_pg_connections=2, statuses=[200, 409], reserved='60.00'),
             dict(orderings_tested=2, server_lock_waits_observed=2, independent_connections=True,
                  authorized_first_download=200, recovery_first_download=409),
             dict(funds_failure_observed=True, entitlement_not_restored=True, receipt_stable=True, retry_completed=True),
             dict(proof_phase_rolled_back=True, durable_receipt_preserved=True, funds_before_resources=0, retry_completed=True)]
    file['cases'].update({name: dict(status='passed', **part) for name, part in zip(review.FILE_CASES, parts)})
    monthly = copy.deepcopy(pg)
    monthly['seeded_version_changes'] = False
    monthly['cases'][review.MONTHLY_CASE] = dict(status='passed', completed_sagas=2, economic_windows=2,
        ordinary_payment_legs=3, ordinary_refunds=2, resource_recoveries=2, saga_adjustments=4,
        confirmed_rebuild_rejected=True, cross_source_refund=True, five_party_reports_and_csv=True,
        latest_payment_first=True, prior_paid_batches_unchanged=True, replay_and_rebuild=True,
        seeded_version_changes=False, saga_orchestration='production quote/request/payment/worker',
        months={'2026-12': dict(net='600', cost='120', profit='480'),
                '2027-01': dict(net='54.07', cost='70.29', profit='-16.22'),
                '2027-02': dict(net='-43.85', cost='-57.00', profit='13.15'),
                '2027-03': dict(net='-80', cost='-16', profit='-64')})
    counts = {t: 1 for t in ('orders', 'refunds', 'ordinary_refund_executions',
                            'ordinary_refund_local_completions', 'refund_resource_recoveries', 'settlements')}
    native = dict(status='passed', protected_preserved=True,
        cleanup=dict(deleted_tablecounts=counts, retained_sla_count=3),
        proof=dict(charges=2, monthly_quota=2, monthly_used=2, next_http_status=429, simulated_payment_legs=1,
            report_csv_verified=1, delivery_audit_identity_verified=1, resource_failures_before_funds=1,
            idempotent_retry_completed=1, retained_sla_count=3, refunded_amount=16,
            negative_settlement=dict(gross_amount=-16, cost_amount=-4, profit_amount=-12, platform_fee=-2.4,
                provider_share=-6, service_share=-1.2, expert_fee=-1.2, channel_fee=-1.2)))
    return dict(saas=saas, file=file, monthly=monthly, native=native)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'app'
        self.source.mkdir()
        names = ('main.py', 'refund_execution.py', 'refund_execution_worker.py', 'refund_execution_resources.py',
                 'refund_workflow.py', 'refund_planning.py', 'refund_file_resources.py')
        hashes = {}
        for name in names:
            raw = ('# fixture ' + name + '\n').encode()
            (self.source / name).write_bytes(raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
        self.proofs = evidence()
        for kind, key in (('saas', 'installed_source_sha256'), ('file', 'source_sha256'), ('monthly', 'source_sha256')):
            self.proofs[kind][key] = dict(hashes)
        self.paths = {k: self.root / (k + '.json') for k in review.KINDS}
        self.write()
        self.output = self.root / 'submitted.json'
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        Models.metadata.create_all(self.engine)
        sessions = sessionmaker(bind=self.engine)
        self.fail_audit = False
        self.corrupt_protected = False

        def audit(db, actor, _action, _target_type, target, _detail, **kwargs):
            db.add(Audit(actor=actor, target=target, before=json.dumps(kwargs['before']), after=json.dumps(kwargs['after'])))
            if self.corrupt_protected:
                db.get(Product, 'protected-product').name = 'changed'
            if self.fail_audit:
                db.flush()
                raise RuntimeError('injected_after_audit_flush')

        self.m = SimpleNamespace(__file__=str(self.source / 'main.py'), SessionLocal=sessions,
            User=User, DevelopmentTask=Task, Order=Order, Product=Product, Payment=Payment, Refund=Refund,
            audit=audit, now=lambda: datetime(2026, 10, 16, tzinfo=timezone.utc))
        with sessions.begin() as db:
            db.add(User(id='actual-admin', email='admin@market.local', platform_role='super_admin',
                        is_active=True, activation_status='active'))
            db.add(Product(id='protected-product', name='original'))
            db.add(Order(id='protected', order_no=review.PROTECTED, product_id='protected-product'))
            for code in (*review.CODES, 'unrelated'):
                db.add(Task(code=code, status='in_progress', progress=90, acceptance='Original acceptance\nPrior record'))

    def write(self):
        for kind, path in self.paths.items():
            path.write_text(json.dumps(self.proofs[kind]))
            path.chmod(0o644)  # Public evidence does not require 0600.

    def unchanged(self):
        with self.m.SessionLocal() as db:
            self.assertTrue(all(t.status == 'in_progress' and t.acceptance == 'Original acceptance\nPrior record'
                                for t in db.scalars(select(Task))))
            self.assertEqual(db.scalars(select(Audit)).all(), [])

    def test_exact_three_review_changes_original_acceptance_and_actual_actor(self):
        result = review.submit(self.m, self.paths, self.output)
        self.assertEqual(result['changed'], 3)
        self.assertEqual(result['counts']['review'], 3)
        self.assertEqual(result['protected_before'], result['protected_after'])
        with self.m.SessionLocal() as db:
            for task in db.scalars(select(Task)):
                if task.code in review.CODES:
                    self.assertEqual(task.status, 'review')
                    self.assertTrue(task.acceptance.startswith('Original acceptance\nPrior record\n'))
                    self.assertIn(result['evidence_bundle_sha256'], task.acceptance)
                else:
                    self.assertEqual(task.status, 'in_progress')
            audits = db.scalars(select(Audit)).all()
            self.assertEqual(len(audits), 3)
            self.assertTrue(all(a.actor == 'admin@market.local' and json.loads(a.before)['status'] == 'in_progress'
                                and json.loads(a.after)['status'] == 'review' for a in audits))

    def test_failed_or_missing_evidence_never_writes(self):
        self.proofs['native']['status'] = 'failed'
        self.write()
        with self.assertRaises(ValueError):
            review.submit(self.m, self.paths, self.output)
        self.assertFalse(self.output.exists())
        self.unchanged()
        self.paths['file'].unlink()
        with self.assertRaises(FileNotFoundError):
            review.submit(self.m, self.paths, self.output)
        self.unchanged()

    def test_source_mismatch_any_evidence_refuses_writes(self):
        for kind, key in (('saas', 'installed_source_sha256'), ('file', 'source_sha256'), ('monthly', 'source_sha256')):
            original = self.proofs[kind][key]['main.py']
            self.proofs[kind][key]['main.py'] = '0' * 64
            self.write()
            with self.assertRaises(ValueError):
                review.submit(self.m, self.paths, self.output)
            self.proofs[kind][key]['main.py'] = original
            self.unchanged()

    def test_cleanup_missing_case_and_bad_financial_values_refused(self):
        original = copy.deepcopy(self.proofs)
        mutations = [lambda p: p['saas']['cleanup'].update(tenant='purged'),
            lambda p: p['file']['cases'].pop(review.FILE_CASES[4]),
            lambda p: p['file']['cases'][review.FILE_CASES[0]].update(total_cost='-33.32'),
            lambda p: p['monthly']['cases'][review.MONTHLY_CASE]['months']['2027-01'].update(profit='16.22'),
            lambda p: p['native']['proof'].update(monthly_quota=10),
            lambda p: p['file'].update(protected_order_count_after=0),
            lambda p: p['saas']['proof'].update(idempotent=False),
            lambda p: p['monthly']['cases'][review.MONTHLY_CASE].update(seeded_version_changes=True)]
        for mutation in mutations:
            self.proofs = copy.deepcopy(original)
            mutation(self.proofs)
            self.write()
            with self.assertRaises(ValueError):
                review.submit(self.m, self.paths, self.output)
            self.unchanged()

    def test_existing_output_refuses_before_transaction(self):
        self.output.write_text('retained')
        with self.assertRaises(FileExistsError):
            review.submit(self.m, self.paths, self.output)
        self.assertEqual(self.output.read_text(), 'retained')
        self.unchanged()

    def test_audit_failure_rolls_back_all_three(self):
        self.fail_audit = True
        with self.assertRaises(RuntimeError):
            review.submit(self.m, self.paths, self.output)
        self.unchanged()

    def test_non_admin_or_non_active_task_refuses_atomically(self):
        with self.m.SessionLocal.begin() as db:
            db.get(User, 'actual-admin').platform_role = 'platform_operator'
        with self.assertRaises(ValueError):
            review.submit(self.m, self.paths, self.output)
        self.unchanged()
        self.output.unlink()
        with self.m.SessionLocal.begin() as db:
            db.get(User, 'actual-admin').platform_role = 'super_admin'
            db.get(Task, review.CODES[-1]).status = 'review'
        with self.assertRaises(ValueError):
            review.submit(self.m, self.paths, self.output)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(Task, review.CODES[0]).status, 'in_progress')
            self.assertEqual(db.scalars(select(Audit)).all(), [])

    def test_protected_fingerprint_change_rolls_back_tasks_and_audits(self):
        self.corrupt_protected = True
        with self.assertRaisesRegex(ValueError, 'protected_fingerprint_changed'):
            review.submit(self.m, self.paths, self.output)
        self.unchanged()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(Product, 'protected-product').name, 'original')

    def test_source_path_escape_and_missing_inventory_refused(self):
        self.proofs['file']['source_sha256']['../outside.py'] = 'f' * 64
        self.write()
        with self.assertRaises(ValueError):
            review.submit(self.m, self.paths, self.output)
        self.unchanged()
        self.proofs['file']['source_sha256'].pop('../outside.py')
        self.proofs['monthly']['source_sha256'].pop('refund_execution.py')
        self.write()
        with self.assertRaises(ValueError):
            review.submit(self.m, self.paths, self.output)
        self.unchanged()


if __name__ == '__main__':
    unittest.main()
