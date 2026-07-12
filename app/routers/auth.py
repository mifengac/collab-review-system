from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import (
    CurrentUser,
    authenticate_user,
    create_access_token,
    hash_password,
)
from app.database import get_db
from app.models import User, UserRole
from app.schemas import LoginRequest, TokenResponse, UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Annotated[Session, Depends(get_db)]):
    user = authenticate_user(db, body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(user.username, {"uid": user.id, "role": user.role.value})
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    return db.query(User).filter(User.is_active.is_(True)).order_by(User.id).all()


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
