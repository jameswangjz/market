#!/usr/bin/env python3
"""OPS-002: restored-copy observations only; no migration or production rollback."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time
import uuid

from trading_rollback_rehearsal import file_hash


LABEL = 'market.ops002.owner'
TYPES = ('report', 'application', 'data_report', 'data_application')
ECONOMIC = ('orders', 'payments', 'refunds', 'wallets', 'wallet_transactions',
            'fund_accounts', 'fund_transactions', 'settlements', 'ledger_entries',
            'settlement_batches', 'settlement_lines', 'settlement_measurements',
            'settlement_reconciliations', 'settlement_corrections',
            'settlement_adjustments', 'settlement_adjustment_proposals',
            'api_financial_charges', 'ordinary_refund_executions',
            'ordinary_refund_payment_legs', 'ordinary_refund_local_completions',
            'refund_funding_allocations', 'version_change_funds',
            'version_change_adjustments', 'ordinary_refund_settlement_links',
            'version_change_settlement_links')


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def rehearse(backup, image='postgres:16-alpine', timeout=120, engine=None):
    engine = ['nerdctl', '--namespace', 'k8s.io'] if engine is None else engine
    if not isinstance(engine, list) or not engine or not all(isinstance(v, str) and v for v in engine):
        raise ValueError('engine must be an explicit nonempty argv list')
    backup = Path(backup).resolve(strict=True)
    if not 1 <= timeout <= 600:
        raise ValueError('command timeout must be 1..600 seconds')
    if not re.fullmatch(r'(?:[a-zA-Z0-9.:-]+/)*postgres(?::[a-zA-Z0-9_.-]+|@sha256:[0-9a-f]{64})', image):
        raise ValueError('explicit PostgreSQL image tag or digest required')
    with backup.open('rb') as stream:
        header = stream.read(5)
    if not header or header == b'PGDMP':
        raise ValueError('nonempty plain SQL pg_dump required')
    digest = file_hash(backup)
    token = uuid.uuid4().hex
    name = 'market-ops002-' + token

    def command(*args, **kwargs):
        return subprocess.run([*engine, *args], check=True, timeout=timeout,
                              stderr=subprocess.PIPE, **kwargs)

    def inspect(require_isolation=True):
        result = command('inspect', name, stdout=subprocess.PIPE)
        info = json.loads(result.stdout)[0]
        if info['Config'].get('Labels', {}).get(LABEL) != token:
            raise ValueError('container ownership mismatch; refusing access/removal')
        if require_isolation:
            host = info.get('HostConfig', {})
            if 'NetworkMode' in host:
                isolated = host['NetworkMode'] == 'none'
            else:
                labels = info['Config'].get('Labels', {})
                network = info.get('NetworkSettings', {})
                try:
                    networks = json.loads(labels.get('nerdctl/networks', 'null'))
                except (ValueError, TypeError):
                    networks = None
                isolated = (networks == ['none'] and
                            network.get('Networks') == {} and
                            network.get('IPAddress') == '' and
                            not network.get('Ports') and not host.get('PortBindings'))
            if not isolated:
                raise ValueError('container isolation mismatch')
        return info

    def sql(query):
        inspect()
        result = command('exec', '-i', name, 'psql', '-X', '-qAt',
                         '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'market',
                         input=("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
                                "SET LOCAL statement_timeout='4000ms';\n" + query +
                                '\nROLLBACK;\n').encode(), stdout=subprocess.PIPE)
        return [json.loads(line) for line in result.stdout.splitlines() if line]

    def snapshot():
        # Discover actual tables/columns; absent economic tables are explicit gaps.
        schema = sql("SELECT json_build_object('table',table_name,'column',column_name,"
                     "'numeric',data_type IN ('numeric','integer','bigint','smallint',"
                     "'double precision','real')) FROM information_schema.columns "
                     "WHERE table_schema='public' ORDER BY table_name,ordinal_position;")
        tables = {}
        for row in schema:
            tables.setdefault(row['table'], []).append(row)
        queries = []
        for table in ECONOMIC:
            if table not in tables:
                continue
            sums = ["'" + row['column'].replace("'", "''") + "',SUM(" + quote(row['column']) + ')::text'
                    for row in tables[table] if row['numeric'] and
                    re.search(r'amount|balance|cost|price|fee|total', row['column'])]
            queries.append("SELECT json_build_object('table','" + table +
                           "','rows',COUNT(*),'numeric_sums',json_build_object(" +
                           ','.join(sums) + "),'row_content_md5',md5(COALESCE("
                           "string_agg(md5(to_jsonb(t)::text),'' ORDER BY "
                           "md5(to_jsonb(t)::text) COLLATE \"C\"),''))) FROM public." + quote(table) + ' t;')
        gaps = []
        for table, alias, status in (('products', 'p', 'status'),
                                     ('product_release_versions', 'v', 'status'),
                                     ('orders', 'o', 'main_status'),
                                     ('file_objects', 'f', 'status')):
            required = {row['column'] for row in tables.get(table, [])}
            if 'products' not in tables or not {'product_type'} <= {r['column'] for r in tables['products']}:
                raise ValueError('products.product_type absent; inventory unavailable')
            if status not in required or (table != 'products' and 'product_id' not in required):
                if table in ('products', 'orders'):
                    raise ValueError('required historical inventory schema incomplete: ' + table)
                gaps.append({'table': table, 'reason': 'table or required columns absent',
                             'required_columns': ['product_id', status]})
                continue
            source = 'public.products p' if table == 'products' else (
                'public.' + quote(table) + ' ' + alias + ' JOIN public.products p ON p.id=' + alias + '.product_id')
            queries.append("SELECT json_build_object('inventory','" + table +
                           "','type',p.product_type,'status'," + alias + '.' + quote(status) +
                           ",'rows',COUNT(*)) FROM " + source +
                           " WHERE p.product_type IN ('report','application','data_report','data_application')"
                           ' GROUP BY p.product_type,' + alias + '.' + quote(status) +
                           ' ORDER BY p.product_type,' + alias + '.' + quote(status) + ';')
        return {'observations': sql('\n'.join(queries)),
                'inventory_gaps': gaps,
                'row_hash_method': 'MD5 of sorted per-row MD5(to_jsonb(row)::text); duplicates retained; all columns',
                'missing_economic_tables': [t for t in ECONOMIC if t not in tables],
                'economic_columns': {t: [r['column'] for r in tables.get(t, [])]
                                     for t in ECONOMIC if t in tables}}

    command('image', 'inspect', image, stdout=subprocess.DEVNULL)
    cleanup_verified = False
    try:
        command('run', '-d', '--pull=never', '--name', name, '--network=none',
                '--label', LABEL + '=' + token, '--memory=1g', '--cpus=1',
                '--pids-limit=256', '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', image,
                stdout=subprocess.DEVNULL)
        inspect()
        for attempt in range(30):
            try:
                command('exec', name, 'pg_isready', '-U', 'postgres', stdout=subprocess.DEVNULL)
                break
            except subprocess.CalledProcessError:
                if attempt == 29:
                    raise ValueError('isolated PostgreSQL not ready')
                time.sleep(0.5)
        command('exec', name, 'createuser', '-U', 'postgres', 'market', stdout=subprocess.DEVNULL)
        command('exec', name, 'createdb', '-U', 'postgres', 'market', stdout=subprocess.DEVNULL)
        with backup.open('rb') as stream:
            command('exec', '-i', name, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                    '-U', 'postgres', '-d', 'market', stdin=stream, stdout=subprocess.DEVNULL)
        before = snapshot()
        after = snapshot()
        if before != after or digest != file_hash(backup):
            raise ValueError('restored observations or backup changed')
    finally:
        # run may have created the container even when the client timed out.
        listing = command('ps', '-a', '--format', '{{.Names}}', stdout=subprocess.PIPE)
        if name in listing.stdout.decode().splitlines():
            inspect(require_isolation=False)
            command('rm', '-f', '-v', name, stdout=subprocess.DEVNULL)
        listing = command('ps', '-a', '--format', '{{.Names}}', stdout=subprocess.PIPE)
        cleanup_verified = name not in listing.stdout.decode().splitlines()
        if not cleanup_verified:
            raise ValueError('cleanup not verified')
    return {'status': 'passed', 'restore_status': 'passed',
            'scope': 'restored backup only', 'production_rollback_verified': False,
            'synthetic': False, 'fixture_writes': False, 'historical_migration_executed': False,
            'captured_at': datetime.now(timezone.utc).isoformat(), 'backup_sha256': digest,
            'image': image, 'engine_argv': engine, 'network': 'none', 'cleanup_verified': cleanup_verified,
            'before': before, 'after': after, 'observations_equal': True,
            'limitations': 'Row hashes cover listed present economic tables only; MD5 is not an adversarial integrity proof. '
                           'No migration or application compatibility tested. '
                           'No live snapshot. File metadata only; version-only links excluded. '
                           'Current product type; no date/status/enterprise/fixture exclusions.',
            'approval_manifest': {'status': 'proposed_only', 'approved': False,
                                  'backup_sha256': digest, 'candidate_types': list(TYPES),
                                  'proposed_mapping': {kind: 'dataset' for kind in TYPES},
                                  'required_scope': 'Explicit user approval of IDs, fields, mapping, '
                                                    'target database and economic invariants before any migration.'}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--image', default='postgres:16-alpine')
    parser.add_argument('--command-timeout', type=int, default=120)
    parser.add_argument('--engine-argv', default='["nerdctl", "--namespace", "k8s.io"]',
                        help='Explicit JSON argv for the container engine')
    args = parser.parse_args()
    try:
        engine = json.loads(args.engine_argv)
        if engine is None:
            raise ValueError('engine argv must not be null')
        print(json.dumps(rehearse(args.backup, args.image, args.command_timeout,
                                  engine), sort_keys=True))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        parser.exit(1, 'OPS-002 failed; no successful restore or cleanup claim.\n')


if __name__ == '__main__':
    main()
