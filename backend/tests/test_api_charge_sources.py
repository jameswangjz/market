"""Durable source transitions against the existing real SQLite fixture."""
import hashlib
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import select

from app.api_charge_ledger import EPOCH, STREAM
import test_api_charge_ledger as legacy


class SourceTests(legacy.ChargeLedgerTests):
    def setUp(self):
        super().setUp()
        self.marker = {'run_id': 'a' * 40}
        self.ledger_client.hgetall.side_effect = lambda key: dict(self.marker)
        self.ledger_client.scan_iter.return_value = []
        self.ledger_client.xrange.return_value = []
        self.ledger_client.xread.return_value = []
        self.ledger_client.pttl.return_value = -1
        self.ledger_client.eval.side_effect = self.cas

    def cas(self, script, count, key, before, after, size):
        import json
        if self.marker != json.loads(before):
            return 0
        self.marker = json.loads(after)
        return 1

    def test_open_preserves_legacy_and_audits_without_usage(self):
        with self.m.SessionLocal() as db:
            old = db.get(self.hooks['Cursor'], STREAM)
            snapshot = (old.last_id, old.redis_run_id, old.activated_at)
            result = self.hooks['open_epoch'](db, self.ledger_client, 'operator', 'lost Redis')
            active, cursor = self.hooks['source_context'](db, self.ledger_client)
            self.assertEqual(active, STREAM + ':' + result['source_id'])
            self.assertEqual(cursor.last_id, '0-0')
            self.assertEqual(snapshot, (old.last_id, old.redis_run_id, old.activated_at))
            self.assertEqual(db.query(self.hooks['Charge']).count(), 0)
            self.assertEqual(db.scalar(select(self.hooks['Gap'])).reason, 'lost Redis')
            gap = db.scalar(select(self.hooks['Gap']))
            self.assertEqual((gap.status, gap.missing_count, gap.old_source_id),
                             ('unresolved', None, snapshot[1]))
            self.assertEqual((result['gap_status'], result['missing_count']), ('unresolved', None))
            self.marker.clear()
            with self.assertRaises(ValueError):
                self.hooks['source_context'](db, self.ledger_client)

    def test_pending_activation_retry_reuses_gap_and_identity(self):
        with self.m.SessionLocal() as db:
            with patch.object(self.ledger_client, 'eval', side_effect=ConnectionError):
                with self.assertRaises(ConnectionError):
                    self.hooks['open_epoch'](db, self.ledger_client, 'op', 'gap')
            pending = db.scalar(select(self.hooks['SourceGeneration']))
            identity = pending.id
            self.assertEqual(pending.status, 'pending')
            with self.assertRaises(ValueError):
                self.hooks['source_context'](db, self.ledger_client)
            result = self.hooks['open_epoch'](db, self.ledger_client, 'op', 'retry')
            self.assertEqual(result['source_id'], identity)
            self.assertEqual(db.query(self.hooks['Gap']).count(), 1)

    def test_admin_open_abandons_pending_after_physical_restart(self):
        with self.m.SessionLocal() as db:
            with patch.object(self.ledger_client, 'eval', return_value=0):
                with self.assertRaises(ValueError):
                    self.hooks['open_epoch'](db, self.ledger_client, 'op', 'first gap')
            pending = db.scalar(select(self.hooks['SourceGeneration']))
            cursor = db.get(self.hooks['Cursor'], pending.stream)
            snapshot = (cursor.last_id, cursor.redis_run_id, cursor.activated_at)
            self.ledger_client.info.return_value = {'run_id': 'b' * 40}
            with self.assertRaises(ValueError):
                self.hooks['source_context'](db, self.ledger_client)
            result = self.hooks['open_epoch'](db, self.ledger_client, 'admin', 'pending process lost')
            self.assertNotEqual(result['source_id'], pending.id)
            db.refresh(pending)
            db.refresh(cursor)
            self.assertEqual(pending.status, 'abandoned')
            self.assertEqual(snapshot, (cursor.last_id, cursor.redis_run_id, cursor.activated_at))
            import json
            self.assertEqual(json.loads(pending.audit_log)[-1],
                dict(action='abandon', actor='admin', reason='pending process lost'))
            gaps = list(db.scalars(select(self.hooks['Gap'])))
            self.assertEqual(len(gaps), 2)
            new_gap = next(g for g in gaps if g.source_id == result['source_id'])
            self.assertEqual(new_gap.old_source_id, pending.id)
            self.assertEqual(result['audits'], [dict(action='open', actor='admin', reason='pending process lost')])
            self.hooks['source_context'](db, self.ledger_client)

    def rotate_on_counter_read(self, db):
        term = db.get(self.m.subscription_terms['Term'], 'refund-segment')
        with patch.object(self.m, 'now', return_value=term.starts_at - timedelta(days=1)):
            initial = self.hooks['open_epoch'](db, self.ledger_client, 'op', 'first gap')
        self.rotated_source = None
        def rotate(key):
            if self.rotated_source is None:
                self.rotated_source = self.hooks['open_epoch'](db, self.ledger_client, 'op', 'new gap')
            return None
        self.ledger_client.get.side_effect = rotate
        return initial, term

    def test_summary_rejects_healthy_source_rotation_during_counter_read(self):
        with self.m.SessionLocal() as db:
            initial, term = self.rotate_on_counter_read(db)
            with self.assertRaises(self.module.PlanUnavailable):
                self.summary(db)
            self.assertNotEqual(initial['source_id'], self.rotated_source['source_id'])
            self.hooks['source_context'](db, self.ledger_client)

    def test_window_rejects_healthy_source_rotation_during_counter_read(self):
        from app.subscription_calendar import CalendarAnchor
        with self.m.SessionLocal() as db:
            initial, term = self.rotate_on_counter_read(db)
            bounds = CalendarAnchor(term.starts_at).period(0)
            self.ledger_client.xrevrange.return_value = []
            with self.assertRaises(self.module.PlanUnavailable):
                self.hooks['window_summary'](db, order_id=self.order_id, segment_id=term.id,
                    start=term.starts_at, end=self.at, as_of=self.at,
                    source_run_id=initial['source_id'], boundary_id='0-0', scope=term.combination_id,
                    periods=[dict(order_id=self.order_id, segment_id=term.id,
                        period_key='refund-combo:1:0', start_at=int(bounds.start.timestamp()),
                        end_at=int(bounds.end.timestamp()), used_at_switch=0)],
                    receipt_evidence=('receipt:id:hash',))
            self.assertNotEqual(initial['source_id'], self.rotated_source['source_id'])
            self.hooks['source_context'](db, self.ledger_client)

    def test_legacy_context_without_cursor_has_no_side_effects(self):
        with self.m.SessionLocal() as db:
            db.delete(db.get(self.hooks['Cursor'], STREAM))
            db.commit()
            self.ledger_client.reset_mock()
            self.assertEqual(self.hooks['source_context'](db, self.ledger_client), (STREAM, None))
            self.assertEqual(db.query(self.hooks['Cursor']).count(), 0)
            self.ledger_client.hsetnx.assert_not_called()
            self.ledger_client.hset.assert_not_called()
            self.ledger_client.info.assert_not_called()

    def test_partial_activation_lost_reply_retries_without_new_gap(self):
        def lost_reply(*args):
            self.cas(*args)
            raise ConnectionError('reply lost after activation')
        with self.m.SessionLocal() as db:
            with patch.object(self.ledger_client, 'eval', side_effect=lost_reply):
                with self.assertRaises(ConnectionError):
                    self.hooks['open_epoch'](db, self.ledger_client, 'op', 'gap')
            identity = self.marker['run_id']
            self.assertEqual(db.scalar(select(self.hooks['SourceGeneration'])).status, 'pending')
            self.hooks['open_epoch'](db, self.ledger_client, 'op', 'retry')
            self.assertEqual(self.marker['run_id'], identity)
            self.assertEqual(db.query(self.hooks['Gap']).count(), 1)

    def test_activation_cas_conflict_requires_original_snapshot_for_retry(self):
        with self.m.SessionLocal() as db:
            original = dict(self.marker)
            with patch.object(self.ledger_client, 'eval', return_value=0):
                with self.assertRaises(ValueError):
                    self.hooks['open_epoch'](db, self.ledger_client, 'op', 'gap')
            self.marker = {'run_id': 'c' * 40}
            with self.assertRaises(ValueError):
                self.hooks['open_epoch'](db, self.ledger_client, 'op', 'retry')
            self.marker = original
            self.hooks['open_epoch'](db, self.ledger_client, 'op', 'retry')
            self.assertEqual(db.query(self.hooks['Gap']).count(), 1)

    def test_seal_hash_failure_can_retry_after_rollback(self):
        with self.m.SessionLocal() as db:
            self.hooks['open_epoch'](db, self.ledger_client, 'op', 'gap')
            with patch.object(self.ledger_client, 'xrange', side_effect=ConnectionError):
                with self.assertRaises(ConnectionError):
                    self.hooks['seal_epoch'](db, self.ledger_client, 'op', 'maintenance')
            db.rollback()
            self.assertEqual(self.marker['status'], 'paused')
            with self.assertRaises(ValueError):
                self.hooks['source_context'](db, self.ledger_client)
            self.hooks['seal_epoch'](db, self.ledger_client, 'op', 'maintenance retry')
            row = db.scalar(select(self.hooks['SourceGeneration']))
            self.assertEqual(row.status, 'paused')
            self.assertIsNotNone(row.sealed_hash)

    def test_daily_quota_value_expiry_and_disappearance_prevent_resume(self):
        key = 'market:apisix:quota:subscription:day:scope:2031-01-01'
        self.ledger_client.scan_iter.side_effect = lambda match: [key] if ':day:' in match else []
        self.ledger_client.get.return_value = '4'
        self.ledger_client.execute_command.return_value = 1925078400000
        with self.m.SessionLocal() as db:
            self.hooks['open_epoch'](db, self.ledger_client, 'op', 'gap')
            self.hooks['seal_epoch'](db, self.ledger_client, 'op', 'maintenance')
            for value, expiry, present in [('0', 1925078400000, True),
                                            ('4', 1925078400001, True), ('4', -2, True),
                                            ('4', 1925078400000, False)]:
                self.ledger_client.get.return_value = value
                self.ledger_client.execute_command.return_value = expiry
                self.ledger_client.scan_iter.side_effect = lambda match: [key] if present and ':day:' in match else []
                with self.assertRaises(ValueError):
                    self.hooks['resume_epoch'](db, self.ledger_client, 'op', 'restart')
            self.ledger_client.scan_iter.side_effect = lambda match: [key] if ':day:' in match else []
            self.ledger_client.get.return_value = '4'
            self.ledger_client.execute_command.return_value = 1925078400000
            self.hooks['resume_epoch'](db, self.ledger_client, 'op', 'restart')

    def test_resume_cas_failure_snapshot_mutation_and_cursor_preservation(self):
        with self.m.SessionLocal() as db:
            result = self.hooks['open_epoch'](db, self.ledger_client, 'op', 'gap')
            cursor = db.get(self.hooks['Cursor'], result['stream'])
            cursor.last_id = '12-3'
            db.commit()
            snapshot = (cursor.stream, cursor.last_id, cursor.redis_run_id, cursor.activated_at)
            self.hooks['seal_epoch'](db, self.ledger_client, 'op', 'maintenance')
            sealed_marker = dict(self.marker)
            self.ledger_client.info.return_value = {'run_id': 'b' * 40}
            with patch.object(self.ledger_client, 'eval', return_value=0):
                with self.assertRaises(ValueError):
                    self.hooks['resume_epoch'](db, self.ledger_client, 'op', 'restart')
            self.marker['not_before_us'] = '1'
            with self.assertRaises(ValueError):
                self.hooks['resume_epoch'](db, self.ledger_client, 'op', 'retry')
            self.marker = sealed_marker
            self.hooks['resume_epoch'](db, self.ledger_client, 'op', 'retry')
            db.refresh(cursor)
            self.assertEqual(snapshot, (cursor.stream, cursor.last_id, cursor.redis_run_id, cursor.activated_at))

    def test_planned_restart_rebinds_only_sealed_unchanged_evidence(self):
        with self.m.SessionLocal() as db:
            self.hooks['open_epoch'](db, self.ledger_client, 'op', 'initial gap')
            self.hooks['seal_epoch'](db, self.ledger_client, 'op', 'maintenance')
            self.assertEqual(self.marker['status'], 'paused')
            self.ledger_client.info.return_value = {'run_id': 'b' * 40}
            self.hooks['resume_epoch'](db, self.ledger_client, 'op', 'restart complete')
            self.assertEqual(self.marker['process_run_id'], 'b' * 40)
            self.hooks['source_context'](db, self.ledger_client)

    def test_unplanned_restart_requires_gap(self):
        with self.m.SessionLocal() as db:
            self.hooks['open_epoch'](db, self.ledger_client, 'op', 'initial gap')
            self.ledger_client.info.return_value = {'run_id': 'b' * 40}
            with self.assertRaises(ValueError):
                self.hooks['resume_epoch'](db, self.ledger_client, 'op', 'restart')

    def test_changed_sealed_stream_refuses_resume(self):
        with self.m.SessionLocal() as db:
            self.hooks['open_epoch'](db, self.ledger_client, 'op', 'initial gap')
            self.hooks['seal_epoch'](db, self.ledger_client, 'op', 'maintenance')
            self.ledger_client.xrange.return_value = [('1-0', {'quantity': '1'})]
            with self.assertRaises(ValueError):
                self.hooks['resume_epoch'](db, self.ledger_client, 'op', 'restart')

    def test_managed_event_uses_namespaced_identity_and_real_watermark(self):
        with self.m.SessionLocal() as db, patch.object(self.m, 'now', return_value=self.at - timedelta(seconds=2)):
            result = self.hooks['open_epoch'](db, self.ledger_client, 'op', 'gap')
            self.event['source_run_id'] = result['source_id']
            self.ledger_client.xread.return_value = [(result['stream'], [('1-0', self.event)])]
            self.hooks['ingest'](db, self.ledger_client)
            charge = db.scalar(select(self.hooks['Charge']))
            self.assertEqual(charge.id, hashlib.sha256((result['source_id'] + '|1-0').encode()).hexdigest()[:40])
            self.assertEqual(charge.event_id, '1-0')


# Reuse fixture helpers, without collecting the inherited legacy test methods.
for _name in tuple(dir(legacy.ChargeLedgerTests)):
    if _name.startswith('test_') and _name not in SourceTests.__dict__:
        setattr(SourceTests, _name, None)
