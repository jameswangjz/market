"""Isolated SQLite HTTP tests of clock/event atomicity; no live data or network."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import test_after_sales_workflow as fixture
import test_ticket_calendar as calendar_fixture


class TicketSlaClockTests(unittest.TestCase):
    setUpClass = classmethod(fixture.AfterSalesWorkflowTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.AfterSalesWorkflowTests.tearDownClass.__func__)
    tearDown = fixture.AfterSalesWorkflowTests.tearDown
    request = fixture.AfterSalesWorkflowTests.request
    create_order = fixture.AfterSalesWorkflowTests.create_order
    create = fixture.AfterSalesWorkflowTests.create
    act = fixture.AfterSalesWorkflowTests.act

    def setUp(self):
        fixture.AfterSalesWorkflowTests.setUp(self)
        self.at = datetime(2026, 10, 12, 1, tzinfo=timezone.utc)
        self.time = patch.object(self.m, 'now', side_effect=lambda: self.at)
        self.time.start()
        self.addCleanup(self.time.stop)
        metrics, calendar = importlib.import_module(self.m.__package__ + '.enterprise_sla')._promise('strategic')
        self.snapshot = {'service_level_code': 'strategic', 'metrics': metrics,
                         'calendar': calendar, 'contract_rule_version': 'frozen-contract-test'}
        self.m.freeze_sla_snapshot.return_value = deepcopy(self.snapshot)
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'quality').platform_role = 'delivery_monitor'
            db.commit()

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)

    def sla(self, ticket):
        return self.request('GET', f"/api/after-sales/{ticket['id']}/sla")

    def clock(self, ticket, metric, round=1):
        return next(row for row in self.sla(ticket)['items'] if row['metric'] == metric and row['round'] == round)

    def intervention(self, priority='normal'):
        return self.act(self.create(priority=priority), 'request_intervention', 'buyer')

    def counts(self):
        with self.m.SessionLocal() as db:
            return tuple(db.query(model).count() for model in (
                self.m.AfterSalesEvent, self.m.AuditLog, self.m.TicketSlaClock,
                self.Message, self.Receipt, self.Outbox, self.m.TicketCalendarBinding))

    def clock_state(self):
        with self.m.SessionLocal() as db:
            table = self.m.TicketSlaClock.__table__
            return list(db.execute(select(table).order_by(table.c.id)).tuples())

    def test_platform_response_permissions_and_same_state_without_provider_first_response(self):
        ticket = self.intervention()
        for actor, code in [('buyer', 403), ('provider', 403), ('finance', 404),
                            ('reviewer', 404), ('outsider', 404), ('disabled_ops', 401)]:
            self.act(ticket, 'respond_intervention', actor, code)
        self.advance(60)
        ticket = self.act(ticket, 'respond_intervention', 'quality')
        self.assertEqual(ticket['status'], 'platform_intervention')
        self.assertIsNone(ticket['first_response_at'])
        clock = self.clock(ticket, 'intervention_response')
        self.assertEqual((clock['elapsed_seconds'], clock['lifecycle'], clock['responsibility']),
                         (60, 'completed', 'platform'))
        self.assertEqual(len(clock['source_event_ids']), 2)

    def test_first_only_idempotent_replay_and_payload_conflict(self):
        ticket = self.intervention()
        self.advance(60)
        responded = self.act(ticket, 'respond_intervention', 'ops')
        before = self.counts()
        self.advance(60)
        self.assertEqual(self.act(ticket, 'respond_intervention', 'ops')['version'], responded['version'])
        self.assertEqual(self.counts(), before)
        self.act(ticket, 'respond_intervention', 'ops', 409, reason='Different payload')
        self.act(responded, 'respond_intervention', 'platform', 409)
        self.assertEqual(self.clock(responded, 'intervention_response')['elapsed_seconds'], 60)

    def test_review_without_response_is_insufficient_and_rounds_remain_separate(self):
        ticket = self.intervention()
        self.advance(120)
        ticket = self.act(ticket, 'review_intervention', 'platform')
        self.assertEqual(self.clock(ticket, 'intervention_response')['status'], 'insufficient_data')
        self.assertEqual(self.clock(ticket, 'intervention_review')['elapsed_seconds'], 120)
        ticket = self.act(ticket, 'request_intervention', 'provider')
        self.advance(60)
        ticket = self.act(ticket, 'respond_intervention', 'ops')
        self.advance(60)
        ticket = self.act(ticket, 'review_intervention', 'quality')
        rows = self.sla(ticket)['items']
        responses = [row for row in rows if row['metric'] == 'intervention_response']
        self.assertEqual([row['round'] for row in responses], [1, 2])
        self.assertEqual([row['elapsed_seconds'] for row in responses], [None, 60])
        self.assertEqual(len({row['id'] for row in responses}), 2)

    def test_rectification_wait_pause_intervention_continues_and_multiple_rounds(self):
        ticket = self.act(self.create(), 'respond', 'provider')
        ticket = self.act(ticket, 'submit_resolution', 'provider')
        ticket = self.act(ticket, 'reject_resolution', 'buyer')
        self.advance(60)
        ticket = self.act(ticket, 'request_info', 'provider')
        self.advance(120)
        ticket = self.act(ticket, 'request_intervention', 'buyer')
        self.advance(180)
        ticket = self.act(ticket, 'review_intervention', 'ops')
        self.advance(120)
        ticket = self.act(ticket, 'supplement', 'buyer')
        self.advance(60)
        ticket = self.act(ticket, 'submit_resolution', 'provider')
        self.assertEqual(self.clock(ticket, 'rectification')['elapsed_seconds'], 300)
        self.assertEqual(len(self.clock(ticket, 'rectification')['source_event_ids']), 6)
        self.assertEqual(self.clock(ticket, 'resolution')['elapsed_seconds'], 300)
        ticket = self.act(ticket, 'reject_resolution', 'buyer')
        self.advance(30)
        ticket = self.act(ticket, 'submit_resolution', 'provider')
        self.assertEqual(self.clock(ticket, 'rectification', 2)['elapsed_seconds'], 30)
        self.assertEqual(self.clock(ticket, 'rectification', 1)['elapsed_seconds'], 300)

    def test_urgent_intervention_natural_clock_survives_missing_support_calendar(self):
        self.snapshot['calendar'].update(exclude_public_holidays=True, covered_years=[])
        self.m.freeze_sla_snapshot.return_value = deepcopy(self.snapshot)
        ticket = self.intervention(priority='urgent')
        self.advance(100)
        ticket = self.act(ticket, 'respond_intervention', 'platform')
        result = self.sla(ticket)
        rows = {row['metric']: row for row in result['items']}
        self.assertEqual(result['calendar_status'], 'not_ready')
        self.assertEqual(rows['urgent_intervention_response']['elapsed_seconds'], 100)
        self.assertEqual(rows['urgent_intervention_response']['clock'], 'natural')
        self.assertEqual(rows['intervention_review']['status'], 'insufficient_data')

    def test_closed_unfinished_round_is_not_met_and_elapsed_stops(self):
        ticket = self.act(self.create(), 'respond', 'provider')
        ticket = self.act(ticket, 'submit_resolution', 'provider')
        ticket = self.act(ticket, 'reject_resolution', 'buyer')
        ticket = self.act(ticket, 'request_intervention', 'buyer')
        self.advance(60)
        ticket = self.act(ticket, 'force_close', 'ops')
        self.advance(86400)
        for metric in ('rectification', 'intervention_review'):
            row = self.clock(ticket, metric)
            self.assertEqual((row['elapsed_seconds'], row['status'], row['lifecycle']),
                             (60, 'closed_without_confirmation', 'closed_incomplete'))

    def test_get_cannot_invent_historical_starts_and_missing_event_chain_is_unknown(self):
        ticket = self.create()
        with self.m.SessionLocal() as db:
            db.get(self.m.AfterSalesTicket, ticket['id']).evidence_status = 'historical_unknown'
            db.commit()
        before = self.counts()
        self.assertEqual(self.sla(ticket)['items'], [])
        self.assertEqual(self.counts(), before)
        with self.m.SessionLocal() as db:
            db.get(self.m.AfterSalesTicket, ticket['id']).evidence_status = 'recorded'
            db.query(self.m.AfterSalesEvent).delete()
            db.commit()
        result = self.sla(ticket)
        self.assertEqual(result['status'], 'insufficient_data')
        self.assertEqual(result['items'], [])

    def test_same_timestamp_uses_ticket_version_not_random_event_id(self):
        ticket = self.act(self.create(), 'respond', 'provider')
        ticket = self.act(ticket, 'submit_resolution', 'provider')
        ticket = self.act(ticket, 'reject_resolution', 'buyer')
        ticket = self.act(ticket, 'submit_resolution', 'provider')
        row = self.clock(ticket, 'rectification')
        self.assertEqual((row['elapsed_seconds'], row['lifecycle']), (0, 'completed'))

    def test_stale_cached_writer_loses_and_duplicate_round_constraint_holds(self):
        ticket = self.intervention()
        with self.m.SessionLocal() as reader:
            cached = reader.get(self.m.AfterSalesTicket, ticket['id'])
            user = reader.get(self.m.User, 'platform')
            self.act(ticket, 'respond_intervention', 'ops')
            body_type = importlib.import_module(self.m.__package__ + '.after_sales_workflow').TicketAction
            body = body_type(action='respond_intervention', reason='Concurrent response',
                             expected_version=cached.version, idempotency_key='concurrent-response')
            with self.assertRaises(HTTPException) as raised:
                self.m.after_sales_workflow['act'](reader, user, ticket['id'], body)
            self.assertEqual(raised.exception.status_code, 409)
            reader.rollback()
        with self.m.SessionLocal() as db:
            original = db.scalar(select(self.m.TicketSlaClock))
            db.add(self.m.TicketSlaClock(ticket_id=original.ticket_id, metric=original.metric,
                round=original.round, party=original.party, rule_snapshot_json='{}', calendar_snapshot_json='{}',
                source_event_ids_json='[]', source_ticket_version=1, status='running', lifecycle='open', measured_at=self.at))
            with self.assertRaises(IntegrityError):
                db.flush()
            db.rollback()

    def test_get_and_action_lock_order_precedes_calendar_resolver(self):
        ticket = self.intervention()
        seen = []
        def capture(state):
            statement = state.statement
            if getattr(statement, '_for_update_arg', None) is not None:
                seen.extend(table.name for table in statement.get_final_froms())
        event.listen(Session, 'do_orm_execute', capture)
        try:
            self.sla(ticket)
            self.assertEqual(seen[:2], ['orders', 'after_sales_tickets'])
            seen.clear()
            self.act(ticket, 'respond_intervention', 'ops')
            self.assertEqual(seen[:2], ['orders', 'after_sales_tickets'])
            self.assertNotIn('orders', seen[1:])
        finally:
            event.remove(Session, 'do_orm_execute', capture)

    def test_clock_audit_outbox_failures_roll_back_entire_action(self):
        ticket = self.intervention()
        real_hook, real_audit = self.m.ticket_sla_event, self.m.audit
        def bad_clock(*args):
            real_hook(*args)
            raise RuntimeError('Clock failure after persistence')
        def bad_audit(db, actor, action, *args, **kwargs):
            if action == 'materialize_ticket_sla_clocks':
                raise RuntimeError('Clock audit failure')
            return real_audit(db, actor, action, *args, **kwargs)
        for name, failure in [('ticket_sla_event', bad_clock), ('audit', bad_audit),
                              ('notify_business_event', RuntimeError('Outbox failure'))]:
            before = self.counts()
            before_clocks = self.clock_state()
            self.advance(60)
            with patch.object(self.m, name, side_effect=failure):
                self.act(ticket, 'respond_intervention', 'ops', 500)
            self.assertEqual(self.counts(), before)
            self.assertEqual(self.clock_state(), before_clocks)
            with self.m.SessionLocal() as db:
                self.assertEqual(db.get(self.m.AfterSalesTicket, ticket['id']).version, ticket['version'])
        self.act(ticket, 'respond_intervention', 'ops')

    def test_get_is_idempotent_at_same_instant_and_cannot_cross_enterprise_scope(self):
        ticket = self.intervention()
        first = self.sla(ticket)
        before = self.counts()
        second = self.sla(ticket)
        self.assertEqual(first['items'], second['items'])
        self.assertEqual(self.counts(), before)
        self.request('GET', f"/api/after-sales/{ticket['id']}/sla", actor='outsider', expected=404)
        self.request('GET', f"/api/after-sales/{ticket['id']}/sla?enterprise_id=other_tenant", expected=403)

    def test_calendar_append_is_immutable_and_clock_keeps_original_contract_snapshot(self):
        self.at = datetime(2026, 12, 31, 1, tzinfo=timezone.utc)
        self.snapshot['calendar'].update(exclude_public_holidays=True, covered_years=[2026])
        self.m.freeze_sla_snapshot.return_value = deepcopy(self.snapshot)
        ticket = self.intervention()
        with self.m.SessionLocal() as db:
            original = db.get(self.m.AfterSalesTicket, ticket['id']).sla_snapshot_json
        self.at = datetime(2027, 1, 4, 1, tzinfo=timezone.utc)
        self.assertEqual(self.clock(ticket, 'intervention_review')['status'], 'insufficient_data')
        self.holiday = importlib.import_module(self.m.__package__ + '.holiday_calendar')
        self.Version, self.State = (self.m.holiday_calendar[key] for key in ('Version', 'State'))
        first_version = calendar_fixture.TicketCalendarTests.cache(self, 2027)
        bound = self.clock(ticket, 'intervention_review')
        self.assertIsNotNone(bound['elapsed_seconds'])
        self.assertEqual(bound['calendar_bindings'][0]['calendar_version_id'], first_version)
        self.assertEqual(bound['calendar_snapshot']['covered_years'], [2026])
        self.assertEqual(bound['rule_version'], 'frozen-contract-test')
        calendar_fixture.TicketCalendarTests.cache(self, 2027, '02-01')
        self.assertEqual(self.clock(ticket, 'intervention_review')['calendar_bindings'], bound['calendar_bindings'])
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.AfterSalesTicket, ticket['id']).sla_snapshot_json, original)

    def test_calendar_binding_rolls_back_if_clock_audit_fails(self):
        self.at = datetime(2026, 12, 31, 1, tzinfo=timezone.utc)
        self.snapshot['calendar'].update(exclude_public_holidays=True, covered_years=[2026])
        self.m.freeze_sla_snapshot.return_value = deepcopy(self.snapshot)
        ticket = self.intervention()
        self.holiday = importlib.import_module(self.m.__package__ + '.holiday_calendar')
        self.Version, self.State = (self.m.holiday_calendar[key] for key in ('Version', 'State'))
        calendar_fixture.TicketCalendarTests.cache(self, 2027)
        self.at = datetime(2027, 1, 4, 1, tzinfo=timezone.utc)
        real_audit = self.m.audit
        def fail(db, actor, action, *args, **kwargs):
            if action == 'materialize_ticket_sla_clocks':
                raise RuntimeError('Fail after annual binding')
            return real_audit(db, actor, action, *args, **kwargs)
        before = self.counts()
        with patch.object(self.m, 'audit', side_effect=fail):
            self.request('GET', f"/api/after-sales/{ticket['id']}/sla", expected=500)
        self.assertEqual(self.counts(), before)

    def test_intervention_response_requires_request_evidence_and_nonblank_reason(self):
        ticket = self.intervention()
        self.act(ticket, 'respond_intervention', 'ops', 422, reason='  ')
        with self.m.SessionLocal() as db:
            db.query(self.m.AfterSalesEvent).filter_by(event_type='request_intervention').delete()
            db.commit()
        self.act(ticket, 'respond_intervention', 'ops', 409)

    def test_terminal_event_uses_exact_close_instant_even_with_advancing_wall_clock(self):
        ticket = self.intervention()
        def tick():
            self.at += timedelta(microseconds=1)
            return self.at
        with patch.object(self.m, 'now', side_effect=tick):
            ticket = self.act(ticket, 'force_close', 'ops')
        row = self.clock(ticket, 'intervention_review')
        self.assertEqual(row['lifecycle'], 'closed_incomplete')
        with self.m.SessionLocal() as db:
            close_event = db.scalar(select(self.m.AfterSalesEvent).where(
                self.m.AfterSalesEvent.ticket_id == ticket['id'], self.m.AfterSalesEvent.event_type == 'force_close'))
            self.assertEqual(close_event.occurred_at, db.get(self.m.AfterSalesTicket, ticket['id']).closed_at)

    def test_provider_first_response_requires_response_event_not_legacy_timestamp(self):
        ticket = self.intervention()
        with self.m.SessionLocal() as db:
            db.get(self.m.AfterSalesTicket, ticket['id']).first_response_at = self.at
            db.commit()
        row = self.clock(ticket, 'first_response')
        self.assertEqual(row['lifecycle'], 'open')
        self.assertNotEqual(row['status'], 'met')

    def test_contradictory_historical_close_evidence_is_not_retimed_by_get(self):
        ticket = self.intervention()
        self.advance(60)
        ticket = self.act(ticket, 'force_close', 'ops')
        with self.m.SessionLocal() as db:
            row = db.get(self.m.AfterSalesTicket, ticket['id'])
            row.closed_at -= timedelta(microseconds=1)
            db.commit()
        state = self.clock_state()
        result = self.sla(ticket)
        self.assertEqual((result['status'], result['items']), ('insufficient_data', []))
        self.assertEqual(self.clock_state(), state)


if __name__ == '__main__':
    unittest.main()
