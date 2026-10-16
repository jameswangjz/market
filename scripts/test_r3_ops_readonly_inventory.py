"""Isolated SQLite checks using column definitions from the real backend models."""
import ast
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sqlalchemy as sa

SPEC = importlib.util.spec_from_file_location('inventory', Path(__file__).with_name('r3_ops_readonly_inventory.py'))
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)
ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / 'backend/app' if (ROOT / 'backend/app/main.py').is_file() else Path('/app/app')


def real_tables(engine):
    tree = ast.parse((APP_DIR / 'main.py').read_text())
    wanted = {'Product': 'products', 'Order': 'orders', 'Payment': 'payments',
              'Refund': 'refunds', 'AfterSalesTicket': 'after_sales_tickets',
              'AfterSalesEvent': 'after_sales_events'}
    metadata = sa.MetaData()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in wanted:
            continue
        columns = []
        for field in node.body:
            if not isinstance(field, ast.AnnAssign) or not isinstance(field.value, ast.Call):
                continue
            call = field.value
            if not isinstance(call.func, ast.Name) or call.func.id != 'mapped_column':
                continue
            kind = call.args[0]
            if isinstance(kind, ast.Call) and isinstance(kind.func, ast.Name) and kind.func.id == 'ForeignKey':
                column_type = sa.String(36)
            else:
                column_type = eval(compile(ast.Expression(kind), '<real-model-type>', 'eval'), vars(sa))
            columns.append(sa.Column(field.target.id, column_type, nullable=True))
        sa.Table(wanted[node.name], metadata, *columns)
    metadata.create_all(engine)
    return metadata.tables


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine('sqlite://')
        self.tables = real_tables(self.engine)
        self.types = ops.current_types(APP_DIR)
        with self.engine.begin() as c:
            c.execute(self.tables['products'].insert(), [
                {'id': 'p', 'product_type': 'report', 'price': 12},
                {'id': 'q', 'product_type': 'application', 'price': 4}])
            c.execute(self.tables['orders'].insert(), {
                'id': 'o', 'order_no': ops.PROTECTED_ORDER, 'product_id': 'p',
                'main_status': 'paid', 'amount': 12, 'buyer_name': 'PRIVATE PERSON',
                'buyer_user_id': 'PRIVATE USER', 'economic_snapshot_json': '{"email":"secret@example.com"}'})
            c.execute(self.tables['payments'].insert(), {
                'id': 'pay', 'order_id': 'o', 'amount': 12, 'status': 'paid', 'proof': 'PRIVATE PROOF'})
            c.execute(self.tables['refunds'].insert(), {
                'id': 'ref', 'order_id': 'o', 'payment_id': 'pay', 'amount': 2})
            c.execute(self.tables['after_sales_tickets'].insert(), {
                'id': 'ticket-private', 'order_id': 'o', 'status': 'resolved',
                'evidence_status': 'historical_unknown', 'sla_snapshot_json': '{}'})

    def tearDown(self):
        self.engine.dispose()

    def report(self):
        return ops.collect(self.engine, self.types, sqlite_test=True)

    def test_real_schema_inventory_privacy_and_repeatability(self):
        before = self.report()
        after = self.report()
        self.assertEqual({k: v for k, v in before.items() if k != 'captured_at'},
                         {k: v for k, v in after.items() if k != 'captured_at'})
        self.assertEqual(before['products_by_type'], {'application': 1, 'report': 1})
        self.assertEqual(before['protected_order']['orders']['count'], 1)
        self.assertEqual(before['protected_order']['payments']['count'], 1)
        self.assertEqual(before['protected_order']['refunds']['count'], 1)
        text = json.dumps(before)
        for private in ('PRIVATE', 'secret@example.com', 'ticket-private', 'buyer_user_id'):
            self.assertNotIn(private, text)
        reasons = before['tickets']['missing_evidence_review'][0]['reasons']
        self.assertIn('missing_business_events', reasons)
        self.assertIn('missing_closed_at', reasons)

    def test_passed_and_aware_utc_capture(self):
        start = datetime.now(timezone.utc)
        report = self.report()
        end = datetime.now(timezone.utc)
        captured = datetime.fromisoformat(report['captured_at'])
        self.assertEqual(report['status'], 'passed')
        self.assertIsNotNone(captured.tzinfo)
        self.assertEqual(captured.utcoffset(), timedelta(0))
        self.assertLessEqual(start, captured)
        self.assertLessEqual(captured, end)

    def test_historical_evidence_review_preserves_original_rows(self):
        def snapshot():
            with self.engine.connect() as c:
                return {name: [dict(row) for row in c.execute(sa.select(table)).mappings()]
                        for name, table in self.tables.items()}
        before = snapshot()
        report = self.report()
        self.assertEqual(before, snapshot())
        candidate = report['tickets']['missing_evidence_review'][0]
        self.assertEqual(candidate['disposition'], 'review_only')
        self.assertIn('historical_evidence_unknown', candidate['reasons'])
        self.assertEqual(before['after_sales_tickets'][0]['evidence_status'], 'historical_unknown')
        self.assertIsNone(before['after_sales_tickets'][0]['closed_at'])
        self.assertEqual(report['all_frozen_economics'], self.report()['all_frozen_economics'])

    def test_financial_change_changes_hash_and_order_does_not(self):
        before = self.report()['protected_order']['sha256']
        with self.engine.connect() as c:
            c.exec_driver_sql('PRAGMA query_only=OFF')
            c.execute(self.tables['payments'].update().values(amount=13))
            c.commit()
        self.assertNotEqual(before, self.report()['protected_order']['sha256'])
        rows = [{'id': 'a', 'amount': 1}, {'id': 'b', 'amount': 2}]
        self.assertEqual(ops.bundle([], rows, [], []), ops.bundle([], rows[::-1], [], []))

    def test_zero_candidates_still_requires_approval(self):
        with self.engine.connect() as c:
            c.execute(self.tables['products'].update().values(product_type='dataset'))
            c.commit()
        report = self.report()
        self.assertTrue(report['migration']['requires_explicit_user_approval'])
        self.assertFalse(report['migration']['approved'])
        self.assertEqual(sum(r['product_count'] for r in report['legacy_candidates']), 0)

    def test_database_guard_precedes_queries_and_no_writes(self):
        statements = []
        sa.event.listen(self.engine, 'before_cursor_execute',
                        lambda c, cursor, sql, params, ctx, many: statements.append(sql))
        self.report()
        self.assertEqual(statements[0], 'PRAGMA query_only=ON')
        for statement in statements:
            self.assertIn(statement.split()[0].upper(), {'PRAGMA', 'BEGIN', 'SELECT'})
        with self.engine.connect() as c:
            with self.assertRaises(sa.exc.OperationalError):
                c.execute(self.tables['products'].delete())

    def test_postgres_guard_and_rollback(self):
        from unittest.mock import MagicMock
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        connection.dialect.name = 'postgresql'
        calls = []
        connection.exec_driver_sql.side_effect = lambda sql: calls.append(sql)
        with patch.object(ops, 'inventory', return_value={}) as inventory:
            inventory.side_effect = lambda *args: calls.append('inventory') or {}
            ops.collect(engine, self.types)
            self.assertEqual(calls, ['SET TRANSACTION READ ONLY',
                                    'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ', 'inventory'])
            inventory.assert_called_once()
            connection.rollback.assert_called_once()
            connection.commit.assert_not_called()
            connection.execute.assert_not_called()

    def test_inventory_failure_rolls_back_without_success_report(self):
        from unittest.mock import MagicMock
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        connection.dialect.name = 'postgresql'
        with patch.object(ops, 'inventory', side_effect=ValueError('invalid schema')):
            with self.assertRaises(ValueError):
                ops.collect(engine, self.types)
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_exclusive_private_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            ops.write_output(path, '{}\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                ops.write_output(path, 'changed')
            self.assertEqual(path.read_text(), '{}\n')
            link = Path(tmp) / 'link'
            link.symlink_to(path)
            with self.assertRaises(FileExistsError):
                ops.write_output(link, 'changed')


if __name__ == '__main__':
    unittest.main()
