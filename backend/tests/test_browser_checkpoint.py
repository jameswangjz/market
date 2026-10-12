import unittest

from trading_phase13_checkpoint import validate_retained_review


class RetainedBrowserCheckpointTests(unittest.TestCase):
    def evidence(self):
        return {'status': 'passed', 'database': 'real PostgreSQL',
            'runtime_tag': '20261012031458-ad8a888', 'cleanup': 'retained_by_user',
            **{key: 'passed' for key in ('browser_sso', 'mock_session', 'application_access', 'negative_cases')},
            'retention': {'requested_by_user': True, 'order_unchanged': True, 'order_id': 'a' * 32}}

    def test_review_accepts_explicit_retention_without_claiming_cleanup(self):
        validate_retained_review(self.evidence(), '20261012031458-ad8a888')

    def test_refuses_missing_or_failed_gates(self):
        for key in ('browser_sso', 'mock_session', 'application_access', 'negative_cases'):
            evidence = self.evidence()
            evidence[key] = 'pending'
            with self.assertRaises(ValueError):
                validate_retained_review(evidence, '20261012031458-ad8a888')

    def test_refuses_unapproved_or_changed_order(self):
        for key in ('requested_by_user', 'order_unchanged'):
            evidence = self.evidence()
            evidence['retention'][key] = False
            with self.assertRaises(ValueError):
                validate_retained_review(evidence, '20261012031458-ad8a888')

    def test_refuses_other_runtime_or_order(self):
        for key, value in (('runtime_tag', '20261011233332-d866c37'), ('cleanup', 'verified')):
            evidence = self.evidence()
            evidence[key] = value
            with self.assertRaises(ValueError):
                validate_retained_review(evidence, '20261012031458-ad8a888')


if __name__ == '__main__':
    unittest.main()
