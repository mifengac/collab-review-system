"""启动自检：示例 SECRET_KEY 警告；生产拒绝 SEED_DEMO 演示账号。"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.database import Base
from app.models import User
from app.services.seed import seed_all
from app.services.startup_checks import INSECURE_SECRET_KEYS, check_production_secrets


def test_check_production_secrets_warns_on_default_key(caplog):
    s = Settings(
        debug=False,
        secret_key="dev-secret-change-me-in-production",
        database_url="sqlite:///:memory:",
    )
    with caplog.at_level(logging.WARNING):
        warnings = check_production_secrets(s)
    assert len(warnings) == 1
    assert "SECRET_KEY" in warnings[0]
    assert any("SECRET_KEY" in r.message for r in caplog.records)


def test_check_production_secrets_silent_when_debug_or_custom():
    s_debug = Settings(
        debug=True,
        secret_key="dev-secret-change-me-in-production",
        database_url="sqlite:///:memory:",
    )
    assert check_production_secrets(s_debug) == []

    s_ok = Settings(
        debug=False,
        secret_key="a-sufficiently-long-random-production-secret-key-9f3a",
        database_url="sqlite:///:memory:",
    )
    assert check_production_secrets(s_ok) == []
    assert s_ok.secret_key not in INSECURE_SECRET_KEYS


def test_seed_demo_refused_when_debug_false(monkeypatch, caplog):
    tmp = tempfile.mkdtemp(prefix="crs_seed_prod_")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp}/seed.db")
    monkeypatch.setenv("UPLOAD_DIR", str(Path(tmp) / "uploads"))
    monkeypatch.setenv("SECRET_KEY", "prod-like-secret-not-in-insecure-list")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "Admin@123456")
    monkeypatch.setenv("SEED_DEMO_USERS", "true")
    monkeypatch.setenv("DEBUG", "false")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.seed_demo_users is True
    assert settings.debug is False

    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        with caplog.at_level(logging.WARNING):
            seed_all(db)
        names = {u.username for u in db.query(User).all()}
        assert "admin" in names
        assert "handler1" not in names
        assert "leader_a" not in names
        assert any("拒绝创建演示账号" in r.message for r in caplog.records)
    finally:
        db.close()
        engine.dispose()
        get_settings.cache_clear()


def test_seed_demo_allowed_when_debug_true(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="crs_seed_dev_")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp}/seed.db")
    monkeypatch.setenv("UPLOAD_DIR", str(Path(tmp) / "uploads"))
    monkeypatch.setenv("SECRET_KEY", "dev-test-secret")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "Admin@123456")
    monkeypatch.setenv("SEED_DEMO_USERS", "true")
    monkeypatch.setenv("DEBUG", "true")
    get_settings.cache_clear()

    settings = get_settings()
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        seed_all(db)
        names = {u.username for u in db.query(User).all()}
        assert "admin" in names
        assert "handler1" in names
        assert "leader_a" in names
    finally:
        db.close()
        engine.dispose()
        get_settings.cache_clear()
