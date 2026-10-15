"""SQLite ledger and mocked Redis proof checks, not a gateway end-to-end test."""
from datetime import timedelta
import unittest

from sqlalchemy import column, select
from sqlalchemy.dialects import postgresql

from app.api_charge_ledger import STREAM, stream_position_sql
import test_api_charge_ledger as legacy


class WatermarkTests(unittest.TestCase):
    setUpClass = classmethod(legacy.ChargeLedgerTests.setUpClass.__func__)
    tearDownClass = classmethod(legacy.ChargeLedgerTests.tearDownClass.__func__)
    tearDown = legacy.ChargeLedgerTests.tearDown
    request = legacy.ChargeLedgerTests.request
    create_order = legacy.ChargeLedgerTests.create_order
    monthly = legacy.ChargeLedgerTests.monthly
    # Reuse fixture construction, without inheriting the legacy test cases.
    def setUp(self):
        legacy.ChargeLedgerTests.setUp(self)
        self.ledger_client.xrevrange.return_value = [('1-10', {})]
        self.ledger_client.get.return_value = '2'
        with self.m.SessionLocal() as db:
            term = db.get(self.m.subscription_terms['Term'], 'refund-segment')
            from app.subscription_calendar import CalendarAnchor
            bounds = CalendarAnchor(term.starts_at).period(0)
            self.arguments = dict(order_id=self.order_id, segment_id=term.id,
                start=term.starts_at + timedelta(days=1), end=self.at, as_of=self.at,
                source_run_id='a' * 40, boundary_id='1-2', scope=term.combination_id,
                periods=[dict(order_id=self.order_id, segment_id=term.id,
                    period_key='refund-combo:1:0', start_at=int(bounds.start.timestamp()),
                    end_at=int(bounds.end.timestamp()), used_at_switch=1)],
                receipt_evidence=('receipt:id:hash',))
            cursor = db.get(self.hooks['Cursor'], STREAM)
            cursor.last_id = '1-10'
            for identity in ('1-2', '1-10'):
                values = dict(self.event, quantity=1,
                              charged_at_us=int(self.arguments['start'].timestamp() * 1000000) - 1)
                db.add(self.hooks['Charge'](id=identity, **values))
            db.commit()

    def window(self, db, **changes):
        return self.hooks['window_summary'](db, **dict(self.arguments, **changes))

    def test_numeric_watermark_and_early_ngxtime(self):
        with self.m.SessionLocal() as db:
            result = self.window(db)
            self.assertEqual(result.charged_calls, 1)
            self.assertIn('charge-boundary:1-2', result.evidence_ids)

    def test_reject_invalid_proofs(self):
        with self.m.SessionLocal() as db:
            for changes in (dict(order_id='foreign'), dict(scope='foreign'),
                            dict(source_run_id='b' * 40), dict(boundary_id='bad'),
                            dict(start=self.arguments['start'].replace(tzinfo=None)),
                            dict(periods=[])):
                with self.subTest(changes=changes), self.assertRaises(self.module.PlanUnavailable):
                    self.window(db, **changes)

    def test_cursor_lag_and_missing_stream(self):
        with self.m.SessionLocal() as db:
            for rows in ([('1-11', {})], []):
                self.ledger_client.xrevrange.return_value = rows
                with self.assertRaises(self.module.PlanUnavailable):
                    self.window(db)

    def test_receipt_baseline_and_term_ownership(self):
        with self.m.SessionLocal() as db:
            for row in (dict(self.arguments['periods'][0], used_at_switch=0),
                        dict(self.arguments['periods'][0], order_id='foreign'),
                        dict(self.arguments['periods'][0], segment_id='foreign')):
                with self.subTest(row=row), self.assertRaises(self.module.PlanUnavailable):
                    self.window(db, periods=[row])
            with self.assertRaises(self.module.PlanUnavailable):
                self.window(db, boundary_id='1-11')

    def test_concurrent_tail_growth_rejects(self):
        self.ledger_client.xrevrange.side_effect = [[('1-10', {})], [('1-11', {})]]
        with self.m.SessionLocal() as db, self.assertRaises(self.module.PlanUnavailable):
            self.window(db)

    def test_expired_window_still_requires_owner_periods(self):
        other_order = self.create_order()
        with self.m.SessionLocal() as db:
            term = db.get(self.m.subscription_terms['Term'], 'refund-segment')
            other = self.m.subscription_terms['Term'](id='other-term', order_id=other_order,
                combination_id=term.combination_id, generation=1, version_id='version', version_code='v1',
                start_month=1, months=1, starts_at=term.ends_at,
                ends_at=term.ends_at + timedelta(days=31), status='active')
            db.add(other)
            db.flush()
            from app.subscription_calendar import CalendarAnchor
            bounds = CalendarAnchor(term.starts_at).period(1)
            row = dict(order_id=other_order, segment_id=other.id, period_key='refund-combo:1:1',
                start_at=int(bounds.start.timestamp()), end_at=int(bounds.end.timestamp()), used_at_switch=0)
            with self.assertRaises(self.module.PlanUnavailable):
                self.window(db, end=term.ends_at, as_of=term.ends_at + timedelta(days=1), periods=[row])

    def test_expired_period_counter_mismatch_rejects(self):
        with self.m.SessionLocal() as db:
            term = db.get(self.m.subscription_terms['Term'], 'refund-segment')
            as_of = term.ends_at + timedelta(days=40)
            self.assertEqual(self.window(db, end=term.ends_at, as_of=as_of).charged_calls, 1)
            self.ledger_client.get.return_value = '3'
            with self.assertRaises(self.module.PlanUnavailable):
                self.window(db, end=term.ends_at, as_of=as_of)

    def test_coverage_counter_and_source_changes(self):
        with self.m.SessionLocal() as db:
            self.ledger_client.get.return_value = '3'
            with self.assertRaises(self.module.PlanUnavailable):
                self.window(db)
            self.ledger_client.get.return_value = '2'
            self.ledger_client.hget.return_value = None
            with self.assertRaises(self.module.PlanUnavailable):
                self.window(db)
            self.ledger_client.hget.return_value = '0'
            self.ledger_client.info.return_value = {'run_id': 'b' * 40}
            with self.assertRaises(self.module.PlanUnavailable):
                self.window(db)

    def test_future_term_zero_requires_evidence(self):
        with self.m.SessionLocal() as db:
            db.query(self.hooks['Charge']).delete()
            db.get(self.hooks['Cursor'], STREAM).last_id = '0-0'
            term = db.get(self.m.subscription_terms['Term'], 'refund-segment')
            term.starts_at = self.at + timedelta(days=1)
            term.ends_at = self.at + timedelta(days=32)
            db.flush()
            from app.subscription_calendar import CalendarAnchor
            bounds = CalendarAnchor(term.starts_at).period(0)
            row = dict(self.arguments['periods'][0], start_at=int(bounds.start.timestamp()),
                       end_at=int(bounds.end.timestamp()), used_at_switch=0)
            self.ledger_client.xrevrange.return_value = []
            self.ledger_client.get.return_value = None
            result = self.window(db, start=term.starts_at, end=term.starts_at,
                                 boundary_id='0-0', periods=[row])
            self.assertEqual(result.charged_calls, 0)
            self.ledger_client.hget.return_value = None
            with self.assertRaises(self.module.PlanUnavailable):
                self.window(db, start=term.starts_at, end=term.starts_at,
                            boundary_id='0-0', periods=[row])

    def test_postgresql_numeric_expression(self):
        sql = str(select(column('id')).where(
            stream_position_sql(column('id'), 'postgresql') > (1, 2)).compile(
                dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))
        self.assertIn('split_part', sql)
        self.assertEqual(sql.count('NUMERIC(20, 0)'), 2)
