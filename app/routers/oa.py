"""OA 对接预留接口：第一版返回 mock / 空数据。"""
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.database import get_db
from app.schemas import OAInboxItem, OASyncRequest, OASyncResponse

router = APIRouter(prefix="/api/oa", tags=["OA预留"])


@router.get("/inbox", response_model=list[OAInboxItem])
def oa_inbox(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    """模拟 OA 待办收件箱。后续替换为真实 OA 拉取。"""
    return [
        OAInboxItem(
            oa_flow_id="MOCK-FLOW-001",
            oa_step_id="STEP-10",
            oa_deal_index="1",
            title="【模拟】关于加强重点场所安全检查的通知",
            doc_no="公治〔2026〕12号",
            source_unit="市局治安支队",
            received_at=datetime.utcnow(),
        )
    ]


@router.post("/sync", response_model=OASyncResponse)
def oa_sync(
    body: OASyncRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    """OA 同步预留：当前不写库，返回提示。"""
    return OASyncResponse(
        success=True,
        message="OA 同步接口已预留，尚未对接真实 OA。force=" + str(body.force),
        imported=0,
        data=[],
    )
