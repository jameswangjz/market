#!/usr/bin/env python3
"""Check new-then-old API reads against a restored, network-isolated database."""
import argparse
import contextlib
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

DATABASE_URL = 'postgresql+psycopg://market@127.0.0.1:5432/market'
PROTECTED_TABLES = ('orders', 'products', 'product_release_versions', 'payments',
                    'settlements', 'settlement_lines', 'settlement_batches',
                    'settlement_adjustment_proposals', 'audit_logs', 'users', 'enterprises', 'memberships')
FIXTURE_PREFIX = 'r2-rollback-'
FIXTURE_IDS = {
    'users': tuple(FIXTURE_PREFIX + name for name in ('finance', 'provider', 'buyer')),
    'enterprises': tuple(FIXTURE_PREFIX + name for name in ('provider-co', 'buyer-co')),
    'memberships': (FIXTURE_PREFIX + 'provider-member', FIXTURE_PREFIX + 'buyer-member'),
    'product_categories': (FIXTURE_PREFIX + 'category-root', FIXTURE_PREFIX + 'category-child'),
    'products': (FIXTURE_PREFIX + 'product', FIXTURE_PREFIX + 'foreign-product'),
    'orders': (FIXTURE_PREFIX + 'order', FIXTURE_PREFIX + 'foreign-order'),
    'settlements': (FIXTURE_PREFIX + 'settlement', FIXTURE_PREFIX + 'foreign-settlement'),
}
FINANCE_FIELDS = ('id', 'order_id', 'status', 'gross_amount', 'net_amount',
                  'cost_amount', 'profit_amount', 'provider_share')
PROVIDER_FIELDS = ('id', 'order_id', 'status', 'provider_share')
PROBE_FAILURES = {
    'restored business rows required': 'missing_business_rows',
    'restored finance and provider actors required': 'missing_scoped_actors',
    'owned restored settlements required; provide an isolated schema fixture': 'missing_owned_settlements',
    'nonempty business response required': 'empty_business_response',
    'business response shape mismatch': 'response_shape',
    'finance numeric shape mismatch': 'numeric_shape',
    'provider response escaped owned restored scope': 'provider_scope',
    'restored public category rows required': 'missing_categories',
    'read/auth HTTP status mismatch': 'http_status',
    'protected restored business fingerprint changed': 'business_data_changed',
    'isolated fixture seeding guard rejected': 'fixture_guard',
    'isolated fixture identity collision': 'fixture_collision',
    'original restored fingerprint changed': 'original_data_changed',
}


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     separators=(',', ':')).encode()).hexdigest()


def protected_fingerprint(engine, exclude_fixtures=False, columns=None):
    from sqlalchemy import MetaData, Table, inspect, select
    result = {}
    with engine.connect() as connection:
        names = columns if columns is not None else (*PROTECTED_TABLES, 'product_categories')
        for name in names:
            if name == 'product_categories' and columns is None and not inspect(connection).has_table(name, schema='public'):
                continue
            table = Table(name, MetaData(), schema='public', autoload_with=connection, resolve_fks=False)
            keys = list(table.primary_key.columns)
            if not keys:
                raise ValueError('protected table requires primary key')
            checksum = hashlib.sha256()
            count = 0
            selected = [table.c[key] for key in columns[name]] if columns is not None else list(table.c)
            query = select(*selected).order_by(*keys)
            if exclude_fixtures and name in FIXTURE_IDS:
                query = query.where(table.c.id.notin_(FIXTURE_IDS[name]))
            rows = connection.execution_options(stream_results=True).execute(query)
            for row in rows.mappings():
                checksum.update(bytes.fromhex(canonical_hash(dict(row))))
                count += 1
            result[name] = {'rows': count, 'sha256': checksum.hexdigest(), 'columns': [col.name for col in selected]}
    if not result['orders']['rows'] or not result['products']['rows'] or not result['settlements']['rows']:
        raise ValueError('restored business rows required')
    return result


def fixture_guard():
    if (os.environ.get('DATABASE_URL') != DATABASE_URL or
            os.environ.get('MESSAGE_CENTER_DISABLE_BACKGROUND') != 'true' or
            os.environ.get('REHEARSAL_ISOLATION_PROVEN') != 'true' or
            os.environ.get('REHEARSAL_FIXTURE_MODE') != 'true' or
            os.environ.get('REHEARSAL_PROBE_SIDE') != 'new'):
        raise ValueError('isolated fixture seeding guard rejected')


