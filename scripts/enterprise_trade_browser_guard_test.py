"""Offline ownership/privacy guards; no backend imports or network."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import enterprise_trade_browser_fixture as bridge


class Guards(unittest.TestCase):
    def test_saas_public_origin_is_explicit_and_configured(self):
        import os
        from unittest.mock import patch
        g = SimpleNamespace(OIDC_REDIRECT='unused')
        with self.assertRaises(ValueError):
            bridge.configure_saas(g, None)
        with patch.dict(os.environ, {}, clear=False):
            bridge.configure_saas(g, 'http://fixture.invalid:30083')
            self.assertEqual(g.OIDC_REDIRECT, 'http://fixture.invalid:30083/sso/callback')
            self.assertEqual(os.environ['MOCK_SAAS_PUBLIC_BASE_URL'], 'http://fixture.invalid:30083')

    def test_origin_rejects_credentials_and_paths(self):
        for value in ('http://user:secret@host', 'http://host/api', 'http://host?x=1', 'file:///tmp/a'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bridge.origin(value)

    def test_private_read_rejects_symlink_and_public_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'browser.json'
            path.write_text(json.dumps({'password': 'fixture-only'}))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                bridge.read_private(path)
            path.chmod(0o600)
            self.assertEqual(bridge.read_private(path)['password'], 'fixture-only')
            link = Path(tmp) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                bridge.read_private(link)

    def test_order_guard_rejects_every_ownership_boundary(self):
        fixture = {'product_id': 'owned-p', 'product_version_id': 'owned-v', 'enterprise_id': 'owned-e',
                   'actors': {'super': {'user_id': 'owned-u'}}}
        values = dict(product_id='owned-p', product_version_id='owned-v', buyer_enterprise_id='owned-e',
                      buyer_user_id='owned-u', id='a'*32, order_no='owned-no')
        bridge.owned_order(SimpleNamespace(**values), fixture)
        for key, value in [('product_id', bridge.PROTECTED_PRODUCT), ('product_version_id', 'foreign'),
                           ('buyer_enterprise_id', 'foreign'), ('buyer_user_id', 'foreign'),
                           ('id', 'ORD-261012005144E1B3'), ('order_no', 'ORD-261012005144E1B3')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                bridge.owned_order(SimpleNamespace(**(values | {key: value})), fixture)

    def test_directory_requires_private_real_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            path.chmod(0o755)
            with self.assertRaises(ValueError):
                bridge.directory(path)
            path.chmod(0o700)
            self.assertEqual(bridge.directory(path), path)


if __name__ == '__main__':
    unittest.main()
