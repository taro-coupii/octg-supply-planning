# C-01R resolved (stage 7): protected via app.main's include_router(dependencies=[...]).
# COMPROMISE[C-15R]: require_admin exists (app/auth/deps.py) but is deliberately
# not applied here — PLANNER can reach these routes (see docs/superpowers/COMPROMISES.md).
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import BusinessUnit, Customer

router = APIRouter(prefix="/admin")


class BusinessUnitIn(BaseModel):
    name: str
    parent_id: str | None = None


class BusinessUnitPatch(BaseModel):
    name: str | None = None
    parent_id: str | None = None


class BusinessUnitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    parent_id: str | None


def _would_cycle(db: Session, bu_id: str, new_parent_id: str) -> bool:
    """Walk the parent chain of the proposed parent; True if bu_id appears in it."""
    current_id: str | None = new_parent_id
    while current_id is not None:
        if current_id == bu_id:
            return True
        current = db.query(BusinessUnit).filter_by(id=current_id).first()
        current_id = current.parent_id if current else None
    return False


@router.post("/business-units", response_model=BusinessUnitOut, status_code=201)
def create_business_unit(payload: BusinessUnitIn, db: Session = Depends(get_db)):
    bu = BusinessUnit(name=payload.name, parent_id=payload.parent_id)
    db.add(bu)
    db.commit()
    db.refresh(bu)
    return bu


@router.patch("/business-units/{bu_id}", response_model=BusinessUnitOut)
def update_business_unit(bu_id: str, payload: BusinessUnitPatch, db: Session = Depends(get_db)):
    bu = db.query(BusinessUnit).filter_by(id=bu_id).first()
    if bu is None:
        raise HTTPException(status_code=404, detail="Business unit not found")

    if payload.parent_id is not None and _would_cycle(db, bu_id, payload.parent_id):
        raise HTTPException(status_code=422, detail="Parent change would create a cycle")

    if payload.name is not None:
        bu.name = payload.name
    if "parent_id" in payload.model_fields_set:
        bu.parent_id = payload.parent_id

    db.commit()
    db.refresh(bu)
    return bu


@router.delete("/business-units/{bu_id}", status_code=204)
def delete_business_unit(bu_id: str, db: Session = Depends(get_db)):
    bu = db.query(BusinessUnit).filter_by(id=bu_id).first()
    if bu is None:
        raise HTTPException(status_code=404, detail="Business unit not found")

    if db.query(BusinessUnit).filter_by(parent_id=bu_id).first() is not None:
        raise HTTPException(status_code=409, detail="Business unit has children")
    if db.query(Customer).filter_by(business_unit_id=bu_id).first() is not None:
        raise HTTPException(status_code=409, detail="Business unit has customers")

    db.delete(bu)
    db.commit()
    return None
