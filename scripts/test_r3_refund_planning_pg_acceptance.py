"""Offline BE011 incremental probe guards; no PostgreSQL/SSH/app import."""
import ast
from pathlib import Path
import unittest
from unittest.mock import patch

import r3_refund_planning_pg_acceptance as probe


class PlanningPGGuards(unittest.TestCase):
    def test_owned_schema_guard(self):
        self.assertTrue(probe.prior.base.owned_schema('qa_r3_be_0123456789abcdef'))
        for name in ('public','qa_r3_be_','qa_r3_be_0123456789abcdef,public'):
            self.assertFalse(probe.prior.base.owned_schema(name))

    def test_fixture_keys_fit_real_columns(self):
        keys=[probe.oid(name) for name in probe.CASES]
        self.assertEqual(len(set(keys)),6)
        self.assertTrue(all(len(key)<=36 for key in keys))
        self.assertTrue(all(len('owned-plan-pay-'+name)<=36 for name in probe.CASES))

    def test_increment_replaces_not_repeats_previous_case_group(self):
        with patch.object(probe.prior,'remote_run',return_value={'status':'passed'}) as run:
            hashes={name:'0'*64 for name in probe.SOURCE_FILES}
            result=probe.remote_run('not-unpacked-in-unit-test',hashes)
            run.assert_called_once()
            self.assertEqual(result['coverage'],['TRD-R3-BE-011'])
            self.assertIn('no repeated BE010',result['scope'])

    def test_no_fabricated_charge_adapter_or_approval_plan(self):
        source=Path(probe.__file__).read_text()
        self.assertNotIn('prepare_refund_plan=',source)
        self.assertNotIn('refund_charge_summary=',source)
        self.assertIn("'gateway_usage_adapter_used':False",source)
        ast.parse(source)

    def test_sensitive_exception_text_not_in_public_evidence(self):
        self.assertNotIn('PRIVATE_SENTINEL',str(probe.prior.base.safe_error(ValueError('PRIVATE_SENTINEL'))))


if __name__=='__main__':
    unittest.main()
