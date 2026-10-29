import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastmcp import Client
from PIL import Image
import job_store
import printer_server as server
from printer_faults import PrinterFault
from print_troubleshooting import blank_output_advice


def png(marked=True):
    output = io.BytesIO()
    Image.new('1', (384, 40), 0 if marked else 255).save(output, format='PNG')
    return output.getvalue()


class BlankOutputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.job = job_store.create(self.state, 'fixture', png(), 384, 40, 1, 36, 'center')
        self.folder = Path(self.job['preview']).parent

    def sent(self):
        job_store.save_json(self.folder / 'attempt.json', {'status': 'sent', 'result': {'data_sent': True}})

    def test_advice_distinguishes_blank_preview_from_reported_paper_problem(self):
        for marked in (True, False):
            with self.subTest(marked=marked):
                advice = blank_output_advice(png(marked))
                self.assertEqual(advice['preview_contains_marks'], marked)
                self.assertEqual(advice['checks'][0]['code'], 'paper_orientation' if marked else 'blank_preview')
                self.assertFalse(advice['sensor_confirmed'])
                self.assertFalse(advice['physical_output_verified'])
                self.assertFalse(advice['automatic_retry'])
                self.assertNotIn('paper_reversed', advice)

    async def test_report_persists_without_altering_the_attempt_or_enabling_replay(self):
        self.sent()
        before = (self.folder / 'attempt.json').read_bytes()
        with patch.object(job_store, 'request_confirmation', new_callable=AsyncMock) as window:
            report = job_store.report_blank_output(self.state, self.job['job_id'])
            self.assertEqual((self.folder / 'attempt.json').read_bytes(), before)
            self.assertEqual(report['source'], 'client_report')
            status = job_store.read_status(self.state, self.job['job_id'])
            self.assertEqual(status['status'], 'sent')
            self.assertTrue(status['data_sent'])
            self.assertFalse(status['physical_output_verified'])
            self.assertEqual(status['reported_issue']['symptom'], 'blank_output')
            sender = AsyncMock()
            with self.assertRaises(PrinterFault) as caught:
                await job_store.dispatch(self.state, self.job['job_id'], 'CONFIRM_PRINT', sender)
            self.assertEqual(caught.exception.code, 'job_already_attempted')
            sender.assert_not_called()
            window.assert_not_called()

    def test_unsent_or_cancelled_job_cannot_be_recorded_as_blank_output(self):
        for stage in (None, 'cancelled_before_send', 'blocked_before_send'):
            if stage:
                job_store.save_json(self.folder / 'attempt.json', {'status': stage})
            with self.subTest(stage=stage), self.assertRaises(PrinterFault) as caught:
                job_store.report_blank_output(self.state, self.job['job_id'])
            self.assertEqual(caught.exception.code, 'output_not_recorded')
        self.assertFalse((self.folder / 'issue-report.json').exists())

    def test_uncertain_task_gets_advice_without_claiming_completed_send(self):
        job_store.save_json(self.folder / 'attempt.json', {'status': 'uncertain_do_not_retry'})
        report = job_store.report_blank_output(self.state, self.job['job_id'])
        self.assertEqual(report['last_attempt_status'], 'uncertain_do_not_retry')
        self.assertIsNone(job_store.read_status(self.state, self.job['job_id'])['data_sent'])

    async def test_mcp_report_and_status_never_connect_or_send(self):
        self.sent()
        with patch.dict(os.environ, {'PD01_STATE_DIR': str(self.state)}), \
             patch.object(server, 'ensure_connected', new_callable=AsyncMock) as connection, \
             patch.object(server, 'send_stored_png', new_callable=AsyncMock) as sender:
            async with Client(server.mcp) as client:
                report = await client.call_tool('report_blank_output', {'job_id': self.job['job_id']})
                result = await client.call_tool('get_job_status', {'job_id': self.job['job_id']})
                missing = await client.call_tool('report_blank_output', {'job_id': '0000000000000000'})
        self.assertFalse(report.structured_content['sensor_confirmed'])
        self.assertFalse(report.structured_content['additional_print_sent'])
        self.assertTrue(result.structured_content['reported_issue']['checks'])
        self.assertEqual(missing.structured_content['error_code'], 'job_not_found')
        connection.assert_not_called()
        sender.assert_not_called()

    def test_corrupt_report_is_not_presented_as_a_sensor_result(self):
        self.sent()
        (self.folder / 'issue-report.json').write_text('broken')
        with self.assertRaises(PrinterFault) as caught:
            job_store.read_status(self.state, self.job['job_id'])
        self.assertEqual(caught.exception.code, 'job_corrupt')


if __name__ == '__main__':
    unittest.main()
