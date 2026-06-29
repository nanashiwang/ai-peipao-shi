"""日志和截图证据保留策略。

默认只生成清理计划；真正删除必须由调用方显式确认。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import SendLog

SCREENSHOT_RE = re.compile(r"task_\d+_\d{8}_\d{6}_\d{6}\.(png|jpg)")
ROTATED_LOG_PATTERNS = ("server*.log.*", "server*.err.log.*", "*.log.*")


def _env_int(env: dict, key: str, default: int) -> int:
    try:
        value = int(str(env.get(key, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(value, 1)


def retention_policy_from_env(env: dict | None = None) -> dict:
    source = env or os.environ
    return {
        "send_log_days": _env_int(source, "SEND_LOG_RETENTION_DAYS", 365),
        "screenshot_days": _env_int(source, "SEND_SCREENSHOT_RETENTION_DAYS", 90),
        "runtime_log_days": _env_int(source, "RUNTIME_LOG_RETENTION_DAYS", 30),
    }


def _safe_child(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _file_info(path: Path, base: Path) -> dict:
    stat = path.stat()
    return {
        "filename": path.name,
        "path": str(path.relative_to(base)),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds"),
    }


def _expired_files(base: Path, pattern: str, cutoff: datetime, name_filter=None) -> list[Path]:
    if not base.exists():
        return []
    candidates = []
    for path in base.glob(pattern):
        if not path.is_file() or not _safe_child(path, base):
            continue
        if name_filter and not name_filter(path.name):
            continue
        if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
            candidates.append(path)
    return sorted(candidates, key=lambda item: item.stat().st_mtime)


def screenshot_retention_plan(screenshot_dir: Path, now: datetime, days: int) -> dict:
    cutoff = now - timedelta(days=days)
    expired = _expired_files(screenshot_dir, "task_*", cutoff, lambda name: bool(SCREENSHOT_RE.fullmatch(name)))
    return {
        "retention_days": days,
        "cutoff": cutoff.isoformat(sep=" ", timespec="seconds"),
        "expired_count": len(expired),
        "expired_bytes": sum(path.stat().st_size for path in expired),
        "candidates": [_file_info(path, screenshot_dir) for path in expired[:50]],
    }


def runtime_log_retention_plan(root: Path, now: datetime, days: int) -> dict:
    cutoff = now - timedelta(days=days)
    expired: list[Path] = []
    for pattern in ROTATED_LOG_PATTERNS:
        expired.extend(_expired_files(root, pattern, cutoff))
    unique = sorted({path.resolve(): path for path in expired}.values(), key=lambda item: item.stat().st_mtime)
    return {
        "retention_days": days,
        "cutoff": cutoff.isoformat(sep=" ", timespec="seconds"),
        "expired_count": len(unique),
        "expired_bytes": sum(path.stat().st_size for path in unique),
        "candidates": [_file_info(path, root) for path in unique[:50]],
    }


def send_log_retention_plan(db: Session, now: datetime, days: int) -> dict:
    cutoff = now - timedelta(days=days)
    expired_count = db.query(SendLog).filter(SendLog.sent_at < cutoff).count()
    oldest = db.query(func.min(SendLog.sent_at)).scalar()
    return {
        "retention_days": days,
        "cutoff": cutoff.isoformat(sep=" ", timespec="seconds"),
        "expired_count": expired_count,
        "oldest_sent_at": oldest.isoformat(sep=" ", timespec="seconds") if oldest else "",
    }


def retention_report(db: Session, screenshot_dir: Path, root: Path, policy: dict, now: datetime | None = None) -> dict:
    current = now or datetime.utcnow()
    send_logs = send_log_retention_plan(db, current, int(policy["send_log_days"]))
    screenshots = screenshot_retention_plan(screenshot_dir, current, int(policy["screenshot_days"]))
    runtime_logs = runtime_log_retention_plan(root, current, int(policy["runtime_log_days"]))
    expired_count = send_logs["expired_count"] + screenshots["expired_count"] + runtime_logs["expired_count"]
    expired_bytes = screenshots["expired_bytes"] + runtime_logs["expired_bytes"]
    return {
        "generated_at": current.isoformat(sep=" ", timespec="seconds"),
        "policy": policy,
        "expired_count": expired_count,
        "expired_bytes": expired_bytes,
        "send_logs": send_logs,
        "screenshots": screenshots,
        "runtime_logs": runtime_logs,
        "detail": f"过期发送日志 {send_logs['expired_count']} 条，截图/运行日志 {screenshots['expired_count'] + runtime_logs['expired_count']} 个",
    }


def prune_retention(
    db: Session,
    screenshot_dir: Path,
    root: Path,
    policy: dict,
    now: datetime | None = None,
    *,
    execute: bool = False,
) -> dict:
    current = now or datetime.utcnow()
    report = retention_report(db, screenshot_dir, root, policy, current)
    result = {"executed": execute, "report": report, "deleted": {"send_logs": 0, "screenshots": 0, "runtime_logs": 0}}
    if not execute:
        return result

    send_cutoff = current - timedelta(days=int(policy["send_log_days"]))
    result["deleted"]["send_logs"] = db.query(SendLog).filter(SendLog.sent_at < send_cutoff).delete(synchronize_session=False)

    screenshot_cutoff = current - timedelta(days=int(policy["screenshot_days"]))
    for path in _expired_files(screenshot_dir, "task_*", screenshot_cutoff, lambda name: bool(SCREENSHOT_RE.fullmatch(name))):
        path.unlink(missing_ok=True)
        result["deleted"]["screenshots"] += 1

    runtime_cutoff = current - timedelta(days=int(policy["runtime_log_days"]))
    deleted_runtime = set()
    for pattern in ROTATED_LOG_PATTERNS:
        for path in _expired_files(root, pattern, runtime_cutoff):
            resolved = path.resolve()
            if resolved in deleted_runtime:
                continue
            path.unlink(missing_ok=True)
            deleted_runtime.add(resolved)
            result["deleted"]["runtime_logs"] += 1

    db.commit()
    return result
