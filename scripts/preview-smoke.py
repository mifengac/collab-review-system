#!/usr/bin/env python3
"""
Docker 预览环境自动冒烟验收（仅标准库）。

用法：
  python scripts/preview-smoke.py --base-url http://127.0.0.1:5010
  PREVIEW_PORT=5010 python scripts/preview-smoke.py

安全：不打印 access_token、密码、Cookie、完整环境变量。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

# 公开演示账号（与 SEED_DEMO_USERS / mock_oa 一致；仅脚本内部使用，不打印）
_SMOKE_USER = "handler1"
_SMOKE_PASS = "Demo@123456"

EXPECTED_STATS = {
    "todo": 23,
    "unread": 12,
    "done": 18,
    "read_done": 7,
    "running": 30,  # 35 条在 max_pages=3 时截断为 30
}

EXPECTED_MODULE_FETCH = {
    "todo": {"fetched": 23, "complete": True, "truncated": False},
    "unread": {"fetched": 12, "complete": True, "truncated": False},
    "done": {"fetched": 18, "complete": True, "truncated": False},
    "read_done": {"fetched": 7, "complete": True, "truncated": False},
    "running": {"fetched": 30, "complete": False, "truncated": True, "pages": 3},
}


class SmokeError(Exception):
    """冒烟失败（消息不得含敏感信息）。"""


def _safe_headers(token: str | None = None) -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def http_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers=_safe_headers(token),
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except urllib.error.URLError as exc:
        raise SmokeError(f"无法连接预览服务: {url}") from exc

    # 响应体不得原样打印；解析失败给通用错误
    try:
        parsed = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError as exc:
        raise SmokeError(f"接口返回非 JSON（HTTP {status}）: {url}") from exc
    return status, parsed


def assert_status(status: int, expect: int, what: str) -> None:
    if status != expect:
        raise SmokeError(f"{what} 期望 HTTP {expect}，实际 {status}")


def check_health(base: str) -> None:
    status, data = http_json("GET", f"{base}/api/health")
    assert_status(status, 200, "健康检查")
    if not isinstance(data, dict) or data.get("status") != "ok":
        raise SmokeError("健康检查响应异常")


def check_auth_config(base: str) -> None:
    status, data = http_json("GET", f"{base}/api/auth/config")
    assert_status(status, 200, "认证配置")
    if not isinstance(data, dict):
        raise SmokeError("认证配置响应异常")
    if data.get("auth_mode") != "oa":
        raise SmokeError(f"auth_mode 期望 oa，实际 {data.get('auth_mode')!r}")
    if data.get("oa_mock_enabled") is not True:
        raise SmokeError("oa_mock_enabled 期望 true")
    if data.get("oa_sync_on_login") is not True:
        raise SmokeError("oa_sync_on_login 期望 true")


def login(base: str) -> tuple[str, dict[str, Any]]:
    status, data = http_json(
        "POST",
        f"{base}/api/auth/login",
        body={"username": _SMOKE_USER, "password": _SMOKE_PASS},
    )
    assert_status(status, 200, "登录")
    if not isinstance(data, dict):
        raise SmokeError("登录响应异常")
    token = data.get("access_token")
    if not token or not isinstance(token, str):
        raise SmokeError("登录未返回 access_token")
    user = data.get("user") or {}
    if user.get("username") != _SMOKE_USER:
        raise SmokeError("登录用户名不匹配")
    oa_sync = data.get("oa_sync") or {}
    if oa_sync.get("status") != "partial":
        raise SmokeError(
            f"oa_sync.status 期望 partial，实际 {oa_sync.get('status')!r}"
        )
    return token, data


def check_stats(base: str, token: str) -> dict[str, int]:
    status, data = http_json("GET", f"{base}/api/oa/stats", token=token)
    assert_status(status, 200, "公文统计")
    if not isinstance(data, list):
        raise SmokeError("stats 响应应为列表")
    counts: dict[str, int] = {}
    for row in data:
        if isinstance(row, dict) and row.get("module_code"):
            counts[str(row["module_code"])] = int(row.get("count") or 0)
    for code, expect in EXPECTED_STATS.items():
        actual = counts.get(code)
        if actual != expect:
            raise SmokeError(
                f"模块 {code} 数量期望 {expect}，实际 {actual}"
            )
    return counts


def check_sync_log(base: str, token: str) -> dict[str, Any]:
    status, data = http_json(
        "GET", f"{base}/api/oa/sync-logs?limit=1", token=token
    )
    assert_status(status, 200, "同步记录")
    if not isinstance(data, list) or not data:
        raise SmokeError("无同步记录")
    log = data[0]
    if not isinstance(log, dict):
        raise SmokeError("同步记录格式异常")
    if log.get("trigger") != "login":
        raise SmokeError(f"trigger 期望 login，实际 {log.get('trigger')!r}")
    if log.get("status") != "partial":
        raise SmokeError(f"sync-log status 期望 partial，实际 {log.get('status')!r}")

    results = log.get("module_results") or []
    by_code: dict[str, dict[str, Any]] = {}
    for m in results:
        if isinstance(m, dict) and m.get("module_code"):
            by_code[str(m["module_code"])] = m

    for code, exp in EXPECTED_MODULE_FETCH.items():
        m = by_code.get(code)
        if not m:
            raise SmokeError(f"同步记录缺少模块 {code}")
        if int(m.get("fetched") or 0) != exp["fetched"]:
            raise SmokeError(
                f"{code}.fetched 期望 {exp['fetched']}，实际 {m.get('fetched')}"
            )
        if bool(m.get("complete")) != exp["complete"]:
            raise SmokeError(
                f"{code}.complete 期望 {exp['complete']}，实际 {m.get('complete')}"
            )
        if bool(m.get("truncated")) != exp["truncated"]:
            raise SmokeError(
                f"{code}.truncated 期望 {exp['truncated']}，实际 {m.get('truncated')}"
            )
        if "pages" in exp and int(m.get("pages") or 0) != exp["pages"]:
            raise SmokeError(
                f"{code}.pages 期望 {exp['pages']}，实际 {m.get('pages')}"
            )
    return log


def check_items_mock_prefix(base: str, token: str) -> list[dict[str, Any]]:
    status, data = http_json(
        "GET", f"{base}/api/oa/items?module_code=todo&limit=50", token=token
    )
    assert_status(status, 200, "公文列表")
    if not isinstance(data, list) or not data:
        raise SmokeError("todo 公文列表为空")
    for row in data:
        fid = str((row or {}).get("flowinid") or "")
        if not fid.startswith("MOCK-"):
            raise SmokeError("公文 flowinid 未使用 MOCK- 前缀（疑似非模拟数据）")
    return data


def check_create_collab_idempotent(
    base: str, token: str, oa_id: int
) -> tuple[int, int]:
    status1, item1 = http_json(
        "POST",
        f"{base}/api/oa/items/{oa_id}/create-collab",
        body={},
        token=token,
    )
    assert_status(status1, 200, "创建协同事项")
    if not isinstance(item1, dict) or not item1.get("id"):
        raise SmokeError("create-collab 未返回事项 id")
    item_id = int(item1["id"])

    status2, item2 = http_json(
        "POST",
        f"{base}/api/oa/items/{oa_id}/create-collab",
        body={},
        token=token,
    )
    assert_status(status2, 200, "再次创建协同事项")
    if not isinstance(item2, dict) or int(item2.get("id") or 0) != item_id:
        raise SmokeError("create-collab 未幂等，重复创建了新事项")

    # 列表中应已有 linked_item_id
    status, items = http_json(
        "GET", f"{base}/api/oa/items?module_code=todo&limit=200", token=token
    )
    assert_status(status, 200, "复查公文列表")
    linked = None
    if isinstance(items, list):
        for row in items:
            if int((row or {}).get("id") or 0) == oa_id:
                linked = (row or {}).get("linked_item_id")
                break
    if linked is None or int(linked) != item_id:
        raise SmokeError("create-collab 后 linked_item_id 未正确写入")
    return oa_id, item_id


def run_smoke(base_url: str) -> dict[str, Any]:
    base = base_url.rstrip("/")
    check_health(base)
    check_auth_config(base)
    token, _login_body = login(base)
    # 立刻丢弃 token 的字符串引用前先完成校验；不打印
    counts = check_stats(base, token)
    log = check_sync_log(base, token)
    items = check_items_mock_prefix(base, token)
    oa_id = int(items[0]["id"])
    oa_id, item_id = check_create_collab_idempotent(base, token, oa_id)
    # 清除 token 引用
    token = ""
    del token

    truncated = [
        m.get("module_name") or m.get("module_code")
        for m in (log.get("module_results") or [])
        if isinstance(m, dict) and m.get("truncated")
    ]
    return {
        "counts": counts,
        "truncated": truncated,
        "oa_id": oa_id,
        "item_id": item_id,
    }


def default_base_url() -> str:
    port = os.environ.get("PREVIEW_PORT", "5010").strip() or "5010"
    return f"http://127.0.0.1:{port}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预览环境冒烟测试")
    parser.add_argument(
        "--base-url",
        default=None,
        help="预览基地址，默认根据 PREVIEW_PORT 生成",
    )
    args = parser.parse_args(argv)
    base = (args.base_url or default_base_url()).rstrip("/")

    try:
        result = run_smoke(base)
    except SmokeError as exc:
        print(f"[preview-smoke] 失败: {exc}", file=sys.stderr)
        return 1
    except Exception:
        # 不输出异常细节（可能含 URL 查询串等）
        print("[preview-smoke] 失败: 冒烟过程发生未预期错误", file=sys.stderr)
        return 1

    counts = result["counts"]
    print("[preview-smoke] 登录成功（handler1 / 模拟 OA）")
    print(
        "[preview-smoke] 五类数量: "
        f"todo={counts.get('todo')} unread={counts.get('unread')} "
        f"done={counts.get('done')} read_done={counts.get('read_done')} "
        f"running={counts.get('running')}"
    )
    trunc = result["truncated"] or ["（无）"]
    print(f"[preview-smoke] 截断模块: {', '.join(str(x) for x in trunc)}")
    print(
        f"[preview-smoke] create-collab 幂等成功 "
        f"(oa_id={result['oa_id']} → item_id={result['item_id']})"
    )
    print("[preview-smoke] 全部检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
