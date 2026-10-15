"""Offline tests only; no application import or live services required."""
import unittest
from unittest.mock import patch

import r3_native_refund_runtime_acceptance as acceptance


class OwnershipGuards(unittest.TestCase):
    def setUp(self):
        self.order, self.term, self.combo, self.refund = (c * 32 for c in 'abcd')
        self.context = dict(start='100-0', end='101-2', order_id=self.order,
                            term_id=self.term, combination_id=self.combo,
                            route_key='qa-vcs-0123456789ab-api-source', source_run_id='e' * 40)
        self.event = dict(order_id=self.order, segment_id=self.term, scope=self.combo,
                          period_key=self.combo + ':1:0', route_key=self.context['route_key'],
                          source_run_id='e' * 40, quantity='1', policy='gateway_actual_charge_v1',
                          charged_at_us='1792100000000000')

    def test_runtime_refuses_even_when_reviewed(self):
        with patch.object(acceptance.base, 'load_main') as load, \
                patch.object(acceptance.base, 'write_manifest') as write, \
                patch.object(acceptance.base, 'cleanup') as cleanup:
            for reviewed in (False, True):
                with self.assertRaises(acceptance.base.AcceptanceFailure):
                    acceptance.run('/unused', reviewed=reviewed)
                with self.assertRaises(acceptance.base.AcceptanceFailure):
                    acceptance.cleanup('/unused', reviewed=reviewed)
            for helper in (load, write, cleanup):
                helper.assert_not_called()

    def test_only_exact_derived_extra_keys(self):
        keys = acceptance.extra_keys(combination_id=self.combo, generation=1,
                                     period_index=0, refund_id=self.refund)
        self.assertEqual(keys, {
            acceptance.COVERAGE + self.combo + ':' + self.combo + '%3A1%3A0',
            acceptance.INTENT + self.refund})
        self.assertNotIn(acceptance.STREAM, keys)
        for updates in ({'period_index': 1}, {'generation': True},
                        {'refund_id': '*'}, {'combination_id': '../unowned'}):
            with self.subTest(updates=updates), self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.extra_keys(**(dict(combination_id=self.combo, generation=1,
                                              period_index=0, refund_id=self.refund) | updates))

    def test_exact_native_charge(self):
        self.assertEqual(acceptance.validate_charge('101-1', self.event, **self.context), '101-1')

    def test_charge_rejects_cross_owner_source_and_extra_fields(self):
        for updates in ({'order_id': 'f' * 32}, {'segment_id': 'f' * 32},
                        {'scope': 'f' * 32}, {'period_key': self.combo + ':1:1'},
                        {'route_key': 'unowned'}, {'source_run_id': 'f' * 40},
                        {'quantity': '2'}, {'charged_at_us': '0'}, {'extra': 'data'}):
            with self.subTest(updates=updates), self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.validate_charge('101-1', self.event | updates, **self.context)

    def test_charge_window_is_exclusive_start_inclusive_end(self):
        for event_id in ('100-0', '101-3', '99999999999999999999-0', '01-0', '*'):
            with self.subTest(event_id=event_id), self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.validate_charge(event_id, self.event, **self.context)
        acceptance.validate_charge('101-2', self.event, **self.context)

    def test_fk_checks_include_both_ends_and_nullable_parent(self):
        owned = {'refunds': {(self.refund,)}, 'orders': {(self.order,)}}
        edge = ('refunds', (self.refund,), 'orders', (self.order,))
        acceptance.prove_fk_edges(owned, [edge, ('refunds', (self.refund,), 'tickets', None)])
        for bad in (('refunds', ('unowned',), 'orders', (self.order,)),
                    ('refunds', (self.refund,), 'orders', ('unowned',)),
                    ('refunds', (self.refund,), 'users', ('admin',))):
            with self.subTest(edge=bad), self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.prove_fk_edges(owned, [bad])

    def test_unknown_financial_tables_refused(self):
        acceptance.validate_financial_tables({'refunds', 'ordinary_refund_executions'})
        with self.assertRaises(acceptance.base.AcceptanceFailure):
            acceptance.validate_financial_tables({'refunds', 'ordinary_refund_settlement_links'})

    def test_refund_audit_exact_scope_and_actor(self):
        roots = dict(refund_id=self.refund, order_id=self.order,
                     buyer_id='qa-vcs-0123456789ab-u-buyer', operator_id='qa-vcs-0123456789ab-u-delivery')
        row = dict(target_type='refund', target_id=self.refund, order_id=self.order,
                   category='refund', business_domain='refund', action='ordinary_refund_completed',
                   actor='refund-worker')
        acceptance.validate_refund_audit(row, **roots)
        for update in ({'actor': 'production-admin'}, {'order_id': 'f' * 32},
                       {'target_id': 'f' * 32}, {'category': 'order'}, {'action': 'delete'}):
            with self.subTest(update=update), self.assertRaises(acceptance.base.AcceptanceFailure):
                acceptance.validate_refund_audit(row | update, **roots)


if __name__ == '__main__':
    unittest.main()
