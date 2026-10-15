"""Local SQLite proofs; no real application, network, or production access."""
from types import SimpleNamespace
from pathlib import Path
import importlib
import sys
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import Column, ForeignKey, String, create_engine, event, func, select
from sqlalchemy.orm import declarative_base, sessionmaker

import r3_native_refund_cleanup as cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        Base = declarative_base()
        def model(class_name, table, **fields):
            return type(class_name, (Base,), {'__tablename__': table,
                'id': Column(String, primary_key=True), **fields})
        def text():
            return Column(String, default='')
        def fk(table):
            return Column(String, ForeignKey(table + '.id'))
        m = self.m = SimpleNamespace(Base=Base)
        m.User = model('User', 'users', name=text(), email=text())
        m.Enterprise = model('Enterprise', 'enterprises', name=text(), credit_code=text())
        m.Product = model('Product', 'products', name=text(), enterprise_id=fk('enterprises'))
        m.ProductReleaseVersion = model('Version', 'product_release_versions', product_id=fk('products'))
        m.SaaSProductVersion = model('SaaS', 'saas_product_versions', product_id=fk('products'))
        m.Membership = model('Member', 'memberships', user_id=fk('users'), enterprise_id=fk('enterprises'))
        m.Order = model('Order', 'orders', order_no=text(), product_id=fk('products'),
            product_version_id=fk('product_release_versions'), buyer_user_id=fk('users'),
            buyer_enterprise_id=fk('enterprises'), provider_enterprise_id=fk('enterprises'))
        m.Payment = model('Payment', 'payments', order_id=fk('orders'))
        m.Refund = model('Refund', 'refunds', order_id=fk('orders'), payment_id=fk('payments'))
        m.AuditLog = model('Audit', 'audit_logs', actor=text(), action=text(), category=text(), business_domain=text(),
            target_type=text(), target_id=text(), order_id=text(), tenant_id=text(), request_id=text())
        Message = model('Message', 'notification_messages', target_type=text(), target_id=text(),
            category=text(), tenant_id=text(), event_key=text())
        Receipt = model('Receipt', 'notification_recipients', message_id=fk('notification_messages'), user_id=fk('users'), legacy_id=text())
        Expired = model('Expired', 'notification_expired_urgent', message_id=fk('notification_messages'), user_id=fk('users'))
        m.message_center = dict(Message=Message, Receipt=Receipt, ExpiredUrgent=Expired)
        m.PlatformNotification = model('Notification', 'platform_notifications', target_type=text(),
            target_id=text(), recipient_user_id=fk('users'))
        self.Event = model('Event', 'ordinary_refund_events', refund_id=fk('refunds'), operation_key=text())
        self.engine = create_engine('sqlite://')
        @event.listens_for(self.engine, 'connect')
        def foreign_keys(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
        Base.metadata.create_all(self.engine)
        m.SessionLocal = sessionmaker(self.engine)
        self.manifest = cleanup.base.fixture_ids('qa-vcs-123456abcdef')
        self.manifest.update(authorized=True, environment='local')
        ids = cleanup.scope(self.manifest)
        with m.SessionLocal.begin() as db:
            db.add_all([m.User(id=u, name=u, email=u + '@example.invalid') for u in ids['users'].values()])
            db.add_all([m.Enterprise(id=e, name=e, credit_code=e) for e in ids['enterprises']])
            db.flush()
            db.add_all([m.Product(id=p, name=p, enterprise_id=ids['enterprises'][1]) for p in ids['products'].values()])
            db.flush()
            db.add_all([m.ProductReleaseVersion(id=v, product_id=ids['products'][k])
                       for k, group in ids['versions'].items() for v in group.values()])
            db.flush()
            db.add(m.Order(id='order', order_no='order', product_id=ids['products'][0],
                product_version_id=ids['versions'][0]['source'], buyer_user_id=ids['users']['buyer'],
                buyer_enterprise_id=ids['enterprises'][0], provider_enterprise_id=ids['enterprises'][1]))
            db.flush()
            db.add(m.Payment(id='payment', order_id='order'))
            db.flush()
            db.add(m.Refund(id='refund', order_id='order', payment_id='payment'))
            db.add(m.Order(id=cleanup.PROTECTED, order_no=cleanup.PROTECTED))
            db.flush()
            db.add(self.Event(id='event', refund_id='refund', operation_key='event-key'))

    def tearDown(self):
        self.engine.dispose()

    def run_cleanup(self, **kwargs):
        kwargs.setdefault('dry_run', True)
        with patch.object(cleanup.base, 'environment_fingerprint', return_value='local'):
            return cleanup.cleanup(self.m, self.manifest, **kwargs)

    def test_dryrun_and_exact_delete(self):
        before = self.run_cleanup()
        self.assertEqual(before['cleanup_rows']['refunds'], [['refund']])
        journal = []
        def record(keys):
            with self.m.SessionLocal() as db:
                self.assertIsNotNone(db.get(self.m.Refund, 'refund'))
            journal.append(keys)
        self.assertEqual(self.run_cleanup(dry_run=False, journal_callback=record), before['tablecounts'])
        self.assertEqual(journal, [before['cleanup_rows']])
        with self.m.SessionLocal() as db:
            self.assertIsNotNone(db.get(self.m.Order, cleanup.PROTECTED))
            self.assertEqual(db.scalar(select(func.count()).select_from(self.m.Refund)), 0)

    def test_foreign_root_refused(self):
        with self.m.SessionLocal.begin() as db:
            db.get(self.m.User, self.manifest['users']['buyer']).email = 'foreign'
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^root_ownership_refused$'):
            self.run_cleanup()

    def test_foreign_nonroot_fk_refused(self):
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.Payment(id='external-payment'))
            db.flush()
            db.get(self.m.Refund, 'refund').payment_id = 'external-payment'
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^external_fk_parent$'):
            self.run_cleanup()

    def test_unknown_child_refused(self):
        from sqlalchemy import Table
        unknown = Table('api_financial_charge_cursors', self.m.Base.metadata,
            Column('id', String, primary_key=True), Column('order_id', ForeignKey('orders.id')))
        unknown.create(self.engine)
        with self.engine.begin() as db:
            db.execute(unknown.insert().values(id='cursor', order_id='order'))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^unknown_owned_table:api_financial_charge_cursors$'):
            self.run_cleanup()

    def test_journal_failure_rolls_back(self):
        def fail(_):
            raise RuntimeError('journal_failed')
        with self.assertRaisesRegex(RuntimeError, 'journal_failed'):
            self.run_cleanup(dry_run=False, journal_callback=fail)
        self.assertEqual(self.run_cleanup()['cleanup_rows']['refunds'], [['refund']])

    def test_row_bound(self):
        with self.m.SessionLocal() as db:
            with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^row_bound_exceeded$'):
                cleanup.validate(self.m, db, self.manifest,
                    {'refunds': {(str(i),) for i in range(2001)}})

    def test_metadata_preflight_does_not_approve_unknown(self):
        inventory = cleanup.preflight_metadata(self.m)
        self.assertIn('refunds', inventory['expected_present'])
        self.assertNotIn('holiday_calendar_versions', cleanup.ALLOWED)

    def default_assignment_fixture(self):
        from sqlalchemy import Table
        level = Table('service_levels', self.m.Base.metadata,
            Column('id', String, primary_key=True), Column('code', String))
        assignment = Table('service_level_assignments', self.m.Base.metadata,
            Column('id', String, primary_key=True), Column('enterprise_id', ForeignKey('enterprises.id')),
            Column('service_level_id', ForeignKey('service_levels.id')), Column('source', String),
            Column('created_by', String), Column('user_id', String))
        level.create(self.engine)
        assignment.create(self.engine)
        with self.engine.begin() as db:
            db.execute(level.insert().values(id='global-standard', code='standard'))
            db.execute(assignment.insert().values(id='default-assignment',
                enterprise_id=self.manifest['enterprises']['buyer'], service_level_id='global-standard',
                source='default', created_by='system', user_id=''))
        return level, assignment

    def test_exact_default_assignment_deleted_catalog_retained(self):
        level, _ = self.default_assignment_fixture()
        before = self.run_cleanup()
        self.assertNotIn('service_levels', before['cleanup_rows'])
        result = self.run_cleanup(dry_run=False, journal_callback=lambda _: None)
        self.assertEqual(result['service_level_assignments'], 1)
        with self.engine.connect() as db:
            self.assertEqual([dict(r) for r in db.execute(select(level)).mappings()],
                             [{'id': 'global-standard', 'code': 'standard'}])

    def test_default_assignment_other_source_actor_user_refused(self):
        _, assignment = self.default_assignment_fixture()
        for field, value in dict(source='manual', created_by='startup', user_id=self.manifest['users']['buyer']).items():
            with self.subTest(field=field):
                with self.engine.begin() as db:
                    db.execute(assignment.update().values(source='default', created_by='system', user_id=''))
                    db.execute(assignment.update().values(**{field: value}))
                with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^default_assignment_mismatch$'):
                    self.run_cleanup()

    def test_default_assignment_nonstandard_catalog_refused(self):
        level, _ = self.default_assignment_fixture()
        with self.engine.begin() as db:
            db.execute(level.update().values(code='enterprise'))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^default_assignment_catalog_mismatch$'):
            self.run_cleanup()

    def test_default_assignment_other_external_fk_refused(self):
        from sqlalchemy import Table
        _, assignment = self.default_assignment_fixture()
        table = Table('external_catalog', self.m.Base.metadata, Column('id', String, primary_key=True))
        table.create(self.engine)
        assignment.append_column(Column('extra_id', ForeignKey('external_catalog.id')))
        with self.engine.begin() as db:
            db.exec_driver_sql('ALTER TABLE service_level_assignments ADD COLUMN extra_id VARCHAR REFERENCES external_catalog(id)')
            db.execute(table.insert().values(id='outside'))
            db.execute(assignment.update().values(extra_id='outside'))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^external_fk_parent$'):
            self.run_cleanup()

    def test_postgres_lock_timeout_precedes_locks(self):
        db = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name='postgresql')),
            execute=Mock(side_effect=RuntimeError('stop_before_lock')))
        with self.assertRaisesRegex(RuntimeError, '^stop_before_lock$'):
            cleanup.collect(self.m, db, self.manifest, lock=True)
        self.assertEqual(str(db.execute.call_args.args[0]), "SET LOCAL lock_timeout = '10s'")
        db.execute.assert_called_once()

    def test_terminal_event_count_guard(self):
        from sqlalchemy import Table
        table = Table('sla_gateway_terminal_events', self.m.Base.metadata,
            Column('event_id', String, primary_key=True), Column('product_id', String))
        table.create(self.engine)
        with self.engine.begin() as db:
            db.execute(table.insert().values(event_id='sealed', product_id=self.manifest['products']['api']))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^owned_sla_terminal_events_present$'):
            self.run_cleanup()

    def retained_fixture(self, *, db_route=False, with_fk=False):
        from sqlalchemy import Table, Integer
        route = type('Route', (self.m.Base,), {'__tablename__': 'api_gateway_routes',
            'id': Column(String, primary_key=True), 'product_id': Column(ForeignKey('products.id')),
            'route_key': Column(String)})
        self.m.ApiGatewayRoute = route
        route.__table__.create(self.engine)
        fields = [Column(k, String, primary_key=(k == 'event_id')) for k in
                  ('event_id', 'content_sha256', 'product_id', 'version_id', 'route_id')]
        if with_fk:
            fields.append(Column('linked_product', ForeignKey('products.id')))
        terminal = Table('sla_gateway_terminal_events', self.m.Base.metadata, *fields)
        windows = Table('sla_gateway_source_windows', self.m.Base.metadata,
            Column('producer_id', String, primary_key=True), Column('window_slot', Integer, primary_key=True),
            Column('received_count', Integer), Column('expected_count', Integer), Column('seal_sha256', String))
        terminal.create(self.engine)
        windows.create(self.engine)
        fact = dict(event_id='event-retained', content_sha256='a' * 64,
            product_id=self.manifest['products']['api'], version_id=self.manifest['versions']['api']['source'],
            route_id='route-owned' if db_route else self.manifest['routes']['api']['source'])
        with self.m.SessionLocal.begin() as db:
            db.add(route(id='route-owned', product_id=fact['product_id'],
                route_key=self.manifest['routes']['api']['source']))
            db.execute(terminal.insert().values(**fact))
            db.execute(windows.insert().values(producer_id='global', window_slot=1,
                received_count=42, expected_count=42, seal_sha256='b' * 64))
        self.manifest['database_ids']['native_refund'] = {'retained_sla_facts': [dict(fact)]}
        return terminal, windows, fact

    def test_retained_facts_and_global_window_survive_cleanup(self):
        terminal, windows, fact = self.retained_fixture()
        before = self.run_cleanup()
        self.assertNotIn(terminal.name, before['cleanup_rows'])
        self.assertNotIn(windows.name, before['cleanup_rows'])
        self.run_cleanup(dry_run=False, journal_callback=lambda _: None)
        with self.engine.connect() as db:
            self.assertEqual(dict(db.execute(select(terminal)).mappings().one()), fact)
            window = db.execute(select(windows)).mappings().one()
            self.assertEqual((window['received_count'], window['expected_count'], window['seal_sha256']),
                             (42, 42, 'b' * 64))
        self.assertEqual(self.run_cleanup()['cleanup_rows'], {})

    def test_retained_db_route_id_survives_cleanup(self):
        terminal, _, fact = self.retained_fixture(db_route=True)
        self.run_cleanup(dry_run=False, journal_callback=lambda _: None)
        with self.engine.connect() as db:
            self.assertEqual(dict(db.execute(select(terminal)).mappings().one()), fact)

    def test_retained_foreign_and_tampered_journal_refused(self):
        self.retained_fixture()
        journal = self.manifest['database_ids']['native_refund']['retained_sla_facts']
        original = dict(journal[0])
        for field, value in (('event_id', 'stranger'), ('content_sha256', 'c' * 64),
                             ('product_id', 'foreign-product'),
                             ('version_id', self.manifest['versions']['model']['source']),
                             ('route_id', self.manifest['routes']['api']['target'])):
            with self.subTest(field=field):
                journal[0] = {**original, field: value}
                with self.assertRaises(cleanup.guarded.AcceptanceFailure):
                    self.run_cleanup()

    def test_retained_extra_actual_fact_refused(self):
        terminal, _, fact = self.retained_fixture()
        with self.engine.begin() as db:
            db.execute(terminal.insert().values(**{**fact, 'event_id': 'unjournaled'}))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^retained_sla_set_mismatch$'):
            self.run_cleanup()

    def test_retained_actual_tamper_refused(self):
        terminal, _, _ = self.retained_fixture()
        with self.engine.begin() as db:
            db.execute(terminal.update().values(content_sha256='d' * 64))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^retained_sla_set_mismatch$'):
            self.run_cleanup()

    def test_retained_with_fk_refused(self):
        self.retained_fixture(with_fk=True)
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^retained_sla_has_fk$'):
            self.run_cleanup()

    def test_health_foreign_version_refused(self):
        from sqlalchemy import Table
        table = Table('application_health_slots', self.m.Base.metadata,
            Column('id', String, primary_key=True), Column('product_id', ForeignKey('products.id')),
            Column('version_id', String))
        table.create(self.engine)
        with self.engine.begin() as db:
            db.execute(table.insert().values(id='slot', product_id=self.manifest['products']['api'],
                version_id=self.manifest['versions']['model']['source']))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^health_version_mismatch$'):
            self.run_cleanup()

    def assessment_fixture(self):
        from sqlalchemy import Table
        metadata = self.m.Base.metadata
        contract = Table('order_sla_contracts', metadata, Column('id', String, primary_key=True),
            Column('order_id', ForeignKey('orders.id')), Column('source_payment_id', ForeignKey('payments.id')),
            Column('source_delivery_task_id', String))
        assessment = Table('sla_online_assessments', metadata, Column('id', String, primary_key=True),
            Column('order_id', ForeignKey('orders.id')), Column('contract_id', ForeignKey('order_sla_contracts.id')))
        evidence = Table('sla_assessment_evidence', metadata, Column('id', String, primary_key=True),
            Column('order_id', ForeignKey('orders.id')), Column('assessment_id', ForeignKey('sla_online_assessments.id')))
        for table in (contract, assessment, evidence):
            table.create(self.engine)
        with self.engine.begin() as db:
            db.execute(contract.insert().values(id='contract', order_id='order', source_payment_id='payment'))
            db.execute(assessment.insert().values(id='assessment', order_id='order', contract_id='contract'))
            db.execute(evidence.insert().values(id='evidence', order_id='order', assessment_id='assessment'))
        return contract, assessment, evidence

    def test_owned_assessment_and_evidence_deleted(self):
        tables = self.assessment_fixture()
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.AuditLog(id='sla-audit', actor='system', action='evaluate_order_sla',
                target_type='sla_online_assessment', target_id='assessment', category='sla',
                business_domain='sla', order_id='order'))
        result = self.run_cleanup(dry_run=False, journal_callback=lambda _: None)
        for table in tables:
            self.assertEqual(result[table.name], 1)
        with self.engine.connect() as db:
            for table in tables:
                self.assertEqual(db.scalar(select(func.count()).select_from(table)), 0)

    def test_assessment_null_contract_refused(self):
        _, assessment, _ = self.assessment_fixture()
        with self.engine.begin() as db:
            db.execute(assessment.update().values(contract_id=None))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^sla_assessment_contract_mismatch$'):
            self.run_cleanup()

    def test_exact_sla_monitor_audit_deleted(self):
        self.assessment_fixture()
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.AuditLog(id='monitor-audit', actor='system:sla-online-monitor',
                action='evaluate_order_sla', target_type='sla_online_assessment', target_id='assessment',
                category='sla', business_domain='sla', order_id='order'))
        result = self.run_cleanup(dry_run=False, journal_callback=lambda _: None)
        self.assertEqual(result['audit_logs'], 1)

    def test_sla_monitor_audit_other_actor_or_fields_refused(self):
        self.assessment_fixture()
        original = dict(id='monitor-audit', actor='system:sla-online-monitor', action='evaluate_order_sla',
            target_type='sla_online_assessment', target_id='assessment', category='sla', business_domain='sla',
            order_id='order', request_id=self.manifest['request_id'])
        for field, value in dict(actor='system:sla-online-monitor-other', action='other_action',
                category='other', business_domain='other', order_id='').items():
            with self.subTest(field=field):
                with self.m.SessionLocal.begin() as db:
                    db.add(self.m.AuditLog(**{**original, field: value}))
                with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^audit_actor_mismatch$'):
                    self.run_cleanup()
                with self.m.SessionLocal.begin() as db:
                    db.execute(cleanup.delete(self.m.AuditLog.__table__))

    def test_assessment_foreign_contract_refused(self):
        contract, assessment, _ = self.assessment_fixture()
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.Payment(id='external-payment'))
            db.flush()
            db.execute(contract.insert().values(id='external-contract', order_id=cleanup.PROTECTED,
                source_payment_id='external-payment'))
            db.execute(assessment.update().values(contract_id='external-contract'))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^sla_assessment_contract_mismatch$'):
            self.run_cleanup()

    def test_evidence_foreign_assessment_refused(self):
        _, assessment, evidence = self.assessment_fixture()
        with self.engine.begin() as db:
            db.execute(assessment.insert().values(id='external-assessment', order_id=cleanup.PROTECTED))
            db.execute(evidence.update().values(assessment_id='external-assessment'))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^sla_evidence_assessment_mismatch$'):
            self.run_cleanup()

    def test_evidence_global_source_fk_refused(self):
        from sqlalchemy import Table
        _, _, evidence = self.assessment_fixture()
        source = Table('sla_gateway_source_windows', self.m.Base.metadata, Column('id', String, primary_key=True))
        source.create(self.engine)
        evidence.append_column(Column('source_window_id', ForeignKey('sla_gateway_source_windows.id')))
        with self.engine.begin() as db:
            db.exec_driver_sql('ALTER TABLE sla_assessment_evidence ADD COLUMN source_window_id VARCHAR REFERENCES sla_gateway_source_windows(id)')
            db.execute(source.insert().values(id='global-window'))
            db.execute(evidence.update().values(source_window_id='global-window'))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^external_fk_parent$'):
            self.run_cleanup()
        with self.engine.connect() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(source)), 1)

    def test_protected_order_cannot_join_roots(self):
        with self.m.SessionLocal.begin() as db:
            db.get(self.m.Order, 'order').order_no = cleanup.PROTECTED
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^protected_order_selected$'):
            self.run_cleanup()

    def test_refund_message_wrong_event_key(self):
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.message_center['Message'](id='message', target_type='refund', target_id='refund',
                tenant_id=self.manifest['enterprises']['buyer'], category='refund',
                event_key='ordinary-refund:foreign-event'))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^refund_event_key_mismatch$'):
            self.run_cleanup()

    def test_system_actor_wrong_action(self):
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.AuditLog(id='audit', target_type='refund', target_id='refund',
                actor='system', action='unrelated', order_id='order', category='refund'))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^audit_actor_mismatch$'):
            self.run_cleanup()

    def export_audit(self):
        return dict(id='export-audit', actor=self.manifest['users']['delivery'] + '@example.invalid',
            action='export_settlement_report', target_type='settlement_report', target_id='',
            category='settlement_report', business_domain='settlement', order_id='', tenant_id='',
            request_id=self.manifest['request_id'])

    def legacy_delivery_audit(self):
        return dict(id='legacy-delivery-audit', actor=self.manifest['users']['finance'] + '@example.invalid',
            action='create_delivery_task', target_type='delivery_task', target_id='', category='delivery',
            business_domain='delivery', order_id='order', tenant_id='', request_id=self.manifest['request_id'])

    def test_exact_legacy_delivery_audit_deleted(self):
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.AuditLog(**self.legacy_delivery_audit()))
        result = self.run_cleanup(dry_run=False, journal_callback=lambda _: None)
        self.assertEqual(result['audit_logs'], 1)
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.get(self.m.AuditLog, 'legacy-delivery-audit'))

    def test_legacy_delivery_each_constraint_required(self):
        changes = dict(actor='foreign@example.invalid', action='other_delivery', target_type='order',
            target_id='foreign-task', category='order', business_domain='other', order_id='')
        for field, value in changes.items():
            with self.subTest(field=field):
                with self.m.SessionLocal.begin() as db:
                    db.add(self.m.AuditLog(**{**self.legacy_delivery_audit(), field: value}))
                with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^audit_target_mismatch$'):
                    self.run_cleanup()
                with self.m.SessionLocal.begin() as db:
                    db.execute(cleanup.delete(self.m.AuditLog.__table__))

    def test_legacy_delivery_external_tenant_refused(self):
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.AuditLog(**{**self.legacy_delivery_audit(), 'tenant_id': 'external-tenant'}))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^audit_tenant_mismatch$'):
            self.run_cleanup()

    def test_exact_export_audit_deleted(self):
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.AuditLog(**self.export_audit()))
        self.assertEqual(self.run_cleanup()['cleanup_rows']['audit_logs'], [['export-audit']])
        self.run_cleanup(dry_run=False, journal_callback=lambda _: None)
        with self.m.SessionLocal() as db:
            self.assertIsNone(db.get(self.m.AuditLog, 'export-audit'))

    def test_export_audit_each_constraint_required(self):
        changes = dict(actor='foreign@example.invalid', action='other_export', target_type='report',
            target_id='order', category='settlement', business_domain='other', order_id='order',
            tenant_id=self.manifest['enterprises']['buyer'])
        for field, value in changes.items():
            with self.subTest(field=field):
                with self.m.SessionLocal.begin() as db:
                    db.add(self.m.AuditLog(**{**self.export_audit(), field: value}))
                with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^audit_target_mismatch$'):
                    self.run_cleanup()
                with self.m.SessionLocal.begin() as db:
                    db.execute(cleanup.delete(self.m.AuditLog.__table__))

    def test_export_audit_still_checks_every_fk(self):
        # Add a mapped FK to an external non-root row without changing the export fields.
        table = self.m.AuditLog.__table__
        column = Column('payment_id', ForeignKey('payments.id'))
        table.append_column(column)
        with self.engine.begin() as db:
            db.exec_driver_sql('ALTER TABLE audit_logs ADD COLUMN payment_id VARCHAR REFERENCES payments(id)')
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.Payment(id='external-payment'))
            db.flush()
            db.execute(table.insert().values(**self.export_audit(), payment_id='external-payment'))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^external_fk_parent$'):
            self.run_cleanup()

    def test_polymorphic_collision_refused(self):
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.AuditLog(id='audit', target_type='user', target_id='refund',
                actor=self.manifest['users']['buyer'] + '@example.invalid'))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^audit_target_mismatch$'):
            self.run_cleanup()

    def test_receipt_exception_requires_proven_message(self):
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.User(id='outside', name='outside', email='outside'))
            db.add(self.m.message_center['Message'](id='message', target_type='refund', target_id='refund',
                tenant_id=self.manifest['enterprises']['buyer'], category='refund',
                event_key='ordinary-refund:event-key'))
            db.flush()
            db.add(self.m.message_center['Receipt'](id='receipt', message_id='message', user_id='outside'))
        self.run_cleanup(dry_run=False, journal_callback=lambda _: None)
        with self.m.SessionLocal() as db:
            self.assertIsNotNone(db.get(self.m.User, 'outside'))

    def system_message_fixture(self):
        from sqlalchemy import Table, Integer
        metadata = self.m.Base.metadata
        route = Table('api_gateway_routes', metadata, Column('id', String, primary_key=True),
            Column('product_id', ForeignKey('products.id')), Column('route_key', String))
        self.m.ApiGatewayRoute = type('Route', (self.m.Base,), {'__table__': route})
        credential = Table('api_credentials', metadata, Column('id', String, primary_key=True),
            Column('route_id', ForeignKey('api_gateway_routes.id')), Column('enterprise_id', ForeignKey('enterprises.id')))
        combo = Table('subscription_combinations', metadata, Column('id', String, primary_key=True),
            Column('product_id', ForeignKey('products.id')), Column('enterprise_id', ForeignKey('enterprises.id')),
            Column('generation', Integer), Column('total_months', Integer))
        term = Table('subscription_terms', metadata, Column('id', String, primary_key=True),
            Column('order_id', ForeignKey('orders.id')), Column('combination_id', ForeignKey('subscription_combinations.id')),
            Column('generation', Integer), Column('start_month', Integer), Column('months', Integer))
        delivery = Table('notification_deliveries', metadata, Column('id', String, primary_key=True),
            Column('message_id', ForeignKey('notification_messages.id')), Column('user_id', ForeignKey('users.id')))
        attempt = Table('notification_delivery_attempts', metadata, Column('id', String, primary_key=True),
            Column('delivery_id', ForeignKey('notification_deliveries.id')))
        for table in (route, credential, combo, term, delivery, attempt):
            table.create(self.engine)
        with self.engine.begin() as db:
            db.execute(route.insert().values(id='route', product_id=self.manifest['products']['api'],
                route_key=self.manifest['routes']['api']['source']))
            db.execute(credential.insert().values(id='credential', route_id='route',
                enterprise_id=self.manifest['enterprises']['buyer']))
            db.execute(combo.insert().values(id='combo', product_id=self.manifest['products']['api'],
                enterprise_id=self.manifest['enterprises']['buyer'], generation=1, total_months=1))
            db.execute(term.insert().values(id='term', order_id='order', combination_id='combo',
                generation=1, start_month=0, months=1))
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.message_center['Message'](id='quota-message', target_type='api_credential',
                target_id='credential', category='system', tenant_id=self.manifest['enterprises']['buyer'],
                event_key='quota:credential:month:combo:1:0:2'))
            db.add(self.m.message_center['Message'](id='health-message', target_type='product',
                target_id=self.manifest['products']['api'], category='system',
                tenant_id=self.manifest['enterprises']['provider'], event_key='application-health:' +
                self.manifest['products']['api'] + ':' + self.manifest['versions']['api']['target'] + ':incident:20261015155003'))
            db.flush()
            db.add(self.m.PlatformNotification(id='legacy', target_type='api_credential', target_id='credential',
                recipient_user_id=self.manifest['users']['buyer']))
            db.add(self.m.message_center['Receipt'](id='receipt', message_id='quota-message',
                user_id=self.manifest['users']['buyer'], legacy_id='legacy'))
            db.execute(delivery.insert().values(id='delivery', message_id='quota-message', user_id=self.manifest['users']['buyer']))
            db.execute(attempt.insert().values(id='attempt', delivery_id='delivery'))
        return delivery, attempt

    def test_system_notifications_receipt_delivery_attempt_deleted(self):
        self.system_message_fixture()
        result = self.run_cleanup(dry_run=False, journal_callback=lambda _: None)
        for table, count in dict(notification_messages=2, notification_recipients=1,
                platform_notifications=1, notification_deliveries=1, notification_delivery_attempts=1).items():
            self.assertEqual(result[table], count)

    def test_quota_message_bad_period_or_limit_refused(self):
        self.system_message_fixture()
        for key in ('quota:credential:month:combo:1:0:3', 'quota:credential:month:combo:2:0:2',
                    'quota:credential:month:combo:1:1:2', 'quota:credential:month:foreign:1:0:2'):
            with self.subTest(key=key):
                with self.m.SessionLocal.begin() as db:
                    db.get(self.m.message_center['Message'], 'quota-message').event_key = key
                with self.assertRaises(cleanup.guarded.AcceptanceFailure):
                    self.run_cleanup()

    def test_system_message_bad_tenant_or_health_key_refused(self):
        self.system_message_fixture()
        with self.m.SessionLocal.begin() as db:
            db.get(self.m.message_center['Message'], 'quota-message').tenant_id = self.manifest['enterprises']['provider']
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^quota_message_mismatch$'):
            self.run_cleanup()
        with self.m.SessionLocal.begin() as db:
            db.get(self.m.message_center['Message'], 'quota-message').tenant_id = self.manifest['enterprises']['buyer']
        prefix = 'application-health:' + self.manifest['products']['api'] + ':'
        for key in (prefix + self.manifest['versions']['model']['source'] + ':incident:20261015155003',
                    prefix + self.manifest['versions']['api']['source'] + ':other:20261015155003',
                    prefix + self.manifest['versions']['api']['source'] + ':incident:20261015'):
            with self.subTest(key=key):
                with self.m.SessionLocal.begin() as db:
                    db.get(self.m.message_center['Message'], 'health-message').event_key = key
                with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^health_message_mismatch$'):
                    self.run_cleanup()

    def test_legacy_system_target_without_receipt_proof_refused(self):
        self.system_message_fixture()
        with self.m.SessionLocal.begin() as db:
            db.get(self.m.message_center['Receipt'], 'receipt').legacy_id = ''
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^legacy_system_message_unproven$'):
            self.run_cleanup()

    def test_delivery_external_user_without_matching_receipt_refused(self):
        delivery, _ = self.system_message_fixture()
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.User(id='external-user', name='external-user', email='external-user'))
            db.flush()
            db.execute(delivery.update().values(user_id='external-user'))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^delivery_receipt_unproven$'):
            self.run_cleanup()

    def test_bidirectional_inbox_cleanup_preserves_global_parents(self):
        delivery, attempt = self.system_message_fixture()
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.User(id='external-user', name='External', email='external@example.invalid'))
            db.add(self.m.message_center['Message'](id='global-message', target_type='system', target_id='global'))
            db.flush()
            for key, message, user in (
                    ('owned-message', 'quota-message', 'external-user'),
                    ('owned-user', 'global-message', self.manifest['users']['buyer']),
                    ('unrelated', 'global-message', 'external-user')):
                db.add(self.m.message_center['Receipt'](id=key, message_id=message, user_id=user))
                db.execute(delivery.insert().values(id=key, message_id=message, user_id=user))
                db.execute(attempt.insert().values(id=key, delivery_id=key))
        before = self.run_cleanup()
        self.assertNotIn(['global-message'], before['cleanup_rows']['notification_messages'])
        self.assertNotIn(['external-user'], before['cleanup_rows']['users'])
        self.run_cleanup(dry_run=False, journal_callback=lambda _: None)
        with self.m.SessionLocal() as db:
            self.assertIsNotNone(db.get(self.m.User, 'external-user'))
            self.assertIsNotNone(db.get(self.m.message_center['Message'], 'global-message'))
            self.assertIsNotNone(db.get(self.m.message_center['Receipt'], 'unrelated'))
            self.assertEqual(list(db.scalars(select(delivery.c.id))), ['unrelated'])
            self.assertEqual(list(db.scalars(select(attempt.c.id))), ['unrelated'])

    def test_global_message_delivery_without_receipt_preserves_all_on_refusal(self):
        delivery, _ = self.system_message_fixture()
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.message_center['Message'](id='global-message', target_type='system', target_id='global'))
            db.flush()
            db.execute(delivery.insert().values(id='unproven', message_id='global-message',
                user_id=self.manifest['users']['buyer']))
        with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^delivery_receipt_unproven$'):
            self.run_cleanup(dry_run=False, journal_callback=lambda _: self.fail('must not journal'))
        with self.m.SessionLocal() as db:
            self.assertIsNotNone(db.get(self.m.Refund, 'refund'))
            self.assertIsNotNone(db.get(self.m.message_center['Message'], 'global-message'))

    def test_unanchored_receipt_explicitly_selected_refused(self):
        self.system_message_fixture()
        with self.m.SessionLocal.begin() as db:
            db.add(self.m.User(id='external-user', name='External', email='external@example.invalid'))
            db.add(self.m.message_center['Message'](id='global-message', target_type='system', target_id='global'))
            db.flush()
            db.add(self.m.message_center['Receipt'](id='unanchored', message_id='global-message', user_id='external-user'))
        with self.m.SessionLocal() as db:
            owned = cleanup.collect(self.m, db, self.manifest)
            owned['notification_recipients'].add(('unanchored',))
            with self.assertRaisesRegex(cleanup.guarded.AcceptanceFailure, '^receipt_anchor_unproven$'):
                cleanup.validate(self.m, db, self.manifest, owned)


