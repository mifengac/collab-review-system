"""数据库引擎与会话。结构兼容 SQLite / PostgreSQL / Kingbase。"""
from __future__ import annotations

import logging
import re
from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

# KingbaseES / 部分国产 PG 兼容库的 version() 不是 "PostgreSQL x.y" 格式，
# SQLAlchemy 默认解析会 AssertionError。在 create_engine 前打补丁。
_ORIGINAL_PG_GET_SERVER_VERSION_INFO = PGDialect._get_server_version_info


def _parse_kingbase_as_pg_version(version_string: str) -> tuple[int, ...] | None:
    """
    将 KingbaseES version() 映射为 SQLAlchemy 可用的 PostgreSQL 版本元组。
    例：KingbaseES V008R006C009B0014 ... → (12, 0)
    V8 系通常按 PG12 兼容能力处理（方言特性开关用）。
    """
    if not version_string:
        return None
    if "KingbaseES" not in version_string and "Kingbase" not in version_string:
        return None
    # V008R006C009B0014
    m = re.search(r"V0*(\d+)R0*(\d+)", version_string, re.IGNORECASE)
    if m:
        major = int(m.group(1))
        # 经验映射：V8+ → PG12；更低版本保守给 PG9.6（仅影响方言特性开关）
        if major >= 8:
            return (12, 0)
        return (9, 6)
    # 识别到金仓但格式变化时，仍给可用默认值，避免启动失败
    logger.warning(
        "Kingbase 版本串无法细分解析，按 PostgreSQL 12 兼容处理: %s",
        version_string[:120],
    )
    return (12, 0)


def _get_server_version_info_kingbase_safe(self, connection):  # noqa: ANN001
    try:
        return _ORIGINAL_PG_GET_SERVER_VERSION_INFO(self, connection)
    except AssertionError:
        v = connection.exec_driver_sql("select pg_catalog.version()").scalar()
        mapped = _parse_kingbase_as_pg_version(str(v or ""))
        if mapped is not None:
            logger.info(
                "已兼容 KingbaseES version 字符串，按 PostgreSQL %s 处理方言特性",
                ".".join(str(x) for x in mapped),
            )
            return mapped
        raise


# 幂等打补丁（测试/重复 import 安全）
if getattr(PGDialect._get_server_version_info, "__name__", "") != (
    "_get_server_version_info_kingbase_safe"
):
    PGDialect._get_server_version_info = _get_server_version_info_kingbase_safe


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


def _migrate_oa_work_items_is_active(eng: Engine) -> None:
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


def _migrate_file_versions_version_kind(eng: Engine) -> None:
    """为 file_versions 补充 version_kind（normal/marked/final），默认 normal。"""
    cols = _table_columns("file_versions", eng)
    if not cols:
        return  # 新库 create_all 已含该列

    if "version_kind" in cols:
        return

    dialect = eng.dialect.name
    try:
        with eng.begin() as conn:
            if dialect == "sqlite":
                conn.execute(
                    text(
                        "ALTER TABLE file_versions "
                        "ADD COLUMN version_kind VARCHAR(16) NOT NULL DEFAULT 'normal'"
                    )
                )
            else:
                # PostgreSQL / 金仓：用 varchar 存枚举值，兼容 SQLAlchemy Enum
                conn.execute(
                    text(
                        "ALTER TABLE file_versions "
                        "ADD COLUMN IF NOT EXISTS version_kind "
                        "VARCHAR(16) NOT NULL DEFAULT 'normal'"
                    )
                )
        logger.info("schema migrate: 已为 file_versions 补充 version_kind 字段")
    except Exception as exc:
        logger.error(
            "schema migrate: version_kind 字段升级失败，类型=%s",
            type(exc).__name__,
        )
        raise RuntimeError(
            "数据库结构升级失败：无法为 file_versions 增加 version_kind 字段"
        ) from exc

    cols_after = _table_columns("file_versions", eng)
    if "version_kind" not in cols_after:
        raise RuntimeError(
            "数据库结构升级失败：file_versions.version_kind 字段仍不存在"
        )


def _migrate_action_logs_item_id_nullable(eng: Engine) -> None:
    """
    action_logs.item_id 改为可空（系统级审计导出等）。
    PostgreSQL/金仓可 ALTER；SQLite 旧库无法简单改列，新库由 create_all 建为可空。
    """
    cols = _table_columns("action_logs", eng)
    if not cols or "item_id" not in cols:
        return
    dialect = eng.dialect.name
    if dialect == "sqlite":
        return
    try:
        with eng.begin() as conn:
            conn.execute(
                text("ALTER TABLE action_logs ALTER COLUMN item_id DROP NOT NULL")
            )
        logger.info("schema migrate: action_logs.item_id 已允许为空")
    except Exception as exc:
        # 可能已经是可空，不阻断启动
        logger.warning(
            "schema migrate: action_logs.item_id 可空升级跳过，类型=%s",
            type(exc).__name__,
        )


def migrate_schema(bind: Engine | None = None) -> None:
    """
    最小幂等结构升级（create_all 不会给旧表加列）。
    当前：
    - oa_work_items.is_active + 查询索引
    - file_versions.version_kind（痕迹版/终稿标记）
    - action_logs.item_id 可空（PG/金仓）

    关键失败时抛出 RuntimeError，禁止带着错误结构继续启动。
    日志仅记录受控中文说明与异常类型，不输出连接串。
    """
    eng = _resolve_bind(bind)
    _migrate_oa_work_items_is_active(eng)
    _migrate_file_versions_version_kind(eng)
    _migrate_action_logs_item_id_nullable(eng)


def init_db() -> None:
    """创建表结构 + 轻量升级。使用正式 engine。"""
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrate_schema(bind=engine)
