"""A local AppKit preview window; approval is returned on a private child-process pipe."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from printer_faults import fault

TIMEOUT_SECONDS = 120
SOURCE = Path(__file__).parent / "native" / "PrintConfirmation.swift"


def build_helper() -> Path:
    if sys.platform != "darwin":
        raise fault("confirmation_unavailable", phase="confirmation")
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:20]
    cache = Path.home() / "Library" / "Caches" / "pd01-printer-macos"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = cache / f"confirm-{digest}"
    if binary.is_file():
        return binary
    descriptor, temporary = tempfile.mkstemp(dir=cache, prefix=".confirm-")
    os.close(descriptor)
    try:
        result = subprocess.run(
            ["/usr/bin/xcrun", "swiftc", "-O", "-module-cache-path", str(cache / "modules"),
             str(SOURCE), "-o", temporary],
            capture_output=True, timeout=180,
        )
        if result.returncode:
            raise fault("confirmation_build_failed", phase="confirmation")
        os.chmod(temporary, 0o700)
        os.replace(temporary, binary)
        return binary
    except subprocess.TimeoutExpired as error:
        raise fault("confirmation_build_timeout", phase="confirmation") from error
    finally:
        Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class ConfirmationDecision:
    approved: bool
    reason: str

    def __bool__(self):
        return self.approved


def parse_decision(output: bytes, returncode: int, request: dict) -> ConfirmationDecision:
    if returncode != 0:
        raise fault("confirmation_window_failed", phase="confirmation")
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError):
        raise fault("confirmation_response_invalid", phase="confirmation") from None
    if not isinstance(result, dict) or any(result.get(key) != request[key]
                                          for key in ("job_id", "nonce", "sha256", "purpose")):
        raise fault("confirmation_response_invalid", phase="confirmation")
    if result.get("printer_id") != request.get("printer_id"):
        raise fault("confirmation_response_invalid", phase="confirmation")
    if result.get("approved") is True and result.get("reason") == "approved":
        return ConfirmationDecision(True, "approved")
    if result.get("approved") is False and result.get("reason") in ("cancelled", "window_closed"):
        return ConfirmationDecision(False, result["reason"])
    raise fault("confirmation_response_invalid", phase="confirmation")


async def request_confirmation(metadata: dict, png: bytes, *, demo: bool = False) -> ConfirmationDecision:
    """Show exact in-memory pixels. No CLI/MCP option can silently approve this window."""
    binary = await asyncio.to_thread(build_helper)
    request = {
        "job_id": metadata["job_id"], "nonce": secrets.token_hex(32),
        "sha256": hashlib.sha256(png).hexdigest(),
        "purpose": "demo" if demo else "print",
        "image": base64.b64encode(png).decode("ascii"),
        "fontSize": metadata["font_size"], "align": metadata["align"],
        "kind": metadata.get("kind", "text"),
        "printer_id": metadata.get("printer_id"), "printerName": metadata.get("printer_name"),
    }
    # Swift uses camelCase for the one field; the response uses the job-store spelling.
    payload = {**request, "jobID": request["job_id"], "printerID": request["printer_id"]}
    try:
        process = await asyncio.create_subprocess_exec(
            str(binary), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as error:
        raise fault("confirmation_window_failed", phase="confirmation") from error
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode("utf-8")), TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise fault("confirmation_timeout", phase="confirmation") from None
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    return parse_decision(output, process.returncode, request)
