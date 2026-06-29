"""RPA 发送安全闸门。

保持为纯标准库模块，方便在不安装 pywinauto 的测试环境里验证核心发送策略。
"""


REAL_SEND_GUARD_MESSAGE = "REAL_SEND_GUARD: 被控端 allow_real_send=false，已阻止真实发送。"


class SendGuardError(ValueError):
    pass


def real_send_enabled(config: dict) -> bool:
    return config.get("allow_real_send") is True


def real_send_block_detail() -> str:
    return REAL_SEND_GUARD_MESSAGE


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
