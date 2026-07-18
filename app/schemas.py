"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import ActionType, FileKind, ItemStatus, UrgencyLevel, UserRole, VersionKind


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Auth ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class OAModuleResultOut(BaseModel):
    module_code: str
    module_name: str
    success: bool
    fetched: int = 0
    pages: int = 0
    imported: int = 0
    updated: int = 0
    deactivated: int = 0
    complete: bool = False
    truncated: bool = False
    error: str | None = None


class OASyncStatusOut(BaseModel):
    enabled: bool = False
    success: bool = False
    total: int = 0
    imported: int = 0
    updated: int = 0
    error: str | None = None
    status: str | None = None  # success | partial | failed
    log_id: int | None = None
    module_results: list[OAModuleResultOut] = []


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"
    oa_sync: OASyncStatusOut | None = None


class AuthConfigOut(BaseModel):
    auth_mode: str
    oa_enabled: bool
    title: str
    oa_sync_on_login: bool = False
    # 仅 DEBUG=true 且 OA_MOCK_ENABLED=true 时为 true（前端显示模拟环境标识）
    oa_mock_enabled: bool = False
    # ONLYOFFICE 是否已配置启用（前端控制「在线编辑」按钮）
    onlyoffice_enabled: bool = False


class UserOut(ORMModel):
    id: int
    username: str
    display_name: str
    role: UserRole
    unit: str | None = None
    is_active: bool


class UserCreate(BaseModel):
    username: str
    password: str = Field(min_length=6)
    display_name: str
    role: UserRole = UserRole.handler
    unit: str | None = None


class UserUpdate(BaseModel):
    display_name: str | None = None
    role: UserRole | None = None
    unit: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=6)


class BatchRoleUpdate(BaseModel):
    """管理员批量调整角色。"""

    user_ids: list[int] = Field(min_length=1, max_length=500)
    role: UserRole


class BatchRoleUpdateResult(BaseModel):
    updated: int
    skipped: int = 0
    message: str


class UserOption(ORMModel):
    """新建/分派事项时的选人列表（精简字段）。"""

    id: int
    username: str
    display_name: str
    role: UserRole
    unit: str | None = None


# ---------- Dict ----------
class DepartmentOut(ORMModel):
    id: int
    name: str
    sort_order: int
    is_active: bool


class BusinessTagOut(ORMModel):
    id: int
    name: str
    sort_order: int
    is_active: bool


# ---------- Item ----------
class ItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    oa_doc_no: str | None = None
    source_unit: str | None = None
    handler_dept: str | None = None
    business_tag: str | None = None
    urgency: UrgencyLevel = UrgencyLevel.normal
    deadline: datetime | None = None
    handler_id: int | None = None
    leader_a_id: int | None = None
    leader_b_id: int | None = None
    remark: str | None = None
    # OA 预留
    oa_flow_id: str | None = None
    oa_step_id: str | None = None
    oa_deal_index: str | None = None
    oa_raw_title: str | None = None
    oa_raw_doc_no: str | None = None


class ItemUpdate(BaseModel):
    """仅基本字段；承办人/A/B 领导请使用 POST /api/items/{id}/assign。"""

    title: str | None = None
    oa_doc_no: str | None = None
    source_unit: str | None = None
    handler_dept: str | None = None
    business_tag: str | None = None
    urgency: UrgencyLevel | None = None
    deadline: datetime | None = None
    remark: str | None = None


class ItemBrief(ORMModel):
    id: int
    title: str
    oa_doc_no: str | None
    source_unit: str | None
    handler_dept: str | None
    business_tag: str | None
    urgency: UrgencyLevel
    deadline: datetime | None
    status: ItemStatus
    creator_id: int
    handler_id: int | None
    leader_a_id: int | None
    leader_b_id: int | None
    created_at: datetime
    updated_at: datetime


class ItemDetail(ItemBrief):
    remark: str | None
    oa_flow_id: str | None
    oa_step_id: str | None
    oa_deal_index: str | None
    oa_raw_title: str | None
    oa_raw_doc_no: str | None
    creator: UserOut | None = None
    handler: UserOut | None = None
    leader_a: UserOut | None = None
    leader_b: UserOut | None = None


class WorkflowAction(BaseModel):
    comment: str | None = None


class ItemAssign(BaseModel):
    handler_id: int | None = None
    leader_a_id: int | None = None
    leader_b_id: int | None = None
    comment: str | None = None


class BatchAssignRequest(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=200)
    handler_id: int | None = None
    leader_a_id: int | None = None
    leader_b_id: int | None = None
    comment: str | None = None


