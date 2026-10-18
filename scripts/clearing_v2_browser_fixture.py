"""Disposable SQLite browser fixture; never opens a deployed database."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile


def seed_beneficiaries(m, db, stamp, tokens):
    actors = {'expert': 'clr2-expert', 'channel': 'clr2-channel', 'unrelated': 'clr2-other', 'expert_member': 'clr2-expert-member'}
    for actor in actors.values():
        db.add(m.User(id=actor, name=actor, password_hash='unused', email=actor + '@example.invalid',
                      verified_status='verified', is_active=True, activation_status='active', platform_role=''))
    db.flush()
    db.add(m.Membership(user_id=actors['expert_member'], enterprise_id='buyer_tenant', role='member', business_roles=''))
    Participant = m.settlement_parties['Participant']
    identities = [('clr2-expert-own', 'Fixture Expert Own', 'expert', actors['expert'], '30'),
                  ('clr2-other-expert', 'PRIVATE-OTHER-PARTY', 'expert', actors['unrelated'], '50'),
                  ('clr2-member-expert', 'Fixture Enterprise Expert', 'expert', actors['expert_member'], '20'),
                  ('clr2-expert-channel', 'Fixture Expert Channel', 'channel', actors['expert'], '70'),
                  ('clr2-channel-own', 'Fixture Channel Own', 'channel', actors['channel'], '30')]
    allocations = []
    for pid, name, kind, uid, weight in identities:
        db.add(Participant(id=pid, name=name, user_id=uid, enterprise_id='buyer_tenant' if uid == actors['expert_member'] else '', owner_enterprise_id='provider_tenant',
                           kind=kind, currency='CNY', creator_id='finance'))
        allocations.append(dict(participant_id=pid, participant_name=name, participant_type=kind, currency='CNY', weight=weight))
    configuration = m.settlement_parties['Configuration'](id='clr2-beneficiary-config', product_id='product', version=1,
        allocations_json=json.dumps(allocations), creator_id='finance')
    db.add(configuration)
    db.flush()
    order = m.Order(id='clr2-beneficiary-order', order_no='PRIVATE-BUYER-ORDER', buyer_user_id='buyer',
        buyer_enterprise_id='buyer_tenant', provider_enterprise_id='provider_tenant', product_id='product',
        product_version_id='version', product_name='Beneficiary fixture product', buyer_name='Private synthetic buyer',
        amount=258, paid_amount=258, payment_status='paid', economic_snapshot_json=json.dumps(dict(currency='CNY',
            settlement_parties=dict(id=configuration.id, version=1))))
    db.add(order)
    db.flush()
    db.add(m.Payment(order_id=order.id, payment_no='PAY-BENEFICIARY-FIXTURE', amount=258,
        status='paid', paid_at=stamp, proof='Synthetic manual receipt', confirmed_by='finance'))
    db.flush()
    settlement = m.Settlement(id='clr2-beneficiary-settlement', settlement_no='SET-BENEFICIARY-FIXTURE',
        order_id=order.id, gross_amount=258, net_amount=258, cost_amount=180, profit_amount=78,
        platform_fee=10, provider_share=18, expert_fee=40, channel_fee=10, service_share=0,
        status='locked', created_at=stamp)
    batch = m.SettlementBatch(id='clr2-beneficiary-batch', batch_no='BATCH-BENEFICIARY-FIXTURE', cycle='monthly',
        period_start=stamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0), status='confirmed',
        idempotency_key='clr2-beneficiary-batch', total_amount=258, total_profit=78)
    db.add_all([settlement, batch])
    db.flush()
    db.add(m.SettlementLine(batch_id=batch.id, settlement_id=settlement.id,
        participant_type='provider', participant_id='provider_tenant', participant_name='Synthetic provider', amount=18, status='locked'))
    db.flush()
    m.settlement_finance['capture'](db, settlement, 'isolated-beneficiary-fixture')
    from sqlalchemy import select
    P = m.settlement_finance['Position']
    position = db.scalar(select(P).where(P.settlement_id == settlement.id, P.participant_id == 'clr2-expert-own'))
    tx = m.settlement_finance['draft'](db, db.get(m.User, 'finance'), dict(kind='payout', mode='manual',
        reference='PRIVATE-ACCOUNTING-VOUCHER', note='Synthetic evidence', request_id='beneficiary-fixture-payment',
        allocations=[dict(position_id=position.id, amount='5.00')]))
    m.settlement_finance['confirm'](db, db.get(m.User, 'ops'), tx['id'],
        dict(expected_version=1, request_id='beneficiary-fixture-review', note='Independent synthetic review'))
    db.commit()
    for label, uid in actors.items():
        tokens[label] = m.issue_token(db.get(m.User, uid))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8126)
    parser.add_argument('--tokens', type=Path, required=True)
    parser.add_argument('--beneficiaries', action='store_true', help='Seed isolated expert/channel identities and real finance positions')
    parser.add_argument('--check-only', action='store_true', help='Verify synthetic reporting HTTP contracts and exit')
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
            if args.beneficiaries:
                seed_beneficiaries(m, db, stamp, case.tokens)
        args.tokens.mkdir(mode=0o700, parents=True, exist_ok=False)
        tokens_created = True
        for actor in ('platform', 'ops', 'finance', 'provider', 'buyer', *(['expert', 'channel', 'unrelated', 'expert_member'] if args.beneficiaries else [])):
            path = args.tokens / (actor + '.token')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(case.tokens[actor])
        if args.check_only:
            if not args.beneficiaries:
                raise RuntimeError('--check-only requires --beneficiaries')
            for actor, participant_id, amount in [('expert', 'clr2-expert-own', '12.00'),
                    ('expert', 'clr2-expert-channel', '7.00'), ('channel', 'clr2-channel-own', '3.00'), ('expert_member', 'clr2-member-expert', '8.00')]:
                headers = {'Authorization': 'Bearer ' + case.tokens[actor]}
                if actor == 'expert_member':
                    headers['X-Enterprise-Id'] = 'buyer_tenant'
                choices = case.client.get('/api/settlement-reporting/beneficiaries', headers=headers)
                assert choices.status_code == 200 and participant_id in {p['participant_id'] for p in choices.json()['items']}
                params = dict(business_view='beneficiary', participant_id=participant_id)
                if actor == 'expert_member':
                    params['enterprise_id'] = 'buyer_tenant'
                response = case.client.get('/api/settlement-reporting/report', params=params, headers=headers)
                assert response.status_code == 200, response.text
                assert response.json()['summary']['profit_share'] == amount
                assert 'PRIVATE-OTHER-PARTY' not in response.text and 'PRIVATE-ACCOUNTING-VOUCHER' not in response.text
                csv = case.client.get('/api/settlement-reporting/export', params={**params, 'kind': 'details'}, headers=headers)
                assert csv.status_code == 200 and participant_id in csv.text
                denied = case.client.get('/api/settlement-reporting/report', params={**params, 'participant_id': 'clr2-other-expert'}, headers=headers)
                assert denied.status_code == 403
            print('Synthetic beneficiary HTTP/CSV checks passed', flush=True)
            return
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
