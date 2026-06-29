"""发送日志原因分类。

只解析已回写的 detail 文本，不改动发送任务状态，便于前端做可视化展示。
"""

from __future__ import annotations

TRACE_PREFIX = "RPA_TRACE:"


def parse_send_trace(detail: str) -> list[str]:
    text = detail or ""
    traces: list[str] = []
    for line in text.splitlines():
        if TRACE_PREFIX not in line:
            continue
        _, raw_trace = line.split(TRACE_PREFIX, 1)
        for item in raw_trace.replace(";", "；").split("；"):
            clean = item.strip()
            if clean and clean not in traces:
                traces.append(clean)
    return traces


def classify_send_log(status: str, detail: str) -> dict:
    clean_status = (status or "").strip()
    clean_detail = detail or ""
    trace = parse_send_trace(clean_detail)
    stage = "发送结果"
    reason = clean_status or "unknown"
    label = "未分类"
    level = "warn" if clean_status == "failed" else ""

    rules = [
        ("发送前校验失败", "标题校验", "title_mismatch", "标题不匹配", "danger"),
        ("会话校验失败", "标题校验", "title_mismatch", "标题不匹配", "danger"),
        ("搜索结果未命中", "搜索定位", "search_not_found", "搜索无命中", "danger"),
        ("无法在会话列表定位", "会话定位", "conversation_not_found", "会话列表无命中", "danger"),
        ("INPUT_FOCUS", "输入框", "input_focus_failed", "输入框失败", "danger"),
        ("输入框定位失败", "输入框", "input_focus_failed", "输入框失败", "danger"),
        ("当前前台窗口不是企业微信", "前台窗口", "foreground_not_wecom", "焦点不在企微", "danger"),
        ("无法确认当前前台窗口", "前台窗口", "foreground_unknown", "无法确认焦点", "danger"),
        ("REAL_SEND_GUARD", "发送闸门", "real_send_guard", "真实发送硬开关", "warn"),
        ("不在白名单", "发送闸门", "target_not_allowed", "白名单拦截", "warn"),
        ("SEND_GUARD", "发送闸门", "send_guard", "安全闸门拦截", "danger"),
        ("DRY_RUN", "试运行", "dry_run_done", "试运行完成", "ok"),
        ("REAL_RPA", "真实发送", "real_send_done", "真实发送完成", "ok"),
        ("WEB_CHAT", "网页发送", "web_chat_done", "网页发送完成", "ok"),
    ]
    for token, matched_stage, matched_reason, matched_label, matched_level in rules:
        if token in clean_detail:
            stage = matched_stage
            reason = matched_reason
            label = matched_label
            level = matched_level
            break

    if label == "未分类":
        if clean_status == "sent":
            stage, reason, label, level = "真实发送", "sent", "真实发送完成", "ok"
        elif clean_status == "dry_run":
            stage, reason, label, level = "试运行", "dry_run_done", "试运行完成", "ok"
        elif clean_status == "skipped":
            stage, reason, label, level = "发送闸门", "skipped", "已跳过", "warn"
        elif clean_status == "failed":
            stage, reason, label, level = "发送失败", "failed_unknown", "待复核失败", "danger"

    return {
        "send_stage": stage,
        "send_reason": reason,
        "send_reason_label": label,
        "send_reason_level": level,
        "send_trace": trace,
        "send_trace_text": " / ".join(trace),
    }
