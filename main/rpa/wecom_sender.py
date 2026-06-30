"""企业微信 PC 端 RPA 发送器。

这个脚本负责三件事：
1. 连接本地后端健康检查接口，确认服务可用。
2. 通过 pywinauto 操作企业微信窗口，发送待发送任务。
3. 识别未读会话并把可见聊天内容同步回后端。
"""

import argparse
import base64
import ctypes
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

try:
    from rpa.send_guard import (
        SendGuardError,
        add_send_trace,
        config_for_send_mode,
        conversation_title_mismatch_detail,
        detail_with_send_trace,
        dry_run_result_detail,
        real_send_block_detail,
        real_send_enabled,
        real_send_requested,
        search_result_not_found_detail,
        sent_content_confirmed,
        should_press_send_hotkey,
        target_in_allowed_conversations,
        target_not_allowed_detail,
        validate_active_conversation_title,
        validate_foreground_wecom,
        text_matches_target,
        validate_visual_hit,
    )
except ModuleNotFoundError:
    from send_guard import (
        SendGuardError,
        add_send_trace,
        config_for_send_mode,
        conversation_title_mismatch_detail,
        detail_with_send_trace,
        dry_run_result_detail,
        real_send_block_detail,
        real_send_enabled,
        real_send_requested,
        search_result_not_found_detail,
        sent_content_confirmed,
        should_press_send_hotkey,
        target_in_allowed_conversations,
        target_not_allowed_detail,
        validate_active_conversation_title,
        validate_foreground_wecom,
        text_matches_target,
        validate_visual_hit,
    )

try:
    import pyperclip
    import win32api
    import win32con
    import win32gui
    import win32process
    from pywinauto import Desktop, keyboard, mouse
except ModuleNotFoundError as exc:
    missing = exc.name or "pywinauto/pyperclip"
    print(
        "\nRPA 依赖没有安装在当前 Python 环境里。\n"
        f"缺少模块：{missing}\n\n"
        "你现在大概率用了系统 Python 运行脚本。请在项目目录用下面任一方式运行：\n\n"
        "  .\\.venv\\Scripts\\Activate.ps1\n"
        "  python rpa\\wecom_sender.py --diagnose\n\n"
        "或不激活虚拟环境，直接指定项目 Python：\n\n"
        "  .\\.venv\\Scripts\\python.exe rpa\\wecom_sender.py --diagnose\n\n"
        "如果 .venv 里也缺依赖，执行：\n\n"
        "  .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt\n",
        file=sys.stderr,
    )
    sys.exit(1)


# Pillow 与 PaddleOCR 是“整屏截图 + 本地 OCR 定位”新链路的可选依赖；
# 没装也不阻断旧的 UIA/剪贴板链路加载，真正用到时再抛带安装提示的错误。
try:
    from PIL import Image, ImageGrab
except ModuleNotFoundError:
    Image = None
    ImageGrab = None


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DEFAULT_CONFIG = ROOT / "config.json"
FALLBACK_CONFIG = ROOT / "config.example.json"

# 企业微信 5.x 的聊天区可能由 FlutterPlugins.exe 承载，前台句柄经常不是 WXWork.exe。
# 这里默认允许该子进程通过安全检查，但不放开 WeMail.exe 等文档/邮件子程序。
DEFAULT_WECOM_PROCESS_NAMES = ["WXWork.exe", "WXWorkWeb.exe", "FlutterPlugins.exe"]


# 统一的 RPA 异常类型，便于上层捕获并打印友好提示。
class RpaError(RuntimeError):
    pass


# PaddleOCR 引擎单例：首次实例化会加载本地模型，开销较大，全程复用一个实例。
_OCR_ENGINE = None


def get_ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from paddleocr import PaddleOCR
        except ModuleNotFoundError as exc:
            raise RpaError(
                "本地 OCR 依赖未安装。请在 RPA 虚拟环境执行：\n"
                "  .\\.venv\\Scripts\\python.exe -m pip install paddlepaddle paddleocr\n"
            ) from exc
        _OCR_ENGINE = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    return _OCR_ENGINE


# 这些词是企微界面里的通用导航/功能词，不能当作真实会话名。
CHAT_TEXT_BLACKLIST = {
    "企业微信",
    "通讯录",
    "工作台",
    "会议",
    "邮件",
    "文档",
    "搜索",
    "发送",
    "表情",
    "截图",
    "文件",
    "图片",
    "聊天信息",
}


# 这些模式用于识别“未读”红点、未读条数等 UI 文本。
UNREAD_PATTERNS = [
    re.compile(r"^\d+$"),
    re.compile(r"\d+\s*条"),
    re.compile(r"未读|新消息|条新消息"),
]


def import_ark_vision():
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from app.services.ark_client import call_ark_vision_json

    return call_ark_vision_json


# 读取运行配置；如果当前路径不存在，就回退到示例配置。
def load_config(path: Path) -> dict:
    if not path.exists():
        path = FALLBACK_CONFIG
    return json.loads(path.read_text(encoding="utf-8"))


def save_config(config: dict, path: Path):
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


# 强制直连后端，忽略被控端机器上的系统/环境代理（V2Ray/Clash 等）。
# 否则 urllib 默认会跟随系统代理，代理转发不到服务器时会返回 503，导致心跳/领取全部失败。
_DIRECT_OPENER = build_opener(ProxyHandler({}))


