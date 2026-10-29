import asyncio
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image
import job_store
from printer_faults import PrinterFault
import printer_runtime as runtime
import printer_server as server


def raster():
    data = io.BytesIO()
    Image.new('1', (384, 100), 0).save(data, format='PNG')
    return data.getvalue()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        overrides = [(runtime, 'queue_path', lambda: self.root / 'hardware.lock'),
                     (runtime, 'QUEUE_TIMEOUT_SECONDS', .1), (runtime, 'NOTIFY_TIMEOUT_SECONDS', .02),
                     (runtime, 'WRITE_TIMEOUT_SECONDS', .02), (runtime, 'CLEANUP_TIMEOUT_SECONDS', .02),
                     (runtime, 'CONNECT_TIMEOUT_SECONDS', .02), (runtime, 'SCAN_TIMEOUT_SECONDS', .02),
                     (server, 'print_lock', asyncio.Lock()), (server, 'TAIL_DRAIN_SECONDS', 0)]
        for target, name, value in overrides:
            mocked = patch.object(target, name, value)
            mocked.start(); self.addCleanup(mocked.stop)

    def client(self, *, hang_at=None):
        callback = None
        async def hang(*args, **kwargs):
            await asyncio.Event().wait()
        async def notify(_uuid, handler):
            nonlocal callback
            callback = handler
            if hang_at == 'notify': await hang()
        async def write(_uuid, data, response=False):
            if hang_at == 'query' or hang_at == 'send' and data != server.STATE_QUERY:
                await hang()
            if data == server.STATE_QUERY:
                frame = bytearray(server.command(0xA3, b'\x00')); frame[3] = 1
                callback(None, frame)
        client = SimpleNamespace(start_notify=AsyncMock(side_effect=notify),
            write_gatt_char=AsyncMock(side_effect=write),
            stop_notify=AsyncMock(side_effect=hang if hang_at == 'cleanup' else None),
            disconnect=AsyncMock(side_effect=hang if hang_at == 'cleanup' else None))
        for name, value in [('current_client', client), ('ensure_connected', AsyncMock(return_value=client))]:
            mocked = patch.object(server, name, value)
            mocked.start(); self.addCleanup(mocked.stop)
        return client

    async def test_notification_timeout_exits_before_any_write(self):
        client = self.client(hang_at='notify')
        with self.assertRaises(PrinterFault) as caught:
            await asyncio.wait_for(server.print_png(raster()), .5)
        self.assertEqual(caught.exception.code, 'printer_notify_timeout')
        self.assertFalse(caught.exception.output_uncertain)
        client.write_gatt_char.assert_not_called()
        client.disconnect.assert_awaited_once()

    async def test_query_and_image_write_timeouts_have_different_output_uncertainty(self):
        for stage, uncertain in [('query', False), ('send', True)]:
            client = self.client(hang_at=stage)
            with self.subTest(stage=stage), self.assertRaises(PrinterFault) as caught:
                await asyncio.wait_for(server.print_png(raster()), .5)
            self.assertEqual(caught.exception.code, 'printer_write_timeout')
            self.assertEqual(caught.exception.output_uncertain, uncertain)
            client.disconnect.assert_awaited_once()

    async def test_cleanup_attempts_disconnect_even_if_stopping_notifications_hangs(self):
        client = self.client(hang_at='cleanup')
        with self.assertRaises(PrinterFault) as caught:
            await asyncio.wait_for(server.print_png(raster()), 2)
        self.assertEqual(caught.exception.code, 'printer_cleanup_failed')
        self.assertTrue(caught.exception.output_uncertain)
        client.stop_notify.assert_awaited_once()
        client.disconnect.assert_awaited_once()
        self.assertIsNone(server.current_client)
        self.assertFalse(server.print_lock.locked())

    async def test_overall_deadline_stops_slow_chunk_stream(self):
        self.client()
        with patch.object(runtime, 'PRINT_TIMEOUT_SECONDS', .04):
            with self.assertRaises(PrinterFault) as caught:
                await asyncio.wait_for(server.print_png(raster()), .5)
        self.assertEqual(caught.exception.code, 'printer_operation_timeout')
        self.assertTrue(caught.exception.output_uncertain)

    async def test_diagnostic_total_deadline_includes_spacing_between_samples(self):
        client = self.client()
        with patch.object(server, 'configured_address', return_value='test-device'), \
             patch.object(server, 'DEVICE_ADDRESS', 'test-device'), \
             patch.object(runtime, 'DIAGNOSTIC_TIMEOUT_SECONDS', .04):
            with self.assertRaises(PrinterFault) as caught:
                await asyncio.wait_for(server.diagnose_printer(self.root, samples=2), .5)
        self.assertEqual(caught.exception.code, 'printer_operation_timeout')
        self.assertFalse(caught.exception.output_uncertain)
        client.disconnect.assert_awaited_once()

    async def test_waiting_job_phase_is_visible_and_timeout_preserves_single_attempt(self):
        job = job_store.create(self.root, 'fixture', raster(), 384, 100, 1, 36, 'center')
        client = self.client()
        async def send(data): return await server.print_png(data)
        with patch.object(job_store, 'request_confirmation', AsyncMock(return_value=True)):
            async with runtime.hardware_queue(asyncio.Lock()):
                task = asyncio.create_task(job_store.dispatch(self.root, job['job_id'], 'CONFIRM_PRINT', send))
                await asyncio.sleep(.02)
                status = job_store.read_status(self.root, job['job_id'])
                self.assertEqual(status['status'], 'waiting_for_printer')
                self.assertFalse(status['data_sent'])
                with self.assertRaises(PrinterFault) as caught: await task
        self.assertEqual(caught.exception.code, 'printer_queue_timeout')
        self.assertEqual(job_store.read_status(self.root, job['job_id'])['status'], 'blocked_before_send')
        with self.assertRaises(PrinterFault) as duplicate:
            await job_store.dispatch(self.root, job['job_id'], 'CONFIRM_PRINT', send)
        self.assertEqual(duplicate.exception.code, 'job_already_attempted')
        client.write_gatt_char.assert_not_called()

    async def test_primary_fault_is_retained_when_cleanup_also_times_out(self):
        client = self.client(hang_at='cleanup')
        client.start_notify.side_effect = RuntimeError('fake unavailable channel')
        with self.assertRaises(PrinterFault) as caught:
            await asyncio.wait_for(server.print_png(raster()), .5)
        self.assertEqual(caught.exception.code, 'printer_check_failed')
        self.assertFalse(caught.exception.output_uncertain)
        self.assertEqual(caught.exception.as_result()['cleanup_errors'], ['disconnect'])

    async def test_queue_timeout_never_enters_transport_and_marks_unsent(self):
        client = self.client()
        async with runtime.hardware_queue(asyncio.Lock()):
            with self.assertRaises(PrinterFault) as caught:
                await asyncio.wait_for(server.print_png(raster()), .5)
        self.assertEqual(caught.exception.code, 'printer_queue_timeout')
        self.assertFalse(caught.exception.output_uncertain)
        server.ensure_connected.assert_not_called()
        client.write_gatt_char.assert_not_called()

    async def test_cancelling_queue_wait_does_not_leave_a_local_lock_held(self):
        lock = asyncio.Lock()
        async def waiting():
            async with runtime.hardware_queue(lock):
                self.fail('Cancelled waiter acquired the hardware')
        async with runtime.hardware_queue(asyncio.Lock()):
            task = asyncio.create_task(waiting())
            await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(lock.locked())
        async with runtime.hardware_queue(lock): pass

    async def test_cancelling_a_queued_job_records_unsent_and_never_replays(self):
        job = job_store.create(self.root, 'fixture', raster(), 384, 100, 1, 36, 'center')
        client = self.client()
        async def send(data): return await server.print_png(data)
        with patch.object(job_store, 'request_confirmation', AsyncMock(return_value=True)):
            async with runtime.hardware_queue(asyncio.Lock()):
                task = asyncio.create_task(job_store.dispatch(self.root, job['job_id'], 'CONFIRM_PRINT', send))
                await asyncio.sleep(.02)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
        status = job_store.read_status(self.root, job['job_id'])
        self.assertEqual(status['status'], 'cancelled_before_send')
        self.assertFalse(status['data_sent'])
        with self.assertRaises(PrinterFault) as caught:
            await job_store.dispatch(self.root, job['job_id'], 'CONFIRM_PRINT', send)
        self.assertEqual(caught.exception.code, 'job_already_attempted')
        client.write_gatt_char.assert_not_called()

    async def test_scan_and_connect_deadlines_are_bounded_and_cleanup_is_attempted(self):
        async def hang(*args, **kwargs): await asyncio.Event().wait()
        with patch.object(server, 'DEVICE_ADDRESS', 'test-device'), \
             patch.object(server.BleakScanner, 'find_device_by_address', AsyncMock(side_effect=hang)):
            with self.assertRaises(PrinterFault) as caught:
                await asyncio.wait_for(server.connect_device(), .5)
        self.assertEqual(caught.exception.code, 'printer_scan_timeout')
        client = SimpleNamespace(connect=AsyncMock(side_effect=hang), disconnect=AsyncMock())
        with patch.object(server, 'DEVICE_ADDRESS', 'test-device'), \
             patch.object(server.BleakScanner, 'find_device_by_address', AsyncMock(return_value=object())), \
             patch.object(server, 'BleakClient', return_value=client):
            with self.assertRaises(PrinterFault) as caught:
                await asyncio.wait_for(server.connect_device(), .5)
        self.assertEqual(caught.exception.code, 'printer_connect_timeout')
        client.disconnect.assert_awaited_once()

    async def test_two_processes_and_state_directories_share_one_transport_queue(self):
        script = '''
import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import printer_runtime as runtime
import printer_server as server
runtime.queue_path = lambda: Path(sys.argv[1])
server.CHUNK_DELAY_SECONDS = 0
server.TAIL_DRAIN_SECONDS = 0
callback = None
started = False
def record(event):
    with Path(sys.argv[2]).open('a') as stream:
        stream.write(json.dumps([sys.argv[3], event]) + '\\n')
async def notify(_uuid, handler):
    global callback
    callback = handler
async def write(_uuid, data, response=False):
    global started
    if data == server.STATE_QUERY:
        frame = bytearray(server.command(0xA3, b'\\x00')); frame[3] = 1
        callback(None, frame)
    elif not started:
        started = True
        record('start')
        await asyncio.sleep(.15)
async def disconnect(): record('end')
client = SimpleNamespace(start_notify=AsyncMock(side_effect=notify), stop_notify=AsyncMock(),
    write_gatt_char=AsyncMock(side_effect=write), disconnect=AsyncMock(side_effect=disconnect))
server.current_client = client
server.ensure_connected = AsyncMock(return_value=client)
server.png_to_rows = lambda _: [bytes(48)]
asyncio.run(server.print_png(b'fake'))
'''
        log = self.root / 'events.jsonl'
        processes = [subprocess.Popen([sys.executable, '-c', script, str(self.root / 'hardware.lock'), str(log), str(i)],
            env={**__import__('os').environ, 'PD01_STATE_DIR': str(self.root / str(i))},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
        try:
            outputs = await asyncio.gather(*(asyncio.to_thread(p.communicate, timeout=15) for p in processes))
            self.assertEqual([p.returncode for p in processes], [0, 0], outputs)
        finally:
            for process in processes:
                if process.poll() is None: process.kill(); process.wait()
        events = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([event for _, event in events], ['start', 'end', 'start', 'end'])

    async def test_process_death_releases_file_lock_without_deleting_it(self):
        script = '''
import asyncio, sys
from pathlib import Path
import printer_runtime as runtime
runtime.queue_path = lambda: Path(sys.argv[1])
async def main():
    async with runtime.hardware_queue(asyncio.Lock()):
        Path(sys.argv[2]).touch()
        await asyncio.Event().wait()
asyncio.run(main())
'''
        marker = self.root / 'ready'
        process = subprocess.Popen([sys.executable, '-c', script, str(self.root / 'hardware.lock'), str(marker)])
        try:
            for _ in range(100):
                if marker.exists(): break
                await asyncio.sleep(.02)
            self.assertTrue(marker.exists())
        finally:
            process.kill(); await asyncio.to_thread(process.wait, timeout=3)
        self.assertTrue((self.root / 'hardware.lock').exists())
        async with runtime.hardware_queue(asyncio.Lock()): pass


if __name__ == '__main__': unittest.main()
