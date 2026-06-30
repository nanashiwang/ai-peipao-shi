"""发送任务领取锁策略。

PostgreSQL 等数据库优先使用行锁 + SKIP LOCKED；SQLite 本地试点退回到条件更新兜底。
"""

from __future__ import annotations


MAX_CLAIM_LIMIT = 50
SKIP_LOCKED_DIALECTS = {"postgresql", "mysql", "mariadb", "oracle"}


def normalize_claim_limit(limit: int, default: int = 5) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = default
    return min(max(value, 1), MAX_CLAIM_LIMIT)


def database_dialect_name(db_or_bind) -> str:
    bind = db_or_bind.get_bind() if hasattr(db_or_bind, "get_bind") else db_or_bind
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "")


def supports_skip_locked(db_or_bind) -> bool:
    return database_dialect_name(db_or_bind) in SKIP_LOCKED_DIALECTS


def apply_claim_row_lock(query, db):
    if supports_skip_locked(db):
        return query.with_for_update(skip_locked=True)
    return query


def claim_lock_report(db_or_bind) -> dict:
    dialect = database_dialect_name(db_or_bind) or "unknown"
    skip_locked = supports_skip_locked(db_or_bind)
    mode = "row_lock_skip_locked" if skip_locked else "conditional_update_fallback"
    detail = "已启用数据库行锁 + SKIP LOCKED" if skip_locked else "使用状态条件更新兜底，适合 SQLite 本地/试点"
    return {
        "status": "ok",
        "label": "任务领取锁",
        "detail": detail,
        "metrics": {
            "dialect": dialect,
            "mode": mode,
            "max_claim_limit": MAX_CLAIM_LIMIT,
        },
    }
