import asyncio
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastmcp import Client
from PIL import Image, ImageFont

import job_store
import pd01_mac as cli
import printer_server as server


class MCPWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.confirmation = patch.object(job_store, 'request_confirmation', new_callable=AsyncMock, return_value=True)
        self.confirmation.start()
        self.addCleanup(self.confirmation.stop)
        self.environment = patch.dict(os.environ, {'PD01_STATE_DIR': str(self.state)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.font = patch.object(server, '_load_font', side_effect=lambda size: ImageFont.load_default(size=size))
        self.font.start()
        self.addCleanup(self.font.stop)

    async def test_mcp_returns_exact_stored_png_and_default_layout(self):
        async with Client(server.mcp) as client:
            result = await client.call_tool('prepare_text', {'text': 'A poem\nA quiet day'})
        data = result.structured_content
        self.assertEqual(data['font_size'], 36)
        self.assertEqual(data['align'], 'center')
        images = [block for block in result.content if block.type == 'image']
        self.assertEqual(len(images), 1)
        png = base64.b64decode(images[0].data)
        self.assertEqual(png, Path(data['preview']).read_bytes())
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.width, 384)
        self.assertFalse(data['paper_used'])

    async def test_cli_and_mcp_cannot_send_same_job_twice(self):
        job = cli.prepare(self.state, 'A poem')
        async def send(*args, **kwargs):
            await asyncio.sleep(0.02)
            return {'data_sent': True}
        with patch.object(server, 'send_stored_png', new_callable=AsyncMock, side_effect=send) as sender:
            outcomes = await asyncio.gather(
                cli.print_job(self.state, job['job_id'], 'CONFIRM_PRINT'),
                server.print_prepared(job['job_id'], 'CONFIRM_PRINT'),
                return_exceptions=True,
            )
        self.assertEqual(sender.await_count, 1)
        self.assertEqual(sum(isinstance(item, RuntimeError) for item in outcomes), 1)

    async def test_mcp_failure_remains_blocked_after_new_client(self):
        job = cli.prepare(self.state, 'A poem')
        with patch.object(server, 'send_stored_png', new_callable=AsyncMock,
                          side_effect=RuntimeError('simulated partial send')) as sender:
            async with Client(server.mcp) as client:
                result = await client.call_tool('print_prepared',
                    {'job_id': job['job_id'], 'confirmation': 'CONFIRM_PRINT'}, raise_on_error=False)
                self.assertTrue(result.is_error)
            async with Client(server.mcp) as client:
                result = await client.call_tool('print_prepared',
                    {'job_id': job['job_id'], 'confirmation': 'CONFIRM_PRINT'}, raise_on_error=False)
                self.assertTrue(result.is_error)
            self.assertEqual(sender.await_count, 1)

    async def test_cancellation_blocks_retry(self):
        job = cli.prepare(self.state, 'A poem')
        started = asyncio.Event()
        async def send(_):
            started.set()
            await asyncio.Event().wait()
        sender = AsyncMock(side_effect=send)
        task = asyncio.create_task(job_store.dispatch(self.state, job['job_id'], 'CONFIRM_PRINT', sender))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(RuntimeError):
            await job_store.dispatch(self.state, job['job_id'], 'CONFIRM_PRINT', sender)
        self.assertEqual(sender.await_count, 1)

    async def test_separate_processes_share_durable_attempt_guard(self):
        job = cli.prepare(self.state, 'A poem')
        script = '''
import asyncio, sys
from pathlib import Path
import job_store
async def approve(_metadata, _png):
    return True
job_store.request_confirmation = approve
async def send(_):
    with (Path(sys.argv[1]) / 'sent.txt').open('a') as out:
        out.write('sent\\n')
    await asyncio.sleep(0.05)
    return {'data_sent': True}
try:
    asyncio.run(job_store.dispatch(Path(sys.argv[1]), sys.argv[2], 'CONFIRM_PRINT', send))
except RuntimeError:
    sys.exit(2)
'''
        command = [sys.executable, '-c', script, str(self.state), job['job_id']]
        processes = [subprocess.Popen(command, cwd=Path(server.__file__).parent) for _ in range(2)]
        codes = await asyncio.gather(*(asyncio.to_thread(process.wait, timeout=10) for process in processes))
        self.assertEqual(sorted(codes), [0, 2])
        self.assertEqual((self.state / 'sent.txt').read_text().splitlines(), ['sent'])
        # A subsequent process still cannot replay the completed job.
        replay = await asyncio.to_thread(subprocess.run, command, cwd=Path(server.__file__).parent)
        self.assertEqual(replay.returncode, 2)

    async def test_mcp_wrong_confirmation_never_sends(self):
        job = cli.prepare(self.state, 'A poem')
        with patch.object(server, 'send_stored_png', new_callable=AsyncMock) as sender:
            with self.assertRaises(ValueError):
                await server.print_prepared(job['job_id'], 'yes')
            sender.assert_not_called()

    async def test_transport_reports_send_and_cleans_up_under_lock(self):
        async def disconnected():
            self.assertTrue(server.print_lock.locked())
        client = SimpleNamespace(write_gatt_char=AsyncMock(),
                                 disconnect=AsyncMock(side_effect=disconnected))
        with patch.object(server, 'print_lock', asyncio.Lock()), \
             patch.object(server, 'current_client', client), \
             patch.object(server, 'ensure_connected', new_callable=AsyncMock, return_value=client), \
             patch.object(server, 'png_to_rows', return_value=[bytes(48)]):
            result = await server.print_png(b'not sent to hardware')
            self.assertTrue(result['data_sent'])
            self.assertFalse(result['physical_output_verified'])
            self.assertNotIn('printed', result)
            client.disconnect.assert_awaited_once()
            self.assertIsNone(server.current_client)

    async def test_disconnect_cannot_interrupt_in_process_send(self):
        started = asyncio.Event()
        release = asyncio.Event()
        async def write(*args, **kwargs):
            started.set()
            await release.wait()
        client = SimpleNamespace(write_gatt_char=AsyncMock(side_effect=write), disconnect=AsyncMock())
        with patch.object(server, 'print_lock', asyncio.Lock()), \
             patch.object(server, 'current_client', client), \
             patch.object(server, 'ensure_connected', new_callable=AsyncMock, return_value=client), \
             patch.object(server, 'png_to_rows', return_value=[bytes(48)]):
            sending = asyncio.create_task(server.print_png(b'not sent to hardware'))
            await started.wait()
            closing = asyncio.create_task(server.disconnect_printer())
            await asyncio.sleep(0)
            client.disconnect.assert_not_called()
            release.set()
            await asyncio.gather(sending, closing)
            client.disconnect.assert_awaited_once()


class LayoutTests(unittest.TestCase):
    def wrap(self, text, width):
        class Monospace:
            def textlength(self, text, font):
                return len(text)
        return server._wrap_text(Monospace(), text, None, width)

    def test_chinese_closing_punctuation_not_at_start(self):
        text = '春风吹过，花开了。'
        lines = self.wrap(text, 4)
        self.assertEqual(''.join(lines), text)
        self.assertTrue(all(not line.startswith(('，', '。')) for line in lines))
        self.assertTrue(all(len(line) <= 4 for line in lines))

    def test_opening_punctuation_not_at_end(self):
        lines = self.wrap('春风（花开）', 3)
        self.assertTrue(all(not line.endswith('（') for line in lines))
        self.assertTrue(all(not line.startswith('）') for line in lines))
        self.assertEqual(''.join(lines), '春风（花开）')

    def test_english_word_stays_together_when_it_fits(self):
        self.assertEqual(self.wrap('hello world', 8), ['hello', 'world'])

    def test_chinese_clause_break_preferred_to_short_tail(self):
        self.assertEqual(self.wrap('把风和日子，慢慢收藏。', 9), ['把风和日子，', '慢慢收藏。'])

    def test_overlong_word_can_still_wrap(self):
        self.assertEqual(self.wrap('abcdefghij', 4), ['abcd', 'efgh', 'ij'])

    def test_manual_lines_and_blank_lines_preserved(self):
        self.assertEqual(self.wrap('one\r\n\r\ntwo', 8), ['one', '', 'two'])


if __name__ == '__main__':
    unittest.main()
