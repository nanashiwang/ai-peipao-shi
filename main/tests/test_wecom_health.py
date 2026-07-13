from pathlib import Path

import pytest

from rpa.wecom_health import (
    WECOM_STATUS_AUTH_REQUIRED,
    detect_qr_auth_page_from_image,
    detect_wecom_unavailable_text,
    read_cached_unavailable_health,
    write_unavailable_health_cache,
)


def test_detects_wecom_safety_verification_text():
    text = "当前设备环境异常，需通过手机企业微信扫码进行安全验证\n未完成安全验证，将自动退出登录"

    reason = detect_wecom_unavailable_text(text)

    assert "安全验证" in reason


def test_regular_chat_text_does_not_trigger_auth_detection():
    text = "家长：今天登录课程平台失败，麻烦老师帮忙看一下。老师：收到。"

    assert detect_wecom_unavailable_text(text) == ""


def test_unavailable_health_cache_roundtrip(tmp_path: Path):
    write_unavailable_health_cache(tmp_path, WECOM_STATUS_AUTH_REQUIRED, "需要扫码")

    cached = read_cached_unavailable_health(tmp_path, ttl_seconds=60)

    assert cached is not None
    assert cached["status"] == WECOM_STATUS_AUTH_REQUIRED
    assert cached["detail"] == "需要扫码"


def test_detects_center_qr_auth_page(tmp_path: Path):
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")

    image = Image.new("RGB", (900, 640), "white")
    draw = ImageDraw.Draw(image)
    qr_left, qr_top, cell, grid = 365, 250, 3, 57
    for y in range(grid):
        for x in range(grid):
            finder = (x < 9 and y < 9) or (x > grid - 10 and y < 9) or (x < 9 and y > grid - 10)
            payload = (x * 7 + y * 11 + x * y) % 3 == 0
            if finder or payload:
                draw.rectangle(
                    [qr_left + x * cell, qr_top + y * cell, qr_left + (x + 1) * cell - 1, qr_top + (y + 1) * cell - 1],
                    fill="black",
                )
    path = tmp_path / "auth.png"
    image.save(path)

    assert "扫码" in detect_qr_auth_page_from_image(path)


def test_detects_right_side_qr_auth_page(tmp_path: Path):
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")

    image = Image.new("RGB", (1280, 720), "white")
    draw = ImageDraw.Draw(image)
    qr_left, qr_top, cell, grid = 850, 430, 4, 57
    for y in range(grid):
        for x in range(grid):
            finder = (x < 9 and y < 9) or (x > grid - 10 and y < 9) or (x < 9 and y > grid - 10)
            payload = (x * 7 + y * 11 + x * y) % 3 == 0
            if finder or payload:
                draw.rectangle(
                    [qr_left + x * cell, qr_top + y * cell, qr_left + (x + 1) * cell - 1, qr_top + (y + 1) * cell - 1],
                    fill="black",
                )
    path = tmp_path / "auth-right.png"
    image.save(path)

    assert "扫码" in detect_qr_auth_page_from_image(path)
