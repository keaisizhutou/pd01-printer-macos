"""Actionable printer errors and device-scoped, empirically validated status flags."""
from __future__ import annotations

import hashlib
import json
import errno
import asyncio
from pathlib import Path

SOFTWARE_ERRORS = {
    "input_file_not_found": ("找不到输入文件", "检查文件是否存在、路径是否正确。"),
    "input_invalid": ("输入文件无法按 UTF-8 文本读取", "将文件保存为 UTF-8 编码后重试。"),
    "input_too_large": ("输入文件过大", "缩短文字或混排内容，拆成多个任务。"),
    "text_empty": ("打印文字为空", "请输入文字后重新准备预览。"),
    "text_too_long": ("文字过长", "缩短内容或拆成多张后重新准备。"),
    "font_size_invalid": ("字号不在支持范围内", "请选择 12 到 72 的整数值。"),
    "alignment_invalid": ("对齐方式无效", "请选择左对齐、居中或右对齐。"),
    "font_unavailable": ("找不到可用字体", "设置可读取的中文 TTF/TTC 字体后重新准备。"),
    "glyph_too_wide": ("文字超出可打印宽度", "缩小字号后重新准备预览。"),
    "image_not_found": ("找不到图片文件", "检查图片文件是否存在，再重新准备。"),
    "image_invalid": ("图片无法读取或格式不支持", "换用可正常打开的 PNG 或 JPEG 图片。"),
    "image_too_large": ("图片尺寸过大", "先缩小原图，再重新准备打印预览。"),
    "image_multiple_frames": ("暂不支持多帧图片", "先将需要打印的一帧保存为 PNG。"),
    "layout_invalid": ("混排内容格式不正确", "检查文字、图片和留白区块的格式及参数。"),
    "paper_length_exceeded": ("内容超出单张长度上限", "缩短内容、缩小图片或拆成多张；程序不会截掉内容。"),
    "job_id_invalid": ("打印任务编号无效", "使用准备预览时返回的任务编号。"),
    "job_not_found": ("打印任务不存在", "重新准备预览，使用新任务编号。"),
    "job_corrupt": ("打印任务记录不完整或已损坏", "重新准备一个任务，不要复用当前记录。"),
    "preview_changed": ("确认的预览内容已发生变化", "重新生成并查看预览，再确认打印。"),
    "confirmation_required": ("尚未请求人工确认", "先展示预览，再请求打开本机确认窗口。"),
    "job_already_attempted": ("这张任务已尝试过，已阻止重复打印", "先检查实物和任务记录；确需另一张时重新准备并确认。"),
    "output_not_recorded": ("该任务还没有可核对的发送结果", "先检查任务编号和任务状态；没有发送的任务不能记录为出白纸。"),
    "confirmation_timeout": ("等待本机确认超时，本次未打印", "准备新任务，在确认窗口中点击打印；不要复用旧任务。"),
    "confirmation_unavailable": ("本机确认窗口不可用", "请在已登录桌面的 Mac 上运行。"),
    "confirmation_build_failed": ("确认窗口构建失败", "安装 Apple Command Line Tools 后运行 setup-confirmation。"),
    "confirmation_build_timeout": ("确认窗口构建超时", "先单独运行 setup-confirmation，完成后再准备新任务。"),
    "confirmation_window_failed": ("确认窗口启动失败或异常退出，本次未打印", "检查本机桌面会话，再准备新任务。"),
    "confirmation_response_invalid": ("确认结果无法校验，本次未打印", "重新准备任务，并在对应的本机窗口中确认。"),
    "storage_full": ("本机存储空间不足", "释放磁盘空间；如发送已经开始，先检查实物，不能自动重打。"),
    "storage_permission_denied": ("没有读取或保存文件的权限", "检查输入文件和本机数据目录的权限。"),
    "storage_read_only": ("本机数据目录不可写", "改用可写目录；不要移动或删除已有打印尝试记录。"),
    "operation_cancelled": ("操作已中止", "如发送可能已经开始，请先检查实物，再决定是否准备新任务。"),
    "operation_failed": ("操作未完成", "查看本机错误记录；如发送可能已开始，请先检查实物，不要自动重打。"),
    "calibration_unavailable": ("已配置的故障检查规则无法用于当前设备", "检查本机校准记录；更换设备或记录损坏时重新校准，程序不会跳过检查继续打印。"),
    "calibration_samples_missing": ("缺少故障校准样本", "按正常、开盖留纸、无纸关盖、重新装纸四个条件分别读取样本。"),
    "calibration_invalid_samples": ("校准样本不满足要求，未启用新规则", "每个条件至少读取三次，并确认恢复状态回到正常基准且来自同一台设备。"),
}


