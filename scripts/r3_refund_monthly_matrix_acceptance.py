"""Local QA006 matrix; no transport, deployment, or production writes.

Reuse the settlement harness graph and identities. Version-change history is
seeded, Redis and resource recovery are synthetic, and funds are simulated.
The caller supplies a disposable application database and ASGI client.
"""
import csv
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import io
import json
from unittest.mock import patch

import r3_refund_settlement_pg_acceptance as harness

CASE_NAMES = ('cross_month_partial_refunds_after_version_change',)
SOURCES = harness.SOURCES
seed = harness.fixture.seed
FROZEN_RATES = dict(platform_rate=20, provider_rate=50, service_rate=10,
                    expert_rate=10, channel_rate=10)
EXPECTED_SHARES = dict(platform=Decimal('-0.16'), provider=Decimal('-0.40'),
                       service=Decimal('-0.08'), expert=Decimal('-0.08'), channel=Decimal('-0.08'))


def exercise(m, client, headers):
    try:
        return {CASE_NAMES[0]: dict(status='passed', **monthly_matrix(m, client, headers))}
    except Exception as exc:
        return {CASE_NAMES[0]: harness.cross.safe_error(exc)}


def remote_run(encoded, manifest):
    with patch.object(harness.cross, 'seed', seed), \
            patch.object(harness.cross, 'exercise', exercise), \
            patch.object(harness.cross, 'CASE_NAMES', CASE_NAMES), \
            patch.object(harness.cross, 'SOURCE_FILES', SOURCES):
        result = harness.cross.remote_run(encoded, manifest)
    result.update(incremental_scope='QA006 cross-month partial refund monthly matrix',
        scope='Owned PostgreSQL and real ASGI/worker/settlement functions; seeded version change',
        external_adapters=dict(Redis='MockRedis', resources='Synthetic prepare/apply with durable Recovery',
                               funds='Platform simulated payment legs'),
        gaps=['Version-change history is seeded, not full Saga execution',
              'No actual Redis/APISIX/supplier or bank execution',
              'No deployment, browser, concurrency, or process-kill coverage'])
    return result