class RealModelCleanupTests(unittest.TestCase):
    def test_base_prepare_with_real_gateway_and_sla_contract(self):
        tests = str(Path(__file__).resolve().parents[1] / 'backend' / 'tests')
        sys.path.insert(0, tests)
        try:
            from test_message_business_events import load_isolated_main
        finally:
            sys.path.remove(tests)
        m = load_isolated_main()
        self.addCleanup(lambda: [sys.modules.pop(n, None) for n in list(sys.modules)
                                if n == m.__package__ or n.startswith(m.__package__ + '.')])
        self.addCleanup(m.engine.dispose)
        @event.listens_for(m.engine, 'connect')
        def foreign_keys(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
        m.Base.metadata.create_all(m.engine)
        manifest = cleanup.base.fixture_ids('qa-vcs-abcdef123456')
        manifest['authorized'] = True
        manifest['environment'] = cleanup.base.environment_fingerprint(m)
        manifest['database_ids']['native_refund'] = {'marker': 'retained'}
        cleanup.base.prepare(m, manifest)
        with m.SessionLocal.begin() as db:
            db.add(m.ServiceLevel(id='global-standard', code='standard', name='Fixture standard', created_by='system'))
            db.flush()
            db.add(m.ServiceLevelAssignment(id='startup-default', service_level_id='global-standard',
                enterprise_id=manifest['enterprises']['buyer'], source='default', created_by='system', user_id=''))
            db.get(m.User, manifest['users']['delivery']).platform_role = 'super_admin'
            order = m.Order(id='native-order', order_no='native-order', buyer_name='fixture',
                product_name=manifest['products']['api'], product_id=manifest['products']['api'],
                product_version_id=manifest['versions']['api']['source'],
                buyer_user_id=manifest['users']['buyer'], buyer_enterprise_id=manifest['enterprises']['buyer'],
                provider_enterprise_id=manifest['enterprises']['provider'], amount=20, paid_amount=20,
                payment_status='paid', snapshot_version=1, delivery_method_snapshot='api',
                main_status='completed', delivery_status='accepted')
            db.add(order)
            db.flush()
            payment = m.Payment(id='native-payment', order_id=order.id, payment_no='native-payment',
                amount=20, status='paid', paid_at=m.now())
            task = m.DeliveryTask(id='native-task', order_id=order.id, method='api', status='completed')
            db.add_all([payment, task])
            db.flush()
            frozen = {'service_level_code': 'fixture', 'metrics': {'delivery': {
                'target_seconds': 3600, 'clock': 'natural', 'direction': 'upper'}}}
            with patch.object(m, 'freeze_sla_snapshot', return_value=frozen):
                m.order_sla_contracts['freeze_payment'](db, order, payment, previously_paid=False)
            m.order_sla_contracts['record_verified_delivery'](db, order, task)
            route = m.ApiGatewayRoute(id='native-route', product_id=order.product_id,
                route_key=manifest['routes']['api']['source'], upstream_url='http://fixture.invalid')
            db.add(route)
            db.flush()
            revision = m.GatewayConfigRevision(id='native-revision', product_id=order.product_id, route_id=route.id)
            db.add(revision)
            db.flush()
            record = m.GatewayPublishRecord(id='native-record', route_id=route.id, revision_id=revision.id)
            db.add(record)
            db.flush()
            db.add(m.version_gateway['Config'](id='native-config', product_id=order.product_id,
                version_id=order.product_version_id, route_id=route.id, revision_id=revision.id,
                publish_record_id=record.id, upstream_url=route.upstream_url, route_key=route.route_key,
                version_code='source', auth_mode='api_key', upstream_auth_mode='oauth2',
                upstream_scope='resource.invoke', timeout_ms=30000, strip_prefix=True,
                health_method='GET', health_path='/health', rate_limit_per_minute=60,
                daily_quota=10000, monthly_quota=100000))
            db.add(m.ApiCredential(id='native-credential', route_id=route.id,
                enterprise_id=order.buyer_enterprise_id, key_hash='fixture-only-hash',
                order_id=order.id, product_version_id=order.product_version_id))
            db.flush()
            db.add(m.ApiUsage(id='native-usage', route_id=route.id, credential_id='native-credential',
                enterprise_id=order.buyer_enterprise_id, method='GET'))
            tables = m.Base.metadata.tables
            db.execute(tables['application_health_slots'].insert().values(id='native-slot',
                product_id=order.product_id, version_id=order.product_version_id, slot=1, claimed_at=m.now()))
            db.execute(tables['application_health_observations'].insert().values(id='native-observation',
                product_id=order.product_id, version_id=order.product_version_id, last_slot=1,
                last_checked_at=m.now(), last_status='healthy', updated_at=m.now()))
        schema = importlib.import_module(m.__package__ + '.refund_schema')
        with m.SessionLocal.begin() as db:
            tables = m.Base.metadata.tables
            contract_id = db.scalar(select(tables['order_sla_contracts'].c.id))
            db.execute(tables['sla_online_assessments'].insert().values(id='native-assessment',
                order_id='native-order', contract_id=contract_id, period_start=m.now(), period_end=m.now(),
                calculated_at=m.now(), input_hash='a' * 64, metrics_json='{}', source_json='{}', status='met'))
            db.execute(tables['sla_assessment_evidence'].insert().values(id='native-evidence',
                order_id='native-order', assessment_id='native-assessment', version=1,
                input_hash='a' * 64, calculated_at=m.now(), context_json='{}', metrics_json='{}', sources_json='{}'))
            m.audit(db, 'system', 'evaluate_order_sla', 'sla_online_assessment', 'native-assessment',
                category='sla', business_domain='sla', order_id='native-order')
        with m.SessionLocal.begin() as db, patch.object(m, 'notify_business_event'):
            refund = m.refund_workflow['create'](db, db.get(m.User, manifest['users']['buyer']),
                'native-order', schema.RefundRequestBody(amount='1', reason='fixture request',
                    idempotency_key='native-request-001'), None)
            m.refund_workflow['review'](db, db.get(m.User, manifest['users']['delivery']), refund.id,
                schema.RefundReviewBody(decision='reject', reason='fixture review',
                    expected_version=1, idempotency_key='native-review-001'))
        with m.SessionLocal.begin() as db:
            Message, Receipt, Delivery = (m.message_center[k] for k in ('Message', 'Receipt', 'Outbox'))
            db.add(m.User(id='global-user', name='Global', email='global@example.invalid', password_hash='not-a-password'))
            db.add(Message(id='global-message', title='Global', event_key='global-event',
                target_type='system', target_id='global'))
            db.add(Message(id='owned-message', title='Fixture', category='order',
                tenant_id=manifest['enterprises']['buyer'], target_type='order', target_id='native-order',
                event_key='business:order:native-order:fixture:1'))
            db.flush()
            for key, message, user in (('incoming', 'global-message', manifest['users']['buyer']),
                                       ('outgoing', 'owned-message', 'global-user')):
                db.add(Receipt(id=key, message_id=message, user_id=user))
                db.add(Delivery(id=key, message_id=message, user_id=user, channel='email'))
                db.flush()
                db.execute(m.Base.metadata.tables['notification_delivery_attempts'].insert().values(
                    id=key, delivery_id=key, lease_token='fixture-' + key, attempt_no=1))
        with m.SessionLocal() as db:
            owned = cleanup.collect(m, db, manifest, lock=True)
        self.assertEqual(owned['version_gateway_configs'], {('native-config',)})
        self.assertEqual(len(owned['order_sla_contracts']), 1)
        inventory = cleanup.preflight_metadata(m)
        self.assertIn('holiday_calendar_versions', inventory['unapproved_present'])
        journal = []
        result = cleanup.cleanup(m, manifest, journal.append)
        self.assertEqual(result['version_gateway_configs'], 1)
        self.assertEqual(result['api_usage'], 1)
        self.assertEqual(result['application_health_slots'], 1)
        self.assertEqual(result['application_health_observations'], 1)
        self.assertEqual(result['ordinary_refund_events'], 2)
        self.assertEqual(result['refunds'], 1)
        self.assertEqual(result['sla_online_assessments'], 1)
        self.assertEqual(result['sla_assessment_evidence'], 1)
        self.assertEqual(result['service_level_assignments'], 1)
        self.assertEqual(result['notification_recipients'], 2)
        self.assertEqual(result['notification_deliveries'], 2)
        self.assertEqual(result['notification_delivery_attempts'], 2)
        self.assertEqual(len(journal), 1)
        self.assertEqual(manifest['database_ids']['native_refund'], {'marker': 'retained'})
        with m.SessionLocal() as db:
            self.assertEqual(cleanup.collect(m, db, manifest), {})
            self.assertEqual(db.get(m.ServiceLevel, 'global-standard').code, 'standard')
            self.assertEqual(db.get(m.User, 'global-user').email, 'global@example.invalid')
            self.assertEqual(db.get(m.message_center['Message'], 'global-message').event_key, 'global-event')


if __name__ == '__main__':
    unittest.main()
