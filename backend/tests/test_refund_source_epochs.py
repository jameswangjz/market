"""Financial source identity is independent of Redis process identity."""
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import HTTPException

from app.refund_segment_store import EPOCH, STREAM, source_context
from app import refund_segment_boundary as boundary


class SourceContextTests(unittest.TestCase):
    def test_legacy_restart_still_fails(self):
        cursor = SimpleNamespace(redis_run_id='a' * 40)
        db = Mock()
        db.get.return_value = cursor
        client = Mock()
        client.info.return_value = {'run_id': 'b' * 40}
        with self.assertRaises(HTTPException):
            source_context({'api_charge_ledger': {'Cursor': object}}, db, client)

    def test_legacy_cursor_and_epoch_are_required_to_match(self):
        db, client = Mock(), Mock()
        db.get.return_value = SimpleNamespace(redis_run_id='a' * 40)
        client.info.return_value = {'run_id': 'a' * 40}
        client.hget.return_value = 'b' * 40
        with self.assertRaises(HTTPException):
            source_context({'api_charge_ledger': {'Cursor': object}}, db, client)
        client.hget.return_value = 'a' * 40
        self.assertEqual(STREAM, source_context(
            {'api_charge_ledger': {'Cursor': object}}, db, client)[0])

    def test_managed_hook_owns_physical_registration_validation(self):
        cursor = SimpleNamespace(redis_run_id='c' * 40)
        context = (STREAM + ':' + cursor.redis_run_id, cursor)
        hook, db, client = Mock(return_value=context), Mock(), Mock()
        self.assertEqual(context, source_context(
            {'api_charge_ledger': {'source_context': hook}}, db, client))
        hook.assert_called_once_with(db, client)
        client.info.assert_not_called()
        hook.side_effect = HTTPException(409, 'unregistered Redis process')
        with self.assertRaises(HTTPException):
            source_context({'api_charge_ledger': {'source_context': hook}}, db, client)

    def switch(self, activated_at):
        cursor = SimpleNamespace(redis_run_id='c' * 40, activated_at=activated_at)
        period = dict(start_at=100, end_at=200, period_key='period', route_key='route', monthly_quota=10)
        policy = dict(mode='subscription', status='active', quota_scope='scope',
                      subscription_periods=json.dumps([period]))
        client = Mock()
        client.eval.return_value = '{}'
        boundary.freeze_policy_switch(client, credential_key='market:apisix:credential:test',
            change_id='change', direction='target', expected_policy=policy,
            target_policy=policy, periods=[period], source_context=(STREAM + ':' + 'c' * 40, cursor))
        return client.eval.call_args.args

    def test_boundary_reads_managed_stream_and_pins_stable_epoch(self):
        args = self.switch(datetime.fromtimestamp(99, timezone.utc))
        self.assertEqual(STREAM + ':' + 'c' * 40, args[5])
        self.assertEqual('c' * 40, json.loads(args[-1])['source_run_id'])

    def test_boundary_cannot_promote_period_across_activation_gap(self):
        with self.assertRaisesRegex(ValueError, 'predates source activation'):
            self.switch(datetime.fromtimestamp(101, timezone.utc))


if __name__ == '__main__':
    unittest.main()
