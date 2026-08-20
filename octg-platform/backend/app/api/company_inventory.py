from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    BookingStatus,
    InventoryAssignment,
    InventoryOnHand,
    InventoryOnOrder,
    UnitOfMeasure,
)

router = APIRouter(prefix="/company-inventory")

VALID_UNITS = {u.value for u in UnitOfMeasure}


def _validate_unit(unit: str) -> str:
    if unit not in VALID_UNITS:
        raise ValueError(f"unit must be one of {sorted(VALID_UNITS)}")
    return unit


class OnHandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    business_unit_id: str
    product_id: str
    quantity: float
    unit: str
    source_system: str


class AssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    business_unit_id: str
    product_id: str
    customer_id: str
    quantity: float
    unit: str
    reference: str | None


class OnOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    business_unit_id: str
    product_id: str
    quantity: float
    unit: str
    expected_date: date | None
    booking_status: str

    @field_validator("booking_status", mode="before")
    @classmethod
    def _booking_status_value(cls, v):
        return v.value if isinstance(v, BookingStatus) else v


class CompanyInventoryOut(BaseModel):
    on_hand: list[OnHandOut]
    assignments: list[AssignmentOut]
    on_order: list[OnOrderOut]


class OnHandIn(BaseModel):
    business_unit_id: str
    product_id: str
    quantity: float = Field(ge=0)
    unit: str

    @field_validator("unit")
    @classmethod
    def _unit(cls, v):
        return _validate_unit(v)


class OnHandPatchIn(BaseModel):
    quantity: float = Field(ge=0)
    unit: str

    @field_validator("unit")
    @classmethod
    def _unit(cls, v):
        return _validate_unit(v)


@router.get("", response_model=CompanyInventoryOut)
def get_company_inventory(db: Session = Depends(get_db)):
    return CompanyInventoryOut(
        on_hand=db.query(InventoryOnHand).all(),
        assignments=db.query(InventoryAssignment).all(),
        on_order=db.query(InventoryOnOrder).all(),
    )


@router.post("/on-hand", response_model=OnHandOut, status_code=201)
def create_on_hand(payload: OnHandIn, db: Session = Depends(get_db)):
    row = InventoryOnHand(
        business_unit_id=payload.business_unit_id,
        product_id=payload.product_id,
        quantity=payload.quantity,
        unit=payload.unit,
        source_system="manual",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _get_manual_row_or_409(db: Session, on_hand_id: str) -> InventoryOnHand:
    row = db.query(InventoryOnHand).filter_by(id=on_hand_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="on-hand row not found")
    # COMPROMISE[C-03R]: company inventory is meant to be an Oracle read-only
    # projection (spec §データモデル), but this stage has no Oracle feed yet, so
    # writes are allowed only against manually-created rows. Any row whose
    # source_system=="oracle" is rejected with 409 to keep the platform from
    # silently diverging from the eventual read-only source of truth.
    if row.source_system == "oracle":
        raise HTTPException(
            status_code=409,
            detail="reason: rows sourced from Oracle (source_system=oracle) are read-only",
        )
    return row


@router.patch("/on-hand/{on_hand_id}", response_model=OnHandOut)
def patch_on_hand(on_hand_id: str, payload: OnHandPatchIn, db: Session = Depends(get_db)):
    row = _get_manual_row_or_409(db, on_hand_id)
    row.quantity = payload.quantity
    row.unit = payload.unit
    db.commit()
    db.refresh(row)
    return row


@router.delete("/on-hand/{on_hand_id}", status_code=204)
def delete_on_hand(on_hand_id: str, db: Session = Depends(get_db)):
    row = _get_manual_row_or_409(db, on_hand_id)
    db.delete(row)
    db.commit()
    return None
