import importlib.util
from pathlib import Path
import unittest
import secrets
import tempfile
import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, String, DateTime, Boolean, Text, select, func, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session


class InboxTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('message_center', Path(__file__).parents[1] / 'app' / 'message_center.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.module = module
        self.clock = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
        self.tempdir = tempfile.TemporaryDirectory(prefix='message-center-tests-')
        self.objects = {}
        self.scan_result = 'clean'
        class ObjectResponse(io.BytesIO):
            def stream(self, size):
                while chunk := self.read(size):
                    yield chunk
            def release_conn(self):
                pass
        objects = self.objects
        class ObjectStore:
            def bucket_exists(self, bucket):
                return True
            def make_bucket(self, bucket):
                pass
            def put_object(self, bucket, key, stream, length, **kwargs):
                objects[(bucket, key)] = stream.read(length)
            def get_object(self, bucket, key):
                return ObjectResponse(objects[(bucket, key)])
            def remove_object(self, bucket, key):
                objects.pop((bucket, key), None)
        class Base(DeclarativeBase):
            pass
        class User(Base):
            __tablename__ = 'users'
            id: Mapped[str] = mapped_column(String, primary_key=True)
            email: Mapped[str] = mapped_column(String, default='')
            phone: Mapped[str | None] = mapped_column(String, nullable=True)
            name: Mapped[str] = mapped_column(String, default='test user')
            platform_role: Mapped[str] = mapped_column(String, default='')
            is_active: Mapped[bool] = mapped_column(Boolean, default=True)
            activation_status: Mapped[str] = mapped_column(String, default='active')
            email_verified: Mapped[bool] = mapped_column(Boolean, default=True)
        class Enterprise(Base):
            __tablename__ = 'enterprises'
            id: Mapped[str] = mapped_column(String, primary_key=True)
            name: Mapped[str] = mapped_column(String)
        class Membership(Base):
            __tablename__ = 'memberships'
            id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: secrets.token_hex(16))
            user_id: Mapped[str] = mapped_column(String)
            enterprise_id: Mapped[str] = mapped_column(String)
            role: Mapped[str] = mapped_column(String, default='member')
            status: Mapped[str] = mapped_column(String, default='active')
        class SystemSetting(Base):
            __tablename__ = 'system_settings'
            id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: secrets.token_hex(16))
            setting_key: Mapped[str] = mapped_column(String, unique=True)
            setting_value: Mapped[str] = mapped_column(Text, default='')
            is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
            updated_by: Mapped[str] = mapped_column(String, default='')
            updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: self.clock)
        class Legacy(Base):
            __tablename__ = 'platform_notifications'
            id: Mapped[str] = mapped_column(String, primary_key=True)
            recipient_user_id: Mapped[str] = mapped_column(String)
            title: Mapped[str] = mapped_column(String)
            content: Mapped[str] = mapped_column(String)
            target_type: Mapped[str] = mapped_column(String, default='')
            target_id: Mapped[str] = mapped_column(String, default='')
            status: Mapped[str] = mapped_column(String, default='unread')
            created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
        self.engine = create_engine('sqlite:///' + str(Path(self.tempdir.name) / 'isolated.db'),
                                    connect_args={'check_same_thread': False, 'timeout': 15})
        @event.listens_for(self.engine, 'connect')
        def enable_foreign_keys(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
        self.actor = SimpleNamespace(id='a', email='a@example.invalid', phone=None, platform_role='')
        def db_session():
            with Session(self.engine) as db:
                yield db
        self.app = FastAPI()
        self.center = module.install(dict(Base=Base, app=self.app, now=lambda: self.clock,
            db_session=db_session, current_user=lambda: self.actor, audit=lambda *a, **kw: None,
            PlatformNotification=Legacy, User=User, Membership=Membership, SystemSetting=SystemSetting,
            Enterprise=Enterprise, Minio=lambda *args, **kwargs: ObjectStore(),
            MINIO_ENDPOINT='isolated.invalid', MINIO_ACCESS_KEY='fake', MINIO_SECRET_KEY='fake',
            MINIO_BUCKET='isolated', clamav_scan_stream=lambda *args: (self.scan_result, 'isolated'),
            SessionLocal=lambda: Session(self.engine), disable_background=True,
            NOTIFICATION_ATTACHMENT_DIR=str(Path(self.tempdir.name) / 'attachments')))
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            db.add_all([User(id='a', email='a@example.invalid'), User(id='b', email='b@example.invalid'),
                        User(id='c', email='c@example.invalid'),
                        User(id='platform', email='platform@example.invalid', platform_role='super_admin'),
                        Membership(user_id='a', enterprise_id='tenant-a', role='enterprise_admin'),
                        Membership(user_id='b', enterprise_id='tenant-a'),
                        Membership(user_id='c', enterprise_id='tenant-b'),
                        Enterprise(id='tenant-a', name='Enterprise A'),
                        Enterprise(id='tenant-b', name='Enterprise B')])
            db.commit()
        self.User, self.Membership, self.SystemSetting = User, Membership, SystemSetting
        self.legacy = Legacy
        self.client = TestClient(self.app)
        self.redis_patch = patch('redis.Redis.from_url')
        self.redis = self.redis_patch.start().return_value
        self.addCleanup(self.redis_patch.stop)
        for transport in ('SMTP', 'SMTP_SSL'):
            guard = patch('smtplib.' + transport, side_effect=AssertionError('Unmocked SMTP forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.tempdir.cleanup()

    def create(self, recipients=('a',), severity='important', event_key=None, **kwargs):
        with Session(self.engine) as db:
            message = self.center['create'](db, recipients, '测试通知', '正文', severity=severity, event_key=event_key, **kwargs)
            db.commit()
            return message.id

    def test_global_unread_not_page_count(self):
        for _ in range(105):
            self.create()
        d = self.client.get('/api/notifications?page_size=20').json()
        self.assertEqual((len(d['items']), d['total'], d['unread']), (20, 105, 105))

    def test_cross_user_and_soft_delete(self):
        self.create(('a', 'b'))
        rid = self.client.get('/api/notifications').json()['items'][0]['id']
        self.actor = SimpleNamespace(id='b', email='b@test', phone=None)
        self.assertEqual(self.client.post(f'/api/notifications/{rid}/read').status_code, 404)
        self.actor = SimpleNamespace(id='a', email='a@test', phone=None)
        self.assertEqual(self.client.delete(f'/api/notifications/{rid}').status_code, 200)
        self.assertEqual(self.client.get('/api/notifications').json()['total'], 0)
        self.actor = SimpleNamespace(id='b', email='b@test', phone=None)
        self.assertEqual(self.client.get('/api/notifications').json()['total'], 1)

    def test_urgent_read_is_not_acknowledgment(self):
        self.create(severity='urgent')
        rid = self.client.get('/api/notifications').json()['items'][0]['id']
        read = self.client.post(f'/api/notifications/{rid}/read').json()
        self.assertIsNotNone(read['first_read_at'])
        self.assertIsNone(read['acknowledged_at'])
        self.assertEqual(self.client.delete(f'/api/notifications/{rid}').status_code, 409)
        self.client.post(f'/api/notifications/{rid}/acknowledge')
        self.assertEqual(self.client.get('/api/notifications/summary').json()['unacknowledged_urgent'], 0)

    def test_batch_rollback_on_foreign_recipient(self):
        self.create(); self.create(('b',))
        mine = self.client.get('/api/notifications').json()['items'][0]['id']
        self.actor = SimpleNamespace(id='b', email='b@test', phone=None)
        theirs = self.client.get('/api/notifications').json()['items'][0]['id']
        self.actor = SimpleNamespace(id='a', email='a@test', phone=None)
        self.assertEqual(self.client.post('/api/notifications/batch-actions', json={'ids':[mine,theirs],'action':'delete'}).status_code, 404)
        self.assertEqual(self.client.get('/api/notifications').json()['total'], 1)

    def test_idempotent_create_and_transaction_rollback(self):
        self.create(event_key='same'); self.create(event_key='same')
        self.assertEqual(self.client.get('/api/notifications').json()['total'], 1)
        with Session(self.engine) as db:
            self.center['create'](db, ['a'], 'rollback', 'body'); db.rollback()
        self.assertEqual(self.client.get('/api/notifications').json()['total'], 1)

    def test_legacy_migration_repeated_preserves_unknown_receipts(self):
        with Session(self.engine) as db:
            db.add(self.legacy(id='old', recipient_user_id='a', title='old', content='body', status='read'))
            db.commit()
            self.assertEqual(self.center['migrate'](db), 1); db.commit()
            self.assertEqual(self.center['migrate'](db), 0); db.commit()
        item = self.client.get('/api/notifications').json()['items'][0]
        self.assertEqual(item['status'], 'read')
        self.assertIsNone(item['delivered_at'])
        self.assertIsNone(item['first_read_at'])

    def login(self, uid):
        with Session(self.engine) as db:
            user = db.get(self.User, uid)
            self.actor = SimpleNamespace(id=user.id, email=user.email, phone=user.phone,
                                         platform_role=user.platform_role)

    def send(self, **overrides):
        body = dict(title='Contract message', content='Body', severity='important',
                    category='announcement', tenant_id='tenant-a', recipient_ids=['b'],
                    attachment_ids=[], draft=False, event_key=secrets.token_hex(16))
        body.update(overrides)
        return self.client.post('/api/notifications/send', json=body)

    def assert_ok(self, response):
        self.assertIn(response.status_code, (200, 201), response.text)
        return response.json()

    def test_sender_context_and_sent_visibility(self):
        context = self.assert_ok(self.client.get('/api/notifications/sender-context'))
        self.assertEqual({row['id'] for row in context['enterprises']}, {'tenant-a'})
        self.assertEqual({row['id'] for row in context['users']}, {'a', 'b'})
        self.assert_ok(self.send())
        sent = self.assert_ok(self.client.get('/api/notifications/sent'))
        self.assertEqual(sent['total'], 1)
        self.login('b')
        self.assertEqual(self.assert_ok(self.client.get('/api/notifications/sent'))['total'], 0)

    def test_enterprise_sender_cannot_send_across_tenants(self):
        response = self.send(recipient_ids=['b', 'c'])
        self.assertIn(response.status_code, (400, 403), response.text)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(self.center['Message'])), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(self.center['Receipt'])), 0)
        self.login('c')
        self.assertEqual(self.client.get('/api/notifications').json()['total'], 0)

    def test_platform_sender_can_send_across_tenants(self):
        self.login('platform')
        self.assert_ok(self.send(tenant_id='', recipient_ids=['b', 'c']))
        for uid in ('b', 'c'):
            self.login(uid)
            self.assertEqual(self.client.get('/api/notifications').json()['total'], 1)

    def test_ordinary_user_cannot_send_or_change_settings(self):
        self.login('b')
        self.assertEqual(self.send().status_code, 403)
        response = self.client.put('/api/notifications/settings', json={
            'retention_days': 180, 'attachment_max_mb': 10, 'email_enabled': True})
        self.assertEqual(response.status_code, 403, response.text)

    def test_draft_publish_recall_and_repeat_publish(self):
        self.assert_ok(self.send(draft=True))
        sent = self.assert_ok(self.client.get('/api/notifications/sent?folder=all'))['items'][0]
        mid = sent.get('message_id', sent['id'])
        self.login('b')
        self.assertEqual(self.client.get('/api/notifications').json()['total'], 0)
        self.login('a')
        self.assert_ok(self.client.post(f'/api/notifications/sent/{mid}/publish'))
        repeated = self.client.post(f'/api/notifications/sent/{mid}/publish')
        self.assertIn(repeated.status_code, (200, 409), repeated.text)
        self.login('b')
        self.assertEqual(self.client.get('/api/notifications').json()['total'], 1)
        self.assertIn(self.client.post(f'/api/notifications/sent/{mid}/recall', json={'reason': 'test'}).status_code, (403, 404))
        self.login('a')
        self.assert_ok(self.client.post(f'/api/notifications/sent/{mid}/recall', json={'reason': 'test'}))
        self.login('b')
        self.assertEqual(self.client.get('/api/notifications').json()['total'], 0)

    def test_concurrent_event_key_has_one_message_and_receipt(self):
        barrier = Barrier(4)
        def create_same_event(_):
            barrier.wait(timeout=10)
            return self.create(event_key='concurrent-event')
        with ThreadPoolExecutor(max_workers=4) as executor:
            ids = list(executor.map(create_same_event, range(4)))
        self.assertEqual(len(set(ids)), 1)
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(self.center['Message'])), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(self.center['Receipt'])), 1)

    def test_read_all_cutoff_excludes_new_messages_and_other_users(self):
        self.create(('a', 'b'), severity='urgent')
        cutoff = self.clock
        self.clock += timedelta(seconds=1)
        self.create()
        self.assert_ok(self.client.post('/api/notifications/read-all', json={'cutoff': cutoff.isoformat()}))
        data = self.client.get('/api/notifications').json()
        self.assertEqual(data['unread'], 1)
        self.assertTrue(all(item['acknowledged_at'] is None for item in data['items']))
        self.login('b')
        self.assertEqual(self.client.get('/api/notifications/summary').json()['unread'], 1)

    def test_read_all_rejects_invalid_cutoff(self):
        response = self.client.post('/api/notifications/read-all', json={'cutoff': 'not-a-date'})
        self.assertEqual(response.status_code, 422, response.text)

    def test_recall_requires_nonblank_reason(self):
        sent = self.assert_ok(self.send())
        mid = sent['id']
        for body in (None, {}, {'reason': ''}, {'reason': '   '}):
            with self.subTest(body=body):
                response = self.client.post(f'/api/notifications/sent/{mid}/recall', json=body)
                self.assertEqual(response.status_code, 422, response.text)
        self.assert_ok(self.client.post(f'/api/notifications/sent/{mid}/recall', json={'reason': 'test'}))

    def test_summary_returns_recent_five_and_server_cutoff(self):
        for _ in range(7):
            self.create()
            self.clock += timedelta(seconds=1)
        summary = self.assert_ok(self.client.get('/api/notifications/summary'))
        self.assertEqual(summary['unread'], 7)
        self.assertEqual(len(summary['recent']), 5)
        timestamp = datetime.fromisoformat(summary['server_time'].replace('Z', '+00:00'))
        self.assertEqual(timestamp, self.clock)

    def test_preferences_are_persistent_and_user_scoped(self):
        self.assert_ok(self.client.put('/api/notifications/preferences', json={'email_enabled': False}))
        self.assertFalse(self.assert_ok(self.client.get('/api/notifications/preferences'))['email_enabled'])
        self.login('b')
        self.assertTrue(self.assert_ok(self.client.get('/api/notifications/preferences'))['email_enabled'])
        self.login('a')
        self.assertFalse(self.assert_ok(self.client.get('/api/notifications/preferences'))['email_enabled'])

    def test_settings_roundtrip(self):
        self.login('platform')
        expected = dict(retention_days=90, attachment_max_mb=2, email_enabled=False)
        self.assert_ok(self.client.put('/api/notifications/settings', json=expected))
        actual = self.assert_ok(self.client.get('/api/notifications/settings'))
        for key, value in expected.items():
            self.assertEqual(actual[key], value)

    def test_attachment_download_authorization(self):
        uploaded = self.assert_ok(self.client.post('/api/notifications/attachments',
            files={'upload': ('contract.txt', b'isolated attachment', 'text/plain')}))
        aid = uploaded['id']
        self.login('c')
        response = self.client.get(f'/api/notifications/attachments/{aid}/download')
        self.assertIn(response.status_code, (403, 404), response.text)
        self.login('a')
        self.assert_ok(self.send(attachment_ids=[aid]))
        self.login('b')
        response = self.client.get(f'/api/notifications/attachments/{aid}/download')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b'isolated attachment')

    def test_exported_maintenance_functions(self):
        for name in ('process_once', 'cleanup', 'rollback_history'):
            self.assertTrue(callable(self.center.get(name)), f'Missing exported {name}')

    def smtp_settings(self):
        values = dict(smtp_host='smtp.example.invalid', smtp_port='587', smtp_ssl='false',
                      smtp_starttls='true', smtp_username='sender@example.invalid',
                      smtp_password='test-only-secret')
        with Session(self.engine) as db:
            db.add_all(self.SystemSetting(setting_key=key, setting_value=value)
                       for key, value in values.items())
            db.commit()

    def drain(self):
        for _ in range(30):
            if not self.center['process_once']():
                return
        self.fail('process_once did not drain the isolated ready queue')

    def test_smtp_retries_every_ten_seconds_three_retries(self):
        self.smtp_settings()
        self.create()
        with patch('smtplib.SMTP') as smtp, patch('smtplib.SMTP_SSL') as smtp_ssl:
            smtp.side_effect = OSError('isolated SMTP failure')
            smtp_ssl.side_effect = OSError('isolated SMTP failure')
            attempts = lambda: smtp.call_count + smtp_ssl.call_count
            self.drain()
            self.assertEqual(attempts(), 1)
            for expected in (2, 3, 4):
                self.clock += timedelta(seconds=9)
                self.drain()
                self.assertEqual(attempts(), expected - 1)
                self.clock += timedelta(seconds=1)
                self.drain()
                self.assertEqual(attempts(), expected)
            self.clock += timedelta(seconds=60)
            self.drain()
            self.assertEqual(attempts(), 4)
        self.assertEqual(self.client.get('/api/notifications').json()['total'], 1)
        with Session(self.engine) as db:
            rows = db.scalars(select(self.center['Outbox'])).all()
            self.assertTrue(rows)
            self.assertTrue(any(row.status in ('dead', 'failed') for row in rows))
            email_job = next(row for row in rows if row.channel == 'email')
            attempts = db.scalars(select(self.center['DeliveryAttempt']).where(
                self.center['DeliveryAttempt'].delivery_id == email_job.id).order_by(
                    self.center['DeliveryAttempt'].attempt_no)).all()
            self.assertEqual([attempt.attempt_no for attempt in attempts], [1, 2, 3, 4])
            self.assertTrue(all(attempt.finished_at is not None for attempt in attempts))

    def test_smtp_failure_then_recovery_does_not_duplicate_inbox(self):
        self.smtp_settings()
        self.create(event_key='recover')
        with patch('smtplib.SMTP') as smtp, patch('smtplib.SMTP_SSL') as smtp_ssl:
            smtp.side_effect = OSError('temporary failure')
            smtp_ssl.side_effect = OSError('temporary failure')
            self.drain()
            self.clock += timedelta(seconds=10)
            smtp.side_effect = None
            smtp_ssl.side_effect = None
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            smtp_ssl.return_value.__enter__.return_value.send_message.return_value = {}
            self.drain()
            accepted_calls = smtp.call_count + smtp_ssl.call_count
            self.drain()
            self.assertEqual(smtp.call_count + smtp_ssl.call_count, accepted_calls)
            self.assertEqual(accepted_calls, 2)
        self.assertEqual(self.client.get('/api/notifications').json()['total'], 1)

    def test_cleanup_removes_expired_not_live_messages(self):
        expired = self.create(event_key='expired')
        live = self.create(event_key='live')
        self.drain()
        with Session(self.engine) as db:
            db.get(self.center['Message'], expired).expires_at = self.clock - timedelta(seconds=1)
            db.commit()
        self.center['cleanup']()
        with Session(self.engine) as db:
            self.assertIsNone(db.get(self.center['Message'], expired))
            self.assertIsNotNone(db.get(self.center['Message'], live))
            self.assertEqual(db.scalar(select(func.count()).select_from(self.center['Receipt'])), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(self.center['Delivery'])), 2)
            self.assertEqual(db.scalar(select(func.count()).select_from(self.center['DeliveryAttempt'])), 2)

    def test_failed_delivery_admin_retry_and_recovery(self):
        self.smtp_settings()
        self.create(event_key='admin-recovery')
        with patch('smtplib.SMTP', side_effect=OSError('temporary failure')):
            for _ in range(4):
                self.drain()
                self.clock += timedelta(seconds=10)
        with Session(self.engine) as db:
            failed = db.scalar(select(self.center['Delivery']).where(
                self.center['Delivery'].channel == 'email', self.center['Delivery'].status == 'failed'))
            self.assertIsNotNone(failed)
            jid = failed.id
            self.assertEqual(failed.attempts, 4)
        self.login('b')
        self.assertEqual(self.client.post(f'/api/notifications/deliveries/{jid}/retry').status_code, 403)
        self.login('platform')
        self.assert_ok(self.client.post(f'/api/notifications/deliveries/{jid}/retry'))
        with patch('smtplib.SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            self.drain()
            self.assertEqual(smtp.call_count, 1)
        with Session(self.engine) as db:
            recovered = db.get(self.center['Delivery'], jid)
            self.assertEqual((recovered.status, recovered.attempts), ('accepted', 1))
        self.login('a')
        self.assertEqual(self.client.get('/api/notifications').json()['total'], 1)

    def test_email_opt_out_and_global_disable_do_not_call_smtp(self):
        self.smtp_settings()
        self.assert_ok(self.client.put('/api/notifications/preferences', json={'email_enabled': False}))
        self.create()
        with patch('smtplib.SMTP') as smtp, patch('smtplib.SMTP_SSL') as ssl:
            self.drain()
            smtp.assert_not_called()
            ssl.assert_not_called()
        self.login('platform')
        self.assert_ok(self.client.put('/api/notifications/settings', json={
            'retention_days': 180, 'attachment_max_mb': 20, 'email_enabled': False}))
        self.create(('b',))
        with patch('smtplib.SMTP') as smtp, patch('smtplib.SMTP_SSL') as ssl:
            self.drain()
            smtp.assert_not_called()
            ssl.assert_not_called()

    def test_redis_failure_keeps_inbox_and_recovers(self):
        self.create()
        self.redis.publish.side_effect = OSError('isolated Redis unavailable')
        with patch('smtplib.SMTP') as smtp, patch('smtplib.SMTP_SSL') as ssl:
            self.drain()
            self.assertEqual(self.client.get('/api/notifications').json()['total'], 1)
            self.clock += timedelta(seconds=10)
            self.redis.publish.side_effect = None
            self.drain()
            smtp.assert_not_called()
            ssl.assert_not_called()
        with Session(self.engine) as db:
            job = db.scalar(select(self.center['Delivery']).where(self.center['Delivery'].channel == 'realtime'))
            self.assertEqual((job.status, job.attempts), ('accepted', 2))

    def test_expired_worker_lease_is_reclaimed(self):
        self.create()
        with Session(self.engine) as db:
            job = db.scalar(select(self.center['Delivery']).where(self.center['Delivery'].channel == 'realtime'))
            job.status = 'processing'
            job.lease_until = self.clock - timedelta(seconds=1)
            job.lease_token = 'crashed-worker'
            job.attempts = 1
            jid = job.id
            db.commit()
        with patch('smtplib.SMTP') as smtp, patch('smtplib.SMTP_SSL') as ssl:
            self.drain()
            smtp.assert_not_called()
            ssl.assert_not_called()
        with Session(self.engine) as db:
            job = db.get(self.center['Delivery'], jid)
            self.assertEqual((job.status, job.attempts), ('accepted', 2))
            self.assertNotEqual(job.lease_token, 'crashed-worker')

    def test_attachment_rejects_malware_and_scan_failure(self):
        for scan_result, expected in (('infected', 422), ('error', 503)):
            with self.subTest(scan_result=scan_result):
                self.scan_result = scan_result
                response = self.client.post('/api/notifications/attachments',
                    files={'upload': ('blocked.txt', b'test', 'text/plain')})
                self.assertEqual(response.status_code, expected, response.text)
                self.assertEqual(self.objects, {})

    def test_attachment_limits_and_ordinary_upload_denial(self):
        self.login('platform')
        self.assert_ok(self.client.put('/api/notifications/settings', json={
            'retention_days': 180, 'attachment_max_mb': 1, 'email_enabled': True}))
        response = self.client.post('/api/notifications/attachments',
            files={'upload': ('large.txt', b'x' * (1024 * 1024 + 1), 'text/plain')})
        self.assertEqual(response.status_code, 413, response.text)
        self.login('b')
        response = self.client.post('/api/notifications/attachments',
            files={'upload': ('ordinary.txt', b'test', 'text/plain')})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.objects, {})

    def test_rollback_history_preserves_source_and_live_messages(self):
        live = self.create(event_key='live-message')
        with Session(self.engine) as db:
            db.add(self.legacy(id='rollback-source', recipient_user_id='a', title='old', content='body'))
            db.commit()
            self.assertEqual(self.center['migrate'](db), 1)
            db.commit()
        self.assertEqual(self.center['rollback_history'](), 1)
        self.assertEqual(self.center['rollback_history'](), 0)
        with Session(self.engine) as db:
            self.assertIsNotNone(db.get(self.legacy, 'rollback-source'))
            self.assertIsNotNone(db.get(self.center['Message'], live))
            self.assertEqual(db.scalar(select(func.count()).select_from(self.center['Receipt'])), 1)

    def test_create_rollback_removes_outbox_too(self):
        with Session(self.engine) as db:
            self.center['create'](db, ['a'], 'rollback', 'body', event_key='outbox-rollback')
            db.rollback()
        with Session(self.engine) as db:
            for name in ('Message', 'Receipt', 'Delivery'):
                self.assertEqual(db.scalar(select(func.count()).select_from(self.center[name])), 0)

    def test_cleanup_expired_history_is_not_remigrated(self):
        with Session(self.engine) as db:
            db.add(self.legacy(id='expired-history', recipient_user_id='a', title='old', content='body',
                               created_at=self.clock - timedelta(days=181)))
            db.commit()
            self.center['migrate'](db)
            db.commit()
        self.center['cleanup']()
        with Session(self.engine) as db:
            self.assertEqual(self.center['migrate'](db), 0)
            db.commit()
            self.assertEqual(db.scalar(select(func.count()).select_from(self.center['Message'])), 0)


