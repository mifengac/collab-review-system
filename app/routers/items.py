from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.auth import CurrentUser
from app.database import get_db
from app.models import ActionLog, ActionType, Item, ItemStatus
from app.schemas import (
    ActionLogOut,
    DashboardOut,
    ItemBrief,
    ItemCreate,
    ItemDetail,
    ItemUpdate,
    WorkflowAction,
)
from app.services import workflow
from app.services.permissions import (
    ensure_can_edit_item,
    ensure_can_view_item,
    item_scope_filter,
)
from app.services.workflow import WorkflowError, write_log

router = APIRouter(prefix="/api/items", tags=["协同事项"])


def _get_item(db: Session, item_id: int) -> Item:
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


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    scope = item_scope_filter(user)
    base = db.query(Item)
    if scope is not None:
        base = base.filter(scope)

    # 待办：按角色 + 事项范围
    if user.role.value == "handler":
        todo_q = base.filter(
            Item.handler_id == user.id,
            Item.status.in_(
                [
                    ItemStatus.draft,
                    ItemStatus.handling,
                    ItemStatus.leader_a_rejected,
                    ItemStatus.leader_b_rejected,
                ]
            ),
        )
    elif user.role.value == "leader_a":
        todo_q = base.filter(
            Item.leader_a_id == user.id,
            Item.status == ItemStatus.leader_a_review,
        )
    elif user.role.value == "leader_b":
        todo_q = base.filter(
            Item.leader_b_id == user.id,
            Item.status == ItemStatus.leader_b_review,
        )
    else:
        # admin / 其他：范围内进行中
        todo_q = base.filter(
            Item.status.in_(
                [
                    ItemStatus.draft,
                    ItemStatus.handling,
                    ItemStatus.leader_a_review,
                    ItemStatus.leader_a_rejected,
                    ItemStatus.leader_b_review,
                    ItemStatus.leader_b_rejected,
                ]
            )
        )

    todo = todo_q.order_by(Item.deadline.asc().nullslast(), Item.updated_at.desc()).limit(50).all()
    my_created = (
        db.query(Item)
        .filter(Item.creator_id == user.id)
        .order_by(Item.created_at.desc())
        .limit(50)
        .all()
    )
    soon = datetime.utcnow() + timedelta(days=3)
    overdue_q = base.filter(
        Item.deadline.isnot(None),
        Item.deadline <= soon,
        Item.status.notin_([ItemStatus.finalized, ItemStatus.archived, ItemStatus.cancelled]),
    )
    overdue_soon = overdue_q.order_by(Item.deadline.asc()).limit(50).all()
    return DashboardOut(
        todo=[ItemBrief.model_validate(i) for i in todo],
        my_created=[ItemBrief.model_validate(i) for i in my_created],
        overdue_soon=[ItemBrief.model_validate(i) for i in overdue_soon],
    )


@router.get("", response_model=list[ItemBrief])
def list_items(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    status: ItemStatus | None = None,
    keyword: str | None = None,
    limit: int = Query(100, le=500),
):
    q = db.query(Item)
    scope = item_scope_filter(user)
    if scope is not None:
        q = q.filter(scope)
    if status:
        q = q.filter(Item.status == status)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(
            (Item.title.like(like))
            | (Item.oa_doc_no.like(like))
            | (Item.source_unit.like(like))
        )
    return q.order_by(Item.updated_at.desc()).limit(limit).all()


@router.post("", response_model=ItemDetail)
def create_item(
    body: ItemCreate,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    data = body.model_dump()
    if not data.get("handler_id"):
        data["handler_id"] = user.id
    item = Item(
        **data,
        creator_id=user.id,
        status=ItemStatus.draft,
    )
    db.add(item)
    db.flush()
    write_log(db, item, user, ActionType.create, detail=f"创建事项：{item.title}")
    # 创建后进入承办中
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
    db.commit()
    return _get_item(db, item.id)


@router.get("/{item_id}", response_model=ItemDetail)
def get_item(item_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    item = _get_item(db, item_id)
    ensure_can_view_item(user, item)
    return item


@router.put("/{item_id}", response_model=ItemDetail)
def update_item(
    item_id: int,
    body: ItemUpdate,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    item = _get_item(db, item_id)
    ensure_can_edit_item(user, item)
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(item, k, v)
    write_log(db, item, user, ActionType.update, detail="更新事项信息")
    db.commit()
    return _get_item(db, item_id)


@router.get("/{item_id}/timeline", response_model=list[ActionLogOut])
def timeline(item_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    item = _get_item(db, item_id)
    ensure_can_view_item(user, item)
    logs = (
        db.query(ActionLog)
        .options(joinedload(ActionLog.actor))
        .filter(ActionLog.item_id == item_id)
        .order_by(ActionLog.created_at.asc(), ActionLog.id.asc())
        .all()
    )
    return logs


def _run_wf(fn, db: Session, item: Item, user, body: WorkflowAction):
    try:
        fn(db, item, user, body.comment)
        db.commit()
    except WorkflowError as e:
        db.rollback()
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    return _get_item(db, item.id)


@router.post("/{item_id}/submit-a", response_model=ItemDetail)
def api_submit_a(
    item_id: int,
    body: WorkflowAction,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    item = _get_item(db, item_id)
    ensure_can_view_item(user, item)
    return _run_wf(workflow.submit_to_a, db, item, user, body)


@router.post("/{item_id}/approve-a", response_model=ItemDetail)
def api_approve_a(
    item_id: int,
    body: WorkflowAction,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    item = _get_item(db, item_id)
    ensure_can_view_item(user, item)
    return _run_wf(workflow.approve_a, db, item, user, body)


@router.post("/{item_id}/reject-a", response_model=ItemDetail)
def api_reject_a(
    item_id: int,
    body: WorkflowAction,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    item = _get_item(db, item_id)
    ensure_can_view_item(user, item)
    return _run_wf(workflow.reject_a, db, item, user, body)


@router.post("/{item_id}/finalize", response_model=ItemDetail)
def api_finalize(
    item_id: int,
    body: WorkflowAction,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    item = _get_item(db, item_id)
    ensure_can_view_item(user, item)
    return _run_wf(workflow.finalize_b, db, item, user, body)


@router.post("/{item_id}/reject-b", response_model=ItemDetail)
def api_reject_b(
    item_id: int,
    body: WorkflowAction,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    item = _get_item(db, item_id)
    ensure_can_view_item(user, item)
    return _run_wf(workflow.reject_b, db, item, user, body)


@router.post("/{item_id}/archive", response_model=ItemDetail)
def api_archive(
    item_id: int,
    body: WorkflowAction,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    item = _get_item(db, item_id)
    ensure_can_view_item(user, item)
    return _run_wf(workflow.archive, db, item, user, body)


@router.post("/{item_id}/cancel", response_model=ItemDetail)
def api_cancel(
    item_id: int,
    body: WorkflowAction,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    item = _get_item(db, item_id)
    ensure_can_view_item(user, item)
    return _run_wf(workflow.cancel, db, item, user, body)
