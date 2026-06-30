"""数据库基础设施。

这里统一创建 SQLAlchemy 引擎、会话工厂和 Base，供整个后端复用。
"""

import os
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker


# 没有显式配置时，默认使用本地 SQLite 文件。
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./coach_mvp.db")

# SQLite 需要关闭同线程检查，其他数据库则保持默认连接参数。
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


# FastAPI 依赖注入使用的数据库会话生成器。
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 导入模型并创建所有表结构。
def init_db():
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_columns()


# create_all 只建新表，不会给已存在的旧表补列。这里为旧库平滑补新增列，避免删库重建。
def portable_column_type(col_type: str, dialect_name: str) -> str:
    if dialect_name == "postgresql" and col_type.upper() == "DATETIME":
        return "TIMESTAMP"
    return col_type


def ensure_columns():
    wanted = {
        "families": [
            ("course_stage", "VARCHAR(120)"),
            ("unit_progress", "VARCHAR(120)"),
            ("pbl_count", "INTEGER"),
            ("checkin_rate", "VARCHAR(40)"),
            ("next_milestone", "TEXT"),
            ("campus_name", "VARCHAR(80)"),
        ],
        "weekly_reports": [
            ("send_task_id", "INTEGER"),
            ("send_status", "VARCHAR(30)"),
            ("sent_at", "DATETIME"),
            ("parent_ack_at", "DATETIME"),
            ("parent_ack_note", "TEXT"),
            ("parent_feedback_score", "INTEGER"),
            ("parent_feedback_note", "TEXT"),
            ("parent_feedback_at", "DATETIME"),
        ],
        "send_tasks": [
            ("device_id", "VARCHAR(64)"),
            ("send_mode", "VARCHAR(20)"),
            ("retry_count", "INTEGER"),
            ("max_retries", "INTEGER"),
            ("next_retry_at", "DATETIME"),
            ("last_error", "TEXT"),
        ],
        "send_logs": [("device_id", "VARCHAR(64)"), ("screenshot_path", "TEXT"), ("send_mode", "VARCHAR(20)")],
        "ai_outputs": [("evidence_json", "TEXT")],
        "parent_profiles": [
            ("satisfaction_level", "VARCHAR(20)"),
            ("renewal_intent", "VARCHAR(40)"),
        ],
        "user_accounts": [("campus_names", "TEXT")],
        "devices": [("allow_real_send", "BOOLEAN")],
    }
    inspector = inspect(engine)
    dialect_name = engine.dialect.name
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, cols in wanted.items():
            if table not in tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table)}
            for col_name, col_type in cols:
                if col_name in have:
                    continue
                default = {
                    "send_mode": "'dry_run'",
                    "send_task_id": "0",
                    "send_status": "'not_created'",
                    "sent_at": "NULL",
                    "parent_ack_at": "NULL",
                    "parent_feedback_score": "0",
                    "parent_feedback_at": "NULL",
                    "pbl_count": "0",
                    "retry_count": "0",
                    "max_retries": "2",
                    "next_retry_at": "NULL",
                    "satisfaction_level": "'未知'",
                    "renewal_intent": "'未知'",
                    "allow_real_send": "FALSE",
                }.get(col_name, "''")
                safe_type = portable_column_type(col_type, dialect_name)
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {safe_type} DEFAULT {default}"))
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{col_name} ON {table} ({col_name})"))
