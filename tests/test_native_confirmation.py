import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import ImageFont
import job_store
import native_confirmation as native
import pd01_mac as cli


class NativeGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.font = patch.object(cli.driver, '_load_font', side_effect=lambda size: ImageFont.load_default(size=size))
        self.font.start()
        self.addCleanup(self.font.stop)
        self.job = cli.prepare(self.state, 'Please review this exact image')

    async def test_confirmation_token_alone_never_sends_while_waiting(self):
        shown = asyncio.Event()
        approved = asyncio.Event()
        async def gate(metadata, png):
            self.assertEqual(metadata['job_id'], self.job['job_id'])
            self.assertEqual(hashlib.sha256(png).hexdigest(), self.job['sha256'])
            shown.set()
            await approved.wait()
            return True
        sender = AsyncMock(return_value={'data_sent': True})
        with patch.object(job_store, 'request_confirmation', side_effect=gate):
            task = asyncio.create_task(cli.print_job(self.state, self.job['job_id'], 'CONFIRM_PRINT', sender))
            await shown.wait()
            sender.assert_not_called()
            approved.set()
            result = await task
        sender.assert_awaited_once_with(Path(self.job['preview']).read_bytes())
        self.assertTrue(result['data_sent'])

    async def test_cancel_window_blocks_hardware_and_replay(self):
        sender = AsyncMock()
        with patch.object(job_store, 'request_confirmation', new_callable=AsyncMock, return_value=False) as gate:
            result = await cli.print_job(self.state, self.job['job_id'], 'CONFIRM_PRINT', sender)
            self.assertTrue(result['cancelled'])
            self.assertFalse(result['data_sent'])
            self.assertFalse(result['paper_used'])
            sender.assert_not_called()
            with self.assertRaises(RuntimeError):
                await cli.print_job(self.state, self.job['job_id'], 'CONFIRM_PRINT', sender)
            self.assertEqual(gate.await_count, 1)

    async def test_window_build_failure_never_falls_back_to_sending(self):
        sender = AsyncMock()
        with patch.object(job_store, 'request_confirmation', new_callable=AsyncMock,
                          side_effect=RuntimeError('No local desktop')):
            with self.assertRaises(RuntimeError):
                await cli.print_job(self.state, self.job['job_id'], 'CONFIRM_PRINT', sender)
        sender.assert_not_called()
        state = json.loads((Path(self.job['preview']).parent / 'attempt.json').read_text())
        self.assertEqual(state['status'], 'confirmation_failed_before_send')

    async def test_cancelling_pending_window_does_not_send(self):
        shown = asyncio.Event()
        async def gate(*_):
            shown.set()
            await asyncio.Event().wait()
        sender = AsyncMock()
        with patch.object(job_store, 'request_confirmation', side_effect=gate):
            task = asyncio.create_task(cli.print_job(self.state, self.job['job_id'], 'CONFIRM_PRINT', sender))
            await shown.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        sender.assert_not_called()

    async def test_helper_timeout_kills_window_and_denies(self):
        class Process:
            returncode = None
            killed = False
            async def communicate(self, data):
                await asyncio.Event().wait()
            def kill(self):
                self.killed = True
                self.returncode = -9
            async def wait(self):
                return self.returncode
        process = Process()
        with patch.object(native, 'build_helper', return_value=Path('/unused/helper')), \
             patch.object(native.asyncio, 'create_subprocess_exec', new_callable=AsyncMock, return_value=process), \
             patch.object(native, 'TIMEOUT_SECONDS', 0.01):
            approved = await native.request_confirmation(self.job, Path(self.job['preview']).read_bytes())
        self.assertFalse(approved)
        self.assertTrue(process.killed)


class ApprovalResponseTests(unittest.TestCase):
    def setUp(self):
        self.request = {'job_id': 'job', 'nonce': 'unique', 'sha256': 'digest', 'purpose': 'print'}

    def response(self, changes=None):
        return json.dumps({**self.request, 'approved': True, **(changes or {})}).encode()

    def test_accepts_matching_button_result(self):
        self.assertTrue(native.response_approved(self.response(), 0, self.request))

    def test_old_or_other_job_approval_is_rejected(self):
        for key in self.request:
            with self.subTest(key=key):
                self.assertFalse(native.response_approved(self.response({key: 'other'}), 0, self.request))

    def test_cancel_crash_and_malformed_result_are_rejected(self):
        for output, code in [(self.response({'approved': False}), 0),
                             (self.response({'approved': 'true'}), 0),
                             (self.response(), 1), (b'', 0), (b'[]', 0), (b'garbage', 0)]:
            self.assertFalse(native.response_approved(output, code, self.request))


if __name__ == '__main__':
    unittest.main()
