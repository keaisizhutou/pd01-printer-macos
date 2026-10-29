from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from printer_telemetry import read_battery


class BatteryTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_valid_single_byte_standard_percentage_is_accepted(self):
        characteristic = SimpleNamespace(properties=['read'])
        for payload, percent in [(b'\x00', 0), (b'\x64', 100), (b'\x65', None), (b'\x32\x00', None)]:
            client = SimpleNamespace(services=SimpleNamespace(get_characteristic=Mock(return_value=characteristic)),
                                     read_gatt_char=AsyncMock(return_value=payload))
            result = await read_battery(client)
            self.assertEqual(result['percent'], percent)
            client.read_gatt_char.assert_awaited_once_with(characteristic)

    async def test_absent_or_failed_interface_is_unknown_not_zero_percent(self):
        client = SimpleNamespace(services=SimpleNamespace(get_characteristic=Mock(return_value=None)))
        result = await read_battery(client)
        self.assertEqual(result, {'status': 'not_exposed', 'percent': None})
        characteristic = SimpleNamespace(properties=['read'])
        client.services.get_characteristic.return_value = characteristic
        client.read_gatt_char = AsyncMock(side_effect=RuntimeError('unavailable'))
        self.assertEqual(await read_battery(client), {'status': 'read_failed', 'percent': None})

    async def test_optional_battery_service_failure_does_not_break_primary_status(self):
        class UnavailableServices:
            @property
            def services(self):
                raise RuntimeError('Service discovery no longer available')
        self.assertEqual(await read_battery(UnavailableServices()),
                         {'status': 'read_failed', 'percent': None})


if __name__ == '__main__':
    unittest.main()
