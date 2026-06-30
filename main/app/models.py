"""数据库模型定义。

这些 ORM 类对应家庭、消息、画像、周报、发送任务和日志表。
"""

from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(60), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    actor: Mapped[str] = mapped_column(String(120), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    before_json: Mapped[str] = mapped_column(Text, default="")
    after_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 网页测试版账号表：用于模拟陪跑师和家长登录聊天。
class UserAccount(Base):
    __tablename__ = "user_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password: Mapped[str] = mapped_column(String(120), default="")
    display_name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(30), default="parent")
    campus_names: Mapped[str] = mapped_column(Text, default="")
    family_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 家庭主表，保存家长、学员阶段和陪跑师等基础信息。
class Family(Base):
    __tablename__ = "families"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    parent_nickname: Mapped[str] = mapped_column(String(120), default="")
    child_grade: Mapped[str] = mapped_column(String(80), default="")
    course_stage: Mapped[str] = mapped_column(String(120), default="")
    unit_progress: Mapped[str] = mapped_column(String(120), default="")
    pbl_count: Mapped[int] = mapped_column(Integer, default=0)
    checkin_rate: Mapped[str] = mapped_column(String(40), default="")
    next_milestone: Mapped[str] = mapped_column(Text, default="")
    campus_name: Mapped[str] = mapped_column(String(80), default="", index=True)
    coach_name: Mapped[str] = mapped_column(String(80), default="")
    service_status: Mapped[str] = mapped_column(String(40), default="试点中")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    messages = relationship("RawMessage", back_populates="family")


# 原始消息表，导入的聊天记录都先落这里。
class RawMessage(Base):
    __tablename__ = "raw_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[str] = mapped_column(String(64), ForeignKey("families.family_id"), index=True)
    message_time: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    speaker: Mapped[str] = mapped_column(String(80), default="")
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(80), default="导入")
    checkin_status: Mapped[str] = mapped_column(String(40), default="")
    is_effective: Mapped[str] = mapped_column(String(10), default="Y")

    family = relationship("Family", back_populates="messages")


# 打卡识别结果表，记录每条消息对应的打卡证据。
class CheckinRecord(Base):
    __tablename__ = "checkin_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)
    message_id: Mapped[int] = mapped_column(Integer, default=0)
    checkin_type: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 跟进记录表，记录电话、私信、群提醒、补课、投诉和续报沟通等人工服务动作。
class FollowupRecord(Base):
    __tablename__ = "followup_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)
    followup_type: Mapped[str] = mapped_column(String(40), default="私信")
    content: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(30), default="待跟进")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 周报表，保存 Agent 生成的结构化周报和最终文本。
class WeeklyReport(Base):
    __tablename__ = "weekly_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)
    week_label: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(30), default="draft")
    overall_state: Mapped[str] = mapped_column(Text, default="")
    main_changes: Mapped[str] = mapped_column(Text, default="")
    parent_focus: Mapped[str] = mapped_column(Text, default="")
    teacher_suggestion: Mapped[str] = mapped_column(Text, default="")
    next_followup: Mapped[str] = mapped_column(Text, default="")
    final_text: Mapped[str] = mapped_column(Text, default="")
    send_task_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    send_status: Mapped[str] = mapped_column(String(30), default="not_created", index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=None)
    parent_ack_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=None)
    parent_ack_note: Mapped[str] = mapped_column(Text, default="")
    parent_feedback_score: Mapped[int] = mapped_column(Integer, default=0)
    parent_feedback_note: Mapped[str] = mapped_column(Text, default="")
    parent_feedback_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 家长画像表，保存沟通风格、风险和建议动作。
class ParentProfile(Base):
    __tablename__ = "parent_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    trust_level: Mapped[str] = mapped_column(String(10), default="C")
    trust_trend: Mapped[str] = mapped_column(String(20), default="稳定")
    pain_points: Mapped[str] = mapped_column(Text, default="")
    communication_style: Mapped[str] = mapped_column(String(80), default="")
    satisfaction_level: Mapped[str] = mapped_column(String(20), default="未知")
    child_summary: Mapped[str] = mapped_column(Text, default="")
    service_risks: Mapped[str] = mapped_column(Text, default="")
    renewal_intent: Mapped[str] = mapped_column(String(40), default="未知")
    evidence: Mapped[str] = mapped_column(Text, default="")
    suggested_actions: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 话术模板表，给回复 Agent 和发送任务提供固定模板。
class Template(Base):
    __tablename__ = "template_library"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    scene: Mapped[str] = mapped_column(String(80), index=True)
    content: Mapped[str] = mapped_column(Text)
    send_time: Mapped[str] = mapped_column(String(20), default="")
    enabled: Mapped[str] = mapped_column(String(10), default="Y")


# AI 输出表，保存原始 JSON、展示文本和人工审核后的内容。
class AIOutput(Base):
    __tablename__ = "ai_outputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_type: Mapped[str] = mapped_column(String(60), index=True)
    source: Mapped[str] = mapped_column(String(120), default="")
    raw_json: Mapped[str] = mapped_column(Text, default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="")
    display_text: Mapped[str] = mapped_column(Text, default="")
    edited_output: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="needs_review")
    risk_level: Mapped[str] = mapped_column(String(40), default="低")
    need_human_review: Mapped[str] = mapped_column(String(10), default="Y")
    suggested_actions: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 待发送任务表，是后端与 RPA 发送器之间的中转队列。
class SendTask(Base):
    __tablename__ = "send_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)
    target_name: Mapped[str] = mapped_column(String(120), default="")
    scene: Mapped[str] = mapped_column(String(80), default="")
    content: Mapped[str] = mapped_column(Text)
    send_mode: Mapped[str] = mapped_column(String(20), default="dry_run")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    device_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    next_retry_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=None)
    last_error: Mapped[str] = mapped_column(Text, default="")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 发送日志表，记录任务最终状态和失败原因。
class SendLog(Base):
    __tablename__ = "send_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)
    target_name: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(30))
    send_mode: Mapped[str] = mapped_column(String(20), default="dry_run")
    device_id: Mapped[str] = mapped_column(String(64), default="")
    screenshot_path: Mapped[str] = mapped_column(Text, default="")
    verify_status: Mapped[str] = mapped_column(String(30), default="")
    verify_detail: Mapped[str] = mapped_column(Text, default="")
    verified_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=None)
    detail: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 被控端设备表：每台跑 RPA 的电脑一条，记录鉴权 token、负责的会话、心跳与健康状态。
class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    token: Mapped[str] = mapped_column(String(80), default="")
    conversations: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(20), default="offline")
    wecom_ok: Mapped[str] = mapped_column(String(10), default="")
    allow_real_send: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_any_conversation: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime, nullable=True, default=None)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 导入去重表，防止同一来源文件被重复导入。
class ProcessedImport(Base):
    __tablename__ = "processed_imports"
    __table_args__ = (UniqueConstraint("source_name", name="uq_source_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_name: Mapped[str] = mapped_column(String(200))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
