"""Measure audit paging on synthetic rows in a disposable PostgreSQL schema."""
import argparse
import importlib.util
import json
import os
import secrets
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--rows', type=int, default=20000)
    args = parser.parse_args()
    if not 10000 <= args.rows <= 100000:
        raise ValueError('Synthetic rows must be between 10000 and 100000')
    root = Path(args.root)
    spec = importlib.util.spec_from_file_location('audit_test_loader', root / 'tests/test_message_business_events.py')
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    module = loader.load_isolated_main()
    url = make_url(os.environ['DATABASE_URL']).set(drivername='postgresql+psycopg')
    if url.get_backend_name() != 'postgresql':
        raise ValueError('PostgreSQL required')
    admin = create_engine(url, connect_args={'connect_timeout': 10})
    schema = 'audit_verify_' + secrets.token_hex(12)
    scoped = None
    try:
        with admin.begin() as db:
            db.execute(CreateSchema(schema))
        scoped = create_engine(url, execution_options={'schema_translate_map': {None: schema}},
                               connect_args={'options': f'-c search_path={schema} -c statement_timeout=20000'})
        module.Base.metadata.create_all(scoped)
        clock = datetime.now(timezone.utc)
        with scoped.begin() as db:
            for offset in range(0, args.rows, 1000):
                db.execute(insert(module.AuditLog), [dict(id=f'perf-{i}', actor='synthetic-finance',
                    action='synthetic_event', target_type='settlement', target_id=f'SET-{i}',
                    category='settlement' if i % 20 == 0 else 'ops', order_id=f'order-{i % 100}',
                    batch_no=f'BATCH-{i % 10}', created_at=clock-timedelta(seconds=i),
                    detail='Synthetic audit query fixture') for i in range(offset, min(offset+1000, args.rows))])
        results = []
        for name, filters in [('category', {'category': 'settlement'}), ('order', {'order_id': 'order-1'}),
                              ('batch', {'batch_no': 'BATCH-1'}), ('keyword', {'q': 'Synthetic'})]:
            durations = []
            with Session(scoped) as db:
                for _ in range(10):
                    start = time.perf_counter()
                    stmt = module.audit_support['query'](db, **filters)
                    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
                    rows = db.scalars(stmt.offset(50).limit(50)).all()
                    module.audit_support['objects'](db, rows)
                    durations.append((time.perf_counter()-start)*1000)
            p95 = sorted(durations)[-1]
            if p95 > 1500:
                raise AssertionError(f'{name} query exceeded development acceptance threshold: {p95:.1f}ms')
            results.append({'filter': name, 'total': total, 'page_rows': len(rows),
                            'max_ms': round(p95, 2), 'mean_ms': round(statistics.mean(durations), 2)})
        print(json.dumps({'status': 'passed', 'schema': schema, 'synthetic_rows': args.rows,
                          'development_max_threshold_ms': 1500, 'queries': results}), flush=True)
    finally:
        if scoped:
            scoped.dispose()
        with admin.begin() as db:
            db.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin.dispose()
        module.engine.dispose()
        print(json.dumps({'schema_removed': schema}), flush=True)


if __name__ == '__main__':
    main()
