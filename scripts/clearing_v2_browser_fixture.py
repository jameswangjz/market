"""Disposable SQLite browser fixture; never opens a deployed database."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8126)
    parser.add_argument('--tokens', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'backend' / 'tests'))
    import test_message_business_events as fixture
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    import uvicorn

    case = fixture.BusinessRoutesHTTPTests('test_personal_review_http_scope_resubmission_and_get_no_events')
    case.setUpClass()
    case.setUp()
    m = case.m
    temporary = tempfile.TemporaryDirectory(prefix='clr2-browser-')
    # Real browser requests are concurrent. A StaticPool in-memory connection
    # is appropriate for serial unit tests, not concurrent HTTP/SSE requests.
    old_engine = m.engine
    m.engine = create_engine('sqlite:///' + str(Path(temporary.name) / 'fixture.db'),
                             connect_args={'check_same_thread': False, 'timeout': 30})
    @event.listens_for(m.engine, 'connect')
    def foreign_keys(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
    source = old_engine.raw_connection()
    destination = m.engine.raw_connection()
    try:
        source.driver_connection.backup(destination.driver_connection)
    finally:
        source.close()
        destination.close()
        old_engine.dispose()
    m.SessionLocal = sessionmaker(bind=m.engine, autoflush=False)
    stamp = datetime.now(timezone.utc)
    tokens_created = False
    try:
        with m.SessionLocal() as db:
            for key in ('provider_tenant', 'buyer_tenant'):
                db.get(m.Enterprise, key).verification_status = 'verified'
            order = m.Order(id='clr2-browser-order', order_no='ORD-CLR2-BROWSER',
                buyer_user_id='buyer', buyer_enterprise_id='buyer_tenant',
                provider_enterprise_id='provider_tenant', product_id='product',
                product_version_id='version', product_name='Isolated clearing fixture',
                buyer_name='Synthetic buyer', amount=150, paid_amount=150,
                payment_status='paid', economic_snapshot_json=json.dumps({'currency': 'CNY'}))
            db.add(order)
            db.flush()
            db.add(m.Payment(order_id=order.id, payment_no='PAY-CLR2-BROWSER',
                amount=150, status='paid', paid_at=stamp))
            batch = m.SettlementBatch(id='clr2-browser-batch', batch_no='BATCH-CLR2-BROWSER',
                cycle='monthly', period_start=stamp.replace(day=1), status='confirmed',
                idempotency_key='clr2-browser', total_amount=150, total_profit=50)
            settlement = m.Settlement(id='clr2-browser-settlement', settlement_no='SET-CLR2-BROWSER',
                order_id=order.id, gross_amount=150, net_amount=150, cost_amount=100,
                profit_amount=50, platform_fee=10, provider_share=40, status='locked', created_at=stamp)
            db.add_all([batch, settlement])
            db.flush()
            for kind, amount in (('platform', Decimal('10')), ('provider', Decimal('40'))):
                db.add(m.SettlementLine(batch_id=batch.id, settlement_id=settlement.id,
                    participant_type=kind, participant_id='provider_tenant' if kind == 'provider' else 'platform',
                    participant_name=kind, amount=amount, status='locked'))
            db.flush()
            m.settlement_finance['capture'](db, settlement, 'isolated-browser-fixture')
            db.commit()
        args.tokens.mkdir(mode=0o700, parents=True, exist_ok=False)
        tokens_created = True
        for actor in ('platform', 'ops', 'finance', 'provider', 'buyer'):
            path = args.tokens / (actor + '.token')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(case.tokens[actor])
        dist = root / 'frontend' / 'dist'
        if not (dist / 'index.html').is_file():
            raise RuntimeError('Build frontend before starting browser fixture')
        m.app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='fixture-assets')

        @m.app.get('/console', include_in_schema=False)
        def console():
            return FileResponse(dist / 'index.html')

        print('Isolated fixture ready; synthetic data only', flush=True)
        uvicorn.run(m.app, host='0.0.0.0', port=args.port, lifespan='off', log_level='warning')
    finally:
        case.tearDown()
        case.tearDownClass()
        temporary.cleanup()
        if tokens_created:
            for path in args.tokens.glob('*.token'):
                path.unlink()
            args.tokens.rmdir()


if __name__ == '__main__':
    main()
