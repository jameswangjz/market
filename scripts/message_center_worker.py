"""Standalone notification dispatcher; use the same image/config as the API."""
import importlib
import logging
import os
import signal
import threading
import time
from pathlib import Path


def main():
    interval = float(os.environ.get('MESSAGE_CENTER_WORKER_INTERVAL', '2'))
    if not 0 < interval <= 60:
        raise ValueError('MESSAGE_CENTER_WORKER_INTERVAL must be in (0, 60]')
    batch_size = int(os.environ.get('MESSAGE_CENTER_WORKER_BATCH_SIZE', '50'))
    cycle_seconds = float(os.environ.get('MESSAGE_CENTER_WORKER_CYCLE_SECONDS', '15'))
    if not 1 <= batch_size <= 1000 or not 0 < cycle_seconds <= 60:
        raise ValueError('Worker batch size/time budget is out of range')
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    center = importlib.import_module('app.main').message_center
    process = center['process_once']
    scheduled_scan = center.get('scheduled_scan')
    next_scan = 0.0
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    heartbeat = Path(os.environ.get('MESSAGE_CENTER_WORKER_HEARTBEAT', '/tmp/message-center-worker-heartbeat'))
    while not stopped.is_set():
        started = time.monotonic()
        if callable(scheduled_scan) and started >= next_scan:
            next_scan = started + 60
            try:
                scheduled_scan()
            except Exception:
                logging.exception('Scheduled notification scan failed; dispatch continues')
        try:
            processed = 0
            for _ in range(batch_size):
                if stopped.is_set() or time.monotonic() - started >= cycle_seconds:
                    break
                result = process()
                heartbeat.touch()
                if not result:
                    break
                processed += 1
            if processed:
                logging.info('Notification dispatch: %d jobs', processed)
        except Exception:
            logging.exception('Notification dispatch failed; retrying next cycle')
        stopped.wait(max(0, interval - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
