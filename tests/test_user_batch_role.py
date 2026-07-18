"""批量改角色、用户列表筛选/搜索、导入 SQL 角色枚举约定。"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from passlib.context import CryptContext
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

_tmp = tempfile.mkdtemp(prefix="crs_batch_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ.setdefault("UPLOAD_DIR", str(Path(_tmp) / "uploads"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "Admin@123456")
os.environ.setdefault("DEBUG", "true")
os.environ["AUTH_MODE"] = "local"
os.environ["SEED_DEMO_USERS"] = "true"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.main import app  # noqa: E402
from app.models import User, UserRole  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATE_SQL = REPO_ROOT / "scripts" / "20260718_migrate_users_from_old_system.sql"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _login(client: TestClient, username: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _users_by_name(client: TestClient, h: dict, **params) -> dict:
    r = client.get("/api/auth/users", headers=h, params=params or None)
    assert r.status_code == 200, r.text
    return {u["username"]: u for u in r.json()}


def test_batch_role_forbidden_for_non_admin(client: TestClient):
    token = _login(client, "office1", "Demo@123456")
    h = _auth(token)
    users = _users_by_name(client, h)
    r = client.patch(
        "/api/auth/users/batch-role",
        headers=h,
        json={"user_ids": [users["handler1"]["id"]], "role": "viewer"},
    )
    assert r.status_code == 403


def test_batch_role_cannot_promote_to_admin(client: TestClient):
    token = _login(client, "admin", "Admin@123456")
    h = _auth(token)
    users = _users_by_name(client, h)
    r = client.patch(
        "/api/auth/users/batch-role",
        headers=h,
        json={"user_ids": [users["handler1"]["id"]], "role": "admin"},
    )
    assert r.status_code == 400
    assert "管理员" in r.json()["detail"]


def test_batch_role_cannot_change_admin_account(client: TestClient):
    token = _login(client, "admin", "Admin@123456")
    h = _auth(token)
    users = _users_by_name(client, h)
    r = client.patch(
        "/api/auth/users/batch-role",
        headers=h,
        json={"user_ids": [users["admin"]["id"]], "role": "viewer"},
    )
    assert r.status_code == 400
    assert "管理员" in r.json()["detail"]


def test_batch_role_success_and_list_filters(client: TestClient):
    token = _login(client, "admin", "Admin@123456")
    h = _auth(token)

    # 准备两个同部门、可批量调整的用户
    for uname, dname in (("batch_u1", "批量甲"), ("batch_u2", "批量乙")):
        existing = _users_by_name(client, h)
        if uname in existing:
            continue
        r = client.post(
            "/api/auth/users",
            headers=h,
            json={
                "username": uname,
                "password": "Batch@123456",
                "display_name": dname,
                "role": "viewer",
                "unit": "巡警特警及维稳工作大队",
            },
        )
        assert r.status_code == 200, r.text

    users = _users_by_name(client, h)
    ids = [users["batch_u1"]["id"], users["batch_u2"]["id"]]

    r = client.patch(
        "/api/auth/users/batch-role",
        headers=h,
        json={"user_ids": ids, "role": "handler"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 2
    assert body["skipped"] == 0

    users = _users_by_name(client, h)
    assert users["batch_u1"]["role"] == "handler"
    assert users["batch_u2"]["role"] == "handler"

    # 再次批量同一角色 → skipped
    r = client.patch(
        "/api/auth/users/batch-role",
        headers=h,
        json={"user_ids": ids, "role": "handler"},
    )
    assert r.status_code == 200
    assert r.json()["updated"] == 0
    assert r.json()["skipped"] == 2

    # 部门筛选
    r = client.get(
        "/api/auth/users",
        headers=h,
        params={"unit": "巡警特警及维稳工作大队"},
    )
    assert r.status_code == 200
    names = {u["username"] for u in r.json()}
    assert "batch_u1" in names and "batch_u2" in names
    assert all(u["unit"] == "巡警特警及维稳工作大队" for u in r.json())

    # 关键字：工号
    r = client.get("/api/auth/users", headers=h, params={"q": "batch_u1"})
    assert r.status_code == 200
    assert {u["username"] for u in r.json()} == {"batch_u1"}

    # 关键字：姓名
    r = client.get("/api/auth/users", headers=h, params={"q": "批量乙"})
    assert r.status_code == 200
    assert {u["username"] for u in r.json()} == {"batch_u2"}


def test_pg_userrole_enum_and_migrate_sql_conventions():
    """验证金仓/PG 上 role 为原生枚举 userrole，且导入 SQL 写法与之匹配。"""
    ddl = str(CreateTable(User.__table__).compile(dialect=postgresql.dialect()))
    assert "role userrole" in ddl.replace("\n", " ").lower() or "role userrole" in ddl.lower()
    # 枚举值为小写 value，不是 NAME
    assert UserRole.viewer.value == "viewer"
    assert [r.value for r in UserRole] == [
        "admin",
        "office_clerk",
        "supervisor",
        "handler",
        "leader_a",
        "leader_b",
        "viewer",
    ]

    assert MIGRATE_SQL.is_file(), f"缺少导入 SQL: {MIGRATE_SQL}"
    text = MIGRATE_SQL.read_text(encoding="utf-8")
    assert '"User"' in text and '"badgeNo"' in text and '"Department"' in text
    assert "'viewer'::userrole" in text
    assert "ON CONFLICT (username) DO NOTHING" in text
    # 生成语句不得从旧库 passwordHash 取值（注释里可以提到「不迁移」）
    assert 'u."passwordHash"' not in text
    assert "password_hash" in text and "bcrypt" in text.lower()

    # 抽出 bcrypt 常量并确认是合法哈希（无法本地登录的设计）
    m = re.search(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}", text)
    assert m, "SQL 中应含合法 bcrypt 哈希常量"
    bcrypt_hash = m.group(0)
    ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
    # 随机口令不应匹配该哈希（极高概率）；主要验证哈希格式可被 passlib 识别
    assert ctx.identify(bcrypt_hash) == "bcrypt"
    assert not ctx.verify("definitely-not-the-discarded-secret", bcrypt_hash)
