"""文件存储与版本管理。"""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ActionType, Document, FileKind, FileVersion, Item, User, VersionKind
from app.services.workflow import write_log

# 允许的扩展名
MAIN_EXTS = {".docx"}
ATTACH_EXTS = {".docx", ".xlsx", ".pdf", ".jpg", ".jpeg", ".png"}
MAX_SIZE = 50 * 1024 * 1024  # 50MB


def _safe_name(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^\w.\u4e00-\u9fff\-]+", "_", name)
    return name[:200] or "file"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_file_content(ext: str, raw: bytes) -> None:
    """轻量魔数校验：内容与扩展名须匹配。"""
    ext = ext.lower()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")

    if ext in {".docx", ".xlsx"}:
        # OOXML 为 ZIP 包，以 PK 开头
        if not raw.startswith(b"PK"):
            raise HTTPException(status_code=400, detail="文件内容与扩展名不匹配")
    elif ext == ".pdf":
        if not raw.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="文件内容与扩展名不匹配")
    elif ext in {".jpg", ".jpeg"}:
        # JPEG SOI
        if not raw.startswith(b"\xff\xd8\xff"):
            raise HTTPException(status_code=400, detail="文件内容与扩展名不匹配")
    elif ext == ".png":
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(status_code=400, detail="文件内容与扩展名不匹配")


def ensure_upload_dir() -> Path:
    settings = get_settings()
    path = settings.upload_path
    path.mkdir(parents=True, exist_ok=True)
    return path


async def save_upload(
    db: Session,
    item: Item,
    actor: User,
    file: UploadFile,
    kind: FileKind,
    document_id: int | None = None,
    document_name: str | None = None,
) -> tuple[Document, FileVersion]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名无效")

    ext = Path(file.filename).suffix.lower()
    allowed = MAIN_EXTS if kind == FileKind.main else ATTACH_EXTS
    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {ext}，允许：{', '.join(sorted(allowed))}",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(raw) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="文件超过 50MB 限制")

    validate_file_content(ext, raw)

    sha = _sha256_bytes(raw)
    original = _safe_name(file.filename)
    upload_root = ensure_upload_dir()
    item_dir = upload_root / str(item.id)
    item_dir.mkdir(parents=True, exist_ok=True)

    if document_id:
        doc = db.query(Document).filter(Document.id == document_id, Document.item_id == item.id).first()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")
    else:
        name = document_name or original
        if kind == FileKind.main:
            # 每个事项仅一个主材料文档槽，新版本挂在同一 document 下
            doc = (
                db.query(Document)
                .filter(Document.item_id == item.id, Document.kind == FileKind.main)
                .first()
            )
            if not doc:
                doc = Document(item_id=item.id, name=name, kind=FileKind.main, current_version=0)
                db.add(doc)
                db.flush()
            else:
                doc.name = name
        else:
            doc = Document(
                item_id=item.id,
                name=name,
                kind=FileKind.attachment,
                current_version=0,
            )
            db.add(doc)
            db.flush()

    next_ver = (doc.current_version or 0) + 1
    stored_name = f"{doc.id}_v{next_ver}_{uuid.uuid4().hex[:8]}{ext}"
    stored_path = item_dir / stored_name
    stored_path.write_bytes(raw)

    rel_path = str(Path(str(item.id)) / stored_name)
    version = FileVersion(
        document_id=doc.id,
        version_no=next_ver,
        original_filename=original,
        stored_path=rel_path,
        content_type=file.content_type,
        file_size=len(raw),
        sha256=sha,
        version_kind=VersionKind.normal,
        uploader_id=actor.id,
    )
    doc.current_version = next_ver
    db.add(version)
    db.flush()

    write_log(
        db,
        item,
        actor,
        ActionType.upload,
        comment=None,
        detail=f"{kind.value} {original} v{next_ver} ({len(raw)} bytes, sha256={sha[:12]}…)",
    )
    db.commit()
    db.refresh(doc)
    db.refresh(version)
    return doc, version


def resolve_file_path(version: FileVersion) -> Path:
    root = ensure_upload_dir()
    path = root / version.stored_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件实体不存在")
    return path


def save_bytes_as_new_version(
    db: Session,
    item: Item,
    actor: User,
    doc: Document,
    raw: bytes,
    *,
    original_filename: str,
    content_type: str | None = None,
    action_detail_prefix: str = "onlyoffice",
    version_kind: VersionKind = VersionKind.normal,
) -> FileVersion:
    """将字节内容写入文档的新版本（不可覆盖历史）。供 ONLYOFFICE 回调等使用。"""
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(raw) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="文件超过 50MB 限制")

    original = _safe_name(original_filename or "edited.docx")
    ext = Path(original).suffix.lower() or ".docx"
    if ext != ".docx":
        raise HTTPException(status_code=400, detail="在线编辑仅支持 docx")
    validate_file_content(ext, raw)

    sha = _sha256_bytes(raw)
    upload_root = ensure_upload_dir()
    item_dir = upload_root / str(item.id)
    item_dir.mkdir(parents=True, exist_ok=True)

    next_ver = (doc.current_version or 0) + 1
    stored_name = f"{doc.id}_v{next_ver}_{uuid.uuid4().hex[:8]}{ext}"
    stored_path = item_dir / stored_name
    stored_path.write_bytes(raw)

    rel_path = str(Path(str(item.id)) / stored_name)
    version = FileVersion(
        document_id=doc.id,
        version_no=next_ver,
        original_filename=original,
        stored_path=rel_path,
        content_type=content_type
        or "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=len(raw),
        sha256=sha,
        version_kind=version_kind or VersionKind.normal,
        uploader_id=actor.id,
    )
    doc.current_version = next_ver
    db.add(version)
    db.flush()

    kind_label = {
        VersionKind.normal: "",
        VersionKind.marked: "[痕迹存档] ",
        VersionKind.final: "[终稿] ",
    }.get(version.version_kind, "")
    write_log(
        db,
        item,
        actor,
        ActionType.upload,
        comment=None,
        detail=(
            f"{kind_label}{action_detail_prefix} {original} v{next_ver} "
            f"({len(raw)} bytes, sha256={sha[:12]}…)"
        ),
    )
    db.commit()
    db.refresh(doc)
    db.refresh(version)
    return version
