"""权限隔离、审批收紧、终态禁止上传、文件魔数校验。"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 与 test_smoke 共用同一套 env 时，若已导入 app 则沿用；独立运行时初始化
_tmp = tempfile.mkdtemp(prefix="crs_perm_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ.setdefault("UPLOAD_DIR", str(Path(_tmp) / "uploads"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "Admin@123456")
os.environ.setdefault("DEBUG", "true")
os.environ["AUTH_MODE"] = "local"
os.environ["SEED_DEMO_USERS"] = "true"
os.environ["AUTH_MODE"] = "local"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.main import app  # noqa: E402

MIN_DOCX = b"PK\x03\x04" + b"\x00" * 20
FAKE_DOCX = b"this-is-not-a-zip-docx"


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


def _users(client: TestClient, h: dict) -> dict:
    return {u["username"]: u for u in client.get("/api/auth/users", headers=h).json()}


def _create_item(client: TestClient, h: dict, users: dict, title: str = "权限测试事项") -> int:
    r = client.post(
        "/api/items",
        headers=h,
        json={
            "title": title,
            "handler_id": users["handler1"]["id"],
            "leader_a_id": users["leader_a"]["id"],
            "leader_b_id": users["leader_b"]["id"],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _ensure_outsider(client: TestClient, h: dict) -> dict:
    """创建非参与人 outsider（若不存在）。"""
    users = _users(client, h)
    if "outsider" in users:
        return users["outsider"]
    r = client.post(
        "/api/auth/users",
        headers=h,
        json={
            "username": "outsider",
            "password": "Out@123456",
            "display_name": "局外人",
            "role": "handler",
            "unit": "其他单位",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _upload_main(client: TestClient, h: dict, item_id: int, content: bytes = MIN_DOCX):
    return client.post(
        f"/api/items/{item_id}/upload",
        headers=h,
        data={"kind": "main"},
        files={
            "file": (
                "材料.docx",
                io.BytesIO(content),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )


def test_outsider_cannot_view_item(client: TestClient):
    admin_h = _auth(_login(client, "admin", "Admin@123456"))
    users = _users(client, admin_h)
    item_id = _create_item(client, admin_h, users, "外人不可见")
    _ensure_outsider(client, admin_h)

    ot = _login(client, "outsider", "Out@123456")
    r = client.get(f"/api/items/{item_id}", headers=_auth(ot))
    assert r.status_code == 403
    assert "无权" in r.json()["detail"]

    # 列表也不可见
    r = client.get("/api/items", headers=_auth(ot))
    assert r.status_code == 200
    assert all(it["id"] != item_id for it in r.json())


def test_outsider_cannot_download(client: TestClient):
    admin_h = _auth(_login(client, "admin", "Admin@123456"))
    users = _users(client, admin_h)
    item_id = _create_item(client, admin_h, users, "外人不可下载")
    r = _upload_main(client, admin_h, item_id)
    assert r.status_code == 200
    version_id = r.json()["versions"][0]["id"]
    _ensure_outsider(client, admin_h)

    ot = _login(client, "outsider", "Out@123456")
    r = client.get(f"/api/versions/{version_id}/download", headers=_auth(ot))
    assert r.status_code == 403


def test_non_handler_cannot_submit_a(client: TestClient):
    admin_h = _auth(_login(client, "admin", "Admin@123456"))
    users = _users(client, admin_h)
    item_id = _create_item(client, admin_h, users, "非承办不可提交")
    # leader_a 不是承办人/创建人
    at = _login(client, "leader_a", "Demo@123456")
    r = client.post(
        f"/api/items/{item_id}/submit-a",
        headers=_auth(at),
        json={"comment": "越权提交"},
    )
    assert r.status_code == 403


def test_wrong_leader_a_cannot_approve_or_reject(client: TestClient):
    admin_h = _auth(_login(client, "admin", "Admin@123456"))
    users = _users(client, admin_h)
    # 创建第二个 A 领导角色用户
    r = client.post(
        "/api/auth/users",
        headers=admin_h,
        json={
            "username": "leader_a2",
            "password": "Demo@123456",
            "display_name": "另一A领导",
            "role": "leader_a",
        },
    )
    assert r.status_code == 200, r.text

    item_id = _create_item(client, admin_h, users, "指定A才能审")
    # 承办人提交
    ht = _login(client, "handler1", "Demo@123456")
    r = client.post(f"/api/items/{item_id}/submit-a", headers=_auth(ht), json={})
    assert r.status_code == 200

    # 非指定 A 领导（leader_a2）不可通过/退回
    a2 = _login(client, "leader_a2", "Demo@123456")
    r = client.post(
        f"/api/items/{item_id}/approve-a",
        headers=_auth(a2),
        json={"comment": "越权通过"},
    )
    assert r.status_code == 403

    r = client.post(
        f"/api/items/{item_id}/reject-a",
        headers=_auth(a2),
        json={"comment": "越权退回"},
    )
    assert r.status_code == 403


def test_wrong_leader_b_cannot_finalize_or_reject(client: TestClient):
    admin_h = _auth(_login(client, "admin", "Admin@123456"))
    users = _users(client, admin_h)
    r = client.post(
        "/api/auth/users",
        headers=admin_h,
        json={
            "username": "leader_b2",
            "password": "Demo@123456",
            "display_name": "另一B领导",
            "role": "leader_b",
        },
    )
    assert r.status_code == 200, r.text

    item_id = _create_item(client, admin_h, users, "指定B才能定稿")
    ht = _login(client, "handler1", "Demo@123456")
    assert client.post(f"/api/items/{item_id}/submit-a", headers=_auth(ht), json={}).status_code == 200
    at = _login(client, "leader_a", "Demo@123456")
    assert (
        client.post(
            f"/api/items/{item_id}/approve-a",
            headers=_auth(at),
            json={"comment": "ok"},
        ).status_code
        == 200
    )

    b2 = _login(client, "leader_b2", "Demo@123456")
    r = client.post(
        f"/api/items/{item_id}/finalize",
        headers=_auth(b2),
        json={"comment": "越权定稿"},
    )
    assert r.status_code == 403

    r = client.post(
        f"/api/items/{item_id}/reject-b",
        headers=_auth(b2),
        json={"comment": "越权退回"},
    )
    assert r.status_code == 403


def test_finalized_cannot_upload(client: TestClient):
    admin_h = _auth(_login(client, "admin", "Admin@123456"))
    users = _users(client, admin_h)
    item_id = _create_item(client, admin_h, users, "定稿后禁上传")
    assert _upload_main(client, admin_h, item_id).status_code == 200

    ht = _login(client, "handler1", "Demo@123456")
    assert client.post(f"/api/items/{item_id}/submit-a", headers=_auth(ht), json={}).status_code == 200
    at = _login(client, "leader_a", "Demo@123456")
    assert (
        client.post(
            f"/api/items/{item_id}/approve-a",
            headers=_auth(at),
            json={"comment": "ok"},
        ).status_code
        == 200
    )
    bt = _login(client, "leader_b", "Demo@123456")
    assert (
        client.post(
            f"/api/items/{item_id}/finalize",
            headers=_auth(bt),
            json={"comment": "定稿"},
        ).status_code
        == 200
    )

    r = _upload_main(client, admin_h, item_id)
    assert r.status_code == 400
    assert "不可上传" in r.json()["detail"]


def test_fake_docx_rejected(client: TestClient):
    admin_h = _auth(_login(client, "admin", "Admin@123456"))
    users = _users(client, admin_h)
    item_id = _create_item(client, admin_h, users, "伪docx")
    r = _upload_main(client, admin_h, item_id, content=FAKE_DOCX)
    assert r.status_code == 400
    assert "不匹配" in r.json()["detail"]


def test_min_zip_docx_accepted(client: TestClient):
    admin_h = _auth(_login(client, "admin", "Admin@123456"))
    users = _users(client, admin_h)
    item_id = _create_item(client, admin_h, users, "合法zip头")
    r = _upload_main(client, admin_h, item_id, content=MIN_DOCX)
    assert r.status_code == 200, r.text
    assert r.json()["current_version"] >= 1
