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
                    'settlement_adjustment_proposals', 'audit_logs')
FINANCE_FIELDS = ('id', 'order_id', 'status', 'gross_amount', 'net_amount',
                  'cost_amount', 'profit_amount', 'provider_share')
PROVIDER_FIELDS = ('id', 'order_id', 'status', 'provider_share')


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     separators=(',', ':')).encode()).hexdigest()


def protected_fingerprint(engine):
    from sqlalchemy import MetaData, Table, select
    result = {}
    with engine.connect() as connection:
        for name in PROTECTED_TABLES:
            table = Table(name, MetaData(), schema='public', autoload_with=connection, resolve_fks=False)
            keys = list(table.primary_key.columns)
            if not keys:
                raise ValueError('protected table requires primary key')
            checksum = hashlib.sha256()
            count = 0
            rows = connection.execution_options(stream_results=True).execute(select(table).order_by(*keys))
            for row in rows.mappings():
                checksum.update(bytes.fromhex(canonical_hash(dict(row))))
                count += 1
            result[name] = {'rows': count, 'sha256': checksum.hexdigest()}
    if not result['orders']['rows'] or not result['products']['rows'] or not result['settlements']['rows']:
        raise ValueError('restored business rows required')
    return result


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


def probe(ensure_category_schema=False, new_detail_checks=False):
    if (os.environ.get('DATABASE_URL') != DATABASE_URL or
            os.environ.get('MESSAGE_CENTER_DISABLE_BACKGROUND') != 'true'):
        raise ValueError('isolated probe environment required')
    from sqlalchemy import create_engine, event, select
    from fastapi.testclient import TestClient
    baseline = create_engine(DATABASE_URL, connect_args={'options': '-c default_transaction_read_only=on'})
    try:
        before = protected_fingerprint(baseline)
    finally:
        baseline.dispose()
    from app import main as m
    if ensure_category_schema:
        # Deliberately separate schema-only compatibility preparation from HTTP reads.
        m.product_categories['ensure_schema'](m.engine)
    @event.listens_for(m.engine, 'connect')
    def readonly(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute('SET default_transaction_read_only = on')
        connection.commit()
    m.engine.dispose()
    with m.SessionLocal() as db:
        active = (m.User.is_active.is_(True), m.User.activation_status != 'deleted')
        finance = db.scalar(select(m.User).where(*active,
            m.User.platform_role == 'finance_settlement').order_by(m.User.id).limit(1))
        candidate = db.execute(select(m.User, m.Membership).join(m.Membership,
            m.Membership.user_id == m.User.id).where(*active, m.User.platform_role == '',
            m.User.verified_status == 'verified', m.Membership.status == 'active',
            m.Membership.role.in_(['super_admin', 'enterprise_admin']),
            m.Membership.enterprise_id.in_(select(m.Order.provider_enterprise_id).join(
                m.Settlement, m.Settlement.order_id == m.Order.id).where(m.Settlement.status != 'superseded'))
            ).order_by(m.User.id, m.Membership.id).limit(1)).first()
        if finance is None or candidate is None:
            raise ValueError('restored finance and provider actors required')
        provider, membership = candidate
        own = set(db.scalars(select(m.Settlement.id).join(m.Order, m.Order.id == m.Settlement.order_id).where(
            m.Order.provider_enterprise_id == membership.enterprise_id, m.Settlement.status != 'superseded')))
        foreign = db.scalar(select(m.Settlement.id).join(m.Order, m.Order.id == m.Settlement.order_id).where(
            m.Order.provider_enterprise_id != membership.enterprise_id, m.Settlement.status != 'superseded'
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
    if before != after:
        raise ValueError('protected restored business fingerprint changed')
    return {'http_get_checks': checks, 'shared_http_get_checks': 7,
            'new_detail_capability_checks': 2 if new_detail_checks else 0,
            'finance_rows': len(finance_rows), 'provider_rows': len(provider_rows),
            'finance_read_sha256': canonical_hash(finance_rows), 'provider_read_sha256': canonical_hash(provider_rows),
            'public_read_sha256': canonical_hash(public), 'protected_sha256': canonical_hash(after),
            'protected_unchanged': True, 'category_schema_preparation': ensure_category_schema}


def run_images(old_image, new_image, engine, container, ensure_category_schema=False, new_detail_checks=False):
    if not re.fullmatch(r'market-rollback-[0-9a-f]{32}', container):
        raise ValueError('only helper-owned disposable container accepted')
    if not isinstance(engine, list) or not engine or not all(isinstance(v, str) and v for v in engine):
        raise ValueError('explicit engine argv required')
    def command(*args, **kwargs):
        return subprocess.run([*engine, *args], capture_output=True, check=True, **kwargs)
    inspection = json.loads(command('inspect', container).stdout)
    parent = inspection[0]
    if (parent.get('HostConfig', {}).get('NetworkMode') != 'none' or
            parent.get('Config', {}).get('Labels', {}).get('market.isolated-rollback') != 'true' or
            not parent.get('State', {}).get('Running')):
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
                result = command('run', '-i', '--pull=never', '--name', name,
                    '--network=container:' + container, '--memory=1g', '--cpus=1', '--pids-limit=256',
                    '-e', 'DATABASE_URL=' + DATABASE_URL, '-e', 'MESSAGE_CENTER_DISABLE_BACKGROUND=true',
                    '-e', 'REHEARSAL_ENSURE_CATEGORY_SCHEMA=' + str(side == 'new' and ensure_category_schema).lower(),
                    '-e', 'REHEARSAL_NEW_DETAIL_CHECKS=' + str(side == 'new' and new_detail_checks).lower(),
                    '--entrypoint', 'python', image_id, '-', '--probe', stdin=source, timeout=180)
            report = json.loads(result.stdout)
            extra = 2 if side == 'new' and new_detail_checks else 0
            if (report.get('protected_unchanged') is not True or report.get('http_get_checks') != 7 + extra or
                    report.get('shared_http_get_checks') != 7 or report.get('new_detail_capability_checks') != extra or
                    not all(re.fullmatch(r'[0-9a-f]{64}', report.get(key, '')) for key in
                            ('finance_read_sha256', 'provider_read_sha256', 'public_read_sha256', 'protected_sha256'))):
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
    return {'scope': 'restored database read rollback compatibility only', 'images': reports,
            'cleanup_verified': True, 'full_production_rollback': False}


def main():
    if sys.argv[1:] == ['--probe']:
        logging.disable(logging.CRITICAL)
        try:
            with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
                result = probe(os.environ.get('REHEARSAL_ENSURE_CATEGORY_SCHEMA') == 'true',
                               os.environ.get('REHEARSAL_NEW_DETAIL_CHECKS') == 'true')
            print(json.dumps(result, sort_keys=True))
        except Exception:
            sys.exit(1)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-image', required=True)
    parser.add_argument('--new-image', required=True)
    parser.add_argument('--ensure-category-schema', action='store_true')
    parser.add_argument('--new-detail-checks', action='store_true', help='Optional new-image-only detail capability checks')
    args = parser.parse_args()
    try:
        result = run_images(args.old_image, args.new_image, json.loads(os.environ['REHEARSAL_ENGINE_ARGV']),
                            os.environ['REHEARSAL_CONTAINER'], args.ensure_category_schema, args.new_detail_checks)
        print(json.dumps(result, sort_keys=True))
    except (KeyError, ValueError, OSError, subprocess.SubprocessError):
        parser.exit(1, 'Read rollback compatibility failed; no response bodies or credentials logged.\n')


if __name__ == '__main__':
    main()
