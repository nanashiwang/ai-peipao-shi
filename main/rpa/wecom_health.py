"""企微 RPA 健康状态识别的纯函数工具。

该模块不依赖 pywinauto/win32，便于单元测试，也便于 RPA 主脚本复用。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

WECOM_STATUS_OK = "Y"
WECOM_STATUS_NOT_FOUND = "N"
WECOM_STATUS_AUTH_REQUIRED = "AUTH_REQ"
WECOM_STATUS_ERROR = "ERROR"

DEFAULT_AUTH_REQUIRED_DETAIL = "企业微信需要手机扫码安全验证或重新登录，RPA 已暂停真实发送。"
QR_AUTH_CANDIDATE_BOXES = (
    (0.42, 0.38, 0.58, 0.64),
    (0.64, 0.50, 0.86, 0.96),
)


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def detect_wecom_unavailable_text(text: str) -> str:
    """根据 UIA/OCR 文本判断企微是否停在登录或安全验证页。"""
    clean = compact_text(text)
    if not clean:
        return ""
    if "当前设备环境异常" in clean and ("安全验证" in clean or "企业微信扫码" in clean):
        return "企业微信当前设备环境异常，需要手机企业微信扫码进行安全验证。"
    if "未完成安全验证" in clean:
        return "企业微信安全验证未完成，需要手机扫码后才能继续发送。"
    if "手机企业微信扫码" in clean and "安全验证" in clean:
        return "企业微信需要手机企业微信扫码安全验证。"
    if "退出登录" in clean and "安全验证" in clean:
        return "企业微信停在安全验证页，需要手机扫码或重新登录。"
    qr_or_scan = "二维码" in clean or "扫码" in clean
    login_or_verify = any(token in clean for token in ("登录企业微信", "重新登录", "安全验证", "设备环境异常"))
    if qr_or_scan and login_or_verify:
        return DEFAULT_AUTH_REQUIRED_DETAIL
    return ""


def _crop_ratio(image, box: tuple[float, float, float, float]):
    width, height = image.size
    left = max(0, min(width, int(width * box[0])))
    top = max(0, min(height, int(height * box[1])))
    right = max(0, min(width, int(width * box[2])))
    bottom = max(0, min(height, int(height * box[3])))
    if right <= left or bottom <= top:
        return image
    return image.crop((left, top, right, bottom))


def _binary_transition_ratio(mask: list[bool], width: int, height: int) -> float:
    if width <= 1 or height <= 1:
        return 0.0
    changes = 0
    comparisons = 0
    for y in range(height):
        row = y * width
        for x in range(width - 1):
            comparisons += 1
            if mask[row + x] != mask[row + x + 1]:
                changes += 1
    for y in range(height - 1):
        row = y * width
        next_row = (y + 1) * width
        for x in range(width):
            comparisons += 1
            if mask[row + x] != mask[next_row + x]:
                changes += 1
    return changes / comparisons if comparisons else 0.0


def qr_auth_page_metrics(
    image,
    qr_box: tuple[float, float, float, float] = QR_AUTH_CANDIDATE_BOXES[0],
) -> dict[str, float]:
    """计算“亮背景 + 候选区域二维码”页面的轻量指标。"""
    gray = image.convert("L")
    page = _crop_ratio(gray, (0.18, 0.08, 0.82, 0.88)).resize((240, 180))
    page_pixels = list(page.getdata())
    page_bright_ratio = sum(1 for value in page_pixels if value >= 225) / max(len(page_pixels), 1)

    qr = _crop_ratio(gray, qr_box).resize((160, 160))
    qr_pixels = list(qr.getdata())
    dark_mask = [value <= 95 for value in qr_pixels]
    qr_dark_ratio = sum(1 for item in dark_mask if item) / max(len(dark_mask), 1)
    qr_light_ratio = sum(1 for value in qr_pixels if value >= 205) / max(len(qr_pixels), 1)
    qr_transition_ratio = _binary_transition_ratio(dark_mask, 160, 160)
    return {
        "page_bright_ratio": page_bright_ratio,
        "qr_dark_ratio": qr_dark_ratio,
        "qr_light_ratio": qr_light_ratio,
        "qr_transition_ratio": qr_transition_ratio,
    }


def detect_qr_auth_page_from_pil(image) -> str:
    for qr_box in QR_AUTH_CANDIDATE_BOXES:
        metrics = qr_auth_page_metrics(image, qr_box)
        if (
            metrics["page_bright_ratio"] >= 0.70
            and 0.065 <= metrics["qr_dark_ratio"] <= 0.55
            and metrics["qr_light_ratio"] >= 0.35
            and metrics["qr_transition_ratio"] >= 0.085
        ):
            return DEFAULT_AUTH_REQUIRED_DETAIL
    return ""


def detect_qr_auth_page_from_image(image_path: str | Path, crop_box: tuple[int, int, int, int] | None = None) -> str:
    try:
        from PIL import Image
    except ModuleNotFoundError:
        return ""
    try:
        with Image.open(image_path) as image:
            target = image.convert("RGB")
            if crop_box:
                target = target.crop(crop_box)
            return detect_qr_auth_page_from_pil(target)
    except Exception:
        return ""


def health_cache_path(root: str | Path, filename: str = ".wecom_health_cache.json") -> Path:
    return Path(root) / filename


def read_cached_unavailable_health(root: str | Path, ttl_seconds: int, filename: str = ".wecom_health_cache.json") -> dict[str, Any] | None:
    if ttl_seconds <= 0:
        return None
    path = health_cache_path(root, filename)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    status = str(data.get("status") or "").strip()
    detail = str(data.get("detail") or "").strip()
    timestamp = float(data.get("ts") or 0)
    if not status or status == WECOM_STATUS_OK or not detail:
        return None
    if time.time() - timestamp > ttl_seconds:
        return None
    return {"status": status[:10], "detail": detail, "ts": timestamp}


def write_unavailable_health_cache(
    root: str | Path,
    status: str,
    detail: str,
    filename: str = ".wecom_health_cache.json",
) -> None:
    path = health_cache_path(root, filename)
    data = {"status": (status or WECOM_STATUS_ERROR)[:10], "detail": detail or "", "ts": time.time()}
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def clear_health_cache(root: str | Path, filename: str = ".wecom_health_cache.json") -> None:
    try:
        health_cache_path(root, filename).unlink(missing_ok=True)
    except Exception:
        pass
