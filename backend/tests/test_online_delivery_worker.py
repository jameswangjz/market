import importlib.util
import os
from pathlib import Path
import signal
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    'online_delivery_worker',
    Path(__file__).parents[1] / 'app' / 'online_delivery_worker.py',
)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class WorkerTests(unittest.TestCase):
    def one_cycle(self, process, **kwargs):
        stopped = Mock()
        stopped.is_set.return_value = False
        stopped.wait.side_effect = lambda _: setattr(stopped.is_set, 'return_value', True)
        heartbeat = kwargs.pop('heartbeat', Mock())
        worker.run_worker(process, stopped, heartbeat=heartbeat, **kwargs)
        return stopped, heartbeat

    def test_config_defaults_and_boundaries(self):
        self.assertEqual(worker.read_config({}), (2, 10))
        for interval in ('0.001', '60'):
            for batch in ('1', '100'):
                self.assertEqual(worker.read_config({
                    'ONLINE_DELIVERY_WORKER_INTERVAL': interval,
                    'ONLINE_DELIVERY_WORKER_BATCH': batch,
                }), (float(interval), int(batch)))

    def test_invalid_interval(self):
        for value in ('0', '-1', '60.001', 'nan', 'inf', '-inf', '', 'secret-value'):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                worker.read_config({'ONLINE_DELIVERY_WORKER_INTERVAL': value})
            self.assertNotIn('secret-value', str(caught.exception))

    def test_invalid_batch(self):
        for value in ('0', '-1', '101', '1.5', 'nan', '', 'secret-value'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                worker.read_config({'ONLINE_DELIVERY_WORKER_BATCH': value})

    def test_no_job_updates_heartbeat_and_waits(self):
        process = Mock(return_value=False)
        stopped, heartbeat = self.one_cycle(process, monotonic=lambda: 0)
        process.assert_called_once_with()
        heartbeat.touch.assert_called_once_with()
        stopped.wait.assert_called_once_with(2)

    def test_batch_bound(self):
        process = Mock(return_value=True)
        _, heartbeat = self.one_cycle(process, batch=3, monotonic=lambda: 0)
        self.assertEqual(process.call_count, 3)
        self.assertEqual(heartbeat.touch.call_count, 3)

    def test_truthy_result_and_empty_queue(self):
        process = Mock(side_effect=[{'processed': True}, None])
        _, heartbeat = self.one_cycle(process, monotonic=lambda: 0)
        self.assertEqual(process.call_count, 2)
        self.assertEqual(heartbeat.touch.call_count, 2)

    def test_time_budget_stops_new_tasks(self):
        process = Mock(return_value=True)
        stopped, heartbeat = self.one_cycle(
            process, monotonic=Mock(side_effect=[0, 0, 15, 15]),
        )
        process.assert_called_once_with()
        heartbeat.touch.assert_called_once_with()
        stopped.wait.assert_called_once_with(0)

    def test_shutdown_during_task_stops_batch(self):
        stopped = threading.Event()
        process = Mock(side_effect=lambda: stopped.set() or True)
        heartbeat = Mock()
        worker.run_worker(process, stopped, heartbeat=heartbeat)
        process.assert_called_once_with()
        heartbeat.touch.assert_called_once_with()

    def test_already_stopped_does_not_process_or_touch(self):
        stopped = threading.Event()
        stopped.set()
        process, heartbeat = Mock(), Mock()
        worker.run_worker(process, stopped, heartbeat=heartbeat)
        process.assert_not_called()
        heartbeat.touch.assert_not_called()

    def test_exception_preserves_old_heartbeat_and_redacts_log(self):
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / 'heartbeat'
            heartbeat.touch()
            os.utime(heartbeat, (100, 100))
            before = heartbeat.stat().st_mtime_ns
            process = Mock(side_effect=RuntimeError('secret SQL parameters'))
            with self.assertLogs(worker.logger, level='ERROR') as logs:
                stopped, _ = self.one_cycle(process, heartbeat=heartbeat)
            self.assertEqual(heartbeat.stat().st_mtime_ns, before)
            stopped.wait.assert_called_once()
            self.assertIn('RuntimeError', logs.output[0])
            self.assertNotIn('secret', ''.join(logs.output))
            self.assertIsNone(logs.records[0].exc_info)

    def test_failure_does_not_create_heartbeat_and_retries(self):
        stopped = Mock()
        stopped.is_set.return_value = False
        waits = []

        def wait(timeout):
            waits.append(timeout)
            if len(waits) == 2:
                stopped.is_set.return_value = True

        stopped.wait.side_effect = wait
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / 'heartbeat'

            attempts = []

            def process_once():
                attempts.append(True)
                self.assertFalse(heartbeat.exists())
                if len(attempts) == 1:
                    raise RuntimeError('private')
                return False

            process = Mock(side_effect=process_once)
            with self.assertLogs(worker.logger, level='ERROR'):
                worker.run_worker(process, stopped, heartbeat=heartbeat, monotonic=lambda: 0)
            self.assertTrue(heartbeat.exists())
            self.assertEqual(process.call_count, 2)
            self.assertEqual(waits, [2, 2])

    def test_main_import_order_and_signal_shutdown(self):
        for shutdown_signal in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=shutdown_signal), patch.dict(os.environ, {}, clear=True):
                handlers = {}
                process = Mock(return_value=False)

                def register(sig, handler):
                    handlers[sig] = handler
                    return signal.SIG_DFL

                def import_main(name):
                    self.assertEqual(name, 'app.main')
                    self.assertEqual(os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'], 'true')
                    handlers[shutdown_signal](shutdown_signal, None)
                    return SimpleNamespace(online_delivery={'process_once': process})

                with patch.object(worker.signal, 'signal', side_effect=register) as signals, \
                     patch.object(worker.importlib, 'import_module', side_effect=import_main):
                    self.assertEqual(worker.main(), 0)
                process.assert_not_called()
                self.assertEqual(signals.call_count, 4)

    def test_startup_failure_reports_only_type(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(worker.signal, 'signal'), \
             patch.object(worker.importlib, 'import_module', side_effect=RuntimeError('secret')), \
             self.assertLogs(worker.logger, level='ERROR') as logs:
            self.assertEqual(worker.main(), 1)
        self.assertIn('RuntimeError', logs.output[0])
        self.assertNotIn('secret', ''.join(logs.output))
        self.assertIsNone(logs.records[0].exc_info)


if __name__ == '__main__':
    unittest.main()
