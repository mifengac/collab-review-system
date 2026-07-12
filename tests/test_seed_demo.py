"""SEED_DEMO_USERS=false 时不创建演示账号。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.database import Base
from app.models import User
from app.services.seed import seed_all


def test_seed_demo_users_false(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="crs_seed_")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp}/seed.db")
    monkeypatch.setenv("UPLOAD_DIR", str(Path(tmp) / "uploads"))
    monkeypatch.setenv("SECRET_KEY", "seed-test-secret")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "Admin@123456")
    monkeypatch.setenv("SEED_DEMO_USERS", "false")
    # 避免本地 .env 中 SEED_DEMO_USERS=true 干扰：显式用环境变量 + 清缓存
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.seed_demo_users is False, (
        "期望 SEED_DEMO_USERS=false；请确认环境变量优先于 .env"
    )

    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        seed_all(db)
        names = {u.username for u in db.query(User).all()}
        assert "admin" in names
        assert "handler1" not in names
        assert "leader_a" not in names
        assert "leader_b" not in names
    finally:
        db.close()
        engine.dispose()
        get_settings.cache_clear()
