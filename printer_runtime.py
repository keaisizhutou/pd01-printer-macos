"""Bounded Bluetooth operations and a per-user, cross-process hardware queue."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import errno
import os
from pathlib import Path
import time

from printer_faults import PrinterFault, normalize_error

QUEUE_TIMEOUT_SECONDS = 120.0
SCAN_TIMEOUT_SECONDS = 12.0
CONNECT_TIMEOUT_SECONDS = 20.0
NOTIFY_TIMEOUT_SECONDS = 5.0
WRITE_TIMEOUT_SECONDS = 5.0
CLEANUP_TIMEOUT_SECONDS = 3.0
PRINT_TIMEOUT_SECONDS = 120.0
DIAGNOSTIC_TIMEOUT_SECONDS = 90.0


async def bounded(awaitable, seconds: float, code: str, message: str, *,
                  phase: str = "preflight", output_uncertain: bool = False):
    try:
        return await asyncio.wait_for(awaitable, seconds)
    except asyncio.TimeoutError:
        raise PrinterFault(code, message,
                           "检查打印机电源、距离和蓝牙占用；发送可能已开始时先核对实物，不要自动重打。",
                           phase=phase, output_uncertain=output_uncertain) from None


def queue_path() -> Path:
    # Shared across different PD01_STATE_DIR values and check/print processes.
    return Path.home() / "Library" / "Application Support" / "pd01-printer-macos" / "hardware.lock"


def _try_lock(descriptor: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_UN)


@asynccontextmanager
async def hardware_queue(local_lock: asyncio.Lock):
    """Serialize the adapter; cancellation/crash releases ownership, never a print retry."""
    deadline = time.monotonic() + QUEUE_TIMEOUT_SECONDS
    descriptor = None
    locked = False
    entered = False
    await bounded(local_lock.acquire(), QUEUE_TIMEOUT_SECONDS, "printer_queue_timeout",
                  "等待打印机队列超时，本次未发送打印数据", phase="queue")
    try:
        path = queue_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        if os.name == "nt" and os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        while not (locked := _try_lock(descriptor)):
            if time.monotonic() >= deadline:
                raise PrinterFault("printer_queue_timeout", "等待打印机队列超时，本次未发送打印数据",
                                   "检查其他打印任务是否仍在执行；不要重试已尝试过的任务。", phase="queue")
            await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
        entered = True
        yield
    except OSError as error:
        if not entered:
            raise normalize_error(error, phase="queue") from error
        raise
    finally:
        try:
            if descriptor is not None:
                try:
                    if locked:
                        _unlock(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            local_lock.release()
        # Never unlink a lock file: other processes may still have its inode open.
