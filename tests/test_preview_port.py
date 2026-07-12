"""PREVIEW_PORT 配置与预览 compose 映射测试（不启动 Docker）。"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.preview.yml"
PREVIEW_UP = ROOT / "scripts" / "preview-up.sh"


def test_compose_uses_preview_port_variable():
    text = COMPOSE.read_text(encoding="utf-8")
    assert "${PREVIEW_PORT:-5010}:5009" in text
    assert re.search(r'^\s*-\s*"5010:5009"\s*$', text, re.M) is None


def test_preview_port_default_5010():
    # 模拟 compose 默认展开
    mapping = "${PREVIEW_PORT:-5010}:5009"
    env = {}
    port = env.get("PREVIEW_PORT") or "5010"
    expanded = mapping.replace("${PREVIEW_PORT:-5010}", port)
    assert expanded == "5010:5009"


def test_preview_port_custom_5020():
    mapping = "${PREVIEW_PORT:-5010}:5009"
    port = "5020"
    expanded = mapping.replace("${PREVIEW_PORT:-5010}", port)
    assert expanded == "5020:5009"


def test_preview_up_exports_preview_port():
    text = PREVIEW_UP.read_text(encoding="utf-8")
    assert "export PREVIEW_PORT=" in text
    assert "PREVIEW_PORT:-5010" in text
    assert "preview-smoke.py" in text
    # 不得把正式 5009 当作预览默认
    assert 'PREVIEW_PORT="${PREVIEW_PORT:-5009}"' not in text


def test_formal_port_not_overwritten_in_compose_prod():
    prod = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "5009" in prod
    assert "collab-review-preview" not in prod
