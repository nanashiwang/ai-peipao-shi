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
def ensure_columns():
    wanted = {
        "weekly_reports": [
            ("send_task_id", "INTEGER"),
            ("send_status", "VARCHAR(30)"),
            ("sent_at", "DATETIME"),
        ],
        "send_tasks": [("device_id", "VARCHAR(64)"), ("send_mode", "VARCHAR(20)")],
        "send_logs": [("device_id", "VARCHAR(64)"), ("screenshot_path", "TEXT"), ("send_mode", "VARCHAR(20)")],
        "ai_outputs": [("evidence_json", "TEXT")],
    }
    inspector = inspect(engine)
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
                }.get(col_name, "''")
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type} DEFAULT {default}"))
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{col_name} ON {table} ({col_name})"))
