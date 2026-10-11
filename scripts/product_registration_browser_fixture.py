"""Isolated persisted PostgreSQL provider fixture; no startup or network hooks.

Run in the API pod with PYTHONPATH=/app and subscription_pg_concurrency.py
alongside this script. Private state/token files must live directly in /tmp.
"""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat

from sqlalchemy import delete, func, select
import subscription_pg_concurrency as safe

m = safe.m


def private_path(value):
    path = Path(value).absolute()
    if path.parent not in (Path('/tmp'), Path('/var/tmp')) or path.is_symlink():
        raise ValueError('Use a direct non-symlink temporary file')
    return path


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())


def validate(state):
    prefix = state['prefix']
    safe.check(re.fullmatch(r'qa-reg-[0-9a-f]{12}', prefix), 'Invalid fixture marker')
    safe.check(state['product'] == prefix + '-p' and
               state['versions'] == [prefix + '-v1', prefix + '-v2'] and
               state['users'] == [prefix + '-u'] and
               state['enterprises'] == [prefix + '-e'] and
               state['emails'] == [prefix + '-provider@example.invalid'] and
               state['requests'] == [prefix + '-browser'] and
               state['version'] == state['versions'][0], 'Invalid ownership manifest')


def owned_scopes(db, state):
    validate(state)
    user = db.get(m.User, state['users'][0])
    product = db.scalar(select(m.Product).where(m.Product.id == state['product']).with_for_update())
    if user:
        safe.check(user.email == state['emails'][0] and not user.platform_role,
                   'Refusing cleanup: changed user ownership')
    if product:
        safe.check(product.enterprise_id == state['enterprises'][0], 'Changed product owner')
    actual_versions = db.scalars(select(m.ProductReleaseVersion.id).where(
        m.ProductReleaseVersion.product_id == state['product'])).all()
    safe.check(set(actual_versions) <= set(state['versions']), 'Unexpected product version')
    enterprise_members = db.scalars(select(m.Membership).where(
        m.Membership.enterprise_id.in_(state['enterprises']))).all()
    safe.check(all(row.user_id in state['users'] for row in enterprise_members), 'Foreign enterprise member')
    safe.check(not db.scalar(select(m.Order.id).where(m.Order.product_id == state['product']).limit(1)),
               'Refusing cleanup: fixture unexpectedly has orders')
    members = db.scalars(select(m.Membership).where(m.Membership.user_id.in_(state['users']))).all()
    safe.check(all(row.enterprise_id in state['enterprises'] for row in members), 'Foreign membership')
    db.scalars(select(m.Enterprise).where(m.Enterprise.id.in_(state['enterprises']))
               .with_for_update()).all()
    predicates = safe.scopes(db, state)
    for model in (m.OAuthClient, m.ApiGatewayRoute, m.version_gateway['Config']):
        predicates[model.__tablename__] = model.product_id == state['product']
    predicates[m.ProductReleaseVersion.__tablename__] = m.ProductReleaseVersion.product_id == state['product']
    predicates[m.ServiceLevelAssignment.__tablename__] = m.ServiceLevelAssignment.enterprise_id.in_(state['enterprises'])
    return predicates


def remove_owned(state):
    with m.SessionLocal() as db:
        safe.check(db.bind.dialect.name == 'postgresql', 'Real PostgreSQL required')
        outbox = m.message_center['Outbox']
        db.scalars(select(outbox).where(outbox.user_id.in_(state['users']))
                   .order_by(outbox.id).with_for_update()).all()
        predicates = owned_scopes(db, state)
        keys = {}
        for name, predicate in predicates.items():
            table = m.Base.metadata.tables[name]
            for column in table.primary_key.columns:
                keys[column] = list(db.scalars(select(column).where(predicate)))
        for table in reversed(m.Base.metadata.sorted_tables):
            if table.name in predicates:
                db.execute(delete(table).where(predicates[table.name]))
        db.commit()
    with m.SessionLocal() as db:
        for name, predicate in predicates.items():
            table = m.Base.metadata.tables[name]
            safe.check(db.scalar(select(func.count()).select_from(table).where(predicate)) == 0,
                       f'Owned rows remain: {name}')
        for table in m.Base.metadata.tables.values():
            for fk in table.foreign_keys:
                values = keys.get(fk.column, [])
                if values:
                    safe.check(db.scalar(select(func.count()).select_from(table)
                                         .where(fk.parent.in_(values))) == 0,
                               f'Owned FK leftovers: {table.name}.{fk.parent.name}')


