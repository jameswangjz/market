"""Isolated HTTP tests for immutable safe evidence, not production collection."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

import test_after_sales_workflow as tickets
import test_sla_online_assessments as online
import test_sla_online_sources as source_fixture


class BoundedEvidenceTests(unittest.TestCase):
    def test_request_timestamp_is_optional_trailing_and_fact_metadata_is_safe(self):
        from app.sla_online_facts import RequestFact
        from app.sla_evidence import safe_fact
        self.assertIsNone(RequestFact('legacy-id', 'gateway', 200, 1, True, True).occurred_at)
        with self.assertRaises(ValueError):
            RequestFact('id', 'gateway', 200, 1, True, True,
                        occurred_at=datetime(2026, 10, 15))
        self.assertEqual(safe_fact({'responsibility': 'provider', 'policy_version': 'sla-online-v1.1'}),
                         {'responsibility': 'provider', 'policy_version': 'sla-online-v1.1'})
        self.assertEqual(safe_fact({'responsibility': 'PRIVATE', 'policy_version': 'JWT.PRIVATE.TOKEN'}), {})

    def test_metric_units_have_a_dedicated_whitelist(self):
        from app.sla_evidence import safe_metric
        for unit in ('%', 'ms', 'seconds', 'minutes'):
            self.assertEqual(safe_metric({'unit': unit})['unit'], unit)
        self.assertNotIn('unit', safe_metric({'unit': 'PRIVATE RAW PAYLOAD'}))

    def test_late_anomaly_is_retained_ahead_of_two_hundred_successes(self):
        from app.sla_evidence import bundle
        facts = [{'kind': 'probe', 'fact_id': f'normal-{i:04}', 'status': 'healthy', 'included': True}
                 for i in range(200)]
        facts.append({'kind': 'probe', 'fact_id': 'late-failure', 'status': 'failed', 'included': True})
        result = bundle(facts)
        self.assertEqual((result['total'], result['retained_count'], result['truncated']), (201, 200, True))
        self.assertEqual(result['items'][0]['fact_id'], 'late-failure')

    def test_same_priority_uses_time_then_id_independent_of_input_order(self):
        from app.sla_evidence import bundle
        facts = [{'kind': 'probe', 'fact_id': f'fact-{i:04}', 'status': 'unknown',
                  'occurred_at': '2026-10-15T00:00:00+00:00', 'included': False} for i in range(205)]
        expected = bundle(facts)
        self.assertEqual(bundle(reversed(facts)), expected)
        self.assertEqual(expected['items'][0]['fact_id'], 'fact-0000')
        self.assertEqual(expected['items'][-1]['fact_id'], 'fact-0199')


class TicketEvidenceTests(unittest.TestCase):
    setUpClass = classmethod(tickets.AfterSalesWorkflowTests.setUpClass.__func__)
    tearDownClass = classmethod(tickets.AfterSalesWorkflowTests.tearDownClass.__func__)
    tearDown = tickets.AfterSalesWorkflowTests.tearDown
    request = tickets.AfterSalesWorkflowTests.request
    create_order = tickets.AfterSalesWorkflowTests.create_order
    create = tickets.AfterSalesWorkflowTests.create
    act = tickets.AfterSalesWorkflowTests.act

    def setUp(self):
        tickets.AfterSalesWorkflowTests.setUp(self)
        self.ticket = self.create()
        self.path = f"/api/after-sales/{self.ticket['id']}/sla/evidence"

    def clocks(self):
        with self.m.SessionLocal() as db:
            event_id = db.scalar(select(self.m.AfterSalesEvent.id))
            for metric, party in [('first_response', 'provider'), ('resolution', 'provider'), ('intervention_review', 'platform')]:
                db.add(self.m.TicketSlaClock(ticket_id=self.ticket['id'], metric=metric, round=1, party=party,
                    rule_snapshot_json='{"target_seconds":240,"warning_seconds":192,"clock":"natural","secret":"PRIVATE"}',
                    rule_version='safe-version', calendar_snapshot_json='{"version":"calendar-v1","secret":"PRIVATE"}',
                    calendar_bindings_json='[]', source_event_ids_json=json.dumps([event_id]),
                    source_ticket_version=1, elapsed_seconds=10, status='running', lifecycle='open',
                    measured_at=self.m.now(), started_at=self.m.now()))
            db.commit()

    def test_read_only_persisted_clocks_and_safe_rule_projection(self):
        self.clocks()
        with patch.dict(self.m.ticket_sla_clocks, refresh=lambda *args: self.fail('GET refreshed clocks')), \
                patch.object(self.m, 'resolve_ticket_calendar', side_effect=AssertionError('GET bound calendar')), \
                patch.object(self.m, 'notify_business_event', side_effect=AssertionError('GET sent notification')), \
                patch.object(Session, 'commit', side_effect=AssertionError('GET committed')):
            row = self.request('GET', self.path)
        self.assertEqual(row['total'], 3)
        self.assertFalse(row['materialized'])
        self.assertFalse(row['stale'])
        self.assertEqual(row['items'][0]['target_seconds'], 240)
        self.assertNotIn('PRIVATE', json.dumps(row))
        self.assertTrue(row['order_no'].startswith('ORD'))

    def test_missing_clocks_remain_unknown_and_do_not_invent_history(self):
        row = self.request('GET', self.path)
        self.assertEqual(row['items'], [])
        self.assertEqual(row['reason'], 'no_persisted_clocks')
        with self.m.SessionLocal() as db:
            db.get(self.m.AfterSalesTicket, self.ticket['id']).evidence_status = 'historical_unknown'
            db.commit()
        self.assertEqual(self.request('GET', self.path)['buyer_cooperation']['status'], 'unknown')

    def test_permission_scope_and_party_pagination(self):
        self.clocks()
        for actor in ('outsider', 'finance'):
            self.request('GET', self.path, actor=actor, expected=404)
        self.request('GET', self.path + '?enterprise_id=other_tenant', expected=403)
        for size in (10, 12, 20, 50, 100):
            row = self.request('GET', self.path + f'?page_size={size}&responsibility=provider')
            self.assertEqual((row['total'], len(row['items'])), (2, 2))
        self.assertEqual(self.request('GET', self.path + '?page=99')['items'], [])
        for suffix in ('?page=0', '?page_size=101', '?responsibility=finance'):
            self.request('GET', self.path + suffix, expected=422)

    def test_timeline_pagination_hook_preserves_scope_count_and_stable_order(self):
        ticket = self.act(self.ticket, 'respond', 'provider')
        self.act(ticket, 'request_info', 'provider')
        from starlette.requests import Request
        request = Request({'type': 'http', 'headers': [], 'query_string': b''})
        path = f"/api/after-sales/{self.ticket['id']}/timeline"
        first = self.request('GET', path + '?page=1&page_size=2')
        second = self.request('GET', path + '?page=2&page_size=2')
        self.request('GET', path + '?page=0', expected=422)
        self.request('GET', path + '?page_size=101', expected=422)
        self.assertEqual(first['total'], 3)
        self.assertEqual([e['action'] for e in first['items'] + second['items']], ['create', 'respond', 'request_info'])

    def test_buyer_wait_intervals_are_not_restarted_by_platform_intervention(self):
        ticket = self.act(self.ticket, 'respond', 'provider')
        ticket = self.act(ticket, 'request_info', 'provider')
        ticket = self.act(ticket, 'request_intervention', 'buyer')
        ticket = self.act(ticket, 'review_intervention', 'ops')
        self.act(ticket, 'supplement', 'buyer')
        row = self.request('GET', self.path)
        intervals = row['buyer_cooperation']['items']
        self.assertEqual(len(intervals), 1)
        with self.m.SessionLocal() as db:
            event = db.get(self.m.AfterSalesEvent, intervals[0]['end_event_id'])
            self.assertEqual(event.event_type, 'supplement')


class AssessmentEvidenceTests(unittest.TestCase):
    setUpClass = classmethod(online.OnlineAssessmentTests.setUpClass.__func__)
    tearDownClass = classmethod(online.OnlineAssessmentTests.tearDownClass.__func__)
    tearDown = online.OnlineAssessmentTests.tearDown
    request = online.OnlineAssessmentTests.request
    create_order = online.OnlineAssessmentTests.create_order
    pay = online.OnlineAssessmentTests.pay
    paid = online.OnlineAssessmentTests.paid
    task_id = online.OnlineAssessmentTests.task_id
    process = online.OnlineAssessmentTests.process
    evaluate = online.OnlineAssessmentTests.evaluate
    sources = online.OnlineAssessmentTests.sources
    api_contracts = online.OnlineAssessmentTests.api_contracts

    def setUp(self):
        online.OnlineAssessmentTests.setUp(self)
        self.Version = self.m.sla_evidence['Version']
        self.order_id = self.paid()
        self.api_contracts()
        self.module = importlib.import_module(self.m.__package__ + '.sla_evidence')

    def source(self, count=1):
        source = self.sources()
        source['evidence'] = {'availability': self.module.bundle([
            {'kind': 'probe', 'fact_id': f'fact-{index}', 'occurred_at': self.at, 'status': 'healthy',
             'included': True, 'authorization': 'JWT PRIVATE', 'payload': {'person': 'PRIVATE'},
             'config_summary_json': 'PRIVATE'} for index in range(count)], start=self.at - timedelta(days=1), end=self.at)}
        return source

    def get_sources(self, row, suffix='', actor='ops', expected=200):
        return self.request('GET', f"/api/sla/results/{row['id']}/sources?metric=availability" + suffix,
                            actor=actor, expected=expected)

    def test_version_metrics_facts_and_cutoff_are_frozen_not_rejoined(self):
        source = self.source()
        source['request_source_cutoff'] = self.at.isoformat()
        with patch.dict(self.m.sla_online_sources, measure_order=lambda *a: source):
            first = self.evaluate()['items'][0]
            first_evidence = self.get_sources(first)
            self.assertEqual(first_evidence['metric_result']['value'], 97)
            self.assertEqual(self.evaluate()['items'][0]['version'], first['version'])
            source['measurements']['availability'] = self.metric(100, 'met', 100, coverage_percent=100)
            source['evidence']['availability']['items'][0]['status'] = 'failed'
            self.at += timedelta(minutes=5)
            source['request_source_cutoff'] = self.at.isoformat()
            second = self.evaluate()['items'][0]
        old = self.get_sources(second, f"&version={first['version']}")
        self.assertEqual(old, first_evidence)
        self.assertEqual(self.get_sources(second)['metric_result']['value'], 100)
        self.assertEqual(self.get_sources(second)['items'][0]['status'], 'failed')
        self.assertNotIn('PRIVATE', json.dumps(old))
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.Version).count(), 2)

    def test_historical_missing_association_is_unknown_never_backfilled(self):
        source = self.source()
        with patch.dict(self.m.sla_online_sources, measure_order=lambda *a: source):
            row = self.evaluate()['items'][0]
            with self.m.SessionLocal() as db:
                current = db.get(self.Assessment, row['id'])
                payload = [current.snapshot_json, json.loads(current.metrics_json), json.loads(current.source_json)]
                current.input_hash = hashlib.sha256(json.dumps(payload, sort_keys=True,
                    separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
                db.query(self.Version).delete()
                db.commit()
            self.assertEqual(self.evaluate()['items'][0]['version'], row['version'])
        result = self.get_sources(row)
        self.assertEqual(result['evidence_status'], 'unknown')
        self.assertEqual(result['items'], [])
        self.assertIsNone(result['metric_result'])
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.Version).count(), 0)

    def test_truncation_safe_fields_and_source_page_boundaries(self):
        source = self.source(205)
        with patch.dict(self.m.sla_online_sources, measure_order=lambda *a: source):
            row = self.evaluate()['items'][0]
        for size in (10, 12, 20, 50, 100):
            result = self.get_sources(row, f'&page_size={size}')
            self.assertEqual((result['total'], result['retained_count'], result['source_total']), (200, 200, 205))
            self.assertTrue(result['truncated'])
            self.assertEqual(len(result['items']), size)
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertEqual(result['metric_result']['unit'], '%')
        self.assertEqual(len(self.get_sources(row, '&page_size=100&page=2')['items']), 100)
        self.assertEqual(self.get_sources(row, '&page_size=100&page=3')['items'], [])
        self.assertEqual(self.get_sources(row, '&page=99')['items'], [])
        for suffix in ('&page=0', '&page_size=101', '&version=0'):
            self.get_sources(row, suffix, expected=422)

    def test_online_sources_permission_remains_admin_operator_only(self):
        row = self.evaluate()['items'][0]
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'reviewer').platform_role = 'delivery_monitor'
            db.commit()
        for actor in ('buyer', 'provider', 'reviewer', 'finance'):
            self.get_sources(row, actor=actor, expected=403)
        self.get_sources(row, actor='platform')
        self.get_sources({'id': 'missing'}, expected=404)

    def test_new_version_association_rolls_back_with_audit_or_outbox(self):
        for failure in ('audit', 'notify_business_event'):
            with patch.dict(self.m.sla_online_sources, measure_order=lambda *a: self.source()), \
                    patch.object(self.m, failure, side_effect=RuntimeError('isolated failure')):
                self.evaluate(500)
            with self.m.SessionLocal() as db:
                self.assertEqual(db.query(self.Version).count(), 0)
                self.assertEqual(db.query(self.Assessment).count(), 0)
                self.assertEqual(db.query(self.m.AuditLog).filter_by(action='evaluate_order_sla').count(), 0)

    def test_sources_get_is_read_only_and_does_not_call_measure_order(self):
        row = self.evaluate()['items'][0]
        with patch.dict(self.m.sla_online_sources, measure_order=lambda *a: self.fail('GET recollected facts')), \
                patch.object(Session, 'commit', side_effect=AssertionError('GET committed')), \
                patch.object(self.m, 'notify_business_event', side_effect=AssertionError('GET notified')):
            self.assertEqual(self.get_sources(row)['version'], row['version'])

    def test_results_optional_pagination_preserves_unpaged_contract(self):
        self.evaluate()
        for size in (10, 12, 20, 50, 100):
            result = self.request('GET', f'/api/sla/results?page_size={size}', actor='ops')
            self.assertEqual(result['total'], 1)
            self.assertEqual(result['page_size'], size)
        self.assertNotIn('page', self.request('GET', '/api/sla/results', actor='ops'))
        self.assertEqual(self.request('GET', '/api/sla/results?page=99', actor='ops')['items'], [])
        for suffix in ('?page=0', '?page_size=101'):
            self.request('GET', '/api/sla/results' + suffix, actor='ops', expected=422)

    def test_delivery_source_uses_assessment_as_of_not_future_verification(self):
        with self.m.SessionLocal() as db:
            contract = self.m.order_sla_contracts['get_contract'](db, self.order_id)
            paid = contract.paid_at.replace(tzinfo=timezone.utc)
            contract.verified_at = paid + timedelta(seconds=300)
            db.commit()
        self.at = paid + timedelta(seconds=100)
        first = self.evaluate()['items'][0]
        path = f"/api/sla/results/{first['id']}/sources?metric=delivery_duration"
        pending = self.request('GET', path, actor='ops')['items'][0]
        self.assertEqual((pending['status'], pending['reason']), ('pending', 'delivery_pending'))
        self.assertNotIn('verified_at', pending)
        self.at = paid + timedelta(seconds=241)
        self.evaluate()
        overdue = self.request('GET', path, actor='ops')['items'][0]
        self.assertEqual((overdue['status'], overdue['reason']), ('overdue', 'delivery_overdue'))
        self.assertNotIn('verified_at', overdue)
        self.at = paid + timedelta(seconds=301)
        self.evaluate()
        verified = self.request('GET', path, actor='ops')['items'][0]
        self.assertEqual((verified['status'], verified['reason']), ('verified', 'first_verified_delivery'))
        self.assertEqual(verified['verified_at'], (paid + timedelta(seconds=300)).isoformat())


class PersistedSourceEvidenceTests(unittest.TestCase):
    setUpClass = classmethod(source_fixture.OnlineSourceTests.setUpClass.__func__)
    tearDownClass = classmethod(source_fixture.OnlineSourceTests.tearDownClass.__func__)
    tearDown = source_fixture.OnlineSourceTests.tearDown
    request = source_fixture.OnlineSourceTests.request
    pay = source_fixture.OnlineSourceTests.pay
    task_id = source_fixture.OnlineSourceTests.task_id
    create = source_fixture.OnlineSourceTests.create
    step = source_fixture.OnlineSourceTests.step
    admin_request = source_fixture.OnlineSourceTests.admin_request
    redis_eval = source_fixture.OnlineSourceTests.redis_eval
    probes = source_fixture.OnlineSourceTests.probes
    requests = source_fixture.OnlineSourceTests.requests
    evaluate = source_fixture.OnlineSourceTests.evaluate
    setUp = source_fixture.OnlineSourceTests.setUp

    def source_page(self, row, metric):
        return self.request('GET', f"/api/sla/results/{row['id']}/sources?metric={metric}&page_size=100", actor='ops')

    def test_actual_probe_facts_are_frozen_before_source_row_changes(self):
        self.probes()
        row = self.evaluate()
        before = self.source_page(row, 'availability')
        self.assertEqual(before['total'], 100)
        self.assertTrue(all(fact['status'] == 'healthy' for fact in before['items']))
        with self.m.SessionLocal() as db:
            Slot = self.m.sla_probe_runtime['Slot']
            db.query(Slot).update({'status': 'failed', 'config_summary_json': '{"JWT":"PRIVATE"}'})
            db.commit()
        self.assertEqual(self.source_page(row, 'availability'), before)
        self.assertNotIn('PRIVATE', json.dumps(before))

    def test_failed_probe_retains_safe_diagnostics_without_config_or_url(self):
        self.probes()
        Slot = self.m.sla_probe_runtime['Slot']
        with self.m.SessionLocal() as db:
            rows = list(db.scalars(select(Slot).order_by(Slot.slot).limit(2)))
            rows[0].status, rows[0].reason_code = 'failed', 'http_failure'
            rows[0].status_code, rows[0].latency_ms = 503, 123
            rows[0].responsibility = 'provider'
            rows[0].config_summary_json = '{"url":"PRIVATE","secret":"PRIVATE"}'
            rows[1].reason_code = 'https://PRIVATE/secret'
            db.commit()
        page = self.source_page(self.evaluate(), 'availability')
        fact = page['items'][0]
        self.assertEqual((fact['status'], fact['source_reason'], fact['status_code'], fact['duration_ms']),
                         ('failed', 'http_failure', 503, 123))
        self.assertEqual(fact['responsibility'], 'provider')
        self.assertTrue(fact['policy_version'].startswith('sla-'))
        self.assertNotIn('PRIVATE', json.dumps(page))

    def test_gateway_evidence_uses_actual_aggregate_rows_and_current_window_is_absent(self):
        self.requests()
        row = self.evaluate()
        frozen = self.source_page(row, 'latency_p95')
        self.assertEqual(frozen['total'], 100)
        self.assertEqual(len(frozen['items']), 100)
        self.assertTrue(all(fact['included'] for fact in frozen['items']))
        self.assertEqual(sum(fact['duration_ms'] == 1000 for fact in frozen['items']), 6)
        self.assertEqual(frozen['metric_result']['sample_count'], 100)
        self.assertFalse(frozen['source_available']['latency_p95'])
        sent_ms = int((self.start + timedelta(seconds=1)).timestamp() * 1000)
        self.assertEqual(frozen['items'][0]['occurred_at'], datetime.fromtimestamp(sent_ms / 1000, timezone.utc).isoformat())
        self.assertTrue(all(fact['responsibility'] == 'unknown' for fact in frozen['items']))
        self.assertTrue(all(fact['policy_version'].startswith('sla-') for fact in frozen['items']))
        Model = self.m.sla_gateway_events['Model']
        with self.m.SessionLocal() as db:
            db.query(Model).update({'duration_ms': 90000, 'upstream_statuses_json': '{"secret":"PRIVATE"}'})
            db.commit()
        self.assertEqual(self.source_page(row, 'latency_p95'), frozen)

    def test_other_buyers_gateway_events_cannot_enter_frozen_sources(self):
        self.requests()
        with self.m.SessionLocal() as db:
            db.add(self.m.ApiCredential(id='other-credential', route_id='route', enterprise_id='other_tenant',
                                       key_hash='other-synthetic-hash'))
            db.flush()
            identity = 'gw_' + 'f' * 32
            self.m.sla_gateway_events['ingest'](db, [dict(event_id=identity, request_id=identity,
                product_id='product', version_id='version', version_code='v1', entry_id='route-v1', route_id='route-v1',
                credential_id='other-credential', completed_at_ms=int((self.start + timedelta(seconds=10)).timestamp() * 1000),
                status_code=200, duration_ms=90000, authorized=True, published_route=True)])
            db.commit()
        frozen = self.source_page(self.evaluate(), 'request_error_rate')
        self.assertEqual(frozen['total'], 100)
        self.assertNotIn(identity, [fact['fact_id'] for fact in frozen['items']])


if __name__ == '__main__':
    unittest.main()
