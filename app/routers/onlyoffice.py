"""ONLYOFFICE Document Server 回调。"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.schemas import OnlyOfficeCallbackOut
from app.services.onlyoffice import handle_callback_save, is_onlyoffice_ready, verify_onlyoffice_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/onlyoffice", tags=["ONLYOFFICE"])


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


@router.post("/callback", response_model=OnlyOfficeCallbackOut)
async def onlyoffice_callback(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    document_id: Annotated[int, Query(description="文档 ID")],
    authorization: Annotated[str | None, Header()] = None,
):
    """
    Document Server 保存回调。
    必须校验 JWT（Authorization Bearer 或 body.token）；日志不记录 token。
    """
    settings = get_settings()
    if not is_onlyoffice_ready(settings):
        raise HTTPException(status_code=503, detail="在线编辑未启用")

    try:
        body: dict[str, Any] = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="回调 body 无效") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="回调 body 无效")

    token = _extract_bearer(authorization) or (body.get("token") if isinstance(body.get("token"), str) else None)
    if not token:
        raise HTTPException(status_code=401, detail="缺少 ONLYOFFICE 令牌")

    # 校验 JWT，并以验签后的内容为权威数据源：
    # body.token 方式下 payload 即回调体本身；Authorization 头方式下回调体在 "payload" 键内。
    # 防止“拿旧的合法令牌 + 伪造 body（如恶意 url）”绕过校验。
    payload = verify_onlyoffice_jwt(token)
    inner = payload.get("payload")
    verified = inner if isinstance(inner, dict) else payload
    for field in ("status", "url", "users", "key"):
        if field in verified:
            body[field] = verified[field]
        else:
            body.pop(field, None)

    status = int(body.get("status") or 0)
    logger.info(
        "ONLYOFFICE callback document_id=%s status=%s has_url=%s",
        document_id,
        status,
        bool(body.get("url")),
    )

    result = handle_callback_save(db, document_id, body)
    return OnlyOfficeCallbackOut(error=int(result.get("error") or 0))
