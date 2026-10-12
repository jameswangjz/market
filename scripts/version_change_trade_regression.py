"""Observe real API/model worker changes before isolated monthly settlement.

Public writes are restricted to the existing exact-owned acceptance fixture.
Monthly batch writes run only in the snapshot harness's process-created schema.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

import version_change_saga_runtime_acceptance as runtime


def run(path):
    from fastapi.testclient import TestClient
    m = runtime.load_main()
    manifest = runtime.fixture_ids()
    manifest['environment'] = runtime.environment_fingerprint(m)
    runtime.preflight(m, manifest)
    manifest['authorized'] = True
    runtime.write_manifest(path, manifest, create=True)
    client = TestClient(m.app)
    try:
        tokens = runtime.prepare(m, manifest)
        migrations = runtime.exercise(m, manifest, path, manifest, client, tokens)
        monthly_path = Path(str(path) + '.monthly')
        monthly = {
            'source_order_ids': list(manifest['database_ids']['orders'].values()),
            'change_ids': list(manifest['database_ids']['changes'].values()) +
                          list(manifest['database_ids']['downgrades'].values()),
            'finance_user_id': manifest['users']['finance'],
        }
        with os.fdopen(os.open(monthly_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                               0o600), 'w') as stream:
            json.dump(monthly, stream)
        result = subprocess.run([sys.executable,
            str(Path(__file__).with_name('version_change_trade_monthly_acceptance.py')),
            '--manifest', str(monthly_path)], capture_output=True, text=True)
        runtime.check(result.returncode == 0, 'Actual-trade monthly snapshot checks failed: ' +
                      result.stdout[-1000:])
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        runtime.check(evidence.get('status') == 'passed', 'Monthly evidence did not pass')
        runtime.cleanup(m, manifest, path)
        return {'status': 'passed', 'scope': 'actual API/model worker trades to isolated monthly HTTP',
                'migrations': migrations, 'monthly': evidence,
                'cleanup': 'exact owned live fixtures and external keys removed; protected order unchanged',
                'not_tested': ['real bank transfers', 'browser transactions', 'cross-month adjustments']}
    finally:
        client.close()


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.manifest), sort_keys=True, default=str))
    except runtime.AcceptanceFailure as exc:
        print(json.dumps({'status': 'failed', 'check': str(exc)}))
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'status': 'failed'} | runtime.safe_exception_metadata(exc)))
        raise SystemExit(1)
