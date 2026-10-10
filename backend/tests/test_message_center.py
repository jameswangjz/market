import importlib.util
from pathlib import Path
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, String, DateTime, select, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session
from sqlalchemy.pool import StaticPool


class InboxTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('message_center', Path(__file__).parents[1] / 'app' / 'message_center.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        class Base(DeclarativeBase):
            pass
        class User(Base):
            __tablename__ = 'users'
            id: Mapped[str] = mapped_column(String, primary_key=True)
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
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        self.actor = SimpleNamespace(id='a', email='a@test', phone=None)
        def db_session():
            with Session(self.engine) as db:
                yield db
        self.app = FastAPI()
        self.center = module.install(dict(Base=Base, app=self.app, now=lambda: datetime.now(timezone.utc),
            db_session=db_session, current_user=lambda: self.actor, audit=lambda *a, **kw: None,
            PlatformNotification=Legacy, User=User, SessionLocal=lambda: Session(self.engine)))
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            db.add_all([User(id='a'), User(id='b')]); db.commit()
        self.legacy = Legacy
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def create(self, recipients=('a',), severity='important', event_key=None):
        with Session(self.engine) as db:
            message = self.center['create'](db, recipients, '测试通知', '正文', severity=severity, event_key=event_key)
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


if __name__ == '__main__':
    unittest.main()
