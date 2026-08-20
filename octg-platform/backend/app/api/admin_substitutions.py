# C-01R resolved (stage 7): protected via app.main's include_router(dependencies=[...]).
# COMPROMISE[C-15R]: require_admin exists (app/auth/deps.py) but is deliberately
# not applied here — PLANNER can reach these routes (see docs/superpowers/COMPROMISES.md).
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import CustomerSubstitutionRule, TechnicalSubstitution

router = APIRouter(prefix="/admin")


class SubstitutionIn(BaseModel):
    from_product_id: str
    to_product_id: str


class SubstitutionOut(SubstitutionIn):
    model_config = ConfigDict(from_attributes=True)

    id: str


class CustomerRuleIn(BaseModel):
    technical_substitution_id: str
    allowed: bool


class CustomerRuleOut(CustomerRuleIn):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_id: str


@router.get("/substitutions", response_model=list[SubstitutionOut])
def list_substitutions(db: Session = Depends(get_db)):
    return db.query(TechnicalSubstitution).all()


@router.post("/substitutions", response_model=SubstitutionOut, status_code=201)
def create_substitution(payload: SubstitutionIn, db: Session = Depends(get_db)):
    if payload.from_product_id == payload.to_product_id:
        raise HTTPException(status_code=422, detail="from and to must differ")

    existing = (
        db.query(TechnicalSubstitution)
        .filter_by(from_product_id=payload.from_product_id, to_product_id=payload.to_product_id)
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Substitution already exists")

    sub = TechnicalSubstitution(from_product_id=payload.from_product_id, to_product_id=payload.to_product_id)
    db.add(sub)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Substitution already exists")
    db.refresh(sub)
    return sub


@router.delete("/substitutions/{sub_id}", status_code=204)
def delete_substitution(sub_id: str, db: Session = Depends(get_db)):
    sub = db.query(TechnicalSubstitution).filter_by(id=sub_id).first()
    if sub is None:
        raise HTTPException(status_code=404, detail="Substitution not found")
    db.delete(sub)
    db.commit()
    return None


@router.get("/substitutions/customer-rules", response_model=list[CustomerRuleOut])
def get_customer_rules(customer_id: str, db: Session = Depends(get_db)):
    return db.query(CustomerSubstitutionRule).filter_by(customer_id=customer_id).all()


@router.put("/substitutions/customer-rules", response_model=list[CustomerRuleOut])
def put_customer_rules(customer_id: str, payload: list[CustomerRuleIn], db: Session = Depends(get_db)):
    for item in payload:
        existing = (
            db.query(CustomerSubstitutionRule)
            .filter_by(customer_id=customer_id, technical_substitution_id=item.technical_substitution_id)
            .first()
        )
        if existing is None:
            db.add(
                CustomerSubstitutionRule(
                    customer_id=customer_id,
                    technical_substitution_id=item.technical_substitution_id,
                    allowed=item.allowed,
                )
            )
        else:
            existing.allowed = item.allowed
    db.commit()
    return db.query(CustomerSubstitutionRule).filter_by(customer_id=customer_id).all()
