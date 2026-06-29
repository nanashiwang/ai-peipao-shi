"""RPA 发送安全闸门。

保持为纯标准库模块，方便在不安装 pywinauto 的测试环境里验证核心发送策略。
"""


REAL_SEND_GUARD_MESSAGE = "REAL_SEND_GUARD: 被控端 allow_real_send=false，已阻止真实发送。"
TARGET_NOT_ALLOWED_TEMPLATE = "目标「{target}」不在白名单，已跳过。"
FOREGROUND_UNKNOWN_MESSAGE = "无法确认当前前台窗口，已停止 RPA 操作，避免误操作其他页面。"
FOREGROUND_NOT_WECOM_TEMPLATE = "当前前台窗口不是企业微信，已停止 RPA 操作。foreground={foreground}"


class SendGuardError(ValueError):
    pass


def real_send_enabled(config: dict) -> bool:
    return config.get("allow_real_send") is True


def real_send_block_detail() -> str:
    return REAL_SEND_GUARD_MESSAGE


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
    return task_config
