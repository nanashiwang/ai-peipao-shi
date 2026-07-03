"""FastAPI 应用入口。

这个文件把数据导入、Agent 生成、发送任务和 RPA 同步等接口组装成完整的本地 MVP。
"""

import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from urllib.parse import unquote

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db import DATABASE_URL, get_db, init_db
from app.models import (
    AIOutput,
    AuditLog,
    CheckinRecord,
    Device,
    DeviceConversationCheck,
    Family,
    FollowupRecord,
    ParentProfile,
    RawMessage,
    SendLog,
    SendTask,
    Template,
    UserAccount,
    WecomArchiveState,
    WeeklyReport,
)
from app.services.agent_service import (
    build_agent_context,
    run_checkin_pbl_agent as run_checkin_pbl_agent_service,
    run_family_profile_agent as run_family_profile_agent_service,
    run_quick_reply_agent_service,
    run_reply_agent_service,
    run_weekly_report_agent as run_weekly_report_agent_service,
)
from app.services.agent_eval import list_agent_eval_cases, run_agent_evaluation
from app.services.ai_mock import generate_parent_profile, generate_weekly_report
from app.services.admin_auth import (
    ADMIN_ROLES,
    DEPLOYED_ENVS,
    admin_auth_required,
    admin_auth_secret,
    bearer_token,
    normalize_campus_names,
    path_requires_admin_auth,
    role_allowed_for_request,
    sign_admin_token,
    sign_parent_token,
    verify_admin_token,
    verify_parent_token,
)
from app.services.backup_service import backup_path, create_backup, list_backups, run_restore_drill
from app.services.claim_lock import apply_claim_row_lock, claim_lock_report, normalize_claim_limit
from app.services.importer import import_rows, import_template_csv_bytes, list_import_templates, rows_from_upload
from app.services.rate_limit import (
    admin_rate_limit_rule_for_path,
    admin_rate_limiter,
    rate_limit_enabled,
    rate_limit_key_for_request,
    rate_limit_report,
)
from app.services.retention_service import prune_retention, retention_policy_from_env, retention_report
from app.services.runtime_config import assert_runtime_config_safe, runtime_config_report
from app.services.scenario import detect_checkin, detect_scene
from app.services.send_log_classifier import classify_send_log
from app.services.redaction_service import redact_record
from app.services.send_task_operations import (
    OPERATION_LABELS,
    role_allows_task_operation,
    send_task_operation_state,
    send_task_workflow_stage,
)
from app.services.wecom_archive import (
    ArchiveEnvelope,
    config_status as wecom_archive_config_status,
    group_archive_messages,
    normalize_archive_message,
    pull_archive_messages,
    read_wecom_archive_config,
)
from rpa.package_manifest import build_package_manifest

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
SEND_SCREENSHOT_DIR = ROOT / "data" / "send_screenshots"
BACKUP_DIR = ROOT / "data" / "backups"
REPLY_AGENT_CONFIG_PATH = ROOT / "config" / "reply_agents.json"
MAX_SEND_SCREENSHOT_BYTES = 6 * 1024 * 1024
REAL_SEND_DUPLICATE_WINDOW_SECONDS = 3600
REAL_SEND_MIN_INTERVAL_SECONDS = 30
SEND_TASK_EXECUTION_MAX_AGE_DAYS = 7
DEVICE_CONVERSATION_PROOF_MAX_AGE_HOURS = 24
WEEKLY_REPORT_SCENE = "周报发送"
CONVERSATION_CHECK_SCENE = "会话可读校验"
CONVERSATION_CHECK_CONTENT = "只读校验：打开目标会话并读取可见消息，不粘贴不发送。"
REAL_SEND_PREP_CONVERSATION_CHECK_PREFIX = "真实发送前置校验："
_wecom_archive_poller_started = False

REPLY_AGENT_OPTIONS = [
    {"key": "context_agent", "name": "上下文 Agent", "description": "读取家庭画像、最近聊天、周报和话术模板"},
    {"key": "scene_agent", "name": "场景识别 Agent", "description": "识别请假、催打卡、投诉、续费等回复场景"},
    {"key": "reply_agent", "name": "回复生成 Agent", "description": "生成可直接进入发送队列的家长回复"},
    {"key": "safety_agent", "name": "安全兜底 Agent", "description": "命中高风险、投诉、退费等内容时转人工"},
]
REPLY_AGENT_DEFAULT_CONFIG = {
    "auto_reply_enabled": False,
    "auto_create_send_task": True,
    "send_mode": "dry_run",
    "tone": "standard",
    "reply_agent": "ai_reply_agent",
    "enabled_agents": [item["key"] for item in REPLY_AGENT_OPTIONS],
    "high_risk_policy": "manual",
    "skip_recent_hours": 8,
    "max_batch": 200,
}

# 应用实例和 CORS 配置，便于本地前端直接访问。
app = FastAPI(title="重庆机构陪跑师效率系统 MVP", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def admin_auth_middleware(request: Request, call_next):
    if rate_limit_enabled():
        rule = admin_rate_limit_rule_for_path(request.url.path)
        if rule:
            client_host = request.client.host if request.client else ""
            decision = admin_rate_limiter.check(rate_limit_key_for_request(client_host, rule), rule)
            if not decision.allowed:
                return JSONResponse(
                    {"detail": "请求过于频繁，请稍后再试", "retry_after_seconds": decision.retry_after_seconds},
                    status_code=429,
                    headers={"Retry-After": str(decision.retry_after_seconds)},
                )
    if not admin_auth_required() or not path_requires_admin_auth(request.url.path):
        return await call_next(request)
    try:
        identity = verify_admin_token(bearer_token(request.headers.get("authorization", "")), admin_auth_secret())
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"detail": str(exc) or "请先登录管理端"}, status_code=401)
    if not role_allowed_for_request(identity.role, request.method, request.url.path):
        return JSONResponse({"detail": "当前角色无权执行该操作"}, status_code=403)
    request.state.admin_identity = identity
    return await call_next(request)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# 模板输入：保存到模板库的最小字段集合。
class TemplateIn(BaseModel):
    name: str
    scene: str
    content: str
    send_time: str = ""
    enabled: str = "Y"


# 周报人工审核时，只需要更新最终文本和状态。
class ReportUpdate(BaseModel):
    final_text: str
    status: str = "approved"


class FollowupIn(BaseModel):
    followup_type: str = "私信"
    content: str
    result: str = ""
    next_action: str = ""
    owner: str = ""
    status: str = "待跟进"
    occurred_at: datetime | None = None


class FamilyAIBundleIn(BaseModel):
    source: str = "家庭详情一键生成"


# 直接创建发送任务时的输入结构。
class SendTaskIn(BaseModel):
    family_id: str
    target_name: str
    scene: str = "手动测试"
    content: str
    device_id: str = ""
    send_mode: str = "dry_run"
    confirm_real_send: bool = False


class SendTaskPreflightIn(SendTaskIn):
    family_id: str = ""


# 页面手动登记企微会话时使用；parent_nickname 就是企微搜索框里要输入的会话名。
class FamilyIn(BaseModel):
    family_id: str = ""
    parent_nickname: str
    child_grade: str = ""
    course_stage: str = ""
    unit_progress: str = ""
    pbl_count: int | None = None
    checkin_rate: str = ""
    next_milestone: str = ""
    campus_name: str = ""
    coach_name: str = ""
    service_status: str = "企微待同步"


# 回写发送结果时记录状态和详情。
class SendResultIn(BaseModel):
    status: str
    detail: str = ""
    device_id: str = ""
    client_result_id: str = ""
    screenshot_base64: str = ""
    verify_status: str = ""
    verify_detail: str = ""
    verified_at: datetime | None = None


class SendLogManualVerificationIn(BaseModel):
    confirmed: bool
    detail: str = ""


# 设备注册/更新入参。
class DeviceIn(BaseModel):
    device_id: str
    name: str = ""
    note: str = ""
    conversations: list[str] = []
    allow_real_send: bool | None = None
    allow_any_conversation: bool | None = None


class DeviceUpdateIn(BaseModel):
    name: str = ""
    note: str = ""
    conversations: list[str] | None = None
    allow_real_send: bool | None = None
    allow_any_conversation: bool | None = None


# 设备心跳上报：企微健康 + 本机负责的会话（后端据此刷新该设备 conversations，供动态领取过滤）。
class HeartbeatIn(BaseModel):
    wecom_ok: str = ""
    detail: str = ""
    conversations: list[str] = []
    outbox_pending_count: int = 0
    outbox_last_error: str = ""


class DeviceConversationCheckRequestIn(BaseModel):
    target_name: str
    family_id: str = ""


class DeviceConversationBatchCheckRequestIn(BaseModel):
    target_names: list[str] = Field(default_factory=list)
    missing_only: bool = False


class RetentionPruneIn(BaseModel):
    confirm_execute: bool = False


# 控制台在线配置阿里 ARK（百炼）云端定位密钥。
class ArkConfigIn(BaseModel):
    api_key: str
    endpoint_id: str = "qwen-vl-plus"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class ReplyAgentConfigIn(BaseModel):
    auto_reply_enabled: bool = False
    auto_create_send_task: bool = True
    send_mode: str = "dry_run"
    tone: str = "standard"
    reply_agent: str = "ai_reply_agent"
    enabled_agents: list[str] = Field(default_factory=lambda: ["context_agent", "scene_agent", "reply_agent", "safety_agent"])
    high_risk_policy: str = "manual"
    skip_recent_hours: int = 8
    max_batch: int = 200


# RPA 同步会话里的单条消息结构。
class RpaMessageIn(BaseModel):
    speaker: str = ""
    content: str
    message_time: str = ""
    source: str = "企业微信RPA"
    external_id: str = ""


# 一次同步一个会话，消息列表和是否自动生成回复都放在这里。
class RpaConversationIn(BaseModel):
    target_name: str
    family_id: str = ""
    parent_nickname: str = ""
    child_grade: str = ""
    campus_name: str = ""
    coach_name: str = ""
    messages: list[RpaMessageIn]
    auto_generate_reply: bool = True
    auto_create_reply_task: bool = False
    auto_generate_all_agents: bool = False
    latest_message: str = ""
    conversation_opened: bool = False
    empty_conversation_ok: bool = False


class WecomArchiveSyncIn(BaseModel):
    limit: int | None = None
    auto_generate_reply: bool = True
    auto_create_reply_task: bool = False
    auto_generate_all_agents: bool = False
    messages: list[dict] = []


# 更新发送任务的可编辑字段。
class SendTaskUpdate(BaseModel):
    target_name: str = ""
    scene: str = ""
    content: str = ""
    device_id: str | None = None
    send_mode: str = ""
    confirm_real_send: bool = False
    status: str = "pending"


class SendTaskRealSendIn(BaseModel):
    content: str | None = None
    device_id: str | None = None


# Agent 请求的统一入参。
class AgentRequest(BaseModel):
    family_id: str
    message: str = ""
    tone: str = "standard"
    source: str = ""


class AutoReplyDraftIn(BaseModel):
    tone: str = "standard"
    source: str = "自动回复草稿"
    skip_recent_hours: int = 24
    limit: int = 200


# AI 输出人工审核时使用的更新结构。
class AIOutputUpdate(BaseModel):
    edited_output: str
    status: str = "approved"


# 从 AI 输出直接创建任务时的可选覆盖字段。
class AIOutputTaskIn(BaseModel):
    content: str = ""
    scene: str = ""
    target_name: str = ""
    device_id: str = ""
    send_mode: str = "dry_run"
    confirm_real_send: bool = False


class AccountIn(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "parent"
    campus_names: str = ""
    family_id: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


class AccountProfileUpdateIn(BaseModel):
    username: str = ""
    display_name: str = ""
    current_password: str = ""
    new_password: str = ""


class ParentReportAckIn(BaseModel):
    note: str = ""


class ParentReportFeedbackIn(BaseModel):
    score: int
    note: str = ""


class ChatMessageIn(BaseModel):
    family_id: str
    username: str
    content: str


class ConversationDirectSendIn(BaseModel):
    content: str
    device_id: str = ""


class ChatAiIn(BaseModel):
    family_id: str
    create_task: bool = True


class ChatReplyIn(BaseModel):
    family_id: str
    tone: str = "standard"
    message: str = ""
    create_task: bool = True


# 把 ORM 对象转成普通字典，方便直接返回给前端。
def as_dict(obj):
    data = {}
    for col in obj.__table__.columns:
        value = getattr(obj, col.name)
        data[col.name] = value.isoformat(sep=" ", timespec="seconds") if hasattr(value, "isoformat") else value
    return data


def send_log_view(log: SendLog) -> dict:
    data = as_dict(log)
    data.update(classify_send_log(log.status, log.detail))
    data.update(send_log_manual_verification_state(log))
    return data


def is_real_send_attempt_log(log: SendLog) -> bool:
    return bool(
        log.send_mode == "real_send"
        and (
            log.status == "sent"
            or log.verify_status in {"failed", "unknown"}
            or "SEND_CONFIRM_FAILED" in (log.detail or "")
        )
    )


def real_send_closure_metrics(logs: list[SendLog]) -> dict:
    attempted_logs = [log for log in logs if is_real_send_attempt_log(log)]
    attempted_count = len(attempted_logs)
    sent_count = sum(1 for log in attempted_logs if log.status == "sent")
    confirmed_count = sum(1 for log in attempted_logs if log.status == "sent" and log.verify_status == "confirmed")
    unconfirmed_sent_count = sum(1 for log in attempted_logs if log.status == "sent" and log.verify_status != "confirmed")
    confirm_failed_count = attempted_count - confirmed_count
    rate = round((confirmed_count / attempted_count) * 100, 2) if attempted_count else 100.0
    return {
        "attempted_24h": attempted_count,
        "real_sent_24h": sent_count,
        "confirmed_24h": confirmed_count,
        "unconfirmed_sent_24h": unconfirmed_sent_count,
        "confirm_failed_24h": confirm_failed_count,
        "confirm_rate": rate,
    }


def real_send_breakdown(logs: list[SendLog], key: str, limit: int = 10) -> list[dict]:
    grouped: dict[str, list[SendLog]] = {}
    for log in logs:
        if not is_real_send_attempt_log(log):
            continue
        value = (getattr(log, key) or "未标记").strip() or "未标记"
        grouped.setdefault(value, []).append(log)
    rows = []
    for value, items in grouped.items():
        metrics = real_send_closure_metrics(items)
        latest = max(items, key=lambda item: item.sent_at or datetime.min)
        failed_items = [item for item in items if not (item.status == "sent" and item.verify_status == "confirmed")]
        latest_issue = max(failed_items, key=lambda item: item.sent_at or datetime.min) if failed_items else None
        rows.append(
            {
                key: value,
                **metrics,
                "last_sent_at": timeline_time(latest.sent_at),
                "last_issue": (latest_issue.verify_detail or latest_issue.detail or "")[:160] if latest_issue else "",
            }
        )
    return sorted(rows, key=lambda row: (row["confirm_rate"], -row["attempted_24h"], row.get(key, "")))[:limit]


def current_runtime_config_report() -> dict:
    return runtime_config_report(
        os.getenv("APP_ENV"),
        DATABASE_URL,
        ARK_CONFIG_PATH,
        database_url_explicit=bool(os.getenv("DATABASE_URL")),
        env=os.environ,
    )


def current_retention_policy() -> dict:
    return retention_policy_from_env(os.environ)


def admin_auth_component() -> dict:
    required = admin_auth_required()
    has_secret = bool(os.getenv("ADMIN_AUTH_SECRET", "").strip())
    generated_secret = False
    if required and not has_secret:
        try:
            admin_auth_secret()
            generated_secret = True
        except RuntimeError:
            generated_secret = False
    if required and not has_secret and not generated_secret:
        status = "critical"
        detail = "管理端鉴权已启用，但 ADMIN_AUTH_SECRET 未配置"
    elif required:
        status = "ok"
        detail = "管理端鉴权已启用" if has_secret else "管理端鉴权已启用，试点密钥已自动持久化"
    else:
        status = "ok"
        detail = "管理端鉴权未强制启用，仅适合本地/试点内网"
    return component_status(status, "管理端鉴权", detail, {"required": required, "secret_configured": has_secret or generated_secret})


def admin_identity_from_request(request: Request | None):
    if request is None:
        return None
    identity = getattr(getattr(request, "state", None), "admin_identity", None)
    if identity:
        return identity
    token = bearer_token(request.headers.get("authorization", ""))
    if not token:
        return None
    try:
        return verify_admin_token(token, admin_auth_secret())
    except (RuntimeError, ValueError):
        return None


def should_redact_for_request(request: Request | None) -> bool:
    identity = admin_identity_from_request(request)
    return bool(identity and identity.role == "readonly")


def maybe_redact_for_request(data, request: Request | None):
    return redact_record(data) if should_redact_for_request(request) else data


def operation_role_from_request(request: Request | None) -> str:
    if request is None:
        return "admin"
    identity = admin_identity_from_request(request)
    if identity:
        return identity.role
    return "readonly"


def coach_scope_from_request(request: Request | None) -> str:
    identity = admin_identity_from_request(request)
    if not identity or identity.role != "coach":
        return ""
    return (identity.display_name or identity.username or "").strip()


def campus_scope_from_request(request: Request | None) -> tuple[str, ...]:
    identity = admin_identity_from_request(request)
    return normalize_campus_names(identity.campus_names if identity else "")


def coach_filter_for_request(request: Request | None, requested_coach: str = "") -> str:
    return coach_scope_from_request(request) or (requested_coach or "").strip()


def family_scope_parts(request: Request | None) -> tuple[str, tuple[str, ...]]:
    return coach_scope_from_request(request), campus_scope_from_request(request)


def scoped_family_query(db: Session, request: Request | None = None):
    query = db.query(Family)
    coach_name, campus_names = family_scope_parts(request)
    if coach_name:
        query = query.filter(Family.coach_name == coach_name)
    if campus_names:
        query = query.filter(Family.campus_name.in_(campus_names))
    return query


def apply_family_id_scope(query, family_id_column, db: Session, request: Request | None = None):
    coach_name, campus_names = family_scope_parts(request)
    if not coach_name and not campus_names:
        return query
    scoped_ids = db.query(Family.family_id)
    if coach_name:
        scoped_ids = scoped_ids.filter(Family.coach_name == coach_name)
    if campus_names:
        scoped_ids = scoped_ids.filter(Family.campus_name.in_(campus_names))
    return query.filter(family_id_column.in_(scoped_ids))


def ensure_family_access(family: Family | None, request: Request | None) -> None:
    coach_name, campus_names = family_scope_parts(request)
    if coach_name and (not family or family.coach_name != coach_name):
        raise HTTPException(403, "当前陪跑师无权访问该家庭")
    if campus_names and (not family or family.campus_name not in campus_names):
        raise HTTPException(403, "当前账号无权访问该校区家庭")


def ensure_family_id_access(db: Session, family_id: str, request: Request | None) -> None:
    coach_name, campus_names = family_scope_parts(request)
    if not coach_name and not campus_names:
        return
    ensure_family_access(db.query(Family).filter(Family.family_id == family_id).one_or_none(), request)


def ensure_task_family_access(db: Session, task: SendTask | None, request: Request | None) -> None:
    if task:
        ensure_family_id_access(db, task.family_id, request)


def scoped_payload_coach_name(request: Request | None, coach_name: str) -> str:
    scope = coach_scope_from_request(request)
    clean = (coach_name or "").strip()
    if scope:
        if clean and clean != scope:
            raise HTTPException(403, "陪跑师只能创建或维护自己负责的家庭")
        return scope
    return clean


def scoped_payload_campus_name(request: Request | None, campus_name: str) -> str:
    scope = campus_scope_from_request(request)
    clean = (campus_name or "").strip()
    if not scope:
        return clean
    if clean:
        if clean not in scope:
            raise HTTPException(403, "当前账号不能创建或维护其他校区家庭")
        return clean
    if len(scope) == 1:
        return scope[0]
    raise HTTPException(400, "当前账号绑定多个校区，请先选择家庭所属校区")


def send_task_view(task: SendTask, request: Request | None = None, db: Session | None = None) -> dict:
    data = as_dict(task)
    data.update(send_task_operation_state(task.status, task.send_mode, operation_role_from_request(request)))
    data["retry_alert"] = task_needs_retry_alert(task)
    if db is not None:
        data["send_readiness"] = send_task_readiness(db, task)
    return data


def ensure_task_operation_allowed(task: SendTask, request: Request | None, operation: str) -> None:
    role = operation_role_from_request(request)
    if not role_allows_task_operation(task.status, task.send_mode, role, operation):
        label = OPERATION_LABELS.get(operation, operation)
        role_label = {"admin": "超管", "coach": "陪跑师", "readonly": "只读"}.get(role, role or "未知")
        stage = send_task_workflow_stage(task.status, task.send_mode)
        raise HTTPException(403, f"当前角色或任务状态不能执行「{label}」操作（角色：{role_label}，任务阶段：{stage}）")


def ensure_new_task_operation_allowed(request: Request | None, operation: str) -> None:
    role = operation_role_from_request(request)
    if not role_allows_task_operation("pending", "dry_run", role, operation):
        label = OPERATION_LABELS.get(operation, operation)
        role_label = {"admin": "超管", "coach": "陪跑师", "readonly": "只读"}.get(role, role or "未知")
        raise HTTPException(403, f"当前角色不能执行「{label}」操作（角色：{role_label}）")


def ensure_conversation_direct_send_allowed(request: Request | None) -> None:
    role = operation_role_from_request(request)
    if role not in {"admin", "coach"}:
        role_label = {"admin": "超管", "coach": "陪跑师", "readonly": "只读"}.get(role, role or "未知")
        raise HTTPException(403, f"当前角色不能在会话工作台直接真实发送（角色：{role_label}）")


def actor_from_request(request: Request | None, fallback: str = "控制端") -> str:
    if request is None:
        return fallback
    device_id = (request.headers.get("x-device-id") or "").strip()
    if device_id:
        return f"设备:{device_id}"[:120]
    actor = unquote((request.headers.get("x-actor") or request.headers.get("x-user") or "").strip())
    return (actor or fallback)[:120]


def device_from_optional_headers(db: Session, request: Request | None) -> Device | None:
    if request is None:
        return None
    device_id = (request.headers.get("x-device-id") or "").strip()
    if not device_id:
        return None
    token = (request.headers.get("x-device-token") or "").strip()
    dev = db.query(Device).filter(Device.device_id == device_id).first()
    if not dev or dev.token != token:
        raise HTTPException(401, "设备未注册或 token 不正确")
    return dev


def require_device_for_request(db: Session, request: Request | None) -> Device:
    dev = device_from_optional_headers(db, request)
    if dev is None:
        raise HTTPException(401, "缺少设备令牌")
    return dev


def audit_json(data: dict | None) -> str:
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True, default=str)


def send_task_snapshot(task: SendTask | None) -> dict:
    if not task:
        return {}
    return {
        "id": task.id,
        "family_id": task.family_id,
        "target_name": task.target_name,
        "scene": task.scene,
        "content": task.content,
        "send_mode": task.send_mode,
        "status": task.status,
        "device_id": task.device_id,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "next_retry_at": task.next_retry_at.isoformat(sep=" ", timespec="seconds") if task.next_retry_at else "",
        "last_error": task.last_error,
        "scheduled_at": task.scheduled_at.isoformat(sep=" ", timespec="seconds") if task.scheduled_at else "",
    }


def changed_fields(before: dict, after: dict) -> list[str]:
    keys = sorted(set(before) | set(after))
    return [key for key in keys if before.get(key) != after.get(key)]


def log_audit_event(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    actor: str,
    summary: str = "",
    before: dict | None = None,
    after: dict | None = None,
) -> AuditLog:
    log = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor=(actor or "系统")[:120],
        summary=summary,
        before_json=audit_json(before),
        after_json=audit_json(after),
    )
    db.add(log)
    return log


def add_send_task_with_audit(db: Session, task: SendTask, action: str, actor: str, summary: str) -> SendTask:
    db.add(task)
    db.flush()
    log_audit_event(db, "send_task", task.id, action, actor, summary, after=send_task_snapshot(task))
    return task


def audit_send_task_change(db: Session, task: SendTask, action: str, actor: str, summary: str, before: dict) -> None:
    after = send_task_snapshot(task)
    fields = changed_fields(before, after)
    detail = summary if not fields else f"{summary}；变更字段：{', '.join(fields)}"
    log_audit_event(db, "send_task", task.id, action, actor, detail, before=before, after=after)


def validate_send_task_content(content: str) -> str:
    """发送任务会触达真实家长/群，入队前先拦截空内容和明显编码损坏。"""
    text = (content or "").strip()
    if not text:
        raise HTTPException(400, "发送内容不能为空")
    if "\ufffd" in text:
        raise HTTPException(400, "发送内容包含替换字符，疑似编码损坏")
    question_count = text.count("?")
    if re.search(r"\?{4,}", text) or (question_count >= 6 and question_count / max(len(text), 1) >= 0.2):
        raise HTTPException(400, "发送内容包含大量问号，疑似中文编码损坏")
    mojibake_hits = sum(1 for token in ("锛", "涓", "浠", "寰", "绯", "璇", "鎴", "鐨", "瀹") if token in text)
    if mojibake_hits >= 3:
        raise HTTPException(400, "发送内容疑似乱码，请重新编辑后再提交")
    return text


AI_SENSITIVE_TERMS = ("退费", "退款", "投诉", "赔偿", "合同", "维权", "法律", "承诺效果", "保证效果", "价格争议")
AI_UNCERTAIN_TERMS = ("不确定", "无法判断", "需要确认", "转人工", "人工介入", "主管确认", "先确认")


def iter_ai_safety_texts(value) -> list[str]:
    """只扫描正文和值，避免 JSON 字段名误触发安全词。"""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text[0] in "{[":
            try:
                return iter_ai_safety_texts(json.loads(text))
            except json.JSONDecodeError:
                pass
        return [value]
    if isinstance(value, dict):
        items: list[str] = []
        for item in value.values():
            items.extend(iter_ai_safety_texts(item))
        return items
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            items.extend(iter_ai_safety_texts(item))
        return items
    if isinstance(value, (bool, int, float)):
        return []
    return [str(value)]


def ai_safety_findings(*texts: str) -> dict:
    blob = "\n".join(item for text in texts for item in iter_ai_safety_texts(text))
    sensitive = sorted({term for term in AI_SENSITIVE_TERMS if term in blob})
    uncertain = sorted({term for term in AI_UNCERTAIN_TERMS if term in blob})
    return {
        "sensitive_terms": sensitive,
        "uncertain_terms": uncertain,
        "requires_manual": bool(sensitive or uncertain),
    }


def validate_ai_output_send_boundary(output: AIOutput, content: str, send_mode: str) -> dict:
    findings = ai_safety_findings(output.raw_json, output.display_text, output.edited_output, content)
    if findings["requires_manual"] and output.status != "approved":
        raise HTTPException(400, "AI输出包含敏感或不确定内容，必须先人工审核后再加入发送任务")
    if send_mode == "real_send" and findings["requires_manual"]:
        raise HTTPException(400, "AI敏感/不确定内容禁止直接真实发送，请改为试运行或人工发送")
    return findings


