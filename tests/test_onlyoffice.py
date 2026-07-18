"""ONLYOFFICE 3a：editor-config、下载 token、回调保存与权限。"""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jose import jwt

_tmp = tempfile.mkdtemp(prefix="crs_oo_")
_OO_ENV = {
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "UPLOAD_DIR": str(Path(_tmp) / "uploads"),
    "SECRET_KEY": "oo-test-secret-key-not-default",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "Admin@123456",
    "DEBUG": "true",
    "AUTH_MODE": "local",
    "SEED_DEMO_USERS": "true",
    "ONLYOFFICE_ENABLED": "true",
    "ONLYOFFICE_PUBLIC_URL": "http://onlyoffice.test",
    "ONLYOFFICE_INTERNAL_URL": "http://onlyoffice.test",
    "ONLYOFFICE_JWT_SECRET": "oo-jwt-secret-for-tests",
    "APP_INTERNAL_URL": "http://app.test",
}
_PREV_ENV = {k: os.environ.get(k) for k in _OO_ENV}
os.environ.update(_OO_ENV)

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.main import app  # noqa: E402
from app.services import onlyoffice as oo_svc  # noqa: E402

MIN_DOCX = b"PK\x03\x04" + b"\x00" * 40


@pytest.fixture(scope="module", autouse=True)
def _restore_oo_env_after_module():
    """本模块结束后还原 ONLYOFFICE 相关环境，避免污染其它测试。"""
    yield
    for k, old in _PREV_ENV.items():
        if old is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = old
    # 关掉 onlyoffice，防止后续模块 get_settings 读到 true
    for k in (
        "ONLYOFFICE_ENABLED",
        "ONLYOFFICE_PUBLIC_URL",
        "ONLYOFFICE_INTERNAL_URL",
        "ONLYOFFICE_JWT_SECRET",
        "APP_INTERNAL_URL",
    ):
        os.environ.pop(k, None)
    get_settings.cache_clear()


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


