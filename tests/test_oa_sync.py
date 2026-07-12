"""OA 公文池同步与 create-collab 测试（全部 mock）。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="crs_oasync_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ.setdefault("UPLOAD_DIR", str(Path(_tmp) / "uploads"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "Admin@123456")
os.environ.setdefault("DEBUG", "true")
os.environ["SEED_DEMO_USERS"] = "true"
os.environ["AUTH_MODE"] = "local"
os.environ["OA_SYNC_ON_LOGIN"] = "false"
os.environ["OA_BASE_URL"] = "http://oa.example.invalid"
os.environ["OA_DEFAULT_ROLE"] = "handler"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.main import app  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Item, OAWorkItem, User  # noqa: E402
from app.models import OASyncLog  # noqa: E402
from app.services.oa_auth import OAUserProfile  # noqa: E402
from app.services.oa_client import (  # noqa: E402
    OAFetchReport,
    OAModuleFetchResult,
    normalize_oa_item,
)
from app.services.oa_sync import sync_oa_work_items  # noqa: E402


def _report(items, status="success", error=None, module_results=None):
    if module_results is None:
        # 按条目模块汇总
        by_code = {}
        for it in items:
            by_code.setdefault(it.module_code, []).append(it)
        module_results = []
        for code, lst in by_code.items():
            module_results.append(
                OAModuleFetchResult(
                    module_code=code,
                    module_name=lst[0].module_name,
                    success=True,
                    fetched=len(lst),
                    pages=1,
                    complete=True,
                    truncated=False,
                )
            )
        if not module_results and status == "failed":
            module_results = [
                OAModuleFetchResult(
                    module_code="todo",
                    module_name="待办公文",
                    success=False,
                    error=error or "同步失败",
                )
            ]
    return OAFetchReport(
        items=list(items),
        module_results=module_results,
        status=status,
        error_summary=error,
    )


SAMPLE_RAW = {
    "fileSrc": "市局治安支队",
    "docseq": "公治〔2026〕88号",
    "recedate": "2026-07-01 09:30:00",
    "finiFlag": 0,
    "dealindx": "1",
    "dealMan": "张三",
    "readFlag": 0,
    "stepname": "承办",
    "periname": "张三",
    "flowinid": "FLOW-1001",
    "finsname": "关于加强重点场所检查的通知",
    "worklist_itemex1": "备用标题",
    "stepinco": "STEP-10",
    "hasattach": "1",
    "worklist_itemex3": "来源备用",
    "flowname": "公文流转",
    "sysurge": 1,
    "openDate": "2026-07-01 10:00:00",
}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _restore_local():
    yield
    os.environ["AUTH_MODE"] = "local"
    os.environ["OA_SYNC_ON_LOGIN"] = "false"
    get_settings.cache_clear()


def _login(client, username="admin", password="Admin@123456"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_normalize_oa_item():
    item = normalize_oa_item("todo", "待办公文", SAMPLE_RAW)
    assert item is not None
    assert item.flowinid == "FLOW-1001"
    assert item.title.startswith("关于加强")
    assert item.doc_no == "公治〔2026〕88号"
    assert item.source_unit == "市局治安支队"
    assert item.has_attach is True
    assert item.stepinco == "STEP-10"
    assert item.dealindx == "1"


def test_normalize_skip_missing_flowinid():
    raw = dict(SAMPLE_RAW)
    del raw["flowinid"]
    assert normalize_oa_item("todo", "待办公文", raw) is None


def test_sync_insert_update_no_dup_keep_link(client: TestClient):
    h = _login(client)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "handler1").first()
        assert user
        fi = normalize_oa_item("todo", "待办公文", SAMPLE_RAW)
        assert fi
        r1 = sync_oa_work_items(db, user, user.username, [fi])
        assert r1["imported"] == 1
        assert r1["updated"] == 0

        # 改标题再同步
        raw2 = dict(SAMPLE_RAW)
        raw2["finsname"] = "标题已更新"
        fi2 = normalize_oa_item("todo", "待办公文", raw2)
        r2 = sync_oa_work_items(db, user, user.username, [fi2])
        assert r2["imported"] == 0
        assert r2["updated"] == 1

        rows = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.owner_user_id == user.id, OAWorkItem.flowinid == "FLOW-1001")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].title == "标题已更新"

        # 设置 linked 后再同步不覆盖（创建真实事项以满足 FK）
        item = Item(
            title="占位事项",
            creator_id=user.id,
            handler_id=user.id,
            status="承办中",
        )
        # status 用枚举
        from app.models import ItemStatus

        item.status = ItemStatus.handling
        db.add(item)
        db.flush()
        rows[0].linked_item_id = item.id
        db.commit()
        linked = item.id
        fi3 = normalize_oa_item("todo", "待办公文", SAMPLE_RAW)
        sync_oa_work_items(db, user, user.username, [fi3])
        row = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.owner_user_id == user.id, OAWorkItem.flowinid == "FLOW-1001")
            .first()
        )
        assert row.linked_item_id == linked
    finally:
        db.close()


def test_oa_login_sync_on_login(client: TestClient):
    os.environ["AUTH_MODE"] = "oa"
    os.environ["OA_SYNC_ON_LOGIN"] = "true"
    get_settings.cache_clear()

    profile = OAUserProfile(
        username="oa_sync_user",
        display_name="同步用户",
        unit="办公室",
    )
    fi = normalize_oa_item("todo", "待办公文", SAMPLE_RAW)
    with patch(
        "app.routers.auth.authenticate_and_fetch_oa",
        return_value=(profile, _report([fi], status="success")),
    ):
        r = client.post(
            "/api/auth/login",
            json={"username": "oa_sync_user", "password": "oa-pass"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["oa_sync"]["enabled"] is True
    assert r.json()["oa_sync"]["success"] is True
    assert r.json()["oa_sync"]["total"] >= 1
    assert r.json()["oa_sync"]["status"] == "success"
    assert r.json()["oa_sync"]["log_id"] is not None

    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    items = client.get("/api/oa/items?module_code=todo", headers=h).json()
    assert any(x["flowinid"] == "FLOW-1001" for x in items)


def test_oa_login_sync_fail_still_login(client: TestClient):
    os.environ["AUTH_MODE"] = "oa"
    os.environ["OA_SYNC_ON_LOGIN"] = "true"
    get_settings.cache_clear()

    profile = OAUserProfile(username="oa_sync_fail", display_name="失败同步", unit=None)
    with patch(
        "app.routers.auth.authenticate_and_fetch_oa",
        return_value=(
            profile,
            _report([], status="failed", error="列表接口超时"),
        ),
    ):
        r = client.post(
            "/api/auth/login",
            json={"username": "oa_sync_fail", "password": "x"},
        )
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "oa_sync_fail"
    assert r.json()["oa_sync"]["enabled"] is True
    assert r.json()["oa_sync"]["success"] is False
    assert "超时" in (r.json()["oa_sync"]["error"] or "")


def test_list_only_own_items(client: TestClient):
    # 准备两条不同 owner 的 OA 记录
    db = SessionLocal()
    try:
        u1 = db.query(User).filter(User.username == "handler1").first()
        u2 = db.query(User).filter(User.username == "leader_a").first()
        fi = normalize_oa_item("todo", "待办公文", SAMPLE_RAW)
        sync_oa_work_items(db, u1, u1.username, [fi])
        raw2 = dict(SAMPLE_RAW)
        raw2["flowinid"] = "FLOW-OTHER"
        raw2["finsname"] = "别人的公文"
        sync_oa_work_items(db, u2, u2.username, [normalize_oa_item("todo", "待办公文", raw2)])
    finally:
        db.close()

    h1 = _login(client, "handler1", "Demo@123456")
    list1 = client.get("/api/oa/items", headers=h1).json()
    assert all(x["owner_user_id"] for x in list1)
    assert any(x["flowinid"] == "FLOW-1001" for x in list1)
    assert not any(x["flowinid"] == "FLOW-OTHER" for x in list1)


def test_create_collab_and_reuse(client: TestClient):
    h = _login(client, "handler1", "Demo@123456")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "handler1").first()
        raw = dict(SAMPLE_RAW)
        raw["flowinid"] = "FLOW-CREATE-1"
        raw["finsname"] = "创建协同测试公文"
        sync_oa_work_items(db, user, user.username, [normalize_oa_item("todo", "待办公文", raw)])
        oa = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.owner_user_id == user.id, OAWorkItem.flowinid == "FLOW-CREATE-1")
            .first()
        )
        oa_id = oa.id
    finally:
        db.close()

    r1 = client.post(f"/api/oa/items/{oa_id}/create-collab", headers=h)
    assert r1.status_code == 200, r1.text
    item_id = r1.json()["id"]
    assert r1.json()["title"] == "创建协同测试公文"
    assert r1.json()["oa_flow_id"] == "FLOW-CREATE-1"
    assert r1.json()["status"] == "承办中"

    r2 = client.post(f"/api/oa/items/{oa_id}/create-collab", headers=h)
    assert r2.status_code == 200
    assert r2.json()["id"] == item_id

    # 库中仅一条事项关联
    db = SessionLocal()
    try:
        count = db.query(Item).filter(Item.oa_flow_id == "FLOW-CREATE-1").count()
        assert count == 1
        oa = db.query(OAWorkItem).filter(OAWorkItem.id == oa_id).first()
        assert oa.linked_item_id == item_id
    finally:
        db.close()


def test_cannot_create_others_oa_item(client: TestClient):
    db = SessionLocal()
    try:
        owner = db.query(User).filter(User.username == "handler1").first()
        raw = dict(SAMPLE_RAW)
        raw["flowinid"] = "FLOW-PRIVATE"
        sync_oa_work_items(db, owner, owner.username, [normalize_oa_item("todo", "待办公文", raw)])
        oa_id = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.flowinid == "FLOW-PRIVATE", OAWorkItem.owner_user_id == owner.id)
            .first()
            .id
        )
    finally:
        db.close()

    h = _login(client, "leader_a", "Demo@123456")
    r = client.post(f"/api/oa/items/{oa_id}/create-collab", headers=h)
    assert r.status_code == 403


def test_manual_sync_with_password(client: TestClient):
    os.environ["AUTH_MODE"] = "local"
    get_settings.cache_clear()
    h = _login(client, "handler1", "Demo@123456")
    profile = OAUserProfile(username="handler1", display_name="承办员张三", unit="信息工作大队")
    fi = normalize_oa_item("unread", "待阅公文", {**SAMPLE_RAW, "flowinid": "FLOW-MANUAL"})
    with patch(
        "app.routers.oa.authenticate_and_fetch_oa",
        return_value=(profile, _report([fi], status="success")),
    ) as m:
        r = client.post(
            "/api/oa/sync",
            headers=h,
            json={"username": "handler1", "password": "Demo@123456", "modules": ["unread"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True
        assert r.json()["total"] >= 1
        assert r.json()["status"] == "success"
        assert r.json()["log_id"] is not None
        assert m.called
        call_username = m.call_args.args[0] if m.call_args.args else m.call_args.kwargs.get("username")
        assert call_username == "handler1"

    # 无密码提示
    r2 = client.post("/api/oa/sync", headers=h, json={})
    assert r2.status_code == 200
    assert r2.json()["success"] is False
    assert "密码" in r2.json()["message"]


def test_manual_sync_ignores_body_username_uses_current_user(client: TestClient):
    """请求体传他人 username 时，后端仍用当前登录用户 handler1 调 OA。"""
    os.environ["AUTH_MODE"] = "local"
    get_settings.cache_clear()
    h = _login(client, "handler1", "Demo@123456")
    profile = OAUserProfile(username="handler1", display_name="承办员", unit=None)
    fi = normalize_oa_item(
        "todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-IGNORE-BODY-USER"}
    )
    with patch(
        "app.routers.oa.authenticate_and_fetch_oa",
        return_value=(profile, _report([fi], status="success")),
    ) as m:
        r = client.post(
            "/api/oa/sync",
            headers=h,
            json={
                "username": "other_user",
                "password": "any-password",
                "modules": ["todo"],
            },
        )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    assert m.called
    call_username = m.call_args.args[0] if m.call_args.args else m.call_args.kwargs.get("username")
    assert call_username == "handler1"
    assert call_username != "other_user"


def test_manual_sync_profile_mismatch_403_no_write(client: TestClient):
    """OA 返回的 profile.username 与当前用户不一致时 403，且不写入 OAWorkItem。"""
    os.environ["AUTH_MODE"] = "local"
    get_settings.cache_clear()
    h = _login(client, "handler1", "Demo@123456")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "handler1").first()
        before = (
            db.query(OAWorkItem).filter(OAWorkItem.owner_user_id == user.id).count()
        )
    finally:
        db.close()

    profile = OAUserProfile(username="other_user", display_name="他人", unit=None)
    fi = normalize_oa_item(
        "todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-MISMATCH-403"}
    )
    with patch(
        "app.routers.oa.authenticate_and_fetch_oa",
        return_value=(profile, _report([fi], status="success")),
    ):
        r = client.post(
            "/api/oa/sync",
            headers=h,
            json={"password": "any-password", "modules": ["todo"]},
        )
    assert r.status_code == 403, r.text
    assert "不一致" in r.json()["detail"]
    assert "禁止同步他人公文" in r.json()["detail"]

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "handler1").first()
        after = (
            db.query(OAWorkItem).filter(OAWorkItem.owner_user_id == user.id).count()
        )
        leaked = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.flowinid == "FLOW-MISMATCH-403")
            .count()
        )
        assert after == before
        assert leaked == 0
    finally:
        db.close()


def test_oa_login_sync_db_fail_still_login(client: TestClient):
    """登录后入库失败：仍 200 签发 JWT，oa_sync.success=false，错误为通用文案。"""
    os.environ["AUTH_MODE"] = "oa"
    os.environ["OA_SYNC_ON_LOGIN"] = "true"
    get_settings.cache_clear()

    profile = OAUserProfile(
        username="oa_sync_db_fail",
        display_name="入库失败用户",
        unit="办公室",
    )
    fi = normalize_oa_item(
        "todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-DB-FAIL"}
    )
    sensitive = "SECRET_COOKIE=abc; password=leaked-token-xyz"

    with patch(
        "app.routers.auth.authenticate_and_fetch_oa",
        return_value=(profile, _report([fi], status="success")),
    ), patch(
        "app.routers.auth.sync_oa_work_items",
        side_effect=RuntimeError(sensitive),
    ):
        r = client.post(
            "/api/auth/login",
            json={"username": "oa_sync_db_fail", "password": "oa-pass-secret"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["username"] == "oa_sync_db_fail"
    assert body["access_token"]
    assert body["oa_sync"]["enabled"] is True
    assert body["oa_sync"]["success"] is False
    err = body["oa_sync"]["error"] or ""
    assert err == "OA 登录成功但公文入库失败，请稍后重试或联系管理员"
    assert "SECRET_COOKIE" not in err
    assert "leaked-token" not in err
    assert "oa-pass-secret" not in r.text
    assert sensitive not in r.text


def _five_module_items():
    items = []
    for code, name in [
        ("todo", "待办公文"),
        ("unread", "待阅公文"),
        ("done", "已办公文"),
        ("read_done", "已阅公文"),
        ("running", "流转中公文"),
    ]:
        raw = dict(SAMPLE_RAW)
        raw["flowinid"] = f"FLOW-{code.upper()}"
        raw["finsname"] = f"{name}测试"
        items.append(normalize_oa_item(code, name, raw))
    return items


def test_all_modules_success_log(client: TestClient):
    h = _login(client, "handler1", "Demo@123456")
    profile = OAUserProfile(username="handler1", display_name="承办员", unit=None)
    items = _five_module_items()
    results = [
        OAModuleFetchResult(
            module_code=it.module_code,
            module_name=it.module_name,
            success=True,
            fetched=1,
            pages=1,
            complete=True,
        )
        for it in items
    ]
    with patch(
        "app.routers.oa.authenticate_and_fetch_oa",
        return_value=(
            profile,
            OAFetchReport(items=items, module_results=results, status="success"),
        ),
    ):
        r = client.post(
            "/api/oa/sync",
            headers=h,
            json={"password": "x", "modules": [i.module_code for i in items]},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "success"
    assert r.json()["success"] is True
    assert len(r.json()["module_results"]) == 5
    logs = client.get("/api/oa/sync-logs?limit=5", headers=h).json()
    assert logs
    assert logs[0]["trigger"] == "manual"
    assert logs[0]["status"] == "success"
    assert logs[0]["total"] >= 5


def test_partial_module_failure_keeps_success_data(client: TestClient):
    h = _login(client, "handler1", "Demo@123456")
    profile = OAUserProfile(username="handler1", display_name="承办员", unit=None)
    ok = normalize_oa_item(
        "todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-PARTIAL-OK"}
    )
    results = [
        OAModuleFetchResult(
            module_code="todo",
            module_name="待办公文",
            success=True,
            fetched=1,
            pages=1,
        ),
        OAModuleFetchResult(
            module_code="unread",
            module_name="待阅公文",
            success=False,
            fetched=0,
            pages=0,
            error="OA 会话失效或无权访问该模块",
        ),
    ]
    with patch(
        "app.routers.oa.authenticate_and_fetch_oa",
        return_value=(
            profile,
            OAFetchReport(
                items=[ok],
                module_results=results,
                status="partial",
                error_summary="部分模块同步失败：待阅公文",
            ),
        ),
    ):
        r = client.post(
            "/api/oa/sync",
            headers=h,
            json={"password": "x"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "partial"
    assert r.json()["success"] is True
    assert "部分" in r.json()["message"]
    items = client.get("/api/oa/items?module_code=todo", headers=h).json()
    assert any(x["flowinid"] == "FLOW-PARTIAL-OK" for x in items)


def test_all_modules_failed(client: TestClient):
    h = _login(client, "handler1", "Demo@123456")
    profile = OAUserProfile(username="handler1", display_name="承办员", unit=None)
    results = [
        OAModuleFetchResult(
            module_code=c,
            module_name=n,
            success=False,
            error="OA 列表服务异常",
        )
        for c, n in [
            ("todo", "待办公文"),
            ("unread", "待阅公文"),
            ("done", "已办公文"),
            ("read_done", "已阅公文"),
            ("running", "流转中公文"),
        ]
    ]
    with patch(
        "app.routers.oa.authenticate_and_fetch_oa",
        return_value=(
            profile,
            OAFetchReport(
                items=[],
                module_results=results,
                status="failed",
                error_summary="全部模块同步失败",
            ),
        ),
    ):
        r = client.post("/api/oa/sync", headers=h, json={"password": "x"})
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert r.json()["success"] is False


def test_user_can_only_see_own_sync_logs(client: TestClient):
    # handler1 产生一条
    h1 = _login(client, "handler1", "Demo@123456")
    profile = OAUserProfile(username="handler1", display_name="承办员", unit=None)
    fi = normalize_oa_item("todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-LOG-1"})
    with patch(
        "app.routers.oa.authenticate_and_fetch_oa",
        return_value=(profile, _report([fi])),
    ):
        client.post("/api/oa/sync", headers=h1, json={"password": "x"})

    # leader_a 不应看到 handler1 的记录（自己也没有）
    h2 = _login(client, "leader_a", "Demo@123456")
    logs2 = client.get("/api/oa/sync-logs", headers=h2).json()
    assert all(x["user_id"] for x in logs2)
    # 若 leader 无同步，列表可为空；有的话也只能是自己
    db = SessionLocal()
    try:
        u1 = db.query(User).filter(User.username == "handler1").first()
        for log in logs2:
            assert log["user_id"] != u1.id or False
        # 更明确：leader 的 logs 都不属于 handler1
        assert not any(log["user_id"] == u1.id for log in logs2)
    finally:
        db.close()


def test_admin_can_list_all_sync_logs(client: TestClient):
    ha = _login(client, "admin", "Admin@123456")
    h1 = _login(client, "handler1", "Demo@123456")
    profile = OAUserProfile(username="handler1", display_name="承办员", unit=None)
    fi = normalize_oa_item("todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-ADMIN-LOG"})
    with patch(
        "app.routers.oa.authenticate_and_fetch_oa",
        return_value=(profile, _report([fi])),
    ):
        client.post("/api/oa/sync", headers=h1, json={"password": "x"})
    logs = client.get("/api/oa/sync-logs?limit=50", headers=ha).json()
    assert any(x.get("total", 0) >= 0 for x in logs)
    # 管理员至少能看到 handler1 的记录
    db = SessionLocal()
    try:
        u1 = db.query(User).filter(User.username == "handler1").first()
        assert any(x["user_id"] == u1.id for x in logs)
    finally:
        db.close()


def test_sync_log_has_no_secrets(client: TestClient):
    h = _login(client, "handler1", "Demo@123456")
    profile = OAUserProfile(username="handler1", display_name="承办员", unit=None)
    fi = normalize_oa_item("todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-NO-SECRET"})
    with patch(
        "app.routers.oa.authenticate_and_fetch_oa",
        return_value=(profile, _report([fi])),
    ):
        r = client.post(
            "/api/oa/sync",
            headers=h,
            json={"password": "SuperSecretPwd!99"},
        )
    assert r.status_code == 200
    blob = r.text
    assert "SuperSecretPwd!99" not in blob
    logs = client.get("/api/oa/sync-logs?limit=3", headers=h).json()
    text = str(logs)
    assert "SuperSecretPwd" not in text
    assert "cookie" not in text.lower() or "Cookie" not in text
    # module_results 不应含密码字段
    for log in logs:
        assert "password" not in str(log).lower() or log.get("error_summary") is None or "password" not in (log.get("error_summary") or "").lower()
        mr = str(log.get("module_results") or "")
        assert "SuperSecret" not in mr


def test_login_trigger_and_write_log_fail_still_login(client: TestClient):
    """同步记录写入失败不影响 OA 登录与入库结果。"""
    os.environ["AUTH_MODE"] = "oa"
    os.environ["OA_SYNC_ON_LOGIN"] = "true"
    get_settings.cache_clear()
    profile = OAUserProfile(username="oa_log_write_fail", display_name="记录失败", unit=None)
    fi = normalize_oa_item("todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-LOG-FAIL"})
    with patch(
        "app.routers.auth.authenticate_and_fetch_oa",
        return_value=(profile, _report([fi])),
    ), patch(
        "app.routers.auth.write_oa_sync_log",
        return_value=None,
    ):
        r = client.post(
            "/api/auth/login",
            json={"username": "oa_log_write_fail", "password": "x"},
        )
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "oa_log_write_fail"
    assert r.json()["oa_sync"]["enabled"] is True
    assert r.json()["oa_sync"]["success"] is True
    assert r.json()["oa_sync"]["total"] >= 1
    # log 写入失败时 log_id 可为 null
    assert r.json()["oa_sync"].get("log_id") is None


# ---------- 本轮：is_active 清理 / 事务回滚 / 敏感清洗 ----------

from app.services.oa_client import (  # noqa: E402
    sanitize_raw,
    safe_error_text,
)
from app.database import migrate_schema, engine  # noqa: E402
from sqlalchemy import text, inspect  # noqa: E402
from app.models import ItemStatus  # noqa: E402


def test_sanitize_raw_recursive_and_whitelist():
    raw = {
        "flowinid": "F1",
        "finsname": "标题",
        "password": "TopSecretPwd!",
        "nested": {"cookie": "SESS=abc", "token": "tok-xyz", "ok": 1},
        "list_nest": [{"authorization": "Bearer xxx", "docseq": "1"}],
        "j_password": "nope",
        "access_token": "at",
        "refresh_token": "rt",
    }
    out = sanitize_raw(raw)
    blob = str(out).lower()
    assert "TopSecretPwd" not in str(out)
    assert "password" not in blob
    assert "cookie" not in blob
    assert "token" not in blob
    assert "authorization" not in blob
    assert "bearer" not in blob
    assert out.get("flowinid") == "F1"
    assert out.get("finsname") == "标题"
    # nested 不在白名单，应被丢弃
    assert "nested" not in out
    assert "list_nest" not in out


def test_safe_error_text_unknown_and_sensitive():
    assert "密码" not in safe_error_text(RuntimeError("password=abc"))
    assert safe_error_text(RuntimeError("x")) == "OA 模块同步失败"
    from app.services.oa_auth import OAAuthError

    assert "会话" in safe_error_text(OAAuthError("OA 会话失效，请重新登录"))


def test_deactivate_missing_on_complete_sync(client: TestClient):
    h = _login(client, "handler1", "Demo@123456")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "handler1").first()
        a = normalize_oa_item("todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-A"})
        b = normalize_oa_item("todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-B"})
        mr = [
            OAModuleFetchResult(
                module_code="todo",
                module_name="待办公文",
                success=True,
                fetched=2,
                pages=1,
                complete=True,
            )
        ]
        sync_oa_work_items(db, user, user.username, [a, b], module_results=mr)
        # 第二次只剩 B
        mr2 = [
            OAModuleFetchResult(
                module_code="todo",
                module_name="待办公文",
                success=True,
                fetched=1,
                pages=1,
                complete=True,
            )
        ]
        sync_oa_work_items(db, user, user.username, [b], module_results=mr2)
        row_a = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.owner_user_id == user.id, OAWorkItem.flowinid == "FLOW-A")
            .first()
        )
        row_b = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.owner_user_id == user.id, OAWorkItem.flowinid == "FLOW-B")
            .first()
        )
        assert row_a is not None and row_a.is_active is False
        assert row_b is not None and row_b.is_active is True
    finally:
        db.close()

    items = client.get("/api/oa/items?module_code=todo", headers=h).json()
    ids = {x["flowinid"] for x in items}
    assert "FLOW-B" in ids
    assert "FLOW-A" not in ids


def test_move_todo_to_done(client: TestClient):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "handler1").first()
        todo = normalize_oa_item(
            "todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-MOVE"}
        )
        mr_todo = [
            OAModuleFetchResult(
                module_code="todo",
                module_name="待办公文",
                success=True,
                fetched=1,
                complete=True,
            )
        ]
        sync_oa_work_items(db, user, user.username, [todo], module_results=mr_todo)
        # 移到已办：todo 完整空列表 + done 有数据
        done = normalize_oa_item(
            "done", "已办公文", {**SAMPLE_RAW, "flowinid": "FLOW-MOVE"}
        )
        mrs = [
            OAModuleFetchResult(
                module_code="todo",
                module_name="待办公文",
                success=True,
                fetched=0,
                complete=True,
            ),
            OAModuleFetchResult(
                module_code="done",
                module_name="已办公文",
                success=True,
                fetched=1,
                complete=True,
            ),
        ]
        sync_oa_work_items(db, user, user.username, [done], module_results=mrs)
        t = (
            db.query(OAWorkItem)
            .filter(
                OAWorkItem.owner_user_id == user.id,
                OAWorkItem.module_code == "todo",
                OAWorkItem.flowinid == "FLOW-MOVE",
            )
            .first()
        )
        d = (
            db.query(OAWorkItem)
            .filter(
                OAWorkItem.owner_user_id == user.id,
                OAWorkItem.module_code == "done",
                OAWorkItem.flowinid == "FLOW-MOVE",
            )
            .first()
        )
        assert t is not None and t.is_active is False
        assert d is not None and d.is_active is True
    finally:
        db.close()


def test_inactive_keeps_linked_item(client: TestClient):
    h = _login(client, "handler1", "Demo@123456")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "handler1").first()
        fi = normalize_oa_item(
            "todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-LINKED"}
        )
        mr = [
            OAModuleFetchResult(
                module_code="todo",
                module_name="待办公文",
                success=True,
                complete=True,
                fetched=1,
            )
        ]
        sync_oa_work_items(db, user, user.username, [fi], module_results=mr)
        oa = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.flowinid == "FLOW-LINKED", OAWorkItem.owner_user_id == user.id)
            .first()
        )
        oa_id = oa.id
    finally:
        db.close()

    r = client.post(f"/api/oa/items/{oa_id}/create-collab", headers=h)
    assert r.status_code == 200
    item_id = r.json()["id"]

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "handler1").first()
        # 完整空同步 → inactive
        mr_empty = [
            OAModuleFetchResult(
                module_code="todo",
                module_name="待办公文",
                success=True,
                fetched=0,
                complete=True,
            )
        ]
        sync_oa_work_items(db, user, user.username, [], module_results=mr_empty)
        oa = db.query(OAWorkItem).filter(OAWorkItem.id == oa_id).first()
        assert oa is not None
        assert oa.is_active is False
        assert oa.linked_item_id == item_id
        item = db.query(Item).filter(Item.id == item_id).first()
        assert item is not None
    finally:
        db.close()

    # 事项仍可访问
    assert client.get(f"/api/items/{item_id}", headers=h).status_code == 200


def test_failed_module_does_not_deactivate(client: TestClient):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "handler1").first()
        fi = normalize_oa_item(
            "todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-KEEP"}
        )
        mr_ok = [
            OAModuleFetchResult(
                module_code="todo",
                module_name="待办公文",
                success=True,
                complete=True,
                fetched=1,
            )
        ]
        sync_oa_work_items(db, user, user.username, [fi], module_results=mr_ok)
        # 失败：无任何 items
        mr_fail = [
            OAModuleFetchResult(
                module_code="todo",
                module_name="待办公文",
                success=False,
                complete=False,
                error="OA 列表服务异常",
            )
        ]
        sync_oa_work_items(db, user, user.username, [], module_results=mr_fail)
        row = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.flowinid == "FLOW-KEEP", OAWorkItem.owner_user_id == user.id)
            .first()
        )
        assert row.is_active is True
    finally:
        db.close()


def test_truncated_does_not_deactivate(client: TestClient):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "handler1").first()
        a = normalize_oa_item("todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-T1"})
        b = normalize_oa_item("todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-T2"})
        mr = [
            OAModuleFetchResult(
                module_code="todo",
                module_name="待办公文",
                success=True,
                complete=True,
                fetched=2,
            )
        ]
        sync_oa_work_items(db, user, user.username, [a, b], module_results=mr)
        # 截断：只看到 T1
        mr_tr = [
            OAModuleFetchResult(
                module_code="todo",
                module_name="待办公文",
                success=True,
                complete=False,
                truncated=True,
                fetched=1,
                pages=3,
            )
        ]
        sync_oa_work_items(db, user, user.username, [a], module_results=mr_tr)
        row_b = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.flowinid == "FLOW-T2", OAWorkItem.owner_user_id == user.id)
            .first()
        )
        assert row_b.is_active is True
    finally:
        db.close()


def test_empty_complete_deactivates_all(client: TestClient):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "handler1").first()
        fi = normalize_oa_item(
            "unread", "待阅公文", {**SAMPLE_RAW, "flowinid": "FLOW-EMPTY"}
        )
        mr = [
            OAModuleFetchResult(
                module_code="unread",
                module_name="待阅公文",
                success=True,
                complete=True,
                fetched=1,
            )
        ]
        sync_oa_work_items(db, user, user.username, [fi], module_results=mr)
        mr0 = [
            OAModuleFetchResult(
                module_code="unread",
                module_name="待阅公文",
                success=True,
                complete=True,
                fetched=0,
            )
        ]
        sync_oa_work_items(db, user, user.username, [], module_results=mr0)
        row = (
            db.query(OAWorkItem)
            .filter(
                OAWorkItem.flowinid == "FLOW-EMPTY",
                OAWorkItem.module_code == "unread",
            )
            .first()
        )
        assert row.is_active is False
    finally:
        db.close()


def test_manual_sync_rollback_on_failure(client: TestClient):
    """入库异常时先 rollback，半成品不得落库。"""
    h = _login(client, "handler1", "Demo@123456")
    profile = OAUserProfile(username="handler1", display_name="承办员", unit=None)
    fi = normalize_oa_item(
        "todo", "待办公文", {**SAMPLE_RAW, "flowinid": "FLOW-ROLLBACK"}
    )

    def boom(*args, **kwargs):
        db = args[0]
        # 模拟先写入再失败
        db.add(
            OAWorkItem(
                owner_user_id=kwargs.get("owner").id if False else 1,
                oa_user_code="handler1",
                module_code="todo",
                module_name="待办公文",
                flowinid="FLOW-ROLLBACK-GHOST",
                stepinco="",
                dealindx="",
                external_key="todo|FLOW-ROLLBACK-GHOST||",
                title="ghost",
                is_active=True,
            )
        )
        raise RuntimeError("mid-flight-fail password=ShouldNotSave")

    # 更可控的 side_effect
    def side_effect(db, owner, oa_user_code, fetched_items, module_results=None):
        ghost = OAWorkItem(
            owner_user_id=owner.id,
            oa_user_code=owner.username,
            module_code="todo",
            module_name="待办公文",
            flowinid="FLOW-ROLLBACK-GHOST",
            stepinco="",
            dealindx="",
            external_key="todo|FLOW-ROLLBACK-GHOST||",
            title="ghost",
            is_active=True,
        )
        db.add(ghost)
        raise RuntimeError("mid-flight-fail password=ShouldNotSave")

    with patch(
        "app.routers.oa.authenticate_and_fetch_oa",
        return_value=(profile, _report([fi])),
    ), patch(
        "app.routers.oa.sync_oa_work_items",
        side_effect=side_effect,
    ):
        r = client.post("/api/oa/sync", headers=h, json={"password": "x"})
    assert r.status_code == 200
    assert r.json()["success"] is False
    assert r.json()["status"] == "failed"
    assert "password" not in (r.json().get("message") or "").lower()

    db = SessionLocal()
    try:
        ghost = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.flowinid == "FLOW-ROLLBACK-GHOST")
            .count()
        )
        assert ghost == 0
        # failed log 存在
        logs = (
            db.query(OASyncLog)
            .filter(OASyncLog.status == "failed")
            .order_by(OASyncLog.id.desc())
            .limit(1)
            .all()
        )
        assert logs
        assert "ShouldNotSave" not in (logs[0].error_summary or "")
        # session 仍可用
        assert db.query(User).count() >= 1
    finally:
        db.close()


def test_migrate_adds_is_active_column(client: TestClient):
    """当前库已有 is_active 时 migrate_schema 幂等；真实旧库见 test_migrate_schema.py。"""
    migrate_schema()
    migrate_schema()
    insp = inspect(engine)
    if insp.has_table("oa_work_items"):
        cols = {c["name"] for c in insp.get_columns("oa_work_items")}
        assert "is_active" in cols


def test_sync_log_strict_no_secrets(client: TestClient):
    h = _login(client, "handler1", "Demo@123456")
    profile = OAUserProfile(username="handler1", display_name="承办员", unit=None)
    secret_pwd = "TestPwd_NeverLog_991"
    secret_cookie = "SESSID=abc_test_cookie"
    secret_token = "tok_test_xyz_789"
    # 构造带敏感字段的 raw 经 normalize 应被清洗
    raw = {
        **SAMPLE_RAW,
        "flowinid": "FLOW-SEC",
        "password": secret_pwd,
        "nested_should_drop": {"cookie": secret_cookie, "token": secret_token},
    }
    fi = normalize_oa_item("todo", "待办公文", raw)
    assert secret_pwd not in str(fi.raw)
    assert secret_cookie not in str(fi.raw)
    assert secret_token not in str(fi.raw)

    with patch(
        "app.routers.oa.authenticate_and_fetch_oa",
        return_value=(profile, _report([fi])),
    ):
        r = client.post(
            "/api/oa/sync",
            headers=h,
            json={"password": secret_pwd},
        )
    assert r.status_code == 200
    text = r.text
    assert secret_pwd not in text
    assert secret_cookie not in text
    assert secret_token not in text
    assert "password" not in text.lower() or "不保存" in text  # UI 提示可含「密码」字样但无值

    logs = client.get("/api/oa/sync-logs?limit=3", headers=h).json()
    blob = str(logs)
    assert secret_pwd not in blob
    assert secret_cookie not in blob
    assert secret_token not in blob
    for log in logs:
        assert "password=" not in str(log).lower()
        mr = str(log.get("module_results") or "")
        assert secret_pwd not in mr
        assert secret_cookie not in mr
        assert secret_token not in mr
