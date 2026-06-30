"""本地 mock AI 生成器。

在没有接通 Ark 或其他模型服务时，用规则和文本模板先跑通业务链路。
"""

from collections import Counter
from datetime import datetime

from app.models import RawMessage
from app.services.scenario import detect_pain_points


# 取最近几条消息当作证据摘要，供周报和画像展示。
def _recent_evidence(messages: list[RawMessage], limit: int = 3) -> str:
    rows = []
    for msg in messages[-limit:]:
        when = msg.message_time.strftime("%m-%d %H:%M")
        rows.append(f"{when} {msg.speaker}: {msg.content[:80]}")
    return "\n".join(rows)


# 生成周报的本地兜底版本，不依赖外部模型。
def generate_weekly_report(family_id: str, messages: list[RawMessage]) -> dict:
    contents = [m.content for m in messages]
    pain_points = "、".join(detect_pain_points(contents))
    checkins = Counter(m.checkin_status for m in messages if m.checkin_status)
    checkin_text = "，".join(f"{k}{v}次" for k, v in checkins.items()) or "暂无明确打卡记录"
    week_label = datetime.utcnow().strftime("%Y-W%U")
    final_text = (
        f"本周孩子整体处于可跟进状态，主要问题集中在{pain_points}。\n"
        f"打卡情况：{checkin_text}。\n"
        "建议家长先关注一个最小行动目标，陪跑师下周重点跟进完成率和情绪反馈。"
    )
    return {
        "family_id": family_id,
        "week_label": week_label,
        "overall_state": f"本周有效沟通 {len(messages)} 条，状态需要持续陪跑。",
        "main_changes": f"高频信号集中在：{pain_points}。",
        "parent_focus": "减少一次性要求，先抓最稳定的一个学习动作。",
        "teacher_suggestion": "用固定时间点提醒 + 当天反馈闭环，降低家长焦虑。",
        "next_followup": "下周观察打卡连续性、作业启动速度和家长反馈频率。",
        "final_text": final_text,
    }


# 生成家长画像的本地兜底版本。
def generate_parent_profile(family_id: str, messages: list[RawMessage]) -> dict:
    contents = [m.content for m in messages]
    pain_points = detect_pain_points(contents)
    parent_msgs = [m for m in messages if "家长" in m.speaker or "妈妈" in m.speaker or "爸爸" in m.speaker]
    risk = "退费风险/投诉风险需人工关注" if any("退费" in m.content or "投诉" in m.content for m in messages) else "暂无明显高风险"
    satisfaction_level = "低" if risk != "暂无明显高风险" else "中高"
    renewal_intent = "明确关注" if any(word in m.content for m in messages for word in ("续报", "续费", "下一阶段", "继续学")) else "可培育"
    high_freq = len(parent_msgs) >= 5
    communication_style = "高频追问型" if high_freq else "理性规划型"
    trust_level = "B" if len(messages) >= 8 and risk == "暂无明显高风险" else "C"
    return {
        "family_id": family_id,
        "trust_level": trust_level,
        "trust_trend": "稳定",
        "pain_points": "、".join(pain_points),
        "communication_style": communication_style,
        "satisfaction_level": satisfaction_level,
        "child_summary": "学习动作已经出现，但稳定性仍需陪跑师用低压力方式持续推动。",
        "service_risks": risk,
        "renewal_intent": renewal_intent,
        "evidence": _recent_evidence(messages),
        "suggested_actions": "本周建议陪跑师用一句肯定 + 一个具体动作 + 一个复盘时间点沟通。",
    }
