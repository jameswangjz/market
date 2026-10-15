"""Mock HTTPS and isolated SQLite checks; no real calendars or services touched."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
from io import StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select

import test_message_business_events as fixture


def document(year=2026, holiday='01-01'):
    return {'year': year, 'region': 'CN', 'dates': [
        {'date': f'{year}-{holiday}', 'type': 'public_holiday', 'name': 'Synthetic holiday'},
        {'date': f'{year}-01-03', 'type': 'transfer_workday', 'name': 'Synthetic workday'}]}


def response(data=None, *, code=200, raw=None, headers=None):
    raw = json.dumps(data if data is not None else document()).encode() if raw is None else raw
    return httpx.Response(code, headers=headers or {}, stream=httpx.ByteStream(raw))


class HolidayCalendarTests(unittest.TestCase):
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    setUp = fixture.BusinessRoutesHTTPTests.setUp
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown
    request = fixture.BusinessRoutesHTTPTests.request

    @classmethod
    def setUpClass(cls):
        fixture.BusinessRoutesHTTPTests.setUpClass.__func__(cls)
        cls.module = importlib.import_module(cls.m.__package__ + '.holiday_calendar')
        if not hasattr(cls.m, 'holiday_calendar'):
            cls.m.holiday_calendar = cls.module.install(vars(cls.m))
        cls.real_client = httpx.Client

    def transport(self, handler):
        return patch.object(self.module.httpx, 'Client',
            side_effect=lambda **kwargs: self.real_client(transport=httpx.MockTransport(handler), **kwargs))

    def sync(self, year=2026, actor='ops'):
        return self.request('POST', f'/api/sla/holiday-calendars/{year}/sync', actor=actor)

    def base(self, **extra):
        return {'version': 'synthetic-policy-v1', 'timezone': 'Asia/Shanghai',
                'weekdays': [0, 1, 2, 3, 4], 'windows': [['09:00', '17:00']],
                'exclude_public_holidays': True, **extra}

    def enriched(self, base=None):
        with self.m.SessionLocal() as db:
            return self.m.enrich_support_calendar(db, base or self.base())

    def test_fixed_source_and_bounded_client_options_with_admin_sync(self):
        seen = []
        def handler(request):
            seen.append(request)
            return response()
        with self.transport(handler) as constructor:
            result = self.sync(actor='platform')
        self.assertEqual(result['sync_status'], 'updated')
        self.assertEqual(result['cache_status'], 'available')
        self.assertEqual(str(seen[0].url), self.module.SOURCE.format(year=2026))
        self.assertEqual(seen[0].headers['accept-encoding'], 'identity')
        self.assertFalse(constructor.call_args.kwargs['follow_redirects'])
        self.assertFalse(constructor.call_args.kwargs['trust_env'])
        self.assertEqual(constructor.call_args.kwargs['timeout'].read, 5)
        version = result['current_version']
        self.assertEqual(len(version['content_hash']), 64)
        self.assertTrue(version['fetched_at'])
        status = self.request('GET', '/api/sla/holiday-calendars', actor='ops', params={'year': 2026})
        self.assertEqual(status['items'][0]['current_version']['id'], version['id'])

    def test_permissions_and_year_bounds_reject_before_network(self):
        with self.transport(lambda request: self.fail('No network should be attempted')):
            for actor in ('buyer', 'buyer_admin', 'provider', 'outsider', 'finance', 'reviewer', 'quality'):
                self.request('GET', '/api/sla/holiday-calendars', actor=actor, expected=403)
                self.request('POST', '/api/sla/holiday-calendars/2026/sync', actor=actor, expected=403)
            for year in (1999, 2101, -1, 'invalid'):
                self.request('POST', f'/api/sla/holiday-calendars/{year}/sync', actor='ops', expected=422)
                self.request('GET', '/api/sla/holiday-calendars', actor='ops', expected=422,
                    params={'year': year})
        for year in (True, 2026.0, 0):
            with self.assertRaises(ValueError):
                self.module.validate_year(year)

    def test_schema_validation_rejects_empty_conflicting_and_foreign_data(self):
        invalid = [[], {}, {'year': '2026', 'region': 'CN', 'dates': document()['dates']},
                   document(2027), {**document(), 'region': 'US'}, {**document(), 'dates': []}]
        for date_value, kind in (('2026-02-30', 'public_holiday'), ('2027-01-01', 'public_holiday'),
                                  ('20260101', 'public_holiday'), ('2026-01-01', 'unknown')):
            invalid.append({**document(), 'dates': [{'date': date_value, 'type': kind}]})
        duplicate = document()
        duplicate['dates'].append({**duplicate['dates'][0], 'type': 'transfer_workday'})
        invalid.append(duplicate)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises((ValueError, TypeError)):
                self.module.normalized_document(payload, 2026)

    def test_same_content_is_idempotent_and_history_keeps_reverted_versions(self):
        with self.transport(lambda request: response()):
            first = self.sync()
        reversed_entries = document()
        reversed_entries['dates'].reverse()
        with self.transport(lambda request: response(reversed_entries)):
            same = self.sync()
        self.assertEqual(same['sync_status'], 'unchanged')
        self.assertEqual(same['current_version'], first['current_version'])
        with self.transport(lambda request: response(document(holiday='01-02'))):
            changed = self.sync()
        self.assertNotEqual(changed['current_version']['id'], first['current_version']['id'])
        with self.transport(lambda request: response()):
            restored = self.sync()
        self.assertEqual(restored['current_version']['id'], first['current_version']['id'])
        self.assertEqual(len(restored['versions']), 2)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.holiday_calendar['Version']).count(), 2)

    def test_404_does_not_delete_available_cache_or_claim_unpublished_year_ready(self):
        with self.transport(lambda request: response()):
            first = self.sync()
        with self.transport(lambda request: response(code=404)):
            old = self.sync()
            missing = self.sync(2027)
        self.assertEqual((old['sync_status'], old['cache_status']), ('notpublished', 'available'))
        self.assertEqual(old['current_version'], first['current_version'])
        self.assertEqual((missing['sync_status'], missing['cache_status']), ('notpublished', 'not_ready'))
        self.assertIsNone(missing['current_version'])
        self.assertEqual(self.enriched()['covered_years'], [2026])

    def test_redirect_http_errors_invalid_json_and_bad_data_preserve_cache(self):
        with self.transport(lambda request: response()):
            first = self.sync()['current_version']
        for make_response in (lambda: response(code=302, headers={'location': 'http://127.0.0.1/private'}),
                              lambda: response(code=500), lambda: response(raw=b'not json'),
                              lambda: response(document(2027)), lambda: response({'year': 2026, 'region': 'CN', 'dates': []})):
            seen = []
            def handler(request):
                seen.append(request)
                return make_response()
            with self.transport(handler):
                result = self.sync()
            self.assertEqual(len(seen), 1)
            self.assertEqual(result['sync_status'], 'error')
            self.assertEqual(result['cache_status'], 'available')
            self.assertEqual(result['current_version'], first)

    def test_download_size_encoding_and_timeout_are_bounded(self):
        cases = (
            lambda request: response(headers={'content-length': str(self.module.MAX_BYTES + 1)}),
            lambda request: response(raw=b'x' * (self.module.MAX_BYTES + 1)),
            lambda request: response(headers={'content-encoding': 'gzip'}),
        )
        for handler in cases:
            with self.transport(handler):
                result = self.sync()
            self.assertEqual((result['sync_status'], result['cache_status']), ('error', 'not_ready'))
        def timeout(request):
            raise httpx.ReadTimeout('synthetic timeout', request=request)
        with self.transport(timeout):
            result = self.sync()
        self.assertEqual(result['reason'], 'timeout')
        with self.transport(lambda request: response()), patch.object(self.module.time, 'monotonic',
                side_effect=[0, self.module.MAX_SECONDS + 1]):
            # Exercise the fetch helper alone so ORM/client timing cannot consume the test clock.
            with self.assertRaises(self.module.FetchFailure) as error:
                self.module.fetch_document(2026)
        self.assertEqual(error.exception.reason, 'total_timeout')

    def test_enrichment_is_detached_only_cached_years_and_strategic_needs_no_db(self):
        base = self.base(covered_years=[2030], holidays=['2030-01-01'])
        before = deepcopy(base)
        with self.transport(lambda request: response()):
            self.sync()
        with self.transport(lambda request: response(document(2027))):
            self.sync(2027)
        calendar = self.enriched(base)
        self.assertEqual(calendar['covered_years'], [2026, 2027])
        self.assertEqual(calendar['holidays'], ['2026-01-01', '2027-01-01'])
        self.assertEqual(calendar['workdays'], ['2026-01-03', '2027-01-03'])
        self.assertEqual(len(calendar['holiday_calendar_sources']), 2)
        self.assertNotEqual(calendar['version'], base['version'])
        self.assertEqual(base, before)
        strategic = self.m.enrich_support_calendar(None, self.base(exclude_public_holidays=False))
        self.assertEqual(strategic['holiday_calendar_status'], 'not_required')
        self.assertEqual(strategic['covered_years'], [])

    def test_slow_small_chunks_check_deadline_without_waiting_for_aggregation(self):
        consumed = []
        closed = []

        class SlowStream(httpx.SyncByteStream):
            def __iter__(self):
                consumed.append(1)
                yield b'{'
                consumed.append(2)
                yield b'"year":'
                raise AssertionError('Fetch read beyond the total time budget')

            def close(self):
                closed.append(True)

        with self.transport(lambda request: httpx.Response(200, stream=SlowStream())), patch.object(
                self.module.time, 'monotonic', side_effect=[0, 4, 16]):
            with self.assertRaises(self.module.FetchFailure) as error:
                self.module.fetch_document(2026)
        self.assertEqual(error.exception.reason, 'total_timeout')
        self.assertEqual(consumed, [1, 2])
        self.assertTrue(closed)

    def test_corrupted_cache_hash_is_never_claimed_as_covered(self):
        with self.transport(lambda request: response()):
            self.sync()
        with self.m.SessionLocal() as db:
            version = db.scalar(select(self.m.holiday_calendar['Version']))
            version.document_json = json.dumps(document(holiday='01-02'))
            db.commit()
        self.assertEqual(self.enriched()['covered_years'], [])
        status = self.request('GET', '/api/sla/holiday-calendars', actor='ops', params={'year': 2026})
        self.assertEqual(status['items'][0]['cache_status'], 'not_ready')

    def test_new_snapshot_has_enriched_hashes_and_old_snapshot_does_not_change(self):
        with self.m.SessionLocal() as db:
            old = self.m.freeze_sla_snapshot(db, 'buyer_tenant')
        old_json = json.dumps(old, sort_keys=True)
        with self.transport(lambda request: response()):
            self.sync()
        with self.m.SessionLocal() as db:
            new = self.m.freeze_sla_snapshot(db, 'buyer_tenant')
        self.assertEqual(new['calendar']['covered_years'], [2026])
        self.assertEqual(old['calendar']['covered_years'], [])
        self.assertEqual(old_json, json.dumps(old, sort_keys=True))
        self.assertEqual(old['contract_rule_version'], new['contract_rule_version'])
        self.assertNotEqual(old['rule_version'], new['rule_version'])
        expected = hashlib.sha256(json.dumps(new['calendar'], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(new['calendar_content_hash'], expected)

    def test_worker_commits_current_year_and_records_next_year_notpublished(self):
        def handler(request):
            return response() if request.url.path.endswith('/2026.json') else response(code=404)
        with self.transport(handler), patch('sys.stdout', new_callable=StringIO) as output:
            code = self.module.run_years(self.m, (2026, 2027))
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row['sync_status'] for row in rows], ['updated', 'notpublished'])
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.holiday_calendar['State']).count(), 2)

    def test_worker_reports_error_but_still_attempts_next_year(self):
        def handler(request):
            return response(code=500) if request.url.path.endswith('/2026.json') else response(document(2027))
        with self.transport(handler), patch('sys.stdout', new_callable=StringIO):
            code = self.module.run_years(self.m, (2026, 2027))
        self.assertEqual(code, 1)
        self.assertEqual(self.enriched()['covered_years'], [2027])

    def test_error_alerts_with_cache_are_daily_idempotent_and_admin_operator_only(self):
        with self.transport(lambda request: response()):
            self.sync()
        stamp = datetime(2026, 10, 14, 16, 30, tzinfo=timezone.utc)
        with patch.object(self.m, 'now', return_value=stamp), self.transport(lambda request: response(code=500)):
            self.sync()
            self.sync()
        with self.m.SessionLocal() as db:
            messages = db.scalars(select(self.Message).where(
                self.Message.event_key.like('holiday-calendar:%'))).all()
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].event_key, 'holiday-calendar:2026:error:2026-10-15')
            recipients = db.scalars(select(self.Receipt.user_id).where(
                self.Receipt.message_id == messages[0].id)).all()
            self.assertEqual(set(recipients), {'platform', 'ops'})
        with patch.object(self.m, 'now', return_value=stamp + timedelta(days=1)), self.transport(
                lambda request: response(code=500)):
            self.sync()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.Message).filter(self.Message.event_key.like('holiday-calendar:%')).count(), 2)

    def test_current_year_unpublished_alerts_but_next_year_does_not(self):
        stamp = datetime(2026, 10, 14, tzinfo=timezone.utc)
        with patch.object(self.m, 'now', return_value=stamp), self.transport(lambda request: response(code=404)):
            self.sync(2027)
            self.sync(2026)
            self.sync(2026)
        with self.m.SessionLocal() as db:
            messages = db.scalars(select(self.Message).where(
                self.Message.event_key.like('holiday-calendar:%'))).all()
            self.assertEqual([message.event_key for message in messages],
                ['holiday-calendar:2026:notpublished:2026-10-14'])

    def test_current_year_404_with_usable_cache_is_not_an_alert(self):
        with self.transport(lambda request: response()):
            self.sync()
        with self.transport(lambda request: response(code=404)):
            self.sync()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.Message).filter(self.Message.event_key.like('holiday-calendar:%')).count(), 0)


if __name__ == '__main__':
    unittest.main()
