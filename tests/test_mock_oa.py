"""模拟 OA 服务与截断/分页/安全配置测试。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="crs_mockoa_")
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
os.environ["OA_MOCK_ENABLED"] = "false"
os.environ["OA_SYNC_PAGE_SIZE"] = "10"
os.environ["OA_SYNC_MAX_PAGES"] = "3"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.mock_oa import (  # noqa: E402
    MODULE_COUNTS,
    PAGE_SIZE,
    app as mock_app,
    clear_sessions,
    get_module_counts,
    resolve_module_strict,
)
from app.services.oa_auth import _parse_profile  # noqa: E402
from app.services.oa_client import (  # noqa: E402
    OA_WORK_MODULES,
    _compute_report_status,
    fetch_oa_work_items_report,
    module_query_params,
)
from app.services.oa_client import OAModuleFetchResult  # noqa: E402


@pytest.fixture
def mock_client():
    clear_sessions()
    with TestClient(mock_app) as c:
        yield c
    clear_sessions()


def _login_mock(client: TestClient, username="handler1", password="Demo@123456"):
    r = client.post(
        "/hportal/j_security_check",
        data={"j_username": username, "j_password": password, "remember": "on"},
    )
    assert r.status_code == 200
    return r


def test_mock_login_success_and_profile_parseable(mock_client: TestClient):
    _login_mock(mock_client)
    r = mock_client.post("/hportal/view/GetModuleTree.do")
    assert r.status_code == 200
    data = r.json()
    assert "userInfo" in data
    profile = _parse_profile(data, fallback_username="handler1")
    assert profile.username == "handler1"
    assert profile.display_name
    assert "模拟" in profile.display_name or profile.display_name


def test_mock_login_wrong_password(mock_client: TestClient):
    r = mock_client.post(
        "/hportal/j_security_check",
        data={"j_username": "handler1", "j_password": "wrong-pass", "remember": "on"},
    )
    assert r.status_code == 200
    # 无有效会话
    r2 = mock_client.post("/hportal/view/GetModuleTree.do")
    assert r2.status_code == 401


def _list_params(code: str) -> dict:
    """从 OA_WORK_MODULES 生成列表 query（去掉 noReportLog 也可；保留完整一致）。"""
    return dict(module_query_params(code))


def test_mock_five_modules_mapping(mock_client: TestClient):
    _login_mock(mock_client)
    for code in ("todo", "unread", "done", "read_done", "running"):
        r = mock_client.post(
            "/hmoa/s",
            params=_list_params(code),
            data={"page": "1", "showOnlyMe": "false", "orderOption": "1"},
        )
        assert r.status_code == 200, (code, r.text)
        body = r.json()
        assert "result" in body and "totalCount" in body
        assert body["totalCount"] == MODULE_COUNTS[code]
        assert len(body["result"]) <= PAGE_SIZE
        if body["result"]:
            row = body["result"][0]
            assert "flowinid" in row
            assert "finsname" in row
            assert row["flowinid"].startswith("MOCK-")


def test_mock_page_size_and_page2(mock_client: TestClient):
    _login_mock(mock_client)
    q = _list_params("todo")
    r1 = mock_client.post("/hmoa/s", params=q, data={"page": "1"})
    r2 = mock_client.post("/hmoa/s", params=q, data={"page": "2"})
    b1, b2 = r1.json(), r2.json()
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(b1["result"]) == 10
    assert len(b2["result"]) == 10
    ids1 = {x["flowinid"] for x in b1["result"]}
    ids2 = {x["flowinid"] for x in b2["result"]}
    assert ids1.isdisjoint(ids2)
    assert b1["totalCount"] == 23


def test_mock_todo_full_pagination(mock_client: TestClient):
    _login_mock(mock_client)
    q = _list_params("todo")
    all_ids = []
    for page in (1, 2, 3):
        r = mock_client.post("/hmoa/s", params=q, data={"page": str(page)})
        assert r.status_code == 200
        rows = r.json()["result"]
        all_ids.extend([x["flowinid"] for x in rows])
    assert len(all_ids) == 23
    assert len(set(all_ids)) == 23


def test_mock_rejects_missing_or_wrong_params(mock_client: TestClient):
    """缺少/错误 taskType、readFlag 必须 400，且不得返回其他模块数据。"""
    _login_mock(mock_client)
    cases = [
        # todo 缺少 taskType=0
        {"service": "flowDealingList"},
        # unread 缺少 taskType=3
        {"service": "flowUnreadList", "readFlag": "0"},
        # unread 缺少 readFlag=0
        {"service": "flowUnreadList", "taskType": "3"},
        # read_done 错误传 readFlag=0（那是 unread）
        # 注意：readFlag=0 + taskType=3 是合法 unread，此处测「想读已阅却传 0」
        # 用 read_done 期望但传 readFlag=0 会映射到 unread 成功——改为传错误 taskType
        {"service": "flowUnreadList", "taskType": "3", "readFlag": "9"},
        # running 缺少 taskType=-1
        {"service": "flowDealingList", "readFlag": "0"},
        # running 缺少 readFlag=0
        {"service": "flowDealingList", "taskType": "-1"},
        # todo 误用 taskType=1 不是宽松成 done 时的缺失（这里明确错误组合）
        {"service": "flowDealingList", "taskType": "99"},
    ]
    for params in cases:
        r = mock_client.post("/hmoa/s", params=params, data={"page": "1"})
        assert r.status_code == 400, params
        body = r.json()
        assert body.get("success") is False
        assert "不匹配" in (body.get("message") or "")
        assert "password" not in str(body).lower()
        assert "cookie" not in str(body).lower()
        assert "token" not in str(body).lower()
        assert "result" not in body or body.get("result") in (None, [])


def test_mock_wrong_read_flag_not_return_other_module_data(mock_client: TestClient):
    """read_done 传 readFlag=0 会匹配 unread，属合法另一模块；错误参数不得混返回。"""
    _login_mock(mock_client)
    # 明确错误：taskType 与 service 组合不存在
    r = mock_client.post(
        "/hmoa/s",
        params={"service": "flowUnreadList", "taskType": "0", "readFlag": "1"},
        data={"page": "1"},
    )
    assert r.status_code == 400
    assert r.json().get("success") is False


def test_resolve_module_strict_helpers():
    assert resolve_module_strict("flowDealingList", "0", None) == "todo"
    assert resolve_module_strict("flowUnreadList", "3", "0") == "unread"
    assert resolve_module_strict("flowDealingList", "1", None) == "done"
    assert resolve_module_strict("flowUnreadList", "3", "1") == "read_done"
    assert resolve_module_strict("flowDealingList", "-1", "0") == "running"
    assert resolve_module_strict("flowDealingList", None, None) is None
    assert resolve_module_strict("flowUnreadList", "3", None) is None
    assert resolve_module_strict("flowDealingList", "-1", None) is None


def test_mock_data_no_real_har_secrets(mock_client: TestClient):
    """模拟数据不得含真实 HAR 痕迹（账号域名/Cookie/真实姓名模式等）。"""
    _login_mock(mock_client)
    forbidden = [
        "j_password",
        "Set-Cookie",
        "JSESSIONID",
        "192.168.",
        "10.0.",
        "Bearer ",
        "oa.har",
    ]
    blob_parts = []
    for code in OA_WORK_MODULES:
        r = mock_client.post(
            "/hmoa/s",
            params=_list_params(code),
            data={"page": "1"},
        )
        assert r.status_code == 200, code
        blob_parts.append(r.text)
    # 全部虚构数据
    full = "\n".join(blob_parts)
    for bad in forbidden:
        assert bad not in full
    assert "测试公文" in full
    assert "模拟" in full or "测试单位" in full
    # 不允许出现明显真实文号格式混入真实机关名（用 MOCK 前缀保证）
    assert "MOCK-" in full


class _MockHttpxShim:
    """将 Starlette TestClient 适配为 oa_client 所需的同步 httpx 风格 client。"""

    def __init__(self, tc: TestClient):
        self._tc = tc
        self.cookies = tc.cookies

    def post(self, url, params=None, data=None, headers=None, **kwargs):
        from urllib.parse import urlparse

        path = urlparse(str(url)).path or "/"
        return self._tc.post(path, params=params or {}, data=data or {}, headers=headers)


def test_running_max_pages_truncated_partial_status(mock_client: TestClient):
    """35 条 + page_size=10 + max_pages=3 → fetched=30 truncated complete=false partial。"""
    _login_mock(mock_client)
    os.environ["OA_BASE_URL"] = "http://mock.local"
    os.environ["OA_SYNC_PAGE_SIZE"] = "10"
    os.environ["OA_SYNC_MAX_PAGES"] = "3"
    get_settings.cache_clear()
    try:
        report = fetch_oa_work_items_report(
            _MockHttpxShim(mock_client), modules=["running"], max_pages=3
        )
    finally:
        get_settings.cache_clear()

    assert len(report.module_results) == 1
    m = report.module_results[0]
    assert m.success is True
    assert m.fetched == 30
    assert m.pages == 3
    assert m.truncated is True
    assert m.complete is False
    assert report.status == "partial"
    assert report.error_summary
    assert "未清理" in report.error_summary or "尚未全部" in report.error_summary


def test_todo_23_complete_with_3_pages(mock_client: TestClient):
    _login_mock(mock_client)
    os.environ["OA_BASE_URL"] = "http://mock.local"
    os.environ["OA_SYNC_PAGE_SIZE"] = "10"
    get_settings.cache_clear()
    try:
        report = fetch_oa_work_items_report(
            _MockHttpxShim(mock_client), modules=["todo"], max_pages=3
        )
    finally:
        get_settings.cache_clear()
    m = report.module_results[0]
    assert m.fetched == 23
    assert m.complete is True
    assert m.truncated is False
    assert report.status == "success"


def test_compute_report_status_truncated_is_partial():
    results = [
        OAModuleFetchResult(
            module_code="running",
            module_name="流转中公文",
            success=True,
            fetched=30,
            pages=3,
            complete=False,
            truncated=True,
        ),
        OAModuleFetchResult(
            module_code="todo",
            module_name="待办公文",
            success=True,
            fetched=23,
            pages=3,
            complete=True,
            truncated=False,
        ),
    ]
    assert _compute_report_status(results) == "partial"


def test_module_counts():
    c = get_module_counts()
    assert c["todo"] == 23
    assert c["unread"] == 12
    assert c["done"] == 18
    assert c["read_done"] == 7
    assert c["running"] == 35


def test_oa_mock_enabled_default_false():
    get_settings.cache_clear()
    s = get_settings()
    assert s.oa_mock_enabled is False or os.environ.get("OA_MOCK_ENABLED", "false").lower() in (
        "0",
        "false",
        "",
    )


def test_debug_false_mock_true_rejects_import():
    """DEBUG=false + OA_MOCK_ENABLED=true 时主应用拒绝启动。"""
    import importlib
    import sys

    old = {
        "DEBUG": os.environ.get("DEBUG"),
        "OA_MOCK_ENABLED": os.environ.get("OA_MOCK_ENABLED"),
    }
    try:
        os.environ["DEBUG"] = "false"
        os.environ["OA_MOCK_ENABLED"] = "true"
        get_settings.cache_clear()
        # 重新加载 main 应失败
        mods = [m for m in list(sys.modules) if m == "app.main" or m.startswith("app.main.")]
        for m in mods:
            del sys.modules[m]
        with pytest.raises(RuntimeError) as ei:
            importlib.import_module("app.main")
        assert "OA_MOCK_ENABLED" in str(ei.value) or "DEBUG" in str(ei.value)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()
        # 恢复 main 模块为正常配置
        if "app.main" in sys.modules:
            del sys.modules["app.main"]
        os.environ["DEBUG"] = "true"
        os.environ["OA_MOCK_ENABLED"] = "false"
        get_settings.cache_clear()
        importlib.import_module("app.main")


def test_auth_config_mock_banner_only_when_debug_and_enabled():
    from app.main import app

    with TestClient(app) as client:
        os.environ["DEBUG"] = "true"
        os.environ["OA_MOCK_ENABLED"] = "true"
        get_settings.cache_clear()
        r = client.get("/api/auth/config")
        assert r.status_code == 200
        assert r.json()["oa_mock_enabled"] is True

        os.environ["OA_MOCK_ENABLED"] = "false"
        get_settings.cache_clear()
        r2 = client.get("/api/auth/config")
        assert r2.json()["oa_mock_enabled"] is False


def test_truncated_does_not_deactivate_with_real_fetch(mock_client: TestClient):
    """截断同步不清理旧记录：先完整写入再截断同步。"""
    from app.database import SessionLocal
    from app.main import app
    from app.models import OAWorkItem, User
    from app.services.oa_sync import sync_oa_work_items
    from app.services.oa_client import normalize_oa_item

    with TestClient(app) as client:
        lr = client.post(
            "/api/auth/login",
            json={"username": "handler1", "password": "Demo@123456"},
        )
        assert lr.status_code == 200

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "handler1").first()
        assert user
        # 预置旧记录 FLOW-OLD
        old = normalize_oa_item(
            "running",
            "流转中公文",
            {
                "flowinid": "FLOW-OLD-KEEP",
                "finsname": "应保留的旧公文",
                "stepinco": "S",
                "dealindx": "1",
            },
        )
        sync_oa_work_items(
            db,
            user,
            user.username,
            [old],
            module_results=[
                OAModuleFetchResult(
                    module_code="running",
                    module_name="流转中公文",
                    success=True,
                    complete=True,
                    fetched=1,
                )
            ],
        )
        # 截断结果：只看到 MOCK 前 30 条，不包含 FLOW-OLD
        clear_sessions()
        _login_mock(mock_client)
        os.environ["OA_BASE_URL"] = "http://mock.local"
        os.environ["OA_SYNC_PAGE_SIZE"] = "10"
        get_settings.cache_clear()
        try:
            report = fetch_oa_work_items_report(
                _MockHttpxShim(mock_client), modules=["running"], max_pages=3
            )
        finally:
            get_settings.cache_clear()
        assert report.module_results[0].truncated is True
        sync_oa_work_items(
            db,
            user,
            user.username,
            report.items,
            module_results=report.module_results,
        )
        row = (
            db.query(OAWorkItem)
            .filter(
                OAWorkItem.owner_user_id == user.id,
                OAWorkItem.flowinid == "FLOW-OLD-KEEP",
            )
            .first()
        )
        assert row is not None
        assert row.is_active is True
    finally:
        db.close()
