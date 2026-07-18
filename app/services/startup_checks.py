"""启动时配置自检：只警告、不阻断（危险配置另有 RuntimeError）。"""
from __future__ import annotations

import logging

from app.config import Settings

logger = logging.getLogger(__name__)

# 代码默认值与 .env.example 中的示例密钥，生产不得继续使用
INSECURE_SECRET_KEYS = frozenset(
    {
        "dev-secret-change-me-in-production",
        "please-change-this-to-a-random-secret-key",
        "test-secret-key",
    }
)


def check_production_secrets(settings: Settings) -> list[str]:
    """
    返回警告文案列表（空表示无告警）。
    DEBUG=false 且 SECRET_KEY 仍是示例值时，打醒目警告，不阻断启动。
    """
    warnings: list[str] = []
    if not settings.debug:
        key = (settings.secret_key or "").strip()
        if not key or key in INSECURE_SECRET_KEYS:
            msg = (
                "【生产配置警告】DEBUG=false 但 SECRET_KEY 仍是示例/空值。"
                "请立即改为足够长的随机字符串，否则 JWT 可被伪造。"
            )
            logger.warning(msg)
            warnings.append(msg)
    return warnings
