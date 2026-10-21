"""Isolated HTTP tests: no real email, Redis or business data mutations."""
import importlib
import re
import unittest
from datetime import timedelta
from unittest.mock import patch

import test_message_business_events as fixture


class PasswordSecurityTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BusinessRoutesHTTPTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BusinessRoutesHTTPTests.tearDownClass.__func__)
    tearDown = fixture.BusinessRoutesHTTPTests.tearDown
    request = fixture.BusinessRoutesHTTPTests.request

    def setUp(self):
        fixture.BusinessRoutesHTTPTests.setUp(self)
        self.mod = importlib.import_module(self.m.__package__ + '.password_security')
        with self.m.SessionLocal() as db:
            user = db.get(self.m.User, 'buyer')
            user.password_hash = self.m.hash_password('Original123!')
            user.email_verified = True
            for key, value in dict(smtp_host='smtp.invalid', smtp_username='mailer@example.invalid',
                                   smtp_password='test-secret', smtp_starttls='true').items():
                db.add(self.m.SystemSetting(setting_key=key, setting_value=value))
            db.commit()
            self.tokens['buyer'] = self.m.issue_token(user)
        self.mail = patch.object(self.mod.smtplib, 'SMTP').start()
        self.addCleanup(patch.stopall)

    def call(self, path, body, expected=200, auth=False):
        headers = {'Authorization': 'Bearer ' + self.tokens['buyer']} if auth else {}
        response = self.client.post('/api/auth/' + path, json=body, headers=headers)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def request_code(self):
        self.call('password-reset/request', {'identifier': 'buyer@example.invalid'})
        message = self.mail.return_value.__enter__.return_value.send_message.call_args.args[0]
        return re.search(r'验证码：(\d{6})', message.get_content()).group(1)

    def test_change_revokes_old_token_and_password_but_new_login_works(self):
        self.call('change-password', {'current_password': 'bad', 'new_password': 'Updated123!'}, 400, True)
        self.call('change-password', {'current_password': 'Original123!', 'new_password': 'Updated123!'}, auth=True)
        self.request('GET', '/api/auth/me', expected=401)
        self.call('login', {'identifier': 'buyer@example.invalid', 'password': 'Original123!'}, 401)
        login = self.call('login', {'identifier': 'buyer@example.invalid', 'password': 'Updated123!'})
        self.tokens['buyer'] = login['token']
        self.request('GET', '/api/auth/me')

    def test_email_reset_one_time_hashed_and_audited_without_secrets(self):
        code = self.request_code()
        with self.m.SessionLocal() as db:
            state = db.get(self.m.PasswordSecurity, 'buyer')
            self.assertNotEqual(state.code_hash, code)
        self.call('password-reset/confirm', {'identifier': 'buyer@example.invalid', 'code': code, 'new_password': 'Updated123!'})
        self.call('password-reset/confirm', {'identifier': 'buyer@example.invalid', 'code': code, 'new_password': 'Another123!'}, 400)
        self.request('GET', '/api/auth/me', expected=401)
        with self.m.SessionLocal() as db:
            logs = db.scalars(self.select(self.m.AuditLog).where(self.m.AuditLog.action == 'reset_password')).all()
            self.assertEqual(len(logs), 1)
            self.assertNotIn(code, logs[0].detail)
            self.assertNotIn('Updated123!', logs[0].detail)

    def test_wrong_code_lockout_and_send_throttle(self):
        code = self.request_code()
        self.call('password-reset/request', {'identifier': 'buyer@example.invalid'})
        self.assertEqual(self.mail.return_value.__enter__.return_value.send_message.call_count, 1)
        wrong = '000000' if code != '000000' else '111111'
        for _ in range(5):
            self.call('password-reset/confirm', {'identifier': 'buyer@example.invalid', 'code': wrong, 'new_password': 'Updated123!'}, 400)
        self.call('password-reset/confirm', {'identifier': 'buyer@example.invalid', 'code': code, 'new_password': 'Updated123!'}, 400)

    def test_expiry_disabled_unverified_and_unknown_accounts(self):
        code = self.request_code()
        with self.m.SessionLocal() as db:
            db.get(self.m.PasswordSecurity, 'buyer').expires_at = self.m.now() - timedelta(seconds=1)
            db.commit()
        self.call('password-reset/confirm', {'identifier': 'buyer@example.invalid', 'code': code, 'new_password': 'Updated123!'}, 400)
        for identifier in ('missing', 'disabled_ops@example.invalid', 'provider@example.invalid', 'inactive@example.invalid'):
            result = self.call('password-reset/request', {'identifier': identifier})
            self.assertNotIn('code', result)
        self.assertEqual(self.mail.return_value.__enter__.return_value.send_message.call_count, 1)

    def test_change_requires_auth_strength_confirmation_and_old_password(self):
        body = {'current_password': 'Original123!', 'new_password': 'Updated123!'}
        self.call('change-password', body, 401)
        for password, status in [('short1', 422), ('abcdefgh', 400), ('Original123!', 400)]:
            self.call('change-password', {**body, 'new_password': password}, status, True)
        for _ in range(5):
            self.call('change-password', {**body, 'current_password': 'bad'}, 400, True)
        self.call('change-password', body, 429, True)

    def test_smtp_failure_does_not_expose_code_or_server_credentials(self):
        self.mail.return_value.__enter__.side_effect = RuntimeError('secret')
        result = self.call('password-reset/request', {'identifier': 'buyer@example.invalid'})
        self.assertNotIn('secret', str(result))

    def test_contact_change_invalidates_existing_reset_and_fixed_development_code_is_not_accepted(self):
        code = self.request_code()
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'buyer').email = 'new@example.invalid'
            db.commit()
        for value in (code, '123456'):
            self.call('password-reset/confirm', {'identifier': 'new@example.invalid', 'code': value, 'new_password': 'Updated123!'}, 400)

    def test_change_clears_temporary_password_and_pending_reset_code(self):
        code = self.request_code()
        with self.m.SessionLocal() as db:
            db.get(self.m.User, 'buyer').temporary_password_hash = self.m.hash_password('Temporary123!')
            db.get(self.m.User, 'buyer').temporary_password_expires_at = self.m.now() + timedelta(days=1)
            db.commit()
        self.call('change-password', {'current_password': 'Original123!', 'new_password': 'Updated123!'}, auth=True)
        self.call('login', {'identifier': 'buyer@example.invalid', 'password': 'Temporary123!'}, 401)
        self.call('password-reset/confirm', {'identifier': 'buyer@example.invalid', 'code': code, 'new_password': 'Another123!'}, 400)


if __name__ == '__main__':
    unittest.main()
