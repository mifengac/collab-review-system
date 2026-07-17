"""KingbaseES version() 字符串兼容 SQLAlchemy PostgreSQL 方言。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# 与其他测试一致：先设环境再 import app
_tmp = tempfile.mkdtemp(prefix="crs_kb_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/t.db")
os.environ.setdefault("UPLOAD_DIR", str(Path(_tmp) / "uploads"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("SEED_DEMO_USERS", "false")
os.environ.setdefault("AUTH_MODE", "local")
os.environ.setdefault("OA_MOCK_ENABLED", "false")

from app.database import (  # noqa: E402
    _get_server_version_info_kingbase_safe,
    _parse_kingbase_as_pg_version,
)
from sqlalchemy.dialects.postgresql.base import PGDialect  # noqa: E402


class _FakeResult:
    def __init__(self, value: str):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    def __init__(self, version: str):
        self.version = version

    def exec_driver_sql(self, sql):  # noqa: ARG002
        return _FakeResult(self.version)


def test_parse_kingbase_v8r6():
    s = (
        "KingbaseES V008R006C009B0014 on x86_64-pc-linux-gnu,"
        "compiled by gcc(GCC)4.8.5 20250623 (Red hat 4.8.5-28),64-bit"
    )
    assert _parse_kingbase_as_pg_version(s) == (12, 0)


def test_parse_non_kingbase_returns_none():
    assert _parse_kingbase_as_pg_version("PostgreSQL 14.5 on x86_64") is None
    assert _parse_kingbase_as_pg_version("") is None


def test_dialect_patch_accepts_kingbase_string():
    s = (
        "KingbaseES V008R006C009B0014 on x86_64-pc-linux-gnu,"
        "compiled by gcc(GCC)4.8.5 20250623 (Red hat 4.8.5-28),64-bit"
    )
    dialect = PGDialect()
    info = dialect._get_server_version_info(_FakeConn(s))
    assert info == (12, 0)


def test_dialect_patch_function_name():
    assert PGDialect._get_server_version_info.__name__ == (
        "_get_server_version_info_kingbase_safe"
    )
    # 直接调用补丁函数同样可用
    s = "KingbaseES V008R006C008B0020 on aarch64-unknown-linux-gnu"
    info = _get_server_version_info_kingbase_safe(PGDialect(), _FakeConn(s))
    assert info == (12, 0)