# 对后端接口发起 JSON 请求，是 RPA 和后台的数据桥梁。
def request_json(base_url: str, path: str, method: str = "GET", payload: dict | None = None, extra_headers: dict | None = None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if extra_headers:
        headers.update(extra_headers)
    req = Request(f"{base_url.rstrip('/')}{path}", data=data, headers=headers, method=method)
    try:
        with _DIRECT_OPENER.open(req, timeout=10) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RpaError(f"API HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RpaError(f"API 连接失败：{exc}") from exc
    return json.loads(body) if body else {}


def should_upload_send_screenshot(config: dict, status: str) -> bool:
    if not config.get("upload_send_screenshot", True):
        return False
    statuses = config.get("upload_send_screenshot_statuses", ["sent", "failed", "skipped", "dry_run"])
    return status in set(statuses)


def screenshot_upload_payload(path: Path, config: dict) -> str:
    max_bytes = int(config.get("send_screenshot_max_upload_bytes", 5 * 1024 * 1024))
    data = path.read_bytes()
    if len(data) <= max_bytes:
        return base64.b64encode(data).decode("ascii")
    if Image is None:
        print(f"screenshot_upload_skipped detail=file_too_large size={len(data)}")
        return ""
    with Image.open(path) as img:
        max_side = int(config.get("send_screenshot_max_side", 1600))
        img.thumbnail((max_side, max_side))
        if img.mode not in {"RGB", "L"}:
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=int(config.get("send_screenshot_jpeg_quality", 70)), optimize=True)
    data = buf.getvalue()
    if len(data) > max_bytes:
        print(f"screenshot_upload_skipped detail=resized_file_too_large size={len(data)}")
        return ""
    return base64.b64encode(data).decode("ascii")


def capture_send_screenshot(config: dict, task_id: int, status: str) -> str:
    if not should_upload_send_screenshot(config, status):
        return ""
    try:
        path = capture_fullscreen_image(f"result_{status}_{task_id}", config)
        return screenshot_upload_payload(path, config)
    except Exception as exc:
        print(f"screenshot_upload_failed detail={exc}")
        return ""


# 健康检查后端服务是否可访问。
def check_api(config: dict) -> dict:
    return request_json(config["api_base_url"], "/health")


# 校验脚本依赖的配置字段，并提示潜在的 dry_run 风险。
def validate_config(config: dict):
    required = ["api_base_url", "window_title_keywords"]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise RpaError(f"配置缺失：{', '.join(missing)}")
    if config.get("auto_send_ai_replies", False) and config.get("dry_run", True):
        print("WARN: auto_send_ai_replies=true 但 dry_run=true，本轮只会粘贴不真实发送。")
    if not config.get("dry_run", True) and not real_send_enabled(config):
        print("WARN: dry_run=false 但控制端/本机未开启真实发送开关，本轮会阻止真实发送。")


def launch_wecom(config: dict):
    if not config.get("auto_launch_wecom", True):
        return
    paths = config.get("wecom_executable_paths", [])
    for raw in paths:
        candidate = Path(os.path.expandvars(raw)).expanduser()
        if candidate.exists():
            subprocess.Popen([str(candidate)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(float(config.get("wecom_launch_wait_seconds", 6)))
            return
    raise RpaError("没有找到企业微信窗口，也没有可用的 wecom_executable_paths。请先手动打开企业微信，或在 rpa/config.json 配置企业微信路径。")


# 截图只在需要排障时保存，避免无意义地占满磁盘。
def capture_debug_image(window, config: dict, reason: str):
    return ""


def capture_fullscreen_image(reason: str = "scan", config: dict | None = None) -> Path:
    """整屏截图。新版企微是自绘渲染，pywinauto 的 capture_as_image（PrintWindow）会黑屏，
    所以改用 PowerShell 的 CopyFromScreen 截整个虚拟屏；失败再回退到 PIL ImageGrab。"""
    safe_reason = re.sub(r"[^0-9A-Za-z一-鿿_-]+", "_", reason)[:24] or "scan"
    path = ROOT / f"debug_wecom_{safe_reason}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
        "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
        "$g=[System.Drawing.Graphics]::FromImage($bmp); "
        "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size); "
        f"$bmp.Save('{path.as_posix()}',[System.Drawing.Imaging.ImageFormat]::Png); "
        "$g.Dispose(); $bmp.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=True, timeout=20,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if path.exists():
            return path
    except Exception as exc:
        print(f"powershell_capture_failed detail={exc}")
    if ImageGrab is None:
        raise RpaError("整屏截图失败，且未安装 Pillow 作后备。请先安装 OCR 依赖。")
    ImageGrab.grab(all_screens=True).save(path)
    return path


def screenshot_wecom(window, config: dict, reason: str = "scan"):
    """把企微提到最前并最大化再整屏截图。返回 (截图路径, 截图时窗口物理矩形 dict)。
    这是所有 OCR 定位的统一入口：activate 把企微带到最前 + maximize 占满屏，
    避免被其它窗口（如终端）遮挡导致 OCR 截到无关文字，且让会话列表布局稳定。"""
    activate(window, config)
    ensure_foreground_wecom(window, config)
    if config.get("maximize_before_shot", True):
        try:
            window.maximize()
        except Exception as exc:
            print(f"maximize_failed detail={exc}")
    time.sleep(float(config.get("screenshot_settle_seconds", 0.4)))
    rect = window.rectangle()
    path = capture_fullscreen_image(reason, config)
    return path, {"left": rect.left, "top": rect.top, "width": rect.width(), "height": rect.height()}


# 兼容旧调用点（detect_unread_badges_from_screenshot）：返回整屏截图路径。
def capture_window_image(window, config: dict, reason: str = "scan") -> Path:
    return capture_fullscreen_image(reason, config)


# 旧的 PrintWindow 截图实现已废弃（对 Flutter 自绘窗口会黑屏），保留函数体但不再被调用。
def _legacy_capture_window_image(window, config: dict, reason: str = "scan") -> Path:
    raise RpaError("截图方案已停用：当前 RPA 只使用 UIA/剪贴板链路。")
    safe_reason = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", reason)[:24] or "scan"
    path = ROOT / f"debug_wecom_{safe_reason}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    image = window.capture_as_image()
    image.save(path)
    return path
    safe_reason = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", reason)[:24] or "debug"
    path = ROOT / f"debug_wecom_{safe_reason}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    try:
        image = window.capture_as_image()
        image.save(path)
        return str(path)
    except Exception:
        return ""


# 把常用按键映射到 pywinauto 的发送语法。
def hotkey(keys: list[str]):
    modifiers = {"ctrl": "^", "control": "^", "alt": "%", "shift": "+"}
    special = {"enter": "{ENTER}", "tab": "{TAB}", "esc": "{ESC}", "escape": "{ESC}"}
    prefix = ""
    main = ""
    for raw in keys:
        key = raw.lower()
        if key in modifiers:
            prefix += modifiers[key]
        elif key in special:
            main = special[key]
        elif len(key) == 1:
            main = key
        else:
            main = f"{{{key.upper()}}}"
    keyboard.send_keys(prefix + main)


# 根据窗口标题关键词，找到企微主窗口。
def find_wecom_window(config: dict):
    desktop = Desktop(backend="uia")
    windows = find_wecom_windows(desktop, config)
    if not windows:
        launch_wecom(config)
        desktop = Desktop(backend="uia")
        windows = find_wecom_windows(desktop, config)
    if not windows:
        raise RpaError("没有找到企业微信窗口，请确认企微 PC 端已登录且窗口没有最小化。")
    return windows[0]


def find_wecom_windows(desktop, config: dict):
    windows = []
    for keyword in config["window_title_keywords"]:
        windows.extend(desktop.windows(title_re=f".*{keyword}.*", visible_only=True))
    # 企业微信 5.x 可能把真正可操作的主窗口暴露成“无标题但可见”的 WXWork.exe 顶层窗口。
    # 仅按标题找会漏掉它，所以再补扫一次所有可见顶层窗口，按进程和窗口面积过滤。
    try:
        windows.extend(desktop.windows(visible_only=True))
    except Exception:
        pass
    unique = []
    seen = set()
    for win in windows:
        handle = win.handle
        if handle not in seen:
            if is_wecom_process(handle, config):
                try:
                    rect = win.rectangle()
                    area = rect.width() * rect.height()
                except Exception:
                    area = 0
                title = control_text(win)
                min_area = int(config.get("wecom_min_window_area", 120000))
                if title or area >= min_area:
                    unique.append(win)
            seen.add(handle)
    if not unique:
        min_area = int(config.get("wecom_min_window_area", 120000))
        candidates = []
        def collect(hwnd, _):
            if hwnd in seen or not is_wecom_process(hwnd, config):
                return
            title = win32gui.GetWindowText(hwnd).strip()
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                area = max(0, right - left) * max(0, bottom - top)
            except Exception:
                area = 0
            title_matched = any(keyword in title for keyword in config.get("window_title_keywords", []))
            if title_matched and area >= min_area:
                candidates.append((area, hwnd))
                seen.add(hwnd)
        try:
            win32gui.EnumWindows(collect, None)
        except Exception:
            candidates = []
        for _, hwnd in sorted(candidates, reverse=True):
            try:
                unique.append(desktop.window(handle=hwnd))
            except Exception:
                pass
    def score_window(win):
        path = window_process_path(win.handle)
        process_name = Path(path).name.lower()
        try:
            rect = win.rectangle()
            area = rect.width() * rect.height()
        except Exception:
            area = 0
        title = control_text(win)
        # 优先选 WXWork.exe 的最大主窗口，避免选到 Flutter/文档等子窗口。
        return (
            1 if process_name == "wxwork.exe" else 0,
            1 if title == "企业微信" else 0,
            area,
        )
    return sorted(unique, key=score_window, reverse=True)


def window_process_path(handle: int) -> str:
    try:
        _, pid = win32process.GetWindowThreadProcessId(handle)
        process = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
        try:
            return win32process.GetModuleFileNameEx(process, 0)
        finally:
            win32api.CloseHandle(process)
    except Exception:
        return ""


def is_wecom_process(handle: int, config: dict) -> bool:
    allowed = {name.lower() for name in config.get("wecom_process_names", DEFAULT_WECOM_PROCESS_NAMES)}
    path = window_process_path(handle)
    if not path:
        return False
    return Path(path).name.lower() in allowed


# 把窗口内可见文本尽量展开，用于诊断和会话校验。
def visible_text(window) -> str:
    try:
        return window.window_text() + "\n" + "\n".join(child.window_text() for child in window.descendants()[:300])
    except Exception:
        return window.window_text()


    # 容错式遍历子控件，避免 UIA 读取异常中断主流程。
def safe_descendants(window, limit: int = 800):
    try:
        return window.descendants()[:limit]
    except Exception:
        return []


    # 封装控件文本读取，统一吞掉偶发的 UIA 异常。
def control_text(control) -> str:
    try:
        return control.window_text().strip()
    except Exception:
        return ""


    # 封装控件矩形读取，供点击定位和区域筛选使用。
def control_rect(control):
    try:
        return control.rectangle()
    except Exception:
        return None


    # 判断一段文本是否像未读提示，而不是普通消息内容。
def is_unread_text(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    return any(pattern.search(cleaned) for pattern in UNREAD_PATTERNS)


# 收集需要监听/同步的会话名称，保证去重后再使用。
def watched_conversations(config: dict) -> list[str]:
    names = []
    names.extend(config.get("watch_conversations", []))
    names.extend(config.get("allowed_conversations", []))
    names.extend((config.get("conversation_family_map") or {}).keys())
    result = []
    for name in names:
        if name and name not in result:
            result.append(name)
    return result


def ignored_conversations(config: dict) -> set[str]:
    return {name.strip() for name in config.get("ignored_conversations", []) if name and name.strip()}


def add_ignored_conversation(config: dict, config_path: Path, name: str):
    ignored = config.setdefault("ignored_conversations", [])
    if name not in ignored:
        ignored.append(name)
        save_config(config, config_path)
        print(f"已加入忽略会话：{name}")


# 激活窗口并给前台切换留一点时间。
def activate(window, config: dict | None = None):
    try:
        console = ctypes.windll.kernel32.GetConsoleWindow()
        if console and int(console) != int(window.handle):
            ctypes.windll.user32.ShowWindow(console, win32con.SW_MINIMIZE)
            time.sleep(0.2)
    except Exception:
        pass
    try:
        window.restore()
    except Exception:
        pass
    try:
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass
        current_thread = win32api.GetCurrentThreadId()
        foreground = foreground_handle()
        foreground_thread = win32process.GetWindowThreadProcessId(foreground)[0] if foreground else 0
        target_thread = win32process.GetWindowThreadProcessId(int(window.handle))[0]
        if foreground_thread:
            win32process.AttachThreadInput(current_thread, foreground_thread, True)
        win32process.AttachThreadInput(current_thread, target_thread, True)
        win32gui.ShowWindow(int(window.handle), win32con.SW_RESTORE)
        win32gui.SetWindowPos(int(window.handle), win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        win32gui.SetWindowPos(int(window.handle), win32con.HWND_NOTOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        try:
            ctypes.windll.user32.SwitchToThisWindow(int(window.handle), True)
        except Exception:
            pass
        win32gui.SetForegroundWindow(int(window.handle))
    except Exception:
        pass
    finally:
        try:
            current_thread = win32api.GetCurrentThreadId()
            foreground = foreground_handle()
            foreground_thread = win32process.GetWindowThreadProcessId(foreground)[0] if foreground else 0
            target_thread = win32process.GetWindowThreadProcessId(int(window.handle))[0]
            if foreground_thread:
                win32process.AttachThreadInput(current_thread, foreground_thread, False)
            win32process.AttachThreadInput(current_thread, target_thread, False)
        except Exception:
            pass
    try:
        window.set_focus()
    except Exception:
        pass
    if foreground_handle() != int(window.handle):
        try:
            rect = window.rectangle()
            mouse.click(button="left", coords=(int(rect.left + min(120, rect.width() / 2)), int(rect.top + 16)))
        except Exception:
            pass
    if foreground_handle() != int(window.handle) and not is_wecom_process(foreground_handle(), config or {}):
        focus_wecom_from_taskbar()
    time.sleep(0.8)
    ensure_foreground_wecom(window, config)


def focus_wecom_from_taskbar():
    try:
        desktop = Desktop(backend="uia")
        for control in desktop.descendants():
            text = control_text(control)
            if "企业微信" not in text and "WXWork" not in text and "WeCom" not in text:
                continue
            rect = control_rect(control)
            if not rect:
                continue
            if rect.bottom < 1000:
                continue
            control.click_input()
            time.sleep(0.8)
            return
    except Exception:
        return


def bring_wecom_to_front(window):
    """把已确认的企微窗口重新提到前台，但不 restore/maximize，避免破坏 OCR 截图后的窗口尺寸。"""
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass
    try:
        hwnd = int(window.handle)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    try:
        window.set_focus()
    except Exception:
        pass
    time.sleep(0.4)


def foreground_handle() -> int:
    try:
        return int(win32gui.GetForegroundWindow())
    except Exception:
        return 0


def ensure_foreground_wecom(window, config: dict | None = None):
    effective_config = config or {}
    target_handle = int(window.handle)
    handle = foreground_handle()
    try:
        validate_foreground_wecom(
            handle,
            target_handle,
            is_wecom_process(handle, effective_config) if handle else False,
            win32gui.GetWindowText(handle) if handle else "",
        )
        return
    except SendGuardError as exc:
        if not handle:
            raise RpaError(str(exc)) from exc
    if config and config.get("recover_foreground_wecom", True):
        bring_wecom_to_front(window)
        handle = foreground_handle()
        try:
            validate_foreground_wecom(
                handle,
                target_handle,
                is_wecom_process(handle, effective_config) if handle else False,
                win32gui.GetWindowText(handle) if handle else "",
            )
            return
        except SendGuardError as exc:
            raise RpaError(str(exc)) from exc
    title = win32gui.GetWindowText(handle) if handle else ""
    try:
        validate_foreground_wecom(handle, target_handle, False, title)
    except SendGuardError as exc:
        raise RpaError(str(exc)) from exc


# 在搜索结果区域里挑选最像目标会话的控件。
def click_matching_search_result(window, conversation: str) -> bool:
    ensure_foreground_wecom(window, config=None)
    window_rect = window.rectangle()
    candidates = []
    try:
        controls = window.descendants()[:500]
    except Exception:
        return False

    for control in controls:
        try:
            text = control.window_text().strip()
            rect = control.rectangle()
        except Exception:
            continue
        if conversation not in text:
            continue
        if rect.width() < 20 or rect.height() < 10:
            continue
        if rect.left < window_rect.left or rect.top < window_rect.top:
            continue
        if rect.right > window_rect.right or rect.bottom > window_rect.bottom:
            continue

        center_x = rect.left + rect.width() / 2
        center_y = rect.top + rect.height() / 2
        is_left_pane = center_x < window_rect.left + window_rect.width() * 0.55
        is_below_search_bar = center_y > window_rect.top + window_rect.height() * 0.08
        score = 0
        if is_left_pane:
            score += 2
        if is_below_search_bar:
            score += 2
        score -= abs(center_y - (window_rect.top + window_rect.height() * 0.22)) / 1000
        candidates.append((score, control, rect))

    if not candidates:
        return False

    _, control, rect = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
    try:
        control.click_input()
    except Exception:
        mouse.click(
            button="left",
            coords=(int(rect.left + rect.width() / 2), int(rect.top + rect.height() / 2)),
        )
    return True


# 打开搜索结果：只允许 UIA 精确命中；失败即中止，避免 Enter/坐标打开错误会话。
def open_search_result(window, conversation: str, config: dict):
    ensure_foreground_wecom(window, config)
    if click_matching_search_result(window, conversation):
        time.sleep(float(config.get("open_conversation_wait_seconds", 1.0)))
        ensure_foreground_wecom(window, config)
        keyboard.send_keys("{ESC}")
        time.sleep(0.2)
        return

    raise RpaError(search_result_not_found_detail(conversation, "搜索结果"))


# ============ 视觉定位（整屏截图 + 本地 OCR，ARK Vision 兜底）============
# 新版企业微信是 Flutter/CEF 自绘渲染，UIA 读不到控件，Ctrl+F 也不触发搜索框。
# 改用“整屏截图 -> 本地 OCR 识别会话名/标题 -> 相对窗口比例点击”的视觉方案。


def get_screen_scale(image_width: int) -> float:
    """截图(逻辑像素) 与 窗口物理像素 之间的缩放比 = 物理屏宽 / 截图宽。
    DESKTOPHORZRES(118) 返回真实物理分辨率，不受进程 DPI 感知影响。单主屏假设。"""
    try:
        if image_width:
            hdc = ctypes.windll.user32.GetDC(0)
            phys_w = ctypes.windll.gdi32.GetDeviceCaps(hdc, 118)
            ctypes.windll.user32.ReleaseDC(0, hdc)
            if phys_w:
                return float(phys_w) / float(image_width)
    except Exception:
        pass
    return 1.0


def _cleanup_debug_image(path, config: dict):
    if config.get("keep_debug_screenshots", False):
        return
    try:
        Path(path).unlink()
    except Exception:
        pass


def click_window_ratio(window, rx: float, ry: float, config: dict):
    """按相对企微窗口的比例点击。rx/ry∈[0,1]，用 window.rectangle()（物理像素）换算，
    自动抵消屏幕 DPI 缩放。点击前只 ensure_foreground，不再 activate（activate 会 restore 改尺寸）。"""
    ensure_foreground_wecom(window, config)
    rect = window.rectangle()
    x = int(rect.left + rx * rect.width())
    y = int(rect.top + ry * rect.height())
    mouse.click(button="left", coords=(x, y))
    return x, y


def click_visual_region_hit(
    window,
    hit: dict,
    win_rect_img: dict,
    config: dict,
    *,
    x_ratio_key: str,
    fallback_x_ratio_key: str,
    default_x_ratio: float,
    y_offset_key: str,
    target: str = "",
    stage: str = "视觉定位",
):
    """点击 OCR/视觉命中的列表行。

    OCR 坐标来自整屏截图；在高 DPI 缩放下，pywinauto 鼠标坐标与截图坐标一致，
    直接用 window.rectangle() 的物理尺寸会把点击点下移，容易点到下一行。
    """
    try:
        validate_visual_hit(target, hit, stage)
    except SendGuardError as exc:
        raise RpaError(str(exc)) from exc
    raw_rx = float(config.get(x_ratio_key, config.get(fallback_x_ratio_key, default_x_ratio)))
    raw_ry = float(hit["ry"]) + float(config.get(y_offset_key, 0.0))
    rx = max(0.0, min(1.0, raw_rx))
    ry = max(0.0, min(1.0, raw_ry))
    if config.get("visual_click_use_screenshot_coords", True) and hit.get("image_width"):
        scale = get_screen_scale(int(hit["image_width"]))
        left = win_rect_img["left"] / scale
        top = win_rect_img["top"] / scale
        width = win_rect_img["width"] / scale
        height = win_rect_img["height"] / scale
        x = int(left + rx * width)
        y = int(top + ry * height)
        ensure_foreground_wecom(window, config)
        mouse.click(button="left", coords=(x, y))
        print(
            f"visual_click rx={rx:.3f} ry={ry:.3f} x={x} y={y} "
            f"scale={scale:.3f} coordinate=screenshot"
        )
        return x, y
    x, y = click_window_ratio(window, rx, ry, config)
    print(f"visual_click rx={rx:.3f} ry={ry:.3f} x={x} y={y} coordinate=window")
    return x, y


def click_conversation_hit(window, hit: dict, win_rect_img: dict, config: dict, target: str = ""):
    return click_visual_region_hit(
        window,
        hit,
        win_rect_img,
        config,
        x_ratio_key="conversation_open_click_ratio_x",
        fallback_x_ratio_key="conversation_row_click_ratio_x",
        default_x_ratio=0.18,
        y_offset_key="conversation_open_click_offset_ratio_y",
        target=target,
        stage="会话列表",
    )


def click_search_result_hit(window, hit: dict, win_rect_img: dict, config: dict, target: str = ""):
    return click_visual_region_hit(
        window,
        hit,
        win_rect_img,
        config,
        x_ratio_key="search_result_open_click_ratio_x",
        fallback_x_ratio_key="search_result_click_ratio_x",
        default_x_ratio=0.20,
        y_offset_key="search_result_open_click_offset_ratio_y",
        target=target,
        stage="搜索结果",
    )


def ocr_region(image_path, region_ratio, win_rect_img: dict, config: dict) -> list[dict]:
    """对整屏截图中“企微窗口的某个比例区域”做 OCR。
    region_ratio=[x0,y0,x1,y1] 相对窗口；win_rect_img 是截图时窗口的物理矩形。
    返回 [{text, score, rx, ry}]，rx/ry 是命中文字中心相对窗口的比例（可直接喂 click_window_ratio）。"""
    if Image is None:
        raise RpaError("未安装 Pillow，无法读取截图做 OCR。请先安装 OCR 依赖。")
    import numpy as np

    image = Image.open(image_path).convert("RGB")
    img_w, img_h = image.size
    scale = get_screen_scale(img_w)
    # 窗口物理矩形换算到截图(逻辑像素)坐标系
    ll = win_rect_img["left"] / scale
    tt = win_rect_img["top"] / scale
    ww = win_rect_img["width"] / scale
    hh = win_rect_img["height"] / scale
    x0 = int(max(0, min(ll + region_ratio[0] * ww, img_w)))
    y0 = int(max(0, min(tt + region_ratio[1] * hh, img_h)))
    x1 = int(max(0, min(ll + region_ratio[2] * ww, img_w)))
    y1 = int(max(0, min(tt + region_ratio[3] * hh, img_h)))
    if x1 <= x0 or y1 <= y0:
        return []
    crop = image.crop((x0, y0, x1, y1))
    raw = get_ocr_engine().ocr(np.array(crop), cls=True)
    lines = raw[0] if raw and raw[0] else []
    min_score = float(config.get("ocr_min_score", 0.5))
    items = []
    for entry in lines:
        try:
            box, (text, score) = entry
        except Exception:
            continue
        if float(score) < min_score:
            continue
        cx = x0 + sum(p[0] for p in box) / len(box)
        cy = y0 + sum(p[1] for p in box) / len(box)
        items.append({
            "text": str(text).strip(),
            "score": float(score),
            "rx": (cx - ll) / ww,
            "ry": (cy - tt) / hh,
            "image_width": img_w,
            "image_height": img_h,
        })
    return items


def find_text_in_ocr(items: list[dict], target: str, config: dict) -> dict | None:
    """在 OCR 结果里找目标会话名：精确包含优先，否则按相似度取最相似且达阈值的。"""
    import difflib

    target = (target or "").strip()
    if not target:
        return None
    exact = [it for it in items if target in it["text"] and it["text"] not in CHAT_TEXT_BLACKLIST]
    if exact:
        return max(exact, key=lambda it: it["score"])
    min_ratio = float(config.get("ocr_match_min_ratio", 0.6))
    best = None
    best_ratio = 0.0
    for it in items:
        if it["text"] in CHAT_TEXT_BLACKLIST:
            continue
        ratio = difflib.SequenceMatcher(None, target, it["text"]).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = it
    if best and best_ratio >= min_ratio:
        return {**best, "match_ratio": best_ratio}
    return None


def ark_locate_in_region(image_path, target: str, config: dict, win_rect_img: dict, region_ratio) -> dict | None:
    """ARK Vision 兜底：裁出窗口 region 区域图，让视觉模型给目标中心相对位置，换算回窗口比例。
    use_ark_vision_fallback=false 或 ARK 未配置/调用失败 -> 返回 None（静默降级，不阻断主路径）。"""
    if not config.get("use_ark_vision_fallback", True) or Image is None:
        return None
    try:
        call_ark_vision_json = import_ark_vision()
    except Exception as exc:
        print(f"ark_unavailable detail={exc}")
        return None
    image = Image.open(image_path).convert("RGB")
    img_w, img_h = image.size
    scale = get_screen_scale(img_w)
    ll = win_rect_img["left"] / scale
    tt = win_rect_img["top"] / scale
    ww = win_rect_img["width"] / scale
    hh = win_rect_img["height"] / scale
    x0 = int(max(0, min(ll + region_ratio[0] * ww, img_w)))
    y0 = int(max(0, min(tt + region_ratio[1] * hh, img_h)))
    x1 = int(max(0, min(ll + region_ratio[2] * ww, img_w)))
    y1 = int(max(0, min(tt + region_ratio[3] * hh, img_h)))
    if x1 <= x0 or y1 <= y0:
        return None
    crop_path = ROOT / f"debug_ark_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    image.crop((x0, y0, x1, y1)).save(crop_path)
    try:
        data = call_ark_vision_json(
            "你是企业微信界面定位助手。在给定截图里找到名为目标的会话或聊天标题，"
            "只输出 JSON，不要 Markdown。字段：found(true/false), x_ratio, y_ratio。"
            "x_ratio/y_ratio 是目标中心相对整张图的位置比例(0~1)。找不到则 found=false。",
            str(crop_path),
            f"目标名称：{target}",
        )
    except Exception as exc:
        print(f"ark_call_failed detail={exc}")
        return None
    finally:
        _cleanup_debug_image(crop_path, config)
    if not data or not data.get("found"):
        return None
    try:
        ax = float(data.get("x_ratio"))
        ay = float(data.get("y_ratio"))
    except Exception:
        return None
    rx = region_ratio[0] + ax * (region_ratio[2] - region_ratio[0])
    ry = region_ratio[1] + ay * (region_ratio[3] - region_ratio[1])
    return {"rx": rx, "ry": ry, "text": target, "score": 1.0, "via": "ark", "image_width": img_w, "image_height": img_h}


def locate_and_open_conversation(window, target: str, config: dict) -> bool:
    """阶段1：截图会话列表区 -> 本地 OCR 定位目标会话 -> 点击打开；OCR 未命中走 ARK 兜底，
    再不行且开启搜索兜底则 search_then_locate；否则报错中止（绝不盲点坐标）。"""
    region = tuple(config.get("conv_list_region", [0.0, 0.10, 0.30, 1.0]))
    img, rect = screenshot_wecom(window, config, f"locate_{target}")
    try:
        if config.get("use_local_ocr", True):
            try:
                hit = find_text_in_ocr(ocr_region(img, region, rect, config), target, config)
                if hit:
                    add_send_trace(config, "会话列表OCR命中")
                    print(f"locate target={target} via=ocr rx={hit['rx']:.3f} ry={hit['ry']:.3f} score={hit['score']:.2f}")
                    click_conversation_hit(window, hit, rect, config, target)
                    time.sleep(float(config.get("open_conversation_wait_seconds", 1.0)))
                    return True
            except RpaError as exc:
                print(f"local_ocr_unavailable detail={exc}")
        hit = ark_locate_in_region(img, target, config, rect, region)
        if hit:
            add_send_trace(config, "会话列表ARK命中")
            print(f"locate target={target} via=ark rx={hit['rx']:.3f} ry={hit['ry']:.3f}")
            click_conversation_hit(window, hit, rect, config, target)
            time.sleep(float(config.get("open_conversation_wait_seconds", 1.0)))
            return True
    finally:
        _cleanup_debug_image(img, config)
    if config.get("enable_search_fallback", False):
        add_send_trace(config, "进入搜索兜底")
        return search_then_locate(window, target, config)
    add_send_trace(config, "会话列表未命中")
    raise RpaError(f"无法在会话列表定位「{target}」（OCR/ARK 均未命中），已中止，绝不盲点坐标。")


def search_then_locate(window, target: str, config: dict) -> bool:
    """阶段2：点击搜索框(不用 Ctrl+F) -> 输入会话名 -> 截图搜索结果区 -> OCR/ARK 定位 -> 点击。
    覆盖目标会话不在当前可见列表的情况。"""
    activate(window, config)
    ensure_foreground_wecom(window, config)
    box = tuple(config.get("search_box_region", [0.0, 0.0, 0.30, 0.07]))
    click_window_ratio(window, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, config)
    time.sleep(0.4)
    ensure_foreground_wecom(window, config)
    keyboard.send_keys("^a")
    time.sleep(0.1)
    pyperclip.copy(target)
    keyboard.send_keys("^v")
    time.sleep(float(config.get("search_wait_seconds", 1.5)))
    region = tuple(config.get("search_result_region", [0.0, 0.07, 0.30, 1.0]))
    img, rect = screenshot_wecom(window, config, f"search_{target}")
    try:
        if config.get("use_local_ocr", True):
            try:
                hit = find_text_in_ocr(ocr_region(img, region, rect, config), target, config)
                if hit:
                    add_send_trace(config, "搜索结果OCR命中")
                    print(f"search_locate target={target} via=ocr rx={hit['rx']:.3f} ry={hit['ry']:.3f}")
                    click_search_result_hit(window, hit, rect, config, target)
                    time.sleep(float(config.get("open_conversation_wait_seconds", 1.0)))
                    return True
            except RpaError as exc:
                print(f"local_ocr_unavailable detail={exc}")
        hit = ark_locate_in_region(img, target, config, rect, region)
        if hit:
            add_send_trace(config, "搜索结果ARK命中")
            print(f"search_locate target={target} via=ark")
            click_search_result_hit(window, hit, rect, config, target)
            time.sleep(float(config.get("open_conversation_wait_seconds", 1.0)))
            return True
    finally:
        _cleanup_debug_image(img, config)
    add_send_trace(config, "搜索结果未命中")
    raise RpaError(search_result_not_found_detail(target, "搜索结果"))


def verify_active_conversation(window, target: str, config: dict):
    """发送前安全闸门：截图聊天区顶部标题 -> OCR 确认==target 才放行。防发错群。
    verify_block_on_mismatch=true 时不匹配直接 raise；false 时仅告警。"""
    if not config.get("verify_active_conversation_enabled", True):
        return
    region = tuple(config.get("chat_title_region", [0.30, 0.0, 0.80, 0.13]))
    img, rect = screenshot_wecom(window, config, f"verify_{target}")
    try:
        text = visible_text(window)
        title = window.window_text()
        ocr_items = []
        if config.get("use_local_ocr", True):
            try:
                ocr_items = ocr_region(img, region, rect, config)
            except RpaError as exc:
                print(f"local_ocr_unavailable detail={exc}")
        min_ratio = float(config.get("title_match_min_ratio", 0.7))
        clean_target = (target or "").strip()
        if clean_target and (clean_target in (text or "") or clean_target in (title or "")):
            add_send_trace(config, "标题窗口文本命中")
        elif any(text_matches_target(clean_target, str(item.get("text", "")), min_ratio) for item in ocr_items):
            add_send_trace(config, "标题OCR命中")
        try:
            validate_active_conversation_title(
                target,
                visible_text=text,
                window_title=title,
                ocr_items=ocr_items,
                ark_hit=False,
                min_ratio=min_ratio,
            )
            ok = True
        except SendGuardError:
            ark_hit = ark_locate_in_region(img, target, config, rect, region) is not None
            if ark_hit:
                add_send_trace(config, "标题ARK命中")
            try:
                validate_active_conversation_title(
                    target,
                    visible_text=text,
                    window_title=title,
                    ocr_items=ocr_items,
                    ark_hit=ark_hit,
                    min_ratio=min_ratio,
                )
                ok = True
            except SendGuardError:
                ok = False
    finally:
        _cleanup_debug_image(img, config)
    if not ok:
        add_send_trace(config, "标题校验未命中")
        if config.get("verify_block_on_mismatch", True):
            raise RpaError(conversation_title_mismatch_detail(target))
        print(f"WARN 未确认当前会话为「{target}」，但 verify_block_on_mismatch=false，继续。")


# 聚焦/打开目标会话：新版企微改用截图+OCR 视觉定位（见 locate_and_open_conversation）。
def search_conversation(window, conversation: str, config: dict):
    locate_and_open_conversation(window, conversation, config)


# 采集左侧会话列表中可见的控件，用于判断是否存在未读红点。
def left_pane_controls(window, config: dict):
    rect = window.rectangle()
    max_x = rect.left + rect.width() * float(config.get("conversation_list_max_ratio_x", 0.42))
    min_y = rect.top + rect.height() * float(config.get("conversation_list_min_ratio_y", 0.08))
    controls = []
    for control in safe_descendants(window):
        text = control_text(control)
        crect = control_rect(control)
        if not text or not crect:
            continue
        center_x = crect.left + crect.width() / 2
        center_y = crect.top + crect.height() / 2
        if rect.left <= center_x <= max_x and center_y >= min_y:
            controls.append((control, text, crect))
    return controls


# 从左侧列表里识别出被关注的会话行。
def find_conversation_rows(window, config: dict) -> list[dict]:
    controls = left_pane_controls(window, config)
    watched = watched_conversations(config)
    ignored = ignored_conversations(config)
    rows = []
    used_names = set()
    for control, text, rect in controls:
        band_items = [
            (other_text, other_control, other_rect)
            for other_control, other_text, other_rect in controls
            if abs((other_rect.top + other_rect.bottom) / 2 - (rect.top + rect.bottom) / 2) <= max(18, rect.height())
        ]
        band = [item[0] for item in band_items]
        unread = any(is_unread_text(item) for item in band)
        if not unread:
            continue

        matched_name = ""
        for name in watched:
            if name and any(name in item for item in band):
                matched_name = name
                break
        if not matched_name:
            names = [
                item[0]
                for item in band_items
                if not is_unread_text(item[0])
                and item[0] not in CHAT_TEXT_BLACKLIST
                and len(item[0]) >= 2
                and not re.fullmatch(r"\d{1,2}:\d{2}", item[0])
            ]
            matched_name = sorted(names, key=len, reverse=True)[0] if names else ""
        if not matched_name or matched_name in used_names or matched_name in ignored:
            continue
        name_control = next((item[1] for item in band_items if matched_name in item[0]), control)
        name_rect = next((item[2] for item in band_items if matched_name in item[0]), rect)
        rows.append({"name": matched_name, "control": name_control, "rect": name_rect, "unread": True, "band": band})
        used_names.add(matched_name)
    return rows


def cluster_points(points: list[tuple[int, int]], distance: int = 18) -> list[list[tuple[int, int]]]:
    clusters = []
    for point in points:
        x, y = point
        matched = None
        for cluster in clusters:
            cx = sum(p[0] for p in cluster) / len(cluster)
            cy = sum(p[1] for p in cluster) / len(cluster)
            if abs(x - cx) <= distance and abs(y - cy) <= distance:
                matched = cluster
                break
        if matched is None:
            clusters.append([point])
        else:
            matched.append(point)
    return clusters


def detect_unread_badges_from_screenshot(window, config: dict) -> list[dict]:
    image_path = capture_window_image(window, config, "unread_scan")
    image = Image.open(image_path).convert("RGB")
    win_rect = window.rectangle()
    width, height = image.size
    min_x = int(width * float(config.get("screenshot_list_min_ratio_x", 0.07)))
    max_x = int(width * float(config.get("screenshot_list_max_ratio_x", 0.36)))
    min_y = int(height * float(config.get("screenshot_list_min_ratio_y", 0.08)))
    max_y = int(height * float(config.get("screenshot_list_max_ratio_y", 0.98)))
    points = []
    for y in range(min_y, max_y):
        for x in range(min_x, max_x):
            r, g, b = image.getpixel((x, y))
            if r >= int(config.get("unread_red_min_r", 220)) and g <= int(config.get("unread_red_max_g", 120)) and b <= int(config.get("unread_red_max_b", 130)):
                points.append((x, y))
    rows = []
    for cluster in cluster_points(points, int(config.get("unread_badge_cluster_distance", 16))):
        if len(cluster) < int(config.get("unread_badge_min_pixels", 20)):
            continue
        xs = [p[0] for p in cluster]
        ys = [p[1] for p in cluster]
        x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
        if (x2 - x1) > 60 or (y2 - y1) > 40:
            continue
        center_y = int((y1 + y2) / 2)
        crop_left = int(width * float(config.get("conversation_row_crop_left_ratio_x", 0.07)))
        crop_right = int(width * float(config.get("conversation_row_crop_right_ratio_x", 0.36)))
        crop_top = max(0, center_y - int(config.get("conversation_row_crop_half_height", 34)))
        crop_bottom = min(height, center_y + int(config.get("conversation_row_crop_half_height", 34)))
        crop = image.crop((crop_left, crop_top, crop_right, crop_bottom))
        crop_path = ROOT / f"debug_wecom_row_{center_y}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
        crop.save(crop_path)
        rows.append(
            {
                "name": "",
                "control": None,
                "rect": None,
                "unread": True,
                "band": [],
                "local_click_x": int(width * float(config.get("conversation_row_click_ratio_x", 0.18))),
                "local_click_y": center_y,
                "click_x": int(win_rect.left + width * float(config.get("conversation_row_click_ratio_x", 0.18))),
                "click_y": int(win_rect.top + center_y),
                "screenshot": str(image_path),
                "row_crop": str(crop_path),
            }
        )
    unique = []
    seen_y = set()
    for row in sorted(rows, key=lambda item: item["click_y"]):
        bucket = row["click_y"] // int(config.get("conversation_row_dedupe_height", 20))
        if bucket in seen_y:
            continue
        seen_y.add(bucket)
        unique.append(row)
    return unique[: int(config.get("max_unread_conversations_per_run", 5))]


def recognize_conversation_row(row: dict, config: dict) -> str:
    if not config.get("use_ark_vision_for_conversation_name", True):
        return ""
    crop = row.get("row_crop")
    if not crop:
        return ""
    try:
        call_ark_vision_json = import_ark_vision()
        data = call_ark_vision_json(
            "你是企业微信左侧会话列表 OCR。只输出 JSON，不要 Markdown。字段：conversation_name, unread_count。conversation_name 是这一行的会话名称，不要输出消息摘要。",
            crop,
            "识别这张企业微信左侧会话行截图中的会话名称和红色未读数。",
        )
        name = str(data.get("conversation_name", "")).strip()
        if name and name not in CHAT_TEXT_BLACKLIST:
            return name
    except Exception as exc:
        print(f"vision_ocr_failed crop={crop} detail={exc}")
    return ""


# 收集当前未读会话；如果 UIA 读不到红点，再按 watch 列表兜底。
def collect_unread_conversations(window, config: dict) -> list[dict]:
    activate(window, config)
    rows = find_conversation_rows(window, config)
    unread = [row for row in rows if row["unread"]]
    if unread:
        return unread[: int(config.get("max_unread_conversations_per_run", 5))]
    if config.get("scan_watch_conversations_when_unread_unknown", False):
        print("WARN: 未从 UIA 检测到未读红点，按 watch_conversations 兜底扫描。")
        return [{"name": name, "control": None, "rect": None, "unread": False, "band": []} for name in watched_conversations(config)[: int(config.get("max_unread_conversations_per_run", 5))]]
    return []


# 打开指定会话行；有控件就点击，没有就走搜索。
def open_conversation_row(window, row: dict, config: dict):
    activate(window, config)
    ensure_foreground_wecom(window, config)
    if row.get("click_x") is not None and row.get("click_y") is not None:
        mouse.click(button="left", coords=(int(row["click_x"]), int(row["click_y"])))
        time.sleep(float(config.get("open_conversation_wait_seconds", 1.0)))
    elif row.get("control") is not None:
        try:
            row["control"].click_input()
        except Exception:
            rect = row.get("rect")
            if not rect:
                raise
            mouse.click(button="left", coords=(int(rect.left + rect.width() / 2), int(rect.top + rect.height() / 2)))
        time.sleep(float(config.get("open_conversation_wait_seconds", 1.0)))
    else:
        search_conversation(window, row["name"], config)


def resolve_conversation(target_name: str, config: dict) -> dict:
    mapped = (config.get("conversation_family_map") or {}).get(target_name)
    if mapped:
        return {"exists": True, "target_name": target_name, "family": {"family_id": mapped, "parent_nickname": target_name}}
    return request_json(config["api_base_url"], f"/api/rpa/conversations/resolve?target_name={quote_path(target_name)}")


def quote_path(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def prompt_unknown_conversation(target_name: str, config: dict, config_path: Path, row: dict | None = None) -> str:
    mode = config.get("unknown_conversation_policy", "prompt")
    if mode == "ignore":
        add_ignored_conversation(config, config_path, target_name)
        return ""
    if mode == "skip":
        return ""
    if not sys.stdin.isatty():
        print(f"unknown_conversation={target_name or '<未识别>'}: 非交互模式，已跳过。可在 rpa/config.json 配置 unknown_conversation_policy=ignore 或手动添加 conversation_family_map。")
        return ""

    if not target_name:
        print("\n检测到未读会话，但没有自动识别出会话名。")
        if row and row.get("row_crop"):
            print(f"请打开截图确认会话名：{row['row_crop']}")
        target_name = input("请输入企微会话名，空则跳过：").strip()
        if not target_name:
            return ""

    print(f"\n检测到数据库中不存在的企微会话：{target_name}")
    print("选择：a=添加为新家庭，i=加入忽略列表，s=本次跳过")
    try:
        choice = input("请输入 a/i/s：").strip().lower()
    except EOFError:
        print("无法读取命令行输入，已按本次跳过处理。")
        return ""
    if choice == "i":
        add_ignored_conversation(config, config_path, target_name)
        return ""
    if choice != "a":
        return ""

    default_family_id = f"WECOM_{target_name}"
    try:
        family_id = input(f"family_id [{default_family_id}]：").strip() or default_family_id
        parent_name = input(f"家长/会话显示名 [{target_name}]：").strip() or target_name
        child_grade = input("孩子年级，可空：").strip()
        coach_name = input("陪跑师，可空：").strip()
    except EOFError:
        print("无法读取新家庭字段，已按本次跳过处理。")
        return ""

    config.setdefault("conversation_family_map", {})[target_name] = family_id
    config.setdefault("new_family_defaults", {})[family_id] = {
        "parent_nickname": parent_name,
        "child_grade": child_grade,
        "coach_name": coach_name,
    }
    save_config(config, config_path)
    print(f"已添加会话映射：{target_name} -> {family_id}")
    return family_id


def ensure_conversation_family(target_name: str, config: dict, config_path: Path, row: dict | None = None) -> tuple[str, str]:
    if not target_name:
        family_id = prompt_unknown_conversation("", config, config_path, row)
        return family_id, next((name for name, fid in (config.get("conversation_family_map") or {}).items() if fid == family_id), "")
    resolved = resolve_conversation(target_name, config)
    if resolved.get("exists") and resolved.get("family"):
        return resolved["family"]["family_id"], target_name
    return prompt_unknown_conversation(target_name, config, config_path, row), target_name


# 再次确认当前窗口里确实处于目标会话。
def assert_conversation(window, conversation: str):
    text = visible_text(window)
    if conversation not in text and conversation not in window.window_text():
        raise RpaError(f"会话校验失败：当前企微窗口未识别到「{conversation}」。")


# 计算消息输入区的位置并点击。
# 把企微窗口设为/取消置顶（HWND_TOPMOST）。置顶后即使别的窗口占着前台焦点，
# 企微也显示在最上层，鼠标坐标点击会落在企微身上并把输入焦点交给它——
# 用来绕过 Windows 对后台进程 SetForegroundWindow 的限制。
def set_window_topmost(window, on: bool):
    try:
        flag = win32con.HWND_TOPMOST if on else win32con.HWND_NOTOPMOST
        win32gui.SetWindowPos(
            int(window.handle), flag, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
        )
    except Exception as exc:
        print(f"set_window_topmost_failed on={on} detail={exc}")


def focus_message_input(window, config: dict):
    # 定位/校验阶段把窗口最大化便于 OCR，但最大化状态下企微输入框会跑到可视区之外、
    # input_click_ratio 点不到。发送前用 activate 强制企微前台并 restore 成非最大化。
    if config.get("restore_before_input", True):
        try:
            activate(window, config)
            time.sleep(float(config.get("restore_settle_seconds", 0.5)))
        except Exception as exc:
            print(f"restore_before_input_failed detail={exc}")
    # 置顶企微，确保它显示在最上层、坐标点击落在它身上（绕过前台抢占限制）。
    if config.get("topmost_during_send", True):
        set_window_topmost(window, True)
        time.sleep(0.2)
    rect = window.rectangle()
    x = int(rect.left + rect.width() * float(config.get("input_click_ratio_x", 0.45)))
    y = int(rect.top + rect.height() * float(config.get("input_click_ratio_y", 0.85)))
    # 先点一次输入框：这一下既激活企微（点击会把焦点交给被点的窗口）、又把光标放进输入框。
    mouse.click(button="left", coords=(x, y))
    time.sleep(0.3)
    ensure_foreground_wecom(window, config)
    # 退出可能的消息多选模式：多选时底部是多选操作栏而非输入框，且 Ctrl+A 会“全选消息”。
    # 退完多选后再点一次输入框（多选退出后输入框才重新出现）。
    if config.get("esc_before_input", True):
        for _ in range(int(config.get("esc_repeat", 2))):
            keyboard.send_keys("{ESC}")
            time.sleep(0.2)
        mouse.click(button="left", coords=(x, y))
        time.sleep(0.3)
        ensure_foreground_wecom(window, config)





# 过滤掉导航词、时间戳和过短文本，避免把 UI 文本当聊天内容。
def is_chat_message_text(text: str, config: dict) -> bool:
    cleaned = (text or "").strip()
    if len(cleaned) < int(config.get("min_chat_text_length", 2)):
        return False
    if len(cleaned) > int(config.get("max_chat_text_length", 500)):
        return False
    if cleaned in CHAT_TEXT_BLACKLIST:
        return False
    if cleaned in watched_conversations(config):
        return False
    if is_unread_text(cleaned):
        return False
    if re.fullmatch(r"\d{1,2}:\d{2}", cleaned) or re.fullmatch(r"\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}", cleaned):
        return False
    return True


# 从会话窗口里抽取当前可见的聊天文本，返回后端可写入的消息结构。
def extract_visible_chat_messages(window, conversation: str, config: dict) -> list[dict]:
    rect = window.rectangle()
    min_x = rect.left + rect.width() * float(config.get("chat_area_min_ratio_x", 0.32))
    min_y = rect.top + rect.height() * float(config.get("chat_area_min_ratio_y", 0.10))
    max_y = rect.top + rect.height() * float(config.get("chat_area_max_ratio_y", 0.76))
    self_names = set(config.get("self_names", ["我", "本人", "陪跑师", "老师"]))
    rows = []
    seen = set()
    for control in safe_descendants(window, int(config.get("message_descendant_limit", 1000))):
        text = control_text(control)
        crect = control_rect(control)
        if not crect:
            continue
        center_x = crect.left + crect.width() / 2
        center_y = crect.top + crect.height() / 2
        if center_x < min_x or center_y < min_y or center_y > max_y:
            continue
        if not is_chat_message_text(text, config):
            continue
        key = re.sub(r"\s+", " ", text)
        if key in seen:
            continue
        seen.add(key)
        speaker = conversation
        content = text
        if ":" in text[:12] or "：" in text[:12]:
            parts = re.split(r"[:：]", text, maxsplit=1)
            if len(parts) == 2 and 1 <= len(parts[0].strip()) <= 8:
                speaker = parts[0].strip()
                content = parts[1].strip()
        if speaker in self_names:
            speaker = "我"
        rows.append({"speaker": speaker, "content": content, "source": "企业微信RPA"})
    limit = int(config.get("max_messages_per_conversation", 20))
    return rows[-limit:]


def focus_chat_history(window, config: dict):
    ensure_foreground_wecom(window, config)
    rect = window.rectangle()
    x = int(rect.left + rect.width() * float(config.get("chat_history_click_ratio_x", 0.66)))
    y = int(rect.top + rect.height() * float(config.get("chat_history_click_ratio_y", 0.46)))
    mouse.click(button="left", coords=(x, y))
    time.sleep(0.2)


def normalize_clipboard_lines(text: str, conversation: str, config: dict) -> list[dict]:
    lines = [line.strip() for line in re.split(r"[\r\n]+", text or "") if line.strip()]
    messages = []
    current_speaker = conversation
    current_time = ""
    time_pattern = re.compile(r"^(\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}|\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}|昨天\s+\d{1,2}:\d{2}|今天\s+\d{1,2}:\d{2}|\d{1,2}:\d{2})$")
    self_names = set(config.get("self_names", ["我", "本人", "陪跑师", "老师"]))
    for line in lines:
        if not is_chat_message_text(line, config):
            continue
        if time_pattern.match(line):
            current_time = line.replace("今天", datetime.now().strftime("%Y-%m-%d")).replace("昨天", "")
            continue
        speaker = current_speaker
        content = line
        if ":" in line[:16] or "：" in line[:16]:
            parts = re.split(r"[:：]", line, maxsplit=1)
            if len(parts) == 2 and 1 <= len(parts[0].strip()) <= 12:
                speaker = parts[0].strip()
                content = parts[1].strip()
        if not content or not is_chat_message_text(content, config):
            continue
        if speaker in self_names:
            speaker = "我"
        messages.append({"speaker": speaker, "content": content, "message_time": current_time, "source": "企业微信RPA-剪贴板"})
    limit = int(config.get("max_messages_per_conversation", 50))
    deduped = []
    seen = set()
    for msg in messages:
        key = (msg["speaker"], msg["content"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(msg)
    return deduped[-limit:]


def extract_chat_messages_by_clipboard(window, conversation: str, config: dict) -> list[dict]:
    if not config.get("use_clipboard_chat_extract", True):
        return []
    if not config.get("allow_clipboard_chat_extract", False):
        print("clipboard_extract=disabled: 为避免误复制其他页面，剪贴板读取默认关闭。需要时显式加 --allow-clipboard-copy。")
        return []
    old_clipboard = ""
    compare_clipboard = ""
    try:
        old_clipboard = pyperclip.paste()
        compare_clipboard = old_clipboard
    except Exception:
        pass
    if config.get("clipboard_extract_clear_before_copy", False):
        compare_clipboard = f"__WECOM_RPA_COPY_SENTINEL_{time.time_ns()}__"
        try:
            pyperclip.copy(compare_clipboard)
        except Exception:
            compare_clipboard = old_clipboard
    focus_chat_history(window, config)
    ensure_foreground_wecom(window, config)
    keyboard.send_keys("^a")
    time.sleep(0.2)
    ensure_foreground_wecom(window, config)
    keyboard.send_keys("^c")
    time.sleep(float(config.get("clipboard_copy_wait_seconds", 0.6)))
    try:
        copied = pyperclip.paste()
    except Exception as exc:
        print(f"clipboard_read_failed target={conversation} detail={exc}")
        return []
    finally:
        if config.get("restore_clipboard_after_extract", False):
            try:
                pyperclip.copy(old_clipboard)
            except Exception:
                pass
    if not copied or copied == compare_clipboard:
        return []
    return normalize_clipboard_lines(copied, conversation, config)


def extract_chat_messages(window, conversation: str, config: dict) -> list[dict]:
    messages = extract_visible_chat_messages(window, conversation, config)
    if messages:
        return messages
    return extract_chat_messages_by_clipboard(window, conversation, config)


def known_conversations_from_api(config: dict) -> list[dict]:
    families = request_json(config["api_base_url"], "/api/families")
    ignored = ignored_conversations(config)
    rows = []
    for family in families:
        target = (family.get("parent_nickname") or "").strip()
        if not target or target in ignored:
            continue
        rows.append(
            {
                "target_name": target,
                "family_id": family.get("family_id") or f"WECOM_{target}",
                "parent_nickname": target,
                "child_grade": family.get("child_grade") or "",
                "coach_name": family.get("coach_name") or "",
            }
        )
    return rows


# 把当前会话的消息同步给后端，并附带是否自动生成回复的控制位。
def sync_conversation_to_api(target_name: str, family_id: str, messages: list[dict], config: dict, latest_message: str = "") -> dict:
    defaults = (config.get("new_family_defaults") or {}).get(family_id, {})
    payload = {
        "target_name": target_name,
        "family_id": family_id,
        "parent_nickname": defaults.get("parent_nickname", target_name),
        "child_grade": defaults.get("child_grade", ""),
        "coach_name": defaults.get("coach_name", ""),
        "messages": messages,
        "auto_generate_reply": bool(config.get("auto_generate_ai_reply", True)),
        "auto_create_reply_task": bool(config.get("auto_create_reply_task", True)),
        "auto_generate_all_agents": bool(config.get("auto_generate_all_agents", True)),
        "latest_message": latest_message,
    }
    return request_json(config["api_base_url"], "/api/rpa/conversations/sync", method="POST", payload=payload)


def sync_target_conversation(config: dict, target: str, family_id: str = "", fields: dict | None = None) -> dict:
    check_api(config)
    window = find_wecom_window(config)
    search_conversation(window, target, config)
    verify_active_conversation(window, target, config)
    messages = extract_chat_messages(window, target, config)
    if not messages:
        raise RpaError(f"已进入「{target}」，但 UIA/剪贴板都没有读到聊天文本。请确认聊天区可见，或企微是否禁用了文本复制。")
    latest_count = int(config.get("latest_messages_count", 0) or 0)
    if latest_count > 0:
        messages = messages[-latest_count:]
    latest = next((msg["content"] for msg in reversed(messages) if msg.get("speaker") != "我"), messages[-1]["content"])
    defaults = fields or {}
    merged_defaults = config.setdefault("new_family_defaults", {}).setdefault(family_id or f"WECOM_{target}", {})
    for key in ("parent_nickname", "child_grade", "coach_name"):
        if defaults.get(key):
            merged_defaults[key] = defaults[key]
    result = sync_conversation_to_api(target, family_id or defaults.get("family_id") or f"WECOM_{target}", messages, config, latest)
    print(
        f"sync target={target} messages={len(messages)} inserted={result.get('messages_inserted')} "
        f"outputs={len(result.get('generated_outputs') or [])} task={result.get('send_task', {}).get('id') if result.get('send_task') else ''}"
    )
    return {"target": target, "status": "synced", "detail": result}


def sync_known_conversations(config: dict) -> list[dict]:
    rows = known_conversations_from_api(config)
    if not rows:
        print("known=0: 前端还没有登记企微会话。请先在「企微会话」页面添加会话名，例如：艺博展讯。")
        return []
    results = []
    for row in rows[: int(config.get("max_known_conversations_per_run", 20))]:
        target = row["target_name"]
        try:
            results.append(sync_target_conversation(config, target, row["family_id"], row))
        except Exception as exc:
            print(f"sync target={target} status=failed detail={exc}")
            results.append({"target": target, "status": "failed", "detail": str(exc)})
        time.sleep(float(config.get("known_conversation_interval_seconds", 1.0)))
    return results


# 先同步未读会话，再把结果返回给后续流程。
def sync_unread_conversations(config: dict, config_path: Path) -> list[dict]:
    check_api(config)
    window = find_wecom_window(config)
    rows = collect_unread_conversations(window, config)
    if not rows:
        print("unread=0: 未检测到未读会话。若实际有未读，请打开企微主窗口，或在 config.json 设置 scan_watch_conversations_when_unread_unknown=true。")
        return []

    results = []
    for row in rows:
        target = row["name"]
        try:
            family_id, resolved_target = ensure_conversation_family(target, config, config_path, row)
            target = resolved_target or target
            if not family_id:
                print(f"sync target={target} status=skipped reason=unknown_or_ignored")
                results.append({"target": target, "status": "skipped", "detail": "unknown_or_ignored"})
                continue
            open_conversation_row(window, row, config)
            messages = extract_visible_chat_messages(window, target, config)
            if not messages:
                screenshot = capture_debug_image(window, config, f"no_messages_{target}")
                raise RpaError(f"已进入「{target}」，但没有提取到可见聊天文本。debug={screenshot}")
            latest = next((msg["content"] for msg in reversed(messages) if msg.get("speaker") != "我"), messages[-1]["content"])
            result = sync_conversation_to_api(target, family_id, messages, config, latest)
            print(f"sync target={target} messages={len(messages)} inserted={result.get('messages_inserted')} ai_output={bool(result.get('ai_output'))} task={result.get('send_task', {}).get('id') if result.get('send_task') else ''}")
            results.append({"target": target, "status": "synced", "detail": result})
        except Exception as exc:
            screenshot = capture_debug_image(window, config, f"sync_failed_{target}")
            detail = f"{exc}; debug={screenshot}" if screenshot else str(exc)
            print(f"sync target={target} status=failed detail={detail}")
            results.append({"target": target, "status": "failed", "detail": detail})
    return results


def clear_message_input():
    keyboard.send_keys("^a")
    time.sleep(0.1)
    keyboard.send_keys("{DELETE}")
    time.sleep(0.1)


def verification_payload(status: str, detail: str, verified: bool = False) -> dict:
    return {
        "verify_status": status,
        "verify_detail": detail,
        "verified_at": datetime.utcnow().isoformat(timespec="seconds") if verified else None,
    }


def confirm_sent_message(window, target: str, text: str, config: dict) -> tuple[bool, dict]:
    time.sleep(float(config.get("post_send_verify_wait_seconds", 0.8)))
    messages = []
    clipboard_count = None
    try:
        ensure_foreground_wecom(window, config)
        if target and config.get("post_send_verify_reopen_conversation", True):
            search_conversation(window, target, config)
            verify_active_conversation(window, target, config)
            add_send_trace(config, "发送后已重新进入目标会话校验")
        verify_config = {**config, "chat_area_max_ratio_y": config.get("post_send_verify_chat_area_max_ratio_y", 0.84)}
        messages = extract_visible_chat_messages(window, target, verify_config)
    except Exception as exc:
        add_send_trace(config, f"发送后UIA回读异常:{exc}")
    if sent_content_confirmed(text, messages):
        detail = f"VERIFY_CONFIRMED: 目标「{target}」可见聊天记录回读命中本次内容，message_count={len(messages)}"
        add_send_trace(config, "发送后消息回读命中")
        return True, verification_payload("confirmed", detail, True)
    if config.get("post_send_verify_clipboard_fallback", True):
        try:
            ensure_foreground_wecom(window, config)
            clipboard_config = {
                **config,
                "allow_clipboard_chat_extract": True,
                "restore_clipboard_after_extract": config.get("post_send_verify_restore_clipboard", True),
                "clipboard_extract_clear_before_copy": True,
            }
            clipboard_messages = extract_chat_messages_by_clipboard(window, target, clipboard_config)
            clipboard_count = len(clipboard_messages)
            if sent_content_confirmed(text, clipboard_messages):
                detail = f"VERIFY_CONFIRMED: 目标「{target}」剪贴板聊天记录回读命中本次内容，message_count={len(clipboard_messages)}"
                add_send_trace(config, "发送后剪贴板回读命中")
                return True, verification_payload("confirmed", detail, True)
            add_send_trace(config, f"发送后剪贴板回读未命中:{len(clipboard_messages)}")
        except Exception as exc:
            add_send_trace(config, f"发送后剪贴板回读异常:{exc}")
    clipboard_note = f"，clipboard_count={clipboard_count}" if clipboard_count is not None else ""
    detail = f"目标「{target}」聊天记录未回读到本次内容，uia_count={len(messages)}{clipboard_note}"
    add_send_trace(config, "发送后消息回读未命中")
    return False, verification_payload("failed", detail, True)


# 真正发送消息前，先补充签名，再决定是 dry-run 还是回车发送。
def send_message(window, content: str, config: dict):
    text = content.strip()
    signature = config.get("append_signature", "").strip()
    if signature:
        text = f"{text}\n{signature}"
    config["_send_verification"] = {}
    if not config.get("dry_run", True) and not real_send_enabled(config):
        config["_send_verification"] = verification_payload("not_applicable", "真实发送未放行，未按发送键")
        return "skipped", real_send_block_detail()
    try:
        try:
            focus_message_input(window, config)
            add_send_trace(config, "输入框已聚焦")
        except Exception as exc:
            add_send_trace(config, "输入框定位失败")
            raise RpaError(f"INPUT_FOCUS: 输入框定位失败：{exc}") from exc
        ensure_foreground_wecom(window, config)
        # 清空输入框：focus_message_input 已退出多选并点中输入框，此处 Ctrl+A 只全选输入框文本。
        # 若担心焦点未落在输入框（会误触发消息多选），把 clear_input_before_paste 关掉即可。
        if config.get("clear_input_before_paste", True):
            clear_message_input()
        pyperclip.copy(text)
        ensure_foreground_wecom(window, config)
        keyboard.send_keys("^v")
        time.sleep(0.4)
        if not should_press_send_hotkey(config):
            ensure_foreground_wecom(window, config)
            clear_message_input()
            config["_send_verification"] = verification_payload("not_applicable", "dry-run 未按发送键，无需群内回读")
            add_send_trace(config, "dry-run已清空输入框")
            return "dry_run", dry_run_result_detail()
        if not config.get("verify_sent_message_enabled", True):
            config["_send_verification"] = verification_payload("failed", "发送后回读配置已关闭，已阻止真实发送")
            add_send_trace(config, "发送后回读配置关闭，已阻止真实发送")
            return "failed", "SEND_GUARD: 发送后回读配置关闭，无法落地校验结果，已阻止真实发送。"
        ensure_foreground_wecom(window, config)
        hotkey(config.get("send_hotkey", ["enter"]))
        add_send_trace(config, "真实发送热键已触发")
        confirmed, verification = confirm_sent_message(window, config.get("_current_target", ""), text, config)
        config["_send_verification"] = verification
        if not confirmed:
            return "failed", "SEND_CONFIRM_FAILED: 已触发真实发送热键，但未在可见聊天记录回读到本次内容，请人工核对后再重试。"
        return "sent", "REAL_RPA: 已通过企业微信 PC 端发送。"
    finally:
        # 无论成功/失败/异常，都取消置顶，避免企微一直压在所有窗口最上层。
        if config.get("topmost_during_send", True):
            set_window_topmost(window, False)


def validate_task_content(content: str) -> str:
    """RPA 发送前的最后一道内容闸门，防止历史乱码任务绕过后端校验。"""
    text = (content or "").strip()
    if not text:
        raise RpaError("发送内容为空，已阻止发送。")
    if "\ufffd" in text:
        raise RpaError("发送内容包含替换字符，疑似编码损坏，已阻止发送。")
    question_count = text.count("?")
    if re.search(r"\?{4,}", text) or (question_count >= 6 and question_count / max(len(text), 1) >= 0.2):
        raise RpaError("发送内容包含大量问号，疑似中文编码损坏，已阻止发送。")
    mojibake_hits = sum(1 for token in ("锛", "涓", "浠", "寰", "绯", "璇", "鎴", "鐨", "瀹") if token in text)
    if mojibake_hits >= 3:
        raise RpaError("发送内容疑似乱码，请重新编辑后再发送。")
    return text


def config_for_task_send_mode(config: dict, send_mode: str) -> dict:
    try:
        return config_for_send_mode(config, send_mode)
    except SendGuardError as exc:
        raise RpaError(str(exc)) from exc


def config_with_device_policy(config: dict, task: dict) -> dict:
    merged = {**config}
    if "device_allow_real_send" in task:
        merged["server_allow_real_send"] = task.get("device_allow_real_send") is True
    return merged


# 根据白名单判断是否允许发送，并走发送或跳过逻辑。
def process_task(task: dict, config: dict):
    config = config_with_device_policy(config, task)
    target = task.get("target_name") or ""
    if not task.get("server_allowed_target") and not target_in_allowed_conversations(target, config.get("allowed_conversations", [])):
        return "skipped", target_not_allowed_detail(target), verification_payload("not_applicable", "目标不在本设备会话范围，未发送")
    if task.get("server_allowed_target"):
        add_send_trace(config, "服务端会话策略放行")
    mode = (task.get("send_mode") or "").strip()
    if real_send_requested(config, mode) and not real_send_enabled(config):
        return "skipped", real_send_block_detail(), verification_payload("not_applicable", "真实发送未放行，未按发送键")
    content = validate_task_content(task.get("content") or "")
    task_config = config_for_task_send_mode(config, mode)
    task_config["_current_target"] = target
    try:
        window = find_wecom_window(task_config)
        search_conversation(window, target, task_config)
        verify_active_conversation(window, target, task_config)  # 发送前安全闸门：OCR 校验聊天标题，防发错群
        status, detail = send_message(window, content, task_config)
        return status, detail_with_send_trace(detail, task_config), task_config.get("_send_verification", {})
    except Exception as exc:
        raise RpaError(detail_with_send_trace(str(exc), task_config)) from exc


# 发送单条任务并把结果回写到后端日志接口。
def send_task_and_record(task: dict, config: dict):
    task_id = task["id"]
    verification = {}
    try:
        status, detail, verification = process_task(task, config)
    except Exception as exc:
        status, detail = "failed", str(exc)
    screenshot_base64 = capture_send_screenshot(config, task_id, status)
    verification = verification or {}
    request_json(config["api_base_url"], f"/api/send-tasks/{task_id}/result", method="POST",
                 payload={
                     "status": status,
                     "detail": detail,
                     "device_id": config.get("device_id", ""),
                     "screenshot_base64": screenshot_base64,
                     "verify_status": verification.get("verify_status", ""),
                     "verify_detail": verification.get("verify_detail", ""),
                     "verified_at": verification.get("verified_at"),
                  },
                 extra_headers=device_headers(config))
    print(f"task={task_id} target={task.get('target_name')} mode={task.get('send_mode') or 'config_default'} status={status} detail={detail}")
    return status, detail


# 把本轮同步中自动创建的 AI 回复任务继续取出来发送。
def send_created_reply_tasks(sync_results: list[dict], config: dict):
    created_ids = []
    for result in sync_results:
        task = ((result.get("detail") or {}).get("send_task") or {})
        if task.get("id"):
            created_ids.append(task["id"])
    if not created_ids:
        print("reply_tasks=0: 本轮没有新建AI回复发送任务。")
        return
    tasks = request_json(config["api_base_url"], "/api/send-tasks")
    by_id = {task["id"]: task for task in tasks}
    for task_id in created_ids:
        task = by_id.get(task_id)
        if not task:
            print(f"task={task_id} status=missing")
            continue
        send_task_and_record(task, config)
        time.sleep(float(config.get("send_interval_seconds", 3)))


    # 同步未读会话后，按开关决定是否继续自动发送 AI 回复。
def reply_unread_conversations(config: dict, config_path: Path):
    results = sync_unread_conversations(config, config_path)
    if config.get("auto_send_ai_replies", False):
        send_heartbeat(config)
        send_created_reply_tasks(results, config)
    else:
        print("auto_send_ai_replies=false: AI回复已生成待发送任务，请在后台审核后发送。")


# 设备鉴权请求头：让后端识别是哪台被控机。没配 device_id 时返回空（走旧单机模式）。
def device_headers(config: dict) -> dict:
    device_id = config.get("device_id", "")
    if not device_id:
        return {}
    return {"X-Device-Id": device_id, "X-Device-Token": config.get("device_token", "")}


# 向后端上报心跳：在线状态、企微是否可用、本机负责的会话（供后端动态领取过滤）。
def send_heartbeat(config: dict):
    device_id = config.get("device_id", "")
    if not device_id:
        return None
    wecom_ok = "N"
    try:
        wins = find_wecom_windows(Desktop(backend="uia"), config)
        wecom_ok = "Y" if wins else "N"
    except Exception:
        wecom_ok = "N"
    payload = {"wecom_ok": wecom_ok, "detail": "", "conversations": watched_conversations(config)}
    try:
        response = request_json(config["api_base_url"], f"/api/devices/{device_id}/heartbeat",
                                method="POST", payload=payload, extra_headers=device_headers(config))
        if isinstance(response, dict) and "allow_real_send" in response:
            config["server_allow_real_send"] = response.get("allow_real_send") is True
        return response
    except Exception as exc:
        print(f"heartbeat_failed detail={exc}")
        return None


# 发送队列中的待发送任务：配了 device_id 走多被控端领取，否则拉全部筛 pending（兼容旧单机）。
def run_once(config: dict):
    base_url = config["api_base_url"]
    limit = int(config.get("max_tasks_per_run", 5))
    device_id = config.get("device_id", "")
    if device_id:
        send_heartbeat(config)
        selected = request_json(base_url, f"/api/devices/{device_id}/claim?limit={limit}",
                                method="POST", extra_headers=device_headers(config))
        real_policy = selected[0].get("device_allow_real_send") if selected else config.get("server_allow_real_send", "unknown")
        print(f"device={device_id}, claimed={len(selected)}, default_dry_run={config.get('dry_run', True)}, server_allow_real_send={real_policy}")
    else:
        tasks = request_json(base_url, "/api/send-tasks")
        pending = [task for task in tasks if task.get("status") == "pending"]
        selected = pending[:limit]
        print(f"pending={len(pending)}, selected={len(selected)}, default_dry_run={config.get('dry_run', True)}")
    for task in selected:
        send_task_and_record(task, config)
        time.sleep(float(config.get("send_interval_seconds", 3)))


    # 只检查窗口是否存在，方便排查企微是否已启动。
def check_window(config: dict):
    window = find_wecom_window(config)
    activate(window, config)
    print(f"window_title={window.window_text()}")
    print("wecom_window_found=true")


# 诊断 API、窗口、未读识别和配置，尽量一次把问题定位清楚。
def diagnose(config: dict):
    print("== RPA诊断 ==")
    validate_config(config)
    try:
        health = check_api(config)
        print(f"api_ok=true detail={health}")
    except Exception as exc:
        print(f"api_ok=false detail={exc}")

    try:
        window = find_wecom_window(config)
        activate(window, config)
        print(f"wecom_window_found=true title={window.window_text()} handle={window.handle}")
        rows = find_conversation_rows(window, config)
        unread = [row for row in rows if row["unread"]]
        print(f"conversation_rows_detected={len(rows)} unread_detected={len(unread)}")
        for row in rows[:10]:
            print(f"row name={row['name']} unread={row['unread']} band={' / '.join(row['band'][:6])}")
        text = visible_text(window)
        print(f"visible_text_chars={len(text)}")
        known = known_conversations_from_api(config)
        print(f"known_conversations={len(known)} names={[item['target_name'] for item in known]}")
    except Exception as exc:
        print(f"wecom_window_found=false detail={exc}")
        print("提示：请确认企业微信PC端已登录、没有最小化；如用管理员权限启动企微，也请用管理员权限启动本脚本。")

    print(f"watch_conversations={watched_conversations(config)}")
    print(f"ignored_conversations={sorted(ignored_conversations(config))}")
    print(f"allowed_conversations={config.get('allowed_conversations', [])}")
    print(f"dry_run={config.get('dry_run', True)} auto_generate_ai_reply={config.get('auto_generate_ai_reply', True)} auto_create_reply_task={config.get('auto_create_reply_task', True)} auto_generate_all_agents={config.get('auto_generate_all_agents', True)} auto_send_ai_replies={config.get('auto_send_ai_replies', False)}")


# 持续监听未读会话，适合长时间开着的本地试点场景。
def watch_unread(config: dict, config_path: Path):
    interval = float(config.get("unread_poll_interval_seconds", 20))
    print(f"watch_unread=true interval={interval}s")
    while True:
        try:
            if config.get("auto_reply_in_watch_mode", False):
                reply_unread_conversations(config, config_path)
            else:
                sync_unread_conversations(config, config_path)
        except KeyboardInterrupt:
            print("已停止监听。")
            return
        except Exception as exc:
            print(f"watch_cycle_failed={exc}")
        time.sleep(interval)


def watch_known(config: dict):
    interval = float(config.get("known_poll_interval_seconds", 60))
    print(f"watch_known=true interval={interval}s")
    while True:
        try:
            sync_known_conversations(config)
        except KeyboardInterrupt:
            print("已停止监听。")
            return
        except Exception as exc:
            print(f"watch_known_cycle_failed={exc}")
        time.sleep(interval)


# 创建一条测试任务，验证后端、白名单和 RPA 发送链路是否可用。
def create_test_task(config: dict, target: str, content: str):
    payload = {
        "family_id": "RPA_TEST",
        "target_name": target,
        "scene": "RPA真实发送测试",
        "content": content,
    }
    task = request_json(config["api_base_url"], "/api/send-tasks", method="POST", payload=payload)
    print(json.dumps(task, ensure_ascii=False, indent=2))


# 阶段0 零风险探针：截图会话列表区做 OCR，打印识别到的文字+相对窗口位置，不点击、不发送。
def ocr_probe(config: dict, target: str = ""):
    window = find_wecom_window(config)
    img, rect = screenshot_wecom(window, config, "ocr_probe")
    print(f"== OCR PROBE == win_rect={rect}")
    print(f"screenshot={img}")
    for name, key, default in [
        ("会话列表", "conv_list_region", [0.0, 0.10, 0.30, 1.0]),
        ("聊天标题", "chat_title_region", [0.30, 0.0, 0.80, 0.13]),
    ]:
        region = tuple(config.get(key, default))
        items = ocr_region(img, region, rect, config)
        print(f"-- {name} region={region} items={len(items)} --")
        for it in sorted(items, key=lambda x: x["ry"]):
            print(f"  text={it['text']!r} score={it['score']:.2f} rx={it['rx']:.3f} ry={it['ry']:.3f}")
        if target:
            print(f"  match {target!r} -> {find_text_in_ocr(items, target, config)}")


# 命令行入口：根据参数选择诊断、同步、监听或直接发送。
def main():
    parser = argparse.ArgumentParser(description="企业微信 PC 端 RPA 发送器")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    parser.add_argument("--diagnose", action="store_true", help="诊断 API、企微窗口、未读识别与配置")
    parser.add_argument("--check-window", action="store_true", help="只检查是否能找到企业微信窗口")
    parser.add_argument("--sync-unread", action="store_true", help="识别未读会话并同步可见聊天记录到数据库")
    parser.add_argument("--sync-known", action="store_true", help="按前端登记的企微会话名逐个搜索并同步聊天记录")
    parser.add_argument("--sync-target", default="", help="直接搜索并同步指定企微会话，例如：艺博展讯")
    parser.add_argument("--latest-count", type=int, default=0, help="同步时只取最新 N 条消息；0 表示按配置上限")
    parser.add_argument("--no-ai", action="store_true", help="只同步消息，不调用 AI Agent，便于隔离测试 RPA 读取链路")
    parser.add_argument("--allow-clipboard-copy", action="store_true", help="允许 RPA 在确认前台为企业微信后使用 Ctrl+A/C 读取聊天区文本")
    parser.add_argument("--reply-unread", action="store_true", help="同步未读会话，调用AI回复Agent并创建/可选发送任务")
    parser.add_argument("--watch-unread", action="store_true", help="循环监听未读会话并同步")
    parser.add_argument("--watch-known", action="store_true", help="循环同步前端登记的企微会话")
    parser.add_argument("--ocr-probe", action="store_true", help="阶段0探针：截图+OCR 打印会话列表识别结果，不点击不发送")
    parser.add_argument("--create-test-task", action="store_true", help="创建一条发送到白名单会话的测试任务")
    parser.add_argument("--target", default="艺博展讯", help="测试任务目标会话")
    parser.add_argument("--content", default="RPA真实发送测试：这是一条本地试点系统自动发送的测试消息。", help="测试任务内容")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    if args.allow_clipboard_copy:
        config["allow_clipboard_chat_extract"] = True
    if args.latest_count:
        config["latest_messages_count"] = args.latest_count
    if args.no_ai:
        config["auto_generate_ai_reply"] = False
        config["auto_create_reply_task"] = False
        config["auto_generate_all_agents"] = False
    validate_config(config)
    if args.diagnose:
        diagnose(config)
        return
    if args.check_window:
        check_window(config)
        return
    if args.sync_unread:
        sync_unread_conversations(config, config_path)
        return
    if args.sync_known:
        sync_known_conversations(config)
        return
    if args.sync_target:
        sync_target_conversation(config, args.sync_target)
        return
    if args.reply_unread:
        reply_unread_conversations(config, config_path)
        return
    if args.watch_unread:
        watch_unread(config, config_path)
        return
    if args.watch_known:
        watch_known(config)
        return
    if args.ocr_probe:
        ocr_probe(config, args.target)
        return
    if args.create_test_task:
        create_test_task(config, args.target, args.content)
        return
    run_once(config)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
