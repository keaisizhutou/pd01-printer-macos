import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import printer_server as driver
from printer_faults import PrinterFault, interpret_status, load_profile


# Synthetic test fixture; this is NOT a shipped PD01 hardware mapping.
PROFILE = {"schema": 1, "device_id_sha256": hashlib.sha256(b'test-device').hexdigest(),
           "rules": [{"byte_index": 0, "mask": 1, "value": 1,
                      "code": "paper_or_cover_problem", "message": "缺纸或纸仓未扣好",
                      "action": "检查纸张并重新扣好纸仓。"}]}


def state_frame(flag):
    frame = bytearray(driver.command(0xA3, bytes([flag, 1, 193])))
    frame[3] = 1
    return frame


class StatusMeaningTests(unittest.TestCase):
    def test_unverified_payload_is_unknown_not_paper_ready(self):
        result = interpret_status(bytes([0, 1, 193]), None)
        self.assertEqual(result['validated_checks'], [])
        self.assertIn('尚未验证', result['summary'])
        self.assertFalse(result['paper_movement_verified'])

    def test_only_validated_mask_is_interpreted(self):
        self.assertEqual(interpret_status(bytes([1]), PROFILE)['faults'][0]['code'], 'paper_or_cover_problem')
        self.assertEqual(interpret_status(bytes([0x80]), PROFILE)['faults'], [])
        self.assertFalse(interpret_status(bytes([0]), PROFILE)['physical_output_verified'])

    def test_profile_is_bound_to_the_current_device(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'status-profile.json').write_text(json.dumps(PROFILE))
            self.assertIsNotNone(load_profile(state, 'test-device'))
            self.assertIsNone(load_profile(state, 'another-device'))

    def test_corrupt_profile_does_not_invent_status(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'status-profile.json').write_text('broken')
            self.assertIsNone(load_profile(state, 'test-device'))


