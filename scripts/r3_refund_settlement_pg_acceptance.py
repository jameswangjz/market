"""BE013 owned-schema PostgreSQL acceptance; synthetic resources, simulated funds.

No deployment, startup, public writes, or protected-order content reads. The
parent harness permits only a count of the protected order. Run explicitly with
the private transport; local verification must not invoke this entry point.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import importlib
import io
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from unittest.mock import patch

import r3_refund_execution_pg_acceptance as execution_probe

base = execution_probe.base
transport = execution_probe.transport
cross = execution_probe.cross
fixture = execution_probe.fixture
CASES = ('negative_idempotence_frozen_cost', 'same_refund_two_connections',
         'audit_failure_rollback_retry', 'revised_refund_monthly_reports')
SOURCES = tuple(dict.fromkeys(execution_probe.SOURCES + ('refund_settlement.py',)))


def seed(m):
    headers = fixture.seed(m)
    source, graphs = cross.MockRedis(), {}
    for name in CASES:
        with m.SessionLocal.begin() as db:
            graphs[name] = cross.graph(m, db, source, 'be013-' + name)
    m._owned_settlement = source, graphs
    return headers


def exercise(m, client, headers):
    from decimal import Decimal, ROUND_HALF_UP
    from sqlalchemy import func, select, text

    implementation = importlib.import_module(m.__package__ + '.refund_execution')
    worker = importlib.import_module(m.__package__ + '.refund_execution_worker')
    source, graphs = m._owned_settlement
    hooks = m.refund_execution
    Link = m.refund_settlement['Model']
    Recovery = m.refund_execution_resources['Recovery']
    results = {}

    def call(path, body, actor='admin', params=None):
        response = client.post(path, json=body, headers=headers[actor], params=params)
        base.check(response.status_code == 200, 'Owned settlement fixture ASGI failure')
        return response.json()

    def prepare(db, order, refund, envelope):
        term = db.scalar(select(m.subscription_terms['Term']).where(
            m.subscription_terms['Term'].order_id == order.id))
        base.check(term is not None, 'Actual owned term required')
        combo = db.get(m.subscription_terms['Combination'], term.combination_id)
        plan = dict(refund_id=refund.id, order_id=order.id, term_id=term.id,
                    combination_id=combo.id, generation=combo.generation,
                    kind='synthetic_pg_resource', synthetic=True,
                    frozen_envelope_hash=implementation.digest(envelope))
        plan['plan_hash'] = implementation.digest(plan)
        return plan

    def apply(db, execution):
        plan = json.loads(execution.resource_plan_json)
        proof = dict(refund_id=execution.refund_id,
                     resource_plan_hash=execution.resource_plan_hash, synthetic=True,
                     evidence_fingerprint=implementation.digest(dict(
                         refund_id=execution.refund_id, synthetic=True)))
        existing = db.scalar(select(Recovery).where(Recovery.refund_id == execution.refund_id))
        if existing is None:
            previous = db.scalar(select(func.max(Recovery.recovery_number)).where(
                Recovery.term_id == plan['term_id'])) or 0
            db.add(Recovery(refund_id=execution.refund_id, execution_id=execution.id,
                order_id=plan['order_id'], term_id=plan['term_id'],
                combination_id=plan['combination_id'], generation=plan['generation'],
                recovery_number=previous + 1, stage='resources_applied',
                plan_json=implementation.canonical(plan), proof_json=implementation.canonical(proof),
                payload_hash=implementation.digest(dict(plan=plan, proof=proof))))
            db.flush()
        else:
            base.check(json.loads(existing.plan_json) == plan and
                       json.loads(existing.proof_json) == proof,
                       'Synthetic recovery replay mismatch')
        return proof

    def completed(name):
        oid = graphs[name]['roots'][0]
        row = call('/api/orders/' + oid + '/refund-requests',
            dict(amount='1.00', reason='Owned BE013 acceptance', idempotency_key=name + '-request'),
            actor='buyer', params=dict(enterprise_id='owned-buyer', business_view='buyer'))
        rid = row['id']
        call('/api/refunds/' + rid + '/review', dict(decision='approve',
            reason='Owned approval', expected_version=row['version'], idempotency_key=name + '-approve'))
        with m.SessionLocal() as db:
            version = db.get(m.refund_workflow['Case'], rid).version
        call('/api/refunds/' + rid + '/execute', dict(expected_version=version,
            reason='Owned simulated execution', idempotency_key=name + '-execute'))
        for _ in range(16):
            with m.SessionLocal() as db:
                execution = db.scalar(select(hooks['Execution']).where(hooks['Execution'].refund_id == rid))
                if execution.status == 'completed':
                    base.check(db.get(hooks['Completion'], rid) is not None,
                               'Completion evidence missing')
                    base.check(db.scalar(select(Recovery.id).where(Recovery.refund_id == rid)) is not None,
                               'Durable resource recovery missing')
                    return rid
                base.check(execution.status != 'failed', 'Owned executor failed')
            base.check(worker.process_once(ns=vars(m)), 'Owned executor stalled')
        raise base.AcceptanceFailure('Bounded completion attempts exhausted')

    def counts(db, rid):
        return tuple(db.scalar(select(func.count()).select_from(model).where(column == rid))
                     for model, column in ((m.Settlement, m.Settlement.refund_id), (Link, Link.refund_id)))

    def create(db, rid):
        cross.owned(db)
        return m.create_refund_negative_settlement(db, db.get(m.Refund, rid), 'owned-be013')

    def idempotence():
        rid = completed(CASES[0])
        with m.SessionLocal.begin() as db:
            refund = db.get(m.Refund, rid)
            plan = json.loads(db.get(hooks['Completion'], rid).economic_plan_json)
            segments = plan.get('segments', [plan])
            cents = Decimal('0.01')
            expected_cost = sum((Decimal(p.get('cost', p['economic_snapshot']['total_cost'])) *
                Decimal(p['cumulative_refund_amount']) / Decimal(p['original_amount'])).quantize(
                    cents, rounding=ROUND_HALF_UP) -
                (Decimal(p.get('cost', p['economic_snapshot']['total_cost'])) *
                 (Decimal(p['cumulative_refund_amount']) - Decimal(p['refund_amount'])) /
                 Decimal(p['original_amount'])).quantize(cents, rounding=ROUND_HALF_UP)
                for p in segments)
            base.check(expected_cost > 0, 'Fixture must exercise nonzero frozen cost')
            order = db.get(m.Order, refund.order_id)
            for release in db.scalars(select(m.ProductReleaseVersion).where(
                    m.ProductReleaseVersion.product_id == order.product_id)):
                release.cost = Decimal('999.00')
            db.flush()
            settlement = create(db, rid)
            sid = settlement.id
            base.check(settlement.gross_amount == -refund.amount and
                       settlement.net_amount == -refund.amount and
                       settlement.cost_amount == -expected_cost and
                       settlement.profit_amount == -refund.amount + expected_cost,
                       'Negative settlement must use frozen economics')
            base.check(sum(getattr(settlement, key) for key in
                ('platform_fee', 'provider_share', 'service_share', 'expert_fee', 'channel_fee')) ==
                settlement.profit_amount, 'Shares must reconcile to reversed profit')
            base.check(create(db, rid).id == sid, 'Same-transaction idempotence failed')
        with m.SessionLocal.begin() as db:
            base.check(create(db, rid).id == sid and counts(db, rid) == (1, 1),
                       'Committed settlement/link must remain unique')
        return dict(negative_amounts=True, frozen_cost=True, idempotent=True, settlements=1, links=1)

    def concurrent():
        rid = completed(CASES[1])
        barrier = threading.Barrier(2, timeout=8)

        def contender():
            with m.SessionLocal.begin() as db:
                cross.owned(db)
                pid = db.scalar(text('SELECT pg_backend_pid()'))
                barrier.wait()
                sid = create(db, rid).id
            return pid, sid

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contender) for _ in range(2)]
            answers = [future.result(timeout=15) for future in futures]
        base.check(len({row[0] for row in answers}) == 2, 'Distinct PostgreSQL connections required')
        base.check(len({row[1] for row in answers}) == 1, 'Concurrent mapping diverged')
        with m.SessionLocal() as db:
            base.check(counts(db, rid) == (1, 1), 'Concurrent insertion left duplicate/orphan rows')
        return dict(pg_connections=2, same_settlement=True, settlements=1, links=1)

    def rollback():
        rid = completed(CASES[2])
        original = m.audit
        faults = []

        def fault(db, actor, action, *args, **kwargs):
            if action == 'ordinary_refund_negative_settlement':
                faults.append(True)
                original(db, actor, action, *args, **kwargs)
                db.flush()
                raise RuntimeError('Synthetic settlement audit failure')
            return original(db, actor, action, *args, **kwargs)

        try:
            with patch.object(m, 'audit', side_effect=fault), m.SessionLocal.begin() as db:
                create(db, rid)
        except RuntimeError:
            base.check(len(faults) == 1, 'Expected settlement audit injection')
        else:
            raise base.AcceptanceFailure('Audit failure did not abort transaction')
        with m.SessionLocal.begin() as db:
            base.check(counts(db, rid) == (0, 0), 'Audit failure left orphan settlement/link')
            refund = db.get(m.Refund, rid)
            base.check(db.scalar(select(func.count()).select_from(m.AuditLog).where(
                m.AuditLog.action == 'ordinary_refund_negative_settlement',
                m.AuditLog.order_id == refund.order_id)) == 0, 'Failed audit persisted')
            base.check(refund.status == 'completed' and db.get(hooks['Completion'], rid) is not None,
                       'Settlement rollback damaged completed execution')
            sid = create(db, rid).id
        with m.SessionLocal.begin() as db:
            base.check(create(db, rid).id == sid and counts(db, rid) == (1, 1),
                       'Retry did not create exactly one durable mapping')
        return dict(audit_fault_injected=True, rollback_no_orphans=True, retry_completed=True)

    def monthly_reports():
        BUSINESS_TIMEZONE = importlib.import_module(m.__package__ + '.subscription_calendar').BUSINESS_TIMEZONE
        name = CASES[3]
        graph = graphs[name]
        oid = graph['roots'][0]
        instant = m.now()
        local = instant.astimezone(BUSINESS_TIMEZONE)
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        # Keep the original payment inside the selected natural month even
        # when graph's seven-day anchor falls in the previous month.
        with m.SessionLocal.begin() as db:
            for payment in db.scalars(select(m.Payment).where(m.Payment.order_id == oid)):
                payment.paid_at = instant
        rid = completed(name)
        with m.SessionLocal() as db:
            order = db.get(m.Order, oid)
            order_no = order.order_no
            refund = db.get(m.Refund, rid)
            refund_no = refund.refund_no
            plan = json.loads(db.get(hooks['Completion'], rid).economic_plan_json)
            segments = plan.get('segments', [plan])
            base.check(all(p['calculation_version'] == 'economic-segment-refund-v1' for p in segments),
                       'Revised economic refund plan required')
            refund_cost = sum(Decimal(p['cost_reversal_delta']) for p in segments)
            Adjustment = m.version_change_ledger['Model']
            adjustments = db.scalars(select(Adjustment).where(
                Adjustment.change_id == graph['change'], Adjustment.source_order_id == oid)).all()
            base.check(len(adjustments) == 2 and {row.side for row in adjustments} == {'old', 'target'},
                       'Both frozen VC sides required')
            expected_vc = {row.id: (row.revenue, row.cost, row.profit) for row in adjustments}
            expected = dict(gross_amount=Decimal('10.00') + sum(row.revenue for row in adjustments) - refund.amount,
                cost_amount=Decimal('2.00') + sum(row.cost for row in adjustments) - refund_cost,
                profit_amount=Decimal('8.00') + sum(row.profit for row in adjustments) - refund.amount + refund_cost)
            expected['net_amount'] = expected['gross_amount']
        payload = dict(cycle='monthly', period_start=start.isoformat(), period_end=end.isoformat(),
                       order_ids=[oid], idempotency_key=name + '-batch')

        def inspect_batch(batch_id):
            with m.SessionLocal() as db:
                cross.owned(db)
                lines = db.scalars(select(m.SettlementLine).where(
                    m.SettlementLine.batch_id == batch_id, m.SettlementLine.status != 'superseded')).all()
                ids = {line.settlement_id for line in lines}
                settlements = db.scalars(select(m.Settlement).where(m.Settlement.id.in_(ids))).all()
                base.check(len(settlements) == 4 and len(lines) == 20 and
                           all(row.order_id == oid for row in settlements),
                           'Monthly scope must exclude other root and funding purchase')
                for settlement in settlements:
                    own_lines = [line for line in lines if line.settlement_id == settlement.id]
                    base.check(len(own_lines) == 5 and {line.participant_type for line in own_lines} ==
                               {'platform', 'provider', 'service', 'expert', 'channel'},
                               'Each settlement requires exactly five participant lines')
                    base.check(sum(line.amount for line in own_lines) == settlement.profit_amount,
                               'Participant amounts must reconcile')
                vc_links = db.scalars(select(m.VersionChangeSettlementLink).where(
                    m.VersionChangeSettlementLink.settlement_id.in_(ids))).all()
                base.check({link.adjustment_id for link in vc_links} == set(expected_vc),
                           'Monthly batch must contain exactly the selected root VC sides')
                by_id = {row.id: row for row in settlements}
                for link in vc_links:
                    row = by_id[link.settlement_id]
                    base.check((row.gross_amount, row.cost_amount, row.profit_amount) == expected_vc[link.adjustment_id],
                               'VC signed economics changed')
                link = db.get(Link, rid)
                base.check(link is not None and link.settlement_id in ids and counts(db, rid) == (1, 1),
                           'Exactly one ordinary negative mapping required')
                negative = by_id[link.settlement_id]
                base.check(negative.is_refund and negative.gross_amount == Decimal('-1.00') and
                           negative.cost_amount == -refund_cost, 'Ordinary negative signs incorrect')
                for field, amount in expected.items():
                    base.check(sum(getattr(row, field) for row in settlements) == amount,
                               'Monthly signed aggregate mismatch')
                batch = db.get(m.SettlementBatch, batch_id)
                base.check(batch.total_amount == expected['net_amount'] and batch.total_profit == expected['profit_amount'],
                           'Batch totals differ from frozen signed economics')
                return link.settlement_id

        first = call('/api/settlement-batches', payload)
        negative_id = inspect_batch(first['id'])
        repeated = call('/api/settlement-batches', payload)
        base.check(repeated['id'] == first['id'] and repeated.get('idempotent'), 'Batch request replay failed')
        rebuilt = call('/api/settlement-batches', dict(payload, rebuild=True, idempotency_key=name + '-rebuild'))
        base.check(rebuilt['id'] != first['id'] and inspect_batch(rebuilt['id']) == negative_id,
                   'Unlocked rebuild duplicated negative settlement')
        with m.SessionLocal() as db:
            base.check(db.get(m.SettlementBatch, first['id']).status == 'superseded' and
                       db.scalar(select(func.count()).select_from(m.SettlementLine).where(
                           m.SettlementLine.batch_id == first['id'], m.SettlementLine.status != 'superseded')) == 0,
                       'Rebuild left active old participant lines')

        response = client.get('/api/settlement-reports', headers=headers['admin'])
        base.check(response.status_code == 200, 'Monthly report request failed')
        report = response.json()
        base.check(report['summary']['count'] == 4 and report['summary']['order_count'] == 1 and
                   len(report['items']) == 4 and {row['order_id'] for row in report['items']} == {oid},
                   'Report must count one associated order and four settlements')
        base.check(len(report['monthly']) == 1 and len(report['products']) == 1 and
                   all(row['order_count'] == 1 for row in report['monthly'] + report['products'] + report['summary']['participants']),
                   'Monthly/product/participant counts must deduplicate the original order')
        for field, amount in expected.items():
            base.check(Decimal(str(report['summary'][field])).quantize(Decimal('0.01')) == amount,
                       'Report changed signed monetary totals')
        base.check(sorted(row['source_kind'] for row in report['items']) ==
                   ['order', 'ordinary_refund', 'version_change', 'version_change'], 'Report source kinds incorrect')
        ordinary = [row for row in report['items'] if row['source_kind'] == 'ordinary_refund']
        base.check(len(ordinary) == 1 and ordinary[0]['refund_id'] == rid and
                   ordinary[0]['refund_no'] == refund_no and ordinary[0]['is_refund'] and
                   ordinary[0]['order_no'] == order_no, 'Report lost original ORD/REF identity')
        exported = client.get('/api/settlement-reports/export', params=dict(kind='details'), headers=headers['admin'])
        base.check(exported.status_code == 200, 'Report CSV request failed')
        csv_rows = list(csv.DictReader(io.StringIO(exported.content.decode('utf-8-sig'))))
        csv_refunds = [row for row in csv_rows if row['source_kind'] == 'ordinary_refund']
        base.check(len(csv_rows) == 4 and len(csv_refunds) == 1 and
                   csv_refunds[0]['refund_id'] == rid and csv_refunds[0]['refund_no'] == refund_no and
                   csv_refunds[0]['order_no'] == order_no and csv_refunds[0]['is_refund'] == 'True',
                   'CSV lost original ORD/REF identity')
        return dict(settlements=4, participant_lines=20, refund_links=1, distinct_orders=1,
                    signed_totals=True, unlocked_rebuild=True, report_csv_identity=True,
                    limitation='Seeded cross.graph economic adjustments; not a full real Saga or supplier recovery')

    with patch.object(m, 'apisix_policy_redis', return_value=source), \
            patch.dict(m.refund_execution_resources, prepare=prepare, apply=apply):
        # Run reporting before the standalone cases add other report rows.
        runs = ((CASES[3], monthly_reports), *zip(CASES[:3], (idempotence, concurrent, rollback)))
        for name, run in runs:
            try:
                results[name] = dict(status='passed', **run())
            except Exception as exc:
                results[name] = cross.safe_error(exc)
    return results


def remote_run(encoded, manifest):
    with patch.object(cross, 'seed', seed), patch.object(cross, 'exercise', exercise), \
            patch.object(cross, 'CASE_NAMES', CASES), patch.object(cross, 'SOURCE_FILES', SOURCES):
        result = cross.remote_run(encoded, manifest)
    result.update(incremental_scope='BE013 owned PostgreSQL settlement adapter',
        scope='Actual PostgreSQL owned schema and executor; synthetic resource evidence; simulated funds',
        coverage=[], deployed=False,
        external_adapters=dict(resources='Synthetic prepare/apply; durable Recovery with real owned term/combo IDs',
                               funds='Platform simulated payment legs', Redis='MockRedis'),
        gaps=['No actual Redis/APISIX/supplier recovery or bank execution',
              'Cross-version economic graph is seeded, not a full real Saga',
              'No browser, deployment, or abrupt process termination coverage'])
    return result


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pod', required=True)
    args = parser.parse_args()
    base.check(bool(re.fullmatch(r'market-api-[a-z0-9-]+', args.pod)) and bool(os.getenv('SSHPASS')),
               'Bounded pod and private transport required')
    encoded, manifest = transport.pack_workingtree(Path(__file__).resolve().parents[1])
    base.check(all(name in manifest for name in SOURCES), 'Settlement package source incomplete')
    payload = 'import sys,types\n'
    for name in ('r3_backend_acceptance_pg', 'r3_backend_acceptance_concurrency',
                 'r3_after_sales_group_pg_acceptance', 'r3_refund_workflow_pg_acceptance',
                 'r3_refund_cross_version_pg_acceptance', 'r3_refund_execution_pg_acceptance'):
        source = Path(__file__).with_name(name + '.py').read_text()
        payload += ('m=types.ModuleType(' + repr(name) + ');sys.modules[m.__name__]=m;exec(compile('
                    + repr(source) + ',m.__name__,"exec"),m.__dict__)\n')
    payload += "__name__='settlement_remote'\n" + Path(__file__).read_text()
    payload += '\nprint(json.dumps(remote_run(' + repr(encoded) + ',' + repr(manifest) + ')))\n'
    command = ('kubectl --request-timeout=5s -n market exec -i ' + args.pod
               + ' -- env MESSAGE_CENTER_DISABLE_BACKGROUND=true PYTHONDONTWRITEBYTECODE=1 python -B -')
    process = subprocess.run(['sshpass', '-e', 'ssh', '-o', 'ConnectTimeout=5', '-o', 'LogLevel=ERROR',
        'root@192.168.10.10', command], input=payload, capture_output=True, text=True, timeout=180)
    base.check(process.returncode == 0, 'Private transport failed')
    result = json.loads(process.stdout)
    print(json.dumps(result))
    return 0 if result.get('status') == 'passed' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps(dict(status='failed', error=cross.safe_error(exc))))
        raise SystemExit(1)