def seed_fixtures(m):
    fixture_guard()
    from sqlalchemy import inspect, or_, select, text
    # Create only the category table/nullable column needed by the two API models.
    m.ProductCategory.__table__.create(m.engine, checkfirst=True)
    if 'category_id' not in {col['name'] for col in inspect(m.engine).get_columns('products')}:
        with m.engine.begin() as connection:
            connection.execute(text('ALTER TABLE products ADD COLUMN category_id VARCHAR(36)'))
    models = {'users': m.User, 'enterprises': m.Enterprise, 'memberships': m.Membership,
              'products': m.Product, 'orders': m.Order, 'settlements': m.Settlement,
              'product_categories': m.ProductCategory}
    with m.SessionLocal() as db:
        for table, ids in FIXTURE_IDS.items():
            model = models[table]
            checks = [model.id.in_(ids)]
            if table == 'users':
                checks.extend([model.email.in_([value + '@example.invalid' for value in ids]), model.username.in_(ids)])
            elif table == 'enterprises':
                checks.extend([model.name.in_(ids), model.credit_code.in_(ids)])
            elif table == 'orders':
                checks.append(model.order_no.in_(ids))
            elif table == 'settlements':
                checks.append(model.settlement_no.in_(ids))
            elif table == 'product_categories':
                checks.append(model.name.in_(ids))
            if db.scalar(select(model.id).where(or_(*checks)).limit(1)) is not None:
                raise ValueError('isolated fixture identity collision')
        for name in ('finance', 'provider', 'buyer'):
            identifier = FIXTURE_PREFIX + name
            db.add(m.User(id=identifier, username=identifier, email=identifier + '@example.invalid',
                          name=identifier, password_hash='isolated-no-login', verified_status='verified',
                          is_active=True, activation_status='active',
                          platform_role='finance_settlement' if name == 'finance' else ''))
        for identifier in FIXTURE_IDS['enterprises']:
            db.add(m.Enterprise(id=identifier, name=identifier, credit_code=identifier))
        root, child = FIXTURE_IDS['product_categories']
        db.add(m.ProductCategory(id=root, name=root, parent_id=None, delivery_method=''))
        db.flush()
        db.add(m.ProductCategory(id=child, name=child, parent_id=root, delivery_method='file'))
        for name in ('provider', 'buyer'):
            db.add(m.Membership(id=FIXTURE_PREFIX + name + '-member', user_id=FIXTURE_PREFIX + name,
                                enterprise_id=FIXTURE_PREFIX + name + '-co', role='super_admin', status='active'))
        db.flush()
        for foreign in (False, True):
            suffix = 'foreign-' if foreign else ''
            provider_co = FIXTURE_PREFIX + ('buyer-co' if foreign else 'provider-co')
            product_id = FIXTURE_PREFIX + suffix + 'product'
            order_id = FIXTURE_PREFIX + suffix + 'order'
            settlement_id = FIXTURE_PREFIX + suffix + 'settlement'
            db.add(m.Product(id=product_id, enterprise_id=provider_co, name=product_id,
                             product_type='dataset', delivery_method='file', status='draft', category_id=child))
            db.flush()
            db.add(m.Order(id=order_id, order_no=order_id, provider_enterprise_id=provider_co,
                           buyer_enterprise_id=FIXTURE_PREFIX + 'buyer-co', buyer_user_id=FIXTURE_PREFIX + 'buyer',
                           product_id=product_id, product_name=product_id, buyer_name=FIXTURE_PREFIX + 'buyer-co',
                           amount=100, paid_amount=100, payment_status='paid', main_status='completed',
                           delivery_status='delivered', total_cost_snapshot=40))
            db.flush()
            db.add(m.Settlement(id=settlement_id, settlement_no=settlement_id, order_id=order_id,
                               gross_amount=100, net_amount=100, cost_amount=40, profit_amount=60,
                               platform_fee=12, provider_share=30, service_share=6, expert_fee=6,
                               channel_fee=6, status='pending'))
        db.commit()


