"""Offline BE010 PG guards; no app import, SSH or database connection."""
import ast
import hashlib
from pathlib import Path
import tempfile
import unittest

import r3_refund_workflow_pg_acceptance as probe


class RefundPGGuards(unittest.TestCase):
    def test_owned_schema_only(self):
        self.assertTrue(probe.base.owned_schema('qa_r3_be_0123456789abcdef'))
        for value in ('public','qa_r3_be_','qa_r3_be_0123456789abcdef;public'):
            self.assertFalse(probe.base.owned_schema(value))

    def test_fixture_order_payment_ids_fit_pg_columns(self):
        keys=[]
        for case in probe.CASES:
            oid,number=probe.fixture.owned_order_identity('refund-'+case)
            self.assertLessEqual(len(oid),36);self.assertLessEqual(len(number),40)
            self.assertLessEqual(len('owned-pay-'+case),36)
            keys.append(oid)
        self.assertEqual(len(set(keys)),8)

    def test_fixture_approval_is_explicit_manual_not_fabricated_usage(self):
        source=Path(probe.__file__).read_text()
        self.assertIn("basis='manual'",source)
        self.assertIn("'fixture_only':True",source)
        self.assertNotIn("basis='calls'",source)
        self.assertIn('No BE011 gateway-charge computation',source)
        ast.parse(source)

    def test_workingtree_integrity_guard_cannot_be_bypassed(self):
        import base64,json,zlib
        files={'main.py':'# owned\n'}
        encoded=base64.b64encode(zlib.compress(json.dumps(files).encode())).decode()
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(AssertionError):
            probe.transport.unpack_workingtree(encoded,{'main.py':'0'*64},Path(directory))

    def test_error_messages_do_not_leak_secret(self):
        self.assertNotIn('PRIVATE_SENTINEL',str(probe.base.safe_error(ValueError('PRIVATE_SENTINEL'))))


if __name__=='__main__':
    unittest.main()
