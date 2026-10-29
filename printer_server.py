#!/usr/bin/env python3
"""Privacy-safe FastMCP server for PD01-compatible BLE thermal printers."""

from __future__ import annotations

import asyncio
import io
import json
import os
import uuid
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image as MCPImage
from PIL import Image, ImageDraw, ImageFont
from mcp.types import TextContent

import job_store
import printer_devices
import printer_runtime as runtime
from printer_faults import PrinterFault, fault, normalize_error, interpret_status, load_profile, raise_for_faults
from print_layout import PAPER_WIDTH, check_height, image_raster, png_bytes, mixed_raster
from printer_telemetry import read_battery

if os.name == "nt":
    from winrt.windows.devices.bluetooth import BluetoothCacheMode, BluetoothLEDevice
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattCommunicationStatus,
        GattWriteOption,
    )
    from winrt.windows.devices.enumeration import DeviceInformation
    from winrt.windows.storage.streams import Buffer


DEVICE_NAME = os.getenv("PD01_DEVICE_NAME", "PD01")
DEVICE_ADDRESS = os.getenv("PD01_DEVICE_ADDRESS", "").strip()
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "4176"))
PRINT_ENERGY = int(os.getenv("PD01_PRINT_ENERGY", "32768"))

BYTES_PER_LINE = PAPER_WIDTH // 8
MAX_TEXT_LENGTH = 2_000

SERVICE_UUID = "0000ae30-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000ae02-0000-1000-8000-00805f9b34fb"
CHUNK_DELAY_SECONDS = 0.02
TAIL_DRAIN_SECONDS = 2.0
STATUS_TIMEOUT_SECONDS = 3.0

FONT_CANDIDATES = (
    os.getenv("PD01_FONT_PATH", ""),
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)

mcp = FastMCP("pd01-printer")
current_client: Any | None = None
print_lock = asyncio.Lock()


def crc8(data: bytes) -> int:
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def command(code: int, payload: bytes = b"") -> bytes:
    length = len(payload).to_bytes(2, "little")
    return bytes((0x51, 0x78, code, 0x00)) + length + payload + bytes((crc8(payload), 0xFF))


class PrinterFeedback:
    """Parse fragmented/combined notifications without claiming physical paper output."""
    def __init__(self, status_profile: dict | None = None):
        self.buffer = bytearray()
        self.state_received = asyncio.Event()
        self.state_replies = 0
        self.state_payloads = []
        self.commands = set()
        self.ready_reported = False
        self.status_profile = status_profile
        self.fault_reported = None

    def receive(self, _characteristic, data):
        self.buffer.extend(data)
        while len(self.buffer) >= 8:
            start = self.buffer.find(b"\x51\x78")
            if start < 0:
                self.buffer[:] = self.buffer[-1:] if self.buffer[-1:] == b"\x51" else b""
                return
            del self.buffer[:start]
            if len(self.buffer) < 8:
                return
            length = int.from_bytes(self.buffer[4:6], "little")
            if length > 1024:
                del self.buffer[0]
                continue
            size = 8 + length
            if len(self.buffer) < size:
                return
            frame = bytes(self.buffer[:size])
            payload = frame[6:-2]
            if frame[-1] != 0xFF or crc8(payload) != frame[-2]:
                del self.buffer[0]
                continue
            del self.buffer[:size]
            if frame[3] != 1:
                continue
            self.commands.add(frame[2])
            if frame[2] == 0xA3:
                if not payload:
                    continue
                self.state_replies += 1
                self.state_payloads.append(payload)
                self.state_payloads = self.state_payloads[-16:]
                status = interpret_status(payload, self.status_profile)
                if status["faults"]:
                    # A recovery notification must not erase an error seen during this job.
                    self.fault_reported = status
                self.state_received.set()
            elif frame[2] == 0xAE and payload == b"\x00":
                self.ready_reported = True


