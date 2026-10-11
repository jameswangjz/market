"""Run isolated real Redis cutover tests and emit machine-readable evidence.

QUOTA_REDIS_SERVER must point to redis-server if it is not on PATH. No business
Redis URL or database configuration is read. Every Redis server is ephemeral.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


def run():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'backend/tests'))
    import test_quota_cutover

    binary = os.environ.get('QUOTA_REDIS_SERVER') or shutil.which('redis-server')
    version = subprocess.run([binary, '--version'], capture_output=True, text=True,
                             check=True, timeout=5).stdout.strip() if binary else None
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(test_quota_cutover.QuotaCutoverTests)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    passed = result.wasSuccessful() and not result.skipped and result.testsRun > 0
    report = dict(status='passed' if passed else 'failed', redis_version=version,
                  tests_run=result.testsRun, skipped=len(result.skipped),
                  failures=len(result.failures), errors=len(result.errors),
                  evidence_scope='Isolated Redis plus gateway Lua; cluster-enabled single-node refusal',
                  business_redis_accessed=False, historical_business_migration_completed=False)
    print(json.dumps(report, sort_keys=True))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(run())
