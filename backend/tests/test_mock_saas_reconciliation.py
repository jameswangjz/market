"""Standalone reconciliation envelope and immutable receipt regressions."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('mock_reconciliation_store',
    Path(__file__).resolve().parents[2] / 'mock-saas/app/store.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
helper_spec = importlib.util.spec_from_file_location('shared_reconciliation',
    Path(__file__).resolve().parents[1] / 'app/saas_reconciliation.py')
shared = importlib.util.module_from_spec(helper_spec)
helper_spec.loader.exec_module(shared)


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = module.Store(Path(directory.name) / 'state.db')
        self.token = dict(active=True, product_id='p', scope='saas.tenant saas.user saas.department')
        self.open = dict(type='OPEN', tenant_id='t', product_id='p', enterprise_id='e',
                         subscription_id='s', expires_at='2090-01-01T00:00:00Z')
        self.store.machine_operation(self.open, self.token)
        self.context = dict(schema_version=1, event_id='event', event_type='RENEW', product_id='p',
            enterprise_id='e', subscription_id='s', tenant_id='t', order_id=None, order_no=None,
            order_starts_at=None, order_ends_at=None, subscription_expires_at=None,
            effective_at=None, effective_at_semantics='provider_receipt', period_status='confirmed',
            affected_orders=[])

    def call(self, operation='RENEW', **fields):
        return self.store.machine_operation(dict(type=operation, tenant_id='t', **fields), self.token)

    def context_for(self, operation, key=None, **changes):
        return dict(self.context, event_type=operation, event_id=key or 'event', **changes)

    def status(self):
        return self.call('STATUS', enterprise_id='e', subscription_id='s')

    def test_receipt_replay_restart_status_and_hash(self):
        body = dict(type='RENEW', tenant_id='t', expires_at='2091-01-01T00:00:00Z',
                    idempotency_key='renew', reconciliation_context=self.context_for('RENEW', 'renew'))
        original = deepcopy(body)
        first = self.store.machine_operation(body, self.token)
        self.assertEqual(body, original)
        self.assertIsNone(first['reconciliation_context']['effective_at'])
        self.assertNotIn('effective_at', first)
        self.assertEqual(first['accepted_at'], first['reconciliation_receipt']['received_at'])
        self.assertEqual(first['reconciliation_receipt']['expires_at'], '2091-01-01T00:00:00+00:00')
        self.store = module.Store(self.store.path)
        with patch.object(module, 'datetime') as clock:
            self.assertEqual(self.store.machine_operation(body, self.token), first)
            clock.now.assert_not_called()
        self.assertEqual(self.status()['reconciliation_receipt'], first['reconciliation_receipt'])
        changed = deepcopy(body)
        changed['reconciliation_context']['subscription_expires_at'] = '2092-01-01T00:00:00Z'
        with self.assertRaises(module.MachineOperationError) as caught:
            self.store.machine_operation(changed, self.token)
        self.assertEqual(caught.exception.status, 409)

    def test_legacy_hash_and_receipt_unchanged(self):
        body = dict(type='RENEW', tenant_id='t', idempotency_key='legacy', subscription_months=1)
        first = self.store.machine_operation(body, self.token)
        self.assertNotIn('reconciliation_receipt', first)
        self.assertNotIn('accepted_at', first)
        expected = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'),
                                            ensure_ascii=False).encode()).hexdigest()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT request_hash FROM machine_operations').fetchone()[0], expected)
        self.assertEqual(first, self.store.machine_operation(body, self.token))
        self.assertNotIn('reconciliation_receipt', self.status())

    def test_user_department_paths_and_missing_aliases(self):
        for operation, field, value in [('USER_ASSIGN', 'user_id', 'user'),
                ('USER_UNASSIGN', 'user_id', 'user'), ('USER_UNASSIGN', 'user_id', 'mock-user-absent'),
                ('DEPT_CREATE', 'department_id', 'dept'), ('DEPT_REMOVE', 'department_id', 'dept'),
                ('DEPT_REMOVE', 'department_id', 'mock-dept-absent')]:
            with self.subTest(operation=operation, value=value):
                fields = {field: value, 'idempotency_key': operation + value,
                          'reconciliation_context': self.context_for(operation, operation + value)}
                first = self.call(operation, **fields)
                self.assertEqual(first, self.call(operation, **fields))
                self.assertEqual(self.status()['reconciliation_receipt'], first['reconciliation_receipt'])

    def test_version_receipt_survives_subsequent_context(self):
        change = self.call('CHANGE', change_id='change', direction='target', to_version='v2',
                           idempotency_key='change', reconciliation_context=self.context_for('CHANGE', 'change'))
        self.call('RENEW', subscription_months=1, reconciliation_context=self.context)
        self.assertEqual(self.status()['effective_at'], change['effective_at'])
        self.assertEqual(self.status()['version'], 'v2')

    def test_refund_and_close_receipts_replay_with_actual_expiry(self):
        fields = dict(action='refund_shorten', protocol='refund_shorten_v1', refund_id='refund',
            product_id='p', enterprise_id='e', subscription_id='s', idempotency_key='refund-shorten:refund',
            expected_expires_at='2090-01-01T00:00:00Z', expires_at='2089-01-01T00:00:00Z',
            reconciliation_context=self.context_for('RENEW', 'refund-shorten:refund'))
        first = self.call(**fields)
        self.assertEqual(first, self.call(**fields))
        self.assertEqual(first['reconciliation_receipt']['expires_at'], first['expires_at'])
        closed = self.call('CLOSE', idempotency_key='close', reconciliation_context=self.context_for('CLOSE', 'close'))
        self.assertEqual(closed, self.call('CLOSE', idempotency_key='close', reconciliation_context=self.context_for('CLOSE', 'close')))
        self.assertEqual(self.status()['reconciliation_receipt'], closed['reconciliation_receipt'])

    def test_status_reports_saved_context_without_replacing_it(self):
        self.call(subscription_months=1, reconciliation_context=self.context)
        query = dict(self.context, event_id='status-query')
        status = self.call('STATUS', enterprise_id='e', subscription_id='s', reconciliation_context=query)
        self.assertEqual(status['reconciliation_context'], self.context)
        self.assertEqual(self.store.tenant('t')['reconciliation_context'], self.context)

    def test_invalid_contexts_fail_atomically(self):
        invalid = [None, [], {}, dict(self.context, schema_version=True),
            dict(self.context, schema_version=2), dict(self.context, effective_at_semantics='unknown'),
            dict(self.context, affected_orders={}), dict(self.context, event_id=' '),
            dict(self.context, extra=True)]
        for date in ('2026-02-30T00:00:00Z', '2026-01-01', '2026-01-01T00:00:00',
                     '2026-01-01T00:00:00+01:00', 123):
            invalid.append(dict(self.context, effective_at=date))
        invalid.extend([dict(self.context, order_starts_at='2026-01-02T00:00:00Z'),
            dict(self.context, order_starts_at='2026-01-02T00:00:00Z', order_ends_at='2026-01-01T00:00:00Z'),
            dict(self.context, period_status='provisional', effective_at='2026-01-01T00:00:00Z')])
        before = self.store.tenant('t')
        for context in invalid:
            with self.subTest(context=context):
                with self.assertRaises(module.MachineOperationError) as caught:
                    self.call(reconciliation_context=context, subscription_months=1)
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(before, self.store.tenant('t'))

    def test_identity_validation_uses_stored_tenant(self):
        for key in ('product_id', 'enterprise_id', 'subscription_id', 'tenant_id'):
            with self.subTest(key=key):
                with self.assertRaises(module.MachineOperationError) as caught:
                    self.call(reconciliation_context=dict(self.context, **{key: 'wrong'}), subscription_months=1)
                self.assertEqual(caught.exception.status, 403 if key == 'product_id' else 409)

    def test_affected_order_spans(self):
        row = dict(order_id='order', order_no='number', term_id=None,
            order_starts_at='2089-01-01T00:00:00Z', order_ends_at='2090-01-01T00:00:00+00:00',
            remaining_starts_at='2089-06-01T00:00:00Z', remaining_ends_at='2090-01-01T00:00:00Z')
        context = dict(self.context, affected_orders=[row])
        self.call(subscription_months=1, reconciliation_context=context)
        for changes in ({'remaining_ends_at': '2091-01-01T00:00:00Z'},
                        {'remaining_starts_at': '2088-01-01T00:00:00Z'},
                        {'remaining_ends_at': None}, {'term_id': []}):
            with self.subTest(changes=changes):
                with self.assertRaises(module.MachineOperationError):
                    self.call(subscription_months=1, reconciliation_context=dict(context,
                        affected_orders=[dict(row, **changes)]))
        obsolete = dict(row)
        obsolete['segment_id'] = obsolete.pop('term_id')
        with self.assertRaises(module.MachineOperationError) as caught:
            self.call(subscription_months=1, reconciliation_context=dict(context, affected_orders=[obsolete]))
        self.assertEqual(caught.exception.status, 400)

    def test_provisional_open_and_confirmed_tenant_without_order(self):
        context = self.context_for('OPEN', 'open', tenant_id=None, period_status='provisional')
        body = dict(self.open, tenant_id='new', reconciliation_context=context, idempotency_key='open')
        result = self.store.machine_operation(body, self.token)
        self.assertEqual(result['reconciliation_context'], context)
        self.assertEqual(result, self.store.machine_operation(body, self.token))
        self.call(subscription_months=1, reconciliation_context=self.context)

    def test_event_binding_order_pairs_and_bounded_affected_orders(self):
        row = dict(order_id='order', order_no='number', order_starts_at=None,
            order_ends_at=None, remaining_starts_at=None, remaining_ends_at=None, term_id=None)
        invalid = [self.context_for('OPEN', 'bound'), self.context_for('RENEW', 'wrong'),
            self.context_for('RENEW', 'bound', order_id='unpaired'),
            self.context_for('RENEW', 'bound', order_no='unpaired'),
            self.context_for('RENEW', 'bound', affected_orders=[dict(row, order_no=None)]),
            self.context_for('RENEW', 'bound', affected_orders=[row] * 1001)]
        before = self.store.tenant('t')
        for context in invalid:
            with self.subTest(event=context['event_type'], count=len(context['affected_orders'])):
                with self.assertRaises(module.MachineOperationError) as caught:
                    self.call(subscription_months=1, idempotency_key='bound', reconciliation_context=context)
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(self.store.tenant('t'), before)
        self.call(subscription_months=1, idempotency_key='bound', reconciliation_context=
            self.context_for('RENEW', 'bound', affected_orders=[row] * 1000))

    def test_shared_helper_open_renew_change_refund_and_old_replay(self):
        order = SimpleNamespace(id='order', order_no='ORDER-1')

        def envelope(base, operation, key):
            return shared.event_context(base, event_id=key, event_type=operation,
                product_id='p', enterprise_id='e', subscription_id='s', tenant_id='flow')

        provisional = shared.reconciliation_context(order=order,
            effective_at_semantics='provider_receipt', provisional=True)
        opened_body = dict(self.open, tenant_id='flow', idempotency_key='flow-open',
            reconciliation_context=envelope(provisional, 'OPEN', 'flow-open'))
        opened = self.store.machine_operation(opened_body, self.token)
        confirmed = shared.reconciliation_context(order=order,
            starts_at='2089-01-01T00:00:00Z', ends_at='2091-01-01T00:00:00Z',
            subscription_expires_at='2091-01-01T00:00:00Z',
            effective_at='2089-01-01T00:00:00Z', effective_at_semantics='platform_delivery')
        self.store.machine_operation(dict(type='RENEW', tenant_id='flow', idempotency_key='flow-renew',
            expires_at='2091-01-01T00:00:00Z',
            reconciliation_context=envelope(confirmed, 'RENEW', 'flow-renew')), self.token)
        changed_context = shared.change_context({'segments': [dict(order_id=order.id,
            order_no=order.order_no, starts_at='2089-01-01T00:00:00Z',
            ends_at='2091-01-01T00:00:00Z', remaining_starts_at='2089-06-01T00:00:00Z',
            remaining_ends_at='2091-01-01T00:00:00Z')]}, order, '2091-01-01T00:00:00Z',
            [dict(order_id=order.id, term_id='term')])
        changed = self.store.machine_operation(dict(type='CHANGE', tenant_id='flow',
            idempotency_key='flow-change', change_id='flow-change', direction='target', to_version='v2',
            reconciliation_context=envelope(changed_context, 'CHANGE', 'flow-change')), self.token)
        cutoff = '2090-06-01T00:00:00Z'
        refund_context = shared.reconciliation_context(order=order,
            starts_at='2089-01-01T00:00:00Z', ends_at='2091-01-01T00:00:00Z',
            subscription_expires_at=cutoff, effective_at=cutoff,
            effective_at_semantics='entitlement_cutoff')
        body = dict(type='RENEW', tenant_id='flow', product_id='p', enterprise_id='e', subscription_id='s',
            action='refund_shorten', protocol='refund_shorten_v1', refund_id='flow-refund',
            idempotency_key='refund-shorten:flow-refund', expected_expires_at='2091-01-01T00:00:00Z',
            expires_at=cutoff, reconciliation_context=envelope(refund_context, 'RENEW', 'refund-shorten:flow-refund'))
        refunded = self.store.machine_operation(body, self.token)
        self.assertEqual(refunded, self.store.machine_operation(body, self.token))
        self.assertEqual(opened, self.store.machine_operation(opened_body, self.token))
        status = self.call('STATUS', enterprise_id='e', subscription_id='s')
        flow_status = self.store.machine_operation(dict(type='STATUS', tenant_id='flow',
            enterprise_id='e', subscription_id='s', reconciliation_context=
            envelope(provisional, 'STATUS', 'flow-status')), self.token)
        self.assertEqual(flow_status['reconciliation_context'], body['reconciliation_context'])
        self.assertEqual(flow_status['reconciliation_receipt'], refunded['reconciliation_receipt'])
        self.assertEqual(flow_status['effective_at'], changed['effective_at'])
        self.assertEqual(flow_status['expires_at'], '2090-06-01T00:00:00.000000+00:00')
        self.assertNotIn('reconciliation_context', status)


if __name__ == '__main__':
    unittest.main()
