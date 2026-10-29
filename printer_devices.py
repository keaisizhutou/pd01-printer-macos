"""One selection policy for CLI and MCP; no first-match fallback for unbound printers."""
from __future__ import annotations

import json
import os
from pathlib import Path

from bleak import BleakScanner
import job_store
from printer_faults import PrinterFault
import printer_runtime as runtime


def configured_target(state: Path) -> dict | None:
    address = os.getenv("PD01_DEVICE_ADDRESS", "").strip()
    if address:
        return {"id": address, "name": os.getenv("PD01_DEVICE_NAME", "PD01")}
    path = state / "device.json"
    if not path.exists():
        return None
    try:
        target = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(target, dict) or not isinstance(target.get("id"), str) or not target["id"].strip():
            raise ValueError()
        name = target.get("name") or "PD01"
        if not isinstance(name, str):
            raise ValueError()
        return {"id": target["id"], "name": name}
    except (ValueError, TypeError):
        raise PrinterFault("printer_configuration_invalid", "本机打印机配置损坏",
                           "重新扫描并选择打印机；不要套用其他设备的校准记录。") from None


async def discover(name: str) -> list[dict]:
    try:
        records = await runtime.bounded(
            BleakScanner.discover(timeout=10, return_adv=True), runtime.SCAN_TIMEOUT_SECONDS,
            "printer_scan_timeout", "蓝牙扫描超时")
    except PrinterFault:
        raise
    except Exception as error:
        raise PrinterFault("printer_search_failed", "蓝牙扫描失败",
                           "检查 Mac 蓝牙是否开启，以及当前程序是否有蓝牙权限。") from error
    targets = {}
    for device, advertisement in records.values():
        if (advertisement.local_name or device.name or "") == name:
            targets[device.address] = {"id": device.address, "name": name}
    return sorted(targets.values(), key=lambda item: item["id"])


def _save_target(state: Path, target: dict) -> dict:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_store.save_json(state / "device.json", target)
    return target


async def resolve(state: Path, name: str) -> dict:
    selected = configured_target(state)
    if selected:
        return selected
    targets = await discover(name)
    if not targets:
        raise PrinterFault("printer_not_found", "没有发现目标打印机",
                           "唤醒打印机并靠近 Mac，关闭手机打印 App，再重新扫描。")
    if len(targets) != 1:
        raise PrinterFault("multiple_printers_found", "发现多台同名打印机，尚未选择目标",
                           "先使用 printers／list_printers 查看列表，再使用 select-printer／select_printer 指定设备。")
    return _save_target(state, targets[0])


async def select(state: Path, name: str, device_id: str) -> dict:
    forced = os.getenv("PD01_DEVICE_ADDRESS", "").strip()
    if forced and forced != device_id:
        raise PrinterFault("printer_selection_overridden", "环境变量指定了另一台打印机",
                           "先更新或移除 PD01_DEVICE_ADDRESS，再重新选择，程序不会静默忽略它。")
    for target in await discover(name):
        if target["id"] == device_id:
            return _save_target(state, target)
    raise PrinterFault("printer_not_found", "扫描列表里没有指定的 PD01",
                       "先查看 printers／list_printers，确认设备开机并使用列表中的设备编号。")
