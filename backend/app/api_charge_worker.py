"""Persist gateway financial charge events without trimming their source stream."""
import importlib
import logging
import os
from pathlib import Path
import signal
import threading

from .online_delivery_worker import run_worker
from .api_charge_ledger import bounded_client


HEARTBEAT = Path('/tmp/api-charge-ledger-heartbeat')


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
        run_worker(lambda: process_once(ns), stopped, interval=2, batch=1, heartbeat=HEARTBEAT)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
