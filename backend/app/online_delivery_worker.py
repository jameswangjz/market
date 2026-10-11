"""Standalone worker for new file deliveries using the API image/config."""

import importlib
import logging
import os
from pathlib import Path
import signal
import threading
import time


HEARTBEAT = Path('/tmp/online-delivery-heartbeat')
CYCLE_SECONDS = 15
logger = logging.getLogger(__name__)


def read_config(environ):
    try:
        interval = float(environ.get('ONLINE_DELIVERY_WORKER_INTERVAL', '2'))
    except (TypeError, ValueError):
        raise ValueError('ONLINE_DELIVERY_WORKER_INTERVAL must be in (0, 60]') from None
    if not 0 < interval <= 60:
        raise ValueError('ONLINE_DELIVERY_WORKER_INTERVAL must be in (0, 60]')
    try:
        batch = int(environ.get('ONLINE_DELIVERY_WORKER_BATCH', '10'))
    except (TypeError, ValueError):
        raise ValueError('ONLINE_DELIVERY_WORKER_BATCH must be in [1, 100]') from None
    if not 1 <= batch <= 100:
        raise ValueError('ONLINE_DELIVERY_WORKER_BATCH must be in [1, 100]')
    return interval, batch


def run_worker(process_once, stopped, *, interval=2, batch=10,
               heartbeat=HEARTBEAT, monotonic=time.monotonic):
    interval, batch = read_config({
        'ONLINE_DELIVERY_WORKER_INTERVAL': str(interval),
        'ONLINE_DELIVERY_WORKER_BATCH': str(batch),
    })
    while not stopped.is_set():
        started = monotonic()
        try:
            for _ in range(batch):
                if stopped.is_set() or monotonic() - started >= CYCLE_SECONDS:
                    break
                # process_once owns the task transaction, including its commit.
                result = process_once()
                heartbeat.touch()
                if not result:
                    break
        except Exception as exc:
            logger.error('Online delivery cycle failed: %s', type(exc).__name__)
        stopped.wait(max(0, interval - (monotonic() - started)))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    stopped = threading.Event()
    previous_handlers = {}
    try:
        interval, batch = read_config(os.environ)
        os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, lambda *_: stopped.set())
        delivery = importlib.import_module('app.main').online_delivery
        process_once = delivery['process_once']
        if not callable(process_once):
            raise TypeError('process_once must be callable')
        run_worker(process_once, stopped, interval=interval, batch=batch)
    except Exception as exc:
        logger.error('Online delivery worker failed: %s', type(exc).__name__)
        return 1
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
