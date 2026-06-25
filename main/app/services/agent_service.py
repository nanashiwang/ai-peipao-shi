"""Agent 编排层。

这个模块负责：收集上下文、调用 Ark 或本地规则、把结果整理成后端可保存的结构。
"""

import json
from collections import Counter
from datetime import datetime

from app.models import Family, ParentProfile, RawMessage, SendLog, Template, WeeklyReport
from app.services.ark_client import ArkNotConfigured, call_ark_json
from app.services.scenario import detect_pain_points, detect_scene


AGENT_LABELS = {
    "family_profile": "家庭画像 Agent",
    "weekly_report": "AI周报 Agent",
    "ai_reply": "AI回复 Agent",
    "checkin_pbl": "打卡/PBL Agent",
}


# 把结构化数据转成格式化 JSON 字符串，便于调试和落库。
def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


# 把列表拼成中文顿号分隔文本，适合展示给运营/老师看。
def _lines(items: list[str]) -> str:
    return "、".join(items) if items else "暂无"


# 提取最近几条消息，作为上下文摘要。
def _recent_messages(messages: list[RawMessage], limit: int = 8) -> list[RawMessage]:
    return sorted(messages, key=lambda item: item.message_time)[-limit:]


# 把消息摘要成可展示的一行行证据。
def _message_summary(messages: list[RawMessage], limit: int = 4) -> list[str]:
    rows = []
    for msg in _recent_messages(messages, limit):
        when = msg.message_time.strftime("%m-%d %H:%M")
        rows.append(f"{when} {msg.speaker}: {msg.content[:70]}")
    return rows


# 家庭标题优先用家长昵称，回退到 family_id。
def _family_title(family: Family | None, family_id: str) -> str:
    if not family:
        return family_id
    return family.parent_nickname or family.family_id


# 在模板里按场景找最匹配的启用模板。
def _find_template(templates: list[Template], scene: str) -> Template | None:
    enabled = [item for item in templates if item.enabled == "Y"]
    for item in enabled:
        if item.scene == scene:
            return item
    for item in enabled:
        if scene and (scene in item.scene or item.scene in scene):
            return item
    return enabled[0] if enabled else None


# 根据高风险关键词给出粗略风险等级。
def _risk_level(messages: list[RawMessage]) -> tuple[str, bool, list[str]]:
    risk_words = ["退费", "投诉", "不满意", "没效果", "价格", "承诺", "情绪崩"]
    signals = []
    for msg in messages:
        for word in risk_words:
            if word in msg.content and word not in signals:
                signals.append(word)
    if any(word in signals for word in ["退费", "投诉", "不满意", "没效果"]):
        return "高", True, [f"出现{word}表达" for word in signals]
    if signals:
        return "中", True, [f"出现{word}相关表达" for word in signals]
    return "低", False, ["暂无明显高风险"]


# 兼容 Ark 返回的单值或列表字段。
def _safe_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


