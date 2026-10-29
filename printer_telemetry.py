"""Read only standardized telemetry; never guess battery values from opaque status bytes."""
import asyncio

BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


async def read_battery(client) -> dict:
    result = {"status": "unverified", "percent": None}
    try:
        services = getattr(client, "services", None)
        if services is None:
            return result
        characteristic = services.get_characteristic(BATTERY_LEVEL_UUID)
        if characteristic is None:
            return {"status": "not_exposed", "percent": None}
        if "read" not in characteristic.properties:
            return {"status": "not_readable", "percent": None}
        data = bytes(await asyncio.wait_for(client.read_gatt_char(characteristic), 3))
        if len(data) != 1 or data[0] > 100:
            return {"status": "invalid_value", "percent": None}
        return {"status": "standard_value", "percent": data[0]}
    except Exception:
        return {"status": "read_failed", "percent": None}
