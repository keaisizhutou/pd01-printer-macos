"""Local macOS CLI for PD01: check, preview, then explicitly print once."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

import printer_server as driver
import job_store
from print_layout import image_raster, png_bytes, mixed_raster
from printer_faults import PrinterFault, fault, normalize_error

DEFAULT_STATE = job_store.default_state()


def prepare(state: Path, text: str, font_size: int = 36, align: str = "center") -> dict:
    png, width, height, lines = driver.render_text(text, font_size, align)
    return job_store.create(state, text, png, width, height, lines, font_size, align)


def prepare_image(state: Path, source_path: Path) -> dict:
    """Rasterize an image to the printer width; preview and sent pixels remain identical."""
    raster = image_raster(source_path)
    return job_store.create(state, "图片打印", png_bytes(raster), raster.width,
                            raster.height, 0, None, "center", kind="image")


def read_input(path: Path) -> str:
    try:
        if path.stat().st_size > 1_000_000:
            raise fault("input_too_large")
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise fault("input_file_not_found") from None
    except UnicodeError:
        raise fault("input_invalid") from None


def prepare_mixed(state: Path, manifest: Path) -> dict:
    try:
        layout = json.loads(read_input(manifest))
    except ValueError:
        raise fault("layout_invalid") from None
    blocks = layout.get("blocks") if isinstance(layout, dict) else layout
    png, width, height, text = mixed_raster(blocks, manifest.resolve().parent, driver.render_text)
    return job_store.create(state, text, png, width, height, 0, None, "center", kind="mixed")


async def check(state: Path) -> dict:
    result = await driver.diagnose_printer(state, check_channel=True)
    return {**result, "connected": result["reachable"], "compatible_write_channel": True}


async def print_job(state: Path, job_id: str, confirmation: str, sender=None) -> dict:
    resolver = None
    if sender is None:
        frozen_target = None
        async def resolver():
            nonlocal frozen_target
            frozen_target = await driver.resolve_print_target(state)
            return frozen_target
        async def sender(png):
            return await driver.send_stored_png(state, png, target=frozen_target)
    return await job_store.dispatch(state, job_id, confirmation, sender, resolve_target=resolver)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("check", help="Connect and inspect the write channel without using paper")
    actions.add_parser("printers", help="List matching printers for explicit selection")
    selecting = actions.add_parser("select-printer", help="Select one printer from the scan list")
    selecting.add_argument("device_id")
    status_action = actions.add_parser("status", help="Query printer status without printing or feeding paper")
    status_action.add_argument("--samples", type=int, default=1)
    status_action.add_argument("--sample-label", choices=("normal", "cover_open", "paper_missing", "recovered"))
    actions.add_parser("calibrate-status", help="Validate four controlled status sample sets and save local fault rules")
    actions.add_parser("setup-confirmation", help="Build the local confirmation window without printing")
    actions.add_parser("confirm-demo", help="Show the native confirmation window without connecting or printing")
    prep = actions.add_parser("prepare", help="Render UTF-8 text to a local PNG without printing")
    prep.add_argument("--text-file", type=Path, required=True)
    prep.add_argument("--font-size", type=int, default=36)
    prep.add_argument("--align", choices=("left", "center", "right"), default="center")
    image_prep = actions.add_parser("prepare-image", help="Prepare a black-and-white image preview without printing")
    image_prep.add_argument("--image-file", type=Path, required=True)
    mixed_prep = actions.add_parser("prepare-mixed", help="Prepare a text/image receipt from a local JSON manifest")
    mixed_prep.add_argument("--manifest", type=Path, required=True)
    job_status = actions.add_parser("job-status", help="Read the saved job phase and any actionable error")
    job_status.add_argument("job_id")
    blank_report = actions.add_parser("report-blank", help="Record reported blank output and return troubleshooting advice; never prints")
    blank_report.add_argument("job_id")
    printing = actions.add_parser("print", help="Open a native confirmation window, then print once if approved")
    printing.add_argument("job_id")
    printing.add_argument("--confirm", required=True)
    args = parser.parse_args()
    state = args.state_dir.expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == "check":
        result = asyncio.run(check(state))
    elif args.action == "printers":
        result = asyncio.run(driver.list_available_printers(state))
    elif args.action == "select-printer":
        result = asyncio.run(driver.select_target(state, args.device_id))
    elif args.action == "status":
        result = asyncio.run(driver.diagnose_printer(state, args.samples))
        if args.sample_label:
            from datetime import datetime, timezone
            import hashlib
            folder = state / "diagnostics"
            folder.mkdir(exist_ok=True, mode=0o700)
            record = {"label": args.sample_label, "samples": result["status_samples"],
                      "timestamp": datetime.now(timezone.utc).isoformat(),
                      "device_id_sha256": hashlib.sha256(result["selected_printer"]["id"].encode()).hexdigest()}
            job_store.save_json(folder / f'{args.sample_label}.json', record)
    elif args.action == "setup-confirmation":
        from native_confirmation import build_helper
        result = {'confirmation_helper': str(build_helper()), 'paper_used': False}
    elif args.action == "calibrate-status":
        from status_calibration import calibrate
        try:
            result = calibrate(state, driver.configured_address(state))
        except FileNotFoundError:
            raise fault("calibration_samples_missing") from None
        except (ValueError, KeyError, TypeError):
            raise fault("calibration_invalid_samples") from None
    elif args.action == "confirm-demo":
        from native_confirmation import request_confirmation
        text = '这是确认窗口测试。\n点击后也不会打印。'
        png, width, height, lines = driver.render_text(text, 36, 'center')
        approved = asyncio.run(request_confirmation(
            {'job_id': 'demo', 'font_size': 36, 'align': 'center'}, png, demo=True))
        result = {'demo': True, 'button_confirmed': bool(approved), 'paper_used': False}
    elif args.action == "prepare":
        result = prepare(state, read_input(args.text_file), args.font_size, args.align)
    elif args.action == "prepare-image":
        result = prepare_image(state, args.image_file)
    elif args.action == "prepare-mixed":
        result = prepare_mixed(state, args.manifest)
    elif args.action == "job-status":
        result = job_store.read_status(state, args.job_id)
    elif args.action == "report-blank":
        result = job_store.report_blank_output(state, args.job_id)
    else:
        result = asyncio.run(print_job(state, args.job_id, args.confirm))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("ok") is False:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except PrinterFault as error:
        print(json.dumps(error.as_result(), ensure_ascii=False, indent=2))
        sys.exit(1)
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps(normalize_error(error, output_uncertain=isinstance(error, KeyboardInterrupt)).as_result(),
                         ensure_ascii=False, indent=2))
        sys.exit(1)
