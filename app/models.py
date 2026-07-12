"""SQLAlchemy 数据模型。"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserRole(str, enum.Enum):
    admin = "admin"
    office_clerk = "office_clerk"  # 办公室收文员/分派
    supervisor = "supervisor"  # 督办人员
    handler = "handler"  # 承办人
    leader_a = "leader_a"  # A 领导
    leader_b = "leader_b"  # B 领导
    viewer = "viewer"


class ItemStatus(str, enum.Enum):
    draft = "草稿"
    handling = "承办中"
    leader_a_review = "A领导审核中"
    leader_a_rejected = "A领导退回"
    leader_b_review = "B领导审核中"
    leader_b_rejected = "B领导退回"
    finalized = "已定稿"
    archived = "已归档"
    cancelled = "已作废"


class UrgencyLevel(str, enum.Enum):
    normal = "一般"
    important = "重要"
    urgent = "紧急"
    critical = "特急"


class FileKind(str, enum.Enum):
    main = "main"  # 主材料
    attachment = "attachment"  # 附件


class ActionType(str, enum.Enum):
    create = "创建事项"
    update = "更新事项"
    assign = "分派调整"
    upload = "上传文件"
    download = "下载文件"
    submit_a = "提交A领导审核"
    approve_a = "A领导通过"
    reject_a = "A领导退回"
    submit_b = "提交B领导审核"
    finalize = "定稿"
    reject_b = "B领导退回"
    archive = "归档"
    cancel = "作废"
    supervise = "督办催办"
    comment = "填写意见"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.handler, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    items_created = relationship("Item", back_populates="creator", foreign_keys="Item.creator_id")
    files = relationship("FileVersion", back_populates="uploader")
    logs = relationship("ActionLog", back_populates="actor")


class Department(Base):
    """组织/大队字典。"""

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class BusinessTag(Base):
    """业务标签字典。"""

    __tablename__ = "business_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Item(Base):
    """协同事项。"""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    oa_doc_no: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="OA文号/来文编号")
    source_unit: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="来文单位")
    handler_dept: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="承办大队")
    business_tag: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="业务标签")
    urgency: Mapped[UrgencyLevel] = mapped_column(
        Enum(UrgencyLevel), default=UrgencyLevel.normal, nullable=False
    )
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ItemStatus] = mapped_column(
        Enum(ItemStatus), default=ItemStatus.draft, nullable=False, index=True
    )

    creator_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    handler_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    leader_a_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    leader_b_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    # OA 预留字段
    oa_flow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    oa_step_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    oa_deal_index: Mapped[str | None] = mapped_column(String(64), nullable=True)
    oa_raw_title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    oa_raw_doc_no: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    creator = relationship("User", foreign_keys=[creator_id], back_populates="items_created")
    handler = relationship("User", foreign_keys=[handler_id])
    leader_a = relationship("User", foreign_keys=[leader_a_id])
    leader_b = relationship("User", foreign_keys=[leader_b_id])
    documents = relationship("Document", back_populates="item", cascade="all, delete-orphan")
    logs = relationship(
        "ActionLog", back_populates="item", cascade="all, delete-orphan", order_by="ActionLog.created_at"
    )


class Document(Base):
    """逻辑文档（主材料或附件槽位），其下有多个不可覆盖的历史版本。"""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False, comment="文档显示名")
    kind: Mapped[FileKind] = mapped_column(Enum(FileKind), default=FileKind.attachment)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    item = relationship("Item", back_populates="documents")
    versions = relationship(
        "FileVersion",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="FileVersion.version_no",
    )


class FileVersion(Base):
    """文件版本，历史版本不可覆盖。"""

    __tablename__ = "file_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_no", name="uq_doc_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploader_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    document = relationship("Document", back_populates="versions")
    uploader = relationship("User", back_populates="files")


class ActionLog(Base):
    """操作留痕 / 流转时间线。"""

    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), nullable=False, index=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[ActionType] = mapped_column(Enum(ActionType), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    item = relationship("Item", back_populates="logs")
    actor = relationship("User", back_populates="logs")


class OAWorkItem(Base):
    """OA 公文池：同步自 OA 列表，用户点击后再创建协同事项。"""

    __tablename__ = "oa_work_items"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "module_code",
            "flowinid",
            "stepinco",
            "dealindx",
            name="uq_oa_work_item_owner_module_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    oa_user_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    module_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    module_name: Mapped[str] = mapped_column(String(64), nullable=False)

    flowinid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stepinco: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dealindx: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_key: Mapped[str] = mapped_column(String(256), nullable=False, default="")

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    doc_no: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_unit: Mapped[str | None] = mapped_column(String(256), nullable=True)
    flow_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    step_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    handler_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    open_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    has_attach: Mapped[bool] = mapped_column(Boolean, default=False)
    read_flag: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fini_flag: Mapped[int | None] = mapped_column(Integer, nullable=True)
    urgency: Mapped[int | None] = mapped_column(Integer, nullable=True)

    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    linked_item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True, index=True)
    # 当前是否仍出现在对应模块的完整同步结果中
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    owner = relationship("User", foreign_keys=[owner_user_id])
    linked_item = relationship("Item", foreign_keys=[linked_item_id])


class OASyncLog(Base):
    """OA 公文同步诊断记录（不含密码/cookie/原始响应）。"""

    __tablename__ = "oa_sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    # login | manual
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    # success | partial | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="failed", index=True)
    imported: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    module_results_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    user = relationship("User", foreign_keys=[user_id])
