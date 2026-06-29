"""FastAPI 应用入口。

这个文件把数据导入、Agent 生成、发送任务和 RPA 同步等接口组装成完整的本地 MVP。
"""

import io
import json
import re
import secrets
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db import get_db, init_db
from app.models import (
    AIOutput,
    CheckinRecord,
    Device,
    Family,
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
from app.services.ai_mock import generate_parent_profile, generate_weekly_report
from app.services.importer import import_rows, rows_from_upload
from app.services.scenario import detect_checkin, detect_scene

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
STATIC = Path(__file__).resolve().parent / "static"

# 应用实例和 CORS 配置，便于本地前端直接访问。
app = FastAPI(title="重庆机构陪跑师效率系统 MVP", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
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
    coach_name: str = ""
    service_status: str = "企微待同步"


# 回写发送结果时记录状态和详情。
class SendResultIn(BaseModel):
    status: str
    detail: str = ""
    device_id: str = ""


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


def validate_send_mode(send_mode: str) -> str:
    mode = (send_mode or "dry_run").strip()
    if mode not in {"dry_run", "real_send"}:
        raise HTTPException(400, "send_mode 只能是 dry_run 或 real_send")
    return mode


def validate_send_mode_submit(send_mode: str, confirm_real_send: bool, current_mode: str = "") -> str:
    mode = validate_send_mode(send_mode)
    if mode == "real_send" and current_mode != "real_send" and not confirm_real_send:
        raise HTTPException(400, "真实发送需要显式确认")
    return mode


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
    if db.query(Template).count():
        return
    defaults = [
        ("早间打卡提醒", "打卡提醒", "早上好，今天先完成计划里的第一个小任务，完成后记得发打卡哦。", "08:30"),
        ("完成打卡回复", "完成打卡", "收到，今天这个动作完成得不错，继续保持这个节奏。", ""),
        ("未完成打卡回复", "未完成", "收到，今天先不加压，我们把任务拆小一点，晚点完成最关键的一项即可。", ""),
        ("请假回复", "请假/孩子有事", "收到，今天先以孩子状态为主。后续我会帮您把本次内容衔接上。", ""),
        ("资料链接回复", "资料/链接领取", "资料我稍后发您，请优先看标注部分，有问题直接在群里问我。", ""),
        ("课程时间回复", "课程时间询问", "课程时间以群内通知为准，我也会提前提醒您。", ""),
        ("PBL提交提醒", "PBL提交", "{家长称呼}您好，今天是PBL输出节点，建议先让孩子完成基础表达，再补充一个自己的发现。", "19:00"),
        ("未完成提醒", "未完成", "{家长称呼}您好，今天如果来不及，可以先补最核心的一项，保持节奏不断就很好。", "20:00"),
        ("负面反馈转人工", "转人工", "{家长称呼}您好，您的反馈我已经收到，这类情况我会先和主管/老师确认，再给您明确回复。", ""),
        ("续报意向沟通", "续报", "{家长称呼}您好，孩子这一阶段的学习变化我整理好了，也想和您同步一下后续学习节奏。", ""),
    ]
    for name, scene, content, send_time in defaults:
        db.add(Template(name=name, scene=scene, content=content, send_time=send_time))
    db.commit()


# 把 Agent 结果写入 ai_outputs 表，作为后续人工审核入口。
def save_ai_output(db: Session, family_id: str, agent_type: str, source: str, result: dict) -> AIOutput:
    output = AIOutput(
        family_id=family_id,
        agent_type=agent_type,
        source=source,
        raw_json=json.dumps(result["raw"], ensure_ascii=False, indent=2),
        display_text=result["display_text"],
        edited_output=result["display_text"],
        status="needs_review",
        risk_level=result["risk_level"],
        need_human_review="Y" if result["need_human_review"] else "N",
        suggested_actions="、".join(result["suggested_actions"]),
    )
    db.add(output)
    return output


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
        "child_summary": raw.get("学生状态", ""),
        "service_risks": join_agent_field(raw.get("风险信号")),
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


def account_payload(account: UserAccount) -> dict:
    return {key: value for key, value in as_dict(account).items() if key != "password"}


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
    init_db()
    db = next(get_db())
    try:
        seed_templates(db)
    finally:
        db.close()


    # 首页返回静态前端页面。
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# 健康检查接口给 RPA 和前端都可以用。
@app.get("/health")
def health():
    return {"ok": True, "mode": "local-mvp"}


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
    return account_payload(account)


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
def generate_test_chat_ai(payload: ChatAiIn, db: Session = Depends(get_db)):
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
        db.add(task)

    db.commit()
    return {
        "family_id": family.family_id,
        "profile_output": as_dict(profile_output),
        "reply_output": as_dict(reply_output),
        "send_task": as_dict(task) if task else None,
        "elapsed_ms": int((perf_counter() - started) * 1000),
    }


@app.post("/api/test-chat/reply")
def generate_test_chat_reply(payload: ChatReplyIn, db: Session = Depends(get_db)):
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
        db.add(task)

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


# 载入内置样例数据，方便演示和联调。
@app.post("/api/sample-data")
def load_sample_data(db: Session = Depends(get_db)):
    sample = SAMPLES / "sample_messages.csv"
    rows = rows_from_upload(sample.name, sample.read_bytes())
    return import_rows(db, rows)


# 家庭列表接口，顺带补充每个家庭的消息数。
@app.get("/api/families")
def list_families(db: Session = Depends(get_db)):
    families = db.query(Family).order_by(Family.family_id).all()
    return [{**as_dict(f), "message_count": db.query(RawMessage).filter(RawMessage.family_id == f.family_id).count()} for f in families]


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
        family.coach_name = payload.coach_name
        family.service_status = payload.service_status
    else:
        family = Family(
            family_id=family_id,
            parent_nickname=target_name,
            child_grade=payload.child_grade,
            coach_name=payload.coach_name,
            service_status=payload.service_status,
        )
        db.add(family)
    db.commit()
    return {**as_dict(family), "message_count": db.query(RawMessage).filter(RawMessage.family_id == family.family_id).count()}


# 单个家庭详情页要的消息、画像和周报都在这里聚合返回。
@app.get("/api/families/{family_id}")
def family_detail(family_id: str, db: Session = Depends(get_db)):
    family = db.query(Family).filter(Family.family_id == family_id).one_or_none()
    if not family:
        raise HTTPException(404, "家庭不存在")
    messages = db.query(RawMessage).filter(RawMessage.family_id == family_id).order_by(RawMessage.message_time).all()
    profile = db.query(ParentProfile).filter(ParentProfile.family_id == family_id).one_or_none()
    reports = db.query(WeeklyReport).filter(WeeklyReport.family_id == family_id).order_by(WeeklyReport.id.desc()).all()
    return {
        "family": as_dict(family),
        "messages": [as_dict(m) for m in messages],
        "profile": as_dict(profile) if profile else None,
        "reports": [as_dict(r) for r in reports],
    }


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


# AI 输出列表接口，支持按家庭和 Agent 类型筛选。
@app.get("/api/ai-outputs")
def list_ai_outputs(family_id: str = "", agent_type: str = "", db: Session = Depends(get_db)):
    query = db.query(AIOutput).order_by(AIOutput.id.desc())
    if family_id:
        query = query.filter(AIOutput.family_id == family_id)
    if agent_type:
        query = query.filter(AIOutput.agent_type == agent_type)
    return [as_dict(item) for item in query.limit(200).all()]


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
def create_task_from_ai_output(output_id: int, payload: AIOutputTaskIn | None = None, db: Session = Depends(get_db)):
    output = db.get(AIOutput, output_id)
    if not output:
        raise HTTPException(404, "AI输出不存在")
    family = db.query(Family).filter(Family.family_id == output.family_id).one_or_none()
    data = payload or AIOutputTaskIn()
    content = data.content.strip() or output.edited_output or output.display_text
    content = validate_send_task_content(content)
    target_name = data.target_name.strip() or (family.parent_nickname if family else output.family_id)
    scene = data.scene.strip() or output.source or output.agent_type
    device_id = validate_task_device_binding(db, data.device_id, target_name)
    task = SendTask(
        family_id=output.family_id,
        target_name=target_name,
        scene=scene,
        content=content,
        device_id=device_id,
        send_mode=validate_send_mode_submit(data.send_mode, data.confirm_real_send),
        status="pending",
    )
    output.status = "task_created"
    output.edited_output = content
    output.updated_at = datetime.utcnow()
    db.add(task)
    db.commit()
    return as_dict(task)


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
def sync_rpa_conversation(payload: RpaConversationIn, db: Session = Depends(get_db)):
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
            db.add(task)

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
def list_reports(db: Session = Depends(get_db)):
    return [as_dict(r) for r in db.query(WeeklyReport).order_by(WeeklyReport.id.desc()).all()]


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
def list_profiles(db: Session = Depends(get_db)):
    return [as_dict(p) for p in db.query(ParentProfile).order_by(ParentProfile.family_id).all()]


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
def create_tasks_from_reports(db: Session = Depends(get_db)):
    created = 0
    reports = db.query(WeeklyReport).filter(WeeklyReport.status == "approved").all()
    for report in reports:
        exists = db.query(SendTask).filter(SendTask.family_id == report.family_id, SendTask.scene == "周报发送").first()
        if exists:
            continue
        family = db.query(Family).filter(Family.family_id == report.family_id).first()
        db.add(
            SendTask(
                family_id=report.family_id,
                target_name=family.parent_nickname if family else report.family_id,
                scene="周报发送",
                content=validate_send_task_content(report.final_text),
                send_mode="dry_run",
            )
        )
        created += 1
    db.commit()
    return {"created": created}


# 按场景规则和话术模板创建发送任务。
@app.post("/api/send-tasks/from-scenes")
def create_tasks_from_scenes(db: Session = Depends(get_db)):
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
        db.add(SendTask(family_id=msg.family_id, target_name=family.parent_nickname if family else msg.family_id, scene=scene, content=validate_send_task_content(templates[scene]), send_mode="dry_run"))
        created += 1
    db.commit()
    return {"created": created}


# 发送任务列表接口（可选按 status / device_id 过滤，便于看板和调试）。
@app.get("/api/send-tasks")
def list_send_tasks(status: str = "", device_id: str = "", db: Session = Depends(get_db)):
    query = db.query(SendTask)
    if status:
        query = query.filter(SendTask.status == status)
    if device_id:
        query = query.filter(SendTask.device_id == device_id)
    return [as_dict(t) for t in query.order_by(SendTask.id.desc()).all()]


# 直接新增一条发送任务。
@app.post("/api/send-tasks")
def create_send_task(payload: SendTaskIn, db: Session = Depends(get_db)):
    data = payload.model_dump()
    data["content"] = validate_send_task_content(data.get("content", ""))
    data["send_mode"] = validate_send_mode_submit(data.get("send_mode", "dry_run"), bool(data.pop("confirm_real_send", False)))
    data["device_id"] = validate_task_device_binding(db, data.get("device_id", ""), data.get("target_name", ""))
    task = SendTask(**data)
    db.add(task)
    db.commit()
    return as_dict(task)


# 更新发送任务的内容和状态。
@app.put("/api/send-tasks/{task_id}")
def update_send_task(task_id: int, payload: SendTaskUpdate, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if payload.status not in {"pending", "approved", "assigned", "cancelled", "sent", "failed", "dry_run"}:
        raise HTTPException(400, "status 不合法")
    target_name = payload.target_name or task.target_name
    device_id = task.device_id if payload.device_id is None else payload.device_id
    task.target_name = target_name
    task.scene = payload.scene or task.scene
    if payload.content:
        task.content = validate_send_task_content(payload.content)
    if payload.send_mode:
        task.send_mode = validate_send_mode_submit(payload.send_mode, payload.confirm_real_send, task.send_mode)
    task.device_id = validate_task_device_binding(db, device_id or "", target_name)
    task.status = payload.status
    db.commit()
    return as_dict(task)


@app.post("/api/send-tasks/{task_id}/cancel")
def cancel_send_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    task.status = "cancelled"
    db.commit()
    return as_dict(task)


@app.post("/api/send-tasks/{task_id}/result")
def record_send_result(task_id: int, payload: SendResultIn, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if payload.status not in {"sent", "failed", "skipped", "dry_run"}:
        raise HTTPException(400, "status 只能是 sent/failed/skipped/dry_run")
    task.status = payload.status
    log = SendLog(
        task_id=task.id,
        family_id=task.family_id,
        target_name=task.target_name,
        status=payload.status,
        detail=payload.detail,
        device_id=payload.device_id or task.device_id,
    )
    db.add(log)
    db.commit()
    return as_dict(log)


def send_task_to_web_chat(db: Session, task: SendTask) -> tuple[RawMessage, SendLog]:
    family = db.query(Family).filter(Family.family_id == task.family_id).first()
    if not family:
        raise HTTPException(404, "任务对应家庭不存在，无法发送到网页通讯")
    if task.status != "pending":
        raise HTTPException(400, "只有 pending 状态的任务可以发送")
    task.content = validate_send_task_content(task.content)

    message = RawMessage(
        family_id=task.family_id,
        speaker=family.coach_name or "陪跑师",
        content=task.content,
        source="网页通讯发送任务",
        checkin_status=detect_checkin(task.content),
        is_effective="Y",
    )
    task.status = "sent"
    log = SendLog(
        task_id=task.id,
        family_id=task.family_id,
        target_name=task.target_name,
        status="sent",
        detail="WEB_CHAT: 已发送到网页通讯会话。",
    )
    db.add(message)
    db.add(log)
    return message, log


@app.post("/api/send-tasks/{task_id}/web-send")
def web_send(task_id: int, db: Session = Depends(get_db)):
    task = db.get(SendTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    message, log = send_task_to_web_chat(db, task)
    db.commit()
    return {"task": as_dict(task), "message": as_dict(message), "log": as_dict(log)}


@app.post("/api/send-tasks/web-send-all")
def web_send_all(db: Session = Depends(get_db)):
    sent = 0
    skipped = 0
    for task in db.query(SendTask).filter(SendTask.status == "pending").all():
        try:
            send_task_to_web_chat(db, task)
            sent += 1
        except HTTPException:
            skipped += 1
    db.commit()
    return {"sent": sent, "skipped": skipped}


@app.get("/api/send-logs")
def list_send_logs(db: Session = Depends(get_db)):
    return [as_dict(l) for l in db.query(SendLog).order_by(SendLog.id.desc()).all()]


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
        "dry_run": True,                 # 默认安全：只粘贴不发，对方验证无误后改 false
        "auto_launch_wecom": False,
    })

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 复制项目文件，保持相对结构让 import 正常（wecom_sender 会把 main/ 加入 sys.path 再 import app.services）
        zf.write(ROOT / "rpa" / "wecom_sender.py", "rpa/wecom_sender.py")
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
        .order_by(SendTask.id)
        .limit(limit)
        .all()
    )
    claimed = []
    for task in candidates:
        try:
            task.content = validate_send_task_content(task.content)
            task.send_mode = validate_send_mode(task.send_mode)
        except HTTPException as exc:
            task.status = "failed"
            db.add(
                SendLog(
                    task_id=task.id,
                    family_id=task.family_id,
                    target_name=task.target_name,
                    status="failed",
                    detail=f"CONTENT_VALIDATION: {exc.detail}",
                    device_id=dev.device_id,
                )
            )
            continue
        # 原子领取：只有仍是 pending 才领得到，避免两台设备抢到同一条。
        # 同时把 scheduled_at 刷成领取时刻，作为超时回收的判断依据。
        updated = (
            db.query(SendTask)
            .filter(SendTask.id == task.id, SendTask.status == "pending")
            .update({"status": "assigned", "device_id": dev.device_id, "scheduled_at": datetime.utcnow()})
        )
        if updated == 1:
            claimed.append(task)
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
