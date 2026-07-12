from __future__ import annotations

import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import (
    CurrentUser,
    authenticate_user,
    create_access_token,
    hash_password,
)
from app.config import get_settings
from app.database import get_db
from app.models import User, UserRole
from app.schemas import (
    AuthConfigOut,
    LoginRequest,
    OASyncStatusOut,
    TokenResponse,
    UserCreate,
    UserOption,
    UserOut,
    UserUpdate,
)
from app.services.oa_auth import (
    OAAuthError,
    OAAuthUnavailable,
    OAUserProfile,
    authenticate_and_fetch_oa,
    authenticate_oa_user,
)
from app.schemas import OAModuleResultOut
from app.services.oa_sync import merge_module_import_stats, sync_oa_work_items, write_oa_sync_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["认证"])


def _issue_token(user: User, oa_sync: OASyncStatusOut | None = None) -> TokenResponse:
    token = create_access_token(user.username, {"uid": user.id, "role": user.role.value})
    return TokenResponse(
        access_token=token,
        user=UserOut.model_validate(user),
        oa_sync=oa_sync,
    )


def _module_results_out(raw_list: list) -> list[OAModuleResultOut]:
    out: list[OAModuleResultOut] = []
    for m in raw_list:
        if hasattr(m, "to_dict"):
            d = m.to_dict()
        elif isinstance(m, dict):
            d = m
        else:
            continue
        out.append(OAModuleResultOut.model_validate(d))
    return out


def _resolve_default_oa_role() -> UserRole:
    settings = get_settings()
    raw = (settings.oa_default_role or "viewer").strip().lower()
    try:
        role = UserRole(raw)
    except ValueError:
        role = UserRole.viewer
    if role == UserRole.admin:
        return UserRole.viewer
    return role


