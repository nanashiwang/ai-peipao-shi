"""RPA 发送安全闸门。

保持为纯标准库模块，方便在不安装 pywinauto 的测试环境里验证核心发送策略。
"""

import difflib
import math


REAL_SEND_GUARD_MESSAGE = "REAL_SEND_GUARD: 被控端 allow_real_send=false，已阻止真实发送。"
TARGET_NOT_ALLOWED_TEMPLATE = "目标「{target}」不在白名单，已跳过。"
FOREGROUND_UNKNOWN_MESSAGE = "无法确认当前前台窗口，已停止 RPA 操作，避免误操作其他页面。"
FOREGROUND_NOT_WECOM_TEMPLATE = "当前前台窗口不是企业微信，已停止 RPA 操作。foreground={foreground}"
CONVERSATION_TITLE_MISMATCH_TEMPLATE = "发送前校验失败：当前聊天标题不是「{target}」，已阻止发送（防发错群）。"
SEARCH_RESULT_NOT_FOUND_TEMPLATE = "{stage}未命中「{target}」，已中止，绝不盲点坐标。"
DRY_RUN_RESULT_MESSAGE = "DRY_RUN: 已定位会话并粘贴内容，未按发送键，已清空输入框。"
SEND_TRACE_PREFIX = "RPA_TRACE:"
SEND_TRACE_KEY = "_send_trace"


class SendGuardError(ValueError):
    pass


def real_send_enabled(config: dict) -> bool:
    return config.get("allow_real_send") is True


def real_send_block_detail() -> str:
    return REAL_SEND_GUARD_MESSAGE


def dry_run_result_detail() -> str:
    return DRY_RUN_RESULT_MESSAGE


def add_send_trace(config: dict, event: str) -> None:
    clean_event = (event or "").strip()
    if not isinstance(config, dict) or not clean_event:
        return
    trace = config.setdefault(SEND_TRACE_KEY, [])
    if clean_event not in trace:
        trace.append(clean_event)


def send_trace_items(config: dict) -> list[str]:
    if not isinstance(config, dict):
        return []
    return [str(item).strip() for item in config.get(SEND_TRACE_KEY, []) if str(item).strip()]


def detail_with_send_trace(detail: str, config: dict) -> str:
    text = (detail or "").strip()
    trace = send_trace_items(config)
    if not trace or SEND_TRACE_PREFIX in text:
        return text
    return f"{text}\n{SEND_TRACE_PREFIX} {'；'.join(trace)}"


def target_not_allowed_detail(target: str) -> str:
    return TARGET_NOT_ALLOWED_TEMPLATE.format(target=target or "")


def target_in_allowed_conversations(target: str, allowed_conversations) -> bool:
    allowed = {str(item).strip() for item in (allowed_conversations or []) if str(item).strip()}
    clean_target = (target or "").strip()
    return bool(clean_target) and clean_target in allowed


def foreground_not_wecom_detail(foreground: str | int) -> str:
    return FOREGROUND_NOT_WECOM_TEMPLATE.format(foreground=foreground or "unknown")


def validate_foreground_wecom(foreground_handle: int, target_handle: int = 0, foreground_is_wecom: bool = False, foreground_title: str = "") -> None:
    if not foreground_handle:
        raise SendGuardError(FOREGROUND_UNKNOWN_MESSAGE)
    if foreground_is_wecom:
        return
    if target_handle and int(foreground_handle) == int(target_handle):
        return
    raise SendGuardError(foreground_not_wecom_detail(foreground_title or foreground_handle))


def conversation_title_mismatch_detail(target: str) -> str:
    return CONVERSATION_TITLE_MISMATCH_TEMPLATE.format(target=(target or "").strip())


def search_result_not_found_detail(target: str, stage: str = "搜索结果") -> str:
    clean_stage = (stage or "搜索结果").strip()
    return SEARCH_RESULT_NOT_FOUND_TEMPLATE.format(stage=clean_stage, target=(target or "").strip())


def visual_hit_has_coordinates(hit) -> bool:
    if not isinstance(hit, dict):
        return False
    try:
        rx = float(hit["rx"])
        ry = float(hit["ry"])
    except (KeyError, TypeError, ValueError):
        return False
    return math.isfinite(rx) and math.isfinite(ry)


def validate_visual_hit(target: str, hit, stage: str = "搜索结果") -> None:
    if not visual_hit_has_coordinates(hit):
        raise SendGuardError(search_result_not_found_detail(target, stage))


def text_matches_target(target: str, text: str, min_ratio: float = 0.7) -> bool:
    clean_target = (target or "").strip()
    clean_text = (text or "").strip()
    if not clean_target or not clean_text:
        return False
    return clean_target in clean_text or difflib.SequenceMatcher(None, clean_target, clean_text).ratio() >= min_ratio


def _ocr_text(item) -> str:
    if isinstance(item, dict):
        return str(item.get("text", ""))
    return str(item or "")


def active_conversation_verified(
    target: str,
    visible_text: str = "",
    window_title: str = "",
    ocr_items=None,
    ark_hit: bool = False,
    min_ratio: float = 0.7,
) -> bool:
    clean_target = (target or "").strip()
    if not clean_target:
        return False
    if clean_target in (visible_text or "") or clean_target in (window_title or ""):
        return True
    if any(text_matches_target(clean_target, _ocr_text(item), min_ratio) for item in (ocr_items or [])):
        return True
    return bool(ark_hit)


def validate_active_conversation_title(
    target: str,
    visible_text: str = "",
    window_title: str = "",
    ocr_items=None,
    ark_hit: bool = False,
    min_ratio: float = 0.7,
) -> None:
    if not active_conversation_verified(target, visible_text, window_title, ocr_items, ark_hit, min_ratio):
        raise SendGuardError(conversation_title_mismatch_detail(target))


def real_send_requested(config: dict, send_mode: str) -> bool:
    mode = (send_mode or "").strip()
    return mode == "real_send" or (not mode and config.get("dry_run") is False)


def config_for_send_mode(config: dict, send_mode: str) -> dict:
    task_config = {**config}
    mode = (send_mode or "").strip()
    if not mode and not task_config.get("dry_run", True) and not real_send_enabled(config):
        raise SendGuardError(real_send_block_detail())
    if mode == "dry_run":
        task_config["dry_run"] = True
    elif mode == "real_send":
        if not real_send_enabled(config):
            raise SendGuardError(real_send_block_detail())
        task_config["dry_run"] = False
    elif mode:
        raise SendGuardError(f"未知发送模式：{mode}")
    if task_config.get("dry_run", True):
        task_config["clear_after_dry_run"] = True
    return task_config


def should_press_send_hotkey(config: dict) -> bool:
    return config.get("dry_run") is False
