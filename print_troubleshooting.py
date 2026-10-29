"""Advice for a reported symptom; never a sensor diagnosis or a reprint trigger."""
from __future__ import annotations

import io
from PIL import Image, UnidentifiedImageError

from printer_faults import fault


def blank_output_advice(png: bytes) -> dict:
    try:
        with Image.open(io.BytesIO(png)) as image:
            histogram = image.convert("L").histogram()
    except (OSError, ValueError, UnidentifiedImageError):
        raise fault("job_corrupt") from None
    contains_marks = sum(histogram[:160]) > 0
    checks = [
        {"code": "paper_orientation", "action": "先检查纸卷方向，让热敏显色面贴着打印头；按本机装纸说明重新装好。"},
        {"code": "paper_pressure", "action": "确认纸已压好、纸仓盖已完全扣紧；有纸和状态正常不能证明纸已压紧。"},
        {"code": "thermal_paper", "action": "确认使用适配的热敏纸；可在取下的纸片两面用指甲划一下，出现灰黑痕的一面可帮助辨认显色面。"},
        {"code": "device_self_test", "action": "若仍是白纸，按设备说明做自检对照；自检也空白时先排查纸张和设备，自检正常再检查程序与协议。"},
    ]
    if not contains_marks:
        checks.insert(0, {"code": "blank_preview", "action": "保存的打印预览本身没有黑色内容；先重新准备并查看预览。"})
    return {
        "symptom": "blank_output", "source": "client_report", "sensor_confirmed": False,
        "message": "根据“走纸但空白”的反馈提供排查建议，尚未确定具体原因。",
        "preview_contains_marks": contains_marks,
        "preview_note": "预览含黑色内容；这不能证明设备已显色或所有发送指令均正常。" if contains_marks
                        else "保存的预览没有黑色内容。",
        "checks": checks,
        "next_step": "检查并调整后，如需另一张，请重新准备任务并点击本机确认按钮。",
        "additional_print_sent": False, "automatic_retry": False,
        "physical_output_verified": False,
    }
