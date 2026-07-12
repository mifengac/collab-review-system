"""preview-smoke 逻辑单元测试（不连真实 OA / 不强制 Docker）。"""
from __future__ import annotations

import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

_SMOKE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "preview-smoke.py"
_spec = importlib.util.spec_from_file_location("preview_smoke", _SMOKE_PATH)
smoke = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(smoke)


def _stats_payload():
    return [
        {"module_code": "todo", "count": 23},
        {"module_code": "unread", "count": 12},
        {"module_code": "done", "count": 18},
        {"module_code": "read_done", "count": 7},
        {"module_code": "running", "count": 30},
    ]


def _module_results():
    return [
        {
            "module_code": "todo",
            "module_name": "待办公文",
            "fetched": 23,
            "pages": 3,
            "complete": True,
            "truncated": False,
        },
        {
            "module_code": "unread",
            "module_name": "待阅公文",
            "fetched": 12,
            "pages": 2,
            "complete": True,
            "truncated": False,
        },
        {
            "module_code": "done",
            "module_name": "已办公文",
            "fetched": 18,
            "pages": 2,
            "complete": True,
            "truncated": False,
        },
        {
            "module_code": "read_done",
            "module_name": "已阅公文",
            "fetched": 7,
            "pages": 1,
            "complete": True,
            "truncated": False,
        },
        {
            "module_code": "running",
            "module_name": "流转中公文",
            "fetched": 30,
            "pages": 3,
            "complete": False,
            "truncated": True,
        },
    ]


class _FakeResp:
    def __init__(self, status: int, body: dict | list):
        self.status = status
        self.body = body

    def getcode(self):
        return self.status

    def read(self):
        return json.dumps(self.body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_smoke_success_path_no_token_leak():
    state = {"phase": 0}
    token_value = "super-secret-jwt-token-should-not-print"

    def fake_urlopen(req, timeout=30.0):
        url = req.full_url
        method = req.get_method()
        if url.endswith("/api/health"):
            return _FakeResp(200, {"status": "ok"})
        if url.endswith("/api/auth/config"):
            return _FakeResp(
                200,
                {
                    "auth_mode": "oa",
                    "oa_mock_enabled": True,
                    "oa_sync_on_login": True,
                },
            )
        if url.endswith("/api/auth/login") and method == "POST":
            return _FakeResp(
                200,
                {
                    "access_token": token_value,
                    "user": {"username": "handler1"},
                    "oa_sync": {"status": "partial", "enabled": True},
                },
            )
        if "/api/oa/stats" in url:
            return _FakeResp(200, _stats_payload())
        if "/api/oa/sync-logs" in url:
            return _FakeResp(
                200,
                [
                    {
                        "trigger": "login",
                        "status": "partial",
                        "module_results": _module_results(),
                    }
                ],
            )
        if "/create-collab" in url:
            return _FakeResp(200, {"id": 99, "title": "测试公文"})
        if "/api/oa/items" in url:
            # 第二次列表（幂等复查）应带 linked_item_id
            state["phase"] += 1
            linked = 99 if state["phase"] >= 2 else None
            return _FakeResp(
                200,
                [
                    {
                        "id": 7,
                        "flowinid": "MOCK-TODO-0001",
                        "linked_item_id": linked,
                    }
                ],
            )
        return _FakeResp(404, {"detail": "nope"})

    out = io.StringIO()
    err = io.StringIO()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with redirect_stdout(out), redirect_stderr(err):
            code = smoke.main(["--base-url", "http://127.0.0.1:5010"])
    assert code == 0, err.getvalue() + out.getvalue()
    text = out.getvalue() + err.getvalue()
    assert token_value not in text
    assert "Demo@123456" not in text
    assert "access_token" not in text
    assert "登录成功" in text


def test_smoke_wrong_stats_fails():
    def fake_urlopen(req, timeout=30.0):
        url = req.full_url
        if url.endswith("/api/health"):
            return _FakeResp(200, {"status": "ok"})
        if url.endswith("/api/auth/config"):
            return _FakeResp(
                200,
                {
                    "auth_mode": "oa",
                    "oa_mock_enabled": True,
                    "oa_sync_on_login": True,
                },
            )
        if url.endswith("/api/auth/login"):
            return _FakeResp(
                200,
                {
                    "access_token": "t",
                    "user": {"username": "handler1"},
                    "oa_sync": {"status": "partial"},
                },
            )
        if "/api/oa/stats" in url:
            bad = _stats_payload()
            bad[0]["count"] = 1
            return _FakeResp(200, bad)
        return _FakeResp(404, {})

    err = io.StringIO()
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = smoke.main(["--base-url", "http://127.0.0.1:5010"])
    assert code == 1
    msg = err.getvalue()
    assert "todo" in msg or "数量" in msg
    assert "Demo@123456" not in msg
    assert "access_token" not in msg


def test_default_base_url_from_env(monkeypatch):
    monkeypatch.setenv("PREVIEW_PORT", "5020")
    assert smoke.default_base_url() == "http://127.0.0.1:5020"
    monkeypatch.delenv("PREVIEW_PORT", raising=False)
    assert smoke.default_base_url() == "http://127.0.0.1:5010"
