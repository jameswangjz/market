import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
export function actualDiscoveryDtos() {
  const root = fileURLToPath(new URL('../../..', import.meta.url));
  return JSON.parse(execFileSync(process.env.REPORTING_PYTHON || '/tmp/settlement-reporting-tests/bin/python',
    [fileURLToPath(new URL('./cost-basis-http-dto.py', import.meta.url))], { cwd: root, encoding: 'utf8', timeout: 120000,
      maxBuffer: 5 * 1024 * 1024, env: { ...process.env, PYTHONWARNINGS: 'ignore', PYTHONPATH: `${root}/backend:${root}/backend/tests`,
        QUOTA_REDIS_SERVER: process.env.QUOTA_REDIS_SERVER || '/tmp/clearing-redis-tools/extracted/usr/bin/redis-server',
        LD_LIBRARY_PATH: process.env.LD_LIBRARY_PATH || '/tmp/clearing-redis-tools/extracted/usr/lib/x86_64-linux-gnu' } }));
}
