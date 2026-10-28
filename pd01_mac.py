"""Local macOS CLI for PD01: check, preview, then explicitly print once."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import printer_server as driver
import job_store

DEFAULT_STATE = job_store.default_state()


def prepare(state: Path, text: str, font_size: int = 36, align: str = "center") -> dict:
    png, width, height, lines = driver.render_text(text, font_size, align)
    return job_store.create(state, text, png, width, height, lines, font_size, align)


async def check(state: Path) -> dict:
    from bleak import BleakClient, BleakScanner
    discovered = await BleakScanner.discover(timeout=10, return_adv=True)
    devices = [device for device, adv in discovered.values()
               if (adv.local_name or device.name or "") == driver.DEVICE_NAME]
    requested_id = os.getenv("PD01_DEVICE_ADDRESS", "").strip()
    if requested_id:
        devices = [device for device in devices if device.address == requested_id]
    if not devices:
        raise RuntimeError("No PD01 found. Wake it, close the phone app, and check Bluetooth permission.")
    if len(devices) != 1:
        raise RuntimeError("Multiple PD01 devices found. Turn off the others or set PD01_DEVICE_ADDRESS.")
    device = devices[0]
    async with BleakClient(device, timeout=20) as client:
        char = client.services.get_characteristic(driver.WRITE_CHAR_UUID)
        if char is None or "write-without-response" not in char.properties:
            raise RuntimeError("The printer does not expose the expected write channel")
        if char.max_write_without_response_size < 123:
            raise RuntimeError("The write channel is too small for this driver's 123-byte chunks")
        job_store.save_json(state / "device.json", {"id": device.address, "name": device.name})
        return {"connected": bool(client.is_connected), "compatible_write_channel": True,
                "paper_used": False}


async def print_job(state: Path, job_id: str, confirmation: str, sender=None) -> dict:
    if sender is None:
        async def sender(png):
            return await driver.send_stored_png(state, png)
    return await job_store.dispatch(state, job_id, confirmation, sender)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("check", help="Connect and inspect the write channel without using paper")
    actions.add_parser("setup-confirmation", help="Build the local confirmation window without printing")
    actions.add_parser("confirm-demo", help="Show the native confirmation window without connecting or printing")
    prep = actions.add_parser("prepare", help="Render UTF-8 text to a local PNG without printing")
    prep.add_argument("--text-file", type=Path, required=True)
    prep.add_argument("--font-size", type=int, default=36)
    prep.add_argument("--align", choices=("left", "center", "right"), default="center")
    printing = actions.add_parser("print", help="Open a native confirmation window, then print once if approved")
    printing.add_argument("job_id")
    printing.add_argument("--confirm", required=True)
    args = parser.parse_args()
    state = args.state_dir.expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == "check":
        result = asyncio.run(check(state))
    elif args.action == "setup-confirmation":
        from native_confirmation import build_helper
        result = {'confirmation_helper': str(build_helper()), 'paper_used': False}
    elif args.action == "confirm-demo":
        from native_confirmation import request_confirmation
        text = '这是确认窗口测试。\n点击后也不会打印。'
        png, width, height, lines = driver.render_text(text, 36, 'center')
        approved = asyncio.run(request_confirmation(
            {'job_id': 'demo', 'font_size': 36, 'align': 'center'}, png, demo=True))
        result = {'demo': True, 'button_confirmed': approved, 'paper_used': False}
    elif args.action == "prepare":
        result = prepare(state, args.text_file.read_text(encoding="utf-8"), args.font_size, args.align)
    else:
        result = asyncio.run(print_job(state, args.job_id, args.confirm))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
