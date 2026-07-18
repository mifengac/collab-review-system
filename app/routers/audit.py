"""审计导出：ActionLog / OASyncLog → CSV（UTF-8 BOM，流式分批）。"""
from __future__ import annotations

import csv
import io
import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload

from app.auth import CurrentUser
from app.database import get_db
from app.models import ActionLog, ActionType, OASyncLog, UserRole

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audit", tags=["审计"])

ExportKind = Literal["action_logs", "oa_sync_logs"]

# 单次导出行数硬上限（配合日期范围限制的兜底）
MAX_EXPORT_ROWS = 50000
# 每批从数据库取出的行数
BATCH_SIZE = 1000
# 日期跨度上限（天）
MAX_RANGE_DAYS = 366


def _parse_dt(s: str | None, name: str) -> datetime | None:
    if not s or not str(s).strip():
        return None
    raw = str(s).strip().replace("Z", "+00:00")
    try:
        # 支持 2026-07-01 或完整 ISO
        if len(raw) == 10:
            return datetime.strptime(raw, "%Y-%m-%d")
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} 时间格式无效") from exc


def _sanitize_cell(v: Any) -> Any:
    """CSV 公式注入防护：以 = + - @ 开头的文本加前导单引号，Excel 视为纯文本。"""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def _iter_csv(
    q,
    headers: list[str],
    row_fn: Callable[[Any], list[Any]],
) -> Iterator[bytes]:
    """流式产出 CSV：BOM → 表头 → 逐批数据行，避免全量载入内存。"""

    def _line(cells: list[Any]) -> bytes:
        buf = io.StringIO()
        csv.writer(buf).writerow([_sanitize_cell(c) for c in cells])
        return buf.getvalue().encode("utf-8")

    yield b"\xef\xbb\xbf"  # UTF-8 BOM，Excel 直接打开不乱码
    yield _line(headers)
    for log in q.yield_per(BATCH_SIZE):
        yield _line(row_fn(log))


@router.get("/export")
def export_audit_csv(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    kind: Annotated[ExportKind, Query(description="action_logs 或 oa_sync_logs")],
    date_from: Annotated[str | None, Query(alias="from")] = None,
    date_to: Annotated[str | None, Query(alias="to")] = None,
):
    """
    管理员导出审计 CSV。
    from/to 必填（日期或 ISO 时间），跨度上限 366 天；to 为日期时含当日全天。
    """
    if user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="仅管理员可导出审计数据")

    dt_from = _parse_dt(date_from, "from")
    dt_to = _parse_dt(date_to, "to")
    if dt_from is None or dt_to is None:
        raise HTTPException(status_code=400, detail="必须提供 from 和 to 日期范围")
    if date_to and len(str(date_to).strip()) == 10:
        # 日期止：含整天
        dt_to = dt_to.replace(hour=23, minute=59, second=59)
    if dt_to < dt_from:
        raise HTTPException(status_code=400, detail="to 不能早于 from")
    if (dt_to - dt_from).days > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400, detail=f"导出跨度不能超过 {MAX_RANGE_DAYS} 天，请分段导出"
        )

    if kind == "action_logs":
        base = db.query(ActionLog).filter(
            ActionLog.created_at >= dt_from, ActionLog.created_at <= dt_to
        )
        count = base.count()
        q = (
            base.options(joinedload(ActionLog.actor))
            .order_by(ActionLog.created_at.asc(), ActionLog.id.asc())
            .limit(MAX_EXPORT_ROWS)
        )
        headers = [
            "id",
            "item_id",
            "actor_id",
            "actor_username",
            "action",
            "comment",
            "detail",
            "from_status",
            "to_status",
            "created_at",
        ]

        def row_fn(log: ActionLog) -> list[Any]:
            actor = log.actor
            return [
                log.id,
                log.item_id if log.item_id is not None else "",
                log.actor_id,
                actor.username if actor else "",
                log.action.value if hasattr(log.action, "value") else log.action,
                log.comment or "",
                log.detail or "",
                log.from_status or "",
                log.to_status or "",
                log.created_at.isoformat(sep=" ") if log.created_at else "",
            ]

        filename = f"action_logs_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    else:
        base = db.query(OASyncLog).filter(
            OASyncLog.created_at >= dt_from, OASyncLog.created_at <= dt_to
        )
        count = base.count()
        q = (
            base.options(joinedload(OASyncLog.user))
            .order_by(OASyncLog.created_at.asc(), OASyncLog.id.asc())
            .limit(MAX_EXPORT_ROWS)
        )
        headers = [
            "id",
            "user_id",
            "username",
            "trigger",
            "status",
            "imported",
            "updated",
            "total",
            "error_summary",
            "started_at",
            "finished_at",
            "created_at",
        ]

        def row_fn(log: OASyncLog) -> list[Any]:
            u = log.user
            return [
                log.id,
                log.user_id,
                u.username if u else "",
                log.trigger,
                log.status,
                log.imported,
                log.updated,
                log.total,
                log.error_summary or "",
                log.started_at.isoformat(sep=" ") if log.started_at else "",
                log.finished_at.isoformat(sep=" ") if log.finished_at else "",
                log.created_at.isoformat(sep=" ") if log.created_at else "",
            ]

        filename = f"oa_sync_logs_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"

    # 导出动作本身记日志（系统级，item_id 可空）；写失败不阻断导出
    try:
        db.add(
            ActionLog(
                item_id=None,
                actor_id=user.id,
                action=ActionType.export_audit,
                detail=f"导出 {kind} 共 {min(count, MAX_EXPORT_ROWS)} 行 from={date_from} to={date_to}",
            )
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("审计导出写日志失败: %s", type(exc).__name__)

    return StreamingResponse(
        _iter_csv(q, headers, row_fn),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
