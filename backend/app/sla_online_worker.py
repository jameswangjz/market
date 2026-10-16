"""Five-minute probe collection followed by persisted current-month assessment."""
import importlib
import logging
import os
import time

from .sla_probe_worker import run as collect


def run(ns, *, max_seconds=210):
    started = time.monotonic()
    expire = ns.get('enterprise_sla', {}).get('expire_due')
    if expire is not None:
        # Expiry audit must also run when collection is unavailable or incomplete.
        with ns['SessionLocal']() as db:
            try:
                expire(db)
                db.commit()
            except Exception:
                db.rollback()
                raise
    probes = collect(ns, max_seconds=max_seconds)
    if not probes['complete'] or probes['failures']:
        return {'status': 'incomplete', 'probes': probes, 'assessments': 0}
    if time.monotonic() - started >= max_seconds:
        return {'status': 'incomplete', 'probes': probes, 'assessments': 0}
    with ns['SessionLocal']() as db:
        try:
            rows = ns['sla_online_assessments']['evaluate'](db, 'system:sla-online-monitor')
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'status': 'completed', 'probes': probes, 'assessments': len(rows)}


def main():
    logging.basicConfig(level=logging.INFO)
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    try:
        result = run(vars(importlib.import_module('app.main')))
        logging.info('Online SLA sweep: %s', result)
        return 0 if result['status'] == 'completed' else 1
    except Exception as exc:
        logging.error('Online SLA sweep failed (%s)', type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
