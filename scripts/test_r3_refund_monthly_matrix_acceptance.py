"""Execute QA006 against disposable SQLite with the existing application harness."""
from pathlib import Path
from datetime import datetime, timezone
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend' / 'tests'))

import test_refund_planning as fixture
import r3_refund_monthly_matrix_acceptance as matrix


class MonthlyMatrixTests(unittest.TestCase):
    setUpClass = classmethod(fixture.RefundPlanningHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.RefundPlanningHTTPTests.tearDownClass.__func__)
    setUp = fixture.RefundPlanningHTTPTests.setUp
    tearDown = fixture.RefundPlanningHTTPTests.tearDown
    create_order = fixture.RefundPlanningHTTPTests.create_order
    request = fixture.RefundPlanningHTTPTests.request

    def test_cross_year_partial_refunds_preserve_paid_batch_and_rebuild(self):
        with self.m.SessionLocal.begin() as db:
            for tenant in ('buyer', 'provider'):
                db.add(self.m.Enterprise(id='owned-' + tenant, name=tenant,
                    credit_code='qa006-' + tenant, verification_status='verified'))
            for actor, role in (('buyer', ''), ('admin', 'super_admin')):
                db.add(self.m.User(id='owned-' + actor, name=actor,
                    email='qa006-' + actor + '@example.invalid', password_hash='unused',
                    platform_role=role, is_active=True, activation_status='active', verified_status='verified'))
            db.flush()
            db.add(self.m.Membership(user_id='owned-buyer', enterprise_id='owned-buyer',
                role='super_admin', status='active'))
            with patch.object(self.m, 'now', return_value=datetime.now(timezone.utc)):
                headers = {actor: {'Authorization': 'Bearer ' + self.m.issue_token(
                    db.get(self.m.User, 'owned-' + actor))} for actor in ('buyer', 'admin')}
        # The parent graph's PG search-path guard is inapplicable to this
        # disposable SQLite fixture. No other graph verification is replaced.
        with patch.object(matrix.harness.cross, 'owned', return_value=None):
            cases = matrix.exercise(self.m, self.client, headers)
        result = cases[matrix.CASE_NAMES[0]]
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['refunds'], 2)
        self.assertEqual(result['csv_rows'], 2)
        self.assertTrue(result['original_paid_batch_unchanged'])
        self.assertTrue(result['rebuild_idempotent'])
        self.assertTrue(result['five_nonzero_exact_shares'])


class RemoteWrapperTests(unittest.TestCase):
    def test_invalid_frozen_rates_fail_before_database_writes(self):
        valid = matrix.FROZEN_RATES
        invalid = [dict(valid, extra_rate=0), {k: v for k, v in valid.items() if k != 'channel_rate'},
                   dict(valid, provider_rate=49), dict(valid, platform_rate=-1, provider_rate=71),
                   dict(valid, platform_rate='NaN'), dict(valid, platform_rate='Infinity'),
                   dict(valid, platform_rate=True), dict(valid, platform_rate='invalid'), []]
        for rates in invalid:
            with self.subTest(rates=rates):
                db = Mock()
                m = SimpleNamespace(order_economics={'rate_keys': tuple(valid)})
                with patch.object(matrix.harness.cross, 'owned') as owned:
                    with self.assertRaises(matrix.harness.base.AcceptanceFailure):
                        matrix.harness.cross.graph(m, db, None, 'invalid-rates', frozen_rates=rates)
                owned.assert_called_once_with(db)
                self.assertEqual(db.mock_calls, [])

    def test_inherited_runner_receives_exact_case_and_source_inventory(self):
        def run(encoded, manifest):
            self.assertEqual((encoded, manifest), ('encoded', {}))
            self.assertIs(matrix.harness.cross.seed, matrix.seed)
            self.assertIs(matrix.harness.cross.exercise, matrix.exercise)
            self.assertEqual(matrix.harness.cross.CASE_NAMES, matrix.CASE_NAMES)
            self.assertEqual(matrix.harness.cross.SOURCE_FILES, matrix.harness.SOURCES)
            return dict(status='passed')
        with patch.object(matrix.harness.cross, 'remote_run', side_effect=run):
            result = matrix.remote_run('encoded', {})
        self.assertEqual(result['status'], 'passed')
        self.assertIn('QA006', result['incremental_scope'])


if __name__ == '__main__':
    unittest.main()
