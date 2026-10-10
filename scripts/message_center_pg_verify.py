"""Verify PostgreSQL concurrency in a disposable schema, without importing main.

Connection-local TEMP tables cannot be shared by concurrent connections. All
test tables therefore live in a random schema that is dropped in finally.
SMTP/Redis are mocked; production tables are never read or written.
"""
import argparse
import importlib.util
import json
import os
import secrets
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from sqlalchemy import Boolean, DateTime, MetaData, String, Text, create_engine, delete, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.schema import CreateSchema, DropSchema


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def load_center(path, engine, schema):
    spec = importlib.util.spec_from_file_location('isolated_message_center', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Base(DeclarativeBase):
        metadata = MetaData(schema=schema)

    class User(Base):
        __tablename__ = 'users'
        id: Mapped[str] = mapped_column(String, primary_key=True)
        email: Mapped[str] = mapped_column(String, default='test@example.invalid')
        phone: Mapped[str | None] = mapped_column(String, nullable=True)
        name: Mapped[str] = mapped_column(String, default='isolated user')
        platform_role: Mapped[str] = mapped_column(String, default='')
        is_active: Mapped[bool] = mapped_column(Boolean, default=True)
        activation_status: Mapped[str] = mapped_column(String, default='active')
        email_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    class Legacy(Base):
        __tablename__ = 'platform_notifications'
        id: Mapped[str] = mapped_column(String, primary_key=True)
        recipient_user_id: Mapped[str] = mapped_column(String)
        title: Mapped[str] = mapped_column(String)
        content: Mapped[str] = mapped_column(Text)
        target_type: Mapped[str] = mapped_column(String, default='')
        target_id: Mapped[str] = mapped_column(String, default='')
        status: Mapped[str] = mapped_column(String, default='unread')
        created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    class Setting(Base):
        __tablename__ = 'system_settings'
        id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: secrets.token_hex(16))
        setting_key: Mapped[str] = mapped_column(String, unique=True)
        setting_value: Mapped[str] = mapped_column(Text, default='')

    def session():
        with Session(engine) as db:
            yield db

    center = module.install(dict(Base=Base, app=FastAPI(), now=lambda: datetime.now(timezone.utc),
        db_session=session, SessionLocal=lambda: Session(engine), disable_background=True,
        current_user=lambda: SimpleNamespace(id='a', email='test@example.invalid', phone=None, platform_role=''),
        audit=lambda *args, **kwargs: None, User=User, PlatformNotification=Legacy, SystemSetting=Setting))
    require(all(table.schema == schema for table in Base.metadata.tables.values()), 'Unscoped metadata')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([User(id='a'), User(id='b')])
        db.commit()
    return center


