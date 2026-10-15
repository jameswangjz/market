"""Synthetic annual bindings on isolated SQLite; no network or production data."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import test_message_business_events as fixture


def at(value):
    return datetime.fromisoformat(value + '+08:00')


class TicketCalendarTests(unittest.TestCase):
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown

    @classmethod
    def setUpClass(cls):
        fixture.BusinessRoutesHTTPTests.setUpClass.__func__(cls)
        cls.holiday = importlib.import_module(cls.m.__package__+'.holiday_calendar')
        if not hasattr(cls.m, 'holiday_calendar'):
            cls.m.holiday_calendar = cls.holiday.install(vars(cls.m))
        cls.module = importlib.import_module(cls.m.__package__+'.ticket_calendar')
        cls.hooks = cls.module.install(vars(cls.m))

    def setUp(self):
        fixture.BusinessRoutesHTTPTests.setUp(self)
        self.Binding = self.hooks['Binding']
        self.Version, self.State = (self.m.holiday_calendar[key] for key in ('Version', 'State'))
        self.calendar = {'version': 'original-calendar', 'timezone': 'Asia/Shanghai',
            'weekdays': [0, 1, 2, 3, 4], 'windows': [['09:00', '17:00']],
            'exclude_public_holidays': True, 'covered_years': [2026],
            'holidays': ['2026-10-01'], 'workdays': ['2026-10-10']}
        self.ticket(self.calendar)

    def ticket(self, calendar, created='2026-12-31T09:00', closed=None):
        raw = json.dumps({'calendar': deepcopy(calendar)}, indent=3) + '\n'
        with self.m.SessionLocal() as db:
            if db.get(self.m.Order, 'owned-order') is None:
                db.add(self.m.Order(id='owned-order', order_no='OWNED-CALENDAR', product_id='product',
                    buyer_enterprise_id='buyer_tenant', provider_enterprise_id='provider_tenant',
                    buyer_user_id='buyer', buyer_name='Fixture', product_name='Fixture'))
                db.flush()
            row = db.get(self.m.AfterSalesTicket, 'owned-ticket')
            if row is None:
                row = self.m.AfterSalesTicket(id='owned-ticket', order_id='owned-order',
                    ticket_no='OWNED-TICKET', type='quality', description='Synthetic')
                db.add(row)
            row.sla_snapshot_json = raw
            row.created_at = at(created).astimezone(timezone.utc)
            row.closed_at = at(closed).astimezone(timezone.utc) if closed else None
            db.commit()
        self.original = raw

    def cache(self, year, day='01-01', **overrides):
        document = self.holiday.normalized_document({'year': year, 'region': 'CN', 'dates': [
            {'date': f'{year}-{day}', 'type': 'public_holiday', 'name': 'Synthetic'},
            {'date': f'{year}-01-03', 'type': 'transfer_workday', 'name': 'Synthetic'}]}, year)
        raw = self.holiday.canonical(document)
        values = dict(year=year, region='CN', source_url=self.holiday.SOURCE.format(year=year),
                      content_hash=hashlib.sha256(raw.encode()).hexdigest(), document_json=raw,
                      fetched_at=datetime.now(timezone.utc))
        values.update(overrides)
        with self.m.SessionLocal() as db:
            version = self.Version(**values)
            db.add(version)
            db.flush()
            state = db.get(self.State, year)
            if state is None:
                state = self.State(year=year, checked_at=self.m.now(), sync_status='updated')
                db.add(state)
            state.current_version_id = version.id
            db.commit()
            return version.id

    def resolve(self, when='2027-01-04T10:00', commit=True):
        with self.m.SessionLocal() as db:
            result = self.m.resolve_ticket_calendar(db, db.get(self.m.AfterSalesTicket, 'owned-ticket'), at(when))
            if commit:
                db.commit()
            else:
                db.rollback()
            return result

    def counts(self):
        with self.m.SessionLocal() as db:
            return (len(db.scalars(select(self.Binding)).all()),
                    len(db.scalars(select(self.m.AuditLog).where(
                        self.m.AuditLog.action == 'bind_ticket_calendar_year')).all()))

    def assert_snapshot_unchanged(self):
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.AfterSalesTicket, 'owned-ticket').sla_snapshot_json, self.original)

    def test_cross_year_appends_and_audits_without_replacing_original_year(self):
        self.cache(2026, '02-01')
        version = self.cache(2027)
        result = self.resolve()
        self.assertEqual(result['covered_years'], [2026, 2027])
        self.assertEqual(result['missing_years'], [])
        self.assertEqual(result['holidays'], ['2026-10-01', '2027-01-01'])
        self.assertEqual(result['workdays'], ['2026-10-10', '2027-01-03'])
        self.assertEqual(self.counts(), (1, 1))
        with self.m.SessionLocal() as db:
            binding = db.scalar(select(self.Binding))
            self.assertEqual((binding.year, binding.calendar_version_id), (2027, version))
            audit = db.scalar(select(self.m.AuditLog).where(self.m.AuditLog.action == 'bind_ticket_calendar_year'))
            after = json.loads(audit.after_json)
            self.assertEqual((audit.actor, audit.order_id, after['year']), ('system', 'owned-order', 2027))
            self.assertEqual(after['calendar_version_id'], version)
            self.assertEqual(after['content_hash'], db.get(self.Version, version).content_hash)
            self.assertEqual(after['order_id'], 'owned-order')
        self.assert_snapshot_unchanged()

    def test_cache_update_cannot_replace_bound_year_and_repeated_read_is_idempotent(self):
        first = self.cache(2027)
        before = self.resolve()
        self.cache(2027, '02-01')
        after = self.resolve()
        self.assertEqual(after, before)
        self.assertEqual(after['ticket_calendar_bindings'][0]['calendar_version_id'], first)
        self.assertEqual(self.counts(), (1, 1))
        self.assert_snapshot_unchanged()

    def test_missing_year_can_be_bound_when_it_later_arrives(self):
        missing = self.resolve()
        self.assertEqual(missing['missing_years'], [2027])
        self.assertEqual(missing['covered_years'], [2026])
        self.assertEqual(self.counts(), (0, 0))
        self.cache(2027)
        self.assertEqual(self.resolve()['missing_years'], [])
        self.assertEqual(self.counts(), (1, 1))

    def test_later_year_appends_without_rebinding_an_earlier_added_year(self):
        first = self.cache(2027)
        self.resolve()
        self.cache(2027, '02-01')
        later = self.cache(2028)
        result = self.resolve('2028-01-04T10:00')
        self.assertEqual(result['covered_years'], [2026, 2027, 2028])
        self.assertEqual(result['missing_years'], [])
        self.assertEqual([source['calendar_version_id'] for source in result['ticket_calendar_bindings']],
                         [first, later])
        self.assertIn('2027-01-01', result['holidays'])
        self.assertNotIn('2027-02-01', result['holidays'])
        self.assertEqual(self.counts(), (2, 2))
        self.assert_snapshot_unchanged()

    def test_document_hash_is_checked_after_normalization(self):
        version = self.cache(2027)
        with self.m.SessionLocal() as db:
            row = db.get(self.Version, version)
            document = json.loads(row.document_json)
            document['dates'].reverse()
            row.document_json = json.dumps(document, indent=4)
            db.commit()
        self.assertEqual(self.resolve()['missing_years'], [])
        self.assertEqual(self.counts(), (1, 1))

    def test_rollback_removes_both_binding_and_audit_and_hook_never_commits(self):
        self.cache(2027)
        with self.m.SessionLocal() as db, patch.object(db, 'commit', side_effect=AssertionError('Hook committed')):
            self.m.resolve_ticket_calendar(db, db.get(self.m.AfterSalesTicket, 'owned-ticket'), at('2027-01-04T10:00'))
            self.assertEqual(len(db.scalars(select(self.Binding)).all()), 1)
            db.rollback()
        self.assertEqual(self.counts(), (0, 0))
        self.assert_snapshot_unchanged()

    def test_audit_failure_can_roll_back_new_binding(self):
        self.cache(2027)
        with self.m.SessionLocal() as db:
            with patch.object(self.m, 'audit', side_effect=RuntimeError('Synthetic audit error')):
                with self.assertRaises(RuntimeError):
                    self.m.resolve_ticket_calendar(db, db.get(self.m.AfterSalesTicket, 'owned-ticket'), at('2027-01-04T10:00'))
            db.rollback()
        self.assertEqual(self.counts(), (0, 0))

    def test_half_open_beijing_year_end_and_closed_ticket_do_not_need_next_year(self):
        self.cache(2027)
        for closed, when in ((None, '2027-01-01T00:00'),
                             ('2027-01-01T00:00', '2028-01-04T10:00'),
                             ('2026-12-31T17:00', '2027-01-04T10:00')):
            with self.subTest(closed=closed):
                self.ticket(self.calendar, closed=closed)
                result = self.resolve(when)
                self.assertEqual(result['covered_years'], [2026])
                self.assertEqual(result['missing_years'], [])
                self.assertEqual(self.counts(), (0, 0))

    def test_one_microsecond_after_new_year_requires_the_new_year(self):
        result = self.resolve('2027-01-01T00:00:00.000001')
        self.assertEqual(result['missing_years'], [2027])

    def test_beijing_new_year_is_previous_utc_day_1600(self):
        boundary = at('2027-01-01T00:00').astimezone(timezone.utc)
        self.assertEqual(boundary, datetime(2026, 12, 31, 16, tzinfo=timezone.utc))
        start = at('2026-12-31T09:00').astimezone(timezone.utc)
        self.assertEqual(self.module._required_years(start, boundary), [2026])
        after = at('2027-01-01T00:00:00.000001').astimezone(timezone.utc)
        self.assertEqual(self.module._required_years(start, after), [2026, 2027])

    def test_zero_or_reversed_interval_does_not_bind(self):
        for when in ('2026-12-31T09:00', '2026-12-31T08:00'):
            self.assertEqual(self.resolve(when)['missing_years'], [])
        self.assertEqual(self.counts(), (0, 0))

    def test_strategic_returns_detached_original_without_binding(self):
        calendar = {**self.calendar, 'exclude_public_holidays': False}
        self.ticket(calendar)
        self.cache(2027)
        result = self.resolve()
        self.assertEqual(result, calendar)
        result['holidays'].append('2027-02-01')
        self.assertEqual(self.counts(), (0, 0))
        self.assert_snapshot_unchanged()

    def test_unknown_year_is_missing_not_guessed(self):
        self.ticket({**self.calendar, 'covered_years': []}, created='2101-01-02T09:00')
        self.assertEqual(self.resolve('2101-01-03T10:00')['missing_years'], [2101])
        self.assertEqual(self.counts(), (0, 0))

    def test_hash_source_region_document_and_state_year_are_validated(self):
        invalid = [{'content_hash': '0'*64}, {'source_url': 'https://other.example.invalid/2027.json'},
                   {'region': 'US'}, {'document_json': '{}'},
                   {'document_json': '{"year":2027,"region":"CN","dates":[]}'}]
        for day, overrides in zip(('01-01', '01-02', '01-04', '01-05', '01-06'), invalid):
            with self.subTest(overrides=overrides):
                self.cache(2027, day, **overrides)
                result = self.resolve()
                self.assertEqual(result['missing_years'], [2027])
                self.assertEqual(self.counts(), (0, 0))

    def test_state_pointing_to_a_different_year_is_not_trusted(self):
        version = self.cache(2026)
        with self.m.SessionLocal() as db:
            db.add(self.State(year=2027, current_version_id=version, checked_at=self.m.now(), sync_status='updated'))
            db.commit()
        self.assertEqual(self.resolve()['missing_years'], [2027])
        self.assertEqual(self.counts(), (0, 0))

    def test_corrupt_bound_version_does_not_fall_back_to_new_cache(self):
        first = self.cache(2027)
        self.resolve()
        self.cache(2027, '02-01')
        with self.m.SessionLocal() as db:
            db.get(self.Version, first).content_hash = '0'*64
            db.commit()
        result = self.resolve()
        self.assertEqual(result['missing_years'], [2027])
        self.assertEqual(result['covered_years'], [2026])
        self.assertEqual(self.counts(), (1, 1))

    def test_ticket_refresh_observes_latest_closed_at_without_locking_order(self):
        self.cache(2027)
        with self.m.SessionLocal() as reader:
            ticket = reader.get(self.m.AfterSalesTicket, 'owned-ticket')
            with self.m.SessionLocal() as writer:
                writer.get(self.m.AfterSalesTicket, 'owned-ticket').closed_at = at('2027-01-01T00:00').astimezone(timezone.utc)
                writer.commit()
            self.assertIsNone(ticket.closed_at)
            seen = []
            real = reader.scalar
            def capture(statement, *args, **kwargs):
                seen.append(statement)
                return real(statement, *args, **kwargs)
            with patch.object(reader, 'scalar', side_effect=capture):
                result = self.m.resolve_ticket_calendar(reader, ticket, at('2027-01-04T10:00'))
            self.assertEqual(result['missing_years'], [])
            self.assertIsNotNone(ticket.closed_at)
            self.assertIsNotNone(seen[0]._for_update_arg)
            self.assertEqual([table.name for table in seen[0].get_final_froms()], ['after_sales_tickets'])

    def test_binding_unique_ticket_year_foreign_key_and_install_idempotency(self):
        version = self.cache(2027)
        self.resolve()
        self.assertIs(self.module.install(vars(self.m)), self.hooks)
        with self.m.SessionLocal() as db:
            db.add(self.Binding(ticket_id='owned-ticket', year=2027, calendar_version_id=version))
            with self.assertRaises(IntegrityError):
                db.flush()
            db.rollback()
            db.add(self.Binding(ticket_id='owned-ticket', year=2028, calendar_version_id='missing-version'))
            with self.assertRaises(IntegrityError):
                db.flush()
            db.rollback()


if __name__ == '__main__':
    unittest.main()
