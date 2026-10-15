"""Real persisted ledgers -> monthly assessment; synthetic facts only, SQLite."""
from datetime import datetime, timedelta, timezone
import importlib
import json
import unittest
from unittest.mock import patch

from sqlalchemy import select

import test_api_auto_delivery as fixture


class OnlineSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.ApiAutoDeliveryTests.setUpClass.__func__(cls)
        for name in ('sla_gateway_events', 'sla_probe_runtime'):
            if not hasattr(cls.m, name):
                importlib.import_module(cls.m.__package__ + '.' + name).install(vars(cls.m))

    tearDownClass = classmethod(fixture.ApiAutoDeliveryTests.tearDownClass.__func__)
    tearDown = fixture.ApiAutoDeliveryTests.tearDown
    request = fixture.ApiAutoDeliveryTests.request
    pay = fixture.ApiAutoDeliveryTests.pay
    task_id = fixture.ApiAutoDeliveryTests.task_id
    create = fixture.ApiAutoDeliveryTests.create
    step = fixture.ApiAutoDeliveryTests.step
    admin_request = fixture.ApiAutoDeliveryTests.admin_request
    redis_eval = fixture.ApiAutoDeliveryTests.redis_eval

    def setUp(self):
        fixture.ApiAutoDeliveryTests.setUp(self)
        roster = patch.dict(vars(self.m), sla_gateway_expected_producers=lambda *args: None)
        roster.start()
        self.addCleanup(roster.stop)
        self.order_id = self.create()
        self.step(self.order_id)
        with self.m.SessionLocal() as db:
            contract = self.m.order_sla_contracts['get_contract'](db, self.order_id)
            self.assertIsNotNone(contract.verified_at)
            self.contract_id = contract.id
            term = db.scalar(select(self.m.SubscriptionTerm).where(self.m.SubscriptionTerm.order_id == self.order_id))
            self.start = max(contract.verified_at.replace(tzinfo=timezone.utc), term.starts_at)
            if db.scalar(select(self.m.GatewayConfigRevision.id).where(self.m.GatewayConfigRevision.route_id == 'route')) is None:
                db.add(self.m.GatewayConfigRevision(route_id='route', product_id='product', revision=1,
                    status='active', config_json=json.dumps(self.expected_route)))
            db.commit()
        self.at = self.start + timedelta(minutes=500)
        patcher = patch.object(self.m, 'now', side_effect=lambda: self.at)
        patcher.start()
        self.addCleanup(patcher.stop)

    def probes(self):
        module = importlib.import_module(self.m.__package__ + '.sla_online_facts')
        Slot = self.m.sla_probe_runtime['Slot']
        with self.m.SessionLocal() as db:
            for slot in module.expected_probe_slots(self.start, self.at):
                at = datetime.fromtimestamp(slot * 300, timezone.utc)
                db.add(Slot(product_id='product', version_id='version', entry_id='order:' + self.order_id,
                    scope='business_entry', slot=slot, scheduled_at=at, observed_at=at, completed_at=at,
                    service_from=self.start, service_until=self.at + timedelta(days=1),
                    config_digest='synthetic-config', config_summary_json='{}', status='healthy',
                    publication_evidence_id='synthetic-published', eligibility_evidence_id='synthetic-term',
                    contract_evidence_id=self.contract_id, source_evidence_id='synthetic-source'))
            db.commit()

    def requests(self):
        with self.m.SessionLocal() as db:
            credential = db.scalar(select(self.m.ApiCredential).where(self.m.ApiCredential.enterprise_id == 'buyer_tenant'))
            events = []
            for index in range(100):
                identity = 'gw_' + format(index + 1, '032x')
                events.append(dict(event_id=identity, request_id=identity, product_id='product',
                    version_id='version', version_code='v1', entry_id='route-v1', route_id='route-v1',
                    credential_id=credential.id,
                    completed_at_ms=int((self.start + timedelta(seconds=index + 1)).timestamp() * 1000),
                    status_code=200, duration_ms=1.0 if index < 94 else 1000.0,
                    authorized=True, published_route=True, producer_id='synthetic-producer'))
            self.m.sla_gateway_events['ingest'](db, events)
            db.commit()

    def evaluate(self):
        return self.request('POST', '/api/sla/evaluate', actor='ops')['items'][0]

    def test_persisted_probe_slots_are_measured_without_network_or_management_fallback(self):
        self.probes()
        with patch.object(self.m, 'apisix_admin_request', side_effect=AssertionError('Assessment attempted provisioning')):
            row = self.evaluate()
        metric = row['metrics']['availability']
        self.assertEqual((metric['sample_count'], metric['value'], metric['coverage_percent']), (100, 100, 100))
        self.assertEqual(metric['status'], 'met')
        self.assertEqual(row['source_summary']['source_available']['availability'], True)

    def test_gateway_unsealed_windows_are_insufficient_even_with_one_hundred_real_rows(self):
        self.requests()
        row = self.evaluate()
        self.assertEqual(row['metrics']['request_error_rate']['sample_count'], 100)
        self.assertEqual(row['metrics']['request_error_rate']['status'], 'insufficient_data')
        self.assertEqual(row['metrics']['latency_p95']['status'], 'insufficient_data')
        self.assertEqual(row['source_summary']['source_available']['request_error_rate'], False)

    def test_api_shared_entry_accepts_other_contract_evidence_without_pooling_samples(self):
        self.probes()
        Slot = self.m.sla_probe_runtime['Slot']
        with self.m.SessionLocal() as db:
            for row in db.scalars(select(Slot)):
                row.entry_id = 'synthetic-shared-entry'
                row.contract_evidence_id = 'another-paid-contract'
            db.commit()
        def shared_query(**window):
            self.assertEqual(window['order_id'], self.order_id)
            return select(Slot).where(Slot.entry_id == 'synthetic-shared-entry',
                Slot.scheduled_at >= window['period_start'], Slot.scheduled_at < window['period_end'])
        # Runtime owns eligibility and entry mapping; this adapter never resolves
        # or provisions a source while assessing another buyer's frozen target.
        with patch.dict(self.m.sla_probe_runtime, slot_query=shared_query):
            row = self.evaluate()
        self.assertEqual(row['metrics']['availability']['sample_count'], 100)
        self.assertEqual(row['metrics']['availability']['status'], 'met')
        with self.m.SessionLocal() as db:
            first = db.scalar(select(Slot).order_by(Slot.slot))
            first.version_id = 'wrong-version'
            db.commit()
        with patch.dict(self.m.sla_probe_runtime, slot_query=shared_query):
            row = self.evaluate()
        self.assertEqual(row['metrics']['availability']['sample_count'], 99)
        self.assertEqual(row['metrics']['availability']['status'], 'insufficient_data')

    def test_saas_probe_evidence_remains_contract_specific(self):
        self.probes()
        Slot = self.m.sla_probe_runtime['Slot']
        with self.m.SessionLocal() as db:
            order = db.get(self.m.Order, self.order_id)
            contract = self.m.order_sla_contracts['get_contract'](db, self.order_id)
            contract.delivery_method = 'tenant_access'
            for row in db.scalars(select(Slot)):
                row.contract_evidence_id = 'other-contract'
            result = self.m.sla_online_sources['measure_order'](
                db, order, contract, self.start, self.at, self.at)
        self.assertFalse(result['source_available']['availability'])

    def test_synthetically_sealed_windows_preserve_nearest_rank_p95_not_mean(self):
        self.requests()
        Window = self.m.sla_gateway_events['SourceWindow']
        with self.m.SessionLocal() as db:
            for slot in range(int(self.start.timestamp()) // 300, (int(self.at.timestamp()) + 299) // 300):
                row = db.get(Window, ('synthetic-producer', slot))
                if row is None:
                    row = Window(producer_id='synthetic-producer', window_slot=slot, updated_at=self.at)
                    db.add(row)
                # Test-only seal. Production collector intentionally cannot seal.
                row.complete, row.failure_count = True, 0
            db.commit()
        with patch.dict(vars(self.m), {'sla_gateway_expected_producers': lambda *args: ['synthetic-producer']}):
            result = self.evaluate()
        self.assertEqual((result['p95_latency_ms'], result['metrics']['latency_p95']['sample_count']), (1000, 100))
        self.assertNotEqual(result['p95_latency_ms'], 60.94)
        self.assertIsNone(result['avg_latency_ms'])

    def test_absent_or_revoked_service_evidence_never_borrows_other_orders_probe_rows(self):
        self.probes()
        with self.m.SessionLocal() as db:
            term = db.scalar(select(self.m.SubscriptionTerm).where(self.m.SubscriptionTerm.order_id == self.order_id))
            term.status = 'revoked'
            db.commit()
        row = self.evaluate()
        self.assertIsNone(row['availability'])
        self.assertEqual(row['metrics']['availability']['status'], 'insufficient_data')

    def test_request_statistics_are_scoped_to_order_buyer(self):
        self.requests()
        before = self.evaluate()
        with self.m.SessionLocal() as db:
            db.add(self.m.ApiCredential(id='other-credential', route_id='route', enterprise_id='other_tenant',
                                       key_hash='other-synthetic-hash'))
            db.flush()
            identity = 'gw_' + 'f' * 32
            self.m.sla_gateway_events['ingest'](db, [dict(event_id=identity, request_id=identity,
                product_id='product', version_id='version', version_code='v1', entry_id='route-v1', route_id='route-v1',
                credential_id='other-credential', completed_at_ms=int((self.start + timedelta(seconds=10)).timestamp() * 1000),
                status_code=200, duration_ms=90000.0, authorized=True, published_route=True)])
            db.commit()
        after = self.evaluate()
        self.assertEqual(after['metrics']['latency_p95']['sample_count'], 100)
        self.assertEqual(after['p95_latency_ms'], before['p95_latency_ms'])
        self.assertEqual(after['version'], before['version'])

    def test_future_renewal_service_window_is_not_started_not_source_fault(self):
        with self.m.SessionLocal() as db:
            term = db.scalar(select(self.m.SubscriptionTerm).where(self.m.SubscriptionTerm.order_id == self.order_id))
            term.starts_at = self.at + timedelta(days=1)
            db.commit()
        row = self.evaluate()
        for name in ('availability', 'request_error_rate', 'latency_p95'):
            self.assertEqual(row['metrics'][name]['status'], 'not_started')


if __name__ == '__main__':
    unittest.main()