class PrinterFault(RuntimeError):
    def __init__(self, code: str, message: str, action: str, *,
                 phase: str = "preflight", output_uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action
        self.phase = phase
        self.output_uncertain = output_uncertain

    def as_result(self) -> dict:
        result = {"ok": False, "status": "output_uncertain" if self.output_uncertain else "blocked",
                "error_code": self.code, "message": self.message, "action": self.action,
                "phase": self.phase, "data_sent": None if self.output_uncertain else False,
                "paper_used": None if self.output_uncertain else False,
                "physical_output_verified": False, "automatic_retry": False}
        if getattr(self, "cleanup_errors", None):
            result["cleanup_errors"] = self.cleanup_errors
        return result


def fault(code: str, *, phase: str = "preparation", output_uncertain: bool = False) -> PrinterFault:
    message, action = SOFTWARE_ERRORS[code]
    return PrinterFault(code, message, action, phase=phase, output_uncertain=output_uncertain)


def normalize_error(error: BaseException, *, phase: str = "preparation", output_uncertain: bool = False) -> PrinterFault:
    if isinstance(error, PrinterFault):
        return error
    code = "operation_failed"
    if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)):
        code = "operation_cancelled"
    elif isinstance(error, OSError):
        code = {errno.ENOSPC: "storage_full", errno.EACCES: "storage_permission_denied",
                errno.EPERM: "storage_permission_denied", errno.EROFS: "storage_read_only"}.get(error.errno, code)
    return fault(code, phase=phase, output_uncertain=output_uncertain)


def load_profile(state: Path, device_id: str | None) -> dict | None:
    path = state / "status-profile.json"
    if not device_id or not path.exists():
        return None
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
        expected = hashlib.sha256(device_id.encode()).hexdigest()
        if profile.get("schema") != 1 or profile.get("device_id_sha256") != expected:
            return None
        rules = profile.get("rules")
        if not isinstance(rules, list) or len(rules) > 16:
            return None
        for rule in rules:
            if not isinstance(rule, dict):
                return None
            if not all(isinstance(rule.get(key), int) and not isinstance(rule[key], bool)
                       for key in ("byte_index", "mask", "value")):
                return None
            if not (0 <= rule["byte_index"] < 32 and 1 <= rule["mask"] <= 255
                    and 0 <= rule["value"] <= 255 and rule["value"] & ~rule["mask"] == 0):
                return None
            if not all(isinstance(rule.get(key), str) and rule[key]
                       for key in ("code", "message", "action")):
                return None
        return profile
    except (OSError, ValueError, AttributeError):
        return None


def interpret_status(payload: bytes, profile: dict | None) -> dict:
    faults = []
    checks = []
    if profile:
        for rule in profile.get("rules", []):
            index = rule["byte_index"]
            if index >= len(payload):
                continue
            checks.append(rule["code"])
            if (payload[index] & rule["mask"]) == rule["value"]:
                faults.append({key: rule[key] for key in ("code", "message", "action")})
    return {"status_reply_received": bool(payload), "validated_checks": checks,
            "faults": faults,
            "summary": faults[0]["message"] if faults else (
                "设备未报告已验证的异常；仍不能证明实际出纸" if checks
                else "设备已回复；纸张和纸仓故障检测尚未验证"),
            "paper_movement_verified": False, "physical_output_verified": False}


def raise_for_faults(status: dict, *, phase: str, output_uncertain: bool) -> None:
    if status["faults"]:
        fault = status["faults"][0]
        raise PrinterFault(fault["code"], fault["message"], fault["action"],
                           phase=phase, output_uncertain=output_uncertain)