STATE_QUERY = command(0xA3, b"\x00")
QUALITY_200_DPI = command(0xA4, b"\x32")
APPLY_ENERGY = command(0xBE, b"\x01")
LATTICE_START = command(0xA6, bytes.fromhex("aa551738445f5f5f44382c"))
LATTICE_END = command(0xA6, bytes.fromhex("aa55170000000000000017"))
SET_PAPER = command(0xA1, bytes.fromhex("3000"))


def set_energy(value: int) -> bytes:
    if not 0x2000 <= value <= 0xFFFF:
        raise ValueError("PD01_PRINT_ENERGY must be between 8192 and 65535")
    return command(0xAF, value.to_bytes(2, "big"))


def print_row(row: bytes) -> bytes:
    if len(row) != BYTES_PER_LINE:
        raise ValueError(f"A print row must contain {BYTES_PER_LINE} bytes")
    return command(0xA2, row)


def feed_paper(lines: int = 25) -> bytes:
    if not 1 <= lines <= 255:
        raise ValueError("Feed lines must be between 1 and 255")
    return command(0xBD, bytes((lines,)))


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                raise fault("font_unavailable") from None
    raise fault("font_unavailable")


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    no_start = set(",.;:!?%)]}，。、；：？！）》」』】〕〉…")
    no_end = set("([{（｛《「『【〔〈")

    def inside_word(left: str, right: str) -> bool:
        return all(char.isascii() and (char.isalnum() or char in "_'")
                   for char in (left, right))

    wrapped: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not paragraph:
            wrapped.append("")
            continue
        remainder = paragraph
        while remainder:
            if draw.textlength(remainder, font=font) <= max_width:
                wrapped.append(remainder)
                break
            fit = 0
            for index in range(1, len(remainder) + 1):
                if draw.textlength(remainder[:index], font=font) > max_width:
                    break
                fit = index
            if not fit:
                raise fault("glyph_too_wide")
            boundaries = [index for index in range(fit, 0, -1)
                          if remainder[index] not in no_start
                          and remainder[index - 1] not in no_end]
            preferred = [index for index in boundaries
                         if not inside_word(remainder[index - 1], remainder[index])]
            natural = [index for index in preferred
                       if index >= max(1, fit // 2)
                       and (remainder[index - 1].isspace()
                            or remainder[index - 1] in "，。；：？！,;:!?")]
            split_at = (natural or preferred or boundaries or [fit])[0]
            wrapped.append(remainder[:split_at].rstrip())
            remainder = remainder[split_at:].lstrip()
    return wrapped or [""]


def render_text(text: str, font_size: int, align: str) -> tuple[bytes, int, int, int]:
    if not isinstance(text, str) or not text.strip():
        raise fault("text_empty")
    if len(text) > MAX_TEXT_LENGTH:
        raise fault("text_too_long")
    if not isinstance(font_size, int) or isinstance(font_size, bool) or not 12 <= font_size <= 72:
        raise fault("font_size_invalid")
    if align not in {"left", "center", "right"}:
        raise fault("alignment_invalid")

    font = _load_font(font_size)
    padding_x = 20
    padding_y = 20
    line_spacing = 8
    scratch = Image.new("L", (PAPER_WIDTH, 1), 255)
    scratch_draw = ImageDraw.Draw(scratch)
    lines = _wrap_text(scratch_draw, text, font, PAPER_WIDTH - padding_x * 2)
    bbox = font.getbbox("Ag国")
    line_height = bbox[3] - bbox[1] + line_spacing
    total_height = max(1, padding_y * 2 + line_height * len(lines))
    check_height(total_height)

    image = Image.new("L", (PAPER_WIDTH, total_height), 255)
    draw = ImageDraw.Draw(image)
    y = padding_y
    for line in lines:
        if line:
            line_width = draw.textlength(line, font=font)
            if align == "left":
                x = padding_x
            elif align == "right":
                x = max(padding_x, int(PAPER_WIDTH - padding_x - line_width))
            else:
                x = max(padding_x, int((PAPER_WIDTH - line_width) / 2))
            draw.text((x, y), line, font=font, fill=0)
        y += line_height

    image = image.point(lambda value: 0 if value < 160 else 255, mode="1")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), image.width, image.height, len(lines)


