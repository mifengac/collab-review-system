from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.database import get_db
from app.models import BusinessTag, Department
from app.schemas import BusinessTagOut, DepartmentOut

router = APIRouter(prefix="/api/dict", tags=["字典"])


@router.get("/departments", response_model=list[DepartmentOut])
def departments(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    return (
        db.query(Department)
        .filter(Department.is_active.is_(True))
        .order_by(Department.sort_order, Department.id)
        .all()
    )


@router.get("/tags", response_model=list[BusinessTagOut])
def tags(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    return (
        db.query(BusinessTag)
        .filter(BusinessTag.is_active.is_(True))
        .order_by(BusinessTag.sort_order, BusinessTag.id)
        .all()
    )