# 把字符串/布尔值统一成布尔判断，适配模型返回的各种写法。
def _bool_value(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y", "是", "需要"}


# 拼装给 Ark 的完整上下文，包含家庭、画像、消息、周报和日志。
def _context_payload(context: dict, extra: dict | None = None) -> dict:
    family = context["family"]
    profile = context["profile"]
    payload = {
        "family": {
            "family_id": family.family_id if family else "",
            "parent_nickname": family.parent_nickname if family else "",
            "child_grade": family.child_grade if family else "",
            "coach_name": family.coach_name if family else "",
            "service_status": family.service_status if family else "",
        },
        "profile": {
            "trust_level": profile.trust_level,
            "pain_points": profile.pain_points,
            "communication_style": profile.communication_style,
            "child_summary": profile.child_summary,
            "service_risks": profile.service_risks,
            "suggested_actions": profile.suggested_actions,
        } if profile else None,
        "recent_messages": [
            {
                "time": msg.message_time.isoformat(),
                "speaker": msg.speaker,
                "content": msg.content,
                "source": msg.source,
                "checkin_status": msg.checkin_status,
            }
            for msg in _recent_messages(context["messages"], 20)
        ],
        "recent_reports": [
            {
                "week_label": report.week_label,
                "status": report.status,
                "final_text": report.final_text,
            }
            for report in context["reports"]
        ],
        "recent_send_logs": [
            {
                "status": log.status,
                "detail": log.detail,
                "sent_at": log.sent_at.isoformat(),
            }
            for log in context["logs"]
        ],
        "enabled_templates": [
            {
                "name": template.name,
                "scene": template.scene,
                "content": template.content,
                "send_time": template.send_time,
            }
            for template in context["templates"]
        ],
    }
    if extra:
        payload.update(extra)
    return payload


# 统一 Ark 返回格式，保证后续保存逻辑只处理同一种结构。
def _normalize_result(raw: dict, display_text: str, fallback_risk: str = "低", fallback_review: bool = True, actions: list[str] | None = None) -> dict:
    risk_level = str(raw.get("风险等级") or raw.get("risk_level") or fallback_risk)
    need_review = _bool_value(raw.get("是否需要人工介入", raw.get("need_human_review", fallback_review)))
    suggested_actions = _safe_list(raw.get("建议跟进动作") or raw.get("推荐下一步动作") or raw.get("suggested_actions") or actions)
    return {
        "raw": raw,
        "display_text": display_text,
        "risk_level": risk_level,
        "need_human_review": need_review,
        "suggested_actions": suggested_actions,
    }


# 调用 Ark；如果没有配置就返回 None，让上层走本地兜底逻辑。
def _call_ark_or_none(system_prompt: str, payload: dict) -> dict | None:
    try:
        return call_ark_json(system_prompt, payload)
    except ArkNotConfigured:
        return None
    except Exception as exc:
        return {"_ark_error": str(exc)}


# 读取家庭相关的完整上下文，给各类 Agent 复用。
def build_agent_context(db, family_id: str) -> dict:
    family = db.query(Family).filter(Family.family_id == family_id).one_or_none()
    messages = db.query(RawMessage).filter(RawMessage.family_id == family_id).order_by(RawMessage.message_time).all()
    profile = db.query(ParentProfile).filter(ParentProfile.family_id == family_id).one_or_none()
    reports = db.query(WeeklyReport).filter(WeeklyReport.family_id == family_id).order_by(WeeklyReport.id.desc()).limit(3).all()
    logs = db.query(SendLog).filter(SendLog.family_id == family_id).order_by(SendLog.id.desc()).limit(5).all()
    templates = db.query(Template).filter(Template.enabled == "Y").all()
    return {
        "family": family,
        "messages": messages,
        "profile": profile,
        "reports": reports,
        "logs": logs,
        "templates": templates,
    }


# 家庭画像 Agent：先尝试 Ark，失败则使用规则生成画像。
def run_family_profile_agent(context: dict) -> dict:
    family = context["family"]
    messages = context["messages"]
    family_id = family.family_id if family else ""
    ark_raw = _call_ark_or_none(
        "你是教育机构陪跑师效率系统的家庭画像Agent。只输出JSON，不要Markdown。字段必须包含：agent,family_id,家长关注点,沟通风格,满意度评级,风险等级,风险信号,学生状态,推荐沟通策略,建议跟进动作,是否需要人工介入,使用依据摘要。",
        _context_payload(context, {"agent": "family_profile_agent"}),
    )
    if ark_raw and "_ark_error" not in ark_raw:
        display = (
            f"{_family_title(family, family_id)}｜画像\n"
            f"关注点：{_lines(_safe_list(ark_raw.get('家长关注点')))}\n"
            f"沟通风格：{ark_raw.get('沟通风格', '')}；满意度：{ark_raw.get('满意度评级', '')}；风险：{ark_raw.get('风险等级', '')}\n"
            f"学生状态：{ark_raw.get('学生状态', '')}\n"
            f"建议策略：{ark_raw.get('推荐沟通策略', '')}\n"
            f"建议动作：{_lines(_safe_list(ark_raw.get('建议跟进动作')))}"
        )
        return _normalize_result(ark_raw, display, actions=_safe_list(ark_raw.get("建议跟进动作")))

    pain_points = detect_pain_points([msg.content for msg in messages])
    risk_level, need_human, risk_signals = _risk_level(messages)
    checkins = Counter(msg.checkin_status for msg in messages if msg.checkin_status)
    completion_text = "，".join(f"{name}{count}次" for name, count in checkins.items()) or "暂无明确打卡记录"
    parent_messages = [msg for msg in messages if any(word in msg.speaker for word in ["家长", "妈妈", "爸爸"])]
    communication_style = "高频追问型" if len(parent_messages) >= 5 else "结果导向型"
    strategy = "先给具体数据和下一步安排，再做情绪安抚。" if communication_style == "结果导向型" else "先回应情绪，再拆成一个小行动。"
    data = {
        "agent": "family_profile_agent",
        "family_id": family_id,
        "家长关注点": pain_points,
        "沟通风格": communication_style,
        "满意度评级": "中" if risk_level != "高" else "低",
        "风险等级": risk_level,
        "风险信号": risk_signals,
        "学生状态": f"近期学习记录：{completion_text}。",
        "推荐沟通策略": strategy,
        "建议跟进动作": ["同步本周完成数据", "约定下次反馈时间", "必要时请主管介入"] if need_human else ["发送阶段反馈", "下周跟进打卡连续性"],
        "是否需要人工介入": need_human,
        "使用依据摘要": _message_summary(messages) + ["发送日志" if context["logs"] else "暂无发送日志"],
    }
    if ark_raw and "_ark_error" in ark_raw:
        data["豆包API调用失败"] = ark_raw["_ark_error"]
    display = (
        f"{_family_title(family, family_id)}｜画像\n"
        f"关注点：{_lines(data['家长关注点'])}\n"
        f"沟通风格：{data['沟通风格']}；满意度：{data['满意度评级']}；风险：{data['风险等级']}\n"
        f"学生状态：{data['学生状态']}\n"
        f"建议策略：{data['推荐沟通策略']}\n"
        f"建议动作：{_lines(data['建议跟进动作'])}"
    )
    return {"raw": data, "display_text": display, "risk_level": risk_level, "need_human_review": need_human, "suggested_actions": data["建议跟进动作"]}


# AI 周报 Agent：生成周报摘要、重点和下周建议。
def run_weekly_report_agent(context: dict) -> dict:
    family = context["family"]
    messages = context["messages"]
    profile = context["profile"]
    family_id = family.family_id if family else ""
    ark_raw = _call_ark_or_none(
        "你是教育机构陪跑师效率系统的AI周报Agent。只输出JSON，不要Markdown。字段必须包含：agent,family_id,period,本周学习总结,学习亮点,需要关注,下周建议,给家长的话,给孩子的话,风险提示,风险等级,是否需要人工介入,是否可加入发送任务,使用依据摘要。",
        _context_payload(context, {"agent": "weekly_report_agent"}),
    )
    if ark_raw and "_ark_error" not in ark_raw:
        display = (
            f"【{_family_title(family, family_id)}本周反馈】\n"
            f"{ark_raw.get('本周学习总结', '')}\n\n"
            f"学习亮点：{_lines(_safe_list(ark_raw.get('学习亮点')))}\n"
            f"需要关注：{_lines(_safe_list(ark_raw.get('需要关注')))}\n"
            f"下周建议：{_lines(_safe_list(ark_raw.get('下周建议')))}\n\n"
            f"给家长的话：{ark_raw.get('给家长的话', '')}\n"
            f"给孩子的话：{ark_raw.get('给孩子的话', '')}"
        )
        return _normalize_result(ark_raw, display, actions=["审核周报", "加入发送任务"])

    pain_points = detect_pain_points([msg.content for msg in messages])
    checkins = Counter(msg.checkin_status for msg in messages if msg.checkin_status)
    completed = sum(count for name, count in checkins.items() if "完成" in name and "未" not in name)
    total = sum(checkins.values()) or max(len(messages), 1)
    completion_rate = min(100, round(completed / total * 100)) if total else 0
    risk_level, need_human, risk_signals = _risk_level(messages)
    period = datetime.utcnow().strftime("%Y-W%U")
    parent_focus = profile.pain_points if profile and profile.pain_points else _lines(pain_points)
    data = {
        "agent": "weekly_report_agent",
        "family_id": family_id,
        "period": period,
        "本周学习总结": f"本周有效沟通 {len(messages)} 条，打卡完成率约 {completion_rate}%，整体仍需陪跑师持续跟进。",
        "学习亮点": ["能保持部分核心任务推进", "家长愿意反馈孩子状态"],
        "需要关注": pain_points,
        "下周建议": ["固定一个打卡时间点", "优先完成D1-D3核心任务", "PBL输出日提前提醒"],
        "给家长的话": f"本周建议重点关注{parent_focus}，我们会把任务拆小，先保证孩子能稳定完成。",
        "给孩子的话": "你已经完成了不少关键动作，下周继续每天进步一点点。",
        "风险提示": _lines(risk_signals),
        "是否可加入发送任务": True,
        "使用依据摘要": _message_summary(messages) + ["家庭画像" if profile else "暂无画像"],
    }
    if ark_raw and "_ark_error" in ark_raw:
        data["豆包API调用失败"] = ark_raw["_ark_error"]
    display = (
        f"【{_family_title(family, family_id)}本周反馈】\n"
        f"{data['本周学习总结']}\n\n"
        f"学习亮点：{_lines(data['学习亮点'])}\n"
        f"需要关注：{_lines(data['需要关注'])}\n"
        f"下周建议：{_lines(data['下周建议'])}\n\n"
        f"给家长的话：{data['给家长的话']}\n"
        f"给孩子的话：{data['给孩子的话']}"
    )
    return {"raw": data, "display_text": display, "risk_level": risk_level, "need_human_review": need_human, "suggested_actions": ["审核周报", "加入发送任务"]}


# AI 回复 Agent：根据最新消息和话术模板生成可审核回复。
def run_reply_agent_service(context: dict, message: str = "", tone: str = "standard") -> dict:
    family = context["family"]
    messages = context["messages"]
    profile = context["profile"]
    templates = context["templates"]
    family_id = family.family_id if family else ""
    latest = message.strip() or (messages[-1].content if messages else "")
    ark_raw = _call_ark_or_none(
        "你是教育机构陪跑师效率系统的AI回复Agent。先识别场景和风险，再结合话术模板与家庭画像生成可审核回复。只输出JSON，不要Markdown。字段必须包含：agent,family_id,最新消息摘要,场景类型,风险等级,是否建议人工介入,调用模板,推荐回复,推荐下一步动作,是否生成待办,是否可加入发送任务,使用依据摘要。",
        _context_payload(context, {"agent": "ai_reply_agent", "latest_message": latest, "tone": tone}),
    )
    if ark_raw and "_ark_error" not in ark_raw:
        return _normalize_result(
            ark_raw,
            str(ark_raw.get("推荐回复", "")),
            fallback_review=True,
            actions=_safe_list(ark_raw.get("推荐下一步动作")),
        )

    scene = detect_scene(latest) or "普通咨询"
    template = _find_template(templates, scene)
    risk_level, need_human, risk_signals = _risk_level(messages + ([type("Msg", (), {"content": latest})] if latest else []))
    base = template.content if template else "收到，我先帮您记录一下，会结合孩子本周情况继续跟进。"
    parent_name = family.parent_nickname if family else "家长"
    reply = base.replace("{家长称呼}", parent_name).replace("{学生姓名}", family.child_grade if family else "")
    if "请假" in scene or "延期" in scene or "补打卡" in scene:
        reply = f"{parent_name}您好，收到。今天如果时间紧，可以先完成最核心的一项，我们这边会帮您记录，明天再提醒孩子衔接上。"
    elif scene == "转人工":
        reply = f"{parent_name}您好，您的反馈我已经收到，这类情况我会先和主管/老师确认后再给您明确回复。"
    elif tone == "gentle":
        reply = f"{parent_name}您好，先别着急。{reply}"
    elif tone == "short":
        reply = f"{parent_name}您好，收到，我会记录并跟进，稍后给您具体安排。"
    data = {
        "agent": "ai_reply_agent",
        "family_id": family_id,
        "最新消息摘要": latest[:120],
        "场景类型": scene,
        "风险等级": risk_level,
        "是否建议人工介入": need_human,
        "调用模板": template.name if template else "无匹配模板",
        "推荐回复": reply,
        "推荐下一步动作": ["记录跟进", "加入发送任务"] + (["主管介入"] if need_human else []),
        "是否生成待办": True,
        "是否可加入发送任务": not need_human or risk_level != "高",
        "使用依据摘要": _message_summary(messages) + ([f"家庭画像：{profile.communication_style}"] if profile else []),
    }
    if ark_raw and "_ark_error" in ark_raw:
        data["豆包API调用失败"] = ark_raw["_ark_error"]
    return {"raw": data, "display_text": reply, "risk_level": risk_level, "need_human_review": True, "suggested_actions": data["推荐下一步动作"]}


def run_quick_reply_agent_service(context: dict, message: str = "", tone: str = "standard") -> dict:
    family = context["family"]
    messages = context["messages"]
    templates = context["templates"]
    family_id = family.family_id if family else ""
    latest = message.strip() or (messages[-1].content if messages else "")
    scene = detect_scene(latest) or "普通咨询"
    template = _find_template(templates, scene)
    risk_level, need_human, risk_signals = _risk_level(messages + ([type("Msg", (), {"content": latest})] if latest else []))
    recent_rows = [
        {"speaker": msg.speaker, "content": msg.content[:120]}
        for msg in _recent_messages(messages, 8)
    ]
    ark_raw = _call_ark_or_none(
        "你是陪跑师的快速回复助手。只输出JSON，不要Markdown。字段：agent,family_id,场景类型,风险等级,推荐回复,推荐下一步动作,是否建议人工介入。回复要可直接发给家长，80-180字，先共情再给明确下一步。",
        {
            "agent": "quick_reply_agent",
            "family": {
                "family_id": family_id,
                "parent_nickname": family.parent_nickname if family else "",
                "child_grade": family.child_grade if family else "",
                "coach_name": family.coach_name if family else "",
            },
            "latest_parent_message": latest,
            "tone": tone,
            "detected_scene": scene,
            "template": {
                "scene": template.scene,
                "content": template.content[:220],
            } if template else None,
            "recent_messages": recent_rows,
        },
    )
    if ark_raw and "_ark_error" not in ark_raw:
        return _normalize_result(
            ark_raw,
            str(ark_raw.get("推荐回复", "")),
            fallback_risk=risk_level,
            fallback_review=True,
            actions=_safe_list(ark_raw.get("推荐下一步动作")),
        )

    parent_name = family.parent_nickname if family else "家长"
    base = template.content if template else "收到，我先帮您记录一下，会结合孩子本周情况继续跟进。"
    reply = base.replace("{家长称呼}", parent_name).replace("{学生姓名}", family.child_grade if family else "")
    if tone == "short":
        reply = f"{parent_name}您好，收到，我会记录并跟进，稍后给您具体安排。"
    elif tone == "gentle":
        reply = f"{parent_name}您好，先别着急。{reply}"
    data = {
        "agent": "quick_reply_agent",
        "family_id": family_id,
        "场景类型": scene,
        "风险等级": risk_level,
        "风险信号": risk_signals,
        "推荐回复": reply,
        "推荐下一步动作": ["审核后发送", "记录本次跟进"] + (["主管介入"] if need_human else []),
        "是否建议人工介入": need_human,
    }
    if ark_raw and "_ark_error" in ark_raw:
        data["豆包API调用失败"] = ark_raw["_ark_error"]
    return {"raw": data, "display_text": reply, "risk_level": risk_level, "need_human_review": True, "suggested_actions": data["推荐下一步动作"]}


# 打卡/PBL Agent：识别完成率、未完成项和提醒话术。
def run_checkin_pbl_agent(context: dict) -> dict:
    family = context["family"]
    messages = context["messages"]
    family_id = family.family_id if family else ""
    ark_raw = _call_ark_or_none(
        "你是教育机构陪跑师效率系统的打卡/PBL Agent。识别完成率、未完成项、PBL点评和提醒话术。只输出JSON，不要Markdown。字段必须包含：agent,family_id,student_name,week,完成率,每日状态,状态标签,未完成项,PBL点评,提醒话术,风险等级,是否需要人工介入,是否加入发送任务,是否标记优秀作品,使用依据摘要。",
        _context_payload(context, {"agent": "checkin_pbl_agent"}),
    )
    if ark_raw and "_ark_error" not in ark_raw:
        comment = ark_raw.get("PBL点评") or {}
        display = (
            f"完成率：{ark_raw.get('完成率', '')}%｜状态：{ark_raw.get('状态标签', '')}\n"
            f"未完成项：{_lines(_safe_list(ark_raw.get('未完成项')))}\n"
            f"PBL总体评价：{comment.get('总体评价', '') if isinstance(comment, dict) else comment}\n"
            f"提醒话术：{ark_raw.get('提醒话术', '')}"
        )
        return _normalize_result(ark_raw, display, actions=["保存到档案", "生成提醒任务"])

    checkins = Counter(msg.checkin_status for msg in messages if msg.checkin_status)
    completed = sum(count for name, count in checkins.items() if "完成" in name and "未" not in name)
    unfinished = [name for name in checkins if "未完成" in name or "请假" in name]
    total = sum(checkins.values()) or 7
    completion_rate = min(100, round(completed / total * 100))
    status = "掉队" if completion_rate < 70 or unfinished else "稳定"
    data = {
        "agent": "checkin_pbl_agent",
        "family_id": family_id,
        "student_name": family.child_grade if family else "",
        "week": datetime.utcnow().strftime("W%U"),
        "完成率": completion_rate,
        "每日状态": {"D1": "完成" if completed else "待确认", "D2": "完成", "D3": "待确认", "D4": "待确认", "D5_PBL": "待点评", "D6": "待确认", "D7": "休息"},
        "状态标签": status,
        "未完成项": unfinished or ["暂无明确未完成项"],
        "PBL点评": {
            "总体评价": "整体能完成基础表达，结构还可以继续加强。",
            "亮点": ["愿意提交作品", "能表达核心观点"],
            "不足": ["结论深度不足", "细节例子偏少"],
            "改进建议": ["补充一个具体例子", "结尾多说一句自己的发现"],
        },
        "提醒话术": f"{_family_title(family, family_id)}您好，本周还有部分学习内容需要确认，建议今晚先补最核心的一项，保持节奏不断。",
        "是否加入发送任务": status == "掉队",
        "是否标记优秀作品": False,
        "使用依据摘要": _message_summary(messages) + ["打卡状态字段"],
    }
    if ark_raw and "_ark_error" in ark_raw:
        data["豆包API调用失败"] = ark_raw["_ark_error"]
    display = (
        f"完成率：{completion_rate}%｜状态：{status}\n"
        f"未完成项：{_lines(data['未完成项'])}\n"
        f"PBL总体评价：{data['PBL点评']['总体评价']}\n"
        f"亮点：{_lines(data['PBL点评']['亮点'])}\n"
        f"不足：{_lines(data['PBL点评']['不足'])}\n"
        f"提醒话术：{data['提醒话术']}"
    )
    return {"raw": data, "display_text": display, "risk_level": "中" if status == "掉队" else "低", "need_human_review": True, "suggested_actions": ["保存到档案", "生成提醒任务"] if status == "掉队" else ["保存到档案"]}
