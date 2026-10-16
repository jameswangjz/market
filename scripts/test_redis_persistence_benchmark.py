import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import redis_persistence_benchmark as perf


class FakeRedis:
    def __init__(self):
        self.calls = []
        self.value = None
        self.cleanup_failure = False
        self.mismatch = False

    def info(self, section):
        return {'redis_version': '7.2.0', 'run_id': 'must-not-appear'} if section == 'server' else {'aof_enabled': 1}

    def set(self, key, value, *, ex):
        self.calls.append(('set', key, value, ex))
        self.value = value
        return True

    def get(self, key):
        self.calls.append(('get', key))
        if self.cleanup_failure:
            raise ConnectionError('secret connection details')
        return 'foreign' if self.mismatch else self.value.encode('ascii')

    def delete(self, key):
        self.calls.append(('delete', key))
        self.value = None
        return 1


class BenchmarkTests(unittest.TestCase):
    def test_bounded_pairs_stats_and_exact_cleanup(self):
        client = FakeRedis()
        ticks = itertools.count(0, 100)
        result = perf.benchmark(client, 'before', 10, clock=lambda: next(ticks))
        self.assertEqual('passed', result['status'])
        self.assertEqual(20, result['measured_operations'])
        self.assertEqual(2100, result['elapsed_ns'])
        self.assertEqual(100, result['pair_latency']['p99'])
        self.assertEqual(100, result['pair_latency']['mean'])
        self.assertEqual(110, sum(c[0] == 'set' for c in client.calls))
        self.assertEqual(111, sum(c[0] == 'get' for c in client.calls))
        keys = {c[1] for c in client.calls}
        self.assertEqual(1, len(keys))
        self.assertTrue(next(iter(keys)).startswith(perf.PREFIX))
        self.assertTrue(all(len(c[2]) == 64 and c[3] == 600 for c in client.calls if c[0] == 'set'))
        self.assertEqual(('delete', next(iter(keys))), client.calls[-1])
        self.assertEqual('7.2.0', result['redis_version'])
        self.assertTrue(result['aof_enabled'])
        self.assertNotIn('must-not-appear', str(result))

    def test_nearest_rank(self):
        stats = perf.latency_stats(list(range(1, 101)))
        self.assertEqual((50, 95, 99, 50.5), (stats['p50'], stats['p95'], stats['p99'], stats['mean']))

    def test_mismatch_never_deletes_foreign_value(self):
        client = FakeRedis()
        client.mismatch = True
        result = perf.benchmark(client, 'after', 1)
        self.assertEqual('failed', result['status'])
        self.assertFalse(any(c[0] == 'delete' for c in client.calls))

    def test_cleanup_read_failure_never_deletes(self):
        client = FakeRedis()
        client.cleanup_failure = True
        result = perf.benchmark(client, 'after', 1)
        self.assertEqual('failed', result['cleanup'])
        self.assertNotIn('secret', str(result))
        self.assertFalse(any(c[0] == 'delete' for c in client.calls))

    def test_cleanup_delete_failure_is_reported(self):
        client = FakeRedis()
        with patch.object(client, 'delete', side_effect=ConnectionError('secret')):
            result = perf.benchmark(client, 'after', 1)
        self.assertEqual('failed', result['status'])
        self.assertEqual('failed', result['cleanup'])
        self.assertNotIn('secret', str(result))

    def test_measurement_failure_still_cleans_owned_payload(self):
        client = FakeRedis()
        original = client.set
        def write(key, value, *, ex):
            if client.calls:
                raise ConnectionError('secret')
            return original(key, value, ex=ex)
        with patch.object(client, 'set', side_effect=write):
            result = perf.benchmark(client, 'before', 1)
        self.assertEqual('failed', result['status'])
        self.assertEqual('deleted', result['cleanup'])

    def test_sample_bounds_before_redis_access(self):
        for value in (0, -1, 5001):
            client = FakeRedis()
            with self.assertRaises(ValueError):
                perf.benchmark(client, 'before', value)
            self.assertEqual([], client.calls)

    def test_existing_output_refused_before_app_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            path.touch()
            with self.assertRaises(FileExistsError):
                perf.main(['--label', 'before', '--output', str(path)])


if __name__ == '__main__':
    unittest.main()
