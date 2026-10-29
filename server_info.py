"""Read-only service identity for installation verification; never a printer health claim."""
from __future__ import annotations

import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys

SOURCE_FILES = (
    "printer_server.py", "server_info.py", "job_store.py", "pd01_mac.py",
    "printer_devices.py", "printer_runtime.py", "printer_faults.py",
    "print_layout.py", "print_troubleshooting.py", "printer_telemetry.py",
    "status_calibration.py", "native_confirmation.py", "native/PrintConfirmation.swift",
    "requirements.txt", "requirements.lock",
)


def service_identity(root: Path, state: Path) -> dict:
    digest = hashlib.sha256()
    for name in SOURCE_FILES:
        digest.update(name.encode() + b"\0")
        path = root / name
        data = path.read_bytes() if path.is_file() else b""
        digest.update(len(data).to_bytes(8, "big") + data)
    runtime = {"prefix": str(Path(sys.prefix).resolve()), "python": sys.version,
               "packages": sorted((dist.metadata.get("Name", ""), dist.version) for dist in metadata.distributions())}
    return {"service": "pd01-printer-macos", "schema": 1,
            "source_id": hashlib.sha256(str(root.resolve()).encode()).hexdigest(),
            "state_id": hashlib.sha256(str(state.resolve()).encode()).hexdigest(),
            "build_id": digest.hexdigest(),
            "runtime_id": hashlib.sha256(json.dumps(runtime, sort_keys=True).encode()).hexdigest(),
            "printer_checked": False,
            "physical_output_verified": False}
