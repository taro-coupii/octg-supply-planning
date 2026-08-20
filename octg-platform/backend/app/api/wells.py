import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    CoverageResult,
    DemandLine,
    DemandRevision,
    DemandRevisionSource,
    DemandStatus,
    Well,
)

router = APIRouter(prefix="/wells")


class WellListOut(BaseModel):
    id: str
    customer_id: str
    name: str
    demand_status: str
    line_count: int


class CoverageOut(BaseModel):
    verdict: str
    reason: str
    action: str | None
    covered_qty: float
    computed_at: datetime.datetime


class DemandLineDetailOut(BaseModel):
    id: str
    product_id: str
    quantity: float
    unit: str
    ros_date: datetime.date
    profile: str
    coverage: CoverageOut | None = None


class RevisionOut(BaseModel):
    id: str
    revision_no: int
    applied_at: datetime.datetime
    source: str
    summary: str


class WellDetailOut(BaseModel):
    id: str
    customer_id: str
    name: str
    demand_status: str
    lines: list[DemandLineDetailOut]
    revisions: list[RevisionOut]


class StatusChangeIn(BaseModel):
    status: str


@router.get("", response_model=list[WellListOut])
def list_wells(customer_id: str, db: Session = Depends(get_db)):
    wells = db.query(Well).filter_by(customer_id=customer_id).order_by(Well.name).all()
    result = []
    for w in wells:
        count = db.query(DemandLine).filter_by(well_id=w.id).count()
        result.append(
            WellListOut(
                id=w.id,
                customer_id=w.customer_id,
                name=w.name,
                demand_status=w.demand_status.value,
                line_count=count,
            )
        )
    return result


@router.get("/{well_id}", response_model=WellDetailOut)
def get_well(well_id: str, db: Session = Depends(get_db)):
    well = db.get(Well, well_id)
    if well is None:
        raise HTTPException(status_code=404, detail="Well not found")

    lines = db.query(DemandLine).filter_by(well_id=well.id).all()
    revisions = (
        db.query(DemandRevision)
        .filter_by(well_id=well.id)
        .order_by(DemandRevision.revision_no.desc())
        .all()
    )

    coverage_by_line_id = {
        cr.demand_line_id: cr
        for cr in db.query(CoverageResult)
        .filter(CoverageResult.demand_line_id.in_([line.id for line in lines]))
        .all()
    }

    return WellDetailOut(
        id=well.id,
        customer_id=well.customer_id,
        name=well.name,
        demand_status=well.demand_status.value,
        lines=[
            DemandLineDetailOut(
                id=line.id,
                product_id=line.product_id,
                quantity=line.quantity,
                unit=line.unit.value,
                ros_date=line.ros_date,
                profile=line.profile.value,
                coverage=(
                    CoverageOut(
                        verdict=coverage_by_line_id[line.id].verdict.value,
                        reason=coverage_by_line_id[line.id].reason,
                        action=coverage_by_line_id[line.id].action,
                        covered_qty=coverage_by_line_id[line.id].covered_qty,
                        computed_at=coverage_by_line_id[line.id].computed_at,
                    )
                    if line.id in coverage_by_line_id
                    else None
                ),
            )
            for line in lines
        ],
        revisions=[
            RevisionOut(
                id=r.id,
                revision_no=r.revision_no,
                applied_at=r.applied_at,
                source=r.source.value,
                summary=r.summary,
            )
            for r in revisions
        ],
    )


@router.post("/{well_id}/status", response_model=WellListOut)
def change_well_status(well_id: str, body: StatusChangeIn, db: Session = Depends(get_db)):
    well = db.get(Well, well_id)
    if well is None:
        raise HTTPException(status_code=404, detail="Well not found")

    try:
        new_status = DemandStatus(body.status)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid status: {body.status!r}")

    if new_status == well.demand_status:
        raise HTTPException(status_code=409, detail="status unchanged")

    old_status = well.demand_status
    well.demand_status = new_status

    max_rev = (
        db.query(DemandRevision)
        .filter_by(well_id=well.id)
        .order_by(DemandRevision.revision_no.desc())
        .first()
    )
    next_rev = (max_rev.revision_no + 1) if max_rev else 1
    db.add(
        DemandRevision(
            well_id=well.id,
            revision_no=next_rev,
            source=DemandRevisionSource.STATUS_CHANGE,
            summary=f"{old_status.value} → {new_status.value}",
        )
    )
    db.commit()

    count = db.query(DemandLine).filter_by(well_id=well.id).count()
    return WellListOut(
        id=well.id,
        customer_id=well.customer_id,
        name=well.name,
        demand_status=well.demand_status.value,
        line_count=count,
    )
