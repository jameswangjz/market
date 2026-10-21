"""Wrap the existing disposable browser fixture; never load deployed app.main.

Start only after the prior 8126 fixture has stopped. No existing helper is edited.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from unittest.mock import patch

SETTLEMENT_ID = 'clr2-funding-settlement'
SETTLEMENT_NO = 'SET-FUNDING-FIXTURE'
REQUEST_ID = 'cccccccccccccccccccccccccccccccc'


def seed(case):
    from sqlalchemy import event
    m = case.m
    if m.engine.dialect.name != 'sqlite' or str(m.engine.url) != 'sqlite://':
        raise RuntimeError('Only the isolated in-memory test database is allowed')
    stamp = datetime.now(timezone.utc)
    with m.SessionLocal() as db:
        order = m.Order(id='clr2-funding-order', order_no='ORD-FUNDING-FIXTURE', buyer_user_id='buyer',
            buyer_enterprise_id='buyer_tenant', provider_enterprise_id='provider_tenant', product_id='product',
            product_version_id='version', product_name='Synthetic funding fixture', buyer_name='Synthetic buyer',
            amount=150, paid_amount=150, payment_status='paid', economic_snapshot_json=json.dumps({'currency': 'CNY'}))
        db.add(order)
        db.flush()
        db.add(m.Payment(id='clr2-funding-payment', order_id=order.id, payment_no='PAY-FUNDING-FIXTURE',
                         amount=150, status='paid', paid_at=stamp))
        batch = m.SettlementBatch(id='clr2-funding-batch', batch_no='BATCH-FUNDING-FIXTURE', cycle='monthly',
            period_start=stamp.replace(day=1), status='confirmed', idempotency_key='clr2-funding', total_amount=160, total_profit=60)
        settlement = m.Settlement(id=SETTLEMENT_ID, settlement_no=SETTLEMENT_NO, order_id=order.id,
            gross_amount=160, net_amount=160, cost_amount=100, profit_amount=60, platform_fee=10,
            provider_share=50, service_share=0, expert_fee=0, channel_fee=0, status='locked', created_at=stamp)
        db.add_all([batch, settlement])
        db.flush()
        for kind, amount in (('platform', 10), ('provider', 50)):
            db.add(m.SettlementLine(batch_id=batch.id, settlement_id=settlement.id, participant_type=kind,
                participant_id='platform' if kind == 'platform' else 'provider_tenant',
                participant_name=kind, amount=amount, status='locked'))
        db.flush()
        baseline = m.settlement_finance['capture'](db, settlement, 'isolated-funding-browser')
        if baseline.snapshot_hash is None:
            raise RuntimeError('Real funding baseline capture failed')
        db.commit()
    Request = m.settlement_finance['FundingRequest']
    @event.listens_for(Request, 'before_insert')
    def fixed_owned_request(mapper, connection, row):
        if row.settlement_id != SETTLEMENT_ID or row.creator_id != 'platform':
            raise RuntimeError('Funding browser fixture accepts only the fixed proposal')
        row.id = REQUEST_ID


def main():
    if '--port' in sys.argv and sys.argv[sys.argv.index('--port') + 1] != '8126':
        raise RuntimeError('Only localhost port 8126 is supported')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend/tests'))
    import test_message_business_events as fixtures
    import clearing_v2_browser_fixture as browser
    import uvicorn
    original_setup = fixtures.BusinessRoutesHTTPTests.setUp
    original_run = uvicorn.run
    def setup(case):
        original_setup(case)
        seed(case)
    def serve(app, **kwargs):
        kwargs.update(host='127.0.0.1', port=8126)
        return original_run(app, **kwargs)
    # The original management mode supplies the read-only security token.
    argv = [*sys.argv, *([] if '--management' in sys.argv else ['--management'])]
    with patch.object(fixtures.BusinessRoutesHTTPTests, 'setUp', setup), \
            patch.object(uvicorn, 'run', serve), patch.object(sys, 'argv', argv):
        browser.main()


if __name__ == '__main__':
    main()
