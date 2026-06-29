"""SQLite 数据备份与恢复演练。

基础版只做非破坏式恢复演练：创建备份、列出备份、校验备份可读和核心表存在。
"""

import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote


BACKUP_RE = re.compile(r"coach_mvp_\d{8}_\d{6}\.sqlite3")
REQUIRED_TABLES = {"families", "raw_messages", "send_tasks", "send_logs"}


def sqlite_path_from_url(database_url: str, base_dir: Path) -> Path:
    if not database_url.startswith("sqlite:///"):
        raise ValueError("当前仅支持 SQLite 文件数据库备份")
    raw = unquote(database_url.replace("sqlite:///", "", 1))
    if raw == ":memory:":
        raise ValueError("内存数据库不支持文件备份")
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def ensure_backup_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def validate_backup_filename(filename: str) -> str:
    if not BACKUP_RE.fullmatch(filename or ""):
        raise ValueError("备份文件名非法")
    return filename


def backup_path(backup_dir: Path, filename: str) -> Path:
    safe = validate_backup_filename(filename)
    base = ensure_backup_dir(backup_dir)
    path = (base / safe).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError("备份路径非法") from exc
    return path


def create_sqlite_backup(database_url: str, backup_dir: Path, base_dir: Path, now: datetime | None = None) -> dict:
    source = sqlite_path_from_url(database_url, base_dir)
    if not source.exists():
        raise FileNotFoundError(f"数据库文件不存在：{source}")
    now = now or datetime.utcnow()
    filename = f"coach_mvp_{now.strftime('%Y%m%d_%H%M%S')}.sqlite3"
    target = backup_path(backup_dir, filename)
    with closing(sqlite3.connect(str(source))) as src, closing(sqlite3.connect(str(target))) as dst:
        src.backup(dst)
    return backup_file_info(target)


def backup_file_info(path: Path) -> dict:
    stat = path.stat()
    return {
        "filename": path.name,
        "size_bytes": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds"),
        "sensitivity": "raw_sensitive",
        "contains_sensitive_data": True,
        "note": "原始数据库备份包含家长、孩子、聊天和发送内容，仅限管理员保存。",
    }


def list_backups(backup_dir: Path) -> list[dict]:
    base = ensure_backup_dir(backup_dir)
    files = [path for path in base.iterdir() if path.is_file() and BACKUP_RE.fullmatch(path.name)]
    return [backup_file_info(path) for path in sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)]


def run_restore_drill(path: Path) -> dict:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError("备份文件不存在")
    with closing(sqlite3.connect(str(path))) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    missing = sorted(REQUIRED_TABLES - tables)
    passed = integrity == "ok" and not missing
    return {
        "filename": path.name,
        "passed": passed,
        "integrity": integrity,
        "table_count": len(tables),
        "missing_tables": missing,
    }
