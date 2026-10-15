"""Offline package integrity and ownership guards; no PostgreSQL/SSH calls."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zlib

import r3_backend_acceptance_concurrency as probe


class ConcurrencyProbeGuards(unittest.TestCase):
    def package(self, files):
        return base64.b64encode(zlib.compress(json.dumps(files).encode())).decode()

    def test_schema_cleanup_is_owned_only(self):
        self.assertTrue(probe.owned_schema('qa_r3_conc_0123456789abcdef'))
        for name in ('public', 'qa_r3_conc_', 'qa_r3_conc_0123456789abcdef;public'):
            self.assertFalse(probe.owned_schema(name))

    def test_valid_package_roundtrip(self):
        files = {'main.py': 'x = 1\n'}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            probe.unpack_workingtree(self.package(files),
                {'main.py': hashlib.sha256(files['main.py'].encode()).hexdigest()}, target)
            self.assertEqual((target/'main.py').read_text(), files['main.py'])

    def test_unsafe_path_and_tampering_rejected(self):
        for files, manifest in [({'../main.py': 'x'}, {'../main.py': hashlib.sha256(b'x').hexdigest()}),
                                ({'main.py': 'x'}, {'main.py': '0'*64}),
                                ({'main.py': 'x'}, {})]:
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(AssertionError):
                probe.unpack_workingtree(self.package(files), manifest, Path(directory))

    def test_errors_never_print_sensitive_messages(self):
        self.assertNotIn('PRIVATE_SENTINEL', str(probe.safe_error(ValueError('PRIVATE_SENTINEL'))))


if __name__ == '__main__':
    unittest.main()