def png_to_rows(png_data: bytes) -> list[bytes]:
    with Image.open(io.BytesIO(png_data)) as source:
        image = source.convert("L")

    if image.width > PAPER_WIDTH:
        new_height = max(1, round(image.height * PAPER_WIDTH / image.width))
        image = image.resize((PAPER_WIDTH, new_height), Image.Resampling.LANCZOS)
    elif image.width < PAPER_WIDTH:
        canvas = Image.new("L", (PAPER_WIDTH, image.height), 255)
        canvas.paste(image, ((PAPER_WIDTH - image.width) // 2, 0))
        image = canvas

    pixels = image.load()
    rows: list[bytes] = []
    for y in range(image.height):
        row = bytearray(BYTES_PER_LINE)
        for x in range(PAPER_WIDTH):
            if pixels[x, y] < 160:
                row[x // 8] |= 1 << (x % 8)
        rows.append(bytes(row))
    return rows


class WindowsPairedGattClient:
    """Minimal GATT writer using a Windows paired-device instance."""

    def __init__(self, name: str, address: str):
        self.name = name
        self.address = address
        self._device = None
        self._services = []
        self._write_characteristic = None

    @property
    def is_connected(self) -> bool:
        return self._device is not None and self._write_characteristic is not None

    async def connect(self) -> None:
        selector = BluetoothLEDevice.get_device_selector_from_device_name(self.name)
        records = list(await DeviceInformation.find_all_async_aqs_filter(selector))
        if not records:
            raise RuntimeError(f"Windows has no device record named {self.name}")

        compact_address = self.address.replace(":", "").upper()
        if compact_address:
            records.sort(key=lambda item: compact_address not in item.id.upper().replace(":", ""))

        errors: list[str] = []
        for record in records:
            device = await BluetoothLEDevice.from_id_async(record.id)
            if device is None:
                errors.append(f"Could not open Windows device record: {record.id}")
                continue
            services = []
            try:
                service_result = await device.get_gatt_services_with_cache_mode_async(
                    BluetoothCacheMode.UNCACHED
                )
                if service_result.status != GattCommunicationStatus.SUCCESS:
                    errors.append(f"GATT service status={int(service_result.status)}")
                    device.close()
                    continue

                services = list(service_result.services)
                write_uuid = str(uuid.UUID(WRITE_CHAR_UUID)).lower()
                write_characteristic = None
                for service in services:
                    result = await service.get_characteristics_with_cache_mode_async(
                        BluetoothCacheMode.UNCACHED
                    )
                    if result.status != GattCommunicationStatus.SUCCESS:
                        continue
                    for characteristic in result.characteristics:
                        if str(characteristic.uuid).lower() == write_uuid:
                            write_characteristic = characteristic
                            break
                    if write_characteristic is not None:
                        break

                if write_characteristic is None:
                    errors.append("AE01 write characteristic is unavailable; wake the printer")
                    for service in services:
                        service.close()
                    device.close()
                    continue

                self._device = device
                self._services = services
                self._write_characteristic = write_characteristic
                return
            except Exception as error:
                for service in services:
                    try:
                        service.close()
                    except Exception:
                        pass
                device.close()
                errors.append(str(error))

        raise RuntimeError("; ".join(errors) or f"Could not open {self.name}")

    async def write_gatt_char(self, characteristic_uuid: str, data: bytes, response: bool = False) -> None:
        if not self.is_connected:
            raise RuntimeError("Printer connection is closed")
        if uuid.UUID(characteristic_uuid) != uuid.UUID(WRITE_CHAR_UUID):
            raise RuntimeError(f"Unsupported write characteristic: {characteristic_uuid}")

        buffer = Buffer(len(data))
        buffer.length = buffer.capacity
        with memoryview(buffer) as view:
            view[:] = data
        result = await self._write_characteristic.write_value_with_result_and_option_async(
            buffer,
            GattWriteOption.WRITE_WITH_RESPONSE if response else GattWriteOption.WRITE_WITHOUT_RESPONSE,
        )
        if result.status != GattCommunicationStatus.SUCCESS:
            raise RuntimeError(f"Bluetooth write failed, status={int(result.status)}")

    async def disconnect(self) -> None:
        for service in self._services:
            try:
                service.close()
            except Exception:
                pass
        self._services = []
        self._write_characteristic = None
        if self._device is not None:
            self._device.close()
            self._device = None


async def connect_device() -> Any:
    address = DEVICE_ADDRESS
    if not address:
        address = (await printer_devices.resolve(job_store.default_state(), DEVICE_NAME))["id"]
    client = None
    try:
        device = await runtime.bounded(
            BleakScanner.find_device_by_address(address, timeout=8), runtime.SCAN_TIMEOUT_SECONDS,
            "printer_scan_timeout", "查找已选择的打印机超时")
        if device is None:
            raise PrinterFault("printer_unavailable", "没有发现已选择的打印机",
                               "唤醒已选择的设备；程序不会改连另一台同名打印机。")
        client = BleakClient(device, timeout=runtime.CONNECT_TIMEOUT_SECONDS)
        await runtime.bounded(client.connect(), runtime.CONNECT_TIMEOUT_SECONDS,
                              "printer_connect_timeout", "连接打印机超时")
        return client
    except BaseException as error:
        if client is not None:
            try:
                await runtime.bounded(client.disconnect(), runtime.CLEANUP_TIMEOUT_SECONDS,
                                      "printer_disconnect_timeout", "断开未完成的连接超时", phase="cleanup")
            except Exception:
                pass
        if isinstance(error, (PrinterFault, asyncio.CancelledError)):
            raise
        raise PrinterFault("printer_unavailable", "无法发现或连接打印机",
                           "确认打印机开机并靠近 Mac，关闭占用它的手机打印 App，再检查蓝牙权限。") from error


async def ensure_connected() -> Any:
    global current_client
    if current_client is not None and current_client.is_connected:
        return current_client
    current_client = await connect_device()
    return current_client


def configured_address(state: Path) -> str | None:
    target = printer_devices.configured_target(state)
    return target["id"] if target else None


async def list_available_printers(state: Path) -> dict:
    async with runtime.hardware_queue(print_lock):
        targets = await printer_devices.discover(DEVICE_NAME)
        try:
            target = printer_devices.configured_target(state)
        except PrinterFault:
            target = None
        selected = target["id"] if target else None
        return {"printers": [{**item, "selected": item["id"] == selected} for item in targets],
                "selected_printer": target,
                "paper_used": False}


async def select_target(state: Path, device_id: str) -> dict:
    async with runtime.hardware_queue(print_lock):
        target = await printer_devices.select(state, DEVICE_NAME, device_id)
        return {"selected_printer": target, "paper_used": False,
                "message": "已选择打印机；已打开的确认窗口仍绑定其原目标。"}


async def resolve_print_target(state: Path) -> dict:
    selected = printer_devices.configured_target(state)
    if selected:
        return selected
    job_store.report_progress("waiting_for_printer")
    async with runtime.hardware_queue(print_lock):
        return await printer_devices.resolve(state, DEVICE_NAME)


async def _cleanup(listening: bool, failure: BaseException | None, uncertain: bool) -> None:
    global current_client
    client = current_client
    errors = []
    try:
        if client is not None:
            operations = [("stop_notify", lambda: client.stop_notify(NOTIFY_CHAR_UUID))] if listening else []
            operations.append(("disconnect", client.disconnect))
            for name, operation in operations:
                try:
                    await runtime.bounded(operation(), runtime.CLEANUP_TIMEOUT_SECONDS,
                                          "printer_cleanup_timeout", "蓝牙连接清理超时",
                                          phase="cleanup", output_uncertain=uncertain)
                except Exception:
                    errors.append(name)
    finally:
        current_client = None
    if errors:
        if isinstance(failure, PrinterFault):
            failure.cleanup_errors = errors
        elif failure is None:
            raise PrinterFault("printer_cleanup_failed", "蓝牙连接清理未完成",
                               "先检查实物并重启打印机，暂勿重打已尝试过的任务。",
                               phase="cleanup", output_uncertain=uncertain)


async def _write(client, data: bytes, *, uncertain: bool = False, phase: str = "preflight") -> None:
    await runtime.bounded(client.write_gatt_char(WRITE_CHAR_UUID, data, response=False),
                          runtime.WRITE_TIMEOUT_SECONDS, "printer_write_timeout", "蓝牙写入超时",
                          phase=phase, output_uncertain=uncertain)


def _check_channel(client) -> None:
    char = client.services.get_characteristic(WRITE_CHAR_UUID)
    if char is None or "write-without-response" not in char.properties:
        raise PrinterFault("write_channel_unavailable", "设备没有提供兼容的打印通道",
                           "确认设备型号与本项目兼容，不要向未识别通道发送打印指令。")
    if char.max_write_without_response_size < 123:
        raise PrinterFault("write_chunk_unsupported", "设备的蓝牙写入容量不满足当前驱动要求",
                           "暂时停止发送，需要适配该设备的分包大小。")


async def diagnose_printer(state: Path, samples: int = 1, *, check_channel: bool = False) -> dict:
    """Read A3 status only. No image, feed, energy or print commands are sent."""
    global current_client, DEVICE_ADDRESS
    if not 1 <= samples <= 10:
        raise ValueError("samples must be between 1 and 10")
    feedback = PrinterFeedback()
    listening = False
    failure = None
    job_store.report_progress("waiting_for_printer")
    async with runtime.hardware_queue(print_lock):
        try:
            async with asyncio.timeout(runtime.DIAGNOSTIC_TIMEOUT_SECONDS):
                address = configured_address(state)
                if address is None:
                    address = (await printer_devices.resolve(state, DEVICE_NAME))["id"]
                profile = load_profile(state, address)
                if address != DEVICE_ADDRESS:
                    if current_client is not None:
                        await runtime.bounded(current_client.disconnect(), runtime.CLEANUP_TIMEOUT_SECONDS, "printer_disconnect_timeout", "切换设备时断开连接超时", phase="cleanup")
                        current_client = None
                    DEVICE_ADDRESS = address
                client = await runtime.bounded(ensure_connected(), runtime.CONNECT_TIMEOUT_SECONDS + runtime.SCAN_TIMEOUT_SECONDS, "printer_connect_timeout", "查找或连接打印机超时")
                if check_channel:
                    _check_channel(client)
                if not hasattr(client, "start_notify"):
                    raise PrinterFault("status_unsupported", "当前连接方式不支持读取设备状态",
                                       "请使用支持通知的 Mac 蓝牙连接；目前不能判断纸张或纸仓状态。")
                await runtime.bounded(client.start_notify(NOTIFY_CHAR_UUID, feedback.receive), runtime.NOTIFY_TIMEOUT_SECONDS, "printer_notify_timeout", "启用打印机状态通知超时")
                listening = True
                payloads = []
                for _ in range(samples):
                    feedback.state_received.clear()
                    await _write(client, STATE_QUERY)
                    try:
                        await asyncio.wait_for(feedback.state_received.wait(), STATUS_TIMEOUT_SECONDS)
                    except asyncio.TimeoutError:
                        raise PrinterFault("status_timeout", "打印机已连接，但没有返回有效状态",
                                           "检查设备是否休眠或被其他 App 占用；当前不能判定纸张状态。") from None
                    payloads.append(feedback.state_payloads[-1])
                    if len(payloads) < samples:
                        await asyncio.sleep(0.3)
                status = interpret_status(payloads[-1], profile)
                calibration_unavailable = (state / "status-profile.json").exists() and profile is None
                if calibration_unavailable:
                    status["summary"] = "已配置的故障检查规则不可用，打印将被阻止；请重新校准"
                return {"reachable": True, "paper_used": False,
                        "selected_printer": {"id": address, "name": DEVICE_NAME},
                        "status_samples": [payload.hex() for payload in payloads], **status,
                        "ok": not status["faults"] and not calibration_unavailable,
                        "calibration_available": profile is not None,
                        "calibration_required": calibration_unavailable,
                        "battery": await read_battery(client)}
        except PrinterFault as error:
            failure = error
            raise
        except asyncio.TimeoutError:
            failure = PrinterFault("printer_operation_timeout", "打印机操作超过总时限",
                                   "检查打印机；如发送可能已经开始，请先核对实物，不要自动重打。",
                                   phase="operation", output_uncertain=False)
            raise failure from None
        except asyncio.CancelledError as error:
            failure = error
            raise
        except Exception as error:
            failure = PrinterFault("status_query_failed", "无法读取打印机状态",
                                   "检查打印机电源、蓝牙占用和权限；当前不能判断纸张或纸仓状态。")
            raise failure from error
        finally:
            await _cleanup(listening, failure, False)


async def print_png(png_data: bytes, device_address: str | None = None,
                    status_profile: dict | None = None) -> dict[str, Any]:
    global current_client, DEVICE_ADDRESS
    rows = png_to_rows(png_data)
    feedback = PrinterFeedback(status_profile)
    listening = False
    post_send_reply = False
    bytes_queued = 0
    status = interpret_status(b"", status_profile)
    failure = None
    job_store.report_progress("waiting_for_printer")
    async with runtime.hardware_queue(print_lock):
        try:
            async with asyncio.timeout(runtime.PRINT_TIMEOUT_SECONDS):
                job_store.report_progress("checking_printer")
                if device_address is not None and device_address != DEVICE_ADDRESS:
                    if current_client is not None:
                        await runtime.bounded(current_client.disconnect(), runtime.CLEANUP_TIMEOUT_SECONDS, "printer_disconnect_timeout", "切换设备时断开连接超时", phase="cleanup")
                        current_client = None
                    DEVICE_ADDRESS = device_address
                client = await runtime.bounded(ensure_connected(), runtime.CONNECT_TIMEOUT_SECONDS + runtime.SCAN_TIMEOUT_SECONDS, "printer_connect_timeout", "查找或连接打印机超时")
                if hasattr(client, "start_notify"):
                    await runtime.bounded(client.start_notify(NOTIFY_CHAR_UUID, feedback.receive), runtime.NOTIFY_TIMEOUT_SECONDS, "printer_notify_timeout", "启用打印机状态通知超时")
                    listening = True
                    await _write(client, STATE_QUERY)
                    try:
                        await asyncio.wait_for(feedback.state_received.wait(), STATUS_TIMEOUT_SECONDS)
                    except asyncio.TimeoutError:
                        raise PrinterFault("status_timeout", "打印机已连接，但没有返回有效状态；本次未发送打印数据",
                                           "检查设备是否休眠或被其他 App 占用，当前无法判断纸张和纸仓状态。") from None
                    status = interpret_status(feedback.state_payloads[-1], status_profile)
                    raise_for_faults(feedback.fault_reported or status, phase="preflight", output_uncertain=False)
                    feedback.state_received.clear()
                elif status_profile and status_profile.get("rules"):
                    raise PrinterFault("status_unsupported", "当前连接不能执行已启用的故障检查，已停止打印",
                                       "请恢复支持设备状态通知的连接后重试。")
                data = bytearray()
                data.extend(STATE_QUERY)
                data.extend(QUALITY_200_DPI)
                data.extend(set_energy(PRINT_ENERGY))
                data.extend(APPLY_ENERGY)
                data.extend(LATTICE_START)
                for row in rows:
                    data.extend(print_row(row))
                data.extend(feed_paper(25))
                data.extend(SET_PAPER)
                data.extend(LATTICE_END)
                data.extend(STATE_QUERY)
                for offset in range(0, len(data), 123):
                    if offset == 0:
                        job_store.report_progress("sending")
                    if feedback.state_payloads:
                        status = interpret_status(feedback.state_payloads[-1], status_profile)
                        raise_for_faults(feedback.fault_reported or status, phase="sending", output_uncertain=bytes_queued > 0)
                    bytes_queued += len(data[offset : offset + 123])
                    await _write(client, data[offset : offset + 123], uncertain=True, phase="sending")
                    await asyncio.sleep(CHUNK_DELAY_SECONDS)
                # Writes without response only enqueue data. Keep BLE open while the tail drains.
                await asyncio.sleep(TAIL_DRAIN_SECONDS)
                if listening:
                    job_store.report_progress("awaiting_device_reply")
                    feedback.state_received.clear()
                    await _write(client, STATE_QUERY, uncertain=True, phase="post_send")
                    try:
                        await asyncio.wait_for(feedback.state_received.wait(), STATUS_TIMEOUT_SECONDS)
                        post_send_reply = True
                    except asyncio.TimeoutError:
                        raise PrinterFault("status_lost_after_send", "打印数据已发送，但设备停止返回状态，出纸结果不确定",
                                           "请先检查打印机和纸张，程序不会自动重打。",
                                           phase="post_send", output_uncertain=True) from None
                    status = interpret_status(feedback.state_payloads[-1], status_profile)
                    raise_for_faults(feedback.fault_reported or status, phase="post_send", output_uncertain=True)
        except PrinterFault as error:
            failure = error
            raise
        except asyncio.TimeoutError:
            failure = PrinterFault("printer_operation_timeout", "打印机操作超过总时限",
                                   "检查打印机；如发送可能已经开始，请先核对实物，不要自动重打。",
                                   phase="operation", output_uncertain=bool(bytes_queued))
            raise failure from None
        except asyncio.CancelledError as error:
            failure = error
            raise
        except Exception as error:
            normalized = normalize_error(error, phase="sending" if bytes_queued else "preflight",
                                         output_uncertain=bool(bytes_queued))
            if normalized.code.startswith("storage_"):
                failure = normalized
                raise normalized from error
            failure = PrinterFault("transfer_failed" if bytes_queued else "printer_check_failed",
                               "蓝牙发送中断，出纸结果不确定" if bytes_queued else "打印前设备检查失败，未发送打印数据",
                               "检查打印机电源、距离和蓝牙占用；如已开始出纸，请先核对实物，程序不会自动重打。",
                               phase="sending" if bytes_queued else "preflight",
                               output_uncertain=bool(bytes_queued))
            raise failure from error
        finally:
            await _cleanup(listening, failure, bool(bytes_queued))

    return {"data_sent": True, "physical_output_verified": False,
            "rows": len(rows), "bytes_sent": len(data),
            "device_reply_after_send": post_send_reply,
            "device_ready_reported": feedback.ready_reported,
            "printer_status": status,
            "feedback_commands": [f"{code:02x}" for code in sorted(feedback.commands)]}


async def send_stored_png(state: Path, png_data: bytes, *, target: dict | None = None) -> dict:
    address = target["id"] if target else configured_address(state)
    profile = load_profile(state, address)
    if (state / "status-profile.json").exists() and profile is None:
        raise fault("calibration_unavailable", phase="preflight")
    if address is None:
        address = (await resolve_print_target(state))["id"]
    return await print_png(png_data, device_address=address, status_profile=profile)


@mcp.tool()
async def list_printers() -> dict[str, Any]:
    """Scan matching PD01 printers and show local device IDs for explicit selection. Never connects or prints."""
    try:
        return await list_available_printers(job_store.default_state())
    except Exception as error:
        return normalize_error(error, phase="selection").as_result()


@mcp.tool()
async def select_printer(device_id: str) -> dict[str, Any]:
    """Select one PD01 from the scan list for future jobs. Existing confirmation windows retain their original target."""
    try:
        return await select_target(job_store.default_state(), device_id)
    except Exception as error:
        return normalize_error(error, phase="selection").as_result()


@mcp.tool()
async def check_printer() -> dict[str, Any]:
    """Query device status and report validated faults. Does not print or feed paper."""
    try:
        return await diagnose_printer(job_store.default_state(), check_channel=True)
    except PrinterFault as error:
        return error.as_result()
    except Exception as error:
        return normalize_error(error, phase="preflight").as_result()


def _prepare_text_result(text: str, font_size: int = 36, align: str = "center") -> ToolResult:
    """Prepare a durable job and return its exact PNG preview. Show image and text before asking for approval."""
    png_data, width, height, line_count = render_text(text, font_size, align)
    job = job_store.create(job_store.default_state(), text, png_data, width, height,
                           line_count, font_size, align)
    return _prepared_job_result(job, png_data)


def _prepared_job_result(job: dict, png_data: bytes) -> ToolResult:
    result = {
        **job,
        "prepared": True,
        "line_count": job["lines"],
        "image_width": job["width"],
        "image_height": job["height"],
        "estimated_length_mm": round(job["height"] / 8, 1),
        "paper_used": False,
        "next_step": "展示实际预览和完整文字，取得用户同意后调用 print_prepared；仍需点击本机确认按钮。",
    }
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False)),
                 MCPImage(data=png_data, format="png").to_image_content()],
        structured_content=result,
    )


