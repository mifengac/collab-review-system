"""OA 登录适配测试（全部 mock，不连接真实 OA）。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="crs_oa_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ.setdefault("UPLOAD_DIR", str(Path(_tmp) / "uploads"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "Admin@123456")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("SEED_DEMO_USERS", "true")
os.environ["AUTH_MODE"] = "local"
os.environ["OA_DEFAULT_ROLE"] = "viewer"
os.environ["OA_BASE_URL"] = "http://oa.example.invalid"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.main import app  # noqa: E402
from app.auth import verify_password  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.services.oa_auth import (  # noqa: E402
    OAAuthError,
    OAAuthUnavailable,
    OAUserProfile,
)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _restore_local_auth():
    """每个用例后恢复 local，避免污染其他测试模块。"""
    yield
    os.environ["AUTH_MODE"] = "local"
    get_settings.cache_clear()


def _set_mode(mode: str, default_role: str = "viewer"):
    os.environ["AUTH_MODE"] = mode
    os.environ["OA_DEFAULT_ROLE"] = default_role
    get_settings.cache_clear()


def test_auth_config_local(client: TestClient):
    _set_mode("local")
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    data = r.json()
    assert data["auth_mode"] == "local"
    assert data["oa_enabled"] is False


def test_local_login_still_works(client: TestClient):
    _set_mode("local")
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "Admin@123456"},
    )
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "admin"


def test_oa_first_login_creates_user_with_default_role(client: TestClient):
    _set_mode("oa", "viewer")
    profile = OAUserProfile(
        username="oa_user_001",
        display_name="OA张三",
        unit="信息工作大队",
        department_code="0101",
        position_name="民警",
    )
    with patch("app.routers.auth.authenticate_oa_user", return_value=profile):
        r = client.post(
            "/api/auth/login",
            json={"username": "oa_user_001", "password": "any-oa-password"},
        )
    assert r.status_code == 200, r.text
    user = r.json()["user"]
    assert user["username"] == "oa_user_001"
    assert user["display_name"] == "OA张三"
    assert user["unit"] == "信息工作大队"
    assert user["role"] == "viewer"

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == "oa_user_001").first()
        assert u is not None
        assert not verify_password("any-oa-password", u.password_hash)
    finally:
        db.close()


def test_oa_second_login_keeps_local_role(client: TestClient):
    _set_mode("oa", "viewer")
    profile = OAUserProfile(
        username="oa_user_002",
        display_name="OA李四",
        unit="办公室",
    )
    with patch("app.routers.auth.authenticate_oa_user", return_value=profile):
        r = client.post(
            "/api/auth/login",
            json={"username": "oa_user_002", "password": "x"},
        )
    assert r.status_code == 200

    # 管理员改角色（本地模式）
    _set_mode("local")
    admin = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "Admin@123456"},
    )
    assert admin.status_code == 200
    ah = {"Authorization": f"Bearer {admin.json()['access_token']}"}
    users = client.get("/api/auth/users", headers=ah).json()
    uid = next(u["id"] for u in users if u["username"] == "oa_user_002")
    r = client.patch(
        f"/api/auth/users/{uid}",
        headers=ah,
        json={"role": "office_clerk"},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "office_clerk"

    _set_mode("oa")
    profile2 = OAUserProfile(
        username="oa_user_002",
        display_name="OA李四更新",
        unit="新单位",
    )
    with patch("app.routers.auth.authenticate_oa_user", return_value=profile2):
        r = client.post(
            "/api/auth/login",
            json={"username": "oa_user_002", "password": "x"},
        )
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "office_clerk"
    assert r.json()["user"]["display_name"] == "OA李四更新"
    assert r.json()["user"]["unit"] == "新单位"


def test_oa_disabled_user_rejected(client: TestClient):
    _set_mode("local")
    admin = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "Admin@123456"},
    )
    ah = {"Authorization": f"Bearer {admin.json()['access_token']}"}
    client.post(
        "/api/auth/users",
        headers=ah,
        json={
            "username": "oa_disabled",
            "password": "Local@123456",
            "display_name": "已禁用",
            "role": "viewer",
        },
    )
    users = client.get("/api/auth/users", headers=ah).json()
    uid = next(u["id"] for u in users if u["username"] == "oa_disabled")
    client.patch(f"/api/auth/users/{uid}", headers=ah, json={"is_active": False})

    _set_mode("oa")
    profile = OAUserProfile(username="oa_disabled", display_name="已禁用", unit=None)
    with patch("app.routers.auth.authenticate_oa_user", return_value=profile):
        r = client.post(
            "/api/auth/login",
            json={"username": "oa_disabled", "password": "oa-pass"},
        )
    assert r.status_code == 403
    assert "禁用" in r.json()["detail"]


def test_oa_auth_failure_401(client: TestClient):
    _set_mode("oa")
    with patch(
        "app.routers.auth.authenticate_oa_user",
        side_effect=OAAuthError("OA 账号或密码错误"),
    ):
        r = client.post(
            "/api/auth/login",
            json={"username": "bad", "password": "bad"},
        )
    assert r.status_code == 401


def test_mixed_oa_unavailable_admin_fallback(client: TestClient):
    _set_mode("mixed")
    with patch(
        "app.routers.auth.authenticate_oa_user",
        side_effect=OAAuthUnavailable("OA 服务暂不可用"),
    ):
        r = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "Admin@123456"},
        )
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "admin"


def test_mixed_oa_unavailable_normal_user_no_fallback(client: TestClient):
    _set_mode("local")
    admin = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "Admin@123456"},
    )
    ah = {"Authorization": f"Bearer {admin.json()['access_token']}"}
    client.post(
        "/api/auth/users",
        headers=ah,
        json={
            "username": "local_user_oa_test",
            "password": "Local@123456",
            "display_name": "本地用户",
            "role": "handler",
        },
    )

    _set_mode("mixed")
    with patch(
        "app.routers.auth.authenticate_oa_user",
        side_effect=OAAuthUnavailable("OA 服务暂不可用"),
    ):
        r = client.post(
            "/api/auth/login",
            json={"username": "local_user_oa_test", "password": "Local@123456"},
        )
    assert r.status_code == 503
    assert "OA" in r.json()["detail"]


def test_auth_config_oa(client: TestClient):
    _set_mode("oa")
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    assert r.json()["auth_mode"] == "oa"
    assert r.json()["oa_enabled"] is True
