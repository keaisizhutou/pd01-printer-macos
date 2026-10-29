"""Shared, local preview jobs and at-most-one send attempt for CLI and MCP."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from contextvars import ContextVar

from native_confirmation import request_confirmation
from printer_faults import PrinterFault, fault, normalize_error
from print_layout import PAPER_WIDTH, check_height
from print_troubleshooting import blank_output_advice

_progress_callback = ContextVar("print_job_progress", default=None)


def report_progress(phase: str) -> None:
    callback = _progress_callback.get()
    if callback:
        callback(phase)

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
    if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{16}", job_id):
        raise fault("job_id_invalid")
    folder = state / "jobs" / job_id
    if not folder.is_dir():
        raise fault("job_not_found")
    return folder


def create(state: Path, text: str, png: bytes, width: int, height: int,
           lines: int, font_size: int | None, align: str, *, kind: str = "text") -> dict:
    if kind not in ("text", "image", "mixed") or width != PAPER_WIDTH or not isinstance(height, int) or height < 1:
        raise fault("layout_invalid")
    check_height(height)
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
        "job_id": job_id, "text": text, "font_size": font_size, "align": align, "kind": kind,
        "width": width, "height": height, "lines": lines,
        "sha256": hashlib.sha256(png).hexdigest(),
    }
    save_json(folder / "job.json", data)
    return {**data, "preview": str(preview), "paper_used": False}


def load(state: Path, job_id: str) -> tuple[dict, bytes]:
    folder = job_folder(state, job_id)
    try:
        data = json.loads((folder / "job.json").read_text(encoding="utf-8"))
        png = (folder / "preview.png").read_bytes()
        digest = data["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or data.get("job_id") != job_id:
            raise ValueError()
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        raise fault("job_corrupt") from None
    except OSError as error:
        raise normalize_error(error) from error
    if hashlib.sha256(png).hexdigest() != digest:
        raise fault("preview_changed")
    return data, png


def read_status(state: Path, job_id: str) -> dict:
    folder = job_folder(state, job_id)
    attempt = folder / "attempt.json"
    if not attempt.exists():
        return {"job_id": job_id, "status": "prepared", "message": "预览已准备，尚未发送",
                "data_sent": False, "physical_output_verified": False,
                "status_source": "local_record", "automatic_retry": False}
    try:
        result = json.loads(attempt.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError()
        if not isinstance(result.get('status'), str) or not isinstance(result.get('error', {}), dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise fault("job_corrupt") from None
    phase = result.get("status")
    messages = {"waiting_confirmation": "最近记录：等待本机窗口按钮确认", "checking_printer": "最近记录：检查打印机状态",
                "sending": "最近记录：发送打印数据", "awaiting_device_reply": "最近记录：已发送，等待设备回复",
                "sent": "数据已发送，实物出纸仍需确认", "cancelled_before_send": "本次已取消，未发送",
                "blocked_before_send": "检测到问题，已阻止发送", "uncertain_do_not_retry": "出纸结果不确定，不能自动重打",
                "confirmation_failed_before_send": "确认阶段失败，未发送", "started_outcome_unknown": "上次操作结果未知，不能自动重打"}
    sent = True if phase == "sent" else False if phase in ("waiting_confirmation", "checking_printer",
        "cancelled_before_send", "blocked_before_send", "confirmation_failed_before_send") else None
    response = {"job_id": job_id, **result, "message": result.get("error", {}).get("message", messages.get(phase, "任务状态未知")),
                "data_sent": sent, "physical_output_verified": False, "status_source": "local_record",
                "automatic_retry": False}
    issue = folder / "issue-report.json"
    if issue.exists():
        try:
            report = json.loads(issue.read_text(encoding="utf-8"))
            if not isinstance(report, dict) or report.get("symptom") != "blank_output" or report.get("source") != "client_report":
                raise ValueError()
        except (ValueError, TypeError):
            raise fault("job_corrupt") from None
        response["reported_issue"] = report
    return response


def report_blank_output(state: Path, job_id: str) -> dict:
    """Record a client's report and return advice, without changing the send attempt."""
    from datetime import datetime, timezone
    metadata, png = load(state, job_id)
    status = read_status(state, job_id)
    if status["status"] not in ("sent", "uncertain_do_not_retry", "started_outcome_unknown"):
        raise fault("output_not_recorded")
    report = {"job_id": job_id, **blank_output_advice(png),
              "reported_at": datetime.now(timezone.utc).isoformat(),
              "preview_sha256": metadata["sha256"], "last_attempt_status": status["status"]}
    save_json(job_folder(state, job_id) / "issue-report.json", report)
    return report


async def dispatch(state: Path, job_id: str, confirmation: str, sender) -> dict:
    if confirmation != CONFIRMATION_TOKEN:
        raise fault("confirmation_required", phase="confirmation")
    metadata, png = load(state, job_id)
    folder = job_folder(state, job_id)
    attempt = folder / "attempt.json"
    try:
        descriptor = os.open(attempt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise fault("job_already_attempted", phase="preflight") from None
    except OSError as error:
        raise normalize_error(error, phase="preflight") from error
    # Claim and flush BEFORE any await or hardware access, across both entry points/processes.
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump({"status": "waiting_confirmation", "data_sent": False}, output)
            output.flush()
            os.fsync(output.fileno())
        sync_directory(folder)
    except OSError as error:
        raise normalize_error(error, phase="confirmation") from error
    sending_started = False
    progress_token = _progress_callback.set(lambda phase: save_json(attempt, {"status": phase}))
    try:
        decision = await request_confirmation(metadata, png)
        if not decision:
            reason = getattr(decision, "reason", "cancelled")
            save_json(attempt, {"status": "cancelled_before_send", "reason": reason, "data_sent": False})
            return {"job_id": job_id, "data_sent": False, "cancelled": True, "reason": reason,
                    "paper_used": False, "physical_output_verified": False}
        report_progress("checking_printer")
        sending_started = True
        result = await sender(png)
        save_json(attempt, {"status": "sent", "result": result})
        return {"job_id": job_id, "data_sent": True,
                "physical_output_verified": False, "driver_result": result,
                "next_step": "请核对实物；若走纸但空白，可用 report_blank_output 获取排查建议。不要自动重打。"}
    except BaseException as error:
        # If saving also fails, the durable initial claim still blocks any later attempt.
        try:
            if isinstance(error, PrinterFault):
                save_json(attempt, {"status": "uncertain_do_not_retry" if error.output_uncertain
                                   else "blocked_before_send", "error": error.as_result()})
            else:
                normalized = normalize_error(error, phase="sending" if sending_started else "confirmation",
                                             output_uncertain=sending_started)
                save_json(attempt, {"status": "uncertain_do_not_retry" if sending_started
                                   else "confirmation_failed_before_send", "error": normalized.as_result()})
        except OSError:
            pass
        if isinstance(error, (PrinterFault, asyncio.CancelledError)):
            raise
        raise normalize_error(error, phase="sending" if sending_started else "confirmation",
                              output_uncertain=sending_started) from error
    finally:
        _progress_callback.reset(progress_token)
