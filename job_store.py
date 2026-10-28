"""Shared, local preview jobs and at-most-one send attempt for CLI and MCP."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile

CONFIRMATION_TOKEN = "CONFIRM_PRINT"


def default_state() -> Path:
    configured = os.getenv("PD01_STATE_DIR")
    return Path(configured).expanduser() if configured else (
        Path.home() / "Library" / "Application Support" / "pd01-printer-macos"
    )


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def save_json(path: Path, value: dict) -> None:
    """Write privately and atomically so interruptions do not truncate existing state."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def job_folder(state: Path, job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{16}", job_id):
        raise ValueError("Invalid job ID")
    folder = state / "jobs" / job_id
    if not folder.is_dir():
        raise ValueError("Unknown job ID")
    return folder


def create(state: Path, text: str, png: bytes, width: int, height: int,
           lines: int, font_size: int, align: str) -> dict:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_id = secrets.token_hex(8)
    folder = state / "jobs" / job_id
    folder.mkdir(parents=True, mode=0o700)
    preview = folder / "preview.png"
    descriptor = os.open(preview, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(png)
        output.flush()
        os.fsync(output.fileno())
    data = {
        "job_id": job_id, "text": text, "font_size": font_size, "align": align,
        "width": width, "height": height, "lines": lines,
        "sha256": hashlib.sha256(png).hexdigest(),
    }
    save_json(folder / "job.json", data)
    return {**data, "preview": str(preview), "paper_used": False}


def load(state: Path, job_id: str) -> tuple[dict, bytes]:
    folder = job_folder(state, job_id)
    data = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    png = (folder / "preview.png").read_bytes()
    if hashlib.sha256(png).hexdigest() != data["sha256"]:
        raise ValueError("Preview changed. Prepare a new job and ask for approval again")
    return data, png


async def dispatch(state: Path, job_id: str, confirmation: str, sender) -> dict:
    if confirmation != CONFIRMATION_TOKEN:
        raise ValueError("Show the preview and obtain explicit human approval before printing")
    _, png = load(state, job_id)
    folder = job_folder(state, job_id)
    attempt = folder / "attempt.json"
    try:
        descriptor = os.open(attempt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RuntimeError("This job has already been attempted. Inspect the paper; do not retry automatically") from None
    # Claim and flush BEFORE any await or hardware access, across both entry points/processes.
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump({"status": "started_outcome_unknown"}, output)
        output.flush()
        os.fsync(output.fileno())
    sync_directory(folder)
    try:
        result = await sender(png)
        save_json(attempt, {"status": "sent", "result": result})
        return {"job_id": job_id, "data_sent": True,
                "physical_output_verified": False, "driver_result": result}
    except BaseException:
        # If saving also fails, the durable initial claim still blocks any later attempt.
        try:
            save_json(attempt, {"status": "uncertain_do_not_retry"})
        except OSError:
            pass
        raise
