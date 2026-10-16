"""Validate installed matrix and expiry serialization in a disposable PG schema."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from pathlib import Path
import secrets
from unittest.mock import patch


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, func, select, text
    from sqlalchemy.orm import sessionmaker
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    schema = 'qa_slc_' + secrets.token_hex(16)
    result = {'status': 'failed', 'schema': schema, 'checks': [], 'cleanup': False}
    engine = create_engine(m.engine.url, execution_options={'schema_translate_map': {None: schema}})
    Session = sessionmaker(engine, autoflush=False)
    previous = dict(m.app.dependency_overrides)
    created = False
    actor = m.User(id='qa-admin', email='qa@invalid.local', platform_role='super_admin',
                   name='QA', password_hash='unused', is_active=True,
                   activation_status='active', verified_status='verified')
    with os.fdopen(fd, 'w') as out:
        try:
            if engine.dialect.name != 'postgresql':
                raise ValueError('PostgreSQL required')
            with engine.begin() as connection:
                connection.execute(text('CREATE SCHEMA "' + schema + '"'))
            created = True
            m.Base.metadata.create_all(engine)
            with Session() as db:
                db.add(m.Enterprise(id='qa-enterprise', name='QA Enterprise', credit_code='QA-SLC',
                                    verification_status='verified'))
                for code, normal, urgent in [('standard', 2880, 1440), ('enterprise', 1440, 720), ('strategic', 720, 360)]:
                    db.add(m.ServiceLevel(id=code, code=code, name=code, status='active',
                        initial_response_minutes=normal, urgent_initial_response_minutes=urgent))
                db.commit()

            def session():
                with Session() as db:
                    yield db

            m.app.dependency_overrides[m.db_session] = session
            m.app.dependency_overrides[m.current_user] = lambda: actor
            client = TestClient(m.app)

            def edit(target):
                return client.patch('/api/sla/service-levels/standard/matrix',
                    json={'expected_version': 0, 'metrics': {'availability': {'target': target, 'warning': 99}}})

            with Session() as db:
                old = m.freeze_sla_snapshot(db, 'qa-enterprise')
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(lambda target: edit(target).status_code, (91, 92)))
            if sorted(statuses) != [200, 409]:
                raise ValueError('Concurrent matrix statuses: ' + str(statuses))
            with Session() as db:
                fresh = m.freeze_sla_snapshot(db, 'qa-enterprise')
                if old['metrics']['availability']['target'] != 90 or fresh['rule_version'] == old['rule_version']:
                    raise ValueError('Frozen matrix isolation failed')
                if db.scalar(select(func.count()).select_from(m.SLAMatrixVersion)) != 2:
                    raise ValueError('Concurrent revision count incorrect')
            result['checks'].append('PG concurrent matrix edit: one 200 and one 409; frozen before/after differ')
            clock = m.now()
            with patch.object(m, 'now', return_value=clock):
                response = client.post('/api/sla/service-level-assignments', json={
                    'enterprise_id': 'qa-enterprise', 'service_level_id': 'enterprise',
                    'reason': 'Isolated expiry test', 'expires_at': (clock + timedelta(minutes=1)).isoformat()})
                response.raise_for_status()
            with patch.object(m, 'now', return_value=clock + timedelta(minutes=1)):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    responses = list(pool.map(lambda _: client.post('/api/sla/expire-bindings'), range(2)))
                if any(response.status_code != 200 for response in responses):
                    raise ValueError('Concurrent expiry failed')
                if sorted(response.json()['audited'] for response in responses) != [0, 1]:
                    raise ValueError('Expiry was not idempotent')
                with Session() as db:
                    if m.freeze_sla_snapshot(db, 'qa-enterprise')['service_level_code'] != 'standard':
                        raise ValueError('Expired tier did not fall back to standard')
            result['checks'].append('PG concurrent expiry audited once; exact-boundary fallback standard')
            result['status'] = 'passed'
        finally:
            m.app.dependency_overrides.clear()
            m.app.dependency_overrides.update(previous)
            engine.dispose()
            if created:
                with m.engine.begin() as connection:
                    connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                result['cleanup'] = True
            json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
