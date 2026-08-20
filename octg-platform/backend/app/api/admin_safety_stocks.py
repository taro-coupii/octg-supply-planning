# C-01R resolved (stage 7): protected via app.main's include_router(dependencies=[...]).
# COMPROMISE[C-15R]: require_admin exists (app/auth/deps.py) but is deliberately
# not applied here — PLANNER can reach these routes (see docs/superpowers/COMPROMISES.md).
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import SafetyStock, UnitOfMeasure

router = APIRouter(prefix="/admin")

VALID_UNITS = {u.value for u in UnitOfMeasure}


class SafetyStockIn(BaseModel):
    business_unit_id: str
    product_id: str
    quantity: float | None = Field(default=None, ge=0)
    unit: str

    @field_validator("unit")
    @classmethod
    def _validate_unit(cls, unit: str) -> str:
        if unit not in VALID_UNITS:
            raise ValueError(f"unit must be one of {sorted(VALID_UNITS)}")
        return unit


class SafetyStockOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    business_unit_id: str
    product_id: str
    quantity: float
    unit: str


@router.get("/safety-stocks", response_model=list[SafetyStockOut])
def list_safety_stocks(db: Session = Depends(get_db)):
    return db.query(SafetyStock).all()


@router.put("/safety-stocks", response_model=list[SafetyStockOut])
def put_safety_stocks(payload: list[SafetyStockIn], db: Session = Depends(get_db)):
    for item in payload:
        existing = (
            db.query(SafetyStock)
            .filter_by(business_unit_id=item.business_unit_id, product_id=item.product_id)
            .first()
        )
        if item.quantity is None:
            # spec §不変条件2: PUT with quantity:null deletes the row (returns to unset)
            if existing is not None:
                db.delete(existing)
            continue

        if existing is None:
            db.add(
                SafetyStock(
                    business_unit_id=item.business_unit_id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                    unit=item.unit,
                )
            )
        else:
            existing.quantity = item.quantity
            existing.unit = item.unit

    db.commit()
    return db.query(SafetyStock).all()
