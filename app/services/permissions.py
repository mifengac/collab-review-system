"""事项级权限判断。后端强制校验，前端隐藏仅作体验优化。"""
from __future__ import annotations

from fastapi import HTTPException, status

from app.models import Item, ItemStatus, User, UserRole

# 终态：禁止编辑与上传
LOCKED_STATUSES = frozenset(
    {
        ItemStatus.finalized,
        ItemStatus.archived,
        ItemStatus.cancelled,
    }
)

# 可查看全部事项
GLOBAL_VIEW_ROLES = frozenset(
    {
        UserRole.admin,
        UserRole.office_clerk,
        UserRole.supervisor,
    }
)

# 可分派参与人
ASSIGN_ROLES = frozenset({UserRole.admin, UserRole.office_clerk})

# 可督办催办
SUPERVISE_ROLES = frozenset(
    {
        UserRole.admin,
        UserRole.office_clerk,
        UserRole.supervisor,
    }
)


def is_admin(user: User) -> bool:
    return user.role == UserRole.admin


def is_office_clerk(user: User) -> bool:
    return user.role == UserRole.office_clerk


def is_supervisor(user: User) -> bool:
    return user.role == UserRole.supervisor


def can_view_all_items(user: User) -> bool:
    return user.role in GLOBAL_VIEW_ROLES


def is_participant(user: User, item: Item) -> bool:
    """创建人 / 承办人 / 指定 A 领导 / 指定 B 领导。"""
    uid = user.id
    return uid in {
        item.creator_id,
        item.handler_id,
        item.leader_a_id,
        item.leader_b_id,
    }


def can_view_item(user: User, item: Item) -> bool:
    if can_view_all_items(user):
        return True
    return is_participant(user, item)


def can_edit_item(user: User, item: Item) -> bool:
    """编辑基本信息：管理员、办公室、承办人；终态不可编辑。"""
    if item.status in LOCKED_STATUSES:
        return False
    if is_admin(user) or is_office_clerk(user):
        return True
    return item.handler_id is not None and user.id == item.handler_id


def can_upload_document(user: User, item: Item) -> bool:
    """上传：管理员、办公室、承办人；督办不可；终态不可。"""
    if item.status in LOCKED_STATUSES:
        return False
    if is_admin(user) or is_office_clerk(user):
        return True
    return item.handler_id is not None and user.id == item.handler_id


def can_download_document(user: User, item: Item) -> bool:
    return can_view_item(user, item)


def can_assign_item(user: User, item: Item) -> bool:
    if item.status in LOCKED_STATUSES:
        return False
    return user.role in ASSIGN_ROLES


def can_supervise_item(user: User, item: Item) -> bool:
    if not can_view_item(user, item):
        return False
    return user.role in SUPERVISE_ROLES


def can_cancel_item(user: User, item: Item) -> bool:
    """作废：管理员、办公室、创建人、承办人。"""
    if item.status in (ItemStatus.archived, ItemStatus.cancelled):
        return False
    if is_admin(user) or is_office_clerk(user):
        return True
    return user.id in {item.creator_id, item.handler_id}


def ensure_can_view_item(user: User, item: Item) -> None:
    if not can_view_item(user, item):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权查看该事项",
        )


def ensure_can_edit_item(user: User, item: Item) -> None:
    if item.status in LOCKED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="已定稿/归档/作废的事项不可编辑",
        )
    if not can_edit_item(user, item):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权编辑该事项",
        )


def ensure_can_upload_document(user: User, item: Item) -> None:
    if item.status in LOCKED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="已定稿/归档/作废的事项不可上传文件",
        )
    if not can_upload_document(user, item):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权上传该事项的文件",
        )


def ensure_can_download_document(user: User, item: Item) -> None:
    if not can_download_document(user, item):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权下载该文件",
        )


def ensure_can_assign_item(user: User, item: Item) -> None:
    if item.status in LOCKED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="已定稿/归档/作废的事项不可调整参与人",
        )
    if not can_assign_item(user, item):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员或办公室收文员可分派事项",
        )


def ensure_can_supervise_item(user: User, item: Item) -> None:
    if not can_supervise_item(user, item):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权填写督办催办",
        )


def item_scope_filter(user: User):
    """
    返回 SQLAlchemy 布尔表达式：全局角色不加过滤；
    其他用户仅能看到参与事项。
    """
    if can_view_all_items(user):
        return None
    from sqlalchemy import or_

    return or_(
        Item.creator_id == user.id,
        Item.handler_id == user.id,
        Item.leader_a_id == user.id,
        Item.leader_b_id == user.id,
    )
