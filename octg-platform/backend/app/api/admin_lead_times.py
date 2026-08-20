# C-01R resolved (stage 7): protected via app.main's include_router(dependencies=[...]).
# COMPROMISE[C-15R]: require_admin exists (app/auth/deps.py) but is deliberately
# not applied here — PLANNER can reach these routes (see docs/superpowers/COMPROMISES.md).
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import LeadTime

router = APIRouter(prefix="/admin")


class LeadTimeIn(BaseModel):
    business_unit_id: str | None = None
    product_id: str | None = None
    months: int = Field(gt=0)


class LeadTimeOut(LeadTimeIn):
    model_config = ConfigDict(from_attributes=True)

    id: str


@router.get("/lead-times", response_model=list[LeadTimeOut])
def list_lead_times(db: Session = Depends(get_db)):
    return db.query(LeadTime).all()


@router.put("/lead-times", response_model=list[LeadTimeOut])
def replace_lead_times(payload: list[LeadTimeIn], db: Session = Depends(get_db)):
    """Full-replace semantics: any existing row not present in payload is deleted."""
    seen: dict[tuple, int] = {}
    for idx, item in enumerate(payload, start=1):
        key = (item.business_unit_id, item.product_id)
        if key in seen:
            raise HTTPException(
                status_code=422,
                detail=f"duplicate (business_unit_id, product_id) at row {idx} (also row {seen[key]})",
            )
        seen[key] = idx

    db.query(LeadTime).delete()
    rows = [
        LeadTime(business_unit_id=item.business_unit_id, product_id=item.product_id, months=item.months)
        for item in payload
    ]
    db.add_all(rows)
    db.commit()
    return db.query(LeadTime).all()
