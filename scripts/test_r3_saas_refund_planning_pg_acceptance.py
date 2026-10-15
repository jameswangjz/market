"""Offline wrapper guards and disposable SQLite rehearsal; no remote actions."""
from pathlib import Path
from datetime import datetime, timezone
import sys
import unittest
from unittest.mock import patch

import r3_saas_refund_planning_pg_acceptance as probe


class RemoteGuards(unittest.TestCase):
    def result(self):
        return dict(status='passed', public_write_operations=0, protected_order_content_read=False,
            protected_order_count_before=1, protected_order_count_after=1,
            owned_schema='qa_r3_be_' + 'a' * 16, cleanup='owned schema removed')

    def test_wrapper_injects_real_functions_and_restores_parent(self):
        original = probe.cross.seed
        def run(encoded, manifest):
            self.assertEqual((encoded, manifest), ('encoded', {}))
            self.assertIs(probe.cross.seed, probe.seed)
            self.assertIs(probe.cross.exercise, probe.exercise)
            self.assertEqual(probe.cross.CASE_NAMES, probe.CASE_NAMES)
            self.assertEqual(probe.cross.SOURCE_FILES, probe.SOURCE_FILES)
            return self.result()
        with patch.object(probe.cross, 'remote_run', side_effect=run):
            result = probe.remote_run('encoded', {})
        self.assertEqual(result['status'], 'passed')
        self.assertIs(probe.cross.seed, original)
        self.assertIn('synthetic', result['external_adapters']['provider'])
        self.assertEqual(result['external_adapters']['Redis'], 'Forbidden')

    def test_mandatory_isolation_evidence_fails_closed(self):
        for key, value in [('public_write_operations', 1), ('protected_order_content_read', True),
                ('protected_order_count_before', 0), ('protected_order_count_after', 2),
                ('owned_schema', 'public'), ('owned_schema', 'qa_r3_be_bad'),
                ('cleanup', 'no schema created'), ('status', 'failed')]:
            with self.subTest(key=key, value=value):
                result = self.result()
                result[key] = value
                with patch.object(probe.cross, 'remote_run', return_value=result):
                    self.assertEqual(probe.remote_run('', {})['status'], 'failed')
        for key in self.result():
            result = self.result()
            del result[key]
            with self.subTest(missing=key), patch.object(probe.cross, 'remote_run', return_value=result):
                self.assertNotEqual(probe.remote_run('', {}).get('status'), 'passed')

    def test_graph_requires_owned_schema_before_any_writes(self):
        from unittest.mock import Mock
        db = Mock()
        with patch.object(probe.cross, 'owned', side_effect=RuntimeError('outside owned schema')):
            with self.assertRaises(RuntimeError):
                probe.graph(None, db, 'guard')
        self.assertEqual(db.mock_calls, [])


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend' / 'tests'))
import test_refund_planning as fixture


class SQLiteRehearsal(unittest.TestCase):
    """Exercises real recorder/latest/planner/holds; does not claim PostgreSQL."""
    setUpClass = classmethod(fixture.RefundPlanningHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.RefundPlanningHTTPTests.tearDownClass.__func__)
    setUp = fixture.RefundPlanningHTTPTests.setUp
    tearDown = fixture.RefundPlanningHTTPTests.tearDown
    create_order = fixture.RefundPlanningHTTPTests.create_order
    request = fixture.RefundPlanningHTTPTests.request

    def test_real_saas_graph_and_registered_routes_without_redis(self):
        with self.m.SessionLocal.begin() as db:
            for tenant in ('buyer', 'provider'):
                db.add(self.m.Enterprise(id='owned-' + tenant, name=tenant,
                    credit_code='saas-' + tenant, verification_status='verified'))
            for actor, role in (('buyer', ''), ('admin', 'super_admin')):
                db.add(self.m.User(id='owned-' + actor, name=actor,
                    email='saas-' + actor + '@example.invalid', password_hash='unused',
                    platform_role=role, is_active=True, activation_status='active', verified_status='verified'))
            db.flush()
            db.add(self.m.Membership(user_id='owned-buyer', enterprise_id='owned-buyer',
                role='super_admin', status='active'))
            with patch.object(self.m, 'now', return_value=datetime.now(timezone.utc)):
                headers = {actor: {'Authorization': 'Bearer ' + self.m.issue_token(
                    db.get(self.m.User, 'owned-' + actor))} for actor in ('buyer', 'admin')}
        # Only PG search-path checks and actor seeding differ in this local rehearsal.
        with patch.object(probe.cross, 'owned'), patch.object(self.m, 'apisix_policy_redis',
                side_effect=AssertionError('Redis forbidden')), patch.object(self.m, 'saas_call',
                side_effect=AssertionError('Provider HTTP forbidden')), \
                patch.object(probe.cross.fixture, 'seed', return_value=headers), \
                patch.object(probe.cross, 'safe_error', side_effect=lambda exc: self.fail(str(exc))):
            headers = probe.seed(self.m)
            cases = probe.exercise(self.m, self.client, headers)
        self.assertEqual(set(cases), set(probe.CASE_NAMES))
        self.assertTrue(all(row['status'] == 'passed' for row in cases.values()), cases)


if __name__ == '__main__':
    unittest.main()
