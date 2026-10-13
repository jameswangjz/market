import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    'password_first_purchase_fixture', Path(__file__).with_name('password_first_purchase_fixture.py'))
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class PrivateJsonTests(unittest.TestCase):
    def test_create_uses_private_permissions_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.json'
            fixture.private_json(path, {'password': 'test'}, create=True)
            self.assertEqual(json.loads(path.read_text()), {'password': 'test'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                fixture.private_json(path, {'password': 'replacement'}, create=True)
            self.assertEqual(json.loads(path.read_text()), {'password': 'test'})

    def test_update_replaces_complete_file_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.json'
            path.write_text('{"old":true}\n')
            fixture.private_json(path, {'new': 'complete'})
            self.assertEqual(json.loads(path.read_text()), {'new': 'complete'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_failed_replace_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.json'
            original = '{"old":true}\n'
            path.write_text(original)
            with patch.object(fixture.os, 'replace', side_effect=OSError('simulated')):
                with self.assertRaises(OSError):
                    fixture.private_json(path, {'new': 'complete'})
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
