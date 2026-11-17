"""Persist gateway financial charge events without trimming their source stream."""
import importlib
import logging
import json
import os
from pathlib import Path
import signal
import threading
import time
from datetime import datetime, timezone

from .online_delivery_worker import run_worker
from .api_charge_ledger import bounded_client


HEARTBEAT = Path('/tmp/api-charge-ledger-heartbeat')
LIVENESS_HEARTBEAT = Path('/tmp/api-charge-ledger-alive')
STATUS = Path('/tmp/api-charge-ledger-status.json')
logger = logging.getLogger(__name__)


def error_code(exc):
    message = str(exc)
    if isinstance(exc, ValueError) and message in {
        'Managed financial source unavailable; explicit continuity review required',
        'Redis charge source restarted; explicit continuity review required',
        'Existing Redis charge source is not continuous',
        'Redis source continuity unavailable',
    }:
        return 'source_continuity_review_required'
    if isinstance(exc, ValueError):
        return 'charge_evidence_invalid'
    return 'collection_dependency_error'


def supervised_cycle(ns, state, *, heartbeat=HEARTBEAT, status=STATUS, clock=time.monotonic):
    """Keep a blocked collector alive, but never report collection as ready."""
    try:
        worked = process_once(ns)
    except Exception as exc:
        heartbeat.unlink(missing_ok=True)
        code = error_code(exc)
        at = clock()
        if state.get('error') != code or at - state.get('logged_at', 0) >= 60:
            logger.error('API charge collection paused: %s (%s)', code, type(exc).__name__)
            state['logged_at'] = at
        state['error'] = code
        snapshot = {'status': 'blocked', 'error': code}
        worked = False
    else:
        heartbeat.touch()
        if state.get('error'):
            logger.info('API charge collection resumed')
        state['error'] = None
        snapshot = {'status': 'ready', 'error': None}
    snapshot['checked_at'] = datetime.now(timezone.utc).isoformat()
    temporary = status.with_suffix('.tmp')
    temporary.write_text(json.dumps(snapshot))
    temporary.replace(status)
    return worked


def process_once(ns):
    client = bounded_client(ns)
    try:
        with ns['SessionLocal']() as db:
            result = ns['api_charge_ledger']['ingest'](db, client, limit=500)
            db.commit()
    finally:
        client.close()
    return result['inserted'] > 0


def main():
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    logging.basicConfig(level=logging.INFO)
    ns = vars(importlib.import_module('app.main'))
    stopped = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stopped.set())
                for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        HEARTBEAT.unlink(missing_ok=True)
        LIVENESS_HEARTBEAT.touch()
        state = {}
        run_worker(lambda: supervised_cycle(ns, state), stopped, interval=2, batch=1,
                   heartbeat=LIVENESS_HEARTBEAT)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
