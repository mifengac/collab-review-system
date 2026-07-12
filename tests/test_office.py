"""办公室分派、督办、角色权限与用户管理。"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="crs_office_")
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


def _users(client, h):
    return {u["username"]: u for u in client.get("/api/auth/user-options", headers=h).json()}


def test_office_clerk_create_and_assign(client: TestClient):
    oh = _login(client, "office1")
    users = _users(client, oh)
    r = client.post(
        "/api/items",
        headers=oh,
        json={
            "title": "办公室分派事项",
            "source_unit": "市局",
            "handler_dept": "治安管理行动大队",
        },
    )
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["status"] == "承办中"
    item_id = item["id"]

    r = client.post(
        f"/api/items/{item_id}/assign",
        headers=oh,
        json={
            "handler_id": users["handler1"]["id"],
            "leader_a_id": users["leader_a"]["id"],
            "leader_b_id": users["leader_b"]["id"],
            "comment": "分派给行动大队",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["handler_id"] == users["handler1"]["id"]
    assert r.json()["leader_a_id"] == users["leader_a"]["id"]

    logs = client.get(f"/api/items/{item_id}/timeline", headers=oh).json()
    assert any(x["action"] == "分派调整" for x in logs)


def test_office_clerk_sees_all_items(client: TestClient):
    ah = _admin(client)
    users = _users(client, ah)
    # 管理员创建事项，handler 承办
    r = client.post(
        "/api/items",
        headers=ah,
        json={
            "title": "全局可见事项",
            "handler_id": users["handler1"]["id"],
            "leader_a_id": users["leader_a"]["id"],
            "leader_b_id": users["leader_b"]["id"],
        },
    )
    item_id = r.json()["id"]

    oh = _login(client, "office1")
    r = client.get(f"/api/items/{item_id}", headers=oh)
    assert r.status_code == 200
    r = client.get("/api/items", headers=oh)
    assert r.status_code == 200
    assert any(it["id"] == item_id for it in r.json())


def test_office_cannot_approve_unless_leader(client: TestClient):
    oh = _login(client, "office1")
    users = _users(client, oh)
    r = client.post(
        "/api/items",
        headers=oh,
        json={
            "title": "办公室不可代批",
            "handler_id": users["handler1"]["id"],
            "leader_a_id": users["leader_a"]["id"],
            "leader_b_id": users["leader_b"]["id"],
        },
    )
    item_id = r.json()["id"]
    ht = _login(client, "handler1")
    assert client.post(f"/api/items/{item_id}/submit-a", headers=ht, json={}).status_code == 200

    r = client.post(
        f"/api/items/{item_id}/approve-a",
        headers=oh,
        json={"comment": "办公室代批"},
    )
    assert r.status_code == 403


def test_supervisor_view_download_no_edit_upload_approve(client: TestClient):
    ah = _admin(client)
    users = _users(client, ah)
    r = client.post(
        "/api/items",
        headers=ah,
        json={
            "title": "督办只读事项",
            "handler_id": users["handler1"]["id"],
            "leader_a_id": users["leader_a"]["id"],
            "leader_b_id": users["leader_b"]["id"],
        },
    )
    item_id = r.json()["id"]
    up = client.post(
        f"/api/items/{item_id}/upload",
        headers=ah,
        data={"kind": "main"},
        files={
            "file": (
                "a.docx",
                io.BytesIO(MIN_DOCX),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert up.status_code == 200
    version_id = up.json()["versions"][0]["id"]

    sh = _login(client, "supervisor1")
    assert client.get(f"/api/items/{item_id}", headers=sh).status_code == 200
    assert client.get(f"/api/versions/{version_id}/download", headers=sh).status_code == 200

    r = client.put(
        f"/api/items/{item_id}",
        headers=sh,
        json={"title": "督办改标题"},
    )
    assert r.status_code == 403

    r = client.post(
        f"/api/items/{item_id}/upload",
        headers=sh,
        data={"kind": "attachment"},
        files={
            "file": (
                "b.docx",
                io.BytesIO(MIN_DOCX),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert r.status_code == 403

    ht = _login(client, "handler1")
    assert client.post(f"/api/items/{item_id}/submit-a", headers=ht, json={}).status_code == 200
    r = client.post(
        f"/api/items/{item_id}/approve-a",
        headers=sh,
        json={"comment": "督办代批"},
    )
    assert r.status_code == 403


def test_supervisor_can_supervise(client: TestClient):
    ah = _admin(client)
    users = _users(client, ah)
    item_id = client.post(
        "/api/items",
        headers=ah,
        json={
            "title": "催办测试",
            "handler_id": users["handler1"]["id"],
            "leader_a_id": users["leader_a"]["id"],
            "leader_b_id": users["leader_b"]["id"],
        },
    ).json()["id"]

    sh = _login(client, "supervisor1")
    r = client.post(
        f"/api/items/{item_id}/supervise",
        headers=sh,
        json={"comment": "请今日下班前反馈"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "督办催办"

    r = client.post(
        f"/api/items/{item_id}/supervise",
        headers=sh,
        json={"comment": ""},
    )
    assert r.status_code == 422 or r.status_code == 400


def test_cancel_requires_comment(client: TestClient):
    ah = _admin(client)
    users = _users(client, ah)
    item_id = client.post(
        "/api/items",
        headers=ah,
        json={
            "title": "作废原因必填",
            "handler_id": users["handler1"]["id"],
        },
    ).json()["id"]
    r = client.post(f"/api/items/{item_id}/cancel", headers=ah, json={"comment": ""})
    assert r.status_code == 400
    assert "原因" in r.json()["detail"] or "作废" in r.json()["detail"]

    r = client.post(
        f"/api/items/{item_id}/cancel",
        headers=ah,
        json={"comment": "重复来文，作废"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "已作废"


def test_finalized_cannot_assign(client: TestClient):
    ah = _admin(client)
    users = _users(client, ah)
    item_id = client.post(
        "/api/items",
        headers=ah,
        json={
            "title": "终态不可分派",
            "handler_id": users["handler1"]["id"],
            "leader_a_id": users["leader_a"]["id"],
            "leader_b_id": users["leader_b"]["id"],
        },
    ).json()["id"]
    ht = _login(client, "handler1")
    assert client.post(f"/api/items/{item_id}/submit-a", headers=ht, json={}).status_code == 200
    at = _login(client, "leader_a")
    assert (
        client.post(
            f"/api/items/{item_id}/approve-a",
            headers=at,
            json={"comment": "ok"},
        ).status_code
        == 200
    )
    bt = _login(client, "leader_b")
    assert (
        client.post(
            f"/api/items/{item_id}/finalize",
            headers=bt,
            json={"comment": "定稿"},
        ).status_code
        == 200
    )

    oh = _login(client, "office1")
    r = client.post(
        f"/api/items/{item_id}/assign",
        headers=oh,
        json={"handler_id": users["handler1"]["id"]},
    )
    assert r.status_code == 400


def test_outsider_still_blocked(client: TestClient):
    ah = _admin(client)
    users = _users(client, ah)
    item_id = client.post(
        "/api/items",
        headers=ah,
        json={
            "title": "外人仍不可见",
            "handler_id": users["handler1"]["id"],
            "leader_a_id": users["leader_a"]["id"],
            "leader_b_id": users["leader_b"]["id"],
        },
    ).json()["id"]

    # 创建 outsider
    if "outsider2" not in users:
        client.post(
            "/api/auth/users",
            headers=ah,
            json={
                "username": "outsider2",
                "password": "Out@123456",
                "display_name": "外人2",
                "role": "handler",
            },
        )
    ot = _login(client, "outsider2", "Out@123456")
    assert client.get(f"/api/items/{item_id}", headers=ot).status_code == 403


def test_non_admin_cannot_manage_users(client: TestClient):
    oh = _login(client, "office1")
    r = client.post(
        "/api/auth/users",
        headers=oh,
        json={
            "username": "hackuser",
            "password": "Hack@123456",
            "display_name": "黑客",
            "role": "handler",
        },
    )
    assert r.status_code == 403

    users = _users(client, oh)
    r = client.patch(
        f"/api/auth/users/{users['handler1']['id']}",
        headers=oh,
        json={"display_name": "被改"},
    )
    assert r.status_code == 403

    # 办公室可读用户列表
    assert client.get("/api/auth/users", headers=oh).status_code == 200
    # 普通用户不可
    ht = _login(client, "handler1")
    assert client.get("/api/auth/users", headers=ht).status_code == 403
    # 但可选人
    assert client.get("/api/auth/user-options", headers=ht).status_code == 200
