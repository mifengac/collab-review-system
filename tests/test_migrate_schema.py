"""真实旧版 SQLite 库迁移测试：缺 is_active 时升级成功 / 失败硬停。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, inspect, text

# 独立环境，避免污染其他测试的 DATABASE_URL
_tmp = tempfile.mkdtemp(prefix="crs_migrate_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/placeholder.db")
os.environ.setdefault("UPLOAD_DIR", str(Path(_tmp) / "uploads"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("SEED_DEMO_USERS", "false")
os.environ.setdefault("AUTH_MODE", "local")
os.environ.setdefault("OA_MOCK_ENABLED", "false")

from app.database import migrate_schema  # noqa: E402


def _make_legacy_db(path: Path):
    """创建明确不含 is_active 的旧版 oa_work_items。"""
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE oa_work_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_user_id INTEGER NOT NULL,
                    oa_user_code VARCHAR(64) NOT NULL,
                    module_code VARCHAR(32) NOT NULL,
                    module_name VARCHAR(64) NOT NULL,
                    flowinid VARCHAR(64) NOT NULL,
                    stepinco VARCHAR(64),
                    dealindx VARCHAR(64),
                    external_key VARCHAR(256) NOT NULL DEFAULT '',
                    title VARCHAR(512) NOT NULL,
                    doc_no VARCHAR(128),
                    source_unit VARCHAR(256),
                    flow_name VARCHAR(256),
                    step_name VARCHAR(256),
                    handler_name VARCHAR(128),
                    received_at DATETIME,
                    open_date DATETIME,
                    has_attach BOOLEAN DEFAULT 0,
                    read_flag INTEGER,
                    fini_flag INTEGER,
                    urgency INTEGER,
                    raw_json TEXT,
                    linked_item_id INTEGER,
                    synced_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO oa_work_items (
                    owner_user_id, oa_user_code, module_code, module_name,
                    flowinid, stepinco, dealindx, external_key, title
                ) VALUES (
                    1, 'legacy_user', 'todo', '待办公文',
                    'LEGACY-FLOW-1', 'S1', '1', 'todo|LEGACY-FLOW-1|S1|1',
                    '旧库遗留公文'
                )
                """
            )
        )
    return eng


def _make_legacy_file_versions_db(path: Path):
    """旧库 file_versions 无 version_kind 列。"""
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    username VARCHAR(64) NOT NULL
                )
                """
            )
        )
        conn.execute(text("INSERT INTO users (id, username) VALUES (1, 'u1')"))
        conn.execute(
            text(
                """
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY,
                    item_id INTEGER NOT NULL,
                    name VARCHAR(256) NOT NULL,
                    kind VARCHAR(16),
                    current_version INTEGER DEFAULT 0
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO documents (id, item_id, name, kind, current_version) "
                "VALUES (1, 1, '主材料.docx', 'main', 1)"
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE file_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    version_no INTEGER NOT NULL,
                    original_filename VARCHAR(512) NOT NULL,
                    stored_path VARCHAR(1024) NOT NULL,
                    content_type VARCHAR(128),
                    file_size INTEGER DEFAULT 0,
                    sha256 VARCHAR(64) NOT NULL,
                    uploader_id INTEGER NOT NULL,
                    created_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO file_versions (
                    document_id, version_no, original_filename, stored_path,
                    file_size, sha256, uploader_id
                ) VALUES (
                    1, 1, 'old.docx', '1/old.docx',
                    10, 'abc123legacyhash0000000000000000000000000000000000000000', 1
                )
                """
            )
        )
    return eng


def test_legacy_sqlite_migrate_adds_version_kind(tmp_path):
    db_path = tmp_path / "legacy_fv.db"
    eng = _make_legacy_file_versions_db(db_path)
    cols_before = {c["name"] for c in inspect(eng).get_columns("file_versions")}
    assert "version_kind" not in cols_before

    migrate_schema(bind=eng)

    cols = {c["name"] for c in inspect(eng).get_columns("file_versions")}
    assert "version_kind" in cols
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT version_kind, original_filename FROM file_versions WHERE version_no=1")
        ).one()
        assert row[0] == "normal"
        assert row[1] == "old.docx"
    # 幂等
    migrate_schema(bind=eng)
    eng.dispose()


def test_legacy_sqlite_migrate_adds_is_active_and_preserves_data(tmp_path):
    db_path = tmp_path / "legacy.db"
    eng = _make_legacy_db(db_path)

    cols_before = {c["name"] for c in inspect(eng).get_columns("oa_work_items")}
    assert "is_active" not in cols_before

    migrate_schema(bind=eng)

    cols = {c["name"] for c in inspect(eng).get_columns("oa_work_items")}
    assert "is_active" in cols

    with eng.connect() as conn:
        row = conn.execute(
            text(
                "SELECT title, is_active, flowinid FROM oa_work_items "
                "WHERE flowinid='LEGACY-FLOW-1'"
            )
        ).one()
        assert row[0] == "旧库遗留公文"
        assert row[1] in (1, True)
        assert row[2] == "LEGACY-FLOW-1"
        count = conn.execute(text("SELECT COUNT(*) FROM oa_work_items")).scalar()
        assert count == 1

    eng.dispose()


def test_migrate_idempotent_repeat_safe(tmp_path):
    db_path = tmp_path / "legacy2.db"
    eng = _make_legacy_db(db_path)
    migrate_schema(bind=eng)
    migrate_schema(bind=eng)
    migrate_schema(bind=eng)

    cols = {c["name"] for c in inspect(eng).get_columns("oa_work_items")}
    assert "is_active" in cols
    with eng.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM oa_work_items")).scalar() == 1
    eng.dispose()


def test_migrate_failure_raises_runtime_error(tmp_path):
    """模拟 ALTER 失败时必须抛出 RuntimeError，不可静默继续。"""
    db_path = tmp_path / "legacy_fail.db"
    eng = _make_legacy_db(db_path)

    def boom(*args, **kwargs):
        raise OSError("simulated alter failure")

    with patch.object(eng, "begin", side_effect=boom):
        with pytest.raises(RuntimeError) as ei:
            migrate_schema(bind=eng)
    assert "is_active" in str(ei.value) or "升级失败" in str(ei.value)

    # 字段仍不存在
    cols = {c["name"] for c in inspect(eng).get_columns("oa_work_items")}
    assert "is_active" not in cols
    eng.dispose()


def test_migrate_new_db_noop_when_no_table(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    # 无表：应直接返回
    migrate_schema(bind=eng)
    assert not inspect(eng).has_table("oa_work_items")
    eng.dispose()


def test_migrate_already_has_is_active_ok(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'modern.db'}")
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE oa_work_items (
                    id INTEGER PRIMARY KEY,
                    owner_user_id INTEGER,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    external_key VARCHAR(256) DEFAULT '',
                    module_code VARCHAR(32) DEFAULT ''
                )
                """
            )
        )
    migrate_schema(bind=eng)
    migrate_schema(bind=eng)
    cols = {c["name"] for c in inspect(eng).get_columns("oa_work_items")}
    assert "is_active" in cols
    eng.dispose()
