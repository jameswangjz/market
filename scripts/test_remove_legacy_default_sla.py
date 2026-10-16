import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from remove_legacy_default_sla import NAME, remove
import test_enterprise_sla as fixture


class CleanupTests(unittest.TestCase):
    setUpClass = classmethod(fixture.EnterpriseSLATests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.EnterpriseSLATests.tearDownClass.__func__)
    setUp = fixture.EnterpriseSLATests.setUp
    tearDown = fixture.EnterpriseSLATests.tearDown

    def seed(self):
        with self.m.SessionLocal() as db:
            db.add_all([self.m.SLAProfile(id='retired', name=NAME),
                        self.m.SLAProfile(id='other', name='Other historical rule')])
            db.flush()
            for key, profile in [('old-result', 'retired'), ('other-result', 'other')]:
                db.add(self.m.SLAResult(id=key, profile_id=profile,
                    period_start=self.clock, period_end=self.clock))
            db.commit()

    def test_only_requested_profile_and_results_deleted_after_backup(self):
        self.seed()
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / 'backup.json'
            with self.m.SessionLocal() as db:
                result = remove(self.m, db, backup, ['retired'], 1, 'qa@invalid.local')
                db.commit()
                self.assertEqual(result['deleted_assessments'], 1)
                self.assertIsNone(db.get(self.m.SLAProfile, 'retired'))
                self.assertIsNone(db.get(self.m.SLAResult, 'old-result'))
                self.assertIsNotNone(db.get(self.m.SLAProfile, 'other'))
                self.assertIsNotNone(db.get(self.m.SLAResult, 'other-result'))
                self.assertEqual(db.query(self.m.ServiceLevel).count(), 3)
                self.assertEqual(db.query(self.m.AuditLog).filter_by(action='retire_legacy_default_sla').count(), 1)
            saved = json.loads(backup.read_text())
            self.assertEqual([row['id'] for row in saved['results']], ['old-result'])
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_inventory_mismatch_never_deletes_or_creates_backup(self):
        self.seed()
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / 'backup.json'
            with self.m.SessionLocal() as db:
                with self.assertRaises(ValueError):
                    remove(self.m, db, backup, ['retired'], 2, 'qa@invalid.local')
                self.assertEqual(db.query(self.m.SLAProfile).count(), 2)
            self.assertFalse(backup.exists())

    def test_audit_failure_rolls_back_deletion_but_preserves_backup(self):
        self.seed()
        with tempfile.TemporaryDirectory() as directory:
            backup = Path(directory) / 'backup.json'
            with self.m.SessionLocal() as db:
                with patch.object(self.m, 'audit', side_effect=RuntimeError('audit unavailable')):
                    with self.assertRaises(RuntimeError):
                        remove(self.m, db, backup, ['retired'], 1, 'qa@invalid.local')
                db.rollback()
                self.assertIsNotNone(db.get(self.m.SLAProfile, 'retired'))
                self.assertIsNotNone(db.get(self.m.SLAResult, 'old-result'))
            self.assertTrue(backup.exists())

    def test_startup_has_no_legacy_profile_creation(self):
        source = inspect.getsource(self.m.startup)
        self.assertNotIn('SLAProfile(', source)
        self.assertNotIn(NAME, source)


if __name__ == '__main__':
    unittest.main()
