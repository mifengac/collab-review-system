"""
OA 公文池定时维护（有明确能力边界）。

限制（务必对用户说明）：
- 本系统不保存 OA 密码，定时任务**不能**重新登录 OA 拉新列表。
- 仅对「近 N 天有同步记录」的用户，在本地库做过期未关联公文的下线归并。
- 要拿到最新公文，仍须用户登录或手动同步（带密码）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import distinct
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import OASyncLog, OAWorkItem

logger = logging.getLogger(__name__)


def run_scheduled_oa_maintenance(db: Session) -> dict:
    """
    本地公文池整理：
    - 选取近 oa_scheduled_sync_user_days 天内有 OASyncLog 的用户；
    - 将其「未关联事项、仍 active、synced_at 超过 oa_stale_days 天」的公文标为 inactive。
    不访问 OA 网络。
    """
    settings = get_settings()
    started = datetime.utcnow()
    user_days = max(1, int(settings.oa_scheduled_sync_user_days or 7))
    stale_days = max(1, int(settings.oa_stale_days or 30))
    since_user = started - timedelta(days=user_days)
    stale_before = started - timedelta(days=stale_days)

    user_ids = [
        row[0]
        for row in db.query(distinct(OASyncLog.user_id))
        .filter(OASyncLog.created_at >= since_user)
        .all()
    ]

    deactivated = 0
    for uid in user_ids:
        rows = (
            db.query(OAWorkItem)
            .filter(
                OAWorkItem.owner_user_id == uid,
                OAWorkItem.is_active.is_(True),
                OAWorkItem.linked_item_id.is_(None),
                OAWorkItem.synced_at < stale_before,
            )
            .all()
        )
        for row in rows:
            row.is_active = False
            deactivated += 1

    # 写一条系统级说明日志（挂在首个用户上；无用户则只打服务日志）
    summary = (
        f"定时本地维护完成：用户数={len(user_ids)}，下线过期未关联公文={deactivated}。"
        f"未访问 OA（无密码不可重登）。"
    )
    if user_ids:
        log = OASyncLog(
            user_id=user_ids[0],
            trigger="scheduled",
            status="success",
            imported=0,
            updated=0,
            total=deactivated,
            module_results_json="[]",
            error_summary=summary[:512],
            started_at=started,
            finished_at=datetime.utcnow(),
        )
        db.add(log)

    db.commit()
    logger.info(
        "OA scheduled maintenance users=%s deactivated=%s",
        len(user_ids),
        deactivated,
    )
    return {
        "users": len(user_ids),
        "deactivated": deactivated,
        "message": summary,
    }
