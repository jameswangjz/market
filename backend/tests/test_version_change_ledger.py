"""Internal version-change adjustment ledger tests; no payment execution."""
import copy
import importlib
import unittest

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import test_version_change_quotes as quote_fixtures


class VersionChangeLedgerTests(unittest.TestCase):
    setUpClass = classmethod(quote_fixtures.VersionChangeQuoteTests.setUpClass.__func__)
    tearDownClass = classmethod(quote_fixtures.VersionChangeQuoteTests.tearDownClass.__func__)
    tearDown = quote_fixtures.VersionChangeQuoteTests.tearDown

    def setUp(self):
        quote_fixtures.VersionChangeQuoteTests.setUp(self)
        self.module = importlib.import_module(self.m.__package__ + '.version_change_ledger')
        self.ledger = self.module.install(vars(self.m))
        self.m.Base.metadata.create_all(self.m.engine)
        self.oid, _ = self.prepare()
        with self.m.SessionLocal() as db:
            self.prepared = self.m.version_change_quotes['prepare'](
                db, db.get(self.m.User, 'buyer'), self.oid, 'v2', False, self.m.now())
            db.rollback()

    prepare = quote_fixtures.VersionChangeQuoteTests.prepare
    create = quote_fixtures.VersionChangeQuoteTests.create
    deliver = quote_fixtures.VersionChangeQuoteTests.deliver

    def stage(self, db, change_id='change-001', prepared=None):
        return self.ledger['stage'](db, change_id, prepared or self.prepared, 'ledger-test')

    def test_stage_is_atomic_and_caller_rollback_removes_rows_and_audit(self):
        with self.m.SessionLocal() as db:
            audit_before = db.scalar(select(self.m.func.count()).select_from(self.m.AuditLog))
            rows = self.stage(db)
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row.status == 'pending' for row in rows))
            self.assertGreater(db.scalar(select(self.m.func.count()).select_from(self.m.AuditLog)), audit_before)
            db.rollback()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.scalar(select(self.m.func.count()).select_from(self.ledger['Model'])), 0)
            self.assertEqual(db.scalar(select(self.m.func.count()).select_from(self.m.AuditLog)), audit_before)

    def test_repeated_stage_is_idempotent_and_changed_payload_rejected(self):
        with self.m.SessionLocal() as db:
            first = self.stage(db)
            again = self.stage(db)
            self.assertEqual({row.id for row in first}, {row.id for row in again})
            changed = copy.deepcopy(self.prepared)
            changed['internal'][0]['target']['revenue'] = '226.00'
            with self.assertRaises(self.m.HTTPException):
                self.stage(db, prepared=changed)
            db.rollback()

    def test_unique_change_order_side_is_database_backstop(self):
        with self.m.SessionLocal() as db:
            row = self.stage(db)[0]
            duplicate = self.ledger['Model'](change_id=row.change_id, source_order_id=row.source_order_id,
                combination_id=row.combination_id, side=row.side, revenue=row.revenue, cost=row.cost,
                profit=row.profit, currency=row.currency, original_version_id=row.original_version_id,
                target_version_id=row.target_version_id, rule_json=row.rule_json,
                effective_at=row.effective_at, status='pending', payload_fingerprint=row.payload_fingerprint)
            db.add(duplicate)
            with self.assertRaises(IntegrityError):
                db.flush()
            db.rollback()

    def test_exact_old_and_target_cost_rules_versions_and_future_segments(self):
        with self.m.SessionLocal() as db:
            rows = self.stage(db)
            entries = {(row.source_order_id, row.side): row for row in rows}
            self.assertEqual(len({row.source_order_id for row in rows}), 2)
            for segment in self.prepared['internal']:
                old = entries[(segment['order_id'], 'old')]
                target = entries[(segment['order_id'], 'target')]
                self.assertEqual(old.revenue, self.m.Decimal(segment['old']['revenue']))
                self.assertEqual(old.cost, self.m.Decimal(segment['old']['cost']))
                self.assertEqual(old.original_version_id, segment['old']['version_id'])
                self.assertEqual(old.rule_json, self.m.json.dumps(segment['old']['rule'], sort_keys=True, separators=(',', ':'), ensure_ascii=False))
                self.assertEqual(target.revenue, self.m.Decimal(segment['target']['revenue']))
                self.assertEqual(target.cost, self.m.Decimal(segment['target']['cost']))
                self.assertEqual(target.target_version_id, segment['target']['version_id'])
                self.assertEqual(target.rule_json, self.m.json.dumps(segment['target']['rule'], sort_keys=True, separators=(',', ':'), ensure_ascii=False))
            self.assertNotEqual(entries[(self.prepared['internal'][0]['order_id'], 'old')].cost,
                                entries[(self.prepared['internal'][0]['order_id'], 'target')].cost)
            db.rollback()

    def test_zero_amounts_are_valid_but_arbitrary_money_and_rule_changes_are_rejected(self):
        with self.m.SessionLocal() as db:
            for segment in self.prepared['internal']:
                order = db.get(self.m.Order, segment['order_id'])
                snapshot = self.m.json.loads(order.economic_snapshot_json)
                snapshot.update(unit_price='0.00', unit_cost='0.00', amount='0.00', total_cost='0.00')
                order.amount = order.paid_amount = order.total_cost_snapshot = 0
                order.unit_price_snapshot = order.unit_cost_snapshot = 0
                order.economic_snapshot_json = self.m.json.dumps(snapshot, sort_keys=True)
            target_version = db.get(self.m.ProductReleaseVersion, 'v2')
            target_version.price = target_version.cost = 0
            db.commit()
            zero = self.m.version_change_quotes['prepare'](
                db, db.get(self.m.User, 'buyer'), self.oid, 'v2', False, self.m.now())
        with self.m.SessionLocal() as db:
            self.assertEqual(len(self.stage(db, 'change-zero', zero)), 4)
            db.rollback()
        for edit in ('money', 'rule', 'target-rule'):
            invalid = copy.deepcopy(self.prepared)
            if edit == 'money':
                invalid['internal'][0]['target']['revenue'] = '999999999999999.00'
            elif edit == 'rule':
                invalid['internal'][0]['old']['rule']['rates']['platform_rate'] = '101'
            else:
                invalid['internal'][0]['target']['rule']['version'] = 'global:forged'
            with self.subTest(edit=edit), self.m.SessionLocal() as db:
                with self.assertRaises(self.m.HTTPException):
                    self.stage(db, 'change-invalid-' + edit, invalid)
                db.rollback()

    def test_foreign_order_rejected_and_post_requires_proof_void_only_pending(self):
        foreign = copy.deepcopy(self.prepared)
        foreign['internal'][0]['order_id'] = 'missing-order'
        with self.m.SessionLocal() as db:
            with self.assertRaises(self.m.HTTPException):
                self.stage(db, prepared=foreign)
            rows = self.stage(db)
            with self.assertRaises(self.m.HTTPException):
                self.ledger['finalize'](db, 'change-001', {'proof': 'x'}, 'ledger-test')
            proof = {'request_change_id': 'change-001', 'resource_change_id': 'execution-1',
                     'evidence_fingerprint': 'a' * 64}
            self.ledger['finalize'](db, 'change-001', proof, 'ledger-test')
            self.assertTrue(all(row.resource_change_id == 'execution-1' for row in rows))
            changed_proof = {**proof, 'resource_change_id': 'different-execution'}
            with self.assertRaises(self.m.HTTPException):
                self.ledger['finalize'](db, 'change-001', changed_proof, 'ledger-test')
            with self.assertRaises(self.m.HTTPException):
                self.ledger['void'](db, 'change-001', 'cannot erase posted evidence', 'ledger-test')
            db.rollback()
        with self.m.SessionLocal() as db:
            self.stage(db)
            self.ledger['void'](db, 'change-001', 'resource execution failed', 'ledger-test')
            self.assertTrue(all(row.status == 'void' for row in db.scalars(
                select(self.ledger['Model']).where(self.ledger['Model'].change_id == 'change-001')).all()))
            db.rollback()


if __name__ == '__main__':
    unittest.main()
