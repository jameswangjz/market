"""Offline ownership/source guards; never imports app.main or connects to PG."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import r3_sla_evidence_pg_acceptance as probe


class EvidenceProbeGuards(unittest.TestCase):
    def test_owned_schema_guard_rejects_public_and_injected_names(self):
        self.assertTrue(probe.base.owned_schema('qa_r3_be_0123456789abcdef'))
        for name in ('public','qa_r3_be_','qa_r3_be_0123456789abcdef,public'):
            self.assertFalse(probe.base.owned_schema(name))

    def test_source_match_and_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            expected={}
            for name in probe.SOURCE_FILES:
                (root/name).write_text('# owned\n')
                expected[name]=hashlib.sha256(b'# owned\n').hexdigest()
            self.assertEqual(probe.verify_sources(root,expected),expected)
            (root/'sla_evidence.py').write_text('# changed\n')
            with self.assertRaises(probe.base.AcceptanceFailure):
                probe.verify_sources(root,expected)

    def test_missing_extra_and_symlink_sources_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(probe.base.AcceptanceFailure):
                probe.verify_sources(root,{})
            for name in probe.SOURCE_FILES:
                (root/name).write_text('# owned\n')
            expected={n:hashlib.sha256(b'# owned\n').hexdigest() for n in probe.SOURCE_FILES}
            with self.assertRaises(probe.base.AcceptanceFailure):
                probe.verify_sources(root,{**expected,'unexpected.py':'0'*64})
            (root/'main.py').unlink();(root/'main.py').symlink_to(root/'sla_evidence.py')
            with self.assertRaises(probe.base.AcceptanceFailure):
                probe.verify_sources(root,expected)

    def test_source_guard_aborts_before_pg_harness(self):
        from types import SimpleNamespace
        with patch.object(probe.importlib.util,'find_spec',return_value=SimpleNamespace(origin='/missing/app/__init__.py')), \
                patch.object(probe.base,'run') as run:
            with self.assertRaises(probe.base.AcceptanceFailure):
                probe.run({})
            run.assert_not_called()

    def test_errors_do_not_echo_private_exception(self):
        self.assertNotIn('PRIVATE_SENTINEL',str(probe.base.safe_error(ValueError('PRIVATE_SENTINEL'))))


if __name__=='__main__':
    unittest.main()
