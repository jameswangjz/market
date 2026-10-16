"""Matrix and binding contracts against isolated, disposable SQLite only."""
from datetime import timedelta
import csv
import importlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import select
import test_enterprise_sla as enterprise_fixture


class MatrixTests(unittest.TestCase):
    setUpClass = classmethod(enterprise_fixture.EnterpriseSLATests.setUpClass.__func__)
    tearDownClass = classmethod(enterprise_fixture.EnterpriseSLATests.tearDownClass.__func__)
    setUp = enterprise_fixture.EnterpriseSLATests.setUp
    tearDown = enterprise_fixture.EnterpriseSLATests.tearDown
    request = enterprise_fixture.EnterpriseSLATests.request
    adjust = enterprise_fixture.EnterpriseSLATests.adjust
    snapshot = enterprise_fixture.EnterpriseSLATests.snapshot

    def edit(self, metrics, version=0, expected=200, actor='ops', level='standard', **extra):
        return self.request('PATCH', f'/api/sla/service-levels/{level}/matrix', actor=actor,
            expected=expected, json={'expected_version': version, 'metrics': metrics, **extra})

    def test_revision_freeze_hash_and_old_snapshot_are_preserved(self):
        old = self.snapshot()
        encoded = json.dumps(old, sort_keys=True)
        response = self.edit({'availability': {'target': 93, 'warning': 95},
                              'delivery': {'target': 8, 'warning': 6}})['item']
        self.assertEqual(response['version'], 1)
        self.assertEqual(response['metrics']['delivery']['target_seconds'], 480)
        new = self.snapshot()
        self.assertNotEqual(old['rule_version'], new['rule_version'])
        self.assertEqual(new['metrics']['availability']['target'], 93)
        parser = importlib.import_module(self.m.__package__ + '.sla_order_assessment')
        for snapshot in (old, new):
            parser._snapshot(snapshot, 'buyer_tenant', 'availability')
        self.assertEqual(encoded, json.dumps(old, sort_keys=True))
        history = self.request('GET', '/api/sla/service-levels/standard/matrix/versions', actor='ops')['items']
        self.assertEqual([row['version'] for row in history], [1, 0])
        self.assertEqual(history[1]['metrics']['availability']['target'], old['metrics']['availability']['target'])
        self.edit({'availability': {'target': 90, 'warning': 92}}, expected=409)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SLAMatrixVersion).count(), 2)
            self.assertEqual(db.query(self.m.AuditLog).filter_by(action='update_sla_matrix').count(), 1)

    def test_validation_rejects_unknown_readonly_bool_range_and_direction(self):
        invalid = [
            {'unknown': {'target': 1, 'warning': 1}},
            {'availability': {'target': 101, 'warning': 102}},
            {'availability': {'target': 95, 'warning': 94}},
            {'delivery': {'target': 1, 'warning': 2}},
            {'latency_p95': {'target': 0, 'warning': 0}},
            {'delivery': {'target': True, 'warning': 1}},
            {'delivery': {'target': 2, 'warning': 1, 'clock': 'support'}},
            {'delivery': {'target': 1e308, 'warning': 1}},
            {'delivery': {'target': 0.5, 'warning': 0.4}},
        ]
        for metrics in invalid:
            self.edit(metrics, expected=422)
        self.edit({'delivery': {'target': 2, 'warning': 1}}, calendar={}, expected=422)
        for actor in ('buyer', 'finance', 'reviewer', 'quality'):
            self.edit({'delivery': {'target': 2, 'warning': 1}}, expected=403, actor=actor)
            self.request('GET', '/api/sla/matrices', actor=actor, expected=403)

    def test_custom_copies_current_complete_matrix_and_calendar_then_detaches(self):
        self.edit({'availability': {'target': 94, 'warning': 96}}, level='enterprise')
        source = self.request('GET', '/api/sla/service-levels/enterprise/matrix', actor='ops')['item']
        created = self.request('POST', '/api/sla/service-levels', actor='ops',
            json={'code': 'custom', 'name': 'Custom', 'base_code': 'enterprise'})['item']
        custom = self.request('GET', f"/api/sla/service-levels/{created['id']}/matrix", actor='ops')['item']
        self.assertEqual(custom['metrics'], source['metrics'])
        self.assertEqual(custom['calendar'], source['calendar'])
        self.adjust(level=created['id'])
        frozen = self.snapshot()
        self.assertEqual(frozen['service_level_code'], 'custom')
        self.edit({'availability': {'target': 95, 'warning': 97}}, level='enterprise', version=1)
        self.assertEqual(self.snapshot()['metrics'], frozen['metrics'])
        self.request('POST', '/api/sla/service-levels', actor='ops', expected=400,
            json={'code': 'incomplete', 'name': 'Incomplete'})
        items = self.request('GET', '/api/sla/matrices', actor='ops')['items']
        self.assertEqual([row['code'] for row in items], ['standard', 'enterprise', 'strategic', 'custom'])

    def test_expiry_get_readonly_exact_boundary_audit_idempotence_no_older_fallback(self):
        self.adjust(expires_at=(self.clock + timedelta(hours=1)).isoformat())
        self.m.now.return_value = self.clock + timedelta(hours=1)
        for _ in range(2):
            listing = self.request('GET', '/api/sla/enterprise-bindings?q=buyer&page=99&page_size=1', actor='ops')
            self.assertEqual(listing['total'], 1)
            self.assertEqual(listing['page'], 1)
            self.assertEqual(listing['items'][0]['service_level']['code'], 'standard')
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SLAExpiryFallback).count(), 0)
        self.assertEqual(self.request('POST', '/api/sla/expire-bindings', actor='ops')['audited'], 1)
        self.assertEqual(self.request('POST', '/api/sla/expire-bindings', actor='ops')['audited'], 0)
        self.assertEqual(self.snapshot()['service_level_code'], 'standard')
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.AuditLog).filter_by(action='expire_service_level_assignment').count(), 1)
        self.request('GET', '/api/sla/enterprise-bindings', actor='buyer', expected=403)

    def test_users_enterprises_legacy_and_scoped_filtered_counts(self):
        for route in ('/api/users', '/api/admin/enterprises'):
            legacy = self.request('GET', route, actor='buyer')
            self.assertNotIn('page', legacy)
            paged = self.request('GET', route + '?page=99&page_size=1', actor='buyer')
            self.assertEqual(paged['total'], len(legacy['items']))
            empty = self.request('GET', route + '?page=99&page_size=1&q=other_tenant', actor='buyer')
            self.assertEqual((empty['total'], empty['page'], empty['items']), (0, 1, []))

    def test_legacy_level_metadata_write_is_disabled(self):
        self.request('PATCH', '/api/sla/service-levels/standard', actor='ops', expected=410,
            json={'code': 'standard', 'name': 'Standard', 'initial_response_minutes': 1})

    def test_csv_metadata_is_complete_readonly_and_outside_signed_rules(self):
        path = Path(__file__).resolve().parents[2] / 'docs' / 'SLA级别指标配置矩阵-提前预警修正版-20261014.csv'
        with path.open(encoding='utf-8-sig', newline='') as stream:
            original = list(csv.DictReader(stream))
        items = self.request('GET', '/api/sla/matrices', actor='ops')['items']
        extracted = []
        for item in items:
            rows = item['display_metadata']['rows']
            self.assertEqual(len(rows), 16)
            self.assertEqual(sum(row['metric_key'] is not None for row in rows), 14)
            extracted.extend({key: value for key, value in row.items() if key != 'metric_key'} for row in rows)
        self.assertEqual(extracted, original)
        self.assertNotIn('display_metadata', self.snapshot())
        self.edit({'delivery': {'target': 5, 'warning': 4}}, display_metadata={}, expected=422)

    def test_initialize_persists_baselines_once_without_replacing_manual_values(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.ServiceLevel, 'standard').initial_response_minutes = 777
            db.flush()
            self.m.sla_matrix['initialize'](db)
            self.m.sla_matrix['initialize'](db)
            db.commit()
            self.assertEqual(db.query(self.m.SLAMatrixVersion).count(), 3)
        self.assertEqual(self.snapshot()['metrics']['first_response']['target'], 777)
        self.edit({'first_response': {'target': 800, 'warning': 600}})
        with self.m.SessionLocal() as db:
            self.m.sla_matrix['initialize'](db)
            self.assertEqual(db.query(self.m.SLAMatrixVersion).count(), 4)

    def test_regular_member_sees_self_role_and_active_enterprise(self):
        with self.m.SessionLocal() as db:
            member = db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == 'buyer'))
            member.role = 'member'
            db.commit()
        for suffix in ('', '?page=1&page_size=10', '?page=1&enterprise_id=buyer_tenant'):
            items = self.request('GET', '/api/users' + suffix, actor='buyer')['items']
            self.assertEqual([row['id'] for row in items], ['buyer'])
            self.assertEqual(items[0]['enterprise_name'], 'buyer_tenant')
            self.assertNotEqual(items[0]['user_role'], '未加入企业')
            self.assertEqual(items[0]['enterprise_memberships'][0]['role'], 'member')

    def test_shared_member_hidden_membership_cannot_be_probed_with_filter_or_count(self):
        with self.m.SessionLocal() as db:
            db.add(self.m.Membership(user_id='buyer_admin', enterprise_id='other_tenant', role='super_admin'))
            db.commit()
        items = self.request('GET', '/api/users?page=1', actor='buyer')['items']
        shared = next(row for row in items if row['id'] == 'buyer_admin')
        self.assertEqual({row['enterprise_id'] for row in shared['enterprise_memberships']}, {'buyer_tenant'})
        self.assertNotIn('other_tenant', shared['enterprise_name'])
        for enterprise_id in ('other_tenant', 'unknown'):
            result = self.request('GET', '/api/users?page=1&enterprise_id=' + enterprise_id, actor='buyer')
            self.assertEqual((result['total'], result['items']), (0, []))

    def test_freeze_audits_expiry_without_worker_and_never_revives_older_binding(self):
        with self.m.SessionLocal() as db:
            db.add_all([
                self.m.ServiceLevelAssignment(service_level_id='strategic', enterprise_id='buyer_tenant',
                    effective_at=self.clock - timedelta(days=2)),
                self.m.ServiceLevelAssignment(service_level_id='enterprise', enterprise_id='buyer_tenant',
                    effective_at=self.clock - timedelta(days=1), expires_at=self.clock)])
            db.commit()
        with self.m.SessionLocal() as db:
            for _ in range(2):
                self.assertEqual(self.m.freeze_sla_snapshot(db, 'buyer_tenant')['service_level_code'], 'standard')
            db.commit()
            self.assertEqual(db.query(self.m.SLAExpiryFallback).count(), 1)

    def test_enterprise_name_search_never_exposes_hidden_shared_membership(self):
        with self.m.SessionLocal() as db:
            db.add(self.m.Membership(user_id='buyer_admin', enterprise_id='other_tenant', role='member'))
            db.add(self.m.EnterpriseInvitation(enterprise_id='buyer_tenant', inviter_id='buyer',
                invitee_id='outsider', target='outsider@example.invalid', token='slc-name-invitation',
                expires_at=self.clock + timedelta(days=1)))
            db.commit()
        listing = self.request('GET', '/api/users?page=1&enterprise_name=buyer_tenant', actor='buyer')
        self.assertIn('outsider', {row['id'] for row in listing['items']})
        hidden = self.request('GET', '/api/users?page=1&enterprise_name=other_tenant', actor='buyer')
        self.assertEqual((hidden['total'], hidden['items']), (0, []))
        visible = self.request('GET', '/api/users?page=1&enterprise_name=other_tenant', actor='platform')
        self.assertIn('buyer_admin', {row['id'] for row in visible['items']})

    def test_matrix_audit_failure_rolls_back_revision_and_thresholds(self):
        with patch.object(self.m, 'audit', side_effect=RuntimeError('audit failed')):
            self.edit({'delivery': {'target': 5, 'warning': 4}}, expected=500)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.SLAMatrixVersion).count(), 0)
        self.assertEqual(self.snapshot()['metrics']['delivery']['target'], 10)

    def test_pending_invitee_and_pending_activation_survive_paging_and_enterprise_filter(self):
        with self.m.SessionLocal() as db:
            db.add(self.m.EnterpriseInvitation(enterprise_id='buyer_tenant', inviter_id='buyer',
                invitee_id='outsider', target='outsider@example.invalid', token='matrix-test-invitation',
                expires_at=self.clock + timedelta(days=1)))
            member = db.scalar(select(self.m.Membership).where(self.m.Membership.user_id == 'inactive'))
            member.status = 'pending_activation'
            db.commit()
        result = self.request('GET', '/api/users?page=1&page_size=100&enterprise_id=buyer_tenant', actor='buyer')
        ids = {row['id'] for row in result['items']}
        self.assertTrue({'inactive', 'outsider'} <= ids)
        self.assertEqual(result['total'], len(ids))
        pending = self.request('GET', '/api/users?page=1&activation_status=pending_activation', actor='buyer')
        self.assertEqual([row['id'] for row in pending['items']], ['inactive'])

    def test_new_ticket_clocks_use_revision_and_existing_ticket_snapshot_stays_identical(self):
        def create_ticket(key):
            order_id = enterprise_fixture.fixture.BusinessRoutesHTTPTests.create_order(self)
            with self.m.SessionLocal() as db:
                order = db.get(self.m.Order, order_id)
                order.payment_status, order.paid_amount = 'paid', order.amount
                db.commit()
            return self.request('POST', '/api/after-sales', json={
                'order_id': order_id, 'description': 'Matrix revision ticket', 'idempotency_key': key})
        old = create_ticket('before-matrix')
        with self.m.SessionLocal() as db:
            original = db.get(self.m.AfterSalesTicket, old['id']).sla_snapshot_json
        self.edit({'first_response': {'target': 120, 'warning': 90}})
        new = create_ticket('after-matrix')
        for ticket, target in ((old, 2880), (new, 120)):
            clocks = self.request('GET', f"/api/after-sales/{ticket['id']}/sla")['items']
            response = next(row for row in clocks if row['metric'] == 'first_response')
            self.assertEqual(response['target_seconds'], target * 60)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.AfterSalesTicket, old['id']).sla_snapshot_json, original)


if __name__ == '__main__':
    unittest.main()
