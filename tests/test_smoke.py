"""基础 smoke test：登录、建事项、上传、A/B 审核流转。"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 测试使用独立临时目录，避免污染开发库
_tmp = tempfile.mkdtemp(prefix="crs_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["UPLOAD_DIR"] = str(Path(_tmp) / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "Admin@123456"
os.environ["DEBUG"] = "true"
os.environ["AUTH_MODE"] = "local"
os.environ["SEED_DEMO_USERS"] = "true"

# 清除 settings 缓存后再导入应用
from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.main import app  # noqa: E402

# 最小合法 OOXML / ZIP 头
MIN_DOCX = b"PK\x03\x04" + b"\x00" * 20


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


def test_health(client: TestClient):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_fail(client: TestClient):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_full_workflow(client: TestClient):
    admin_token = _login(client, "admin", "Admin@123456")
    h = _auth(admin_token)

    me = client.get("/api/auth/me", headers=h)
    assert me.status_code == 200
    assert me.json()["username"] == "admin"

    depts = client.get("/api/dict/departments", headers=h)
    assert depts.status_code == 200
    assert len(depts.json()) == 4
    tags = client.get("/api/dict/tags", headers=h)
    assert tags.status_code == 200
    assert len(tags.json()) == 15

    users = client.get("/api/auth/users", headers=h).json()
    by_name = {u["username"]: u for u in users}
    handler_id = by_name["handler1"]["id"]
    leader_a_id = by_name["leader_a"]["id"]
    leader_b_id = by_name["leader_b"]["id"]

    r = client.post(
        "/api/items",
        headers=h,
        json={
            "title": "测试重点场所排查材料",
            "oa_doc_no": "公治〔2026〕测试001",
            "source_unit": "市局治安支队",
            "handler_dept": "治安管理行动大队",
            "business_tag": "人口密集场所",
            "urgency": "重要",
            "handler_id": handler_id,
            "leader_a_id": leader_a_id,
            "leader_b_id": leader_b_id,
            "remark": "smoke test",
        },
    )
    assert r.status_code == 200, r.text
    item = r.json()
    item_id = item["id"]
    assert item["status"] == "承办中"

    files = {
        "file": (
            "材料初稿.docx",
            io.BytesIO(MIN_DOCX),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    }
    r = client.post(
        f"/api/items/{item_id}/upload",
        headers=h,
        data={"kind": "main"},
        files=files,
    )
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["kind"] == "main"
    assert doc["current_version"] == 1
    version_id = doc["versions"][0]["id"]
    document_id = doc["id"]

    files2 = {
        "file": (
            "材料修订.docx",
            io.BytesIO(MIN_DOCX + b"-v2"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    }
    r = client.post(
        f"/api/items/{item_id}/upload",
        headers=h,
        data={"kind": "main"},
        files=files2,
    )
    assert r.status_code == 200
    assert r.json()["current_version"] == 2

    r = client.get(f"/api/versions/{version_id}/download", headers=h)
    assert r.status_code == 200
    assert r.content == MIN_DOCX

    ht = _login(client, "handler1", "Demo@123456")
    r = client.post(
        f"/api/items/{item_id}/submit-a",
        headers=_auth(ht),
        json={"comment": "请领导审阅"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "A领导审核中"

    at = _login(client, "leader_a", "Demo@123456")
    r = client.post(
        f"/api/items/{item_id}/approve-a",
        headers=_auth(at),
        json={"comment": "同意，报 B 领导"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "B领导审核中"

    bt = _login(client, "leader_b", "Demo@123456")
    r = client.post(
        f"/api/items/{item_id}/finalize",
        headers=_auth(bt),
        json={"comment": "定稿"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "已定稿"

    r = client.get(f"/api/items/{item_id}/timeline", headers=h)
    assert r.status_code == 200
    actions = [x["action"] for x in r.json()]
    assert "创建事项" in actions
    assert "上传文件" in actions
    assert "下载文件" in actions
    assert "提交A领导审核" in actions
    assert "A领导通过" in actions
    assert "定稿" in actions

    r = client.get(f"/api/documents/{document_id}/editor-config", headers=h)
    assert r.status_code == 200
    assert r.json()["reserved"] is True

    r = client.post(f"/api/office/callback/{document_id}")
    assert r.status_code == 200

    r = client.get("/api/oa/inbox", headers=h)
    assert r.status_code == 200

    r = client.post("/api/oa/sync", headers=h, json={"force": False})
    assert r.status_code == 200
    # 未带 OA 密码时返回提示（不保存密码，需登录后自动同步或手动填密）
    assert "message" in r.json()

    r = client.get("/api/items/dashboard", headers=h)
    assert r.status_code == 200
    assert "todo" in r.json()


def test_reject_requires_comment(client: TestClient):
    admin_token = _login(client, "admin", "Admin@123456")
    h = _auth(admin_token)
    users = {u["username"]: u for u in client.get("/api/auth/users", headers=h).json()}
    r = client.post(
        "/api/items",
        headers=h,
        json={
            "title": "退回测试事项",
            "handler_id": users["handler1"]["id"],
            "leader_a_id": users["leader_a"]["id"],
            "leader_b_id": users["leader_b"]["id"],
        },
    )
    item_id = r.json()["id"]
    client.post(f"/api/items/{item_id}/submit-a", headers=h, json={})
    at = _login(client, "leader_a", "Demo@123456")
    r = client.post(
        f"/api/items/{item_id}/reject-a",
        headers=_auth(at),
        json={"comment": ""},
    )
    assert r.status_code == 400
