"""Exercise installed alert transactions in an owned disposable PostgreSQL schema."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    from app import main as m
    from app.gateway_monitoring import MonitoringBase, GatewayAlertAction, GatewayAlertState
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, func, select, text
    from sqlalchemy.orm import sessionmaker
    schema = 'qa_list_' + secrets.token_hex(16)
    result = {'status': 'failed', 'schema': schema, 'checks': [], 'cleanup': False}
    engine = create_engine(m.engine.url, execution_options={'schema_translate_map': {None: schema}})
    Session = sessionmaker(engine, autoflush=False)
    actor = m.User(id='qa-admin', email='qa@invalid.local', name='QA', password_hash='unused',
                   platform_role='super_admin', is_active=True, activation_status='active', verified_status='verified')
    previous = dict(m.app.dependency_overrides)
    created = False
    with os.fdopen(fd, 'w') as out:
        try:
            if engine.dialect.name != 'postgresql':
                raise ValueError('PostgreSQL required')
            with engine.begin() as c:
                c.execute(text('CREATE SCHEMA "' + schema + '"'))
            created = True
            m.Base.metadata.create_all(engine)
            MonitoringBase.metadata.create_all(engine)
            with Session() as db:
                db.add(m.Enterprise(id='qa-enterprise', name='QA Enterprise', credit_code='QA-ISOLATED'))
                db.flush()
                db.add(m.Product(id='qa-product', enterprise_id='qa-enterprise', name='QA Product',
                                 product_type='api', status='published'))
                db.flush()
                db.add(m.ApiGatewayRoute(id='qa-route', product_id='qa-product', route_key='qa-route', upstream_url='https://invalid'))
                db.flush()
                db.add(m.GatewayConfigRevision(id='qa-revision', route_id='qa-route', product_id='qa-product'))
                db.flush()
                for key in ('qa-lifecycle', 'qa-race'):
                    db.add(m.GatewayPublishRecord(id=key, route_id='qa-route', revision_id='qa-revision', status='failed'))
                db.commit()
            def session():
                with Session() as db:
                    yield db
            m.app.dependency_overrides[m.db_session] = session
            m.app.dependency_overrides[m.current_user] = lambda: actor
            client = TestClient(m.app)
            def action(alert, name, version, key):
                return client.post('/api/gateway/routes/qa-route/alerts/' + alert + '/' + name,
                    json={'request_id': key, 'expected_version': version, 'note': 'Isolated PostgreSQL validation'})
            for version, name in enumerate(('acknowledge', 'process', 'close')):
                response = action('qa-lifecycle', name, version, 'same-' + name)
                if response.status_code != 200:
                    raise ValueError('Alert lifecycle failed: ' + str(response.status_code))
                if action('qa-lifecycle', name, version, 'same-' + name).json().get('replayed') is not True:
                    raise ValueError('Replay not idempotent')
            result['checks'].append('acknowledge/process/close and exact-key replay')
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(lambda key: action('qa-race', 'acknowledge', 0, key).status_code, ('race-one', 'race-two')))
            if sorted(statuses) != [200, 409]:
                raise ValueError('Concurrent write was not serialized: ' + str(statuses))
            result['checks'].append('PostgreSQL concurrent first action has one winner')
            with Session() as db:
                if db.scalar(select(func.count()).select_from(GatewayAlertAction)) != 4:
                    raise ValueError('Duplicate/missing action records')
                if db.scalar(select(func.count()).select_from(m.AuditLog).where(m.AuditLog.target_type == 'gateway_alert')) != 4:
                    raise ValueError('Unified audits differ from committed actions')
                if db.get(GatewayAlertState, 'qa-race').version != 1:
                    raise ValueError('Concurrent version mismatch')
            result['checks'].append('deduplicated durable actions and unified audit')
            result['status'] = 'passed'
        finally:
            m.app.dependency_overrides.clear()
            m.app.dependency_overrides.update(previous)
            engine.dispose()
            if created:
                with m.engine.begin() as c:
                    c.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                result['cleanup'] = True
            json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
