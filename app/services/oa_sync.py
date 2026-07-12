"""OA 公文池入库同步与同步记录。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import OASyncLog, OAWorkItem, User
from app.services.oa_client import (
    OAFetchedItem,
    OAModuleFetchResult,
    safe_error_text,
    sanitize_value,
)

logger = logging.getLogger(__name__)


def sync_oa_work_items(
    db: Session,
    owner: User,
    oa_user_code: str,
    fetched_items: list[OAFetchedItem],
    module_results: list[OAModuleFetchResult] | None = None,
) -> dict[str, Any]:
    """
    按唯一键 upsert；is_active=true。
    仅对 complete=true 且 success=true 的模块，将本轮未出现的旧记录 is_active=false。
    不覆盖 linked_item_id；不物理删除。
    返回 imported / updated / total / deactivated / by_module。
    """
    imported = 0
    updated = 0
    deactivated = 0
    by_module: dict[str, dict[str, int]] = {}
    now = datetime.utcnow()
    oa_code = (oa_user_code or owner.username or "").strip()

    # 本轮各模块出现的 external_key
    seen_by_module: dict[str, set[str]] = {}

    for fi in fetched_items:
        stepinco = fi.stepinco or ""
        dealindx = fi.dealindx or ""
        ext = fi.external_key
        code = fi.module_code
        if code not in by_module:
            by_module[code] = {"imported": 0, "updated": 0, "deactivated": 0}
        seen_by_module.setdefault(code, set()).add(ext)

        existing = (
            db.query(OAWorkItem)
            .filter(
                OAWorkItem.owner_user_id == owner.id,
                OAWorkItem.external_key == ext,
            )
            .first()
        )
        if not existing:
            existing = (
                db.query(OAWorkItem)
                .filter(
                    OAWorkItem.owner_user_id == owner.id,
                    OAWorkItem.module_code == fi.module_code,
                    OAWorkItem.flowinid == fi.flowinid,
                    OAWorkItem.stepinco == stepinco,
                    OAWorkItem.dealindx == dealindx,
                )
                .first()
            )

        raw_json = json.dumps(fi.raw, ensure_ascii=False) if fi.raw else None
        if existing:
            existing.oa_user_code = oa_code
            existing.module_name = fi.module_name
            existing.title = fi.title
            existing.doc_no = fi.doc_no
            existing.source_unit = fi.source_unit
            existing.flow_name = fi.flow_name
            existing.step_name = fi.step_name
            existing.handler_name = fi.handler_name
            existing.received_at = fi.received_at
            existing.open_date = fi.open_date
            existing.has_attach = fi.has_attach
            existing.read_flag = fi.read_flag
            existing.fini_flag = fi.fini_flag
            existing.urgency = fi.urgency
            existing.raw_json = raw_json
            existing.synced_at = now
            existing.updated_at = now
            existing.is_active = True
            # 不覆盖 linked_item_id
            updated += 1
            by_module[code]["updated"] += 1
        else:
            db.add(
                OAWorkItem(
                    owner_user_id=owner.id,
                    oa_user_code=oa_code,
                    module_code=fi.module_code,
                    module_name=fi.module_name,
                    flowinid=fi.flowinid,
                    stepinco=stepinco,
                    dealindx=dealindx,
                    external_key=ext,
                    title=fi.title,
                    doc_no=fi.doc_no,
                    source_unit=fi.source_unit,
                    flow_name=fi.flow_name,
                    step_name=fi.step_name,
                    handler_name=fi.handler_name,
                    received_at=fi.received_at,
                    open_date=fi.open_date,
                    has_attach=fi.has_attach,
                    read_flag=fi.read_flag,
                    fini_flag=fi.fini_flag,
                    urgency=fi.urgency,
                    raw_json=raw_json,
                    linked_item_id=None,
                    is_active=True,
                    synced_at=now,
                )
            )
            imported += 1
            by_module[code]["imported"] += 1

    # 仅完整成功的模块可停用本轮未出现的旧记录
    complete_modules: set[str] = set()
    if module_results:
        for m in module_results:
            if m.success and m.complete and not m.truncated:
                complete_modules.add(m.module_code)
                seen_by_module.setdefault(m.module_code, set())

    for code in complete_modules:
        seen = seen_by_module.get(code) or set()
        q = (
            db.query(OAWorkItem)
            .filter(
                OAWorkItem.owner_user_id == owner.id,
                OAWorkItem.module_code == code,
                OAWorkItem.is_active.is_(True),
            )
        )
        stale = q.all()
        for row in stale:
            if row.external_key not in seen:
                row.is_active = False
                row.updated_at = now
                # 保留 linked_item_id
                deactivated += 1
                by_module.setdefault(code, {"imported": 0, "updated": 0, "deactivated": 0})
                by_module[code]["deactivated"] = by_module[code].get("deactivated", 0) + 1

    db.commit()
    return {
        "imported": imported,
        "updated": updated,
        "deactivated": deactivated,
        "total": imported + updated,
        "by_module": by_module,
    }


def merge_module_import_stats(
    module_results: list[OAModuleFetchResult],
    by_module: dict[str, dict[str, int]],
) -> list[dict[str, Any]]:
    """把入库统计合并进模块结果。"""
    out: list[dict[str, Any]] = []
    for m in module_results:
        d = m.to_dict()
        stats = by_module.get(m.module_code) or {}
        d["imported"] = int(stats.get("imported") or 0)
        d["updated"] = int(stats.get("updated") or 0)
        d["deactivated"] = int(stats.get("deactivated") or 0)
        out.append(d)
    return out


def _safe_module_result_dict(m: dict[str, Any] | OAModuleFetchResult) -> dict[str, Any]:
    if isinstance(m, OAModuleFetchResult):
        d = m.to_dict()
    else:
        d = {
            "module_code": m.get("module_code"),
            "module_name": m.get("module_name"),
            "success": bool(m.get("success")),
            "fetched": int(m.get("fetched") or 0),
            "pages": int(m.get("pages") or 0),
            "imported": int(m.get("imported") or 0),
            "updated": int(m.get("updated") or 0),
            "deactivated": int(m.get("deactivated") or 0),
            "complete": bool(m.get("complete")),
            "truncated": bool(m.get("truncated")),
            "error": safe_error_text(m.get("error"), default="OA 模块同步失败")
            if m.get("error")
            else None,
        }
    # 再 scrub 一遍
    if d.get("error"):
        d["error"] = safe_error_text(d["error"], default="OA 模块同步失败")
    return d


def write_oa_sync_log(
    db: Session,
    *,
    user_id: int,
    trigger: str,
    status: str,
    imported: int,
    updated: int,
    total: int,
    module_results: list[dict[str, Any]] | list[OAModuleFetchResult],
    error_summary: str | None,
    started_at: datetime,
    finished_at: datetime | None = None,
) -> OASyncLog | None:
    """
    写入同步诊断记录。失败时吞掉异常并 rollback，不影响主流程。
    严禁写入密码、cookie、token、原始响应。
    调用前应确保主业务事务已 commit/rollback，避免误提交残留对象。
    """
    try:
        results_dicts = [_safe_module_result_dict(m) for m in module_results]
        summary = None
        if error_summary:
            summary = safe_error_text(error_summary, default="同步过程出现错误")

        # 使用独立事务感：先 flush 前的脏对象不应被提交——调用方须先 rollback
        log = OASyncLog(
            user_id=user_id,
            trigger=trigger if trigger in ("login", "manual") else "manual",
            status=status if status in ("success", "partial", "failed") else "failed",
            imported=imported,
            updated=updated,
            total=total,
            module_results_json=json.dumps(
                sanitize_value(results_dicts), ensure_ascii=False
            ),
            error_summary=summary,
            started_at=started_at,
            finished_at=finished_at or datetime.utcnow(),
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        return log
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("write OASyncLog failed: %s", type(exc).__name__)
        return None
