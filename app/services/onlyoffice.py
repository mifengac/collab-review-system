"""ONLYOFFICE Document Server 对接：JWT、文档 key、下载令牌、回调落库。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import HTTPException
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Document, FileVersion, Item, ItemStatus, User
from app.services.files import MAX_SIZE, resolve_file_path, save_bytes_as_new_version
from app.services.permissions import LOCKED_STATUSES, can_upload_document, can_view_item

logger = logging.getLogger(__name__)

DOWNLOAD_PURPOSE = "oo_download"
DOWNLOAD_TOKEN_MINUTES = 15
OO_ALGORITHM = "HS256"


def is_onlyoffice_ready(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    if not s.onlyoffice_enabled:
        return False
    if not (s.onlyoffice_jwt_secret or "").strip():
        return False
    if not (s.onlyoffice_public_url or "").strip():
        return False
    if not (s.app_internal_url or "").strip():
        return False
    return True


def _strip_slash(url: str) -> str:
    return (url or "").rstrip("/")


def build_document_key(doc: Document, version: FileVersion) -> str:
    """版本变化时 key 必须变（ONLYOFFICE 缓存按 key）。"""
    sha = (version.sha256 or "")[:16]
    return f"crs-d{doc.id}-v{version.id}-{sha}"


def create_download_token(document_id: int, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    expire = datetime.utcnow() + timedelta(minutes=DOWNLOAD_TOKEN_MINUTES)
    payload = {
        "purpose": DOWNLOAD_PURPOSE,
        "document_id": document_id,
        "exp": expire,
    }
    return jwt.encode(payload, s.secret_key, algorithm=s.algorithm)


def verify_download_token(token: str, document_id: int, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    try:
        payload = jwt.decode(token, s.secret_key, algorithms=[s.algorithm])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="下载令牌无效或已过期") from exc
    if payload.get("purpose") != DOWNLOAD_PURPOSE:
        raise HTTPException(status_code=401, detail="下载令牌用途无效")
    if int(payload.get("document_id") or 0) != int(document_id):
        raise HTTPException(status_code=401, detail="下载令牌与文档不匹配")


def sign_onlyoffice_config(config: dict[str, Any], settings: Settings | None = None) -> str:
    s = settings or get_settings()
    secret = (s.onlyoffice_jwt_secret or "").strip()
    if not secret:
        raise HTTPException(status_code=500, detail="ONLYOFFICE JWT 未配置")
    return jwt.encode(config, secret, algorithm=OO_ALGORITHM)


def verify_onlyoffice_jwt(token: str, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    """校验 Document Server 回调带来的 JWT（payload 通常为回调 body）。"""
    s = get_settings()
    secret = (s.onlyoffice_jwt_secret or "").strip()
    if not secret:
        raise HTTPException(status_code=500, detail="ONLYOFFICE JWT 未配置")
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[OO_ALGORITHM],
            options={"verify_aud": False},
        )
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="ONLYOFFICE 令牌无效") from exc
    return payload if isinstance(payload, dict) else {}


def editor_js_url(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    base = _strip_slash(s.onlyoffice_public_url)
    return f"{base}/web-apps/apps/api/documents/api.js"


def build_editor_config(
    db: Session,
    doc: Document,
    item: Item,
    user: User,
    settings: Settings | None = None,
) -> dict[str, Any]:
    s = settings or get_settings()
    if not is_onlyoffice_ready(s):
        raise HTTPException(status_code=503, detail="在线编辑未启用或配置不完整")

    if not can_view_item(user, item):
        raise HTTPException(status_code=403, detail="无权查看该事项")

    version = (
        db.query(FileVersion)
        .filter(
            FileVersion.document_id == doc.id,
            FileVersion.version_no == doc.current_version,
        )
        .first()
    )
    if not version:
        raise HTTPException(status_code=400, detail="文档尚无可用版本，请先上传 docx")

    app_base = _strip_slash(s.app_internal_url)
    dl_token = create_download_token(doc.id, s)
    # 勿在日志中输出 token
    file_url = f"{app_base}/api/documents/{doc.id}/raw?token={dl_token}"
    callback_url = f"{app_base}/api/onlyoffice/callback?document_id={doc.id}"

    locked = item.status in LOCKED_STATUSES
    can_edit = (not locked) and can_upload_document(user, item)
    mode = "edit" if can_edit else "view"

    key = build_document_key(doc, version)
    config: dict[str, Any] = {
        "documentType": "word",
        "document": {
            "fileType": "docx",
            "key": key,
            "title": doc.name or version.original_filename or f"doc-{doc.id}.docx",
            "url": file_url,
            "permissions": {
                "edit": can_edit,
                "download": True,
                "print": True,
            },
        },
        "editorConfig": {
            "mode": mode,
            "lang": "zh-CN",
            "callbackUrl": callback_url,
            "user": {
                "id": str(user.id),
                "name": user.display_name or user.username,
            },
            "customization": {
                "forcesave": True,
            },
        },
    }
    token = sign_onlyoffice_config(config, s)
    config["token"] = token
    return {
        "document_id": doc.id,
        "mode": mode,
        "reserved": False,
        "message": "ONLYOFFICE 编辑配置已生成",
        "editor_url": editor_js_url(s),
        "config": config,
    }


def download_remote_file(url: str, *, timeout: float = 60.0) -> bytes:
    """从 Document Server 提供的 URL 拉取编辑后的文件。"""
    if not url or not str(url).startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="回调文件地址无效")
    try:
        with httpx.Client(timeout=timeout, verify=False, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            raw = resp.content
    except httpx.HTTPError as exc:
        logger.warning("ONLYOFFICE 下载编辑结果失败: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="无法从编辑服务下载文件") from exc
    if not raw:
        raise HTTPException(status_code=400, detail="编辑结果文件为空")
    if len(raw) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="编辑结果超过 50MB 限制")
    return raw


def resolve_callback_actor(
    db: Session,
    doc: Document,
    users_field: list | None,
) -> User:
    if users_field:
        for raw_id in users_field:
            try:
                uid = int(str(raw_id).strip())
            except (TypeError, ValueError):
                continue
            u = db.query(User).filter(User.id == uid, User.is_active.is_(True)).first()
            if u:
                return u
    # 回退：最近版本上传人
    last = (
        db.query(FileVersion)
        .filter(FileVersion.document_id == doc.id)
        .order_by(FileVersion.version_no.desc())
        .first()
    )
    if last:
        u = db.query(User).filter(User.id == last.uploader_id).first()
        if u:
            return u
    raise HTTPException(status_code=400, detail="无法确定保存操作人")


def handle_callback_save(
    db: Session,
    document_id: int,
    body: dict[str, Any],
) -> dict[str, Any]:
    """
    处理 status=2/6 等需保存的回调。
    返回 ONLYOFFICE 协议：{"error": 0} 成功，非 0 失败。
    """
    status = int(body.get("status") or 0)
    # 1=editing, 2=ready for save, 3=save error, 4=closed no changes,
    # 6=force save, 7=force save error
    if status not in (2, 6):
        return {"error": 0}

    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        return {"error": 1}

    item = db.query(Item).filter(Item.id == doc.item_id).first()
    if not item:
        return {"error": 1}

    if item.status in LOCKED_STATUSES:
        logger.info(
            "ONLYOFFICE 拒绝保存：事项终态 document_id=%s status=%s",
            document_id,
            item.status.value if isinstance(item.status, ItemStatus) else item.status,
        )
        return {"error": 1}

    url = body.get("url")
    if not url:
        logger.warning("ONLYOFFICE 回调 status=%s 但无 url document_id=%s", status, document_id)
        return {"error": 1}

    try:
        raw = download_remote_file(str(url))
        actor = resolve_callback_actor(db, doc, body.get("users"))
        filename = doc.name if (doc.name or "").lower().endswith(".docx") else f"{doc.name or 'edited'}.docx"
        if not filename.lower().endswith(".docx"):
            filename = f"{filename}.docx"
        save_bytes_as_new_version(
            db,
            item,
            actor,
            doc,
            raw,
            original_filename=filename,
            action_detail_prefix="onlyoffice保存",
        )
    except HTTPException as exc:
        logger.warning(
            "ONLYOFFICE 保存失败 document_id=%s detail=%s",
            document_id,
            getattr(exc, "detail", type(exc).__name__),
        )
        return {"error": 1}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ONLYOFFICE 保存异常 document_id=%s err=%s",
            document_id,
            type(exc).__name__,
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"error": 1}

    return {"error": 0}


def current_version_file_path(db: Session, doc: Document):
    version = (
        db.query(FileVersion)
        .filter(
            FileVersion.document_id == doc.id,
            FileVersion.version_no == doc.current_version,
        )
        .first()
    )
    if not version:
        raise HTTPException(status_code=404, detail="文档尚无版本")
    return version, resolve_file_path(version)
