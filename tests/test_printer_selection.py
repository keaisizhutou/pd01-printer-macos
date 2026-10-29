import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastmcp import Client
from PIL import Image
import job_store
import native_confirmation
import pd01_mac as cli
import printer_devices as devices
from printer_faults import PrinterFault
import printer_runtime as runtime
import printer_server as server


def advertisements(*ids):
    return {value: (SimpleNamespace(address=value, name='PD01'), SimpleNamespace(local_name='PD01')) for value in ids}


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        overrides = [patch.dict(os.environ, {'PD01_STATE_DIR': str(self.state), 'PD01_DEVICE_ADDRESS': ''}),
                     patch.object(runtime, 'queue_path', lambda: self.state / 'hardware.lock'),
                     patch.object(server, 'print_lock', asyncio.Lock()),
                     patch.object(devices.BleakScanner, 'discover', AsyncMock(return_value=advertisements('fake-A', 'fake-B')))]
        for mocked in overrides: mocked.start(); self.addCleanup(mocked.stop)

    async def test_cli_and_mcp_reject_ambiguous_unbound_targets_without_a_window_or_send(self):
        output = io.BytesIO(); Image.new('1', (384, 20), 0).save(output, format='PNG')
        jobs = [job_store.create(self.state, 'fixture', output.getvalue(), 384, 20, 1, 36, 'center') for _ in range(2)]
        with patch.object(job_store, 'request_confirmation', new_callable=AsyncMock) as window, \
             patch.object(server, 'send_stored_png', new_callable=AsyncMock) as sender:
            with self.assertRaises(PrinterFault) as caught:
                await cli.print_job(self.state, jobs[0]['job_id'], 'CONFIRM_PRINT')
            result = await server.print_prepared(jobs[1]['job_id'], 'CONFIRM_PRINT')
        self.assertEqual(caught.exception.code, 'multiple_printers_found')
        self.assertEqual(result['error_code'], 'multiple_printers_found')
        self.assertFalse((self.state / 'device.json').exists())
        window.assert_not_called(); sender.assert_not_called()

    async def test_single_discovered_target_is_saved_and_reused_without_switching(self):
        devices.BleakScanner.discover.return_value = advertisements('fake-A')
        target = await server.resolve_print_target(self.state)
        self.assertEqual(target['id'], 'fake-A')
        devices.BleakScanner.discover.return_value = advertisements('fake-B')
        self.assertEqual((await server.resolve_print_target(self.state))['id'], 'fake-A')
        self.assertEqual(devices.BleakScanner.discover.await_count, 1)

    async def test_mcp_list_and_selection_share_the_cli_configuration(self):
        async with Client(server.mcp) as client:
            listed = await client.call_tool('list_printers', {})
            self.assertEqual(len(listed.structured_content['printers']), 2)
            result = await client.call_tool('select_printer', {'device_id': 'fake-B'})
        self.assertEqual(result.structured_content['selected_printer']['id'], 'fake-B')
        self.assertEqual(server.configured_address(self.state), 'fake-B')
        self.assertFalse(result.structured_content['paper_used'])

    async def test_environment_override_cannot_be_silently_ignored(self):
        with patch.dict(os.environ, {'PD01_DEVICE_ADDRESS': 'fake-A'}):
            with self.assertRaises(PrinterFault) as caught:
                await server.select_target(self.state, 'fake-B')
        self.assertEqual(caught.exception.code, 'printer_selection_overridden')
        self.assertFalse((self.state / 'device.json').exists())

    async def test_offline_bound_target_does_not_fall_back_to_another_name_match(self):
        with patch.object(server, 'DEVICE_ADDRESS', 'fake-A'), \
             patch.object(server.BleakScanner, 'find_device_by_address', AsyncMock(return_value=None)) as search, \
             patch.object(server, 'BleakClient') as connection:
            with self.assertRaises(PrinterFault) as caught: await server.connect_device()
        self.assertEqual(caught.exception.code, 'printer_unavailable')
        self.assertEqual(search.call_args.args[0], 'fake-A')
        devices.BleakScanner.discover.assert_not_called(); connection.assert_not_called()

    async def test_window_target_is_frozen_even_if_another_process_changes_selection(self):
        job_store.save_json(self.state / 'device.json', {'id': 'fake-A', 'name': 'PD01'})
        output = io.BytesIO(); Image.new('1', (384, 20), 0).save(output, format='PNG')
        job = job_store.create(self.state, 'fixture', output.getvalue(), 384, 20, 1, 36, 'center')
        async def approve(metadata, _png):
            self.assertEqual(metadata['printer_id'], 'fake-A')
            self.assertIn('fake-A', metadata['printer_name'])
            job_store.save_json(self.state / 'device.json', {'id': 'fake-B', 'name': 'PD01'})
            return True
        with patch.object(job_store, 'request_confirmation', AsyncMock(side_effect=approve)), \
             patch.object(server, 'send_stored_png', AsyncMock(return_value={'data_sent': True})) as sender:
            await cli.print_job(self.state, job['job_id'], 'CONFIRM_PRINT')
        self.assertEqual(sender.call_args.kwargs['target']['id'], 'fake-A')
        self.assertEqual(job_store.read_status(self.state, job['job_id'])['printer_id'], 'fake-A')
        self.assertEqual(server.configured_address(self.state), 'fake-B')

    async def test_frozen_target_drives_transport_and_profile_binding(self):
        job_store.save_json(self.state / 'device.json', {'id': 'fake-B', 'name': 'PD01'})
        with patch.object(server, 'print_png', AsyncMock(return_value={})) as sender:
            await server.send_stored_png(self.state, b'fixture', target={'id': 'fake-A', 'name': 'PD01'})
        self.assertEqual(sender.call_args.kwargs['device_address'], 'fake-A')

    async def test_corrupt_configuration_can_be_repaired_by_explicit_selection(self):
        (self.state / 'device.json').write_text('broken')
        listed = await server.list_available_printers(self.state)
        self.assertEqual(len(listed['printers']), 2)
        await server.select_target(self.state, 'fake-A')
        self.assertEqual(server.configured_address(self.state), 'fake-A')

    def test_approval_for_another_printer_is_rejected(self):
        request = {'job_id': 'test', 'nonce': 'nonce', 'sha256': 'hash', 'purpose': 'print', 'printer_id': 'fake-A'}
        response = {**request, 'approved': True, 'reason': 'approved', 'printer_id': 'fake-B'}
        with self.assertRaises(PrinterFault) as caught:
            native_confirmation.parse_decision(json.dumps(response).encode(), 0, request)
        self.assertEqual(caught.exception.code, 'confirmation_response_invalid')
        response['printer_id'] = 'fake-A'
        self.assertTrue(native_confirmation.parse_decision(json.dumps(response).encode(), 0, request))
        del response['printer_id']
        with self.assertRaises(PrinterFault):
            native_confirmation.parse_decision(json.dumps(response).encode(), 0, request)

    async def test_first_status_sample_returns_and_saves_the_actual_target(self):
        devices.BleakScanner.discover.return_value = advertisements('fake-A')
        callback = None
        async def notify(_uuid, handler):
            nonlocal callback
            callback = handler
        async def write(*args, **kwargs):
            frame = bytearray(server.command(0xA3, b'\x00')); frame[3] = 1
            callback(None, frame)
        client = SimpleNamespace(start_notify=AsyncMock(side_effect=notify), stop_notify=AsyncMock(),
                                 write_gatt_char=AsyncMock(side_effect=write), disconnect=AsyncMock())
        with patch.object(server, 'DEVICE_ADDRESS', 'fake-A'), \
             patch.object(server, 'current_client', client), \
             patch.object(server, 'ensure_connected', AsyncMock(return_value=client)):
            result = await server.diagnose_printer(self.state)
        self.assertEqual(result['selected_printer']['id'], 'fake-A')
        self.assertEqual(server.configured_address(self.state), 'fake-A')
        client.disconnect.assert_awaited_once()


class SampleIdentityTests(unittest.TestCase):
    def test_cli_labels_samples_with_queried_target_rather_than_a_changed_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            job_store.save_json(state / 'device.json', {'id': 'fake-B', 'name': 'PD01'})
            result = {'status_samples': ['00'] * 3, 'selected_printer': {'id': 'fake-A'}, 'ok': True}
            with patch.object(sys, 'argv', ['pd01_mac.py', '--state-dir', directory, 'status', '--sample-label', 'normal']), \
                 patch.object(server, 'diagnose_printer', AsyncMock(return_value=result)), \
                 contextlib.redirect_stdout(io.StringIO()):
                cli.main()
            record = json.loads((state / 'diagnostics' / 'normal.json').read_text())
            import hashlib
            self.assertEqual(record['device_id_sha256'], hashlib.sha256(b'fake-A').hexdigest())


if __name__ == '__main__': unittest.main()
