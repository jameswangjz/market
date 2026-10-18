"""Isolated multi-cost UI fixture; uses real VC proofs, never a deployed database."""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8129)
    parser.add_argument('--tokens', type=Path, required=True)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'backend' / 'tests'))
    sys.path.insert(0, str(root / 'backend'))
    from test_cross_version_multi_source_refund import MultiSourceRefundTests
    from fastapi import Depends, HTTPException
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from sqlalchemy import create_engine, event, select
    from sqlalchemy.orm import sessionmaker
    from starlette.requests import Request
    import uvicorn

    case = MultiSourceRefundTests('test_actual_multi_capture_preserves_source_positions_and_replays')
    case.setUpClass()
    case.setUp()
    temporary = tempfile.TemporaryDirectory(prefix='clr2-multi-cost-ui-')
    created_tokens = False
    try:
        case.two_changes()
        m = case.m
        old = m.engine
        m.engine = create_engine('sqlite:///' + str(Path(temporary.name) / 'fixture.db'),
            connect_args={'check_same_thread': False, 'timeout': 30})
        @event.listens_for(m.engine, 'connect')
        def foreign_keys(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
        source, destination = old.raw_connection(), m.engine.raw_connection()
        try:
            source.driver_connection.backup(destination.driver_connection)
        finally:
            source.close()
            destination.close()
            old.dispose()
        m.SessionLocal = sessionmaker(bind=m.engine, autoflush=False)
        with m.SessionLocal() as db:
            db.get(m.Enterprise, 'provider_tenant').verification_status = 'verified'
            db.flush()
            for i, sid in enumerate(case.target_ids, 1):
                if db.scalar(select(m.SettlementLine.id).where(m.SettlementLine.settlement_id == sid)):
                    continue
                settlement = db.get(m.Settlement, sid)
                batch = m.SettlementBatch(id='multi-cost-batch-' + str(i), batch_no='BATCH-MULTI-COST-' + str(i),
                    cycle='monthly', period_start=m.now().replace(day=1), status='confirmed',
                    idempotency_key='multi-cost-ui-' + str(i), total_amount=settlement.net_amount, total_profit=settlement.profit_amount)
                db.add(batch)
                db.flush()
                db.add(m.SettlementLine(batch_id=batch.id, settlement_id=sid, participant_type='provider',
                    participant_id='provider_tenant', participant_name='Synthetic provider', amount=settlement.provider_share, status='locked'))
            refund = case.seed_completed(db, case.plans(db), rid='multi-cost-ui')
            negative = m.refund_settlement['negative_settlement'](db, refund)
            if m.refund_settlement['assessment'](db, negative.id).get('status') != 'ready':
                raise RuntimeError('Real multi-source refund basis is not ready')
            request = Request({'type': 'http', 'query_string': b'enterprise_id=provider_tenant&business_view=provider', 'headers': []})
            m.settlement_parties['create_participant'](db, db.get(m.User, 'provider'), dict(
                request_id='multi-cost-bearer', name='Synthetic multi cost bearer', kind='provider',
                enterprise_id='provider_tenant', currency='CNY'), request)
            db.commit()

        def state(db):
            refund = db.get(m.Refund, 'multi-cost-ui')
            order = db.get(m.Order, refund.order_id)
            completion = db.get(m.refund_execution['Completion'], refund.id)
            return dict(refund_id=refund.id, refund_no=refund.refund_no, refund_amount=format(Decimal(refund.amount), '.2f'),
                buyer_actual_paid=format(Decimal(order.paid_amount), '.2f'), completion_plan_hash=completion.plan_hash,
                completion_proof_hash=completion.proof_hash, completion_funding_hash=completion.funding_hash,
                source_hashes={sid: db.get(m.settlement_finance['Baseline'], sid).snapshot_hash for sid in case.target_ids})

        @m.app.get('/api/__fixture__/multi-cost/state')
        def fixture_state(user=Depends(m.current_user), db=Depends(m.db_session)):
            if user.id not in {'provider', 'ops', 'finance'}:
                raise HTTPException(403, 'synthetic_fixture_actor_required')
            return state(db)

        if args.check_only:
            headers = {'Authorization': 'Bearer ' + case.tokens['provider'], 'X-Enterprise-Id': 'provider_tenant'}
            params = dict(enterprise_id='provider_tenant', business_view='provider', start='1900-01-01', end='9998-12-31', page_size=500)
            response = case.client.get('/api/settlement-reporting/report', params=params, headers=headers)
            assert response.status_code == 200, response.text
            rows = [row for row in response.json()['items'] if row['refund_id'] == 'multi-cost-ui']
            assert len(rows) == 1
            detail = case.client.get('/api/enterprise-settlements/' + rows[0]['id'], params=params, headers=headers)
            assert detail.status_code == 200, detail.text
            refs = detail.json()['refund_adjustment']['financial_basis']['references']
            assert len(refs) == 2
            assert all(s.get('source_order_no') and s.get('reference_settlement_no') and s.get('reference_batch_no')
                       and Decimal(s['cost_amount']) < 0 for s in refs), refs
            print('Real isolated multi-source report, business names, caps and proofs ready', flush=True)
            return
        args.tokens.mkdir(mode=0o700, parents=True, exist_ok=False)
        created_tokens = True
        for actor in ('provider', 'ops', 'finance', 'buyer'):
            fd = os.open(args.tokens / (actor + '.token'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as file:
                file.write(case.tokens[actor])
        dist = root / 'frontend' / 'dist'
        if not (dist / 'index.html').is_file():
            raise RuntimeError('Build frontend before browser validation')
        m.app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='multi-cost-fixture-assets')
        @m.app.get('/console', include_in_schema=False)
        def console():
            return FileResponse(dist / 'index.html')
        print('Isolated multi-cost fixture ready; real synthetic proofs only', flush=True)
        uvicorn.run(m.app, host='127.0.0.1', port=args.port, lifespan='off', log_level='warning')
    finally:
        case.tearDown()
        case.doCleanups()
        case.tearDownClass()
        temporary.cleanup()
        if created_tokens:
            for file in args.tokens.glob('*.token'):
                file.unlink()
            args.tokens.rmdir()


if __name__ == '__main__':
    main()
