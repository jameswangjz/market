"""Bounded financial ledger PG increment; private mock stream, owned schema only."""
import argparse
from datetime import timedelta
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
from unittest.mock import patch

import r3_refund_planning_pg_acceptance as planning


class MockRedis:
    def __init__(self, stream, rows):
        self.stream, self.rows = stream, rows
        self.hashes = {}

    def xread(self, streams, count):
        return [(self.stream, self.rows[:count])]

    def info(self, section):
        if section != 'server':
            raise ValueError('Only private server identity is supported')
        return {'run_id': 'a' * 40}

    def hsetnx(self, key, field, value):
        identity = (key, field)
        if identity in self.hashes:
            return 0
        self.hashes[identity] = value
        return 1

    def hget(self, key, field):
        return self.hashes.get((key, field))


def exercise(m, client, headers):
    from sqlalchemy import select
    module = importlib.import_module(m.__package__ + '.api_charge_ledger')
    hooks = m.api_charge_ledger
    Charge, Cursor = hooks['Charge'], hooks['Cursor']
    check = planning.prior.base.check
    event = dict(order_id=planning.oid('cumulative'), segment_id='owned-planning-segment',
        scope='owned-planning-combo', period_key='owned-planning-combo:1:0', route_key='owned-route',
        charged_at_us=str(int((planning.AT-timedelta(seconds=1)).timestamp()*1000000)),
        quantity='1', policy=module.POLICY, source_run_id='a' * 40)
    redis = MockRedis(module.STREAM, [])
    results = {}

    def state():
        with m.SessionLocal() as db:
            return ([r.id for r in db.scalars(select(Charge).order_by(Charge.id))],
                    [(r.stream, r.last_id) for r in db.scalars(select(Cursor))])

    def rollback():
        before = state()
        with m.SessionLocal() as db:
            redis.rows = [('1-0', event)]
            result = hooks['ingest'](db, redis)
            check(result['inserted'] == 1, 'Expected flushed charge')
            check(db.get(Charge, '1-0') is not None and db.get(Cursor, module.STREAM).last_id == '1-0',
                  'Fact and cursor must both be visible before rollback')
            db.rollback()
        check(state() == before, 'Fact/cursor rollback mismatch')
        return dict(actual_flush=True, facts_and_cursor_rolled_back=True)

    def replay():
        redis.rows = [('1-0', event)]
        with m.SessionLocal.begin() as db:
            first = hooks['ingest'](db, redis)
        with m.SessionLocal.begin() as db:
            second = hooks['ingest'](db, redis)
        check(first['inserted'] == 1 and second['inserted'] == 0, 'Duplicate charge inserted')
        check(state() == (['1-0'], [(module.STREAM, '1-0')]), 'Unexpected durable ledger state')
        return dict(inserted=[1, 0], facts=1, watermark='1-0', forced_stream_replay=True)

    def invalid_owner():
        before = state()
        for field, value in [('order_id', planning.oid('manual')), ('segment_id', 'owned-missing-segment')]:
            rows = [('2-0', event), ('3-0', dict(event, **{field: value}))]
            rejected = False
            with m.SessionLocal() as db:
                try:
                    redis.rows = rows
                    hooks['ingest'](db, redis)
                except ValueError:
                    rejected = True
                finally:
                    db.rollback()
            check(rejected and state() == before, 'Invalid ownership advanced watermark or facts')
        return dict(wrong_order_rejected=True, wrong_segment_rejected=True,
                    preceding_valid_fact_rolled_back=True, watermark_unchanged=True)

    for name, action in [('fact_cursor_transaction_rollback', rollback),
                         ('duplicate_ingest', replay), ('invalid_order_segment', invalid_owner)]:
        try:
            results[name] = dict(status='passed', **action())
        except Exception as exc:
            results[name] = dict(status='failed', error=planning.prior.base.safe_error(exc))
    return results


def remote_run(encoded, manifest):
    with patch.object(planning.prior, 'seed', planning.seed), patch.object(planning.prior, 'exercise', exercise):
        result = planning.prior.remote_run(encoded, manifest)
    result.update(scope='BE011 financial ledger three-case PG increment; private MockRedis; no Lua/deployed claim',
                  coverage=['TRD-R3-BE-011'], gaps=['No real Redis/Lua or deployed gateway ledger claim'])
    result['source_sha256']['api_charge_ledger.py'] = manifest['api_charge_ledger.py']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pod', required=True)
    args = parser.parse_args()
    planning.prior.base.check(bool(re.fullmatch(r'market-api-[a-z0-9-]+', args.pod)), 'Invalid pod')
    planning.prior.base.check(bool(os.getenv('SSHPASS')), 'Private SSHPASS required')
    encoded, manifest = planning.prior.transport.pack_workingtree(Path(__file__).resolve().parents[1])
    payload = 'import sys,types\n'
    for name in ('r3_backend_acceptance_pg', 'r3_backend_acceptance_concurrency',
                 'r3_after_sales_group_pg_acceptance', 'r3_refund_workflow_pg_acceptance',
                 'r3_refund_planning_pg_acceptance'):
        code = Path(__file__).with_name(name+'.py').read_text()
        payload += 'm=types.ModuleType('+repr(name)+');sys.modules[m.__name__]=m;exec(compile('+repr(code)+',m.__name__,"exec"),m.__dict__)\n'
    payload += "__name__='ledger_pg_payload'\n" + Path(__file__).read_text()
    payload += '\ntry:\n print(json.dumps(remote_run('+repr(encoded)+','+repr(manifest)+')))\nexcept Exception as exc:\n print(json.dumps({"status":"failed","error":planning.prior.base.safe_error(exc)}))\n'
    command = 'kubectl --request-timeout=5s -n market exec -i '+args.pod+' -- env MESSAGE_CENTER_DISABLE_BACKGROUND=true PYTHONDONTWRITEBYTECODE=1 python -B -'
    completed = subprocess.run(['sshpass', '-e', 'ssh', '-o', 'ConnectTimeout=5', '-o',
        'ConnectionAttempts=1', '-o', 'LogLevel=ERROR', 'root@192.168.10.10', command],
        input=payload, capture_output=True, text=True, timeout=120)
    planning.prior.base.check(completed.returncode == 0, 'Bounded ledger PG transport failed; details suppressed')
    result = json.loads(completed.stdout)
    print(json.dumps(result))
    return 0 if result.get('status') == 'passed' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps(dict(status='failed', error=planning.prior.base.safe_error(exc))))
        raise SystemExit(1)
