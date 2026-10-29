import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

from fastmcp import Client
from PIL import Image
from server_info import service_identity
import native_confirmation
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class InstallationContractTests(unittest.TestCase):
    def test_confirmation_binary_cache_is_separated_by_cpu_architecture(self):
        with patch.object(native_confirmation.platform, 'machine', return_value='arm64'):
            arm = native_confirmation.helper_path()
        with patch.object(native_confirmation.platform, 'machine', return_value='x86_64'):
            intel = native_confirmation.helper_path()
        self.assertNotEqual(arm, intel)
        self.assertTrue(arm.name.endswith('-arm64'))
        self.assertTrue(intel.name.endswith('-x86_64'))

    def test_lock_matches_its_input_and_all_requirements_have_hashes(self):
        source = (ROOT / 'requirements.txt').read_bytes()
        lock = (ROOT / 'requirements.lock').read_text()
        self.assertIn('# source-sha256: ' + hashlib.sha256(source).hexdigest(), lock)
        canonical = lambda name: re.sub(r'[-_.]+', '-', name).lower()
        pinned = {canonical(name): version for name, version in
                  re.findall(r'^([a-zA-Z0-9_.-]+)==([^\s;\\]+)', lock, re.MULTILINE)}
        for line in source.decode().splitlines():
            if line and not line.startswith('#'):
                name, version = line.split('==')
                self.assertEqual(pinned[canonical(name)], version)
        records = lock.replace('\\\n', '').splitlines()
        for line in records:
            if line and not line.startswith('#'):
                self.assertIn('--hash=sha256:', line)

    def test_service_identity_distinguishes_source_state_and_code_without_exposing_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'; source.mkdir()
            (source / 'printer_server.py').write_text('first revision')
            first = service_identity(source, root / 'state-a')
            self.assertEqual(first, service_identity(source, root / 'state-a'))
            self.assertNotEqual(first['state_id'], service_identity(source, root / 'state-b')['state_id'])
            self.assertNotEqual(first['source_id'], service_identity(root / 'other', root / 'state-a')['source_id'])
            (source / 'printer_server.py').write_text('second revision')
            second = service_identity(source, root / 'state-a')
            self.assertNotEqual(first['build_id'], second['build_id'])
            self.assertEqual(first['source_id'], second['source_id'])
            self.assertNotIn(str(root), json.dumps(second))
            self.assertFalse(second['printer_checked'])
            self.assertFalse(second['physical_output_verified'])


@unittest.skipUnless(sys.platform == 'darwin', 'Real installation smoke test targets macOS')
class LocalHTTPInstallationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pd01-install-test-')
        self.state = Path(self.temp.name)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.environment = {**os.environ, 'PD01_STATE_DIR': str(self.state),
                            'MCP_HOST': '127.0.0.1', 'MCP_PORT': str(self.port),
                            'PD01_DEVICE_ADDRESS': '', 'PYTHONNOUSERSITE': '1'}
        self.bootstrap = '''
from pathlib import Path
import runpy, sys
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(sys.argv[1]).parent))
import bleak, job_store
async def forbidden(*args, **kwargs):
    raise AssertionError('Installation validation must not access Bluetooth or a confirmation window')
bleak.BleakScanner.discover = forbidden
bleak.BleakScanner.find_device_by_address = forbidden
bleak.BleakScanner.find_device_by_filter = forbidden
job_store.request_confirmation = forbidden
runpy.run_path(sys.argv[1], run_name='__main__')
'''
        self.command = [sys.executable, '-c', self.bootstrap, str(ROOT / 'printer_server.py')]
        self.log = (self.state / 'server.log').open('wb')
        self.process = subprocess.Popen(self.command, cwd=self.state, env=self.environment,
                                        stdout=self.log, stderr=self.log)
        self.addAsyncCleanup(self.close)
        self.expected = service_identity(ROOT, self.state)
        for _ in range(150):
            if self.process.poll() is not None:
                self.fail('Test server exited before readiness: ' + str(self.process.returncode))
            try:
                actual = await asyncio.to_thread(self.health)
                if actual == self.expected:
                    break
            except (OSError, urllib.error.URLError):
                pass
            await asyncio.sleep(.1)
        else:
            self.fail('Test server did not become ready within 15 seconds')

    def health(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{self.port}/health', timeout=1) as response:
            return json.loads(response.read(16384))

    async def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, timeout=3)
        self.log.close()
        self.temp.cleanup()

    async def test_running_service_identity_is_read_back_without_printer_access(self):
        self.assertEqual(await asyncio.to_thread(self.health), self.expected)
        self.assertFalse((self.state / 'device.json').exists())
        self.assertFalse((self.state / 'status-profile.json').exists())
        self.assertFalse((self.state / 'jobs').exists())

    async def test_real_http_mcp_prepares_all_layouts_and_rejects_unapproved_printing(self):
        picture = self.state / 'square.png'
        Image.new('1', (80, 80), 0).save(picture)
        cases = [('prepare_text', {'text': '安装验收，不打印'}, 'text'),
                 ('prepare_image', {'image_file': str(picture)}, 'image'),
                 ('prepare_mixed', {'base_dir': str(self.state), 'blocks': [
                     {'type': 'text', 'text': '混排验收'},
                     {'type': 'image', 'path': 'square.png', 'width': 120}]}, 'mixed')]
        async with Client(f'http://127.0.0.1:{self.port}/mcp') as client:
            names = {tool.name for tool in await client.list_tools()}
            self.assertEqual(len(names), 10)
            for tool, arguments, kind in cases:
                result = await client.call_tool(tool, arguments)
                job = result.structured_content
                self.assertTrue(job.get('prepared'), job)
                self.assertEqual(job['kind'], kind)
                image = next(block for block in result.content if block.type == 'image')
                data = base64.b64decode(image.data)
                self.assertEqual(data, Path(job['preview']).read_bytes())
                self.assertEqual(hashlib.sha256(data).hexdigest(), job['sha256'])
                status = await client.call_tool('get_job_status', {'job_id': job['job_id']})
                self.assertEqual(status.structured_content['status'], 'prepared')
                rejected = await client.call_tool('print_prepared', {'job_id': job['job_id'], 'confirmation': 'wrong'})
                self.assertEqual(rejected.structured_content['error_code'], 'confirmation_required')
                report = await client.call_tool('report_blank_output', {'job_id': job['job_id']})
                self.assertEqual(report.structured_content['error_code'], 'output_not_recorded')
        self.assertFalse(list(self.state.rglob('attempt.json')))
        self.assertFalse((self.state / 'device.json').exists())
        self.assertFalse((self.state / 'status-profile.json').exists())

    async def test_occupied_port_does_not_replace_or_stop_existing_service(self):
        second = subprocess.Popen(self.command, cwd=self.state, env={**self.environment,
            'PD01_STATE_DIR': str(self.state / 'another-state')},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            code = await asyncio.to_thread(second.wait, timeout=15)
            self.assertNotEqual(code, 0)
            self.assertIsNone(self.process.poll())
            self.assertEqual(await asyncio.to_thread(self.health), self.expected)
        finally:
            if second.poll() is None:
                second.kill()
                await asyncio.to_thread(second.wait, timeout=3)


if __name__ == '__main__': unittest.main()