def validate_rows(payload, fields):
    if not isinstance(payload, dict) or not isinstance(payload.get('items'), list) or not payload['items']:
        raise ValueError('nonempty business response required')
    rows = []
    for item in payload['items']:
        if not isinstance(item, dict) or not set(fields).issubset(item):
            raise ValueError('business response shape mismatch')
        for key in fields:
            if key.endswith('_amount') or key.endswith('_share'):
                if isinstance(item[key], bool) or not isinstance(item[key], (int, float)) or not math.isfinite(item[key]):
                    raise ValueError('finance numeric shape mismatch')
        rows.append({key: item[key] for key in fields})
    return sorted(rows, key=lambda item: item['id'])


def read_checks(get, finance_headers, provider_headers, own, foreign=None, new_detail_checks=False):
    finance_rows = validate_rows(get('/api/settlements', finance_headers), FINANCE_FIELDS)
    provider_rows = validate_rows(get('/api/enterprise-settlements?business_view=provider', provider_headers),
                                  PROVIDER_FIELDS)
    if {row['id'] for row in provider_rows} != own:
        raise ValueError('provider response escaped owned restored scope')
    get('/api/enterprise-settlements?business_view=buyer', provider_headers, 403)
    get('/api/settlements', provider_headers, 403)
    get('/api/settlements', expected=401)
    get('/api/enterprise-settlements?business_view=provider', expected=401)
    public = get('/api/storefront/categories')
    if not isinstance(public, dict) or not isinstance(public.get('items'), list) or not public['items']:
        raise ValueError('restored public category rows required')
    if new_detail_checks:
        if not own or foreign is None:
            raise ValueError('new detail checks require owned/foreign rows; provide an isolated schema fixture')
        get('/api/enterprise-settlements/' + sorted(own)[0] + '?business_view=provider', provider_headers)
        get('/api/enterprise-settlements/' + foreign + '?business_view=provider', provider_headers, 404)
    return finance_rows, provider_rows, public


