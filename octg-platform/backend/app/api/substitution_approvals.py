# C-01R resolved (stage 7): protected via app.main's include_router(dependencies=[...]).
"""spec §API rows 7-8: substitution approvals CRUD.

Decided rows are immutable — re-decide is 409 (spec §3-6, invariant 6). A
Rejected row must not block a fresh duplicate request — only Pending does.
"""

import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    Customer,
    CustomerSubstitutionRule,
    DemandLine,
    Product,
    SubstitutionApproval,
    SubstitutionApprovalStatus,
    TechnicalSubstitution,
    Well,
)

router = APIRouter(prefix="/substitution-approvals")


class ApprovalIn(BaseModel):
    demand_line_id: str
    technical_substitution_id: str


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_id: str
    well_id: str
    demand_line_id: str
    technical_substitution_id: str
    status: str
    customer_approved: bool
    well_approved: bool
    requested_at: datetime.datetime
    decided_at: datetime.datetime | None
    note: str | None

    @staticmethod
    def from_row(row: SubstitutionApproval) -> "ApprovalOut":
        return ApprovalOut(
            id=row.id,
            customer_id=row.customer_id,
            well_id=row.well_id,
            demand_line_id=row.demand_line_id,
            technical_substitution_id=row.technical_substitution_id,
            status=row.status.value,
            customer_approved=row.customer_approved,
            well_approved=row.well_approved,
            requested_at=row.requested_at,
            decided_at=row.decided_at,
            note=row.note,
        )


@router.post("", response_model=ApprovalOut, status_code=201)
def create_approval(payload: ApprovalIn, db: Session = Depends(get_db)):
    line = db.get(DemandLine, payload.demand_line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="Demand line not found")
    well = db.get(Well, line.well_id)
    customer = db.get(Customer, well.customer_id)
    ts = db.get(TechnicalSubstitution, payload.technical_substitution_id)
    if ts is None:
        raise HTTPException(status_code=404, detail="Substitution not found")

    rule = (
        db.query(CustomerSubstitutionRule)
        .filter_by(customer_id=customer.id, technical_substitution_id=ts.id)
        .first()
    )
    if rule is None or not rule.allowed:
        raise HTTPException(status_code=422, detail="Substitution not allowed for this customer")

    existing_pending = (
        db.query(SubstitutionApproval)
        .filter_by(
            demand_line_id=line.id,
            technical_substitution_id=ts.id,
            status=SubstitutionApprovalStatus.PENDING,
        )
        .first()
    )
    if existing_pending is not None:
        raise HTTPException(status_code=409, detail="A pending approval request already exists")

    approval = SubstitutionApproval(
        customer_id=customer.id,
        well_id=well.id,
        demand_line_id=line.id,
        technical_substitution_id=ts.id,
        status=SubstitutionApprovalStatus.PENDING,
        customer_approved=False,
        well_approved=False,
        requested_at=datetime.datetime.now(datetime.timezone.utc),
        decided_at=None,
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return ApprovalOut.from_row(approval)


class DecideIn(BaseModel):
    customer_approved: bool
    well_approved: bool
    note: str | None = None


@router.post("/{approval_id}/decide", response_model=ApprovalOut)
def decide_approval(approval_id: str, payload: DecideIn, db: Session = Depends(get_db)):
    approval = db.get(SubstitutionApproval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.status != SubstitutionApprovalStatus.PENDING:
        raise HTTPException(status_code=409, detail="Approval already decided")

    approval.customer_approved = payload.customer_approved
    approval.well_approved = payload.well_approved
    approval.note = payload.note
    approval.status = (
        SubstitutionApprovalStatus.APPROVED
        if payload.customer_approved and payload.well_approved
        else SubstitutionApprovalStatus.REJECTED
    )
    approval.decided_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()
    db.refresh(approval)
    return ApprovalOut.from_row(approval)


class ApprovalListOut(BaseModel):
    id: str
    status: str
    customer_id: str
    customer_name: str
    well_id: str
    well_name: str
    demand_line_id: str
    technical_substitution_id: str
    to_product_name: str
    customer_approved: bool
    well_approved: bool
    requested_at: datetime.datetime
    decided_at: datetime.datetime | None
    note: str | None


@router.get("", response_model=list[ApprovalListOut])
def list_approvals(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SubstitutionApproval)
    if status is not None:
        try:
            status_enum = SubstitutionApprovalStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid status")
        query = query.filter_by(status=status_enum)

    rows = query.all()
    out: list[ApprovalListOut] = []
    for row in rows:
        customer = db.get(Customer, row.customer_id)
        well = db.get(Well, row.well_id)
        ts = db.get(TechnicalSubstitution, row.technical_substitution_id)
        to_product = db.get(Product, ts.to_product_id) if ts else None
        out.append(
            ApprovalListOut(
                id=row.id,
                status=row.status.value,
                customer_id=row.customer_id,
                customer_name=customer.name if customer else "",
                well_id=row.well_id,
                well_name=well.name if well else "",
                demand_line_id=row.demand_line_id,
                technical_substitution_id=row.technical_substitution_id,
                to_product_name=to_product.name if to_product else "",
                customer_approved=row.customer_approved,
                well_approved=row.well_approved,
                requested_at=row.requested_at,
                decided_at=row.decided_at,
                note=row.note,
            )
        )
    return out