class WorkerTests(unittest.TestCase):
    def load_script(self, name):
        root = Path(__file__).parents[1]
        if not (root / 'scripts').is_dir():
            root = root.parent
        path = root / 'scripts' / (name + '.py')
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_worker_drains_fifty_jobs_and_keeps_dispatching_after_scan_error(self):
        worker = self.load_script('message_center_worker')
        stopped = Mock()
        stopped.is_set.return_value = False
        stopped.wait.side_effect = lambda *_: setattr(stopped.is_set, 'return_value', True)
        process = Mock(return_value=True)
        scan = Mock(side_effect=RuntimeError('isolated scan failure'))
        center = {'process_once': process, 'scheduled_scan': scan}
        with patch.dict('os.environ', {'MESSAGE_CENTER_WORKER_INTERVAL': '2',
                                      'MESSAGE_CENTER_WORKER_BATCH_SIZE': '50',
                                      'MESSAGE_CENTER_WORKER_CYCLE_SECONDS': '15'}), \
                patch.object(worker.importlib, 'import_module', return_value=SimpleNamespace(message_center=center)), \
                patch.object(worker.threading, 'Event', return_value=stopped), \
                patch.object(worker.signal, 'signal'), patch.object(worker.time, 'monotonic', return_value=0), \
                patch.object(worker.Path, 'touch') as heartbeat, self.assertLogs(level='ERROR'):
            worker.main()
        self.assertEqual(process.call_count, 50)
        self.assertEqual(heartbeat.call_count, 50)
        scan.assert_called_once()

    def test_worker_scans_every_sixty_seconds(self):
        worker = self.load_script('message_center_worker')
        clock = [0]
        stopped = Mock()
        stopped.is_set.return_value = False
        def wait(_):
            clock[0] += 30
            if clock[0] >= 90:
                stopped.is_set.return_value = True
        stopped.wait.side_effect = wait
        process, scan = Mock(return_value=False), Mock()
        with patch.dict('os.environ', {'MESSAGE_CENTER_WORKER_INTERVAL': '2',
                                      'MESSAGE_CENTER_WORKER_BATCH_SIZE': '50',
                                      'MESSAGE_CENTER_WORKER_CYCLE_SECONDS': '15'}), \
                patch.object(worker.importlib, 'import_module', return_value=SimpleNamespace(
                    message_center={'process_once': process, 'scheduled_scan': scan})), \
                patch.object(worker.threading, 'Event', return_value=stopped), \
                patch.object(worker.signal, 'signal'), \
                patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(worker.Path, 'touch'):
            worker.main()
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(process.call_count, 3)

    def test_worker_stops_between_jobs(self):
        worker = self.load_script('message_center_worker')
        stopped = Mock()
        stopped.is_set.return_value = False
        def process():
            stopped.is_set.return_value = True
            return True
        dispatch = Mock(side_effect=process)
        with patch.dict('os.environ', {'MESSAGE_CENTER_WORKER_INTERVAL': '2',
                                      'MESSAGE_CENTER_WORKER_BATCH_SIZE': '50',
                                      'MESSAGE_CENTER_WORKER_CYCLE_SECONDS': '15'}), \
                patch.object(worker.importlib, 'import_module', return_value=SimpleNamespace(
                    message_center={'process_once': dispatch})), \
                patch.object(worker.threading, 'Event', return_value=stopped), \
                patch.object(worker.signal, 'signal'), patch.object(worker.Path, 'touch'):
            worker.main()
        dispatch.assert_called_once()

    def test_cleanup_entrypoint_calls_export_without_startup(self):
        cleanup = self.load_script('message_center_cleanup')
        exported = Mock(return_value={'messages_removed': 0, 'attachments_removed': 0})
        with patch.dict('os.environ', {}), patch.object(cleanup.importlib, 'import_module',
                return_value=SimpleNamespace(message_center={'cleanup': exported})):
            cleanup.main()
        exported.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
