"""Reject incomplete live proof before updating defect task states."""
import copy
import unittest

from fault_fixes_checkpoint import validate


class FaultFixProofTests(unittest.TestCase):
    def setUp(self):
        native = {'revoked_token_next_call_after_auto_eviction': 200,
                  'atomic_new_token_preserved': True, 'atomic_exact_token_deleted': True}
        self.proof = {'status': 'passed',
            'scope': 'OAuth cache 401 recovery and isolated PostgreSQL startup fixes',
            'lua_harness': 'passed', 'cleanup_verified': True,
            'saas': {'expired_401_cache_auto_evicted': True,
                     'expired_signed_warm_token_next_attempt_recovered': True,
                     'individually_revoked_warm_token_next_attempt_recovered': True},
            'native_gateway': {'api': dict(native), 'model': dict(native)},
            'startup': {'valid_buyer_foreign_key': True,
                        'repeat_core_rows_unchanged': True, 'platform_memberships': 0}}

    def test_complete_proof_is_accepted(self):
        validate(self.proof)

    def test_missing_recovery_cleanup_or_concurrency_proof_is_rejected(self):
        edits = [lambda value: value.update(cleanup_verified=False),
            lambda value: value['saas'].update(expired_401_cache_auto_evicted=False),
            lambda value: value['native_gateway']['api'].update(revoked_token_next_call_after_auto_eviction=401),
            lambda value: value['native_gateway']['model'].update(atomic_new_token_preserved=False),
            lambda value: value['startup'].update(repeat_core_rows_unchanged=False),
            lambda value: value['startup'].update(platform_memberships=1)]
        for edit in edits:
            with self.subTest(edit=edit):
                proof = copy.deepcopy(self.proof)
                edit(proof)
                with self.assertRaises(ValueError):
                    validate(proof)


if __name__ == '__main__':
    unittest.main()
