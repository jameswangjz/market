"""Bounded Redis SET/GET microbenchmark, not production capacity or platform impact."""
import argparse
import json
import math
import os
from pathlib import Path
import secrets
import time
import uuid


PREFIX = 'market:qa:redis-persistence-perf:'
WARMUP = 100


def sample_count(value):
    count = int(value)
    if not 1 <= count <= 5000:
        raise ValueError('samples must be between 1 and 5000')
    return count


def latency_stats(values):
    ordered = sorted(values)
    return dict(unit='ns', percentile_method='nearest_rank',
                p50=ordered[math.ceil(len(ordered) * .50) - 1],
                p95=ordered[math.ceil(len(ordered) * .95) - 1],
                p99=ordered[math.ceil(len(ordered) * .99) - 1],
                mean=sum(ordered) / len(ordered))


def benchmark(client, label, samples=1000, *, clock=time.perf_counter_ns):
    samples = sample_count(samples)
    if label not in ('before', 'after'):
        raise ValueError('invalid label')
    key = PREFIX + str(uuid.uuid4())
    payload = secrets.token_hex(32)
    result = dict(schema='redis_persistence_microbenchmark_v1', label=label,
                  scope=__doc__, samples=samples, warmup_pairs=WARMUP,
                  payload_bytes=64, ttl_seconds=600, status='failed')

    def matches(value):
        return value == payload or value == payload.encode('ascii')

    def pair():
        if not client.set(key, payload, ex=600):
            raise RuntimeError('benchmark SET failed')
        if not matches(client.get(key)):
            raise RuntimeError('benchmark GET mismatch')

    try:
        server = client.info('server')
        persistence = client.info('persistence')
        result['redis_version'] = server['redis_version']
        result['aof_enabled'] = bool(int(persistence['aof_enabled']))
        for _ in range(WARMUP):
            pair()
        timings = []
        start = clock()
        for _ in range(samples):
            before = clock()
            pair()
            timings.append(clock() - before)
        elapsed = clock() - start
        result.update(status='passed', pair_latency=latency_stats(timings),
                      elapsed_ns=elapsed, measured_pairs=samples,
                      measured_operations=2 * samples,
                      operations_per_second=(2 * samples * 1e9 / elapsed if elapsed else None))
    except Exception as exc:
        result['error_type'] = type(exc).__name__
    finally:
        try:
            if matches(client.get(key)):
                result['cleanup'] = 'deleted' if client.delete(key) == 1 else 'already_absent'
            else:
                result['cleanup'] = 'retained_or_absent'
                result['status'] = 'failed'
        except Exception as exc:
            result.update(status='failed', cleanup='failed', cleanup_error_type=type(exc).__name__)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', choices=('before', 'after'), required=True)
    parser.add_argument('--samples', type=sample_count, default=1000)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
        from app import main as m
        from app.api_charge_ledger import bounded_client
        client = bounded_client(vars(m))
        try:
            result = benchmark(client, args.label, args.samples)
        finally:
            client.close()
        json.dump(result, output, indent=2, allow_nan=False)
        output.write('\n')
    print(json.dumps(result, allow_nan=False))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
