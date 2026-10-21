"""Frozen, allowlisted SaaS reconciliation metadata contracts."""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.saas_reconciliation import change_context, event_context, reconciliation_context, timestamp


class ReconciliationContextTests(unittest.TestCase):
    def test_provisional_open_has_no_invented_dates(self):
        value = reconciliation_context(order=SimpleNamespace(id='o', order_no='N'),
            effective_at_semantics='provider_receipt', provisional=True,
            event_id='open:o', event_type='OPEN')
        self.assertEqual(value['schema_version'], 1)
        self.assertEqual(value['period_status'], 'provisional')
        self.assertEqual((value['order_id'], value['order_no']), ('o', 'N'))
        for key in ('order_starts_at', 'order_ends_at', 'subscription_expires_at', 'effective_at'):
            self.assertIsNone(value[key])

    def test_change_projects_current_and_future_frozen_segments_without_finances(self):
        quote = {'segments': [dict(order_id='a', order_no='A', starts_at='2026-01-01T00:00:00Z',
            ends_at='2026-02-01T00:00:00Z', remaining_starts_at='2026-01-15T00:00:00Z',
            remaining_ends_at='2026-02-01T00:00:00Z', original_amount=100, unit_cost=20),
            dict(order_id='b', order_no='B', starts_at='2026-02-01T00:00:00Z',
                 ends_at='2026-03-01T00:00:00Z')]}
        value = change_context(quote, SimpleNamespace(id='a', order_no='changed'),
            '2026-03-01T00:00:00Z', [{'order_id': 'a', 'term_id': 'term-a'},
                                      {'order_id': 'b', 'term_id': 'term-b'}])
        frozen = json.dumps(value, sort_keys=True)
        quote['segments'][0]['order_no'] = 'mutated'
        self.assertEqual(json.dumps(value, sort_keys=True), frozen)
        self.assertEqual(value['order_no'], 'A')
        self.assertEqual(value['affected_orders'][1]['term_id'], 'term-b')
        self.assertEqual(value['affected_orders'][1]['order_starts_at'], '2026-02-01T00:00:00Z')
        self.assertNotIn('original_amount', frozen)
        self.assertNotIn('unit_cost', frozen)
        self.assertIsNone(value['effective_at'])
        target = event_context(value, event_id='change:c:target', event_type='CHANGE',
            product_id='p', enterprise_id='e', subscription_id='s', tenant_id='t')
        rollback = event_context(value, event_id='change:c:original', event_type='CHANGE',
            product_id='p', enterprise_id='e', subscription_id='s', tenant_id='t')
        self.assertNotEqual(target['event_id'], rollback['event_id'])
        self.assertEqual(target['affected_orders'], rollback['affected_orders'])
        target['affected_orders'][0]['order_no'] = 'mutated'
        self.assertEqual(json.dumps(value, sort_keys=True), frozen)

    def test_dates_keep_exact_microseconds(self):
        instant = datetime(2026, 1, 31, 4, 0, 0, 123456, tzinfo=timezone.utc)
        value = reconciliation_context(starts_at=instant, ends_at=instant,
            subscription_expires_at=instant, effective_at=instant,
            effective_at_semantics='entitlement_cutoff')
        self.assertEqual(value['effective_at'], instant.isoformat())
        self.assertEqual(value['period_status'], 'confirmed')

    def test_timestamp_normalizes_offsets_and_internal_sqlite_naive_values(self):
        self.assertEqual(timestamp('2026-01-31T12:00:00.123456+08:00'),
                         '2026-01-31T04:00:00.123456+00:00')
        self.assertEqual(timestamp(datetime(2026, 1, 31, 4)), '2026-01-31T04:00:00+00:00')

    def test_older_quote_without_segments_keeps_unknown_dates_nullable(self):
        value = change_context({}, SimpleNamespace(id='o'), None)
        self.assertEqual(value['affected_orders'], [])
        self.assertIsNone(value['order_no'])
        self.assertIsNone(value['order_starts_at'])
        self.assertIsNone(value['order_ends_at'])
        self.assertEqual(value['period_status'], 'provisional')
