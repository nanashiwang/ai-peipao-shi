"""发送任务操作分层。

控制端只返回当前角色和任务状态下能做的动作，真实发送确认收敛到管理员角色。
"""

from __future__ import annotations


OPERATION_LABELS = {
    "view": "查看",
    "edit": "编辑",
    "review": "审核",
    "assign_device": "调度设备",
    "dry_run": "企微试运行",
    "web_send": "网页发送",
    "retry": "失败重试",
    "cancel": "取消",
    "confirm_real_send": "确认真实发送",
}

TERMINAL_STATUSES = {"sent", "cancelled"}
WRITE_ROLES = {"admin", "coach"}


def normalize_operation_role(role: str | None) -> str:
    clean = (role or "").strip()
    return clean if clean in {"admin", "coach", "readonly"} else "readonly"


def send_task_workflow_stage(status: str | None, send_mode: str | None) -> str:
    clean_status = (status or "pending").strip()
    clean_mode = (send_mode or "dry_run").strip()
    if clean_status == "pending" and clean_mode == "real_send":
        return "待企微真实发送"
    if clean_status == "pending":
        return "待审核/试运行"
    if clean_status == "assigned":
        return "被控端发送中"
    if clean_status == "dry_run":
        return "试运行完成"
    if clean_status == "failed":
        return "发送失败待复核"
    if clean_status == "sent":
        return "已发送归档"
    if clean_status == "cancelled":
        return "已取消归档"
    return clean_status or "未知状态"


def role_allows_task_operation(status: str | None, send_mode: str | None, role: str | None, operation: str) -> bool:
    role = normalize_operation_role(role)
    status = (status or "pending").strip()
    send_mode = (send_mode or "dry_run").strip()
    if operation == "view":
        return True
    if role not in WRITE_ROLES:
        return False
    if operation == "cancel":
        return status in {"pending", "assigned"}
    if operation == "dry_run":
        return status == "pending"
    if operation == "web_send":
        return status == "pending" and send_mode != "real_send"
    if operation == "retry":
        return status == "failed" and (role == "admin" or send_mode != "real_send")
    if operation == "confirm_real_send":
        return role == "admin" and status == "pending" and send_mode != "real_send"
    if operation == "assign_device":
        return role == "admin" and status == "pending"
    if operation in {"edit", "review"}:
        if status in TERMINAL_STATUSES or status == "assigned":
            return False
        return role == "admin" or send_mode != "real_send"
    return False


def send_task_operation_state(status: str | None, send_mode: str | None, role: str | None) -> dict:
    operations = [
        key
        for key in OPERATION_LABELS
        if role_allows_task_operation(status, send_mode, role, key)
    ]
    role = normalize_operation_role(role)
    warnings = []
    if role == "readonly":
        warnings.append("只读角色仅可查看，敏感字段返回脱敏视图。")
    if (send_mode or "dry_run") == "real_send" and role != "admin":
        warnings.append("真实发送任务仅管理员可编辑或确认。")
    if (status or "pending") in TERMINAL_STATUSES:
        warnings.append("终态任务只能查看审计与发送结果。")
    return {
        "workflow_stage": send_task_workflow_stage(status, send_mode),
        "allowed_operations": operations,
        "operation_labels": {key: OPERATION_LABELS[key] for key in operations},
        "operation_warnings": warnings,
    }