def _create_item_with_main(client, h, users, title="OO测试事项") -> tuple[int, int]:
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
    item_id = r.json()["id"]
    r = client.post(
        f"/api/items/{item_id}/upload",
        headers=h,
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
    return item_id, doc["id"]


def test_auth_config_onlyoffice_flag(client: TestClient):
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    assert r.json()["onlyoffice_enabled"] is True


def test_editor_config_permission_and_shape(client: TestClient):
    ah = _admin(client)
    users = _users(client, ah)
    _item_id, doc_id = _create_item_with_main(client, ah, users, "OO权限")

    # 参与人可取 + 强制修订 + 可 review
    hh = _login(client, "handler1")
    r = client.get(f"/api/documents/{doc_id}/editor-config", headers=hh)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reserved"] is False
    assert body["mode"] == "edit"
    assert body["track_changes_forced"] is True
    assert body["can_review"] is True
    assert body["version_no"] == 1
    assert body["editor_url"]
    assert body["config"]["document"]["url"]
    assert "token" in body["config"]
    assert "raw?token=" in body["config"]["document"]["url"]
    assert body["config"]["editorConfig"]["callbackUrl"].endswith(
        f"/api/onlyoffice/callback?document_id={doc_id}"
    )
    assert body["config"]["editorConfig"]["customization"]["trackChanges"] is True
    assert body["config"]["document"]["permissions"]["edit"] is True
    assert body["config"]["document"]["permissions"]["review"] is True
    # 作者名含单位
    uname = body["config"]["editorConfig"]["user"]["name"]
    assert "·" in uname or uname  # 有单位则带点号

    # 局外人 / viewer 403
    r = client.post(
        "/api/auth/users",
        headers=ah,
        json={
            "username": "oo_outsider",
            "password": "Out@123456",
            "display_name": "局外",
            "role": "viewer",
        },
    )
    assert r.status_code == 200
    oh = _login(client, "oo_outsider", "Out@123456")
    r = client.get(f"/api/documents/{doc_id}/editor-config", headers=oh)
    assert r.status_code == 403


def test_raw_download_token_checks(client: TestClient):
    ah = _admin(client)
    users = _users(client, ah)
    _item_id, doc_id = _create_item_with_main(client, ah, users, "OO下载令牌")

    r = client.get(f"/api/documents/{doc_id}/raw")
    assert r.status_code == 422  # 缺 query

    r = client.get(f"/api/documents/{doc_id}/raw", params={"token": "not-a-jwt"})
    assert r.status_code == 401

    # 错误 purpose
    bad = jwt.encode(
        {"purpose": "other", "document_id": doc_id},
        get_settings().secret_key,
        algorithm=get_settings().algorithm,
    )
    r = client.get(f"/api/documents/{doc_id}/raw", params={"token": bad})
    assert r.status_code == 401

    # 文档 id 不匹配
    wrong_doc = jwt.encode(
        {"purpose": "oo_download", "document_id": doc_id + 999},
        get_settings().secret_key,
        algorithm=get_settings().algorithm,
    )
    r = client.get(f"/api/documents/{doc_id}/raw", params={"token": wrong_doc})
    assert r.status_code == 401

    # 合法 token
    good = oo_svc.create_download_token(doc_id)
    r = client.get(f"/api/documents/{doc_id}/raw", params={"token": good})
    assert r.status_code == 200
    assert r.content.startswith(b"PK")


def test_callback_requires_jwt_and_saves_version(client: TestClient, monkeypatch):
    ah = _admin(client)
    users = _users(client, ah)
    item_id, doc_id = _create_item_with_main(client, ah, users, "OO回调保存")

    r = client.get(f"/api/documents/{doc_id}/versions", headers=ah)
    ver_before = max(v["version_no"] for v in r.json())

    new_bytes = b"PK\x03\x04" + b"NEWOO" + b"\x00" * 30
    expect_sha = hashlib.sha256(new_bytes).hexdigest()

    def _fake_download(url: str, *, timeout: float = 60.0) -> bytes:
        assert url.startswith("http")
        return new_bytes

    monkeypatch.setattr(oo_svc, "download_remote_file", _fake_download)

    body = {
        "status": 2,
        "url": "http://onlyoffice.test/cache/files/out.docx",
        "key": "crs-test-key",
        "users": [str(users["handler1"]["id"])],
    }
    # 无 JWT
    r = client.post(f"/api/onlyoffice/callback?document_id={doc_id}", json=body)
    assert r.status_code == 401

    # 错误密钥
    bad_tok = jwt.encode(body, "wrong-secret", algorithm="HS256")
    r = client.post(
        f"/api/onlyoffice/callback?document_id={doc_id}",
        json=body,
        headers={"Authorization": f"Bearer {bad_tok}"},
    )
    assert r.status_code == 401

    # 正确 JWT
    good_tok = jwt.encode(body, get_settings().onlyoffice_jwt_secret, algorithm="HS256")
    r = client.post(
        f"/api/onlyoffice/callback?document_id={doc_id}",
        json=body,
        headers={"Authorization": f"Bearer {good_tok}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["error"] == 0

    r = client.get(f"/api/documents/{doc_id}/versions", headers=ah)
    versions = r.json()
    assert max(v["version_no"] for v in versions) == ver_before + 1
    latest = max(versions, key=lambda v: v["version_no"])
    assert latest["sha256"] == expect_sha

    # status=1 不保存新版本
    body1 = {**body, "status": 1}
    tok1 = jwt.encode(body1, get_settings().onlyoffice_jwt_secret, algorithm="HS256")
    r = client.post(
        f"/api/onlyoffice/callback?document_id={doc_id}",
        json=body1,
        headers={"Authorization": f"Bearer {tok1}"},
    )
    assert r.status_code == 200
    assert r.json()["error"] == 0
    r = client.get(f"/api/documents/{doc_id}/versions", headers=ah)
    assert max(v["version_no"] for v in r.json()) == ver_before + 1


def test_callback_replayed_token_cannot_forge_body(client: TestClient, monkeypatch):
    """拿旧的合法令牌配伪造 body（恶意 url + status=2）不得触发保存。"""
    ah = _admin(client)
    users = _users(client, ah)
    item_id, doc_id = _create_item_with_main(client, ah, users, "OO重放防护")

    r = client.get(f"/api/documents/{doc_id}/versions", headers=ah)
    ver_before = max(v["version_no"] for v in r.json())

    called = {"n": 0}

    def _fake_download(url: str, *, timeout: float = 60.0) -> bytes:
        called["n"] += 1
        return b"PK\x03\x04forged" + b"\x00" * 30

    monkeypatch.setattr(oo_svc, "download_remote_file", _fake_download)

    # 攻击者持有一个内容无害（status=1、无 url）的合法签名令牌
    replayed_tok = jwt.encode(
        {"status": 1, "key": "crs-old-key"},
        get_settings().onlyoffice_jwt_secret,
        algorithm="HS256",
    )
    forged_body = {
        "status": 2,
        "url": "http://attacker.example/evil.docx",
        "users": ["1"],
    }
    r = client.post(
        f"/api/onlyoffice/callback?document_id={doc_id}",
        json=forged_body,
        headers={"Authorization": f"Bearer {replayed_tok}"},
    )
    # 验签通过但字段以令牌内容为准：status=1 → 不保存
    assert r.status_code == 200
    assert called["n"] == 0, "不得按伪造 body 的 url 发起下载"

    r = client.get(f"/api/documents/{doc_id}/versions", headers=ah)
    assert max(v["version_no"] for v in r.json()) == ver_before, "不得产生新版本"


def test_callback_rejects_finalized_item(client: TestClient, monkeypatch):
    ah = _admin(client)
    users = _users(client, ah)
    item_id, doc_id = _create_item_with_main(client, ah, users, "OO终态拒绝")

    # 推到定稿
    hh = _login(client, "handler1")
    assert client.post(f"/api/items/{item_id}/submit-a", headers=hh, json={}).status_code == 200
    la = _login(client, "leader_a")
    assert client.post(f"/api/items/{item_id}/approve-a", headers=la, json={}).status_code == 200
    lb = _login(client, "leader_b")
    assert (
        client.post(f"/api/items/{item_id}/finalize", headers=lb, json={"comment": "定"}).status_code
        == 200
    )

    monkeypatch.setattr(oo_svc, "download_remote_file", lambda url, **kw: MIN_DOCX + b"X")

    body = {
        "status": 2,
        "url": "http://onlyoffice.test/out.docx",
        "users": [str(users["handler1"]["id"])],
    }
    tok = jwt.encode(body, get_settings().onlyoffice_jwt_secret, algorithm="HS256")
    r = client.post(
        f"/api/onlyoffice/callback?document_id={doc_id}",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    assert r.json()["error"] == 1

    r = client.get(f"/api/documents/{doc_id}/versions", headers=ah)
    assert max(v["version_no"] for v in r.json()) == 1


def test_editor_permission_matrix_and_same_key(client: TestClient):
    """
    可写：承办人 / A·B 领导 / admin；
    只读：办公室、督办；
    拒绝：viewer、非参与人；
    终态只读；
    同一版本两人 config 的 document.key 相同。
    """
    ah = _admin(client)
    users = _users(client, ah)
    item_id, doc_id = _create_item_with_main(client, ah, users, "OO权限矩阵")

    def cfg(headers):
        r = client.get(f"/api/documents/{doc_id}/editor-config", headers=headers)
        return r.status_code, (r.json() if r.status_code == 200 else r.json())

    # admin 可写 + review
    st, body = cfg(ah)
    assert st == 200
    assert body["mode"] == "edit"
    assert body["can_review"] is True
    assert body["config"]["document"]["permissions"]["edit"] is True
    key_admin = body["config"]["document"]["key"]

    # 承办人可写 + review，key 与 admin 相同（同版本协同）
    st, body = cfg(_login(client, "handler1"))
    assert st == 200
    assert body["mode"] == "edit"
    assert body["can_review"] is True
    assert body["track_changes_forced"] is True
    assert body["config"]["document"]["key"] == key_admin

    # A 领导：仅能修订——edit=false + review=true 是权限级强制留痕（界面无法关闭）
    st, body = cfg(_login(client, "leader_a"))
    assert st == 200
    assert body["mode"] == "review"
    assert body["can_review"] is False
    assert body["track_changes_forced"] is True
    assert body["config"]["document"]["permissions"]["edit"] is False
    assert body["config"]["document"]["permissions"]["review"] is True
    # review 能力要求 DS 的 editorConfig.mode 仍为 edit
    assert body["config"]["editorConfig"]["mode"] == "edit"
    assert body["config"]["editorConfig"]["customization"]["trackChanges"] is True
    assert body["config"]["document"]["key"] == key_admin

    # B 领导同 A
    st, body = cfg(_login(client, "leader_b"))
    assert st == 200
    assert body["mode"] == "review"
    assert body["can_review"] is False
    assert body["config"]["document"]["permissions"]["edit"] is False
    assert body["config"]["document"]["permissions"]["review"] is True
    assert body["config"]["document"]["key"] == key_admin

    # 办公室只读
    st, body = cfg(_login(client, "office1"))
    assert st == 200
    assert body["mode"] == "view"
    assert body["track_changes_forced"] is False
    assert body["config"]["document"]["permissions"]["edit"] is False
    assert body["config"]["document"]["key"] == key_admin

    # 督办只读
    st, body = cfg(_login(client, "supervisor1"))
    assert st == 200
    assert body["mode"] == "view"
    assert body["config"]["document"]["permissions"]["edit"] is False

    # 另一 handler 非本事项承办人 → 非参与人 403
    r = client.post(
        "/api/auth/users",
        headers=ah,
        json={
            "username": "handler_other",
            "password": "Out@123456",
            "display_name": "他大队承办",
            "role": "handler",
            "unit": "治安管理行动大队",
        },
    )
    assert r.status_code == 200
    st, _ = cfg(_login(client, "handler_other", "Out@123456"))
    assert st == 403

    # viewer 即使理论上能看见列表也不给 config
    r = client.post(
        "/api/auth/users",
        headers=ah,
        json={
            "username": "viewer_only",
            "password": "Out@123456",
            "display_name": "只读员",
            "role": "viewer",
        },
    )
    assert r.status_code == 200
    st, _ = cfg(_login(client, "viewer_only", "Out@123456"))
    assert st == 403

    # 终态：可进编辑器但只读
    hh = _login(client, "handler1")
    assert client.post(f"/api/items/{item_id}/submit-a", headers=hh, json={}).status_code == 200
    la = _login(client, "leader_a")
    assert client.post(f"/api/items/{item_id}/approve-a", headers=la, json={}).status_code == 200
    lb = _login(client, "leader_b")
    assert (
        client.post(f"/api/items/{item_id}/finalize", headers=lb, json={"comment": "定"}).status_code
        == 200
    )
    st, body = cfg(hh)
    assert st == 200
    assert body["mode"] == "view"
    assert body["track_changes_forced"] is False
    assert body["config"]["document"]["permissions"]["edit"] is False


def test_editor_display_name_with_unit():
    from app.models import User, UserRole
    from app.services.onlyoffice import editor_display_name

    u = User(
        username="u1",
        password_hash="x",
        display_name="张三",
        role=UserRole.handler,
        unit="治安管理行动大队",
    )
    assert editor_display_name(u) == "张三·治安管理行动大队"
    u.unit = None
    assert editor_display_name(u) == "张三"
