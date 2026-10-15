"""Enterprise SLA HTTP/transaction tests using only isolated SQLite fixtures."""
import csv
from datetime import timedelta
import importlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import or_, select

import test_message_business_events as fixture


class EnterpriseSLATests(unittest.TestCase):
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown
    request = fixture.BusinessRoutesHTTPTests.request

    @classmethod
    def setUpClass(cls):
        fixture.BusinessRoutesHTTPTests.setUpClass.__func__(cls)
        module = importlib.import_module(cls.m.__package__ + '.enterprise_sla')
        cls.m.enterprise_sla = module.install(vars(cls.m))

    def setUp(self):
        fixture.BusinessRoutesHTTPTests.setUp(self)
        self.clock = self.m.now()
        self.time = patch.object(self.m, 'now', return_value=self.clock)
        self.time.start()
        self.addCleanup(self.time.stop)
        with self.m.SessionLocal() as db:
            db.add_all([self.m.ServiceLevel(id=code, code=code, name=code, status='active',
                initial_response_minutes={'standard': 2880, 'enterprise': 1440, 'strategic': 720}[code],
                urgent_initial_response_minutes={'standard': 1440, 'enterprise': 720, 'strategic': 360}[code],
                problem_response_hours=48) for code in ('standard', 'enterprise', 'strategic')])
            db.commit()

    def adjust(self, level='enterprise', reason='Contract upgrade', actor='ops', expected=200, **extra):
        return self.request('POST', '/api/sla/service-level-assignments', actor=actor, expected=expected,
            json={'enterprise_id': 'buyer_tenant', 'service_level_id': level, 'reason': reason, **extra})

    def review(self, decision='approve', level='enterprise', actor='ops', expected=200, **extra):
        return self.request('POST', '/api/admin/verifications/enterprise/buyer_tenant/review',
            actor=actor, expected=expected,
            json={'decision': decision, 'service_level_id': level, **extra})

    def snapshot(self):
        with self.m.SessionLocal() as db:
            return self.m.freeze_sla_snapshot(db, 'buyer_tenant')

    def pending(self):
        with self.m.SessionLocal() as db:
            db.get(self.m.Enterprise, 'buyer_tenant').verification_status = 'pending_review'
            db.commit()

    def test_only_system_admin_and_platform_operator_can_adjust_or_bind_at_review(self):
        for actor in ('buyer', 'buyer_admin', 'provider', 'outsider', 'finance', 'reviewer', 'quality'):
            self.adjust(actor=actor, expected=403)
            self.review(actor=actor, expected=403)
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'reviewer').platform_role = 'security_compliance'
            db.get(self.m.User, 'quality').platform_role = 'delivery_monitor'
            db.commit()
        for actor in ('reviewer', 'quality'):
            self.adjust(actor=actor, expected=403)
            self.review(actor=actor, expected=403)
        for actor in ('platform', 'ops'):
            self.adjust(actor=actor)
        self.assertEqual(self.snapshot()['service_level_code'], 'enterprise')

    def test_approval_and_binding_share_transaction_and_rejection_never_binds(self):
        self.pending()
        self.review()
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Enterprise, 'buyer_tenant').verification_status, 'verified')
            assignment = db.scalar(select(self.m.ServiceLevelAssignment))
            self.assertEqual((assignment.service_level_id, assignment.source), ('enterprise', 'verification'))
        self.pending()
        self.review(decision='reject', level='strategic', comment='Missing evidence')
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Enterprise, 'buyer_tenant').verification_status, 'rejected')
            self.assertEqual(db.query(self.m.ServiceLevelAssignment).count(), 1)
            self.assertEqual(db.scalar(select(self.m.ServiceLevelAssignment)).service_level_id, 'enterprise')

    def test_invalid_binding_rolls_back_approval_and_audit_failure_rolls_back_everything(self):
        self.pending()
        self.review(level='missing', expected=404)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Enterprise, 'buyer_tenant').verification_status, 'pending_review')
            self.assertEqual(db.query(self.m.ServiceLevelAssignment).count(), 0)
        real_audit = self.m.audit

        def failing_audit(db, actor, action, *args, **kwargs):
            if action == 'review_enterprise_verification':
                raise RuntimeError('Injected final review audit failure')
            return real_audit(db, actor, action, *args, **kwargs)

        with patch.object(self.m, 'audit', side_effect=failing_audit):
            self.review(expected=500)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.Enterprise, 'buyer_tenant').verification_status, 'pending_review')
            self.assertEqual(db.query(self.m.ServiceLevelAssignment).count(), 0)
            self.assertEqual(db.query(self.m.AuditLog).filter_by(action='assign_service_level').count(), 0)

    def test_adjustments_require_reason_verified_enterprise_and_enterprise_scope(self):
        for reason in ('', '   '):
            self.adjust(reason=reason, expected=400)
        self.review(sla_reason='', expected=400)
        self.adjust(user_id='buyer', expected=400)
        self.adjust(enterprise_id='missing', expected=404)
        self.pending()
        self.adjust(expected=409)
        self.review(level='')
        self.assertEqual(self.snapshot()['service_level_code'], 'standard')

    def test_adjustments_close_finite_assignments_and_record_reason_actor_and_version(self):
        first = self.adjust(level='standard', expires_at=(self.clock + timedelta(days=30)).isoformat())['item']
        second = self.adjust(level='enterprise', reason='  Approved premium support  ')['item']
        with self.m.SessionLocal() as db:
            previous = db.get(self.m.ServiceLevelAssignment, first['id'])
            self.assertEqual(previous.expires_at.replace(tzinfo=self.clock.tzinfo), self.clock)
            active = db.scalars(select(self.m.ServiceLevelAssignment).where(
                self.m.ServiceLevelAssignment.enterprise_id == 'buyer_tenant',
                self.m.ServiceLevelAssignment.effective_at <= self.clock,
                or_(self.m.ServiceLevelAssignment.expires_at.is_(None),
                    self.m.ServiceLevelAssignment.expires_at > self.clock))).all()
            self.assertEqual([item.id for item in active], [second['id']])
            log = db.scalar(select(self.m.AuditLog).where(self.m.AuditLog.target_id == second['id']))
            self.assertEqual((log.tenant_id, log.detail), ('buyer_tenant', 'Approved premium support'))
            after = json.loads(log.after_json)
            self.assertEqual(after['actor_role'], 'platform_operator')
            self.assertEqual(after['assignment']['assigned_by'], 'ops@example.invalid')
            self.assertEqual(len(after['rule_version']), 64)
            self.assertEqual(json.loads(log.before_json)['assignments'][0]['id'], first['id'])

    def test_invalid_expiry_disabled_level_and_unconfirmed_level_fail_without_changes(self):
        for expires in (self.clock, self.clock - timedelta(seconds=1)):
            self.adjust(expires_at=expires.isoformat(), expected=400)
        with self.m.SessionLocal() as db:
            db.get(self.m.ServiceLevel, 'enterprise').status = 'disabled'
            db.add(self.m.ServiceLevel(id='custom', code='custom', name='custom', status='active'))
            db.commit()
        self.adjust(expected=404)
        self.adjust(level='custom', expected=409)
        with self.m.SessionLocal() as db:
            self.assertEqual(db.query(self.m.ServiceLevelAssignment).count(), 0)

    def test_snapshot_uses_current_enterprise_binding_and_leaves_old_json_unchanged(self):
        old = self.snapshot()
        old_json = json.dumps(old, sort_keys=True)
        self.adjust()
        new = self.snapshot()
        self.assertEqual((old['service_level_code'], new['service_level_code']), ('standard', 'enterprise'))
        self.assertNotEqual(old['rule_version'], new['rule_version'])
        self.assertEqual(old_json, json.dumps(old, sort_keys=True))
        self.assertEqual(json.loads(json.dumps(new)), new)
        with self.m.SessionLocal() as db:
            db.get(self.m.ServiceLevel, 'enterprise').description = 'Updated future configuration'
            db.commit()
        latest = self.snapshot()
        self.assertNotEqual(latest['rule_version'], new['rule_version'])
        self.assertNotEqual(latest['legacy_level_config']['description'], new['legacy_level_config']['description'])
        self.assertEqual(old_json, json.dumps(old, sort_keys=True))

    def test_snapshot_defaults_at_exact_expiry_ignores_future_and_user_overrides(self):
        with self.m.SessionLocal() as db:
            db.add_all([
                self.m.ServiceLevelAssignment(service_level_id='strategic', enterprise_id='buyer_tenant',
                    effective_at=self.clock - timedelta(days=1), expires_at=self.clock),
                self.m.ServiceLevelAssignment(service_level_id='enterprise', enterprise_id='buyer_tenant',
                    effective_at=self.clock + timedelta(days=1)),
                self.m.ServiceLevelAssignment(service_level_id='strategic', enterprise_id='buyer_tenant',
                    user_id='buyer', effective_at=self.clock - timedelta(days=1)),
            ])
            db.commit()
        result = self.snapshot()
        self.assertEqual(result['service_level_code'], 'standard')
        self.assertIsNone(result['assignment'])

    def test_snapshot_does_not_commit_and_uses_pending_same_transaction_assignment(self):
        self.assertIs(self.m.freeze_sla_snapshot, self.m.enterprise_sla['freeze_sla_snapshot'])
        with self.m.SessionLocal() as db:
            body = self.m.ServiceLevelAssignmentBody(enterprise_id='buyer_tenant',
                service_level_id='strategic', reason='Pending transaction')
            user = db.get(self.m.User, 'ops')
            with patch.object(db, 'commit', side_effect=AssertionError('Hook must not commit')):
                self.m.enterprise_sla['adjust_assignment'](db, user, body)
                snapshot = self.m.freeze_sla_snapshot(db, 'buyer_tenant')
                self.assertEqual(snapshot['service_level_code'], 'strategic')
            db.rollback()
        self.assertEqual(self.snapshot()['service_level_code'], 'standard')

    def test_snapshot_requires_verified_existing_enterprise(self):
        with self.m.SessionLocal() as db:
            with self.assertRaises(self.m.HTTPException) as error:
                self.m.freeze_sla_snapshot(db, 'missing')
            self.assertEqual(error.exception.status_code, 404)
        self.pending()
        with self.m.SessionLocal() as db:
            with self.assertRaises(self.m.HTTPException) as error:
                self.m.freeze_sla_snapshot(db, 'buyer_tenant')
            self.assertEqual(error.exception.status_code, 409)

    def test_old_session_refreshes_assignment_and_level_after_enterprise_lock(self):
        assignment_id = self.adjust()['item']['id']
        m = self.m
        with m.SessionLocal() as reader:
            cached_enterprise = reader.get(m.Enterprise, 'buyer_tenant')
            cached_assignment = reader.get(m.ServiceLevelAssignment, assignment_id)
            cached_level = reader.get(m.ServiceLevel, 'strategic')
            with m.SessionLocal() as writer:
                writer.get(m.ServiceLevelAssignment, assignment_id).service_level_id = 'strategic'
                writer.get(m.ServiceLevel, 'strategic').description = 'Committed in another session'
                writer.commit()
            self.assertEqual(cached_assignment.service_level_id, 'enterprise')
            self.assertEqual(cached_level.description, '')
            result = m.freeze_sla_snapshot(reader, 'buyer_tenant')
            self.assertEqual(result['service_level_code'], 'strategic')
            self.assertEqual(result['legacy_level_config']['description'], 'Committed in another session')
            self.assertEqual(cached_assignment.service_level_id, 'strategic')
            self.assertEqual(cached_enterprise.verification_status, 'verified')

    def test_old_session_refreshes_enterprise_verification_and_disabled_level_before_adjustment(self):
        m = self.m
        with m.SessionLocal() as reader:
            cached_enterprise = reader.get(m.Enterprise, 'buyer_tenant')
            cached_level = reader.get(m.ServiceLevel, 'enterprise')
            user = reader.get(m.User, 'ops')
            body = m.ServiceLevelAssignmentBody(enterprise_id='buyer_tenant',
                service_level_id='enterprise', reason='Latest committed configuration')
            with m.SessionLocal() as writer:
                writer.get(m.ServiceLevel, 'enterprise').status = 'disabled'
                writer.commit()
            self.assertEqual(cached_level.status, 'active')
            with self.assertRaises(m.HTTPException) as error:
                m.enterprise_sla['adjust_assignment'](reader, user, body)
            self.assertEqual(error.exception.status_code, 404)
            self.assertEqual(cached_level.status, 'disabled')
            with m.SessionLocal() as writer:
                writer.get(m.Enterprise, 'buyer_tenant').verification_status = 'pending_review'
                writer.commit()
            self.assertEqual(cached_enterprise.verification_status, 'verified')
            with self.assertRaises(m.HTTPException) as error:
                m.enterprise_sla['adjust_assignment'](reader, user, body)
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(cached_enterprise.verification_status, 'pending_review')

    def test_custom_response_values_are_frozen_without_overwriting_old_snapshot(self):
        self.adjust(level='strategic')
        old = json.dumps(self.snapshot(), sort_keys=True)
        with self.m.SessionLocal() as db:
            level = db.get(self.m.ServiceLevel, 'strategic')
            level.initial_response_minutes = 777
            level.urgent_initial_response_minutes = 333
            db.commit()
        snapshot = self.snapshot()
        self.assertEqual(snapshot['metrics']['first_response']['target'], 777)
        self.assertEqual(snapshot['metrics']['urgent_first_response']['target'], 333)
        self.assertEqual(json.loads(old)['metrics']['first_response']['target'], 720)

    def test_exact_seed_migration_dry_run_apply_audit_and_idempotence(self):
        from sla_first_response_migration import migrate
        module = importlib.import_module(self.m.__package__ + '.enterprise_sla')
        order_id = fixture.BusinessRoutesHTTPTests.create_order(self)
        historical = '{ "service_level_code": "strategic", "response_minutes": 15 }'
        with self.m.SessionLocal() as db:
            db.add(self.m.AfterSalesTicket(id='old-promise', ticket_no='OLD-PROMISE',
                order_id=order_id, type='issue', description='Historical promise',
                service_level_code='strategic', sla_snapshot_json=historical))
            level = db.get(self.m.ServiceLevel, 'strategic')
            values = dict(name='战略级', description=module.LEGACY_STRATEGIC_DESCRIPTION,
                customer_scope='strategic', support_days_per_week=7, support_hours_per_day=24,
                initial_response_minutes=15, urgent_initial_response_minutes=None,
                problem_response_hours=24, created_by='system', status='active',
                online_docs=True, knowledge_base=True, standard_api=True,
                online_customer_service=True, dedicated_manager=True, technical_support=True,
                quarterly_report=True, annual_optimization=True)
            for key, value in values.items():
                setattr(level, key, value)
            db.commit()
            self.assertTrue(module.is_legacy_strategic_seed(level))
            report = migrate(db, vars(self.m))
            self.assertEqual(report['matched_ids'], ['strategic'])
            self.assertEqual(level.initial_response_minutes, 15)
            self.assertEqual(db.query(self.m.AuditLog).filter_by(action='migrate_strategic_first_response').count(), 0)
            report = migrate(db, vars(self.m), apply=True, actor='platform')
            db.commit()
            self.assertEqual(report['snapshot_hash_before'], report['snapshot_hash_after'])
            self.assertEqual(report['snapshot_count'], 1)
            self.assertEqual(db.get(self.m.AfterSalesTicket, 'old-promise').sla_snapshot_json, historical)
            self.assertEqual((level.initial_response_minutes, level.urgent_initial_response_minutes), (720, 360))
            self.assertEqual(db.query(self.m.AuditLog).filter_by(action='migrate_strategic_first_response').count(), 1)
            audit = db.query(self.m.AuditLog).filter_by(action='migrate_strategic_first_response').one()
            audited = json.loads(audit.after_json)
            self.assertEqual(audited['snapshot_hash_before'], report['snapshot_hash_before'])
            self.assertEqual(audited['snapshot_hash_after'], report['snapshot_hash_after'])
            self.assertEqual(migrate(db, vars(self.m), apply=True, actor='platform')['matched_ids'], [])
            for key, value in values.items():
                setattr(level, key, value)
            level.initial_response_minutes = 16
            db.flush()
            self.assertEqual(migrate(db, vars(self.m), apply=True, actor='platform')['matched_ids'], [])
            self.assertEqual(level.initial_response_minutes, 16)

    def test_service_level_body_defaults_and_custom_values(self):
        body = self.m.ServiceLevelBody(code='strategic', name='Strategic')
        self.assertEqual((body.initial_response_minutes, body.urgent_initial_response_minutes), (720, 360))
        self.assertNotIn('urgent_initial_response_minutes', body.model_fields_set)
        custom = self.m.ServiceLevelBody(code='strategic', name='Custom',
            initial_response_minutes=777, urgent_initial_response_minutes=333)
        self.assertEqual((custom.initial_response_minutes, custom.urgent_initial_response_minutes), (777, 333))

    def test_legacy_update_omitting_urgent_preserves_custom_value_and_api_distinguishes_clocks(self):
        with self.m.SessionLocal() as db:
            level = db.get(self.m.ServiceLevel, 'strategic')
            level.urgent_initial_response_minutes = 333
            db.commit()
        response = self.request('PATCH', '/api/sla/service-levels/strategic', actor='platform',
            json={'code': 'strategic', 'name': 'strategic', 'initial_response_minutes': 720})
        self.assertEqual(response['item']['initial_response_minutes'], 720)
        self.assertEqual(response['item']['urgent_initial_response_minutes'], 333)
        listed = self.request('GET', '/api/sla/service-levels', actor='platform')
        item = next(row for row in listed['items'] if row['code'] == 'strategic')
        self.assertEqual((item['initial_response_minutes'], item['urgent_initial_response_minutes']), (720, 333))
        self.assertEqual((item['first_response_clock'], item['urgent_first_response_clock']), ('support', 'natural'))
        with self.m.SessionLocal() as db:
            self.assertEqual(db.get(self.m.ServiceLevel, 'strategic').urgent_initial_response_minutes, 333)

    def test_snapshot_metrics_match_final_matrix_and_do_not_reinterpret_legacy_units(self):
        path = Path(__file__).resolve().parents[2] / 'docs' / 'SLA级别指标配置矩阵-提前预警修正版-20261014.csv'
        with path.open(encoding='utf-8-sig', newline='') as stream:
            matrix = {row['指标编号']: row for row in csv.DictReader(stream)}
        keys = ('first_response', 'urgent_first_response', 'resolution', 'urgent_resolution',
                'rectification', 'intervention_response', 'urgent_intervention_response',
                'intervention_review', 'notification', 'availability', 'request_error_rate',
                'latency_p95', 'delivery_timeliness', 'delivery')
        for code, prefix in (('standard', 'STD'), ('enterprise', 'ENT'), ('strategic', 'STR')):
            self.adjust(level=code)
            snapshot = self.snapshot()
            for number, key in enumerate(keys, start=3):
                row = matrix[f'{prefix}-{number:02}']
                metric = snapshot['metrics'][key]
                self.assertEqual(metric['target'], float(row['填写目标值']))
                self.assertEqual(metric['warning'], float(row['填写预警阈值']))
            self.assertEqual(snapshot['metrics']['intervention_review']['pause_states'], [])
            self.assertEqual(snapshot['metrics']['first_response']['pause_states'], [])
            calendar = snapshot['calendar']
            self.assertEqual(calendar['version'], snapshot['matrix_version'])
            self.assertEqual(calendar['weekday_numbering'], 'python')
            self.assertEqual(calendar['weekdays'], list(range(5 if code == 'standard' else 7)))
            self.assertEqual(calendar['windows'],
                [['00:00', '24:00']] if code == 'strategic' else [['09:00', '17:00']])
            for field in ('holidays', 'workdays', 'covered_years'):
                self.assertEqual(calendar[field], [])
            self.assertIsNone(calendar['holiday_calendar_version'])
            self.assertEqual(calendar['holiday_calendar_status'],
                'not_required' if code == 'strategic' and hasattr(self.m, 'holiday_calendar') else 'unconfigured')
        self.assertEqual(snapshot['metrics']['urgent_first_response']['target'], 360)
        self.assertEqual(snapshot['metrics']['delivery']['warning_seconds'], 192)
        self.assertEqual(snapshot['legacy_level_config']['initial_response_minutes'], 720)
        self.assertEqual(snapshot['legacy_level_config']['problem_response_hours'], 48)
        self.assertEqual(snapshot['calendar']['timezone'], 'Asia/Shanghai')


if __name__ == '__main__':
    unittest.main()
