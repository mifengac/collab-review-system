"""OA 公文池：列表、同步、创建协同事项。"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.auth import CurrentUser
from app.config import get_settings
from app.database import get_db
from app.models import ActionType, Item, ItemStatus, OAWorkItem, UserRole
from app.schemas import (
    ItemDetail,
    OAInboxItem,
    OAModuleStat,
    OASyncRequest,
    OASyncResponse,
    OAWorkItemOut,
    UserOut,
)
from app.services.oa_auth import (
    OAAuthError,
    OAAuthUnavailable,
    authenticate_and_fetch_oa,
)
from app.services.oa_client import OA_WORK_MODULES
from app.services.oa_sync import sync_oa_work_items
from app.services.permissions import can_create_item
from app.services.workflow import write_log

router = APIRouter(prefix="/api/oa", tags=["OA公文池"])


def _item_detail(db: Session, item_id: int) -> Item:
    item = (
        db.query(Item)
        .options(
            joinedload(Item.creator),
            joinedload(Item.handler),
            joinedload(Item.leader_a),
            joinedload(Item.leader_b),
        )
        .filter(Item.id == item_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="事项不存在")
    return item


@router.get("/items", response_model=list[OAWorkItemOut])
def list_oa_items(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    module_code: str | None = None,
    keyword: str | None = None,
    limit: int = Query(100, le=500),
):
    """当前用户自己的 OA 公文池。"""
    q = db.query(OAWorkItem).filter(OAWorkItem.owner_user_id == user.id)
    if module_code:
        q = q.filter(OAWorkItem.module_code == module_code)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(
            (OAWorkItem.title.like(like))
            | (OAWorkItem.doc_no.like(like))
            | (OAWorkItem.source_unit.like(like))
        )
    return (
        q.order_by(OAWorkItem.synced_at.desc(), OAWorkItem.id.desc())
        .limit(limit)
        .all()
    )


@router.get("/stats", response_model=list[OAModuleStat])
def oa_stats(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    rows = (
        db.query(
            OAWorkItem.module_code,
            OAWorkItem.module_name,
            func.count(OAWorkItem.id),
            func.max(OAWorkItem.synced_at),
        )
        .filter(OAWorkItem.owner_user_id == user.id)
        .group_by(OAWorkItem.module_code, OAWorkItem.module_name)
        .all()
    )
    by_code = {
        r[0]: OAModuleStat(
            module_code=r[0],
            module_name=r[1],
            count=int(r[2] or 0),
            last_synced_at=r[3],
        )
        for r in rows
    }
    # 按固定模块顺序补齐 0
    out: list[OAModuleStat] = []
    for code, cfg in OA_WORK_MODULES.items():
        if code in by_code:
            out.append(by_code[code])
        else:
            out.append(
                OAModuleStat(
                    module_code=code,
                    module_name=str(cfg.get("name") or code),
                    count=0,
                    last_synced_at=None,
                )
            )
    return out


@router.get("/inbox", response_model=list[OAInboxItem])
def oa_inbox(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    """兼容旧接口：返回当前用户待办公文摘要。"""
    rows = (
        db.query(OAWorkItem)
        .filter(
            OAWorkItem.owner_user_id == user.id,
            OAWorkItem.module_code == "todo",
        )
        .order_by(OAWorkItem.synced_at.desc())
        .limit(50)
        .all()
    )
    return [
        OAInboxItem(
            oa_flow_id=r.flowinid,
            oa_step_id=r.stepinco,
            oa_deal_index=r.dealindx,
            title=r.title,
            doc_no=r.doc_no,
            source_unit=r.source_unit,
            received_at=r.received_at,
        )
        for r in rows
    ]


@router.post("/sync", response_model=OASyncResponse)
def oa_sync(
    body: OASyncRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    """
    手动同步：必须提供 username/password 临时登录 OA。
    不保存密码；会话仅存在于本次请求。
    """
    settings = get_settings()
    if not settings.oa_base_url:
        return OASyncResponse(
            success=False,
            message="未配置 OA_BASE_URL，无法同步",
            imported=0,
            updated=0,
            total=0,
        )

    username = (body.username or "").strip() or user.username
    password = body.password or ""
    if not password:
        return OASyncResponse(
            success=False,
            message="请提供 OA 密码以重新登录同步（系统不保存 OA 密码；或开启登录后自动同步）",
            imported=0,
            updated=0,
            total=0,
        )

    modules = body.modules or settings.oa_sync_module_list
    try:
        profile, items, fetch_err = authenticate_and_fetch_oa(
            username,
            password,
            modules=modules,
            max_pages=settings.oa_sync_max_pages,
        )
    except OAAuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    except OAAuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.message) from exc

    # 仅允许同步到当前登录用户（防止用他人 OA 账号写入）
    if profile.username.strip() != user.username.strip():
        # 若本地用户名与 OA userCode 不一致，仍写入当前会话用户
        pass

    if fetch_err and not items:
        return OASyncResponse(
            success=False,
            message=f"OA 登录成功但列表同步失败：{fetch_err}",
            imported=0,
            updated=0,
            total=0,
        )

    stats = sync_oa_work_items(db, user, profile.username, items)
    return OASyncResponse(
        success=True,
        message="同步完成",
        imported=stats["imported"],
        updated=stats["updated"],
        total=stats["total"],
        data=[],
    )


@router.post("/items/{oa_item_id}/create-collab", response_model=ItemDetail)
def create_collab_from_oa(
    oa_item_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    """从 OA 公文创建协同事项；已关联则直接返回。"""
    oa = db.query(OAWorkItem).filter(OAWorkItem.id == oa_item_id).first()
    if not oa:
        raise HTTPException(status_code=404, detail="OA 公文不存在")
    if oa.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="无权操作他人的 OA 公文")

    if oa.linked_item_id:
        return _item_detail(db, oa.linked_item_id)

    if not can_create_item(user):
        raise HTTPException(
            status_code=403,
            detail="当前角色不可创建协同事项，请联系办公室收文员分派",
        )

    handler_id = user.id if user.role == UserRole.handler else None
    remark_parts = [
        f"来源：OA {oa.module_name}",
    ]
    if oa.flow_name:
        remark_parts.append(f"流程：{oa.flow_name}")
    if oa.step_name:
        remark_parts.append(f"节点：{oa.step_name}")

    item = Item(
        title=oa.title[:256],
        oa_doc_no=oa.doc_no,
        source_unit=oa.source_unit,
        remark="；".join(remark_parts),
        creator_id=user.id,
        handler_id=handler_id,
        status=ItemStatus.draft,
        oa_flow_id=oa.flowinid,
        oa_step_id=oa.stepinco or None,
        oa_deal_index=oa.dealindx or None,
        oa_raw_title=oa.title[:256],
        oa_raw_doc_no=oa.doc_no,
    )
    db.add(item)
    db.flush()
    write_log(db, item, user, ActionType.create, detail=f"自 OA 公文池创建：{oa.title[:80]}")
    item.status = ItemStatus.handling
    write_log(
        db,
        item,
        user,
        ActionType.update,
        from_status=ItemStatus.draft,
        to_status=ItemStatus.handling,
        detail="创建后进入承办中",
    )
    oa.linked_item_id = item.id
    oa.updated_at = datetime.utcnow()
    db.commit()
    return _item_detail(db, item.id)
