import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import printer_server as driver
from printer_faults import PrinterFault


def reply_frame(code, payload=b'\x00'):
    frame = bytearray(driver.command(code, payload))
    frame[3] = 1
    return bytes(frame)


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_and_combined_frames(self):
        feedback = driver.PrinterFeedback()
        state = reply_frame(0xA3, b'\x00\x01\xcb')
        feedback.receive(None, state[:5])
        self.assertFalse(feedback.state_received.is_set())
        feedback.receive(None, state[5:] + reply_frame(0xAE))
        self.assertEqual(feedback.state_replies, 1)
        self.assertTrue(feedback.ready_reported)

    async def test_invalid_crc_does_not_count_as_device_reply(self):
        feedback = driver.PrinterFeedback()
        corrupt = bytearray(reply_frame(0xA3))
        corrupt[-2] ^= 1
        feedback.receive(None, corrupt)
        self.assertFalse(feedback.state_received.is_set())
        feedback.receive(None, reply_frame(0xA3))
        self.assertTrue(feedback.state_received.is_set())

    async def run_transport(self, answer, answer_after_send=True):
        callback = None
        writes = []
        async def notify(uuid, handler):
            nonlocal callback
            callback = handler
        async def write(uuid, data, response=False):
            writes.append(bytes(data))
            if answer and data == driver.STATE_QUERY:
                if len(writes) == 1 or answer_after_send:
                    callback(None, reply_frame(0xA3))
        client = SimpleNamespace(start_notify=AsyncMock(side_effect=notify),
            stop_notify=AsyncMock(), write_gatt_char=AsyncMock(side_effect=write), disconnect=AsyncMock())
        with patch.object(driver, 'print_lock', asyncio.Lock()), \
             patch.object(driver, 'current_client', client), \
             patch.object(driver, 'ensure_connected', new_callable=AsyncMock, return_value=client), \
             patch.object(driver, 'png_to_rows', return_value=[bytes(48)]), \
             patch.object(driver, 'TAIL_DRAIN_SECONDS', 0.01), \
             patch.object(driver, 'STATUS_TIMEOUT_SECONDS', 0.02):
            if answer and answer_after_send:
                result = await driver.print_png(b'fake')
                self.assertTrue(result['device_reply_after_send'])
                self.assertFalse(result['physical_output_verified'])
                self.assertEqual(writes[0], driver.STATE_QUERY)
                self.assertEqual(writes[-1], driver.STATE_QUERY)
                self.assertNotIn('device_completion_reported', result)
            elif answer:
                with self.assertRaises(PrinterFault) as caught:
                    await driver.print_png(b'fake')
                self.assertTrue(caught.exception.output_uncertain)
                self.assertEqual(caught.exception.code, 'status_lost_after_send')
                self.assertGreater(len(writes), 1)
            else:
                with self.assertRaises(PrinterFault) as caught:
                    await driver.print_png(b'fake')
                self.assertFalse(caught.exception.output_uncertain)
                self.assertEqual(caught.exception.code, 'status_timeout')
                self.assertEqual(writes, [driver.STATE_QUERY])
        client.stop_notify.assert_awaited_once()
        client.disconnect.assert_awaited_once()

    async def test_reply_is_checked_before_image_and_after_send(self):
        await self.run_transport(answer=True)

    async def test_no_preflight_reply_stops_before_image(self):
        await self.run_transport(answer=False)

    async def test_no_post_send_reply_reports_uncertainty(self):
        await self.run_transport(answer=True, answer_after_send=False)
