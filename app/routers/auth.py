from __future__ import annotations

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
    authenticate_oa_user,
)

router = APIRouter(prefix="/api/auth", tags=["认证"])


def _issue_token(user: User) -> TokenResponse:
    token = create_access_token(user.username, {"uid": user.id, "role": user.role.value})
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


def _resolve_default_oa_role() -> UserRole:
    settings = get_settings()
    raw = (settings.oa_default_role or "viewer").strip().lower()
    try:
        role = UserRole(raw)
    except ValueError:
        role = UserRole.viewer
    # 禁止通过默认角色自动成为管理员
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
        # 仅刷新展示信息，绝不改 role / password_hash
        if profile.display_name:
            user.display_name = profile.display_name
        if profile.unit is not None:
            user.unit = profile.unit
        db.commit()
        db.refresh(user)
        return user

    # 首次 OA 登录：创建本地影子账号，密码为随机哈希（不可用 OA 密码本地登录）
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


def _login_oa(db: Session, username: str, password: str) -> User:
    try:
        profile = authenticate_oa_user(username, password)
    except OAAuthError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    except OAAuthUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.message) from exc
    return upsert_oa_user(db, profile)


@router.get("/config", response_model=AuthConfigOut)
def auth_config():
    settings = get_settings()
    return AuthConfigOut(
        auth_mode=settings.auth_mode_normalized,
        oa_enabled=settings.oa_enabled,
        title=settings.app_name,
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
        return _issue_token(_login_oa(db, username, password))

    # mixed：优先 OA；仅当 OA 服务不可用（503）时允许本地 admin 维护登录
    try:
        return _issue_token(_login_oa(db, username, password))
    except HTTPException as exc:
        # 401 认证失败 / 403 本地禁用 等：不回落
        if exc.status_code != 503:
            raise
        if username == settings.admin_username:
            try:
                return _issue_token(_login_local(db, username, password))
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
    """新建/分派事项选人：所有登录用户可取启用中的人员精简列表。"""
    return (
        db.query(User)
        .filter(User.is_active.is_(True))
        .order_by(User.id)
        .all()
    )


@router.get("/users", response_model=list[UserOut])
def list_users(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    """用户管理列表：管理员可见全部（含禁用）；办公室可见启用用户。"""
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