def validate_send_mode(send_mode: str) -> str:
    mode = (send_mode or "dry_run").strip()
    if mode not in {"dry_run", "real_send"}:
        raise HTTPException(400, "send_mode 只能是 dry_run 或 real_send")
    return mode


def normalize_reply_agent_config(data: dict | None = None) -> dict:
    raw = {**REPLY_AGENT_DEFAULT_CONFIG, **(data or {})}

    def bounded_int(key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(raw.get(key) if raw.get(key) is not None else default)
        except (TypeError, ValueError):
            value = default
        return min(max(value, low), high)

    option_keys = {item["key"] for item in REPLY_AGENT_OPTIONS}
    enabled_agents = raw.get("enabled_agents")
    if not isinstance(enabled_agents, list):
        enabled_agents = REPLY_AGENT_DEFAULT_CONFIG["enabled_agents"]
    enabled_agents = [str(item) for item in enabled_agents if str(item) in option_keys]
    if not enabled_agents:
        enabled_agents = ["reply_agent", "safety_agent"]
    tone = str(raw.get("tone") or "standard").strip()
    if tone not in {"standard", "gentle", "short"}:
        tone = "standard"
    send_mode = str(raw.get("send_mode") or "dry_run").strip()
    if send_mode not in {"dry_run", "real_send"}:
        send_mode = "dry_run"
    high_risk_policy = str(raw.get("high_risk_policy") or "manual").strip()
    if high_risk_policy not in {"manual", "create_task"}:
        high_risk_policy = "manual"
    reply_agent = str(raw.get("reply_agent") or "ai_reply_agent").strip()
    if reply_agent not in {"ai_reply_agent", "quick_reply_agent"}:
        reply_agent = "ai_reply_agent"
    return {
        "auto_reply_enabled": bool(raw.get("auto_reply_enabled")),
        "auto_create_send_task": bool(raw.get("auto_create_send_task")),
        "send_mode": send_mode,
        "tone": tone,
        "reply_agent": reply_agent,
        "enabled_agents": enabled_agents,
        "high_risk_policy": high_risk_policy,
        "skip_recent_hours": bounded_int("skip_recent_hours", 8, 0, 168),
        "max_batch": bounded_int("max_batch", 200, 1, 500),
    }


def read_reply_agent_config() -> dict:
    if not REPLY_AGENT_CONFIG_PATH.exists():
        return normalize_reply_agent_config()
    try:
        data = json.loads(REPLY_AGENT_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return normalize_reply_agent_config()
    return normalize_reply_agent_config(data)


def reply_agent_config_view(config: dict | None = None) -> dict:
    data = normalize_reply_agent_config(config or read_reply_agent_config())
    return {**data, "available_agents": REPLY_AGENT_OPTIONS}


def write_reply_agent_config(config: dict) -> dict:
    data = normalize_reply_agent_config(config)
    REPLY_AGENT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPLY_AGENT_CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def has_recent_reply_output(db: Session, family_id: str, hours: int) -> bool:
    if hours <= 0:
        return False
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    return (
        db.query(AIOutput)
        .filter(
            AIOutput.family_id == family_id,
            AIOutput.agent_type == "ai_reply",
            AIOutput.created_at >= cutoff,
        )
        .first()
        is not None
    )


def validate_send_task_execution_guard(task: SendTask, now: datetime | None = None, reference_time: datetime | None = None) -> str:
    mode = validate_send_mode(task.send_mode)
    reference_time = reference_time or task.scheduled_at or task.created_at
    if not reference_time:
        raise HTTPException(400, "任务缺少调度时间，疑似旧任务，请保存后重新审核")
    now = now or datetime.utcnow()
    stale_before = now - timedelta(days=SEND_TASK_EXECUTION_MAX_AGE_DAYS)
    if reference_time < stale_before:
        raise HTTPException(400, f"任务已超过 {SEND_TASK_EXECUTION_MAX_AGE_DAYS} 天未处理，请保存后重新审核")
    return mode


def send_log_mode(task: SendTask) -> str:
    mode = (task.send_mode or "dry_run").strip()
    return mode if mode in {"dry_run", "real_send"} else "invalid"


SEND_VERIFY_STATUSES = {"", "confirmed", "failed", "unknown", "not_applicable"}


def infer_send_verify_status(status: str, detail: str, mode: str) -> str:
    detail = detail or ""
    if mode != "real_send":
        return "not_applicable" if status in {"dry_run", "skipped", "sent"} else ""
    if status == "sent" and ("VERIFY_CONFIRMED" in detail or "发送后消息回读命中" in detail):
        return "confirmed"
    if "SEND_CONFIRM_FAILED" in detail:
        return "failed"
    if status in {"dry_run", "skipped"}:
        return "not_applicable"
    return "unknown" if status == "sent" else ""


def real_send_verify_detail_has_evidence(detail: str, target_name: str) -> bool:
    clean_detail = (detail or "").strip()
    if "VERIFY_CONFIRMED" not in clean_detail or "回读命中" not in clean_detail:
        return False
    if "回读已落库" not in clean_detail:
        return False
    clean_target = (target_name or "").strip()
    return not clean_target or clean_target in clean_detail


def real_send_landed_proof_reason(db: Session, task: SendTask, device_id: str) -> str:
    clean_device_id = (device_id or task.device_id or "").strip()
    if not clean_device_id:
        return "缺少设备ID，无法关联本次群内回读落库证明"
    proof = device_conversation_check(db, clean_device_id, task.target_name)
    if not proof:
        return f"设备「{clean_device_id}」没有目标「{task.target_name}」的回读落库证明"
    if proof.status != "ok":
        return f"设备「{clean_device_id}」目标「{task.target_name}」回读证明状态为 {proof.status}"
    if (proof.message_count or 0) <= 0:
        return f"设备「{clean_device_id}」目标「{task.target_name}」回读证明没有聊天消息"
    reference_time = task.scheduled_at or task.created_at
    if reference_time and (not proof.verified_at or proof.verified_at < reference_time):
        return (
            f"设备「{clean_device_id}」目标「{task.target_name}」回读证明早于本次任务执行时间"
            f"（证明时间：{timeline_time(proof.verified_at)}，任务时间：{timeline_time(reference_time)}）"
        )
    return ""


def normalize_send_verification(payload: SendResultIn, task: SendTask, finished_at: datetime) -> tuple[str, str, datetime | None]:
    mode = send_log_mode(task)
    verify_status = (payload.verify_status or "").strip() or infer_send_verify_status(payload.status, payload.detail, mode)
    if verify_status not in SEND_VERIFY_STATUSES:
        raise HTTPException(400, "verify_status 不合法")
    verify_detail = (payload.verify_detail or "").strip()
    if (
        mode == "real_send"
        and payload.status == "sent"
        and verify_status == "confirmed"
        and not real_send_verify_detail_has_evidence(verify_detail, task.target_name)
    ):
        return (
            "unknown",
            "设备上报 confirmed，但缺少目标会话回读命中或落库证据；必须回到目标群/私聊读取聊天记录，并上报 VERIFY_CONFIRMED + 回读已落库明细",
            payload.verified_at,
        )
    if verify_status == "confirmed":
        verified_at = payload.verified_at or finished_at
        if not verify_detail:
            verify_detail = "已在目标会话可见聊天记录中回读命中本次内容"
    elif verify_status == "failed" and not verify_detail:
        verify_detail = "未在目标会话可见聊天记录中回读命中本次内容"
        verified_at = payload.verified_at
    else:
        verified_at = payload.verified_at
    return verify_status, verify_detail, verified_at


AUTO_RETRY_DELAY_SECONDS = 120
NON_RETRYABLE_FAILURE_TERMS = (
    "SEND_GUARD",
    "REAL_SEND_BLOCKED",
    "内容",
    "白名单",
    "标题不是",
    "不在设备",
    "旧任务",
    "超过",
)
REAL_SEND_RETRYABLE_FAILURE_TERMS = (
    "没有找到企业微信窗口",
    "当前前台窗口不是企业微信",
    "无法确认当前前台窗口",
    "INPUT_FOCUS",
    "输入框定位失败",
    "搜索结果未命中",
    "无法在会话列表定位",
    "窗口临时丢失",
    "窗口不可点",
    "BASELINE_READ_FAILED",
    "发送前消息基线采集失败",
)
REAL_SEND_AFTER_HOTKEY_TERMS = (
    "真实发送热键已触发",
    "REAL_RPA",
    "发送结果未知",
)
REAL_SEND_MANUAL_CONFIRM_EVIDENCE_TERMS = REAL_SEND_AFTER_HOTKEY_TERMS + (
    "SEND_CONFIRM_FAILED",
    "VERIFY_PERSIST_FAILED",
)


def send_log_has_real_send_attempt_evidence(log: SendLog) -> bool:
    if log.send_mode != "real_send":
        return False
    detail = f"{log.detail or ''}\n{log.verify_detail or ''}"
    return log.status == "sent" or any(term in detail for term in REAL_SEND_MANUAL_CONFIRM_EVIDENCE_TERMS)


def send_log_manual_verification_state(log: SendLog) -> dict:
    is_confirmed = log.status == "sent" and log.verify_status == "confirmed"
    has_attempt_evidence = send_log_has_real_send_attempt_evidence(log)
    allowed = bool(log.send_mode == "real_send" and not is_confirmed and has_attempt_evidence)
    return {
        "manual_verify_allowed": allowed,
        "manual_confirm_allowed": allowed,
        "manual_verify_reason": "" if allowed else "仅真实发送热键已触发但自动回读未闭环的日志允许人工核验",
    }


def is_retryable_send_failure(task: SendTask, detail: str) -> bool:
    if task.retry_count >= task.max_retries:
        return False
    detail = detail or ""
    if any(term in detail for term in NON_RETRYABLE_FAILURE_TERMS):
        return False
    mode = send_log_mode(task)
    if mode == "dry_run":
        return True
    if mode == "real_send":
        if any(term in detail for term in REAL_SEND_AFTER_HOTKEY_TERMS):
            return False
        return any(term in detail for term in REAL_SEND_RETRYABLE_FAILURE_TERMS)
    return False


def task_needs_retry_alert(task: SendTask) -> bool:
    return task.status == "failed" and (send_log_mode(task) == "real_send" or task.retry_count >= task.max_retries)


def apply_failed_send_retry_policy(task: SendTask, detail: str, now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.utcnow()
    task.last_error = detail or ""
    if is_retryable_send_failure(task, detail):
        task.retry_count += 1
        delay = AUTO_RETRY_DELAY_SECONDS * task.retry_count
        task.next_retry_at = now + timedelta(seconds=delay)
        task.scheduled_at = task.next_retry_at
        task.status = "pending"
        device_note = f"，仍由设备「{task.device_id}」重试" if task.device_id else ""
        return "auto_retry", f"发送失败已自动排队重试 {task.retry_count}/{task.max_retries}，下次时间 {timeline_time(task.next_retry_at)}{device_note}"
    task.status = "failed"
    task.next_retry_at = None
    if task_needs_retry_alert(task):
        return "alert", "发送失败已进入人工告警，请复核后手动重试"
    return "failed", "发送失败待复核"


def validate_send_mode_submit(send_mode: str, confirm_real_send: bool, current_mode: str = "") -> str:
    mode = validate_send_mode(send_mode)
    if mode == "real_send" and current_mode != "real_send" and not confirm_real_send:
        raise HTTPException(400, "真实发送需要显式确认")
    return mode


def normalize_send_content(content: str) -> str:
    return re.sub(r"\s+", " ", (content or "").strip())


def detect_image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    raise HTTPException(400, "截图只支持 PNG 或 JPG")


def store_send_screenshot(task_id: int, screenshot_base64: str) -> str:
    raw = (screenshot_base64 or "").strip()
    if not raw:
        return ""
    if "," in raw and raw.lower().startswith("data:image/"):
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "截图 base64 无法解析") from exc
    if not data:
        return ""
    if len(data) > MAX_SEND_SCREENSHOT_BYTES:
        raise HTTPException(400, f"截图不能超过 {MAX_SEND_SCREENSHOT_BYTES // 1024 // 1024}MB")
    ext = detect_image_extension(data)
    SEND_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"shot_{secrets.token_urlsafe(24)}.{ext}"
    path = (SEND_SCREENSHOT_DIR / filename).resolve()
    try:
        path.relative_to(SEND_SCREENSHOT_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(400, "截图路径非法") from exc
    path.write_bytes(data)
    return f"/api/send-artifacts/{filename}"


def resolve_send_screenshot(filename: str) -> Path:
    legacy_pattern = r"task_\d+_\d{8}_\d{6}_\d{6}\.(png|jpg)"
    random_pattern = r"shot_[A-Za-z0-9_-]{32,}\.(png|jpg)"
    if not re.fullmatch(f"(?:{legacy_pattern})|(?:{random_pattern})", filename or ""):
        raise HTTPException(404, "截图不存在")
    path = (SEND_SCREENSHOT_DIR / filename).resolve()
    try:
        path.relative_to(SEND_SCREENSHOT_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(404, "截图不存在") from exc
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "截图不存在")
    return path


def validate_real_send_risk(
    db: Session,
    target_name: str,
    content: str,
    exclude_task_id: int = 0,
    now: datetime | None = None,
) -> None:
    now = now or datetime.utcnow()
    normalized = normalize_send_content(content)
    if not target_name or not normalized:
        return

    active_tasks = (
        db.query(SendTask)
        .filter(
            SendTask.target_name == target_name,
            SendTask.send_mode == "real_send",
            SendTask.status.in_(["pending", "assigned"]),
        )
        .all()
    )
    for task in active_tasks:
        if exclude_task_id and task.id == exclude_task_id:
            continue
        if normalize_send_content(task.content) == normalized:
            raise HTTPException(400, f"目标「{target_name}」已有相同真实发送任务，已阻止重复排队")

    interval_start = now - timedelta(seconds=REAL_SEND_MIN_INTERVAL_SECONDS)
    last_sent = (
        db.query(SendLog)
        .filter(SendLog.target_name == target_name, SendLog.status == "sent", SendLog.sent_at >= interval_start)
        .order_by(SendLog.sent_at.desc())
        .first()
    )
    if last_sent:
        raise HTTPException(400, f"目标「{target_name}」距离上次发送不足 {REAL_SEND_MIN_INTERVAL_SECONDS} 秒，已阻止")

    duplicate_start = now - timedelta(seconds=REAL_SEND_DUPLICATE_WINDOW_SECONDS)
    recent_logs = (
        db.query(SendLog)
        .filter(SendLog.target_name == target_name, SendLog.status == "sent", SendLog.sent_at >= duplicate_start)
        .all()
    )
    for log in recent_logs:
        task = db.get(SendTask, log.task_id)
        if task and normalize_send_content(task.content) == normalized:
            minutes = max(1, REAL_SEND_DUPLICATE_WINDOW_SECONDS // 60)
            raise HTTPException(400, f"目标「{target_name}」近 {minutes} 分钟已发送相同内容，已阻止")


def device_conversations(dev: Device) -> list[str]:
    try:
        raw = json.loads(dev.conversations or "[]")
    except Exception:
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def validate_device_conversation_scope(dev: Device | None, target_name: str) -> None:
    if not dev:
        raise HTTPException(400, "指定设备不存在")
    if getattr(dev, "allow_any_conversation", False):
        return
    convs = device_conversations(dev)
    if not convs:
        raise HTTPException(400, "指定设备未配置负责会话")
    if target_name not in convs:
        raise HTTPException(400, f"目标「{target_name}」不在设备「{dev.device_id}」负责会话内")


def validate_task_device_binding(db: Session, device_id: str, target_name: str) -> str:
    clean_device_id = (device_id or "").strip()
    if not clean_device_id:
        return ""
    dev = db.query(Device).filter(Device.device_id == clean_device_id).first()
    validate_device_conversation_scope(dev, target_name)
    return clean_device_id


def target_bound_devices(db: Session, target_name: str) -> list[Device]:
    clean_target = (target_name or "").strip()
    if not clean_target:
        return []
    devices = db.query(Device).order_by(Device.device_id).all()
    return [dev for dev in devices if clean_target in device_conversations(dev)]


def resolve_real_send_device_binding(db: Session, device_id: str, target_name: str) -> str:
    clean_device_id = validate_task_device_binding(db, device_id, target_name)
    if clean_device_id:
        return clean_device_id
    devices = target_bound_devices(db, target_name)
    if len(devices) == 1:
        return devices[0].device_id
    clean_target = (target_name or "").strip() or "未填写目标"
    if not devices:
        raise HTTPException(400, f"目标「{clean_target}」没有绑定唯一负责设备，请先在设备监控给对应人员设备配置负责会话")
    names = "、".join(dev.device_id for dev in devices)
    raise HTTPException(400, f"目标「{clean_target}」绑定了多个负责设备（{names}），请明确选择由哪台人员设备发送")


def validate_real_send_device_binding(device_id: str) -> None:
    if not (device_id or "").strip():
        raise HTTPException(400, "企微真实发送必须指定发送设备；每台设备代表一个发送人，不能由系统随机派发")


CLAIM_TIMEOUT_SECONDS = 300        # assigned 超过这个时间没回写就回收成 pending
CONVERSATION_CHECK_FAILURE_COOLDOWN_SECONDS = 600


def device_online(dev: Device, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    return bool(dev.last_heartbeat) and (now - dev.last_heartbeat) <= timedelta(seconds=HEARTBEAT_ONLINE_SECONDS)


def device_ready_for_real_send(dev: Device, now: datetime | None = None) -> bool:
    return bool(dev.allow_real_send and device_online(dev, now) and dev.wecom_ok == "Y" and (dev.outbox_pending_count or 0) == 0)


def device_conversation_check(db: Session, device_id: str, target_name: str) -> DeviceConversationCheck | None:
    clean_device_id = (device_id or "").strip()
    clean_target = (target_name or "").strip()
    if not clean_device_id or not clean_target:
        return None
    return (
        db.query(DeviceConversationCheck)
        .filter(DeviceConversationCheck.device_id == clean_device_id, DeviceConversationCheck.target_name == clean_target)
        .first()
    )


def device_conversation_proof_reason(db: Session, dev: Device, target_name: str, now: datetime | None = None) -> str:
    now = now or datetime.utcnow()
    proof = device_conversation_check(db, dev.device_id, target_name)
    max_age = timedelta(hours=DEVICE_CONVERSATION_PROOF_MAX_AGE_HOURS)
    if not proof:
        return (
            f"设备「{dev.device_id}」最近 {DEVICE_CONVERSATION_PROOF_MAX_AGE_HOURS} 小时没有成功读取目标「{target_name}」的会话记录；"
            "请先在该被控端同步/校验目标会话"
        )
    if proof.status != "ok":
        return f"设备「{dev.device_id}」最近读取目标「{target_name}」失败：{proof.last_error or proof.status}"
    if not proof.verified_at or proof.verified_at < now - max_age:
        return (
            f"设备「{dev.device_id}」对目标「{target_name}」的可读证明已过期"
            f"（最近校验：{timeline_time(proof.verified_at)}），请重新同步/校验"
        )
    empty_title_check = "空会话" in (proof.source or "") or "标题校验" in (proof.source or "")
    if (proof.message_count or 0) <= 0 and not empty_title_check:
        return f"设备「{dev.device_id}」对目标「{target_name}」的最近校验未读到聊天消息，请先确认目标会话可读"
    return ""


def device_conversation_recently_verified(db: Session, dev: Device, target_name: str, now: datetime | None = None) -> bool:
    return not device_conversation_proof_reason(db, dev, target_name, now)


def active_conversation_check_task(db: Session, dev: Device, target_name: str) -> SendTask | None:
    return (
        db.query(SendTask)
        .filter(
            SendTask.device_id == dev.device_id,
            SendTask.target_name == target_name,
            SendTask.scene == CONVERSATION_CHECK_SCENE,
            SendTask.status.in_(["pending", "assigned"]),
        )
        .order_by(SendTask.id.desc())
        .first()
    )


def recent_conversation_check_failure_reason(
    db: Session,
    dev: Device,
    target_name: str,
    now: datetime | None = None,
) -> str:
    now = now or datetime.utcnow()
    cooldown_before = now - timedelta(seconds=CONVERSATION_CHECK_FAILURE_COOLDOWN_SECONDS)
    failed_task = (
        db.query(SendTask)
        .filter(
            SendTask.device_id == dev.device_id,
            SendTask.target_name == target_name,
            SendTask.scene == CONVERSATION_CHECK_SCENE,
            SendTask.status == "failed",
            SendTask.scheduled_at >= cooldown_before,
        )
        .order_by(SendTask.scheduled_at.desc(), SendTask.id.desc())
        .first()
    )
    if failed_task:
        retry_at = timeline_time(failed_task.scheduled_at + timedelta(seconds=CONVERSATION_CHECK_FAILURE_COOLDOWN_SECONDS))
        return f"最近会话只读校验失败，自动补证明冷却至 {retry_at}；可人工复核后手动刷新证明"
    proof = device_conversation_check(db, dev.device_id, target_name)
    if proof and proof.status != "ok" and proof.updated_at and proof.updated_at >= cooldown_before:
        retry_at = timeline_time(proof.updated_at + timedelta(seconds=CONVERSATION_CHECK_FAILURE_COOLDOWN_SECONDS))
        return f"最近会话读取失败：{proof.last_error or proof.status}；自动补证明冷却至 {retry_at}"
    return ""


def build_conversation_check_action(
    db: Session,
    dev: Device,
    target_name: str,
    family_id: str = "",
    now: datetime | None = None,
) -> dict | None:
    clean_target = (target_name or "").strip()
    if not clean_target or (not dev.allow_any_conversation and clean_target not in device_conversations(dev)):
        return None
    proof_reason = device_conversation_proof_reason(db, dev, clean_target, now)
    if not proof_reason:
        return None
    existing_check = active_conversation_check_task(db, dev, clean_target)
    return {
        "action": "queue_conversation_check",
        "label": "刷新会话证明",
        "device_id": dev.device_id,
        "target_name": clean_target,
        "family_id": (family_id or f"WECOM_{clean_target}")[:64],
        "reason": proof_reason,
        "existing_task_id": existing_check.id if existing_check else None,
        "available": existing_check is None,
    }


def device_conversation_proof_summary(db: Session, dev: Device, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    targets = device_conversations(dev)
    issues = []
    ready_targets = []
    for target in targets:
        reason = device_conversation_proof_reason(db, dev, target, now)
        if reason:
            issues.append({"target_name": target, "reason": reason})
        else:
            ready_targets.append(target)
    total = len(targets)
    ready_count = len(ready_targets)
    missing_count = len(issues)
    if total:
        label = f"{ready_count}/{total} 个负责会话24小时内可读"
    else:
        label = "未配置负责会话"
    return {
        "total": total,
        "ready_count": ready_count,
        "missing_count": missing_count,
        "ready_targets": ready_targets,
        "issue_targets": issues,
        "missing_targets": [item["target_name"] for item in issues],
        "coverage": round((ready_count / total) * 100, 2) if total else 0.0,
        "ready": bool(total and missing_count == 0),
        "label": label,
    }


def device_has_inflight_real_send(db: Session, dev: Device, exclude_task_id: int = 0) -> bool:
    query = db.query(SendTask).filter(
        SendTask.device_id == dev.device_id,
        SendTask.send_mode == "real_send",
        SendTask.status == "assigned",
    )
    if exclude_task_id:
        query = query.filter(SendTask.id != exclude_task_id)
    return query.first() is not None


def send_task_readiness(db: Session, task: SendTask, now: datetime | None = None) -> dict:
    """给控制端展示任务能否被指定设备稳定执行，不改变调度结果。"""
    now = now or datetime.utcnow()
    reasons: list[str] = []
    actions: list[dict] = []
    mode = send_log_mode(task)
    status = (task.status or "pending").strip()
    if status == "sent":
        return {"status": "done", "label": "已发送归档", "reasons": [], "actions": []}
    if status == "cancelled":
        return {"status": "done", "label": "已取消", "reasons": [], "actions": []}
    if status == "failed":
        return {"status": "review", "label": "失败待复核", "reasons": [task.last_error or "需要人工复核后重试"], "actions": []}
    if task.next_retry_at and task.next_retry_at > now:
        reasons.append(f"等待同设备自动重试：{timeline_time(task.next_retry_at)}")

    clean_device_id = (task.device_id or "").strip()
    if mode == "real_send" and not clean_device_id:
        reasons.append("真实发送必须先绑定发送设备；设备代表发送人，系统不会随机派发")
    dev = db.query(Device).filter(Device.device_id == clean_device_id).first() if clean_device_id else None
    if clean_device_id and not dev:
        reasons.append(f"绑定设备「{clean_device_id}」不存在")
    if dev:
        if not device_online(dev, now):
            reasons.append(f"设备「{dev.device_id}」不在线或心跳超时")
        if dev.wecom_ok != "Y":
            reasons.append(f"设备「{dev.device_id}」企微状态异常：{dev.wecom_ok or '未知'}")
        if mode == "real_send" and not dev.allow_real_send:
            reasons.append(f"设备「{dev.device_id}」真实发送开关未开启")
            actions.append({
                "action": "enable_real_send",
                "label": "开启设备真发",
                "device_id": dev.device_id,
                "available": True,
                "reason": "该目标已绑定此人员设备，但控制端真实发送开关未开启",
            })
        if mode == "real_send" and (dev.outbox_pending_count or 0) > 0:
            reasons.append(f"设备「{dev.device_id}」还有 {dev.outbox_pending_count} 条发送结果待补传，已暂停领取新真实发送任务")
        if mode == "real_send" and device_has_inflight_real_send(db, dev, exclude_task_id=task.id):
            reasons.append(f"设备「{dev.device_id}」已有真实发送任务执行中，需等待上一条回写后再领取")
        if not dev.allow_any_conversation and task.target_name not in device_conversations(dev):
            reasons.append(f"目标「{task.target_name}」不在设备「{dev.device_id}」负责会话内")
        if mode == "real_send":
            proof_reason = device_conversation_proof_reason(db, dev, task.target_name, now)
            append_reason(reasons, proof_reason)
            append_reason(reasons, recent_conversation_check_failure_reason(db, dev, task.target_name, now))
            proof_action = build_conversation_check_action(db, dev, task.target_name, task.family_id, now)
            if proof_action:
                actions.append(proof_action)
    if status == "assigned" and task.scheduled_at and task.scheduled_at < now - timedelta(seconds=CLAIM_TIMEOUT_SECONDS):
        reasons.append("任务已被该设备领取但超时未回写，等待同设备回收重试")

    if reasons:
        label = "等待设备就绪" if any("等待" in item or "不在线" in item for item in reasons) else "发送前需处理"
        return {"status": "blocked" if mode == "real_send" else "warn", "label": label, "reasons": reasons, "actions": actions}
    if mode == "real_send":
        return {"status": "ready", "label": "真实发送条件就绪", "reasons": [], "actions": []}
    return {"status": "ready", "label": "试运行条件就绪", "reasons": [], "actions": []}


def requeue_stale_assigned_tasks(
    db: Session,
    now: datetime | None = None,
    device_id: str = "",
    request: Request | None = None,
    actor: str = "控制端维护",
) -> int:
    now = now or datetime.utcnow()
    stale_before = now - timedelta(seconds=CLAIM_TIMEOUT_SECONDS)
    query = db.query(SendTask).filter(SendTask.status == "assigned", SendTask.scheduled_at < stale_before)
    if device_id:
        query = query.filter(SendTask.device_id == device_id)
    query = apply_family_id_scope(query, SendTask.family_id, db, request)
    count = 0
    for stale_task in query.limit(100).all():
        before = send_task_snapshot(stale_task)
        if send_log_mode(stale_task) == "real_send":
            stale_task.status = "failed"
            stale_task.next_retry_at = None
            stale_task.last_error = (
                f"设备「{stale_task.device_id or '未绑定'}」领取真实发送任务后超时未回写，发送结果不确定；"
                "为避免重复真实发送，已转人工复核，请先核对目标会话后再手动重试"
            )
            audit_send_task_change(
                db,
                stale_task,
                "real_send_stale_review",
                actor,
                "真实发送 assigned 超时未回写，已转人工复核以避免重复发送",
                before,
            )
            sync_weekly_report_send_status(db, stale_task, "failed")
        else:
            stale_task.status = "pending"
            stale_task.next_retry_at = now
            stale_task.scheduled_at = now
            stale_task.last_error = f"设备「{stale_task.device_id or '未绑定'}」领取后超时未回写，已恢复为原设备待重试"
            audit_send_task_change(db, stale_task, "same_device_requeue", actor, "超时 assigned 任务已恢复为原设备重试", before)
            sync_weekly_report_send_status(db, stale_task, "pending")
        count += 1
    return count


def append_reason(reasons: list[str], detail) -> None:
    text = str(detail or "").strip()
    if text and text not in reasons:
        reasons.append(text)


CONVERSATION_PROOF_PREPARATION_TERMS = (
    "没有成功读取目标",
    "可读证明已过期",
    "最近读取目标",
    "最近校验未读到聊天消息",
    "最近会话读取失败",
    "最近会话只读校验失败",
    "自动补证明冷却",
)


def is_conversation_proof_preparation_reason(reason: str) -> bool:
    text = str(reason or "")
    return any(term in text for term in CONVERSATION_PROOF_PREPARATION_TERMS)


def hard_real_send_readiness_reasons(readiness: dict) -> list[str]:
    return [
        reason
        for reason in readiness.get("reasons", []) or []
        if not is_conversation_proof_preparation_reason(reason)
    ]


def build_send_task_preflight(db: Session, payload: SendTaskPreflightIn, request: Request | None = None) -> dict:
    reasons: list[str] = []
    target_name = (payload.target_name or "").strip()
    content = (payload.content or "").strip()
    device_id = (payload.device_id or "").strip()
    requested_device_id = device_id
    target_devices = target_bound_devices(db, target_name)
    mode = "dry_run"
    try:
        content = validate_send_task_content(content)
    except HTTPException as exc:
        append_reason(reasons, exc.detail)
    try:
        mode = validate_send_mode_submit(payload.send_mode, payload.confirm_real_send)
    except HTTPException as exc:
        append_reason(reasons, exc.detail)
        mode = "real_send" if (payload.send_mode or "").strip() == "real_send" else "dry_run"
    if device_id:
        try:
            ensure_new_task_operation_allowed(request, "assign_device")
        except HTTPException as exc:
            append_reason(reasons, exc.detail)
    dev = db.query(Device).filter(Device.device_id == device_id).first() if device_id else None
    if device_id and not dev:
        append_reason(reasons, f"指定设备「{device_id}」不存在")
    if dev and not dev.allow_any_conversation and target_name not in device_conversations(dev):
        append_reason(reasons, f"目标「{target_name}」不在设备「{dev.device_id}」负责会话内")
    if mode == "real_send":
        try:
            ensure_new_task_operation_allowed(request, "confirm_real_send")
        except HTTPException as exc:
            append_reason(reasons, exc.detail)
        if not device_id:
            if len(target_devices) == 1:
                dev = target_devices[0]
                device_id = dev.device_id
            elif not target_devices:
                append_reason(reasons, f"目标「{target_name or '未填写目标'}」没有绑定唯一负责设备，请先在设备监控给对应人员设备配置负责会话")
            else:
                names = "、".join(item.device_id for item in target_devices)
                append_reason(reasons, f"目标「{target_name}」绑定了多个负责设备（{names}），请明确选择由哪台人员设备发送")
        if content:
            try:
                validate_real_send_risk(db, target_name, content)
            except HTTPException as exc:
                append_reason(reasons, exc.detail)

    task = SendTask(
        family_id=(payload.family_id or "PREFLIGHT")[:64],
        target_name=target_name,
        scene=payload.scene or "预检",
        content=content or payload.content,
        device_id=device_id,
        send_mode=mode,
        status="pending",
    )
    readiness = send_task_readiness(db, task)
    for reason in readiness.get("reasons", []):
        append_reason(reasons, reason)
    conversation_check_hint = build_conversation_check_action(db, dev, target_name, payload.family_id) if mode == "real_send" and dev else None
    auto_prepare_reasons = [reason for reason in reasons if is_conversation_proof_preparation_reason(reason)]
    hard_reasons = [reason for reason in reasons if not is_conversation_proof_preparation_reason(reason)]
    auto_prepare = mode == "real_send" and not hard_reasons and bool(auto_prepare_reasons)
    ok = (not reasons and readiness.get("status") == "ready") or auto_prepare
    if ok and auto_prepare:
        label = "发送预检通过，设备会先自动补齐会话证明"
    elif ok:
        label = "发送预检通过"
    else:
        label = readiness.get("label") or "发送预检未通过"
    return {
        "ok": ok,
        "label": label,
        "reasons": reasons,
        "hard_reasons": hard_reasons,
        "auto_prepare": auto_prepare,
        "auto_prepare_reasons": auto_prepare_reasons,
        "readiness": readiness,
        "conversation_check_hint": conversation_check_hint,
        "send_mode": mode,
        "device_id": device_id,
        "requested_device_id": requested_device_id,
        "resolved_device_id": device_id,
        "target_device_candidates": [
            {
                "device_id": item.device_id,
                "name": item.name,
                "allow_real_send": item.allow_real_send,
                "online": device_online(item),
                "wecom_ok": item.wecom_ok,
            }
            for item in target_devices
        ],
        "target_name": target_name,
    }


def ensure_real_send_readiness(db: Session, task: SendTask) -> None:
    if send_log_mode(task) != "real_send":
        return
    if (task.status or "pending").strip() != "pending":
        return
    readiness = send_task_readiness(db, task)
    hard_reasons = hard_real_send_readiness_reasons(readiness)
    if readiness.get("status") != "ready" and hard_reasons:
        detail = "；".join(hard_reasons) or readiness.get("label") or "真实发送条件未就绪"
        raise HTTPException(400, f"真实发送预检未通过：{detail}")


def weekly_report_send_status(task: SendTask | None) -> str:
    if not task:
        return "not_created"
    if task.status == "pending":
        return "task_created"
    return task.status or "task_created"


def sync_weekly_report_send_status(
    db: Session,
    task: SendTask,
    status: str | None = None,
    sent_at: datetime | None = None,
) -> None:
    reports = db.query(WeeklyReport).filter(WeeklyReport.send_task_id == task.id).all()
    if not reports:
        return
    now = datetime.utcnow()
    next_status = status or weekly_report_send_status(task)
    for report in reports:
        report.send_status = next_status
        if next_status == "sent":
            report.sent_at = sent_at or now
        report.updated_at = now


def ensure_weekly_report_send_task(db: Session, report: WeeklyReport, actor: str) -> tuple[SendTask, bool]:
    if report.status != "approved":
        raise HTTPException(400, "只有已审核周报可以创建发送任务")
    content = validate_send_task_content(report.final_text)
    family = db.query(Family).filter(Family.family_id == report.family_id).first()
    target_name = family.parent_nickname if family else report.family_id
    task = db.get(SendTask, report.send_task_id) if report.send_task_id else None
    if task:
        before = send_task_snapshot(task)
        if task.status == "pending" and task.send_mode == "dry_run":
            task.target_name = target_name
            task.scene = WEEKLY_REPORT_SCENE
            task.content = content
            if changed_fields(before, send_task_snapshot(task)):
                audit_send_task_change(db, task, "update", actor, "同步周报发送任务内容", before)
        report.send_status = weekly_report_send_status(task)
        return task, False

    active_task = (
        db.query(SendTask)
        .filter(
            SendTask.family_id == report.family_id,
            SendTask.scene == WEEKLY_REPORT_SCENE,
            SendTask.content == content,
            SendTask.status.in_(["pending", "assigned"]),
        )
        .order_by(SendTask.id.desc())
        .first()
    )
    if active_task:
        report.send_task_id = active_task.id
        report.send_status = weekly_report_send_status(active_task)
        report.updated_at = datetime.utcnow()
        return active_task, False

    task = SendTask(
        family_id=report.family_id,
        target_name=target_name,
        scene=WEEKLY_REPORT_SCENE,
        content=content,
        send_mode="dry_run",
    )
    add_send_task_with_audit(db, task, "create", actor, f"已审核周报 {report.id} 创建发送任务")
    report.send_task_id = task.id
    report.send_status = "task_created"
    report.sent_at = None
    report.updated_at = datetime.utcnow()
    return task, True


# 兼容多种日期字符串格式，供 RPA 同步时使用。
def parse_rpa_time(value: str) -> datetime:
    if not value:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%m-%d %H:%M", "%H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt.startswith("%m") or fmt.startswith("%H"):
                now = datetime.utcnow()
                parsed = parsed.replace(year=now.year)
                if fmt.startswith("%H"):
                    parsed = parsed.replace(month=now.month, day=now.day)
            return parsed
        except ValueError:
            pass
    return datetime.utcnow()


def record_device_conversation_check(
    db: Session,
    dev: Device | None,
    target_name: str,
    status: str,
    message_count: int = 0,
    source: str = "",
    last_error: str = "",
    verified_at: datetime | None = None,
) -> DeviceConversationCheck | None:
    if not dev or not (target_name or "").strip():
        return None
    now = verified_at or datetime.utcnow()
    clean_target = target_name.strip()
    row = device_conversation_check(db, dev.device_id, clean_target)
    if not row:
        row = DeviceConversationCheck(device_id=dev.device_id, target_name=clean_target)
        db.add(row)
    row.status = status
    row.message_count = max(int(message_count or 0), 0)
    row.source = (source or "")[:80]
    row.last_error = (last_error or "")[:500]
    row.verified_at = now
    row.updated_at = now
    return row


# 系统启动时预置一批默认模板，保证前端初次打开就有可用话术。
def seed_templates(db: Session):
    defaults = [
        ("首联欢迎", "首联欢迎", "{家长称呼}您好，我是本阶段陪跑老师。后续我会围绕上课提醒、打卡反馈和阶段复盘持续跟进孩子学习节奏。", ""),
        ("班会通知", "班会通知", "{家长称呼}您好，本次班会会同步课程节奏、打卡要求和常见问题，建议您预留时间参加，后续我也会把重点发在群里。", ""),
        ("早间打卡提醒", "打卡提醒", "早上好，今天先完成计划里的第一个小任务，完成后记得发打卡哦。", "08:30"),
        ("完成打卡回复", "完成打卡", "收到，今天这个动作完成得不错，继续保持这个节奏。", ""),
        ("未完成打卡回复", "未完成", "收到，今天先不加压，我们把任务拆小一点，晚点完成最关键的一项即可。", ""),
        ("请假回复", "请假/孩子有事", "收到，今天先以孩子状态为主。后续我会帮您把本次内容衔接上。", ""),
        ("请假补课跟进", "请假/补课", "{家长称呼}您好，请假的情况我已记录。后续我会帮孩子确认补学安排，优先保证核心内容不断档。", ""),
        ("资料链接回复", "资料/链接领取", "资料我稍后发您，请优先看标注部分，有问题直接在群里问我。", ""),
        ("课程时间回复", "课程时间询问", "课程时间以群内通知为准，我也会提前提醒您。", ""),
        ("PBL提交提醒", "PBL提交", "{家长称呼}您好，今天是PBL输出节点，建议先让孩子完成基础表达，再补充一个自己的发现。", "19:00"),
        ("PBL点评反馈", "PBL点评", "{家长称呼}您好，我看到了孩子的PBL作品。整体表达已经有雏形，接下来建议补充一个更具体的例子，让展示更完整。", ""),
        ("未完成提醒", "未完成", "{家长称呼}您好，今天如果来不及，可以先补最核心的一项，保持节奏不断就很好。", "20:00"),
        ("效果质疑复核", "效果质疑", "{家长称呼}您好，您的担心我收到。这类情况我会结合孩子近期打卡、课堂表现和输出结果一起复盘，再给您一个更具体的调整建议。", ""),
        ("负面反馈转人工", "转人工", "{家长称呼}您好，您的反馈我已经收到，这类情况我会先和主管/老师确认，再给您明确回复。", ""),
        ("续报意向沟通", "续报", "{家长称呼}您好，孩子这一阶段的学习变化我整理好了，也想和您同步一下后续学习节奏。", ""),
        ("结课总结", "结课", "{家长称呼}您好，本阶段课程已进入收尾，我会整理孩子这段时间的变化、优势和后续建议，方便您判断下一步学习安排。", ""),
    ]
    existing_names = {item.name for item in db.query(Template).all()}
    for name, scene, content, send_time in defaults:
        if name in existing_names:
            continue
        db.add(Template(name=name, scene=scene, content=content, send_time=send_time))
    db.commit()


# 把 Agent 结果写入 ai_outputs 表，作为后续人工审核入口。
def save_ai_output(db: Session, family_id: str, agent_type: str, source: str, result: dict) -> AIOutput:
    safety = ai_safety_findings(json.dumps(result.get("raw", {}), ensure_ascii=False), result.get("display_text", ""))
    output = AIOutput(
        family_id=family_id,
        agent_type=agent_type,
        source=source,
        raw_json=json.dumps(result["raw"], ensure_ascii=False, indent=2),
        evidence_json=json.dumps(build_ai_evidence(db, family_id, result), ensure_ascii=False, indent=2),
        display_text=result["display_text"],
        edited_output=result["display_text"],
        status="needs_review",
        risk_level=result["risk_level"],
        need_human_review="Y" if result["need_human_review"] or safety["requires_manual"] else "N",
        suggested_actions="、".join(result["suggested_actions"]),
    )
    db.add(output)
    return output


def build_ai_evidence(db: Session, family_id: str, result: dict, limit: int = 6) -> dict:
    raw = result.get("raw") or {}
    evidence_summary = raw.get("使用依据摘要") or raw.get("evidence") or raw.get("依据") or []
    if not isinstance(evidence_summary, list):
        evidence_summary = [str(evidence_summary)] if evidence_summary else []
    messages = (
        db.query(RawMessage)
        .filter(RawMessage.family_id == family_id)
        .order_by(RawMessage.message_time.desc(), RawMessage.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "family_id": family_id,
        "evidence_summary": [str(item) for item in evidence_summary if str(item).strip()],
        "source_messages": [
            {
                "message_id": msg.id,
                "message_time": timeline_time(msg.message_time),
                "speaker": msg.speaker,
                "content": msg.content,
                "source": msg.source,
                "checkin_status": msg.checkin_status,
            }
            for msg in sorted(messages, key=lambda item: item.message_time)
        ],
    }


def join_agent_field(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    return str(value)


def upsert_parent_profile_from_agent(db: Session, family_id: str, result: dict):
    raw = result["raw"]
    profile_data = {
        "family_id": family_id,
        "trust_level": "B" if raw.get("风险等级") == "低" else "C",
        "trust_trend": "稳定" if raw.get("风险等级") != "高" else "下降",
        "pain_points": join_agent_field(raw.get("家长关注点")),
        "communication_style": raw.get("沟通风格", ""),
        "satisfaction_level": join_agent_field(raw.get("满意度评级")) or "未知",
        "child_summary": raw.get("学生状态", ""),
        "service_risks": join_agent_field(raw.get("风险信号")),
        "renewal_intent": join_agent_field(raw.get("续报意向")) or "未知",
        "evidence": join_agent_field(raw.get("使用依据摘要")),
        "suggested_actions": join_agent_field(raw.get("建议跟进动作") or raw.get("推荐下一步动作")),
    }
    profile = db.query(ParentProfile).filter(ParentProfile.family_id == family_id).one_or_none()
    if profile:
        for key, value in profile_data.items():
            setattr(profile, key, value)
        profile.updated_at = datetime.utcnow()
    else:
        db.add(ParentProfile(**profile_data))


def create_weekly_report_from_agent(db: Session, family_id: str, result: dict):
    raw = result["raw"]
    db.add(
        WeeklyReport(
            family_id=family_id,
            week_label=raw.get("period") or raw.get("week") or datetime.utcnow().strftime("%Y-W%U"),
            status="needs_review",
            overall_state=raw.get("本周学习总结", ""),
            main_changes=join_agent_field(raw.get("学习亮点")),
            parent_focus=join_agent_field(raw.get("需要关注")),
            teacher_suggestion=join_agent_field(raw.get("下周建议")),
            next_followup=raw.get("风险提示", ""),
            final_text=result["display_text"],
        )
    )


def create_checkin_records_from_context(db: Session, context: dict) -> int:
    created = 0
    for msg in context["messages"]:
        if not msg.checkin_status:
            continue
        exists = db.query(CheckinRecord).filter(CheckinRecord.message_id == msg.id).first()
        if exists:
            continue
        db.add(CheckinRecord(family_id=msg.family_id, message_id=msg.id, checkin_type=msg.checkin_status, evidence=msg.content))
        created += 1
    return created


# 检查家庭是否存在，不存在就直接返回 404。
def require_family(db: Session, family_id: str) -> Family:
    family = db.query(Family).filter(Family.family_id == family_id).one_or_none()
    if not family:
        raise HTTPException(404, "家庭不存在")
    return family


def require_family_for_request(db: Session, family_id: str, request: Request | None) -> Family:
    family = require_family(db, family_id)
    ensure_family_access(family, request)
    return family


FOLLOWUP_TYPES = {"电话", "私信", "群提醒", "周报", "补课", "投诉", "续报沟通"}
FOLLOWUP_STATUSES = {"待跟进", "已完成", "需升级"}


def clean_followup_payload(payload: FollowupIn) -> dict:
    followup_type = payload.followup_type.strip()
    status = payload.status.strip()
    content = payload.content.strip()
    if followup_type not in FOLLOWUP_TYPES:
        raise HTTPException(400, "跟进类型不合法")
    if status not in FOLLOWUP_STATUSES:
        raise HTTPException(400, "跟进状态不合法")
    if not content:
        raise HTTPException(400, "跟进内容不能为空")
    return {
        "followup_type": followup_type,
        "status": status,
        "content": content,
        "result": payload.result.strip(),
        "next_action": payload.next_action.strip(),
        "owner": payload.owner.strip(),
        "occurred_at": payload.occurred_at or datetime.utcnow(),
    }


def account_payload(account: UserAccount) -> dict:
    return {key: value for key, value in as_dict(account).items() if key != "password"}


def admin_account_payload(account: UserAccount) -> dict:
    data = account_payload(account)
    if account.role in ADMIN_ROLES:
        data["admin_token"] = sign_admin_token(account.username, account.role, account.display_name, admin_auth_secret(), campus_names=account.campus_names)
    if account.role == "parent" and account.family_id:
        data["parent_token"] = sign_parent_token(account.username, account.display_name, account.family_id, admin_auth_secret())
    return data


def update_own_account_profile(db: Session, account: UserAccount, payload: AccountProfileUpdateIn) -> dict:
    new_username = payload.username.strip() or account.username
    new_display_name = payload.display_name.strip() or account.display_name or new_username
    new_password = payload.new_password.strip()
    changing_username = new_username != account.username
    changing_password = bool(new_password)

    if changing_username or changing_password:
        if not payload.current_password:
            raise HTTPException(400, "修改账号或密码前请输入当前密码")
        if not verify_account_password(db, account, payload.current_password):
            raise HTTPException(401, "当前密码不正确")
    if changing_username:
        exists = (
            db.query(UserAccount)
            .filter(UserAccount.username == new_username, UserAccount.id != account.id)
            .one_or_none()
        )
        if exists:
            raise HTTPException(400, "账号已存在")
        account.username = new_username
    account.display_name = new_display_name
    if changing_password:
        account.password = hash_password(new_password)
    db.commit()
    db.refresh(account)
    return admin_account_payload(account)


PASSWORD_HASH_PREFIX = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    salt = secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()
    return f"{PASSWORD_HASH_PREFIX}${salt}${digest}"


def verify_password(stored_password: str, password: str) -> bool:
    stored = stored_password or ""
    if stored.startswith(f"{PASSWORD_HASH_PREFIX}$"):
        try:
            _, salt, digest = stored.split("$", 2)
        except ValueError:
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()
        return hmac.compare_digest(candidate, digest)
    return hmac.compare_digest(stored, password)


def password_needs_upgrade(stored_password: str) -> bool:
    return not (stored_password or "").startswith(f"{PASSWORD_HASH_PREFIX}$")


def verify_account_password(db: Session, account: UserAccount | None, password: str) -> bool:
    if not account or not verify_password(account.password, password):
        return False
    if password_needs_upgrade(account.password):
        account.password = hash_password(password)
        db.flush()
    return True


def admin_account_count(db: Session) -> int:
    return db.query(UserAccount).filter(UserAccount.role == "admin").count()


def control_account_count(db: Session) -> int:
    return db.query(UserAccount).filter(UserAccount.role.in_(ADMIN_ROLES)).count()


def admin_auth_status_payload(db: Session) -> dict:
    total = admin_account_count(db)
    return {
        "auth_required": admin_auth_required(),
        "bootstrap_required": total == 0,
        "admin_account_count": total,
        "control_account_count": control_account_count(db),
        "roles": ["admin", "coach", "readonly"],
        "default_first_role": "admin",
        "message": "首次注册账号将自动成为超管" if total == 0 else "已有超管，后续账号需超管登录后创建",
    }


def ensure_family(db: Session, family_id: str, parent_name: str, child_grade: str = "", coach_name: str = "", campus_name: str = "") -> Family:
    family = db.query(Family).filter(Family.family_id == family_id).one_or_none()
    if family:
        if parent_name:
            family.parent_nickname = parent_name
        if child_grade:
            family.child_grade = child_grade
        if campus_name:
            family.campus_name = campus_name
        if coach_name:
            family.coach_name = coach_name
        return family
    family = Family(
        family_id=family_id,
        parent_nickname=parent_name,
        child_grade=child_grade,
        campus_name=campus_name,
        coach_name=coach_name,
        service_status="会话工作台",
    )
    db.add(family)
    db.flush()
    return family


def ensure_account(db: Session, username: str, password: str, display_name: str, role: str, family_id: str = "", campus_names: str = "") -> UserAccount:
    account = db.query(UserAccount).filter(UserAccount.username == username).one_or_none()
    hashed = hash_password(password)
    if account:
        account.password = hashed
        account.display_name = display_name
        account.role = role
        account.family_id = family_id
        account.campus_names = campus_names
        return account
    account = UserAccount(username=username, password=hashed, display_name=display_name, role=role, family_id=family_id, campus_names=campus_names)
    db.add(account)
    db.flush()
    return account


def seed_bootstrap_admin(db: Session) -> None:
    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if admin_auth_required():
        admin_auth_secret()
    if username and password:
        ensure_account(db, username, password, os.getenv("ADMIN_DISPLAY_NAME", "系统管理员"), "admin", campus_names=os.getenv("ADMIN_CAMPUS_NAMES", ""))


def add_chat_message(db: Session, family_id: str, speaker: str, content: str, minutes_offset: int = 0) -> RawMessage:
    msg = RawMessage(
        family_id=family_id,
        message_time=datetime.utcnow(),
        speaker=speaker,
        content=content,
        source="会话工作台",
        checkin_status=detect_checkin(content),
        is_effective="Y" if len(content.strip()) >= 2 else "N",
    )
    db.add(msg)
    return msg


def latest_parent_message(context: dict) -> str:
    family = context.get("family")
    coach_name = family.coach_name if family else ""
    for msg in reversed(context["messages"]):
        speaker = msg.speaker or ""
        if speaker != coach_name and "老师" not in speaker and speaker not in {"我", "陪跑师"}:
            return msg.content
    return context["messages"][-1].content


def latest_effective_parent_message(context: dict) -> str:
    family = context.get("family")
    coach_name = family.coach_name if family else ""
    for msg in reversed(context["messages"]):
        content = (msg.content or "").strip()
        if not content or msg.is_effective == "N":
            continue
        speaker = (msg.speaker or "").strip()
        if speaker != coach_name and "老师" not in speaker and speaker not in {"我", "陪跑师"}:
            return content
    return ""


def has_recent_pending_reply_draft(db: Session, family_id: str, hours: int) -> bool:
    if hours <= 0:
        return False
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    return (
        db.query(AIOutput)
        .filter(
            AIOutput.family_id == family_id,
            AIOutput.agent_type == "ai_reply",
            AIOutput.status == "needs_review",
            AIOutput.created_at >= cutoff,
        )
        .first()
        is not None
    )


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _wecom_archive_poll_loop(interval_seconds: int) -> None:
    time.sleep(5)
    while True:
        db = next(get_db())
        try:
            payload = WecomArchiveSyncIn(
                auto_generate_reply=_env_bool("WECOM_ARCHIVE_AUTO_REPLY", False),
                auto_create_reply_task=_env_bool("WECOM_ARCHIVE_AUTO_CREATE_TASK", False),
                auto_generate_all_agents=False,
            )
            result = sync_wecom_archive(payload, request=None, db=db)
            print(
                "wecom_archive_poll_ok "
                f"pulled={result.get('pulled', 0)} normalized={result.get('normalized', 0)} seq={result.get('seq', 0)}"
            )
        except Exception as exc:
            print(f"wecom_archive_poll_failed detail={exc}")
        finally:
            db.close()
        time.sleep(interval_seconds)


def start_wecom_archive_poller() -> None:
    global _wecom_archive_poller_started
    if _wecom_archive_poller_started or not _env_bool("WECOM_ARCHIVE_POLL_ENABLED", False):
        return
    if not wecom_archive_config_status(read_wecom_archive_config()).get("configured"):
        return
    interval = max(_env_int("WECOM_ARCHIVE_POLL_INTERVAL_SECONDS", 60), 10)
    thread = threading.Thread(target=_wecom_archive_poll_loop, args=(interval,), daemon=True)
    thread.start()
    _wecom_archive_poller_started = True
    print(f"wecom_archive_poller_started interval={interval}s")


# 启动时初始化数据库并预置模板。
@app.on_event("startup")
def on_startup():
    assert_runtime_config_safe(current_runtime_config_report())
    init_db()
    db = next(get_db())
    try:
        seed_bootstrap_admin(db)
        seed_templates(db)
        db.commit()
    finally:
        db.close()
    start_wecom_archive_poller()


    # 首页返回静态前端页面。
@app.get("/")
def index():
    # 首页动态注入静态资源版本号(内容hash)，实现自动缓存失效。
    import hashlib
    from fastapi.responses import HTMLResponse
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    _h = hashlib.md5()
    for _n in ("security.js", "app.js", "style.css"):
        _p = STATIC / _n
        if _p.exists():
            _h.update(_p.read_bytes())
    _v = _h.hexdigest()[:8]
    html = (
        html.replace("/static/security.js", "/static/security.js?v=" + _v)
        .replace("/static/app.js", "/static/app.js?v=" + _v)
        .replace("/static/style.css", "/static/style.css?v=" + _v)
    )
    return HTMLResponse(
        html,
        headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
        },
    )


# 健康检查接口给 RPA 和前端都可以用。
@app.get("/health")
def health():
    report = current_runtime_config_report()
    return {
        "ok": report["status"] != "critical",
        "mode": report["metrics"]["app_env"],
        "database": report["metrics"]["database_kind"],
        "config_status": report["status"],
    }


@app.post("/api/test-chat/register")
def register_account(payload: AccountIn, db: Session = Depends(get_db)):
    username = payload.username.strip()
    if not username or not payload.password:
        raise HTTPException(400, "账号和密码不能为空")
    exists = db.query(UserAccount).filter(UserAccount.username == username).one_or_none()
    if exists:
        raise HTTPException(400, "账号已存在")
    role = payload.role if payload.role in {"coach", "parent"} else "parent"
    family_id = payload.family_id.strip()
    if role == "parent" and not family_id:
        family_id = f"WEB_{username}"
        ensure_family(db, family_id, payload.display_name or username)
    account = UserAccount(
        username=username,
        password=hash_password(payload.password),
        display_name=payload.display_name or username,
        role=role,
        campus_names=",".join(normalize_campus_names(payload.campus_names)),
        family_id=family_id,
    )
    db.add(account)
    db.commit()
    return account_payload(account)


@app.post("/api/test-chat/login")
def login_account(payload: LoginIn, db: Session = Depends(get_db)):
    if os.getenv("APP_ENV", "").strip().lower() in DEPLOYED_ENVS and admin_auth_required():
        raise HTTPException(404, "测试登录已在部署环境禁用")
    account = db.query(UserAccount).filter(UserAccount.username == payload.username.strip()).one_or_none()
    if not verify_account_password(db, account, payload.password):
        raise HTTPException(401, "账号或密码错误")
    db.commit()
    return admin_account_payload(account)


@app.get("/api/admin/auth/status")
def admin_auth_status(db: Session = Depends(get_db)):
    return admin_auth_status_payload(db)


@app.post("/api/admin/auth/register")
def admin_register(payload: AccountIn, authorization: str = Header(""), db: Session = Depends(get_db)):
    username = payload.username.strip()
    if not username or not payload.password:
        raise HTTPException(400, "账号和密码不能为空")
    if db.query(UserAccount).filter(UserAccount.username == username).one_or_none():
        raise HTTPException(400, "账号已存在")

    is_first_admin = admin_account_count(db) == 0
    if is_first_admin:
        role = "admin"
    else:
        try:
            identity = verify_admin_token(bearer_token(authorization), admin_auth_secret())
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(401, "已有超管账号，请先用超管登录后再创建新账号") from exc
        if identity.role != "admin":
            raise HTTPException(403, "只有超管可以创建控制端账号")
        role = payload.role if payload.role in ADMIN_ROLES else "coach"

    account = UserAccount(
        username=username,
        password=hash_password(payload.password),
        display_name=payload.display_name or username,
        role=role,
        campus_names=",".join(normalize_campus_names(payload.campus_names)),
        family_id="",
    )
    db.add(account)
    db.commit()
    return admin_account_payload(account)


@app.post("/api/admin/auth/login")
def admin_login(payload: LoginIn, db: Session = Depends(get_db)):
    account = db.query(UserAccount).filter(UserAccount.username == payload.username.strip()).one_or_none()
    if not account or account.role not in ADMIN_ROLES or not verify_account_password(db, account, payload.password):
        raise HTTPException(401, "管理端账号或密码错误")
    db.commit()
    return admin_account_payload(account)


@app.get("/api/admin/auth/me")
def admin_me(authorization: str = Header("")):
    try:
        identity = verify_admin_token(bearer_token(authorization), admin_auth_secret())
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(401, str(exc)) from exc
    return {
        "username": identity.username,
        "role": identity.role,
        "display_name": identity.display_name,
        "campus_names": list(identity.campus_names),
        "expires_at": datetime.utcfromtimestamp(identity.exp).isoformat(sep=" ", timespec="seconds"),
    }


@app.put("/api/admin/auth/me")
def update_admin_me(payload: AccountProfileUpdateIn, authorization: str = Header(""), db: Session = Depends(get_db)):
    try:
        identity = verify_admin_token(bearer_token(authorization), admin_auth_secret())
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(401, str(exc)) from exc
    account = (
        db.query(UserAccount)
        .filter(UserAccount.username == identity.username, UserAccount.role.in_(ADMIN_ROLES))
        .one_or_none()
    )
    if not account:
        raise HTTPException(404, "账号不存在")
    return update_own_account_profile(db, account, payload)


def parent_dashboard_payload(db: Session, family: Family) -> dict:
    latest_report = (
        db.query(WeeklyReport)
        .filter(WeeklyReport.family_id == family.family_id, WeeklyReport.status == "approved")
        .order_by(WeeklyReport.updated_at.desc(), WeeklyReport.id.desc())
        .first()
    )
    profile = db.query(ParentProfile).filter(ParentProfile.family_id == family.family_id).one_or_none()
    recent_messages = (
        db.query(RawMessage)
        .filter(RawMessage.family_id == family.family_id)
        .order_by(RawMessage.message_time.desc())
        .limit(8)
        .all()
    )
    latest_message = recent_messages[0] if recent_messages else None
    checkin_count = db.query(RawMessage).filter(RawMessage.family_id == family.family_id, RawMessage.checkin_status != "").count()
    return {
        "family": {
            "family_id": family.family_id,
            "parent_nickname": family.parent_nickname,
            "child_grade": family.child_grade,
            "campus_name": family.campus_name,
            "coach_name": family.coach_name,
            "course_stage": family.course_stage,
            "unit_progress": family.unit_progress,
            "pbl_count": family.pbl_count,
            "checkin_rate": family.checkin_rate,
            "next_milestone": family.next_milestone,
            "service_status": family.service_status,
        },
        "progress": {
            "message_count": db.query(RawMessage).filter(RawMessage.family_id == family.family_id).count(),
            "checkin_count": checkin_count,
            "latest_message_at": timeline_time(latest_message.message_time) if latest_message else "",
            "weekly_report_status": latest_report.send_status if latest_report else "not_ready",
            "suggested_next_action": family.next_milestone or (latest_report.teacher_suggestion if latest_report else ""),
        },
        "profile": {
            "child_summary": profile.child_summary if profile else "",
            "communication_style": profile.communication_style if profile else "",
            "satisfaction_level": profile.satisfaction_level if profile else "未知",
            "suggested_actions": profile.suggested_actions if profile else "",
        },
        "weekly_report": as_dict(latest_report) if latest_report else None,
        "recent_messages": [as_dict(item) for item in reversed(recent_messages)],
    }


def require_parent_account(db: Session, authorization: str):
    try:
        identity = verify_parent_token(bearer_token(authorization), admin_auth_secret())
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(401, str(exc)) from exc
    account = db.query(UserAccount).filter(
        UserAccount.username == identity.username,
        UserAccount.role == "parent",
        UserAccount.family_id == identity.family_id,
    ).one_or_none()
    if not account:
        raise HTTPException(401, "家长端账号已失效，请重新登录")
    return account, identity


@app.put("/api/parent/auth/me")
def update_parent_me(payload: AccountProfileUpdateIn, authorization: str = Header(""), db: Session = Depends(get_db)):
    account, _ = require_parent_account(db, authorization)
    return update_own_account_profile(db, account, payload)


def sync_parent_report_feedback(db: Session, report: WeeklyReport, score: int, note: str, actor: str) -> bool:
    if score < 1 or score > 5:
        raise HTTPException(400, "反馈评分必须在 1 到 5 分之间")
    clean_note = note.strip()[:300]
    report.parent_feedback_score = score
    report.parent_feedback_note = clean_note
    report.parent_feedback_at = datetime.utcnow()

    profile = db.query(ParentProfile).filter(ParentProfile.family_id == report.family_id).one_or_none()
    if not profile:
        profile = ParentProfile(family_id=report.family_id)
        db.add(profile)
    profile.satisfaction_level = "低" if score <= 2 else "中" if score == 3 else "高"
    if score <= 2:
        signal = f"家长周报反馈低分：{score}分"
        if signal not in (profile.service_risks or ""):
            profile.service_risks = "；".join([item for item in [profile.service_risks, signal] if item])
        action = "主管或陪跑师需复核周报反馈并跟进家长"
        if action not in (profile.suggested_actions or ""):
            profile.suggested_actions = "；".join([item for item in [profile.suggested_actions, action] if item])

    followup = (
        db.query(FollowupRecord)
        .filter(
            FollowupRecord.family_id == report.family_id,
            FollowupRecord.followup_type == "周报",
            FollowupRecord.content.contains(f"周报#{report.id}"),
        )
        .order_by(FollowupRecord.id.desc())
        .first()
    )
    if score <= 2:
        content = f"家长周报反馈低分：周报#{report.id}，评分 {score}/5。{clean_note}"
        if followup:
            followup.content = content
            followup.status = "需升级"
            followup.next_action = "24小时内人工回访，确认周报内容或服务体验问题"
            followup.created_by = actor
        else:
            db.add(
                FollowupRecord(
                    family_id=report.family_id,
                    followup_type="周报",
                    content=content,
                    status="需升级",
                    next_action="24小时内人工回访，确认周报内容或服务体验问题",
                    created_by=actor,
                )
            )
            return True
    elif followup:
        followup.status = "已完成"
        followup.result = f"家长周报反馈已恢复为 {score}/5"
    return False


@app.get("/api/parent/dashboard")
def parent_dashboard(authorization: str = Header(""), db: Session = Depends(get_db)):
    account, identity = require_parent_account(db, authorization)
    return parent_dashboard_payload(db, require_family(db, identity.family_id))


@app.post("/api/parent/reports/{report_id}/ack")
def parent_ack_report(report_id: int, payload: ParentReportAckIn | None = None, authorization: str = Header(""), db: Session = Depends(get_db)):
    account, identity = require_parent_account(db, authorization)
    report = db.get(WeeklyReport, report_id)
    if not report or report.family_id != identity.family_id or report.status != "approved":
        raise HTTPException(404, "可签收周报不存在")
    report.parent_ack_at = report.parent_ack_at or datetime.utcnow()
    note = (payload or ParentReportAckIn()).note.strip()
    report.parent_ack_note = note[:200]
    db.commit()
    return {"report": as_dict(report), "ack_by": account.display_name or account.username}


@app.post("/api/parent/reports/{report_id}/feedback")
def parent_feedback_report(report_id: int, payload: ParentReportFeedbackIn, authorization: str = Header(""), db: Session = Depends(get_db)):
    account, identity = require_parent_account(db, authorization)
    report = db.get(WeeklyReport, report_id)
    if not report or report.family_id != identity.family_id or report.status != "approved":
        raise HTTPException(404, "可反馈周报不存在")
    created_followup = sync_parent_report_feedback(db, report, payload.score, payload.note, account.display_name or account.username)
    db.commit()
    return {"report": as_dict(report), "followup_created": created_followup}


@app.get("/api/test-chat/accounts")
def list_accounts(db: Session = Depends(get_db)):
    return [account_payload(item) for item in db.query(UserAccount).order_by(UserAccount.role, UserAccount.username).all()]


@app.get("/api/conversations")
@app.get("/api/test-chat/conversations")
def list_test_conversations(request: Request = None, db: Session = Depends(get_db)):
    rows = []
    for family in scoped_family_query(db, request).order_by(Family.family_id).all():
        last = db.query(RawMessage).filter(RawMessage.family_id == family.family_id).order_by(RawMessage.message_time.desc()).first()
        rows.append(
            {
                **as_dict(family),
                "message_count": db.query(RawMessage).filter(RawMessage.family_id == family.family_id).count(),
                "last_message": last.content if last else "",
                "last_speaker": last.speaker if last else "",
            }
        )
    return rows


def conversation_send_device_view(db: Session, dev: Device) -> dict:
    proof = device_conversation_proof_summary(db, dev)
    return {
        "device_id": dev.device_id,
        "name": dev.name,
        "online": device_online(dev),
        "wecom_ok": dev.wecom_ok,
        "allow_real_send": dev.allow_real_send,
        "allow_any_conversation": dev.allow_any_conversation,
        "outbox_pending_count": dev.outbox_pending_count or 0,
        "conversation_list": device_conversations(dev),
        "conversation_count": proof["total"],
        "conversation_proof_count": proof["ready_count"],
        "conversation_proof_total": proof["total"],
        "conversation_proof_missing_count": proof["missing_count"],
        "conversation_proof_label": proof["label"],
        "conversation_proof_ready": proof["ready"],
        "conversation_proof_missing_targets": proof["missing_targets"],
    }


@app.get("/api/conversations/send-devices")
def list_conversation_send_devices(request: Request = None, db: Session = Depends(get_db)):
    role = operation_role_from_request(request)
    if role not in {"admin", "coach"}:
        return []
    target_scope = None
    if role == "coach":
        target_scope = {
            (family.parent_nickname or "").strip()
            for family in scoped_family_query(db, request).all()
            if (family.parent_nickname or "").strip()
        }
    rows = []
    for dev in db.query(Device).order_by(Device.device_id).all():
        conversations = set(device_conversations(dev))
        if target_scope is not None and not conversations.intersection(target_scope):
            continue
        rows.append(conversation_send_device_view(db, dev))
    return rows


@app.get("/api/conversations/{family_id}/messages")
@app.get("/api/test-chat/messages/{family_id}")
def list_chat_messages(family_id: str, request: Request = None, db: Session = Depends(get_db)):
    require_family_for_request(db, family_id, request)
    rows = db.query(RawMessage).filter(RawMessage.family_id == family_id).order_by(RawMessage.message_time).all()
    return [as_dict(item) for item in rows]


@app.post("/api/conversations/messages")
@app.post("/api/test-chat/messages")
def send_chat_message(payload: ChatMessageIn, request: Request = None, db: Session = Depends(get_db)):
    family = require_family_for_request(db, payload.family_id, request)
    account = db.query(UserAccount).filter(UserAccount.username == payload.username.strip()).one_or_none()
    if not account:
        raise HTTPException(404, "账号不存在")
    content = payload.content.strip()
    if not content:
        raise HTTPException(400, "消息不能为空")
    speaker = account.display_name or account.username
    msg = add_chat_message(db, family.family_id, speaker, content)
    family.service_status = "网页通讯中"
    db.commit()
    return as_dict(msg)


@app.post("/api/conversations/{family_id}/direct-send")
def direct_send_conversation_message(family_id: str, payload: ConversationDirectSendIn, request: Request = None, db: Session = Depends(get_db)):
    """会话工作台人工输入直发：不生成 AI 审核草稿，直接进入对应设备真实发送队列。"""
    ensure_conversation_direct_send_allowed(request)
    family = require_family_for_request(db, family_id, request)
    target_name = (family.parent_nickname or "").strip()
    if not target_name:
        raise HTTPException(400, "当前会话没有企微目标名，无法直接发送")
    content = validate_send_task_content(payload.content)
    device_id = resolve_real_send_device_binding(db, payload.device_id, target_name)
    validate_real_send_device_binding(device_id)
    validate_real_send_risk(db, target_name, content)
    task = SendTask(
        family_id=family.family_id,
        target_name=target_name,
        scene="会话工作台人工直发",
        content=content,
        device_id=device_id,
        send_mode="real_send",
        status="pending",
        scheduled_at=datetime.utcnow(),
    )
    ensure_real_send_readiness(db, task)
    add_send_task_with_audit(
        db,
        task,
        "direct_real_send",
        actor_from_request(request),
        "会话工作台人工输入，跳过审核直接加入企微真实发送队列",
    )
    db.commit()
    return {
        "task": send_task_view(task, request, db),
        "family_id": family.family_id,
        "target_name": target_name,
        "device_id": device_id,
        "note": "已直接加入企微真实发送队列；发送成功后由被控端回读确认并落库。",
    }


@app.post("/api/test-chat/seed")
def seed_test_chat(db: Session = Depends(get_db)):
    raise HTTPException(410, "mock 会话生成功能已移除，请使用真实企微同步或导入真实聊天记录")


@app.post("/api/test-chat/ai")
def generate_test_chat_ai(payload: ChatAiIn, request: Request = None, db: Session = Depends(get_db)):
    started = perf_counter()
    family = require_family_for_request(db, payload.family_id, request)
    context = build_agent_context(db, family.family_id)
    if not context["messages"]:
        raise HTTPException(404, "该会话还没有消息")

    profile_result = run_family_profile_agent_service(context)
    profile_output = save_ai_output(db, family.family_id, "family_profile", "会话工作台", profile_result)
    upsert_parent_profile_from_agent(db, family.family_id, profile_result)

    latest = latest_parent_message(context)
    reply_result = run_reply_agent_service(context, latest, "standard")
    reply_output = save_ai_output(db, family.family_id, "ai_reply", "会话工作台", reply_result)
    task = None
    if payload.create_task:
        task = SendTask(
            family_id=family.family_id,
            target_name=family.parent_nickname,
            scene=reply_result["raw"].get("场景类型", "网页AI回复"),
            content=reply_result["display_text"],
            status="pending",
        )
        reply_output.status = "task_created"
        add_send_task_with_audit(db, task, "create", actor_from_request(request), "网页通讯 AI 生成回复任务")

    db.commit()
    return {
        "family_id": family.family_id,
        "profile_output": as_dict(profile_output),
        "reply_output": as_dict(reply_output),
        "send_task": as_dict(task) if task else None,
        "elapsed_ms": int((perf_counter() - started) * 1000),
    }


@app.post("/api/test-chat/reply")
def generate_test_chat_reply(payload: ChatReplyIn, request: Request = None, db: Session = Depends(get_db)):
    started = perf_counter()
    family = require_family_for_request(db, payload.family_id, request)
    context = build_agent_context(db, family.family_id)
    if not context["messages"]:
        raise HTTPException(404, "该会话还没有消息")

    latest = payload.message.strip() or latest_parent_message(context)
    reply_result = run_quick_reply_agent_service(context, latest, payload.tone)
    reply_output = save_ai_output(db, family.family_id, "ai_reply", "网页通讯快速回复", reply_result)
    task = None
    if payload.create_task:
        task = SendTask(
            family_id=family.family_id,
            target_name=family.parent_nickname,
            scene=reply_result["raw"].get("场景类型", "网页AI回复"),
            content=reply_result["display_text"],
            status="pending",
        )
        reply_output.status = "task_created"
        add_send_task_with_audit(db, task, "create", actor_from_request(request), "网页通讯快速回复生成任务")

    db.commit()
    return {
        "family_id": family.family_id,
        "reply_output": as_dict(reply_output),
        "send_task": as_dict(task) if task else None,
        "elapsed_ms": int((perf_counter() - started) * 1000),
    }


# 导入文件接口，支持 CSV / XLSX 上传。
@app.post("/api/import")
async def import_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    rows = rows_from_upload(file.filename or "upload.csv", data)
    return import_rows(db, rows)


@app.get("/api/import/templates")
def import_templates():
    return list_import_templates()


@app.get("/api/import/templates/{template_key}/csv")
def download_import_template(template_key: str):
    try:
        data = import_template_csv_bytes(template_key)
    except KeyError as exc:
        raise HTTPException(404, "导入模板不存在") from exc
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={template_key}.csv"},
    )


# mock 样例数据入口已永久关闭，生产只允许真实企微同步或真实导入。
@app.post("/api/sample-data")
def load_sample_data(db: Session = Depends(get_db)):
    raise HTTPException(410, "mock 样例导入已移除，请导入真实数据或使用企业微信同步")


# 家庭列表接口，顺带补充每个家庭的消息数。
@app.get("/api/families")
def list_families(campus_name: str = "", request: Request = None, db: Session = Depends(get_db)):
    query = scoped_family_query(db, request)
    clean_campus = (campus_name or "").strip()
    if clean_campus:
        query = query.filter(Family.campus_name == clean_campus)
    families = query.order_by(Family.family_id).all()
    rows = [{**as_dict(f), "message_count": db.query(RawMessage).filter(RawMessage.family_id == f.family_id).count()} for f in families]
    return maybe_redact_for_request(rows, request)


@app.post("/api/families")
def upsert_family(payload: FamilyIn, request: Request = None, db: Session = Depends(get_db)):
    target_name = payload.parent_nickname.strip()
    if not target_name:
        raise HTTPException(400, "企微会话名不能为空")
    family_id = payload.family_id.strip() or f"WECOM_{target_name}"
    coach_name = scoped_payload_coach_name(request, payload.coach_name)
    family = db.query(Family).filter(Family.family_id == family_id).one_or_none()
    if not family:
        family = db.query(Family).filter(Family.parent_nickname == target_name).one_or_none()
    if family:
        ensure_family_access(family, request)
        family.family_id = family.family_id or family_id
        family.parent_nickname = target_name
        family.child_grade = payload.child_grade
        if payload.course_stage:
            family.course_stage = payload.course_stage
        if payload.unit_progress:
            family.unit_progress = payload.unit_progress
        if payload.pbl_count is not None:
            family.pbl_count = payload.pbl_count
        if payload.checkin_rate:
            family.checkin_rate = payload.checkin_rate
        if payload.next_milestone:
            family.next_milestone = payload.next_milestone
        if payload.campus_name:
            family.campus_name = scoped_payload_campus_name(request, payload.campus_name)
        family.coach_name = coach_name
        family.service_status = payload.service_status
    else:
        campus_name = scoped_payload_campus_name(request, payload.campus_name)
        family = Family(
            family_id=family_id,
            parent_nickname=target_name,
            child_grade=payload.child_grade,
            course_stage=payload.course_stage,
            unit_progress=payload.unit_progress,
            pbl_count=payload.pbl_count if payload.pbl_count is not None else 0,
            checkin_rate=payload.checkin_rate,
            next_milestone=payload.next_milestone,
            campus_name=campus_name,
            coach_name=coach_name,
            service_status=payload.service_status,
        )
        db.add(family)
    db.commit()
    return {**as_dict(family), "message_count": db.query(RawMessage).filter(RawMessage.family_id == family.family_id).count()}


def timeline_time(value) -> str:
    return value.isoformat(sep=" ", timespec="seconds") if hasattr(value, "isoformat") else ""


def add_timeline_item(items: list[dict], kind: str, occurred_at, title: str, content: str, **meta) -> None:
    items.append({
        "kind": kind,
        "occurred_at": timeline_time(occurred_at),
        "title": title,
        "content": content or "",
        **meta,
    })


def build_family_timeline(db: Session, family_id: str, limit: int = 80) -> list[dict]:
    safe_limit = min(max(limit, 1), 200)
    items: list[dict] = []

    for msg in db.query(RawMessage).filter(RawMessage.family_id == family_id).all():
        add_timeline_item(
            items,
            "message",
            msg.message_time,
            msg.speaker or "聊天消息",
            msg.content,
            source=msg.source,
            status=msg.checkin_status or "",
            related_id=msg.id,
        )

    for record in db.query(CheckinRecord).filter(CheckinRecord.family_id == family_id).all():
        add_timeline_item(
            items,
            "checkin",
            record.created_at,
            f"打卡识别：{record.checkin_type}",
            record.evidence,
            status=record.checkin_type,
            related_id=record.message_id,
        )

    for output in db.query(AIOutput).filter(AIOutput.family_id == family_id).all():
        add_timeline_item(
            items,
            "ai_output",
            output.updated_at or output.created_at,
            f"AI输出：{output.agent_type}",
            output.edited_output or output.display_text,
            source=output.source,
            status=output.status,
            risk_level=output.risk_level,
            related_id=output.id,
        )

    for report in db.query(WeeklyReport).filter(WeeklyReport.family_id == family_id).all():
        add_timeline_item(
            items,
            "weekly_report",
            report.updated_at,
            f"周报：{report.week_label or report.id}",
            report.final_text or report.overall_state,
            status=report.status,
            related_id=report.id,
        )

    for record in db.query(FollowupRecord).filter(FollowupRecord.family_id == family_id).all():
        detail = record.content
        if record.result:
            detail = f"{detail}\n结果：{record.result}"
        if record.next_action:
            detail = f"{detail}\n下一步：{record.next_action}"
        add_timeline_item(
            items,
            "followup",
            record.occurred_at,
            f"跟进：{record.followup_type}",
            detail,
            status=record.status,
            owner=record.owner,
            created_by=record.created_by,
            related_id=record.id,
        )

    for log in db.query(SendLog).filter(SendLog.family_id == family_id).all():
        add_timeline_item(
            items,
            "send_log",
            log.sent_at,
            f"发送结果：{log.status}",
            log.detail,
            status=log.status,
            send_mode=log.send_mode,
            device_id=log.device_id,
            target_name=log.target_name,
            screenshot_path=log.screenshot_path,
            related_id=log.task_id,
        )

    items.sort(key=lambda item: item.get("occurred_at") or "", reverse=True)
    return items[:safe_limit]


def priority_level(score: int) -> str:
    if score >= 70:
        return "高"
    if score >= 45:
        return "中"
    return "低"


SERVICE_FUNNEL_STAGES = ["正常", "需跟进", "风险", "续报", "已结课"]
RISK_TERMS = ("退费", "退款", "投诉", "维权", "不满", "没效果", "无效果", "负面", "高风险")
FOLLOWUP_TERMS = ("请假", "补课", "缺课", "没打卡", "未打卡", "没提交", "未提交", "PBL", "跟进")
RENEWAL_TERMS = ("续报", "续费", "下一阶段", "报名", "续班")


def has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in (text or "") for term in terms)


def latest_family_message(db: Session, family_id: str) -> RawMessage | None:
    return (
        db.query(RawMessage)
        .filter(RawMessage.family_id == family_id)
        .order_by(RawMessage.message_time.desc())
        .first()
    )


def family_scope_query(db: Session, coach_name: str = "", campus_name: str = "", campus_names: tuple[str, ...] | None = None):
    query = db.query(Family).order_by(Family.family_id)
    clean_coach = (coach_name or "").strip()
    clean_campus = (campus_name or "").strip()
    scoped_campuses = normalize_campus_names(campus_names)
    if clean_campus:
        query = query.filter(Family.campus_name == clean_campus)
    if scoped_campuses:
        query = query.filter(Family.campus_name.in_(scoped_campuses))
    if clean_coach:
        query = query.filter(Family.coach_name == clean_coach)
    return query


def infer_family_service_stage(db: Session, family: Family, now: datetime | None = None) -> tuple[str, str]:
    context = build_family_dashboard_context(db, [family.family_id])
    return infer_family_service_stage_from_context(family, context, now)


def build_family_dashboard_context(db: Session, family_ids: list[str]) -> dict:
    ids = [family_id for family_id in family_ids if family_id]
    if not ids:
        return {
            "profiles": {},
            "open_followups": {},
            "recent_messages": {},
            "pending_task_counts": {},
            "open_task_counts": {},
            "review_output_counts": {},
            "review_report_counts": {},
            "send_log_status_counts": {},
        }

    profiles = {item.family_id: item for item in db.query(ParentProfile).filter(ParentProfile.family_id.in_(ids)).all()}

    open_followups = {}
    followups = (
        db.query(FollowupRecord)
        .filter(FollowupRecord.family_id.in_(ids), FollowupRecord.status != "已完成")
        .order_by(FollowupRecord.family_id, FollowupRecord.occurred_at.desc(), FollowupRecord.id.desc())
        .all()
    )
    for followup in followups:
        open_followups.setdefault(followup.family_id, followup)

    recent_messages: dict[str, list[RawMessage]] = {family_id: [] for family_id in ids}
    ranked_messages = (
        db.query(
            RawMessage.id.label("id"),
            func.row_number()
            .over(
                partition_by=RawMessage.family_id,
                order_by=(RawMessage.message_time.desc(), RawMessage.id.desc()),
            )
            .label("row_number"),
        )
        .filter(RawMessage.family_id.in_(ids))
        .subquery()
    )
    messages = (
        db.query(RawMessage)
        .join(ranked_messages, RawMessage.id == ranked_messages.c.id)
        .filter(ranked_messages.c.row_number <= 80)
        .order_by(RawMessage.family_id, RawMessage.message_time.desc(), RawMessage.id.desc())
        .all()
    )
    for message in messages:
        recent_messages.setdefault(message.family_id, []).append(message)

    pending_task_counts = dict(
        db.query(SendTask.family_id, func.count(SendTask.id))
        .filter(SendTask.family_id.in_(ids), SendTask.status == "pending")
        .group_by(SendTask.family_id)
        .all()
    )
    open_task_counts = dict(
        db.query(SendTask.family_id, func.count(SendTask.id))
        .filter(SendTask.family_id.in_(ids), SendTask.status.in_(["pending", "assigned"]))
        .group_by(SendTask.family_id)
        .all()
    )
    review_output_counts = dict(
        db.query(AIOutput.family_id, func.count(AIOutput.id))
        .filter(AIOutput.family_id.in_(ids), AIOutput.status == "needs_review")
        .group_by(AIOutput.family_id)
        .all()
    )
    review_report_counts = dict(
        db.query(WeeklyReport.family_id, func.count(WeeklyReport.id))
        .filter(WeeklyReport.family_id.in_(ids), WeeklyReport.status != "approved")
        .group_by(WeeklyReport.family_id)
        .all()
    )

    send_log_status_counts: dict[str, dict[str, int]] = {}
    for family_id, status, count in (
        db.query(SendLog.family_id, SendLog.status, func.count(SendLog.id))
        .filter(SendLog.family_id.in_(ids))
        .group_by(SendLog.family_id, SendLog.status)
        .all()
    ):
        send_log_status_counts.setdefault(family_id, {})[status or ""] = int(count)

    return {
        "profiles": profiles,
        "open_followups": open_followups,
        "recent_messages": recent_messages,
        "pending_task_counts": {key: int(value) for key, value in pending_task_counts.items()},
        "open_task_counts": {key: int(value) for key, value in open_task_counts.items()},
        "review_output_counts": {key: int(value) for key, value in review_output_counts.items()},
        "review_report_counts": {key: int(value) for key, value in review_report_counts.items()},
        "send_log_status_counts": send_log_status_counts,
    }


def infer_family_service_stage_from_context(family: Family, context: dict, now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.utcnow()
    explicit_status = family.service_status or ""
    if has_any(explicit_status, ("已结课", "结课", "结束服务")):
        return "已结课", "服务状态已标记结课"
    if has_any(explicit_status, RENEWAL_TERMS):
        return "续报", "服务状态已进入续报阶段"

    open_followup = context["open_followups"].get(family.family_id)
    if open_followup:
        if open_followup.status == "需升级":
            return "风险", f"{open_followup.followup_type}跟进需升级"
        return "需跟进", f"{open_followup.followup_type}跟进待处理"

    profile = context["profiles"].get(family.family_id)
    recent_messages = context["recent_messages"].get(family.family_id, [])[:20]
    signal_text = " ".join(
        [
            explicit_status,
            profile.service_risks if profile else "",
            profile.suggested_actions if profile else "",
            profile.trust_trend if profile else "",
            profile.renewal_intent if profile else "",
            " ".join(msg.content or "" for msg in recent_messages),
        ]
    )
    if has_any(signal_text, RISK_TERMS):
        return "风险", "存在退费/投诉/负面等风险信号"
    if has_any(signal_text, RENEWAL_TERMS):
        return "续报", "出现续报/下一阶段沟通信号"

    pending_tasks = context["pending_task_counts"].get(family.family_id, 0)
    review_outputs = context["review_output_counts"].get(family.family_id, 0)
    review_reports = context["review_report_counts"].get(family.family_id, 0)
    last_msg = recent_messages[0] if recent_messages else None
    silent_days = (now - last_msg.message_time).days if last_msg else 999
    if pending_tasks or review_outputs or review_reports:
        return "需跟进", "存在待发送/待审核事项"
    if silent_days >= 3:
        return "需跟进", f"已 {silent_days} 天无最新沟通"
    if has_any(signal_text, FOLLOWUP_TERMS):
        return "需跟进", "出现打卡/PBL/请假补课跟进信号"
    return "正常", "暂无高优先级异常"


def build_service_funnel(db: Session, coach_name: str = "", now: datetime | None = None, family_limit: int = 8, campus_name: str = "", campus_names: tuple[str, ...] | None = None) -> dict:
    now = now or datetime.utcnow()
    buckets = {stage: [] for stage in SERVICE_FUNNEL_STAGES}
    families = family_scope_query(db, coach_name, campus_name, campus_names).all()
    context = build_family_dashboard_context(db, [family.family_id for family in families])
    for family in families:
        stage, reason = infer_family_service_stage_from_context(family, context, now)
        messages = context["recent_messages"].get(family.family_id, [])
        last_msg = messages[0] if messages else None
        buckets[stage].append({
            "family_id": family.family_id,
            "family_name": family.parent_nickname or family.family_id,
            "campus_name": family.campus_name,
            "coach_name": family.coach_name,
            "service_status": family.service_status,
            "reason": reason,
            "last_message_at": timeline_time(last_msg.message_time) if last_msg else "",
        })

    stages = []
    for stage in SERVICE_FUNNEL_STAGES:
        families = sorted(buckets[stage], key=lambda item: (item["coach_name"] or "", item["family_id"]))
        stages.append({
            "stage": stage,
            "family_count": len(families),
            "families": families[:family_limit],
        })
    return {
        "generated_at": timeline_time(now),
        "coach_name": (coach_name or "").strip(),
        "campus_name": (campus_name or "").strip(),
        "total_families": sum(item["family_count"] for item in stages),
        "stages": stages,
    }


def todo_item(family: Family, reason: str, evidence: str = "", related_id: int = 0, occurred_at=None) -> dict:
    return {
        "family_id": family.family_id,
        "family_name": family.parent_nickname or family.family_id,
        "campus_name": family.campus_name,
        "coach_name": family.coach_name,
        "reason": reason,
        "evidence": evidence or "",
        "related_id": related_id,
        "occurred_at": timeline_time(occurred_at) if occurred_at else "",
    }


def build_workbench_todos(db: Session, coach_name: str = "", limit: int = 8, now: datetime | None = None, campus_name: str = "", campus_names: tuple[str, ...] | None = None) -> dict:
    safe_limit = min(max(limit, 1), 30)
    categories = {
        "pbl_incomplete": {"label": "PBL未完成", "items": []},
        "leave_makeup": {"label": "请假补课", "items": []},
        "weekly_pending_send": {"label": "周报待发", "items": []},
        "negative_feedback": {"label": "负面反馈", "items": []},
        "followup_pending": {"label": "跟进待办", "items": []},
        "ai_review": {"label": "AI待审核", "items": []},
        "send_failed": {"label": "发送失败", "items": []},
    }

    families = family_scope_query(db, coach_name, campus_name, campus_names).all()
    family_ids = [family.family_id for family in families]
    context = build_family_dashboard_context(db, family_ids)

    pending_weeklies = {}
    if family_ids:
        weekly_rows = (
            db.query(WeeklyReport)
            .filter(
                WeeklyReport.family_id.in_(family_ids),
                WeeklyReport.status == "approved",
                or_(
                    WeeklyReport.send_status.notin_(["sent", "dry_run"]),
                    WeeklyReport.send_status.is_(None),
                    WeeklyReport.send_status == "",
                ),
            )
            .order_by(WeeklyReport.family_id, WeeklyReport.updated_at.desc(), WeeklyReport.id.desc())
            .all()
        )
        for weekly in weekly_rows:
            pending_weeklies.setdefault(weekly.family_id, weekly)

    review_outputs = {}
    if family_ids:
        output_rows = (
            db.query(AIOutput)
            .filter(AIOutput.family_id.in_(family_ids), AIOutput.status == "needs_review")
            .order_by(AIOutput.family_id, AIOutput.updated_at.desc(), AIOutput.id.desc())
            .all()
        )
        for output in output_rows:
            review_outputs.setdefault(output.family_id, output)

    failed_logs = {}
    if family_ids:
        log_rows = (
            db.query(SendLog)
            .filter(SendLog.family_id.in_(family_ids), SendLog.status == "failed")
            .order_by(SendLog.family_id, SendLog.sent_at.desc(), SendLog.id.desc())
            .all()
        )
        for log in log_rows:
            failed_logs.setdefault(log.family_id, log)

    for family in families:
        messages = context["recent_messages"].get(family.family_id, [])
        for msg in messages:
            content = msg.content or ""
            if "PBL" in content.upper() and has_any(content, ("没", "未", "还没", "未提交", "没提交", "未完成", "没完成", "忘了")):
                categories["pbl_incomplete"]["items"].append(todo_item(family, "PBL 作品疑似未完成/未提交", content, msg.id, msg.message_time))
                break

        for msg in messages:
            content = msg.content or ""
            scene = detect_scene(content)
            if scene in {"请假/孩子有事", "请假/补课"} or has_any(content, ("请假", "补课", "缺课", "调课", "上不了")):
                categories["leave_makeup"]["items"].append(todo_item(family, "请假/补课事项待确认", content, msg.id, msg.message_time))
                break

        weekly = pending_weeklies.get(family.family_id)
        if weekly:
            reason = "周报已审核但尚未完成发送闭环"
            categories["weekly_pending_send"]["items"].append(todo_item(family, reason, weekly.final_text, weekly.id, weekly.updated_at))

        profile = context["profiles"].get(family.family_id)
        risk_text = " ".join([profile.service_risks if profile else "", profile.suggested_actions if profile else ""])
        risk_msg = next((msg for msg in messages if has_any(msg.content or "", RISK_TERMS)), None)
        if has_any(risk_text, RISK_TERMS) or risk_msg:
            categories["negative_feedback"]["items"].append(
                todo_item(family, "出现退费/投诉/不满等负面信号", risk_msg.content if risk_msg else risk_text, risk_msg.id if risk_msg else 0, risk_msg.message_time if risk_msg else None)
            )

        followup = context["open_followups"].get(family.family_id)
        if followup:
            reason = "跟进记录需升级" if followup.status == "需升级" else "跟进记录待处理"
            evidence = "；".join([item for item in [followup.content, followup.next_action] if item])
            categories["followup_pending"]["items"].append(todo_item(family, reason, evidence, followup.id, followup.occurred_at))

        output = review_outputs.get(family.family_id)
        if output:
            categories["ai_review"]["items"].append(todo_item(family, "AI 输出需要人工审核", output.edited_output or output.display_text, output.id, output.updated_at or output.created_at))

        failed_log = failed_logs.get(family.family_id)
        if failed_log:
            categories["send_failed"]["items"].append(todo_item(family, "发送失败需要复核", failed_log.detail, failed_log.task_id, failed_log.sent_at))

    result = []
    for key, data in categories.items():
        items = sorted(data["items"], key=lambda item: item.get("occurred_at") or "", reverse=True)
        result.append({
            "key": key,
            "label": data["label"],
            "count": len(items),
            "items": items[:safe_limit],
        })
    return {
        "generated_at": timeline_time(now or datetime.utcnow()),
        "coach_name": (coach_name or "").strip(),
        "campus_name": (campus_name or "").strip(),
        "categories": result,
    }


def build_workbench_overview(db: Session, coach_name: str = "", limit: int = 8, now: datetime | None = None, campus_name: str = "", campus_names: tuple[str, ...] | None = None) -> dict:
    now = now or datetime.utcnow()
    return {
        "service_funnel": build_service_funnel(db, coach_name, now, limit, campus_name, campus_names),
        "todos": build_workbench_todos(db, coach_name, limit, now, campus_name, campus_names),
    }


def build_admin_service_quality_dashboard(db: Session, coach_name: str = "", now: datetime | None = None, campus_name: str = "", campus_names: tuple[str, ...] | None = None) -> dict:
    now = now or datetime.utcnow()
    rows = []
    totals = {
        "family_count": 0,
        "risk_family_count": 0,
        "followup_family_count": 0,
        "pending_task_count": 0,
        "review_output_count": 0,
        "review_report_count": 0,
        "send_log_count": 0,
        "sent_count": 0,
        "dry_run_count": 0,
        "failed_count": 0,
    }
    grouped: dict[str, list[Family]] = {}
    campus_grouped: dict[str, list[Family]] = {}
    families = family_scope_query(db, coach_name, campus_name, campus_names).all()
    context = build_family_dashboard_context(db, [family.family_id for family in families])
    stage_by_family = {}
    reason_by_family = {}
    for family in families:
        stage, reason = infer_family_service_stage_from_context(family, context, now)
        stage_by_family[family.family_id] = stage
        reason_by_family[family.family_id] = reason
        grouped.setdefault(family.coach_name or "未分配", []).append(family)
        campus_grouped.setdefault(family.campus_name or "未分配校区", []).append(family)

    for coach, families in sorted(grouped.items()):
        row = {
            "coach_name": coach,
            "campus_names": sorted({family.campus_name or "未分配校区" for family in families}),
            "family_count": len(families),
            "normal_count": 0,
            "followup_family_count": 0,
            "risk_family_count": 0,
            "renewal_family_count": 0,
            "closed_family_count": 0,
            "pending_task_count": 0,
            "review_output_count": 0,
            "review_report_count": 0,
            "send_log_count": 0,
            "sent_count": 0,
            "dry_run_count": 0,
            "failed_count": 0,
            "send_completion_rate": 0.0,
            "send_failure_rate": 0.0,
            "risk_families": [],
        }
        for family in families:
            stage = stage_by_family[family.family_id]
            reason = reason_by_family[family.family_id]
            if stage == "正常":
                row["normal_count"] += 1
            elif stage == "需跟进":
                row["followup_family_count"] += 1
            elif stage == "风险":
                row["risk_family_count"] += 1
                row["risk_families"].append({
                    "family_id": family.family_id,
                    "family_name": family.parent_nickname or family.family_id,
                    "reason": reason,
                })
            elif stage == "续报":
                row["renewal_family_count"] += 1
            elif stage == "已结课":
                row["closed_family_count"] += 1

            row["pending_task_count"] += context["open_task_counts"].get(family.family_id, 0)
            row["review_output_count"] += context["review_output_counts"].get(family.family_id, 0)
            row["review_report_count"] += context["review_report_counts"].get(family.family_id, 0)
            log_counts = context["send_log_status_counts"].get(family.family_id, {})
            row["send_log_count"] += sum(log_counts.values())
            row["sent_count"] += log_counts.get("sent", 0)
            row["dry_run_count"] += log_counts.get("dry_run", 0)
            row["failed_count"] += log_counts.get("failed", 0)

        completed = row["sent_count"] + row["dry_run_count"]
        if row["send_log_count"]:
            row["send_completion_rate"] = round(completed / row["send_log_count"], 4)
            row["send_failure_rate"] = round(row["failed_count"] / row["send_log_count"], 4)
        row["risk_families"] = row["risk_families"][:5]
        for key in totals:
            totals[key] += row.get(key, 0)
        rows.append(row)

    totals["coach_count"] = len(rows)
    totals["campus_count"] = len(campus_grouped)
    totals["send_completion_rate"] = round((totals["sent_count"] + totals["dry_run_count"]) / totals["send_log_count"], 4) if totals["send_log_count"] else 0.0
    totals["send_failure_rate"] = round(totals["failed_count"] / totals["send_log_count"], 4) if totals["send_log_count"] else 0.0
    rows.sort(key=lambda item: (-item["risk_family_count"], -item["pending_task_count"], item["coach_name"]))
    campuses = [
        {
            "campus_name": campus,
            "family_count": len(families),
            "coach_count": len({family.coach_name or "未分配" for family in families}),
            "risk_family_count": sum(1 for family in families if stage_by_family[family.family_id] == "风险"),
            "pending_task_count": sum(context["open_task_counts"].get(family.family_id, 0) for family in families),
        }
        for campus, families in sorted(campus_grouped.items())
    ]
    return {
        "generated_at": timeline_time(now),
        "coach_name": (coach_name or "").strip(),
        "campus_name": (campus_name or "").strip(),
        "totals": totals,
        "campuses": campuses,
        "coaches": rows,
    }


def build_today_priorities(db: Session, limit: int = 12, now: datetime | None = None, coach_name: str = "", campus_name: str = "", campus_names: tuple[str, ...] | None = None) -> list[dict]:
    now = now or datetime.utcnow()
    safe_limit = min(max(limit, 1), 50)
    items: list[dict] = []
    families = family_scope_query(db, coach_name, campus_name, campus_names).order_by(Family.family_id).all()
    for family in families:
        reasons: list[str] = []
        score = 0
        profile = db.query(ParentProfile).filter(ParentProfile.family_id == family.family_id).one_or_none()
        if profile:
            risk_text = " ".join([profile.service_risks or "", profile.suggested_actions or "", profile.trust_trend or ""])
            if any(token in risk_text for token in ("退费", "投诉", "高风险", "不满", "下降")):
                score += 45
                reasons.append("存在高风险/信任下降信号")
            elif any(token in risk_text for token in ("风险", "焦虑", "担心", "负面")):
                score += 25
                reasons.append("存在需关注的服务风险")

        last_msg = (
            db.query(RawMessage)
            .filter(RawMessage.family_id == family.family_id)
            .order_by(RawMessage.message_time.desc())
            .first()
        )
        if last_msg:
            silent_days = (now - last_msg.message_time).days
            if silent_days >= 7:
                score += 35
                reasons.append(f"已 {silent_days} 天无最新沟通")
            elif silent_days >= 3:
                score += 18
                reasons.append(f"已 {silent_days} 天无最新沟通")
        else:
            silent_days = 999
            score += 20
            reasons.append("暂无有效沟通记录")

        review_outputs = db.query(AIOutput).filter(AIOutput.family_id == family.family_id, AIOutput.status == "needs_review").count()
        if review_outputs:
            score += min(30, review_outputs * 10)
            reasons.append(f"{review_outputs} 条 AI 内容待审核")

        review_reports = db.query(WeeklyReport).filter(WeeklyReport.family_id == family.family_id, WeeklyReport.status != "approved").count()
        if review_reports:
            score += min(24, review_reports * 8)
            reasons.append(f"{review_reports} 份周报待审核")

        pending_tasks = db.query(SendTask).filter(SendTask.family_id == family.family_id, SendTask.status == "pending").all()
        if pending_tasks:
            real_count = sum(1 for task in pending_tasks if task.send_mode == "real_send")
            score += min(35, len(pending_tasks) * 8 + real_count * 12)
            reasons.append(f"{len(pending_tasks)} 个发送任务待处理")

        failed_send = db.query(SendLog).filter(SendLog.family_id == family.family_id, SendLog.status == "failed").count()
        if failed_send:
            score += min(20, failed_send * 6)
            reasons.append(f"{failed_send} 条发送失败需复核")

        open_followups = db.query(FollowupRecord).filter(FollowupRecord.family_id == family.family_id, FollowupRecord.status != "已完成").all()
        if open_followups:
            upgrade_count = sum(1 for item in open_followups if item.status == "需升级")
            score += min(40, len(open_followups) * 10 + upgrade_count * 15)
            reasons.append(f"{len(open_followups)} 条跟进记录待处理")

        if not reasons:
            continue
        if open_followups and any(item.status == "需升级" for item in open_followups):
            action = "先处理需升级跟进"
        elif pending_tasks:
            action = "先处理待发送任务"
        elif review_outputs or review_reports:
            action = "先审核 AI 内容/周报"
        elif open_followups:
            action = "先处理跟进记录"
        elif score >= 45:
            action = "查看家庭时间线并人工跟进"
        else:
            action = "保持观察"
        items.append({
            "family_id": family.family_id,
            "family_name": family.parent_nickname or family.family_id,
            "campus_name": family.campus_name,
            "coach_name": family.coach_name,
            "score": score,
            "level": priority_level(score),
            "reasons": reasons,
            "suggested_action": action,
            "last_message_at": timeline_time(last_msg.message_time) if last_msg else "",
            "pending_task_count": len(pending_tasks),
            "review_output_count": review_outputs,
            "review_report_count": review_reports,
            "open_followup_count": len(open_followups),
        })

    items.sort(key=lambda item: (-item["score"], item["family_id"]))
    return items[:safe_limit]


# 单个家庭详情页要的消息、画像和周报都在这里聚合返回。
@app.get("/api/families/{family_id}")
def family_detail(family_id: str, timeline_limit: int = 80, request: Request = None, db: Session = Depends(get_db)):
    family = require_family_for_request(db, family_id, request)
    messages = db.query(RawMessage).filter(RawMessage.family_id == family_id).order_by(RawMessage.message_time).all()
    profile = db.query(ParentProfile).filter(ParentProfile.family_id == family_id).one_or_none()
    reports = db.query(WeeklyReport).filter(WeeklyReport.family_id == family_id).order_by(WeeklyReport.id.desc()).all()
    followups = db.query(FollowupRecord).filter(FollowupRecord.family_id == family_id).order_by(FollowupRecord.occurred_at.desc()).all()
    data = {
        "family": as_dict(family),
        "messages": [as_dict(m) for m in messages],
        "profile": as_dict(profile) if profile else None,
        "reports": [as_dict(r) for r in reports],
        "followups": [as_dict(r) for r in followups],
        "timeline": build_family_timeline(db, family_id, timeline_limit),
    }
    return maybe_redact_for_request(data, request)


@app.get("/api/families/{family_id}/timeline")
def family_timeline(family_id: str, limit: int = 80, request: Request = None, db: Session = Depends(get_db)):
    require_family_for_request(db, family_id, request)
    return maybe_redact_for_request(build_family_timeline(db, family_id, limit), request)


@app.get("/api/followups")
def list_followups(family_id: str = "", status: str = "", request: Request = None, db: Session = Depends(get_db)):
    query = db.query(FollowupRecord)
    if family_id:
        require_family_for_request(db, family_id, request)
        query = query.filter(FollowupRecord.family_id == family_id)
    else:
        query = apply_family_id_scope(query, FollowupRecord.family_id, db, request)
    if status:
        query = query.filter(FollowupRecord.status == status)
    rows = query.order_by(FollowupRecord.occurred_at.desc(), FollowupRecord.id.desc()).all()
    return maybe_redact_for_request([as_dict(item) for item in rows], request)


@app.post("/api/families/{family_id}/followups")
def create_family_followup(family_id: str, payload: FollowupIn, request: Request = None, db: Session = Depends(get_db)):
    family = require_family_for_request(db, family_id, request)
    data = clean_followup_payload(payload)
    record = FollowupRecord(family_id=family.family_id, created_by=actor_from_request(request), **data)
    db.add(record)
    db.commit()
    return maybe_redact_for_request(as_dict(record), request)


@app.get("/api/workbench/today-priorities")
def today_priorities(limit: int = 12, campus_name: str = "", request: Request = None, db: Session = Depends(get_db)):
    return maybe_redact_for_request(
        build_today_priorities(db, limit, coach_name=coach_filter_for_request(request), campus_name=campus_name, campus_names=campus_scope_from_request(request)),
        request,
    )


@app.get("/api/workbench/overview")
def workbench_overview(coach_name: str = "", campus_name: str = "", limit: int = 8, request: Request = None, db: Session = Depends(get_db)):
    return maybe_redact_for_request(
        build_workbench_overview(db, coach_filter_for_request(request, coach_name), limit, campus_name=campus_name, campus_names=campus_scope_from_request(request)),
        request,
    )


@app.get("/api/admin/service-quality")
def admin_service_quality(coach_name: str = "", campus_name: str = "", request: Request = None, db: Session = Depends(get_db)):
    return maybe_redact_for_request(
        build_admin_service_quality_dashboard(db, coach_filter_for_request(request, coach_name), campus_name=campus_name, campus_names=campus_scope_from_request(request)),
        request,
    )


@app.get("/api/agent/evaluations")
def agent_evaluation_cases():
    return list_agent_eval_cases()


@app.post("/api/agent/evaluations/run")
def run_agent_evaluations():
    return run_agent_evaluation()


# 如果家庭没有有效消息，就不生成周报和画像，避免写入空数据。
def generate_for_family(db: Session, family_id: str):
    messages = db.query(RawMessage).filter(RawMessage.family_id == family_id, RawMessage.is_effective == "Y").order_by(RawMessage.message_time).all()
    if not messages:
        return None
    report_data = generate_weekly_report(family_id, messages)
    report = WeeklyReport(**report_data)
    db.add(report)

    profile_data = generate_parent_profile(family_id, messages)
    profile = db.query(ParentProfile).filter(ParentProfile.family_id == family_id).one_or_none()
    if profile:
        for key, value in profile_data.items():
            setattr(profile, key, value)
    else:
        db.add(ParentProfile(**profile_data))
    return report_data


def create_family_ai_bundle(db: Session, family_id: str, source: str = "家庭详情一键生成") -> dict:
    family = require_family(db, family_id)
    context = build_agent_context(db, family_id)
    if not context["messages"]:
        raise HTTPException(404, "该家庭还没有消息，无法生成 AI 操作区内容")

    outputs = []
    profile_result = run_family_profile_agent_service(context)
    profile_output = save_ai_output(db, family_id, "family_profile", source, profile_result)
    upsert_parent_profile_from_agent(db, family_id, profile_result)
    outputs.append(profile_output)

    weekly_result = run_weekly_report_agent_service(context)
    weekly_output = save_ai_output(db, family_id, "weekly_report", source, weekly_result)
    create_weekly_report_from_agent(db, family_id, weekly_result)
    outputs.append(weekly_output)

    reply_result = run_reply_agent_service(context, latest_parent_message(context), "standard")
    reply_output = save_ai_output(db, family_id, "ai_reply", source, reply_result)
    outputs.append(reply_output)

    checkin_result = run_checkin_pbl_agent_service(context)
    checkin_output = save_ai_output(db, family_id, "checkin_pbl", source, checkin_result)
    checkin_records_created = create_checkin_records_from_context(db, context)
    outputs.append(checkin_output)

    return {
        "family_id": family_id,
        "family_name": family.parent_nickname or family.family_id,
        "outputs": outputs,
        "checkin_records_created": checkin_records_created,
    }


# 批量生成所有家庭的周报/画像。
@app.post("/api/generate/all")
def generate_all(request: Request = None, db: Session = Depends(get_db)):
    count = 0
    for family in scoped_family_query(db, request).all():
        if generate_for_family(db, family.family_id):
            count += 1
    db.commit()
    return {"generated_families": count}


# 单家庭生成接口，前端家庭详情页会直接调用。
@app.post("/api/families/{family_id}/generate")
def generate_one(family_id: str, request: Request = None, db: Session = Depends(get_db)):
    require_family_for_request(db, family_id, request)
    result = generate_for_family(db, family_id)
    if not result:
        raise HTTPException(404, "没有可生成的有效消息")
    db.commit()
    return result


@app.post("/api/families/{family_id}/ai-bundle")
def generate_family_ai_bundle(family_id: str, payload: FamilyAIBundleIn | None = None, request: Request = None, db: Session = Depends(get_db)):
    require_family_for_request(db, family_id, request)
    result = create_family_ai_bundle(db, family_id, (payload or FamilyAIBundleIn()).source)
    db.commit()
    data = {
        **{key: value for key, value in result.items() if key != "outputs"},
        "outputs": [as_dict(output) for output in result["outputs"]],
    }
    return maybe_redact_for_request(data, request)


# AI 输出列表接口，支持按家庭和 Agent 类型筛选。
@app.get("/api/ai-outputs")
def list_ai_outputs(family_id: str = "", agent_type: str = "", request: Request = None, db: Session = Depends(get_db)):
    query = db.query(AIOutput).order_by(AIOutput.id.desc())
    if family_id:
        require_family_for_request(db, family_id, request)
        query = query.filter(AIOutput.family_id == family_id)
    else:
        query = apply_family_id_scope(query, AIOutput.family_id, db, request)
    if agent_type:
        query = query.filter(AIOutput.agent_type == agent_type)
    return maybe_redact_for_request([as_dict(item) for item in query.limit(200).all()], request)


# 保存人工审核后的 AI 输出。
@app.put("/api/ai-outputs/{output_id}")
def update_ai_output(output_id: int, payload: AIOutputUpdate, request: Request = None, db: Session = Depends(get_db)):
    output = db.get(AIOutput, output_id)
    if not output:
        raise HTTPException(404, "AI输出不存在")
    ensure_family_id_access(db, output.family_id, request)
    output.edited_output = payload.edited_output
    output.status = payload.status
    output.updated_at = datetime.utcnow()
    db.commit()
    return as_dict(output)


# 从 AI 输出直接生成待发送任务。
@app.post("/api/ai-outputs/{output_id}/send-task")
def create_task_from_ai_output(output_id: int, payload: AIOutputTaskIn | None = None, request: Request = None, db: Session = Depends(get_db)):
    output = db.get(AIOutput, output_id)
    if not output:
        raise HTTPException(404, "AI输出不存在")
    ensure_family_id_access(db, output.family_id, request)
    family = db.query(Family).filter(Family.family_id == output.family_id).one_or_none()
    data = payload or AIOutputTaskIn()
    content = data.content.strip() or output.edited_output or output.display_text
    content = validate_send_task_content(content)
    target_name = data.target_name.strip() or (family.parent_nickname if family else output.family_id)
    scene = data.scene.strip() or output.source or output.agent_type
    if data.device_id:
        ensure_new_task_operation_allowed(request, "assign_device")
    send_mode = validate_send_mode_submit(data.send_mode, data.confirm_real_send)
    validate_ai_output_send_boundary(output, content, send_mode)
    if send_mode == "real_send":
        ensure_new_task_operation_allowed(request, "confirm_real_send")
        device_id = resolve_real_send_device_binding(db, data.device_id, target_name)
        validate_real_send_device_binding(device_id)
        validate_real_send_risk(db, target_name, content)
    else:
        device_id = validate_task_device_binding(db, data.device_id, target_name)
    task = SendTask(
        family_id=output.family_id,
        target_name=target_name,
        scene=scene,
        content=content,
        device_id=device_id,
        send_mode=send_mode,
        status="pending",
    )
    ensure_real_send_readiness(db, task)
    output.status = "task_created"
    output.edited_output = content
    output.updated_at = datetime.utcnow()
    add_send_task_with_audit(db, task, "create", actor_from_request(request), f"AI 输出 {output_id} 创建发送任务")
    db.commit()
    return send_task_view(task, request, db)


# 家庭画像 Agent 接口。
@app.post("/api/agent/profile")
@app.post("/agent/profile")
def run_profile_agent(payload: AgentRequest, request: Request = None, db: Session = Depends(get_db)):
    family = require_family_for_request(db, payload.family_id, request)
    context = build_agent_context(db, payload.family_id)
    result = run_family_profile_agent_service(context)
    output = save_ai_output(db, payload.family_id, "family_profile", payload.source or "生成画像", result)

    raw = result["raw"]
    upsert_parent_profile_from_agent(db, payload.family_id, result)
    db.commit()
    return {**as_dict(output), "family_name": family.parent_nickname}


# 周报 Agent 接口。
@app.post("/api/agent/weekly-report")
@app.post("/agent/weekly-report")
def run_weekly_report_agent(payload: AgentRequest, request: Request = None, db: Session = Depends(get_db)):
    family = require_family_for_request(db, payload.family_id, request)
    context = build_agent_context(db, payload.family_id)
    result = run_weekly_report_agent_service(context)
    output = save_ai_output(db, payload.family_id, "weekly_report", payload.source or "生成周报", result)
    create_weekly_report_from_agent(db, payload.family_id, result)
    db.commit()
    return {**as_dict(output), "family_name": family.parent_nickname}


# 回复 Agent 接口。
@app.get("/api/agent/reply-config")
def get_reply_agent_config():
    return reply_agent_config_view()


@app.post("/api/agent/reply-config")
def save_reply_agent_config(payload: ReplyAgentConfigIn):
    return reply_agent_config_view(write_reply_agent_config(payload.model_dump()))


@app.post("/api/agent/replies/auto-draft")
def auto_draft_replies(payload: AutoReplyDraftIn | None = None, request: Request = None, db: Session = Depends(get_db)):
    data = payload or AutoReplyDraftIn()
    tone = (data.tone or "standard").strip() or "standard"
    source = (data.source or "自动回复草稿").strip() or "自动回复草稿"
    safe_limit = min(max(data.limit, 1), 500)
    skip_recent_hours = max(data.skip_recent_hours, 0)
    created_outputs: list[tuple[AIOutput, str]] = []
    skipped_items = []

    families = scoped_family_query(db, request).order_by(Family.family_id).limit(safe_limit).all()
    for family in families:
        family_name = family.parent_nickname or family.family_id
        if has_recent_pending_reply_draft(db, family.family_id, skip_recent_hours):
            skipped_items.append({"family_id": family.family_id, "family_name": family_name, "reason": "已有近期待审核回复"})
            continue

        context = build_agent_context(db, family.family_id)
        message = latest_effective_parent_message(context)
        if not message:
            skipped_items.append({"family_id": family.family_id, "family_name": family_name, "reason": "暂无有效家长消息"})
            continue

        result = run_reply_agent_service(context, message, tone)
        output = save_ai_output(db, family.family_id, "ai_reply", source, result)
        created_outputs.append((output, family_name))

    db.commit()
    outputs = [{**as_dict(output), "family_name": family_name} for output, family_name in created_outputs]
    return maybe_redact_for_request(
        {
            "created": len(outputs),
            "skipped": len(skipped_items),
            "outputs": outputs,
            "skipped_items": skipped_items,
            "note": "仅生成 AI 待审草稿，不创建发送任务，不触发企业微信发送。",
        },
        request,
    )


@app.post("/api/agent/reply")
@app.post("/agent/reply")
def run_reply_agent(payload: AgentRequest, request: Request = None, db: Session = Depends(get_db)):
    family = require_family_for_request(db, payload.family_id, request)
    context = build_agent_context(db, payload.family_id)
    result = run_reply_agent_service(context, payload.message, payload.tone)
    output = save_ai_output(db, payload.family_id, "ai_reply", payload.source or "生成回复", result)
    db.commit()
    return {**as_dict(output), "family_name": family.parent_nickname}


# 打卡/PBL Agent 接口，同时写入打卡记录。
@app.post("/api/agent/checkin-pbl")
@app.post("/agent/checkin-pbl")
def run_checkin_pbl_agent(payload: AgentRequest, request: Request = None, db: Session = Depends(get_db)):
    family = require_family_for_request(db, payload.family_id, request)
    context = build_agent_context(db, payload.family_id)
    result = run_checkin_pbl_agent_service(context)
    output = save_ai_output(db, payload.family_id, "checkin_pbl", payload.source or "识别打卡/PBL", result)
    created = create_checkin_records_from_context(db, context)
    db.commit()
    return {**as_dict(output), "family_name": family.parent_nickname, "checkin_records_created": created}


def speaker_is_self(speaker: str) -> bool:
    return (speaker or "").strip() in {"我", "本人", "自己", "老师", "陪跑师"}


def sync_conversation_payload(
    db: Session,
    payload: RpaConversationIn,
    *,
    actor: str = "企微同步",
    source_prefix: str = "企微RPA",
    dev: Device | None = None,
) -> dict:
    family_id = payload.family_id.strip()
    family = None
    if family_id:
        family = db.query(Family).filter(Family.family_id == family_id).one_or_none()
    if not family:
        family = db.query(Family).filter(Family.parent_nickname == payload.target_name).one_or_none()
    if not family:
        family_id = family_id or f"WECOM_{payload.target_name}"
        family = Family(
            family_id=family_id,
            parent_nickname=payload.parent_nickname or payload.target_name,
            child_grade=payload.child_grade,
            campus_name=payload.campus_name,
            coach_name=payload.coach_name,
            service_status="企微RPA同步",
        )
        db.add(family)
        db.flush()
    elif payload.campus_name and not family.campus_name:
        family.campus_name = payload.campus_name
    family_id = family.family_id

    inserted = 0
    latest_parent_message = payload.latest_message.strip()
    readable_messages = [msg for msg in payload.messages if (msg.content or "").strip()]
    empty_title_check = bool(payload.conversation_opened and payload.empty_conversation_ok and not readable_messages)
    proof_source = next((msg.source for msg in readable_messages if (msg.source or "").strip()), "")
    if not proof_source:
        proof_source = "企业微信RPA-空会话标题校验" if empty_title_check else "企业微信RPA"
    proof_ok = bool(readable_messages) or empty_title_check
    conversation_check = record_device_conversation_check(
        db,
        dev,
        payload.target_name,
        "ok" if proof_ok else "failed",
        message_count=len(readable_messages),
        source=proof_source,
        last_error="" if proof_ok else "同步时未读到聊天消息",
    )
    for msg in payload.messages:
        content = msg.content.strip()
        if not content:
            continue
        external_id = (msg.external_id or "").strip()
        if external_id:
            exists = db.query(RawMessage).filter(RawMessage.external_id == external_id).first()
        else:
            exists = (
                db.query(RawMessage)
                .filter(
                    RawMessage.family_id == family_id,
                    RawMessage.speaker == (msg.speaker or payload.target_name),
                    RawMessage.content == content,
                    RawMessage.source == msg.source,
                )
                .first()
            )
        if exists:
            continue
        speaker = msg.speaker or payload.target_name
        db.add(
            RawMessage(
                family_id=family_id,
                message_time=parse_rpa_time(msg.message_time),
                speaker=speaker,
                content=content,
                source=msg.source,
                external_id=external_id,
                checkin_status=detect_checkin(content),
                is_effective="Y" if len(content) >= 2 else "N",
            )
        )
        inserted += 1
        if not speaker_is_self(speaker):
            latest_parent_message = content

    ai_output = None
    task = None
    generated_outputs = []
    auto_reply_note = ""
    reply_config = read_reply_agent_config()
    auto_reply_enabled = (
        payload.auto_generate_reply
        and reply_config["auto_reply_enabled"]
        and "reply_agent" in reply_config["enabled_agents"]
    )
    if auto_reply_enabled and latest_parent_message and inserted > 0:
        db.flush()
        if has_recent_reply_output(db, family_id, reply_config["skip_recent_hours"]):
            auto_reply_note = f"已跳过自动回复：{reply_config['skip_recent_hours']} 小时内已有回复记录"
        else:
            context = build_agent_context(db, family_id)
            if reply_config["reply_agent"] == "quick_reply_agent":
                result = run_quick_reply_agent_service(context, latest_parent_message, reply_config["tone"])
            else:
                result = run_reply_agent_service(context, latest_parent_message, reply_config["tone"])
            ai_output = save_ai_output(db, family_id, "ai_reply", f"{source_prefix}：{payload.target_name}", result)
            generated_outputs.append(ai_output)
            manual_required = ai_output.need_human_review == "Y" or ai_output.risk_level == "高"
            if not manual_required:
                ai_output.status = "approved"
            if reply_config["auto_create_send_task"]:
                if manual_required and reply_config["high_risk_policy"] == "manual":
                    auto_reply_note = "已生成回复但命中人工介入策略，未自动加入发送任务"
                else:
                    try:
                        content = validate_send_task_content(result["display_text"])
                        send_mode = validate_send_mode(reply_config["send_mode"])
                        if send_mode == "real_send":
                            device_id = resolve_real_send_device_binding(db, "", payload.target_name)
                            validate_real_send_device_binding(device_id)
                            validate_real_send_risk(db, payload.target_name, content)
                        else:
                            device_id = ""
                        validate_ai_output_send_boundary(ai_output, content, send_mode)
                        task = SendTask(
                            family_id=family_id,
                            target_name=payload.target_name,
                            scene=result["raw"].get("场景类型", "企微AI回复"),
                            content=content,
                            device_id=device_id,
                            send_mode=send_mode,
                            status="pending",
                        )
                        ensure_real_send_readiness(db, task)
                        ai_output.status = "task_created"
                        add_send_task_with_audit(db, task, "create", actor, f"企微会话「{payload.target_name}」自动回复生成发送任务")
                    except HTTPException as exc:
                        auto_reply_note = f"自动回复已生成，但加入发送任务失败：{exc.detail}"

    if payload.auto_generate_all_agents:
        db.flush()
        context = build_agent_context(db, family_id)
        if context["messages"]:
            profile_result = run_family_profile_agent_service(context)
            profile_output = save_ai_output(db, family_id, "family_profile", f"{source_prefix}：{payload.target_name}", profile_result)
            generated_outputs.append(profile_output)
            upsert_parent_profile_from_agent(db, family_id, profile_result)

            weekly_result = run_weekly_report_agent_service(context)
            weekly_output = save_ai_output(db, family_id, "weekly_report", f"{source_prefix}：{payload.target_name}", weekly_result)
            generated_outputs.append(weekly_output)
            create_weekly_report_from_agent(db, family_id, weekly_result)

            checkin_result = run_checkin_pbl_agent_service(context)
            checkin_output = save_ai_output(db, family_id, "checkin_pbl", f"{source_prefix}：{payload.target_name}", checkin_result)
            generated_outputs.append(checkin_output)
            create_checkin_records_from_context(db, context)

    db.commit()
    return {
        "family_id": family_id,
        "target_name": payload.target_name,
        "messages_inserted": inserted,
        "conversation_check": as_dict(conversation_check) if conversation_check else None,
        "ai_output": as_dict(ai_output) if ai_output else None,
        "auto_reply_enabled": auto_reply_enabled,
        "auto_reply_note": auto_reply_note,
        "generated_outputs": [as_dict(item) for item in generated_outputs],
        "send_task": as_dict(task) if task else None,
    }


# RPA 同步企业微信会话消息，并可选自动生成回复任务。
@app.post("/api/rpa/conversations/sync")
def sync_rpa_conversation(payload: RpaConversationIn, request: Request = None, db: Session = Depends(get_db)):
    dev = require_device_for_request(db, request)
    validate_device_conversation_scope(dev, payload.target_name)
    return sync_conversation_payload(
        db,
        payload,
        actor=actor_from_request(request, "企微RPA"),
        source_prefix="企微RPA",
        dev=dev,
    )


@app.get("/api/rpa/conversations/resolve")
def resolve_rpa_conversation(target_name: str, request: Request = None, db: Session = Depends(get_db)):
    dev = require_device_for_request(db, request)
    validate_device_conversation_scope(dev, target_name)
    family = db.query(Family).filter(Family.parent_nickname == target_name).one_or_none()
    if not family:
        family = db.query(Family).filter(Family.family_id == target_name).one_or_none()
    return {
        "exists": bool(family),
        "target_name": target_name,
        "family": as_dict(family) if family else None,
    }


def archive_state_for_config(db: Session, corp_id: str) -> WecomArchiveState:
    key = (corp_id or "default").strip() or "default"
    state = db.query(WecomArchiveState).filter(WecomArchiveState.corp_id == key).one_or_none()
    if not state:
        state = WecomArchiveState(corp_id=key, seq=0)
        db.add(state)
        db.flush()
    return state


def archive_envelope_from_payload(item: dict) -> ArchiveEnvelope:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else item
    decrypted = item.get("decrypted") if isinstance(item.get("decrypted"), dict) else item
    return ArchiveEnvelope(
        seq=int(item.get("seq") or raw.get("seq") or decrypted.get("seq") or 0),
        msgid=str(item.get("msgid") or raw.get("msgid") or decrypted.get("msgid") or ""),
        raw=raw,
        decrypted=decrypted,
    )


@app.get("/api/wecom-archive/status")
def wecom_archive_status(db: Session = Depends(get_db)):
    config = read_wecom_archive_config()
    state = archive_state_for_config(db, config.corp_id)
    db.commit()
    return {
        **wecom_archive_config_status(config),
        "seq": state.seq,
        "last_msg_time": timeline_time(state.last_msg_time) if state.last_msg_time else "",
        "last_error": state.last_error,
        "updated_at": timeline_time(state.updated_at),
    }


@app.post("/api/wecom-archive/sync")
def sync_wecom_archive(payload: WecomArchiveSyncIn, request: Request = None, db: Session = Depends(get_db)):
    config = read_wecom_archive_config()
    state = archive_state_for_config(db, config.corp_id)
    try:
        if payload.messages:
            envelopes = [archive_envelope_from_payload(item) for item in payload.messages]
        else:
            status = wecom_archive_config_status(config)
            if not status["configured"]:
                raise HTTPException(400, status["detail"])
            envelopes = pull_archive_messages(state.seq, payload.limit, config)

        normalized = []
        for envelope in envelopes:
            item = normalize_archive_message(envelope, config)
            if item:
                normalized.append(item)

        results = []
        for group in group_archive_messages(normalized):
            conversation_payload = RpaConversationIn(
                target_name=group["target_name"],
                family_id=group["family_id"],
                messages=[
                    RpaMessageIn(
                        speaker=msg.speaker,
                        content=msg.content,
                        message_time=msg.message_time.isoformat(),
                        source=msg.source,
                        external_id=msg.external_id,
                    )
                    for msg in group["messages"]
                ],
                latest_message=group["latest_message"],
                auto_generate_reply=payload.auto_generate_reply,
                auto_create_reply_task=payload.auto_create_reply_task,
                auto_generate_all_agents=payload.auto_generate_all_agents,
                conversation_opened=True,
                empty_conversation_ok=False,
            )
            results.append(
                sync_conversation_payload(
                    db,
                    conversation_payload,
                    actor=actor_from_request(request, "企业微信存档"),
                    source_prefix="企业微信存档",
                )
            )

        if envelopes:
            state.seq = max(state.seq or 0, max((item.seq for item in envelopes), default=state.seq or 0))
        if normalized:
            state.last_msg_time = max(item.message_time for item in normalized)
        state.last_error = ""
        state.updated_at = datetime.utcnow()
        db.commit()
        return {
            "status": "ok",
            "pulled": len(envelopes),
            "normalized": len(normalized),
            "groups": len(results),
            "seq": state.seq,
            "results": results,
        }
    except HTTPException as exc:
        state.last_error = str(exc.detail)
        state.updated_at = datetime.utcnow()
        db.commit()
        raise
    except Exception as exc:
        state.last_error = str(exc)
        state.updated_at = datetime.utcnow()
        db.commit()
        raise HTTPException(500, f"企业微信会话存档同步失败：{exc}") from exc


# 周报列表接口。
@app.get("/api/reports")
def list_reports(request: Request = None, db: Session = Depends(get_db)):
    query = apply_family_id_scope(db.query(WeeklyReport), WeeklyReport.family_id, db, request)
    return maybe_redact_for_request([as_dict(r) for r in query.order_by(WeeklyReport.id.desc()).all()], request)


# 一键把未审核周报全部标成已审核。
@app.post("/api/reports/approve-all")
def approve_all_reports(request: Request = None, db: Session = Depends(get_db)):
    count = 0
    query = apply_family_id_scope(db.query(WeeklyReport).filter(WeeklyReport.status != "approved"), WeeklyReport.family_id, db, request)
    for report in query.all():
        report.status = "approved"
        count += 1
    db.commit()
    return {"approved": count}


# 单条周报人工更新接口。
@app.put("/api/reports/{report_id}")
def update_report(report_id: int, payload: ReportUpdate, request: Request = None, db: Session = Depends(get_db)):
    report = db.get(WeeklyReport, report_id)
    if not report:
        raise HTTPException(404, "周报不存在")
    ensure_family_id_access(db, report.family_id, request)
    if report.final_text != payload.final_text or report.status != payload.status:
        report.parent_ack_at = None
        report.parent_ack_note = ""
        report.parent_feedback_score = 0
        report.parent_feedback_note = ""
        report.parent_feedback_at = None
    report.final_text = payload.final_text
    report.status = payload.status
    db.commit()
    return as_dict(report)


# 家长画像列表接口。
@app.get("/api/profiles")
def list_profiles(request: Request = None, db: Session = Depends(get_db)):
    query = apply_family_id_scope(db.query(ParentProfile), ParentProfile.family_id, db, request)
    return maybe_redact_for_request([as_dict(p) for p in query.order_by(ParentProfile.family_id).all()], request)


# 模板列表接口。
@app.get("/api/templates")
def list_templates(db: Session = Depends(get_db)):
    return [as_dict(t) for t in db.query(Template).order_by(Template.id).all()]


# 新增模板。
@app.post("/api/templates")
def create_template(payload: TemplateIn, db: Session = Depends(get_db)):
    template = Template(**payload.model_dump())
    db.add(template)
    db.commit()
    return as_dict(template)


# 更新模板。
@app.put("/api/templates/{template_id}")
def update_template(template_id: int, payload: TemplateIn, db: Session = Depends(get_db)):
    template = db.get(Template, template_id)
    if not template:
        raise HTTPException(404, "模板不存在")
    for key, value in payload.model_dump().items():
        setattr(template, key, value)
    db.commit()
    return as_dict(template)


# 切换模板启用状态。
@app.post("/api/templates/{template_id}/toggle")
def toggle_template(template_id: int, db: Session = Depends(get_db)):
    template = db.get(Template, template_id)
    if not template:
        raise HTTPException(404, "模板不存在")
    template.enabled = "N" if template.enabled == "Y" else "Y"
    db.commit()
    return as_dict(template)


# 重新扫描所有消息里的打卡关键词，补齐打卡记录。
@app.post("/api/scan-checkins")
def scan_checkins(request: Request = None, db: Session = Depends(get_db)):
    created = 0
    for msg in apply_family_id_scope(db.query(RawMessage), RawMessage.family_id, db, request).all():
        status = msg.checkin_status or detect_checkin(msg.content)
        if not status:
            continue
        msg.checkin_status = status
        exists = db.query(CheckinRecord).filter(CheckinRecord.message_id == msg.id).first()
        if not exists:
            db.add(CheckinRecord(family_id=msg.family_id, message_id=msg.id, checkin_type=status, evidence=msg.content))
            created += 1
    db.commit()
    return {"checkin_records_created": created}


# 从已审核周报创建发送任务。
@app.post("/api/send-tasks/from-approved-reports")
def create_tasks_from_reports(request: Request = None, db: Session = Depends(get_db)):
    created = 0
    reports = apply_family_id_scope(db.query(WeeklyReport).filter(WeeklyReport.status == "approved"), WeeklyReport.family_id, db, request).all()
    actor = actor_from_request(request)
    for report in reports:
        _, was_created = ensure_weekly_report_send_task(db, report, actor)
        if was_created:
            created += 1
    db.commit()
    return {"created": created}


@app.post("/api/reports/{report_id}/send-task")
def create_task_from_report(report_id: int, request: Request = None, db: Session = Depends(get_db)):
    report = db.get(WeeklyReport, report_id)
    if not report:
        raise HTTPException(404, "周报不存在")
    ensure_family_id_access(db, report.family_id, request)
    task, created = ensure_weekly_report_send_task(db, report, actor_from_request(request))
    db.commit()
    return {"created": created, "report": as_dict(report), "task": as_dict(task)}


# 按场景规则和话术模板创建发送任务。
@app.post("/api/send-tasks/from-scenes")
def create_tasks_from_scenes(request: Request = None, db: Session = Depends(get_db)):
    created = 0
    templates = {t.scene: t.content for t in db.query(Template).filter(Template.enabled == "Y").all()}
    query = apply_family_id_scope(db.query(RawMessage), RawMessage.family_id, db, request)
    for msg in query.order_by(RawMessage.message_time.desc()).limit(200):
        scene = detect_scene(msg.content)
        if not scene or scene == "转人工" or scene not in templates:
            continue
        exists = db.query(SendTask).filter(SendTask.family_id == msg.family_id, SendTask.scene == scene, SendTask.content == templates[scene]).first()
        if exists:
            continue
        family = db.query(Family).filter(Family.family_id == msg.family_id).first()
        add_send_task_with_audit(
            db,
            SendTask(family_id=msg.family_id, target_name=family.parent_nickname if family else msg.family_id, scene=scene, content=validate_send_task_content(templates[scene]), send_mode="dry_run"),
            "create",
            actor_from_request(request),
            f"场景「{scene}」自动创建发送任务",
        )
        created += 1
    db.commit()
    return {"created": created}


# 发送任务列表接口（可选按 status / device_id 过滤，便于看板和调试）。
@app.get("/api/send-tasks")
def list_send_tasks(status: str = "", device_id: str = "", request: Request = None, db: Session = Depends(get_db)):
    recovered = requeue_stale_assigned_tasks(db, request=request, actor=actor_from_request(request, "控制端维护"))
    if recovered:
        db.commit()
    query = apply_family_id_scope(db.query(SendTask), SendTask.family_id, db, request)
    if status:
        query = query.filter(SendTask.status == status)
    if device_id:
        query = query.filter(SendTask.device_id == device_id)
    return maybe_redact_for_request([send_task_view(t, request, db) for t in query.order_by(SendTask.id.desc()).all()], request)


@app.post("/api/send-tasks/preflight")
def preflight_send_task(payload: SendTaskPreflightIn, request: Request = None, db: Session = Depends(get_db)):
    return build_send_task_preflight(db, payload, request)


# 直接新增一条发送任务。
@app.post("/api/send-tasks")
def create_send_task(payload: SendTaskIn, request: Request = None, db: Session = Depends(get_db)):
    data = payload.model_dump()
    ensure_family_id_access(db, data.get("family_id", ""), request)
    data["content"] = validate_send_task_content(data.get("content", ""))
    data["send_mode"] = validate_send_mode_submit(data.get("send_mode", "dry_run"), bool(data.pop("confirm_real_send", False)))
    if data.get("device_id"):
        ensure_new_task_operation_allowed(request, "assign_device")
    if data["send_mode"] == "real_send":
        ensure_new_task_operation_allowed(request, "confirm_real_send")
        data["device_id"] = resolve_real_send_device_binding(db, data.get("device_id", ""), data.get("target_name", ""))
        validate_real_send_device_binding(data["device_id"])
        validate_real_send_risk(db, data.get("target_name", ""), data["content"])
    else:
        data["device_id"] = validate_task_device_binding(db, data.get("device_id", ""), data.get("target_name", ""))
    task = SendTask(**data)
    ensure_real_send_readiness(db, task)
    add_send_task_with_audit(db, task, "create", actor_from_request(request), "控制端手动创建发送任务")
    db.commit()
    return send_task_view(task, request, db)


# 更新发送任务的内容和状态。
@app.put("/api/send-tasks/{task_id}")
def update_send_task(task_id: int, payload: SendTaskUpdate, request: Request = None, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    ensure_task_family_access(db, task, request)
    if payload.status not in {"pending", "approved", "assigned", "cancelled", "sent", "failed", "dry_run"}:
        raise HTTPException(400, "status 不合法")
    before = send_task_snapshot(task)
    previous_mode = task.send_mode
    target_name = payload.target_name or task.target_name
    scene = payload.scene or task.scene
    content = task.content
    device_id = task.device_id if payload.device_id is None else payload.device_id
    requested_mode = payload.send_mode or task.send_mode
    if requested_mode == "real_send" and task.send_mode != "real_send":
        ensure_task_operation_allowed(task, request, "confirm_real_send")
    confirming_real_send = requested_mode == "real_send" and task.send_mode != "real_send"
    if payload.device_id is not None and payload.device_id != task.device_id and not confirming_real_send:
        ensure_task_operation_allowed(task, request, "assign_device")
    has_edit = any([
        bool(payload.target_name and payload.target_name != task.target_name),
        bool(payload.scene and payload.scene != task.scene),
        bool(payload.content and payload.content != task.content),
        bool(payload.status and payload.status != task.status),
        bool(payload.send_mode and payload.send_mode != task.send_mode and payload.send_mode != "real_send"),
    ])
    if has_edit:
        ensure_task_operation_allowed(task, request, "edit")
    if payload.content:
        content = validate_send_task_content(payload.content)
    send_mode = task.send_mode
    if payload.send_mode:
        send_mode = validate_send_mode_submit(payload.send_mode, payload.confirm_real_send, task.send_mode)
    if send_mode == "real_send":
        clean_device_id = resolve_real_send_device_binding(db, device_id or "", target_name)
        validate_real_send_device_binding(clean_device_id)
        validate_real_send_risk(db, target_name, content, exclude_task_id=task.id)
    else:
        clean_device_id = validate_task_device_binding(db, device_id or "", target_name)
    next_status = payload.status
    if previous_mode != "real_send" and send_mode == "real_send":
        next_status = "pending"
    task.target_name = target_name
    task.scene = scene
    task.content = content
    task.send_mode = send_mode
    task.device_id = clean_device_id
    task.status = next_status
    if task.status == "pending":
        task.scheduled_at = datetime.utcnow()
    ensure_real_send_readiness(db, task)
    action = "confirm_real_send" if previous_mode != "real_send" and send_mode == "real_send" else "update"
    summary = "确认真实发送" if action == "confirm_real_send" else "更新发送任务"
    audit_send_task_change(db, task, action, actor_from_request(request), summary, before)
    sync_weekly_report_send_status(db, task)
    db.commit()
    return send_task_view(task, request, db)


@app.post("/api/send-tasks/{task_id}/cancel")
def cancel_send_task(task_id: int, request: Request = None, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    ensure_task_family_access(db, task, request)
    ensure_task_operation_allowed(task, request, "cancel")
    before = send_task_snapshot(task)
    task.status = "cancelled"
    audit_send_task_change(db, task, "cancel", actor_from_request(request), "取消发送任务", before)
    sync_weekly_report_send_status(db, task, "cancelled")
    db.commit()
    return send_task_view(task, request, db)


@app.post("/api/send-tasks/{task_id}/dry-run")
def queue_task_dry_run(task_id: int, request: Request = None, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    ensure_task_family_access(db, task, request)
    ensure_task_operation_allowed(task, request, "dry_run")
    if task.status != "pending":
        raise HTTPException(400, "只有 pending 状态的任务可以发起试运行")
    content = validate_send_task_content(task.content)
    clean_device_id = validate_task_device_binding(db, task.device_id or "", task.target_name)
    before = send_task_snapshot(task)
    task.content = content
    task.device_id = clean_device_id
    task.send_mode = "dry_run"
    task.status = "pending"
    task.scheduled_at = datetime.utcnow()
    audit_send_task_change(db, task, "queue_dry_run", actor_from_request(request), "控制端发起企微 dry-run 试运行", before)
    sync_weekly_report_send_status(db, task, "pending")
    db.commit()
    return send_task_view(task, request, db)


@app.post("/api/send-tasks/{task_id}/real-send")
def queue_task_real_send(
    task_id: int,
    payload: SendTaskRealSendIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    ensure_task_family_access(db, task, request)
    ensure_task_operation_allowed(task, request, "confirm_real_send")
    content_source = task.content if not payload or payload.content is None else payload.content
    content = validate_send_task_content(content_source)
    device_id = task.device_id if not payload or payload.device_id is None else payload.device_id
    clean_device_id = resolve_real_send_device_binding(db, device_id or "", task.target_name)
    validate_real_send_device_binding(clean_device_id)
    validate_real_send_risk(db, task.target_name, content, exclude_task_id=task.id)
    before = send_task_snapshot(task)
    task.content = content
    task.device_id = clean_device_id
    task.send_mode = "real_send"
    task.status = "pending"
    task.last_error = ""
    task.next_retry_at = None
    task.scheduled_at = datetime.utcnow()
    ensure_real_send_readiness(db, task)
    audit_send_task_change(db, task, "confirm_real_send", actor_from_request(request), "控制端确认企微真实发送", before)
    sync_weekly_report_send_status(db, task, "pending")
    db.commit()
    return send_task_view(task, request, db)


@app.post("/api/send-tasks/{task_id}/retry")
def retry_failed_task(task_id: int, request: Request = None, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    ensure_task_family_access(db, task, request)
    ensure_task_operation_allowed(task, request, "retry")
    if task.status != "failed":
        raise HTTPException(400, "只有 failed 状态的任务可以重试")
    content = validate_send_task_content(task.content)
    if task.send_mode == "real_send":
        clean_device_id = resolve_real_send_device_binding(db, task.device_id or "", task.target_name)
        validate_real_send_device_binding(clean_device_id)
        validate_real_send_risk(db, task.target_name, content, exclude_task_id=task.id)
    else:
        clean_device_id = validate_task_device_binding(db, task.device_id or "", task.target_name)
    before = send_task_snapshot(task)
    now = datetime.utcnow()
    task.content = content
    task.device_id = clean_device_id
    task.status = "pending"
    task.next_retry_at = now
    task.scheduled_at = now
    audit_send_task_change(db, task, "manual_retry", actor_from_request(request), "人工复核后重新加入发送队列", before)
    sync_weekly_report_send_status(db, task, "pending")
    db.commit()
    return send_task_view(task, request, db)


@app.post("/api/send-tasks/{task_id}/result")
def record_send_result(task_id: int, payload: SendResultIn, request: Request = None, db: Session = Depends(get_db)):
    device = device_from_optional_headers(db, request)
    if device is None:
        raise HTTPException(401, "缺少设备令牌，禁止匿名回写发送结果")
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task.device_id and task.device_id != device.device_id:
        raise HTTPException(403, "无权回写他人设备的任务结果")
    client_result_id = (payload.client_result_id or "").strip()
    if client_result_id:
        existing_log = (
            db.query(SendLog)
            .filter(SendLog.task_id == task.id, SendLog.client_result_id == client_result_id)
            .first()
        )
        if existing_log:
            return send_log_view(existing_log)
    if task.status in ("sent", "cancelled"):
        raise HTTPException(409, "任务已处于终态，不可重复回写结果")
    ensure_task_family_access(db, task, request)
    if payload.status not in {"sent", "failed", "skipped", "dry_run"}:
        raise HTTPException(400, "status 只能是 sent/failed/skipped/dry_run")
    screenshot_path = store_send_screenshot(task.id, payload.screenshot_base64)
    before = send_task_snapshot(task)
    finished_at = datetime.utcnow()
    verify_status, verify_detail, verified_at = normalize_send_verification(payload, task, finished_at)
    if send_log_mode(task) == "real_send" and payload.status == "sent" and verify_status == "confirmed":
        proof_reason = real_send_landed_proof_reason(db, task, payload.device_id)
        if proof_reason:
            verify_status = "unknown"
            verify_detail = (
                "设备上报 confirmed，但服务端未找到本次目标会话回读落库证明；"
                f"必须实际回到目标群/私聊读取聊天记录并完成落库。原因：{proof_reason}"
            )
            verified_at = payload.verified_at
    result_status = payload.status
    result_detail = payload.detail or ""
    if send_log_mode(task) == "real_send" and payload.status == "sent" and verify_status != "confirmed":
        result_status = "failed"
        verify_status = verify_status or "unknown"
        verify_detail = verify_detail or "设备上报 sent，但未提供目标会话回读命中证据"
        result_detail = (
            "SEND_CONFIRM_FAILED: 真实发送热键已触发，但未获得目标会话回读确认；"
            f"原始设备结果=sent。{result_detail}"
        ).strip()
    retry_action = ""
    retry_summary = ""
    if result_status == "failed":
        retry_action, retry_summary = apply_failed_send_retry_policy(task, result_detail, finished_at)
    elif result_status == "skipped" and "REAL_SEND_GUARD" in result_detail:
        task.status = "pending"
        task.last_error = result_detail
        task.next_retry_at = finished_at + timedelta(seconds=AUTO_RETRY_DELAY_SECONDS)
        task.scheduled_at = task.next_retry_at
        retry_action = "policy_wait"
        retry_summary = f"设备真实发送开关未开启，任务保持原设备待发送；下次检查 {timeline_time(task.next_retry_at)}"
    else:
        task.status = result_status
        task.last_error = ""
        task.next_retry_at = None
        if result_status in {"sent", "dry_run"}:
            task.retry_count = 0
    log = SendLog(
        task_id=task.id,
        family_id=task.family_id,
        target_name=task.target_name,
        status=result_status,
        send_mode=send_log_mode(task),
        detail=result_detail,
        device_id=payload.device_id or task.device_id,
        client_result_id=client_result_id,
        screenshot_path=screenshot_path,
        verify_status=verify_status,
        verify_detail=verify_detail,
        verified_at=verified_at,
        sent_at=finished_at,
    )
    db.add(log)
    actor = actor_from_request(request, f"设备:{payload.device_id}" if payload.device_id else "RPA被控端")
    audit_send_task_change(db, task, "result", actor, f"回写发送结果：{result_status}", before)
    if retry_action == "auto_retry":
        log_audit_event(db, "send_task", task.id, "auto_retry", actor, retry_summary, before=before, after=send_task_snapshot(task))
        sync_weekly_report_send_status(db, task, "pending")
    elif retry_action == "policy_wait":
        log_audit_event(db, "send_task", task.id, "policy_wait", actor, retry_summary, before=before, after=send_task_snapshot(task))
        sync_weekly_report_send_status(db, task, "pending")
    else:
        if retry_action == "alert":
            log_audit_event(db, "send_task", task.id, "send_alert", actor, retry_summary, before=before, after=send_task_snapshot(task))
        sync_weekly_report_send_status(db, task, result_status, finished_at)
    db.commit()
    return send_log_view(log)


def ensure_manual_send_log_verification_allowed(log: SendLog, request: Request | None) -> None:
    role = operation_role_from_request(request)
    if role != "admin":
        role_label = {"coach": "陪跑师", "readonly": "只读"}.get(role, role or "未知")
        raise HTTPException(403, f"只有超管可以人工核验真实发送结果（当前角色：{role_label}）")
    if log.send_mode != "real_send":
        raise HTTPException(400, "只有企微真实发送日志需要人工核验")
    if not send_log_has_real_send_attempt_evidence(log):
        raise HTTPException(400, "该日志没有真实发送热键触发证据，不能人工标记为已发；请按原失败原因处理或重新下发")


def manual_send_verify_detail(log: SendLog, confirmed: bool, detail: str) -> tuple[str, str]:
    clean_detail = (detail or "").strip()
    if not clean_detail:
        raise HTTPException(400, "必须填写人工核验证据，说明你在目标群/私聊中看到或未看到的结果")
    target = (log.target_name or "目标会话").strip()
    if confirmed:
        verify_detail = (
            f"VERIFY_CONFIRMED: 人工核对目标「{target}」群/私聊回读命中本次内容，"
            f"回读已落库（人工核验）。证据：{clean_detail}"
        )
        detail_text = f"MANUAL_VERIFY_CONFIRMED: 超管人工核对目标「{target}」实际已发送成功。"
    else:
        verify_detail = f"人工核对目标「{target}」群/私聊未看到本次内容。证据：{clean_detail}"
        detail_text = f"MANUAL_VERIFY_FAILED: 超管人工核对目标「{target}」未确认发送成功。"
    return detail_text, verify_detail


@app.post("/api/send-logs/{log_id}/manual-verification")
def manually_verify_send_log(
    log_id: int,
    payload: SendLogManualVerificationIn,
    request: Request = None,
    db: Session = Depends(get_db),
):
    log = db.get(SendLog, log_id)
    if not log:
        raise HTTPException(404, "发送日志不存在")
    ensure_family_id_access(db, log.family_id, request)
    ensure_manual_send_log_verification_allowed(log, request)
    task = db.get(SendTask, log.task_id) if log.task_id else None
    if task:
        ensure_task_family_access(db, task, request)
    before_task = send_task_snapshot(task) if task else None
    before_log = as_dict(log)
    now = datetime.utcnow()
    detail_text, verify_detail = manual_send_verify_detail(log, payload.confirmed, payload.detail)
    actor = actor_from_request(request, "控制端")

    log.detail = detail_text
    log.verify_detail = verify_detail
    log.verified_at = now
    if payload.confirmed:
        log.status = "sent"
        log.verify_status = "confirmed"
        if task:
            task.status = "sent"
            task.last_error = ""
            task.next_retry_at = None
            task.retry_count = 0
            sync_weekly_report_send_status(db, task, "sent", now)
    else:
        log.status = "failed"
        log.verify_status = "failed"
        if task:
            task.status = "failed"
            task.last_error = verify_detail
            task.next_retry_at = None
            sync_weekly_report_send_status(db, task, "failed", now)

    if task:
        audit_send_task_change(
            db,
            task,
            "manual_send_verify",
            actor,
            "人工核验真实发送结果并更新任务状态",
            before_task,
        )
    log_audit_event(
        db,
        "send_log",
        log.id,
        "manual_send_verify",
        actor,
        "人工核验真实发送结果：已确认成功" if payload.confirmed else "人工核验真实发送结果：未确认成功",
        before=before_log,
        after=as_dict(log),
    )
    db.commit()
    return {"log": send_log_view(log), "task": send_task_view(task, request, db) if task else None}


def send_task_to_web_chat(db: Session, task: SendTask, actor: str = "控制端", action: str = "web_send") -> tuple[RawMessage, SendLog]:
    family = db.query(Family).filter(Family.family_id == task.family_id).first()
    if not family:
        raise HTTPException(404, "任务对应家庭不存在，无法发送到网页通讯")
    if task.status != "pending":
        raise HTTPException(400, "只有 pending 状态的任务可以发送")
    task.send_mode = validate_send_task_execution_guard(task)
    task.content = validate_send_task_content(task.content)
    before = send_task_snapshot(task)

    message = RawMessage(
        family_id=task.family_id,
        speaker=family.coach_name or "陪跑师",
        content=task.content,
        source="网页通讯发送任务",
        checkin_status=detect_checkin(task.content),
        is_effective="Y",
    )
    finished_at = datetime.utcnow()
    task.status = "sent"
    log = SendLog(
        task_id=task.id,
        family_id=task.family_id,
        target_name=task.target_name,
        status="sent",
        send_mode=send_log_mode(task),
        detail="WEB_CHAT: 已发送到网页通讯会话。",
        sent_at=finished_at,
    )
    db.add(message)
    db.add(log)
    audit_send_task_change(db, task, action, actor, "发送到网页通讯会话", before)
    sync_weekly_report_send_status(db, task, "sent", finished_at)
    return message, log


@app.post("/api/send-tasks/{task_id}/web-send")
def web_send(task_id: int, request: Request = None, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    ensure_task_family_access(db, task, request)
    ensure_task_operation_allowed(task, request, "web_send")
    message, log = send_task_to_web_chat(db, task, actor_from_request(request), "web_send")
    db.commit()
    return {"task": send_task_view(task, request, db), "message": as_dict(message), "log": send_log_view(log)}


@app.post("/api/send-tasks/web-send-all")
def web_send_all(request: Request = None, db: Session = Depends(get_db)):
    sent = 0
    skipped = 0
    query = apply_family_id_scope(db.query(SendTask).filter(SendTask.status == "pending"), SendTask.family_id, db, request)
    for task in query.all():
        try:
            ensure_task_operation_allowed(task, request, "web_send")
            send_task_to_web_chat(db, task, actor_from_request(request), "web_send_all")
            sent += 1
        except HTTPException:
            skipped += 1
    db.commit()
    return {"sent": sent, "skipped": skipped}


@app.get("/api/send-logs")
def list_send_logs(request: Request = None, db: Session = Depends(get_db)):
    query = apply_family_id_scope(db.query(SendLog), SendLog.family_id, db, request)
    return maybe_redact_for_request([send_log_view(l) for l in query.order_by(SendLog.id.desc()).all()], request)


@app.get("/api/audit-logs")
def list_audit_logs(entity_type: str = "", entity_id: int = 0, limit: int = 200, db: Session = Depends(get_db)):
    safe_limit = min(max(limit, 1), 500)
    query = db.query(AuditLog)
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if entity_id:
        query = query.filter(AuditLog.entity_id == entity_id)
    return [as_dict(l) for l in query.order_by(AuditLog.id.desc()).limit(safe_limit).all()]


@app.get("/api/send-artifacts/{filename}")
def get_send_artifact(filename: str, request: Request = None, db: Session = Depends(get_db)):
    path = resolve_send_screenshot(filename)
    artifact_url = f"/api/send-artifacts/{filename}"
    log = db.query(SendLog).filter(SendLog.screenshot_path == artifact_url).one_or_none()
    if not log:
        raise HTTPException(404, "截图不存在")
    require_family_for_request(db, log.family_id, request)
    return FileResponse(path)


# ============ 设备（多被控端总控）============
HEARTBEAT_ONLINE_SECONDS = 90      # 心跳在这个时间窗内算 online
# 设备鉴权依赖：claim / heartbeat 等设备接口校验 X-Device-Id + X-Device-Token。
def require_device(
    x_device_id: str = Header(...),
    x_device_token: str = Header(...),
    db: Session = Depends(get_db),
) -> Device:
    dev = db.query(Device).filter(Device.device_id == x_device_id).first()
    if not dev or dev.token != x_device_token:
        raise HTTPException(401, "设备未注册或 token 不正确")
    return dev


# 把设备 ORM 转字典并补上 online 状态、负责会话数、任务统计，供看板展示。
def device_view(dev: Device, db: Session) -> dict:
    data = as_dict(dev)
    online = device_online(dev)
    data["online"] = online
    try:
        conversation_list = json.loads(dev.conversations or "[]")
    except Exception:
        conversation_list = []
    data["conversation_list"] = [str(item).strip() for item in conversation_list if str(item).strip()]
    data["conversation_count"] = len(data["conversation_list"])
    counts = {}
    for st in ("pending", "assigned", "sent", "failed"):
        counts[st] = db.query(SendTask).filter(SendTask.device_id == dev.device_id, SendTask.status == st).count()
    data["task_counts"] = counts
    data["real_send_policy_label"] = "允许真实发送" if dev.allow_real_send else "仅试运行"
    data["conversation_scope_label"] = "全会话" if dev.allow_any_conversation else "白名单会话"
    data["outbox_blocked"] = bool((dev.outbox_pending_count or 0) > 0)
    data["outbox_status_label"] = f"结果待补传 {dev.outbox_pending_count} 条" if data["outbox_blocked"] else "结果已同步"
    proof_summary = device_conversation_proof_summary(db, dev)
    latest_check = (
        db.query(DeviceConversationCheck)
        .filter(DeviceConversationCheck.device_id == dev.device_id)
        .order_by(DeviceConversationCheck.verified_at.desc())
        .first()
    )
    data["conversation_proof_count"] = proof_summary["ready_count"]
    data["conversation_proof_total"] = proof_summary["total"]
    data["conversation_proof_missing_count"] = proof_summary["missing_count"]
    data["conversation_proof_missing_targets"] = proof_summary["missing_targets"]
    data["conversation_proof_issue_targets"] = proof_summary["issue_targets"]
    data["conversation_proof_coverage"] = proof_summary["coverage"]
    data["conversation_proof_ready"] = proof_summary["ready"]
    data["conversation_proof_label"] = proof_summary["label"]
    data["last_conversation_proof_at"] = timeline_time(latest_check.verified_at) if latest_check else ""
    data["last_conversation_proof_target"] = latest_check.target_name if latest_check else ""
    since = datetime.utcnow() - timedelta(hours=24)
    real_logs = (
        db.query(SendLog)
        .filter(SendLog.send_mode == "real_send", SendLog.device_id == dev.device_id, SendLog.sent_at >= since)
        .all()
    )
    real_metrics = real_send_closure_metrics(real_logs)
    data["real_send_attempted_24h"] = real_metrics["attempted_24h"]
    data["real_send_confirmed_24h"] = real_metrics["confirmed_24h"]
    data["real_send_confirm_failed_24h"] = real_metrics["confirm_failed_24h"]
    data["real_send_confirm_rate_24h"] = real_metrics["confirm_rate"]
    data["real_send_success_label"] = (
        f"近24小时真发 {real_metrics['attempted_24h']} 条，回读确认 {real_metrics['confirmed_24h']} 条，确认率 {real_metrics['confirm_rate']}%"
        if real_metrics["attempted_24h"]
        else "近24小时暂无真实发送"
    )
    return data


def device_task_payload(task: SendTask, dev: Device) -> dict:
    data = as_dict(task)
    data["device_allow_real_send"] = bool(dev.allow_real_send)
    data["server_allowed_target"] = True
    data["device_allow_any_conversation"] = bool(dev.allow_any_conversation)
    return data


# 注册或更新设备：新建时自动生成 token 并返回（之后 RPA 用它鉴权）。
@app.post("/api/devices")
def register_device(payload: DeviceIn, db: Session = Depends(get_db)):
    dev = db.query(Device).filter(Device.device_id == payload.device_id).first()
    convs = json.dumps(payload.conversations, ensure_ascii=False) if payload.conversations else None
    if dev:
        dev.name = payload.name or dev.name
        dev.note = payload.note or dev.note
        if payload.allow_real_send is not None:
            dev.allow_real_send = bool(payload.allow_real_send)
        if payload.allow_any_conversation is not None:
            dev.allow_any_conversation = bool(payload.allow_any_conversation)
        if convs is not None:
            dev.conversations = convs
    else:
        dev = Device(
            device_id=payload.device_id,
            name=payload.name,
            note=payload.note,
            token=secrets.token_hex(16),
            conversations=convs or "[]",
            allow_real_send=bool(payload.allow_real_send),
            allow_any_conversation=bool(payload.allow_any_conversation),
        )
        db.add(dev)
    db.commit()
    return as_dict(dev)


# 生成某台设备的「接入包」zip：被控端脚本 + 注入 token 的配置 + 一键启动 bat，发给对方双击即用。
@app.get("/api/devices/{device_id}/package")
def download_device_package(device_id: str, server_url: str = "", api_tls_verify: bool = True, db: Session = Depends(get_db)):
    dev = db.query(Device).filter(Device.device_id == device_id).first()
    if not dev:
        raise HTTPException(404, "设备不存在")
    try:
        convs = json.loads(dev.conversations or "[]")
    except Exception:
        convs = []
    base_url = server_url.strip() or "http://127.0.0.1:8000"

    # 被控端 config.json：先取 config.example.json 的默认，再覆盖设备/服务器/云端定位相关字段。
    try:
        client_cfg = json.loads((ROOT / "rpa" / "config.example.json").read_text(encoding="utf-8"))
    except Exception:
        client_cfg = {}
    client_cfg.update({
        "api_base_url": base_url,
        "api_tls_verify": bool(api_tls_verify),
        "api_ca_file": "",
        "device_id": dev.device_id,
        "device_token": dev.token,
        "watch_conversations": convs,
        "allowed_conversations": convs,
        "use_local_ocr": False,          # 被控端走 ARK 云端定位，不装 paddleocr
        "use_ark_vision_fallback": True,
        "enable_search_fallback": True,
        "dry_run": True,                 # 默认安全：只粘贴不发；真实发送走任务 real_send + 控制端设备策略双确认
        "allow_real_send": False,         # 兼容旧客户端的本地兜底；新客户端以控制端设备开关为准
        "auto_launch_wecom": False,
    })

    entries: dict[str, bytes] = {}

    def add_file(src: Path, arc: str) -> None:
        if src.exists():
            entries[arc] = src.read_bytes()

    def add_text(arc: str, text: str) -> None:
        entries[arc] = text.encode("utf-8")

    # 复制项目文件，保持相对结构让 import 正常（wecom_sender 会把 main/ 加入 sys.path 再 import app.services）
    add_file(ROOT / "rpa" / "wecom_sender.py", "rpa/wecom_sender.py")
    add_file(ROOT / "rpa" / "send_guard.py", "rpa/send_guard.py")
    add_file(ROOT / "rpa" / "send_batch_guard.py", "rpa/send_batch_guard.py")
    add_file(ROOT / "rpa" / "result_outbox.py", "rpa/result_outbox.py")
    add_file(ROOT / "app" / "services" / "ark_client.py", "app/services/ark_client.py")
    add_text("app/__init__.py", "")
    add_text("app/services/__init__.py", "")
    ark_path = ROOT / "config" / "ark.json"
    if ark_path.exists():
        add_file(ark_path, "config/ark.json")
    # 注入的设备专属配置
    add_text("rpa/config.json", json.dumps(client_cfg, ensure_ascii=False, indent=2))
    # 启动脚本 / 依赖 / 说明 / 完整性校验
    for src, arc in [
        (ROOT / "rpa" / "requirements-client.txt", "requirements-client.txt"),
        (ROOT / "rpa" / "templates" / "启动.bat", "启动.bat"),
        (ROOT / "rpa" / "templates" / "watchdog.ps1", "watchdog.ps1"),
        (ROOT / "rpa" / "templates" / "install_autostart.bat", "install_autostart.bat"),
        (ROOT / "rpa" / "templates" / "uninstall_autostart.bat", "uninstall_autostart.bat"),
        (ROOT / "rpa" / "templates" / "校验接入包.ps1", "校验接入包.ps1"),
        (ROOT / "rpa" / "templates" / "使用说明.txt", "使用说明.txt"),
    ]:
        add_file(src, arc)
    add_text(
        "package_manifest.json",
        json.dumps(
            build_package_manifest(entries, package_type="rpa-client-script", device_id=dev.device_id),
            ensure_ascii=False,
            indent=2,
        ),
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, data in entries.items():
            zf.writestr(arc, data)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=device_{device_id}.zip"},
    )


# 设备列表（含 online 状态和任务统计），看板用。
@app.get("/api/devices")
def list_devices(db: Session = Depends(get_db)):
    return [device_view(dev, db) for dev in db.query(Device).order_by(Device.device_id).all()]


@app.post("/api/devices/{device_id}/conversation-checks")
def queue_device_conversation_check(
    device_id: str,
    payload: DeviceConversationCheckRequestIn,
    request: Request = None,
    db: Session = Depends(get_db),
):
    ensure_new_task_operation_allowed(request, "assign_device")
    dev = db.query(Device).filter(Device.device_id == device_id).first()
    if not dev:
        raise HTTPException(404, "设备不存在")
    target_name = (payload.target_name or "").strip()
    if not target_name:
        raise HTTPException(400, "请填写要校验的群/私聊名称")
    validate_device_conversation_scope(dev, target_name)
    task = create_conversation_check_task(
        db,
        dev,
        target_name,
        payload.family_id or f"WECOM_{target_name}",
        actor_from_request(request),
    )
    db.commit()
    return send_task_view(task, request, db)


def create_conversation_check_task(
    db: Session,
    dev: Device,
    target_name: str,
    family_id: str,
    actor: str,
    auto_prepare_real_send: bool = False,
) -> SendTask:
    content = (
        f"{REAL_SEND_PREP_CONVERSATION_CHECK_PREFIX}打开目标会话并读取可见消息，证明成功后同设备再继续真实发送。"
        if auto_prepare_real_send
        else CONVERSATION_CHECK_CONTENT
    )
    task = SendTask(
        family_id=((family_id or f"WECOM_{target_name}")[:64]),
        target_name=target_name,
        scene=CONVERSATION_CHECK_SCENE,
        content=content,
        device_id=dev.device_id,
        send_mode="dry_run",
        status="pending",
        scheduled_at=datetime.utcnow(),
    )
    add_send_task_with_audit(
        db,
        task,
        "conversation_check",
        actor,
        f"{'自动下发真实发送前置' if auto_prepare_real_send else '下发设备'}「{dev.device_id}」只读校验「{target_name}」",
    )
    return task


def queue_real_send_missing_proof_checks_for_claim(
    db: Session,
    dev: Device,
    convs: list[str],
    now: datetime,
    limit: int,
) -> list[SendTask]:
    """设备领取真发前自动补只读证明，让控制端任务无需人工二次点准备。"""
    if not dev.allow_real_send or not device_ready_for_real_send(dev, now) or device_has_inflight_real_send(db, dev):
        return []
    query = (
        db.query(SendTask)
        .filter(SendTask.status == "pending")
        .filter(SendTask.send_mode == "real_send")
        .filter(SendTask.device_id == dev.device_id)
        .filter(or_(SendTask.next_retry_at.is_(None), SendTask.next_retry_at <= now))
    )
    if not dev.allow_any_conversation:
        query = query.filter(SendTask.target_name.in_(convs))
    query = query.order_by(SendTask.id).limit(max(limit * 3, 10))
    queued: list[SendTask] = []
    seen_targets: set[str] = set()
    for task in query.all():
        target_name = (task.target_name or "").strip()
        if not target_name or target_name in seen_targets:
            continue
        seen_targets.add(target_name)
        proof_reason = device_conversation_proof_reason(db, dev, target_name, now)
        if not proof_reason or active_conversation_check_task(db, dev, target_name):
            continue
        if recent_conversation_check_failure_reason(db, dev, target_name, now):
            continue
        queued.append(
            create_conversation_check_task(
                db,
                dev,
                target_name,
                task.family_id or f"WECOM_{target_name}",
                f"设备:{dev.device_id}",
                auto_prepare_real_send=True,
            )
        )
        if len(queued) >= limit:
            break
    return queued


def pending_conversation_check_query(db: Session, dev: Device, convs: list[str], now: datetime):
    query = (
        db.query(SendTask)
        .filter(SendTask.status == "pending")
        .filter(SendTask.device_id == dev.device_id)
        .filter(SendTask.scene == CONVERSATION_CHECK_SCENE)
        .filter(or_(SendTask.next_retry_at.is_(None), SendTask.next_retry_at <= now))
        .order_by(SendTask.content.startswith(REAL_SEND_PREP_CONVERSATION_CHECK_PREFIX).desc(), SendTask.id)
    )
    if not dev.allow_any_conversation:
        query = query.filter(SendTask.target_name.in_(convs))
    return query


@app.post("/api/devices/{device_id}/conversation-checks/batch")
def queue_device_conversation_checks_batch(
    device_id: str,
    payload: DeviceConversationBatchCheckRequestIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
):
    ensure_new_task_operation_allowed(request, "assign_device")
    dev = db.query(Device).filter(Device.device_id == device_id).first()
    if not dev:
        raise HTTPException(404, "设备不存在")
    requested_targets = list(payload.target_names if payload else [])
    targets = [str(item).strip() for item in requested_targets if str(item).strip()]
    if not targets:
        targets = device_conversations(dev)
    if payload and payload.missing_only:
        missing = set(device_conversation_proof_summary(db, dev)["missing_targets"])
        targets = [target for target in targets if target in missing]
    seen = set()
    targets = [target for target in targets if not (target in seen or seen.add(target))]
    if not targets:
        detail = "缺失/过期会话证明已补齐，无需再次下发校验" if payload and payload.missing_only else "设备未配置负责会话，无法批量刷新证明"
        raise HTTPException(400, detail)
    actor = actor_from_request(request)
    queued = []
    skipped = []
    for target_name in targets:
        validate_device_conversation_scope(dev, target_name)
        existing = active_conversation_check_task(db, dev, target_name)
        if existing:
            skipped.append({"target_name": target_name, "task_id": existing.id, "reason": "已有待执行只读校验"})
            continue
        task = create_conversation_check_task(db, dev, target_name, f"WECOM_{target_name}", actor)
        queued.append(task)
    db.commit()
    return {
        "device_id": dev.device_id,
        "queued_count": len(queued),
        "skipped_count": len(skipped),
        "queued": [send_task_view(task, request, db) for task in queued],
        "skipped": skipped,
    }


# 修改设备显示名/备注和控制端设备策略。
@app.put("/api/devices/{device_id}")
def update_device(device_id: str, payload: DeviceUpdateIn, db: Session = Depends(get_db)):
    dev = db.query(Device).filter(Device.device_id == device_id).first()
    if not dev:
        raise HTTPException(404, "设备不存在")
    dev.name = payload.name or dev.name
    dev.note = payload.note or dev.note
    if payload.conversations is not None:
        dev.conversations = json.dumps(payload.conversations, ensure_ascii=False)
    if payload.allow_real_send is not None:
        dev.allow_real_send = bool(payload.allow_real_send)
    if payload.allow_any_conversation is not None:
        dev.allow_any_conversation = bool(payload.allow_any_conversation)
    db.commit()
    return device_view(dev, db)


# 设备心跳：更新在线状态、企微健康，并用上报的 conversations 刷新该设备负责的会话。
@app.post("/api/devices/{device_id}/heartbeat")
def device_heartbeat(device_id: str, payload: HeartbeatIn, dev: Device = Depends(require_device), db: Session = Depends(get_db)):
    if dev.device_id != device_id:
        raise HTTPException(403, "device_id 与鉴权头不一致")
    dev.last_heartbeat = datetime.utcnow()
    dev.status = "online"
    dev.wecom_ok = payload.wecom_ok or dev.wecom_ok
    dev.last_error = payload.detail
    dev.outbox_pending_count = max(int(payload.outbox_pending_count or 0), 0)
    dev.outbox_last_error = (payload.outbox_last_error or "")[:500]
    if payload.conversations:
        dev.conversations = json.dumps(payload.conversations, ensure_ascii=False)
    db.commit()
    return device_view(dev, db)


# 动态领取：把该设备负责会话的 pending 任务原子分配给它，返回领到的任务。
@app.post("/api/devices/{device_id}/claim")
def claim_tasks(device_id: str, limit: int = 5, dev: Device = Depends(require_device), db: Session = Depends(get_db)):
    if dev.device_id != device_id:
        raise HTTPException(403, "device_id 与鉴权头不一致")
    safe_limit = normalize_claim_limit(limit)
    now = datetime.utcnow()
    # 先回收本设备超时未回写的 assigned 任务；设备代表发送人，只能回到同设备重试。
    requeue_stale_assigned_tasks(db, now=now, device_id=dev.device_id, actor=f"设备:{dev.device_id}")

    convs = device_conversations(dev)
    if not convs and not dev.allow_any_conversation:
        db.commit()
        return []
    queue_real_send_missing_proof_checks_for_claim(db, dev, convs, now, safe_limit)
    preparation_query = pending_conversation_check_query(db, dev, convs, now).limit(safe_limit)
    candidates = apply_claim_row_lock(preparation_query, db).all()
    if not candidates:
        candidates_query = (
            db.query(SendTask)
            .filter(SendTask.status == "pending")
            .filter(or_(SendTask.device_id == "", SendTask.device_id == dev.device_id, SendTask.device_id.is_(None)))
            .filter(or_(SendTask.next_retry_at.is_(None), SendTask.next_retry_at <= now))
            .filter(or_(SendTask.send_mode != "real_send", SendTask.device_id == dev.device_id))
        )
        if not dev.allow_any_conversation:
            candidates_query = candidates_query.filter(SendTask.target_name.in_(convs))
        if not dev.allow_real_send:
            candidates_query = candidates_query.filter(SendTask.send_mode != "real_send")
        elif not device_ready_for_real_send(dev, now):
            candidates_query = candidates_query.filter(SendTask.send_mode != "real_send")
        elif device_has_inflight_real_send(db, dev):
            candidates_query = candidates_query.filter(SendTask.send_mode != "real_send")
        candidates_query = candidates_query.order_by(SendTask.id).limit(safe_limit)
        candidates = apply_claim_row_lock(candidates_query, db).all()
    candidates = [
        task
        for task in candidates
        if send_log_mode(task) != "real_send" or device_conversation_recently_verified(db, dev, task.target_name, now)
    ]
    real_send_candidate = next((task for task in candidates if send_log_mode(task) == "real_send"), None)
    if real_send_candidate:
        # 真实发送有外部状态，不批量预占任务；一次只派发一条，避免未执行任务进入 assigned。
        candidates = [real_send_candidate]
    claimed = []
    for task in candidates:
        # PostgreSQL 用行锁跳过被其他 worker 锁住的任务；SQLite 再用条件更新兜底，确保只会有一个领取者成功。
        before = send_task_snapshot(task)
        execution_reference_time = task.scheduled_at or task.created_at
        assigned_at = datetime.utcnow()
        updated = (
            db.query(SendTask)
            .filter(SendTask.id == task.id, SendTask.status == "pending")
            .filter(or_(SendTask.device_id == "", SendTask.device_id == dev.device_id, SendTask.device_id.is_(None)))
            .filter(or_(SendTask.next_retry_at.is_(None), SendTask.next_retry_at <= assigned_at))
            .filter(or_(SendTask.send_mode != "real_send", SendTask.device_id == dev.device_id))
        )
        if not dev.allow_any_conversation:
            updated = updated.filter(SendTask.target_name.in_(convs))
        updated = updated.update({"status": "assigned", "device_id": dev.device_id, "scheduled_at": assigned_at}, synchronize_session=False)
        if updated != 1:
            continue
        task.status = "assigned"
        task.device_id = dev.device_id
        task.scheduled_at = assigned_at
        try:
            task.content = validate_send_task_content(task.content)
            task.send_mode = validate_send_task_execution_guard(task, reference_time=execution_reference_time)
            if task.send_mode == "real_send":
                validate_real_send_risk(db, task.target_name, task.content, exclude_task_id=task.id)
        except HTTPException as exc:
            task.status = "failed"
            db.add(
                SendLog(
                    task_id=task.id,
                    family_id=task.family_id,
                    target_name=task.target_name,
                    status="failed",
                    send_mode=send_log_mode(task),
                    detail=f"SEND_GUARD: {exc.detail}",
                    device_id=dev.device_id,
                )
            )
            audit_send_task_change(db, task, "send_guard_failed", f"设备:{dev.device_id}", f"发送安全闸门拦截：{exc.detail}", before)
            sync_weekly_report_send_status(db, task, "failed")
            continue
        claimed.append(task)
        audit_send_task_change(db, task, "assign_device", f"设备:{dev.device_id}", "设备领取发送任务", before)
        sync_weekly_report_send_status(db, task, "assigned")
    db.commit()
    return [device_task_payload(db.get(SendTask, t.id), dev) for t in claimed]


# ============ ARK（阿里百炼）云端定位密钥 · 控制台在线配置 ============
ARK_CONFIG_PATH = ROOT / "config" / "ark.json"


def _mask_key(key: str) -> str:
    if not key:
        return ""
    return (key[:6] + "..." + key[-4:]) if len(key) > 12 else "已配置"


# 读取当前 ARK 配置（api_key 脱敏，仅供看板展示是否已配）。
@app.get("/api/ark-config")
def get_ark_config():
    if ARK_CONFIG_PATH.exists():
        try:
            data = json.loads(ARK_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        key = str(data.get("api_key", ""))
        return {
            "configured": bool(key),
            "api_key_masked": _mask_key(key),
            "endpoint_id": data.get("endpoint_id", ""),
            "base_url": data.get("base_url", ""),
        }
    return {"configured": False, "api_key_masked": "", "endpoint_id": "", "base_url": ""}


def component_status(status: str, label: str, detail: str, metrics: dict | None = None) -> dict:
    return {"status": status, "label": label, "detail": detail, "metrics": metrics or {}}


def screenshot_artifact_stats() -> dict:
    if not SEND_SCREENSHOT_DIR.exists():
        return {"exists": False, "file_count": 0, "total_bytes": 0}
    files = [path for path in SEND_SCREENSHOT_DIR.iterdir() if path.is_file()]
    return {
        "exists": True,
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
    }


def backup_artifact_stats() -> dict:
    backups = list_backups(BACKUP_DIR)
    latest = backups[0] if backups else {}
    return {
        "file_count": len(backups),
        "total_bytes": sum(item["size_bytes"] for item in backups),
        "latest_filename": latest.get("filename", ""),
        "latest_created_at": latest.get("created_at", ""),
    }


def real_send_verification_report(db: Session, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    since = now - timedelta(hours=24)
    logs = db.query(SendLog).filter(SendLog.send_mode == "real_send", SendLog.sent_at >= since).all()
    metrics = real_send_closure_metrics(logs)
    attempted_count = metrics["attempted_24h"]
    confirmed_count = metrics["confirmed_24h"]
    unconfirmed_sent_count = metrics["unconfirmed_sent_24h"]
    confirm_failed_count = metrics["confirm_failed_24h"]
    target_breakdown = real_send_breakdown(logs, "target_name")
    device_breakdown = real_send_breakdown(logs, "device_id")
    status = "critical" if unconfirmed_sent_count or confirm_failed_count else "ok"
    rate = metrics["confirm_rate"]
    detail = (
        f"近24小时真实发送闭环 {attempted_count} 条，成功落地回读 {confirmed_count} 条，确认率 {rate}%"
        if attempted_count
        else "近24小时暂无真实发送，回读确认无异常"
    )
    if confirm_failed_count:
        detail += f"，回读失败/未知 {confirm_failed_count} 条"
        issue_targets = [item for item in target_breakdown if item["confirm_failed_24h"]]
        if issue_targets:
            detail += f"，优先排查目标：{issue_targets[0]['target_name']}"
    return component_status(
        status,
        "真实发送回读确认",
        detail,
        {
            **metrics,
            "target_breakdown": target_breakdown,
            "device_breakdown": device_breakdown,
        },
    )


def device_outbox_report(devices: list[Device]) -> dict:
    blocked = [dev for dev in devices if (dev.outbox_pending_count or 0) > 0]
    pending_total = sum(dev.outbox_pending_count or 0 for dev in blocked)
    max_pending = max((dev.outbox_pending_count or 0 for dev in blocked), default=0)
    last_errors = [
        f"{dev.device_id}: {dev.outbox_last_error}"
        for dev in blocked
        if (dev.outbox_last_error or "").strip()
    ][:5]
    status = "critical" if pending_total else "ok"
    detail = (
        f"{len(blocked)} 台设备存在 {pending_total} 条发送结果待补传，相关真实发送已暂停"
        if pending_total
        else "所有设备结果补传队列已清空"
    )
    return component_status(
        status,
        "结果补传队列",
        detail,
        {
            "blocked_devices": len(blocked),
            "pending_results": pending_total,
            "max_device_pending": max_pending,
            "last_errors": last_errors,
        },
    )


def build_ops_health_dashboard(db: Session, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    devices = db.query(Device).all()
    online_devices = [dev for dev in devices if dev.last_heartbeat and (now - dev.last_heartbeat) <= timedelta(seconds=HEARTBEAT_ONLINE_SECONDS)]
    wecom_ok_devices = [dev for dev in online_devices if dev.wecom_ok == "Y"]
    pending_count = db.query(SendTask).filter(SendTask.status == "pending").count()
    assigned_count = db.query(SendTask).filter(SendTask.status == "assigned").count()
    retry_waiting_count = db.query(SendTask).filter(SendTask.status == "pending", SendTask.next_retry_at.is_not(None), SendTask.next_retry_at > now).count()
    retry_alert_count = sum(1 for task in db.query(SendTask).filter(SendTask.status == "failed").all() if task_needs_retry_alert(task))
    stale_before = now - timedelta(seconds=CLAIM_TIMEOUT_SECONDS)
    stale_assigned_count = db.query(SendTask).filter(SendTask.status == "assigned", SendTask.scheduled_at < stale_before).count()
    recent_failed_count = db.query(SendLog).filter(SendLog.status == "failed", SendLog.sent_at >= now - timedelta(hours=24)).count()
    ark = get_ark_config()
    runtime_config = current_runtime_config_report()
    artifact_stats = screenshot_artifact_stats()
    backup_stats = backup_artifact_stats()
    retention = retention_report(db, SEND_SCREENSHOT_DIR, ROOT, current_retention_policy(), now)

    components = [
        runtime_config,
        admin_auth_component(),
        rate_limit_report(),
        component_status("ok", "后端服务", "FastAPI 正常响应", {"mode": runtime_config["metrics"]["app_env"]}),
        component_status(
            "ok" if devices and len(online_devices) == len(devices) else ("warn" if devices else "warn"),
            "被控端设备",
            f"{len(online_devices)}/{len(devices)} 台在线" if devices else "尚未注册被控端设备",
            {"total": len(devices), "online": len(online_devices)},
        ),
        component_status(
            "ok" if not online_devices or len(wecom_ok_devices) == len(online_devices) else "warn",
            "企业微信可用性",
            f"{len(wecom_ok_devices)}/{len(online_devices)} 台在线设备企微正常",
            {"online": len(online_devices), "wecom_ok": len(wecom_ok_devices)},
        ),
        device_outbox_report(devices),
        component_status(
            "critical" if stale_assigned_count else ("warn" if pending_count >= 50 else "ok"),
            "发送队列",
            f"pending={pending_count}, assigned={assigned_count}, stale_assigned={stale_assigned_count}",
            {"pending": pending_count, "assigned": assigned_count, "stale_assigned": stale_assigned_count},
        ),
        component_status(
            "warn" if recent_failed_count else "ok",
            "近24小时发送失败",
            f"{recent_failed_count} 条失败日志",
            {"failed_24h": recent_failed_count},
        ),
        component_status(
            "critical" if retry_alert_count else ("warn" if retry_waiting_count else "ok"),
            "失败重试与告警",
            f"{retry_waiting_count} 条等待自动重试，{retry_alert_count} 条需人工告警",
            {"retry_waiting": retry_waiting_count, "retry_alert": retry_alert_count},
        ),
        real_send_verification_report(db, now),
        claim_lock_report(db),
        component_status(
            "ok" if ark.get("configured") else "warn",
            "云端视觉定位",
            "ARK 已配置" if ark.get("configured") else "ARK 未配置，云端定位不可用",
            {"configured": bool(ark.get("configured")), "endpoint_id": ark.get("endpoint_id", "")},
        ),
        component_status(
            "ok",
            "截图证据目录",
            f"{artifact_stats['file_count']} 个文件，{artifact_stats['total_bytes']} bytes",
            artifact_stats,
        ),
        component_status(
            "ok" if backup_stats["file_count"] else "warn",
            "数据备份",
            f"{backup_stats['file_count']} 个备份，最近：{backup_stats['latest_created_at'] or '暂无'}",
            backup_stats,
        ),
        component_status(
            "warn" if retention["expired_count"] else "ok",
            "日志保留策略",
            retention["detail"],
            {
                "expired_count": retention["expired_count"],
                "expired_bytes": retention["expired_bytes"],
                "policy": retention["policy"],
            },
        ),
    ]
    if any(item["status"] == "critical" for item in components):
        overall = "critical"
    elif any(item["status"] == "warn" for item in components):
        overall = "warn"
    else:
        overall = "ok"
    return {
        "generated_at": timeline_time(now),
        "overall_status": overall,
        "components": components,
    }


@app.get("/api/ops/health")
def ops_health(db: Session = Depends(get_db)):
    return build_ops_health_dashboard(db)


@app.get("/api/ops/backups")
def ops_list_backups():
    return list_backups(BACKUP_DIR)


@app.get("/api/ops/redacted-export")
def ops_redacted_export(db: Session = Depends(get_db)):
    snapshot = {
        "sensitivity": "redacted",
        "generated_at": datetime.utcnow().isoformat(sep=" ", timespec="seconds"),
        "families": [as_dict(item) for item in db.query(Family).order_by(Family.family_id).all()],
        "messages": [as_dict(item) for item in db.query(RawMessage).order_by(RawMessage.id).limit(500).all()],
        "followups": [as_dict(item) for item in db.query(FollowupRecord).order_by(FollowupRecord.id.desc()).limit(500).all()],
        "send_tasks": [as_dict(item) for item in db.query(SendTask).order_by(SendTask.id.desc()).limit(500).all()],
        "send_logs": [send_log_view(item) for item in db.query(SendLog).order_by(SendLog.id.desc()).limit(500).all()],
        "ai_outputs": [as_dict(item) for item in db.query(AIOutput).order_by(AIOutput.id.desc()).limit(500).all()],
    }
    return redact_record(snapshot)


@app.get("/api/ops/retention")
def ops_retention_plan(db: Session = Depends(get_db)):
    return retention_report(db, SEND_SCREENSHOT_DIR, ROOT, current_retention_policy())


@app.post("/api/ops/retention/prune")
def ops_retention_prune(payload: RetentionPruneIn, db: Session = Depends(get_db)):
    if not payload.confirm_execute:
        result = prune_retention(db, SEND_SCREENSHOT_DIR, ROOT, current_retention_policy(), execute=False)
        return {
            **result["report"],
            "executed": False,
            "deleted": result["deleted"],
            "detail": "未执行删除；传 confirm_execute=true 才会清理过期日志和截图。",
        }
    result = prune_retention(db, SEND_SCREENSHOT_DIR, ROOT, current_retention_policy(), execute=True)
    return {**result["report"], "executed": True, "deleted": result["deleted"]}


@app.post("/api/ops/backups")
def ops_create_backup():
    try:
        return create_backup(DATABASE_URL, BACKUP_DIR, ROOT)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/ops/backups/{filename}")
def ops_download_backup(filename: str):
    try:
        path = backup_path(BACKUP_DIR, filename)
    except ValueError as exc:
        raise HTTPException(404, "备份不存在") from exc
    if not path.exists():
        raise HTTPException(404, "备份不存在")
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


@app.post("/api/ops/backups/{filename}/restore-drill")
def ops_restore_drill(filename: str):
    try:
        path = backup_path(BACKUP_DIR, filename)
        return run_restore_drill(path)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


# 保存 ARK 配置：写入 config/ark.json 并清缓存使其立即生效；被控端下载接入包时会带上这份配置。
@app.post("/api/ark-config")
def save_ark_config(payload: ArkConfigIn):
    api_key = payload.api_key.strip()
    if not api_key:
        raise HTTPException(400, "api_key 不能为空")
    ARK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    model = payload.endpoint_id.strip() or "qwen-vl-plus"
    cfg = {
        "base_url": payload.base_url.strip() or "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": api_key,
        "endpoint_id": model,
        "model_name": model,
    }
    ARK_CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    # 清掉 ark_client 的 lru_cache，使新密钥立即生效（否则要重启后端）。
    try:
        from app.services.ark_client import ark_config, ark_client
        ark_config.cache_clear()
        ark_client.cache_clear()
    except Exception as exc:
        print(f"ark_cache_clear_failed detail={exc}")
    return {"ok": True, "endpoint_id": model}
