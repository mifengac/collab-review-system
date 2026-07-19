"""OA 登录适配测试（全部 mock，不连接真实 OA）。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
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
os.environ["OA_PRECHECK_ENABLED"] = "false"
os.environ.setdefault("OA_WARMUP_PATH", "/hportal/")
os.environ.setdefault("OA_LOGIN_PATH", "/hportal/j_security_check")
os.environ.setdefault("OA_PROFILE_PATH", "/hportal/view/GetModuleTree.do")

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
    _login_and_profile,
    authenticate_oa_user,
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


def test_mixed_admin_logs_in_locally_even_when_oa_up(client: TestClient):
    """mixed 模式下 admin 直接本地验证，凭据绝不发往 OA（OA 正常时也能登录）。"""
    _set_mode("mixed")
    with patch(
        "app.routers.auth.authenticate_oa_user",
        side_effect=OAAuthError("OA 账号或密码错误"),
    ) as mock_oa:
        r = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "Admin@123456"},
        )
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "admin"
    mock_oa.assert_not_called()


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


def _profile_json(username: str = "oa_warmup_user") -> dict:
    return {
        "success": True,
        "userInfo": {
            "userCode": username,
            "userName": "热身用户",
            "departmentName": "测试单位",
            "departmentCode": "T01",
            "positionName": "测试岗",
        },
    }


def test_login_and_profile_request_order_warmup_then_login():
    """_login_and_profile 必须先热身 GET，再（可选）预检，再登录 POST。"""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()
        calls.append((method, path))
        if method == "GET" and path.rstrip("/") == "/hportal":
            return httpx.Response(
                200,
                headers=[
                    ("set-cookie", "TONG_JSESSIONID=fake-pre; Path=/"),
                    ("set-cookie", "route=node1; Path=/"),
                ],
                text="<html>login</html>",
            )
        if method == "POST" and path.endswith("j_security_check"):
            return httpx.Response(200, text="ok")
        if method == "POST" and path.endswith("GetModuleTree.do"):
            return httpx.Response(200, json=_profile_json())
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, follow_redirects=True) as client:
        profile = _login_and_profile(client, "oa_warmup_user", "secret")
    assert profile.username == "oa_warmup_user"
    assert calls[0][0] == "GET"
    assert "/hportal" in calls[0][1]
    assert any(m == "POST" and "j_security_check" in p for m, p in calls)
    assert any(m == "POST" and "GetModuleTree" in p for m, p in calls)
    # 热身必须在登录之前
    warmup_i = next(i for i, (m, p) in enumerate(calls) if m == "GET" and "/hportal" in p)
    login_i = next(i for i, (m, p) in enumerate(calls) if m == "POST" and "j_security_check" in p)
    assert warmup_i < login_i


def test_login_and_profile_with_precheck_order():
    """开启预检时顺序：热身 → 预检 → 登录 → 用户信息。"""
    os.environ["OA_PRECHECK_ENABLED"] = "true"
    get_settings.cache_clear()
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()
        calls.append((method, path))
        if method == "GET":
            return httpx.Response(200, text="ok")
        if "j_security_check" in path:
            return httpx.Response(200, text="ok")
        if "GetModuleTree" in path:
            return httpx.Response(200, json=_profile_json("u2"))
        return httpx.Response(200, text="ok")

    try:
        transport = httpx.MockTransport(handler)
        with httpx.Client(transport=transport, follow_redirects=True) as client:
            _login_and_profile(client, "u2", "pw")
    finally:
        os.environ["OA_PRECHECK_ENABLED"] = "false"
        get_settings.cache_clear()

    paths = [p for _, p in calls]
    assert any("/hportal" in p for p in paths)
    assert any("checkUserPKI" in p or "getUserNum" in p for p in paths)
    assert any("j_security_check" in p for p in paths)
    warmup_i = next(i for i, (m, p) in enumerate(calls) if m == "GET")
    login_i = next(i for i, (_, p) in enumerate(calls) if "j_security_check" in p)
    assert warmup_i < login_i


def test_warmup_timeout_raises_unavailable():
    """热身超时由 authenticate_oa_user 统一转成 OAAuthUnavailable。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("warmup timeout", request=request)

    transport = httpx.MockTransport(handler)
    mock_client = httpx.Client(transport=transport, follow_redirects=True)
    with patch("app.services.oa_auth._open_oa_client") as open_client:
        open_client.return_value.__enter__.return_value = mock_client
        open_client.return_value.__exit__.return_value = None
        with pytest.raises(OAAuthUnavailable) as ei:
            authenticate_oa_user("u", "p")
        assert "超时" in ei.value.message