def monthly_matrix(m, client, headers):
    from sqlalchemy import func, select

    check = harness.base.check
    source = harness.cross.MockRedis()
    # Fixed dates cover the year boundary without depending on the wall clock.
    paid_at = datetime(2026, 12, 15, tzinfo=timezone.utc)
    completed_at = datetime(2027, 1, 2, tzinfo=timezone.utc)
    with patch.object(m, 'now', return_value=paid_at), m.SessionLocal.begin() as db:
        graph = harness.cross.graph(m, db, source, 'qa006-monthly', frozen_rates=FROZEN_RATES)
        oid = graph['roots'][0]
        for payment in db.scalars(select(m.Payment).where(m.Payment.order_id == oid)):
            payment.paid_at = paid_at

    def call(path, body):
        response = client.post(path, json=body, headers=headers['admin'])
        check(response.status_code == 200, 'Matrix POST failed: ' + path + ' ' + response.text)
        return response.json()

    def batch(key, month, **extra):
        start, end = (('2026-11-30', '2026-12-31') if month == 12 else
                      ('2026-12-31', '2027-01-31'))
        return call('/api/settlement-batches', dict(cycle='monthly',
            period_start=start + 'T16:00:00+00:00', period_end=end + 'T16:00:00+00:00',
            order_ids=[oid], idempotency_key=key, **extra))

    def snapshot(batch_id):
        with m.SessionLocal() as db:
            models = (m.SettlementBatch, m.SettlementLine, m.Settlement)
            ids = set(db.scalars(select(m.SettlementLine.settlement_id).where(
                m.SettlementLine.batch_id == batch_id)))
            predicates = (m.SettlementBatch.id == batch_id,
                          m.SettlementLine.batch_id == batch_id, m.Settlement.id.in_(ids))
            return tuple(tuple(tuple(row) for row in db.execute(select(model.__table__).where(
                predicate).order_by(*model.__table__.primary_key.columns)))
                for model, predicate in zip(models, predicates))

    original = batch('qa006-original', 12)
    with m.SessionLocal() as db:
        original_ids = set(db.scalars(select(m.SettlementLine.settlement_id).where(
            m.SettlementLine.batch_id == original['id'])))
    check(len(original_ids) == 3, 'Expected original order and two version-change sides')
    for sid in original_ids:
        call('/api/settlements/' + sid + '/lock', {})
    call('/api/settlement-batches/' + original['id'] + '/confirm', {})
    call('/api/settlement-batches/' + original['id'] + '/pay', {})
    before = snapshot(original['id'])

    implementation = importlib.import_module(m.__package__ + '.refund_execution')
    worker = importlib.import_module(m.__package__ + '.refund_execution_worker')

    def prepare(db, order, refund, envelope):
        term = db.scalar(select(m.subscription_terms['Term']).where(
            m.subscription_terms['Term'].order_id == order.id))
        combo = db.get(m.subscription_terms['Combination'], term.combination_id)
        plan = dict(refund_id=refund.id, order_id=order.id, kind='synthetic_pg_resource',
                    term_id=term.id, combination_id=combo.id, generation=combo.generation,
                    frozen_envelope_hash=implementation.digest(envelope))
        plan['plan_hash'] = implementation.digest(plan)
        return plan

    def apply(db, execution):
        plan = json.loads(execution.resource_plan_json)
        proof = dict(refund_id=execution.refund_id, resource_plan_hash=execution.resource_plan_hash,
                    evidence_fingerprint=implementation.digest(dict(refund_id=execution.refund_id, synthetic=True)))
        Recovery = m.refund_execution_resources['Recovery']
        if db.scalar(select(Recovery).where(Recovery.refund_id == execution.refund_id)) is None:
            previous = db.scalar(select(func.max(Recovery.recovery_number)).where(
                Recovery.term_id == plan['term_id'])) or 0
            db.add(Recovery(refund_id=execution.refund_id, execution_id=execution.id,
                order_id=plan['order_id'], term_id=plan['term_id'], combination_id=plan['combination_id'],
                generation=plan['generation'], recovery_number=previous + 1, stage='resources_applied',
                plan_json=implementation.canonical(plan), proof_json=implementation.canonical(proof),
                payload_hash=implementation.digest(dict(plan=plan, proof=proof))))
            db.flush()
        return proof

    refunds, expected_cost = [], Decimal(0)
    with patch.object(m, 'now', return_value=completed_at), \
            patch.object(m, 'apisix_policy_redis', return_value=source), \
            patch.dict(m.refund_execution_resources, prepare=prepare, apply=apply):
        for index in range(2):
            response = client.post('/api/orders/' + oid + '/refund-requests',
                headers=headers['buyer'], params=dict(enterprise_id='owned-buyer', business_view='buyer'),
                json=dict(amount='1.00', reason='QA006 partial refund',
                          idempotency_key='qa006-request-' + str(index)))
            check(response.status_code == 200, 'Partial request failed')
            row = response.json()
            approved = call('/api/refunds/' + row['id'] + '/review', dict(decision='approve',
                reason='QA006 approval', expected_version=row['version'],
                idempotency_key='qa006-review-' + str(index)))
            call('/api/refunds/' + row['id'] + '/execute', dict(expected_version=approved['version'],
                reason='QA006 simulated execution', idempotency_key='qa006-execute-' + str(index)))
            for _ in range(16):
                if not worker.process_once(ns=vars(m)):
                    break
            else:
                check(False, 'Worker did not quiesce within 16 steps')
            with m.SessionLocal() as db:
                refund = db.get(m.Refund, row['id'])
                check(refund.status == 'completed', 'Partial execution did not complete')
                plan = json.loads(db.get(m.refund_execution['Completion'], refund.id).economic_plan_json)
                segments = plan.get('segments', [plan])
                check(all(p['calculation_version'] == 'economic-segment-refund-v1' for p in segments),
                      'Expected revised economic plans')
                expected_cost += sum(Decimal(p['cost_reversal_delta']) for p in segments)
            refunds.append(row['id'])

    check(expected_cost == Decimal('0.40'), 'Two unit refunds must reverse forty cents of frozen cost')
    first = batch('qa006-refunds', 1)
    check(batch('qa006-refunds', 1)['id'] == first['id'], 'Batch replay changed identity')

    def inspect(batch_id):
        with m.SessionLocal() as db:
            negatives = list(db.scalars(select(m.Settlement).where(m.Settlement.refund_id.in_(refunds))))
            check(len(negatives) == 2, 'Expected exactly two negative settlements')
            check(sum(s.cost_amount for s in negatives) == -expected_cost, 'Cost reversal mismatch')
            check(sum(s.net_amount for s in negatives) == Decimal('-2'), 'Partial refund sum mismatch')
            lines = list(db.scalars(select(m.SettlementLine).where(m.SettlementLine.batch_id == batch_id)))
            check(len(lines) == 10, 'Refund month must have exactly ten lines')
            for settlement in negatives:
                own = [line for line in lines if line.settlement_id == settlement.id]
                check(len(own) == 5 and {line.participant_type for line in own} ==
                      {'platform', 'provider', 'service', 'expert', 'channel'}, 'Five parties required')
                check(sum(line.amount for line in own) == settlement.profit_amount, 'Share sum mismatch')
                check({line.participant_type: line.amount for line in own} == EXPECTED_SHARES,
                      'Each negative must allocate exact nonzero frozen shares')
                check(tuple(getattr(settlement, field) for field in
                    ('platform_fee', 'provider_share', 'service_share', 'expert_fee', 'channel_fee')) ==
                    tuple(EXPECTED_SHARES.values()), 'Settlement frozen share allocation mismatch')
            return {s.id for s in negatives}

    negative_ids = inspect(first['id'])
    rebuilt = batch('qa006-rebuild', 1, rebuild=True)
    check(rebuilt['id'] != first['id'] and inspect(rebuilt['id']) == negative_ids, 'Rebuild duplicated refund')
    with m.SessionLocal() as db:
        old_lines = list(db.scalars(select(m.SettlementLine).where(m.SettlementLine.batch_id == first['id'])))
        check(len(old_lines) == 10 and all(line.status == 'superseded' for line in old_lines),
              'Rebuild left active old lines')
    params = dict(start='2027-01-01T00:00:00+08:00', end='2027-02-01T00:00:00+08:00')
    report_response = client.get('/api/settlement-reports', params=params, headers=headers['admin'])
    check(report_response.status_code == 200, 'Monthly report failed')
    report = report_response.json()
    check({r['id'] for r in report['items']} == negative_ids and len(report['items']) == 2,
          'Report duplicated or omitted partial refunds')
    check(Decimal(str(report['summary']['net_amount'])) == Decimal('-2') and
          Decimal(str(report['summary']['cost_amount'])) == -expected_cost and
          Decimal(str(report['summary']['profit_amount'])) == Decimal('-1.60'),
          'Monthly report monetary totals mismatch')
    response = client.get('/api/settlement-reports/export', params=params, headers=headers['admin'])
    check(response.status_code == 200, 'CSV export failed')
    rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    check(len(rows) == 2 and {r['refund_id'] for r in rows} == set(refunds), 'CSV refund identities mismatch')
    share_headers = ('\u5e73\u53f0\u8fd0\u8425\u65b9', '\u6570\u636e/\u670d\u52a1\u63d0\u4f9b\u65b9',
                     '\u6570\u636e\u670d\u52a1\u65b9', '\u4e13\u5bb6', '\u6e20\u9053')
    for row in rows:
        check(sum(Decimal(row[k]) for k in share_headers) == Decimal(row['profit_amount']), 'CSV share sum mismatch')
        check(tuple(Decimal(row[k]) for k in share_headers) == tuple(EXPECTED_SHARES.values()),
              'CSV must preserve all five exact nonzero allocations')
    check(sum(Decimal(r['cost_amount']) for r in rows) == -expected_cost, 'CSV cost mismatch')
    check(sum(Decimal(r['net_amount']) for r in rows) == Decimal('-2'), 'CSV net mismatch')
    check(snapshot(original['id']) == before, 'Paid original batch, lines, or settlements changed')
    return dict(refunds=2, participant_lines=10, csv_rows=2, cost_reversal=str(expected_cost),
                original_paid_batch_unchanged=True, rebuild_idempotent=True,
                five_nonzero_exact_shares=True)
