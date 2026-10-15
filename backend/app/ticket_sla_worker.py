"""One-shot, keyset-paged ticket SLA sweep; each ticket commits independently."""
import logging
import math
import time


def run(ns, *, max_seconds=210):
    if (not isinstance(max_seconds, (int, float)) or isinstance(max_seconds, bool)
            or not math.isfinite(max_seconds) or max_seconds <= 0):
        raise ValueError('A positive run budget is required')
    started = time.monotonic()
    at = ns['now']()
    cursor = ''
    counts = {'evaluated': 0, 'alerts': 0, 'failures': 0, 'complete': True}
    hook = ns['ticket_sla_monitor']
    while True:
        with ns['SessionLocal']() as db:
            ids = hook['page'](db, after_id=cursor)
        if not ids:
            return counts
        for ticket_id in ids:
            if time.monotonic() - started >= max_seconds:
                counts['complete'] = False
                return counts
            with ns['SessionLocal']() as db:
                try:
                    result = hook['evaluate'](db, ticket_id, at=at)
                    db.commit()
                    counts['evaluated'] += result['evaluated']
                    counts['alerts'] += result['alerts']
                except Exception:
                    db.rollback()
                    counts['failures'] += 1
                    logging.exception('Ticket SLA evaluation failed for ticket %s', ticket_id)
            cursor = ticket_id


def main():
    logging.basicConfig(level=logging.INFO)
    from . import main as app
    counts = run(vars(app))
    logging.info('Ticket SLA sweep: %s', counts)
    return 0 if counts['complete'] and counts['failures'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
