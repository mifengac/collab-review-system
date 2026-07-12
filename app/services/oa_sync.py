"""OA 公文池入库同步。"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import OAWorkItem, User
from app.services.oa_client import OAFetchedItem


def sync_oa_work_items(
    db: Session,
    owner: User,
    oa_user_code: str,
    fetched_items: list[OAFetchedItem],
) -> dict:
    """
    按唯一键 upsert。不覆盖 linked_item_id。
    返回 imported / updated / total。
    """
    imported = 0
    updated = 0
    now = datetime.utcnow()
    oa_code = (oa_user_code or owner.username or "").strip()

    for fi in fetched_items:
        stepinco = fi.stepinco or ""
        dealindx = fi.dealindx or ""
        ext = fi.external_key
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
            # 不覆盖 linked_item_id
            updated += 1
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
                    synced_at=now,
                )
            )
            imported += 1

    db.commit()
    return {
        "imported": imported,
        "updated": updated,
        "total": imported + updated,
    }
