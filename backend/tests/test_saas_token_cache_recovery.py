import threading
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from test_message_business_events import load_isolated_main


class SaaSTokenCacheRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_isolated_main()

    @classmethod
    def tearDownClass(cls):
        cls.m.engine.dispose()
        package = cls.m.__package__
        import sys
        for name in list(sys.modules):
            if name == package or name.startswith(package + "."):
                del sys.modules[name]

    def setUp(self):
        with self.m._saas_tokens_lock:
            self.m._saas_tokens.clear()
        self.config = SimpleNamespace(
            id="config", auth_mode="oauth2", client_id="client", client_secret="secret",
            scope="resource.invoke", timeout_ms=1000, base_url="https://saas.invalid",
            operation_path="/isv.php",
        )

    def response(self, status, payload=None):
        response = httpx.Response(status, json=payload or {}, request=httpx.Request("POST", "https://saas.invalid/isv.php"))
        return response

    def token_response(self, token):
        return self.response(200, {"access_token": token, "expires_in": 3600})

    def call_response(self, status, payload=None):
        return self.response(status, payload or {"ret": 0})

    def test_401_evicts_without_retry_and_next_call_fetches_new_token(self):
        m = self.m
        replies = [self.token_response("old-secret-token"), self.call_response(401),
                   self.token_response("new-secret-token"), self.call_response(200)]
        sent = []

        def post(url, **kwargs):
            sent.append((url, kwargs))
            reply = replies.pop(0)
            return reply

        with patch.object(m.httpx, "post", side_effect=post):
            with self.assertRaises(m.HTTPException) as caught:
                m.saas_call(self.config, "OPEN", {})
            self.assertEqual(len(sent), 2)
            self.assertEqual(m.saas_call(self.config, "OPEN", {}), {"ret": 0})
        self.assertEqual(len(sent), 4)
        self.assertEqual(sent[1][1]["headers"]["Authorization"], "Bearer old-secret-token")
        self.assertEqual(sent[3][1]["headers"]["Authorization"], "Bearer new-secret-token")
        self.assertNotIn("old-secret-token", str(caught.exception.detail))
        self.assertNotIn("new-secret-token", str(caught.exception.detail))

    def test_concurrent_replacement_survives_old_token_401(self):
        m = self.m
        started, release = threading.Event(), threading.Event()
        errors = []
        m._saas_tokens["config"] = ("old-token", m.now() + timedelta(hours=1))

        def post(_url, **_kwargs):
            started.set()
            self.assertTrue(release.wait(3))
            return self.call_response(401)

        with patch.object(m.httpx, "post", side_effect=post):
            def invoke():
                try:
                    self._call_expect_http_error()
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=invoke)
            thread.start()
            self.assertTrue(started.wait(3))
            with m._saas_tokens_lock:
                m._saas_tokens["config"] = ("replacement-token", m.now() + timedelta(hours=1))
            release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        self.assertEqual(m._saas_tokens["config"][0], "replacement-token")

    def _call_expect_http_error(self):
        with self.assertRaises(self.m.HTTPException):
            self.m.saas_call(self.config, "OPEN", {})

    def test_non_401_errors_preserve_cached_token(self):
        m = self.m
        for status in (403, 500):
            with self.subTest(status=status):
                m._saas_tokens["config"] = ("cached-token", m.now() + timedelta(hours=1))
                with patch.object(m.httpx, "post", return_value=self.call_response(status)) as post:
                    with self.assertRaises(m.HTTPException):
                        m.saas_call(self.config, "OPEN", {})
                self.assertEqual(post.call_count, 1)
                self.assertEqual(m._saas_tokens["config"][0], "cached-token")

        m._saas_tokens["config"] = ("cached-token", m.now() + timedelta(hours=1))
        with patch.object(m.httpx, "post", side_effect=httpx.TimeoutException("fixture timeout")) as post:
            with self.assertRaises(m.HTTPException):
                m.saas_call(self.config, "OPEN", {})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(m._saas_tokens["config"][0], "cached-token")

        self.config.auth_mode = "none"
        with patch.object(m.httpx, "post") as post:
            with self.assertRaises(m.HTTPException):
                m.saas_call(self.config, "OPEN", {})
        post.assert_not_called()
        self.assertEqual(m._saas_tokens["config"][0], "cached-token")

    def test_hmac_401_does_not_touch_oauth_cache(self):
        m = self.m
        m._saas_tokens["config"] = ("cached-token", m.now() + timedelta(hours=1))
        self.config.auth_mode = "hmac"
        with patch.object(m.httpx, "post", return_value=self.call_response(401)) as post:
            with self.assertRaises(m.HTTPException):
                m.saas_call(self.config, "OPEN", {})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(m._saas_tokens["config"][0], "cached-token")


if __name__ == "__main__":
    unittest.main()
