"""第 4 轮：批量分派、筛选、定时维护、审计导出。"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="crs_r4_")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp}/test.db")
os.environ.setdefault("UPLOAD_DIR", str(Path(_tmp) / "uploads"))
os.environ.setdefault("SECRET_KEY", "r4-test-secret-key-not-default")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "Admin@123456")
os.environ.setdefault("DEBUG", "true")
os.environ["AUTH_MODE"] = "local"
os.environ["SEED_DEMO_USERS"] = "true"
os.environ["OA_SCHEDULED_SYNC_MINUTES"] = "0"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ActionLog, ActionType, OASyncLog, OAWorkItem  # noqa: E402
from app.services.oa_scheduled import run_scheduled_oa_maintenance  # noqa: E402


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


def _create_item(client, h, users, title: str, **extra) -> int:
    body = {
        "title": title,
        "handler_id": users["handler1"]["id"],
        "leader_a_id": users["leader_a"]["id"],
        "leader_b_id": users["leader_b"]["id"],
        "handler_dept": "治安管理行动大队",
        **extra,
    }
    r = client.post("/api/items", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_batch_assign_success_and_forbidden(client: TestClient):
    ah = _admin(client)
    users = _users(client, ah)
    id1 = _create_item(client, ah, users, "批量分派甲")
    id2 = _create_item(client, ah, users, "批量分派乙")

    # 非办公室 403
    hh = _login(client, "handler1")
    r = client.post(
        "/api/items/batch-assign",
        headers=hh,
        json={"item_ids": [id1], "handler_id": users["handler1"]["id"]},
    )
    assert r.status_code == 403

    oh = _login(client, "office1")
    r = client.post(
        "/api/items/batch-assign",
        headers=oh,
        json={
            "item_ids": [id1, id2],
            "handler_id": users["handler1"]["id"],
            "leader_a_id": users["leader_a"]["id"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] == 2
    assert body["failed"] == []

    # 终态失败条目
    id3 = _create_item(client, ah, users, "将定稿")
    # 推到 B 审并定稿
    assert client.post(f"/api/items/{id3}/submit-a", headers=hh, json={}).status_code == 200
    la = _login(client, "leader_a")
    assert client.post(f"/api/items/{id3}/approve-a", headers=la, json={}).status_code == 200
    lb = _login(client, "leader_b")
    assert (
        client.post(f"/api/items/{id3}/finalize", headers=lb, json={"comment": "定"}).status_code
        == 200
    )
    r = client.post(
        "/api/items/batch-assign",
        headers=oh,
        json={"item_ids": [id3, id1], "handler_id": users["handler1"]["id"]},
    )
    assert r.status_code == 200
    assert r.json()["success"] >= 0
    assert any(f["item_id"] == id3 for f in r.json()["failed"])


def test_list_filters_stage_dept_deadline(client: TestClient):
    ah = _admin(client)
    users = _users(client, ah)
    soon = (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%dT12:00:00")
    later = (datetime.utcnow() + timedelta(days=20)).strftime("%Y-%m-%dT12:00:00")
    id1 = _create_item(
        client,
        ah,
        users,
        "筛选待办",
        handler_dept="信息工作大队",
        deadline=soon,
    )
    id2 = _create_item(
        client,
        ah,
        users,
        "筛选行动",
        handler_dept="治安管理行动大队",
        deadline=later,
    )
    hh = _login(client, "handler1")
    assert client.post(f"/api/items/{id1}/submit-a", headers=hh, json={}).status_code == 200

    r = client.get("/api/items", headers=ah, params={"stage": "pending_a"})
    assert r.status_code == 200
    ids = {x["id"] for x in r.json()}
    assert id1 in ids

    r = client.get(
        "/api/items",
        headers=ah,
        params={"handler_dept": "治安管理行动大队"},
    )
    assert r.status_code == 200
    assert all(x["handler_dept"] == "治安管理行动大队" for x in r.json())
    assert id2 in {x["id"] for x in r.json()}

    r = client.get("/api/items", headers=ah, params={"stage": "not_a_stage"})
    assert r.status_code == 400


def test_scheduled_oa_maintenance_deactivates_stale(client: TestClient):
    ah = _admin(client)
    users = _users(client, ah)
    uid = users["handler1"]["id"]
    db = SessionLocal()
    try:
        old = datetime.utcnow() - timedelta(days=40)
        db.add(
            OASyncLog(
                user_id=uid,
                trigger="login",
                status="success",
                imported=0,
                updated=0,
                total=0,
                created_at=datetime.utcnow(),
                started_at=datetime.utcnow(),
                finished_at=datetime.utcnow(),
            )
        )
        db.add(
            OAWorkItem(
                owner_user_id=uid,
                oa_user_code="handler1",
                module_code="todo",
                module_name="待办",
                flowinid="STALE-1",
                external_key="todo|STALE-1||",
                title="过期未关联",
                is_active=True,
                linked_item_id=None,
                synced_at=old,
            )
        )
        db.add(
            OAWorkItem(
                owner_user_id=uid,
                oa_user_code="handler1",
                module_code="todo",
                module_name="待办",
                flowinid="FRESH-1",
                external_key="todo|FRESH-1||",
                title="仍新鲜",
                is_active=True,
                linked_item_id=None,
                synced_at=datetime.utcnow(),
            )
        )
        db.commit()
        result = run_scheduled_oa_maintenance(db)
        assert result["deactivated"] >= 1
        stale = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.flowinid == "STALE-1")
            .first()
        )
        fresh = (
            db.query(OAWorkItem)
            .filter(OAWorkItem.flowinid == "FRESH-1")
            .first()
        )
        assert stale is not None and stale.is_active is False
        assert fresh is not None and fresh.is_active is True
    finally:
        db.close()


def test_audit_export_csv_and_log(client: TestClient):
    ah = _admin(client)
    users = _users(client, ah)
    _create_item(client, ah, users, "导出用事项")

    # 非 admin 403
    oh = _login(client, "office1")
    r = client.get("/api/audit/export", headers=oh, params={"kind": "action_logs"})
    assert r.status_code == 403

    # 不带日期范围 → 400（必填）
    r = client.get("/api/audit/export", headers=ah, params={"kind": "action_logs"})
    assert r.status_code == 400

    # 跨度超过 366 天 → 400
    r = client.get(
        "/api/audit/export",
        headers=ah,
        params={"kind": "action_logs", "from": "2020-01-01", "to": "2099-12-31"},
    )
    assert r.status_code == 400

    today = datetime.utcnow().strftime("%Y-%m-%d")
    year_start = datetime.utcnow().replace(month=1, day=1).strftime("%Y-%m-%d")
    r = client.get(
        "/api/audit/export",
        headers=ah,
        params={"kind": "action_logs", "from": year_start, "to": today},
    )
    assert r.status_code == 200, r.text
    assert "text/csv" in r.headers.get("content-type", "")
    raw = r.content
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    text = raw.decode("utf-8-sig")
    assert "action" in text.splitlines()[0]
    assert "创建事项" in text or "create" in text.lower() or "创建" in text

    r = client.get(
        "/api/audit/export",
        headers=ah,
        params={"kind": "oa_sync_logs", "from": year_start, "to": today},
    )
    assert r.status_code == 200
    assert r.content.startswith(b"\xef\xbb\xbf")

    # 导出本身有日志
    db = SessionLocal()
    try:
        n = (
            db.query(ActionLog)
            .filter(ActionLog.action == ActionType.export_audit)
            .count()
        )
        assert n >= 1
    finally:
        db.close()


def test_csv_formula_injection_sanitized(client: TestClient):
    """CSV 公式注入防护：危险前缀加单引号；含恶意标题的导出行不以公式开头。"""
    from app.routers.audit import _sanitize_cell

    assert _sanitize_cell("=cmd|'/c calc'!A0") == "'=cmd|'/c calc'!A0"
    assert _sanitize_cell("+SUM(A1)") == "'+SUM(A1)"
    assert _sanitize_cell("-1+1") == "'-1+1"
    assert _sanitize_cell("@evil") == "'@evil"
    assert _sanitize_cell("正常标题") == "正常标题"
    assert _sanitize_cell(123) == 123
    assert _sanitize_cell(None) is None

    # 集成：以 = 开头的日志备注（完整单元格）导出时必须带防护前缀
    ah = _admin(client)
    users = _users(client, ah)
    item_id = _create_item(client, ah, users, "注入防护用事项")
    db = SessionLocal()
    try:
        db.add(
            ActionLog(
                item_id=item_id,
                actor_id=1,
                action=ActionType.comment,
                comment="=HYPERLINK(evil)!A1",
            )
        )
        db.commit()
    finally:
        db.close()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    year_start = datetime.utcnow().replace(month=1, day=1).strftime("%Y-%m-%d")
    r = client.get(
        "/api/audit/export",
        headers=ah,
        params={"kind": "action_logs", "from": year_start, "to": today},
    )
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    assert "'=HYPERLINK(evil)!A1" in text, "以 = 开头的单元格必须带前导单引号"