class BatchAssignFailure(BaseModel):
    item_id: int
    detail: str


class BatchAssignResult(BaseModel):
    success: int
    failed: list[BatchAssignFailure] = []
    message: str


class SuperviseAction(BaseModel):
    comment: str = Field(min_length=1)


# ---------- Document / File ----------
class FileVersionOut(ORMModel):
    id: int
    document_id: int
    version_no: int
    original_filename: str
    content_type: str | None
    file_size: int
    sha256: str
    version_kind: VersionKind = VersionKind.normal
    uploader_id: int
    created_at: datetime
    uploader: UserOut | None = None


class MarkFinalizeOut(BaseModel):
    """定稿归档：当前主材料标为痕迹存档版。"""

    message: str
    document_id: int | None = None
    version_id: int | None = None
    version_no: int | None = None
    version_kind: VersionKind = VersionKind.marked
    open_editor_hint: str = (
        "请打开主材料「在线编辑」，接受全部修订后保存，将自动生成终稿版；"
        "再由 B 领导点击「定稿」锁定事项。"
    )


class DocumentOut(ORMModel):
    id: int
    item_id: int
    name: str
    kind: FileKind
    current_version: int
    created_at: datetime
    versions: list[FileVersionOut] = []


# ---------- Action log ----------
class ActionLogOut(ORMModel):
    id: int
    item_id: int | None = None
    actor_id: int
    action: ActionType
    comment: str | None
    detail: str | None
    from_status: str | None
    to_status: str | None
    created_at: datetime
    actor: UserOut | None = None


# ---------- Dashboard ----------
class DashboardStats(BaseModel):
    in_progress: int = 0
    overdue_soon: int = 0
    overdue: int = 0
    finalized_today: int = 0


class DashboardOut(BaseModel):
    todo: list[ItemBrief]
    my_created: list[ItemBrief]
    overdue_soon: list[ItemBrief]
    stats: DashboardStats = DashboardStats()


# ---------- OA 公文池 ----------
class OAInboxItem(BaseModel):
    oa_flow_id: str
    oa_step_id: str | None = None
    oa_deal_index: str | None = None
    title: str
    doc_no: str | None = None
    source_unit: str | None = None
    received_at: datetime | None = None


class OAWorkItemOut(ORMModel):
    id: int
    owner_user_id: int
    oa_user_code: str
    module_code: str
    module_name: str
    flowinid: str
    stepinco: str | None = None
    dealindx: str | None = None
    title: str
    doc_no: str | None = None
    source_unit: str | None = None
    flow_name: str | None = None
    step_name: str | None = None
    handler_name: str | None = None
    received_at: datetime | None = None
    open_date: datetime | None = None
    has_attach: bool = False
    read_flag: int | None = None
    fini_flag: int | None = None
    urgency: int | None = None
    linked_item_id: int | None = None
    is_active: bool = True
    synced_at: datetime
    created_at: datetime
    updated_at: datetime


class OASyncRequest(BaseModel):
    force: bool = False
    username: str | None = None
    password: str | None = None
    modules: list[str] | None = None


class OASyncResponse(BaseModel):
    success: bool
    message: str
    imported: int = 0
    updated: int = 0
    total: int = 0
    status: str | None = None  # success | partial | failed
    log_id: int | None = None
    module_results: list[OAModuleResultOut] = []
    data: list[Any] = []


class OASyncLogOut(BaseModel):
    id: int
    user_id: int
    trigger: str
    status: str
    imported: int
    updated: int
    total: int
    module_results: list[OAModuleResultOut] = []
    error_summary: str | None = None
    started_at: datetime
    finished_at: datetime
    created_at: datetime


class OAModuleStat(BaseModel):
    module_code: str
    module_name: str
    count: int
    last_synced_at: datetime | None = None


# ---------- ONLYOFFICE ----------
class EditorConfigOut(BaseModel):
    document_id: int
    mode: str = "view"
    reserved: bool = True
    message: str = "在线编辑功能预留，后续接入 ONLYOFFICE Docs"
    editor_url: str | None = None
    config: dict[str, Any] = {}
    version_no: int | None = None
    track_changes_forced: bool = False
    can_review: bool = False


class OnlyOfficeCallbackOut(BaseModel):
    """Document Server 要求的回调响应，error=0 表示成功。"""

    error: int = 0


class MessageOut(BaseModel):
    message: str
    detail: Any = None


TokenResponse.model_rebuild()
