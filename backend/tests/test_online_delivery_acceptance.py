"""Safety checks for acceptance manifests; no database or storage connection."""
from io import BytesIO
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import online_delivery_acceptance as acceptance


class AcceptanceManifestTests(unittest.TestCase):
    def test_archive_is_readable_small_fixture(self):
        payload = acceptance.archive()
        self.assertLess(len(payload), 1024)
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            self.assertEqual(archive.namelist(), ['fixture.txt'])
            self.assertIn(b'acceptance fixture', archive.read('fixture.txt'))

    def test_manifest_is_private_and_refuses_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'scope.json'
            ids = acceptance.fixture_ids('qa-online-012345abcdef')
            acceptance.write_manifest(path, ids, create=True)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), ids)
            with self.assertRaises(FileExistsError):
                acceptance.write_manifest(path, {}, create=True)
            self.assertEqual(json.loads(path.read_text()), ids)

    def test_manifest_replace_preserves_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'scope.json'
            acceptance.write_manifest(path, {'before': 1}, create=True)
            acceptance.write_manifest(path, {'after': 2})
            self.assertEqual(json.loads(path.read_text()), {'after': 2})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_symlink_is_never_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            target, link = Path(directory)/'target.json', Path(directory)/'scope.json'
            target.write_text('unchanged')
            link.symlink_to(target)
            with self.assertRaises(OSError):
                acceptance.write_manifest(link, {}, create=True)
            with self.assertRaises(acceptance.AcceptanceFailure):
                acceptance.write_manifest(link, {})
            self.assertEqual(target.read_text(), 'unchanged')

    def test_changed_scope_never_imports_database_for_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'scope.json'
            ids = acceptance.fixture_ids('qa-online-012345abcdef')
            ids['products'] = ['somebody-elses-product']
            acceptance.write_manifest(path, ids, create=True)
            with patch.object(acceptance, 'load_main') as load:
                with self.assertRaises(acceptance.AcceptanceFailure):
                    acceptance.cleanup_saved(path)
                load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
