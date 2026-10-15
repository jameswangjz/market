"""Bounded QA005 SIGKILL extension: synthetic resources, simulated funds only.

No transport entry point. Main must review before invoking remote_run through
the existing owned-schema source harness. This is not native gateway/bank evidence.
"""
import json
import os
import selectors
import signal
import subprocess
import time
from unittest.mock import patch

import r3_refund_execution_pg_acceptance as execution

base = execution.base
CASE = 'abrupt_kill_after_durable_simulated_leg'
MARKER = 'QA005_DURABLE_LEG_READY'


def kill_child(args, kwargs, durable_check, timeout=20):
    """Handshake before kill; always reap even on timeout or failed assertions."""
    with subprocess.Popen(args, **kwargs) as child:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                deadline, output = time.monotonic() + timeout, b''
                while b'\n' not in output:
                    remaining = deadline - time.monotonic()
                    base.check(remaining > 0 and bool(selector.select(remaining)), 'Child handshake timeout')
                    chunk = os.read(child.stdout.fileno(), 256)
                    base.check(bool(chunk) and len(output) + len(chunk) <= 256, 'Child handshake missing')
                    output += chunk
                base.check(output.strip() == MARKER.encode(), 'Child handshake missing')
            durable_check()
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
            base.check(child.returncode == -signal.SIGKILL, 'Child was not SIGKILLed')
            return subprocess.CompletedProcess(args, 0, json.dumps(dict(pid=child.pid, worked=True)), '')
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def exercise(m, client, headers):
    from sqlalchemy import func, select, text
    hooks = m.refund_execution
    Leg, Completion, Execution = (hooks[k] for k in ('PaymentLeg', 'Completion', 'Execution'))
    run = subprocess.run
    captured, children = {}, []
    with m.SessionLocal() as db:
        schema = db.scalar(text('SELECT current_schema()'))
        base.check(base.owned_schema(schema), 'Owned schema required')
        base.check(db.scalar(text('SHOW search_path')) == schema, 'Exclusive search path required')

    def snapshot(rid):
        with m.SessionLocal() as db:
            base.check(db.scalar(text('SELECT current_schema()')) == schema, 'Schema escaped')
            base.check(db.scalar(text('SHOW search_path')) == schema, 'Search path escaped')
            legs = list(db.scalars(select(Leg).where(Leg.refund_id == rid).order_by(Leg.id)))
            return ([(leg.id, leg.payment_id, leg.source_order_id, leg.amount, leg.currency,
                      leg.status, leg.receipt, leg.completed_at) for leg in legs],
                    db.scalar(select(func.count()).select_from(Completion).where(Completion.refund_id == rid)))

    def durable():
        rows, completions = snapshot(captured['rid'])
        done = [row for row in rows if row[5] == 'completed']
        base.check(len(done) == 1 and completions == 0, 'Expected exactly one durable leg before kill')
        base.check(done[0][6] == 'simulated:' + captured['rid'] + ':' + done[0][1]
                   and done[0][7] is not None, 'Simulated receipt missing')
        captured['before'] = rows

    def child_run(args, **kwargs):
        base.check(len(children) < 2 and args[:3] == [execution.sys.executable, '-B', '-c'],
                   'Unexpected child invocation')
        # Fail closed if the reused harness changes its child protocol.
        program = args[3]
        needle = '    worked=process_once(ns=vars(m))\n'
        base.check(program.count(needle) == 1 and repr(schema) in program,
                   'Reviewed child protocol changed')
        if not children:
            with m.SessionLocal() as db:
                pending = list(db.scalars(select(Execution).where(Execution.status == 'funds_pending')))
                base.check(len(pending) == 1, 'One isolated pending execution required')
                captured['rid'] = pending[0].refund_id
            rows, count = snapshot(captured['rid'])
            base.check(rows and all(row[5] == 'prepared' for row in rows) and count == 0,
                       'Kill case must start before any payment leg')
            pause = (needle + '    assert worked\n'
                     + '    print(' + repr(MARKER) + ', flush=True)\n'
                     + '    import signal\n    while True: signal.pause()\n')
            args = [*args[:3], program.replace(needle, pause)]
            options = {key: value for key, value in kwargs.items() if key not in ('timeout', 'capture_output')}
            result = kill_child(args, dict(options, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL), durable)
        else:
            resume = (needle + '    for _ in range(16):\n'
                      + '        if not process_once(ns=vars(m)): break\n')
            args = [*args[:3], program.replace(needle, resume)]
            result = run(args, **kwargs)
            base.check(result.returncode == 0, 'Fresh resume worker failed')
            rows, count = snapshot(captured['rid'])
            base.check(count == 1 and all(row[5] == 'completed' for row in rows),
                       'Fresh worker must finish recovery before parent drives')
        children.append(json.loads(result.stdout)['pid'])
        return result

    with patch.object(execution.subprocess, 'run', side_effect=child_run):
        results = execution.exercise(m, client, headers)
    try:
        base.check(results['worker_restart_resume']['status'] == 'passed', 'Restart case failed')
        base.check(len(set(children)) == 2, 'Fresh OS resume process required')
        rows, count = snapshot(captured['rid'])
        before = captured['before']
        base.check([row[:5] for row in rows] == [row[:5] for row in before], 'Payment legs duplicated or changed')
        base.check(all(row[5] == 'completed' for row in rows) and count == 1, 'Completion not unique')
        base.check(all(row in rows for row in before if row[5] == 'completed'), 'Durable receipt replayed')
        results[CASE] = dict(status='passed', killed_signal='SIGKILL',
            durable_simulated_legs_before_kill=1, completions_before_kill=0,
            independent_worker_processes=2, leg_identity_preserved=True,
            committed_receipt_unchanged=True, completions_after_resume=count,
            resources='Synthetic idempotent adapter; not native gateway',
            funds='Simulated durable payment legs; no bank movement')
        results['worker_restart_resume'].pop('limitation', None)
    except Exception as exc:
        results[CASE] = execution.cross.safe_error(exc)
    return results


def remote_run(encoded, manifest):
    """Review-only callable; inherits exact schema guard and count-only protection."""
    with patch.object(execution.cross, 'seed', execution.seed), \
            patch.object(execution.cross, 'exercise', exercise), \
            patch.object(execution.cross, 'CASE_NAMES', execution.CASES + (CASE,)), \
            patch.object(execution.cross, 'SOURCE_FILES', execution.SOURCES):
        result = execution.cross.remote_run(encoded, manifest)
    result.update(coverage=[], incremental_scope='QA005 SIGKILL; synthetic resources and simulated funds',
        external_adapters={'resources': 'Synthetic idempotent adapter; not native gateway/APISIX',
                           'funds': 'Simulated payment legs; no native bank execution'},
        gaps=['No native gateway, supplier, bank, browser or deployment evidence',
              'Kill is after durable leg commit, not within an in-flight commit'])
    return result


if __name__ == '__main__':
    raise SystemExit('Review required: no remote transport entry point; run offline unit tests.')