class FaultBlockingTests(unittest.IsolatedAsyncioTestCase):
    async def transport(self, preflight_flag=0, mid_send_flag=None):
        writes = []
        callback = None
        async def notify(uuid, handler):
            nonlocal callback
            callback = handler
        async def write(uuid, data, response=False):
            writes.append(bytes(data))
            if data == driver.STATE_QUERY:
                callback(None, state_frame(preflight_flag))
            elif mid_send_flag is not None:
                callback(None, state_frame(mid_send_flag))
        client = SimpleNamespace(start_notify=AsyncMock(side_effect=notify), stop_notify=AsyncMock(),
                                 write_gatt_char=AsyncMock(side_effect=write), disconnect=AsyncMock())
        return client, writes

    async def test_validated_paper_fault_stops_before_any_image(self):
        client, writes = await self.transport(preflight_flag=1)
        with patch.object(driver, 'print_lock', asyncio.Lock()), \
             patch.object(driver, 'current_client', client), \
             patch.object(driver, 'ensure_connected', new_callable=AsyncMock, return_value=client), \
             patch.object(driver, 'png_to_rows', return_value=[bytes(48)]):
            with self.assertRaises(PrinterFault) as caught:
                await driver.print_png(b'fake', status_profile=PROFILE)
        self.assertEqual(writes, [driver.STATE_QUERY])
        self.assertEqual(caught.exception.code, 'paper_or_cover_problem')
        self.assertFalse(caught.exception.output_uncertain)
        self.assertFalse(caught.exception.as_result()['paper_used'])
        client.disconnect.assert_awaited_once()

    async def test_mid_send_fault_stops_further_chunks_and_reports_uncertainty(self):
        client, writes = await self.transport(mid_send_flag=1)
        with patch.object(driver, 'print_lock', asyncio.Lock()), \
             patch.object(driver, 'current_client', client), \
             patch.object(driver, 'ensure_connected', new_callable=AsyncMock, return_value=client), \
             patch.object(driver, 'png_to_rows', return_value=[bytes(48)] * 5):
            with self.assertRaises(PrinterFault) as caught:
                await driver.print_png(b'fake', status_profile=PROFILE)
        self.assertEqual(len(writes), 2)
        self.assertTrue(caught.exception.output_uncertain)
        self.assertIsNone(caught.exception.as_result()['paper_used'])

    async def test_recovery_reply_cannot_erase_a_fault_reported_in_the_same_send(self):
        writes = []
        callback = None
        async def notify(uuid, handler):
            nonlocal callback
            callback = handler
        async def write(uuid, data, response=False):
            writes.append(bytes(data))
            callback(None, state_frame(0) if data == driver.STATE_QUERY
                     else state_frame(1) + state_frame(0))
        client = SimpleNamespace(start_notify=AsyncMock(side_effect=notify), stop_notify=AsyncMock(),
                                 write_gatt_char=AsyncMock(side_effect=write), disconnect=AsyncMock())
        with patch.object(driver, 'print_lock', asyncio.Lock()), \
             patch.object(driver, 'current_client', client), \
             patch.object(driver, 'ensure_connected', new_callable=AsyncMock, return_value=client), \
             patch.object(driver, 'png_to_rows', return_value=[bytes(48)] * 5):
            with self.assertRaises(PrinterFault) as caught:
                await driver.print_png(b'fake', status_profile=PROFILE)
        self.assertEqual(caught.exception.code, 'paper_or_cover_problem')
        self.assertTrue(caught.exception.output_uncertain)
        self.assertEqual(len(writes), 2)

    async def test_status_query_never_sends_print_or_feed_commands(self):
        client, writes = await self.transport()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(driver, 'print_lock', asyncio.Lock()), \
             patch.object(driver, 'current_client', client), \
             patch.object(driver, 'configured_address', return_value='test-device'), \
             patch.object(driver, 'DEVICE_ADDRESS', 'test-device'), \
             patch.object(driver, 'ensure_connected', new_callable=AsyncMock, return_value=client):
            result = await driver.diagnose_printer(Path(directory), samples=2)
        self.assertEqual(writes, [driver.STATE_QUERY, driver.STATE_QUERY])
        self.assertEqual(result['status_samples'], ['0001c1', '0001c1'])
        self.assertFalse(result['paper_used'])

    async def test_empty_state_frame_is_not_a_valid_status_reply(self):
        feedback = driver.PrinterFeedback()
        frame = bytearray(driver.command(0xA3, b''))
        frame[3] = 1
        feedback.receive(None, frame)
        self.assertFalse(feedback.state_received.is_set())

    async def test_status_channel_failure_has_actionable_feedback(self):
        client = SimpleNamespace(start_notify=AsyncMock(side_effect=RuntimeError('channel unavailable')),
                                 disconnect=AsyncMock())
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(driver, 'print_lock', asyncio.Lock()), \
             patch.object(driver, 'current_client', client), \
             patch.object(driver, 'configured_address', return_value='test-device'), \
             patch.object(driver, 'DEVICE_ADDRESS', 'test-device'), \
             patch.object(driver, 'ensure_connected', new_callable=AsyncMock, return_value=client):
            with self.assertRaises(PrinterFault) as caught:
                await driver.diagnose_printer(Path(directory))
        self.assertEqual(caught.exception.code, 'status_query_failed')
        self.assertTrue(caught.exception.as_result()['action'])
        self.assertFalse(caught.exception.output_uncertain)

    async def test_enabled_fault_checks_cannot_be_silently_skipped(self):
        client = SimpleNamespace(write_gatt_char=AsyncMock(), disconnect=AsyncMock())
        with patch.object(driver, 'print_lock', asyncio.Lock()), \
             patch.object(driver, 'current_client', client), \
             patch.object(driver, 'ensure_connected', new_callable=AsyncMock, return_value=client), \
             patch.object(driver, 'png_to_rows', return_value=[bytes(48)]):
            with self.assertRaises(PrinterFault) as caught:
                await driver.print_png(b'fake', status_profile=PROFILE)
        self.assertEqual(caught.exception.code, 'status_unsupported')
        client.write_gatt_char.assert_not_called()


if __name__ == '__main__':
    unittest.main()
