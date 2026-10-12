"""Independent financial checks; these do not prove external Saga execution."""
import copy
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select

import test_version_change_quotes as fixtures


class LedgerAcceptanceTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.VersionChangeQuoteTests.setUpClass.__func__)
    tearDownClass = classmethod(fixtures.VersionChangeQuoteTests.tearDownClass.__func__)
    setUp = fixtures.VersionChangeQuoteTests.setUp
    tearDown = fixtures.VersionChangeQuoteTests.tearDown
    request = fixtures.VersionChangeQuoteTests.request
    create = fixtures.VersionChangeQuoteTests.create
    deliver = fixtures.VersionChangeQuoteTests.deliver
    prepare = fixtures.VersionChangeQuoteTests.prepare

    def prepared(self, db, oid):
        return self.m.version_change_quotes['prepare'](
            db, db.get(self.m.User, 'buyer'), oid, 'v2', False, self.m.now())

    def assert_financial_pair(self, method):
        oid, _ = self.prepare(method=method)
        with self.m.SessionLocal() as db:
            orders = db.scalars(select(self.m.Order).where(self.m.Order.product_id == 'product')).all()
            before = {order.id: (order.amount, order.total_cost_snapshot,
                order.product_version_id, order.economic_snapshot_json) for order in orders}
            prepared = self.prepared(db, oid)
            rows = self.m.version_change_ledger['stage'](db, 'financial-check', prepared, 'independent-qa')
            totals = {side: tuple(sum(getattr(row, name) for row in rows if row.side == side)
                for name in ('revenue', 'cost', 'profit')) for side in ('old', 'target')}
            self.assertEqual(totals['old'], (Decimal('-250'), Decimal('-75'), Decimal('-175')))
            self.assertEqual(totals['target'], (Decimal('375'), Decimal('150'), Decimal('225')))
            self.assertEqual(sum(row.revenue for row in rows), Decimal('125'))
            self.assertEqual(sum(row.cost for row in rows), Decimal('75'))
            self.assertEqual(sum(row.profit for row in rows), Decimal('50'))
            self.assertEqual({order.id: (order.amount, order.total_cost_snapshot,
                order.product_version_id, order.economic_snapshot_json) for order in orders}, before)
            self.assertTrue(all(row.status == 'pending' for row in rows))
            db.rollback()

    def test_api_cost_is_not_charged_as_full_target_cost_on_net_payment(self):
        self.assert_financial_pair('api')

    def test_model_api_has_current_and_future_financial_segments(self):
        self.assert_financial_pair('model_api')

    def test_saas_has_current_and_future_financial_segments(self):
        self.assert_financial_pair('tenant_access')

    def test_original_rule_and_target_rule_are_independent_snapshots(self):
        oid, _ = self.prepare()
        with self.m.SessionLocal() as db:
            product = db.get(self.m.Product, 'product')
            product.settlement_rule_mode = 'custom'
            target_rule = json.loads(product.settlement_rule_json)
            target_rule.update(platform_rate=30, provider_rate=70,
                service_rate=0, expert_rate=0, channel_rate=0)
            product.settlement_rule_json = json.dumps(target_rule)
            db.flush()
            prepared = self.prepared(db, oid)
            rows = self.m.version_change_ledger['stage'](db, 'rules-check', prepared, 'independent-qa')
            old_rule = json.loads(next(row.rule_json for row in rows if row.side == 'old'))
            new_rule = json.loads(next(row.rule_json for row in rows if row.side == 'target'))
            self.assertEqual(Decimal(new_rule['rates']['platform_rate']), 30)
            self.assertNotEqual(old_rule['rates'], new_rule['rates'])
            db.rollback()

    def test_missing_future_segment_or_valid_but_forged_target_rule_rejected(self):
        oid, _ = self.prepare()
        with self.m.SessionLocal() as db:
            prepared = self.prepared(db, oid)
            missing = {**prepared, 'public': copy.deepcopy(prepared['public']),
                'internal': copy.deepcopy(prepared['internal'])}
            missing['internal'].pop()
            missing['public']['segments'].pop()
            with self.assertRaises(self.m.HTTPException):
                self.m.version_change_ledger['stage'](db, 'missing-segment', missing, 'independent-qa')
            forged = {**prepared, 'internal': copy.deepcopy(prepared['internal'])}
            for segment in forged['internal']:
                segment['target']['rule']['rates'] = dict(platform_rate='50', provider_rate='50',
                    service_rate='0', expert_rate='0', channel_rate='0')
            with self.assertRaises(self.m.HTTPException):
                self.m.version_change_ledger['stage'](db, 'forged-rule', forged, 'independent-qa')
            self.assertEqual(db.query(self.m.VersionChangeAdjustment).count(), 0)

    def test_posting_audit_failure_rolls_back_status_and_financial_evidence(self):
        oid, _ = self.prepare()
        with self.m.SessionLocal() as db:
            self.m.version_change_ledger['stage'](db, 'post-rollback', self.prepared(db, oid), 'independent-qa')
            db.commit()
        with self.m.SessionLocal() as db:
            with patch.object(self.m, 'audit', side_effect=RuntimeError('injected audit failure')):
                with self.assertRaisesRegex(RuntimeError, 'injected audit failure'):
                    self.m.version_change_ledger['finalize'](db, 'post-rollback', {
                        'request_change_id': 'post-rollback', 'resource_change_id': 'synthetic-proof',
                        'evidence_fingerprint': 'a' * 64}, 'independent-qa')
            db.rollback()
        with self.m.SessionLocal() as db:
            rows = db.query(self.m.VersionChangeAdjustment).all()
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row.status == 'pending' and row.posted_at is None for row in rows))
            self.assertEqual(db.query(self.m.AuditLog).filter_by(action='post_version_change_adjustment').count(), 0)


if __name__ == '__main__':
    unittest.main()
