from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from app.auth import CurrentUser
from app.database import get_db
from app.models import ActionType, Document, FileKind, FileVersion, Item
from app.schemas import DocumentOut, EditorConfigOut, FileVersionOut, MessageOut
from app.services.files import resolve_file_path, save_upload
from app.services.permissions import (
    ensure_can_download_document,
    ensure_can_upload_document,
    ensure_can_view_item,
)
from app.services.workflow import write_log

router = APIRouter(prefix="/api", tags=["文件与文档"])


@router.get("/items/{item_id}/documents", response_model=list[DocumentOut])
def list_documents(
    item_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="事项不存在")
    ensure_can_view_item(user, item)
    docs = (
        db.query(Document)
        .options(
            joinedload(Document.versions).joinedload(FileVersion.uploader),
        )
        .filter(Document.item_id == item_id)
        .order_by(Document.id)
        .all()
    )
    return docs


@router.post("/items/{item_id}/upload", response_model=DocumentOut)
async def upload_file(
    item_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
    kind: str = Form("attachment"),
    document_id: int | None = Form(None),
    document_name: str | None = Form(None),
):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="事项不存在")
    ensure_can_upload_document(user, item)
    if kind not in ("main", "attachment"):
        raise HTTPException(status_code=400, detail="kind 须为 main 或 attachment")
    file_kind = FileKind.main if kind == "main" else FileKind.attachment
    doc, _ver = await save_upload(
        db, item, user, file, file_kind, document_id=document_id, document_name=document_name
    )
    doc = (
        db.query(Document)
        .options(joinedload(Document.versions).joinedload(FileVersion.uploader))
        .filter(Document.id == doc.id)
        .first()
    )
    return doc


@router.get("/versions/{version_id}/download")
def download_version(
    version_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    ver = (
        db.query(FileVersion)
        .options(joinedload(FileVersion.document))
        .filter(FileVersion.id == version_id)
        .first()
    )
    if not ver:
        raise HTTPException(status_code=404, detail="版本不存在")
    item = db.query(Item).filter(Item.id == ver.document.item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="事项不存在")
    ensure_can_download_document(user, item)
    path = resolve_file_path(ver)
    write_log(
        db,
        item,
        user,
        ActionType.download,
        detail=f"下载 {ver.original_filename} v{ver.version_no}",
    )
    db.commit()
    return FileResponse(
        path,
        filename=ver.original_filename,
        media_type=ver.content_type or "application/octet-stream",
    )


@router.get("/documents/{document_id}/versions", response_model=list[FileVersionOut])
def list_versions(
    document_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    item = db.query(Item).filter(Item.id == doc.item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="事项不存在")
    ensure_can_view_item(user, item)
    vers = (
        db.query(FileVersion)
        .options(joinedload(FileVersion.uploader))
        .filter(FileVersion.document_id == document_id)
        .order_by(FileVersion.version_no.desc())
        .all()
    )
    return vers


# ---------- ONLYOFFICE 预留 ----------
@router.get("/documents/{document_id}/editor-config", response_model=EditorConfigOut)
def editor_config(
    document_id: int,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    item = db.query(Item).filter(Item.id == doc.item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="事项不存在")
    ensure_can_view_item(user, item)
    return EditorConfigOut(
        document_id=document_id,
        mode="view",
        reserved=True,
        message="在线编辑功能预留，后续可单独部署 ONLYOFFICE Docs 后接入",
        editor_url=None,
        config={
            "document": {
                "fileType": "docx",
                "key": f"doc-{document_id}-v{doc.current_version}",
                "title": doc.name,
                "url": None,
            },
            "editorConfig": {
                "mode": "view",
                "lang": "zh-CN",
                "callbackUrl": f"/api/office/callback/{document_id}",
            },
        },
    )


@router.post("/office/callback/{document_id}", response_model=MessageOut)
async def office_callback(
    document_id: int,
    db: Annotated[Session, Depends(get_db)],
):
    """ONLYOFFICE 回调预留：第一版仅返回 ok，不落库。"""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        return MessageOut(message="document not found (reserved)", detail={"error": 1})
    return MessageOut(message="ok", detail={"error": 0, "reserved": True})