@mcp.tool()
async def prepare_text(text: str, font_size: int = 36, align: str = "center") -> ToolResult:
    """Prepare a durable text job and exact PNG preview; return actionable preparation errors without printing."""
    try:
        return _prepare_text_result(text, font_size, align)
    except Exception as error:
        return ToolResult(structured_content=normalize_error(error).as_result())


@mcp.tool()
async def prepare_image(image_file: str) -> ToolResult:
    """Prepare a local image file and exact monochrome PNG preview. Does not connect or print."""
    try:
        raster = image_raster(Path(image_file).expanduser())
        png = png_bytes(raster)
        job = job_store.create(job_store.default_state(), "图片打印", png, raster.width,
                               raster.height, 0, None, "center", kind="image")
        return _prepared_job_result(job, png)
    except Exception as error:
        return ToolResult(structured_content=normalize_error(error).as_result())


@mcp.tool()
async def prepare_mixed(blocks: list[dict[str, Any]], base_dir: str = ".") -> ToolResult:
    """Prepare ordered text/image/spacer blocks and exact PNG preview. Image paths are local, relative to base_dir. Does not print."""
    try:
        png, width, height, text = mixed_raster(blocks, Path(base_dir).expanduser(), render_text)
        job = job_store.create(job_store.default_state(), text, png, width, height,
                               0, None, "center", kind="mixed")
        return _prepared_job_result(job, png)
    except Exception as error:
        return ToolResult(structured_content=normalize_error(error).as_result())