def upsert_oa_user(db: Session, profile: OAUserProfile) -> User:
    """OA 验证成功后同步本地用户：更新姓名/单位，不覆盖角色，不保存 OA 密码。"""
    username = profile.username.strip()
    user = db.query(User).filter(User.username == username).first()
    if user:
        if not user.is_active:
            raise HTTPException(status_code=403, detail="本地用户已禁用，请联系管理员")
        if profile.display_name:
            user.display_name = profile.display_name
        if profile.unit is not None:
            user.unit = profile.unit
        db.commit()
        db.refresh(user)
        return user

    random_secret = secrets.token_urlsafe(32)
    user = User(
        username=username,
        password_hash=hash_password(random_secret),
        display_name=profile.display_name or username,
        role=_resolve_default_oa_role(),
        unit=profile.unit,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login_local(db: Session, username: str, password: str) -> User:
    user = authenticate_user(db, username, password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return user


def _login_oa(db: Session, username: str, password: str) -> tuple[User, OASyncStatusOut]:
    from datetime import datetime

    settings = get_settings()
    try:
        if settings.oa_sync_on_login:
            started = datetime.utcnow()
            profile, report = authenticate_and_fetch_oa(
                username,
                password,
                modules=settings.oa_sync_module_list,
                max_pages=settings.oa_sync_max_pages,
            )
            user = upsert_oa_user(db, profile)

            imported = updated = total = 0
            module_dicts = [m.to_dict() for m in report.module_results]
            log_id = None
            try:
                stats = sync_oa_work_items(
                    db,
                    user,
                    profile.username,
                    report.items,
                    module_results=report.module_results,
                )
                imported = stats["imported"]
                updated = stats["updated"]
                total = stats["total"]
                module_dicts = merge_module_import_stats(
                    report.module_results, stats.get("by_module") or {}
                )
            except Exception as exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                logger.warning(
                    "OA work items sync after login failed: %s",
                    type(exc).__name__,
                )
                # 写失败记录本身不可抛出
                write_oa_sync_log(
                    db,
                    user_id=user.id,
                    trigger="login",
                    status="failed",
                    imported=0,
                    updated=0,
                    total=0,
                    module_results=module_dicts,
                    error_summary="OA 登录成功但公文入库失败，请稍后重试或联系管理员",
                    started_at=started,
                )
                return user, OASyncStatusOut(
                    enabled=True,
                    success=False,
                    status="failed",
                    error="OA 登录成功但公文入库失败，请稍后重试或联系管理员",
                    module_results=_module_results_out(module_dicts),
                )

            # 诊断日志失败不影响登录与入库结果
            log = write_oa_sync_log(
                db,
                user_id=user.id,
                trigger="login",
                status=report.status,
                imported=imported,
                updated=updated,
                total=total,
                module_results=module_dicts,
                error_summary=report.error_summary,
                started_at=started,
            )
            log_id = log.id if log else None

            success_flag = report.status in ("success", "partial")
            return user, OASyncStatusOut(
                enabled=True,
                success=success_flag,
                status=report.status,
                total=total,
                imported=imported,
                updated=updated,
                error=report.error_summary,
                log_id=log_id,
                module_results=_module_results_out(module_dicts),
            )
        profile = authenticate_oa_user(username, password)
        user = upsert_oa_user(db, profile)
        return user, OASyncStatusOut(enabled=False, success=True)
    except OAAuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    except OAAuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.message) from exc


@router.get("/config", response_model=AuthConfigOut)
def auth_config():
    settings = get_settings()
    return AuthConfigOut(
        auth_mode=settings.auth_mode_normalized,
        oa_enabled=settings.oa_enabled,
        title=settings.app_name,
        oa_sync_on_login=bool(settings.oa_sync_on_login),
        oa_mock_enabled=bool(settings.oa_mock_banner_enabled),
    )


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Annotated[Session, Depends(get_db)]):
    settings = get_settings()
    mode = settings.auth_mode_normalized
    username = (body.username or "").strip()
    password = body.password or ""

    if mode == "local":
        return _issue_token(_login_local(db, username, password))

    if mode == "oa":
        user, oa_sync = _login_oa(db, username, password)
        return _issue_token(user, oa_sync)

    # mixed：优先 OA；仅当 OA 服务不可用（503）时允许本地 admin 维护登录
    try:
        user, oa_sync = _login_oa(db, username, password)
        return _issue_token(user, oa_sync)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        if username == settings.admin_username:
            try:
                # 本地 fallback 不触发 OA 同步
                return _issue_token(
                    _login_local(db, username, password),
                    OASyncStatusOut(enabled=False, success=False, error="本地管理员回落登录"),
                )
            except HTTPException:
                raise HTTPException(
                    status_code=503,
                    detail="OA 暂不可用，且本地管理员认证失败",
                ) from exc
        raise HTTPException(
            status_code=503,
            detail="OA 暂不可用，请联系管理员（普通用户不可本地登录）",
        ) from exc


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user


@router.get("/user-options", response_model=list[UserOption])
def user_options(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    return (
        db.query(User)
        .filter(User.is_active.is_(True))
        .order_by(User.id)
        .all()
    )


@router.get("/users", response_model=list[UserOut])
def list_users(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    if user.role == UserRole.admin:
        return db.query(User).order_by(User.id).all()
    if user.role == UserRole.office_clerk:
        return db.query(User).filter(User.is_active.is_(True)).order_by(User.id).all()
    raise HTTPException(status_code=403, detail="无权查看用户管理列表，请使用选人接口")


@router.post("/users", response_model=UserOut)
def create_user(
    body: UserCreate,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    if user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="仅管理员可创建用户")
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=400, detail="用户名已存在")
    u = User(
        username=body.username,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
        role=body.role,
        unit=body.unit,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UserUpdate,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    if user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="仅管理员可修改用户")
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    data = body.model_dump(exclude_unset=True)
    if "password" in data and data["password"]:
        u.password_hash = hash_password(data.pop("password"))
    else:
        data.pop("password", None)
    for k, v in data.items():
        setattr(u, k, v)
    db.commit()
    db.refresh(u)
    return u
