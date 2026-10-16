"""Isolated HTTP persistence/compatibility tests; source facts are explicit."""
from datetime import timedelta
import importlib
import os
import unittest
from unittest.mock import patch

import test_order_sla_contracts as fixture


class OnlineAssessmentTests(unittest.TestCase):
    setUpClass = classmethod(fixture.OrderSlaContractTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.OrderSlaContractTests.tearDownClass.__func__)
    tearDown = fixture.OrderSlaContractTests.tearDown
    request = fixture.OrderSlaContractTests.request
    create_order = fixture.OrderSlaContractTests.create_order
    pay = fixture.OrderSlaContractTests.pay
    task_id = fixture.OrderSlaContractTests.task_id
    process = fixture.OrderSlaContractTests.process

    def setUp(self):
        fixture.OrderSlaContractTests.setUp(self)
        self.Assessment = self.m.sla_online_assessments['Assessment']
        self.metric = importlib.import_module(self.m.__package__ + '.sla_metrics').MetricResult

    def paid(self):
        order = self.create_order()
        self.pay(order)
        return order

    def evaluate(self, expected=200, suffix=''):
        return self.request('POST', '/api/sla/evaluate' + suffix, actor='ops', expected=expected)

    def sources(self, availability=97):
        return {'measurements': {
            'availability': self.metric(availability, 'met', 100, coverage_percent=100),
            'request_error_rate': self.metric(1, 'met', 100),
            'latency_p95': self.metric(2000, 'met', 100, completeness_percent=100)},
            'source_available': {key: True for key in ('availability', 'request_error_rate', 'latency_p95')},
            'source_version': 'fixture-v1', 'evidence_digest': 'fixture-evidence'}

    def api_contracts(self):
        with self.m.SessionLocal() as db:
            db.query(self.m.order_sla_contracts['Contract']).update({'delivery_method': 'api'})
            db.commit()

    def test_missing_sources_do_not_fall_back_to_old_api_usage_or_profiles(self):
        order = self.paid()
        self.api_contracts()
        row = self.evaluate()['items'][0]
        self.assertEqual((row['order_id'], row['status']), (order, 'insufficient_data'))
        for field in ('availability', 'error_rate', 'avg_latency_ms', 'p95_latency_ms', 'delivery_compliance'):
            self.assertIsNone(row[field])
        self.assertEqual(row['metrics']['availability']['reason'], 'source_missing')

    def test_same_measurements_classified_per_frozen_order_not_profile(self):
        old = self.paid()
        self.request('POST', '/api/sla/service-level-assignments', actor='ops', json={
            'enterprise_id': 'buyer_tenant', 'service_level_id': 'standard', 'reason': 'Future orders only'})
        new = self.paid()
        self.api_contracts()
        with patch.dict(vars(self.m), {'sla_online_sources': {'measure_order': lambda *args: self.sources()}}):
            rows = {row['order_id']: row for row in self.evaluate()['items']}
        self.assertEqual(rows[old]['metrics']['availability']['status'], 'breached')
        self.assertEqual(rows[new]['metrics']['availability']['status'], 'met')
        self.assertEqual(rows[old]['metrics']['availability']['frozen_tier'], 'strategic')
        self.assertEqual(rows[new]['metrics']['availability']['frozen_tier'], 'standard')
        self.assertEqual(rows[old]['p95_latency_ms'], 2000)
        self.assertIsNone(rows[old]['avg_latency_ms'])

    def test_matrix_revision_changes_only_new_frozen_order_assessments(self):
        old = self.paid()
        self.request('PATCH', '/api/sla/service-levels/strategic/matrix', actor='ops', json={
            'expected_version': 0, 'metrics': {'availability': {'target': 95, 'warning': 96}}})
        new = self.paid()
        self.api_contracts()
        with patch.dict(vars(self.m), {'sla_online_sources': {'measure_order': lambda *args: self.sources()}}):
            rows = {row['order_id']: row for row in self.evaluate()['items']}
        self.assertEqual(rows[old]['metrics']['availability']['status'], 'breached')
        self.assertEqual(rows[new]['metrics']['availability']['status'], 'met')
        self.assertNotEqual(rows[old]['rule_version'], rows[new]['rule_version'])

    def test_persistence_idempotence_and_changed_evidence_versions_audit(self):
        self.paid()
        self.api_contracts()
        source = self.sources()
        with patch.dict(vars(self.m), {'sla_online_sources': {'measure_order': lambda *args: source}}):
            first = self.evaluate()['items'][0]
            second = self.evaluate()['items'][0]
            self.assertEqual((first['id'], first['version']), (second['id'], second['version']))
            source['measurements']['availability'] = self.metric(100, 'met', 100, coverage_percent=100)
            third = self.evaluate()['items'][0]
        self.assertEqual(third['version'], first['version'] + 1)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.Assessment).count(), 1)
            self.assertEqual(db.query(self.m.AuditLog).filter_by(action='evaluate_order_sla').count(), 2)

    def test_no_data_and_advancing_now_do_not_create_spurious_versions(self):
        order = self.paid()
        self.process(self.task_id(order))
        self.api_contracts()
        first = self.evaluate()['items'][0]
        self.at += timedelta(seconds=60)
        second = self.evaluate()['items'][0]
        self.assertEqual((first['id'], first['version']), (second['id'], second['version']))
        self.assertEqual(second['metrics']['availability']['status'], 'insufficient_data')

    def test_single_delivery_deadline_warns_without_ten_order_rate_samples(self):
        order = self.paid()
        with self.m.SessionLocal() as db:
            contract = self.m.order_sla_contracts['get_contract'](db, order)
            paid = contract.paid_at
        self.at = paid + timedelta(seconds=191)
        self.assertEqual(self.evaluate()['items'][0]['metrics']['delivery_duration']['status'], 'met')
        self.at = paid + timedelta(seconds=192)
        row = self.evaluate()['items'][0]
        self.assertEqual(row['metrics']['delivery_duration']['status'], 'warning')
        self.assertEqual(row['metrics']['delivery_duration']['target'], 240)
        self.assertEqual(row['metrics']['delivery_timeliness']['status'], 'insufficient_data')
        self.at = paid + timedelta(seconds=240)
        self.assertEqual(self.evaluate()['items'][0]['metrics']['delivery_duration']['status'], 'warning')
        self.at = paid + timedelta(seconds=241)
        with patch.object(self.m, 'notify_business_event', wraps=self.m.notify_business_event) as notify:
            first = self.evaluate()['items'][0]
            self.evaluate()
        calls = [call for call in notify.call_args_list
                 if call.kwargs.get('event_key', '').startswith('order-delivery-duration:')]
        self.assertEqual(len(calls), 1)
        self.assertEqual(first['status'], 'breached')
        with self.m.SessionLocal() as db:
            db.get(self.m.Order, order).payment_status = 'refunded'
            db.commit()
        self.at += timedelta(seconds=1)
        self.assertEqual(self.evaluate()['items'][0]['metrics']['delivery_duration']['status'], 'breached')
        with self.m.SessionLocal() as db:
            Message = self.m.message_center['Message']
            self.assertEqual(db.query(Message).filter_by(
                event_key='order-delivery-duration:' + order + ':breached').count(), 1)

    def test_verified_delivery_duration_stops_at_first_success_and_failure_is_atomic(self):
        order = self.paid()
        self.at += timedelta(seconds=241)
        with patch.object(self.m, 'notify_business_event', side_effect=RuntimeError('Outbox unavailable')):
            self.evaluate(500)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.Assessment).count(), 0)
            self.assertEqual(db.query(self.m.AuditLog).filter_by(action='evaluate_order_sla').count(), 0)
        self.process(self.task_id(order))
        first = self.evaluate()['items'][0]
        self.at += timedelta(days=1)
        later = self.evaluate()['items'][0]
        self.assertEqual(first['metrics']['delivery_duration'], later['metrics']['delivery_duration'])
        self.assertEqual(later['metrics']['delivery_duration']['status'], 'breached')

    def test_assessment_audit_failure_rolls_back_new_results(self):
        self.paid()
        real = self.m.audit
        def fail(db, actor, action, *args, **kwargs):
            if action == 'evaluate_order_sla':
                raise RuntimeError('Assessment audit unavailable')
            return real(db, actor, action, *args, **kwargs)
        with patch.object(self.m, 'audit', side_effect=fail):
            self.evaluate(500)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.Assessment).count(), 0)

    def test_assessment_outbox_failure_rolls_back_result_and_audit(self):
        self.paid()
        self.api_contracts()
        with patch.dict(vars(self.m), {'sla_online_sources': {'measure_order': lambda *args: self.sources()}}):
            with patch.object(self.m, 'notify_business_event', side_effect=RuntimeError('Outbox unavailable')):
                self.evaluate(500)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.Assessment).count(), 0)
            self.assertEqual(db.query(self.m.AuditLog).filter_by(action='evaluate_order_sla').count(), 0)

    def test_delivery_missing_frozen_facts_cannot_be_dropped_to_manufacture_compliance(self):
        orders = [self.paid() for _ in range(10)]
        for order in orders:
            self.process(self.task_id(order))
        self.at += timedelta(minutes=5)
        complete = self.evaluate()['items'][0]
        self.assertEqual(complete['status'], 'met')
        self.assertTrue(all(complete['metrics'][name]['status'] == 'not_applicable'
                            for name in ('availability', 'request_error_rate', 'latency_p95')))
        with self.m.SessionLocal() as db:
            Contract = self.m.order_sla_contracts['Contract']
            db.query(self.Assessment).filter_by(order_id=orders[-1]).delete()
            db.query(Contract).filter_by(order_id=orders[-1]).delete()
            db.commit()
        rows = self.evaluate()['items']
        for row in rows:
            self.assertEqual(row['metrics']['delivery_timeliness']['status'], 'insufficient_data')

    def test_corrupt_order_snapshot_is_not_replaced_with_current_enterprise_rules(self):
        order = self.paid()
        with self.m.SessionLocal() as db:
            contract = self.m.order_sla_contracts['get_contract'](db, order)
            contract.snapshot_json = '{}'
            db.commit()
        row = self.evaluate()['items'][0]
        self.assertEqual(row['metrics']['delivery_timeliness']['reason'], 'invalid_snapshot')

    def test_route_labels_have_explicit_version_and_probe_identity_without_upstream_secrets(self):
        with self.m.SessionLocal() as db:
            route = self.m.ApiGatewayRoute(id='sla-route', product_id='product', route_key='sla-entry',
                version='v1', upstream_url='https://provider.example.invalid', health_path='/health',
                health_method='HEAD', rate_limit_per_minute=60, timeout_ms=1000,
                upstream_client_secret='NEVER-EXPORT-SECRET', upstream_auth_mode='oauth2')
            db.add(route)
            db.flush()
            labels = self.m.apisix_route_payload(route, db.get(self.m.Product, 'product'), db)['labels']
            self.assertEqual(labels['market_version_id'], 'version')
            self.assertEqual(labels['market_route_id'], route.id)
            self.assertEqual((labels['market_health_path'], labels['market_health_method']), ('/health', 'HEAD'))
            self.assertNotIn('NEVER-EXPORT-SECRET', str(labels))
            self.assertNotIn('https://', str(labels))
            with patch.dict(os.environ, {'SLA_PROBE_SECRET': 'synthetic-probe-secret-with-at-least-32-chars',
                                         'SLA_PROBE_SOURCE_CIDRS': '10.23.0.0/24'}):
                labels = self.m.apisix_route_payload(route, db.get(self.m.Product, 'product'), db)['labels']
                self.assertEqual(labels['market_sla_probe_enabled'], 'true')
                self.assertEqual(labels['market_sla_probe_path'], '/gateway/sla-entry/health')
                self.assertEqual(labels['market_sla_probe_method'], 'HEAD')
                self.assertEqual(labels['market_sla_probe_route_id'], route.id)
                runtime = importlib.import_module(self.m.__package__ + '.sla_probe_runtime')
                self.assertEqual(labels['market_sla_probe_consumer'], runtime.probe_consumer_name(route.id, 'product', 'version'))
                self.assertEqual(labels['market_sla_probe_source_cidrs'], '10.23.0.0/24')
                self.assertTrue(labels['market_sla_probe_config_version'])
                self.assertNotIn(os.environ['SLA_PROBE_SECRET'], str(labels))
            with patch.dict(os.environ, {'SLA_PROBE_SECRET': 'synthetic-probe-secret-with-at-least-32-chars',
                                         'SLA_PROBE_SOURCE_CIDRS': '0.0.0.0/0'}):
                labels = self.m.apisix_route_payload(route, db.get(self.m.Product, 'product'), db)['labels']
                self.assertNotIn('market_sla_probe_enabled', labels)
            route.health_path = '/health?token=NEVER-EXPORT-SECRET'
            self.assertNotIn('market_health_path', self.m.apisix_route_payload(route, db.get(self.m.Product, 'product'), db)['labels'])

    def test_legacy_rows_are_labeled_and_not_mutated_or_counted_as_met(self):
        with self.m.SessionLocal() as db:
            profile = self.m.SLAProfile(name='Legacy profile', created_by='fixture')
            db.add(profile)
            db.flush()
            db.add(self.m.SLAResult(profile_id=profile.id, status='met', availability=100,
                                   period_start=self.at - timedelta(days=1), period_end=self.at))
            db.commit()
        self.assertEqual(self.evaluate()['items'], [])
        overview = self.request('GET', '/api/sla/overview', actor='ops')
        self.assertEqual(overview['summary']['met'], 0)
        self.assertEqual(overview['results'][0]['status'], 'legacy')
        self.assertIsNone(overview['results'][0]['availability'])
        with self.m.SessionLocal() as db:
            row = db.query(self.m.SLAResult).one()
            self.assertEqual((row.status, float(row.availability)), ('met', 100))

    def test_beijing_month_boundaries_and_permission(self):
        self.paid()
        row = self.evaluate()['items'][0]
        self.assertTrue(row['period_start'].startswith('2026-10-31T16:00:00'))
        self.evaluate(422, '?year=2026')
        self.evaluate(422, '?year=2027&month=1')
        for query in ('?year=2026&month=0', '?year=2026&month=13', '?year=0&month=10'):
            self.evaluate(422, query)
        with self.m.SessionLocal() as db:
            for year, month in ((2026, 0), (2026, 13), (0, 10), (True, 10)):
                with self.assertRaises(Exception) as raised:
                    self.m.sla_online_assessments['evaluate'](db, year=year, month=month, at=self.at)
                self.assertEqual(raised.exception.status_code, 422)
        for actor in ('buyer', 'provider', 'finance', 'quality'):
            for method, path in [('POST', '/api/sla/evaluate'), ('GET', '/api/sla/results'), ('GET', '/api/sla/overview')]:
                self.request(method, path, actor=actor, expected=403)

    def test_deadline_cohort_min_ten_includes_overdue_uncompleted_and_refunds(self):
        orders = [self.paid() for _ in range(10)]
        for order in orders[:9]:
            self.process(self.task_id(order))
        self.at += timedelta(minutes=5)
        rows = self.evaluate()['items']
        result = next(row for row in rows if row['order_id'] == orders[-1])
        metric = result['metrics']['delivery_timeliness']
        self.assertEqual((metric['sample_count'], metric['value'], metric['status']), (10, 90, 'breached'))
        with self.m.SessionLocal() as db:
            last = db.get(self.m.Order, orders[-1])
            last.payment_status = 'refunded'
            db.commit()
        result = next(row for row in self.evaluate()['items'] if row['order_id'] == orders[-1])
        self.assertEqual(result['metrics']['delivery_timeliness']['sample_count'], 10)

    def test_insufficient_gate_cannot_be_overridden_by_a_forged_source_met(self):
        self.paid()
        self.api_contracts()
        source = self.sources()
        source['measurements']['latency_p95'] = self.metric(1, 'met', 99, completeness_percent=100)
        source['measurements']['availability'] = self.metric(100, 'met', 100, coverage_percent=89)
        with patch.dict(vars(self.m), {'sla_online_sources': {'measure_order': lambda *args: source}}):
            metrics = self.evaluate()['items'][0]['metrics']
        self.assertEqual(metrics['latency_p95']['reason'], 'sample_count')
        self.assertEqual(metrics['availability']['reason'], 'probe_coverage')


if __name__ == '__main__':
    unittest.main()
