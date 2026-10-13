"""Run production startup twice in a newly owned PostgreSQL schema only."""
import json
import logging
import os
from pathlib import Path
import secrets
import sys

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from version_change_pg_crash_acceptance import fingerprint


def check(value, message):
    if not value:
        raise AssertionError(message)


def run():
    original = os.environ['DATABASE_URL']
    url = make_url(original)
    check(url.get_backend_name() == 'postgresql', 'PostgreSQL runtime required')
    admin = create_engine(url, pool_pre_ping=True)
    before = fingerprint(admin)
    schema = 'qa_startup_' + secrets.token_hex(8)
    created, m = False, None
    try:
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        query = dict(url.query)
        query['options'] = (str(query.get('options', '')) + ' -csearch_path=' + schema).strip()
        os.environ['DATABASE_URL'] = url.set(query=query).render_as_string(hide_password=False)
        os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
        from app import main as loaded
        m = loaded
        with m.engine.connect() as connection:
            check(connection.scalar(text('SELECT current_schema()')) == schema,
                  'Startup engine escaped the owned schema')
        m.startup()
        models = (m.User, m.Enterprise, m.Product, m.Order, m.Payment, m.DeliveryTask, m.OrderStateLog)
        def inspect():
            with m.SessionLocal() as db:
                check(db.scalar(text('SELECT current_schema()')) == schema,
                      'Startup session escaped the owned schema')
                admin_user = db.scalar(select(m.User).where(m.User.email == 'admin@market.local'))
                order = db.scalar(select(m.Order).where(m.Order.order_no == 'ORD-260930-0001'))
                check(admin_user is not None and order is not None and order.buyer_user_id == admin_user.id,
                      'Seed order lacks its exact valid buyer reference')
                check(db.scalar(select(func.count()).select_from(m.Membership).join(m.User,
                    m.User.id == m.Membership.user_id).where(m.User.platform_role != '')) == 0,
                    'Platform account was attached to an enterprise')
                counts = {model.__tablename__: db.scalar(select(func.count()).select_from(model))
                          for model in models}
                return admin_user.id, order.id, order.buyer_user_id, counts
        first = inspect()
        m.startup()
        second = inspect()
        check(first == second, 'Repeated startup duplicated seeded core identities or business rows')
        return {'status': 'passed', 'scope': 'production startup twice in isolated real PostgreSQL schema',
                'valid_buyer_foreign_key': True, 'repeat_core_rows_unchanged': True,
                'platform_memberships': 0, 'counts': second[-1],
                'cleanup': 'owned schema removed; protected public order and product unchanged'}
    finally:
        os.environ['DATABASE_URL'] = original
        if m:
            m.engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        with admin.connect() as connection:
            check(connection.scalar(text('SELECT count(*) FROM information_schema.schemata '
                'WHERE schema_name=:schema'), {'schema': schema}) == 0, 'Owned schema remains')
        check(fingerprint(admin) == before, 'Protected public business data changed')
        admin.dispose()


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(run(), sort_keys=True))
    except AssertionError as exc:
        print(json.dumps({'status': 'failed', 'check': str(exc)}))
        raise SystemExit(1) from None
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None
