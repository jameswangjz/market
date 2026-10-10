"""Run in the API image. Uses only an isolated SQLite database, never startup()."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.main as app
import trading_task_control as control


class TaskImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + str(Path(self.temp.name) / 'isolated.db'))
        app.Base.metadata.create_all(self.engine)
        self.original = app.SessionLocal
        app.SessionLocal = sessionmaker(bind=self.engine)
        self.plan = Path(self.temp.name) / 'plan.json'
        self.plan.write_text(json.dumps({'version': 'test-plan', 'tasks': [
            {'code': 'TRD-TEST', 'owner': 'test', 'title': 'isolated test', 'area': 'test', 'priority': 'P0',
             'stage': 0, 'status': 'todo', 'progress': 0, 'dependencies': [], 'acceptance': 'isolated evidence'}]}))

    def tearDown(self):
        app.SessionLocal = self.original
        self.engine.dispose()
        self.temp.cleanup()

    def run_mode(self, mode, *extra):
        stream = io.StringIO()
        with patch.object(sys, 'argv', ['control', mode, '--plan', str(self.plan), *extra]), contextlib.redirect_stdout(stream):
            control.main()
        return json.loads(stream.getvalue())

    def test_import_twice_preserves_changed_progress(self):
        self.assertEqual(self.run_mode('import')['inserted'], 1)
        with app.SessionLocal() as db:
            task = db.scalar(select(app.DevelopmentTask).where(app.DevelopmentTask.code == 'TRD-TEST'))
            task.status, task.progress = 'in_progress', 25
            db.commit()
        self.assertEqual(self.run_mode('import')['inserted'], 0)
        with app.SessionLocal() as db:
            task = db.scalar(select(app.DevelopmentTask))
            self.assertEqual((task.status, task.progress), ('in_progress', 25))
            self.assertEqual(len(list(db.scalars(select(app.AuditLog)))), 1)

    def test_invalid_plan_is_rejected_before_insertion(self):
        plan = json.loads(self.plan.read_text())
        plan['tasks'][0]['dependencies'] = ['TRD-MISSING']
        self.plan.write_text(json.dumps(plan))
        with self.assertRaisesRegex(ValueError, 'Missing dependency'):
            self.run_mode('import')
        with app.SessionLocal() as db:
            self.assertFalse(list(db.scalars(select(app.DevelopmentTask))))

    def test_monitor_notification_deduplicates_without_state_change(self):
        self.run_mode('import')
        with app.SessionLocal() as db:
            db.add(app.User(id='test-admin', email='test-admin@example.invalid', name='Isolated administrator',
                            password_hash='unused-fixture', platform_role='super_admin', is_active=True,
                            activation_status='active', verified_status='verified'))
            task = db.scalar(select(app.DevelopmentTask))
            task.status, task.progress = 'blocked', 25
            db.commit()
        for _ in range(2):
            result = self.run_mode('monitor', '--notify')
            self.assertEqual(result['alerts'][0]['reason'], 'blocked')
        with app.SessionLocal() as db:
            self.assertEqual(len(list(db.scalars(select(app.message_center['Message'])))), 1)
            task = db.scalar(select(app.DevelopmentTask))
            self.assertEqual((task.status, task.progress), ('blocked', 25))


if __name__ == '__main__':
    unittest.main()
