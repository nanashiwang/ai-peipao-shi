"""敏感数据脱敏工具。

只处理展示/导出视图，不修改数据库原始记录，避免影响 AI 分析和发送链路。
"""

from __future__ import annotations

import re
from copy import deepcopy


PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
NAME_KEYS = {
    "parent_nickname",
    "parent_name",
    "guardian_name",
    "target_name",
    "speaker",
    "coach_name",
    "display_name",
    "username",
}
CHILD_KEYS = {
    "child_grade",
    "child_name",
    "student_name",
    "grade",
    "student_grade",
    "course_stage",
    "unit_progress",
    "pbl_count",
    "checkin_rate",
    "next_milestone",
}
PHONE_KEYS = {"phone", "mobile", "parent_phone", "手机号", "手机", "家长手机号"}
CONTENT_KEYS = {
    "content",
    "display_text",
    "edited_output",
    "final_text",
    "detail",
    "evidence_json",
    "raw_json",
    "overall_state",
    "main_changes",
    "parent_focus",
    "teacher_suggestion",
    "next_followup",
    "pain_points",
    "communication_style",
    "satisfaction_level",
    "child_summary",
    "service_risks",
    "renewal_intent",
    "evidence",
    "suggested_actions",
    "summary",
    "before_json",
    "after_json",
    "latest_message",
    "last_message",
    "message",
}


def mask_phone(value: str) -> str:
    return PHONE_RE.sub(lambda match: f"{match.group(1)[:3]}****{match.group(1)[-4:]}", str(value or ""))


def mask_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 1:
        return "*"
    if len(text) == 2:
        return f"{text[0]}*"
    return f"{text[0]}{'*' * (len(text) - 2)}{text[-1]}"


def redact_chat_content(value: str) -> str:
    text = mask_phone(str(value or "").strip())
    if not text:
        return ""
    return f"[聊天内容已脱敏，长度{len(text)}字]"


def redact_scalar(key: str, value):
    clean_key = (key or "").strip()
    if value is None:
        return value
    if clean_key in PHONE_KEYS or "phone" in clean_key.lower() or "手机号" in clean_key:
        return mask_phone(str(value))
    if clean_key in NAME_KEYS:
        return mask_name(str(value))
    if clean_key in CHILD_KEYS:
        return "[孩子信息已脱敏]" if str(value or "").strip() else ""
    if clean_key in CONTENT_KEYS:
        return redact_chat_content(str(value))
    if isinstance(value, str):
        return mask_phone(value)
    return value


def redact_record(record):
    if isinstance(record, list):
        return [redact_record(item) for item in record]
    if not isinstance(record, dict):
        return mask_phone(record) if isinstance(record, str) else record
    data = deepcopy(record)
    for key, value in list(data.items()):
        if isinstance(value, (dict, list)):
            data[key] = redact_record(value)
        else:
            data[key] = redact_scalar(key, value)
    data["privacy_level"] = "redacted"
    return data
