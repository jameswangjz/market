"""Focused monitor tests on isolated app routes and transactional notifications."""
import importlib
import json
import unittest
from unittest.mock import patch

import test_ticket_sla_clocks as fixture


class TicketSlaMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.TicketSlaClockTests.setUpClass.__func__(cls)
        if not hasattr(cls.m, 'ticket_sla_monitor'):
            module = importlib.import_module(cls.m.__package__ + '.ticket_sla_monitor')
            cls.m.ticket_sla_monitor = module.install(vars(cls.m))

    tearDownClass = classmethod(fixture.TicketSlaClockTests.tearDownClass.__func__)
    setUp = fixture.TicketSlaClockTests.setUp
    tearDown = fixture.TicketSlaClockTests.tearDown
    request = fixture.TicketSlaClockTests.request
    create_order = fixture.TicketSlaClockTests.create_order
    create = fixture.TicketSlaClockTests.create
    act = fixture.TicketSlaClockTests.act
    advance = fixture.TicketSlaClockTests.advance
    counts = fixture.TicketSlaClockTests.counts

    def evaluate(self, ticket):
        with self.m.SessionLocal() as db:
            result = self.m.ticket_sla_monitor['evaluate'](db, ticket['id'], at=self.at)
            db.commit()
            return result

    def alerts(self):
        with self.m.SessionLocal() as db:
            return db.query(self.m.ticket_sla_monitor['Alert']).count()

    def test_thresholds_notify_once_per_round_and_risk(self):
        ticket = self.create(priority='urgent')
        self.assertEqual(self.evaluate(ticket)['alerts'], 0)
        self.advance(288 * 60)
        self.assertEqual(self.evaluate(ticket)['alerts'], 1)
        count = self.counts()
        self.assertEqual(self.evaluate(ticket)['alerts'], 0)
        self.assertEqual(self.counts(), count)
        self.advance(73 * 60)
        self.assertEqual(self.evaluate(ticket)['alerts'], 1)
        self.assertEqual(self.alerts(), 2)

    def test_notification_failure_rolls_back_clock_alert_and_audit(self):
        ticket = self.create(priority='urgent')
        before = self.counts()
        self.advance(288 * 60)
        with patch.object(self.m, 'notify_business_event', side_effect=RuntimeError('outbox failed')):
            with self.assertRaisesRegex(RuntimeError, 'outbox failed'):
                self.evaluate(ticket)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.alerts(), 0)
        self.assertEqual(self.evaluate(ticket)['alerts'], 1)

    def test_audit_failure_rolls_back_already_created_notifications(self):
        ticket = self.create(priority='urgent')
        before = self.counts()
        self.advance(288 * 60)
        original = self.m.audit
        def fail_alert_audit(db, actor, action, *args, **kwargs):
            if action == 'notify_ticket_sla_threshold':
                raise RuntimeError('audit failed')
            return original(db, actor, action, *args, **kwargs)
        with patch.object(self.m, 'audit', side_effect=fail_alert_audit):
            with self.assertRaisesRegex(RuntimeError, 'audit failed'):
                self.evaluate(ticket)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.alerts(), 0)

    def test_new_rectification_round_can_warn_again_without_duplicates(self):
        self.snapshot['metrics']['rectification'].update(target_seconds=60, warning_seconds=48)
        self.m.freeze_sla_snapshot.return_value = self.snapshot
        ticket = self.act(self.create(), 'respond', 'provider')
        ticket = self.act(ticket, 'submit_resolution', 'provider')
        ticket = self.act(ticket, 'reject_resolution', 'buyer')
        self.advance(48)
        self.assertEqual(self.evaluate(ticket)['alerts'], 1)
        ticket = self.act(ticket, 'submit_resolution', 'provider')
        ticket = self.act(ticket, 'reject_resolution', 'buyer')
        self.advance(48)
        self.assertEqual(self.evaluate(ticket)['alerts'], 1)
        self.assertEqual(self.evaluate(ticket)['alerts'], 0)
        self.assertEqual(self.alerts(), 2)

    def test_unknown_history_and_calendar_do_not_fabricate_alerts(self):
        ticket = self.create()
        with self.m.SessionLocal() as db:
            row = db.get(self.m.AfterSalesTicket, ticket['id'])
            row.evidence_status = 'unknown'
            db.commit()
        self.advance(1000000)
        self.assertEqual(self.evaluate(ticket)['reason'], 'historical_evidence_unknown')
        self.assertEqual(self.alerts(), 0)

    def test_missing_support_calendar_does_not_raise_normal_clock_alert(self):
        ticket = self.create()
        with self.m.SessionLocal() as db:
            row = db.get(self.m.AfterSalesTicket, ticket['id'])
            frozen = json.loads(row.sla_snapshot_json)
            frozen['calendar'] = None
            row.sla_snapshot_json = json.dumps(frozen)
            db.commit()
        self.advance(1000000)
        self.assertEqual(self.evaluate(ticket)['alerts'], 0)

    def test_worker_uses_real_hooks_and_idempotent_paging(self):
        ticket = self.create(priority='urgent')
        self.advance(288 * 60)
        worker = importlib.import_module(self.m.__package__ + '.ticket_sla_worker')
        first = worker.run(vars(self.m))
        self.assertEqual((first['evaluated'], first['alerts'], first['failures'], first['complete']),
                         (1, 1, 0, True))
        self.assertEqual(worker.run(vars(self.m))['alerts'], 0)
        with self.m.SessionLocal() as db:
            self.assertEqual(self.m.ticket_sla_monitor['page'](db, after_id=ticket['id']), [])
            with self.assertRaises(ValueError):
                self.m.ticket_sla_monitor['page'](db, limit=0)

    def test_worker_failure_is_visible_and_not_partial_commit(self):
        self.create(priority='urgent')
        self.advance(288 * 60)
        worker = importlib.import_module(self.m.__package__ + '.ticket_sla_worker')
        before = self.counts()
        with patch.object(self.m, 'notify_business_event', side_effect=RuntimeError('outbox failed')):
            with self.assertLogs(level='ERROR'):
                result = worker.run(vars(self.m))
        self.assertEqual(result['failures'], 1)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.alerts(), 0)

    def test_worker_budget_exhaustion_is_not_success(self):
        self.create()
        worker = importlib.import_module(self.m.__package__ + '.ticket_sla_worker')
        with patch.object(worker.time, 'monotonic', side_effect=[0, 211]):
            result = worker.run(vars(self.m))
        self.assertFalse(result['complete'])
        self.assertEqual(result['evaluated'], 0)
        for budget in (0, -1, True, float('nan'), float('inf')):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                worker.run(vars(self.m), max_seconds=budget)


if __name__ == '__main__':
    unittest.main()
