"""Local macOS CLI for PD01: check, preview, then explicitly print once."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys

import printer_server as driver

DEFAULT_STATE = Path.home() / "Library" / "Application Support" / "pd01-printer-macos"


def save_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def job_folder(state: Path, job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{16}", job_id):
        raise ValueError("Invalid job ID")
    folder = state / "jobs" / job_id
    if not folder.is_dir():
        raise ValueError("Unknown job ID")
    return folder


def prepare(state: Path, text: str, font_size: int = 36, align: str = "center") -> dict:
    png, width, height, lines = driver.render_text(text, font_size, align)
    job_id = secrets.token_hex(8)
    folder = state / "jobs" / job_id
    folder.mkdir(parents=True, mode=0o700)
    preview = folder / "preview.png"
    preview.write_bytes(png)
    preview.chmod(0o600)
    data = {
        "job_id": job_id, "text": text, "font_size": font_size, "align": align,
        "width": width, "height": height, "lines": lines,
        "sha256": hashlib.sha256(png).hexdigest(),
    }
    save_json(folder / "job.json", data)
    return {**data, "preview": str(preview), "paper_used": False}


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
        save_json(state / "device.json", {"id": device.address, "name": device.name})
        return {"connected": bool(client.is_connected), "compatible_write_channel": True,
                "paper_used": False}


async def print_job(state: Path, job_id: str, confirmation: str, sender=None) -> dict:
    if confirmation != "CONFIRM_PRINT":
        raise ValueError("Show the preview and obtain explicit human approval before printing")
    folder = job_folder(state, job_id)
    data = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    png = (folder / "preview.png").read_bytes()
    if hashlib.sha256(png).hexdigest() != data["sha256"]:
        raise ValueError("Preview changed. Prepare a new job and ask for approval again")
    if sender is None:
        device = json.loads((state / "device.json").read_text(encoding="utf-8"))
        driver.DEVICE_ADDRESS = device["id"]
        sender = driver.print_png
    # An exclusive, durable claim prevents retries even after crashes or partial sends.
    attempt = folder / "attempt.json"
    try:
        with attempt.open("x", encoding="utf-8") as output:
            json.dump({"status": "started_outcome_unknown"}, output)
        attempt.chmod(0o600)
    except FileExistsError:
        raise RuntimeError("This job has already been attempted. Inspect the paper; do not retry automatically") from None
    try:
        result = await sender(png)
        save_json(attempt, {"status": "sent", "result": result})
        return {"job_id": job_id, "data_sent": True,
                "physical_output_verified": False, "driver_result": result}
    except BaseException:
        save_json(attempt, {"status": "uncertain_do_not_retry"})
        raise
    finally:
        if driver.current_client is not None:
            await driver.current_client.disconnect()
            driver.current_client = None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("check", help="Connect and inspect the write channel without using paper")
    prep = actions.add_parser("prepare", help="Render UTF-8 text to a local PNG without printing")
    prep.add_argument("--text-file", type=Path, required=True)
    prep.add_argument("--font-size", type=int, default=36)
    prep.add_argument("--align", choices=("left", "center", "right"), default="center")
    printing = actions.add_parser("print", help="Print an approved job once")
    printing.add_argument("job_id")
    printing.add_argument("--confirm", required=True)
    args = parser.parse_args()
    state = args.state_dir.expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == "check":
        result = asyncio.run(check(state))
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