def probe(ensure_category_schema=False, new_detail_checks=False, seed_isolated_fixtures=False):
    if (os.environ.get('DATABASE_URL') != DATABASE_URL or
            os.environ.get('MESSAGE_CENTER_DISABLE_BACKGROUND') != 'true'):
        raise ValueError('isolated probe environment required')
    synthetic_fixture = os.environ.get('REHEARSAL_FIXTURE_MODE') == 'true'
    if seed_isolated_fixtures:
        fixture_guard()
    if synthetic_fixture and os.environ.get('REHEARSAL_ISOLATION_PROVEN') != 'true':
        raise ValueError('isolated fixture seeding guard rejected')
    from sqlalchemy import create_engine, event, select
    from fastapi.testclient import TestClient
    baseline = create_engine(DATABASE_URL, connect_args={'options': '-c default_transaction_read_only=on'})
    try:
        original_columns = json.loads(os.environ['REHEARSAL_ORIGINAL_COLUMNS']) if (
            synthetic_fixture and not seed_isolated_fixtures) else None
        before = protected_fingerprint(baseline, exclude_fixtures=synthetic_fixture, columns=original_columns)
    finally:
        baseline.dispose()
    from app import main as m
    if seed_isolated_fixtures:
        seed_fixtures(m)
    if ensure_category_schema:
        # Deliberately separate schema-only compatibility preparation from HTTP reads.
        m.product_categories['ensure_schema'](m.engine)
    original_columns = {name: value['columns'] for name, value in before.items()}
    original_after_seed = protected_fingerprint(m.engine, exclude_fixtures=synthetic_fixture, columns=original_columns)
    if before != original_after_seed:
        raise ValueError('original restored fingerprint changed')
    read_before = protected_fingerprint(m.engine)
    @event.listens_for(m.engine, 'connect')
    def readonly(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute('SET default_transaction_read_only = on')
        connection.commit()
    m.engine.dispose()
    with m.SessionLocal() as db:
        active = (m.User.is_active.is_(True), m.User.activation_status != 'deleted')
        finance = db.scalar(select(m.User).where(*active,
            m.User.platform_role == 'finance_settlement',
            m.User.id == FIXTURE_PREFIX + 'finance' if synthetic_fixture else True).order_by(m.User.id).limit(1))
        candidate = db.execute(select(m.User, m.Membership).join(m.Membership,
            m.Membership.user_id == m.User.id).where(*active, m.User.platform_role == '',
            m.User.verified_status == 'verified', m.Membership.status == 'active',
            m.Membership.role.in_(['super_admin', 'enterprise_admin']),
            m.Membership.id == FIXTURE_PREFIX + 'provider-member' if synthetic_fixture else True,
            m.Membership.enterprise_id.in_(select(m.Order.provider_enterprise_id).join(
                m.Settlement, m.Settlement.order_id == m.Order.id).where(m.Settlement.status != 'superseded'))
            ).order_by(m.User.id, m.Membership.id).limit(1)).first()
        if finance is None or candidate is None:
            raise ValueError('restored finance and provider actors required')
        provider, membership = candidate
        own = set(db.scalars(select(m.Settlement.id).join(m.Order, m.Order.id == m.Settlement.order_id).where(
            m.Order.provider_enterprise_id == membership.enterprise_id, m.Settlement.status != 'superseded')))
        foreign = db.scalar(select(m.Settlement.id).join(m.Order, m.Order.id == m.Settlement.order_id).where(
            m.Order.provider_enterprise_id != membership.enterprise_id, m.Settlement.status != 'superseded',
            m.Settlement.id == FIXTURE_PREFIX + 'foreign-settlement' if synthetic_fixture else True
            ).order_by(m.Settlement.id).limit(1)) if new_detail_checks else None
        if not own:
            raise ValueError('owned restored settlements required; provide an isolated schema fixture')
        finance_headers = {'Authorization': 'Bearer ' + m.issue_token(finance)}
        provider_headers = {'Authorization': 'Bearer ' + m.issue_token(provider),
                            'X-Enterprise-Id': membership.enterprise_id}
    # Do not enter TestClient as a context manager: lifespan startup seeds/writes data.
    client = TestClient(m.app, raise_server_exceptions=True)
    checks = 0
    def get(path, headers=None, expected=200):
        nonlocal checks
        response = client.get(path, headers=headers)
        if response.status_code != expected:
            raise ValueError('read/auth HTTP status mismatch')
        checks += 1
        return response.json() if expected == 200 else None
    try:
        finance_rows, provider_rows, public = read_checks(get, finance_headers, provider_headers,
                                                        own, foreign, new_detail_checks)
    finally:
        client.close()
    after = protected_fingerprint(m.engine)
    if read_before != after:
        raise ValueError('protected restored business fingerprint changed')
    original_after = protected_fingerprint(m.engine, exclude_fixtures=synthetic_fixture, columns=original_columns)
    if before != original_after:
        raise ValueError('original restored fingerprint changed')
    return {'http_get_checks': checks, 'shared_http_get_checks': 7,
            'new_detail_capability_checks': 2 if new_detail_checks else 0,
            'finance_rows': len(finance_rows), 'provider_rows': len(provider_rows),
            'finance_read_sha256': canonical_hash(finance_rows), 'provider_read_sha256': canonical_hash(provider_rows),
            'public_read_sha256': canonical_hash(public), 'protected_sha256': canonical_hash(after),
            'protected_unchanged': True, 'category_schema_preparation': ensure_category_schema,
            'synthetic_fixture': synthetic_fixture, 'original_unchanged': True,
            'original_sha256': canonical_hash(original_after), 'original_columns': original_columns}


def run_images(old_image, new_image, engine, container, ensure_category_schema=False, new_detail_checks=False,
               seed_isolated_fixtures=False):
    if not re.fullmatch(r'market-rollback-[0-9a-f]{32}', container):
        raise ValueError('only helper-owned disposable container accepted')
    if not isinstance(engine, list) or not engine or not all(isinstance(v, str) and v for v in engine):
        raise ValueError('explicit engine argv required')
    def command(*args, **kwargs):
        return subprocess.run([*engine, *args], capture_output=True, check=True, **kwargs)
    inspection = json.loads(command('inspect', container).stdout)
    parent = inspection[0]
    mode = parent.get('HostConfig', {}).get('NetworkMode', '')
    if (mode not in {'none', ''} or
            parent.get('Config', {}).get('Labels', {}).get('market.isolated-rollback') != 'true' or
            not parent.get('State', {}).get('Running')):
        print(json.dumps({'failure_code': 'container_isolation',
            'network_none': parent.get('HostConfig', {}).get('NetworkMode') == 'none',
            'network_mode': str(parent.get('HostConfig', {}).get('NetworkMode', ''))[:80],
            'label_present': parent.get('Config', {}).get('Labels', {}).get('market.isolated-rollback') == 'true',
            'running_field': bool(parent.get('State', {}).get('Running')),
            'status_running': parent.get('State', {}).get('Status') == 'running'}), file=sys.stderr)
        raise ValueError('database container isolation could not be verified')
    # nerdctl's Docker-compatible inspect may omit NetworkMode. Verify the
    # namespace and kernel interfaces as well instead of trusting that field.
    namespace = command('exec', container, 'readlink', '/proc/self/ns/net').stdout.decode().strip()
    devices = command('exec', container, 'cat', '/proc/net/dev').stdout.decode()
    routes = command('exec', container, 'cat', '/proc/net/route').stdout.decode().splitlines()
    interfaces = {line.split(':', 1)[0].strip() for line in devices.splitlines() if ':' in line}
    tunnel_down = True
    if 'tunl0' in interfaces:
        flags = command('exec', container, 'cat', '/sys/class/net/tunl0/flags').stdout.decode().strip()
        tunnel_down = bool(re.fullmatch(r'0x[0-9a-fA-F]+', flags)) and not (int(flags, 16) & 1)
    published = any(parent.get('NetworkSettings', {}).get('Ports', {}).values())
    if (not re.fullmatch(r'net:\[\d+\]', namespace) or namespace == os.readlink('/proc/self/ns/net') or
            not {'lo'} <= interfaces <= {'lo', 'tunl0'} or not tunnel_down or len(routes) != 1 or published):
        print(json.dumps({'failure_code': 'kernel_network_isolation',
            'namespace_valid': bool(re.fullmatch(r'net:\[\d+\]', namespace)),
            'separate_namespace': namespace != os.readlink('/proc/self/ns/net'),
            'loopback_only': interfaces == {'lo'}, 'no_routes': len(routes) == 1,
            'interfaces': sorted(name for name in interfaces if re.fullmatch(r'[A-Za-z0-9_.-]{1,20}', name)),
            'no_published_ports': not published}), file=sys.stderr)
        raise ValueError('database container isolation could not be verified')
    reports = {}
    for side, image in (('new', new_image), ('old', old_image)):
        if not image or image.startswith('-') or any(c.isspace() for c in image):
            raise ValueError('invalid API image reference')
        inspected = json.loads(command('image', 'inspect', image).stdout)[0]
        image_id = inspected.get('Id')
        if not isinstance(image_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
            raise ValueError('immutable local API image identity required')
        name = 'market-read-rollback-' + uuid.uuid4().hex
        try:
            with Path(__file__).open('rb') as source:
                try:
                    result = command('run', '-i', '--pull=never', '--name', name,
                    '--network=container:' + container, '--memory=1g', '--cpus=1', '--pids-limit=256',
                    '-e', 'DATABASE_URL=' + DATABASE_URL, '-e', 'MESSAGE_CENTER_DISABLE_BACKGROUND=true',
                    '-e', 'REHEARSAL_ENSURE_CATEGORY_SCHEMA=' + str(side == 'new' and ensure_category_schema).lower(),
                    '-e', 'REHEARSAL_NEW_DETAIL_CHECKS=' + str(side == 'new' and new_detail_checks).lower(),
                    '-e', 'REHEARSAL_ISOLATION_PROVEN=true', '-e', 'REHEARSAL_PROBE_SIDE=' + side,
                    '-e', 'REHEARSAL_FIXTURE_MODE=' + str(seed_isolated_fixtures).lower(),
                    '-e', 'REHEARSAL_SEED_ISOLATED_FIXTURES=' + str(side == 'new' and seed_isolated_fixtures).lower(),
                    '-e', 'REHEARSAL_ORIGINAL_COLUMNS=' + json.dumps(reports.get('new', {}).get('original_columns', {})),
                    '--entrypoint', 'python', image_id, '-', '--probe', stdin=source, timeout=180)
                except subprocess.CalledProcessError as exc:
                    try:
                        failure = json.loads(exc.stderr)
                    except (ValueError, TypeError):
                        failure = {}
                    code = failure.get('failure_code')
                    if code in {*PROBE_FAILURES.values(), 'internal_probe_failure'}:
                        print(json.dumps({'side': side, 'failure_code': code}), file=sys.stderr)
                    raise
            report = json.loads(result.stdout)
            extra = 2 if side == 'new' and new_detail_checks else 0
            if (report.get('protected_unchanged') is not True or report.get('http_get_checks') != 7 + extra or
                    report.get('shared_http_get_checks') != 7 or report.get('new_detail_capability_checks') != extra or
                    not all(re.fullmatch(r'[0-9a-f]{64}', report.get(key, '')) for key in
                            ('finance_read_sha256', 'provider_read_sha256', 'public_read_sha256', 'protected_sha256'))):
                raise ValueError('image probe evidence incomplete')
            if seed_isolated_fixtures and (report.get('synthetic_fixture') is not True or
                    report.get('original_unchanged') is not True or
                    not re.fullmatch(r'[0-9a-f]{64}', report.get('original_sha256', '')) or
                    not isinstance(report.get('original_columns'), dict) or not report['original_columns']):
                raise ValueError('image probe evidence incomplete')
            reports[side] = dict(report, image_id=image_id)
        finally:
            removed = subprocess.run([*engine, 'rm', '-f', '-v', name], capture_output=True, check=False)
            names = command('ps', '-a', '--format', '{{.Names}}').stdout.decode().splitlines()
            if removed.returncode != 0 or name in names:
                raise ValueError('ephemeral API cleanup not verified')
    for key in ('finance_read_sha256', 'provider_read_sha256', 'public_read_sha256', 'protected_sha256'):
        if reports['new'][key] != reports['old'][key]:
            raise ValueError('old/new restored read evidence differs')
    if seed_isolated_fixtures and reports['new']['original_sha256'] != reports['old']['original_sha256']:
        raise ValueError('original restored fingerprint changed')
    return {'scope': 'restored database read rollback compatibility only', 'images': reports,
            'cleanup_verified': True, 'full_production_rollback': False}


def main():
    if sys.argv[1:] == ['--probe']:
        logging.disable(logging.CRITICAL)
        try:
            with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                result = probe(os.environ.get('REHEARSAL_ENSURE_CATEGORY_SCHEMA') == 'true',
                               os.environ.get('REHEARSAL_NEW_DETAIL_CHECKS') == 'true',
                               os.environ.get('REHEARSAL_SEED_ISOLATED_FIXTURES') == 'true')
            print(json.dumps(result, sort_keys=True))
        except Exception as exc:
            code = PROBE_FAILURES.get(str(exc), 'internal_probe_failure')
            print(json.dumps({'failure_code': code}), file=sys.stderr)
            sys.exit(1)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-image', required=True)
    parser.add_argument('--new-image', required=True)
    parser.add_argument('--ensure-category-schema', action='store_true')
    parser.add_argument('--new-detail-checks', action='store_true', help='Optional new-image-only detail capability checks')
    parser.add_argument('--seed-isolated-fixtures', action='store_true', help='Seed exact fixture identities in the isolated new-image probe only')
    args = parser.parse_args()
    try:
        result = run_images(args.old_image, args.new_image, json.loads(os.environ['REHEARSAL_ENGINE_ARGV']),
                            os.environ['REHEARSAL_CONTAINER'], args.ensure_category_schema, args.new_detail_checks,
                            args.seed_isolated_fixtures)
        print(json.dumps(result, sort_keys=True))
    except (KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
        known = {
            'database container isolation could not be verified': 'container_isolation',
            'immutable local API image identity required': 'image_identity',
            'image probe evidence incomplete': 'incomplete_probe',
            'ephemeral API cleanup not verified': 'cleanup',
            'old/new restored read evidence differs': 'read_difference',
        }
        print(json.dumps({'failure_code': known.get(str(exc), type(exc).__name__)}), file=sys.stderr)
        parser.exit(1, 'Read rollback compatibility failed; no response bodies or credentials logged.\n')


if __name__ == '__main__':
    main()
