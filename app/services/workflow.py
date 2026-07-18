"""审核流转状态机。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import (
    ActionLog,
    ActionType,
    Document,
    FileKind,
    FileVersion,
    Item,
    ItemStatus,
    User,
    UserRole,
    VersionKind,
)
from app.services.permissions import can_cancel_item


class WorkflowError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _is_admin(actor: User) -> bool:
    return actor.role == UserRole.admin


def _log(
    db: Session,
    item: Item,
    actor: User,
    action: ActionType,
    from_status: ItemStatus | None,
    to_status: ItemStatus | None,
    comment: str | None = None,
    detail: str | None = None,
) -> ActionLog:
    log = ActionLog(
        item_id=item.id,
        actor_id=actor.id,
        action=action,
        comment=comment,
        detail=detail,
        from_status=from_status.value if from_status else None,
        to_status=to_status.value if to_status else None,
    )
    db.add(log)
    return log


def write_log(
    db: Session,
    item: Item,
    actor: User,
    action: ActionType,
    comment: str | None = None,
    detail: str | None = None,
    from_status: ItemStatus | None = None,
    to_status: ItemStatus | None = None,
) -> ActionLog:
    return _log(db, item, actor, action, from_status, to_status, comment, detail)


def submit_to_a(db: Session, item: Item, actor: User, comment: str | None) -> Item:
    """承办人 / 创建人 / 管理员 提交给 A 领导。"""
    allowed = {
        ItemStatus.draft,
        ItemStatus.handling,
        ItemStatus.leader_a_rejected,
        ItemStatus.leader_b_rejected,
    }
    if item.status not in allowed:
        raise WorkflowError(f"当前状态「{item.status.value}」不可提交 A 领导审核")

    if not _is_admin(actor):
        if actor.id not in {item.handler_id, item.creator_id}:
            raise WorkflowError("仅承办人、创建人或管理员可提交审核", status_code=403)

    if not item.leader_a_id:
        raise WorkflowError("事项未指定 A 领导，请先补齐后再提交审核")

    old = item.status
    item.status = ItemStatus.leader_a_review
    _log(db, item, actor, ActionType.submit_a, old, item.status, comment)
    return item


def approve_a(db: Session, item: Item, actor: User, comment: str | None) -> Item:
    """指定 A 领导或管理员通过，进入 B 领导审核。"""
    if item.status != ItemStatus.leader_a_review:
        raise WorkflowError(f"当前状态「{item.status.value}」不可执行 A 领导通过")

    # 办公室/督办不得代批，除非本人就是指定 A 领导
    if not _is_admin(actor):
        if not item.leader_a_id or actor.id != item.leader_a_id:
            raise WorkflowError("仅该事项指定的 A 领导或管理员可通过", status_code=403)

    if not item.leader_b_id:
        raise WorkflowError("事项未指定 B 领导，请先补齐后再提交 B 领导审核")

    old = item.status
    item.status = ItemStatus.leader_b_review
    _log(db, item, actor, ActionType.approve_a, old, item.status, comment)
    _log(
        db,
        item,
        actor,
        ActionType.submit_b,
        ItemStatus.leader_a_review,
        item.status,
        "A领导通过后自动提交B领导",
    )
    return item


def reject_a(db: Session, item: Item, actor: User, comment: str | None) -> Item:
    if item.status != ItemStatus.leader_a_review:
        raise WorkflowError(f"当前状态「{item.status.value}」不可执行 A 领导退回")
    if not comment or not comment.strip():
        raise WorkflowError("退回必须填写意见")

    if not _is_admin(actor):
        if not item.leader_a_id or actor.id != item.leader_a_id:
            raise WorkflowError("仅该事项指定的 A 领导或管理员可退回", status_code=403)

    old = item.status
    item.status = ItemStatus.leader_a_rejected
    _log(db, item, actor, ActionType.reject_a, old, item.status, comment)
    return item


def _main_document(db: Session, item: Item) -> Document | None:
    return (
        db.query(Document)
        .filter(Document.item_id == item.id, Document.kind == FileKind.main)
        .first()
    )


def _current_main_version(db: Session, item: Item) -> FileVersion | None:
    doc = _main_document(db, item)
    if not doc or not doc.current_version:
        return None
    return (
        db.query(FileVersion)
        .filter(
            FileVersion.document_id == doc.id,
            FileVersion.version_no == doc.current_version,
        )
        .first()
    )


def can_prepare_finalize_archive(actor: User, item: Item) -> bool:
    """定稿归档：承办人、指定 B 领导、管理员。"""
    if item.status != ItemStatus.leader_b_review:
        return False
    if _is_admin(actor):
        return True
    if actor.role == UserRole.handler and item.handler_id and actor.id == item.handler_id:
        return True
    if item.leader_b_id and actor.id == item.leader_b_id:
        return True
    return False


def prepare_finalize_archive(
    db: Session, item: Item, actor: User, comment: str | None = None
) -> FileVersion:
    """
    定稿归档：将主材料当前版本标为痕迹存档版（marked）。
    不改变事项状态；引导用户在编辑器中接受修订后保存生成终稿。
    """
    if item.status != ItemStatus.leader_b_review:
        raise WorkflowError(f"当前状态「{item.status.value}」不可定稿归档")
    if not can_prepare_finalize_archive(actor, item):
        raise WorkflowError("仅承办人、指定 B 领导或管理员可定稿归档", status_code=403)

    version = _current_main_version(db, item)
    if not version:
        raise WorkflowError("尚无主材料版本，请先上传 docx")

    if version.version_kind == VersionKind.final:
        raise WorkflowError("当前已是终稿版，请直接由 B 领导定稿锁定")

    if version.version_kind != VersionKind.marked:
        version.version_kind = VersionKind.marked

    detail = (
        f"主材料 v{version.version_no} 标记为痕迹存档版；"
        f"请在线编辑接受全部修订后保存以生成终稿版"
    )
    _log(
        db,
        item,
        actor,
        ActionType.mark_finalize,
        item.status,
        item.status,
        comment,
        detail=detail,
    )
    return version


def mark_current_as_marked_if_needed(db: Session, item: Item) -> None:
    """B 领导定稿时：若当前主材料仍是 normal，自动标为痕迹存档。"""
    version = _current_main_version(db, item)
    if version and version.version_kind == VersionKind.normal:
        version.version_kind = VersionKind.marked


def finalize_b(db: Session, item: Item, actor: User, comment: str | None) -> Item:
    """指定 B 领导或管理员定稿。定稿后文档只读。"""
    if item.status != ItemStatus.leader_b_review:
        raise WorkflowError(f"当前状态「{item.status.value}」不可定稿")

    if not _is_admin(actor):
        if not item.leader_b_id or actor.id != item.leader_b_id:
            raise WorkflowError("仅该事项指定的 B 领导或管理员可定稿", status_code=403)

    # 定稿时若尚未标记痕迹版，自动将当前版本标为 marked 保留痕迹
    mark_current_as_marked_if_needed(db, item)

    old = item.status
    item.status = ItemStatus.finalized
    _log(db, item, actor, ActionType.finalize, old, item.status, comment)
    return item


def reject_b(db: Session, item: Item, actor: User, comment: str | None) -> Item:
    if item.status != ItemStatus.leader_b_review:
        raise WorkflowError(f"当前状态「{item.status.value}」不可执行 B 领导退回")
    if not comment or not comment.strip():
        raise WorkflowError("退回必须填写意见")

    if not _is_admin(actor):
        if not item.leader_b_id or actor.id != item.leader_b_id:
            raise WorkflowError("仅该事项指定的 B 领导或管理员可退回", status_code=403)

    old = item.status
    item.status = ItemStatus.leader_b_rejected
    _log(db, item, actor, ActionType.reject_b, old, item.status, comment)
    return item


def archive(db: Session, item: Item, actor: User, comment: str | None) -> Item:
    if item.status != ItemStatus.finalized:
        raise WorkflowError("仅「已定稿」事项可归档")
    if not _is_admin(actor) and actor.role != UserRole.office_clerk:
        if actor.id not in {item.creator_id, item.handler_id, item.leader_b_id}:
            raise WorkflowError("仅参与人、办公室或管理员可归档", status_code=403)
    old = item.status
    item.status = ItemStatus.archived
    _log(db, item, actor, ActionType.archive, old, item.status, comment)
    return item


def cancel(db: Session, item: Item, actor: User, comment: str | None) -> Item:
    if item.status in (ItemStatus.finalized, ItemStatus.archived, ItemStatus.cancelled):
        raise WorkflowError(f"当前状态「{item.status.value}」不可作废")
    if not comment or not comment.strip():
        raise WorkflowError("作废必须填写原因")
    if not can_cancel_item(actor, item):
        raise WorkflowError("仅管理员、办公室、创建人或承办人可作废", status_code=403)
    old = item.status
    item.status = ItemStatus.cancelled
    _log(db, item, actor, ActionType.cancel, old, item.status, comment)
    return item
