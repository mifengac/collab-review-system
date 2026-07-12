"""分派绕过、创建角色、角色校验与督办误设承办人。"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="crs_guard_")
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
from app.database import SessionLocal  # noqa: E402
from app.models import Item  # noqa: E402

MIN_DOCX = b"PK\x03\x04" + b"\x00" * 20


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _login(client: TestClient, username: str, password: str = "Demo@123456") -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _admin(client):
    return _login(client, "admin", "Admin@123456")


def _opts(client, h):
    return {u["username"]: u for u in client.get("/api/auth/user-options", headers=h).json()}


def _item_for_handler(client, h, users, title="守卫测试事项"):
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


def test_handler_put_cannot_change_leaders(client: TestClient):
    ah = _admin(client)
    users = _opts(client, ah)
    item_id = _item_for_handler(client, ah, users, "PUT绕过分派")
    ht = _login(client, "handler1")
    r = client.put(
        f"/api/items/{item_id}",
        headers=ht,
        json={
            "title": "仍可改标题",
            "leader_a_id": users["leader_b"]["id"],
            "handler_id": users["handler1"]["id"],
        },
    )
    assert r.status_code == 400
    assert "分派" in r.json()["detail"]

    # 确认未变更
    it = client.get(f"/api/items/{item_id}", headers=ht).json()
    assert it["leader_a_id"] == users["leader_a"]["id"]
    assert it["title"] != "仍可改标题"

    r = client.put(
        f"/api/items/{item_id}",
        headers=ht,
        json={"title": "合法改标题", "remark": "ok"},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "合法改标题"


def test_office_assign_ok(client: TestClient):
    oh = _login(client, "office1")
    users = _opts(client, oh)
    item_id = client.post(
        "/api/items",
        headers=oh,
        json={"title": "办公室可分派"},
    ).json()["id"]
    r = client.post(
        f"/api/items/{item_id}/assign",
        headers=oh,
        json={
            "handler_id": users["handler1"]["id"],
            "leader_a_id": users["leader_a"]["id"],
            "leader_b_id": users["leader_b"]["id"],
        },
    )
    assert r.status_code == 200
    assert r.json()["handler_id"] == users["handler1"]["id"]


def test_supervisor_cannot_create(client: TestClient):
    sh = _login(client, "supervisor1")
    r = client.post("/api/items", headers=sh, json={"title": "督办建项"})
    assert r.status_code == 403
    assert "不可创建" in r.json()["detail"]


def test_leader_a_cannot_create(client: TestClient):
    at = _login(client, "leader_a")
    r = client.post("/api/items", headers=at, json={"title": "领导建项"})
    assert r.status_code == 403


def test_viewer_cannot_create(client: TestClient):
    ah = _admin(client)
    client.post(
        "/api/auth/users",
        headers=ah,
        json={
            "username": "viewer1",
            "password": "Demo@123456",
            "display_name": "只读用户",
            "role": "viewer",
        },
    )
    vh = _login(client, "viewer1")
    r = client.post("/api/items", headers=vh, json={"title": "只读建项"})
    assert r.status_code == 403


def test_supervisor_as_handler_cannot_edit_upload(client: TestClient):
    """即使 handler_id 被误设为 supervisor，也不能编辑/上传。"""
    ah = _admin(client)
    users = _opts(client, ah)
    item_id = _item_for_handler(client, ah, users, "误设督办为承办")

    # 直接改库模拟误分派（绕过 assign 角色校验）
    db = SessionLocal()
    try:
        item = db.query(Item).filter(Item.id == item_id).first()
        item.handler_id = users["supervisor1"]["id"]
        db.commit()
    finally:
        db.close()

    sh = _login(client, "supervisor1")
    r = client.put(
        f"/api/items/{item_id}",
        headers=sh,
        json={"title": "督办想改"},
    )
    assert r.status_code == 403

    r = client.post(
        f"/api/items/{item_id}/upload",
        headers=sh,
        data={"kind": "main"},
        files={
            "file": (
                "x.docx",
                io.BytesIO(MIN_DOCX),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert r.status_code == 403


def test_assign_nonexistent_user(client: TestClient):
    oh = _login(client, "office1")
    item_id = client.post(
        "/api/items",
        headers=oh,
        json={"title": "分派不存在用户"},
    ).json()["id"]
    r = client.post(
        f"/api/items/{item_id}/assign",
        headers=oh,
        json={"handler_id": 999999},
    )
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"] or "禁用" in r.json()["detail"]


def test_assign_leader_a_must_be_leader_a_role(client: TestClient):
    oh = _login(client, "office1")
    users = _opts(client, oh)
    item_id = client.post(
        "/api/items",
        headers=oh,
        json={"title": "A领导角色校验"},
    ).json()["id"]
    r = client.post(
        f"/api/items/{item_id}/assign",
        headers=oh,
        json={"leader_a_id": users["handler1"]["id"]},
    )
    assert r.status_code == 400
    assert "A领导" in r.json()["detail"]


def test_assign_handler_must_be_handler_role(client: TestClient):
    oh = _login(client, "office1")
    users = _opts(client, oh)
    item_id = client.post(
        "/api/items",
        headers=oh,
        json={"title": "承办人角色校验"},
    ).json()["id"]
    r = client.post(
        f"/api/items/{item_id}/assign",
        headers=oh,
        json={"handler_id": users["supervisor1"]["id"]},
    )
    assert r.status_code == 400
    assert "承办人" in r.json()["detail"]
