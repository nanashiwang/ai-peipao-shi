"""FastAPI 应用入口。

这个文件把数据导入、Agent 生成、发送任务和 RPA 同步等接口组装成完整的本地 MVP。
"""

import base64
import binascii
import io
import json
import os
import re
import secrets
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from urllib.parse import unquote

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db import DATABASE_URL, get_db, init_db
from app.models import (
    AIOutput,
    AuditLog,
    CheckinRecord,
    Device,
    Family,
    FollowupRecord,
    ParentProfile,
    RawMessage,
    SendLog,
    SendTask,
    Template,
    UserAccount,
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
    admin_auth_required,
    admin_auth_secret,
    bearer_token,
    path_requires_admin_auth,
    role_allowed_for_request,
    sign_admin_token,
    verify_admin_token,
)
from app.services.backup_service import backup_path, create_sqlite_backup, list_backups, run_restore_drill
from app.services.importer import import_rows, import_template_csv_bytes, list_import_templates, rows_from_upload
from app.services.retention_service import prune_retention, retention_policy_from_env, retention_report
from app.services.runtime_config import assert_runtime_config_safe, runtime_config_report
from app.services.scenario import detect_checkin, detect_scene
from app.services.send_log_classifier import classify_send_log
from app.services.redaction_service import redact_record
from app.services.send_task_operations import (
    OPERATION_LABELS,
    role_allows_task_operation,
    send_task_operation_state,
)

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
STATIC = Path(__file__).resolve().parent / "static"
SEND_SCREENSHOT_DIR = ROOT / "data" / "send_screenshots"
BACKUP_DIR = ROOT / "data" / "backups"
MAX_SEND_SCREENSHOT_BYTES = 6 * 1024 * 1024
REAL_SEND_DUPLICATE_WINDOW_SECONDS = 3600
REAL_SEND_MIN_INTERVAL_SECONDS = 30
SEND_TASK_EXECUTION_MAX_AGE_DAYS = 7
WEEKLY_REPORT_SCENE = "周报发送"

