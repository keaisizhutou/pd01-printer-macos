"""Derive local status rules from controlled normal/open/empty/recovered observations."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import job_store

LABELS = ("normal", "cover_open", "paper_missing", "recovered")


def derive_profile(samples: dict[str, list[str]], device_id: str) -> dict:
    values = {}
    for label in LABELS:
        records = samples.get(label, [])
        if len(records) < 3:
            raise ValueError(f"At least three confirmed {label} samples are required")
        try:
            payloads = [bytes.fromhex(value) for value in records]
            if any(not payload for payload in payloads):
                raise ValueError()
            values[label] = [payload[0] for payload in payloads]
        except (ValueError, TypeError):
            raise ValueError(f"Invalid {label} status samples") from None
        if len(set(values[label])) != 1:
            raise ValueError(f"Unstable {label} flag samples; do not enable fault rules")
    # Only the first status byte has protocol evidence for flags. Never infer bits
    # from the changing later bytes (which may include analog readings).
    normal = values["normal"] + values["recovered"]
    if len(set(normal)) != 1:
        raise ValueError("Normal and recovered status do not agree; do not enable fault rules")
    normal_value = normal[0]

    def asserted_mask(label):
        result = 255
        for value in values[label]:
            result &= value
        return result & ~normal_value & 255

    opened = asserted_mask("cover_open")
    empty = asserted_mask("paper_missing")
    shared = opened & empty
    definitions = [
        (opened & ~empty, "cover_open", "纸仓未扣好", "重新扣紧纸仓盖，再查询设备状态。"),
        (empty & ~opened, "paper_missing", "打印机缺纸", "装入热敏纸、压好纸并扣紧纸仓，再查询设备状态。"),
        (shared, "paper_or_cover_problem", "缺纸或纸仓未扣好", "检查是否有纸、纸是否压好，并重新扣紧纸仓盖。"),
    ]
    rules = [{"byte_index": 0, "mask": mask, "value": mask, "code": code,
              "message": message, "action": action}
             for mask, code, message, action in definitions if mask]
    return {"schema": 1, "device_id_sha256": hashlib.sha256(device_id.encode()).hexdigest(),
            "calibrated_at": datetime.now(timezone.utc).isoformat(),
            "normal_flag_byte": normal_value, "rules": rules,
            "unverified_conditions": [label for label, mask in (("cover_open", opened), ("paper_missing", empty)) if not mask],
            "evidence": {label: samples[label] for label in LABELS}}


def calibrate(state: Path, device_id: str | None) -> dict:
    if not device_id:
        raise ValueError("Check the target printer before calibrating status")
    expected = hashlib.sha256(device_id.encode()).hexdigest()
    samples = {}
    for label in LABELS:
        path = state / "diagnostics" / f"{label}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("label") != label:
            raise ValueError("Sample label mismatch")
        if record.get("device_id_sha256") != expected:
            raise ValueError("All status samples must come from the current printer")
        samples[label] = record["samples"]
    profile = derive_profile(samples, device_id)
    job_store.save_json(state / "status-profile.json", profile)
    return {"profile_saved": True, "validated_faults": [rule["message"] for rule in profile["rules"]],
            "unverified_conditions": profile["unverified_conditions"], "paper_used": False}