def verify(engine, center):
    gate = threading.Barrier(4)
    def producer(_):
        with Session(engine) as db:
            gate.wait(timeout=15)
            message = center['create'](db, ['a'], 'Isolated PG event', 'Test body', event_key='pg-four-producers')
            mid = message.id
            db.commit()
            return mid

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(producer, range(4)))
    require(len(set(ids)) == 1, 'Concurrent event created multiple messages')
    with Session(engine) as db:
        counts = {name: db.scalar(select(func.count()).select_from(center[name]))
                  for name in ('Message', 'Receipt', 'Outbox')}
        require(counts == {'Message': 1, 'Receipt': 1, 'Outbox': 2}, 'Unexpected event/receipt/outbox count')
        jobs = db.scalars(select(center['Outbox'])).all()
        require({job.channel for job in jobs} == {'email', 'realtime'}, 'Duplicate/missing delivery channel')
        # The email channel is irrelevant to row claims; leave two realtime jobs.
        db.execute(delete(center['Outbox']))
        center['create'](db, ['a', 'b'], 'Two worker test', 'Test body', event_key='pg-two-workers')
        db.flush()
        db.execute(delete(center['Outbox']).where(center['Outbox'].channel == 'email'))
        db.commit()

    Delivery = center['Delivery']
    # Hold the first row in a separate transaction: process_once must skip it.
    with Session(engine) as blocker:
        locked = blocker.scalar(select(Delivery).order_by(Delivery.next_attempt_at, Delivery.id).with_for_update().limit(1))
        locked_id = locked.id
        with ThreadPoolExecutor(max_workers=1) as executor:
            processed = executor.submit(center['process_once']).result(timeout=15)
        require(processed is True, 'Worker did not skip the locked job')
        with Session(engine) as db:
            rows = db.scalars(select(Delivery)).all()
            require(db.get(Delivery, locked_id).status == 'pending', 'Worker processed a locked row')
            require(sum(row.status == 'accepted' for row in rows) == 1, 'SKIP LOCKED did not process the other row')
        blocker.rollback()

    with Session(engine) as db:
        for job in db.scalars(select(Delivery)):
            job.status, job.attempts, job.lease_token = 'pending', 0, ''
            job.lease_until, job.completed_at = None, None
        db.execute(delete(center['DeliveryAttempt']))
        db.commit()

    entered = threading.Barrier(3)
    release = threading.Event()
    def hold_external_io(*args, **kwargs):
        entered.wait(timeout=15)
        require(release.wait(timeout=15), 'Timed out waiting for lease inspection')
        return 1

    with patch('redis.Redis.from_url') as redis_factory:
        redis_factory.return_value.publish.side_effect = hold_external_io
        with ThreadPoolExecutor(max_workers=2) as executor:
            workers = [executor.submit(center['process_once']) for _ in range(2)]
            try:
                entered.wait(timeout=15)
                with Session(engine) as db:
                    leased = db.scalars(select(Delivery).where(Delivery.status == 'processing')).all()
                    require(len(leased) == 2, 'Both workers did not claim distinct jobs')
                    require(len({job.id for job in leased}) == 2, 'Workers shared a job')
                    require(len({job.lease_token for job in leased}) == 2, 'Workers shared a lease token')
                    require(all(job.attempts == 1 and job.lease_until is not None for job in leased), 'Invalid active lease')
                    attempts = db.scalars(select(center['DeliveryAttempt'])).all()
                    require(len(attempts) == 2, 'Workers created duplicate/missing attempt rows')
                    require({(row.delivery_id, row.lease_token) for row in attempts} ==
                            {(job.id, job.lease_token) for job in leased}, 'Attempt fencing differs from claimed lease')
            finally:
                release.set()
            require(all(future.result(timeout=15) is True for future in workers), 'Worker did not complete')
        require(redis_factory.return_value.publish.call_count == 2, 'Unexpected external delivery count')
    with Session(engine) as db:
        rows = db.scalars(select(Delivery)).all()
        require(all(row.status == 'accepted' and row.attempts == 1 and row.lease_until is None for row in rows),
                'Jobs did not finish exactly once in the test')
        require(db.scalar(select(func.count()).select_from(center['DeliveryAttempt']).where(
            center['DeliveryAttempt'].finished_at.is_(None))) == 0, 'Unfinished delivery attempt')
    return {'four_producers': counts, 'skip_locked': 'passed', 'two_worker_exclusive_leases': 'passed'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_module = Path(__file__).resolve().parents[1] / 'backend/app/message_center.py'
    if not default_module.exists():
        default_module = Path('/app/app/message_center.py')
    parser.add_argument('--module', type=Path, default=default_module)
    parser.add_argument('--database-url-env', default='DATABASE_URL', help='Environment variable containing PostgreSQL URL')
    args = parser.parse_args()
    require(args.module.is_file(), 'Message center module was not found')
    database_url = os.environ.get(args.database_url_env, '')
    require(bool(database_url), 'Database URL environment variable is missing')
    url = make_url(database_url)
    require(url.get_backend_name() == 'postgresql', 'This verification requires PostgreSQL')
    # psycopg is the backend image's PostgreSQL driver.
    url = url.set(drivername='postgresql+psycopg')
    schema = 'mc_verify_' + secrets.token_hex(12)
    admin = create_engine(url, connect_args={'connect_timeout': 10,
        'options': '-c statement_timeout=20000 -c lock_timeout=10000'}, pool_size=1, max_overflow=0)
    scoped = None
    created = False
    previous_handler = signal.getsignal(signal.SIGTERM)
    def terminate(*_):
        raise KeyboardInterrupt('Verification terminated')
    signal.signal(signal.SIGTERM, terminate)
    try:
        with admin.begin() as db:
            db.execute(CreateSchema(schema))
        created = True
        print(json.dumps({'schema': schema, 'created': True}), flush=True)
        scoped = create_engine(url, pool_size=8, max_overflow=0, pool_timeout=10,
            connect_args={'connect_timeout': 10, 'options':
                f'-c search_path={schema} -c statement_timeout=20000 -c lock_timeout=10000'})
        @event.listens_for(scoped, 'checkout')
        def check_scope(connection, *_):
            with connection.cursor() as cursor:
                cursor.execute('SELECT current_schema(), current_setting(\'search_path\')')
                current_schema, search_path = cursor.fetchone()
            connection.rollback()
            require(current_schema == schema and search_path == schema, 'Connection escaped the test schema')
        with patch('smtplib.SMTP', side_effect=AssertionError('Real SMTP forbidden')) as smtp, \
                patch('smtplib.SMTP_SSL', side_effect=AssertionError('Real SMTP forbidden')) as smtp_ssl, \
                patch('redis.Redis.from_url'):
            center = load_center(args.module, scoped, schema)
            result = verify(scoped, center)
            smtp.assert_not_called()
            smtp_ssl.assert_not_called()
        print(json.dumps({'schema': schema, 'checks': result}, sort_keys=True), flush=True)
    finally:
        try:
            if scoped is not None:
                scoped.dispose()
            if created:
                with admin.begin() as db:
                    db.execute(DropSchema(schema, cascade=True))
                with admin.connect() as db:
                    require(db.scalar(text('SELECT count(*) FROM pg_namespace WHERE nspname=:schema'),
                                      {'schema': schema}) == 0, 'Test schema was not removed')
                print(json.dumps({'schema': schema, 'removed': True}), flush=True)
        finally:
            admin.dispose()
            signal.signal(signal.SIGTERM, previous_handler)


if __name__ == '__main__':
    main()
