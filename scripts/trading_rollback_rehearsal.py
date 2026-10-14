#!/usr/bin/env python3
"""Stream backup restores into an isolated disposable PostgreSQL container."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def rehearse(backup, image='postgres:16-alpine', engine=None, compatibility_hook=None):
    engine = engine or ['nerdctl', '--namespace', 'k8s.io']
    if not isinstance(engine, list) or not engine or not all(isinstance(v, str) and v for v in engine):
        raise ValueError('engine must be an explicit nonempty argv list')
    if not re.fullmatch(r'(?:[a-zA-Z0-9.:-]+/)*postgres(?::[a-zA-Z0-9_.-]+|@sha256:[0-9a-f]{64})', image):
        raise ValueError('explicit PostgreSQL tag or digest required')
    with backup.open('rb') as stream:
        header = stream.read(5)
    if not header or header == b'PGDMP':
        raise ValueError('nonempty plain SQL pg_dump required')
    backup_hash = file_hash(backup)
    name = 'market-rollback-' + uuid.uuid4().hex
    def command(*args, **kwargs):
        return subprocess.run([*engine, *args], check=True, **kwargs)
    command('image', 'inspect', image, stdout=subprocess.DEVNULL)
    started = False
    cleanup_verified = False
    hook_result = 'not run'
    try:
        command('run', '-d', '--pull=never', '--name', name, '--network=none',
                '--label', 'market.isolated-rollback=true', '--memory=1g', '--cpus=1',
                '--pids-limit=256', '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', image,
                stdout=subprocess.DEVNULL)
        started = True
        for _ in range(60):
            try:
                command('exec', name, 'pg_isready', '-U', 'postgres', stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
                break
            except subprocess.CalledProcessError:
                time.sleep(0.5)
        else:
            raise ValueError('isolated PostgreSQL did not become ready')
        command('exec', name, 'createuser', '-U', 'postgres', 'market')
        dumps = []
        for _ in range(2):
            command('exec', name, 'createdb', '-U', 'postgres', 'market')
            with backup.open('rb') as stream:
                command('exec', '-i', name, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                        '-U', 'postgres', '-d', 'market', stdin=stream, stdout=subprocess.DEVNULL)
            with tempfile.TemporaryFile() as snapshot:
                command('exec', name, 'pg_dump', '-U', 'postgres', '-d', 'market', stdout=snapshot)
                snapshot.seek(0)
                checksum = hashlib.sha256()
                # Normalize only pg_dump's random psql restriction tokens.
                for line in snapshot:
                    if not line.startswith((b'\\restrict ', b'\\unrestrict ')):
                        checksum.update(line)
                dumps.append(checksum.hexdigest())
            if compatibility_hook:
                # The caller owns the old/new application probes; only disposable target details are supplied.
                env = dict(os.environ, REHEARSAL_CONTAINER=name, REHEARSAL_DATABASE='market',
                           REHEARSAL_ENGINE_ARGV=json.dumps(engine))
                subprocess.run(compatibility_hook, env=env, check=True)
                hook_result = 'hook exited successfully; business assertions are caller-owned'
            command('exec', name, 'dropdb', '-U', 'postgres', 'market')
        if dumps[0] != dumps[1] or file_hash(backup) != backup_hash:
            raise ValueError('restored snapshots differ or backup changed')
    finally:
        # Attempt cleanup even if run partially succeeded before returning an error.
        removed = subprocess.run([*engine, 'rm', '-f', '-v', name], check=False,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        listing = command('ps', '-a', '--format', '{{.Names}}', stdout=subprocess.PIPE)
        cleanup_verified = name not in listing.stdout.decode().splitlines()
        if not cleanup_verified or (started and removed.returncode != 0):
            raise ValueError('isolated container cleanup could not be verified')
    return {'backup_sha256': backup_hash, 'restored_snapshot_sha256': dumps[0],
            'restores': 2, 'cleanup_verified': cleanup_verified, 'compatibility_hook': hook_result,
            'scope': 'isolated backup restore; application compatibility requires hook assertions'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--image', default='postgres:16-alpine')
    parser.add_argument('--engine-argv', default='["nerdctl", "--namespace", "k8s.io"]',
                        help='JSON argv, e.g. ["docker"]')
    parser.add_argument('--compatibility-hook-argv', help='JSON argv for caller-owned old/new app probes')
    args = parser.parse_args()
    try:
        hook = json.loads(args.compatibility_hook_argv) if args.compatibility_hook_argv else None
        if hook is not None and (not isinstance(hook, list) or not hook or
                                 not all(isinstance(v, str) and v for v in hook)):
            raise ValueError('hook must be explicit argv')
        print(json.dumps(rehearse(args.backup, args.image, json.loads(args.engine_argv), hook)))
    except (OSError, ValueError, subprocess.CalledProcessError):
        parser.exit(1, 'Isolated rehearsal failed; no successful restore claim.\n')


if __name__ == '__main__':
    main()
