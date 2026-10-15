"""Offline guards for the bounded owned-schema ledger increment."""
import unittest
from unittest.mock import patch
import r3_refund_ledger_pg_acceptance as probe


class Guards(unittest.TestCase):
    def test_schema_guard(self):
        self.assertFalse(probe.planning.prior.base.owned_schema('public'))
        self.assertTrue(probe.planning.prior.base.owned_schema('qa_r3_be_0123456789abcdef'))

    def test_private_stream_is_bounded_and_replayable(self):
        redis = probe.MockRedis('owned', [('1-0', {}), ('2-0', {})])
        self.assertEqual(redis.xread({}, 1), [('owned', [('1-0', {})])])
        self.assertEqual(redis.xread({}, 1), redis.xread({}, 1))
        self.assertEqual(redis.info('server'), {'run_id': 'a' * 40})
        with self.assertRaises(ValueError):
            redis.info('all')
        self.assertIsNone(redis.hget('owned-epoch', 'run_id'))
        self.assertEqual(redis.hsetnx('owned-epoch', 'run_id', 'a' * 40), 1)
        self.assertEqual(redis.hsetnx('owned-epoch', 'run_id', 'b' * 40), 0)
        self.assertEqual(redis.hget('owned-epoch', 'run_id'), 'a' * 40)

    def test_wrapper_installs_only_increment(self):
        def run(encoded, manifest):
            self.assertIs(probe.planning.prior.exercise, probe.exercise)
            self.assertIs(probe.planning.prior.seed, probe.planning.seed)
            return dict(source_sha256={})
        with patch.object(probe.planning.prior, 'remote_run', side_effect=run):
            result = probe.remote_run('', {'api_charge_ledger.py': 'owned-digest'})
        self.assertEqual(result['source_sha256']['api_charge_ledger.py'], 'owned-digest')


if __name__ == '__main__':
    unittest.main()
