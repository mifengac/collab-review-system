"""数据库引擎与会话。结构兼容 SQLite / PostgreSQL / Kingbase。"""
from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

connect_args = {}
if settings.is_sqlite:
    connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=True,
)

if settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):  # noqa: ARG001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _table_columns(table_name: str) -> set[str]:
    insp = inspect(engine)
    if not insp.has_table(table_name):
        return set()
    return {c["name"] for c in insp.get_columns(table_name)}


def migrate_schema() -> None:
    """
    最小幂等结构升级（create_all 不会给旧表加列）。
    当前：为 oa_work_items 补充 is_active。
    """
    cols = _table_columns("oa_work_items")
    if not cols:
        return  # 新库由 create_all 建全表
    if "is_active" in cols:
        return

    dialect = engine.dialect.name
    try:
        with engine.begin() as conn:
            if dialect == "sqlite":
                # SQLite：BOOLEAN 用 INTEGER；已有行默认有效
                conn.execute(
                    text(
                        "ALTER TABLE oa_work_items "
                        "ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1"
                    )
                )
            else:
                # PostgreSQL / Kingbase 等
                conn.execute(
                    text(
                        "ALTER TABLE oa_work_items "
                        "ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE"
                    )
                )
        logger.info("schema migrate: added oa_work_items.is_active")
    except Exception as exc:
        # 不记录完整异常文本（可能含连接串等）；仅类型
        logger.warning("schema migrate is_active failed: %s", type(exc).__name__)


def init_db() -> None:
    """创建表结构 + 轻量升级。"""
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrate_schema()
