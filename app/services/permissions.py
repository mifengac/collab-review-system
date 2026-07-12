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


def is_admin(user: User) -> bool:
    return user.role == UserRole.admin


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
    if is_admin(user):
        return True
    return is_participant(user, item)


def can_edit_item(user: User, item: Item) -> bool:
    """编辑基本信息：管理员或承办人；终态不可编辑。"""
    if item.status in LOCKED_STATUSES:
        return False
    if is_admin(user):
        return True
    return item.handler_id is not None and user.id == item.handler_id


def can_upload_document(user: User, item: Item) -> bool:
    """上传文件：管理员或承办人；终态不可上传。"""
    if item.status in LOCKED_STATUSES:
        return False
    if is_admin(user):
        return True
    return item.handler_id is not None and user.id == item.handler_id


def can_download_document(user: User, item: Item) -> bool:
    """下载与查看权限一致。"""
    return can_view_item(user, item)


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


def item_scope_filter(user: User):
    """
    返回 SQLAlchemy 布尔表达式：非管理员仅能看到参与事项。
    管理员返回 None，表示不加过滤。
    """
    if is_admin(user):
        return None
    from sqlalchemy import or_

    return or_(
        Item.creator_id == user.id,
        Item.handler_id == user.id,
        Item.leader_a_id == user.id,
        Item.leader_b_id == user.id,
    )
