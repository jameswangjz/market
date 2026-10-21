"""Receiver transaction boundaries with PostgreSQL errors injected into SQLite."""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

import test_sla_gateway_events as fixture


class IngestTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.GatewayEventsTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.settings = []
        self.rollbacks = []

    def factory(self, *, code=None, attribute='sqlstate', fail_at='window', postgres=True):
        f = self.f
        @contextmanager
        def begin():
            with f.Session() as db:
                real_execute, real_get, real_commit = db.execute, db.get, db.commit
                def error():
                    original = Exception('private database details')
                    setattr(original, attribute, code)
                    return OperationalError('private SQL', {}, original)
                def execute(statement, *args, **kwargs):
                    if str(statement).startswith('SET LOCAL'):
                        self.settings.append(str(statement))
                        if fail_at == 'settings' and code:
                            raise error()
                        return None
                    return real_execute(statement, *args, **kwargs)
                def get(model, identity, *args, **kwargs):
                    if code and fail_at == 'window' and model is f.Window:
                        raise error()
                    return real_get(model, identity, *args, **kwargs)
                def commit():
                    if code and fail_at == 'commit':
                        raise error()
                    return real_commit()
                # Only the receiver's initial check sees PostgreSQL; ORM writes
                # keep using the real disposable SQLite connection.
                real_bind = db.get_bind
                advertised = False
                def get_bind(*args, **kwargs):
                    nonlocal advertised
                    if not advertised:
                        advertised = True
                        return SimpleNamespace(dialect=SimpleNamespace(name='postgresql' if postgres else 'sqlite'))
                    return real_bind(*args, **kwargs)
                with patch.object(db, 'get_bind', side_effect=get_bind), patch.object(db, 'execute', side_effect=execute), \
                        patch.object(db, 'get', side_effect=get), patch.object(db, 'commit', side_effect=commit):
                    try:
                        yield db
                        db.commit()
                    except BaseException:
                        self.rollbacks.append(True)
                        db.rollback()
                        raise
        return SimpleNamespace(begin=begin)

    def test_pg_timeouts_rollback_and_retry_accepts_exactly_once(self):
        for code, attr in [('55P03', 'sqlstate'), ('57014', 'pgcode'), ('40P01', 'sqlstate')]:
            with self.subTest(code=code), patch.dict(self.f.ns, SessionLocal=self.factory(code=code, attribute=attr)):
                response = self.f.post()
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.headers['Retry-After'], '2')
                self.assertNotIn('accepted', response.json())
                self.assertNotIn('private', response.text)
            self.assertEqual(self.f.count(), 0)
        self.assertEqual(len(self.rollbacks), 3)
        self.assertEqual(self.f.post().json(), {'accepted': 1, 'duplicates': 0})
        self.assertEqual(self.f.post().json(), {'accepted': 0, 'duplicates': 1})

    def test_pg_limits_precede_ingestion_and_commit_failure_never_acknowledged(self):
        with patch.dict(self.f.ns, SessionLocal=self.factory(code='57014', fail_at='commit')):
            response = self.f.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.settings, ["SET LOCAL lock_timeout = '2s'", "SET LOCAL statement_timeout = '10s'"])
        self.assertEqual(self.f.count(), 0)
        with self.f.Session() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(self.f.Window)), 0)

    def test_setting_timeout_itself_rolls_back(self):
        with patch.dict(self.f.ns, SessionLocal=self.factory(code='55P03', fail_at='settings')):
            self.assertEqual(self.f.post().status_code, 503)
        self.assertEqual(self.f.count(), 0)
        self.assertEqual(len(self.rollbacks), 1)

    def test_other_db_errors_are_not_reclassified(self):
        for code, postgres in [('23505', True), ('08006', True), ('55P03', False)]:
            with self.subTest(code=code, postgres=postgres), patch.dict(
                    self.f.ns, SessionLocal=self.factory(code=code, postgres=postgres)):
                with self.assertRaises(OperationalError):
                    self.f.post()
            self.assertEqual(self.f.count(), 0)

    def test_normal_sqlite_emits_no_postgres_settings(self):
        statements = []
        def record(connection, cursor, statement, parameters, context, many):
            statements.append(statement)
        event.listen(self.f.engine, 'before_cursor_execute', record)
        self.addCleanup(event.remove, self.f.engine, 'before_cursor_execute', record)
        self.assertEqual(self.f.post().status_code, 200)
        self.assertFalse(any(s.startswith('SET LOCAL') for s in statements))


if __name__ == '__main__':
    unittest.main()
