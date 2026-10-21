"""Real funding HTTP/core on disposable SQLite; no browser/server/deployed DB."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/tests'))
import test_message_business_events as fixtures
import clearing_v2_funding_browser_fixture as funding


class FundingBrowserFixtureTests(unittest.TestCase):
    def test_wrapper_uses_original_fixture_and_binds_only_loopback_without_server(self):
        import uvicorn
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as directory:
            tokens = Path(directory) / 'private'
            def no_server(app, **kwargs):
                self.assertEqual(kwargs['host'], '127.0.0.1')
                self.assertEqual(kwargs['port'], 8126)
                self.assertEqual(tokens.stat().st_mode & 0o777, 0o700)
                token = tokens / 'security.token'
                self.assertEqual(token.stat().st_mode & 0o777, 0o600)
                client = TestClient(app)
                try:
                    result = client.get('/api/settlement-reporting/report',
                        params=dict(q=funding.SETTLEMENT_NO, status='locked', start='1900-01-01', end='9998-12-31', page_size=100),
                        headers={'Authorization': 'Bearer ' + token.read_text()})
                    self.assertEqual(result.status_code, 200)
                    row = result.json()['items'][0]
                    self.assertEqual(row['id'], funding.SETTLEMENT_ID)
                    self.assertTrue(row['baseline_hash'])
                    self.assertEqual(row['gross_amount'], '160.00')
                finally:
                    client.close()
            with patch.object(sys, 'argv', ['funding-fixture', '--tokens', str(tokens)]), \
                    patch.object(uvicorn, 'run', side_effect=no_server) as serve:
                funding.main()
                serve.assert_called_once()
            self.assertFalse(tokens.exists())

    def test_real_isolated_proposal_independent_review_and_coverage(self):
        case = fixtures.BusinessRoutesHTTPTests('test_personal_review_http_scope_resubmission_and_get_no_events')
        case.setUpClass()
        try:
            case.setUp()
            try:
                funding.seed(case)
                m = case.m
                with m.SessionLocal() as db:
                    db.get(m.Enterprise, 'provider_tenant').verification_status = 'verified'
                    db.add(m.User(id='security', name='security', email='security@example.invalid',
                        password_hash='unused', platform_role='security_compliance', is_active=True,
                        activation_status='active', verified_status='verified'))
                    db.commit()
                    case.tokens['security'] = m.issue_token(db.get(m.User, 'security'))
                path = '/api/settlement-finance/funding-adjustments'
                proposal = case.request('POST', path, actor='platform', json=dict(request_id='fixture-propose',
                    settlement_id=funding.SETTLEMENT_ID, kind='platform_absorption', amount='10.00',
                    currency='CNY', reason='Synthetic funding browser evidence', evidence_refs=['synthetic-funding:only']))
                self.assertEqual(proposal['id'], funding.REQUEST_ID)
                decision = dict(request_id='fixture-decide', expected_version=1, decision='approved', note='Independent review')
                case.request('POST', path + '/' + funding.REQUEST_ID + '/decision', actor='platform', expected=403, json=decision)
                approved = case.request('POST', path + '/' + funding.REQUEST_ID + '/decision', actor='ops', json=decision)
                self.assertTrue(approved['can_verify'])
                verified = case.request('POST', path + '/' + funding.REQUEST_ID + '/verify', actor='ops',
                    json=dict(request_id='fixture-verify', expected_version=2))
                self.assertEqual(verified['status'], 'covered')
                self.assertFalse(verified['bank_receipt_verified'])
                for actor, params in [('provider', dict(enterprise_id='provider_tenant', business_view='provider')), ('security', {})]:
                    rows = case.request('GET', path, actor=actor, params=params)['items']
                    self.assertEqual(rows[0]['id'], funding.REQUEST_ID)
                    self.assertFalse(rows[0]['bank_receipt_verified'])
                    case.request('POST', path, actor=actor, expected=403, json=dict(request_id='denied'))
            finally:
                case.tearDown()
        finally:
            case.tearDownClass()

    def test_seed_refuses_nonisolated_engine_before_sessions(self):
        from types import SimpleNamespace
        class Sessions:
            def __call__(self):
                raise AssertionError('must not open database')
        case = SimpleNamespace(m=SimpleNamespace(engine=SimpleNamespace(
            dialect=SimpleNamespace(name='postgresql'), url='postgresql://forbidden'), SessionLocal=Sessions()))
        with self.assertRaisesRegex(RuntimeError, 'isolated'):
            funding.seed(case)


if __name__ == '__main__':
    unittest.main()
