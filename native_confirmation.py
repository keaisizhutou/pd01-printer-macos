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

TIMEOUT_SECONDS = 120
SOURCE = Path(__file__).parent / "native" / "PrintConfirmation.swift"


def build_helper() -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("Native confirmation requires a logged-in macOS desktop")
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
            raise RuntimeError("Cannot build confirmation window. Install Apple Command Line Tools (xcode-select --install).")
        os.chmod(temporary, 0o700)
        os.replace(temporary, binary)
        return binary
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Confirmation window build timed out; run setup-confirmation before printing.") from error
    finally:
        Path(temporary).unlink(missing_ok=True)


def response_approved(output: bytes, returncode: int, request: dict) -> bool:
    if returncode != 0:
        return False
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError):
        return False
    return (isinstance(result, dict) and result.get("approved") is True
            and all(result.get(key) == request[key]
                    for key in ("job_id", "nonce", "sha256", "purpose")))


async def request_confirmation(metadata: dict, png: bytes, *, demo: bool = False) -> bool:
    """Show exact in-memory pixels. No CLI/MCP option can silently approve this window."""
    binary = await asyncio.to_thread(build_helper)
    request = {
        "job_id": metadata["job_id"], "nonce": secrets.token_hex(32),
        "sha256": hashlib.sha256(png).hexdigest(),
        "purpose": "demo" if demo else "print",
        "image": base64.b64encode(png).decode("ascii"),
        "fontSize": metadata["font_size"], "align": metadata["align"],
        "kind": metadata.get("kind", "text"),
    }
    # Swift uses camelCase for the one field; the response uses the job-store spelling.
    payload = {**request, "jobID": request["job_id"]}
    process = await asyncio.create_subprocess_exec(
        str(binary), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode("utf-8")), TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return False
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    return response_approved(output, process.returncode, request)
