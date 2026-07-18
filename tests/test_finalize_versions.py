"""3c：定稿归档痕迹版 / 终稿标记、终态只读。"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="crs_fin_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ.setdefault("UPLOAD_DIR", str(Path(_tmp) / "uploads"))
os.environ.setdefault("SECRET_KEY", "finalize-test-secret-key")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "Admin@123456")
os.environ.setdefault("DEBUG", "true")
os.environ["AUTH_MODE"] = "local"
os.environ["SEED_DEMO_USERS"] = "true"
# 本模块不依赖 ONLYOFFICE 配置；回调测试时再开
for k in (
    "ONLYOFFICE_ENABLED",
    "ONLYOFFICE_PUBLIC_URL",
    "ONLYOFFICE_JWT_SECRET",
    "APP_INTERNAL_URL",
):
    os.environ.pop(k, None)

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.main import app  # noqa: E402
from app.services import onlyoffice as oo_svc  # noqa: E402

MIN_DOCX = b"PK\x03\x04" + b"\x00" * 40


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


def _to_b_review(client, item_id: int):
    hh = _login(client, "handler1")
    assert client.post(f"/api/items/{item_id}/submit-a", headers=hh, json={}).status_code == 200
    la = _login(client, "leader_a")
    assert client.post(f"/api/items/{item_id}/approve-a", headers=la, json={}).status_code == 200


def _create_with_main(client, title: str) -> tuple[int, int, dict]:
    ah = _admin(client)
    users = _users(client, ah)
    r = client.post(
        "/api/items",
        headers=ah,
        json={
            "title": title,
            "handler_id": users["handler1"]["id"],
            "leader_a_id": users["leader_a"]["id"],
            "leader_b_id": users["leader_b"]["id"],
        },
    )
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    r = client.post(
        f"/api/items/{item_id}/upload",
        headers=ah,
        data={"kind": "main"},
        files={
            "file": (
                "main.docx",
                io.BytesIO(MIN_DOCX),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["versions"][0].get("version_kind", "normal") == "normal"
    return item_id, doc["id"], users


def test_mark_finalize_and_timeline(client: TestClient):
    item_id, doc_id, users = _create_with_main(client, "定稿归档流程")
    _to_b_review(client, item_id)

    # 非授权角色不可
    oh = _login(client, "office1")
    r = client.post(f"/api/items/{item_id}/mark-finalize", headers=oh, json={})
    assert r.status_code == 403

    hh = _login(client, "handler1")
    r = client.post(
        f"/api/items/{item_id}/mark-finalize",
        headers=hh,
        json={"comment": "先留痕迹版"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version_kind"] == "marked"
    assert body["version_no"] == 1
    assert body["document_id"] == doc_id

    # 版本列表可见类型
    r = client.get(f"/api/documents/{doc_id}/versions", headers=hh)
    assert r.status_code == 200
    vers = r.json()
    assert any(v["version_no"] == 1 and v["version_kind"] == "marked" for v in vers)

    # 时间线有定稿归档
    r = client.get(f"/api/items/{item_id}/timeline", headers=hh)
    actions = [x["action"] for x in r.json()]
    assert "定稿归档" in actions

    # 状态仍是 B 审核中（未锁定）
    r = client.get(f"/api/items/{item_id}", headers=hh)
    assert r.json()["status"] == "B领导审核中"


def test_onlyoffice_save_after_marked_becomes_final(client: TestClient, monkeypatch):
    """启用 ONLYOFFICE 配置后，痕迹版之后的保存应标为终稿。"""
    from jose import jwt

    monkeypatch.setenv("ONLYOFFICE_ENABLED", "true")
    monkeypatch.setenv("ONLYOFFICE_PUBLIC_URL", "http://onlyoffice.test")
    monkeypatch.setenv("ONLYOFFICE_JWT_SECRET", "oo-jwt-secret-for-tests")
    monkeypatch.setenv("APP_INTERNAL_URL", "http://app.test")
    get_settings.cache_clear()

    item_id, doc_id, users = _create_with_main(client, "终稿标记")
    _to_b_review(client, item_id)
    hh = _login(client, "handler1")
    assert (
        client.post(f"/api/items/{item_id}/mark-finalize", headers=hh, json={}).status_code
        == 200
    )

    new_bytes = b"PK\x03\x04FINAL" + b"\x00" * 30
    monkeypatch.setattr(oo_svc, "download_remote_file", lambda url, **kw: new_bytes)

    body = {
        "status": 2,
        "url": "http://onlyoffice.test/out.docx",
        "users": [str(users["handler1"]["id"])],
    }
    secret = get_settings().onlyoffice_jwt_secret
    tok = jwt.encode(body, secret, algorithm="HS256")
    r = client.post(
        f"/api/onlyoffice/callback?document_id={doc_id}",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["error"] == 0

    r = client.get(f"/api/documents/{doc_id}/versions", headers=hh)
    by_no = {v["version_no"]: v for v in r.json()}
    assert by_no[1]["version_kind"] == "marked"
    assert by_no[2]["version_kind"] == "final"
    get_settings.cache_clear()


def test_leader_save_after_marked_stays_normal(client: TestClient, monkeypatch):
    """标记痕迹版后，领导（无接受修订权）的保存不得升级为终稿。"""
    from jose import jwt

    monkeypatch.setenv("ONLYOFFICE_ENABLED", "true")
    monkeypatch.setenv("ONLYOFFICE_PUBLIC_URL", "http://onlyoffice.test")
    monkeypatch.setenv("ONLYOFFICE_JWT_SECRET", "oo-jwt-secret-for-tests")
    monkeypatch.setenv("APP_INTERNAL_URL", "http://app.test")
    get_settings.cache_clear()

    item_id, doc_id, users = _create_with_main(client, "领导后改不算终稿")
    _to_b_review(client, item_id)
    hh = _login(client, "handler1")
    assert (
        client.post(f"/api/items/{item_id}/mark-finalize", headers=hh, json={}).status_code
        == 200
    )

    monkeypatch.setattr(
        oo_svc, "download_remote_file", lambda url, **kw: b"PK\x03\x04LEADER" + b"\x00" * 30
    )
    secret = get_settings().onlyoffice_jwt_secret

    # B 领导保存：仍为 normal
    body = {
        "status": 2,
        "url": "http://onlyoffice.test/leader.docx",
        "users": [str(users["leader_b"]["id"])],
    }
    tok = jwt.encode(body, secret, algorithm="HS256")
    r = client.post(
        f"/api/onlyoffice/callback?document_id={doc_id}",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200 and r.json()["error"] == 0

    r = client.get(f"/api/documents/{doc_id}/versions", headers=hh)
    by_no = {v["version_no"]: v for v in r.json()}
    assert by_no[2]["version_kind"] == "normal"

    # 随后承办人保存：升级为终稿
    monkeypatch.setattr(
        oo_svc, "download_remote_file", lambda url, **kw: b"PK\x03\x04CLEAN" + b"\x00" * 30
    )
    body2 = {
        "status": 2,
        "url": "http://onlyoffice.test/clean.docx",
        "users": [str(users["handler1"]["id"])],
    }
    tok2 = jwt.encode(body2, secret, algorithm="HS256")
    r = client.post(
        f"/api/onlyoffice/callback?document_id={doc_id}",
        json=body2,
        headers={"Authorization": f"Bearer {tok2}"},
    )
    assert r.status_code == 200 and r.json()["error"] == 0

    r = client.get(f"/api/documents/{doc_id}/versions", headers=hh)
    by_no = {v["version_no"]: v for v in r.json()}
    assert by_no[3]["version_kind"] == "final"
    get_settings.cache_clear()


def test_finalize_locks_and_auto_mark(client: TestClient):
    item_id, doc_id, _users = _create_with_main(client, "B定稿自动痕迹")
    _to_b_review(client, item_id)
    lb = _login(client, "leader_b")
    r = client.post(
        f"/api/items/{item_id}/finalize",
        headers=lb,
        json={"comment": "直接定稿"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "已定稿"

    ah = _admin(client)
    r = client.get(f"/api/documents/{doc_id}/versions", headers=ah)
    v1 = next(v for v in r.json() if v["version_no"] == 1)
    # 未先 mark-finalize 时，定稿自动把当前版标为痕迹存档
    assert v1["version_kind"] == "marked"

    # 终态不可再定稿归档
    hh = _login(client, "handler1")
    r = client.post(f"/api/items/{item_id}/mark-finalize", headers=hh, json={})
    assert r.status_code == 400