def prepare(state_path, token_path):
    safe.check(state_path != token_path and not state_path.exists() and not token_path.exists(),
               'Temporary files must be distinct and absent')
    prefix = 'qa-reg-' + secrets.token_hex(6)
    state = dict(prefix=prefix, product=prefix+'-p', version=prefix+'-v1',
                 versions=[prefix+'-v1', prefix+'-v2'], users=[prefix+'-u'],
                 enterprises=[prefix+'-e'], emails=[prefix+'-provider@example.invalid'],
                 requests=[prefix+'-browser'])
    validate(state)
    with m.SessionLocal() as db:
        safe.check(db.bind.dialect.name == 'postgresql', 'Real PostgreSQL required')
        for model, ids in ((m.User, state['users']), (m.Enterprise, state['enterprises']),
                           (m.Product, [state['product']]), (m.ProductReleaseVersion, state['versions'])):
            safe.check(not db.scalar(select(model.id).where(model.id.in_(ids)).limit(1)), 'ID collision')
    write_private(state_path, state)
    try:
        with m.SessionLocal() as db:
            user = m.User(id=state['users'][0], email=state['emails'][0], name=prefix+'-provider',
                          password_hash='not-a-login-password', verified_status='verified',
                          activation_status='active', is_active=True)
            enterprise = m.Enterprise(id=state['enterprises'][0], name=prefix+'-provider',
                                      credit_code=prefix+'-e', verification_status='verified')
            db.add_all([user, enterprise]); db.flush()
            db.add(m.Membership(user_id=user.id, enterprise_id=enterprise.id, role='super_admin',
                                status='active', business_roles='provider,user'))
            db.add(m.message_center['Preference'](user_id=user.id, email_enabled=False))
            product = m.Product(id=state['product'], enterprise_id=enterprise.id, name=prefix+'-API',
                                provider_name=enterprise.name, product_type='api', delivery_method='api',
                                upstream_url='https://provider.example.com/api', status='pending_integration')
            db.add(product); db.flush()
            versions = []
            for index, vid in enumerate(state['versions'], 1):
                version = m.ProductReleaseVersion(id=vid, product_id=product.id, version_code=f'v{index}',
                    description=f'{prefix} isolated version {index}', price=12*index, cost=7*index,
                    status='active', rate_limit_per_minute=17*index, daily_quota=123*index,
                    monthly_quota=456000*index)
                db.add(version); db.flush()
                config = dict(upstream_url=f'https://provider.example.com/api/v{index}',
                    route_key=f'{prefix}-r{index}', version=version.version_code, auth_mode='api_key',
                    upstream_auth_mode='oauth2', upstream_scope='resource.invoke', timeout_ms=30000,
                    strip_prefix=True, health_method='GET', health_path=f'/health/v{index}',
                    rate_limit_per_minute=17*index, daily_quota=123*index, monthly_quota=456000*index)
                m.version_gateway['save_config'](db, product, vid, config, user)
                versions.append(dict(id=vid, version_code=version.version_code, **config))
            # Placeholder credentials have no upstream account or usable external identity.
            db.add(m.OAuthClient(product_id=product.id, client_id=prefix+'-placeholder',
                                client_secret=secrets.token_urlsafe(32), status='active'))
            db.commit()
            token = m.issue_token(user)
        write_private(token_path, dict(token=token, prefix=prefix, product_id=state['product'],
            product_name=prefix+'-API', user_id=state['users'][0], enterprise_id=state['enterprises'][0],
            versions=versions))
    except BaseException:
        remove_owned(state)
        state_path.unlink()
        token_path.unlink(missing_ok=True)
        raise
    return dict(status='prepared', fixture=prefix, versions=2,
                layer='committed PG fixture; validated save_config; no verify/provision/upload')


def cleanup(state_path, token_path):
    safe.check(state_path != token_path, 'State and token files must differ')
    fd = os.open(state_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        safe.check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
                   'State file must be a regular private 0600 file')
        state = json.load(stream)
    remove_owned(state)
    token_path.unlink(missing_ok=True)
    state_path.unlink()
    return dict(status='cleaned', fixture=state['prefix'], cleanup='owned rows and FK leftovers verified absent')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'cleanup'))
    parser.add_argument('--state-file', required=True, type=private_path)
    parser.add_argument('--token-file', required=True, type=private_path)
    args = parser.parse_args()
    try:
        print(json.dumps(globals()[args.action](args.state_file, args.token_file), indent=2))
    except BaseException as exc:
        # SQL/HTTP exception text can contain parameters. Preserve private state for recovery.
        print(json.dumps(dict(status='failed', error_type=type(exc).__name__,
                              recovery='retain private state; resolve failure then run cleanup')))
        raise SystemExit(1) from None
