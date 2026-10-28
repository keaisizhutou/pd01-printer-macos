import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageFont
import pd01_mac as app


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.font = patch.object(app.driver, '_load_font', side_effect=lambda size: ImageFont.load_default(size=size))
        self.font.start()
        self.addCleanup(self.font.stop)

    def prepared(self):
        return app.prepare(self.state, 'A little poem\nA quiet day', 36)

    async def test_preview_is_real_png_and_does_not_print(self):
        with patch.object(app.driver, 'print_png', new_callable=AsyncMock) as sender:
            job = self.prepared()
            with Image.open(job['preview']) as png:
                self.assertEqual(png.width, 384)
                self.assertGreater(png.height, 0)
            self.assertFalse(job['paper_used'])
            self.assertEqual(job['font_size'], 36)
            self.assertEqual(hashlib.sha256(Path(job['preview']).read_bytes()).hexdigest(), job['sha256'])
            sender.assert_not_called()

    async def test_confirmation_required_before_sender(self):
        job = self.prepared()
        sender = AsyncMock()
        with self.assertRaises(ValueError):
            await app.print_job(self.state, job['job_id'], 'yes', sender)
        sender.assert_not_called()

    async def test_print_uses_exact_preview_and_cannot_replay(self):
        job = self.prepared()
        sender = AsyncMock(return_value={'printed': True})
        result = await app.print_job(self.state, job['job_id'], 'CONFIRM_PRINT', sender)
        sender.assert_awaited_once_with(Path(job['preview']).read_bytes())
        self.assertTrue(result['data_sent'])
        self.assertFalse(result['physical_output_verified'])
        with self.assertRaises(RuntimeError):
            await app.print_job(self.state, job['job_id'], 'CONFIRM_PRINT', sender)
        self.assertEqual(sender.await_count, 1)

    async def test_uncertain_send_blocks_retry(self):
        job = self.prepared()
        sender = AsyncMock(side_effect=RuntimeError('connection lost after partial send'))
        with self.assertRaises(RuntimeError):
            await app.print_job(self.state, job['job_id'], 'CONFIRM_PRINT', sender)
        with self.assertRaises(RuntimeError):
            await app.print_job(self.state, job['job_id'], 'CONFIRM_PRINT', sender)
        self.assertEqual(sender.await_count, 1)

    async def test_concurrent_calls_only_send_once(self):
        job = self.prepared()
        async def send(_):
            await asyncio.sleep(0.01)
            return {'printed': True}
        sender = AsyncMock(side_effect=send)
        outcomes = await asyncio.gather(
            app.print_job(self.state, job['job_id'], 'CONFIRM_PRINT', sender),
            app.print_job(self.state, job['job_id'], 'CONFIRM_PRINT', sender),
            return_exceptions=True,
        )
        self.assertEqual(sender.await_count, 1)
        self.assertEqual(sum(isinstance(item, RuntimeError) for item in outcomes), 1)

    async def test_changed_preview_is_rejected(self):
        job = self.prepared()
        Path(job['preview']).write_bytes(b'changed')
        sender = AsyncMock()
        with self.assertRaises(ValueError):
            await app.print_job(self.state, job['job_id'], 'CONFIRM_PRINT', sender)
        sender.assert_not_called()

    async def test_reject_job_path_traversal(self):
        sender = AsyncMock()
        with self.assertRaises(ValueError):
            await app.print_job(self.state, '../../outside', 'CONFIRM_PRINT', sender)
        sender.assert_not_called()


if __name__ == '__main__':
    unittest.main()
