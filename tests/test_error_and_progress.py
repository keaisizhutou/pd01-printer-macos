import asyncio
import errno
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import ImageFont
from fastmcp import Client
import job_store
import pd01_mac as app
import printer_server as server
from printer_faults import PrinterFault, normalize_error


class ErrorProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        for target, name, replacement in [(job_store, 'request_confirmation', AsyncMock(return_value=True)),
                                          (app.driver, '_load_font', lambda size: ImageFont.load_default(size=size))]:
            mocked = patch.object(target, name, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.job = app.prepare(self.state, 'A test')

    async def test_job_status_exposes_waiting_and_send_phases_without_claiming_output(self):
        started = asyncio.Event()
        release = asyncio.Event()
        async def send(_):
            job_store.report_progress('sending')
            started.set()
            await release.wait()
            return {'data_sent': True}
        task = asyncio.create_task(app.print_job(self.state, self.job['job_id'], 'CONFIRM_PRINT', send))
        await started.wait()
        status = job_store.read_status(self.state, self.job['job_id'])
        self.assertEqual(status['status'], 'sending')
        self.assertIsNone(status['data_sent'])
        self.assertFalse(status['physical_output_verified'])
        release.set()
        await task
        self.assertEqual(job_store.read_status(self.state, self.job['job_id'])['status'], 'sent')

    async def test_disk_full_before_claim_never_reaches_window_or_sender(self):
        sender = AsyncMock()
        with patch.object(job_store.os, 'open', side_effect=OSError(errno.ENOSPC, 'full')):
            with self.assertRaises(PrinterFault) as caught:
                await app.print_job(self.state, self.job['job_id'], 'CONFIRM_PRINT', sender)
        self.assertEqual(caught.exception.code, 'storage_full')
        self.assertFalse(caught.exception.output_uncertain)
        job_store.request_confirmation.assert_not_called()
        sender.assert_not_called()

    async def test_disk_full_after_send_remains_uncertain_and_cannot_replay(self):
        sender = AsyncMock(return_value={'data_sent': True})
        original_save = job_store.save_json
        def save(path, value):
            if value.get('status') == 'sent':
                raise OSError(errno.ENOSPC, 'full')
            return original_save(path, value)
        with patch.object(job_store, 'save_json', side_effect=save):
            with self.assertRaises(PrinterFault) as caught:
                await app.print_job(self.state, self.job['job_id'], 'CONFIRM_PRINT', sender)
        self.assertEqual(caught.exception.code, 'storage_full')
        self.assertTrue(caught.exception.output_uncertain)
        with self.assertRaises(PrinterFault) as rejected:
            await app.print_job(self.state, self.job['job_id'], 'CONFIRM_PRINT', sender)
        self.assertEqual(rejected.exception.code, 'job_already_attempted')
        self.assertEqual(sender.await_count, 1)

    async def test_invalid_configured_fault_profile_cannot_disable_print_checks(self):
        (self.state / 'device.json').write_text(json.dumps({'id': 'test-device'}))
        (self.state / 'status-profile.json').write_text('broken')
        with patch.object(server, 'print_png', new_callable=AsyncMock) as sender:
            with self.assertRaises(PrinterFault) as caught:
                await server.send_stored_png(self.state, b'not used')
        self.assertEqual(caught.exception.code, 'calibration_unavailable')
        sender.assert_not_called()

    async def test_mcp_preparation_errors_are_actionable_and_do_not_print(self):
        with patch.object(server, 'send_stored_png', new_callable=AsyncMock) as sender:
            async with Client(server.mcp) as client:
                result = await client.call_tool('prepare_text', {'text': ''})
        self.assertEqual(result.structured_content['error_code'], 'text_empty')
        self.assertFalse(result.structured_content['data_sent'])
        self.assertTrue(result.structured_content['action'])
        sender.assert_not_called()

    async def test_mcp_job_status_query_never_connects(self):
        with patch.dict('os.environ', {'PD01_STATE_DIR': str(self.state)}), \
             patch.object(server, 'ensure_connected', new_callable=AsyncMock) as connection:
            async with Client(server.mcp) as client:
                result = await client.call_tool('get_job_status', {'job_id': self.job['job_id']})
        self.assertEqual(result.structured_content['status'], 'prepared')
        connection.assert_not_called()

    def test_permission_and_disk_errors_are_not_exposed_as_raw_exceptions(self):
        for number, code in [(errno.ENOSPC, 'storage_full'), (errno.EACCES, 'storage_permission_denied'),
                              (errno.EROFS, 'storage_read_only')]:
            error = normalize_error(OSError(number, 'private-path-information'))
            self.assertEqual(error.code, code)
            self.assertNotIn('private-path', str(error.as_result()))


if __name__ == '__main__':
    unittest.main()