@mcp.tool()
async def get_job_status(job_id: str) -> dict[str, Any]:
    """Read a saved job phase or actionable fault without connecting to or printing on the device."""
    try:
        return job_store.read_status(job_store.default_state(), job_id)
    except Exception as error:
        return normalize_error(error).as_result()


@mcp.tool()
async def report_blank_output(job_id: str) -> dict[str, Any]:
    """Use only when the user reports paper fed but blank output. Save a client report and return checks, never a sensor diagnosis. Does not connect, feed or reprint."""
    try:
        return job_store.report_blank_output(job_store.default_state(), job_id)
    except Exception as error:
        return normalize_error(error).as_result()


@mcp.tool()
async def print_prepared(job_id: str, confirmation: str) -> dict[str, Any]:
    """Open a local Mac preview window. Only the local Confirm Print button allows one send. Closing/cancelling never prints."""
    state = job_store.default_state()
    frozen_target = None
    async def resolve():
        nonlocal frozen_target
        frozen_target = await resolve_print_target(state)
        return frozen_target
    async def sender(png):
        return await send_stored_png(state, png, target=frozen_target)
    try:
        return await job_store.dispatch(state, job_id, confirmation, sender, resolve_target=resolve)
    except PrinterFault as error:
        return error.as_result()
    except Exception as error:
        return normalize_error(error, phase="preflight").as_result()


@mcp.tool()
async def disconnect_printer() -> dict[str, Any]:
    """Disconnect the BLE printer. Does not print or feed paper."""
    global current_client
    try:
        async with runtime.hardware_queue(print_lock):
            await _cleanup(False, None, False)
    except Exception as error:
        return normalize_error(error, phase="cleanup").as_result()
    return {"disconnected": True, "paper_used": False}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=MCP_HOST, port=MCP_PORT)