# 应用实例和 CORS 配置，便于本地前端直接访问。
app = FastAPI(title="重庆机构陪跑师效率系统 MVP", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def admin_auth_middleware(request: Request, call_next):
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
    coach_name: str = ""
    service_status: str = "企微待同步"


# 回写发送结果时记录状态和详情。
class SendResultIn(BaseModel):
    status: str
    detail: str = ""
    device_id: str = ""
    screenshot_base64: str = ""


# 设备注册/更新入参。
class DeviceIn(BaseModel):
    device_id: str
    name: str = ""
    note: str = ""
    conversations: list[str] = []


# 设备心跳上报：企微健康 + 本机负责的会话（后端据此刷新该设备 conversations，供动态领取过滤）。
class HeartbeatIn(BaseModel):
    wecom_ok: str = ""
    detail: str = ""
    conversations: list[str] = []


class RetentionPruneIn(BaseModel):
    confirm_execute: bool = False


# 控制台在线配置阿里 ARK（百炼）云端定位密钥。
class ArkConfigIn(BaseModel):
    api_key: str
    endpoint_id: str = "qwen-vl-plus"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"


# RPA 同步会话里的单条消息结构。
class RpaMessageIn(BaseModel):
    speaker: str = ""
    content: str
    message_time: str = ""
    source: str = "企业微信RPA"


# 一次同步一个会话，消息列表和是否自动生成回复都放在这里。
class RpaConversationIn(BaseModel):
    target_name: str
    family_id: str = ""
    parent_nickname: str = ""
    child_grade: str = ""
    coach_name: str = ""
    messages: list[RpaMessageIn]
    auto_generate_reply: bool = True
    auto_create_reply_task: bool = False
    auto_generate_all_agents: bool = False
    latest_message: str = ""


# 更新发送任务的可编辑字段。
class SendTaskUpdate(BaseModel):
    target_name: str = ""
    scene: str = ""
    content: str = ""
    device_id: str | None = None
    send_mode: str = ""
    confirm_real_send: bool = False
    status: str = "pending"


# Agent 请求的统一入参。
class AgentRequest(BaseModel):
    family_id: str
    message: str = ""
    tone: str = "standard"
    source: str = ""


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
    family_id: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


class ChatMessageIn(BaseModel):
    family_id: str
    username: str
    content: str


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
    return data


def current_runtime_config_report() -> dict:
    return runtime_config_report(
        os.getenv("APP_ENV"),
        DATABASE_URL,
        ARK_CONFIG_PATH,
        database_url_explicit=bool(os.getenv("DATABASE_URL")),
    )


def current_retention_policy() -> dict:
    return retention_policy_from_env(os.environ)


def admin_auth_component() -> dict:
    required = admin_auth_required()
    has_secret = bool(os.getenv("ADMIN_AUTH_SECRET", "").strip())
    if required and not has_secret:
        status = "critical"
        detail = "管理端鉴权已启用，但 ADMIN_AUTH_SECRET 未配置"
    elif required:
        status = "ok"
        detail = "管理端鉴权已启用"
    else:
        status = "ok"
        detail = "管理端鉴权未强制启用，仅适合本地/试点内网"
    return component_status(status, "管理端鉴权", detail, {"required": required, "secret_configured": has_secret})


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
    identity = admin_identity_from_request(request)
    if identity:
        return identity.role
    if request is not None and bearer_token(request.headers.get("authorization", "")):
        return "readonly"
    return "admin"


def send_task_view(task: SendTask, request: Request | None = None) -> dict:
    data = as_dict(task)
    data.update(send_task_operation_state(task.status, task.send_mode, operation_role_from_request(request)))
    data["retry_alert"] = task_needs_retry_alert(task)
    return data


def ensure_task_operation_allowed(task: SendTask, request: Request | None, operation: str) -> None:
    role = operation_role_from_request(request)
    if not role_allows_task_operation(task.status, task.send_mode, role, operation):
        label = OPERATION_LABELS.get(operation, operation)
        raise HTTPException(403, f"当前角色不能执行「{label}」操作")


def ensure_new_task_operation_allowed(request: Request | None, operation: str) -> None:
    role = operation_role_from_request(request)
    if not role_allows_task_operation("pending", "dry_run", role, operation):
        label = OPERATION_LABELS.get(operation, operation)
        raise HTTPException(403, f"当前角色不能执行「{label}」操作")


def actor_from_request(request: Request | None, fallback: str = "控制端") -> str:
    if request is None:
        return fallback
    device_id = (request.headers.get("x-device-id") or "").strip()
    if device_id:
        return f"设备:{device_id}"[:120]
    actor = unquote((request.headers.get("x-actor") or request.headers.get("x-user") or "").strip())
    return (actor or fallback)[:120]


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


def ai_safety_findings(*texts: str) -> dict:
    blob = "\n".join(str(text or "") for text in texts)
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


def validate_send_task_execution_guard(task: SendTask, now: datetime | None = None) -> str:
    mode = validate_send_mode(task.send_mode)
    reference_time = task.scheduled_at or task.created_at
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


def is_retryable_send_failure(task: SendTask, detail: str) -> bool:
    if send_log_mode(task) != "dry_run":
        return False
    if task.retry_count >= task.max_retries:
        return False
    return not any(term in (detail or "") for term in NON_RETRYABLE_FAILURE_TERMS)


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
        return "auto_retry", f"发送失败已自动排队重试 {task.retry_count}/{task.max_retries}，下次时间 {timeline_time(task.next_retry_at)}"
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
    filename = f"task_{task_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.{ext}"
    path = (SEND_SCREENSHOT_DIR / filename).resolve()
    try:
        path.relative_to(SEND_SCREENSHOT_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(400, "截图路径非法") from exc
    path.write_bytes(data)
    return f"/api/send-artifacts/{filename}"


def resolve_send_screenshot(filename: str) -> Path:
    if not re.fullmatch(r"task_\d+_\d{8}_\d{6}_\d{6}\.(png|jpg)", filename or ""):
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
        data["admin_token"] = sign_admin_token(account.username, account.role, account.display_name, admin_auth_secret())
    return data


def ensure_family(db: Session, family_id: str, parent_name: str, child_grade: str = "", coach_name: str = "") -> Family:
    family = db.query(Family).filter(Family.family_id == family_id).one_or_none()
    if family:
        if parent_name:
            family.parent_nickname = parent_name
        if child_grade:
            family.child_grade = child_grade
        if coach_name:
            family.coach_name = coach_name
        return family
    family = Family(
        family_id=family_id,
        parent_nickname=parent_name,
        child_grade=child_grade,
        coach_name=coach_name,
        service_status="网页通讯测试",
    )
    db.add(family)
    db.flush()
    return family


def ensure_account(db: Session, username: str, password: str, display_name: str, role: str, family_id: str = "") -> UserAccount:
    account = db.query(UserAccount).filter(UserAccount.username == username).one_or_none()
    if account:
        account.password = password
        account.display_name = display_name
        account.role = role
        account.family_id = family_id
        return account
    account = UserAccount(username=username, password=password, display_name=display_name, role=role, family_id=family_id)
    db.add(account)
    db.flush()
    return account


def seed_bootstrap_admin(db: Session) -> None:
    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if admin_auth_required():
        admin_auth_secret()
    if username and password:
        ensure_account(db, username, password, os.getenv("ADMIN_DISPLAY_NAME", "系统管理员"), "admin")
    if admin_auth_required() and not db.query(UserAccount).filter(UserAccount.role == "admin").first():
        raise RuntimeError("管理端鉴权已启用，但没有 admin 账号；请设置 ADMIN_USERNAME/ADMIN_PASSWORD 完成首次引导。")


def add_chat_message(db: Session, family_id: str, speaker: str, content: str, minutes_offset: int = 0) -> RawMessage:
    msg = RawMessage(
        family_id=family_id,
        message_time=datetime.utcnow(),
        speaker=speaker,
        content=content,
        source="网页通讯测试",
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


    # 首页返回静态前端页面。
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


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
        password=payload.password,
        display_name=payload.display_name or username,
        role=role,
        family_id=family_id,
    )
    db.add(account)
    db.commit()
    return account_payload(account)


@app.post("/api/test-chat/login")
def login_account(payload: LoginIn, db: Session = Depends(get_db)):
    account = db.query(UserAccount).filter(UserAccount.username == payload.username.strip()).one_or_none()
    if not account or account.password != payload.password:
        raise HTTPException(401, "账号或密码错误")
    return admin_account_payload(account)


@app.post("/api/admin/auth/login")
def admin_login(payload: LoginIn, db: Session = Depends(get_db)):
    account = db.query(UserAccount).filter(UserAccount.username == payload.username.strip()).one_or_none()
    if not account or account.password != payload.password or account.role not in ADMIN_ROLES:
        raise HTTPException(401, "管理端账号或密码错误")
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
        "expires_at": datetime.utcfromtimestamp(identity.exp).isoformat(sep=" ", timespec="seconds"),
    }


@app.get("/api/test-chat/accounts")
def list_accounts(db: Session = Depends(get_db)):
    return [account_payload(item) for item in db.query(UserAccount).order_by(UserAccount.role, UserAccount.username).all()]


@app.get("/api/test-chat/conversations")
def list_test_conversations(db: Session = Depends(get_db)):
    rows = []
    for family in db.query(Family).order_by(Family.family_id).all():
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


@app.get("/api/test-chat/messages/{family_id}")
def list_chat_messages(family_id: str, db: Session = Depends(get_db)):
    require_family(db, family_id)
    rows = db.query(RawMessage).filter(RawMessage.family_id == family_id).order_by(RawMessage.message_time).all()
    return [as_dict(item) for item in rows]


@app.post("/api/test-chat/messages")
def send_chat_message(payload: ChatMessageIn, db: Session = Depends(get_db)):
    family = require_family(db, payload.family_id)
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


@app.post("/api/test-chat/seed")
def seed_test_chat(db: Session = Depends(get_db)):
    family_ids = ["WEB_LIN", "WEB_ZHOU", "WEB_CHEN"]
    for family_id in family_ids:
        db.query(SendLog).filter(SendLog.family_id == family_id).delete()
        db.query(SendTask).filter(SendTask.family_id == family_id).delete()
        db.query(AIOutput).filter(AIOutput.family_id == family_id).delete()
        db.query(WeeklyReport).filter(WeeklyReport.family_id == family_id).delete()
        db.query(ParentProfile).filter(ParentProfile.family_id == family_id).delete()
        db.query(CheckinRecord).filter(CheckinRecord.family_id == family_id).delete()
        db.query(FollowupRecord).filter(FollowupRecord.family_id == family_id).delete()
        db.query(RawMessage).filter(RawMessage.family_id == family_id).delete()
        db.query(Family).filter(Family.family_id == family_id).delete()
    for username in ["coach_yitong", "lin_mom", "zhou_dad", "chen_mom"]:
        db.query(UserAccount).filter(UserAccount.username == username).delete()
    db.flush()

    ensure_account(db, "coach_yitong", "123456", "怡彤老师", "coach")
    samples = [
        (
            "WEB_LIN",
            "林妈妈",
            "初一",
            "coach_yitong",
            [
                ("林妈妈", "老师，孩子这两天作业启动很慢，写到很晚会有点崩。"),
                ("怡彤老师", "我先帮您把任务拆小，今晚先保住英语阅读和数学订正两个核心动作。"),
                ("林妈妈", "好的，他今天说不想打卡，觉得自己做不好。"),
                ("怡彤老师", "可以，今天先不追求完整，完成一小项也算建立节奏。"),
                ("林妈妈", "刚刚完成了数学订正，但是英语没来得及。"),
                ("林妈妈", "我担心他又连续断掉，后面越来越抗拒。"),
            ],
        ),
        (
            "WEB_ZHOU",
            "周爸爸",
            "五年级",
            "coach_yitong",
            [
                ("周爸爸", "孩子今天PBL小作品已经发群里了，想请你帮忙看看。"),
                ("怡彤老师", "收到，我会重点看表达结构和例子是否具体。"),
                ("周爸爸", "他自己说讲得有点乱，但愿意再改一版。"),
                ("怡彤老师", "这个意愿很重要，我会先肯定亮点，再给一个改进点。"),
                ("周爸爸", "另外这周打卡基本完成了，周三漏了一次。"),
            ],
        ),
        (
            "WEB_CHEN",
            "陈妈妈",
            "高一",
            "coach_yitong",
            [
                ("陈妈妈", "最近孩子情绪波动比较大，我问学习他就不耐烦。"),
                ("怡彤老师", "先别急着追问结果，我们先用更低压力的复盘方式。"),
                ("陈妈妈", "我也担心是不是课程太难，他说听得懂但做题慢。"),
                ("怡彤老师", "听懂和独立输出之间有距离，我会把练习拆成基础题和挑战题。"),
                ("陈妈妈", "那今天要不要继续打卡？他现在有点烦。"),
                ("陈妈妈", "如果需要我配合，我可以晚上九点再提醒一次。"),
            ],
        ),
    ]
    for family_id, parent_name, grade, coach_username, messages in samples:
        ensure_family(db, family_id, parent_name, grade, "怡彤老师")
        ensure_account(db, f"{family_id.lower()}_parent", "123456", parent_name, "parent", family_id)
        for speaker, content in messages:
            add_chat_message(db, family_id, speaker, content)

    db.commit()
    return {"accounts": db.query(UserAccount).count(), "families": len(family_ids), "messages": sum(len(item[4]) for item in samples)}


@app.post("/api/test-chat/ai")
def generate_test_chat_ai(payload: ChatAiIn, request: Request = None, db: Session = Depends(get_db)):
    started = perf_counter()
    family = require_family(db, payload.family_id)
    context = build_agent_context(db, family.family_id)
    if not context["messages"]:
        raise HTTPException(404, "该会话还没有消息")

    profile_result = run_family_profile_agent_service(context)
    profile_output = save_ai_output(db, family.family_id, "family_profile", "网页通讯测试", profile_result)
    upsert_parent_profile_from_agent(db, family.family_id, profile_result)

    latest = latest_parent_message(context)
    reply_result = run_reply_agent_service(context, latest, "standard")
    reply_output = save_ai_output(db, family.family_id, "ai_reply", "网页通讯测试", reply_result)
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
    family = require_family(db, payload.family_id)
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


# 载入内置样例数据，方便演示和联调。
@app.post("/api/sample-data")
def load_sample_data(db: Session = Depends(get_db)):
    sample = SAMPLES / "sample_messages.csv"
    rows = rows_from_upload(sample.name, sample.read_bytes())
    return import_rows(db, rows)


# 家庭列表接口，顺带补充每个家庭的消息数。
@app.get("/api/families")
def list_families(request: Request = None, db: Session = Depends(get_db)):
    families = db.query(Family).order_by(Family.family_id).all()
    rows = [{**as_dict(f), "message_count": db.query(RawMessage).filter(RawMessage.family_id == f.family_id).count()} for f in families]
    return maybe_redact_for_request(rows, request)


@app.post("/api/families")
def upsert_family(payload: FamilyIn, db: Session = Depends(get_db)):
    target_name = payload.parent_nickname.strip()
    if not target_name:
        raise HTTPException(400, "企微会话名不能为空")
    family_id = payload.family_id.strip() or f"WECOM_{target_name}"
    family = db.query(Family).filter(Family.family_id == family_id).one_or_none()
    if not family:
        family = db.query(Family).filter(Family.parent_nickname == target_name).one_or_none()
    if family:
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
        family.coach_name = payload.coach_name
        family.service_status = payload.service_status
    else:
        family = Family(
            family_id=family_id,
            parent_nickname=target_name,
            child_grade=payload.child_grade,
            course_stage=payload.course_stage,
            unit_progress=payload.unit_progress,
            pbl_count=payload.pbl_count if payload.pbl_count is not None else 0,
            checkin_rate=payload.checkin_rate,
            next_milestone=payload.next_milestone,
            coach_name=payload.coach_name,
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


def family_scope_query(db: Session, coach_name: str = ""):
    query = db.query(Family).order_by(Family.family_id)
    clean_coach = (coach_name or "").strip()
    if clean_coach:
        query = query.filter(Family.coach_name == clean_coach)
    return query


def infer_family_service_stage(db: Session, family: Family, now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.utcnow()
    explicit_status = family.service_status or ""
    if has_any(explicit_status, ("已结课", "结课", "结束服务")):
        return "已结课", "服务状态已标记结课"
    if has_any(explicit_status, RENEWAL_TERMS):
        return "续报", "服务状态已进入续报阶段"

    profile = db.query(ParentProfile).filter(ParentProfile.family_id == family.family_id).one_or_none()
    recent_messages = (
        db.query(RawMessage)
        .filter(RawMessage.family_id == family.family_id)
        .order_by(RawMessage.message_time.desc())
        .limit(20)
        .all()
    )
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

    pending_tasks = db.query(SendTask).filter(SendTask.family_id == family.family_id, SendTask.status == "pending").count()
    review_outputs = db.query(AIOutput).filter(AIOutput.family_id == family.family_id, AIOutput.status == "needs_review").count()
    review_reports = db.query(WeeklyReport).filter(WeeklyReport.family_id == family.family_id, WeeklyReport.status != "approved").count()
    last_msg = recent_messages[0] if recent_messages else None
    silent_days = (now - last_msg.message_time).days if last_msg else 999
    if pending_tasks or review_outputs or review_reports:
        return "需跟进", "存在待发送/待审核事项"
    if silent_days >= 3:
        return "需跟进", f"已 {silent_days} 天无最新沟通"
    if has_any(signal_text, FOLLOWUP_TERMS):
        return "需跟进", "出现打卡/PBL/请假补课跟进信号"
    return "正常", "暂无高优先级异常"


def build_service_funnel(db: Session, coach_name: str = "", now: datetime | None = None, family_limit: int = 8) -> dict:
    now = now or datetime.utcnow()
    buckets = {stage: [] for stage in SERVICE_FUNNEL_STAGES}
    for family in family_scope_query(db, coach_name).all():
        stage, reason = infer_family_service_stage(db, family, now)
        last_msg = latest_family_message(db, family.family_id)
        buckets[stage].append({
            "family_id": family.family_id,
            "family_name": family.parent_nickname or family.family_id,
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
        "total_families": sum(item["family_count"] for item in stages),
        "stages": stages,
    }


def todo_item(family: Family, reason: str, evidence: str = "", related_id: int = 0, occurred_at=None) -> dict:
    return {
        "family_id": family.family_id,
        "family_name": family.parent_nickname or family.family_id,
        "coach_name": family.coach_name,
        "reason": reason,
        "evidence": evidence or "",
        "related_id": related_id,
        "occurred_at": timeline_time(occurred_at) if occurred_at else "",
    }


def build_workbench_todos(db: Session, coach_name: str = "", limit: int = 8, now: datetime | None = None) -> dict:
    safe_limit = min(max(limit, 1), 30)
    categories = {
        "pbl_incomplete": {"label": "PBL未完成", "items": []},
        "leave_makeup": {"label": "请假补课", "items": []},
        "weekly_pending_send": {"label": "周报待发", "items": []},
        "negative_feedback": {"label": "负面反馈", "items": []},
        "ai_review": {"label": "AI待审核", "items": []},
        "send_failed": {"label": "发送失败", "items": []},
    }

    for family in family_scope_query(db, coach_name).all():
        messages = (
            db.query(RawMessage)
            .filter(RawMessage.family_id == family.family_id)
            .order_by(RawMessage.message_time.desc())
            .limit(80)
            .all()
        )
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

        weekly = (
            db.query(WeeklyReport)
            .filter(
                WeeklyReport.family_id == family.family_id,
                WeeklyReport.status == "approved",
                or_(
                    WeeklyReport.send_status.notin_(["sent", "dry_run"]),
                    WeeklyReport.send_status.is_(None),
                    WeeklyReport.send_status == "",
                ),
            )
            .order_by(WeeklyReport.updated_at.desc())
            .first()
        )
        if weekly:
            reason = "周报已审核但尚未完成发送闭环"
            categories["weekly_pending_send"]["items"].append(todo_item(family, reason, weekly.final_text, weekly.id, weekly.updated_at))

        profile = db.query(ParentProfile).filter(ParentProfile.family_id == family.family_id).one_or_none()
        risk_text = " ".join([profile.service_risks if profile else "", profile.suggested_actions if profile else ""])
        risk_msg = next((msg for msg in messages if has_any(msg.content or "", RISK_TERMS)), None)
        if has_any(risk_text, RISK_TERMS) or risk_msg:
            categories["negative_feedback"]["items"].append(
                todo_item(family, "出现退费/投诉/不满等负面信号", risk_msg.content if risk_msg else risk_text, risk_msg.id if risk_msg else 0, risk_msg.message_time if risk_msg else None)
            )

        output = (
            db.query(AIOutput)
            .filter(AIOutput.family_id == family.family_id, AIOutput.status == "needs_review")
            .order_by(AIOutput.updated_at.desc())
            .first()
        )
        if output:
            categories["ai_review"]["items"].append(todo_item(family, "AI 输出需要人工审核", output.edited_output or output.display_text, output.id, output.updated_at or output.created_at))

        failed_log = (
            db.query(SendLog)
            .filter(SendLog.family_id == family.family_id, SendLog.status == "failed")
            .order_by(SendLog.sent_at.desc())
            .first()
        )
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
        "categories": result,
    }


def build_workbench_overview(db: Session, coach_name: str = "", limit: int = 8, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
    return {
        "service_funnel": build_service_funnel(db, coach_name, now, limit),
        "todos": build_workbench_todos(db, coach_name, limit, now),
    }


def build_admin_service_quality_dashboard(db: Session, coach_name: str = "", now: datetime | None = None) -> dict:
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
    for family in family_scope_query(db, coach_name).all():
        grouped.setdefault(family.coach_name or "未分配", []).append(family)

    for coach, families in sorted(grouped.items()):
        row = {
            "coach_name": coach,
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
            stage, reason = infer_family_service_stage(db, family, now)
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

            row["pending_task_count"] += db.query(SendTask).filter(SendTask.family_id == family.family_id, SendTask.status.in_(["pending", "assigned"])).count()
            row["review_output_count"] += db.query(AIOutput).filter(AIOutput.family_id == family.family_id, AIOutput.status == "needs_review").count()
            row["review_report_count"] += db.query(WeeklyReport).filter(WeeklyReport.family_id == family.family_id, WeeklyReport.status != "approved").count()
            logs = db.query(SendLog).filter(SendLog.family_id == family.family_id).all()
            row["send_log_count"] += len(logs)
            row["sent_count"] += sum(1 for log in logs if log.status == "sent")
            row["dry_run_count"] += sum(1 for log in logs if log.status == "dry_run")
            row["failed_count"] += sum(1 for log in logs if log.status == "failed")

        completed = row["sent_count"] + row["dry_run_count"]
        if row["send_log_count"]:
            row["send_completion_rate"] = round(completed / row["send_log_count"], 4)
            row["send_failure_rate"] = round(row["failed_count"] / row["send_log_count"], 4)
        row["risk_families"] = row["risk_families"][:5]
        for key in totals:
            totals[key] += row.get(key, 0)
        rows.append(row)

    totals["coach_count"] = len(rows)
    totals["send_completion_rate"] = round((totals["sent_count"] + totals["dry_run_count"]) / totals["send_log_count"], 4) if totals["send_log_count"] else 0.0
    totals["send_failure_rate"] = round(totals["failed_count"] / totals["send_log_count"], 4) if totals["send_log_count"] else 0.0
    rows.sort(key=lambda item: (-item["risk_family_count"], -item["pending_task_count"], item["coach_name"]))
    return {
        "generated_at": timeline_time(now),
        "coach_name": (coach_name or "").strip(),
        "totals": totals,
        "coaches": rows,
    }


def build_today_priorities(db: Session, limit: int = 12, now: datetime | None = None) -> list[dict]:
    now = now or datetime.utcnow()
    safe_limit = min(max(limit, 1), 50)
    items: list[dict] = []
    families = db.query(Family).order_by(Family.family_id).all()
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

        if not reasons:
            continue
        if pending_tasks:
            action = "先处理待发送任务"
        elif review_outputs or review_reports:
            action = "先审核 AI 内容/周报"
        elif score >= 45:
            action = "查看家庭时间线并人工跟进"
        else:
            action = "保持观察"
        items.append({
            "family_id": family.family_id,
            "family_name": family.parent_nickname or family.family_id,
            "coach_name": family.coach_name,
            "score": score,
            "level": priority_level(score),
            "reasons": reasons,
            "suggested_action": action,
            "last_message_at": timeline_time(last_msg.message_time) if last_msg else "",
            "pending_task_count": len(pending_tasks),
            "review_output_count": review_outputs,
            "review_report_count": review_reports,
        })

    items.sort(key=lambda item: (-item["score"], item["family_id"]))
    return items[:safe_limit]


# 单个家庭详情页要的消息、画像和周报都在这里聚合返回。
@app.get("/api/families/{family_id}")
def family_detail(family_id: str, timeline_limit: int = 80, request: Request = None, db: Session = Depends(get_db)):
    family = db.query(Family).filter(Family.family_id == family_id).one_or_none()
    if not family:
        raise HTTPException(404, "家庭不存在")
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
    require_family(db, family_id)
    return maybe_redact_for_request(build_family_timeline(db, family_id, limit), request)


@app.get("/api/followups")
def list_followups(family_id: str = "", status: str = "", request: Request = None, db: Session = Depends(get_db)):
    query = db.query(FollowupRecord)
    if family_id:
        query = query.filter(FollowupRecord.family_id == family_id)
    if status:
        query = query.filter(FollowupRecord.status == status)
    rows = query.order_by(FollowupRecord.occurred_at.desc(), FollowupRecord.id.desc()).all()
    return maybe_redact_for_request([as_dict(item) for item in rows], request)


@app.post("/api/families/{family_id}/followups")
def create_family_followup(family_id: str, payload: FollowupIn, request: Request = None, db: Session = Depends(get_db)):
    family = require_family(db, family_id)
    data = clean_followup_payload(payload)
    record = FollowupRecord(family_id=family.family_id, created_by=actor_from_request(request), **data)
    db.add(record)
    db.commit()
    return maybe_redact_for_request(as_dict(record), request)


@app.get("/api/workbench/today-priorities")
def today_priorities(limit: int = 12, request: Request = None, db: Session = Depends(get_db)):
    return maybe_redact_for_request(build_today_priorities(db, limit), request)


@app.get("/api/workbench/overview")
def workbench_overview(coach_name: str = "", limit: int = 8, request: Request = None, db: Session = Depends(get_db)):
    return maybe_redact_for_request(build_workbench_overview(db, coach_name, limit), request)


@app.get("/api/admin/service-quality")
def admin_service_quality(coach_name: str = "", request: Request = None, db: Session = Depends(get_db)):
    return maybe_redact_for_request(build_admin_service_quality_dashboard(db, coach_name), request)


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
def generate_all(db: Session = Depends(get_db)):
    count = 0
    for family in db.query(Family).all():
        if generate_for_family(db, family.family_id):
            count += 1
    db.commit()
    return {"generated_families": count}


# 单家庭生成接口，前端家庭详情页会直接调用。
@app.post("/api/families/{family_id}/generate")
def generate_one(family_id: str, db: Session = Depends(get_db)):
    result = generate_for_family(db, family_id)
    if not result:
        raise HTTPException(404, "没有可生成的有效消息")
    db.commit()
    return result


@app.post("/api/families/{family_id}/ai-bundle")
def generate_family_ai_bundle(family_id: str, payload: FamilyAIBundleIn | None = None, request: Request = None, db: Session = Depends(get_db)):
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
        query = query.filter(AIOutput.family_id == family_id)
    if agent_type:
        query = query.filter(AIOutput.agent_type == agent_type)
    return maybe_redact_for_request([as_dict(item) for item in query.limit(200).all()], request)


# 保存人工审核后的 AI 输出。
@app.put("/api/ai-outputs/{output_id}")
def update_ai_output(output_id: int, payload: AIOutputUpdate, db: Session = Depends(get_db)):
    output = db.get(AIOutput, output_id)
    if not output:
        raise HTTPException(404, "AI输出不存在")
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
    family = db.query(Family).filter(Family.family_id == output.family_id).one_or_none()
    data = payload or AIOutputTaskIn()
    content = data.content.strip() or output.edited_output or output.display_text
    content = validate_send_task_content(content)
    target_name = data.target_name.strip() or (family.parent_nickname if family else output.family_id)
    scene = data.scene.strip() or output.source or output.agent_type
    if data.device_id:
        ensure_new_task_operation_allowed(request, "assign_device")
    device_id = validate_task_device_binding(db, data.device_id, target_name)
    send_mode = validate_send_mode_submit(data.send_mode, data.confirm_real_send)
    validate_ai_output_send_boundary(output, content, send_mode)
    if send_mode == "real_send":
        ensure_new_task_operation_allowed(request, "confirm_real_send")
        validate_real_send_risk(db, target_name, content)
    task = SendTask(
        family_id=output.family_id,
        target_name=target_name,
        scene=scene,
        content=content,
        device_id=device_id,
        send_mode=send_mode,
        status="pending",
    )
    output.status = "task_created"
    output.edited_output = content
    output.updated_at = datetime.utcnow()
    add_send_task_with_audit(db, task, "create", actor_from_request(request), f"AI 输出 {output_id} 创建发送任务")
    db.commit()
    return send_task_view(task, request)


# 家庭画像 Agent 接口。
@app.post("/api/agent/profile")
@app.post("/agent/profile")
def run_profile_agent(payload: AgentRequest, db: Session = Depends(get_db)):
    family = require_family(db, payload.family_id)
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
def run_weekly_report_agent(payload: AgentRequest, db: Session = Depends(get_db)):
    family = require_family(db, payload.family_id)
    context = build_agent_context(db, payload.family_id)
    result = run_weekly_report_agent_service(context)
    output = save_ai_output(db, payload.family_id, "weekly_report", payload.source or "生成周报", result)
    create_weekly_report_from_agent(db, payload.family_id, result)
    db.commit()
    return {**as_dict(output), "family_name": family.parent_nickname}


# 回复 Agent 接口。
@app.post("/api/agent/reply")
@app.post("/agent/reply")
def run_reply_agent(payload: AgentRequest, db: Session = Depends(get_db)):
    family = require_family(db, payload.family_id)
    context = build_agent_context(db, payload.family_id)
    result = run_reply_agent_service(context, payload.message, payload.tone)
    output = save_ai_output(db, payload.family_id, "ai_reply", payload.source or "生成回复", result)
    db.commit()
    return {**as_dict(output), "family_name": family.parent_nickname}


# 打卡/PBL Agent 接口，同时写入打卡记录。
@app.post("/api/agent/checkin-pbl")
@app.post("/agent/checkin-pbl")
def run_checkin_pbl_agent(payload: AgentRequest, db: Session = Depends(get_db)):
    family = require_family(db, payload.family_id)
    context = build_agent_context(db, payload.family_id)
    result = run_checkin_pbl_agent_service(context)
    output = save_ai_output(db, payload.family_id, "checkin_pbl", payload.source or "识别打卡/PBL", result)
    created = create_checkin_records_from_context(db, context)
    db.commit()
    return {**as_dict(output), "family_name": family.parent_nickname, "checkin_records_created": created}


# RPA 同步企业微信会话消息，并可选自动生成回复任务。
@app.post("/api/rpa/conversations/sync")
def sync_rpa_conversation(payload: RpaConversationIn, request: Request = None, db: Session = Depends(get_db)):
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
            coach_name=payload.coach_name,
            service_status="企微RPA同步",
        )
        db.add(family)
        db.flush()
    family_id = family.family_id

    inserted = 0
    latest_parent_message = payload.latest_message.strip()
    for msg in payload.messages:
        content = msg.content.strip()
        if not content:
            continue
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
        db.add(
            RawMessage(
                family_id=family_id,
                message_time=parse_rpa_time(msg.message_time),
                speaker=msg.speaker or payload.target_name,
                content=content,
                source=msg.source,
                checkin_status=detect_checkin(content),
                is_effective="Y" if len(content) >= 2 else "N",
            )
        )
        inserted += 1
        latest_parent_message = content

    ai_output = None
    task = None
    generated_outputs = []
    if payload.auto_generate_reply and latest_parent_message:
        db.flush()
        context = build_agent_context(db, family_id)
        result = run_reply_agent_service(context, latest_parent_message, "standard")
        ai_output = save_ai_output(db, family_id, "ai_reply", f"企微RPA：{payload.target_name}", result)
        generated_outputs.append(ai_output)
        if payload.auto_create_reply_task:
            task = SendTask(
                family_id=family_id,
                target_name=payload.target_name,
                scene=result["raw"].get("场景类型", "企微AI回复"),
                content=result["display_text"],
                status="pending",
            )
            ai_output.status = "task_created"
            add_send_task_with_audit(db, task, "create", actor_from_request(request, "企微RPA"), f"企微会话「{payload.target_name}」自动生成回复任务")

    if payload.auto_generate_all_agents:
        db.flush()
        context = build_agent_context(db, family_id)
        if context["messages"]:
            profile_result = run_family_profile_agent_service(context)
            profile_output = save_ai_output(db, family_id, "family_profile", f"企微RPA：{payload.target_name}", profile_result)
            generated_outputs.append(profile_output)
            upsert_parent_profile_from_agent(db, family_id, profile_result)

            weekly_result = run_weekly_report_agent_service(context)
            weekly_output = save_ai_output(db, family_id, "weekly_report", f"企微RPA：{payload.target_name}", weekly_result)
            generated_outputs.append(weekly_output)
            create_weekly_report_from_agent(db, family_id, weekly_result)

            checkin_result = run_checkin_pbl_agent_service(context)
            checkin_output = save_ai_output(db, family_id, "checkin_pbl", f"企微RPA：{payload.target_name}", checkin_result)
            generated_outputs.append(checkin_output)
            create_checkin_records_from_context(db, context)

    db.commit()
    return {
        "family_id": family_id,
        "target_name": payload.target_name,
        "messages_inserted": inserted,
        "ai_output": as_dict(ai_output) if ai_output else None,
        "generated_outputs": [as_dict(item) for item in generated_outputs],
        "send_task": as_dict(task) if task else None,
    }


@app.get("/api/rpa/conversations/resolve")
def resolve_rpa_conversation(target_name: str, db: Session = Depends(get_db)):
    family = db.query(Family).filter(Family.parent_nickname == target_name).one_or_none()
    if not family:
        family = db.query(Family).filter(Family.family_id == target_name).one_or_none()
    return {
        "exists": bool(family),
        "target_name": target_name,
        "family": as_dict(family) if family else None,
    }


# 周报列表接口。
@app.get("/api/reports")
def list_reports(request: Request = None, db: Session = Depends(get_db)):
    return maybe_redact_for_request([as_dict(r) for r in db.query(WeeklyReport).order_by(WeeklyReport.id.desc()).all()], request)


# 一键把未审核周报全部标成已审核。
@app.post("/api/reports/approve-all")
def approve_all_reports(db: Session = Depends(get_db)):
    count = 0
    for report in db.query(WeeklyReport).filter(WeeklyReport.status != "approved").all():
        report.status = "approved"
        count += 1
    db.commit()
    return {"approved": count}


# 单条周报人工更新接口。
@app.put("/api/reports/{report_id}")
def update_report(report_id: int, payload: ReportUpdate, db: Session = Depends(get_db)):
    report = db.get(WeeklyReport, report_id)
    if not report:
        raise HTTPException(404, "周报不存在")
    report.final_text = payload.final_text
    report.status = payload.status
    db.commit()
    return as_dict(report)


# 家长画像列表接口。
@app.get("/api/profiles")
def list_profiles(request: Request = None, db: Session = Depends(get_db)):
    return maybe_redact_for_request([as_dict(p) for p in db.query(ParentProfile).order_by(ParentProfile.family_id).all()], request)


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
def scan_checkins(db: Session = Depends(get_db)):
    created = 0
    for msg in db.query(RawMessage).all():
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
    reports = db.query(WeeklyReport).filter(WeeklyReport.status == "approved").all()
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
    task, created = ensure_weekly_report_send_task(db, report, actor_from_request(request))
    db.commit()
    return {"created": created, "report": as_dict(report), "task": as_dict(task)}


# 按场景规则和话术模板创建发送任务。
@app.post("/api/send-tasks/from-scenes")
def create_tasks_from_scenes(request: Request = None, db: Session = Depends(get_db)):
    created = 0
    templates = {t.scene: t.content for t in db.query(Template).filter(Template.enabled == "Y").all()}
    for msg in db.query(RawMessage).order_by(RawMessage.message_time.desc()).limit(200):
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
    query = db.query(SendTask)
    if status:
        query = query.filter(SendTask.status == status)
    if device_id:
        query = query.filter(SendTask.device_id == device_id)
    return maybe_redact_for_request([send_task_view(t, request) for t in query.order_by(SendTask.id.desc()).all()], request)


# 直接新增一条发送任务。
@app.post("/api/send-tasks")
def create_send_task(payload: SendTaskIn, request: Request = None, db: Session = Depends(get_db)):
    data = payload.model_dump()
    data["content"] = validate_send_task_content(data.get("content", ""))
    data["send_mode"] = validate_send_mode_submit(data.get("send_mode", "dry_run"), bool(data.pop("confirm_real_send", False)))
    if data.get("device_id"):
        ensure_new_task_operation_allowed(request, "assign_device")
    data["device_id"] = validate_task_device_binding(db, data.get("device_id", ""), data.get("target_name", ""))
    if data["send_mode"] == "real_send":
        ensure_new_task_operation_allowed(request, "confirm_real_send")
        validate_real_send_risk(db, data.get("target_name", ""), data["content"])
    task = SendTask(**data)
    add_send_task_with_audit(db, task, "create", actor_from_request(request), "控制端手动创建发送任务")
    db.commit()
    return send_task_view(task, request)


# 更新发送任务的内容和状态。
@app.put("/api/send-tasks/{task_id}")
def update_send_task(task_id: int, payload: SendTaskUpdate, request: Request = None, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
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
    if payload.device_id is not None and payload.device_id != task.device_id:
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
    clean_device_id = validate_task_device_binding(db, device_id or "", target_name)
    if send_mode == "real_send":
        validate_real_send_risk(db, target_name, content, exclude_task_id=task.id)
    task.target_name = target_name
    task.scene = scene
    task.content = content
    task.send_mode = send_mode
    task.device_id = clean_device_id
    task.status = payload.status
    if task.status == "pending":
        task.scheduled_at = datetime.utcnow()
    action = "confirm_real_send" if previous_mode != "real_send" and send_mode == "real_send" else "update"
    summary = "确认真实发送" if action == "confirm_real_send" else "更新发送任务"
    audit_send_task_change(db, task, action, actor_from_request(request), summary, before)
    sync_weekly_report_send_status(db, task)
    db.commit()
    return send_task_view(task, request)


@app.post("/api/send-tasks/{task_id}/cancel")
def cancel_send_task(task_id: int, request: Request = None, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    ensure_task_operation_allowed(task, request, "cancel")
    before = send_task_snapshot(task)
    task.status = "cancelled"
    audit_send_task_change(db, task, "cancel", actor_from_request(request), "取消发送任务", before)
    sync_weekly_report_send_status(db, task, "cancelled")
    db.commit()
    return send_task_view(task, request)


@app.post("/api/send-tasks/{task_id}/dry-run")
def queue_task_dry_run(task_id: int, request: Request = None, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
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
    return send_task_view(task, request)


@app.post("/api/send-tasks/{task_id}/retry")
def retry_failed_task(task_id: int, request: Request = None, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    ensure_task_operation_allowed(task, request, "retry")
    if task.status != "failed":
        raise HTTPException(400, "只有 failed 状态的任务可以重试")
    content = validate_send_task_content(task.content)
    clean_device_id = validate_task_device_binding(db, task.device_id or "", task.target_name)
    if task.send_mode == "real_send":
        validate_real_send_risk(db, task.target_name, content, exclude_task_id=task.id)
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
    return send_task_view(task, request)


@app.post("/api/send-tasks/{task_id}/result")
def record_send_result(task_id: int, payload: SendResultIn, request: Request = None, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if payload.status not in {"sent", "failed", "skipped", "dry_run"}:
        raise HTTPException(400, "status 只能是 sent/failed/skipped/dry_run")
    screenshot_path = store_send_screenshot(task.id, payload.screenshot_base64)
    before = send_task_snapshot(task)
    finished_at = datetime.utcnow()
    retry_action = ""
    retry_summary = ""
    if payload.status == "failed":
        retry_action, retry_summary = apply_failed_send_retry_policy(task, payload.detail, finished_at)
    else:
        task.status = payload.status
        task.last_error = ""
        task.next_retry_at = None
        if payload.status in {"sent", "dry_run"}:
            task.retry_count = 0
    log = SendLog(
        task_id=task.id,
        family_id=task.family_id,
        target_name=task.target_name,
        status=payload.status,
        send_mode=send_log_mode(task),
        detail=payload.detail,
        device_id=payload.device_id or task.device_id,
        screenshot_path=screenshot_path,
        sent_at=finished_at,
    )
    db.add(log)
    actor = actor_from_request(request, f"设备:{payload.device_id}" if payload.device_id else "RPA被控端")
    audit_send_task_change(db, task, "result", actor, f"回写发送结果：{payload.status}", before)
    if retry_action == "auto_retry":
        log_audit_event(db, "send_task", task.id, "auto_retry", actor, retry_summary, before=before, after=send_task_snapshot(task))
        sync_weekly_report_send_status(db, task, "pending")
    else:
        if retry_action == "alert":
            log_audit_event(db, "send_task", task.id, "send_alert", actor, retry_summary, before=before, after=send_task_snapshot(task))
        sync_weekly_report_send_status(db, task, payload.status, finished_at)
    db.commit()
    return send_log_view(log)


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
    ensure_task_operation_allowed(task, request, "web_send")
    message, log = send_task_to_web_chat(db, task, actor_from_request(request), "web_send")
    db.commit()
    return {"task": send_task_view(task, request), "message": as_dict(message), "log": send_log_view(log)}


@app.post("/api/send-tasks/web-send-all")
def web_send_all(request: Request = None, db: Session = Depends(get_db)):
    sent = 0
    skipped = 0
    for task in db.query(SendTask).filter(SendTask.status == "pending").all():
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
    return maybe_redact_for_request([send_log_view(l) for l in db.query(SendLog).order_by(SendLog.id.desc()).all()], request)


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
def get_send_artifact(filename: str):
    path = resolve_send_screenshot(filename)
    return FileResponse(path)


# ============ 设备（多被控端总控）============
HEARTBEAT_ONLINE_SECONDS = 90      # 心跳在这个时间窗内算 online
CLAIM_TIMEOUT_SECONDS = 300        # assigned 超过这个时间没回写就回收成 pending


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
    online = bool(dev.last_heartbeat) and (datetime.utcnow() - dev.last_heartbeat) <= timedelta(seconds=HEARTBEAT_ONLINE_SECONDS)
    data["online"] = online
    try:
        data["conversation_count"] = len(json.loads(dev.conversations or "[]"))
    except Exception:
        data["conversation_count"] = 0
    counts = {}
    for st in ("pending", "assigned", "sent", "failed"):
        counts[st] = db.query(SendTask).filter(SendTask.device_id == dev.device_id, SendTask.status == st).count()
    data["task_counts"] = counts
    return data


# 注册或更新设备：新建时自动生成 token 并返回（之后 RPA 用它鉴权）。
@app.post("/api/devices")
def register_device(payload: DeviceIn, db: Session = Depends(get_db)):
    dev = db.query(Device).filter(Device.device_id == payload.device_id).first()
    convs = json.dumps(payload.conversations, ensure_ascii=False) if payload.conversations else None
    if dev:
        dev.name = payload.name or dev.name
        dev.note = payload.note or dev.note
        if convs is not None:
            dev.conversations = convs
    else:
        dev = Device(
            device_id=payload.device_id,
            name=payload.name,
            note=payload.note,
            token=secrets.token_hex(16),
            conversations=convs or "[]",
        )
        db.add(dev)
    db.commit()
    return as_dict(dev)


# 生成某台设备的「接入包」zip：被控端脚本 + 注入 token 的配置 + 一键启动 bat，发给对方双击即用。
@app.get("/api/devices/{device_id}/package")
def download_device_package(device_id: str, server_url: str = "", db: Session = Depends(get_db)):
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
        "device_id": dev.device_id,
        "device_token": dev.token,
        "watch_conversations": convs,
        "allowed_conversations": convs,
        "use_local_ocr": False,          # 被控端走 ARK 云端定位，不装 paddleocr
        "use_ark_vision_fallback": True,
        "enable_search_fallback": True,
        "dry_run": True,                 # 默认安全：只粘贴不发；真实发送走任务 real_send + allow_real_send 双确认
        "allow_real_send": False,         # 被控端二次硬开关：不显式打开就算任务 real_send 也不按发送键
        "auto_launch_wecom": False,
    })

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 复制项目文件，保持相对结构让 import 正常（wecom_sender 会把 main/ 加入 sys.path 再 import app.services）
        zf.write(ROOT / "rpa" / "wecom_sender.py", "rpa/wecom_sender.py")
        zf.write(ROOT / "rpa" / "send_guard.py", "rpa/send_guard.py")
        zf.write(ROOT / "app" / "services" / "ark_client.py", "app/services/ark_client.py")
        zf.writestr("app/__init__.py", "")
        zf.writestr("app/services/__init__.py", "")
        ark_path = ROOT / "config" / "ark.json"
        if ark_path.exists():
            zf.write(ark_path, "config/ark.json")
        # 注入的设备专属配置
        zf.writestr("rpa/config.json", json.dumps(client_cfg, ensure_ascii=False, indent=2))
        # 启动脚本 / 依赖 / 说明
        for src, arc in [
            (ROOT / "rpa" / "requirements-client.txt", "requirements-client.txt"),
            (ROOT / "rpa" / "templates" / "启动.bat", "启动.bat"),
            (ROOT / "rpa" / "templates" / "使用说明.txt", "使用说明.txt"),
        ]:
            if src.exists():
                zf.write(src, arc)
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


# 修改设备显示名/备注。
@app.put("/api/devices/{device_id}")
def update_device(device_id: str, payload: DeviceIn, db: Session = Depends(get_db)):
    dev = db.query(Device).filter(Device.device_id == device_id).first()
    if not dev:
        raise HTTPException(404, "设备不存在")
    dev.name = payload.name or dev.name
    dev.note = payload.note or dev.note
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
    if payload.conversations:
        dev.conversations = json.dumps(payload.conversations, ensure_ascii=False)
    db.commit()
    return device_view(dev, db)


# 动态领取：把该设备负责会话的 pending 任务原子分配给它，返回领到的任务。
@app.post("/api/devices/{device_id}/claim")
def claim_tasks(device_id: str, limit: int = 5, dev: Device = Depends(require_device), db: Session = Depends(get_db)):
    if dev.device_id != device_id:
        raise HTTPException(403, "device_id 与鉴权头不一致")
    # 先回收超时未回写的 assigned 任务（设备领了但崩溃），避免任务卡死。
    stale_before = datetime.utcnow() - timedelta(seconds=CLAIM_TIMEOUT_SECONDS)
    db.query(SendTask).filter(
        SendTask.device_id == dev.device_id,
        SendTask.status == "assigned",
        SendTask.scheduled_at < stale_before,
    ).update({"status": "pending"})
    db.commit()

    try:
        convs = json.loads(dev.conversations or "[]")
    except Exception:
        convs = []
    if not convs:
        return []
    candidates = (
        db.query(SendTask)
        .filter(SendTask.status == "pending", SendTask.target_name.in_(convs))
        .filter(or_(SendTask.device_id == "", SendTask.device_id == dev.device_id, SendTask.device_id.is_(None)))
        .filter(or_(SendTask.next_retry_at.is_(None), SendTask.next_retry_at <= datetime.utcnow()))
        .order_by(SendTask.id)
        .limit(limit)
        .all()
    )
    claimed = []
    for task in candidates:
        try:
            task.content = validate_send_task_content(task.content)
            task.send_mode = validate_send_task_execution_guard(task)
            if task.send_mode == "real_send":
                validate_real_send_risk(db, task.target_name, task.content, exclude_task_id=task.id)
        except HTTPException as exc:
            before = send_task_snapshot(task)
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
        # 原子领取：只有仍是 pending 才领得到，避免两台设备抢到同一条。
        # 同时把 scheduled_at 刷成领取时刻，作为超时回收的判断依据。
        before = send_task_snapshot(task)
        assigned_at = datetime.utcnow()
        updated = (
            db.query(SendTask)
            .filter(SendTask.id == task.id, SendTask.status == "pending")
            .update({"status": "assigned", "device_id": dev.device_id, "scheduled_at": assigned_at})
        )
        if updated == 1:
            task.status = "assigned"
            task.device_id = dev.device_id
            task.scheduled_at = assigned_at
            claimed.append(task)
            audit_send_task_change(db, task, "assign_device", f"设备:{dev.device_id}", "设备领取发送任务", before)
            sync_weekly_report_send_status(db, task, "assigned")
    db.commit()
    return [as_dict(db.get(SendTask, t.id)) for t in claimed]


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
        return create_sqlite_backup(DATABASE_URL, BACKUP_DIR, ROOT)
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
