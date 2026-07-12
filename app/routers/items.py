from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from app.auth import CurrentUser
from app.database import get_db
from app.models import ActionLog, ActionType, Item, ItemStatus, User, UserRole
from app.schemas import (
    ActionLogOut,
    DashboardOut,
    DashboardStats,
    ItemAssign,
    ItemBrief,
    ItemCreate,
    ItemDetail,
    ItemUpdate,
    SuperviseAction,
    WorkflowAction,
)
from app.services import workflow
from app.services.permissions import (
    ensure_can_assign_item,
    ensure_can_edit_item,
    ensure_can_supervise_item,
    ensure_can_view_item,
    item_scope_filter,
)
from app.services.workflow import WorkflowError, write_log

router = APIRouter(prefix="/api/items", tags=["协同事项"])

IN_PROGRESS = [
    ItemStatus.draft,
    ItemStatus.handling,
    ItemStatus.leader_a_review,
    ItemStatus.leader_a_rejected,
    ItemStatus.leader_b_review,
    ItemStatus.leader_b_rejected,
]


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


def _user_label(db: Session, user_id: int | None) -> str:
    if not user_id:
        return "（空）"
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        return f"#{user_id}"
    return f"{u.display_name}({u.username})"


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    scope = item_scope_filter(user)
    base = db.query(Item)
    if scope is not None:
        base = base.filter(scope)

    # 待办：按角色 + 事项范围
    if user.role == UserRole.handler:
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
    elif user.role == UserRole.leader_a:
        todo_q = base.filter(
            Item.leader_a_id == user.id,
            Item.status == ItemStatus.leader_a_review,
        )
    elif user.role == UserRole.leader_b:
        todo_q = base.filter(
            Item.leader_b_id == user.id,
            Item.status == ItemStatus.leader_b_review,
        )
    elif user.role in (UserRole.office_clerk, UserRole.supervisor, UserRole.admin):
        # 办公室/督办/管理员：全部进行中待关注
        todo_q = base.filter(Item.status.in_(IN_PROGRESS))
    else:
        todo_q = base.filter(Item.status.in_(IN_PROGRESS))

    todo = todo_q.order_by(Item.deadline.asc().nullslast(), Item.updated_at.desc()).limit(50).all()
    my_created = (
        db.query(Item)
        .filter(Item.creator_id == user.id)
        .order_by(Item.created_at.desc())
        .limit(50)
        .all()
    )

    now = datetime.utcnow()
    soon = now + timedelta(days=3)
    open_status = base.filter(Item.status.notin_([ItemStatus.finalized, ItemStatus.archived, ItemStatus.cancelled]))

    overdue_soon_list = (
        open_status.filter(
            Item.deadline.isnot(None),
            Item.deadline <= soon,
            Item.deadline >= now,
        )
        .order_by(Item.deadline.asc())
        .limit(50)
        .all()
    )
    # 兼容：即将逾期含 3 日内（含已过期一点也列在列表里更实用）——按需求列表仍展示 soon 内未终态
    overdue_soon_list = (
        open_status.filter(Item.deadline.isnot(None), Item.deadline <= soon)
        .order_by(Item.deadline.asc())
        .limit(50)
        .all()
    )

    in_progress_count = base.filter(Item.status.in_(IN_PROGRESS)).count()
    overdue_soon_count = (
        open_status.filter(Item.deadline.isnot(None), Item.deadline <= soon, Item.deadline >= now).count()
    )
    overdue_count = open_status.filter(Item.deadline.isnot(None), Item.deadline < now).count()

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    finalized_today = base.filter(
        Item.status.in_([ItemStatus.finalized, ItemStatus.archived]),
        Item.updated_at >= day_start,
        Item.updated_at < day_end,
    ).count()

    return DashboardOut(
        todo=[ItemBrief.model_validate(i) for i in todo],
        my_created=[ItemBrief.model_validate(i) for i in my_created],
        overdue_soon=[ItemBrief.model_validate(i) for i in overdue_soon_list],
        stats=DashboardStats(
            in_progress=in_progress_count,
            overdue_soon=overdue_soon_count,
            overdue=overdue_count,
            finalized_today=finalized_today,
        ),
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
    # 办公室/管理员创建时可暂不指定承办人；普通用户默认自己承办
    if not data.get("handler_id"):
        if user.role not in (UserRole.admin, UserRole.office_clerk):
            data["handler_id"] = user.id
    item = Item(
        **data,
        creator_id=user.id,
        status=ItemStatus.draft,
    )
    db.add(item)
    db.flush()
    write_log(db, item, user, ActionType.create, detail=f"创建事项：{item.title}")
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
    # 非办公室/管理员通过 PUT 改参与人时限制：承办人可改自己负责事项的基本字段，
    # 但 leader 调整建议走 assign；此处允许 office/admin 经 edit 改参与人，handler 也可改（兼容旧表单）
    for k, v in data.items():
        setattr(item, k, v)
    write_log(db, item, user, ActionType.update, detail="更新事项信息")
    db.commit()
    return _get_item(db, item_id)


@router.post("/{item_id}/assign", response_model=ItemDetail)
def assign_item(
    item_id: int,
    body: ItemAssign,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    """办公室分派/调整承办人与 A/B 领导。"""
    item = _get_item(db, item_id)
    ensure_can_assign_item(user, item)

    st = item.status
    payload = body.model_dump(exclude_unset=True)
    comment = payload.pop("comment", None)

    if st in (
        ItemStatus.draft,
        ItemStatus.handling,
        ItemStatus.leader_a_rejected,
        ItemStatus.leader_b_rejected,
    ):
        allowed_fields = {"handler_id", "leader_a_id", "leader_b_id"}
    elif st == ItemStatus.leader_a_review:
        allowed_fields = {"leader_a_id"}
    elif st == ItemStatus.leader_b_review:
        allowed_fields = {"leader_b_id"}
    else:
        raise HTTPException(status_code=400, detail=f"当前状态「{st.value}」不可调整参与人")

    field_labels = {
        "handler_id": "承办人",
        "leader_a_id": "A领导",
        "leader_b_id": "B领导",
    }
    changes: list[str] = []

    for field in ("handler_id", "leader_a_id", "leader_b_id"):
        if field not in payload:
            continue
        if field not in allowed_fields:
            raise HTTPException(
                status_code=400,
                detail=f"当前状态「{st.value}」不可调整{field_labels[field]}",
            )
        new_val = payload[field]
        old_val = getattr(item, field)
        if old_val != new_val:
            changes.append(
                f"{field_labels[field]}：{_user_label(db, old_val)} → {_user_label(db, new_val)}"
            )
            setattr(item, field, new_val)

    if not changes:
        raise HTTPException(status_code=400, detail="未变更任何参与人")

    write_log(
        db,
        item,
        user,
        ActionType.assign,
        comment=comment,
        detail="；".join(changes),
    )
    db.commit()
    return _get_item(db, item_id)


@router.post("/{item_id}/supervise", response_model=ActionLogOut)
def supervise_item(
    item_id: int,
    body: SuperviseAction,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    item = _get_item(db, item_id)
    ensure_can_supervise_item(user, item)
    comment = body.comment.strip()
    if not comment:
        raise HTTPException(status_code=400, detail="督办意见不能为空")
    log = write_log(
        db,
        item,
        user,
        ActionType.supervise,
        comment=comment,
        detail="督办催办",
    )
    db.commit()
    db.refresh(log)
    log = (
        db.query(ActionLog)
        .options(joinedload(ActionLog.actor))
        .filter(ActionLog.id == log.id)
        .first()
    )
    return log


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
