"""Pure mocked restart orchestration; no application, Redis, or host restart."""
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import uuid

import redis_native_restart_acceptance as restart


class RestartTests(unittest.TestCase):
    def setUp(self):
        root = '/tmp/qa-native-' + uuid.uuid4().hex
        self.paths = [Path(root + suffix + '.json') for suffix in ('-manifest', '-ready', '-ack')]
        for path in self.paths:
            self.addCleanup(path.unlink, missing_ok=True)
        self.source, self.old, self.new, self.seal = 'a' * 40, 'b' * 40, 'c' * 40, 'd' * 64
        self.baseline = dict(events=[['101-1', {'quantity': '1'}]], month='2', day='2',
                             day_expires_at=1792110000000, coverage={'baseline': '0'},
                             metadata={'secret': 'private-value'})
        self.state = dict(proof={'charges': 2}, local_flow_done=True, source_run_id=self.source,
                          physical_run_id=self.old, source_stream=restart.r.STREAM + ':' + self.source)
        self.journal = {'database_ids': {'native_refund': self.state}}
        self.redis = Mock()
        self.redis.info.side_effect = [{'run_id': self.old}, {'run_id': self.new}]
        self.db = MagicMock()
        self.m = SimpleNamespace(SessionLocal=MagicMock(), apisix_policy_redis=Mock(return_value=self.redis),
                                 api_charge_ledger={'SourceGeneration': object})
        self.m.SessionLocal.return_value.__enter__.return_value = self.db
        self.before_audits = [{'action': 'open'}]
        self.after_audits = self.before_audits + [dict(action='seal', evidence_sha256=self.seal),
            dict(action='resume', evidence_sha256=self.seal,
                 before_process_run_id=self.old, after_process_run_id=self.new)]
        self.db.get.side_effect = [SimpleNamespace(audit_log=json.dumps(self.before_audits)),
            SimpleNamespace(status='active', process_run_id=self.new, audit_log=json.dumps(self.after_audits))]

    def execute(self, *, after=None, sleep=None, clock=lambda: 0, snapshot_file=None, wrap_failure=False):
        def ack(_):
            self.assertEqual(1, self.redis.close.call_count)
            restart.write_new(self.paths[2], dict(source_id=self.source, evidence_sha256=self.seal,
                before_process_run_id=self.old, after_process_run_id=self.new))
        def flow(path, reviewed):
            self.assertTrue(reviewed)
            try:
                return restart.r.cleanup_with_predelete_retry(self.m, self.journal, path)
            except Exception:
                if wrap_failure:
                    raise restart.r.base.AcceptanceFailure('runtime_cleanup_failed') from None
                raise
        cursor = SimpleNamespace(redis_run_id=self.source)
        with patch.object(restart.r, 'run', side_effect=flow), \
                patch.object(restart.r, 'cleanup_with_predelete_retry', return_value={'status': 'cleaned'}) as cleanup, \
                patch.object(restart.r, 'source_context', return_value=(self.state['source_stream'], cursor)), \
                patch.object(restart, 'snapshot', side_effect=[self.baseline, after or self.baseline]), \
                patch.object(restart.r.base, 'write_manifest') as write:
            self.cleanup, self.write = cleanup, write
            return restart.run(*self.paths, reviewed=True, sleep=sleep or ack, clock=clock,
                               snapshot_file=snapshot_file)

    def test_verified_restart_updates_only_private_ownership_then_cleans(self):
        self.assertEqual({'status': 'cleaned', 'retained_financial_events': 2}, self.execute())
        self.cleanup.assert_called_once()
        self.write.assert_called_once_with(str(self.paths[0]), self.journal)
        self.assertEqual(self.new, self.state['physical_run_id'])
        self.assertEqual(self.seal, self.state['restart_receipt']['seal_sha256'])
        self.assertIs(True, self.state['retain_financial_events_as_audit'])
        self.assertEqual(dict(self.baseline['events']), self.state['validated_events'])
        ready = json.loads(self.paths[1].read_text())
        self.assertEqual(2, ready['financial_events'])
        self.assertEqual(2, ready['monthly_used'])
        self.assertNotIn('financial', ready)
        self.assertEqual(restart.field_hashes(self.baseline), ready['fields_sha256'])
        self.assertNotIn('private-value', json.dumps(ready))
        self.assertEqual(0o600, stat.S_IMODE(self.paths[1].stat().st_mode))
        self.assertEqual(2, self.redis.close.call_count)
        self.assertEqual(2, self.m.apisix_policy_redis.call_count)

    def test_changed_financial_snapshot_refuses_journal_and_cleanup(self):
        with self.assertRaisesRegex(restart.r.base.AcceptanceFailure, 'snapshot_changed'):
            self.execute(after=self.baseline | {'month': '3'})
        self.cleanup.assert_not_called()
        self.write.assert_not_called()
        self.assertEqual(self.old, self.state['physical_run_id'])

    def test_metadata_change_has_public_fixed_labels_even_after_composer_wraps(self):
        with self.assertRaises(restart.SnapshotChanged) as caught:
            self.execute(after=self.baseline | {'metadata': {'secret': 'changed-private'}}, wrap_failure=True)
        diagnostic = caught.exception.diagnostics
        self.assertEqual(['metadata'], diagnostic['changed_fields'])
        self.assertNotEqual(diagnostic['before_fields_sha256']['metadata'],
                            diagnostic['after_fields_sha256']['metadata'])
        self.assertNotIn('private', str(caught.exception))
        self.cleanup.assert_not_called()
        self.write.assert_not_called()

    def test_optional_snapshot_is_new_private_file_before_ready(self):
        snapshot = Path(str(self.paths[0]).replace('-manifest.json', '-snapshot.json'))
        self.addCleanup(snapshot.unlink, missing_ok=True)
        self.execute(snapshot_file=snapshot)
        value = json.loads(snapshot.read_text())
        self.assertEqual(self.baseline, value['before'])
        self.assertEqual(0o600, stat.S_IMODE(snapshot.stat().st_mode))

    def test_mutable_client_values_cannot_change_captured_baseline(self):
        def change_then_ack(_):
            self.baseline['metadata']['secret'] = 'changed-private'
            restart.write_new(self.paths[2], dict(source_id=self.source, evidence_sha256=self.seal,
                before_process_run_id=self.old, after_process_run_id=self.new))
        with self.assertRaises(restart.SnapshotChanged) as caught:
            self.execute(sleep=change_then_ack)
        self.assertEqual(['metadata'], caught.exception.diagnostics['changed_fields'])

    def test_verified_projection_is_public_and_journaled_before_cleanup(self):
        fixture = ProjectionTests()
        fixture.setUp()
        self.baseline = fixture.before
        self.state.update(fixture.state)
        self.redis.time.return_value = (150, 0)
        with patch.object(restart, 'execution_target', return_value=fixture.target) as target:
            result = self.execute(after=fixture.after)
        target.assert_called_once_with(self.m, self.db, self.state)
        self.assertEqual('current_period_monthly_quota_projection', result['metadata_projection']['reason'])
        self.assertEqual(result['metadata_projection'], self.state['restart_receipt']['metadata_projection'])
        self.cleanup.assert_called_once()
        self.assertEqual(self.new, self.state['physical_run_id'])

    def test_missing_retained_seal_audit_refuses_cleanup(self):
        self.after_audits[-1].pop('evidence_sha256')
        self.db.get.side_effect = [SimpleNamespace(audit_log=json.dumps(self.before_audits)),
            SimpleNamespace(status='active', process_run_id=self.new, audit_log=json.dumps(self.after_audits))]
        with self.assertRaisesRegex(restart.r.base.AcceptanceFailure, 'audit_unproven'):
            self.execute()
        self.cleanup.assert_not_called()
        self.write.assert_not_called()

    def test_timeout_preserves_fixture_and_old_ownership(self):
        ticks = iter([0, 121])
        with self.assertRaisesRegex(restart.r.base.AcceptanceFailure, 'ack_timeout'):
            self.execute(sleep=lambda _: self.fail('must be bounded'), clock=lambda: next(ticks))
        self.cleanup.assert_not_called()
        self.write.assert_not_called()
        self.assertEqual(self.old, self.state['physical_run_id'])

    def test_failed_flow_without_proof_cleans_immediately(self):
        self.state.pop('proof')
        self.assertEqual({'status': 'cleaned'}, self.execute(sleep=lambda _: self.fail('must not wait')))
        self.cleanup.assert_called_once()
        self.m.apisix_policy_redis.assert_not_called()
        self.assertFalse(self.paths[1].exists())

    def test_active_local_flow_does_not_wait(self):
        self.state['local_flow_done'] = False
        self.execute(sleep=lambda _: self.fail('must not wait'))
        self.cleanup.assert_called_once()
        self.m.apisix_policy_redis.assert_not_called()

    def test_no_process_restart_refuses_cleanup(self):
        self.redis.info.side_effect = [{'run_id': self.old}, {'run_id': self.old}]
        with self.assertRaisesRegex(restart.r.base.AcceptanceFailure, 'binding_invalid'):
            self.execute()
        self.cleanup.assert_not_called()

    def test_ack_evidence_must_match_durable_audit(self):
        def wrong_ack(_):
            restart.write_new(self.paths[2], dict(source_id=self.source, evidence_sha256='f' * 64,
                before_process_run_id=self.old, after_process_run_id=self.new))
        with self.assertRaisesRegex(restart.r.base.AcceptanceFailure, 'ack_mismatch'):
            self.execute(sleep=wrong_ack)
        self.cleanup.assert_not_called()
        self.write.assert_not_called()
        self.assertEqual(self.old, self.state['physical_run_id'])

    def test_paths_and_existing_files_fail_before_flow(self):
        for path in ('/etc/qa-native-ready.json', '/tmp/qa-native-../ready.json', '/tmp/foreign.json'):
            with self.assertRaises(restart.r.base.AcceptanceFailure):
                restart.private_path(path)
        restart.write_new(self.paths[2], {})
        with patch.object(restart.r, 'run') as flow, self.assertRaises(restart.r.base.AcceptanceFailure):
            restart.run(*self.paths, reviewed=True)
        flow.assert_not_called()

    def test_ack_symlink_and_public_file_refused(self):
        restart.write_new(self.paths[1], {})
        self.paths[2].symlink_to(self.paths[1])
        with self.assertRaises(OSError):
            restart.read_ack(self.paths[2])
        os.chmod(self.paths[1], 0o644)
        with self.assertRaises(restart.r.base.AcceptanceFailure):
            restart.read_ack(self.paths[1])

    def test_audit_must_preserve_history_and_match_both_processes(self):
        for after in (self.after_audits[1:], self.before_audits + [self.after_audits[-1]],
                      self.before_audits + [self.after_audits[1], self.after_audits[-1] |
                                           {'before_process_run_id': self.new}]):
            with self.assertRaises(restart.r.base.AcceptanceFailure):
                restart.verify_resume(self.before_audits, after, self.old, self.new)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.manifest = restart.r.base.fixture_ids('qa-vcs-0123456789ab')
        combo, oid, term, credential, source = ('a' * 32, 'b' * 32, 'c' * 32, 'd' * 32, 'e' * 40)
        self.state = dict(source_stream=restart.r.STREAM + ':' + source, source_run_id=source,
            physical_run_id='f' * 40, event_ids=['101-1', '101-2'], stream_start='100-0', stream_end='102-0',
            order_id=oid, term_id=term, combination_id=combo, credential_id=credential, consumer='owned-consumer')
        coverage = restart.r.COVERAGE + combo + ':' + restart.r.quota_token(combo + ':1:0')
        self.state['extra_keys'] = [coverage]
        self.manifest['consumers'] = [dict(id=credential, name='owned-consumer')]
        self.manifest['quota_windows'] = [dict(scope=combo, period=combo + ':1:0', day='2026-10-15', minute_start=100)]
        raw = dict(order_id=oid, segment_id=term, scope=combo, period_key=combo + ':1:0',
            route_key=self.manifest['routes']['api']['source'], source_run_id=source, quantity='1',
            policy='gateway_actual_charge_v1', charged_at_us='1792100000000000')
        self.redis = Mock()
        self.redis.xrange.side_effect = lambda stream, min, max, count: [(min, raw)]
        self.redis.get.return_value = '2'
        self.redis.pttl.return_value = -1
        self.redis.execute_command.return_value = 1792110000000
        self.redis.hgetall.side_effect = lambda key: ({'baseline': '0'} if key == coverage else
                                                    {'quota_scope': combo, 'secret': 'never-output'})

    def test_exact_snapshot_has_two_real_records_and_day_absolute_expiry(self):
        result = restart.snapshot(self.redis, self.manifest, self.state)
        self.assertEqual(2, len(result['events']))
        self.assertEqual('2', result['month'])
        self.assertEqual(1792110000000, result['day_expires_at'])
        self.assertTrue(all(c.args[0] == self.state['source_stream'] for c in self.redis.xrange.call_args_list))
        self.redis.delete.assert_not_called()
        self.redis.xdel.assert_not_called()

    def test_missing_real_event_or_lost_usage_rejected(self):
        self.redis.xrange.side_effect = lambda *args, **kwargs: []
        with self.assertRaisesRegex(restart.r.base.AcceptanceFailure, 'event_missing'):
            restart.snapshot(self.redis, self.manifest, self.state)
        self.setUp()
        self.redis.get.return_value = '1'
        with self.assertRaisesRegex(restart.r.base.AcceptanceFailure, 'coverage_invalid'):
            restart.snapshot(self.redis, self.manifest, self.state)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.state = dict(order_id='a' * 32, term_id='b' * 32, combination_id='c' * 32)
        self.period = dict(start_at=100, end_at=200, monthly_quota=2,
            order_id=self.state['order_id'], segment_id=self.state['term_id'],
            period_key=self.state['combination_id'] + ':1:0')
        self.target = dict(monthly_quota='10', quota_scope=self.state['combination_id'],
                           status='active', subscription_periods=json.dumps([self.period]))
        self.before = dict(events=[['101-1', {'quantity': '1'}]], month='2', day='2',
            day_expires_at=123456, coverage={'baseline': '0'}, metadata=dict(self.target))
        self.after = self.before | {'metadata': self.target | {'monthly_quota': '2'}}

    def compare(self, before=None, after=None, target=None, at=150):
        return restart.compare_snapshots(before or self.before, after or self.after,
            target=target or self.target, state=self.state, at=at)

    def test_only_frozen_top_level_ten_to_effective_two_is_observed(self):
        receipt = self.compare()
        self.assertEqual(dict(reason='current_period_monthly_quota_projection', field='monthly_quota',
            before='10', after='2', frozen_target='10', effective_period='2'), receipt)
        self.assertIsNone(self.compare(before=self.after))

    def test_wrong_top_level_values_and_reverse_projection_rejected(self):
        for before, after in (('9', '2'), ('10', '3'), ('2', '10')):
            with self.subTest(before=before, after=after), self.assertRaises(restart.SnapshotChanged):
                self.compare(before=self.before | {'metadata': self.target | {'monthly_quota': before}},
                             after=self.after | {'metadata': self.target | {'monthly_quota': after}})

    def test_identity_nested_quota_or_counter_changes_are_not_projection(self):
        for field, value in (('status', 'inactive'), ('quota_scope', 'foreign'),
                ('subscription_periods', json.dumps([self.period | {'monthly_quota': 3}]))):
            with self.subTest(field=field), self.assertRaises(restart.SnapshotChanged):
                self.compare(after=self.after | {'metadata': self.after['metadata'] | {field: value}})
        for field in ('events', 'month', 'day', 'day_expires_at', 'coverage'):
            with self.subTest(field=field), self.assertRaises(restart.SnapshotChanged):
                self.compare(after=self.after | {field: 'changed'})

    def test_changed_nested_policy_in_both_snapshots_still_requires_frozen_target(self):
        nested = json.dumps([self.period | {'monthly_quota': 3}])
        with self.assertRaises(restart.SnapshotChanged):
            self.compare(before=self.before | {'metadata': self.target | {'subscription_periods': nested}},
                after=self.after | {'metadata': self.after['metadata'] | {'subscription_periods': nested}})

    def test_period_must_be_current_and_owned(self):
        for at in (99, 200):
            with self.assertRaises(restart.SnapshotChanged):
                self.compare(at=at)
        for changes in ({'order_id': 'foreign'}, {'segment_id': 'foreign'}, {'monthly_quota': 3}):
            target = self.target | {'subscription_periods': json.dumps([self.period | changes])}
            with self.assertRaises(restart.SnapshotChanged):
                self.compare(target=target)


if __name__ == '__main__':
    unittest.main()
