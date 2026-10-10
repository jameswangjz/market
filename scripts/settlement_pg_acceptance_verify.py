"""Verify additive migration and concurrent batch generation in an isolated PG schema."""
import argparse
import importlib.util
import json
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    root = Path(args.root)
    spec = importlib.util.spec_from_file_location('settlement_test_loader', root / 'tests/test_message_business_events.py')
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    m = loader.load_isolated_main()
    url = make_url(os.environ['DATABASE_URL']).set(drivername='postgresql+psycopg')
    if url.get_backend_name() != 'postgresql':
        raise ValueError('PostgreSQL required')
    admin = create_engine(url)
    schema = 'settlement_verify_' + secrets.token_hex(12)
    scoped = None
    try:
        with admin.begin() as db:
            db.execute(CreateSchema(schema))
        scoped = create_engine(url, execution_options={'schema_translate_map': {None: schema}}, pool_size=8,
                               connect_args={'options': f'-c search_path={schema} -c statement_timeout=20000'})
        m.engine.dispose()
        m.engine, m.SessionLocal = scoped, sessionmaker(scoped, autoflush=False)
        m.Base.metadata.create_all(scoped)
        with m.SessionLocal() as db:
            db.add(m.User(id='finance', name='Synthetic finance', email='finance@example.invalid',
                          password_hash='unused', platform_role='finance_settlement', verified_status='verified'))
            db.add(m.Enterprise(id='tenant', name='Synthetic tenant', credit_code='SYNTHETIC'))
            db.flush()
            db.add(m.Product(id='p', enterprise_id='tenant', name='Synthetic', product_type='dataset',
                             provider_name='Synthetic', provider_type='enterprise', status='published'))
            db.flush()
            db.add(m.ProductReleaseVersion(id='v', product_id='p', version_code='v1', price=100, cost=10))
            db.add(m.SettlementRule(id='rule', rule_no='RULE-TEST', status='active', name='Synthetic',
                                   platform_rate=20, provider_rate=80, service_rate=0, expert_rate=0, channel_rate=0))
            db.flush()
            db.add(m.Order(id='o', order_no='ORD-SYNTHETIC', product_id='p', product_name='Synthetic',
                           buyer_name='Synthetic', buyer_enterprise_id='tenant', provider_enterprise_id='tenant',
                           buyer_user_id='finance', product_version_id='v', amount=100, paid_amount=100,
                           payment_status='paid'))
            db.flush()
            db.add(m.Payment(id='pay', order_id='o', payment_no='PAY-SYNTHETIC', status='paid', amount=100,
                             paid_at=datetime(2026, 10, 8, tzinfo=timezone.utc)))
            db.add(m.SettlementMeasurement(id='legacy', order_id='o', measurement_type='download', quantity=1,
                                          validation_status='validated'))
            db.commit()
        # Remove only this disposable schema's newly added columns to emulate an old deployment.
        with scoped.begin() as db:
            db.exec_driver_sql('ALTER TABLE settlement_batches DROP COLUMN request_digest')
            for column in ('event_key', 'source_id', 'actor_id', 'sample_kind', 'scope', 'evidence_json'):
                db.exec_driver_sql(f'ALTER TABLE settlement_measurements DROP COLUMN {column}')
        m.ensure_review_and_file_schema()
        m.ensure_review_and_file_schema()
        with m.SessionLocal() as db:
            legacy = db.get(m.SettlementMeasurement, 'legacy')
            assert legacy.event_key == 'legacy:legacy' and legacy.validation_status == 'pending'
            assert legacy.scope == 'order'
        barrier = threading.Barrier(4)
        body = m.SettlementBatchBody(cycle='monthly', period_start=datetime(2026, 10, 1, tzinfo=timezone.utc),
                                    period_end=datetime(2026, 11, 1, tzinfo=timezone.utc), idempotency_key='concurrent-month')
        def generate(_):
            with m.SessionLocal() as db:
                user = db.get(m.User, 'finance')
                barrier.wait(timeout=10)
                return m.create_settlement_batch(body, user, db)
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(generate, range(4)))
        assert len({x['id'] for x in responses}) == 1
        with m.SessionLocal() as db:
            batch_count = db.scalar(select(func.count()).select_from(m.SettlementBatch))
            settlement_count = db.scalar(select(func.count()).select_from(m.Settlement))
            line_count = db.scalar(select(func.count()).select_from(m.SettlementLine))
            assert (batch_count, settlement_count, line_count) == (1, 1, 5)
        print(json.dumps({'status': 'passed', 'schema': schema, 'additive_migration_runs': 2,
                          'threads': 4, 'batches': batch_count, 'settlements': settlement_count,
                          'lines': line_count}), flush=True)
    finally:
        if scoped:
            scoped.dispose()
        with admin.begin() as db:
            db.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin.dispose()
        print(json.dumps({'schema_removed': schema}), flush=True)


if __name__ == '__main__':
    main()
