"""数据库引擎与会话。结构兼容 SQLite / PostgreSQL / Kingbase。"""
from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
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


def _resolve_bind(bind: Engine | None = None) -> Engine:
    return bind if bind is not None else engine


def _table_columns(table_name: str, bind: Engine | None = None) -> set[str]:
    eng = _resolve_bind(bind)
    insp = inspect(eng)
    if not insp.has_table(table_name):
        return set()
    return {c["name"] for c in insp.get_columns(table_name)}


def _ensure_oa_work_item_indexes(eng: Engine) -> None:
    """
    为常用查询补幂等索引（不删除已有库）。
    SQLite / PostgreSQL / Kingbase 均支持 CREATE INDEX IF NOT EXISTS。
    """
    index_sql = [
        (
            "ix_oa_work_items_owner_active",
            "CREATE INDEX IF NOT EXISTS ix_oa_work_items_owner_active "
            "ON oa_work_items (owner_user_id, is_active)",
        ),
        (
            "ix_oa_work_items_owner_module_active",
            "CREATE INDEX IF NOT EXISTS ix_oa_work_items_owner_module_active "
            "ON oa_work_items (owner_user_id, module_code, is_active)",
        ),
        (
            "ix_oa_work_items_owner_external_key",
            "CREATE INDEX IF NOT EXISTS ix_oa_work_items_owner_external_key "
            "ON oa_work_items (owner_user_id, external_key)",
        ),
    ]
    with eng.begin() as conn:
        for _name, sql in index_sql:
            conn.execute(text(sql))


def migrate_schema(bind: Engine | None = None) -> None:
    """
    最小幂等结构升级（create_all 不会给旧表加列）。
    当前：为 oa_work_items 补充 is_active，并补常用查询索引。

    is_active 升级失败时抛出 RuntimeError，禁止带着错误结构继续启动。
    日志仅记录受控中文说明与异常类型，不输出连接串。
    """
    eng = _resolve_bind(bind)
    cols = _table_columns("oa_work_items", eng)
    if not cols:
        return  # 新库由 create_all 建全表（含 is_active）

    if "is_active" not in cols:
        dialect = eng.dialect.name
        try:
            with eng.begin() as conn:
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
                            "ADD COLUMN IF NOT EXISTS is_active "
                            "BOOLEAN NOT NULL DEFAULT TRUE"
                        )
                    )
            logger.info("schema migrate: 已为 oa_work_items 补充 is_active 字段")
        except Exception as exc:
            logger.error(
                "schema migrate: is_active 字段升级失败，类型=%s",
                type(exc).__name__,
            )
            raise RuntimeError(
                "数据库结构升级失败：无法为 oa_work_items 增加 is_active 字段"
            ) from exc

        # ALTER 后重新检查
        cols_after = _table_columns("oa_work_items", eng)
        if "is_active" not in cols_after:
            logger.error("schema migrate: ALTER 后仍未检测到 is_active 字段")
            raise RuntimeError(
                "数据库结构升级失败：oa_work_items.is_active 字段仍不存在"
            )

    # 字段已存在（新升级或重复启动）：补索引（幂等）
    try:
        _ensure_oa_work_item_indexes(eng)
    except Exception as exc:
        logger.error(
            "schema migrate: 索引创建失败，类型=%s",
            type(exc).__name__,
        )
        raise RuntimeError("数据库结构升级失败：无法创建 oa_work_items 查询索引") from exc


def init_db() -> None:
    """创建表结构 + 轻量升级。使用正式 engine。"""
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrate_schema(bind=engine)
