"""首次启动：初始化字典与默认管理员。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth import hash_password
from app.config import get_settings
from app.models import BusinessTag, Department, User, UserRole

DEFAULT_DEPARTMENTS = [
    "信息工作大队",
    "基层基础及人口管理大队",
    "巡警特警及维稳工作大队",
    "治安管理行动大队",
]

DEFAULT_TAGS = [
    "人口",
    "出租屋",
    "中小学",
    "医院",
    "废旧金属",
    "重点人员",
    "保安公司",
    "人口密集场所",
    "无人机",
    "信访维稳",
    "巡防",
    "黄赌打击",
    "安保",
    "行业场所",
    "涉枪涉爆",
]

# 演示用业务账号（密码统一 Demo@123456，仅 SEED_DEMO_USERS=true 时创建）
DEMO_USERS = [
    ("handler1", "承办员张三", UserRole.handler, "信息工作大队"),
    ("leader_a", "A领导李四", UserRole.leader_a, "信息工作大队"),
    ("leader_b", "B领导王五", UserRole.leader_b, "信息工作大队"),
]


def seed_all(db: Session) -> None:
    settings = get_settings()

    if db.query(Department).count() == 0:
        for i, name in enumerate(DEFAULT_DEPARTMENTS):
            db.add(Department(name=name, sort_order=i + 1))

    if db.query(BusinessTag).count() == 0:
        for i, name in enumerate(DEFAULT_TAGS):
            db.add(BusinessTag(name=name, sort_order=i + 1))

    admin = db.query(User).filter(User.username == settings.admin_username).first()
    if not admin:
        db.add(
            User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                display_name=settings.admin_display_name,
                role=UserRole.admin,
                unit="系统管理",
            )
        )

    if settings.seed_demo_users:
        for username, display_name, role, unit in DEMO_USERS:
            if not db.query(User).filter(User.username == username).first():
                db.add(
                    User(
                        username=username,
                        password_hash=hash_password("Demo@123456"),
                        display_name=display_name,
                        role=role,
                        unit=unit,
                    )
                )

    db.commit()
