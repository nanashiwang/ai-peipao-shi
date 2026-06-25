"""数据库基础设施。

这里统一创建 SQLAlchemy 引擎、会话工厂和 Base，供整个后端复用。
"""

import os
from sqlalchemy import create_engine
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
