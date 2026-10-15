"""One-shot keyset-paged independent probe sweep; no migrations or auto-publish."""
import importlib
import logging
import math
import os
import time

from sqlalchemy import inspect

from .sla_probe_runtime import install


def run(ns, *, max_seconds=210, page_size=25, monotonic=time.monotonic):
    if (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
            or not math.isfinite(max_seconds) or max_seconds <= 0):
        raise ValueError('positive run budget required')
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ValueError('page_size must be in [1, 100]')
    runtime = ns['sla_probe_runtime']
    at, started, cursor = ns['now'](), monotonic(), ''
    counts = dict(processed=0, failures=0, created=0, duplicates=0, skipped=0,
                  healthy=0, failed=0, source_missing=0, unknown=0, complete=True)
    while True:
        if monotonic() - started >= max_seconds:
            counts['complete'] = False
            return counts
        with ns['SessionLocal']() as db:
            ids = runtime['page'](db, after_id=cursor, limit=page_size)
        if not ids:
            return counts
        for contract_id in ids:
            if monotonic() - started >= max_seconds:
                counts['complete'] = False
                return counts
            with ns['SessionLocal']() as db:
                try:
                    result = runtime['collect_one'](db, contract_id, instant=at)
                    db.commit()
                    counts['processed'] += 1
                    for key, value in result.items():
                        if key in counts and key not in {'processed', 'failures', 'complete'}:
                            counts[key] += value
                except Exception as exc:
                    db.rollback()
                    counts['failures'] += 1
                    logging.error('SLA probe transaction failed (%s)', type(exc).__name__)
            cursor = contract_id


def main():
    logging.basicConfig(level=logging.INFO)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    try:
        ns = vars(importlib.import_module('app.main'))
        runtime = install(ns)
        with ns['SessionLocal']() as db:
            if not inspect(db.get_bind()).has_table(runtime['Slot'].__tablename__):
                raise RuntimeError('Probe schema must be installed by the deployment owner')
        counts = run(ns)
        logging.info('SLA probe sweep: %s', counts)
        return 0 if counts['complete'] and counts['failures'] == 0 else 1
    except Exception as exc:
        logging.error('SLA probe worker failed (%s)', type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
