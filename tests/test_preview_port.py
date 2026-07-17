"""PREVIEW_PORT 配置与预览 compose 映射测试（不启动 Docker）。"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.preview.yml"
PREVIEW_UP = ROOT / "scripts" / "preview-up.sh"
# 容器内监听端口（与 Dockerfile / 正式 compose 一致）
APP_INTERNAL_PORT = "5002"
FORMAL_PORT = "5002"


def test_compose_uses_preview_port_variable():
    text = COMPOSE.read_text(encoding="utf-8")
    assert f"${{PREVIEW_PORT:-5010}}:{APP_INTERNAL_PORT}" in text
    assert re.search(rf'^\s*-\s*"5010:{APP_INTERNAL_PORT}"\s*$', text, re.M) is None


def test_preview_port_default_5010():
    # 模拟 compose 默认展开
    mapping = f"${{PREVIEW_PORT:-5010}}:{APP_INTERNAL_PORT}"
    env = {}
    port = env.get("PREVIEW_PORT") or "5010"
    expanded = mapping.replace("${PREVIEW_PORT:-5010}", port)
    assert expanded == f"5010:{APP_INTERNAL_PORT}"


def test_preview_port_custom_5020():
    mapping = f"${{PREVIEW_PORT:-5010}}:{APP_INTERNAL_PORT}"
    port = "5020"
    expanded = mapping.replace("${PREVIEW_PORT:-5010}", port)
    assert expanded == f"5020:{APP_INTERNAL_PORT}"


def test_preview_up_exports_preview_port():
    text = PREVIEW_UP.read_text(encoding="utf-8")
    assert "export PREVIEW_PORT=" in text
    assert "PREVIEW_PORT:-5010" in text
    assert "preview-smoke.py" in text
    # 不得把正式端口当作预览默认
    assert f'PREVIEW_PORT="${{PREVIEW_PORT:-{FORMAL_PORT}}}"' not in text


def test_formal_port_not_overwritten_in_compose_prod():
    prod = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert FORMAL_PORT in prod
    assert f":{FORMAL_PORT}" in prod or f"{FORMAL_PORT}:" in prod
    assert "collab-review-preview" not in prod
